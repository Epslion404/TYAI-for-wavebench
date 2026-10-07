"""Rollback must recheck files that pip removes, including new-version-only files."""

import ntpath
from unittest.mock import Mock

import pytest

from wavebench.errors import ConfigError
from wavebench.plugins import install_paths
from wavebench.plugins.lifecycle import EnvironmentInfo, InstalledPlugin, PluginLifecycle
from test_plugin_lifecycle import _plugin_wheel
from wavebench.plugins.package_inspect import inspect_plugin_wheel


def _manager(tmp_path):
    manager = PluginLifecycle(python_executable=tmp_path / "python")
    manager._environment = EnvironmentInfo(
        python=str(tmp_path / "python"), prefix=str(tmp_path), base_prefix=str(tmp_path.parent),
        purelib=str(tmp_path / "lib"), platlib=str(tmp_path / "lib"), version="3.11.0",
        fingerprint="test-environment", scripts=str(tmp_path / "bin"),
    )
    return manager


@pytest.mark.parametrize("operation", ["uninstall", "reinstall"])
@pytest.mark.parametrize("inventory_kind", ["shared", "ambiguous", "unreadable"])
def test_rollback_checks_current_files_before_pip(tmp_path, monkeypatch, operation, inventory_kind):
    monkeypatch.setattr(install_paths, "_path", ntpath)
    manager = _manager(tmp_path)
    wheel = _plugin_wheel(tmp_path, version="0.1.0")
    package = inspect_plugin_wheel(wheel)
    record = {"distribution": package.distribution, "wheel_sha256": package.sha256}
    # This file belongs only to the failed new version, not the rollback wheel.
    rows = ({"files": ["Plugin/new-only.txt"]},)
    if inventory_kind == "ambiguous":
        rows = rows + rows
    inventory = Mock(return_value={package.normalized_distribution: rows})
    if inventory_kind == "unreadable":
        inventory.side_effect = ConfigError("inventory unavailable")
    monkeypatch.setattr(manager, "_inventory", inventory)
    monkeypatch.setattr(manager, "_file_owners", Mock(return_value={
        "Plugin/new-only.txt": (package.normalized_distribution,),
        "PLUGIN/NEW-ONLY.TXT": ("foreign-plugin",),
    }))
    pip_install = Mock()
    pip_uninstall = Mock()
    monkeypatch.setattr(manager, "_pip_install", pip_install)
    monkeypatch.setattr(manager, "_pip_uninstall", pip_uninstall)
    manager._write_json(manager.ledger_path, {"unchanged": True})
    manager._write_json(manager.journal_path, {"stage": "pip_finished"})
    before = manager.ledger_path.read_bytes(), manager.journal_path.read_bytes()
    with pytest.raises(ConfigError, match="recovery required"):
        if operation == "uninstall":
            manager._rollback_uninstall(record, {}, {}, RuntimeError("postflight"))
        else:
            manager._rollback_install(wheel, record, {}, {}, RuntimeError("postflight"))
    pip_install.assert_not_called()
    pip_uninstall.assert_not_called()
    assert (manager.ledger_path.read_bytes(), manager.journal_path.read_bytes()) == before


def test_failed_install_without_distribution_can_finish_rollback(tmp_path, monkeypatch):
    manager = _manager(tmp_path)
    monkeypatch.setattr(manager, "_inventory", Mock(return_value={}))
    monkeypatch.setattr(manager, "_distribution_inventory", Mock(return_value=()))
    uninstall = Mock()
    monkeypatch.setattr(manager, "_pip_uninstall", uninstall)
    ledger = {"unchanged": True}
    with pytest.raises(ConfigError, match="postflight failed") as error:
        manager._rollback_uninstall({"distribution": "missing-plugin"}, ledger, {}, RuntimeError())
    assert "recovery required" not in str(error.value)
    uninstall.assert_called_once_with("missing-plugin", allow_missing=True)
    assert manager._read_json(manager.ledger_path, "ledger") == ledger
    assert not manager.journal_path.exists()


@pytest.mark.parametrize("operation", ["install", "upgrade", "downgrade", "remove"])
def test_dry_run_rejects_pending_transaction_without_mutation(tmp_path, monkeypatch, operation):
    manager = _manager(tmp_path)
    package = inspect_plugin_wheel(_plugin_wheel(tmp_path, version="0.2.0"))
    record = manager._package_record(package, manager._cache_wheel(package))
    ledger = manager.empty_ledger() if operation == "install" else manager._with_record(
        manager.empty_ledger(), record,
    )
    target = _plugin_wheel(tmp_path, version="0.1.0" if operation == "downgrade" else "0.3.0")
    manager._write_json(manager.ledger_path, ledger)
    manager._write_json(manager.journal_path, {"stage": "pip_finished"})
    before = manager.ledger_path.read_bytes(), manager.journal_path.read_bytes()
    monkeypatch.setattr(manager, "info", lambda driver: InstalledPlugin(
        driver, package.distribution, package.version, "healthy",
    ))
    monkeypatch.setattr(manager, "_file_owners", lambda **kwargs: {})
    pip = Mock()
    monkeypatch.setattr(manager, "_pip_install", pip)
    monkeypatch.setattr(manager, "_pip_uninstall", pip)
    with pytest.raises(ConfigError, match="recover"):
        getattr(manager, operation)("example.scope" if operation == "remove" else target, dry_run=True)
    pip.assert_not_called()
    assert (manager.ledger_path.read_bytes(), manager.journal_path.read_bytes()) == before


@pytest.mark.parametrize("generation", [True, -1, "1", 1.0, None])
def test_invalid_ledger_generation_is_rejected(tmp_path, generation):
    manager = _manager(tmp_path)
    ledger = manager.empty_ledger()
    ledger["generation"] = generation
    manager._write_json(manager.ledger_path, ledger)
    before = manager.ledger_path.read_bytes()
    with pytest.raises(ConfigError, match="generation"):
        manager._load_ledger(manager.environment())
    assert manager.ledger_path.read_bytes() == before


@pytest.mark.parametrize("damage", ["generation", "environment", "missing-before", "install-with-before"])
def test_recover_validates_before_ledger_before_probing_or_writing(tmp_path, monkeypatch, damage):
    manager = _manager(tmp_path)
    package = inspect_plugin_wheel(_plugin_wheel(tmp_path, version="0.1.0"))
    record = manager._package_record(package, manager._cache_wheel(package))
    before = manager._with_record(manager.empty_ledger(), record)
    operation = "remove"
    if damage == "generation":
        before["generation"] = True
    elif damage == "environment":
        before["environment"] = {"fingerprint": "foreign"}
    elif damage == "missing-before":
        before["plugins"] = {}
    else:
        operation = "install"
    journal = manager._journal(environment=manager.environment(), operation=operation,
                               stage="pip_finished", before_ledger=before, package=record)
    manager._write_json(manager.ledger_path, manager.empty_ledger())
    manager._write_json(manager.journal_path, journal)
    original = manager.ledger_path.read_bytes(), manager.journal_path.read_bytes()
    probe = Mock()
    monkeypatch.setattr(manager, "_record_matches_environment", probe)
    monkeypatch.setattr(manager, "_distribution_absent", probe)
    with pytest.raises(ConfigError):
        manager.recover()
    probe.assert_not_called()
    assert (manager.ledger_path.read_bytes(), manager.journal_path.read_bytes()) == original


def test_short_cache_paths_still_check_full_digest_and_clean_failed_copy(tmp_path, monkeypatch):
    manager = _manager(tmp_path)
    package = inspect_plugin_wheel(_plugin_wheel(tmp_path, version="0.1.0"))
    cached = manager._cache_wheel(package)
    cached.write_bytes(b"corrupt cache")
    assert manager._cache_wheel(package).read_bytes() == package.wheel_path.read_bytes()
    assert cached.name == package.wheel_path.name
    cached.unlink()
    package.wheel_path.write_bytes(b"changed after inspection")
    with pytest.raises(ConfigError, match="hash mismatch"):
        manager._cache_wheel(package)
    assert not cached.exists()
    assert list(cached.parent.iterdir()) == []
