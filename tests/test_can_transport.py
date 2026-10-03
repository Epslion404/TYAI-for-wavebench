import uuid

import pytest

from wavebench.config import CanConnectionConfig
from wavebench.errors import ConfigError
from wavebench.transport.can_transport import CanTransport


def test_can_transport_rejects_undeclared_channel_before_import_or_io():
    with pytest.raises(ConfigError, match="channel is not configured"):
        CanTransport.open(CanConnectionConfig(channel=None))


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
