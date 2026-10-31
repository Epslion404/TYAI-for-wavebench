import uuid

import pytest

from wavebench.config import CanConnectionConfig
from wavebench.errors import ConfigError, TransportIOError, error_envelope
from wavebench.transport.can_transport import CanTransport


def test_can_transport_rejects_undeclared_channel_before_import_or_io():
    with pytest.raises(ConfigError, match="channel is not configured"):
        CanTransport.open(CanConnectionConfig(channel=None))


def test_can_transport_rejects_invalid_fd_timing_before_import_or_io():
    with pytest.raises(ConfigError, match="data_bitrate requires fd=True"):
        CanTransport.open(CanConnectionConfig(channel="can0", data_bitrate=2_000_000))


def test_can_transport_virtual_bus_round_trip():
    can = pytest.importorskip("can")
    channel = f"wavebench-{uuid.uuid4()}"
    peer = can.Bus(interface="virtual", channel=channel, receive_own_messages=False)
    transport = CanTransport.open(
        CanConnectionConfig(channel=channel, interface="virtual", arbitration_id=0x123)
    )
    try:
        transport.send_frame(b"\x01\x02", max_bytes=8)
        sent = peer.recv(timeout=0.5)
        assert sent is not None
        assert bytes(sent.data) == b"\x01\x02"

        peer.send(can.Message(arbitration_id=0x123, is_extended_id=False, data=b"\xaa\xbb"))
        assert transport.read_exact(2, timeout_ms=500) == b"\xaa\xbb"
    finally:
        transport.close()
        peer.shutdown()


def test_can_fd_transport_accepts_64_byte_virtual_frame():
    can = pytest.importorskip("can")
    channel = f"wavebench-fd-{uuid.uuid4()}"
    peer = can.Bus(interface="virtual", channel=channel, receive_own_messages=False)
    transport = CanTransport.open(
        CanConnectionConfig(channel=channel, interface="virtual", arbitration_id=0x123, fd=True)
    )
    try:
        payload = bytes(range(64))
        transport.send_frame(payload, max_bytes=64)
        sent = peer.recv(timeout=0.5)
        assert sent is not None
        assert sent.is_fd is True
        assert bytes(sent.data) == payload
    finally:
        transport.close()
        peer.shutdown()


def test_can_adapter_unavailable_has_a_stable_error_envelope():
    pytest.importorskip("can")

    with pytest.raises(Exception) as raised:
        CanTransport.open(CanConnectionConfig(channel="missing", interface="not-a-can-interface"))

    payload = error_envelope(raised.value)
    assert payload["code"] == "can_adapter_unavailable"
    assert payload["exit_code"] == 3


def test_can_read_timeout_is_a_structured_transport_failure():
    can = pytest.importorskip("can")
    channel = f"wavebench-timeout-{uuid.uuid4()}"
    peer = can.Bus(interface="virtual", channel=channel, receive_own_messages=False)
    transport = CanTransport.open(CanConnectionConfig(channel=channel, interface="virtual"))
    try:
        with pytest.raises(TransportIOError) as raised:
            transport.read_exact(1, timeout_ms=1)
    finally:
        transport.close()
        peer.shutdown()

    payload = error_envelope(raised.value)
    assert payload["code"] == "transport_io_error"
    assert payload["details"]["reason_code"] == "frame_read_timeout"
    assert payload["details"]["attempts"] == 1
