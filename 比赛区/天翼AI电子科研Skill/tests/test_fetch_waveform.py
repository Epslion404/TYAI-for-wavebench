"""波形读取（fetch）与落盘的离线测试。"""

from __future__ import annotations

import json
import math
import struct
from pathlib import Path

import pytest
from fake_scpi import FakeScpiServer, encode_block

from tianyi_electronics_skill import cli
from tianyi_electronics_skill.core import load_waveform
from tianyi_electronics_skill.waveform import read_waveform, write_waveform

SAMPLE_RATE = 100_000.0
COUNT = 500


def _waveform_responses(*, style: str = "real", count: int = COUNT, rate: float = SAMPLE_RATE) -> dict[str, object]:
    samples = [0.5 * math.sin(2 * math.pi * 1_000 * index / rate) for index in range(count)]
    header = f"0,{(count - 1) / rate:.9f},{1 / rate:.9f}"
    responses: dict[str, object] = {
        "*IDN?": "Rohde&Schwarz,RTM2032,TEST-FETCH,06.010",
        "CHAN1:DATA:HEAD?": header,
    }
    if style == "real":
        responses["FORM REAL"] = ""
        responses["FORM:BORD LSBF"] = ""
        responses["CHAN1:DATA?"] = encode_block(struct.pack(f"<{count}f", *samples))
    else:
        responses["FORM ASC"] = ""
        responses["CHAN1:DATA?"] = ",".join(f"{value:.6f}" for value in samples)
    return responses


def test_read_waveform_real_block_reports_metrics():
    with FakeScpiServer(_waveform_responses()) as server:
        host, port = server.address
        result = read_waveform(host, port, "CHAN1", timeout=2.0)

    assert result["sample_count"] == COUNT
    assert result["sample_rate_hz"] == pytest.approx(SAMPLE_RATE)
    assert result["header_consistent"] is True
    assert result["warnings"] == []
    assert result["metrics"]["frequency_hz"] == pytest.approx(1_000, abs=5)
    assert len(result["samples"]) == COUNT


def test_read_waveform_ascii_format():
    with FakeScpiServer(_waveform_responses(style="ascii", count=4, rate=100_000.0)) as server:
        host, port = server.address
        result = read_waveform(host, port, "CHAN1", timeout=2.0, waveform_format="ascii")

    assert result["sample_count"] == 4
    assert result["sample_rate_hz"] == pytest.approx(100_000.0)


def test_read_waveform_flags_inconsistent_header():
    responses = _waveform_responses()
    responses["CHAN1:DATA:HEAD?"] = "0,1.0,1e-05"  # xstop 与样本数不一致
    with FakeScpiServer(responses) as server:
        host, port = server.address
        result = read_waveform(host, port, "CHAN1", timeout=2.0)

    assert result["header_consistent"] is False
    assert result["sample_rate_hz"] is None
    assert result["metrics"]["frequency_hz"] is None
    assert result["header_raw"] == "0,1.0,1e-05"
    assert any("header" in warning for warning in result["warnings"])


def test_read_waveform_honours_explicit_sample_rate():
    responses = _waveform_responses()
    responses["CHAN1:DATA:HEAD?"] = "0,1.0,1e-05"
    with FakeScpiServer(responses) as server:
        host, port = server.address
        result = read_waveform(host, port, "CHAN1", timeout=2.0, sample_rate=SAMPLE_RATE)

    assert result["sample_rate_hz"] == pytest.approx(SAMPLE_RATE)
    assert result["metrics"]["frequency_hz"] == pytest.approx(1_000, abs=5)


def test_write_waveform_csv_and_json_round_trip(tmp_path):
    with FakeScpiServer(_waveform_responses()) as server:
        host, port = server.address
        result = read_waveform(host, port, "CHAN1", timeout=2.0)

    csv_path = write_waveform(result, tmp_path / "wave.csv")
    values, rate = load_waveform(csv_path)
    assert rate == pytest.approx(SAMPLE_RATE)
    assert len(values) == COUNT
    assert values[1] == pytest.approx(result["samples"][1])

    json_path = write_waveform(result, tmp_path / "wave.json")
    payload = json.loads(Path(json_path).read_text(encoding="utf-8"))
    assert payload["sample_rate_hz"] == pytest.approx(SAMPLE_RATE)
    assert len(payload["samples"]) == COUNT


def test_cli_fetch_writes_output_and_hides_samples_by_default(tmp_path, capsys):
    with FakeScpiServer(_waveform_responses()) as server:
        host, port = server.address
        output = tmp_path / "channel1.csv"
        code = cli.main(
            [
                "fetch",
                "--address",
                host,
                "--port",
                str(port),
                "--channel",
                "CHAN1",
                "--output",
                str(output),
            ]
        )
    printed = json.loads(capsys.readouterr().out)

    assert code == 0
    assert output.exists()
    assert "samples" not in printed
    assert printed["sample_count"] == COUNT
    assert printed["output"] == str(output)
