"""Public CLI contract for the offline ``advisor ask`` entry point."""

from __future__ import annotations

import json

from wavebench.cli import main


def test_advisor_ask_preview_is_offline_and_does_not_write_an_artifact(tmp_path, capsys):
    state_path = tmp_path / "state.json"
    state_path.write_text(
        json.dumps(
            {
                "fields": {"run_status": "partial", "cycles_bucket": "few_cycles"},
                "untrusted": [
                    {
                        "source": "operator.utterance",
                        "text": "inspect the offline capture package",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = main(
        [
            "advisor",
            "ask",
            "--advisor",
            "builtin.rule_advisor",
            "--question",
            "route",
            "--state",
            str(state_path),
            "--preview",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert result == 0
    assert payload["status"] == "preview_only"
    assert payload["artifact_path"] is None
    assert payload["preview"]["untrusted_sources"] == ["operator.utterance"]
    assert payload["preview"]["payload"]["state"]["untrusted"][0]["text"] == (
        "[REDACTED_UNTRUSTED_TEXT]"
    )


def test_advisor_ask_uses_the_local_rule_baseline_without_writing_an_artifact(tmp_path, capsys):
    state_path = tmp_path / "state.json"
    state_path.write_text(
        json.dumps(
            {
                "fields": {"run_status": "partial", "cycles_bucket": "few_cycles"},
                "untrusted": [
                    {
                        "source": "operator.utterance",
                        "text": "inspect the offline capture package",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = main(
        [
            "advisor",
            "ask",
            "--advisor",
            "builtin.rule_advisor",
            "--question",
            "route",
            "--state",
            str(state_path),
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert result == 0
    assert payload["status"] == "ok"
    assert payload["artifact_path"] is None
    assert payload["recommendations"][0]["action"] == "capture.inspect"
    assert payload["recommendations"][0]["command"].startswith("python -m wavebench capture inspect")
