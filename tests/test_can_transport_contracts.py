"""Offline CAN contract regressions; no python-can or physical bus required."""

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from wavebench.config import CanConnectionConfig
from wavebench.errors import ConfigError, SessionCloseError, SessionHealthError, TransportIOError
from wavebench.logging import CommandLogger
from wavebench.transport.can_transport import CanTransport
from wavebench.transport.guarded import GuardedAuditedTransport


def transport(*messages):
    bus = Mock()
    bus.recv.side_effect = [
        SimpleNamespace(data=item) if isinstance(item, bytes) else item for item in messages
    ]
    return CanTransport("fake-bus", bus, CommandLogger(), CanConnectionConfig(channel="fake-bus"))


@pytest.mark.parametrize("payload", [b"123456\n", b"1234567"])
def test_delimited_overflow_is_structured_and_poisons_session(payload):
    backend = transport(payload)
    guarded = GuardedAuditedTransport(backend)
    with pytest.raises(TransportIOError) as caught:
        guarded.read_until(b"\n", max_bytes=4, timeout_ms=100)
    assert caught.value.reason_code == "frame_too_large"
    assert caught.value.consumed_bytes == len(payload)
    assert guarded.session_state.health.value == "poisoned"
    with pytest.raises(SessionHealthError):
        guarded.read_exact(1)
    assert backend.bus.recv.call_count == 1
    guarded.close()
    assert backend.bus.shutdown.call_count == 1


def test_delimiter_within_bound_retains_only_following_bytes():
    backend = transport(b"abc\nxyz")
    assert backend.read_until(b"\n", max_bytes=4) == b"abc\n"
    assert backend.read_exact(3) == b"xyz"
    assert backend.bus.recv.call_count == 1


@pytest.mark.parametrize("operation", ["read_exact", "read_until"])
@pytest.mark.parametrize("failure", [None, RuntimeError("private-payload")])
def test_partial_read_failure_has_sanitized_contract(operation, failure):
    backend = transport(b"a", failure)
    guarded = GuardedAuditedTransport(backend)
    with pytest.raises(TransportIOError) as caught:
        if operation == "read_exact":
            guarded.read_exact(2, timeout_ms=100)
        else:
            guarded.read_until(b"\n", max_bytes=4, timeout_ms=100)
    error = caught.value
    assert error.operation == operation
    assert error.reason_code == ("frame_read_timeout" if failure is None else "frame_read_failed")
    assert error.response_progress.value == "partial"
    assert error.synchronization.value == "lost"
    assert error.consumed_bytes == 1
    assert "private-payload" not in json.dumps(error.to_envelope().as_dict())
    assert guarded.session_state.health.value == "poisoned"
    with pytest.raises(SessionHealthError):
        guarded.read_exact(1)
    assert backend.bus.recv.call_count == 2


@pytest.mark.parametrize("operation", ["read_exact", "read_until"])
def test_late_complete_response_is_not_success(monkeypatch, operation):
    backend = transport(b"a\n")
    clock = Mock(side_effect=[0.0, 0.0, 0.101])
    monkeypatch.setattr("wavebench.transport.can_transport.time.monotonic", clock)
    with pytest.raises(TransportIOError, match="did not complete") as caught:
        if operation == "read_exact":
            backend.read_exact(2, timeout_ms=100)
        else:
            backend.read_until(b"\n", max_bytes=4, timeout_ms=100)
    assert caught.value.reason_code == "frame_read_timeout"
    assert caught.value.consumed_bytes == 2


def test_split_response_uses_remaining_total_budget(monkeypatch):
    backend = transport(b"a", b"b")
    now = [0.0]
    messages = iter([b"a", b"b"])

    def receive(*, timeout):
        now[0] += 0.04
        return SimpleNamespace(data=next(messages))

    backend.bus.recv.side_effect = receive
    monkeypatch.setattr(
        "wavebench.transport.can_transport.time.monotonic",
        lambda: now[0],
    )
    assert backend.read_exact(2, timeout_ms=100) == b"ab"
    assert backend.bus.recv.call_args_list[0].kwargs["timeout"] == pytest.approx(0.1)
    assert backend.bus.recv.call_args_list[1].kwargs["timeout"] == pytest.approx(0.06)


@pytest.mark.parametrize("timeout", [0, -1, True, 1.5])
def test_invalid_timeout_fails_before_receive(timeout):
    backend = transport()
    with pytest.raises(ConfigError):
        backend.read_exact(1, timeout_ms=timeout)
    with pytest.raises(ConfigError):
        backend.read_until(b"\n", max_bytes=4, timeout_ms=timeout)
    backend.bus.recv.assert_not_called()


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
