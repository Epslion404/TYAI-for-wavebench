"""Installation and removal inventories evaluated inside the target interpreter."""

import os
from pathlib import Path
import stat
from types import SimpleNamespace
from zipfile import ZipFile

from wavebench.errors import ConfigError

from .install_paths import installation_path_key


def pip_uninstall_paths(distribution: str, library_paths: list[str]) -> tuple[str, ...]:
    """Return the full removal scope, including compressed directories."""
    inventory = pip_uninstall_inventory(distribution, library_paths)
    return tuple(sorted(set(inventory["files"]) | set(inventory["directories"])))


def pip_uninstall_inventory(
    distribution: str, library_paths: list[str],
) -> dict[str, tuple[str, ...]]:
    """Ask this pip for its final removal scope without touching files.

    RECORD alone omits legacy launchers and Python cache paths that pip derives.
    remove() compresses those paths again, sometimes deleting whole directories
    with unrecorded empty children. Expand that actual rename scope and prove
    every directory is an ancestor of a declared file, then let the caller check
    ownership of both files and directories. Private API failures fail closed.
    """
    from pip._internal.metadata import get_environment
    from pip._internal.req.req_uninstall import UninstallPathSet, compact, compress_for_rename

    matches = [dist for dist in get_environment(library_paths).iter_all_distributions()
               if dist.canonical_name == distribution]
    if len(matches) != 1 or not matches[0].installed_with_dist_info:
        raise ConfigError("pip uninstall distribution is missing, ambiguous or not a wheel install")
    plan = UninstallPathSet.from_dist(matches[0])
    if (plan is None or not isinstance(plan._paths, set)
            or any(not isinstance(path, str) or not path for path in plan._paths)
            or not isinstance(plan._pth, dict) or not isinstance(plan._refuse, set)):
        raise ConfigError("pip uninstall returned an invalid removal plan")
    if plan._pth or plan._refuse:
        raise ConfigError("pip uninstall inventory includes unsupported or external paths")
    if any(Path(path).is_dir() and not Path(path).is_symlink() for path in plan._paths):
        raise ConfigError("pip uninstall inventory unexpectedly includes a directory")
    location = matches[0].location
    entries = matches[0].iter_declared_entries()
    if not location or entries is None:
        raise ConfigError("pip uninstall RECORD cannot be read")
    declared_directories = set()
    for entry in entries:
        target = Path(os.path.abspath(os.path.join(location, entry)))
        # Inferring a directory permits only that directory itself. Every child
        # is checked separately; siblings are never attributed to a parent file.
        declared_directories.update(installation_path_key(str(parent)) for parent in target.parents)
    file_keys = {installation_path_key(path) for path in plan._paths}
    compressed = compress_for_rename(plan._paths)
    if not isinstance(compressed, set) or any(
        not isinstance(path, str) or not path for path in compressed
    ):
        raise ConfigError("pip uninstall returned an invalid compressed removal plan")
    rename_paths = compact(compressed)
    if not isinstance(rename_paths, set) or any(
        not isinstance(path, str) or not path for path in rename_paths
    ) or (plan._paths and not rename_paths):
        raise ConfigError("pip uninstall returned an invalid compressed removal plan")
    directories = set()
    files = set(plan._paths)

    def is_reparse(path):
        # lstat detects Windows junctions on Python 3.11 as well as 3.12;
        # os.path.isjunction is only available starting with Python 3.12.
        information = os.lstat(path)
        return stat.S_ISLNK(information.st_mode) or bool(
            getattr(information, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        )

    def check_directory(path):
        target = Path(path)
        for component in (target, *target.parents):
            if is_reparse(component):
                raise ConfigError(f"pip compressed removal traverses a symlink or junction directory: {component}")
        if installation_path_key(str(target)) not in declared_directories:
            raise ConfigError(f"pip compressed removal would delete an unmanaged directory absent from RECORD: {target}")
        directories.add(str(target))

    def walk_error(error):
        raise ConfigError(f"pip compressed removal directory cannot be read: {error}") from error

    for rename_path in rename_paths:
        target = Path(os.path.normpath(rename_path))
        if is_reparse(target):
            raise ConfigError(f"pip compressed removal includes a symlink or junction: {target}")
        if not target.is_dir():
            if installation_path_key(str(target)) not in file_keys:
                raise ConfigError(f"pip compressed removal includes an unexpected file: {target}")
            continue
        check_directory(target)
        for root, children, filenames in os.walk(target, followlinks=False, onerror=walk_error):
            for child in children:
                check_directory(Path(root) / child)
            for filename in filenames:
                path = Path(root) / filename
                if path.is_symlink() or installation_path_key(str(path)) not in file_keys:
                    raise ConfigError(f"pip compressed removal includes an unproven file: {path}")
                files.add(str(path))
    return {"files": tuple(sorted(files)), "directories": tuple(sorted(directories))}


def wheel_installation_paths(wheel: Path, distribution: str) -> dict[str, str]:
    """Map verified archive members and pip-generated launchers to target paths.

    RECORD is replaced by the installer; ordinary members keep their bytes.
    Only purelib/platlib relocation is supported by plugin admission. The scheme
    and launcher filenames come from the pip that will perform the installation.
    """
    from pip._internal.locations import get_scheme
    from pip._internal.metadata import FilesystemWheel, get_wheel_distribution
    from pip._internal.operations.install.wheel import (
        PipScriptMaker,
        get_console_script_specs,
        parse_wheel,
        wheel_root_is_purelib,
    )

    scheme = get_scheme(distribution)
    mapped = {}
    seen = set()

    def add(member, target):
        target = os.path.abspath(target)
        key = installation_path_key(target)
        if key in seen:
            raise ConfigError(f"wheel installation paths alias each other: {member}")
        seen.add(key)
        mapped[member] = target

    with ZipFile(wheel) as archive:
        _info_dir, metadata = parse_wheel(archive, distribution)
        library = scheme.purelib if wheel_root_is_purelib(metadata) else scheme.platlib
        for member in archive.namelist():
            if member.endswith("/"):
                continue
            parts = member.split("/")
            if parts[0].endswith(".data"):
                if len(parts) < 3 or parts[1] not in ("purelib", "platlib"):
                    raise ConfigError(f"unsupported wheel data relocation: {member}")
                target = os.path.join(getattr(scheme, parts[1]), *parts[2:])
            else:
                target = os.path.join(library, *parts)
            add(member, target)
        dist = get_wheel_distribution(FilesystemWheel(str(wheel)), distribution)
        console = {}
        gui = {}
        for entry in dist.iter_entry_points():
            if entry.group == "console_scripts":
                console[entry.name] = entry.value
            elif entry.group == "gui_scripts":
                gui[entry.name] = entry.value
        maker = PipScriptMaker(None, scheme.scripts)
        maker.clobber = True
        maker.variants = {""}
        maker.set_mode = True
        captured = {}
        maker._fileop = SimpleNamespace(
            write_binary_file=lambda path, data: captured.__setitem__(path, data),
            set_executable_mode=lambda paths: None,
        )
        for specification, options in (
            [(spec, {}) for spec in get_console_script_specs(console)]
            + [(f"{name} = {value}", {"gui": True}) for name, value in gui.items()]
        ):
            captured.clear()
            maker.make(specification, options)
            if not captured:
                raise ConfigError(f"pip generated no launcher for: {specification}")
            for target in captured:
                add(f"generated:{os.path.basename(target)}", target)
    return mapped
