"""CAN/CAN-FD transport backend over python-can.

``CanTransport`` maps the raw-frame byte contract onto a CAN bus: each
``send_frame`` transmits one CAN data frame and each ``read_exact`` /
``read_until`` receives one frame.  The optional ``python-can`` dependency is
only required when a descriptor actually opens the ``can`` backend, so the
default install stays free of bus adapters (RFC §2.6).

SCPI-style text operations have no meaning on a raw CAN bus and fail with a
structured ``raw_frame_unsupported`` error before any byte is transmitted,
matching the guarded layer's raw-frame contract.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from wavebench.config import SerialConnectionConfig
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

_MAX_CAN_DATA_BYTES = 8
_MAX_CANFD_DATA_BYTES = 64


@dataclass
class CanTransport:
    resource: str
    session: Any
    logger: CommandLogger
    arbitration_id: int = 0x100
    is_extended_id: bool = False
    is_fd: bool = False
    read_timeout_ms: int = 500
    max_frame_bytes: int = _MAX_CAN_DATA_BYTES
    _closed: bool = field(default=False, init=False, repr=False)

    @classmethod
    def open(
        cls,
        config: SerialConnectionConfig | None = None,
        logger: CommandLogger | None = None,
        *,
        resource: str | None = None,
        channel: str | None = None,
        bitrate: int = 500_000,
        arbitration_id: int = 0x100,
        is_extended_id: bool = False,
        is_fd: bool = False,
        read_timeout_ms: int = 500,
        max_frame_bytes: int = _MAX_CAN_DATA_BYTES,
        options: dict[str, object] | None = None,
    ) -> "CanTransport":
        logger = logger or CommandLogger()
        resource_value = resource or (config.resource if config is not None else None) or ""
        if not resource_value.startswith("can://"):
            raise ConfigError("can backend resource must use the can://<channel> scheme")
        resource_interface, resource_channel = _split_can_resource(resource_value)
        interface_value = str((options or {}).get("can_interface") or resource_interface or "virtual")
        if channel is not None:
            channel_value = channel
        elif resource_channel is not None:
            channel_value = resource_channel
        else:
            channel_value = resource_interface
        if not channel_value:
            raise ConfigError("can:// resource must include a channel name")
        if isinstance(bitrate, bool) or not isinstance(bitrate, int) or bitrate <= 0:
            raise ConfigError("can bitrate must be a positive integer")
        if is_fd and max_frame_bytes > _MAX_CANFD_DATA_BYTES:
            raise ConfigError(
                f"CAN-FD frames support at most {_MAX_CANFD_DATA_BYTES} data bytes"
            )
        if read_timeout_ms < 1:
            raise ConfigError("can read timeout must be a positive integer")
        if not _valid_frame_size(max_frame_bytes, is_fd=is_fd):
            raise ConfigError(
                f"can max_frame_bytes must be 1..{_MAX_CANFD_DATA_BYTES if is_fd else _MAX_CAN_DATA_BYTES}"
            )
        try:
            import can
        except ImportError as exc:
            raise ConnectionError(
                "python-can is not installed. Run: python -m pip install wavebench[can]"
            ) from exc
        try:
            session = can.interface.Bus(
                interface=interface_value,
                channel=channel_value,
                bitrate=bitrate,
                fd=is_fd,
            )
        except Exception as exc:
            raise ConnectionError(f"failed to open CAN bus {channel_value}: {exc}") from exc
        return cls(
            resource=resource_value,
            session=session,
            logger=logger,
            arbitration_id=arbitration_id,
            is_extended_id=is_extended_id,
            is_fd=is_fd,
            read_timeout_ms=read_timeout_ms,
            max_frame_bytes=max_frame_bytes,
        )

    def record_event(self, direction: str, text: str) -> None:
        self.logger.record(direction, text)

    def send_frame(
        self,
        frame: bytes,
        *,
        max_bytes: int,
        replay: ReplayPolicy = ReplayPolicy.NO_REPLAY,
    ) -> None:
        replay = ReplayPolicy(replay)
        if replay is not ReplayPolicy.NO_REPLAY:
            raise self._frame_preflight_error("send_frame", replay, "frame_replay_unsupported")
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
            raise self._frame_preflight_error("send_frame", replay, "frame_limit_invalid")
        if not isinstance(frame, bytes):
            raise self._frame_preflight_error("send_frame", replay, "frame_invalid")
        if len(frame) > self.max_frame_bytes:
            raise self._frame_preflight_error("send_frame", replay, "frame_too_large")
        if max_bytes < len(frame):
            raise self._frame_preflight_error("send_frame", replay, "frame_too_large")
        self.logger.record("send_frame", f"can id=0x{self.arbitration_id:x} len={len(frame)}")
        try:
            import can

            message = can.Message(
                arbitration_id=self.arbitration_id,
                data=bytes(frame),
                is_extended_id=self.is_extended_id,
                is_fd=self.is_fd,
            )
            sent = self.session.send(message)
            if sent is not None and sent is not True and sent != len(frame):
                raise OSError(
                    f"short CAN frame write: sent {sent} of {len(frame)} bytes"
                )
            # Deliberately no flush_tx_buffer here: python-can defines it as
            # discarding pending transmissions, not waiting for delivery, so
            # calling it after a successful send would drop queued frames.
        except TransportIOError:
            raise
        except Exception as exc:
            raise self._frame_error("send_frame", exc, replay) from exc

    def read_exact(
        self,
        size: int,
        *,
        timeout_ms: int | None = None,
        replay: ReplayPolicy = ReplayPolicy.NO_REPLAY,
    ) -> bytes:
        """Receive one CAN frame whose payload is exactly ``size`` bytes.

        CAN frames carry a variable-length payload, so a frame whose DLC does
        not match the requested size is a structured failure rather than a
        silently padded response.  This keeps the guarded contract's
        ``read_exact`` size assertion exact.
        """
        replay = ReplayPolicy(replay)
        if replay is not ReplayPolicy.NO_REPLAY:
            raise self._frame_preflight_error("read_exact", replay, "frame_replay_unsupported")
        if isinstance(size, bool) or not isinstance(size, int) or size < 1:
            raise self._frame_preflight_error("read_exact", replay, "frame_size_invalid")
        if size > self.max_frame_bytes:
            raise self._frame_preflight_error("read_exact", replay, "frame_size_invalid")
        effective_timeout_ms = timeout_ms if timeout_ms is not None else self.read_timeout_ms
        if isinstance(effective_timeout_ms, bool) or not isinstance(effective_timeout_ms, int) or effective_timeout_ms < 1:
            raise self._frame_preflight_error("read_exact", replay, "frame_timeout_invalid")
        self.logger.record("read_exact", f"can id=0x{self.arbitration_id:x} size={size}")
        try:
            message = self.session.recv(timeout=effective_timeout_ms / 1000.0)
        except Exception as exc:
            raise self._frame_error("read_exact", exc, replay) from exc
        if message is None:
            raise TransportIOError(
                "can frame read timed out",
                operation="read_exact",
                phase=TransportPhase.READING,
                replay_policy=replay,
                command_transmission=CommandTransmission.SENT,
                response_progress=ResponseProgress.NONE,
                synchronization=Synchronization.UNPROVEN,
                attempts=1,
                reason_code="frame_timeout",
                consumed_bytes=0,
                discarded_bytes=0,
            )
        if getattr(message, "is_error_frame", False):
            raise TransportIOError(
                "can controller reported an error frame",
                operation="read_exact",
                phase=TransportPhase.READING,
                replay_policy=replay,
                command_transmission=CommandTransmission.SENT,
                response_progress=ResponseProgress.NONE,
                synchronization=Synchronization.UNPROVEN,
                attempts=1,
                reason_code="frame_bus_error",
                consumed_bytes=0,
                discarded_bytes=0,
            )
        data = bytes(message.data)
        if len(data) != size:
            raise TransportIOError(
                "can frame payload length did not match the requested size",
                operation="read_exact",
                phase=TransportPhase.PARSING,
                replay_policy=replay,
                command_transmission=CommandTransmission.SENT,
                response_progress=ResponseProgress.COMPLETE,
                synchronization=Synchronization.LOST,
                attempts=1,
                reason_code="frame_length_invalid",
                consumed_bytes=len(data),
                discarded_bytes=len(data),
            )
        self.logger.record("response", f"<can_frame len={len(data)}>")
        return data

    def read_until(
        self,
        delimiter: bytes,
        *,
        max_bytes: int,
        timeout_ms: int | None = None,
        replay: ReplayPolicy = ReplayPolicy.NO_REPLAY,
    ) -> bytes:
        """CAN frames are not byte-delimited, so read_until is unsupported."""
        replay = ReplayPolicy(replay)
        raise TransportIOError(
            "can transport does not support byte-delimited reads",
            operation="read_until",
            phase=TransportPhase.BEFORE_SEND,
            replay_policy=replay,
            command_transmission=CommandTransmission.NOT_SENT,
            response_progress=ResponseProgress.NONE,
            synchronization=Synchronization.PROVEN,
            attempts=0,
            reason_code="raw_frame_unsupported",
            consumed_bytes=0,
            discarded_bytes=0,
        )

    # ---- SCPI-style operations are unsupported on a raw CAN bus ----

    def write(self, command: str) -> None:
        raise self._scpi_unsupported("write")

    def write_bytes(self, command: bytes) -> None:
        raise self._scpi_unsupported("write_bytes")

    def query(
        self,
        command: str,
        *,
        replay: ReplayPolicy = ReplayPolicy.NO_REPLAY,
    ) -> str:
        raise self._scpi_unsupported("query")

    def query_float_list(
        self,
        command: str,
        *,
        timeout_ms: int | None = None,
        replay: ReplayPolicy = ReplayPolicy.NO_REPLAY,
    ) -> list[float]:
        raise self._scpi_unsupported("query_float_list")

    def query_bin_block(
        self,
        command: str,
        *,
        replay: ReplayPolicy = ReplayPolicy.NO_REPLAY,
    ) -> bytes:
        raise self._scpi_unsupported("query_bin_block")

    def query_binary(
        self,
        command: str,
        *,
        framing: BinaryResponseFraming,
        max_bytes: int,
        timeout_ms: int | None = None,
        replay: ReplayPolicy = ReplayPolicy.NO_REPLAY,
    ) -> BinaryQueryResult:
        raise self._scpi_unsupported("query_binary")

    def query_opc(
        self,
        *,
        replay: ReplayPolicy = ReplayPolicy.NO_REPLAY,
    ) -> str:
        raise self._scpi_unsupported("query_opc")

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if hasattr(self.session, "shutdown"):
                self.session.shutdown()
            elif hasattr(self.session, "close"):
                self.session.close()
        except Exception as exc:
            raise SessionCloseError([("session", exc)]) from None

    def _scpi_unsupported(self, operation: str) -> TransportIOError:
        return TransportIOError(
            f"can transport does not support {operation}",
            operation=operation,
            phase=TransportPhase.BEFORE_SEND,
            replay_policy=ReplayPolicy.NO_REPLAY,
            command_transmission=CommandTransmission.NOT_SENT,
            response_progress=ResponseProgress.NONE,
            synchronization=Synchronization.PROVEN,
            attempts=0,
            reason_code="raw_frame_unsupported",
            consumed_bytes=0,
            discarded_bytes=0,
        )

    @staticmethod
    def _frame_preflight_error(
        operation: str,
        replay: ReplayPolicy,
        reason_code: str,
    ) -> TransportIOError:
        return TransportIOError(
            f"can {operation} was rejected before transmission",
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
    def _frame_error(
        operation: str,
        exc: Exception,
        replay: ReplayPolicy,
    ) -> TransportIOError:
        return TransportIOError(
            f"can {operation} failed with {type(exc).__name__}",
            operation=operation,
            phase=TransportPhase.READING,
            replay_policy=replay,
            command_transmission=CommandTransmission.SENT,
            response_progress=ResponseProgress.UNKNOWN,
            synchronization=Synchronization.UNPROVEN,
            attempts=1,
            reason_code="bus_error",
            consumed_bytes=0,
            discarded_bytes=0,
        )


def _valid_frame_size(size: int, *, is_fd: bool) -> bool:
    if isinstance(size, bool) or not isinstance(size, int):
        return False
    maximum = _MAX_CANFD_DATA_BYTES if is_fd else _MAX_CAN_DATA_BYTES
    return 1 <= size <= maximum


def _split_can_resource(resource: str) -> tuple[str | None, str | None]:
    """Split ``can://<interface>/<channel>`` or ``can://<channel>``.

    Empty parts are treated as absent so a numeric zero channel is preserved
    while a missing component stays ``None``.
    """
    if not resource.startswith("can://"):
        raise ConfigError("can backend resource must use the can://<channel> scheme")
    suffix = resource[len("can://"):]
    if "/" in suffix:
        interface_raw, channel_raw = suffix.split("/", 1)
        interface = interface_raw.strip() or None
        channel = channel_raw.strip() if channel_raw.strip() != "" else None
        return interface, channel
    return None, suffix.strip() or None


__all__ = ["CanTransport", "_MAX_CAN_DATA_BYTES", "_MAX_CANFD_DATA_BYTES", "_split_can_resource"]