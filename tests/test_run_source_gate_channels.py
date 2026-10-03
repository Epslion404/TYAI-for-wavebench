from contextlib import nullcontext
import json
from unittest.mock import Mock, call, patch
from types import SimpleNamespace

import pytest

from test_run_failure_policy import _record, _service, _write_plan
from wavebench.errors import ConfigError
from wavebench.services.run_plan import load_run_plan
from wavebench.services.run_service import RunInstrumentServices


RELATIONS = ("combine", "coupling", "tracking", "phase_relation")


def test_implicit_restore_channel_is_gated_before_and_after_restore(tmp_path):
    from wavebench.services.source_state import RestorableSourceState

    expected = [1, 2]
    plan = load_run_plan(_write_plan(tmp_path, '''
[restore]
source_state = true

[[steps]]
kind = "source.set_freq"
channel = 1
frequency_hz = 1000
safety_gate = true
'''))
    service = _service(tmp_path)
    source = Mock()
    source.snapshot_restorable_state.return_value = RestorableSourceState(
        2, "ON", "SIN", 1000., 1., "VPP"
    )
    source.set_output.return_value = SimpleNamespace(output="OFF")
    source.restore_restorable_state.return_value = SimpleNamespace(output="OFF")
    services = RunInstrumentServices(source=source)
    with patch.object(service, "check"), patch.object(
        service, "_run_instrument_services", return_value=nullcontext(services)
    ), patch.object(service, "_run_step", return_value=_record(0, plan.steps[0].kind, "failed")):
        result = service.run(plan)
    source.snapshot_restorable_state.assert_called_once_with(channel=None)
    restored = source.restore_restorable_state.call_args.args[0]
    assert restored.channel == 2
    assert restored.output == "OFF"
    assert source.set_output.call_args_list == [
        call(channel=channel, enabled=False) for channel in expected
    ] * 2
    data = json.loads(result.run_json_path.read_text(encoding="utf-8"))
    gate = data["error"]["safety_gate"]
    assert gate["source_channels"] == expected
    assert gate["post_restore"]["source_channels"] == expected


@pytest.mark.parametrize("restore, policy, expected", [
    ('source_state = true', 'true', [1, 3, 2]),
    ('source_state = false', 'true', [1, 3]),
    ('source_state = true\nsource_channels = [4]', 'true', [1, 3, 4]),
    ('source_state = true', '{ source_channels = [1] }', [1]),
])
def test_implicit_restore_does_not_expand_disabled_or_explicit_targets(
    tmp_path, restore, policy, expected
):
    plan = load_run_plan(_write_plan(tmp_path, f'''
[restore]
{restore}

[[steps]]
kind = "source.coupling_configure_v2"
channels = [1, 3]
enabled = true
safety_gate = {policy}
'''))
    assert _service(tmp_path)._safety_gate_for_step(plan, plan.steps[0])["source_channels"] == expected


@pytest.mark.parametrize("relation", RELATIONS)
def test_mixed_default_output_and_relation_gate_closes_actual_default(tmp_path, relation):
    plan = load_run_plan(_write_plan(tmp_path, f'''
[[steps]]
kind = "source.output"
state = "on"

[[steps]]
kind = "source.{relation}_configure_v2"
channels = [1, 3]
enabled = true
safety_gate = true
'''))
    service = _service(tmp_path)
    source = Mock()
    source.set_output.return_value = SimpleNamespace(output="OFF")
    services = RunInstrumentServices(source=source)
    original = service._run_step

    def execute(plan, step, **kwargs):
        if step.index == 0:
            return original(plan, step, **kwargs)
        return _record(step.index, step.kind, "failed")

    with patch.object(service, "check"), patch.object(
        service, "_run_instrument_services", return_value=nullcontext(services)
    ), patch.object(service, "_run_step", side_effect=execute):
        result = service.run(plan)
    assert service.config.source.default_channel == 2
    assert source.set_output.call_args_list == [
        call(channel=None, enabled=True),
        call(channel=1, enabled=False), call(channel=3, enabled=False),
        call(channel=2, enabled=False),
    ]
    gate = result.steps[1].artifact["safety_gate"]
    assert gate["source_channels"] == [1, 3, 2]
    assert [action["channel"] for action in gate["actions"]] == [1, 3, 2]


def test_relation_alone_does_not_add_unrelated_default_output(tmp_path):
    plan = load_run_plan(_write_plan(tmp_path, '''
[[steps]]
kind = "source.coupling_configure_v2"
channels = [1, 3]
enabled = true
safety_gate = true
'''))
    assert _service(tmp_path)._safety_gate_for_step(plan, plan.steps[0])["source_channels"] == [1, 3]


@pytest.mark.parametrize("other", [
    'kind = "source.set_freq"\nfrequency_hz = 1000',
    'kind = "sweep.frequency_response"\nreference_channel = 1\nresponse_channel = 2\n'
    'start_frequency_hz = 100\nstop_frequency_hz = 1000\nfrequency_count = 2',
])
def test_default_scalar_and_sweep_channels_union_with_relation(tmp_path, other):
    plan = load_run_plan(_write_plan(tmp_path, f'''
[[steps]]
kind = "source.coupling_configure_v2"
channels = [1, 3]
enabled = true
safety_gate = true

[[steps]]
{other}
'''))
    assert _service(tmp_path)._safety_gate_for_step(plan, plan.steps[0])["source_channels"] == [1, 3, 2]


@pytest.mark.parametrize("relation", RELATIONS)
@pytest.mark.parametrize("off_fails", [False, True])
def test_multichannel_failure_gate_attempts_every_declared_output(tmp_path, relation, off_fails):
    plan = load_run_plan(_write_plan(tmp_path, f'''
[[steps]]
kind = "source.{relation}_configure_v2"
channels = [1, 2]
enabled = true
safety_gate = true

[[steps]]
kind = "sleep"
duration_s = 0.01
'''))
    service = _service(tmp_path)
    source = Mock()
    source.set_output.side_effect = [
        ConfigError("OFF failed") if off_fails else SimpleNamespace(output="OFF"),
        SimpleNamespace(output="OFF"),
    ]
    services = RunInstrumentServices(source=source)
    assert plan.steps[0].fields["channels"] == (1, 2)
    assert service._safety_gate_for_step(plan, plan.steps[0])["source_channels"] == [1, 2]
    with patch.object(service, "check"), patch.object(
        service, "_run_instrument_services", return_value=nullcontext(services)
    ), patch.object(service, "_run_step", return_value=_record(0, plan.steps[0].kind, "failed")):
        result = service.run(plan)
    assert len(result.steps) == 1
    assert source.set_output.call_args_list == [
        call(channel=1, enabled=False), call(channel=2, enabled=False)
    ]
    run_data = json.loads(result.run_json_path.read_text(encoding="utf-8"))
    gate = run_data["steps"][0]["artifact"]["safety_gate"]
    assert run_data["status"] == "failed"
    assert run_data["error"]["code"] == "safety_gate_failed"
    assert run_data["error"]["safety_gate"] == gate
    assert gate["source_channels"] == [1, 2]
    assert gate["status"] == ("failed" if off_fails else "ok")
    assert [action["channel"] for action in gate["actions"]] == ([2] if off_fails else [1, 2])
    if off_fails:
        assert gate["errors"][0]["channel"] == 1
        assert gate["errors"][0]["message"] == "OFF failed"
    else:
        assert "errors" not in gate


def test_inference_unions_current_other_source_steps_and_restore(tmp_path):
    plan = load_run_plan(_write_plan(tmp_path, '''
[restore]
source_state = true
source_channels = [2, 4]

[[steps]]
kind = "source.set_freq"
channel = 2
frequency_hz = 1000
safety_gate = true

[[steps]]
kind = "source.coupling_configure_v2"
channels = [1, 2]
enabled = true

[[steps]]
kind = "sweep.frequency_response"
source_channel = 3
reference_channel = 1
response_channel = 2
start_frequency_hz = 100
stop_frequency_hz = 1000
frequency_count = 2
'''))
    service = _service(tmp_path)
    assert service._safety_gate_for_step(plan, plan.steps[0])["source_channels"] == [2, 1, 3, 4]
    assert service._inferred_source_gate_channels(plan, plan.steps[1]) == [1, 2, 3, 4]


def test_inference_does_not_treat_scope_channels_as_source_outputs(tmp_path):
    plan = load_run_plan(_write_plan(tmp_path, '''
[[steps]]
kind = "source.set_freq"
channel = 3
frequency_hz = 1000
safety_gate = true

[[steps]]
kind = "scope.capture"
channels = [1, 2]
synchronized = true
save_npy = true
points = "DEF"
'''))
    assert _service(tmp_path)._safety_gate_for_step(plan, plan.steps[0])["source_channels"] == [3]


@pytest.mark.parametrize("policy, expected", [
    ('safety_gate = { source_channels = [3] }', [3]),
    ('safety_gate = false', []),
])
def test_multichannel_step_keeps_explicit_and_disabled_gate_semantics(tmp_path, policy, expected):
    plan = load_run_plan(_write_plan(tmp_path, f'''
[[steps]]
kind = "source.coupling_configure_v2"
channels = [1, 2]
enabled = true
{policy}
'''))
    assert _service(tmp_path)._safety_gate_for_step(plan, plan.steps[0])["source_channels"] == expected


@pytest.mark.parametrize("targets, expected", [('off_source_channels = [3]', [3]), ('', [])])
def test_plan_gate_does_not_expand_explicit_targets_or_enable_local_inference(tmp_path, targets, expected):
    plan = load_run_plan(_write_plan(tmp_path, f'''
[safety]
safety_gate = true
{targets}

[[steps]]
kind = "source.coupling_configure_v2"
channels = [1, 2]
enabled = true
safety_gate = true
'''))
    assert _service(tmp_path)._safety_gate_for_step(plan, plan.steps[0])["source_channels"] == expected


@pytest.mark.parametrize("channel, expected", [('channel = 3', [3]), ('', [2])])
def test_single_channel_gate_keeps_declared_and_default_channel_behavior(tmp_path, channel, expected):
    plan = load_run_plan(_write_plan(tmp_path, f'''
[[steps]]
kind = "source.set_freq"
{channel}
frequency_hz = 1000
safety_gate = true
'''))
    assert _service(tmp_path)._safety_gate_for_step(plan, plan.steps[0])["source_channels"] == expected
