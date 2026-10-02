"""内置确定性 advisor 的 client 实现（`builtin.rule_advisor`）。

与真实服务同样的调用形状，但不联网、不依赖任何 SDK：纯词典重合度打分。它是
「外部 advisor 必须打败的确定性基线」，也是类别内的第二个实现。

只处理 `choice` 问题——本批内置基线只声明 `advisor.route`；多问题类型等有真实需求再加。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import re
from typing import Any

from ..plugins.builtin import RULE_ADVISOR_ID
from .advisor_seam import SeamError, SystemOneClient

RULE_ADVISOR_MODEL_ID = "builtin.rule_advisor"

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> set[str]:
    return set(_TOKEN_RE.findall(text.lower()))


def _state_tokens(state: Mapping[str, Any]) -> set[str]:
    """只用可信字段。

    untrusted span 的正文在预览阶段就被脱敏（`[REDACTED_UNTRUSTED_TEXT]`），不会离开本机，
    因而基线不能依赖它，否则等于在给"没有外发的内容"打分。
    """
    fields = state.get("fields") or {}
    if not isinstance(fields, Mapping):
        raise SeamError("rule advisor received a state without a 'fields' object")
    return _tokens(" ".join(str(value) for value in fields.values()))


def _choice_answer(spec: Mapping[str, Any], state_tokens: set[str]) -> dict[str, Any]:
    criteria = spec.get("criteria")
    if not isinstance(criteria, Mapping) or not criteria:
        raise SeamError("choice question needs a non-empty criteria mapping")
    scores = {str(key): len(_tokens(str(text)) & state_tokens) for key, text in criteria.items()}
    total = sum(scores.values())
    probabilities = {
        key: (value / total if total else 1.0 / len(scores)) for key, value in scores.items()
    }
    # 平局时取字典序最小的选项 id，保证确定性
    best = max(sorted(probabilities), key=lambda key: probabilities[key])
    return {
        "type": "choice",
        "choice": best,
        "confidence": probabilities[best],
        "probabilities": probabilities,
    }


@dataclass(frozen=True)
class RuleAdvisorClient:
    model_id: str = RULE_ADVISOR_MODEL_ID

    def system_one(
        self,
        *,
        model: str,
        state: Mapping[str, Any],
        questions: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        state_tokens = _state_tokens(state)
        answers: dict[str, Any] = {}
        for question_id, spec in questions.items():
            if not isinstance(spec, Mapping):
                raise SeamError(f"question {question_id!r} is not an object")
            kind = str(spec.get("type", ""))
            if kind != "choice":
                raise SeamError(f"rule advisor only answers choice questions, got {kind!r}")
            answers[str(question_id)] = _choice_answer(spec, state_tokens)
        return {"model": self.model_id, "answers": answers}


def advisor_client_for(advisor_id: str) -> SystemOneClient | None:
    """把 advisor id 解析成 client。

    本批只解析内置基线：第三方 advisor 的实现解析（entry point → adapter）属于后续阶段，
    未解析到就返回 `None`，由 seam 降级成 `unavailable`，绝不发起调用。
    """
    return RuleAdvisorClient() if advisor_id == RULE_ADVISOR_ID else None


__all__ = [
    "RULE_ADVISOR_MODEL_ID",
    "RuleAdvisorClient",
    "advisor_client_for",
]
