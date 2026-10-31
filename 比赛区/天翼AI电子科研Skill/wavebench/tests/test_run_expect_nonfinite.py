from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from wavebench.logging import CommandLogger
from wavebench.services.run_analysis import evaluate_expect, step_status
from wavebench.services.run_plan import load_run_plan
from wavebench.services.run_service import RunService

from test_run_service import fake_capture, make_config, write_plan


@pytest.mark.parametrize(
    "value",
    [float("nan"), float("inf"), float("-inf"), "nan", "inf", "-inf"],
)
@pytest.mark.parametrize("limits", [{"min": 0}, {"max": 10}, {"min": 0, "max": 10}])
def test_expect_rejects_nonfinite_values_with_structured_reason(value, limits):
    result = evaluate_expect({"metric": value}, {"metric": limits})

    assert result["status"] == "failed"
    check = result["checks"]["metric"]
    assert check["status"] == "failed"
    assert check["reason"] == "not_finite"
    assert check["value"] == str(float(value))
    assert check["limits"] == limits
    assert result["failures"] == ["metric: not finite"]


@pytest.mark.parametrize(
    ("value", "limits", "status", "reason"),
    [
        (1.5, {"min": 1.0, "max": 2.0}, "ok", None),
        (1.0, {"min": 1.0, "max": 1.0}, "ok", None),
        (0.5, {"min": 1.0}, "failed", None),
        (3.0, {"max": 2.0}, "failed", None),
        ("1.5", {"min": 1.0, "max": 2.0}, "ok", None),
        ("not-a-number", {"min": 0.0}, "failed", "not_numeric"),
    ],
)
def test_expect_keeps_finite_and_numeric_edge_semantics(value, limits, status, reason):
    result = evaluate_expect({"metric": value}, {"metric": limits})

    assert result["status"] == status
    check = result["checks"]["metric"]
    assert check["status"] == status
    if reason is not None:
        assert check["reason"] == reason


def test_expect_missing_value_remains_unavailable():
    result = evaluate_expect({}, {"metric": {"min": 0.0}})

    assert result["status"] == "failed"
    assert result["checks"]["metric"]["reason"] == "unavailable"


@pytest.mark.parametrize("field", ["expect", "expect_fft"])
def test_nonfinite_expect_marks_step_failed(field):
    expectation = evaluate_expect({"metric": float("nan")}, {"metric": {"min": 0.0}})

    assert step_status({field: expectation}) == "failed"


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
@pytest.mark.parametrize("expect_fft", [False, True])
def test_nonfinite_expect_fails_run_and_stops_following_capture(
    tmp_path, value, expect_fft
):
    field = "expect_fft" if expect_fft else "expect"
    metric = "peak_frequency_hz" if expect_fft else "voltage_vpp_v"
    plan = load_run_plan(
        write_plan(
            tmp_path,
            f"""
[[steps]]
kind = "scope.capture"

[steps.{field}]
{metric} = {{ min = 0.0 }}

[[steps]]
kind = "scope.capture"
label = "later"
""",
        )
    )
    capture = fake_capture(tmp_path, "nonfinite", voltage_vpp_v=value)
    with patch("wavebench.services.run_service.ScopeService") as scope_cls, patch(
        "wavebench.services.run_service.capture_fft_summary",
        return_value={"status": "ok", "peak_frequency_hz": value},
    ):
        scope_cls.return_value.capture_waveform.return_value = capture
        result = RunService(config=make_config(tmp_path), logger=CommandLogger()).run(plan)

    scope_cls.return_value.capture_waveform.assert_called_once()
    assert len(result.steps) == 1
    assert result.steps[0].status == "failed"
    assert result.steps[0].artifact[field]["checks"][metric]["reason"] == "not_finite"
    run = json.loads(result.run_json_path.read_text(encoding="utf-8"))
    assert run["status"] == "failed"
    assert run["error"]["code"] == "step_failed"
