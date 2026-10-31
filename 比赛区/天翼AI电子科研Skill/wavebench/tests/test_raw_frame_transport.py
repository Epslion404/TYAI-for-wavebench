from __future__ import annotations

import pytest

from wavebench.errors import TransportIOError
from wavebench.logging import CommandLogger
from wavebench.transport.guarded import GuardedAuditedTransport
from wavebench.transport.serial_transport import SerialTransport


class _SerialSession:
    def __init__(self, reads: list[bytes] | None = None) -> None:
        self.writes: list[bytes] = []
        self.reads = list(reads or [])

    def write(self, payload: bytes) -> int:
        self.writes.append(payload)
        return len(payload)

    def flush(self) -> None:
        pass

    def read(self, size: int) -> bytes:
        assert size > 0
        return self.reads.pop(0) if self.reads else b""


def test_send_frame_rejects_oversized_payload_before_serial_write() -> None:
    session = _SerialSession()
    transport = SerialTransport("COM7", session, CommandLogger())

    with pytest.raises(TransportIOError) as raised:
        transport.send_frame(b"\x01\x02", max_bytes=1)

    assert raised.value.operation == "send_frame"
    assert raised.value.reason_code == "frame_too_large"
    assert raised.value.attempts == 0
    assert session.writes == []


def test_read_exact_reassembles_split_payload_without_interpreting_delimiter_bytes() -> None:
    session = _SerialSession([b"\x01\x00", b"\x02"])
    transport = SerialTransport("COM7", session, CommandLogger())

    frame = transport.read_exact(3)

    assert frame == b"\x01\x00\x02"


def test_read_exact_reports_a_partial_timeout_as_a_lost_session() -> None:
    transport = SerialTransport("COM7", _SerialSession([b"\x01", b""]), CommandLogger())

    with pytest.raises(TransportIOError) as raised:
        transport.read_exact(2, replay="read_continuation_only")

    assert raised.value.reason_code == "frame_read_timeout"
    assert raised.value.replay_policy.value == "read_continuation_only"
    assert raised.value.response_progress.value == "partial"
    assert raised.value.synchronization.value == "lost"
    assert raised.value.consumed_bytes == 1


def test_read_until_returns_delimited_frame_across_packets() -> None:
    transport = SerialTransport("COM7", _SerialSession([b"\xAA", b"\x01", b"\r", b"\n"]), CommandLogger())

    frame = transport.read_until(b"\r\n", max_bytes=8)

    assert frame == b"\xAA\x01\r\n"


def test_read_until_rejects_an_unterminated_frame_at_its_limit() -> None:
    transport = SerialTransport("COM7", _SerialSession([b"\xAA", b"\x01"]), CommandLogger())

    with pytest.raises(TransportIOError) as raised:
        transport.read_until(b"\r\n", max_bytes=2)

    assert raised.value.reason_code == "frame_too_large"
    assert raised.value.response_progress.value == "partial"
    assert raised.value.synchronization.value == "lost"


def test_guarded_transport_forwards_raw_reads_through_the_session_gate() -> None:
    class FrameBackend:
        resource = "COM7"

        def record_event(self, direction: str, text: str) -> None:
            pass

        def read_exact(self, size: int, *, timeout_ms=None, replay="no_replay") -> bytes:
            assert size == 3
            assert timeout_ms is None
            assert replay == "no_replay"
            return b"\x01\x00\x02"

    transport = GuardedAuditedTransport(FrameBackend())

    assert transport.read_exact(3) == b"\x01\x00\x02"
    assert transport.session_state.health.value == "healthy"


def test_guarded_transport_blocks_raw_frame_sends_in_read_only_mode() -> None:
    class FrameBackend:
        resource = "COM7"

        def record_event(self, direction: str, text: str) -> None:
            pass

        def send_frame(self, frame: bytes, *, max_bytes: int, replay: str) -> None:
            raise AssertionError("read-only guard must stop this before backend I/O")

    transport = GuardedAuditedTransport(FrameBackend(), access="read_only")

    with pytest.raises(Exception, match="access policy"):
        transport.send_frame(b"\x01", max_bytes=8)


def test_guarded_transport_poisoned_after_partial_raw_frame_read() -> None:
    class FrameBackend:
        resource = "COM7"

        def record_event(self, direction: str, text: str) -> None:
            pass

        def read_exact(self, size: int, *, timeout_ms=None, replay="no_replay") -> bytes:
            raise TransportIOError(
                "partial raw frame",
                operation="read_exact",
                phase="reading",
                replay_policy=replay,
                command_transmission="sent",
                response_progress="partial",
                synchronization="lost",
                attempts=1,
                reason_code="frame_read_timeout",
                consumed_bytes=1,
                discarded_bytes=0,
            )

    transport = GuardedAuditedTransport(FrameBackend())

    with pytest.raises(TransportIOError):
        transport.read_exact(2)

    assert transport.session_state.health.value == "poisoned"
