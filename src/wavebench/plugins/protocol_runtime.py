"""Execute declared protocol request mappings over an instrument transport.

``DeclarativeProtocolPlugin`` describes framing, checksums and named request
frames; this module joins that declaration with a raw-frame
:class:`InstrumentTransport`.  It stops at the wire frame: sample decoding and
driver-level semantics belong to later protocol contracts.

Every declared request is a transmission, so it runs with
``ReplayPolicy.NO_REPLAY`` and any other replay policy is rejected before I/O.
Response framing, per-request response timeouts and the inter-frame gap come
from the declaration.  Transport failures keep their structured
:class:`TransportIOError`, allowing the guarded session to poison a connection
whose byte position is unknown.  A response that the declaration itself cannot
frame also poisons a guarded session, because later bytes can no longer be
attributed to a known stream position.
"""

from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Any, Callable

from wavebench.errors import ConfigError, TransportIOError
from wavebench.transport.base import InstrumentTransport
from wavebench.transport.contracts import (
    CommandTransmission,
    ReplayPolicy,
    ResponseProgress,
    Synchronization,
    TransportPhase,
)
from wavebench.transport.session import SessionHealth

from .protocol import DeclarativeProtocolPlugin, FrameDefinition, RequestDefinition


@dataclass(frozen=True)
class ProtocolResponse:
    """One completed declared request/response exchange."""

    name: str
    request_wire: bytes
    response_wire: bytes
    response_timeout_ms: int
    elapsed_ms: int


class DeclarativeProtocolRuntime:
    """Send declared request frames and split one framed response per request.

    The runtime is intentionally independent of driver services.  It owns only
    the request -> transmit -> response-framing exchange; authorization,
    access policy and session recovery stay with the guarded transport.
    """

    def __init__(
        self,
        declaration: DeclarativeProtocolPlugin,
        transport: InstrumentTransport,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not isinstance(declaration, DeclarativeProtocolPlugin):
            raise ConfigError(
                "declarative protocol runtime requires a parsed protocol declaration"
            )
        self._frame: FrameDefinition = declaration.frame
        self._transport = transport
        self._clock = clock
        self._sleep = sleep
        self._requests = {
            definition.name: definition for definition in declaration.requests
        }
        self._last_activity: float | None = None

    @property
    def request_names(self) -> tuple[str, ...]:
        """Declared request names in manifest order."""

        return tuple(self._requests)

    def has_request(self, name: str) -> bool:
        return name in self._requests

    def request(
        self,
        name: str,
        *,
        replay: ReplayPolicy = ReplayPolicy.NO_REPLAY,
    ) -> ProtocolResponse:
        """Transmit one declared request and return its framed response.

        Unknown names and replay policies other than ``NO_REPLAY`` fail before
        any byte is sent.  The response budget is the request's
        ``response_timeout_ms`` overriding the frame-level default.
        """

        definition = self._request_definition(name)
        replay = ReplayPolicy(replay)
        if replay is not ReplayPolicy.NO_REPLAY:
            raise self._preflight_error(replay, "request_replay_unsupported")
        timeout_ms = (
            definition.response_timeout_ms
            if definition.response_timeout_ms is not None
            else self._frame.response_timeout_ms
        )
        request_wire = self._encode_request(definition)
        lock = self._transaction_lock()
        if lock is None:
            return self._exchange(definition, request_wire, timeout_ms)
        # A guarded session shares one transaction lock.  Holding it for the
        # whole exchange prevents another caller from interleaving its request
        # between this send and its response read.
        with lock:
            return self._exchange(definition, request_wire, timeout_ms)

    def _exchange(
        self,
        definition: RequestDefinition,
        request_wire: bytes,
        timeout_ms: int,
    ) -> ProtocolResponse:
        self._wait_for_gap()
        started = self._clock()
        self._transport.send_frame(
            request_wire,
            max_bytes=self._frame.max_frame_bytes,
            replay=ReplayPolicy.NO_REPLAY,
        )
        try:
            response_wire = self._read_response(timeout_ms)
        except TransportIOError as exc:
            if exc.command_transmission is CommandTransmission.NOT_SENT:
                # The request was already transmitted.  A read-side preflight
                # failure (for example an unsupported raw-frame operation)
                # still leaves an unread response on the connection.
                self._mark_desynchronized("protocol_response_incomplete")
            raise
        finally:
            # The gap is measured from the last bus activity, including a
            # failed or partial response read.
            self._last_activity = self._clock()
        elapsed_ms = max(int(round((self._clock() - started) * 1000.0)), 0)
        return ProtocolResponse(
            name=definition.name,
            request_wire=request_wire,
            response_wire=response_wire,
            response_timeout_ms=timeout_ms,
            elapsed_ms=elapsed_ms,
        )

    def _read_response(self, timeout_ms: int) -> bytes:
        deadline = self._clock() + timeout_ms / 1000.0
        if self._frame.framing == "fixed":
            fixed_length = self._frame.fixed_length
            assert fixed_length is not None
            return self._split(self._read_exact(fixed_length, deadline))
        if self._frame.framing == "length_prefixed":
            header = self._read_exact(self._frame.header_bytes, deadline)
            expected = self._expected_frame_bytes(header)
            remainder = expected - len(header)
            if remainder:
                header += self._read_exact(remainder, deadline)
            return self._split(header)
        return self._read_delimited(deadline)

    def _read_delimited(self, deadline: float) -> bytes:
        delimiter = self._frame.delimiter
        assert delimiter is not None
        if self._frame.escape is None:
            wire = self._transport.read_until(
                delimiter,
                max_bytes=self._frame.max_frame_bytes,
                timeout_ms=self._remaining_ms(deadline),
                replay=ReplayPolicy.NO_REPLAY,
            )
            return self._split(wire)
        return self._read_escaped_delimited(deadline)

    def _read_escaped_delimited(self, deadline: float) -> bytes:
        """Scan an escape-stuffed delimiter stream without rescanning old bytes.

        ``escape_hex`` is only valid for single-byte delimiter framing, so a
        two-state scan (after-escape / literal) finds the terminator in linear
        time even when the peer sends a long run of escaped delimiters.
        """

        delimiter = self._frame.delimiter
        escape = self._frame.escape
        assert delimiter is not None and len(delimiter) == 1
        assert escape is not None and len(escape) == 1
        wire = bytearray()
        after_escape = False
        while True:
            wire += self._read_exact(1, deadline)
            byte = wire[-1]
            if after_escape:
                after_escape = False
            elif byte == escape[0]:
                after_escape = True
            elif byte == delimiter[0]:
                return self._split(bytes(wire))
            if len(wire) >= self._frame.max_frame_bytes:
                error = self._response_error(
                    "frame_too_large",
                    consumed_bytes=len(wire),
                    progress=ResponseProgress.PARTIAL,
                )
                self._mark_desynchronized("protocol_frame_invalid")
                raise error

    def _split(self, wire: bytes) -> bytes:
        try:
            frame, remaining = self._frame.extract_stream_frame(wire)
        except TransportIOError:
            self._mark_desynchronized("protocol_frame_invalid")
            raise
        if frame is None:
            error = self._response_error(
                "response_frame_incomplete",
                consumed_bytes=len(wire),
                progress=(
                    ResponseProgress.PARTIAL if wire else ResponseProgress.NONE
                ),
            )
            self._mark_desynchronized("protocol_frame_incomplete")
            raise error
        if remaining:
            error = self._response_error(
                "response_trailing_bytes",
                consumed_bytes=len(wire),
                progress=ResponseProgress.COMPLETE,
            )
            self._mark_desynchronized("protocol_stream_desync")
            raise error
        return frame

    def _expected_frame_bytes(self, header: bytes) -> int:
        try:
            return self._frame.expected_frame_bytes(header)
        except TransportIOError:
            self._mark_desynchronized("protocol_frame_invalid")
            raise

    def _read_exact(self, size: int, deadline: float) -> bytes:
        return self._transport.read_exact(
            size,
            timeout_ms=self._remaining_ms(deadline),
            replay=ReplayPolicy.NO_REPLAY,
        )

    def _remaining_ms(self, deadline: float) -> int:
        # The transport requires a positive timeout; a zero/negative remainder
        # becomes a 1 ms read so the backend still produces the structured
        # timeout that drives session health.
        return max(int((deadline - self._clock()) * 1000.0), 1)

    def _wait_for_gap(self) -> None:
        gap_ms = self._frame.inter_frame_gap_ms
        if gap_ms <= 0 or self._last_activity is None:
            return
        delay = gap_ms / 1000.0 - (self._clock() - self._last_activity)
        if delay > 0:
            self._sleep(delay)

    def _request_definition(self, name: str) -> RequestDefinition:
        if not isinstance(name, str) or not name:
            raise ConfigError("protocol request name must be a non-empty string")
        definition = self._requests.get(name)
        if definition is None:
            raise ConfigError(f"protocol request {name!r} is not declared")
        return definition

    def _encode_request(self, definition: RequestDefinition) -> bytes:
        try:
            return self._frame.encode_request(definition.request_hex)
        except TransportIOError as exc:
            # A declaration that cannot be encoded never reached the wire.
            raise self._preflight_error(
                ReplayPolicy.NO_REPLAY,
                exc.reason_code or "request_frame_invalid",
            ) from None

    def _transaction_lock(self) -> Any:
        state = getattr(self._transport, "session_state", None)
        return getattr(state, "transaction_lock", None)

    def _mark_desynchronized(self, reason: str) -> None:
        """Poison a guarded session after a response framing violation.

        The transport only observes byte-level reads.  Once the declared frame
        cannot be split, a later read cannot be attributed to the next exchange,
        so the connection epoch must not stay usable.
        """

        state = getattr(self._transport, "session_state", None)
        degrade = getattr(state, "degrade", None)
        if not callable(degrade):
            return
        try:
            degrade(SessionHealth.POISONED, reason=reason)
        except ValueError:
            # A closed or already-poisoned epoch is at least as restrictive.
            pass

    @staticmethod
    def _preflight_error(replay: ReplayPolicy, reason_code: str) -> TransportIOError:
        return TransportIOError(
            "declared protocol request was rejected before transmission",
            operation="protocol.request",
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
    def _response_error(
        reason_code: str,
        *,
        consumed_bytes: int,
        progress: ResponseProgress,
    ) -> TransportIOError:
        return TransportIOError(
            "declared protocol response could not be framed",
            operation="protocol.request",
            phase=TransportPhase.PARSING,
            replay_policy=ReplayPolicy.NO_REPLAY,
            command_transmission=CommandTransmission.SENT,
            response_progress=progress,
            synchronization=(
                Synchronization.LOST
                if progress is not ResponseProgress.NONE
                else Synchronization.UNPROVEN
            ),
            attempts=1,
            reason_code=reason_code,
            consumed_bytes=consumed_bytes,
            discarded_bytes=consumed_bytes,
        )


__all__ = ["DeclarativeProtocolRuntime", "ProtocolResponse"]
