"""Offline CAN contract regressions; no python-can or physical bus required."""

import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from wavebench.config import CanConnectionConfig
from wavebench.errors import SessionCloseError, SessionHealthError, TransportIOError
from wavebench.logging import CommandLogger
from wavebench.transport.can_transport import CanTransport
from wavebench.transport.contracts import ReplayPolicy
from wavebench.transport.guarded import GuardedAuditedTransport


def transport(*messages):
    bus = Mock()
    bus.recv.side_effect = [
        SimpleNamespace(data=item) if isinstance(item, bytes) else item for item in messages
    ]
    return CanTransport("fake-bus", bus, CommandLogger(), CanConnectionConfig(channel="fake-bus"))


def assert_poisoned_without_further_io(guarded):
    backend = guarded.inner
    assert not backend._pending
    calls = list(backend.bus.mock_calls)
    assert guarded.session_state.health.value == "poisoned"
    for operation in (
        lambda: guarded.read_exact(1),
        lambda: guarded.read_until(b"\n", max_bytes=4),
        lambda: guarded.send_frame(b"a", max_bytes=8),
        lambda: guarded.write("a"),
    ):
        with pytest.raises(SessionHealthError):
            operation()
    assert backend.bus.mock_calls == calls
    guarded.close()
    backend.close()
    backend.bus.shutdown.assert_called_once()
    assert not backend._pending


@pytest.mark.parametrize("payload", [b"123456\n", b"1234567"])
def test_delimited_overflow_is_structured_and_poisons_session(payload):
    backend = transport(payload)
    guarded = GuardedAuditedTransport(backend)
    with pytest.raises(TransportIOError) as caught:
        guarded.read_until(b"\n", max_bytes=4, timeout_ms=100)
    assert caught.value.reason_code == "frame_too_large"
    assert caught.value.consumed_bytes == len(payload)
    assert_poisoned_without_further_io(guarded)
    assert backend.bus.recv.call_count == 1


def test_delimiter_within_bound_retains_only_following_bytes():
    backend = transport(b"abc\nxyz")
    assert backend.read_until(b"\n", max_bytes=4) == b"abc\n"
    assert backend.read_exact(3) == b"xyz"
    assert backend.bus.recv.call_count == 1


@pytest.mark.parametrize("operation", ["read_exact", "read_until"])
@pytest.mark.parametrize("failure", [None, RuntimeError("private-payload")])
@pytest.mark.parametrize("prefix", [(), (b"a",)])
def test_read_failure_has_sanitized_contract(operation, failure, prefix):
    backend = transport(*prefix, failure)
    guarded = GuardedAuditedTransport(backend)
    with pytest.raises(TransportIOError) as caught:
        if operation == "read_exact":
            guarded.read_exact(2, timeout_ms=100)
        else:
            guarded.read_until(b"\n", max_bytes=4, timeout_ms=100)
    error = caught.value
    assert error.operation == operation
    assert error.reason_code == ("frame_read_timeout" if failure is None else "frame_read_failed")
    assert error.response_progress.value == ("partial" if prefix else "none")
    assert error.synchronization.value == ("lost" if prefix else "unproven")
    assert error.consumed_bytes == len(prefix)
    assert "private-payload" not in json.dumps(error.to_envelope().as_dict())
    assert_poisoned_without_further_io(guarded)
    assert backend.bus.recv.call_count == len(prefix) + 1


@pytest.mark.parametrize("operation", ["read_exact", "read_until"])
@pytest.mark.parametrize("timeout_ms", [None, 100])
@pytest.mark.parametrize("arrival", [0.100, 0.101])
def test_late_complete_response_is_not_success(monkeypatch, operation, timeout_ms, arrival):
    backend = transport(b"a\n")
    backend.config = replace(backend.config, timeout_ms=100 if timeout_ms is None else 250)
    guarded = GuardedAuditedTransport(backend)
    now = [0.0]

    def receive(*, timeout):
        assert timeout == pytest.approx(0.1)
        now[0] = arrival
        return SimpleNamespace(data=b"a\n")

    backend.bus.recv.side_effect = receive
    monkeypatch.setattr("wavebench.transport.can_transport.time.monotonic", lambda: now[0])
    with pytest.raises(TransportIOError, match="did not complete") as caught:
        if operation == "read_exact":
            guarded.read_exact(2, timeout_ms=timeout_ms)
        else:
            guarded.read_until(b"\n", max_bytes=4, timeout_ms=timeout_ms)
    assert caught.value.reason_code == "frame_read_timeout"
    assert caught.value.consumed_bytes == 2
    assert caught.value.response_progress.value == "partial"
    assert caught.value.synchronization.value == "lost"
    assert_poisoned_without_further_io(guarded)
    backend.bus.recv.assert_called_once()


@pytest.mark.parametrize("operation", ["read_exact", "read_until"])
@pytest.mark.parametrize("timeout_ms", [None, 100])
@pytest.mark.parametrize("arrival", [0.100, 0.101])
@pytest.mark.parametrize("late_stage", ["receive", "buffer"])
def test_direct_late_response_cannot_satisfy_next_read(
    monkeypatch, operation, timeout_ms, arrival, late_stage
):
    backend = transport()
    backend.config = replace(backend.config, timeout_ms=100 if timeout_ms is None else 250)
    now = [0.0]

    class Reply:
        @property
        def data(self):
            if late_stage == "buffer":
                now[0] = arrival
            return b"a\n"

    def receive(*, timeout):
        assert timeout == pytest.approx(0.1)
        if late_stage == "receive":
            now[0] = arrival
        return Reply()

    backend.bus.recv.side_effect = receive
    monkeypatch.setattr("wavebench.transport.can_transport.time.monotonic", lambda: now[0])

    def read():
        if operation == "read_exact":
            return backend.read_exact(2, timeout_ms=timeout_ms)
        return backend.read_until(b"\n", max_bytes=4, timeout_ms=timeout_ms)

    with pytest.raises(TransportIOError) as caught:
        read()
    assert caught.value.reason_code == "frame_read_timeout"
    assert caught.value.consumed_bytes == 2
    assert caught.value.response_progress.value == "partial"
    assert caught.value.synchronization.value == "lost"
    backend.bus.recv.side_effect = None
    backend.bus.recv.return_value = None
    with pytest.raises(TransportIOError) as fresh:
        read()
    assert fresh.value.reason_code == "frame_read_timeout"
    assert fresh.value.consumed_bytes == 0
    assert fresh.value.response_progress.value == "none"
    assert fresh.value.synchronization.value == "unproven"
    assert not backend._pending
    assert backend.bus.recv.call_count == 2
    backend.bus.send.assert_not_called()
    backend.bus.shutdown.assert_not_called()


@pytest.mark.parametrize("operation", ["read_exact", "read_until"])
@pytest.mark.parametrize("prefix", [b"", b"a"])
@pytest.mark.parametrize("failure", ["deadline", "empty", "bus_error"])
def test_direct_read_failure_clears_pending(monkeypatch, operation, prefix, failure):
    backend = transport(None if failure != "bus_error" else RuntimeError("private-payload"))
    backend._pending.extend(prefix)
    ticks = iter([0.0, 0.1] if failure == "deadline" else [0.0, 0.0])
    monkeypatch.setattr("wavebench.transport.can_transport.time.monotonic", lambda: next(ticks))
    with pytest.raises(TransportIOError) as caught:
        if operation == "read_exact":
            backend.read_exact(2, timeout_ms=100)
        else:
            backend.read_until(b"\n", max_bytes=4, timeout_ms=100)
    error = caught.value
    assert error.reason_code == (
        "frame_read_failed" if failure == "bus_error" else "frame_read_timeout"
    )
    assert error.consumed_bytes == len(prefix)
    assert error.response_progress.value == ("partial" if prefix else "none")
    assert error.synchronization.value == ("lost" if prefix else "unproven")
    assert error.attempts == 1
    assert not backend._pending
    assert backend.bus.recv.call_count == (0 if failure == "deadline" else 1)
    backend.bus.send.assert_not_called()
    backend.bus.shutdown.assert_not_called()


@pytest.mark.parametrize("payload", [b"123456\n", b"1234567"])
def test_direct_overflow_clears_pending(payload):
    backend = transport(payload)
    with pytest.raises(TransportIOError) as caught:
        backend.read_until(b"\n", max_bytes=4)
    assert caught.value.reason_code == "frame_too_large"
    assert caught.value.consumed_bytes == len(payload)
    assert not backend._pending
    backend.bus.recv.assert_called_once()


@pytest.mark.parametrize("operation", ["read_exact", "read_until"])
@pytest.mark.parametrize("timeout_ms", [None, 100])
def test_split_response_uses_remaining_total_budget(monkeypatch, operation, timeout_ms):
    backend = transport(b"a", b"\n")
    backend.config = replace(backend.config, timeout_ms=100 if timeout_ms is None else 250)
    now = [0.0]
    messages = iter([b"a", b"\n"])

    def receive(*, timeout):
        now[0] += 0.04
        return SimpleNamespace(data=next(messages))

    backend.bus.recv.side_effect = receive
    monkeypatch.setattr(
        "wavebench.transport.can_transport.time.monotonic",
        lambda: now[0],
    )
    if operation == "read_exact":
        result = backend.read_exact(2, timeout_ms=timeout_ms)
    else:
        result = backend.read_until(b"\n", max_bytes=4, timeout_ms=timeout_ms)
    assert result == b"a\n"
    assert backend.bus.recv.call_args_list[0].kwargs["timeout"] == pytest.approx(0.1)
    assert backend.bus.recv.call_args_list[1].kwargs["timeout"] == pytest.approx(0.06)


@pytest.mark.parametrize("timeout", [0, -1, True, 1.5])
def test_invalid_timeout_fails_before_receive(timeout):
    backend = transport()
    backend._pending.extend(b"pending")
    guarded = GuardedAuditedTransport(backend)
    for operation in (
        lambda: guarded.read_exact(1, timeout_ms=timeout),
        lambda: guarded.read_until(b"\n", max_bytes=4, timeout_ms=timeout),
    ):
        with pytest.raises(TransportIOError) as caught:
            operation()
        error = caught.value
        assert error.reason_code == "frame_timeout_invalid"
        assert error.phase.value == "before_send"
        assert error.command_transmission.value == "not_sent"
        assert error.response_progress.value == "none"
        assert error.synchronization.value == "proven"
        assert error.attempts == error.consumed_bytes == error.discarded_bytes == 0
        assert guarded.session_state.health.value == "healthy"
        assert backend._pending == b"pending"
    backend.bus.recv.assert_not_called()
    backend.bus.send.assert_not_called()
    backend.bus.shutdown.assert_not_called()


@pytest.mark.parametrize(
    "operation, arguments, reason",
    [
        ("read_exact", {"size": 0}, "frame_size_invalid"),
        ("read_exact", {"size": True}, "frame_size_invalid"),
        ("read_until", {"delimiter": b"", "max_bytes": 4}, "frame_delimiter_invalid"),
        ("read_until", {"delimiter": b"\n", "max_bytes": 0}, "frame_limit_invalid"),
    ],
)
def test_direct_invalid_read_preserves_pending_without_io(operation, arguments, reason):
    backend = transport()
    backend._pending.extend(b"pending")
    with pytest.raises(TransportIOError) as caught:
        getattr(backend, operation)(**arguments)
    error = caught.value
    assert error.reason_code == reason
    assert error.phase.value == "before_send"
    assert error.command_transmission.value == "not_sent"
    assert error.response_progress.value == "none"
    assert error.synchronization.value == "proven"
    assert error.attempts == error.consumed_bytes == error.discarded_bytes == 0
    assert backend._pending == b"pending"
    assert backend.bus.mock_calls == []


@pytest.mark.parametrize("operation", ["read_exact", "read_until"])
def test_raw_read_continuation_is_accepted_without_retransmission(operation):
    backend = transport(b"a", b"\nxyz")
    guarded = GuardedAuditedTransport(backend)
    if operation == "read_exact":
        result = guarded.read_exact(2, replay=ReplayPolicy.READ_CONTINUATION_ONLY)
    else:
        result = guarded.read_until(b"\n", max_bytes=4, replay=ReplayPolicy.READ_CONTINUATION_ONLY)
    assert result == b"a\n"
    assert guarded.read_exact(3, replay=ReplayPolicy.READ_CONTINUATION_ONLY) == b"xyz"
    assert backend.bus.recv.call_count == 2
    backend.bus.send.assert_not_called()
    assert guarded.session_state.health.value == "healthy"


@pytest.mark.parametrize("operation", ["read_exact", "read_until"])
def test_raw_read_continuation_failure_preserves_policy_and_poisons(operation):
    backend = transport(b"a", None)
    guarded = GuardedAuditedTransport(backend)
    with pytest.raises(TransportIOError) as caught:
        if operation == "read_exact":
            guarded.read_exact(2, replay=ReplayPolicy.READ_CONTINUATION_ONLY)
        else:
            guarded.read_until(b"\n", max_bytes=4, replay=ReplayPolicy.READ_CONTINUATION_ONLY)
    assert caught.value.replay_policy is ReplayPolicy.READ_CONTINUATION_ONLY
    assert caught.value.consumed_bytes == 1
    assert_poisoned_without_further_io(guarded)
    backend.bus.send.assert_not_called()


@pytest.mark.parametrize("operation", ["read_exact", "read_until"])
def test_raw_read_rejects_command_replay_before_io(operation):
    backend = transport()
    backend._pending.extend(b"pending")
    with pytest.raises(TransportIOError) as caught:
        if operation == "read_exact":
            backend.read_exact(1, replay=ReplayPolicy.SAFE_TO_REPLAY)
        else:
            backend.read_until(b"\n", max_bytes=4, replay=ReplayPolicy.SAFE_TO_REPLAY)
    assert caught.value.reason_code == "frame_replay_unsupported"
    assert caught.value.attempts == 0
    assert caught.value.replay_policy is ReplayPolicy.SAFE_TO_REPLAY
    assert backend.bus.mock_calls == []
    assert backend._pending == b"pending"


def test_failed_send_is_not_replayed_and_does_not_expose_payload(monkeypatch):
    monkeypatch.setitem(__import__("sys").modules, "can", SimpleNamespace(Message=Mock()))
    backend = transport()
    backend.bus.send.side_effect = RuntimeError("private-payload")
    with pytest.raises(TransportIOError) as caught:
        backend.send_frame(b"a", max_bytes=8)
    error = caught.value
    assert error.operation == "send_frame"
    assert error.command_transmission.value == "unknown"
    assert error.reason_code == "frame_write_failed"
    assert error.attempts == 1
    assert "private-payload" not in json.dumps(error.to_envelope().as_dict())
    backend.bus.send.assert_called_once()


@pytest.mark.parametrize("failure", [None, RuntimeError("private-channel")])
def test_close_is_idempotent_and_reports_failure(failure):
    backend = transport()
    backend._pending.extend(b"pending")
    backend.bus.shutdown.side_effect = failure
    if failure is None:
        backend.close()
    else:
        with pytest.raises(SessionCloseError) as caught:
            backend.close()
        assert "private-channel" not in json.dumps(caught.value.to_envelope().as_dict())
    backend.close()
    backend.bus.shutdown.assert_called_once()
    assert not backend._pending
