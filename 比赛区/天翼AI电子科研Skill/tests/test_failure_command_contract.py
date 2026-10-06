"""Exercise CLI failures through the real session and waveform helper."""

import json
import struct

import pytest

from tianyi_electronics_skill import cli, scpi, waveform
from tianyi_electronics_skill.core import autoscale_scope
from tianyi_electronics_skill.dialects import AUTOSCALE_CANDIDATES


class ScriptedSocket:
    """Replace only the socket; SCPI framing, decoding and draining stay real."""

    def __init__(self, *, fail_command=None, recv_fail_command=None, failure=None, data=None,
                 drain_failure_at=None, readbacks=None, reject_format=False, close_failure=None,
                 accept_autoset=False):
        samples = [1.25 if index % 10 < 5 else -0.75 for index in range(200)]
        payload = struct.pack("<200f", *samples)
        length = str(len(payload)).encode("ascii")
        self.data = data if data is not None else b"#" + str(len(length)).encode() + length + payload
        self.fail_command = fail_command
        self.recv_fail_command = recv_fail_command
        self.failure = failure
        self.drain_failure_at = drain_failure_at
        self.readbacks = readbacks or {}
        self.reject_format = reject_format
        self.close_failure = close_failure
        self.accept_autoset = accept_autoset
        self.commands = []
        self.last_write = None
        self.buffer = b""
        self.drain_count = 0
        self.pending_failure = None
        self.timeout = None

    def settimeout(self, timeout):
        self.timeout = timeout

    def gettimeout(self):
        return self.timeout

    def shutdown(self, _how):
        pass

    def close(self):
        if self.close_failure is not None:
            raise self.close_failure

    def sendall(self, data):
        command = data.decode("ascii").strip()
        self.commands.append(command)
        if command == self.fail_command:
            raise self.failure
        if command == "SYST:ERR?":
            self.drain_count += 1
            if self.drain_count == self.drain_failure_at:
                self.buffer = b""
                self.pending_failure = TimeoutError("error queue timed out")
            elif (self.last_write in AUTOSCALE_CANDIDATES and not self.accept_autoset) or (
                self.reject_format and self.last_write == "FORM REAL"
            ):
                self.buffer = b'-113,"Undefined header"\n'
                self.last_write = None
            else:
                self.buffer = b'0,"No error"\n'
        elif command == "*IDN?":
            self.buffer = b"ACME,SCOPE,1,1\n"
        elif command == "CHAN1:DATA:HEAD?":
            self.buffer = b"0,0.02,0.0001\n"
        elif command == "CHAN1:DATA?":
            self.buffer = self.data
        elif command in ("TIMebase:RANGe?", ":CHAN1:OFFS?"):
            response = self.readbacks.get(command, "0.005" if command.startswith("TIM") else "-0.25")
            self.buffer = (response + "\n").encode()
        else:
            self.last_write = command
        if command == self.recv_fail_command:
            self.buffer = b""
            self.pending_failure = self.failure

    def recv(self, size):
        if self.pending_failure is not None:
            failure, self.pending_failure = self.pending_failure, None
            raise failure
        chunk, self.buffer = self.buffer[:size], self.buffer[size:]
        return chunk


def install_socket(monkeypatch, **kwargs):
    sock = ScriptedSocket(**kwargs)
    monkeypatch.setattr(scpi.socket, "create_connection", lambda *_args, **_kwargs: sock)
    return sock


def run_autoscale(capsys, *, apply=False):
    args = ["autoscale", "--address", "example.test"]
    if apply:
        args.append("--apply")
    code = cli.main(args)
    output = capsys.readouterr()
    assert output.err == ""
    return code, json.loads(output.out)


@pytest.mark.parametrize(
    ("kwargs", "category", "command"),
    [
        ({"recv_fail_command": "CHAN1:DATA?", "failure": TimeoutError("data timed out")},
         "timeout", "CHAN1:DATA?"),
        ({"data": b"#13abc"}, "parse_error", "CHAN1:DATA?"),
        ({"data": b"xx"}, "parse_error", "CHAN1:DATA?"),
        ({"fail_command": "CHAN1:DATA:HEAD?", "failure": TimeoutError("header timed out")},
         "timeout", "CHAN1:DATA:HEAD?"),
        ({"fail_command": "FORM REAL", "failure": OSError("write failed")},
         "transport_error", "FORM REAL"),
        ({"drain_failure_at": 1}, "timeout", "SYST:ERR?"),
        ({"drain_failure_at": 3}, "error_queue_unavailable", "SYST:ERR?"),
        ({"reject_format": True}, "unsupported", "FORM REAL"),
    ],
)
def test_cli_autoscale_real_waveform_failure_has_command(monkeypatch, capsys, kwargs, category, command):
    install_socket(monkeypatch, **kwargs)

    code, result = run_autoscale(capsys)

    assert code == 2
    assert result["applied"] is False
    assert result["failures"] == result["waveform"]["failures"]
    assert result["failures"][0]["category"] == category
    assert result["failures"][0]["command"] == command
    assert all(set(failure) == {"category", "command", "message"} for failure in result["failures"])


@pytest.mark.parametrize(
    ("apply", "readbacks", "expected_code", "applied", "partial"),
    [
        (False, {}, 0, False, False),
        (True, {}, 0, True, False),
        (True, {"TIMebase:RANGe?": "1", ":CHAN1:OFFS?": "1"}, 2, False, False),
        (True, {":CHAN1:OFFS?": "1"}, 3, False, True),
    ],
)
def test_cli_autoscale_exit_contract_with_actual_waveform(monkeypatch, capsys, apply, readbacks,
                                                        expected_code, applied, partial):
    sock = install_socket(monkeypatch, readbacks=readbacks)

    code, result = run_autoscale(capsys, apply=apply)

    assert code == expected_code
    assert result["applied"] is applied
    assert result["partially_applied"] is partial
    assert result["waveform"]["sample_count"] == 200
    assert result["waveform"]["failures"] == []
    if not apply:
        assert result["failures"] == []
        assert not any(command in AUTOSCALE_CANDIDATES for command in sock.commands)
        assert not any(command.startswith("TIMebase:RANGe ") for command in sock.commands)
    else:
        assert [item["command"] for item in result["write_results"][-2:]] == [
            "TIMebase:RANGe 0.005", ":CHAN1:OFFS -0.25",
        ]


@pytest.mark.parametrize(
    ("kwargs", "apply", "command", "expected_code"),
    [
        ({"fail_command": "*IDN?", "failure": TimeoutError("idn timed out")}, False, "*IDN?", 2),
        ({"fail_command": AUTOSCALE_CANDIDATES[0], "failure": OSError("autoset write failed")},
         True, AUTOSCALE_CANDIDATES[0], 2),
        ({"drain_failure_at": 1}, True, "SYST:ERR?", 2),
        ({"fail_command": ":CHAN1:OFFS -0.25", "failure": OSError("offset write failed")},
         True, ":CHAN1:OFFS -0.25", 3),
        ({"drain_failure_at": len(AUTOSCALE_CANDIDATES) * 2 + 5}, True, "SYST:ERR?", 3),
    ],
)
def test_cli_autoscale_outer_failure_tracks_executing_command(monkeypatch, capsys, kwargs,
                                                             apply, command, expected_code):
    install_socket(monkeypatch, **kwargs)

    code, result = run_autoscale(capsys, apply=apply)

    assert code == expected_code
    assert result["failures"][-1]["command"] == command


def test_collect_preserves_exception_type_identity_and_cause(monkeypatch):
    cause = RuntimeError("underlying transport failure")
    failure = TimeoutError("data timed out")
    failure.__cause__ = cause
    install_socket(monkeypatch)
    result = waveform._new_result("example.test", 5025, "CHAN1", "auto", "real", None)

    with scpi.ScpiSession("example.test") as session, pytest.raises(TimeoutError) as raised:
        def fail_query_block(*_args, **_kwargs):
            raise failure

        monkeypatch.setattr(session, "query_block", fail_query_block)
        waveform._collect_waveform(result, session, "CHAN1")

    assert raised.value is failure
    assert raised.value.__cause__ is cause
    assert raised.value.scpi_command == "CHAN1:DATA?"


@pytest.mark.parametrize("helper", [waveform.read_waveform, autoscale_scope])
def test_connection_failure_uses_explicit_stage(monkeypatch, helper):
    def fail_connect(*_args, **_kwargs):
        raise TimeoutError("connect timed out")

    monkeypatch.setattr(scpi.socket, "create_connection", fail_connect)

    result = helper("example.test")

    assert result["failures"] == [{
        "category": "timeout", "command": "connect", "message": "connect timed out",
    }]


@pytest.mark.parametrize("helper", [waveform.read_waveform, autoscale_scope])
def test_session_configuration_failure_uses_explicit_stage(monkeypatch, helper):
    def unexpected_connect(*_args, **_kwargs):
        pytest.fail("invalid session configuration must fail before connecting")

    monkeypatch.setattr(scpi.socket, "create_connection", unexpected_connect)

    result = helper("example.test", timeout=0)

    assert result["failures"][0]["category"] == "configuration_error"
    assert result["failures"][0]["command"] == "configure_session"


def test_fetch_failure_uses_actual_data_query(monkeypatch, capsys):
    install_socket(monkeypatch, data=b"#13abc")

    code = cli.main(["fetch", "--address", "example.test"])
    result = json.loads(capsys.readouterr().out)

    assert code == 2
    assert result["failures"][0]["command"] == "CHAN1:DATA?"
    assert result["failures"][0]["category"] == "parse_error"


@pytest.mark.parametrize(
    ("readbacks", "verified_count", "expected_code", "applied", "partial"),
    [
        ({"TIMebase:RANGe?": "1", ":CHAN1:OFFS?": "1"}, 0, 2, False, False),
        ({":CHAN1:OFFS?": "1"}, 1, 3, False, True),
        ({}, 2, 2, True, False),
    ],
)
def test_cli_close_failure_keeps_verified_application_state(monkeypatch, capsys, readbacks,
                                                           verified_count, expected_code,
                                                           applied, partial):
    sock = install_socket(monkeypatch, readbacks=readbacks,
                          close_failure=OSError("socket close failed"))

    code, result = run_autoscale(capsys, apply=True)

    assert code == expected_code
    assert result["applied"] is applied
    assert result["partially_applied"] is partial
    assert result["mode"] == "derived_settings"
    writes = [item for item in result["write_results"] if "write_accepted" in item]
    assert len(writes) == 2
    assert sum(item["verified"] for item in writes) == verified_count
    assert [command for command in sock.commands if command in (
        "TIMebase:RANGe 0.005", ":CHAN1:OFFS -0.25",
    )] == ["TIMebase:RANGe 0.005", ":CHAN1:OFFS -0.25"]
    assert result["errors"] == ["transport_error: socket close failed"]
    assert result["failures"][-1] == {
        "category": "transport_error", "command": "close", "message": "socket close failed",
    }


def test_cli_vendor_autoset_close_failure_keeps_applied_state(monkeypatch, capsys):
    install_socket(monkeypatch, accept_autoset=True, close_failure=OSError("socket close failed"))

    code, result = run_autoscale(capsys, apply=True)

    assert code == 2
    assert result["applied"] is True
    assert result["partially_applied"] is False
    assert result["mode"] == "vendor_autoset"
    assert result["write_results"][0]["ok"] is True
    assert result["failures"] == [{
        "category": "transport_error", "command": "close", "message": "socket close failed",
    }]


def test_cli_operation_and_close_failure_are_both_preserved(monkeypatch, capsys):
    install_socket(monkeypatch, fail_command=":CHAN1:OFFS -0.25", failure=OSError("offset failed"),
                   close_failure=OSError("socket close failed"))

    code, result = run_autoscale(capsys, apply=True)

    assert code == 3
    assert result["applied"] is False
    assert result["partially_applied"] is True
    failures = {(item["command"], item["category"], item["message"]) for item in result["failures"]}
    assert (":CHAN1:OFFS -0.25", "transport_error", "offset failed") in failures
    assert ("close", "transport_error", "socket close failed") in failures


def test_cli_dry_run_close_failure_is_an_execution_failure(monkeypatch, capsys):
    install_socket(monkeypatch, close_failure=OSError("socket close failed"))

    code, result = run_autoscale(capsys)

    assert code == 2
    assert result["applied"] is False
    assert result["partially_applied"] is False
    assert result["waveform"]["sample_count"] == 200
    assert result["failures"] == [{
        "category": "transport_error", "command": "close", "message": "socket close failed",
    }]


def test_cli_interruption_before_first_verified_write_keeps_unapplied(monkeypatch, capsys):
    install_socket(monkeypatch, fail_command="TIMebase:RANGe 0.005",
                   failure=OSError("timebase write failed"),
                   close_failure=OSError("socket close failed"))

    code, result = run_autoscale(capsys, apply=True)

    assert code == 2
    assert result["applied"] is False
    assert result["partially_applied"] is False
    assert result["mode"] == "derived_settings"
    assert not any(item["ok"] for item in result["write_results"])
    assert {item["command"] for item in result["failures"]} >= {
        "TIMebase:RANGe 0.005", "close",
    }
