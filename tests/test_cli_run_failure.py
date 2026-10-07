from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from wavebench.cli import main
from wavebench.services.run_artifacts import RunStepRecord
from wavebench.services.run_service import RunResult


def _result(tmp: str, status: str, close_errors: list | None = None) -> RunResult:
    root = Path(tmp)
    record = RunStepRecord(
        index=0,
        kind="scope.capture",
        status=status,
        fields={},
        artifact={},
    )
    return RunResult(
        run_dir=root / "run",
        run_json_path=root / "run" / "run.json",
        summary_csv_path=root / "run" / "summary.csv",
        steps=[record],
        close_errors=close_errors or [],
    )


def test_run_plan_returns_nonzero_for_failed_result_and_keeps_paths(capsys) -> None:
    with TemporaryDirectory() as tmp:
        plan_path = Path(tmp) / "plan.toml"
        plan_path.write_text("", encoding="utf-8")
        result = _result(tmp, "failed")
        service = SimpleNamespace(run=lambda _plan: result)
        with patch("wavebench.cli.load_run_plan", return_value=object()), patch(
            "wavebench.cli._load_run_service", return_value=service
        ):
            code = main(["run", "plan", "--plan", str(plan_path)])

        assert code == 2
        output = capsys.readouterr().out
        assert f"run_json={result.run_json_path}" in output
        assert f"summary={result.summary_csv_path}" in output


def test_run_plan_returns_nonzero_for_session_close_failure(capsys) -> None:
    with TemporaryDirectory() as tmp:
        plan_path = Path(tmp) / "plan.toml"
        plan_path.write_text("", encoding="utf-8")
        result = _result(
            tmp,
            "ok",
            close_errors=[{"operation": "session.close.scope", "type": "RuntimeError"}],
        )
        # 步骤全 ok 但会话关闭失败时，run.json 已记 failed，CLI 同样非零退出。
        service = SimpleNamespace(run=lambda _plan: result)
        with patch("wavebench.cli.load_run_plan", return_value=object()), patch(
            "wavebench.cli._load_run_service", return_value=service
        ):
            code = main(["run", "plan", "--plan", str(plan_path)])

        assert code == 2
        output = capsys.readouterr().out
        assert "session_close_errors=1" in output
        # 成功步骤语义不被改写为失败。
        assert result.steps[0].status == "ok"


def test_run_plan_json_status_failed_for_session_close_failure(capsys) -> None:
    import json as _json

    with TemporaryDirectory() as tmp:
        plan_path = Path(tmp) / "plan.toml"
        plan_path.write_text("", encoding="utf-8")
        result = _result(
            tmp,
            "ok",
            close_errors=[{"operation": "session.close.scope", "type": "RuntimeError"}],
        )
        service = SimpleNamespace(run=lambda _plan: result)
        with patch("wavebench.cli.load_run_plan", return_value=object()), patch(
            "wavebench.cli._load_run_service", return_value=service
        ):
            code = main(["run", "plan", "--plan", str(plan_path), "--json"])

        assert code == 2
        envelope = _json.loads(capsys.readouterr().out)
        assert envelope["status"] == "failed"
        assert envelope["exit_code"] == 2
        assert envelope["result"]["close_errors"][0]["operation"] == "session.close.scope"


def test_run_plan_keeps_zero_for_ok_result() -> None:
    with TemporaryDirectory() as tmp:
        plan_path = Path(tmp) / "plan.toml"
        plan_path.write_text("", encoding="utf-8")
        result = _result(tmp, "ok")
        service = SimpleNamespace(run=lambda _plan: result)
        with patch("wavebench.cli.load_run_plan", return_value=object()), patch(
            "wavebench.cli._load_run_service", return_value=service
        ):
            assert main(["run", "plan", "--plan", str(plan_path)]) == 0
