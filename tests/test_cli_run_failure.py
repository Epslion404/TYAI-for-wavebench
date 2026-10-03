from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from wavebench.cli import main
from wavebench.services.run_artifacts import RunStepRecord
from wavebench.services.run_service import RunResult


def _result(tmp: str, status: str) -> RunResult:
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
        status=status,
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


@pytest.mark.parametrize("as_json", [False, True])
def test_run_plan_legacy_result_with_failed_step_returns_nonzero(tmp_path, capsys, as_json):
    record = RunStepRecord(
        index=0,
        kind="scope.capture",
        status="failed",
        fields={},
        artifact={},
    )
    result = RunResult(
        run_dir=tmp_path / "run",
        run_json_path=tmp_path / "run" / "run.json",
        summary_csv_path=tmp_path / "run" / "summary.csv",
        steps=[record],
    )
    assert result.status == "ok"
    service = SimpleNamespace(run=lambda _plan: result)
    with patch("wavebench.cli.load_run_plan", return_value=object()), patch(
        "wavebench.cli._load_run_service", return_value=service
    ):
        args = ["run", "plan", "--plan", str(tmp_path / "plan.toml")]
        if as_json:
            args.append("--json")
        code = main(args)

    assert code == 2
    output = capsys.readouterr().out
    if as_json:
        payload = json.loads(output)
        assert payload["status"] == "failed"
        assert payload["exit_code"] == 2
        assert payload["result"]["steps"][0]["status"] == "failed"
        assert payload["result"]["run_json"] == str(result.run_json_path)
    else:
        assert f"run_json={result.run_json_path}" in output
        assert f"summary={result.summary_csv_path}" in output
