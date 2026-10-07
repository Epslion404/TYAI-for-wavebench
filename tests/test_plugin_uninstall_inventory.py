"""Native pip regressions: derived deletion paths and incomplete installed RECORD."""

import csv
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from zipfile import ZipFile

import pytest

from test_plugin_lifecycle import _plugin_wheel, _target_venv
from wavebench.errors import ConfigError
from wavebench.plugins.lifecycle import PluginLifecycle
from wavebench.plugins.package_inspect import inspect_plugin_wheel


@pytest.mark.skipif(os.name != "nt", reason="Windows pip legacy launcher deletion paths")
@pytest.mark.parametrize("foreign_record", [False, True], ids=["unmanaged", "owned"])
@pytest.mark.parametrize("operation", ["upgrade", "install"])
def test_native_failed_upgrade_preserves_unrecorded_pip_deletion_paths(
    tmp_path, monkeypatch, foreign_record, operation,
):
    manager = PluginLifecycle(python_executable=_target_venv(tmp_path))
    first = _plugin_wheel(tmp_path, version="0.1.0")
    broken = _plugin_wheel(tmp_path, version="0.2.0", broken_descriptor=True, script_entries=(
        "\n[console_scripts]\nnewtool = wavebench_example_scope:descriptor\n"
        "derived.py = wavebench_example_scope:descriptor\n"
        "[gui_scripts]\nnewgui = wavebench_example_scope:descriptor\n"
    ))
    if operation == "upgrade":
        manager.install(first)
    else:
        manager._write_json(manager.ledger_path, manager.empty_ledger())
    ledger_before = manager.ledger_path.read_bytes()
    targets = [Path(manager.environment().scripts) / name for name in (
        "newtool-script.py", "derived.py.exe", "newgui-script.pyw",
    )]
    payload = b"# unrelated user file\n"
    pip_install = manager._pip_install
    calls = []

    def install_then_publish_foreign_files(wheel):
        calls.append(wheel)
        pip_install(wheel)
        if len(calls) == 1:
            for target in targets:
                target.write_bytes(payload)
            if foreign_record:
                info = Path(manager.environment().purelib) / "foreign_tools-1.0.dist-info"
                info.mkdir()
                (info / "METADATA").write_text("Name: foreign-tools\nVersion: 1.0\n")
                with (info / "RECORD").open("w", newline="") as handle:
                    csv.writer(handle).writerows(
                        (os.path.relpath(target, manager.environment().purelib).upper(), "", "")
                        for target in targets
                    )

    monkeypatch.setattr(manager, "_pip_install", install_then_publish_foreign_files)
    with pytest.raises(ConfigError) as error:
        getattr(manager, operation)(broken)
    # Before the fix pip's successful rollback silently erased these files and
    # reported the old plugin healthy. Check bytes before checking the error.
    assert all(target.is_file() and target.read_bytes() == payload for target in targets)
    assert "recovery required" in str(error.value)
    assert "descriptor" in str(error.value)
    assert len(calls) == 1
    assert manager.ledger_path.read_bytes() == ledger_before
    journal_before = manager.journal_path.read_bytes()
    assert json.loads(journal_before)["stage"] == "pip_finished"
    with pytest.raises(ConfigError, match="manual inspection"):
        manager.recover()
    assert manager.journal_path.read_bytes() == journal_before


@pytest.mark.parametrize("missing_module", [
    "wavebench_example_scope/__init__.py", "wavebench_example_scope/unused.py",
    "pure_payload.py", "plat_payload.py",
], ids=["descriptor", "ordinary", "purelib", "platlib"])
def test_native_rollback_rejects_record_missing_an_existing_ordinary_module(
    tmp_path, monkeypatch, missing_module,
):
    manager = PluginLifecycle(python_executable=_target_venv(tmp_path))
    members = {"wavebench_example_scope/unused.py": b"value = 'ordinary'\n"}
    if missing_module in ("pure_payload.py", "plat_payload.py"):
        members.update({
            "wavebench_example_scope-0.1.0.data/purelib/pure_payload.py": b"value = 'purelib'\n",
            "wavebench_example_scope-0.1.0.data/platlib/plat_payload.py": b"value = 'platlib'\n",
        })
    first = _plugin_wheel(tmp_path, version="0.1.0", extra_members=members)
    broken = _plugin_wheel(tmp_path, version="0.2.0", broken_descriptor=True)
    manager.install(first)
    ledger_before = manager.ledger_path.read_bytes()
    pip_install = manager._pip_install
    calls = []
    module = Path(manager.environment().purelib) / missing_module

    def install_then_omit_module(wheel):
        calls.append(wheel)
        pip_install(wheel)
        if len(calls) == 2:
            record = (Path(manager.environment().purelib)
                      / "wavebench_example_scope-0.1.0.dist-info" / "RECORD")
            rows = list(csv.reader(io.StringIO(record.read_text(encoding="utf-8"))))
            with record.open("w", encoding="utf-8", newline="") as handle:
                csv.writer(handle).writerows(
                    row for row in rows if row[0] != missing_module
                )

    monkeypatch.setattr(manager, "_pip_install", install_then_omit_module)
    with pytest.raises(ConfigError) as error:
        manager.upgrade(broken)
    # The old tree accepted this as healthy; a subsequent real uninstall left
    # __init__.py behind because pip could no longer find it in RECORD.
    status = manager.info("example.scope").status
    if status == "healthy":
        manager.remove("example.scope")
    assert status != "healthy", f"incomplete RECORD accepted; residual module={module.exists()}"
    assert "recovery required" in str(error.value)
    assert module.is_file()
    assert manager.ledger_path.read_bytes() == ledger_before
    journal_before = manager.journal_path.read_bytes()
    assert json.loads(journal_before)["stage"] == "rollback_started"
    record = (Path(manager.environment().purelib)
              / "wavebench_example_scope-0.1.0.dist-info" / "RECORD")
    incomplete_record = record.read_bytes()
    if missing_module == "wavebench_example_scope/unused.py":
        foreign = Path(manager.environment().purelib) / "foreign_payload-1.0.dist-info"
        foreign.mkdir()
        (foreign / "METADATA").write_text("Name: foreign-payload\nVersion: 1.0\n")
        (foreign / "RECORD").write_text(f"{missing_module},,\n")
    with pytest.raises(ConfigError, match="manual inspection"):
        manager.recover()
    assert manager.journal_path.read_bytes() == journal_before
    assert record.read_bytes() == incomplete_record


def test_wheel_inventory_uses_distinct_target_purelib_and_platlib(tmp_path, monkeypatch):
    import pip._internal.locations
    from wavebench.plugins.install_inventory import wheel_installation_paths

    purelib = tmp_path / "pure"
    platlib = tmp_path / "platform"
    monkeypatch.setattr(pip._internal.locations, "get_scheme", lambda name: SimpleNamespace(
        purelib=str(purelib), platlib=str(platlib), scripts=str(tmp_path / "bin"),
    ))
    members = {
        "wavebench_example_scope-0.1.0.data/purelib/pure_payload.py": b"pure = True\n",
        "wavebench_example_scope-0.1.0.data/platlib/plat_payload.py": b"platform = True\n",
    }
    wheel = _plugin_wheel(tmp_path, version="0.1.0", extra_members=members)
    with ZipFile(wheel, "a") as archive:
        for directory in ("wavebench_example_scope-0.1.0.data/",
                          "wavebench_example_scope-0.1.0.data/purelib/",
                          "wavebench_example_scope-0.1.0.data/platlib/"):
            archive.writestr(directory, b"")
    inspect_plugin_wheel(wheel)
    inventory = wheel_installation_paths(wheel, "wavebench-example-scope")
    assert inventory[next(iter(members))] == str(purelib / "pure_payload.py")
    assert inventory[list(members)[1]] == str(platlib / "plat_payload.py")
    assert inventory["wavebench_example_scope/__init__.py"] == str(
        purelib / "wavebench_example_scope" / "__init__.py"
    )


def test_wheel_inventory_rejects_alias_after_data_relocation(tmp_path, monkeypatch):
    import pip._internal.locations
    from wavebench.plugins.install_inventory import wheel_installation_paths

    library = str(tmp_path / "lib")
    monkeypatch.setattr(pip._internal.locations, "get_scheme", lambda name: SimpleNamespace(
        purelib=library, platlib=library, scripts=str(tmp_path / "bin"),
    ))
    wheel = _plugin_wheel(tmp_path, version="0.1.0", extra_members={
        "wavebench_example_scope-0.1.0.data/platlib/wavebench_example_scope/__init__.py": b"alias\n",
    })
    with pytest.raises(ConfigError, match="alias"):
        wheel_installation_paths(wheel, "wavebench-example-scope")


@pytest.mark.parametrize("member", [
    "wavebench_example_scope-0.1.0.data/purelib/wavebench/__init__.py",
    "wavebench_example_scope-0.1.0.data/platlib/untrusted.pth",
])
def test_library_data_relocation_preserves_core_and_pth_guards(tmp_path, member):
    wheel = _plugin_wheel(tmp_path, version="0.1.0", extra_members={member: b"untrusted\n"})
    with pytest.raises(ConfigError, match="core package|pth"):
        inspect_plugin_wheel(wheel)


@pytest.mark.skipif(os.name != "nt", reason="Windows pip legacy launcher deletion paths")
def test_native_healthy_generated_scripts_and_mutation_guards(tmp_path, monkeypatch):
    manager = PluginLifecycle(python_executable=_target_venv(tmp_path))
    entries = (
        "\n[console_scripts]\nnewtool = wavebench_example_scope:descriptor\n"
        "derived.py = wavebench_example_scope:descriptor\n"
        "[gui_scripts]\nnewgui = wavebench_example_scope:descriptor\n"
    )
    wheels = {version: _plugin_wheel(tmp_path, version=version, script_entries=entries)
              for version in ("0.1.0", "0.2.0", "0.3.0")}
    manager.install(wheels["0.2.0"])
    assert manager.info("example.scope").status == "healthy"
    scripts = Path(manager.environment().scripts)
    assert all((scripts / name).is_file() for name in ("newtool.exe", "derived.exe", "newgui.exe"))
    ledger_before = manager.ledger_path.read_bytes()
    foreign = scripts / "newtool-script.py"
    foreign.write_bytes(b"# unrelated legacy-named script\n")
    install = Mock(wraps=manager._pip_install)
    uninstall = Mock(wraps=manager._pip_uninstall)
    monkeypatch.setattr(manager, "_pip_install", install)
    monkeypatch.setattr(manager, "_pip_uninstall", uninstall)
    for operation, target in (("remove", "example.scope"), ("upgrade", wheels["0.3.0"]),
                              ("downgrade", wheels["0.1.0"])):
        for dry_run in (True, False):
            with pytest.raises(ConfigError, match="unmanaged file absent from RECORD"):
                getattr(manager, operation)(target, dry_run=dry_run)
            assert manager.ledger_path.read_bytes() == ledger_before
            assert not manager.journal_path.exists()
            assert foreign.read_bytes() == b"# unrelated legacy-named script\n"
    install.assert_not_called()
    uninstall.assert_not_called()
    foreign.unlink()
    manager.upgrade(wheels["0.3.0"])
    assert manager.info("example.scope").status == "healthy"
    manager.remove("example.scope")
    assert all(not (scripts / name).exists() for name in ("newtool.exe", "derived.exe", "newgui.exe"))
