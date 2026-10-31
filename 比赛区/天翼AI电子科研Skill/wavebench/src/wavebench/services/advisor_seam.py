"""advisor 决策 seam（Core 拥有）：state → questions → answers → thresholds → artifact。

契约见 `docs/project/rfcs/WaveBench_advisor插件RFC.md`：

- 只用标准库，**本模块没有任何网络代码**；唯一可能外发的通道是调用方注入的 client；
- `build_state` / `questions` / `answers → recommendations` 全是确定性纯函数，数字在代码里分桶；
- 模型输出永不变成命令字符串：建议里的 `command` 一律来自代码持有的 catalog；
- **client 拿到的就是预览里那份脱敏字节**，未脱敏的 state 不会离开本模块；
- 与安全相关的通用能力由 Core 的 `services/advisor_consent.py`（同意门）与
  `services/decision_artifacts.py`（artifact 契约）拥有，本模块只复用、不重复定义；
- 任何失败（未开启／未同意／未配置 client／调用异常／响应非法）都降级成 `DecisionArtifact`，
  绝不抛出，也绝不参与 `run.json.status`、质量门、`auto_recover` 或 capability 判定。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
import json
import math
from typing import Any, Protocol

from .advisor_consent import (
    STATUS_GRANTED,
    STATUS_PREVIEW_ONLY,
    STATUS_REFUSED,
    build_preview,
    resolve_consent,
    unregistered_state_fields,
)
from .decision_artifacts import DecisionArtifact, DecisionTarget, ThresholdPolicy

# 固定版本：官方文档说明别名会随发布漂移，advisor 不允许用 `latest`。
DEFAULT_MODEL = "jev-1.13.0"
NOT_STATED = "not_stated"

# 插件声明之外的状态字段：属于插件自己的契约被违反，直接拒绝而不是照发。
REASON_FIELD_OUTSIDE_DECLARATION = "state_field_outside_plugin_declaration"
REASON_NOT_CONFIGURED = "advisor_not_configured"


class SeamError(RuntimeError):
    """seam 内部契约被违反（不是网络或服务错误）。"""


class UnknownOptionError(SeamError):
    """模型返回了 catalog 之外的选项 id：fail-closed，不使用该结论。"""


# --------------------------------------------------------------------------- #
# 状态：可信字段与不可信 span 分离
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class UntrustedSpan:
    """仪器回读、日志文本、操作员原话等不可信内容，逐条带来源。"""

    source: str
    text: str


@dataclass(frozen=True)
class DecisionState:
    fields: Mapping[str, Any] = field(default_factory=dict)
    untrusted: tuple[UntrustedSpan, ...] = ()

    def as_payload(self) -> dict[str, Any]:
        return {
            "fields": dict(self.fields),
            "untrusted": [{"source": span.source, "text": span.text} for span in self.untrusted],
        }

    @classmethod
    def from_payload(cls, raw: Mapping[str, Any]) -> DecisionState:
        """从 fixture 构造状态；形状不对就报错，不做兜底猜测。"""
        if not isinstance(raw, Mapping):
            raise SeamError("decision state must be a JSON object")
        fields = raw.get("fields", {})
        if not isinstance(fields, Mapping):
            raise SeamError("decision state 'fields' must be a JSON object")
        spans = raw.get("untrusted", ()) or ()
        if isinstance(spans, (str, bytes)) or not isinstance(spans, Sequence):
            raise SeamError("decision state 'untrusted' must be a JSON array")
        parsed: list[UntrustedSpan] = []
        for span in spans:
            if not isinstance(span, Mapping) or not str(span.get("source", "")).strip():
                raise SeamError("each untrusted span needs a non-empty 'source'")
            parsed.append(UntrustedSpan(source=str(span["source"]), text=str(span.get("text", ""))))
        return cls(
            fields={str(key): value for key, value in fields.items()},
            untrusted=tuple(parsed),
        )


def bucket_cycles(estimated_cycles: float | None) -> str:
    """数字在代码里分桶，绝不把原始测量值交给模型做数值判断。"""
    if estimated_cycles is None:
        return NOT_STATED
    if estimated_cycles < 2.0:
        return "few_cycles"
    if estimated_cycles > 25.0:
        return "many_cycles"
    return "ok"


def bucket_points_per_cycle(points_per_cycle: float | None) -> str:
    if points_per_cycle is None:
        return NOT_STATED
    return "dense" if points_per_cycle >= 20.0 else "sparse"


# --------------------------------------------------------------------------- #
# 问题：选项集来自代码持有的 catalog
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class QuestionSpec:
    question_id: str
    kind: str  # "choice" | "score" | "noul"
    instructions: str
    criteria: Mapping[str, str] | tuple[str, ...] | None = None

    def as_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"type": self.kind, "instructions": self.instructions}
        if self.criteria is not None:
            payload["criteria"] = self.criteria
        return payload


@dataclass(frozen=True)
class RoutingOption:
    """一个可选入口；`command_template` 只留在代码里，模型看不到。"""

    option_id: str
    summary: str
    command_template: str
    mutates_instrument: bool


def build_routing_question(
    options: Sequence[RoutingOption],
    *,
    instructions: str = "Which WaveBench entry point best matches the request?",
) -> QuestionSpec:
    if not options:
        raise SeamError("routing catalog must not be empty")
    criteria: dict[str, str] = {}
    for option in options:
        if option.option_id in criteria:
            raise SeamError(f"duplicate option id: {option.option_id}")
        criteria[option.option_id] = option.summary
    return QuestionSpec("route", "choice", instructions, criteria)


def build_questions(*specs: QuestionSpec) -> dict[str, QuestionSpec]:
    questions: dict[str, QuestionSpec] = {}
    for spec in specs:
        if spec.question_id in questions:
            raise SeamError(f"duplicate question id: {spec.question_id}")
        questions[spec.question_id] = spec
    return questions


# --------------------------------------------------------------------------- #
# 答案与阈值：概率 → 动作，阈值随结果记录
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Answer:
    question_id: str
    kind: str
    value: Any  # choice id 字符串 / score 数值 / noul 概率
    confidence: float | None = None
    probabilities: Mapping[str, float] | None = None


def parse_answers(raw: Mapping[str, Any]) -> dict[str, Answer]:
    """把服务响应解析成 typed answers；未知类型直接报错，不做兜底猜测。"""
    if not isinstance(raw, Mapping):
        raise SeamError("advisor response must be an object")
    body = raw.get("answers")
    if not isinstance(body, Mapping):
        raise SeamError("response has no 'answers' object")
    answers: dict[str, Answer] = {}
    for question_id, entry in body.items():
        if not isinstance(entry, Mapping):
            raise SeamError("answer entry is not an object")
        kind = str(entry.get("type", ""))
        probabilities = entry.get("probabilities")
        if probabilities is not None and not isinstance(probabilities, Mapping):
            raise SeamError("probabilities must be an object")
        normalized = (
            {str(key): _probability(value) for key, value in probabilities.items()}
            if isinstance(probabilities, Mapping) else None
        )
        if kind == "choice":
            value: Any = entry.get("choice")
            if not isinstance(value, str) or normalized is None or value not in normalized:
                raise SeamError("choice requires a string id and its probability")
        elif kind == "score":
            value = _finite_number(entry.get("score"))
        elif kind == "noul":
            value = _probability(entry.get("noul"))
        else:
            raise SeamError("unknown answer type")
        answers[str(question_id)] = Answer(
            question_id=str(question_id),
            kind=kind,
            value=value,
            confidence=_optional_float(entry.get("confidence")),
            probabilities=normalized,
        )
    return answers


def _finite_number(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SeamError("answer number must be numeric")
    try:
        number = float(value)
    except (OverflowError, ValueError, TypeError) as exc:
        raise SeamError("answer number must be finite") from exc
    if not math.isfinite(number):
        raise SeamError("answer number must be finite")
    return number


def _probability(value: Any) -> float:
    number = _finite_number(value)
    if not 0.0 <= number <= 1.0:
        raise SeamError("answer probability must be within 0..1")
    return number


def _optional_float(value: Any) -> float | None:
    return None if value is None else _probability(value)


def apply_choice_threshold(
    answer: Answer,
    options: Sequence[RoutingOption],
    policy: ThresholdPolicy,
) -> list[dict[str, Any]]:
    """把 Choice 概率映射成建议；低于 accept 给人工复核，低于 review 什么都不给。"""
    if answer.kind != "choice":
        raise SeamError("expected choice answer")
    by_id = {option.option_id: option for option in options}
    choice = answer.value
    if not isinstance(choice, str) or choice not in by_id:
        raise UnknownOptionError("option is not in the catalog")
    if not isinstance(answer.probabilities, Mapping) or choice not in answer.probabilities:
        raise SeamError("choice probability is missing")
    for option_id, value in answer.probabilities.items():
        if option_id not in by_id:
            raise UnknownOptionError("probability option is not in the catalog")
        _probability(value)
    _optional_float(answer.confidence)
    probability = _probability(answer.probabilities[choice])
    if probability < policy.review:
        return []
    if probability < policy.accept:
        return [
            {
                "id": f"{answer.question_id}_needs_review",
                "priority": "high",
                "action": "human_review",
                "reason": (
                    f"choice probability {probability:.2f} < accept {policy.accept:.2f}; "
                    "route to a human instead of acting"
                ),
                "command": None,
                "parameters": {"option_id": choice},
                "mutates_instrument_if_applied": False,
                "raw_scpi": False,
            }
        ]
    option = by_id[choice]
    return [
        {
            "id": answer.question_id,
            "priority": "high" if option.mutates_instrument else "normal",
            "action": option.option_id,
            "reason": f"choice probability {probability:.2f} >= accept {policy.accept:.2f}",
            "command": option.command_template,
            "parameters": {},
            "mutates_instrument_if_applied": option.mutates_instrument,
            "raw_scpi": False,
        }
    ]


# --------------------------------------------------------------------------- #
# 调用上下文与门面
# --------------------------------------------------------------------------- #


class SystemOneClient(Protocol):
    """唯一可注入的外部依赖；真实 adapter 不参与离线测试。"""

    model_id: str

    def system_one(
        self,
        *,
        model: str,
        state: Mapping[str, Any],
        questions: Mapping[str, Any],
    ) -> Mapping[str, Any]: ...


@dataclass(frozen=True)
class AdvisorAccess:
    """一次 advisor 调用的同意上下文，由调用方从 `[advisor]` 配置与插件声明装配。"""

    enabled: bool = False
    interactive: bool = False
    accepted: bool = False
    preview_only: bool = False
    endpoint_hosts: tuple[str, ...] = ()  # 插件声明会发往哪些 endpoint
    declared_state_fields: tuple[str, ...] = ()  # 插件声明的允许字段；空表示不做声明校验
    registered_endpoint_hosts: tuple[str, ...] = ()  # 操作员在 [advisor] 登记的白名单
    registered_state_fields: tuple[str, ...] = ()
    run_id: str | None = None
    granted_by: str | None = None
    granted_at: str | None = None


def state_payload_of(state: DecisionState) -> dict[str, Any]:
    return state.as_payload()


def advise(
    *,
    model: str,
    client: SystemOneClient | None,
    state: DecisionState,
    questions: Mapping[str, QuestionSpec],
    options: Sequence[RoutingOption],
    policy: ThresholdPolicy,
    access: AdvisorAccess,
    target: DecisionTarget | None = None,
    question_id: str = "route",
    timer: Callable[[], float] | None = None,
) -> DecisionArtifact:
    """跑一次 advisor；所有失败都降级为 artifact，绝不抛给调用方。"""
    state_payload = state.as_payload()
    questions_payload = {qid: spec.as_payload() for qid, spec in questions.items()}
    preview = build_preview(model=model, state=state_payload, questions=questions_payload)
    sent = preview.payload
    target_payload = target.as_payload() if target is not None else None
    outcome = resolve_consent(
        preview=preview,
        endpoint_hosts=access.endpoint_hosts,
        state_fields=tuple(state.fields),
        enabled=access.enabled,
        interactive=access.interactive,
        registered_endpoint_hosts=access.registered_endpoint_hosts,
        registered_state_fields=access.registered_state_fields,
        run_id=access.run_id,
        preview_only=access.preview_only,
        accepted=access.accepted,
        valid_for="invocation",
        granted_by=access.granted_by,
        granted_at=access.granted_at,
    )
    consent_payload = outcome.as_payload()
    shared: dict[str, Any] = {
        "requested_model": model,
        "target": target_payload,
        "consent": consent_payload,
        "state": dict(sent["state"]),
        "questions": dict(sent["questions"]),
        "thresholds": policy.as_record(),
    }
    if outcome.status != STATUS_GRANTED:
        status = "preview_only" if outcome.status == STATUS_PREVIEW_ONLY else "refused"
        return DecisionArtifact(status=status, reason=outcome.reason, **shared)

    # 插件没声明字段时不做该校验（非传输型 advisor 本来就不需要声明）
    if access.declared_state_fields:
        outside = unregistered_state_fields(tuple(state.fields), access.declared_state_fields)
        if outside:
            return DecisionArtifact(
                status=STATUS_REFUSED,
                reason=f"{REASON_FIELD_OUTSIDE_DECLARATION}: {', '.join(outside)}",
                **shared,
            )
    if client is None:
        return DecisionArtifact(
            status="unavailable", reason=REASON_NOT_CONFIGURED, **shared
        )

    started = timer() if timer is not None else None
    try:
        raw = client.system_one(
            model=model, state=sent["state"], questions=sent["questions"]
        )
    except Exception as exc:  # 网络 / 限流 / 认证：一律降级，不影响确定性路径
        return DecisionArtifact(
            status="unavailable",
            reason=f"advisor_call_failed: {type(exc).__name__}",
            duration_ms=_elapsed_ms(started, timer),
            **shared,
        )
    duration_ms = _elapsed_ms(started, timer)
    reported_model = raw.get("model") if isinstance(raw, Mapping) else None
    reported_model = str(reported_model) if isinstance(reported_model, str) else None
    try:
        answers = parse_answers(raw)
        if question_id not in answers:
            raise SeamError(f"response is missing question {question_id!r}")
        recommendations = tuple(apply_choice_threshold(answers[question_id], options, policy))
    except SeamError as exc:
        return DecisionArtifact(
            status="invalid",
            reason=f"invalid_advisor_response: {exc}",
            reported_model=reported_model,
            duration_ms=duration_ms,
            **shared,
        )
    return DecisionArtifact(
        status="ok",
        reason=None,
        reported_model=reported_model,
        duration_ms=duration_ms,
        answers={
            qid: {
                "type": answer.kind,
                "value": answer.value,
                "confidence": answer.confidence,
                "probabilities": dict(answer.probabilities or {}),
            }
            for qid, answer in answers.items()
        },
        recommendations=recommendations,
        **shared,
    )


def _elapsed_ms(started: float | None, timer: Callable[[], float] | None) -> int:
    if started is None or timer is None:
        return 0
    return int((timer() - started) * 1000)


def render_state(state: DecisionState) -> str:
    """确定性序列化，便于测试与产物比对（同输入同字节）。"""
    return json.dumps(state_payload_of(state), ensure_ascii=False, sort_keys=True)


__all__ = [
    "DEFAULT_MODEL",
    "NOT_STATED",
    "REASON_FIELD_OUTSIDE_DECLARATION",
    "REASON_NOT_CONFIGURED",
    "AdvisorAccess",
    "Answer",
    "DecisionState",
    "QuestionSpec",
    "RoutingOption",
    "SeamError",
    "SystemOneClient",
    "UnknownOptionError",
    "UntrustedSpan",
    "advise",
    "apply_choice_threshold",
    "bucket_cycles",
    "bucket_points_per_cycle",
    "build_questions",
    "build_routing_question",
    "parse_answers",
    "render_state",
    "state_payload_of",
]
