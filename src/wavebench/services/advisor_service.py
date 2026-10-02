"""Offline entry points for advisor requests.

The first public slice is deliberately preview-only: it resolves advisor metadata,
validates a local state fixture, and produces the Core-owned redacted preview without
loading a network client or writing a decision artifact.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any, Mapping

from wavebench.errors import ConfigError

from ..plugins.registry import build_advisor_registry
from .advisor_consent import build_preview


_ROUTING_QUESTION: dict[str, Any] = {
    "type": "choice",
    "instructions": "Which WaveBench entry point best matches the request?",
    "criteria": {
        "capture.inspect": "Inspect an existing offline capture package.",
        "run.template.sine": "Generate a plan to start a sine measurement run.",
    },
}


def preview_advisor_ask(*, advisor_id: str, question_id: str, state_path: str | Path) -> dict[str, Any]:
    """Build the exact local preview for one supported advisor request.

    This function never resolves executable entry points, reads a WaveBench config,
    opens a device, or writes to disk beyond reading the explicit state fixture.
    """
    advisor = build_advisor_registry().registry.get(advisor_id)
    if "advisor.route" not in advisor.capabilities:
        raise ConfigError(f"advisor {advisor_id!r} does not support route questions")
    if question_id != "route":
        raise ConfigError(f"unsupported advisor question: {question_id}")

    state = _load_state(state_path)
    preview = build_preview(
        model=advisor.advisor_id,
        state=state,
        questions={question_id: _ROUTING_QUESTION},
    )
    return {
        "schema": "wavebench.advisor_ask.v1",
        "status": "preview_only",
        "advisor_id": advisor.advisor_id,
        "artifact_path": None,
        "preview": preview.as_dict(),
    }


def ask_local_rule_advisor(
    *, advisor_id: str, question_id: str, state_path: str | Path
) -> dict[str, Any]:
    """Return one deterministic, advisory-only recommendation for the built-in baseline."""
    advisor = build_advisor_registry().registry.get(advisor_id)
    if advisor.advisor_id != "builtin.rule_advisor":
        raise ConfigError(f"advisor {advisor_id!r} has no local client")
    if question_id != "route":
        raise ConfigError(f"unsupported advisor question: {question_id}")
    state = _load_state(state_path)
    choice, confidence = _route_choice(state)
    recommendations: list[dict[str, Any]] = []
    if confidence >= 0.60:
        option = _ROUTING_QUESTION["criteria"][choice]
        command = (
            "python -m wavebench capture inspect {capture_dir}"
            if choice == "capture.inspect"
            else "python -m wavebench run template source-scope-sine"
        )
        recommendations.append(
            {
                "id": "route",
                "priority": "normal",
                "action": choice,
                "reason": f"keyword-overlap confidence {confidence:.2f}; {option}",
                "command": command,
                "parameters": {},
                "mutates_instrument_if_applied": False,
                "raw_scpi": False,
            }
        )
    elif confidence >= 0.35:
        recommendations.append(
            {
                "id": "route_needs_review",
                "priority": "high",
                "action": "human_review",
                "reason": f"keyword-overlap confidence {confidence:.2f} is below the accept threshold",
                "command": None,
                "parameters": {"option_id": choice},
                "mutates_instrument_if_applied": False,
                "raw_scpi": False,
            }
        )
    return {
        "schema": "wavebench.advisor_ask.v1",
        "status": "ok",
        "advisor_id": advisor.advisor_id,
        "artifact_path": None,
        "recommendations": recommendations,
    }


def _load_state(path: str | Path) -> dict[str, Any]:
    source = Path(path)
    try:
        decoded = json.loads(source.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"cannot read advisor state {source}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"advisor state must be valid JSON: {exc}") from exc
    if not isinstance(decoded, Mapping):
        raise ConfigError("advisor state must be a JSON object")
    fields = decoded.get("fields", {})
    untrusted = decoded.get("untrusted", ())
    if not isinstance(fields, Mapping):
        raise ConfigError("advisor state fields must be an object")
    if not isinstance(untrusted, list) or not all(isinstance(item, Mapping) for item in untrusted):
        raise ConfigError("advisor state untrusted must be an array of objects")
    return {"fields": dict(fields), "untrusted": [dict(item) for item in untrusted]}


def _route_choice(state: Mapping[str, Any]) -> tuple[str, float]:
    """Match only the code-owned closed routing catalog; ties are deterministic."""
    text = " ".join(
        [str(value) for value in state["fields"].values()]
        + [str(item.get("text", "")) for item in state["untrusted"]]
    ).lower()
    tokens = set(re.findall(r"[a-z0-9]+", text))
    scores = {
        option_id: len(tokens & set(re.findall(r"[a-z0-9]+", summary.lower())))
        for option_id, summary in _ROUTING_QUESTION["criteria"].items()
    }
    total = sum(scores.values())
    confidence = scores[max(sorted(scores), key=scores.__getitem__)] / total if total else 1 / len(scores)
    return max(sorted(scores), key=scores.__getitem__), confidence
