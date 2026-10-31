"""Offline CAN/CAN-FD transport contract tests.

python-can is a required extra for these tests (``pip install -e .[can]``).
All coverage uses the virtual interface and never touches a real adapter.
"""

from __future__ import annotations

import sys
import types
import unittest
from unittest.mock import patch

from wavebench.errors import ConfigError, ConnectionError, SessionCloseError, TransportIOError
from wavebench.logging import CommandLogger
from wavebench.transport.can_transport import (
    _MAX_CAN_DATA_BYTES,
    _MAX_CANFD_DATA_BYTES,
    CanTransport,
)
from wavebench.transport.contracts import (
    CommandTransmission,
    ReplayPolicy,
    ResponseProgress,
    Synchronization,
    TransportPhase,
)
from wavebench.transport.guarded import GuardedAuditedTransport


_FAKE_CAN_MODULE = None


def setUpModule():
    """Keep the optional backend tests runnable without python-can installed."""
    global _FAKE_CAN_MODULE
    try:
        import can  # noqa: F401
    except ModuleNotFoundError:
        module = types.ModuleType("can")
        interface = types.ModuleType("can.interface")

        class FakeMessage:
            def __init__(self, **kwargs):
                self.__dict__.update(kwargs)

        class FakeBus:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
                self.sent = []

            def send(self, message):
                self.sent.append(message)

            def recv(self, timeout=None):
                return None

            def shutdown(self):
                pass

        interface.Bus = FakeBus
        module.interface = interface
        module.Message = FakeMessage
        sys.modules["can"] = module
        sys.modules["can.interface"] = interface
        _FAKE_CAN_MODULE = module


def tearDownModule():
    if _FAKE_CAN_MODULE is not None:
        sys.modules.pop("can.interface", None)
        sys.modules.pop("can", None)


class FakeCanSession:
    """A controllable python-can session that can return a message or time out."""

    def __init__(self, *, reply: bytes | None = b"\x01\x02", timeout: bool = False):
        self.reply = reply
        self.timeout = timeout
        self.sent: list[object] = []
        self.shutdown_calls = 0
        self.error_on_recv: Exception | None = None
        self.error_on_send: Exception | None = None
        self.error_frame = False
        self.flush_discard_count = 0

    def send(self, message: object) -> None:
        if self.error_on_send is not None:
            raise self.error_on_send
        self.sent.append(message)

    def flush_tx_buffer(self) -> None:
        # Simulate python-can's discard semantics: calling this after send
        # drops queued frames.  CanTransport must not invoke it.
        self.sent.clear()
        self.flush_discard_count += 1

    def recv(self, timeout=None) -> object | None:
        if self.error_on_recv is not None:
            raise self.error_on_recv
        if self.timeout:
            return None
        if self.reply is None:
            return None
        return _message(self.reply, error_frame=self.error_frame)

    def shutdown(self) -> None:
        self.shutdown_calls += 1


def _message(data: bytes, *, error_frame: bool = False) -> object:
    import can

    return can.Message(
        arbitration_id=0x100,
        data=bytes(data),
        is_extended_id=False,
        is_fd=False,
        is_error_frame=error_frame,
    )


class CanTransportOpenTests(unittest.TestCase):
    def test_open_requires_can_resource_scheme(self):
        with self.assertRaisesRegex(ConfigError, "can://"):
            CanTransport.open(resource="COM3")

    def test_open_rejects_empty_channel_and_bad_bitrate(self):
        with self.assertRaisesRegex(ConfigError, "channel"):
            CanTransport.open(resource="can://")
        with self.assertRaisesRegex(ConfigError, "bitrate"):
            CanTransport.open(resource="can://can0", bitrate=0)
        with self.assertRaisesRegex(ConfigError, "bitrate"):
            CanTransport.open(resource="can://can0", bitrate=True)  # type: ignore[arg-type]

    def test_open_rejects_oversized_non_fd_frame(self):
        with self.assertRaisesRegex(ConfigError, "max_frame_bytes"):
            CanTransport.open(
                resource="can://can0", max_frame_bytes=_MAX_CAN_DATA_BYTES + 1
            )

    def test_open_accepts_canfd_size(self):
        # Defaults to virtual bus, so this can run offline.
        with patch("can.interface.Bus") as bus:
            transport = CanTransport.open(
                resource="can://canfd0",
                is_fd=True,
                max_frame_bytes=_MAX_CANFD_DATA_BYTES,
            )
        self.assertTrue(transport.is_fd)
        self.assertEqual(transport.max_frame_bytes, _MAX_CANFD_DATA_BYTES)
        bus.assert_called_once()

    def test_open_passes_fd_to_bus_constructor(self):
        with patch("can.interface.Bus") as bus:
            CanTransport.open(
                resource="can://fd-bus",
                is_fd=True,
                options={"can_interface": "socketcan"},
            )
        kwargs = bus.call_args.kwargs
        self.assertIs(kwargs.get("fd"), True)
        self.assertEqual(kwargs.get("interface"), "socketcan")

    def test_open_parses_interface_and_channel_from_resource(self):
        with patch("can.interface.Bus") as bus:
            transport = CanTransport.open(resource="can://socketcan/can0")
        kwargs = bus.call_args.kwargs
        self.assertEqual(kwargs.get("interface"), "socketcan")
        self.assertEqual(kwargs.get("channel"), "can0")
        self.assertEqual(transport.resource, "can://socketcan/can0")

    def test_open_single_segment_uses_default_interface(self):
        with patch("can.interface.Bus") as bus:
            CanTransport.open(resource="can://can0")
        kwargs = bus.call_args.kwargs
        self.assertEqual(kwargs.get("interface"), "virtual")
        self.assertEqual(kwargs.get("channel"), "can0")

    def test_open_preserves_numeric_zero_channel(self):
        with patch("can.interface.Bus") as bus:
            CanTransport.open(resource="can://kvaser/0", channel=0)
        self.assertEqual(bus.call_args.kwargs.get("channel"), 0)

    def test_open_without_python_can_is_structured(self):
        with patch.dict("sys.modules", {"can": None}):
            with self.assertRaises(ConnectionError) as raised:
                CanTransport.open(resource="can://can0")
        self.assertIn("python-can", str(raised.exception))

    def test_virtual_bus_open_and_close_offline(self):
        transport = CanTransport.open(
            resource="can://offline-rt",
            options={"can_interface": "virtual"},
        )
        self.assertEqual(transport.resource, "can://offline-rt")
        transport.send_frame(b"\x01", max_bytes=8)
        transport.close()
        transport.close()  # idempotent
        self.assertTrue(transport._closed)


class CanTransportFrameTests(unittest.TestCase):
    def test_send_frame_transmits_payload(self):
        session = FakeCanSession(reply=b"")
        transport = CanTransport("can://can0", session, CommandLogger(), max_frame_bytes=8)

        transport.send_frame(b"\x01\x02", max_bytes=8)

        self.assertEqual(len(session.sent), 1)
        self.assertEqual(bytes(session.sent[0].data), b"\x01\x02")
        self.assertEqual(session.flush_discard_count, 0)

    def test_send_frame_does_not_flush_the_transmit_queue(self):
        # flush_tx_buffer is python-can's discard-queued-transmissions operation;
        # calling it after a successful send would drop the frame.
        session = FakeCanSession(reply=b"")
        transport = CanTransport("can://can0", session, CommandLogger(), max_frame_bytes=8)

        transport.send_frame(b"\x01", max_bytes=8)

        self.assertEqual(len(session.sent), 1)
        self.assertEqual(session.flush_discard_count, 0)

    def test_send_frame_rejects_oversized_and_replay(self):
        transport = CanTransport("can://can0", FakeCanSession(), CommandLogger(), max_frame_bytes=2)
        with self.assertRaises(TransportIOError) as raised:
            transport.send_frame(b"\x01\x02\x03", max_bytes=8)
        self.assertEqual(raised.exception.reason_code, "frame_too_large")
        with self.assertRaises(TransportIOError) as raised:
            transport.send_frame(b"\x01", max_bytes=8, replay=ReplayPolicy.SAFE_TO_REPLAY)
        self.assertEqual(raised.exception.reason_code, "frame_replay_unsupported")

    def test_read_exact_receives_matching_frame(self):
        session = FakeCanSession(reply=b"\x01\x02\x03")
        transport = CanTransport("can://can0", session, CommandLogger())
        self.assertEqual(transport.read_exact(3, timeout_ms=100), b"\x01\x02\x03")

    def test_read_exact_timeout_is_structured(self):
        transport = CanTransport("can://can0", FakeCanSession(timeout=True), CommandLogger())
        with self.assertRaises(TransportIOError) as raised:
            transport.read_exact(2, timeout_ms=10)
        self.assertEqual(raised.exception.reason_code, "frame_timeout")
        self.assertEqual(raised.exception.phase, TransportPhase.READING)
        self.assertEqual(raised.exception.response_progress, ResponseProgress.NONE)
        self.assertEqual(raised.exception.synchronization, Synchronization.UNPROVEN)

    def test_read_exact_rejects_payload_length_mismatch(self):
        transport = CanTransport("can://can0", FakeCanSession(reply=b"\x01"), CommandLogger())
        with self.assertRaises(TransportIOError) as raised:
            transport.read_exact(2, timeout_ms=100)
        self.assertEqual(raised.exception.reason_code, "frame_length_invalid")
        self.assertEqual(raised.exception.command_transmission, CommandTransmission.SENT)
        self.assertEqual(raised.exception.response_progress, ResponseProgress.COMPLETE)
        self.assertEqual(raised.exception.synchronization, Synchronization.LOST)

    def test_read_exact_rejects_controller_error_frame(self):
        session = FakeCanSession(reply=b"\x01\x02")
        session.error_frame = True
        transport = CanTransport("can://can0", session, CommandLogger())
        with self.assertRaises(TransportIOError) as raised:
            transport.read_exact(2, timeout_ms=100)
        self.assertEqual(raised.exception.reason_code, "frame_bus_error")
        self.assertEqual(raised.exception.command_transmission, CommandTransmission.SENT)

    def test_read_until_is_unsupported_on_raw_can(self):
        transport = CanTransport("can://can0", FakeCanSession(), CommandLogger())
        with self.assertRaises(TransportIOError) as raised:
            transport.read_until(b"\n", max_bytes=8)
        self.assertEqual(raised.exception.reason_code, "raw_frame_unsupported")
        self.assertEqual(raised.exception.attempts, 0)

    def test_scpi_operations_are_unsupported_on_raw_can(self):
        transport = CanTransport("can://can0", FakeCanSession(), CommandLogger())
        for operation, args in (
            ("write", ("CMD",)),
            ("query", ("CMD?",)),
            ("query_opc", ()),
            ("query_bin_block", ("CMD?",)),
            ("query_float_list", ("CMD?",)),
        ):
            with self.subTest(operation=operation):
                with self.assertRaises(TransportIOError) as raised:
                    getattr(transport, operation)(*args)
                self.assertEqual(raised.exception.reason_code, "raw_frame_unsupported")


class CanTransportFailureTests(unittest.TestCase):
    def test_send_bus_error_is_structured(self):
        session = FakeCanSession()
        session.error_on_send = OSError("simulated bus error")
        transport = CanTransport("can://can0", session, CommandLogger())

        with self.assertRaises(TransportIOError) as raised:
            transport.send_frame(b"\x01", max_bytes=8)

        self.assertEqual(raised.exception.reason_code, "bus_error")
        self.assertEqual(raised.exception.synchronization, Synchronization.UNPROVEN)

    def test_recv_bus_error_is_structured(self):
        session = FakeCanSession()
        session.error_on_recv = OSError("simulated controller failure")
        transport = CanTransport("can://can0", session, CommandLogger())

        with self.assertRaises(TransportIOError) as raised:
            transport.read_exact(2, timeout_ms=100)

        self.assertEqual(raised.exception.reason_code, "bus_error")

    def test_close_is_idempotent(self):
        session = FakeCanSession()
        transport = CanTransport("can://can0", session, CommandLogger())
        transport.close()
        transport.close()
        self.assertEqual(session.shutdown_calls, 1)

    def test_close_failure_is_structured(self):
        session = FakeCanSession()

        def fail_shutdown():
            raise OSError("controller shutdown failed")

        session.shutdown = fail_shutdown
        transport = CanTransport("can://can0", session, CommandLogger())
        with self.assertRaises(SessionCloseError) as raised:
            transport.close()
        self.assertEqual(raised.exception.failures, ({"component": "session", "type": "OSError"},))

    def test_guarded_frame_failure_poisons_session(self):
        session = FakeCanSession(reply=b"\x01")
        transport = GuardedAuditedTransport(
            CanTransport("can://can0", session, CommandLogger())
        )

        with self.assertRaises(TransportIOError):
            transport.read_exact(2, timeout_ms=100)

        # A length-mismatch response is lost synchronization, which poisons the session.
        self.assertEqual(transport.session_state.health.value, "poisoned")


if __name__ == "__main__":
    unittest.main()