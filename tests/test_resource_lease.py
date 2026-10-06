from __future__ import annotations

import hashlib
from contextlib import ExitStack
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from wavebench.errors import ConfigError, ResourceBusyError
from wavebench.services import file_lock
from wavebench.services import resource_lease as lease_module
from wavebench.services.resource_lease import (
    ResourceLease,
    ResourceLeaseManager,
    normalize_resource,
    resource_fingerprint,
)


def test_resource_identity_normalizes_visa_case_and_serial_paths() -> None:
    assert normalize_resource(" TCPIP::Bench::INSTR ") == "tcpip::bench::inst0::instr"
    assert normalize_resource("/dev/ttyUSB0") == "/dev/ttyUSB0"
    assert normalize_resource("COM3") == "com3"
    assert normalize_resource(r"\\.\COM3") == "com3"
    assert normalize_resource("COM003") == "com3"
    assert resource_fingerprint("TCPIP::Bench::INSTR") == resource_fingerprint(
        "tcpip::bench::inst0::instr"
    )


_EQUIVALENT_RESOURCES = [
    ("TCPIP::192.0.2.10::INSTR", "TCPIP0::192.0.2.10::inst0::INSTR"),
    ("TCPIP::192.0.2.10", "tcpip000::192.0.2.10::INST0"),
    ("TCPIP2::bench::INSTR", "tcpip02::BENCH::inst0::instr"),
    ("TCPIP::bench::custom", "TCPIP0::BENCH::CUSTOM::INSTR"),
    ("TCPIP::bench::socket", "TCPIP0::bench::socket::INSTR"),
    ("TCPIP::bench::instr", "TCPIP0::bench::instr::INSTR"),
    ("TCPIP::bench::5025::SOCKET", "TCPIP00::BENCH::5025::socket"),
    ("TCPIP2::bench::5025::SOCKET", "tcpip02::BENCH::00005025::socket"),
    ("ASRL1", "asrl1::INSTR"),
    ("ASRL001", "asrl001::INSTR"),
]
if os.name == "nt":
    _EQUIVALENT_RESOURCES.extend([
        ("COM1", "ASRL1::INSTR"),
        (r"\\.\COM3", "ASRL3"),
    ])


@pytest.mark.parametrize(("resource", "device"), [
    ("TCPIP::bench", "inst0"),
    ("TCPIP::bench::INSTR", "inst0"),
    ("TCPIP::bench::inst0::INSTR", "inst0"),
    ("TCPIP::bench::socket", "socket"),
    ("TCPIP::bench::socket::INSTR", "socket"),
    ("TCPIP::bench::instr", "instr"),
    ("TCPIP::bench::instr::INSTR", "instr"),
])
def test_tcpip_lan_device_identity_matches_pyvisa_parser(resource: str, device: str) -> None:
    rname = pytest.importorskip("pyvisa.rname")
    parsed = rname.ResourceName.from_string(resource)
    assert parsed.resource_class == "INSTR"
    assert parsed.lan_device_name == device
    canonical = normalize_resource(resource)
    assert canonical == f"tcpip::bench::{device}::instr"
    assert normalize_resource(canonical) == canonical
    assert resource_fingerprint(resource) == resource_fingerprint(str(parsed))


def test_three_part_uppercase_socket_class_requires_a_port() -> None:
    rname = pytest.importorskip("pyvisa.rname")
    with pytest.raises(rname.InvalidResourceName):
        rname.ResourceName.from_string("TCPIP::bench::SOCKET")
    with pytest.raises(ConfigError, match="explicit port"):
        normalize_resource("TCPIP::bench::SOCKET")


def _child_attempt(
    directory: Path,
    resource: str,
    *,
    legacy_identity: str | None = None,
    mode: str = "exclusive",
) -> str:
    """Exercise real kernel locks in another interpreter, without instruments."""

    script = """
import hashlib
import json
from pathlib import Path
import sys
from wavebench.errors import ResourceBusyError
from wavebench.services.file_lock import FileLock, FileLockBusy
from wavebench.services.resource_lease import ResourceLease

directory, resource, legacy_identity, mode = json.loads(sys.argv[1])
if legacy_identity is None:
    lease = ResourceLease(resource, directory=Path(directory), mode=mode)
else:
    fingerprint = hashlib.sha256((legacy_identity + '\\x00').encode('utf-8')).hexdigest()
    lease = FileLock(Path(directory) / (fingerprint + '.lock'), mode=mode)
try:
    lease.acquire()
except (ResourceBusyError, FileLockBusy):
    print('busy')
else:
    lease.release()
    print('acquired')
"""
    environment = os.environ.copy()
    source = str(Path(__file__).resolve().parents[1] / "src")
    environment["PYTHONPATH"] = source + os.pathsep + environment.get("PYTHONPATH", "")
    completed = subprocess.run(
        [sys.executable, "-c", script, json.dumps([str(directory), resource, legacy_identity, mode])],
        env=environment,
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
    )
    return completed.stdout.strip()


@pytest.mark.parametrize(("first", "second"), _EQUIVALENT_RESOURCES)
@pytest.mark.parametrize("reverse", [False, True])
def test_equivalent_resources_contend_across_processes(
    tmp_path: Path, first: str, second: str, reverse: bool,
) -> None:
    if reverse:
        first, second = second, first
    assert resource_fingerprint(first) == resource_fingerprint(second)
    with ResourceLease(first, directory=tmp_path):
        assert _child_attempt(tmp_path, second) == "busy"
    assert _child_attempt(tmp_path, second) == "acquired"


@pytest.mark.parametrize("reverse", [False, True])
def test_socket_port_padding_contends_across_processes(tmp_path: Path, reverse: bool) -> None:
    first = "TCPIP::bench::5025::SOCKET"
    second = "TCPIP0::BENCH::05025::socket"
    if reverse:
        first, second = second, first
    with ResourceLease(first, directory=tmp_path):
        assert _child_attempt(tmp_path, second) == "busy"
    assert _child_attempt(tmp_path, second) == "acquired"


_LEGACY_IDENTITIES = [
    ("TCPIP::bench::05025::SOCKET", "tcpip::bench::05025::socket"),
    ("TCPIP00::bench::05025::SOCKET", "tcpip0::bench::05025::socket"),
    ("TCPIP::bench::05025::SOCKET", "tcpip::bench::5025::socket"),
    ("TCPIP0::192.0.2.10::inst0::INSTR", "tcpip::192.0.2.10::instr"),
    ("TCPIP::192.0.2.10::INSTR", "tcpip::192.0.2.10::inst0::instr"),
    ("TCPIP::192.0.2.10::INSTR", "tcpip0::192.0.2.10::inst0"),
    ("TCPIP::bench::custom::INSTR", "tcpip0::bench::custom"),
    ("TCPIP::bench::socket::INSTR", "tcpip::bench::socket"),
    ("TCPIP::bench::socket", "tcpip0::bench::socket::instr"),
    ("TCPIP::bench::instr::INSTR", "tcpip::bench::instr"),
    ("TCPIP::bench::instr", "tcpip0::bench::instr::instr"),
    ("ASRL1::INSTR", "asrl1"),
    ("ASRL1", "asrl1::instr"),
    ("ASRL001", "asrl001::instr"),
]
if os.name == "nt":
    _LEGACY_IDENTITIES.extend([("COM1", "asrl1"), ("ASRL3", "com3")])


@pytest.mark.parametrize(("resource", "legacy_identity"), _LEGACY_IDENTITIES)
@pytest.mark.parametrize("legacy_first", [False, True])
def test_v1_lock_migration_contends_in_both_acquisition_orders(
    tmp_path: Path, resource: str, legacy_identity: str, legacy_first: bool,
) -> None:
    # Hash the old normalized identity directly: using the new fingerprint
    # function here would conceal a lock-protocol migration regression.
    fingerprint = hashlib.sha256(f"{legacy_identity}\x00".encode("utf-8")).hexdigest()
    old_path = tmp_path / f"{fingerprint}.lock"
    lease = ResourceLease(resource, directory=tmp_path)
    if legacy_first:
        with file_lock.FileLock(old_path):
            old_metadata = old_path.with_suffix(".json")
            old_metadata.write_text(json.dumps({"lease_id": "old-holder"}), encoding="utf-8")
            assert lease.status()["held"] is True
            assert lease.holder_metadata()["lease_id"] == "old-holder"
            assert lease.clear_stale_metadata() is False
            assert _child_attempt(tmp_path, resource) == "busy"
        assert lease.clear_stale_metadata() is True
        assert old_path.exists()
        assert _child_attempt(tmp_path, resource) == "acquired"
    else:
        with lease:
            assert _child_attempt(tmp_path, resource, legacy_identity=legacy_identity) == "busy"
        assert _child_attempt(tmp_path, resource, legacy_identity=legacy_identity) == "acquired"


def test_status_ignores_stale_canonical_metadata_while_legacy_lock_is_held(tmp_path: Path) -> None:
    lease = ResourceLease("TCPIP::bench::INSTR", directory=tmp_path)
    with lease:
        pass
    lease.metadata_path.write_text(json.dumps({"lease_id": "stale-canonical"}), encoding="utf-8")
    old_fingerprint = hashlib.sha256(b"tcpip::bench::instr\x00").hexdigest()
    old_path = tmp_path / f"{old_fingerprint}.lock"
    old_metadata = old_path.with_suffix(".json")

    with file_lock.FileLock(old_path):
        old_metadata.write_text(json.dumps({"lease_id": "active-legacy"}), encoding="utf-8")
        status = lease.status()
        assert status["held"] is True
        assert status["stale_metadata"] is False
        assert status["metadata"]["lease_id"] == "active-legacy"
        assert lease.clear_stale_metadata() is False
        assert _child_attempt(tmp_path, "TCPIP0::bench::inst0::INSTR") == "busy"
    assert lease.clear_stale_metadata() is True
    assert old_path.exists()
    assert lease.lock_path.exists()


@pytest.mark.parametrize("holder_ids", [("old-a", "old-b"), (None, None)])
def test_status_reports_ambiguous_legacy_holders_without_stale_fallback(
    tmp_path: Path, holder_ids: tuple[str | None, str | None],
) -> None:
    lease = ResourceLease("TCPIP::bench::INSTR", directory=tmp_path)
    with lease:
        pass
    lease.metadata_path.write_text(json.dumps({"lease_id": "stale"}), encoding="utf-8")
    old_identities = ("tcpip::bench::instr", "tcpip::bench")

    with ExitStack() as stack:
        for identity, holder_id in zip(old_identities, holder_ids):
            fingerprint = hashlib.sha256(f"{identity}\x00".encode("utf-8")).hexdigest()
            path = tmp_path / f"{fingerprint}.lock"
            stack.enter_context(file_lock.FileLock(path))
            if holder_id:
                path.with_suffix(".json").write_text(json.dumps({"lease_id": holder_id}), encoding="utf-8")
        status = lease.status()
        assert status["held"] is True
        assert status["metadata"] is None
        assert status["metadata_ambiguous"] is True
        assert lease.clear_stale_metadata() is False


@pytest.mark.parametrize("legacy_first", [False, True])
def test_unenumerated_v1_socket_padding_requires_all_processes_to_upgrade(
    tmp_path: Path, legacy_first: bool,
) -> None:
    # v1 hashes unbounded port spellings independently. A new caller using
    # only the unpadded spelling cannot enumerate every old hash. This test
    # records the mixed-version limitation, not a new/new mutual-exclusion guarantee.
    old_identity = "tcpip::bench::0000005025::socket"
    old_fingerprint = hashlib.sha256(f"{old_identity}\x00".encode("utf-8")).hexdigest()
    old_path = tmp_path / f"{old_fingerprint}.lock"
    resource = "TCPIP::bench::5025::SOCKET"
    if legacy_first:
        with file_lock.FileLock(old_path):
            assert _child_attempt(tmp_path, resource) == "acquired"
    else:
        with ResourceLease(resource, directory=tmp_path):
            assert _child_attempt(tmp_path, resource, legacy_identity=old_identity) == "acquired"


@pytest.mark.parametrize(("first", "second"), [
    ("TCPIP::192.0.2.10::INSTR", "TCPIP::192.0.2.11::INSTR"),
    ("TCPIP::bench::INSTR", "TCPIP1::bench::INSTR"),
    ("TCPIP::bench::inst0::INSTR", "TCPIP::bench::inst1::INSTR"),
    ("TCPIP::bench::inst1::INSTR", "TCPIP::bench::inst2::INSTR"),
    ("TCPIP::bench::INSTR", "TCPIP::bench::socket"),
    ("TCPIP::bench::inst1::INSTR", "TCPIP::bench::instr"),
    ("TCPIP::bench::5025::SOCKET", "TCPIP::bench::SOCKET::INSTR"),
    ("TCPIP::bench::5025::SOCKET", "TCPIP::bench::5026::SOCKET"),
    ("TCPIP::bench::05025::SOCKET", "TCPIP::bench::05026::SOCKET"),
    ("TCPIP::bench::05025::SOCKET", "TCPIP1::bench::05025::SOCKET"),
    ("TCPIP::bench::INSTR", "TCPIP::bench::5025::SOCKET"),
    ("ASRL1::INSTR", "ASRL2::INSTR"),
    ("COM1", "COM2"),
    ("/dev/ttyUSB0", "/dev/ttyusb0"),
])
def test_distinct_resources_remain_independent_across_processes(
    tmp_path: Path, first: str, second: str,
) -> None:
    assert resource_fingerprint(first) != resource_fingerprint(second)
    with ResourceLease(first, directory=tmp_path):
        assert _child_attempt(tmp_path, second) == "acquired"


@pytest.mark.parametrize("reverse", [False, True])
def test_v1_case_collision_conservatively_blocks_default_and_named_instr(
    tmp_path: Path, reverse: bool,
) -> None:
    first, second = "TCPIP::bench::INSTR", "TCPIP::bench::instr"
    assert resource_fingerprint(first) != resource_fingerprint(second)
    assert lease_module._normalize_resource_v1(first) == lease_module._normalize_resource_v1(second)
    if reverse:
        first, second = second, first
    # They denote different LAN devices, but v1 already conflated their locks.
    # Reusing that lock is necessary to keep active old processes protected.
    with ResourceLease(first, directory=tmp_path):
        assert _child_attempt(tmp_path, second) == "busy"


def test_com_asrl_mapping_is_platform_specific(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lease_module, "_is_windows", lambda: True)
    assert resource_fingerprint("COM1") == resource_fingerprint("ASRL1::INSTR")
    assert normalize_resource("ASRL001") == "asrl001::instr"
    monkeypatch.setattr(lease_module, "_is_windows", lambda: False)
    assert resource_fingerprint("COM1") != resource_fingerprint("ASRL1::INSTR")


def test_shared_alias_leases_coexist_but_exclusive_contends(tmp_path: Path) -> None:
    first = "TCPIP::192.0.2.10::INSTR"
    second = "TCPIP0::192.0.2.10::inst0::INSTR"
    with ResourceLease(first, directory=tmp_path, mode="shared"):
        assert _child_attempt(tmp_path, second, mode="shared") == "acquired"
        assert _child_attempt(tmp_path, second) == "busy"


def test_failed_alias_acquisition_releases_partial_locks(tmp_path: Path) -> None:
    lease = ResourceLease("TCPIP::bench::INSTR", directory=tmp_path)
    paths = lease._lock_paths
    assert len(paths) > 1
    with file_lock.FileLock(paths[-1]):
        with pytest.raises(ResourceBusyError):
            lease.acquire()
        assert not lease.acquired
        for path in paths[:-1]:
            with file_lock.FileLock(path):
                pass
    with lease:
        assert lease.acquired


def test_metadata_failure_releases_all_alias_locks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    lease = ResourceLease("ASRL1", directory=tmp_path)

    def fail_write() -> None:
        raise OSError("metadata failure")

    monkeypatch.setattr(lease, "_write_metadata", fail_write)
    with pytest.raises(OSError, match="metadata failure"):
        lease.acquire()
    assert not lease.acquired
    assert _child_attempt(tmp_path, "ASRL1::INSTR") == "acquired"


def test_lock_id_remains_a_separate_namespace(tmp_path: Path) -> None:
    with ResourceLease("ASRL1", directory=tmp_path, lock_id="other-device"):
        assert _child_attempt(tmp_path, "ASRL1::INSTR") == "acquired"


def test_acquire_many_deduplicates_visa_defaults(tmp_path: Path) -> None:
    manager = ResourceLeaseManager(tmp_path)
    with manager.hold_many([
        "TCPIP::bench::INSTR", "TCPIP0::bench::inst0::INSTR", "ASRL1", "ASRL1::INSTR",
    ]) as leases:
        assert len(leases) == 2
        assert _child_attempt(tmp_path, "TCPIP::bench::inst0") == "busy"
        assert _child_attempt(tmp_path, "ASRL1") == "busy"


def test_acquire_many_keeps_legacy_locks_for_every_supplied_port_spelling(tmp_path: Path) -> None:
    manager = ResourceLeaseManager(tmp_path)
    with manager.hold_many([
        "TCPIP::bench::5025::SOCKET", "TCPIP::bench::05025::SOCKET",
        "TCPIP::bench::005025::SOCKET",
    ]) as leases:
        assert len(leases) == 1
        for port in ("5025", "05025", "005025"):
            assert _child_attempt(
                tmp_path, "unused", legacy_identity=f"tcpip::bench::{port}::socket",
            ) == "busy"


def test_stale_cleanup_removes_malformed_legacy_metadata_only_when_free(tmp_path: Path) -> None:
    identity = "asrl1"
    fingerprint = hashlib.sha256(f"{identity}\x00".encode("utf-8")).hexdigest()
    old_path = tmp_path / f"{fingerprint}.lock"
    metadata = old_path.with_suffix(".json")
    lease = ResourceLease("ASRL1::INSTR", directory=tmp_path)
    with file_lock.FileLock(old_path):
        metadata.write_text("broken JSON", encoding="utf-8")
        assert lease.clear_stale_metadata() is False
        assert metadata.exists()
    assert lease.clear_stale_metadata() is True
    assert old_path.exists()
    assert not metadata.exists()


def test_windows_default_lease_directory_uses_local_app_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("WAVEBENCH_LEASE_DIR", raising=False)
    monkeypatch.setattr(lease_module, "_is_windows", lambda: True)
    monkeypatch.setenv("LOCALAPPDATA", r"C:\\Users\\tester\\AppData\\Local")

    directory = lease_module.default_lease_directory()

    assert directory.parts[-2:] == ("WaveBench", "resource-leases-v1")
    assert "AppData" in str(directory)


def test_lease_busy_error_and_private_metadata(tmp_path: Path) -> None:
    manager = ResourceLeaseManager(tmp_path)
    first = manager.acquire("TCPIP::192.0.2.10::INSTR", operation="run")
    try:
        with pytest.raises(ResourceBusyError, match="fingerprint") as raised:
            manager.acquire("tcpip::192.0.2.10::inst0::instr", operation="other")
        assert raised.value.code == "resource_busy"
        assert raised.value.exit_code == 7
        if os.name != "nt":
            assert first.lock_path.stat().st_mode & 0o777 == 0o600
        payload = json.loads(first.metadata_path.read_text(encoding="utf-8"))
        assert payload["schema"] == "wavebench.resource_lease.v1"
        assert payload["lease_id"]
        assert "192.0.2.10" not in first.metadata_path.read_text(encoding="utf-8")
        assert first.status()["held"] is True
    finally:
        first.release()


def test_release_and_stale_cleanup_keep_lock_file(tmp_path: Path) -> None:
    manager = ResourceLeaseManager(tmp_path)
    lease = manager.acquire("TCPIP::bench::INSTR")
    path = lease.lock_path
    assert lease.clear_stale_metadata() is False
    lease.release()

    assert path.exists()
    assert lease.status()["held"] is False
    lease.metadata_path.write_text(
        json.dumps({"schema": "wavebench.resource_lease.v1", "lease_id": "stale"}),
        encoding="utf-8",
    )
    assert lease.status()["stale_metadata"] is True
    assert lease.clear_stale_metadata() is True
    assert path.exists()
    assert not lease.metadata_path.exists()


def test_acquire_many_releases_partial_batch_on_busy_resource(tmp_path: Path) -> None:
    manager = ResourceLeaseManager(tmp_path)
    held = manager.acquire("TCPIP::held::INSTR")
    try:
        with pytest.raises(ResourceBusyError):
            manager.acquire_many(["TCPIP::free::INSTR", "TCPIP::held::INSTR"])
        free = manager.acquire("TCPIP::free::INSTR")
        free.release()
    finally:
        held.release()


def test_acquire_many_deduplicates_windows_com_aliases(tmp_path: Path) -> None:
    manager = ResourceLeaseManager(tmp_path)

    leases = manager.acquire_many(["COM3", r"\\.\COM3"])

    try:
        assert len(leases) == 1
        assert leases[0].resource == "com3"
    finally:
        for lease in leases:
            lease.release()


def test_hold_many_releases_all_leases(tmp_path: Path) -> None:
    manager = ResourceLeaseManager(tmp_path)
    with manager.hold_many(["TCPIP::a::INSTR", "TCPIP::b::INSTR"]) as leases:
        assert len(leases) == 2
        assert all(lease.acquired for lease in leases)
    assert all(
        not ResourceLease(resource, directory=tmp_path).status()["held"]
        for resource in ("TCPIP::a::INSTR", "TCPIP::b::INSTR")
    )


def test_unavailable_lock_backend_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        file_lock,
        "backend_info",
        lambda: file_lock.LockBackendInfo(
            name="test", available=False, reason="injected-unavailable"
        ),
    )
    with pytest.raises(ConfigError, match="injected-unavailable"):
        ResourceLease("TCPIP::bench::INSTR", directory=tmp_path).acquire()
