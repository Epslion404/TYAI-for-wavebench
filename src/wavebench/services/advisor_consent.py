"""advisor 外部状态外发的同意门（Core 拥有）。

契约见 `docs/project/rfcs/WaveBench_advisor插件RFC.md`：

- 默认关闭，由 `[advisor] enabled` 与调用方的显式 opt-in 共同开启；
- 发送前必须能产出完整预览（payload、字节数、sha256、逐条 untrusted 来源），预览不联网；
- 同意绑定 endpoint 集合与允许字段集，任一变化即失效；
- 非交互场景一律拒绝；
- API key 永不进入 artifact、日志或错误信息。

本模块只用标准库，且不含任何网络代码：拒绝路径下调用方没有任何可外发的通道。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Collection, Mapping, Sequence

ADVISOR_CONSENT_SCHEMA = "wavebench.advisor_consent.v1"

STATUS_GRANTED = "granted"
STATUS_REFUSED = "refused"
STATUS_PREVIEW_ONLY = "preview_only"

REASON_DISABLED = "advisor_disabled"
REASON_NON_INTERACTIVE = "non_interactive_consent_not_allowed"
REASON_NOT_GRANTED = "external_state_consent_not_granted"
REASON_PREVIEW_ONLY = "preview_only"
REASON_ALLOWLIST_MISSING = "registered_allowlist_missing"
REASON_ENDPOINT_NOT_REGISTERED = "endpoint_not_registered"
REASON_FIELD_NOT_REGISTERED = "state_field_not_registered"
REASON_INVALID_SCOPE = "invalid_consent_scope"
REASON_ENDPOINT_CHANGED = "consent_invalidated_by_endpoint_change"
REASON_FIELD_CHANGED = "consent_invalidated_by_allowed_field_change"
REASON_RUN_REQUIRED = "run_scoped_consent_requires_run_target"
REASON_RUN_CHANGED = "consent_invalidated_by_run_change"

VALID_SCOPES = ("invocation", "run")

_SENSITIVE_KEY_PARTS = (
    "api_key",
    "apikey",
    "authorization",
    "credential",
    "password",
    "secret",
    "token",
)
_SENSITIVE_TEXT = re.compile(
    r"(?i)\b(?:api[_-]?key|authorization|credential|password|secret|token)\b"
    # Consume escaped characters as pairs so an escaped quote cannot end the value.
    r"[\"']?\s*[:=]\s*(?:\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|"
    # Free-form credentials can contain JSON punctuation; only whitespace ends
    # an unquoted value. Quoted strings keep their own closing-quote boundary.
    r"(?:bearer\s+)?\S+)"
)


class _FrozenDict(dict):
    """JSON-compatible recursive immutable mapping used by preview payloads."""

    def _immutable(self, *_: Any, **__: Any) -> None:
        raise TypeError("preview payload is immutable")

    __setitem__ = _immutable
    __delitem__ = _immutable
    __ior__ = _immutable
    clear = _immutable
    pop = _immutable
    popitem = _immutable
    setdefault = _immutable
    update = _immutable


def _is_sensitive_key(key: object) -> bool:
    normalized = str(key).lower().replace("-", "_")
    return any(part in normalized for part in _SENSITIVE_KEY_PARTS)


def _redact_sensitive_text(value: str) -> str:
    return _SENSITIVE_TEXT.sub("[REDACTED]", value)


def redact_sensitive(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): "[REDACTED]" if _is_sensitive_key(key) else redact_sensitive(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact_sensitive(item) for item in value]
    if isinstance(value, str):
        return _redact_sensitive_text(value)
    return value


def _redact_untrusted_text(state: Mapping[str, Any]) -> dict[str, Any]:
    """Untrusted spans may contain arbitrary instrument/operator text, so omit their bodies."""
    copied = dict(state)
    spans = copied.get("untrusted")
    if not isinstance(spans, (list, tuple)):
        return copied
    safe_spans: list[Any] = []
    for span in spans:
        if not isinstance(span, Mapping):
            safe_spans.append(redact_sensitive(span))
            continue
        safe = dict(span)
        for key in tuple(safe):
            if str(key).lower() in {"text", "raw", "content", "payload"}:
                safe[key] = "[REDACTED_UNTRUSTED_TEXT]"
        safe_spans.append(safe)
    copied["untrusted"] = safe_spans
    return copied


def _freeze_payload(value: Any) -> Any:
    if isinstance(value, Mapping):
        frozen = _FrozenDict()
        dict.update(frozen, {str(key): _freeze_payload(item) for key, item in value.items()})
        return frozen
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_payload(item) for item in value)
    return value


def normalize_names(values: Sequence[str], *, label: str) -> tuple[str, ...]:
    """校验并规格化名字集合（去重、排序），保持产物可比对。"""
    if isinstance(values, (str, bytes)):
        raise ValueError(f"{label} must be a sequence of names, not a string")
    for value in values:
        if not isinstance(value, str) or not value or value.strip() != value:
            raise ValueError(f"{label} must contain non-empty, trimmed names")
    return tuple(sorted(set(values)))


@dataclass(frozen=True)
class PreviewPayload:
    """即将离开本机的完整请求体；给操作员看，不联网。"""

    payload: Mapping[str, Any]
    payload_sha256: str
    payload_bytes: int
    untrusted_sources: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "payload": json.loads(json.dumps(self.payload, ensure_ascii=False)),
            "payload_sha256": self.payload_sha256,
            "payload_bytes": self.payload_bytes,
            "untrusted_sources": list(self.untrusted_sources),
        }


def build_preview(
    *,
    model: str,
    state: Mapping[str, Any],
    questions: Mapping[str, Any],
) -> PreviewPayload:
    """构造预览：脱敏后才计算字节数/sha256，调用方不能跳过。

    键名按大小写、`-`/`_` 变体递归脱敏，字符串中的 ``token=...`` 等也会擦除；
    `state.untrusted` 的正文一律不外发，只保留来源。字段白名单随后由
    :func:`resolve_consent` 针对调用方声明的字段名校验，脱敏不改变该校验输入。
    """
    body = _freeze_payload(
        redact_sensitive(
            {"model": model, "state": _redact_untrusted_text(state), "questions": dict(questions)}
        )
    )
    encoded = json.dumps(body, ensure_ascii=False, sort_keys=True).encode("utf-8")
    sources: list[str] = []
    for span in state.get("untrusted", ()) or ():
        if isinstance(span, Mapping):
            source = str(span.get("source", ""))
            if source:
                sources.append(source)
    return PreviewPayload(
        payload=body,
        payload_sha256=hashlib.sha256(encoded).hexdigest(),
        payload_bytes=len(encoded),
        untrusted_sources=tuple(sorted(set(sources))),
    )


def unregistered_state_fields(fields: Collection[str], allowed: Collection[str]) -> tuple[str, ...]:
    """字段级白名单检查：出现未注册字段就拒绝，避免顺手把新字段送出机器。"""
    return tuple(sorted(set(fields) - set(allowed)))


@dataclass(frozen=True)
class ConsentDecision:
    """操作员对「把这份 state 发到外部服务」的显式决定。

    默认按次确认；`valid_for="run"` 时同意只对同一 `run_id` 有效。
    """

    granted: bool
    valid_for: str = "invocation"
    run_id: str | None = None
    endpoint_hosts: tuple[str, ...] = ()
    allowed_state_fields: tuple[str, ...] = ()
    granted_by: str | None = None
    granted_at: str | None = None

    def __post_init__(self) -> None:
        if self.valid_for not in VALID_SCOPES:
            raise ValueError(
                f"consent valid_for must be one of {', '.join(VALID_SCOPES)}, got {self.valid_for!r}"
            )
        if self.granted and self.valid_for == "run" and not self.run_id:
            raise ValueError("run-scoped consent requires run_id")
        object.__setattr__(
            self, "endpoint_hosts", normalize_names(self.endpoint_hosts, label="endpoint_hosts")
        )
        object.__setattr__(
            self,
            "allowed_state_fields",
            normalize_names(self.allowed_state_fields, label="allowed_state_fields"),
        )

    def as_payload(self, preview: PreviewPayload) -> dict[str, Any]:
        return {
            "granted": self.granted,
            "valid_for": self.valid_for,
            "run_id": self.run_id,
            "granted_by": self.granted_by,
            "granted_at": self.granted_at,
            "endpoint_hosts": list(self.endpoint_hosts),
            "allowed_state_fields": list(self.allowed_state_fields),
            "payload_sha256": preview.payload_sha256,
            "payload_bytes": preview.payload_bytes,
            "accepted_data_leaves_machine": self.granted,
        }


@dataclass(frozen=True)
class ConsentOutcome:
    """同意门的结论；`status != "granted"` 时调用方必须不发起外发。"""

    status: str
    reason: str | None
    preview: PreviewPayload
    decision: ConsentDecision | None = None
    endpoint_hosts: tuple[str, ...] = ()
    allowed_state_fields: tuple[str, ...] = ()

    @property
    def granted(self) -> bool:
        return self.status == STATUS_GRANTED

    def as_payload(self) -> dict[str, Any]:
        record: dict[str, Any] = {
            "schema": ADVISOR_CONSENT_SCHEMA,
            "status": self.status,
            "reason": self.reason,
            "endpoint_hosts": list(self.endpoint_hosts),
            "allowed_state_fields": list(self.allowed_state_fields),
            "payload_sha256": self.preview.payload_sha256,
            "payload_bytes": self.preview.payload_bytes,
            "accepted_data_leaves_machine": self.granted,
        }
        if self.decision is not None:
            record.update(
                {
                    "granted": self.decision.granted,
                    "valid_for": self.decision.valid_for,
                    "run_id": self.decision.run_id,
                    "granted_by": self.decision.granted_by,
                    "granted_at": self.decision.granted_at,
                }
            )
        else:
            record.update({"granted": False, "valid_for": None, "run_id": None,
                           "granted_by": None, "granted_at": None})
        return record


def consent_covers(
    consent: ConsentDecision,
    *,
    endpoint_hosts: Sequence[str],
    allowed_state_fields: Sequence[str],
    run_id: str | None,
    interactive: bool = False,
) -> tuple[bool, str | None]:
    """判断已有同意是否覆盖本次外发；非交互路径不得复用任何同意。"""
    if not consent.granted:
        return False, REASON_NOT_GRANTED
    if not interactive:
        return False, REASON_NON_INTERACTIVE
    if set(consent.endpoint_hosts) != set(endpoint_hosts):
        return False, REASON_ENDPOINT_CHANGED
    if set(consent.allowed_state_fields) != set(allowed_state_fields):
        return False, REASON_FIELD_CHANGED
    if consent.valid_for == "invocation":
        return True, None
    if run_id is None:
        return False, REASON_RUN_REQUIRED
    if consent.run_id != run_id:
        return False, REASON_RUN_CHANGED
    return True, None


def resolve_consent(
    *,
    preview: PreviewPayload,
    endpoint_hosts: Sequence[str],
    state_fields: Collection[str],
    enabled: bool,
    interactive: bool = False,
    registered_endpoint_hosts: Sequence[str] = (),
    registered_state_fields: Collection[str] = (),
    run_id: str | None = None,
    preview_only: bool = False,
    accepted: bool = False,
    valid_for: str = "invocation",
    granted_by: str | None = None,
    granted_at: str | None = None,
) -> ConsentOutcome:
    """Core 侧的同意门。

    预览不联网，因而在开启后优先返回 ``preview_only``，不要求交互标志或完整白名单；它也绝不
    返回可复用同意。实际外发的拒绝顺序：未开启 → 非交互 → 白名单缺失 → 未注册字段 → 未注册
    endpoint → 未确认。白名单校验使用调用方声明的字段/主机集合，而 preview 已先完成脱敏与哈希。
    """
    hosts = normalize_names(endpoint_hosts, label="endpoint_hosts")
    fields = normalize_names(tuple(state_fields), label="state_fields")

    def refused(reason: str) -> ConsentOutcome:
        return ConsentOutcome(
            status=STATUS_REFUSED,
            reason=reason,
            preview=preview,
            endpoint_hosts=hosts,
            allowed_state_fields=fields,
        )

    if not enabled:
        return refused(REASON_DISABLED)

    if preview_only:
        return ConsentOutcome(
            status=STATUS_PREVIEW_ONLY,
            reason=REASON_PREVIEW_ONLY,
            preview=preview,
            endpoint_hosts=hosts,
            allowed_state_fields=fields,
        )

    if not interactive:
        return refused(REASON_NON_INTERACTIVE)

    registered_fields = normalize_names(
        tuple(registered_state_fields), label="registered_state_fields"
    )
    registered_hosts = normalize_names(
        registered_endpoint_hosts, label="registered_endpoint_hosts"
    )
    if not registered_fields or not registered_hosts:
        return refused(REASON_ALLOWLIST_MISSING)

    unregistered = unregistered_state_fields(fields, registered_fields)
    if unregistered:
        return refused(f"{REASON_FIELD_NOT_REGISTERED}: {', '.join(unregistered)}")

    allowed_hosts = set(registered_hosts)
    unknown_hosts = tuple(sorted(set(hosts) - allowed_hosts))
    if unknown_hosts:
        return refused(f"{REASON_ENDPOINT_NOT_REGISTERED}: {', '.join(unknown_hosts)}")

    if not accepted:
        return refused(REASON_NOT_GRANTED)

    if valid_for not in VALID_SCOPES:
        return refused(REASON_INVALID_SCOPE)
    if valid_for == "run" and not run_id:
        return refused(REASON_RUN_REQUIRED)

    decision = ConsentDecision(
        granted=True,
        valid_for=valid_for,
        run_id=run_id if valid_for == "run" else None,
        endpoint_hosts=hosts,
        allowed_state_fields=fields,
        granted_by=granted_by,
        granted_at=granted_at,
    )
    return ConsentOutcome(
        status=STATUS_GRANTED,
        reason=None,
        preview=preview,
        decision=decision,
        endpoint_hosts=hosts,
        allowed_state_fields=fields,
    )


__all__ = [
    "ADVISOR_CONSENT_SCHEMA",
    "REASON_ALLOWLIST_MISSING",
    "REASON_DISABLED",
    "REASON_ENDPOINT_CHANGED",
    "REASON_ENDPOINT_NOT_REGISTERED",
    "REASON_FIELD_CHANGED",
    "REASON_FIELD_NOT_REGISTERED",
    "REASON_NOT_GRANTED",
    "REASON_NON_INTERACTIVE",
    "REASON_PREVIEW_ONLY",
    "REASON_INVALID_SCOPE",
    "REASON_RUN_CHANGED",
    "REASON_RUN_REQUIRED",
    "STATUS_GRANTED",
    "STATUS_PREVIEW_ONLY",
    "STATUS_REFUSED",
    "VALID_SCOPES",
    "ConsentDecision",
    "ConsentOutcome",
    "PreviewPayload",
    "build_preview",
    "consent_covers",
    "normalize_names",
    "redact_sensitive",
    "resolve_consent",
    "unregistered_state_fields",
]
