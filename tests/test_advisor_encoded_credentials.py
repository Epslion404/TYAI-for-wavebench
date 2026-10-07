"""Encoded credentials must be removed at every actual advisor output boundary."""

import hashlib
import json

import pytest

from wavebench.services import advisor_consent as consent
from wavebench.cli_output import _print_advisor_result
from wavebench.services.advisor_consent import build_preview, redact_sensitive
from wavebench.services.advisor_seam import (
    AdvisorAccess, DecisionState, RoutingOption, advise, build_questions, build_routing_question,
)
from wavebench.services.decision_artifacts import DecisionArtifact, ThresholdPolicy, write_artifact


OPTIONS = (RoutingOption("scope", "Scope help", "wavebench scope --help", False),)
SECRET = "AUDIT_DUMMY"


def encoded_credential(key, layers, diagnosis="keep"):
    text = json.dumps({key: 'Bearer "prefix AUDIT_DUMMY"', "diagnosis": diagnosis})
    for _ in range(layers):
        text = json.dumps(text)
    return text


def assert_boundaries(request, tmp_path, capsys, *, keep=True):
    state = DecisionState(fields={"request": request})
    questions = build_questions(build_routing_question(OPTIONS))
    preview = build_preview(
        model="fake.v1", state=state.as_payload(),
        questions={key: question.as_payload() for key, question in questions.items()},
    )
    calls = []

    class Client:
        def system_one(self, **kwargs):
            calls.append(kwargs)
            return {
                "model": request,
                "answers": {"route": {
                    "type": "choice", "choice": "scope", "probabilities": {"scope": 0.9},
                }},
            }

    artifact = advise(
        model="fake.v1", client=Client(), state=state, questions=questions, options=OPTIONS,
        policy=ThresholdPolicy(), access=AdvisorAccess(
            enabled=True, interactive=True, accepted=True,
            registered_endpoint_hosts=("example.invalid",), registered_state_fields=("request",),
        ),
    )
    assert artifact.status == "ok"
    assert len(calls) == 1
    assert calls[0]["state"] == preview.payload["state"] == artifact.state
    wire_state = json.loads(json.dumps(calls[0]["state"]))
    assert artifact.as_dict()["state"] == json.loads(artifact.to_json())["state"] == wire_state
    wire_body = json.dumps(preview.payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    assert preview.payload_bytes == len(wire_body)
    assert preview.payload_sha256 == hashlib.sha256(wire_body).hexdigest()
    direct = DecisionArtifact(
        status="invalid", requested_model=request, reported_model=request, reason=request,
    )
    for diagnostic in (direct.requested_model, direct.reported_model, direct.reason):
        assert redact_sensitive(diagnostic) == diagnostic
    boundaries = {
        "preview": json.dumps(preview.as_dict()), "client": json.dumps(calls),
        "artifact": artifact.to_json(), "direct": direct.to_json(),
        "direct_attributes": str((direct.requested_model, direct.reported_model, direct.reason)),
    }
    for name, item in (("artifact_file", artifact), ("direct_file", direct)):
        path = write_artifact(item, run_dir=tmp_path, advisor_id="fake")
        boundaries[name] = path.read_text(encoding="utf-8")
        if item is artifact:
            assert json.loads(boundaries[name])["state"] == wire_state
    _print_advisor_result(artifact)
    _print_advisor_result(direct)
    boundaries["cli"] = capsys.readouterr().out
    assert not [name for name, text in boundaries.items() if SECRET in text]
    if keep:
        assert all("keep" in text for text in boundaries.values())
    return preview.payload["state"]["fields"]["request"]


@pytest.mark.parametrize("key", ["authorization", "api_key", "token"])
@pytest.mark.parametrize("layers", [1, 2])
def test_six_outer_json_cases_cross_real_boundaries(key, layers, tmp_path, capsys):
    safe = assert_boundaries(encoded_credential(key, layers), tmp_path, capsys)
    assert isinstance(safe, str)
    for _ in range(layers):
        safe = json.loads(safe)
        assert isinstance(safe, str)
    assert json.loads(safe) == {key: "[REDACTED]", "diagnosis": "keep"}


@pytest.mark.parametrize("key", ["authorization", "api_key", "token"])
@pytest.mark.parametrize("layers", [1, 2])
def test_six_historical_outer_json_cases_with_diagnostic_suffix(key, layers, tmp_path, capsys):
    request = encoded_credential(key, layers) + " diagnosis=keep"
    safe = assert_boundaries(request, tmp_path, capsys)
    token, end = json.JSONDecoder().raw_decode(safe)
    assert safe[end:] == " diagnosis=keep"
    for _ in range(layers - 1):
        token = json.loads(token)
        assert isinstance(token, str)
    assert json.loads(token) == {key: "[REDACTED]", "diagnosis": "keep"}


def test_structured_sensitive_keys_replace_the_entire_value():
    value = {"API-KEY": {"diagnosis": "keep", "deep": [SECRET]},
             "nested": [{"refresh_token": [SECRET], "diagnosis": "keep"}]}
    assert redact_sensitive(value) == {
        "API-KEY": "[REDACTED]", "nested": [{"refresh_token": "[REDACTED]", "diagnosis": "keep"}],
    }


def test_normal_diagnostics_and_types_are_preserved():
    value = "invalid_advisor_response: probability must be within 0..1"
    assert redact_sensitive(value) == value
    assert redact_sensitive({"count": 1, "ready": True, "missing": None}) == {
        "count": 1, "ready": True, "missing": None,
    }


@pytest.mark.parametrize("text", [
    json.dumps({"authorization": {"parts": [SECRET, "extra"]}, "diagnosis": "keep"}),
    json.dumps({"token": "prefix\x00" + SECRET, "diagnosis": "keep"}),
    json.dumps({"密钥": "normal", "api_key": "雪 " + SECRET, "diagnosis": "keep"}),
    r'{"\u0061uthorization": "AUDIT_DUMMY", "diagnosis": "keep"}',
    json.dumps(json.dumps({"token": "\ud800" + SECRET, "diagnosis": "keep"})),
    json.dumps('authorization: Bearer "prefix AUDIT_DUMMY" diagnosis=keep'),
])
def test_json_unicode_nul_and_structured_values(text, tmp_path, capsys):
    assert_boundaries(text, tmp_path, capsys)


def test_max_supported_wrapper_depth(tmp_path, capsys):
    assert_boundaries(encoded_credential("token", 7), tmp_path, capsys)


@pytest.mark.parametrize("text", [
    encoded_credential("token", 8),
    "[" * 33 + '"AUDIT_DUMMY"' + "]" * 33,
    json.dumps({"token": SECRET, "items": list(range(4096))}),
    json.dumps({"token": SECRET, "diagnosis": "x" * (64 * 1024)}),
    json.dumps({"token": SECRET, "diagnosis": "雪" * 22000}, ensure_ascii=False),
    # Wrappers stay under 64 KiB each but together exceed 256 KiB of decoding.
    encoded_credential("token", 7, "x" * 45000),
], ids=["decode-count", "depth", "item-count", "input-bytes", "utf8-bytes", "work-bytes"])
def test_limits_fail_closed_across_real_boundaries(text, tmp_path, capsys):
    assert assert_boundaries(text, tmp_path, capsys, keep=False) == "[REDACTED_JSON_LIMIT]"


@pytest.mark.parametrize("text", [
    json.dumps({"token": SECRET, "padding": "x" * 65536}),
    json.dumps({"token": SECRET, "padding": "雪" * 22000}, ensure_ascii=False),
    "[" * 33 + '"AUDIT_DUMMY"' + "]" * 33,
    json.dumps([0] * 4096 + ["token=AUDIT_DUMMY"]),
], ids=["input-bytes", "utf8-bytes", "depth", "item-count"])
def test_oversized_input_or_structure_never_calls_json_decoder(text, monkeypatch):
    calls = []

    def forbidden(*args, **kwargs):
        calls.append(args)
        raise AssertionError("json.loads must not allocate an over-budget tree")

    monkeypatch.setattr(consent.json, "loads", forbidden)
    assert redact_sensitive(text) == "[REDACTED_JSON_LIMIT]"
    assert calls == []


def test_shared_decoded_work_checked_before_next_json_load(monkeypatch):
    # Lower the work budget to exercise sibling accumulation with a small input.
    text = json.dumps([json.dumps({"diagnosis": "x" * 2000})] * 6 + ["token=AUDIT_DUMMY"])
    real_loads = json.loads
    decoded_sizes = []

    def spy(value):
        decoded_sizes.append(len(value.encode("utf-8")))
        return real_loads(value)

    original_budget = consent._JsonBudget
    monkeypatch.setattr(consent, "_JsonBudget", lambda: original_budget(bytes_left=16000))
    monkeypatch.setattr(consent.json, "loads", spy)
    assert redact_sensitive(text) == "[REDACTED_JSON_LIMIT]"
    assert len(decoded_sizes) == 2
    assert sum(decoded_sizes) <= 16000


@pytest.mark.parametrize("text", [
    'failure ' + encoded_credential("authorization", 1) + " diagnosis=keep",
    encoded_credential("api_key", 2)[:-1],
    r'failure {"\u0061pi_key": "AUDIT_DUMMY"} diagnosis=keep',
])
def test_embedded_or_malformed_escaped_json_is_explicitly_omitted(text, tmp_path, capsys):
    assert assert_boundaries(text, tmp_path, capsys, keep=False) == "[REDACTED_ENCODED_TEXT]"


def test_large_plain_diagnostics_are_preserved():
    text = "diagnosis=keep " + "normal " * 50000
    assert redact_sensitive(text) == text
    assert redact_sensitive(text + "token=AUDIT_DUMMY") == text + "[REDACTED]"
    backslashes = "diagnosis=keep " + "\\" * 100000
    assert redact_sensitive(backslashes) == backslashes


@pytest.mark.parametrize("text", [
    ' { "diagnosis" : "keep", "雪": [1, true, null] } ',
    '"plain diagnostic keep"',
    json.dumps(json.dumps('{ "diagnosis": "keep" }')),
])
def test_secret_free_json_is_byte_preserved(text):
    assert redact_sensitive(text) == text


def test_sensitive_mapping_value_bypasses_encoded_limits(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("a sensitive mapping value must never be interpreted")

    monkeypatch.setattr(consent.json, "loads", forbidden)
    assert redact_sensitive({"access_token": '"' * 100000 + SECRET}) == {
        "access_token": "[REDACTED]",
    }


def test_finite_item_and_container_boundaries_preserve_secret_free_json():
    for text in (json.dumps([0] * 4095), "[" * 32 + "0" + "]" * 32):
        assert redact_sensitive(text) == text


def test_decode_count_is_checked_before_ninth_load(monkeypatch):
    real_loads = json.loads
    calls = []

    def spy(value):
        calls.append(value)
        return real_loads(value)

    text = encoded_credential("token", 8)
    monkeypatch.setattr(consent.json, "loads", spy)
    assert redact_sensitive(text) == "[REDACTED_JSON_LIMIT]"
    assert len(calls) == 8


@pytest.mark.parametrize("text", [
    '"token=AUDIT_DUMMY\\',
    r'{"authorization\": "prefix AUDIT_DUMMY"}',
    '"\\u0061uthorization: AUDIT_DUMMY',
])
def test_obviously_malformed_encoded_candidates_never_fall_back_to_plain_regex(text):
    assert redact_sensitive(text) == "[REDACTED_ENCODED_TEXT]"


def unicode_credential(count, layers=0, diagnostic="雪"):
    text = json.dumps({"token": SECRET, "diagnosis": diagnostic * count}, ensure_ascii=False)
    for _ in range(layers):
        text = json.dumps(text, ensure_ascii=False)
    return text


@pytest.mark.parametrize("count,layers,diagnostic", [
    (21000, 0, "雪"), (10000, 1, "雪"), (6000, 2, "雪"),
    (10000, 0, "\ud800"), (9000, 2, "😀"),
], ids=["unicode-output-limit", "one-wrapper", "two-wrappers", "surrogate", "astral"])
def test_unicode_reencoding_stays_identical_at_all_boundaries(
    count, layers, diagnostic, tmp_path, capsys,
):
    text = unicode_credential(count, layers, diagnostic)
    safe = assert_boundaries(text, tmp_path, capsys, keep=False)
    assert redact_sensitive(safe) == safe
    assert len(safe.encode("utf-8", errors="surrogatepass")) <= 65536


def near_work_limit_credential():
    def wrapped(count):
        text = json.dumps({"token": SECRET, "diagnosis": "x" * count}, separators=(",", ":"))
        for _ in range(7):
            text = json.dumps(text)
        return text

    def work_bytes(text):
        total = 0
        while isinstance(text, str):
            total += len(text.encode("utf-8"))
            text = json.loads(text)
        return total

    count = (262144 - work_bytes(wrapped(0))) // 8
    text = wrapped(count)
    assert work_bytes(text) <= 262144
    assert len(text.encode("utf-8")) <= 65536
    return text


def test_reencoded_output_cannot_cross_next_pass_work_limit(tmp_path, capsys):
    safe = assert_boundaries(near_work_limit_credential(), tmp_path, capsys, keep=False)
    assert safe == "[REDACTED_JSON_LIMIT]"
    assert redact_sensitive(safe) == safe


def test_exact_reported_work_boundary_is_stable_and_validation_is_bounded(
    tmp_path, capsys, monkeypatch,
):
    text = json.dumps({"token": "x", "diagnosis": "x" * 32422})
    for _ in range(7):
        text = json.dumps(text)
    assert len(text.encode("utf-8")) == 33723
    safe = assert_boundaries(text, tmp_path, capsys, keep=False)
    assert safe == "[REDACTED_JSON_LIMIT]"

    real_loads = json.loads
    calls = []

    def spy(value):
        calls.append(len(value.encode("utf-8")))
        return real_loads(value)

    monkeypatch.setattr(consent.json, "loads", spy)
    assert redact_sensitive(text) == safe
    assert sum(calls[:8]) == 262094
    assert 8 < len(calls) <= 16
    assert sum(calls) <= 2 * 262144
    previous_calls = len(calls)
    assert redact_sensitive(safe) == safe
    assert len(calls) == previous_calls


def test_output_byte_limit_rejects_before_validation_decode(monkeypatch):
    text = unicode_credential(21000)
    assert len(text.encode("utf-8")) < 65536
    real_loads = json.loads
    calls = []

    def spy(value):
        calls.append(value)
        return real_loads(value)

    monkeypatch.setattr(consent.json, "loads", spy)
    assert redact_sensitive(text) == "[REDACTED_JSON_LIMIT]"
    assert len(calls) == 1


@pytest.mark.parametrize("text", [
    unicode_credential(9000), unicode_credential(5000, 2),
    unicode_credential(3000, 4), unicode_credential(1000, 2, "\ud800"),
], ids=["unicode", "two-wrappers", "four-wrappers", "surrogate"])
def test_supported_reencoded_unicode_is_stable_and_keeps_diagnostics(text):
    safe = redact_sensitive(text)
    assert safe != "[REDACTED_JSON_LIMIT]"
    assert SECRET not in safe
    assert redact_sensitive(safe) == safe


@pytest.mark.parametrize("count", [7, 8, 256, 2800])
def test_many_plain_credentials_do_not_spend_decodes_on_output_markers(count, tmp_path, capsys):
    safe = assert_boundaries(json.dumps(["token=" + SECRET] * count), tmp_path, capsys, keep=False)
    assert json.loads(safe) == ["[REDACTED]"] * count
    assert redact_sensitive(safe) == safe


def test_marker_leaves_at_token_and_depth_boundaries(tmp_path, capsys):
    text = json.dumps(["token=x"] * 4095)
    safe = assert_boundaries(text, tmp_path, capsys, keep=False)
    assert json.loads(safe) == ["[REDACTED]"] * 4095
    nested = "[" * 32 + json.dumps("token=" + SECRET) + "]" * 32
    safe_nested = assert_boundaries(nested, tmp_path, capsys, keep=False)
    assert safe_nested != "[REDACTED_JSON_LIMIT]"
    assert redact_sensitive(safe_nested) == safe_nested


@pytest.mark.parametrize("count", [8, 256, 1600])
def test_marker_plus_diagnostic_suffix_remains_readable_and_stable(count, tmp_path, capsys):
    text = json.dumps(["token=" + SECRET + " diagnosis=keep"] * count)
    safe = assert_boundaries(text, tmp_path, capsys)
    assert json.loads(safe) == ["[REDACTED] diagnosis=keep"] * count
    assert redact_sensitive(safe) == safe


@pytest.mark.parametrize("marker", [
    "[REDACTED]", "[REDACTED_JSON_LIMIT]", "[REDACTED_ENCODED_TEXT]", "[REDACTED_UNTRUSTED_TEXT]",
])
def test_only_exact_known_markers_bypass_json_interpretation(marker, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("fixed marker literals must not spend a JSON decode")

    monkeypatch.setattr(consent.json, "loads", forbidden)
    assert redact_sensitive(marker) == marker


@pytest.mark.parametrize("text", [
    '[REDACTED] token=AUDIT_DUMMY', '[REDACTED_UNTRUSTED_TEXT token=AUDIT_DUMMY]',
    '["[REDACTED]", "token=AUDIT_DUMMY"]',
])
def test_marker_lookalikes_and_marker_plus_credentials_are_still_redacted(text):
    safe = redact_sensitive(text)
    assert SECRET not in safe
    assert redact_sensitive(safe) == safe
