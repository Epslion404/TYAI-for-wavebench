"""measure 方言探测、诚实失败与 inspect 空状态的离线测试。

注意：设备"无响应"时客户端要等满 `timeout` 才能判定为空响应，
因此涉及空响应的用例统一用较短的 `timeout=0.3`，保持测试秒级完成。
"""

from __future__ import annotations

import math
import struct

import pytest
from fake_scpi import FakeScpiServer, encode_block

from tianyi_electronics_skill.core import inspect_device, measure_scope
from tianyi_electronics_skill.dialects import detect_vendor
from tianyi_electronics_skill.waveform import read_waveform

IDN_KEYSIGHT = "Keysight Technologies,DSOX1204G,CN12345678,02.11"
IDN_RIGOL = "RIGOL TECHNOLOGIES,DS1104Z,DS1ZA123456,00.04.04"
IDN_RS = "Rohde&Schwarz,RTM2032,5710.0999k32/101665,06.010"

REJECTION = '-113,"Undefined header"'
MEASURE_ITEMS = ("FREQUENCY", "VPP", "VMAX", "VMIN", "VAVG", "VRMS")
# 被设备拒绝的命令会各自往错误队列压一条，模拟真实固件行为。
SCALAR_REJECTED = {f"MEASURE:{item}? CHAN1": REJECTION for item in MEASURE_ITEMS}
ITEM_REJECTED = {f"MEASURE:ITEM? {item},CHAN1": REJECTION for item in MEASURE_ITEMS}

SCALAR_STYLE = {
    "MEASURE:FREQUENCY? CHAN1": "1000.0",
    "MEASURE:VPP? CHAN1": "2.0",
    "MEASURE:VMAX? CHAN1": "1.0",
    "MEASURE:VMIN? CHAN1": "-1.0",
    "MEASURE:VAVG? CHAN1": "0.0",
    "MEASURE:VRMS? CHAN1": "0.707",
}

ITEM_STYLE = {
    "MEASURE:ITEM? FREQUENCY,CHAN1": "2000.0",
    "MEASURE:ITEM? VPP,CHAN1": "3.0",
    "MEASURE:ITEM? VMAX,CHAN1": "1.5",
    "MEASURE:ITEM? VMIN,CHAN1": "-1.5",
    "MEASURE:ITEM? VAVG,CHAN1": "0.1",
    "MEASURE:ITEM? VRMS,CHAN1": "1.0",
}


def test_detect_vendor():
    assert detect_vendor(IDN_KEYSIGHT) == "keysight"
    assert detect_vendor(IDN_RIGOL) == "rigol"
    assert detect_vendor(IDN_RS) == "r&s"
    assert detect_vendor("ACME,WIDGET,1,0") == "unknown"
    assert detect_vendor("") == "unknown"


def test_measure_reports_values_commands_and_vendor():
    responses = {"*IDN?": IDN_KEYSIGHT, **SCALAR_STYLE}
    with FakeScpiServer(responses) as server:
        host, port = server.address
        result = measure_scope(host, port, "CHAN1", timeout=2.0)

    assert result["vendor"] == "keysight"
    assert result["measurements"]["frequency_hz"] == 1000.0
    assert result["commands"]["frequency_hz"] == "MEASure:FREQuency? CHAN1"
    assert result["unsupported"] == {}
    assert result["device_errors"] == []
    assert result["fetch_fallback"] is None


def test_measure_falls_back_to_item_style_and_reports_device_errors():
    responses = {"*IDN?": IDN_RIGOL, **ITEM_STYLE}
    with FakeScpiServer(responses, command_errors=SCALAR_REJECTED) as server:
        host, port = server.address
        result = measure_scope(host, port, "CHAN1", timeout=0.3)

    assert result["vendor"] == "rigol"
    assert result["measurements"]["frequency_hz"] == 2000.0
    assert result["commands"]["frequency_hz"] == "MEASure:ITEM? FREQuency,CHAN1"
    assert result["unsupported"] == {}
    assert result["device_errors"] == [REJECTION] * 6


def test_measure_reports_unsupported_and_runs_fetch_fallback():
    count = 200
    rate = 100_000.0
    samples = [0.5 * math.sin(2 * math.pi * 1_000 * i / rate) for i in range(count)]
    responses: dict[str, object] = {
        "*IDN?": IDN_RS,
        "FORM REAL": "",
        "FORM:BORD LSBF": "",
        "CHAN1:DATA:HEAD?": f"0,{(count - 1) / rate:.9f},{1 / rate:.9f}",
        "CHAN1:DATA?": encode_block(struct.pack(f"<{count}f", *samples)),
    }
    with FakeScpiServer(responses, command_errors={**SCALAR_REJECTED, **ITEM_REJECTED}) as server:
        host, port = server.address
        result = measure_scope(host, port, "CHAN1", timeout=0.3)

    assert result["measurements"] == {}
    assert set(result["unsupported"]) == {
        "frequency_hz",
        "vpp_v",
        "vmax_v",
        "vmin_v",
        "vavg_v",
        "vrms_v",
    }
    assert all("unsupported" in reason for reasons in result["unsupported"].values() for reason in reasons)
    assert result["device_errors"] == [REJECTION] * 12
    assert result["fetch_fallback"]["ok"] is True
    assert result["fetch_fallback"]["metrics"]["frequency_hz"] == pytest.approx(1000.0)


def test_measure_can_disable_fetch_fallback():
    with FakeScpiServer(
        {"*IDN?": IDN_RS},
        command_errors={**SCALAR_REJECTED, **ITEM_REJECTED},
    ) as server:
        host, port = server.address
        result = measure_scope(host, port, "CHAN1", timeout=0.3, fetch_fallback=False)

    assert result["fetch_fallback"] is None
    assert result["measurements"] == {}


def test_measure_reports_parse_error_for_non_numeric_answer():
    responses = {"*IDN?": IDN_KEYSIGHT, "MEASURE:FREQUENCY? CHAN1": "BROKEN"}
    with FakeScpiServer(responses, default_response="") as server:
        host, port = server.address
        result = measure_scope(host, port, "CHAN1", timeout=0.3, fetch_fallback=False)

    reasons = result["unsupported"]["frequency_hz"]
    assert any("parse_error" in reason for reason in reasons)
    assert any("no_response" in reason for reason in reasons)


def test_inspect_marks_empty_channel_state_as_unsupported():
    responses = {"*IDN?": IDN_RS, "*OPC?": "1"}
    with FakeScpiServer(responses, default_response="") as server:
        host, port = server.address
        result = inspect_device(host, port, timeout=0.3)

    assert result["idn"] == IDN_RS
    assert result["channels"]["CHAN1"]["display"] is None
    assert "unsupported" in result["channels"]["CHAN1"]
    assert result["transport_errors"] == []


def test_inspect_reports_channel_state_and_transport_errors():
    responses = {"*IDN?": IDN_RIGOL, "*OPC?": "1", ":CHAN1:DISP?": "1"}
    with FakeScpiServer(responses, default_response="") as server:
        host, port = server.address
        result = inspect_device(host, port, timeout=0.3)

    assert result["channels"]["CHAN1"] == {"display": "1"}
    assert result["channels"]["CHAN2"]["display"] is None
    assert result["channels"]["CHAN2"]["unsupported"] == "设备返回空响应"


def test_waveform_reader_without_block_support_fails_honestly():
    with FakeScpiServer({"CHAN1:DATA:HEAD?": "0,0.001,1e-05"}, default_response="") as server:
        host, port = server.address
        result = read_waveform(host, port, "CHAN1", timeout=0.3)

    assert result["sample_count"] == 0
    assert result["errors"], "读取失败必须显式记录，而不是返回空波形"
