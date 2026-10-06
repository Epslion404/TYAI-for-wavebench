"""PR integration checks using real run lifecycle, file writers, and resource leases."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from wavebench.cli import main
from wavebench.config import QualityConfig
from wavebench.logging import CommandLogger
from wavebench.services import run_service
from wavebench.services.resource_lease import ResourceLeaseManager
from wavebench.services.run_artifacts import RunStepRecord
from wavebench.services.run_plan import load_run_plan
from wavebench.services.run_service import RunResult, RunService

from test_run_service import fake_capture, make_config, write_plan


class _Session:
    def __init__(self, lifecycle, instrument, lease):
        self.lifecycle = lifecycle
        self.instrument = instrument
        self.lease = lease
        self.close_calls = 0
        self.errors_before_close = None

    def close(self):
        lifecycle = self.lifecycle
        result = lifecycle.results[0]
        # Construction and successful step persistence precede ExitStack cleanup.
        assert self.lease.acquired
        assert result.run_json_path.is_file()
        assert result.summary_csv_path.is_file()
        for record in result.steps:
            path = result.run_dir / "steps" / (
                f"{record.index:02d}_{record.kind.replace('.', '_')}.json"
            )
            assert json.loads(path.read_text(encoding="utf-8")) == record.as_dict()
        self.errors_before_close = [item["operation"] for item in result.close_errors]
        self.close_calls += 1
        lifecycle.events.append(f"close.{self.instrument}")
        if self.instrument in lifecycle.fail_close:
            # The production close callback, rather than the fake, appends the error.
            assert lifecycle.read_run()["status"] == "ok"
            raise RuntimeError(f"deliberate {self.instrument} close failure")


class _FakeService:
    def __init__(
        self, lifecycle, *, config, logger, session=None, descriptor=None,
        transport=None, session_state=None, lease=None,
    ):
        self.lifecycle = lifecycle
        self.config = config
        self.logger = logger
        self.session = session
        self.descriptor = descriptor
        self.transport = transport
        self.session_state = session_state
        self.lease = lease

    def open_session(self):
        assert self.lease.acquired
        self.lifecycle.events.append(f"open.{self.instrument}")
        self.session = _Session(self.lifecycle, self.instrument, self.lease)
        self.lifecycle.sessions[self.instrument] = self.session
        return self.session


class _Scope(_FakeService):
    instrument = "scope"

    def require_high_impedance(self, channel, *, allow_50ohm):
        assert channel == 1
        assert allow_50ohm is False
        self.lifecycle.events.append("guard.high_impedance")

    def capture_waveform(self, *, channel, label):
        assert channel == 1
        assert self.session is self.lifecycle.sessions["scope"]
        assert self.lease.acquired
        self.lifecycle.events.append("capture")
        return next(self.lifecycle.captures)

    def autoscale(self):
        self.lifecycle.events.append("autoscale")


class _Dmm(_FakeService):
    instrument = "dmm"

    def read(self, *, function):
        assert function == "acv"
        assert self.session is self.lifecycle.sessions["dmm"]
        assert self.lease.acquired
        self.lifecycle.events.append("read.dmm")
        return SimpleNamespace(as_dict=lambda: {"function": "acv", "value": 0.35, "unit": "V"})


class _Lifecycle:
    def __init__(self, root: Path, monkeypatch):
        self.root = root
        self.config = make_config(str(root))
        self.manager = ResourceLeaseManager(root / "locks")
        self.events = []
        self.results = []
        self.sessions = {}
        self.fail_close = ()
        self.captures = iter([fake_capture(str(root), "capture")])
        self.original_write = run_service.write_run_files
        monkeypatch.setattr(run_service, "ScopeService", lambda **kw: _Scope(self, **kw))
        monkeypatch.setattr(run_service, "DmmService", lambda **kw: _Dmm(self, **kw))
        monkeypatch.setattr(run_service, "RunResult", self.observe_result)
        monkeypatch.setattr(run_service, "write_run_files", self.observe_write)

    def observe_result(self, **kwargs):
        result = RunResult(**kwargs)
        assert result.close_errors == []
        self.results.append(result)
        self.events.append("result.constructed")
        return result

    def observe_write(self, **kwargs):
        if kwargs["error"] and kwargs["error"].get("code") == "session_close_failed":
            # This write comes from the real close reporter after all cleanup callbacks.
            assert all(session.close_calls == 1 for session in self.sessions.values())
            assert all(not session.lease.acquired for session in self.sessions.values())
            assert len(self.results[0].close_errors) == len(self.fail_close)
        self.events.append(f"write_run.{kwargs['status']}")
        return self.original_write(**kwargs)

    def service(self):
        return RunService(
            config=self.config, logger=CommandLogger(), lease_manager=self.manager,
        )

    def read_run(self):
        return json.loads(self.results[0].run_json_path.read_text(encoding="utf-8"))

    def assert_leases_released(self):
        resources = [self.config.connection.resource]
        if "dmm" in self.sessions:
            resources.append(self.config.dmm.resource)
        leases = self.manager.acquire_many(resources)
        try:
            assert len(leases) == len(resources)
            assert all(lease.acquired for lease in leases)
        finally:
            for lease in leases:
                lease.release()


@pytest.fixture
def lifecycle(tmp_path, monkeypatch):
    return _Lifecycle(tmp_path, monkeypatch)


def _capture_plan(lifecycle, *, analysis=False, dmm=False, extra=""):
    content = f'''
[[steps]]
id = "capture_main"
kind = "scope.capture"
save_npy = true
{extra}
'''
    if dmm:
        content += '\n[[steps]]\nkind = "dmm.read"\nfunction = "acv"\n'
    if analysis:
        content += '''
[[steps]]
kind = "analysis.pipeline"
source = { step = "capture_main" }
operations = [{ op = "measure", metrics = ["voltage_mean_v"] }]
'''
    return write_plan(str(lifecycle.root), content)


@pytest.mark.parametrize("json_mode", [False, True], ids=["plain", "json"])
@pytest.mark.parametrize("analysis", [False, True], ids=["hardware", "analysis_suffix"])
@pytest.mark.parametrize("fail_close", [("dmm",), ("dmm", "scope")], ids=["one", "both"])
def test_close_failure_flows_through_real_cleanup_to_cli(
    lifecycle, monkeypatch, capsys, json_mode, analysis, fail_close,
):
    lifecycle.fail_close = fail_close
    plan_path = _capture_plan(lifecycle, analysis=analysis, dmm=True)
    monkeypatch.setattr("wavebench.cli._load_run_service", lambda args: lifecycle.service())

    def unexpected_analysis(**kwargs):
        pytest.fail("analysis must be skipped after session close failure")

    monkeypatch.setattr(run_service, "execute_analysis_pipeline", unexpected_analysis)
    args = ["run", "plan", "--plan", str(plan_path)] + (["--json"] if json_mode else [])
    assert main(args) == 2
    output = capsys.readouterr().out
    [result] = lifecycle.results
    operations = [f"session.close.{name}" for name in fail_close]
    assert [item["operation"] for item in result.close_errors] == operations
    assert all(item["type"] == "RuntimeError" for item in result.close_errors)
    assert lifecycle.sessions["dmm"].errors_before_close == []
    assert lifecycle.sessions["scope"].errors_before_close == ["session.close.dmm"]
    assert lifecycle.events == [
        "open.scope", "open.dmm", "guard.high_impedance", "capture", "read.dmm",
        "write_run.ok", "result.constructed", "close.dmm", "close.scope", "write_run.failed",
    ]
    assert [record.status for record in result.steps] == ["ok", "ok"]
    run = lifecycle.read_run()
    assert run["status"] == "failed"
    assert run["error"]["code"] == "session_close_failed"
    assert run["error"]["errors"] == result.close_errors
    assert run["provenance"]["session_lifecycle"]["close_errors"] == result.close_errors
    assert run["steps"] == [record.as_dict() for record in result.steps]
    lifecycle.assert_leases_released()
    if json_mode:
        envelope = json.loads(output)
        assert envelope["status"] == "failed"
        assert envelope["exit_code"] == 2
        assert envelope["result"]["close_errors"] == result.close_errors
        assert envelope["result"]["steps"] == run["steps"]
        assert envelope["result"]["run_json"] == str(result.run_json_path)
        assert envelope["result"]["summary"] == str(result.summary_csv_path)
    else:
        assert f"session_close_errors={len(fail_close)}" in output
        assert f"run_json={result.run_json_path}" in output
        assert f"summary={result.summary_csv_path}" in output
        assert "steps=2" in output


@pytest.mark.parametrize("json_mode", [False, True], ids=["plain", "json"])
def test_successful_real_cleanup_keeps_cli_success(lifecycle, monkeypatch, capsys, json_mode):
    plan_path = _capture_plan(lifecycle, dmm=True)
    monkeypatch.setattr("wavebench.cli._load_run_service", lambda args: lifecycle.service())
    args = ["run", "plan", "--plan", str(plan_path)] + (["--json"] if json_mode else [])
    assert main(args) == 0
    output = capsys.readouterr().out
    [result] = lifecycle.results
    assert result.close_errors == []
    assert lifecycle.read_run()["status"] == "ok"
    assert "session_lifecycle" not in lifecycle.read_run()["provenance"]
    assert all(session.close_calls == 1 for session in lifecycle.sessions.values())
    assert lifecycle.events[-2:] == ["close.dmm", "close.scope"]
    lifecycle.assert_leases_released()
    if json_mode:
        envelope = json.loads(output)
        assert envelope["status"] == "ok"
        assert envelope["exit_code"] == 0
        assert envelope["result"]["close_errors"] == []
    else:
        assert "session_close_errors=" not in output


@pytest.mark.parametrize(
    "metrics, clean, consistency, trusted, expect_failure",
    [
        ([(1000, 5, 0), (2000, 5, 0)], True, "diverged", False, False),
        ([(None, None, None)] * 2, True, "no_comparable_metrics", False, False),
        ([(1000, 5, 0), (2000, 5, 0)], False, "diverged", False, False),
        ([(None, None, None)] * 2, False, "no_comparable_metrics", False, False),
        ([(1000, 5, 0)] * 2, False, "consistent", True, False),
        ([(None, 5, 0)] * 2, False, "consistent", True, False),
        ([(1000, 5, 0)] * 2, False, "consistent", True, True),
    ],
    ids=[
        "clean_diverged", "clean_no_metrics", "warning_diverged", "warning_no_metrics",
        "trusted", "trusted_partial_metrics", "trusted_expectation_failure",
    ],
)
def test_final_quality_decision_survives_real_lifecycle(
    lifecycle, metrics, clean, consistency, trusted, expect_failure,
):
    lifecycle.config = make_config(
        str(lifecycle.root),
        QualityConfig(auto_recover_attempts=1, consistency_required_captures=2),
    )
    warnings = ["low_points_per_cycle: too sparse"]
    captures = [
        fake_capture(
            str(lifecycle.root), f"quality_{index}",
            [] if clean and index == 1 else warnings,
            frequency_hz=frequency, voltage_vpp_v=vpp, voltage_mean_v=mean,
        )
        for index, (frequency, vpp, mean) in enumerate(metrics)
    ]
    lifecycle.captures = iter(captures)
    extra = "quality_gate = true\nauto_recover = true\n"
    if expect_failure:
        extra += "expect = { voltage_vpp_v = { max = 1.0 } }\n"
    plan = load_run_plan(_capture_plan(lifecycle, extra=extra))
    result = lifecycle.service().run(plan)
    [record] = result.steps
    artifact = record.artifact
    gate_status = "ok" if clean or trusted else "failed"
    step_status = "failed" if expect_failure else gate_status
    assert record.status == step_status
    assert artifact["quality_gate"]["status"] == gate_status
    assert artifact["quality_gate"]["warnings"] == ([] if clean else warnings)
    assert artifact["quality_gate"]["consistency_status"] == consistency
    assert artifact["quality"].get("trusted_by_consistency", False) is trusted
    assert artifact["quality_gate"].get("trusted_by_consistency", False) is trusted
    recovery = artifact["quality_recovery"]
    assert recovery["consistency"]["status"] == consistency
    assert [attempt["index"] for attempt in recovery["attempts"]] == [0, 1]
    assert [attempt["package"] for attempt in recovery["attempts"]] == [
        str(capture.package_dir) for capture in captures
    ]
    assert recovery["attempts"][0]["quality"]["warnings"] == warnings
    assert recovery["attempts"][1]["quality"]["status"] == ("ok" if clean else "warning")
    assert artifact["package"] == str(captures[-1].package_dir)
    if expect_failure:
        assert artifact["expect"]["status"] == "failed"
    run = lifecycle.read_run()
    assert run["status"] == step_status
    assert run["steps"] == [record.as_dict()]
    assert result.close_errors == []
    assert lifecycle.events.count("capture") == 2
    assert lifecycle.events.count("autoscale") == 1
    assert lifecycle.sessions["scope"].close_calls == 1
    lifecycle.assert_leases_released()


def test_legacy_four_argument_result_failed_step_keeps_json_failure(tmp_path, monkeypatch, capsys):
    record = RunStepRecord(0, "scope.capture", "failed", {}, {})
    result = RunResult(tmp_path, tmp_path / "run.json", tmp_path / "summary.csv", [record])
    assert result.close_errors == []
    plan_path = _capture_plan(SimpleNamespace(root=tmp_path))
    monkeypatch.setattr(
        "wavebench.cli._load_run_service", lambda args: SimpleNamespace(run=lambda plan: result),
    )
    assert main(["run", "plan", "--plan", str(plan_path), "--json"]) == 2
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["status"] == "failed"
    assert envelope["exit_code"] == 2
    assert envelope["result"]["close_errors"] == []
    assert envelope["result"]["steps"][0]["status"] == "failed"
