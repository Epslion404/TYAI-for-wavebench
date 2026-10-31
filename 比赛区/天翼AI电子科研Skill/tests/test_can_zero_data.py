"""CAN DLC=0 frames normalize to empty samples without faking a waveform."""

import json

import pytest

from tianyi_electronics_skill.analysis import analyze_samples
from tianyi_electronics_skill.cli import main
from tianyi_electronics_skill.protocols import (
    CanAdapter,
    JsonWaveformAdapter,
    ProtocolDecodeError,
    ProtocolRegistry,
)


def test_can_dlc_zero_derives_empty_samples_and_preserves_metadata():
    frame = CanAdapter().decode({"can_id": 291, "data": []}, "offline")

    assert frame.protocol == "can"
    assert frame.source == "offline"
    assert frame.samples == []
    assert frame.metadata == {"can_id": 291, "data_hex": ""}
    assert frame.as_dict()["samples"] == []


def test_can_empty_hex_data_is_also_dlc_zero():
    frame = CanAdapter().decode({"can_id": 291, "data": ""}, "offline")

    assert frame.samples == []
    assert frame.metadata["data_hex"] == ""


def test_can_dlc_zero_preserves_outer_metadata_and_device_errors():
    frame = CanAdapter().decode(
        {"can_id": 0x123, "data": [], "device_errors": ["remote"]},
        "offline",
        {"bus": "can0"},
    )

    assert frame.metadata == {"bus": "can0", "can_id": 291, "data_hex": ""}
    assert frame.device_errors == ["remote"]


def test_can_explicit_empty_samples_are_preserved_without_fabrication():
    frame = CanAdapter().decode({"can_id": 291, "data": [], "samples": []}, "offline")

    assert frame.samples == []


def test_can_explicit_empty_samples_override_data_without_fabrication():
    frame = CanAdapter().decode(
        {"can_id": 291, "data": [1, 2], "samples": []}, "offline"
    )

    assert frame.samples == []
    assert frame.metadata["data_hex"] == "0102"


def test_json_waveform_still_requires_nonempty_samples():
    with pytest.raises(ProtocolDecodeError, match="samples must not be empty"):
        JsonWaveformAdapter().decode({"samples": []}, "offline")


def test_protocol_registry_keeps_dlc_zero_result_ok():
    result = ProtocolRegistry().decode_result({"can_id": 291, "data": []})

    assert result["ok"] is True
    assert result["frame"]["samples"] == []
    assert result["frame"]["metadata"]["can_id"] == 291


def test_zero_can_frame_still_fails_at_the_analysis_entry():
    frame = CanAdapter().decode({"can_id": 291, "data": []}, "offline")

    with pytest.raises(ValueError, match="至少需要 4 个采样点"):
        analyze_samples(frame.samples, frame.sample_rate_hz)


@pytest.mark.parametrize("payload", [
    {"data": []},
    {"can_id": -1, "data": []},
    {"can_id": 0x20000000, "data": []},
    {"can_id": 291, "data": [256]},
    {"can_id": 291, "data": [-1]},
    {"can_id": 291, "data": "zz"},
    {"can_id": 291, "data": [], "samples": [float("nan")]},
    {"can_id": 291, "data": [], "samples": ["1"]},
])
def test_can_zero_data_keeps_existing_validation(payload):
    with pytest.raises((ProtocolDecodeError, ValueError)):
        CanAdapter().decode(payload, "offline")


@pytest.mark.parametrize(("payload", "data_hex"), [
    ({"can_id": 291, "data": []}, ""),
    ({"can_id": 291, "data": "", "samples": []}, ""),
    ({"can_id": 291, "data": [1, 2], "samples": []}, "0102"),
])
def test_cli_can_zero_data_is_success(tmp_path, capsys, payload, data_hex):
    path = tmp_path / "can.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    assert main(["protocol", "decode", "--file", str(path)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["ok"] is True
    assert result["frame"]["samples"] == []
    assert result["frame"]["metadata"]["data_hex"] == data_hex


def test_cli_can_invalid_id_stays_a_parse_error(tmp_path, capsys):
    path = tmp_path / "can.json"
    path.write_text(json.dumps({"can_id": -1, "data": []}), encoding="utf-8")

    assert main(["protocol", "decode", "--file", str(path)]) == 2
    result = json.loads(capsys.readouterr().out)
    assert result["ok"] is False
    assert result["failures"][0]["category"] == "parse_error"
