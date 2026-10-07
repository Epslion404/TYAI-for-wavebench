"""Rollback must recheck files that pip removes, including new-version-only files."""

import ntpath
from unittest.mock import Mock

import pytest

from wavebench.errors import ConfigError
from wavebench.plugins import install_paths
from wavebench.plugins.lifecycle import EnvironmentInfo, PluginLifecycle
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
