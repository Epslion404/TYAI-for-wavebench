"""Credential redaction through competition's existing offline advisor chain."""

import json

import pytest

from wavebench.services.advisor_consent import build_preview
from wavebench.services.advisor_seam import (
    AdvisorAccess,
    DecisionState,
    RoutingOption,
    UntrustedSpan,
    advise,
    build_questions,
    build_routing_question,
)
from wavebench.services.decision_artifacts import ThresholdPolicy, write_artifact


OPTIONS = (RoutingOption("check", "Check the plan", "run check", False),)
QUOTED_CREDENTIALS = [
    'authorization: Bearer "prefix AUDIT_DUMMY"',
    "authorization: Bearer 'prefix AUDIT_DUMMY'",
    'AUTHORIZATION\t=\t bearer\t"prefix AUDIT_DUMMY"',
    r'authorization: Bearer "prefix\" AUDIT_DUMMY\\tail"',
    r"authorization: Bearer 'prefix\' AUDIT_DUMMY\\tail'",
    r'api_key="prefix\" AUDIT_DUMMY\\tail"',
    r"token='prefix\' AUDIT_DUMMY\\tail'",
    'api_key="prefix AUDIT_DUMMY"',
    "password='prefix AUDIT_DUMMY'",
    'authorization:\nBearer\t"prefix AUDIT_DUMMY"',
]
STRUCTURED_CREDENTIALS = [
    json.dumps({"authorization": 'Bearer "prefix AUDIT_DUMMY"'}),
    json.dumps({"authorization": 'Bearer "prefix\\\" AUDIT_DUMMY\\tail"'}),
    json.dumps({"api_key": "prefix AUDIT_DUMMY"}),
]
UNQUOTED_CREDENTIALS = [
    "api_key=prefix,AUDIT_DUMMY",
    "api_key=prefix}AUDIT_DUMMY",
    "authorization: Bearer prefix,AUDIT_DUMMY",
]


@pytest.mark.parametrize("credential", QUOTED_CREDENTIALS + UNQUOTED_CREDENTIALS)
def test_complete_credentials_are_redacted_before_client_and_persistence(credential, tmp_path):
    assert_boundaries(credential + " diagnosis=keep", tmp_path, "[REDACTED] diagnosis=keep")


@pytest.mark.parametrize("credential", STRUCTURED_CREDENTIALS)
def test_structured_quoted_credentials_do_not_cross_boundaries(credential, tmp_path):
    assert_boundaries(credential + " diagnosis=keep", tmp_path)


@pytest.mark.parametrize("input_text,expected", [
    ('authorization: Bearer "prefix AUDIT_DUMMY', "[REDACTED]"),
    ("authorization: Bearer 'prefix AUDIT_DUMMY diagnosis=keep", "[REDACTED]"),
    (r'authorization: Bearer "prefix AUDIT_DUMMY\"', "[REDACTED]"),
    ('authorization: Bearer "prefix AUDIT_DUMMY\\', "[REDACTED]"),
    ('authorization: Bearer "prefix AUDIT_DUMMY\ndiagnosis=keep',
     "[REDACTED]\ndiagnosis=keep"),
    ("authorization: bearer 'prefix AUDIT_DUMMY\r\ndiagnosis=keep",
     "[REDACTED]\r\ndiagnosis=keep"),
    ('authorization: Bearer "prefix AUDIT_DUMMY\ndiagnosis=keep"',
     '[REDACTED]\ndiagnosis=keep"'),
    ('authorization: Bearer "prefix AUDIT_DUMMY"\ndiagnosis=keep',
     "[REDACTED]\ndiagnosis=keep"),
])
def test_unterminated_quote_fails_closed_at_current_line(input_text, expected, tmp_path):
    assert_boundaries(input_text, tmp_path, expected)


def assert_boundaries(request, tmp_path, expected=None):
    state = DecisionState(
        fields={"request": request},
        untrusted=(UntrustedSpan("instrument.response", "raw AUDIT_DUMMY"),),
    )
    questions = build_questions(build_routing_question(OPTIONS))
    preview = build_preview(
        model="fake.v1", state=state.as_payload(),
        questions={qid: spec.as_payload() for qid, spec in questions.items()},
    )
    calls = []

    class FakeClient:
        model_id = "fake.v1"

        def system_one(self, **kwargs):
            calls.append(kwargs)
            return {
                "model": self.model_id,
                "answers": {"route": {
                    "type": "choice", "choice": "check", "probabilities": {"check": 1.0},
                }},
            }

    artifact = advise(
        model="fake.v1", client=FakeClient(), state=state,
        questions=questions, options=OPTIONS, policy=ThresholdPolicy(),
        access=AdvisorAccess(
            enabled=True, interactive=True, accepted=True,
            endpoint_hosts=("fake.example",), registered_endpoint_hosts=("fake.example",),
            declared_state_fields=("request",), registered_state_fields=("request",),
        ),
    )
    assert artifact.status == "ok"
    assert len(calls) == 1
    path = write_artifact(artifact, tmp_path, advisor_id="fake")
    assert path is not None
    persisted = json.loads(path.read_text(encoding="utf-8"))
    boundaries = {
        "preview": json.dumps(preview.as_dict(), allow_nan=False),
        "client": json.dumps(calls, allow_nan=False),
        "artifact_state": json.dumps(artifact.state, allow_nan=False),
        "artifact_json": artifact.to_json(),
        "persisted": path.read_text(encoding="utf-8"),
    }
    for name, text in boundaries.items():
        assert "AUDIT_DUMMY" not in text, f"credential fragment crossed {name}"
        if expected is None or "diagnosis=keep" in expected:
            assert "diagnosis=keep" in text, f"diagnostic lost at {name}"
    safe_state = json.loads(json.dumps(preview.payload["state"]))
    assert json.loads(json.dumps(calls[0]["state"])) == safe_state
    assert json.loads(json.dumps(artifact.state)) == safe_state
    assert persisted["state"] == safe_state
    assert safe_state["untrusted"][0]["text"] == "[REDACTED_UNTRUSTED_TEXT]"
    if expected is not None:
        assert safe_state["fields"]["request"] == expected
