"""Rollback verifies reinstalled launchers before adopting their new digests."""

from copy import deepcopy
import csv
from base64 import urlsafe_b64encode
from hashlib import sha256
import io
import json
import os
from pathlib import Path
from unittest.mock import Mock, call
from zipfile import ZipFile

import pytest

from test_plugin_lifecycle import _plugin_wheel, _target_venv
from wavebench.errors import ConfigError
from wavebench.plugins import lifecycle as lifecycle_module
from wavebench.plugins.lifecycle import EnvironmentInfo, PluginLifecycle
from wavebench.plugins.package_inspect import inspect_plugin_wheel


@pytest.fixture
def rollback_state(tmp_path, monkeypatch):
    manager = PluginLifecycle(python_executable=tmp_path / "python")
    manager._environment = EnvironmentInfo(
        python=str(tmp_path / "python"),
        prefix=str(tmp_path),
        base_prefix=str(tmp_path.parent),
        purelib=str(tmp_path / "lib"),
        platlib=str(tmp_path / "lib"),
        version="3.11.0",
        fingerprint="test-environment",
        scripts=str(tmp_path / "bin"),
    )
    wheel = _plugin_wheel(tmp_path, version="0.1.0")
    package = inspect_plugin_wheel(wheel)
    cached = manager._cache_wheel(package)
    record = manager._package_record(package, cached)
    record["installed_record_sha256"] = "old-record"
    record["installed_files_sha256"] = "old-files"
    ledger = manager._with_record(manager.empty_ledger(), record)
    ledger["plugins"]["unrelated-plugin"] = {"preserved": "untouched"}
    journal = manager._journal(
        environment=manager.environment(),
        operation="remove",
        stage="pip_finished",
        before_ledger=ledger,
        package=record,
    )
    manager._write_json(manager.ledger_path, ledger)
    manager._write_json(manager.journal_path, journal)
    monkeypatch.setattr(manager, "_file_owners", Mock(return_value={}))
    monkeypatch.setattr(manager, "_pip_install", Mock())
    monkeypatch.setattr(manager, "_postflight", Mock(return_value={
        "record_sha256": "new-record", "files_sha256": "new-files",
    }))
    monkeypatch.setattr(manager, "_assert_distribution_file_ownership", Mock())
    return manager, cached, record, ledger, journal


def test_rollback_adopts_verified_digests_without_changing_wheel_identity(rollback_state):
    manager, wheel, record, ledger, journal = rollback_state
    original = deepcopy(ledger)

    with pytest.raises(ConfigError, match="plugin postflight failed") as error:
        manager._rollback_install(wheel, record, ledger, journal, RuntimeError("remove failed"))

    assert "recovery required" not in str(error.value)
    manager._postflight.assert_called_once_with(record, rollback_reinstall=True)
    manager._assert_distribution_file_ownership.assert_has_calls([
        call(record["distribution"], allow_missing=True),
        call(record["distribution"]),
    ])
    restored = manager._read_json(manager.ledger_path, "ledger")
    expected = deepcopy(original)
    expected["generation"] += 1
    expected["plugins"][record["normalized_distribution"]].update(
        installed_record_sha256="new-record", installed_files_sha256="new-files",
    )
    assert restored == expected
    assert ledger == original
    assert journal["before_ledger"] == restored
    assert not manager.journal_path.exists()


@pytest.mark.parametrize("failure", ["postflight", "ownership"])
def test_rollback_validation_failure_keeps_ledger_and_journal(rollback_state, failure):
    manager, wheel, record, ledger, journal = rollback_state
    before = manager.ledger_path.read_bytes()
    probe = (manager._postflight if failure == "postflight"
             else manager._assert_distribution_file_ownership)
    probe.side_effect = ConfigError("validation failed")

    with pytest.raises(ConfigError, match="recovery required"):
        manager._rollback_install(wheel, record, ledger, journal, RuntimeError("remove failed"))

    assert manager.ledger_path.read_bytes() == before
    pending = manager._read_json(manager.journal_path, "journal")
    assert pending["stage"] == ("pip_finished" if failure == "ownership" else "rollback_started")
    assert pending["before_ledger"] == ledger


def test_rollback_rejects_a_different_valid_wheel_before_pip(rollback_state, tmp_path):
    manager, _wheel, record, ledger, journal = rollback_state
    other = _plugin_wheel(tmp_path, version="0.2.0")

    with pytest.raises(ConfigError, match="recovery required") as error:
        manager._rollback_install(other, record, ledger, journal, RuntimeError("remove failed"))

    assert "rollback wheel" in str(error.value.__cause__)
    manager._pip_install.assert_not_called()
    manager._postflight.assert_not_called()
    assert manager._read_json(manager.ledger_path, "ledger") == ledger


@pytest.mark.parametrize("boundary", ["verified_journal", "ledger", "journal_removal"])
def test_recover_finishes_interrupted_verified_rollback(rollback_state, monkeypatch, boundary):
    manager, wheel, record, ledger, journal = rollback_state
    write_json = manager._write_json
    remove_journal = manager._remove_journal

    def interrupted_write(path, payload):
        if ((boundary == "verified_journal" and path == manager.journal_path
             and payload["before_ledger"] != ledger)
                or (boundary == "ledger" and path == manager.ledger_path)):
            raise OSError("simulated interruption")
        write_json(path, payload)

    def interrupted_remove():
        raise OSError("simulated interruption")

    monkeypatch.setattr(manager, "_write_json", interrupted_write)
    if boundary == "journal_removal":
        monkeypatch.setattr(manager, "_remove_journal", interrupted_remove)
    with pytest.raises(ConfigError, match="recovery required"):
        manager._rollback_install(wheel, record, ledger, journal, RuntimeError("remove failed"))

    monkeypatch.setattr(manager, "_write_json", write_json)
    monkeypatch.setattr(manager, "_remove_journal", remove_journal)
    monkeypatch.setattr(manager, "_distribution_absent", lambda _record: False)
    monkeypatch.setattr(manager, "_record_matches_environment", lambda candidate, **kwargs:
                        candidate["installed_record_sha256"] == "new-record")
    assert manager.recover().status == "recovered-after-rollback"
    restored = manager._read_json(manager.ledger_path, "ledger")
    assert restored["generation"] == ledger["generation"] + 1
    assert restored["plugins"][record["normalized_distribution"]][
        "installed_record_sha256"] == "new-record"
    assert restored["plugins"]["unrelated-plugin"] == ledger["plugins"]["unrelated-plugin"]
    assert not manager.journal_path.exists()


def test_postflight_requires_prior_record_digest_except_during_rollback(rollback_state, monkeypatch):
    manager, _wheel, record, _ledger, _journal = rollback_state
    monkeypatch.delattr(manager, "_postflight")
    run_json = Mock(return_value={})
    monkeypatch.setattr(manager, "_run_json", run_json)

    manager._postflight(record)
    assert run_json.call_args.args[0][-2] == "old-record"
    manager._postflight(record, rollback_reinstall=True)
    assert run_json.call_args.args[0][-2] == ""


def test_installer_api_that_emits_no_launcher_fails_closed(tmp_path, monkeypatch):
    from pip._internal.operations.install.wheel import PipScriptMaker

    wheel = _plugin_wheel(tmp_path, version="0.1.0", script_entries=(
        "\n[console_scripts]\nexample-tool = wavebench_example_scope:descriptor\n"
    ))
    monkeypatch.setattr(PipScriptMaker, "make", lambda *args, **kwargs: [])
    with pytest.raises(ConfigError, match="did not generate"):
        lifecycle_module._verify_generated_launchers(wheel, str(tmp_path), str(tmp_path), set())


@pytest.mark.parametrize("damage", ["omitted", "rehashed"])
def test_recover_rejects_launcher_damage_hidden_by_record(rollback_state, tmp_path, monkeypatch, damage):
    from pip._internal.operations.install.wheel import PipScriptMaker

    manager, _wheel, _record, _ledger, _journal = rollback_state
    wheel = _plugin_wheel(tmp_path, version="0.1.0", script_entries=(
        "\n[console_scripts]\nexample-tool = wavebench_example_scope:descriptor\n"
    ))
    package = inspect_plugin_wheel(wheel)
    record = manager._package_record(package, manager._cache_wheel(package))
    record.update(installed_record_sha256="old-record", installed_files_sha256="old-files")
    ledger = manager._with_record(manager.empty_ledger(), record)
    journal = manager._journal(environment=manager.environment(), operation="remove",
                               stage="rollback_started", before_ledger=ledger, package=record)
    manager._write_json(manager.ledger_path, ledger)
    manager._write_json(manager.journal_path, journal)
    scripts = Path(manager.environment().scripts)
    scripts.mkdir()
    maker = PipScriptMaker(None, str(scripts))
    maker.variants = {""}
    launcher = Path(maker.make("example-tool = wavebench_example_scope:descriptor")[0])
    relative = os.path.relpath(launcher, manager.environment().purelib)
    lifecycle_module._verify_generated_launchers(
        wheel, str(scripts), manager.environment().purelib, {relative},
    )
    if damage == "omitted":
        launcher.unlink()
        rows = set()
    else:
        launcher.write_bytes(launcher.read_bytes() + b"tampered")
        rows = {relative}  # The rewritten RECORD claims the new bytes are valid.

    def postflight(candidate, **kwargs):
        assert kwargs == {"rollback_reinstall": True}
        lifecycle_module._verify_generated_launchers(
            wheel, str(scripts), manager.environment().purelib, rows,
        )
        return {"record_sha256": "new-record", "files_sha256": "new-files"}

    monkeypatch.setattr(manager, "_postflight", postflight)
    monkeypatch.setattr(manager, "_record_matches_environment", lambda *args, **kwargs: False)
    monkeypatch.setattr(manager, "_distribution_absent", lambda candidate: False)
    with pytest.raises(ConfigError, match="manual inspection"):
        manager.recover()
    assert manager._read_json(manager.ledger_path, "ledger") == ledger
    assert manager.journal_path.exists()


@pytest.mark.skipif(os.name != "nt", reason="pip Windows launcher ZIP timestamps")
def test_native_launcher_rollback_and_recover_with_different_zip_timestamps(tmp_path, monkeypatch):
    python = _target_venv(tmp_path)
    manager = PluginLifecycle(python_executable=python)
    entries = "\n[console_scripts]\nexample-tool = wavebench_example_scope:descriptor\n"
    first = _plugin_wheel(tmp_path, version="0.1.0", script_entries=entries)
    broken = _plugin_wheel(
        tmp_path, version="0.2.0", script_entries=entries, broken_descriptor=True,
    )
    build_environment = lifecycle_module.build_subprocess_environment
    epoch = 946684800  # 2000-01-01; ZIP timestamp changes without sleeping.

    def fixed_epoch_environment():
        environment = build_environment()
        environment["SOURCE_DATE_EPOCH"] = str(epoch)
        return environment

    monkeypatch.setattr(lifecycle_module, "build_subprocess_environment", fixed_epoch_environment)
    manager.install(first)
    launcher = Path(manager.environment().scripts) / "example-tool.exe"

    def installed_record():
        ledger = manager._read_json(manager.ledger_path, "ledger")
        return ledger["plugins"]["wavebench-example-scope"]

    def zip_timestamp():
        with ZipFile(launcher) as archive:
            return archive.getinfo("__main__.py").date_time

    def assert_new_healthy_snapshot(previous, previous_timestamp):
        current = installed_record()
        assert zip_timestamp() != previous_timestamp
        assert current["installed_record_sha256"] != previous["installed_record_sha256"]
        assert current["installed_files_sha256"] != previous["installed_files_sha256"]
        for field in ("wheel_sha256", "metadata_sha256", "record_sha256", "entry_points", "version"):
            assert current[field] == previous[field]
        assert manager.info("example.scope").status == "healthy"
        assert not manager.journal_path.exists()

    previous, timestamp = installed_record(), zip_timestamp()
    epoch += 4
    with pytest.raises(ConfigError, match="plugin postflight failed") as error:
        manager.upgrade(broken)
    assert "recovery required" not in str(error.value)
    assert_new_healthy_snapshot(previous, timestamp)

    # A failed remove also reinstalls the same wheel and refreshes its digests.
    previous, timestamp = installed_record(), zip_timestamp()
    pip_uninstall = manager._pip_uninstall

    def failed_remove(distribution, **kwargs):
        pip_uninstall(distribution, **kwargs)
        raise ConfigError("simulated failure after uninstall")

    monkeypatch.setattr(manager, "_pip_uninstall", failed_remove)
    epoch += 4
    with pytest.raises(ConfigError, match="plugin postflight failed"):
        manager.remove("example.scope")
    assert_new_healthy_snapshot(previous, timestamp)
    monkeypatch.setattr(manager, "_pip_uninstall", pip_uninstall)

    # Interrupt after pip reinstalls, before verification/journal digest refresh.
    class Interrupted(BaseException):
        pass

    previous, timestamp = installed_record(), zip_timestamp()
    cached = manager._record_wheel(previous)
    pip_install = manager._pip_install

    def interrupted_reinstall(wheel):
        pip_install(wheel)
        if wheel == cached:
            raise Interrupted()

    monkeypatch.setattr(manager, "_pip_install", interrupted_reinstall)
    epoch += 4
    with pytest.raises(Interrupted):
        manager.upgrade(broken)
    monkeypatch.setattr(manager, "_pip_install", pip_install)
    assert json.loads(manager.journal_path.read_text(encoding="utf-8"))["stage"] == "rollback_started"
    with pytest.raises(ConfigError, match="installed RECORD hash mismatch"):
        manager._postflight(previous)

    # Rollback's one relaxed comparison must still reject launcher corruption,
    # missing RECORD files, wheel-member tampering, descriptors and cached wheels.
    launcher_bytes = launcher.read_bytes()
    launcher.write_bytes(launcher_bytes + b"tampered")
    with pytest.raises(ConfigError, match="installed file hash mismatch"):
        manager._postflight(previous, rollback_reinstall=True)
    with pytest.raises(ConfigError, match="manual inspection"):
        manager.recover()
    assert installed_record() == previous
    assert manager.journal_path.exists()
    launcher.unlink()
    with pytest.raises(ConfigError, match="installed RECORD file is missing"):
        manager._postflight(previous, rollback_reinstall=True)
    launcher.write_bytes(launcher_bytes)

    # A rewritten RECORD must not hide deletion or legitimize new launcher bytes.
    installed_record_path = (Path(manager.environment().purelib)
                             / "wavebench_example_scope-0.1.0.dist-info" / "RECORD")
    record_bytes = installed_record_path.read_bytes()
    rows = list(csv.reader(io.StringIO(record_bytes.decode())))
    launcher_row = next(row for row in rows if row[0].endswith("example-tool.exe"))
    output = io.StringIO(newline="")
    csv.writer(output).writerows(row for row in rows if row is not launcher_row)
    installed_record_path.write_text(output.getvalue(), encoding="utf-8", newline="")
    launcher.unlink()
    with pytest.raises(ConfigError, match="manual inspection"):
        manager.recover()
    launcher.write_bytes(launcher_bytes + b"tampered")
    launcher_row[1] = "sha256=" + urlsafe_b64encode(sha256(launcher.read_bytes()).digest()).rstrip(b"=").decode()
    launcher_row[2] = str(launcher.stat().st_size)
    output = io.StringIO(newline="")
    csv.writer(output).writerows(rows)
    installed_record_path.write_text(output.getvalue(), encoding="utf-8", newline="")
    with pytest.raises(ConfigError, match="manual inspection"):
        manager.recover()
    assert installed_record() == previous
    assert manager.journal_path.exists()
    launcher.write_bytes(launcher_bytes)
    installed_record_path.write_bytes(record_bytes)

    module = Path(manager.environment().purelib) / "wavebench_example_scope" / "__init__.py"
    module_bytes = module.read_bytes()
    module.write_bytes(module_bytes + b"\n# changed wheel member\n")
    with pytest.raises(ConfigError, match="does not match cached wheel"):
        manager._postflight(previous, rollback_reinstall=True)
    module.write_bytes(module_bytes.replace(b"driver_id=driver_id", b'driver_id="wrong.scope"'))
    with pytest.raises(ConfigError, match="descriptor identity mismatch"):
        manager._postflight(previous, rollback_reinstall=True)
    module.write_bytes(module_bytes)
    wheel_bytes = cached.read_bytes()
    cached.write_bytes(wheel_bytes + b"tampered")
    with pytest.raises(ConfigError, match="rollback wheel is missing or changed"):
        manager._postflight(previous, rollback_reinstall=True)
    cached.write_bytes(wheel_bytes)

    assert manager.recover().status == "recovered-after-rollback"
    assert_new_healthy_snapshot(previous, timestamp)
    assert manager.remove("example.scope").status == "removed"
    assert not launcher.exists()
