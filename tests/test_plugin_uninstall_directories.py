"""pip's compressed removal plan must preserve unrelated directories."""

import csv
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from test_plugin_lifecycle import _plugin_wheel, _target_venv
from wavebench.errors import ConfigError
from wavebench.plugins.lifecycle import PluginLifecycle
from wavebench.plugins.install_inventory import pip_uninstall_inventory, pip_uninstall_paths


@pytest.fixture
def removal_plan(tmp_path, monkeypatch):
    import pip._internal.metadata
    from pip._internal.req.req_uninstall import UninstallPathSet

    package = tmp_path / "plugin"
    cache = package / "__pycache__"
    cache.mkdir(parents=True)
    module = package / "module.py"
    module.write_bytes(b"value = True\n")
    generated_cache = cache / "module.cpython-312.pyc"
    generated_cache.write_bytes(b"generated cache\n")
    dist = SimpleNamespace(
        canonical_name="plugin", installed_with_dist_info=True, location=str(tmp_path),
        iter_declared_entries=lambda: [os.path.relpath(path, tmp_path) for path in (module, generated_cache)],
    )
    plan = SimpleNamespace(_paths={str(module), str(generated_cache)}, _pth={}, _refuse=set())
    monkeypatch.setattr(pip._internal.metadata, "get_environment", lambda paths: SimpleNamespace(
        iter_all_distributions=lambda: [dist],
    ))
    monkeypatch.setattr(UninstallPathSet, "from_dist", lambda dist: plan)
    return package, module, generated_cache, plan


def test_compressed_scope_includes_known_directories_and_empty_cache(removal_plan, tmp_path):
    package, module, generated_cache, plan = removal_plan
    inventory = pip_uninstall_inventory("plugin", [str(tmp_path)])
    assert set(inventory["files"]) == {str(module), str(generated_cache)}
    assert set(inventory["directories"]) == {str(package), str(package / "__pycache__")}
    generated_cache.unlink()
    plan._paths.remove(str(generated_cache))
    inventory = pip_uninstall_inventory("plugin", [str(tmp_path)])
    assert set(inventory["directories"]) == {str(package), str(package / "__pycache__")}


@pytest.mark.parametrize("inside_cache", [False, True])
def test_compressed_scope_refuses_unproven_empty_children(removal_plan, tmp_path, inside_cache):
    package, _module, _cache, _plan = removal_plan
    parent = package / "__pycache__" if inside_cache else package
    foreign = parent / "foreign_empty"
    foreign.mkdir()
    with pytest.raises(ConfigError, match="unmanaged directory"):
        pip_uninstall_paths("plugin", [str(tmp_path)])
    assert foreign.is_dir()


@pytest.mark.parametrize("damage", [
    "explicit-directory", "no-plan", "no-compressed-plan", "no-compacted-plan", "empty-compacted-plan",
])
def test_invalid_pip_plans_fail_closed(removal_plan, tmp_path, monkeypatch, damage):
    import pip._internal.req.req_uninstall as uninstall

    package, _module, _cache, plan = removal_plan
    if damage == "explicit-directory":
        plan._paths.add(str(package))
    elif damage == "no-plan":
        monkeypatch.setattr(uninstall.UninstallPathSet, "from_dist", lambda dist: None)
    elif damage == "no-compressed-plan":
        monkeypatch.setattr(uninstall, "compress_for_rename", lambda paths: None)
    elif damage == "no-compacted-plan":
        monkeypatch.setattr(uninstall, "compact", lambda paths: None)
    else:
        monkeypatch.setattr(uninstall, "compact", lambda paths: set())
    with pytest.raises(ConfigError, match="directory|invalid"):
        pip_uninstall_inventory("plugin", [str(tmp_path)])
    assert package.is_dir()


def test_compressed_scope_rechecks_children_after_plan_calculation(removal_plan, tmp_path, monkeypatch):
    import pip._internal.req.req_uninstall as uninstall

    package, _module, _cache, _plan = removal_plan
    compact = uninstall.compact

    def publish_file(paths):
        result = compact(paths)
        (package / "foreign.txt").write_bytes(b"foreign")
        return result

    monkeypatch.setattr(uninstall, "compact", publish_file)
    with pytest.raises(ConfigError, match="unproven file"):
        pip_uninstall_inventory("plugin", [str(tmp_path)])
    assert (package / "foreign.txt").read_bytes() == b"foreign"


@pytest.mark.skipif(os.name != "nt", reason="Windows junction directories")
def test_compressed_scope_refuses_empty_junction(removal_plan, tmp_path):
    package, _module, _cache, _plan = removal_plan
    target = tmp_path / "outside"
    target.mkdir()
    junction = package / "foreign_junction"
    result = subprocess.run(["cmd", "/c", "mklink", "/J", str(junction), str(target)],
                            capture_output=True)
    assert result.returncode == 0, result.stderr
    try:
        with pytest.raises(ConfigError, match="symlink or junction"):
            pip_uninstall_inventory("plugin", [str(tmp_path)])
        assert target.is_dir()
        assert junction.is_dir()
    finally:
        junction.rmdir()


def _foreign_directory(manager, *, owned):
    library = Path(manager.environment().purelib)
    directory = library / "wavebench_example_scope" / "foreign_empty_directory"
    directory.mkdir()
    if owned:
        metadata = library / "foreign_empty-1.0.dist-info"
        metadata.mkdir()
        (metadata / "METADATA").write_text("Name: foreign-empty\nVersion: 1.0\n")
        with (metadata / "RECORD").open("w", newline="") as handle:
            relative = os.path.relpath(directory, library)
            csv.writer(handle).writerow((relative.upper() if os.name == "nt" else relative, "", ""))
    return directory


@pytest.mark.parametrize("owned", [True, False], ids=["foreign-owned", "unmanaged"])
def test_native_compressed_remove_preserves_empty_directories(tmp_path, monkeypatch, owned):
    manager = PluginLifecycle(python_executable=_target_venv(tmp_path))
    manager.install(_plugin_wheel(tmp_path, version="0.2.0"))
    directory = _foreign_directory(manager, owned=owned)
    ledger_before = manager.ledger_path.read_bytes()
    uninstall = Mock(wraps=manager._pip_uninstall)
    monkeypatch.setattr(manager, "_pip_uninstall", uninstall)
    # On 613fd916 remove succeeded and recursively erased this empty directory.
    try:
        manager.remove("example.scope")
    except ConfigError:
        pass
    assert directory.is_dir(), "pip compressed removal erased an unrelated empty directory"
    uninstall.assert_not_called()
    assert manager.ledger_path.read_bytes() == ledger_before
    assert not manager.journal_path.exists()
    # Both replacement directions and dry-run must check the same current scope.
    install = Mock(wraps=manager._pip_install)
    monkeypatch.setattr(manager, "_pip_install", install)
    for operation, target in (("remove", "example.scope"),
                              ("upgrade", _plugin_wheel(tmp_path, version="0.3.0")),
                              ("downgrade", _plugin_wheel(tmp_path, version="0.1.0"))):
        for dry_run in (False, True):
            with pytest.raises(ConfigError, match="directory|owned by another"):
                getattr(manager, operation)(target, dry_run=dry_run)
            assert directory.is_dir()
            assert manager.ledger_path.read_bytes() == ledger_before
            assert not manager.journal_path.exists()
    install.assert_not_called()
    uninstall.assert_not_called()
    directory.rmdir()
    manager.remove("example.scope")


@pytest.mark.parametrize("operation", ["install", "upgrade"])
def test_native_compressed_rollback_preserves_empty_directory(tmp_path, monkeypatch, operation):
    manager = PluginLifecycle(python_executable=_target_venv(tmp_path))
    if operation == "upgrade":
        manager.install(_plugin_wheel(tmp_path, version="0.1.0"))
    else:
        manager._write_json(manager.ledger_path, manager.empty_ledger())
    ledger_before = manager.ledger_path.read_bytes()
    broken = _plugin_wheel(tmp_path, version="0.2.0", broken_descriptor=True)
    pip_install = manager._pip_install
    calls = []
    created = []

    def publish_directory(wheel):
        calls.append(wheel)
        pip_install(wheel)
        if len(calls) == 1:
            created.append(_foreign_directory(manager, owned=True))

    monkeypatch.setattr(manager, "_pip_install", publish_directory)
    uninstall = Mock(wraps=manager._pip_uninstall)
    monkeypatch.setattr(manager, "_pip_uninstall", uninstall)
    with pytest.raises(ConfigError, match="recovery required") as error:
        getattr(manager, operation)(broken)
    assert "descriptor" in str(error.value)
    assert created[0].is_dir()
    assert len(calls) == 1
    uninstall.assert_not_called()
    assert manager.ledger_path.read_bytes() == ledger_before
    journal = manager.journal_path.read_bytes()
    assert b'"pip_finished"' in journal
    with pytest.raises(ConfigError, match="manual inspection"):
        manager.recover()
    assert manager.journal_path.read_bytes() == journal


def test_native_normal_compressed_lifecycle_and_partial_removal(tmp_path):
    manager = PluginLifecycle(python_executable=_target_venv(tmp_path))
    entries = "\n[console_scripts]\nnewtool = wavebench_example_scope:descriptor\n"
    first = _plugin_wheel(tmp_path, version="0.1.0", script_entries=entries)
    second = _plugin_wheel(tmp_path, version="0.2.0", script_entries=entries)
    broken = _plugin_wheel(tmp_path, version="0.3.0", broken_descriptor=True, script_entries=entries)
    manager.install(first)
    manager.upgrade(second)
    with pytest.raises(ConfigError, match="postflight failed") as error:
        manager.upgrade(broken)
    assert "recovery required" not in str(error.value)
    assert manager.info("example.scope").status == "healthy"
    assert not manager.journal_path.exists()
    # A known cache directory remains attributable even when its generated file
    # is absent. An unrelated nonempty subtree prevents compression and remains.
    package = Path(manager.environment().purelib) / "wavebench_example_scope"
    for cache in (package / "__pycache__").glob("*.pyc"):
        cache.unlink()
    manager.remove("example.scope")
    assert not package.exists()
    manager.install(first)
    cache = package / "__pycache__"
    sentinel = cache / "foreign.cpython-312.pyc"
    sentinel.write_bytes(b"foreign cache")
    unmanaged = package / "unmanaged.txt"
    unmanaged.write_bytes(b"foreign ordinary file")
    foreign_empty = package / "foreign_empty_directory"
    foreign_empty.mkdir()
    # Here pip removes individual known files, leaving the nonempty package
    # root and its empty foreign directory outside the actual removal scope.
    manager.remove("example.scope")
    assert sentinel.read_bytes() == b"foreign cache"
    assert unmanaged.read_bytes() == b"foreign ordinary file"
    assert foreign_empty.is_dir()


@pytest.mark.skipif(os.name != "nt", reason="Windows junction ownership aliases")
def test_native_compressed_directory_owner_alias_and_path_boundary(tmp_path):
    manager = PluginLifecycle(python_executable=_target_venv(tmp_path))
    manager.install(_plugin_wheel(tmp_path, version="0.1.0"))
    library = Path(manager.environment().purelib)
    package = library / "wavebench_example_scope"
    metadata = library / "foreign_directory-1.0.dist-info"
    metadata.mkdir()
    (metadata / "METADATA").write_text("Name: foreign-directory\nVersion: 1.0\n")
    record = metadata / "RECORD"
    # The package root is an inferred ordinary-file ancestor, but its foreign
    # directory owner must still prevent the whole-directory rename.
    record.write_text("WAVEBENCH_EXAMPLE_SCOPE,,\n")
    ledger = manager.ledger_path.read_bytes()
    with pytest.raises(ConfigError, match="owned by another distribution"):
        manager.remove("example.scope")
    assert package.is_dir()
    assert manager.ledger_path.read_bytes() == ledger
    assert not manager.journal_path.exists()
    alias = library / "foreign_alias"
    result = subprocess.run(["cmd", "/c", "mklink", "/J", str(alias), str(package)],
                            capture_output=True)
    assert result.returncode == 0, result.stderr
    try:
        record.write_text("foreign_alias,,\n")
        with pytest.raises(ConfigError, match="owned by another distribution"):
            manager.remove("example.scope")
        assert package.is_dir()
        assert manager.ledger_path.read_bytes() == ledger
        assert not manager.journal_path.exists()
    finally:
        alias.rmdir()
    # A similar lexical prefix is not a descendant of the removed directory.
    sibling = library / "wavebench_example_scope_other"
    sibling.mkdir()
    record.write_text("wavebench_example_scope_other,,\n")
    manager.remove("example.scope")
    assert sibling.is_dir()


@pytest.mark.skipif(os.name != "nt", reason="Windows junction directories")
def test_native_compressed_empty_junction_refused(tmp_path):
    manager = PluginLifecycle(python_executable=_target_venv(tmp_path))
    manager.install(_plugin_wheel(tmp_path, version="0.1.0"))
    package = Path(manager.environment().purelib) / "wavebench_example_scope"
    target = tmp_path / "unrelated_directory"
    target.mkdir()
    junction = package / "foreign_junction"
    result = subprocess.run(["cmd", "/c", "mklink", "/J", str(junction), str(target)],
                            capture_output=True)
    assert result.returncode == 0, result.stderr
    ledger = manager.ledger_path.read_bytes()
    try:
        with pytest.raises(ConfigError, match="symlink or junction"):
            manager.remove("example.scope")
        assert target.is_dir()
        assert junction.is_dir()
        assert manager.ledger_path.read_bytes() == ledger
        assert not manager.journal_path.exists()
    finally:
        junction.rmdir()
    manager.remove("example.scope")
