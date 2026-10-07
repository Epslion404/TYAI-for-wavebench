"""Optional python-can transport for bounded raw CAN and CAN-FD frames."""

from __future__ import annotations

from dataclasses import dataclass, field
import time
from typing import Any

from wavebench.config import CanConnectionConfig
from wavebench.errors import ConfigError, ConnectionError, SessionCloseError, TransportIOError
from wavebench.logging import CommandLogger

from .contracts import (
    BinaryQueryResult,
    BinaryResponseFraming,
    CommandTransmission,
    ReplayPolicy,
    ResponseProgress,
    Synchronization,
    TransportPhase,
)


class CanDependencyError(ConnectionError):
    """The configured CAN transport cannot run because python-can is absent."""

    code = "can_dependency_unavailable"


class CanAdapterUnavailableError(ConnectionError):
    """The configured CAN interface/channel could not be opened."""

    code = "can_adapter_unavailable"


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
            raise CanDependencyError(
                "CAN support requires the optional wavebench[can] dependency"
            ) from exc
        try:
            bus = can.Bus(interface=config.interface, channel=config.channel, fd=config.fd)
        except Exception as exc:
            raise CanAdapterUnavailableError("configured CAN adapter is unavailable") from exc
        return cls(config.channel or "", bus, logger or CommandLogger(), config)

    @property
    def frame_limit(self) -> int:
        return 64 if self.config.fd else 8

    def record_event(self, direction: str, text: str) -> None:
        self.logger.record(direction, text)

    def send_frame(self, frame: bytes, *, max_bytes: int, replay: ReplayPolicy = ReplayPolicy.NO_REPLAY) -> None:
        replay = ReplayPolicy(replay)
        if not isinstance(frame, bytes) or not frame:
            raise self._frame_preflight_error("send_frame", replay, "frame_invalid")
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
            raise self._frame_preflight_error("send_frame", replay, "frame_limit_invalid")
        if len(frame) > max_bytes or len(frame) > self.frame_limit:
            raise self._frame_preflight_error("send_frame", replay, "frame_too_large")
        if replay is not ReplayPolicy.NO_REPLAY:
            raise self._frame_preflight_error("send_frame", replay, "frame_replay_unsupported")
        try:
            import can  # type: ignore[import-not-found]
            self.bus.send(can.Message(
                arbitration_id=self.config.arbitration_id,
                is_extended_id=self.config.extended_id,
                is_fd=self.config.fd,
                data=frame,
            ))
        except Exception as exc:
            raise self._frame_write_error("send_frame", replay) from exc
        self.logger.record("send_frame", f"<can bytes len={len(frame)}>")

    def read_exact(self, size: int, *, timeout_ms: int | None = None, replay: ReplayPolicy = ReplayPolicy.NO_REPLAY) -> bytes:
        replay = ReplayPolicy(replay)
        if isinstance(size, bool) or not isinstance(size, int) or size < 1:
            raise self._frame_preflight_error("read_exact", replay, "frame_size_invalid")
        if timeout_ms is not None and (
            isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int) or timeout_ms < 1
        ):
            raise self._frame_preflight_error("read_exact", replay, "frame_timeout_invalid")
        if replay is ReplayPolicy.SAFE_TO_REPLAY:
            raise self._frame_preflight_error("read_exact", replay, "frame_replay_unsupported")
        deadline = time.monotonic() + (timeout_ms if timeout_ms is not None else self.config.timeout_ms) / 1000.0
        while len(self._pending) < size:
            self._receive_one("read_exact", replay, deadline)
        result = bytes(self._pending[:size])
        del self._pending[:size]
        self.logger.record("read_frame", f"<can bytes len={len(result)}>")
        return result

    def read_until(self, delimiter: bytes, *, max_bytes: int, timeout_ms: int | None = None, replay: ReplayPolicy = ReplayPolicy.NO_REPLAY) -> bytes:
        replay = ReplayPolicy(replay)
        if not delimiter or not isinstance(delimiter, bytes):
            raise self._frame_preflight_error("read_until", replay, "frame_delimiter_invalid")
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < len(delimiter):
            raise self._frame_preflight_error("read_until", replay, "frame_limit_invalid")
        if timeout_ms is not None and (
            isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int) or timeout_ms < 1
        ):
            raise self._frame_preflight_error("read_until", replay, "frame_timeout_invalid")
        if replay is ReplayPolicy.SAFE_TO_REPLAY:
            raise self._frame_preflight_error("read_until", replay, "frame_replay_unsupported")
        deadline = time.monotonic() + (timeout_ms if timeout_ms is not None else self.config.timeout_ms) / 1000.0
        while True:
            marker = self._pending.find(delimiter)
            if marker >= 0:
                end = marker + len(delimiter)
                if end > max_bytes:
                    raise self._frame_read_error(
                        "read_until", replay, len(self._pending), "frame_too_large"
                    )
                result = bytes(self._pending[:end])
                del self._pending[:end]
                return result
            if len(self._pending) >= max_bytes:
                raise self._frame_read_error(
                    "read_until", replay, len(self._pending), "frame_too_large"
                )
            self._receive_one("read_until", replay, deadline)

    def _receive_one(self, operation: str, replay: ReplayPolicy, deadline: float) -> None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise self._frame_read_error(operation, replay, len(self._pending), "frame_read_timeout")
        try:
            message = self.bus.recv(timeout=remaining)
        except Exception as exc:
            raise self._frame_read_error(operation, replay, len(self._pending), "frame_read_failed") from exc
        if message is None:
            raise self._frame_read_error(operation, replay, len(self._pending), "frame_read_timeout")
        self._pending.extend(bytes(message.data))
        if time.monotonic() >= deadline:
            raise self._frame_read_error(operation, replay, len(self._pending), "frame_read_timeout")

    def write(self, command: str) -> None:
        try:
            frame = command.encode("ascii")
        except UnicodeError as exc:
            raise self._frame_preflight_error("write", ReplayPolicy.NO_REPLAY, "frame_encoding_invalid") from exc
        self.send_frame(frame, max_bytes=self.frame_limit)

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
            raise SessionCloseError([("can_bus", exc)]) from None
        finally:
            self._pending.clear()

    @staticmethod
    def _frame_preflight_error(
        operation: str,
        replay: ReplayPolicy,
        reason_code: str,
    ) -> TransportIOError:
        return TransportIOError(
            "raw CAN frame request was rejected before transmission",
            operation=operation,
            phase=TransportPhase.BEFORE_SEND,
            replay_policy=replay,
            command_transmission=CommandTransmission.NOT_SENT,
            response_progress=ResponseProgress.NONE,
            synchronization=Synchronization.PROVEN,
            attempts=0,
            reason_code=reason_code,
            consumed_bytes=0,
            discarded_bytes=0,
        )

    @staticmethod
    def _frame_write_error(operation: str, replay: ReplayPolicy) -> TransportIOError:
        return TransportIOError(
            "raw CAN frame write failed",
            operation=operation,
            phase=TransportPhase.SENDING,
            replay_policy=replay,
            command_transmission=CommandTransmission.UNKNOWN,
            response_progress=ResponseProgress.NONE,
            synchronization=Synchronization.UNPROVEN,
            attempts=1,
            reason_code="frame_write_failed",
            consumed_bytes=0,
            discarded_bytes=0,
        )

    @staticmethod
    def _frame_read_error(
        operation: str,
        replay: ReplayPolicy,
        consumed_bytes: int,
        reason_code: str,
    ) -> TransportIOError:
        partial = consumed_bytes > 0
        return TransportIOError(
            "raw CAN frame read did not complete",
            operation=operation,
            phase=TransportPhase.READING,
            replay_policy=replay,
            command_transmission=CommandTransmission.SENT,
            response_progress=ResponseProgress.PARTIAL if partial else ResponseProgress.NONE,
            synchronization=Synchronization.LOST if partial else Synchronization.UNPROVEN,
            attempts=1,
            reason_code=reason_code,
            consumed_bytes=consumed_bytes,
            discarded_bytes=0,
        )


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


__all__ = ["CanAdapterUnavailableError", "CanDependencyError", "CanTransport"]
