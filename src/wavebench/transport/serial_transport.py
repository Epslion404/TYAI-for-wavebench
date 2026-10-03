from __future__ import annotations

from dataclasses import dataclass
import time
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


_PARITY_ALIASES = {
    "N": "N",
    "NONE": "N",
    "O": "O",
    "ODD": "O",
    "E": "E",
    "EVEN": "E",
}

_TERMINATIONS = {
    "LF": b"\n",
    "CRLF": b"\r\n",
}


@dataclass
class SerialTransport:
    resource: str
    session: Any
    logger: CommandLogger
    write_termination: bytes = b"\n"
    read_termination: bytes = b"\n"
    read_retry_attempts: int = 1
    read_retry_delay_ms: int = 200

    @classmethod
    def open(
        cls,
        config: SerialConnectionConfig,
        logger: CommandLogger | None = None,
        *,
        read_retry_attempts: int = 1,
        read_retry_delay_ms: int = 200,
    ) -> "SerialTransport":
        try:
            write_termination = _TERMINATIONS[config.write_termination.strip().upper()]
            read_termination = _TERMINATIONS[config.read_termination.strip().upper()]
        except (AttributeError, KeyError) as exc:
            raise ConfigError("serial terminations must be lf or crlf") from exc
        try:
            import serial
        except ImportError as exc:
            raise ConnectionError("pyserial is not installed. Run: python -m pip install pyserial") from exc
        if not config.resource:
            raise ConnectionError("serial resource is not configured")
        logger = logger or CommandLogger()
        try:
            session = serial.Serial(
                port=config.resource,
                baudrate=config.baudrate,
                bytesize=config.bytesize,
                parity=_PARITY_ALIASES[config.parity.upper()],
                stopbits=config.stopbits,
                timeout=config.timeout_ms / 1000.0,
                write_timeout=config.timeout_ms / 1000.0,
                xonxoff=config.xonxoff,
                rtscts=config.rtscts,
                dsrdtr=config.dsrdtr,
            )
        except Exception as exc:
            raise ConnectionError(f"failed to open serial instrument {config.resource}: {exc}") from exc
        return cls(
            resource=config.resource,
            session=session,
            logger=logger,
            write_termination=write_termination,
            read_termination=read_termination,
            read_retry_attempts=read_retry_attempts,
            read_retry_delay_ms=read_retry_delay_ms,
        )

    def record_event(self, direction: str, text: str) -> None:
        self.logger.record(direction, text)

    def write(self, command: str) -> None:
        self.logger.record("write", command)
        try:
            payload = command.rstrip("\r\n").encode("ascii") + self.write_termination
        except Exception as exc:
            raise self._preflight_error("write", ReplayPolicy.NO_REPLAY) from exc
        try:
            written = self.session.write(payload)
            if written == 0:
                raise self._not_sent_error("write")
            if written is not None and written != len(payload):
                raise OSError(f"short serial write: wrote {written} of {len(payload)} bytes")
            if hasattr(self.session, "flush"):
                self.session.flush()
        except TransportIOError:
            raise
        except Exception as exc:
            raise self._write_error("write", exc) from exc

    def write_bytes(self, command: bytes) -> None:
        self.logger.record("write_binary", f"<bytes len={len(command)}>")
        try:
            written = self.session.write(command)
            if written == 0:
                raise self._not_sent_error("write_binary")
            if written is not None and written != len(command):
                raise OSError(
                    f"short serial binary write: wrote {written} of {len(command)} bytes"
                )
            if hasattr(self.session, "flush"):
                self.session.flush()
        except TransportIOError:
            raise
        except Exception as exc:
            raise self._write_error("write_binary", exc) from exc

    def send_frame(
        self,
        frame: bytes,
        *,
        max_bytes: int,
        replay: ReplayPolicy = ReplayPolicy.NO_REPLAY,
    ) -> None:
        replay = ReplayPolicy(replay)
        if not isinstance(frame, bytes) or not frame:
            raise self._frame_preflight_error("send_frame", replay, "frame_invalid")
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
            raise self._frame_preflight_error("send_frame", replay, "frame_limit_invalid")
        if len(frame) > max_bytes:
            raise self._frame_preflight_error("send_frame", replay, "frame_too_large")
        if replay is not ReplayPolicy.NO_REPLAY:
            raise self._frame_preflight_error("send_frame", replay, "frame_replay_unsupported")
        self.logger.record("send_frame", f"<bytes len={len(frame)}>")
        try:
            written = self.session.write(frame)
            if written == 0:
                raise self._not_sent_error("send_frame")
            if written is not None and written != len(frame):
                raise OSError(f"short serial frame write: wrote {written} of {len(frame)} bytes")
            if hasattr(self.session, "flush"):
                self.session.flush()
        except TransportIOError:
            raise
        except Exception as exc:
            raise self._write_error("send_frame", exc) from exc

    def read_exact(
        self,
        size: int,
        *,
        timeout_ms: int | None = None,
        replay: ReplayPolicy = ReplayPolicy.NO_REPLAY,
    ) -> bytes:
        replay = ReplayPolicy(replay)
        if isinstance(size, bool) or not isinstance(size, int) or size < 1:
            raise self._frame_preflight_error("read_exact", replay, "frame_size_invalid")
        if timeout_ms is not None and (
            isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int) or timeout_ms < 1
        ):
            raise self._frame_preflight_error("read_exact", replay, "frame_timeout_invalid")
        if replay is ReplayPolicy.SAFE_TO_REPLAY:
            raise self._frame_preflight_error("read_exact", replay, "frame_replay_unsupported")

        original_timeout = getattr(self.session, "timeout", None)
        deadline = self._frame_read_deadline(timeout_ms)
        received = bytearray()
        try:
            while len(received) < size:
                self._set_frame_read_timeout(deadline, "read_exact", replay, len(received))
                chunk = self.session.read(size - len(received))
                if not chunk:
                    raise self._frame_read_error(
                        "read_exact",
                        replay,
                        consumed_bytes=len(received),
                        reason_code="frame_read_timeout",
                    )
                received.extend(chunk)
                self._raise_if_frame_deadline_exhausted(
                    deadline,
                    "read_exact",
                    replay,
                    len(received),
                )
                if len(received) > size:
                    raise self._frame_read_error(
                        "read_exact",
                        replay,
                        consumed_bytes=len(received),
                        reason_code="frame_read_overrun",
                    )
        except TransportIOError:
            raise
        except Exception as exc:
            raise self._frame_read_error(
                "read_exact",
                replay,
                consumed_bytes=len(received),
                reason_code="frame_read_failed",
            ) from exc
        finally:
            if timeout_ms is not None and hasattr(self.session, "timeout"):
                self.session.timeout = original_timeout
        payload = bytes(received)
        self.logger.record("read_frame", f"<bytes len={len(payload)}>")
        return payload

    def read_until(
        self,
        delimiter: bytes,
        *,
        max_bytes: int,
        timeout_ms: int | None = None,
        replay: ReplayPolicy = ReplayPolicy.NO_REPLAY,
    ) -> bytes:
        replay = ReplayPolicy(replay)
        if not isinstance(delimiter, bytes) or not delimiter:
            raise self._frame_preflight_error("read_until", replay, "frame_delimiter_invalid")
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
            raise self._frame_preflight_error("read_until", replay, "frame_limit_invalid")
        if timeout_ms is not None and (
            isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int) or timeout_ms < 1
        ):
            raise self._frame_preflight_error("read_until", replay, "frame_timeout_invalid")
        if replay is ReplayPolicy.SAFE_TO_REPLAY:
            raise self._frame_preflight_error("read_until", replay, "frame_replay_unsupported")

        original_timeout = getattr(self.session, "timeout", None)
        deadline = self._frame_read_deadline(timeout_ms)
        received = bytearray()
        try:
            while True:
                self._set_frame_read_timeout(deadline, "read_until", replay, len(received))
                chunk = self.session.read(1)
                if not chunk:
                    raise self._frame_read_error(
                        "read_until",
                        replay,
                        consumed_bytes=len(received),
                        reason_code="frame_read_timeout",
                    )
                received.extend(chunk)
                self._raise_if_frame_deadline_exhausted(
                    deadline,
                    "read_until",
                    replay,
                    len(received),
                )
                if received.endswith(delimiter):
                    if len(received) <= max_bytes:
                        break
                    raise self._frame_read_error(
                        "read_until",
                        replay,
                        consumed_bytes=len(received),
                        reason_code="frame_too_large",
                    )
                if len(received) >= max_bytes:
                    raise self._frame_read_error(
                        "read_until",
                        replay,
                        consumed_bytes=len(received),
                        reason_code="frame_too_large",
                    )
        except TransportIOError:
            raise
        except Exception as exc:
            raise self._frame_read_error(
                "read_until",
                replay,
                consumed_bytes=len(received),
                reason_code="frame_read_failed",
            ) from exc
        finally:
            if timeout_ms is not None and hasattr(self.session, "timeout"):
                self.session.timeout = original_timeout
        payload = bytes(received)
        self.logger.record("read_frame", f"<bytes len={len(payload)}>")
        return payload

    def query(
        self,
        command: str,
        *,
        replay: ReplayPolicy = ReplayPolicy.NO_REPLAY,
    ) -> str:
        replay = ReplayPolicy(replay)
        self._reject_continuation("query", replay)
        self.logger.record("query", command)
        attempts = 0

        def read_once() -> str:
            nonlocal attempts
            try:
                payload = command.rstrip("\r\n").encode("ascii") + self.write_termination
            except Exception as exc:
                raise self._preflight_error("query", replay) from exc
            # A backend exception does not prove that zero bytes reached the
            # device.  Count the attempted exchange conservatively; a return
            # value of zero is handled below as the only proven not-sent case.
            attempts += 1
            written = self.session.write(payload)
            if written == 0:
                attempts -= 1
                raise TransportIOError(
                    "serial query command was not transmitted",
                    operation="query",
                    phase=TransportPhase.BEFORE_SEND,
                    replay_policy=replay,
                    command_transmission=CommandTransmission.NOT_SENT,
                    response_progress=ResponseProgress.NONE,
                    synchronization=Synchronization.PROVEN,
                    attempts=0,
                )
            if written is not None and written != len(payload):
                raise TransportIOError(
                    "serial query command transmission was partial",
                    operation="query",
                    phase=TransportPhase.SENDING,
                    replay_policy=replay,
                    command_transmission=CommandTransmission.UNKNOWN,
                    response_progress=ResponseProgress.NONE,
                    synchronization=Synchronization.LOST,
                    attempts=attempts,
                )
            if hasattr(self.session, "flush"):
                self.session.flush()
            if hasattr(self.session, "read_until"):
                raw = self.session.read_until(self.read_termination)
            else:
                raw = self.session.readline()
            if not raw:
                raise TransportIOError(
                    "serial query timed out before a response was observed",
                    operation="query",
                    phase=TransportPhase.READING,
                    replay_policy=replay,
                    command_transmission=CommandTransmission.SENT,
                    response_progress=ResponseProgress.NONE,
                    synchronization=Synchronization.UNPROVEN,
                    attempts=attempts,
                )
            if not raw.endswith(self.read_termination):
                raise TransportIOError(
                    "serial response ended before the configured termination",
                    operation="query",
                    phase=TransportPhase.READING,
                    replay_policy=replay,
                    command_transmission=CommandTransmission.SENT,
                    response_progress=ResponseProgress.PARTIAL,
                    synchronization=Synchronization.LOST,
                    attempts=attempts,
                )
            response_bytes = raw[: -len(self.read_termination)]
            if self.read_termination == b"\n" and response_bytes.endswith(b"\r"):
                response_bytes = response_bytes[:-1]
            return response_bytes.decode("ascii")

        try:
            response = self._read_with_policy("query", command, replay, read_once)
        except Exception as exc:
            if isinstance(exc, TransportIOError):
                raise
            raise self._query_error("query", replay, attempts, exc) from exc
        self.logger.record("response", response)
        return response

    def query_float_list(
        self,
        command: str,
        *,
        timeout_ms: int | None = None,
        replay: ReplayPolicy = ReplayPolicy.NO_REPLAY,
    ) -> list[float]:
        replay = ReplayPolicy(replay)
        if timeout_ms is not None and timeout_ms < 1:
            raise self._preflight_error("query_float_list", replay)
        original_timeout = getattr(self.session, "timeout", None)
        if timeout_ms is not None and hasattr(self.session, "timeout"):
            self.session.timeout = timeout_ms / 1000.0
        try:
            response = self.query(command, replay=replay)
            try:
                return [
                    float(item)
                    for item in response.replace(";", ",").split(",")
                    if item.strip()
                ]
            except ValueError as exc:
                raise TransportIOError(
                    "serial query_float_list response parsing failed",
                    operation="query_float_list",
                    phase=TransportPhase.PARSING,
                    replay_policy=ReplayPolicy(replay),
                    command_transmission=CommandTransmission.SENT,
                    response_progress=ResponseProgress.COMPLETE,
                    synchronization=Synchronization.PROVEN,
                    attempts=1,
                ) from exc
        finally:
            if timeout_ms is not None and hasattr(self.session, "timeout"):
                self.session.timeout = original_timeout

    def query_bin_block(
        self,
        command: str,
        *,
        replay: ReplayPolicy = ReplayPolicy.NO_REPLAY,
    ) -> bytes:
        replay = ReplayPolicy(replay)
        if replay is ReplayPolicy.READ_CONTINUATION_ONLY:
            self._reject_continuation("query_binary", replay)
        raise TransportIOError(
            "serial transport does not support binary block queries",
            operation="query_binary",
            phase=TransportPhase.BEFORE_SEND,
            replay_policy=replay,
            command_transmission=CommandTransmission.NOT_SENT,
            response_progress=ResponseProgress.NONE,
            synchronization=Synchronization.PROVEN,
            attempts=0,
        )

    def query_binary(
        self,
        command: str,
        *,
        framing: BinaryResponseFraming,
        max_bytes: int,
        timeout_ms: int | None = None,
        replay: ReplayPolicy = ReplayPolicy.NO_REPLAY,
        _transport_trailing: bytes = b"",
        _resynchronization_max_bytes: int = 0,
    ) -> BinaryQueryResult:
        replay = ReplayPolicy(replay)
        BinaryResponseFraming(framing)
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
            raise ValueError("max_bytes must be a positive integer")
        if timeout_ms is not None and (
            isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int) or timeout_ms < 1
        ):
            raise ValueError("timeout_ms must be a positive integer")
        if not isinstance(_transport_trailing, bytes):
            raise TypeError("transport trailing must be bytes")
        if (
            isinstance(_resynchronization_max_bytes, bool)
            or not isinstance(_resynchronization_max_bytes, int)
            or _resynchronization_max_bytes < 0
        ):
            raise ValueError("resynchronization limit must be a non-negative integer")
        self._reject_continuation("query_binary", replay)
        raise TransportIOError(
            "serial has not passed the R1.3 binary framing conformance gate",
            operation="query_binary",
            phase=TransportPhase.BEFORE_SEND,
            replay_policy=replay,
            command_transmission=CommandTransmission.NOT_SENT,
            response_progress=ResponseProgress.NONE,
            synchronization=Synchronization.PROVEN,
            attempts=0,
            reason_code="binary_framing_unsupported",
            consumed_bytes=0,
            discarded_bytes=0,
        )

    def query_opc(
        self,
        *,
        replay: ReplayPolicy = ReplayPolicy.NO_REPLAY,
    ) -> str:
        return self.query("*OPC?", replay=replay)

    def _read_with_policy(
        self,
        operation: str,
        command: str,
        replay: ReplayPolicy,
        read_once: Any,
    ) -> Any:
        attempt_limit = (
            self.read_retry_attempts + 1 if replay is ReplayPolicy.SAFE_TO_REPLAY else 1
        )
        last_exc: Exception | None = None
        transmitted_attempts = 0
        for attempt in range(attempt_limit):
            try:
                return read_once()
            except Exception as exc:
                last_exc = exc
                if isinstance(exc, TransportIOError):
                    transmitted_attempts += exc.attempts
                if attempt >= attempt_limit - 1 or not self._can_retry(exc, replay):
                    break
                if self.read_retry_delay_ms > 0:
                    time.sleep(self.read_retry_delay_ms / 1000.0)
                self.logger.record(
                    "retry",
                    f"operation={operation} attempt={attempt + 2}/{attempt_limit}",
                )
        assert last_exc is not None
        if isinstance(last_exc, TransportIOError) and last_exc.attempts != transmitted_attempts:
            raise last_exc.with_attempts(transmitted_attempts) from last_exc
        raise last_exc

    @staticmethod
    def _can_retry(exc: Exception, replay: ReplayPolicy) -> bool:
        return (
            replay is ReplayPolicy.SAFE_TO_REPLAY
            and isinstance(exc, TransportIOError)
            and exc.replay_policy is ReplayPolicy.SAFE_TO_REPLAY
            and exc.response_progress is ResponseProgress.NONE
            and exc.synchronization is Synchronization.PROVEN
            and exc.command_transmission
            in {CommandTransmission.NOT_SENT, CommandTransmission.SENT}
        )

    @staticmethod
    def _reject_continuation(operation: str, replay: ReplayPolicy) -> None:
        if replay is not ReplayPolicy.READ_CONTINUATION_ONLY:
            return
        raise TransportIOError(
            f"serial does not support read continuation for {operation}",
            operation=operation,
            phase=TransportPhase.BEFORE_SEND,
            replay_policy=replay,
            command_transmission=CommandTransmission.NOT_SENT,
            response_progress=ResponseProgress.NONE,
            synchronization=Synchronization.PROVEN,
            attempts=0,
        )

    @staticmethod
    def _query_error(
        operation: str,
        replay: ReplayPolicy,
        attempts: int,
        exc: Exception,
    ) -> TransportIOError:
        return TransportIOError(
            f"serial {operation} failed with {type(exc).__name__}",
            operation=operation,
            phase=TransportPhase.READING,
            replay_policy=replay,
            command_transmission=CommandTransmission.UNKNOWN,
            response_progress=ResponseProgress.UNKNOWN,
            synchronization=Synchronization.UNPROVEN,
            attempts=attempts,
        )

    @staticmethod
    def _write_error(operation: str, exc: Exception) -> TransportIOError:
        return TransportIOError(
            f"serial {operation} failed with {type(exc).__name__}",
            operation=operation,
            phase=TransportPhase.SENDING,
            replay_policy=ReplayPolicy.NO_REPLAY,
            command_transmission=CommandTransmission.UNKNOWN,
            response_progress=ResponseProgress.NONE,
            synchronization=Synchronization.UNPROVEN,
            attempts=1,
        )

    @staticmethod
    def _not_sent_error(operation: str) -> TransportIOError:
        return TransportIOError(
            f"serial {operation} command was not transmitted",
            operation=operation,
            phase=TransportPhase.BEFORE_SEND,
            replay_policy=ReplayPolicy.NO_REPLAY,
            command_transmission=CommandTransmission.NOT_SENT,
            response_progress=ResponseProgress.NONE,
            synchronization=Synchronization.PROVEN,
            attempts=0,
        )

    @staticmethod
    def _preflight_error(operation: str, replay: ReplayPolicy) -> TransportIOError:
        return TransportIOError(
            f"serial {operation} request was rejected before transmission",
            operation=operation,
            phase=TransportPhase.BEFORE_SEND,
            replay_policy=replay,
            command_transmission=CommandTransmission.NOT_SENT,
            response_progress=ResponseProgress.NONE,
            synchronization=Synchronization.PROVEN,
            attempts=0,
        )

    @staticmethod
    def _frame_preflight_error(
        operation: str,
        replay: ReplayPolicy,
        reason_code: str,
    ) -> TransportIOError:
        return TransportIOError(
            "raw frame request was rejected before transmission",
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
    def _frame_read_error(
        operation: str,
        replay: ReplayPolicy,
        *,
        consumed_bytes: int,
        reason_code: str,
    ) -> TransportIOError:
        partial = consumed_bytes > 0
        return TransportIOError(
            "raw frame read did not complete",
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

    @staticmethod
    def _frame_read_deadline(timeout_ms: int | None) -> float | None:
        if timeout_ms is None:
            return None
        return time.monotonic() + timeout_ms / 1000.0

    def _set_frame_read_timeout(
        self,
        deadline: float | None,
        operation: str,
        replay: ReplayPolicy,
        consumed_bytes: int,
    ) -> None:
        if deadline is None:
            return
        remaining_seconds = deadline - time.monotonic()
        if remaining_seconds <= 0:
            raise self._frame_read_error(
                operation,
                replay,
                consumed_bytes=consumed_bytes,
                reason_code="frame_read_timeout",
            )
        if hasattr(self.session, "timeout"):
            self.session.timeout = remaining_seconds

    def _raise_if_frame_deadline_exhausted(
        self,
        deadline: float | None,
        operation: str,
        replay: ReplayPolicy,
        consumed_bytes: int,
    ) -> None:
        if deadline is not None and time.monotonic() >= deadline:
            raise self._frame_read_error(
                operation,
                replay,
                consumed_bytes=consumed_bytes,
                reason_code="frame_read_timeout",
            )

    def close(self) -> None:
        try:
            self.session.close()
        except Exception as exc:
            raise SessionCloseError([("session", exc)]) from None
