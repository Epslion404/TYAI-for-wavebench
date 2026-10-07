"""SCPI 会话与 IEEE 488.2 确定块解析的离线测试。"""

from __future__ import annotations

import struct

import pytest
from fake_scpi import FakeScpiServer, encode_block

from tianyi_electronics_skill.scpi import (
    ScpiSession,
    parse_block_header,
    read_definite_block,
)


class _ScriptedSocket:
    """Minimal socket double for deterministic session-buffer tests."""

    def __init__(self, responses: list[bytes | BaseException], timeout: float = 1.0) -> None:
        self.responses = list(responses)
        self.timeout = timeout
        self.sent: list[bytes] = []
        self.set_timeouts: list[float | None] = []
        self.closed = False

    def sendall(self, data: bytes) -> None:
        self.sent.append(data)

    def recv(self, _size: int) -> bytes:
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    def gettimeout(self) -> float:
        return self.timeout

    def settimeout(self, value: float | None) -> None:
        self.timeout = value  # type: ignore[assignment]
        self.set_timeouts.append(value)

    def shutdown(self, _how: int) -> None:
        pass

    def close(self) -> None:
        self.closed = True


def test_parse_block_header_basic():
    assert parse_block_header(b"#15hello") == (5, 3)
    assert parse_block_header(b"#3100abc") == (100, 5)


def test_parse_block_header_rejects_non_block():
    with pytest.raises(ValueError):
        parse_block_header(b"1000.0\n")


def test_parse_block_header_rejects_indefinite_block():
    with pytest.raises(ValueError):
        parse_block_header(b"#0")


def test_read_block_survives_split_packets_and_delimiters():
    payload = struct.pack("<4f", 1.0, 2.0, 3.0, 4.0) + b"#\n#"
    with FakeScpiServer({"CHAN1:DATA?": encode_block(payload)}, chunks=5) as server:
        host, port = server.address
        with ScpiSession(host, port, timeout=2.0) as session:
            assert session.query_block("CHAN1:DATA?") == payload


def test_read_block_rejects_oversized_declared_length():
    with FakeScpiServer({"CHAN1:DATA?": encode_block(b"\x00" * 64)}) as server:
        host, port = server.address
        with ScpiSession(host, port, timeout=2.0) as session:
            with pytest.raises(ValueError) as raised:
                session.query_block("CHAN1:DATA?", max_bytes=16)
            assert raised.value.scpi_command == "CHAN1:DATA?"
            assert session._socket is None


def test_read_block_times_out_when_payload_incomplete():
    with FakeScpiServer({"CHAN1:DATA?": b"#3100"}) as server:
        host, port = server.address
        with ScpiSession(host, port, timeout=0.3) as session:
            with pytest.raises(TimeoutError) as raised:
                session.query_block("CHAN1:DATA?")
            assert raised.value.scpi_command == "CHAN1:DATA?"
            assert session._socket is None
            with pytest.raises(ConnectionError):
                session.query("NEXT?")


def test_read_block_rejects_text_answer():
    with FakeScpiServer({"CHAN1:DATA?": "not a block"}) as server:
        host, port = server.address
        with ScpiSession(host, port, timeout=1.0) as session:
            with pytest.raises(ValueError):
                session.query_block("CHAN1:DATA?")


def test_read_definite_block_uses_reader():
    data = b"#3100" + b"x" * 100
    state = {"offset": 0}
    sizes: list[int] = []

    def reader(size: int) -> bytes:
        sizes.append(size)
        chunk = data[state["offset"] : state["offset"] + size]
        state["offset"] += len(chunk)
        return chunk

    assert read_definite_block(reader) == b"x" * 100
    assert sizes == [2, 3, 100]


def test_session_query_and_error_queue():
    responses = {"*IDN?": "Rohde&Schwarz,RTM2032,5710.0999k32/101665,06.010"}
    with FakeScpiServer(
        responses,
        errors=['-113,"Undefined header"', '0,"No error"'],
    ) as server:
        host, port = server.address
        with ScpiSession(host, port, timeout=1.0) as session:
            assert session.query("*IDN?").startswith("Rohde&Schwarz")
            assert session.drain_errors() == ['-113,"Undefined header"']
            assert session.drain_errors() == []
    assert server.received[:2] == ["*IDN?", "SYST:ERR?"]


def test_session_write_only_command_reads_nothing():
    with FakeScpiServer({"FORM REAL": ""}) as server:
        host, port = server.address
        with ScpiSession(host, port, timeout=1.0) as session:
            session.write("FORM REAL")
        assert server.wait_for_commands(1)
        assert server.received == ["FORM REAL"]


def test_session_consumes_lf_arriving_after_cr_before_next_response():
    session = ScpiSession("example.test")
    socket = _ScriptedSocket([b"first\r", b"\nsecond\r\n"])
    session._socket = socket  # type: ignore[assignment]

    assert session.query("FIRST?") == "first"
    assert session.query("SECOND?") == "second"
    assert socket.sent == [b"FIRST?\n", b"SECOND?\n"]


def test_error_queue_timeout_is_not_treated_as_empty_and_closes_session():
    session = ScpiSession("example.test", timeout=1.0)
    socket = _ScriptedSocket([TimeoutError("slow error queue")])
    session._socket = socket  # type: ignore[assignment]

    with pytest.raises(TimeoutError, match="slow error queue") as raised:
        session.drain_errors()

    assert raised.value.scpi_command == "SYST:ERR?"
    assert 0.25 not in socket.set_timeouts
    assert socket.closed is True
    assert session._socket is None
