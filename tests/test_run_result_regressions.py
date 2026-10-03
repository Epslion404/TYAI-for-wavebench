"""Exercise result failures through the run lifecycle with offline instrument fakes."""

import csv
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from test_run_service import fake_capture, fake_frequency_response_capture, make_config, write_plan
from wavebench.cli import main
from wavebench.config import QualityConfig
from wavebench.errors import ConfigError
from wavebench.logging import CommandLogger
from wavebench.services.frequency_response import (
    analyze_frequency_response_point,
    write_frequency_response_csv,
)
from wavebench.services.run_analysis import evaluate_expect
from wavebench.services.run_plan import load_run_plan
from wavebench.services.run_service import RunService


@pytest.mark.parametrize("quality_gate", [False, True])
@pytest.mark.parametrize("on_failure", ["stop", "continue"])
def test_exhausted_recovery_evaluates_final_gate(tmp_path, quality_gate, on_failure):
    plan = load_run_plan(write_plan(tmp_path, f"""
[[steps]]
kind = "scope.capture"
quality_gate = {str(quality_gate).lower()}
auto_recover = true
on_failure = "{on_failure}"

[[steps]]
kind = "scope.capture"
label = "later"
"""))
    warning = ["low_signal_amplitude"]
    captures = [
        fake_capture(tmp_path, f"bad{i}", warning, frequency_hz=freq, voltage_vpp_v=0.001)
        for i, freq in enumerate([1000, 2000, 4000])
    ]
    later = fake_capture(tmp_path, "later")
    quality = QualityConfig(auto_recover_attempts=2, consistency_required_captures=2)
    with patch("wavebench.services.run_service.ScopeService") as scope_cls:
        scope = scope_cls.return_value
        scope.capture_waveform.side_effect = [*captures, later]
        result = RunService(config=make_config(tmp_path, quality), logger=CommandLogger()).run(plan)

    artifact = result.steps[0].artifact
    assert artifact["quality"]["status"] == "warning"
    assert len(artifact["quality_recovery"]["attempts"]) == 3
    assert artifact["quality_recovery"]["consistency"]["status"] == "diverged"
    assert artifact["package"] == str(captures[-1].package_dir)
    assert scope.autoscale.call_count == 2
    if quality_gate:
        assert artifact["quality_gate"] == {"status": "warning", "warnings": warning}
    else:
        assert "quality_gate" not in artifact
    stopped = quality_gate and on_failure == "stop"
    assert scope.capture_waveform.call_count == (3 if stopped else 4)
    assert len(result.steps) == (1 if stopped else 2)
    expected_status = "failed" if quality_gate else "ok"
    assert result.steps[0].status == result.status == expected_status
    run = json.loads(result.run_json_path.read_text(encoding="utf-8"))
    assert run["status"] == expected_status
    if stopped:
        assert run["error"]["code"] == "step_failed"
    with result.summary_csv_path.open(encoding="utf-8", newline="") as file:
        assert list(csv.DictReader(file))[0]["status"] == expected_status


@pytest.mark.parametrize("resume", [False, True])
def test_frequency_stop_preserves_failed_point_and_resume_summary(tmp_path, resume):
    if resume:
        capture = fake_frequency_response_capture(tmp_path, "previous", frequency_hz=1000)
        point = analyze_frequency_response_point(
            index=0,
            requested_frequency_hz=1000,
            reference_waveform=capture.waveforms[1],
            response_waveform=capture.waveforms[2],
            frequency_tolerance_ratio=0.05,
            capture_package=str(capture.package_dir),
            metadata_path=str(capture.metadata_path),
        )
        write_frequency_response_csv(tmp_path / "old.csv", [point])
    resume_field = 'resume_from = "old.csv"' if resume else ""
    plan = load_run_plan(write_plan(tmp_path, f"""
[[steps]]
kind = "sweep.frequency_response"
source_channel = 1
reference_channel = 1
response_channel = 2
frequencies_hz = [1000, 2000, 3000]
settle_s = 0
{resume_field}
[steps.stop_conditions]
max_failed_points = 1

[[steps]]
kind = "sleep"
duration_s = 0.001
"""))
    with patch("wavebench.services.run_service.ScopeService") as scope_cls, patch(
        "wavebench.services.run_service.SourceService"
    ) as source_cls:
        source = source_cls.return_value
        source.status.return_value = SimpleNamespace(output="ON")
        source.set_frequency.return_value = SimpleNamespace(output="ON")
        scope = scope_cls.return_value
        scope.capture_waveforms.side_effect = RuntimeError("fake bad acquisition")
        with pytest.raises(ConfigError, match="stop condition triggered: max_failed_points=1"):
            RunService(config=make_config(tmp_path), logger=CommandLogger()).run(plan)

    assert scope.capture_waveforms.call_count == source.set_frequency.call_count == 1
    source.set_frequency.assert_called_once_with(channel=1, value_hz=2000 if resume else 1000)
    [run_path] = (tmp_path / "data/runs").glob("*/run.json")
    run = json.loads(run_path.read_text(encoding="utf-8"))
    assert run["status"] == "failed"
    assert len(run["steps"]) == 1
    assert run["steps"][0]["status"] == "failed"
    response = run["steps"][0]["artifact"]["frequency_response"]
    assert response["stop_reason"] == "max_failed_points=1 (actual 1)"
    assert response["stop_conditions"] == {"max_failed_points": 1}
    assert response["failed_point_count"] == 1
    assert "ConfigError:" in response["error"]
    with (run_path.parent / "frequency_response.csv").open(encoding="utf-8", newline="") as file:
        rows = list(csv.DictReader(file))
    assert len(rows) == (2 if resume else 1)
    assert rows[-1]["status"] == "failed"
    assert "fake bad acquisition" in rows[-1]["error"]
    manifest = json.loads((run_path.parent / "frequency_responses.json").read_text(encoding="utf-8"))
    if resume:
        assert response["resume"]["reused_point_count"] == 1
        assert response["resume"]["rejected_point_count"] == 0
        assert manifest["responses"][0]["resume"] == response["resume"]
    else:
        assert "resume" not in response


@pytest.mark.parametrize("as_json", [False, True])
def test_cli_close_failure_uses_final_run_status(tmp_path, capsys, as_json):
    plan_path = write_plan(tmp_path, '[[steps]]\nkind = "scope.capture"\n')
    service = RunService(config=make_config(tmp_path), logger=CommandLogger())
    capture = fake_capture(tmp_path, "good")
    with patch("wavebench.services.run_service.ScopeService") as scope_cls, patch(
        "wavebench.cli._load_run_service", return_value=service
    ):
        scope = scope_cls.return_value
        scope.capture_waveform.return_value = capture
        scope.open_session.return_value.close.side_effect = RuntimeError("fake close failed")
        args = ["run", "plan", "--plan", str(plan_path)] + (["--json"] if as_json else [])
        code = main(args)

    assert code == 2
    scope.open_session.return_value.close.assert_called_once_with()
    [run_path] = (tmp_path / "data/runs").glob("*/run.json")
    run = json.loads(run_path.read_text(encoding="utf-8"))
    assert run["status"] == "failed"
    assert run["error"]["code"] == "session_close_failed"
    assert all(step["status"] == "ok" for step in run["steps"])
    output = capsys.readouterr().out
    if as_json:
        payload = json.loads(output)
        assert payload["status"] == "failed"
        assert payload["exit_code"] == 2
        assert payload["result"]["run_json"] == str(run_path)
    else:
        assert f"run_json={run_path}" in output
        assert f"summary={run_path.parent / 'summary.csv'}" in output


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"), "nan", "inf", "-inf"])
@pytest.mark.parametrize("limits", [{"min": 0}, {"max": 10}, {"min": 0, "max": 10}])
def test_expect_rejects_nonfinite_values_with_structured_reason(value, limits):
    result = evaluate_expect({"metric": value}, {"metric": limits})
    assert result["status"] == "failed"
    check = result["checks"]["metric"]
    assert check["status"] == "failed"
    assert check["reason"] == "not_finite"
    assert check["limits"] == limits
    assert result["failures"] == ["metric: not finite"]


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
@pytest.mark.parametrize("expect_fft", [False, True])
def test_nonfinite_expect_fails_run_and_stops_following_capture(tmp_path, value, expect_fft):
    field = "expect_fft" if expect_fft else "expect"
    metric = "peak_frequency_hz" if expect_fft else "voltage_vpp_v"
    plan = load_run_plan(write_plan(tmp_path, f"""
[[steps]]
kind = "scope.capture"
[steps.{field}]
{metric} = {{ min = 0.0 }}

[[steps]]
kind = "scope.capture"
label = "later"
"""))
    capture = fake_capture(tmp_path, "nonfinite", voltage_vpp_v=value)
    with patch("wavebench.services.run_service.ScopeService") as scope_cls, patch(
        "wavebench.services.run_service.capture_fft_summary",
        return_value={"status": "ok", "peak_frequency_hz": value},
    ):
        scope_cls.return_value.capture_waveform.return_value = capture
        result = RunService(config=make_config(tmp_path), logger=CommandLogger()).run(plan)

    scope_cls.return_value.capture_waveform.assert_called_once()
    assert len(result.steps) == 1
    assert result.status == result.steps[0].status == "failed"
    assert result.steps[0].artifact[field]["checks"][metric]["reason"] == "not_finite"
    run = json.loads(result.run_json_path.read_text(encoding="utf-8"))
    assert run["status"] == "failed"
    assert run["error"]["code"] == "step_failed"
