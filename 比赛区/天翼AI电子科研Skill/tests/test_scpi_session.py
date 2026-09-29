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
            with pytest.raises(ValueError):
                session.query_block("CHAN1:DATA?", max_bytes=16)


def test_read_block_times_out_when_payload_incomplete():
    with FakeScpiServer({"CHAN1:DATA?": b"#3100"}) as server:
        host, port = server.address
        with ScpiSession(host, port, timeout=0.3) as session:
            with pytest.raises(TimeoutError):
                session.query_block("CHAN1:DATA?")


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
    assert server.received == ["FORM REAL"]
