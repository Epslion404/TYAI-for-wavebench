"""Response framing and deadlines, with deterministic clocks and loopback only."""

from __future__ import annotations

from collections import deque
import struct

import pytest
from fake_scpi import FakeScpiServer, encode_block

from tianyi_electronics_skill import scpi
from tianyi_electronics_skill.waveform import read_waveform


class Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now


class Socket:
    """Honour recv sizes and timeouts while advancing a fake monotonic clock."""

    def __init__(self, clock, responses, *, send_seconds=0.0):
        self.clock = clock
        self.responses = deque(responses)
        self.send_seconds = send_seconds
        self.timeout = 0.1
        self.timeouts = []
        self.sent = []
        self.closed = False
        self.received_sizes = []

    def gettimeout(self):
        return self.timeout

    def settimeout(self, timeout):
        self.timeout = timeout
        self.timeouts.append(timeout)

    def sendall(self, data):
        self.sent.append(data)
        self.clock.now += self.send_seconds

    def recv(self, size):
        self.received_sizes.append(size)
        if not self.responses:
            self.clock.now += self.timeout
            raise TimeoutError("no response")
        data, seconds = self.responses.popleft()
        if seconds >= self.timeout:
            self.clock.now += self.timeout
            self.responses.appendleft((data, seconds - self.timeout))
            raise TimeoutError("response arrived too late")
        self.clock.now += seconds
        if len(data) > size:
            self.responses.appendleft((data[size:], 0.0))
        return data[:size]

    def shutdown(self, _how):
        pass

    def close(self):
        self.closed = True


def session_with_socket(monkeypatch, responses, **kwargs):
    clock = Clock()
    monkeypatch.setattr(scpi, "time", clock)
    sock = Socket(clock, responses, **kwargs)
    session = scpi.ScpiSession("offline.test", timeout=0.1)
    session._socket = sock
    return session, sock, clock


@pytest.mark.parametrize("ending", [b"\n", b"\r", b"\r\n"])
def test_text_block_error_queue_share_buffer_without_eating_next_line(monkeypatch, ending):
    # Coalesced bytes include an entire subsequent response: consume each once.
    payload = b"\n#\r\x00"
    wire = b"header" + ending + encode_block(payload) + ending + b'0,"No error"\nnext\n'
    session, sock, _ = session_with_socket(monkeypatch, [(wire, 0.0)])

    assert session.query("HEADER?") == "header"
    assert session.query_block("DATA?") == payload
    assert session.drain_errors() == []
    assert session.query("NEXT?") == "next"
    assert sock.sent == [b"HEADER?\n", b"DATA?\n", b"SYST:ERR?\n", b"NEXT?\n"]
    assert len(sock.received_sizes) == 1


def test_split_crlf_text_to_block_and_block_to_error_queue(monkeypatch):
    responses = [(part, 0.0) for part in (
        b"header\r", b"\n#", b"1", b"4", b"\n#\r\x00", b"\r", b"\n", b'0,"No error"\r',
        b"\nnext\n",
    )]
    session, _, _ = session_with_socket(monkeypatch, responses)

    assert session.query("HEADER?") == "header"
    assert session.query_block("DATA?") == b"\n#\r\x00"
    assert session.drain_errors() == []
    assert session.query("NEXT?") == "next"


@pytest.mark.parametrize("ending", [b"", b"\n", b"\r", b"\r\n"])
def test_optional_block_ending_preserves_next_block_payload(monkeypatch, ending):
    session, _, _ = session_with_socket(monkeypatch, [
        (encode_block(b"one") + ending + encode_block(b"\n#two"), 0.0),
    ])
    assert session.query_block("FIRST?") == b"one"
    assert session.query_block("SECOND?") == b"\n#two"


@pytest.mark.parametrize("binary", [False, True])
@pytest.mark.parametrize("prefix", [b"", b"partial"])
def test_timeout_poisons_session_and_cannot_reuse_late_response(monkeypatch, binary, prefix):
    initial = b"#14da" if binary and prefix else prefix
    responses = [(initial, 0.0)] if initial else []
    responses.append((b"late\n", 0.2))
    session, sock, _ = session_with_socket(monkeypatch, responses)
    query = session.query_block if binary else session.query

    with pytest.raises(TimeoutError, match="FIRST") as raised:
        query("FIRST?")
    assert raised.value.scpi_command == "FIRST?"
    assert scpi.classify_exception(raised.value) == "timeout"
    assert sock.closed
    assert session._socket is None
    assert not session._read_buffer
    with pytest.raises(ConnectionError) as second:
        session.query("SECOND?")
    assert second.value.scpi_command == "SECOND?"
    assert sock.sent == [b"FIRST?\n"]


@pytest.mark.parametrize("binary,responses", [
    (False, [(b"1", 0.04), (b"2", 0.04), (b"\n", 0.04)]),
    # Deadline expires while reading the length field.
    (True, [(b"#2", 0.04), (b"0", 0.04), (b"4", 0.04), (b"data", 0.0)]),
    # Deadline expires in payload after header has consumed most of the budget.
    (True, [(b"#", 0.03), (b"1", 0.03), (b"4", 0.03), (b"data", 0.03)]),
])
def test_one_deadline_covers_text_block_header_and_payload(monkeypatch, binary, responses):
    session, sock, clock = session_with_socket(monkeypatch, responses)
    query = session.query_block if binary else session.query

    with pytest.raises(TimeoutError):
        query("SLOW?")
    assert clock.now == pytest.approx(0.1)
    assert sock.closed
    assert all(0 < timeout <= 0.1 for timeout in sock.timeouts)
    assert sock.timeouts[-1] < sock.timeouts[0]


def test_send_and_buffered_block_header_share_payload_deadline(monkeypatch):
    session, sock, clock = session_with_socket(
        monkeypatch, [(b"data", 0.05)], send_seconds=0.06,
    )
    session._read_buffer.extend(b"#14")
    with pytest.raises(TimeoutError):
        session.query_block("DATA?")
    assert clock.now == pytest.approx(0.1)
    assert sock.closed


def test_success_restores_timeout_and_gives_next_query_its_own_deadline(monkeypatch):
    session, sock, clock = session_with_socket(monkeypatch, [
        (b"one\n", 0.08), (b"two\n", 0.08),
    ])
    assert session.query("FIRST?") == "one"
    assert sock.timeout == 0.1
    assert session.query("SECOND?") == "two"
    assert sock.timeout == 0.1
    assert clock.now == pytest.approx(0.16)


def test_actual_empty_line_remains_no_response_not_timeout(monkeypatch):
    session, sock, _ = session_with_socket(monkeypatch, [(b"\nvalue\n", 0.0)])
    assert session.query("EMPTY?") == ""
    assert session.query("NEXT?") == "value"
    assert not sock.closed


def test_block_trailer_does_not_consume_following_empty_response(monkeypatch):
    session, _, _ = session_with_socket(monkeypatch, [
        (encode_block(b"data") + b"\r\n\nnext\n", 0.0),
    ])
    assert session.query_block("DATA?") == b"data"
    assert session.query("EMPTY?") == ""
    assert session.query("NEXT?") == "next"


def test_required_empty_response_poisons_with_error_queue_command(monkeypatch):
    session, sock, _ = session_with_socket(monkeypatch, [(b"\n", 0.0)])
    with pytest.raises(TimeoutError) as raised:
        session.drain_errors()
    assert raised.value.scpi_command == "SYST:ERR?"
    assert sock.closed


def test_close_failure_does_not_mask_timeout_or_retain_receive_state(monkeypatch):
    session, sock, _ = session_with_socket(monkeypatch, [(b"late", 0.2)])
    session._awaiting_lf_after_cr = True

    def fail_close():
        raise OSError("close failed")

    monkeypatch.setattr(sock, "close", fail_close)
    with pytest.raises(TimeoutError) as raised:
        session.query("SLOW?")
    assert raised.value.scpi_command == "SLOW?"
    assert session._socket is None
    assert not session._read_buffer
    assert not session._awaiting_lf_after_cr
    assert not session._pending_block_ending


def test_expired_deadline_rejects_fully_buffered_block(monkeypatch):
    session, sock, _ = session_with_socket(monkeypatch, [], send_seconds=0.11)
    session._read_buffer.extend(encode_block(b"data"))
    with pytest.raises(TimeoutError):
        session.query_block("DATA?")
    assert sock.closed
    assert not sock.received_sizes


@pytest.mark.parametrize("binary,wire", [
    (False, b""), (False, b"truncated"), (True, b"#14ab"),
    (True, b"not a block\n"), (True, b"#0"), (True, b"#x"),
    (True, b"#1x"), (True, b"#299"),
])
def test_eof_and_invalid_framing_poison_with_command(monkeypatch, binary, wire):
    session, sock, _ = session_with_socket(monkeypatch, [(wire, 0.0), (b"", 0.0)])
    with pytest.raises((ConnectionError, ValueError)) as raised:
        if binary:
            session.query_block("BROKEN?", max_bytes=8)
        else:
            session.query("BROKEN?")
    assert raised.value.scpi_command == "BROKEN?"
    assert sock.closed
    assert session._socket is None


def test_loopback_crlf_and_binary_trailer_through_real_socket():
    payload = b"\n#\r\x00"
    with FakeScpiServer({
        "HEADER?": b"header\r\n",
        "DATA?": encode_block(payload) + b"\r\n",
        "NEXT?": "next",
    }, chunks=8) as server:
        assert server.address[0] == "127.0.0.1"
        with scpi.ScpiSession(*server.address, timeout=1.0) as session:
            assert session.query("HEADER?") == "header"
            assert session.query_block("DATA?") == payload
            assert session.drain_errors() == []
            assert session.query("NEXT?") == "next"


def test_loopback_no_bytes_timeout_closes_without_sending_next_query():
    with FakeScpiServer({"FIRST?": "", "SECOND?": "second"}) as server:
        with scpi.ScpiSession(*server.address, timeout=0.05) as session:
            with pytest.raises(TimeoutError) as raised:
                session.query("FIRST?")
            assert raised.value.scpi_command == "FIRST?"
            with pytest.raises(ConnectionError):
                session.query("SECOND?")
        assert server.received == ["FIRST?"]


def test_query_once_uses_the_same_complete_response_contract():
    with FakeScpiServer({"ID?": b"first\nextra\n"}) as server:
        assert scpi.query_once(*server.address, "ID?", timeout=1.0) == "first"


@pytest.mark.parametrize("binary", [False, True])
def test_waveform_success_with_idn_and_framed_responses(binary):
    samples = [0.0, 1.0, 0.0, -1.0] * 4
    payload = struct.pack("<16f", *samples)
    responses = {
        "*IDN?": "ACME,SCOPE,1,1",
        "CHAN1:DATA:HEAD?": b"0,0.015,0.001\r\n",
        "CHAN1:DATA?": encode_block(payload) + b"\r\n" if binary else ",".join(map(str, samples)),
    }
    with FakeScpiServer(responses, chunks=8) as server:
        result = read_waveform(*server.address, timeout=1.0, waveform_format="real" if binary else "ascii")
    assert result["sample_count"] == 16
    assert result["samples"] == samples
    assert result["sample_rate_hz"] == pytest.approx(1000.0)
    assert result["failures"] == []
    assert result["errors"] == []


@pytest.mark.parametrize("silent_command", ["*IDN?", "CHAN1:DATA:HEAD?", "CHAN1:DATA?"])
def test_waveform_timeout_reports_actual_query_command(silent_command):
    responses = {
        "*IDN?": "ACME,SCOPE,1,1",
        "CHAN1:DATA:HEAD?": "0,0.003,0.001",
        "CHAN1:DATA?": encode_block(struct.pack("<4f", 0, 1, 0, -1)),
    }
    responses[silent_command] = ""
    with FakeScpiServer(responses) as server:
        result = read_waveform(*server.address, timeout=0.05)
        assert silent_command in server.received
    assert result["sample_count"] == 0
    assert result["failures"][0]["category"] == "timeout"
    assert result["failures"][0]["command"] == silent_command
