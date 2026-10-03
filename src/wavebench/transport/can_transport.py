"""Optional python-can transport for bounded raw CAN and CAN-FD frames."""

from __future__ import annotations

from dataclasses import dataclass, field
import time
from typing import Any

from wavebench.config import CanConnectionConfig
from wavebench.errors import ConfigError, ConnectionError, SessionCloseError, TransportIOError
from wavebench.logging import CommandLogger

from .contracts import (
    BinaryQueryResult, BinaryResponseFraming, CommandTransmission, ReplayPolicy,
    ResponseProgress, Synchronization, TransportPhase,
)


@dataclass
class CanTransport:
    resource: str
    bus: Any
    logger: CommandLogger
    config: CanConnectionConfig
    _pending: bytearray = field(default_factory=bytearray)
    _closed: bool = field(default=False, init=False, repr=False)

    @classmethod
    def open(cls, config: CanConnectionConfig, logger: CommandLogger | None = None) -> "CanTransport":
        _validate_config(config)
        try:
            import can  # type: ignore[import-not-found]
        except ImportError as exc:
            raise ConnectionError("python-can is not installed. Run: python -m pip install 'wavebench[can]'") from exc
        try:
            bus = can.Bus(interface=config.interface, channel=config.channel, fd=config.fd)
        except Exception as exc:
            raise ConnectionError(f"failed to open CAN channel {config.channel}: {exc}") from exc
        return cls(config.channel or "", bus, logger or CommandLogger(), config)

    @property
    def frame_limit(self) -> int:
        return 64 if self.config.fd else 8

    def record_event(self, direction: str, text: str) -> None:
        self.logger.record(direction, text)

    def send_frame(self, frame: bytes, *, max_bytes: int, replay: ReplayPolicy = ReplayPolicy.NO_REPLAY) -> None:
        if not isinstance(frame, bytes) or not frame:
            raise ConfigError("CAN frame must be non-empty bytes")
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
            raise ConfigError("CAN max_bytes must be a positive integer")
        if len(frame) > max_bytes or len(frame) > self.frame_limit:
            raise ConfigError(f"CAN frame exceeds {'CAN-FD' if self.config.fd else 'CAN'} payload limit")
        if ReplayPolicy(replay) is not ReplayPolicy.NO_REPLAY:
            raise ConfigError("CAN frame replay is unsupported")
        try:
            import can  # type: ignore[import-not-found]
            self.bus.send(can.Message(
                arbitration_id=self.config.arbitration_id,
                is_extended_id=self.config.extended_id,
                is_fd=self.config.fd,
                data=frame,
            ))
        except Exception as exc:
            raise TransportIOError(
                "CAN frame write failed",
                operation="send_frame",
                phase=TransportPhase.SENDING,
                replay_policy=ReplayPolicy.NO_REPLAY,
                command_transmission=CommandTransmission.UNKNOWN,
                response_progress=ResponseProgress.NONE,
                synchronization=Synchronization.UNPROVEN,
                attempts=1,
                reason_code="frame_write_failed",
            ) from exc
        self.logger.record("send_frame", f"<can bytes len={len(frame)}>")

    def read_exact(self, size: int, *, timeout_ms: int | None = None, replay: ReplayPolicy = ReplayPolicy.NO_REPLAY) -> bytes:
        if isinstance(size, bool) or not isinstance(size, int) or size < 1:
            raise ConfigError("CAN read size must be a positive integer")
        if ReplayPolicy(replay) is not ReplayPolicy.NO_REPLAY:
            raise ConfigError("CAN read replay is unsupported")
        deadline = self._deadline(timeout_ms)
        while len(self._pending) < size:
            self._receive_one(deadline, "read_exact")
        result = bytes(self._pending[:size])
        del self._pending[:size]
        self.logger.record("read_frame", f"<can bytes len={len(result)}>")
        return result

    def read_until(self, delimiter: bytes, *, max_bytes: int, timeout_ms: int | None = None, replay: ReplayPolicy = ReplayPolicy.NO_REPLAY) -> bytes:
        if not delimiter or not isinstance(delimiter, bytes):
            raise ConfigError("CAN delimiter must be non-empty bytes")
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < len(delimiter):
            raise ConfigError("CAN max_bytes must be an integer at least as large as the delimiter")
        if ReplayPolicy(replay) is not ReplayPolicy.NO_REPLAY:
            raise ConfigError("CAN read replay is unsupported")
        deadline = self._deadline(timeout_ms)
        while True:
            marker = self._pending.find(delimiter)
            if marker >= 0:
                end = marker + len(delimiter)
                if end > max_bytes:
                    raise self._read_error("read_until", "frame_too_large")
                result = bytes(self._pending[:end])
                del self._pending[:end]
                return result
            if len(self._pending) >= max_bytes:
                raise self._read_error("read_until", "frame_too_large")
            self._receive_one(deadline, "read_until")

    def _deadline(self, timeout_ms: int | None) -> float:
        timeout = self.config.timeout_ms if timeout_ms is None else timeout_ms
        if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0:
            raise ConfigError("CAN timeout_ms must be a positive integer")
        return time.monotonic() + timeout / 1000.0

    def _read_error(self, operation: str, reason_code: str) -> TransportIOError:
        consumed = len(self._pending)
        return TransportIOError(
            "CAN raw frame read did not complete",
            operation=operation,
            phase=TransportPhase.READING,
            replay_policy=ReplayPolicy.NO_REPLAY,
            command_transmission=CommandTransmission.SENT,
            response_progress=ResponseProgress.PARTIAL if consumed else ResponseProgress.NONE,
            synchronization=Synchronization.LOST if consumed else Synchronization.UNPROVEN,
            attempts=1,
            reason_code=reason_code,
            consumed_bytes=consumed,
            discarded_bytes=0,
        )

    def _receive_one(self, deadline: float, operation: str) -> None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise self._read_error(operation, "frame_read_timeout")
        try:
            message = self.bus.recv(timeout=remaining)
        except Exception as exc:
            raise self._read_error(operation, "frame_read_failed") from exc
        if message is None:
            raise self._read_error(operation, "frame_read_timeout")
        self._pending.extend(bytes(message.data))
        if time.monotonic() >= deadline:
            raise self._read_error(operation, "frame_read_timeout")

    def write(self, command: str) -> None:
        self.send_frame(command.encode("ascii"), max_bytes=self.frame_limit)

    def write_bytes(self, command: bytes) -> None:
        self.send_frame(command, max_bytes=self.frame_limit)

    def query(self, command: str, *, replay: ReplayPolicy = ReplayPolicy.NO_REPLAY) -> str:
        raise ConfigError("CAN transport has no implicit text response framing; use send_frame/read_exact")

    def query_float_list(self, command: str, *, timeout_ms: int | None = None, replay: ReplayPolicy = ReplayPolicy.NO_REPLAY) -> list[float]:
        raise ConfigError("CAN transport has no implicit text response framing")

    def query_bin_block(self, command: str, *, replay: ReplayPolicy = ReplayPolicy.NO_REPLAY) -> bytes:
        raise ConfigError("CAN transport has no SCPI binary-block framing")

    def query_binary(self, command: str, *, framing: BinaryResponseFraming, max_bytes: int, timeout_ms: int | None = None, replay: ReplayPolicy = ReplayPolicy.NO_REPLAY) -> BinaryQueryResult:
        raise ConfigError("CAN transport has no implicit binary response framing")

    def query_opc(self, *, replay: ReplayPolicy = ReplayPolicy.NO_REPLAY) -> str:
        raise ConfigError("CAN transport has no SCPI OPC query")

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.bus.shutdown()
        except Exception as exc:
            raise SessionCloseError([("bus", exc)]) from None
        finally:
            self._pending.clear()


def _validate_config(config: CanConnectionConfig) -> None:
    if not isinstance(config.channel, str) or not config.channel.strip():
        raise ConfigError("CAN channel is not configured")
    if not isinstance(config.interface, str) or not config.interface.strip():
        raise ConfigError("CAN interface is not configured")
    maximum = 0x1FFFFFFF if config.extended_id else 0x7FF
    if isinstance(config.arbitration_id, bool) or not isinstance(config.arbitration_id, int) or not 0 <= config.arbitration_id <= maximum:
        raise ConfigError("CAN arbitration ID is not configured or out of range")
    if config.timeout_ms <= 0:
        raise ConfigError("CAN timeout_ms must be > 0")


__all__ = ["CanTransport"]
