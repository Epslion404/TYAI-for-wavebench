"""Retain sweep errors and authorization while executing failure OFF actions."""

import json
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from test_run_service import make_config
from wavebench.errors import ConfigError
from wavebench.logging import CommandLogger
from wavebench.services.run_plan import load_run_plan
from wavebench.services.run_service import RunInstrumentServices, RunService


@pytest.mark.parametrize("gate_enabled", [False, True])
@pytest.mark.parametrize("failure_type", [ConfigError, RuntimeError])
@pytest.mark.parametrize("off_fails", [False, True])
def test_sweep_error_semantics_and_all_authorized_off_attempts(
    tmp_path, gate_enabled, failure_type, off_fails
):
    path = tmp_path / "plan.toml"
    safety = '''[safety]
safety_gate = true
off_source_channels = [1]
off_power_channels = [2]
''' if gate_enabled else ''
    path.write_text(safety + '''
[[steps]]
kind = "sweep.frequency_response"
source_channel = 1
reference_channel = 1
response_channel = 2
frequencies_hz = [100, 1000]
settle_s = 0
''', encoding="utf-8")
    plan = load_run_plan(path)
    service = RunService(config=make_config(tmp_path), logger=CommandLogger())
    source, power, scope = Mock(), Mock(), Mock()
    source.status.return_value = SimpleNamespace(output="ON")
    source.set_frequency.side_effect = failure_type("source rejected frequency")
    source.set_output.return_value = power.set_output.return_value = SimpleNamespace(output="OFF")
    if off_fails:
        source.set_output.side_effect = ConfigError("source OFF failed")
    services = RunInstrumentServices(source=source, power=power, scope=scope)
    with patch.object(service, "check"), patch.object(
        service, "_run_instrument_services", return_value=nullcontext(services)
    ):
        if gate_enabled and failure_type is ConfigError:
            result = service.run(plan)
            source.set_output.assert_called_once_with(channel=1, enabled=False)
            power.set_output.assert_called_once_with(channel=2, enabled=False)
            run_data = json.loads(result.run_json_path.read_text(encoding="utf-8"))
            gate = run_data["steps"][0]["artifact"]["safety_gate"]
            assert gate["status"] == ("failed" if off_fails else "ok")
            assert run_data["steps"][0]["artifact"]["error"]["type"] == "ConfigError"
            assert run_data["error"]["code"] == "safety_gate_failed"
        else:
            with pytest.raises(failure_type, match="source rejected frequency"):
                service.run(plan)
            source.set_output.assert_not_called()
            power.set_output.assert_not_called()
            run_json = next((tmp_path / "data" / "runs").glob("*/run.json"))
            run_data = json.loads(run_json.read_text(encoding="utf-8"))
            assert run_data["error"]["type"] == failure_type.__name__
    assert run_data["status"] == "failed"
    assert len(run_data["steps"]) == 1
    assert run_data["steps"][0]["artifact"]["frequency_response"]
    scope.capture_waveforms.assert_not_called()
