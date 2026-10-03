"""Offline safety-gate evidence for parsed multichannel Source V2 steps."""

import json
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

import pytest

from test_run_service import make_config
from wavebench.errors import ConfigError
from wavebench.logging import CommandLogger
from wavebench.services.run_artifacts import RunStepRecord
from wavebench.services.run_plan import load_run_plan
from wavebench.services.run_service import RunInstrumentServices, RunService


RELATIONS = ["combine", "coupling", "tracking", "phase_relation"]


def load_plan(tmp_path, text):
    path = tmp_path / "plan.toml"
    path.write_text(text, encoding="utf-8")
    return load_run_plan(path)


@pytest.mark.parametrize("relation", RELATIONS)
@pytest.mark.parametrize("failed_channel", [None, 1, 2])
def test_failed_relation_attempts_every_inferred_off_target(tmp_path, relation, failed_channel):
    plan = load_plan(tmp_path, f'''
[[steps]]
kind = "source.{relation}_configure_v2"
channels = [1, 2]
enabled = true
safety_gate = true

[[steps]]
kind = "sleep"
duration_s = 0.01
''')
    service = RunService(config=make_config(tmp_path), logger=CommandLogger())
    assert plan.steps[0].fields["channels"] == (1, 2)
    assert service._safety_gate_for_step(plan, plan.steps[0])["source_channels"] == [1, 2]
    source = Mock()

    def off(*, channel, enabled):
        assert enabled is False
        if channel == failed_channel:
            raise ConfigError(f"OFF failed for {channel}")
        return SimpleNamespace(output="OFF")

    source.set_output.side_effect = off
    services = RunInstrumentServices(source=source)
    failed = RunStepRecord(index=0, kind=plan.steps[0].kind, status="failed",
                           fields=plan.steps[0].fields, artifact={})
    with patch.object(service, "check"), patch.object(
        service, "_run_instrument_services", return_value=nullcontext(services)
    ), patch.object(service, "_run_step", return_value=failed) as run_step:
        result = service.run(plan)

    assert run_step.call_count == 1
    assert source.set_output.call_args_list == [
        call(channel=1, enabled=False), call(channel=2, enabled=False)
    ]
    run_data = json.loads(result.run_json_path.read_text(encoding="utf-8"))
    assert run_data["status"] == result.status == "failed"
    assert run_data["error"]["code"] == "safety_gate_failed"
    gate = run_data["steps"][0]["artifact"]["safety_gate"]
    assert gate["source_channels"] == [1, 2]
    assert gate["status"] == ("failed" if failed_channel else "ok")
    assert [action["channel"] for action in gate["actions"]] == [
        channel for channel in [1, 2] if channel != failed_channel
    ]
    if failed_channel:
        assert [error["channel"] for error in gate["errors"]] == [failed_channel]


def test_inference_unions_relation_scalar_and_restore_channels_without_duplicates(tmp_path):
    plan = load_plan(tmp_path, '''
[restore]
source_state = true
source_channels = [1, 4]

[[steps]]
kind = "source.coupling_configure_v2"
channels = [1, 2]
enabled = true
safety_gate = true

[[steps]]
kind = "source.set_freq"
channel = 2
frequency_hz = 1000

[[steps]]
kind = "source.output"
channel = 3
state = "on"
''')
    service = RunService(config=make_config(tmp_path), logger=CommandLogger())
    assert service._safety_gate_for_step(plan, plan.steps[0])["source_channels"] == [1, 2, 3, 4]


@pytest.mark.parametrize("global_gate", [False, True])
def test_explicit_off_targets_keep_authorization_boundary(tmp_path, global_gate):
    global_fields = '[safety]\nsafety_gate = true\noff_source_channels = [3]\n' if global_gate else ''
    local_gate = 'true' if global_gate else '{source_channels = [3]}'
    plan = load_plan(tmp_path, f'''
{global_fields}
[[steps]]
kind = "source.coupling_configure_v2"
channels = [1, 2]
enabled = true
safety_gate = {local_gate}
''')
    service = RunService(config=make_config(tmp_path), logger=CommandLogger())
    assert service._safety_gate_for_step(plan, plan.steps[0]) == {
        "enabled": True, "source_channels": [3], "power_channels": []
    }
