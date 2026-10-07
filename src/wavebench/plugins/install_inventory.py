"""Installation and removal inventories evaluated inside the target interpreter."""

import os
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZipFile

from wavebench.errors import ConfigError

from .install_paths import installation_path_key


def pip_uninstall_paths(distribution: str, library_paths: list[str]) -> tuple[str, ...]:
    """Ask this pip for its removal plan without running remove or touching files.

    RECORD alone omits legacy launchers and Python cache paths that pip derives.
    Use pip's own plan so these rules follow the target pip rather than a version
    guessed by the calling WaveBench interpreter. Private API failures fail closed.
    """
    from pip._internal.metadata import get_environment
    from pip._internal.req.req_uninstall import UninstallPathSet

    matches = [dist for dist in get_environment(library_paths).iter_all_distributions()
               if dist.canonical_name == distribution]
    if len(matches) != 1 or not matches[0].installed_with_dist_info:
        raise ConfigError("pip uninstall distribution is missing, ambiguous or not a wheel install")
    plan = UninstallPathSet.from_dist(matches[0])
    if plan._pth or plan._refuse:
        raise ConfigError("pip uninstall inventory includes unsupported or external paths")
    if any(Path(path).is_dir() and not Path(path).is_symlink() for path in plan._paths):
        raise ConfigError("pip uninstall inventory unexpectedly includes a directory")
    return tuple(sorted(plan._paths))


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
