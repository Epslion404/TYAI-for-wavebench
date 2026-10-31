from __future__ import annotations

import csv
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from wavebench.errors import DataError
from wavebench.logging import CommandLogger
from wavebench.services.frequency_response import FrequencyResponsePoint
from wavebench.services.frequency_response_evidence import case_id, plan_digest
from wavebench.services.run_plan import load_run_plan
from wavebench.services.run_service import RunService

from test_run_service import make_config, write_plan


@pytest.mark.parametrize("safety_gate", [False, True])
def test_sweep_stop_condition_keeps_structured_failure_without_resume(
    tmp_path: Path, safety_gate: bool
) -> None:
    safety_line = "safety_gate = true\n" if safety_gate else ""
    plan_path = write_plan(
        str(tmp_path),
        f"""
[experiment]
name = "stop-no-resume"
label = "stop-no-resume"

[[steps]]
kind = "sweep.frequency_response"
label = "dut"
source_channel = 1
reference_channel = 1
response_channel = 2
frequencies_hz = [100, 1000]
settle_s = 0
{safety_line}
[steps.stop_conditions]
max_failed_points = 1
""",
    )
    plan = load_run_plan(plan_path)
    status = SimpleNamespace(output="ON")
    with patch("wavebench.services.run_service.ScopeService") as scope_cls, patch(
        "wavebench.services.run_service.SourceService"
    ) as source_cls:
        source = source_cls.return_value
        source.status.return_value = status
        source.set_frequency.return_value = status
        source.set_output.return_value = SimpleNamespace(output="OFF")
        scope = scope_cls.return_value
        scope.capture_waveforms.side_effect = DataError("fake capture failed")

        result = RunService(config=make_config(str(tmp_path)), logger=CommandLogger()).run(plan)

    assert len(result.steps) == 1
    step = result.steps[0]
    assert step.status == "failed"
    response = step.artifact["frequency_response"]
    assert response["stop_reason"] and "max_failed_points" in response["stop_reason"]
    assert response["point_count"] == 1
    assert response["failed_point_count"] == 1
    assert "resume" not in response
    assert scope.capture_waveforms.call_count == 1


def test_sweep_stop_condition_keeps_resume_summary(tmp_path: Path) -> None:
    plan_path = write_plan(
        str(tmp_path),
        """
[experiment]
name = "stop-with-resume"
label = "stop-with-resume"

[[steps]]
kind = "sweep.frequency_response"
label = "dut"
source_channel = 1
reference_channel = 1
response_channel = 2
frequencies_hz = [100, 1000]
settle_s = 0
resume_from = "old.csv"

[steps.stop_conditions]
max_failed_points = 1
""",
    )
    plan = load_run_plan(plan_path)
    step = plan.steps[0]
    old_point = FrequencyResponsePoint(
        index=0,
        requested_frequency_hz=100,
        reference_frequency_hz=100,
        response_frequency_hz=100,
        reference_amplitude_peak_v=1.0,
        response_amplitude_peak_v=2.0,
        reference_vpp_v=2.0,
        response_vpp_v=4.0,
        gain_linear=2.0,
        gain_db=6.020599913,
        phase_wrapped_deg=0.0,
        phase_unwrapped_deg=0.0,
        status="ok",
        case_id=case_id(
            plan_name=plan.name,
            step_index=step.index,
            label="dut",
            frequency_hz=100,
            requested_vpp=None,
            reference_channel=1,
            response_channel=2,
        ),
        plan_hash=plan_digest(plan),
    )
    old_csv = plan_path.parent / "old.csv"
    with old_csv.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=list(old_point.as_csv_row()))
        writer.writeheader()
        writer.writerow(old_point.as_csv_row())

    status = SimpleNamespace(output="ON")
    with patch("wavebench.services.run_service.ScopeService") as scope_cls, patch(
        "wavebench.services.run_service.SourceService"
    ) as source_cls:
        source = source_cls.return_value
        source.status.return_value = status
        source.set_frequency.return_value = status
        scope = scope_cls.return_value
        scope.capture_waveforms.side_effect = DataError("fake capture failed")

        result = RunService(config=make_config(str(tmp_path)), logger=CommandLogger()).run(plan)

    assert len(result.steps) == 1
    step_record = result.steps[0]
    assert step_record.status == "failed"
    response = step_record.artifact["frequency_response"]
    assert response["stop_reason"] and "max_failed_points" in response["stop_reason"]
    assert response["point_count"] == 2
    assert response["failed_point_count"] == 1
    assert response["resume"]["reused_point_count"] == 1
    assert response["resume"]["rejected_point_count"] == 0
    assert scope.capture_waveforms.call_count == 1
