"""Conservative local resource leases for instrument sessions.

The kernel-backed lock is the only ownership fact.  A sidecar JSON file is
diagnostic metadata only; stale metadata may be removed after a non-blocking
lock proves that the lock is free, while the ``.lock`` inode is never unlinked.

Leases also hold the old v1 fingerprints for enumerated equivalent spellings
and every caller-supplied spelling. Keeping these locks in the v1 directory
protects both acquisition orders for those identities. Arbitrary port padding
cannot be enumerated; all processes must upgrade for complete alias protection.

The old case folding conflated the three-part INSTR resource class with the
LAN device named instr.  Their canonical identities are now distinct, but their
compatibility locks conservatively contend while v1 processes are supported.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import ntpath
import os
import posixpath
from pathlib import Path
import re
import socket
from typing import Any, Iterable, Iterator, Literal

from wavebench.errors import ConfigError, ResourceBusyError

from .file_lock import (
    FileLock,
    FileLockBusy,
    FileLockError,
    FileLockUnavailable,
    backend_info,
)
from .platform_io import atomic_write_json, ensure_private_directory


LEASE_SCHEMA = "wavebench.resource_lease.v1"
LeaseMode = Literal["shared", "exclusive"]
_LEASE_MODES = frozenset({"shared", "exclusive"})
_TCPIP_PREFIX = re.compile(r"^tcpip(?P<board>\d*)$", re.IGNORECASE)
_ASRL_RESOURCE = re.compile(r"^asrl(?P<number>[0-9]+)(?:::instr)?$", re.IGNORECASE)
_WINDOWS_COM = re.compile(r"^(?:\\\\\.\\)?com(?P<number>\d+)$", re.IGNORECASE)
_WINDOWS_DRIVE = re.compile(r"^[a-zA-Z]:[\\/]")


def normalize_resource(resource: object) -> str:
    """Normalize a VISA or local serial resource for lease identity.

    TCPIP default board and LAN device spellings share an identity.  As in
    PyVISA, a three-part uppercase INSTR denotes the default resource class;
    lowercase instr and socket denote LAN device names.  This distinction is
    resolved before case folding.  Numeric ASRL resources gain
    their optional INSTR suffix.  On Windows, standard ASRL numbers map to COM
    ports, as in the VISA serial backend; POSIX has no such COM mapping.  Serial
    paths retain case and are normalized with ``realpath`` so symlink aliases
    such as ``/dev/serial/by-id`` share one lock when they resolve to the same
    device.
    """

    normalized = _normalize_resource_v1(resource)
    serial = _ASRL_RESOURCE.fullmatch(normalized)
    if serial is not None:
        number = serial.group("number")
        # Do not infer COM aliases for nonstandard, zero-padded ASRL names:
        # pyvisa-py passes their board spelling verbatim to pyserial.
        if _is_windows() and not number.startswith("0"):
            return f"com{number}"
        return f"asrl{number}::instr"
    parts = normalized.split("::")
    prefix = _TCPIP_PREFIX.fullmatch(parts[0])
    if prefix is not None and len(parts) >= 2:
        board = int(prefix.group("board") or "0")
        parts[0] = f"tcpip{board}" if board else "tcpip"
        # TCPIP[board]::host[::LAN device][::INSTR].  SOCKET endpoints
        # keep their explicit port and class, including port 5025; only the
        # numeric spelling of a SOCKET port is folded below.
        original_parts = " ".join(resource.strip().split()).split("::")
        if len(parts) == 2 or (len(parts) == 3 and original_parts[-1] == "INSTR"):
            parts = [*parts[:2], "inst0", "instr"]
        elif len(parts) == 3 and original_parts[-1] != "SOCKET":
            parts.append("instr")
        elif len(parts) == 3:
            raise ConfigError("TCPIP SOCKET resource requires a host and an explicit port")
        if len(parts) == 4 and parts[-1] == "socket" and re.fullmatch(r"[0-9]+", parts[2]):
            parts[2] = str(int(parts[2]))
        return "::".join(parts)
    return normalized


def _normalize_resource_v1(resource: object) -> str:
    """The original normalization, retained for lock protocol compatibility."""

    if not isinstance(resource, str) or not resource.strip():
        raise ConfigError("resource lease requires a non-empty resource")
    stripped = " ".join(resource.strip().split())
    com_match = _WINDOWS_COM.fullmatch(stripped)
    if com_match is not None:
        number = int(com_match.group("number"))
        if number > 0:
            return f"com{number}"
    if stripped.startswith("/"):
        # Keep POSIX device paths stable even when a Windows process sees a
        # leading slash as a drive-relative path (for example ``/dev/ttyUSB0``).
        return posixpath.realpath(stripped)
    if stripped.startswith("~/"):
        return os.path.realpath(os.path.expanduser(stripped))
    if stripped.startswith("\\\\") or _WINDOWS_DRIVE.match(stripped):
        return ntpath.normcase(ntpath.normpath(stripped))
    parts = stripped.split("::")
    if len(parts) >= 2:
        prefix = _TCPIP_PREFIX.fullmatch(parts[0])
        if prefix is not None:
            # TCPIP and TCPIP0 are the same default VISA board.  Other board
            # numbers remain distinct; SOCKET and INSTR are intentionally not
            # collapsed because they can be different server endpoints.
            board = prefix.group("board")
            normalized_prefix = "tcpip" if board in {"", "0"} else f"tcpip{int(board)}"
            return "::".join([normalized_prefix, *(part.casefold() for part in parts[1:])])
    return stripped.casefold()


def _v1_resource_identities(resource: str) -> set[str]:
    """Enumerate old identities without re-normalizing them with new rules."""

    identities = {resource}
    serial = _ASRL_RESOURCE.fullmatch(resource)
    com = _WINDOWS_COM.fullmatch(resource) if _is_windows() else None
    if com is not None and com.group("number").startswith("0"):
        com = None
    if serial is not None or com is not None:
        number = (serial or com).group("number")
        identities.update({f"asrl{number}", f"asrl{number}::instr"})
    parts = resource.split("::")
    prefix = _TCPIP_PREFIX.fullmatch(parts[0])
    if prefix is not None and len(parts) >= 2:
        # v1 normalized TCPIP00 (and more zeroes) to tcpip0, whereas
        # TCPIP and TCPIP0 became tcpip.  Both are the default board.
        prefixes = {parts[0], "tcpip0"} if parts[0] == "tcpip" else {parts[0]}
        tails = {"::".join(parts[1:])}
        if parts[-1] == "instr":
            if len(parts) == 4:
                # PyVISA recognizes resource classes before case folding.
                # Lowercase socket/instr are valid LAN names even without the
                # class suffix.  v1 folded those omitted-suffix spellings too.
                tails.add("::".join(parts[1:-1]))
            if len(parts) == 4 and parts[-2] == "inst0":
                tails.update({parts[1], f"{parts[1]}::instr"})
        identities.update(f"{prefix}::{tail}" for prefix in prefixes for tail in tails)
    return identities


def normalize_lock_id(lock_id: object | None) -> str:
    if lock_id is None:
        return ""
    if not isinstance(lock_id, str):
        raise ConfigError("resource lease lock_id must be a string")
    normalized = " ".join(lock_id.strip().split())
    if any(ord(character) < 32 for character in normalized):
        raise ConfigError("resource lease lock_id must not contain control characters")
    return normalized


def resource_lease_key(resource: object, lock_id: object | None = None) -> str:
    normalized_resource = normalize_resource(resource)
    normalized_lock_id = normalize_lock_id(lock_id)
    return f"{normalized_resource}\x00{normalized_lock_id}"


def resource_fingerprint(resource: object, lock_id: object | None = None) -> str:
    return hashlib.sha256(resource_lease_key(resource, lock_id).encode("utf-8")).hexdigest()


def default_lease_directory() -> Path:
    configured = os.environ.get("WAVEBENCH_LEASE_DIR")
    if configured:
        return Path(configured).expanduser()
    if _is_windows():
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            return Path(local_app_data) / "WaveBench" / "resource-leases-v1"
        try:
            return Path.home() / "AppData" / "Local" / "WaveBench" / "resource-leases-v1"
        except RuntimeError as exc:
            raise ConfigError("unable to determine the Windows user data directory") from exc
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime and os.access(runtime, os.W_OK):
        return Path(runtime) / "wavebench" / "resource-leases-v1"
    return Path.home() / ".cache" / "wavebench" / "resource-leases-v1"


def _is_windows() -> bool:
    return os.name == "nt"


@dataclass
class ResourceLease:
    """One acquired or acquire-able local resource lease."""

    resource: str
    directory: Path = field(default_factory=default_lease_directory)
    lock_id: str = ""
    mode: LeaseMode = "exclusive"
    operation: str | None = None
    _lock: FileLock | None = field(default=None, init=False, repr=False)
    _locks: list[FileLock] = field(default_factory=list, init=False, repr=False)
    _acquired: bool = field(default=False, init=False, repr=False)
    _lease_id: str | None = field(default=None, init=False, repr=False)
    _legacy_resources: set[str] = field(default_factory=set, init=False, repr=False)

    def __post_init__(self) -> None:
        self._legacy_resources.add(_normalize_resource_v1(self.resource))
        self.resource = normalize_resource(self.resource)
        self.directory = Path(self.directory).expanduser()
        self.lock_id = normalize_lock_id(self.lock_id)
        if self.mode not in _LEASE_MODES:
            choices = ", ".join(sorted(_LEASE_MODES))
            raise ConfigError(f"resource lease mode must be one of: {choices}")

    @property
    def fingerprint(self) -> str:
        return resource_fingerprint(self.resource, self.lock_id)

    @property
    def lock_path(self) -> Path:
        return self.directory / f"{self.fingerprint}.lock"

    @property
    def metadata_path(self) -> Path:
        return self.directory / f"{self.fingerprint}.json"

    @property
    def _lock_paths(self) -> list[Path]:
        # Keep the caller's old spelling as well as the canonical identity.
        # Arbitrarily padded ports cannot all be enumerated for v1 processes.
        identities = _v1_resource_identities(self.resource)
        for resource in self._legacy_resources:
            identities.update(_v1_resource_identities(resource))
        fingerprints = {
            hashlib.sha256(f"{resource}\x00{self.lock_id}".encode("utf-8")).hexdigest()
            for resource in identities
        }
        return [self.directory / f"{fingerprint}.lock" for fingerprint in sorted(fingerprints)]

    def _acquire_locks(self, mode: LeaseMode) -> list[FileLock]:
        locks: list[FileLock] = []
        try:
            for path in self._lock_paths:
                lock = FileLock(path, mode=mode)
                lock.acquire()
                locks.append(lock)
            return locks
        except Exception:
            for lock in reversed(locks):
                lock.release()
            raise

    @property
    def acquired(self) -> bool:
        return self._acquired

    def acquire(self) -> "ResourceLease":
        if self._acquired:
            return self
        ensure_private_directory(self.directory)
        try:
            locks = self._acquire_locks(self.mode)
        except FileLockBusy as exc:
            raise ResourceBusyError(
                f"resource lease is busy (fingerprint {self.fingerprint[:16]})"
            ) from exc
        except (FileLockUnavailable, FileLockError) as exc:
            raise ConfigError(f"unable to acquire resource lease: {exc}") from exc

        self._locks = locks
        self._lock = next(lock for lock in locks if lock.path == self.lock_path)
        self._acquired = True
        self._lease_id = os.urandom(16).hex()
        try:
            self._write_metadata()
        except Exception:
            self.release()
            raise
        return self

    def release(self) -> None:
        if not self._locks:
            return
        try:
            self._remove_own_metadata()
        finally:
            for lock in reversed(self._locks):
                lock.release()
            self._locks = []
            self._lock = None
            self._acquired = False
            self._lease_id = None

    def __enter__(self) -> "ResourceLease":
        return self.acquire()

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.release()

    def holder_metadata(self) -> dict[str, Any] | None:
        """Read diagnostic sidecars without proving ownership; use status for that."""

        paths = [self.metadata_path, *(path.with_suffix(".json") for path in self._lock_paths)]
        return self._read_holder_metadata(paths)

    @staticmethod
    def _read_holder_metadata(paths: Iterable[Path]) -> dict[str, Any] | None:
        for path in dict.fromkeys(paths):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                continue
            if isinstance(value, dict):
                return dict(value)
        return None

    def status(self) -> dict[str, Any]:
        """Return lock ownership and stale-metadata state."""

        info = backend_info()
        if not info.available:
            return {
                "schema": LEASE_SCHEMA,
                "available": False,
                "reason": "file_lock_backend_unavailable",
                "backend": info.name,
                "backend_error": info.reason,
                "fingerprint": self.fingerprint,
                "lock_path": str(self.lock_path),
                "metadata_path": str(self.metadata_path),
            }
        if not any(path.exists() for path in self._lock_paths):
            return self._status_payload(held=False, stale_metadata=False, metadata=None)
        locks: list[FileLock] = []
        busy_paths: list[Path] = []
        try:
            try:
                # Keep every free probe locked while collecting diagnostics:
                # its sidecar cannot describe a current holder. Do not stop at
                # the first busy alias and then prefer stale canonical JSON.
                for path in self._lock_paths:
                    lock = FileLock(path, mode="exclusive")
                    try:
                        lock.acquire()
                    except FileLockBusy:
                        busy_paths.append(path)
                    else:
                        locks.append(lock)
            except (FileLockUnavailable, FileLockError) as exc:
                raise ConfigError(f"unable to inspect resource lease: {exc}") from exc
            held = bool(busy_paths)
            ambiguous = False
            if held:
                candidates = [
                    self._read_holder_metadata([path.with_suffix(".json")])
                    for path in busy_paths
                ]
                # Sidecars remain diagnostics, not the ownership fact. Missing
                # or conflicting holder records must not select an arbitrary
                # legacy process or fall back to a provably free lock's JSON.
                ambiguous = (
                    any(value is None for value in candidates)
                    or not isinstance(candidates[0].get("lease_id"), str)
                    or not candidates[0]["lease_id"]
                    or any(value != candidates[0] for value in candidates[1:])
                )
                metadata = None if ambiguous else candidates[0]
            else:
                metadata = self.holder_metadata()
            stale = not held and metadata is not None
        finally:
            for lock in reversed(locks):
                lock.release()
        return self._status_payload(
            held=held, stale_metadata=stale, metadata=metadata, metadata_ambiguous=ambiguous,
        )

    def clear_stale_metadata(self) -> bool:
        """Remove only sidecar metadata after proving the lock is free."""

        info = backend_info()
        if (
            not info.available
            or not any(path.exists() for path in self._lock_paths)
            or not any(path.with_suffix(".json").exists() for path in self._lock_paths)
        ):
            return False
        locks: list[FileLock] = []
        try:
            try:
                locks = self._acquire_locks("exclusive")
            except FileLockBusy:
                return False
            except (FileLockUnavailable, FileLockError) as exc:
                raise ConfigError(f"unable to clear resource lease metadata: {exc}") from exc
            removed = False
            for path in self._lock_paths:
                try:
                    path.with_suffix(".json").unlink()
                    removed = True
                except FileNotFoundError:
                    pass
            return removed
        finally:
            for lock in reversed(locks):
                lock.release()

    def _status_payload(
        self,
        *,
        held: bool,
        stale_metadata: bool,
        metadata: dict[str, Any] | None,
        metadata_ambiguous: bool = False,
    ) -> dict[str, Any]:
        return {
            "schema": LEASE_SCHEMA,
            "available": True,
            "backend": backend_info().name,
            "platform": os.name,
            "held": held,
            "stale_metadata": stale_metadata,
            "fingerprint": self.fingerprint,
            "lock_path": str(self.lock_path),
            "metadata_path": str(self.metadata_path),
            "metadata": metadata,
            "metadata_ambiguous": metadata_ambiguous,
        }

    def _metadata(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema": LEASE_SCHEMA,
            "backend": backend_info().name,
            "platform": os.name,
            "lease_id": self._lease_id,
            "resource_fingerprint": self.fingerprint,
            "lock_id": self.lock_id,
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "mode": self.mode,
            "acquired_at": _utc_now(),
        }
        if self.operation:
            payload["operation"] = self.operation
        return payload

    def _write_metadata(self) -> None:
        # status checks every busy compatibility lock. Publish one identical
        # record on every lock actually acquired, including caller spellings
        # merged by acquire_many, rather than weakening its ambiguity checks.
        metadata = self._metadata()
        for lock in self._locks:
            atomic_write_json(lock.path.with_suffix(".json"), metadata)

    def _remove_own_metadata(self) -> None:
        if self._lease_id is None:
            return
        # Inspect each sidecar independently, also after a partial write failure.
        # Keep the kernel locks until cleanup ends so an exclusive successor
        # cannot publish between the lease_id check and unlink.
        for lock in self._locks:
            path = lock.path.with_suffix(".json")
            metadata = self._read_holder_metadata([path])
            if metadata is None or metadata.get("lease_id") != self._lease_id:
                continue
            try:
                path.unlink()
            except FileNotFoundError:
                pass


@dataclass(frozen=True)
class ResourceLeaseManager:
    """Acquire one or more leases in deterministic order."""

    directory: Path = field(default_factory=default_lease_directory)

    def acquire(
        self,
        resource: str,
        *,
        lock_id: str = "",
        mode: LeaseMode = "exclusive",
        operation: str | None = None,
    ) -> ResourceLease:
        return ResourceLease(
            resource=resource,
            directory=self.directory,
            lock_id=lock_id,
            mode=mode,
            operation=operation,
        ).acquire()

    def acquire_many(
        self,
        resources: Iterable[str],
        *,
        mode: LeaseMode = "exclusive",
        operation: str | None = None,
    ) -> list[ResourceLease]:
        leases = [
            ResourceLease(
                resource=resource,
                directory=self.directory,
                mode=mode,
                operation=operation,
            )
            for resource in resources
        ]
        leases.sort(key=lambda lease: (lease.fingerprint, lease.resource))
        unique: dict[str, ResourceLease] = {}
        for lease in leases:
            if lease.fingerprint in unique:
                unique[lease.fingerprint]._legacy_resources.update(lease._legacy_resources)
            else:
                unique[lease.fingerprint] = lease
        acquired: list[ResourceLease] = []
        try:
            for lease in unique.values():
                acquired.append(lease.acquire())
            return acquired
        except Exception:
            for lease in reversed(acquired):
                lease.release()
            raise

    @contextmanager
    def hold_many(
        self,
        resources: Iterable[str],
        *,
        mode: LeaseMode = "exclusive",
        operation: str | None = None,
    ) -> Iterator[list[ResourceLease]]:
        leases = self.acquire_many(resources, mode=mode, operation=operation)
        try:
            yield leases
        finally:
            for lease in reversed(leases):
                lease.release()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = [
    "LEASE_SCHEMA",
    "LeaseMode",
    "ResourceLease",
    "ResourceLeaseManager",
    "default_lease_directory",
    "normalize_lock_id",
    "normalize_resource",
    "resource_fingerprint",
    "resource_lease_key",
]
