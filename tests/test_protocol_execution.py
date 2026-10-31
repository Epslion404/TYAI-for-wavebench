"""Offline execution contracts for declared protocol request mappings.

Covers issue #3's execution slice: request mapping -> transport -> response
framing, response timeout / inter-frame gap, and replay/session behaviour.
No hardware, optional backend or real clock is used.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from wavebench.errors import ConfigError, SessionHealthError, TransportIOError
from wavebench.plugins.protocol import _parse_protocol_plugin, load_protocol_plugin
from wavebench.plugins.protocol_runtime import (
    DeclarativeProtocolRuntime,
    ProtocolResponse,
)
from wavebench.transport.contracts import (
    CommandTransmission,
    ReplayPolicy,
    ResponseProgress,
    Synchronization,
    TransportPhase,
)
from wavebench.transport.guarded import GuardedAuditedTransport
from wavebench.transport.session import (
    SessionHealth,
    SessionPurpose,
    SessionTransactionCoordinator,
)


METADATA = {
    "driver_id": "example.exec-bus",
    "kind": "scope",
    "display_name": "Example Executable Bus",
    "manufacturer": "Example",
    "models": ["EXEC1"],
    "capabilities": ["scope.idn"],
    "summary": "Offline declarative execution contract.",
}


def declaration(requests, framing="delimiter", **changes):
    protocol = {
        "framing": framing,
        "magic_hex": "A5",
        "header_bytes": 1,
        "max_frame_bytes": 64,
        "sample_type": "u16",
        "endian": "little",
        "response_timeout_ms": 800,
        "inter_frame_gap_ms": 0,
    }
    if framing == "delimiter":
        protocol.update(delimiter_hex="0A", escape_hex="5C")
    elif framing == "fixed":
        protocol.update(fixed_length=4)
    else:
        protocol.update(
            header_bytes=2,
            length_offset=1,
            length_bytes=1,
            length_endian="little",
            length_includes_header=True,
        )
    protocol.update(changes)
    if protocol.get("escape_hex") is None:
        protocol.pop("escape_hex", None)
    raw = {**METADATA, "protocol": {**protocol, "requests": requests}}
    return _parse_protocol_plugin(raw)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


class FakeTransport:
    """Serve one byte stream and record every raw-frame call."""

    resource = "fake://protocol"

    def __init__(self, response: bytes = b"") -> None:
        self.response = bytes(response)
        self.sent: list[tuple[bytes, int, ReplayPolicy]] = []
        self.exact_reads: list[tuple[int, int | None, ReplayPolicy]] = []
        self.until_reads: list[tuple[bytes, int, int | None, ReplayPolicy]] = []
        self._cursor = 0

    def record_event(self, direction: str, text: str) -> None:
        pass

    def send_frame(
        self,
        frame: bytes,
        *,
        max_bytes: int,
        replay: ReplayPolicy = ReplayPolicy.NO_REPLAY,
    ) -> None:
        self.sent.append((frame, max_bytes, replay))

    def read_exact(
        self,
        size: int,
        *,
        timeout_ms: int | None = None,
        replay: ReplayPolicy = ReplayPolicy.NO_REPLAY,
    ) -> bytes:
        self.exact_reads.append((size, timeout_ms, replay))
        if self._cursor + size <= len(self.response):
            chunk = self.response[self._cursor:self._cursor + size]
            self._cursor += size
            return chunk
        raise self._timeout()

    def read_until(
        self,
        delimiter: bytes,
        *,
        max_bytes: int,
        timeout_ms: int | None = None,
        replay: ReplayPolicy = ReplayPolicy.NO_REPLAY,
    ) -> bytes:
        self.until_reads.append((delimiter, max_bytes, timeout_ms, replay))
        index = self.response.find(delimiter, self._cursor)
        if index < 0:
            raise self._timeout()
        end = index + len(delimiter)
        if end - self._cursor > max_bytes:
            raise TransportIOError(
                "fake protocol frame exceeded the declared budget",
                operation="read_until",
                phase=TransportPhase.PARSING,
                replay_policy=replay,
                command_transmission=CommandTransmission.SENT,
                response_progress=ResponseProgress.COMPLETE,
                synchronization=Synchronization.LOST,
                attempts=1,
                reason_code="frame_too_large",
                consumed_bytes=end - self._cursor,
                discarded_bytes=end - self._cursor,
            )
        chunk = self.response[self._cursor:end]
        self._cursor = end
        return chunk

    def _timeout(self) -> TransportIOError:
        return TransportIOError(
            "fake protocol read timed out",
            operation="read_exact",
            phase=TransportPhase.READING,
            replay_policy=ReplayPolicy.NO_REPLAY,
            command_transmission=CommandTransmission.SENT,
            response_progress=ResponseProgress.NONE,
            synchronization=Synchronization.UNPROVEN,
            attempts=1,
            reason_code="frame_read_timeout",
            consumed_bytes=0,
            discarded_bytes=0,
        )


class ClockedTransport(FakeTransport):
    """Simulate reads that take wall time on the injected clock."""

    def __init__(self, response: bytes = b"", *, delay: float, clock: FakeClock) -> None:
        super().__init__(response)
        self.delay = delay
        self.clock = clock

    def read_exact(
        self,
        size: int,
        *,
        timeout_ms: int | None = None,
        replay: ReplayPolicy = ReplayPolicy.NO_REPLAY,
    ) -> bytes:
        result = super().read_exact(size, timeout_ms=timeout_ms, replay=replay)
        self.clock.now += self.delay
        return result

    def read_until(
        self,
        delimiter: bytes,
        *,
        max_bytes: int,
        timeout_ms: int | None = None,
        replay: ReplayPolicy = ReplayPolicy.NO_REPLAY,
    ) -> bytes:
        result = super().read_until(
            delimiter,
            max_bytes=max_bytes,
            timeout_ms=timeout_ms,
            replay=replay,
        )
        self.clock.now += self.delay
        return result


def runtime_for(declaration_value, transport, *, clock=None):
    clock = clock or FakeClock()
    return DeclarativeProtocolRuntime(
        declaration_value,
        transport,
        clock=clock,
        sleep=clock.sleep,
    )


class UnsupportedReadTransport(FakeTransport):
    """Sent frames work, but the backend cannot read a delimiter stream."""

    def read_until(
        self,
        delimiter: bytes,
        *,
        max_bytes: int,
        timeout_ms: int | None = None,
        replay: ReplayPolicy = ReplayPolicy.NO_REPLAY,
    ) -> bytes:
        raise TransportIOError(
            "fake backend does not support delimited reads",
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


class TestRequestExecution:
    def test_delimited_round_trip_uses_the_request_timeout(self):
        fake = FakeTransport(b"\xa5\x02\n")
        declared = declaration(
            [{"name": "read_status", "request_hex": "A501", "response_timeout_ms": 250}],
            escape_hex=None,
        )
        runtime = runtime_for(declared, fake)

        result = runtime.request("read_status")

        assert isinstance(result, ProtocolResponse)
        assert result.name == "read_status"
        assert result.request_wire == b"\xa5\x01\n"
        assert result.response_wire == b"\xa5\x02\n"
        assert result.response_timeout_ms == 250
        assert fake.sent == [(b"\xa5\x01\n", 64, ReplayPolicy.NO_REPLAY)]
        assert fake.until_reads == [(b"\n", 64, 250, ReplayPolicy.NO_REPLAY)]
        assert fake.exact_reads == []

    def test_frame_timeout_is_the_fallback_when_request_does_not_override(self):
        fake = FakeTransport(b"\xa5\x02\n")
        runtime = runtime_for(
            declaration([{"name": "read_status", "request_hex": "A501"}], escape_hex=None),
            fake,
        )

        result = runtime.request("read_status")

        assert result.response_timeout_ms == 800
        assert fake.until_reads[0][2] == 800
        assert fake.until_reads[0][3] is ReplayPolicy.NO_REPLAY

    def test_escaped_delimiter_inside_the_response_is_not_a_frame_boundary(self):
        wire = b"\xa5\x5c\x0a\x02\n"
        fake = FakeTransport(wire)
        runtime = runtime_for(declaration([{"name": "read", "request_hex": "A501"}]), fake)

        result = runtime.request("read")

        assert result.response_wire == wire
        assert len(fake.exact_reads) == len(wire)
        assert fake.until_reads == []

    def test_fixed_framing_reads_the_declared_length_in_one_call(self):
        fake = FakeTransport(b"\xa5\x02\x00\x00")
        declared = declaration(
            [{"name": "read", "request_hex": "A5010000"}],
            framing="fixed",
        )
        runtime = runtime_for(declared, fake)

        result = runtime.request("read")

        assert result.request_wire == b"\xa5\x01\x00\x00"
        assert result.response_wire == b"\xa5\x02\x00\x00"
        assert [size for size, _, _ in fake.exact_reads] == [4]

    def test_length_prefixed_reads_header_then_the_declared_remainder(self):
        fake = FakeTransport(b"\xa5\x03\x7a")
        declared = declaration(
            [{"name": "read", "request_hex": "A502"}],
            framing="length_prefixed",
        )
        runtime = runtime_for(declared, fake)

        result = runtime.request("read")

        assert result.request_wire == b"\xa5\x02"
        assert result.response_wire == b"\xa5\x03\x7a"
        assert [size for size, _, _ in fake.exact_reads] == [2, 1]

    def test_request_names_follow_manifest_order(self):
        declared = declaration(
            [
                {"name": "first", "request_hex": "A501"},
                {"name": "second", "request_hex": "A502"},
            ]
        )
        runtime = runtime_for(declared, FakeTransport())

        assert runtime.request_names == ("first", "second")
        assert runtime.has_request("first")
        assert not runtime.has_request("missing")

    def test_inter_frame_gap_is_measured_from_the_previous_exchange(self):
        clock = FakeClock()
        fake = FakeTransport(b"\xa5\x02\n" * 2)
        declared = declaration(
            [{"name": "read", "request_hex": "A501"}],
            inter_frame_gap_ms=100,
        )
        runtime = runtime_for(declared, fake, clock=clock)

        runtime.request("read")
        assert clock.slept == []

        clock.now = 0.02
        runtime.request("read")

        assert clock.slept == [pytest.approx(0.08)]

    def test_runtime_drives_the_serial_raw_frame_contract(self):
        from wavebench.logging import CommandLogger
        from wavebench.transport.serial_transport import SerialTransport

        class ByteSession:
            timeout = 1.0

            def __init__(self, reads):
                self.reads = list(reads)
                self.writes: list[bytes] = []

            def write(self, payload: bytes) -> int:
                self.writes.append(payload)
                return len(payload)

            def flush(self) -> None:
                pass

            def read(self, size: int) -> bytes:
                assert size > 0
                return self.reads.pop(0) if self.reads else b""

        session = ByteSession([b"\xa5", b"\x02", b"\n"])
        transport = SerialTransport("COM7", session, CommandLogger())
        runtime = runtime_for(
            declaration([{"name": "read", "request_hex": "A501"}]),
            transport,
        )

        result = runtime.request("read")

        assert session.writes == [b"\xa5\x01\n"]
        assert result.response_wire == b"\xa5\x02\n"


class TestRequestPreflight:
    def test_encode_request_appends_the_delimiter_without_escape(self):
        declared = declaration([], escape_hex=None)
        assert declared.frame.encode_request(b"\xa5\x01") == b"\xa5\x01\n"

    def test_encode_request_escapes_an_embedded_delimiter(self):
        declared = declaration([])
        assert declared.frame.encode_request(b"\xa5\x0a\x01") == b"\xa5\x5c\x0a\x01\n"

    def test_unknown_request_fails_before_any_io(self):
        fake = FakeTransport(b"\xa5\x02\n")
        runtime = runtime_for(declaration([{"name": "read", "request_hex": "A501"}]), fake)

        with pytest.raises(ConfigError, match="not declared"):
            runtime.request("missing")

        assert fake.sent == []
        assert fake.exact_reads == []
        assert fake.until_reads == []

    def test_non_replayable_policy_is_rejected_before_any_io(self):
        fake = FakeTransport(b"\xa5\x02\n")
        runtime = runtime_for(declaration([{"name": "read", "request_hex": "A501"}]), fake)

        for policy in (ReplayPolicy.SAFE_TO_REPLAY, ReplayPolicy.READ_CONTINUATION_ONLY):
            with pytest.raises(TransportIOError) as raised:
                runtime.request("read", replay=policy)
            assert raised.value.reason_code == "request_replay_unsupported"
            assert raised.value.phase is TransportPhase.BEFORE_SEND
            assert raised.value.command_transmission is CommandTransmission.NOT_SENT
            assert raised.value.attempts == 0

        assert fake.sent == []
        assert fake.exact_reads == []
        assert fake.until_reads == []

    def test_declaration_without_requests_is_executable_but_empty(self):
        runtime = runtime_for(declaration([]), FakeTransport())
        assert runtime.request_names == ()
        with pytest.raises(ConfigError, match="not declared"):
            runtime.request("read")

    def test_unencodable_request_frame_is_rejected_at_parse_time(self):
        # Fixed framing requires a complete four-byte request frame.
        with pytest.raises(ConfigError, match="not a valid"):
            declaration([{"name": "bad", "request_hex": "A501"}], framing="fixed")

    def test_documented_example_requests_encode_for_their_framing(self):
        path = (
            Path(__file__).resolve().parents[1]
            / "docs/project/reference/plugins/protocol-plugin.example.toml"
        )
        declared = load_protocol_plugin(path)

        for request in declared.requests:
            assert declared.frame.encode_request(request.request_hex)


class TestResponseFailureAndSessionHealth:
    def test_escaped_response_without_a_terminator_exceeds_the_frame_budget(self):
        fake = FakeTransport(b"\x5c\x0a" * 32)  # 64 escaped-literal bytes, no terminator
        guarded = GuardedAuditedTransport(fake)
        runtime = runtime_for(declaration([{"name": "read", "request_hex": "A501"}]), guarded)

        with pytest.raises(TransportIOError) as raised:
            runtime.request("read")

        assert raised.value.reason_code == "frame_too_large"
        assert raised.value.synchronization is Synchronization.LOST
        assert len(fake.exact_reads) == 64
        assert guarded.session_state.health is SessionHealth.POISONED

    def test_declared_length_overflow_fails_before_the_remainder_read(self):
        fake = FakeTransport(b"\xa5\xff")
        guarded = GuardedAuditedTransport(fake)
        declared = declaration(
            [{"name": "read", "request_hex": "A502"}],
            framing="length_prefixed",
        )
        runtime = runtime_for(declared, guarded)

        with pytest.raises(TransportIOError) as raised:
            runtime.request("read")

        assert raised.value.reason_code == "frame_too_large"
        assert raised.value.synchronization is Synchronization.LOST
        assert [size for size, _, _ in fake.exact_reads] == [2]
        assert guarded.session_state.health is SessionHealth.POISONED

    def test_partial_response_timeout_poisons_the_guarded_session(self):
        fake = FakeTransport(b"\xa5\x02")  # delimiter never arrives
        guarded = GuardedAuditedTransport(fake)
        runtime = runtime_for(declaration([{"name": "read", "request_hex": "A501"}]), guarded)

        with pytest.raises(TransportIOError) as raised:
            runtime.request("read")

        assert raised.value.reason_code == "frame_read_timeout"
        assert guarded.session_state.health is SessionHealth.POISONED

        with pytest.raises(SessionHealthError):
            runtime.request("read")

    def test_trickled_response_cannot_exceed_the_declared_timeout(self):
        # 1 ms per byte would satisfy every individual read budget, but the
        # declared 3 ms covers the whole response.
        clock = FakeClock()
        fake = ClockedTransport(b"\x02" * 20 + b"\n", delay=0.001, clock=clock)
        guarded = GuardedAuditedTransport(fake)
        declared = declaration(
            [{"name": "read", "request_hex": "A501", "response_timeout_ms": 3}]
        )
        runtime = runtime_for(declared, guarded, clock=clock)

        with pytest.raises(TransportIOError) as raised:
            runtime.request("read")

        assert raised.value.reason_code == "frame_read_timeout"
        assert raised.value.phase is TransportPhase.READING
        assert raised.value.command_transmission is CommandTransmission.SENT
        assert clock.now >= 0.003
        assert len(fake.exact_reads) <= 4
        assert guarded.session_state.health is SessionHealth.POISONED

    def test_late_complete_response_is_rejected_as_a_timeout(self):
        clock = FakeClock()
        fake = ClockedTransport(b"\xa5\x02\n", delay=0.02, clock=clock)
        guarded = GuardedAuditedTransport(fake)
        declared = declaration(
            [{"name": "read", "request_hex": "A501", "response_timeout_ms": 3}],
            escape_hex=None,
        )
        runtime = runtime_for(declared, guarded, clock=clock)

        with pytest.raises(TransportIOError) as raised:
            runtime.request("read")

        assert raised.value.reason_code == "frame_read_timeout"
        assert raised.value.response_progress is ResponseProgress.PARTIAL
        assert guarded.session_state.health is SessionHealth.POISONED

    def test_expired_authorization_after_send_blocks_the_next_request(self, monkeypatch):
        # A slow send consumes the authorization's deadline before the read.
        now = [100.0]

        class SlowSendTransport(FakeTransport):
            def send_frame(self, frame, *, max_bytes, replay=ReplayPolicy.NO_REPLAY):
                super().send_frame(frame, max_bytes=max_bytes, replay=replay)
                now[0] += 0.02

        monkeypatch.setattr(
            "wavebench.transport.session.time.monotonic",
            lambda: now[0],
        )
        fake = SlowSendTransport(b"\xa5\x02\n")
        guarded = GuardedAuditedTransport(fake)
        coordinator = SessionTransactionCoordinator(guarded.session_state)
        runtime = runtime_for(
            declaration([{"name": "read", "request_hex": "A501"}], escape_hex=None),
            guarded,
        )

        with coordinator.authorize(
            operation_id="raw-exchange",
            purpose=SessionPurpose.RECOVERY,
            allowed_io={"send_frame", "read_until"},
            fields={"identity"},
            timeout_ms=10,
            max_steps=2,
        ):
            with pytest.raises(SessionHealthError):
                runtime.request("read")

        assert fake.until_reads == []
        assert guarded.session_state.health is SessionHealth.POISONED
        with pytest.raises(SessionHealthError):
            runtime.request("read")
        assert fake.until_reads == []

    def test_read_side_preflight_failure_after_send_poisons_the_session(self):
        # send_frame succeeds, so the request is on the bus even though the
        # backend reports the read operation as unsupported.
        fake = UnsupportedReadTransport()
        guarded = GuardedAuditedTransport(fake)
        runtime = runtime_for(
            declaration([{"name": "read", "request_hex": "A501"}], escape_hex=None),
            guarded,
        )

        with pytest.raises(TransportIOError) as raised:
            runtime.request("read")

        assert raised.value.reason_code == "raw_frame_unsupported"
        assert fake.sent == [(b"\xa5\x01\n", 64, ReplayPolicy.NO_REPLAY)]
        assert guarded.session_state.health is SessionHealth.POISONED

        with pytest.raises(SessionHealthError):
            runtime.request("read")

    def test_invalid_response_checksum_poisons_the_guarded_session(self):
        fake = FakeTransport(b"\xa5\x02\x00\x00\n")
        guarded = GuardedAuditedTransport(fake)
        declared = declaration(
            [{"name": "read", "request_hex": "A501BB20"}],
            checksum="crc16_modbus",
        )
        runtime = runtime_for(declared, guarded)

        with pytest.raises(TransportIOError) as raised:
            runtime.request("read")

        assert raised.value.reason_code == "frame_checksum_failed"
        assert raised.value.synchronization is Synchronization.LOST
        assert guarded.session_state.health is SessionHealth.POISONED

        with pytest.raises(SessionHealthError):
            runtime.request("read")
