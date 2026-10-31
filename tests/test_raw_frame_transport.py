from __future__ import annotations

import pytest

from wavebench.config import SerialConnectionConfig
from wavebench.errors import SessionHealthError, TransportIOError
from wavebench.logging import CommandLogger
from wavebench.transport.guarded import GuardedAuditedTransport
from wavebench.transport.serial_transport import SerialTransport
from wavebench.transport.session import SessionPurpose, SessionTransactionCoordinator


class _SerialSession:
    timeout = 1.0

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


def test_read_exact_enforces_one_timeout_budget_across_split_reads(monkeypatch) -> None:
    clock = {"now": 0.0}

    class TimedSession:
        timeout = 1.0

        def __init__(self) -> None:
            self.timeouts: list[float] = []
            self.reads = iter([b"\x01", b"\x02"])

        def read(self, size: int) -> bytes:
            self.timeouts.append(self.timeout)
            clock["now"] += 0.03
            return next(self.reads)

    monkeypatch.setattr("wavebench.transport.serial_transport.time.monotonic", lambda: clock["now"])
    session = TimedSession()
    transport = SerialTransport("COM7", session, CommandLogger())

    with pytest.raises(TransportIOError) as raised:
        transport.read_exact(2, timeout_ms=50)

    assert raised.value.reason_code == "frame_read_timeout"
    assert raised.value.consumed_bytes == 2
    assert session.timeouts == pytest.approx([0.05, 0.02])
    assert session.timeout == 1.0


def test_guarded_transport_poisoned_when_complete_frame_arrives_after_deadline(monkeypatch) -> None:
    clock = {"now": 0.0}

    class TimedSession:
        timeout = 1.0

        def read(self, size: int) -> bytes:
            clock["now"] += 0.06
            return b"\x01\x02"

        def close(self) -> None:
            pass

    monkeypatch.setattr("wavebench.transport.serial_transport.time.monotonic", lambda: clock["now"])
    transport = GuardedAuditedTransport(
        SerialTransport("COM7", TimedSession(), CommandLogger())
    )

    with pytest.raises(TransportIOError) as raised:
        transport.read_exact(2, timeout_ms=50)

    assert raised.value.reason_code == "frame_read_timeout"
    assert raised.value.consumed_bytes == 2
    assert transport.session_state.health.value == "poisoned"


def test_read_denied_after_authorized_send_poisons_the_session(monkeypatch) -> None:
    now = [100.0]
    monkeypatch.setattr("wavebench.transport.session.time.monotonic", lambda: now[0])

    class FrameSession:
        timeout = 1.0

        def __init__(self) -> None:
            self.reads = [b"\xAA\xBB"]

        def send_frame(
            self,
            frame: bytes,
            *,
            max_bytes: int,
            replay=None,
        ) -> None:
            pass

        def read_exact(self, size: int, *, timeout_ms=None, replay=None) -> bytes:
            return self.reads.pop(0)

        def close(self) -> None:
            pass

    transport = GuardedAuditedTransport(FrameSession())
    coordinator = SessionTransactionCoordinator(transport.session_state)
    with coordinator.authorize(
        operation_id="raw-exchange",
        purpose=SessionPurpose.RECOVERY,
        allowed_io={"send_frame", "read_exact"},
        fields={"identity"},
        timeout_ms=10,
        max_steps=2,
    ):
        transport.send_frame(b"\x01", max_bytes=4)
        now[0] += 0.02
        with pytest.raises(SessionHealthError):
            transport.read_exact(2)

    assert transport.session_state.health.value == "poisoned"
    with pytest.raises(SessionHealthError):
        transport.read_exact(2)


def test_expired_unrelated_authorization_keeps_a_completed_exchange_healthy(
    monkeypatch,
) -> None:
    now = [100.0]
    monkeypatch.setattr("wavebench.transport.session.time.monotonic", lambda: now[0])

    class FrameSession:
        timeout = 1.0

        def __init__(self) -> None:
            self.reads = [b"\xAA\xBB"]

        def send_frame(self, frame: bytes, *, max_bytes: int, replay=None) -> None:
            pass

        def read_exact(self, size: int, *, timeout_ms=None, replay=None) -> bytes:
            return self.reads.pop(0)

        def close(self) -> None:
            pass

    transport = GuardedAuditedTransport(FrameSession())
    coordinator = SessionTransactionCoordinator(transport.session_state)
    with coordinator.authorize(
        operation_id="raw-exchange",
        purpose=SessionPurpose.RECOVERY,
        allowed_io={"send_frame", "read_exact"},
        fields={"identity"},
        timeout_ms=1000,
        max_steps=2,
    ):
        transport.send_frame(b"\x01", max_bytes=4)
        assert transport.read_exact(2) == b"\xAA\xBB"

    with coordinator.authorize(
        operation_id="unrelated-phase",
        purpose=SessionPurpose.RECOVERY,
        allowed_io={"read_exact"},
        fields={"identity"},
        timeout_ms=10,
        max_steps=1,
    ):
        now[0] += 0.02
        with pytest.raises(SessionHealthError):
            transport.read_exact(2)

    assert transport.session_state.health.value == "healthy"


def test_read_until_returns_delimited_frame_across_packets() -> None:
    transport = SerialTransport("COM7", _SerialSession([b"\xAA", b"\x01", b"\r", b"\n"]), CommandLogger())

    frame = transport.read_until(b"\r\n", max_bytes=8)

    assert frame == b"\xAA\x01\r\n"


def test_read_until_enforces_one_timeout_budget_across_split_reads(monkeypatch) -> None:
    clock = {"now": 0.0}

    class TimedSession:
        timeout = 1.0

        def __init__(self) -> None:
            self.timeouts: list[float] = []
            self.reads = iter([b"\x01", b"\n"])

        def read(self, size: int) -> bytes:
            self.timeouts.append(self.timeout)
            clock["now"] += 0.03
            return next(self.reads)

    monkeypatch.setattr("wavebench.transport.serial_transport.time.monotonic", lambda: clock["now"])
    session = TimedSession()
    transport = SerialTransport("COM7", session, CommandLogger())

    with pytest.raises(TransportIOError) as raised:
        transport.read_until(b"\n", max_bytes=2, timeout_ms=50)

    assert raised.value.reason_code == "frame_read_timeout"
    assert raised.value.consumed_bytes == 2
    assert session.timeouts == pytest.approx([0.05, 0.02])
    assert session.timeout == 1.0


def _read_test_frame(transport, operation: str, **kwargs) -> bytes:
    if operation == "read_exact":
        return transport.read_exact(2, **kwargs)
    return transport.read_until(b"\n", max_bytes=2, **kwargs)


@pytest.mark.parametrize("operation", ["read_exact", "read_until"])
@pytest.mark.parametrize("late_chunk", [False, True], ids=["partial", "late-complete"])
def test_default_frame_deadline_poisons_guarded_session_without_replay(
    monkeypatch, operation: str, late_chunk: bool,
) -> None:
    clock = {"now": 0.0}

    class TimedSession(_SerialSession):
        timeout = 0.05

        def __init__(self) -> None:
            super().__init__([b"A", b"\n", b"N"])
            self.timeouts: list[float] = []

        def read(self, size: int) -> bytes:
            self.timeouts.append(self.timeout)
            if not late_chunk and self.timeout < 0.04:
                clock["now"] += self.timeout
                return b""
            clock["now"] += 0.04
            return super().read(size)

    monkeypatch.setattr("wavebench.transport.serial_transport.time.monotonic", lambda: clock["now"])
    session = TimedSession()
    logger = CommandLogger()

    def open_serial(**kwargs):
        assert kwargs["timeout"] == 0.05
        session.timeout = kwargs["timeout"]
        return session

    monkeypatch.setattr("serial.Serial", open_serial)
    config = SerialConnectionConfig(
        resource="COM7", baudrate=9600, bytesize=8, parity="N", stopbits=1, timeout_ms=50,
    )
    transport = GuardedAuditedTransport(SerialTransport.open(config, logger))

    with pytest.raises(TransportIOError) as raised:
        _read_test_frame(transport, operation, replay="read_continuation_only")

    assert raised.value.reason_code == "frame_read_timeout"
    assert raised.value.operation == operation
    assert raised.value.consumed_bytes == (2 if late_chunk else 1)
    assert raised.value.attempts == 1
    assert raised.value.replay_policy.value == "read_continuation_only"
    assert raised.value.response_progress.value == "partial"
    assert raised.value.synchronization.value == "lost"
    assert session.timeouts == pytest.approx([0.05, 0.01])
    assert session.timeout == 0.05
    assert session.reads == ([b"N"] if late_chunk else [b"\n", b"N"])
    assert session.writes == []
    assert not any(entry.direction == "read_frame" for entry in logger.entries)
    assert transport.session_state.health.value == "poisoned"
    with pytest.raises(SessionHealthError):
        _read_test_frame(transport, operation)
    assert len(session.timeouts) == 2


@pytest.mark.parametrize("operation", ["read_exact", "read_until"])
@pytest.mark.parametrize("timeout_ms", [20, 100], ids=["shorter", "longer"])
def test_explicit_frame_timeout_overrides_connection_timeout(
    monkeypatch, operation: str, timeout_ms: int,
) -> None:
    clock = {"now": 0.0}

    class TimedSession(_SerialSession):
        timeout = 0.05

        def __init__(self) -> None:
            super().__init__([b"A", b"\n"])
            self.timeouts: list[float] = []

        def read(self, size: int) -> bytes:
            self.timeouts.append(self.timeout)
            clock["now"] += 0.03
            return super().read(size)

    monkeypatch.setattr("wavebench.transport.serial_transport.time.monotonic", lambda: clock["now"])
    session = TimedSession()
    transport = SerialTransport("COM7", session, CommandLogger())

    if timeout_ms == 20:
        with pytest.raises(TransportIOError) as raised:
            _read_test_frame(transport, operation, timeout_ms=timeout_ms)
        assert raised.value.reason_code == "frame_read_timeout"
        assert raised.value.consumed_bytes == 1
        assert session.timeouts == pytest.approx([0.02])
    else:
        assert _read_test_frame(transport, operation, timeout_ms=timeout_ms) == b"A\n"
        assert session.timeouts == pytest.approx([0.1, 0.07])
    assert session.timeout == 0.05


@pytest.mark.parametrize("operation", ["read_exact", "read_until"])
@pytest.mark.parametrize("timeout_ms", [None, 50], ids=["default", "explicit"])
def test_successful_frame_restores_timeout_and_leaves_next_frame_unread(
    monkeypatch, operation: str, timeout_ms: int | None,
) -> None:
    clock = {"now": 0.0}

    class TimedSession(_SerialSession):
        timeout = 0.1

        def __init__(self) -> None:
            super().__init__([b"A", b"\n", b"N"])
            self.sizes: list[int] = []
            self.timeouts: list[float] = []

        def read(self, size: int) -> bytes:
            self.sizes.append(size)
            self.timeouts.append(self.timeout)
            clock["now"] += 0.01
            return super().read(size)

    monkeypatch.setattr("wavebench.transport.serial_transport.time.monotonic", lambda: clock["now"])
    session = TimedSession()
    transport = SerialTransport("COM7", session, CommandLogger())

    assert _read_test_frame(transport, operation, timeout_ms=timeout_ms) == b"A\n"

    budget = 0.1 if timeout_ms is None else 0.05
    assert session.timeouts == pytest.approx([budget, budget - 0.01])
    assert session.sizes == ([2, 1] if operation == "read_exact" else [1, 1])
    assert session.reads == [b"N"]
    assert session.timeout == 0.1


@pytest.mark.parametrize("operation", ["read_exact", "read_until"])
@pytest.mark.parametrize("timeout_ms", [None, 50], ids=["default", "explicit"])
def test_frame_backend_exception_restores_timeout(
    monkeypatch, operation: str, timeout_ms: int | None,
) -> None:
    clock = {"now": 0.0}

    class FailingSession(_SerialSession):
        timeout = 0.1

        def read(self, size: int) -> bytes:
            if self.reads:
                clock["now"] += 0.01
                return super().read(size)
            raise OSError("serial disconnected")

    monkeypatch.setattr("wavebench.transport.serial_transport.time.monotonic", lambda: clock["now"])
    session = FailingSession([b"A"])
    transport = SerialTransport("COM7", session, CommandLogger())

    with pytest.raises(TransportIOError) as raised:
        _read_test_frame(transport, operation, timeout_ms=timeout_ms)

    assert raised.value.reason_code == "frame_read_failed"
    assert raised.value.consumed_bytes == 1
    assert session.timeout == 0.1


@pytest.mark.parametrize("operation", ["read_exact", "read_until"])
@pytest.mark.parametrize("default_timeout", [None, 0.0, float("inf"), float("nan"), "missing"])
def test_frame_requires_a_finite_default_or_explicit_timeout(
    operation: str, default_timeout,
) -> None:
    class Session:
        def read(self, size: int) -> bytes:
            raise AssertionError("invalid default timeout must fail before I/O")

    session = Session()
    if default_timeout != "missing":
        session.timeout = default_timeout
    transport = GuardedAuditedTransport(SerialTransport("COM7", session, CommandLogger()))

    with pytest.raises(TransportIOError) as raised:
        _read_test_frame(transport, operation)

    assert raised.value.reason_code == "frame_timeout_invalid"
    assert raised.value.attempts == 0
    assert raised.value.consumed_bytes == 0
    assert transport.session_state.health.value == "healthy"


@pytest.mark.parametrize("operation", ["read_exact", "read_until"])
def test_explicit_frame_timeout_works_with_unbounded_session(monkeypatch, operation: str) -> None:
    monkeypatch.setattr("wavebench.transport.serial_transport.time.monotonic", lambda: 0.0)
    session = _SerialSession([b"A", b"\n"])
    session.timeout = None
    transport = SerialTransport("COM7", session, CommandLogger())

    assert _read_test_frame(transport, operation, timeout_ms=50) == b"A\n"
    assert session.timeout is None


@pytest.mark.parametrize("operation", ["read_exact", "read_until"])
def test_raw_frame_read_rejects_safe_replay_before_io(operation: str) -> None:
    session = _SerialSession([b"A", b"\n"])
    transport = GuardedAuditedTransport(SerialTransport("COM7", session, CommandLogger()))

    with pytest.raises(TransportIOError) as raised:
        _read_test_frame(transport, operation, replay="safe_to_replay")

    assert raised.value.reason_code == "frame_replay_unsupported"
    assert raised.value.attempts == 0
    assert session.reads == [b"A", b"\n"]
    assert session.writes == []
    assert transport.session_state.health.value == "healthy"


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


@pytest.mark.parametrize(
    ("operation", "result"),
    [
        ("read_exact", b"\x01"),
        ("read_until", b"\x01\x02"),
    ],
)
def test_guarded_transport_rejects_frame_results_that_violate_the_contract(
    operation: str,
    result: bytes,
) -> None:
    class FrameBackend:
        resource = "COM7"

        def __init__(self) -> None:
            self.closed = False

        def record_event(self, direction: str, text: str) -> None:
            pass

        def close(self) -> None:
            self.closed = True

        def read_exact(self, size: int, *, timeout_ms=None, replay="no_replay") -> bytes:
            return result

        def read_until(self, delimiter: bytes, *, max_bytes: int, timeout_ms=None, replay="no_replay") -> bytes:
            return result

    backend = FrameBackend()
    transport = GuardedAuditedTransport(backend)

    with pytest.raises(TransportIOError) as raised:
        if operation == "read_exact":
            transport.read_exact(3)
        else:
            transport.read_until(b"\n", max_bytes=2)

    assert raised.value.reason_code == "frame_contract_violation"
    assert transport.session_state.health.value == "poisoned"
    assert backend.closed is True


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
