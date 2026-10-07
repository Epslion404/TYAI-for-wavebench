import json

import pytest

import tianyi_electronics_skill.core as core
from tianyi_electronics_skill.cli import main
from tianyi_electronics_skill.protocols import ProtocolRegistry


@pytest.mark.parametrize("rate", ["0", "-1", "nan", "inf"])
def test_cli_rejects_invalid_sample_rate_override(tmp_path, capsys, rate):
    payload = tmp_path / "wave.json"
    payload.write_text('{"samples": [1, 2], "sample_rate_hz": 1000}', encoding="utf-8")

    assert main(["protocol", "decode", "--file", str(payload), "--sample-rate", rate]) == 2
    result = json.loads(capsys.readouterr().out)
    assert result["ok"] is False
    assert result["failures"][0]["category"] == "configuration_error"
    assert result["device_errors"] == []


@pytest.mark.parametrize("field,value", [("magic_hex", 123), ("scale", []), ("sample_rate_hz", [])])
def test_cli_structures_malformed_manifest_fields(tmp_path, capsys, field, value):
    payload = tmp_path / "wave.bin"
    payload.write_bytes(b"AB\x00\x01")
    directory = tmp_path / "manifests"
    directory.mkdir()
    manifest = {
        "kind": "binary-waveform",
        "name": "bad",
        "match": {"magic_hex": "4142"},
        "payload": {"sample_type": "i16"},
    }
    manifest["match" if field == "magic_hex" else "payload"][field] = value
    (directory / "bad.json").write_text(json.dumps(manifest), encoding="utf-8")

    assert main(["protocol", "decode", "--file", str(payload), "--manifest-dir", str(directory)]) == 2
    result = json.loads(capsys.readouterr().out)
    assert result["failures"][0]["category"] == "parse_error"
    assert result["device_errors"] == []


@pytest.mark.parametrize("payload", [{"samples": [10**400]}, {"samples": [1], "sample_rate_hz": 10**400}])
def test_numeric_overflow_is_a_structured_decode_failure(payload):
    result = ProtocolRegistry().decode_result(payload)
    assert result["ok"] is False
    assert result["failures"][0]["category"] == "parse_error"


def test_decode_envelope_preserves_device_errors():
    result = ProtocolRegistry().decode_result({"samples": [1], "device_errors": ["device rejected"]})
    assert result["ok"] is True
    assert result["device_errors"] == result["frame"]["device_errors"] == ["device rejected"]


def test_cli_identify_has_local_error_queue_status(tmp_path, capsys):
    payload = tmp_path / "wave.json"
    payload.write_text('{"samples": [1]}', encoding="utf-8")

    assert main(["protocol", "identify", "--file", str(payload)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["device_errors"] == []
    assert result["device_errors_status"] == "not_applicable"


def test_cli_valid_override_preserves_frame_and_envelope(tmp_path, capsys):
    payload = tmp_path / "wave.json"
    payload.write_text('{"samples": [1], "device_errors": ["reported"]}', encoding="utf-8")

    assert main(["protocol", "decode", "--file", str(payload), "--sample-rate", "2000"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["frame"]["sample_rate_hz"] == 2000
    assert result["frame"]["samples"] == [1]
    assert result["device_errors"] == ["reported"]


@pytest.mark.parametrize(
    ("apply", "result", "expected_status"),
    [
        (False, {"applied": False, "partially_applied": False, "failures": [], "errors": []}, 0),
        (False, {"applied": False, "partially_applied": False, "failures": [{"category": "timeout"}], "errors": []}, 2),
        (True, {"applied": True, "partially_applied": False, "failures": [], "errors": []}, 0),
        (True, {"applied": False, "partially_applied": False, "failures": [{"category": "no_response"}], "errors": []}, 2),
        (True, {"applied": False, "partially_applied": True, "failures": [{"category": "verification_failed"}], "errors": []}, 3),
    ],
)
def test_cli_autoscale_exit_status_contract(monkeypatch, capsys, apply, result, expected_status):
    monkeypatch.setattr(core, "autoscale_scope", lambda *_args, **_kwargs: result)
    command = ["autoscale", "--address", "127.0.0.1"]
    if apply:
        command.append("--apply")

    assert main(command) == expected_status
    assert json.loads(capsys.readouterr().out) == result
