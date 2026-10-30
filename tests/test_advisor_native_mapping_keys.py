"""Native Python mapping keys are untrusted text at every advisor boundary."""

import hashlib
import json

import pytest

from wavebench.cli_output import _print_advisor_result
from wavebench.services.advisor_consent import build_preview, redact_sensitive
from wavebench.services.advisor_seam import (
    AdvisorAccess,
    DecisionState,
    QuestionSpec,
    RoutingOption,
    advise,
    build_questions,
    build_routing_question,
)
from wavebench.services.decision_artifacts import ThresholdPolicy, write_artifact

OPTIONS = (RoutingOption("scope", "Scope help", "wavebench scope --help", False),)
SECRET = "AUDIT_DUMMY"


def _questions():
    return build_questions(build_routing_question(OPTIONS))


def _preview(state):
    questions = _questions()
    return build_preview(
        model="fake.v1",
        state=state.as_payload(),
        questions={key: question.as_payload() for key, question in questions.items()},
    )


class _Client:
    def __init__(self):
        self.calls = []

    def system_one(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "model": "fake.v1",
            "answers": {
                "route": {
                    "type": "choice",
                    "choice": "scope",
                    "probabilities": {"scope": 0.9},
                }
            },
        }


def _advise(state, client):
    return advise(
        model="fake.v1",
        client=client,
        state=state,
        questions=_questions(),
        options=OPTIONS,
        policy=ThresholdPolicy(),
        access=AdvisorAccess(
            enabled=True,
            interactive=True,
            accepted=True,
            registered_endpoint_hosts=("example.invalid",),
            registered_state_fields=tuple(state.fields),
        ),
    )


def test_native_mapping_key_is_redacted_with_its_value():
    safe = redact_sensitive({"token=AUDIT_DUMMY": "private value"})

    assert safe == {"[REDACTED]": "[REDACTED]"}
    assert SECRET not in str(safe)
    assert redact_sensitive(safe) == safe


@pytest.mark.parametrize("key", [
    "password=prefix,AUDIT_DUMMY",
    'authorization: Bearer "prefix AUDIT_DUMMY"',
    "note token=AUDIT_DUMMY diagnosis=keep",
    "雪 token=AUDIT_DUMMY",
])
def test_native_key_text_uses_existing_redaction_rules(key):
    safe = redact_sensitive({key: "private value", "diagnosis": "keep"})

    assert SECRET not in str(safe)
    assert "keep" in str(safe)
    assert redact_sensitive(safe) == safe


def test_native_keys_are_redacted_recursively_in_nested_objects_and_arrays():
    value = {
        "params": [
            {"token=AUDIT_DUMMY": "first", "nested": {"api_key=AUDIT_DUMMY": ["x"]}},
            {"plain": {"password=AUDIT_DUMMY": "second"}, "diagnosis": "keep"},
        ]
    }

    safe = redact_sensitive(value)

    assert SECRET not in str(safe)
    assert safe["params"][0] == {
        "[REDACTED]": "[REDACTED]",
        "nested": {"[REDACTED]": "[REDACTED]"},
    }
    assert safe["params"][1] == {
        "plain": {"[REDACTED]": "[REDACTED]"},
        "diagnosis": "keep",
    }
    assert redact_sensitive(safe) == safe


@pytest.mark.parametrize("value", [
    {"token=AUDIT_DUMMY": "first", "password=another": "second", "diagnosis": "keep"},
    {"[REDACTED]": "keep", "password=AUDIT_DUMMY": "secret"},
    {1: "first", "1": "second"},
], ids=["two-credentials", "existing-marker", "str-collision"])
def test_colliding_native_keys_replace_that_mapping_explicitly(value):
    safe = redact_sensitive(value)

    assert safe == {"[REDACTED_KEY_COLLISION]": "[REDACTED_ENCODED_TEXT]"}
    assert redact_sensitive(safe) == safe


def test_nested_native_key_collision_keeps_sibling_fields():
    safe = redact_sensitive(
        {
            "outer": {"token=AUDIT_DUMMY": "first", "password=another": "second"},
            "diagnosis": "keep",
        }
    )

    assert safe == {
        "outer": {"[REDACTED_KEY_COLLISION]": "[REDACTED_ENCODED_TEXT]"},
        "diagnosis": "keep",
    }
    assert redact_sensitive(safe) == safe


def test_native_key_that_is_encoded_json_uses_the_bounded_text_contract():
    key = json.dumps({"token=AUDIT_DUMMY": "private"})
    safe = redact_sensitive({key: "value", "diagnosis": "keep"})

    assert SECRET not in str(safe)
    assert json.loads(next(iter(safe))) == {"[REDACTED]": "[REDACTED]"}
    assert redact_sensitive(safe) == safe


def test_oversized_json_native_key_fails_closed_to_the_text_limit_marker():
    key = json.dumps({"token": "x" * 70000})
    safe = redact_sensitive({key: "value"})

    assert safe == {"[REDACTED_JSON_LIMIT]": "[REDACTED]"}
    assert redact_sensitive(safe) == safe


def test_normal_native_fields_are_unchanged():
    value = {"count": 1, "ready": True, "missing": None, "plain": [1, True]}

    assert redact_sensitive(value) == value
    assert redact_sensitive(value) is not value


def test_native_mapping_keys_cross_real_boundaries(tmp_path, capsys):
    state = DecisionState(
        fields={
            "params": {
                "token=AUDIT_DUMMY": "private value",
                "nested": {"password=AUDIT_DUMMY": "another", "diagnosis": "keep"},
                "count": 2,
            }
        }
    )
    preview = _preview(state)
    safe_params = preview.payload["state"]["fields"]["params"]
    assert safe_params["[REDACTED]"] == "[REDACTED]"
    assert safe_params["nested"] == {"[REDACTED]": "[REDACTED]", "diagnosis": "keep"}
    assert safe_params["count"] == 2

    client = _Client()
    artifact = _advise(state, client)
    assert artifact.status == "ok"
    assert len(client.calls) == 1
    assert client.calls[0]["state"] == preview.payload["state"] == artifact.state

    wire_state = json.loads(json.dumps(client.calls[0]["state"]))
    assert artifact.as_dict()["state"] == json.loads(artifact.to_json())["state"] == wire_state
    wire_body = json.dumps(preview.payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    assert preview.payload_bytes == len(wire_body)
    assert preview.payload_sha256 == hashlib.sha256(wire_body).hexdigest()

    boundaries = {
        "preview": json.dumps(preview.as_dict()),
        "client": json.dumps(client.calls),
        "artifact": artifact.to_json(),
    }
    path = write_artifact(artifact, run_dir=tmp_path, advisor_id="fake")
    assert path is not None
    boundaries["artifact_file"] = path.read_text(encoding="utf-8")
    _print_advisor_result(artifact)
    boundaries["cli"] = capsys.readouterr().out

    assert not [name for name, text in boundaries.items() if SECRET in text]
    assert all("keep" in boundaries[name] for name in ("preview", "client", "artifact", "artifact_file"))


def test_native_key_collision_is_explicit_across_preview_client_and_artifact(tmp_path, capsys):
    state = DecisionState(
        fields={
            "params": {
                "token=AUDIT_DUMMY": "first",
                "password=another": "second",
                "diagnosis": "keep",
            }
        }
    )
    preview = _preview(state)
    assert preview.payload["state"]["fields"]["params"] == {
        "[REDACTED_KEY_COLLISION]": "[REDACTED_ENCODED_TEXT]",
    }

    client = _Client()
    artifact = _advise(state, client)
    assert artifact.status == "ok"
    assert client.calls[0]["state"] == preview.payload["state"] == artifact.state

    boundaries = {
        "preview": json.dumps(preview.as_dict()),
        "client": json.dumps(client.calls),
        "artifact": artifact.to_json(),
    }
    path = write_artifact(artifact, run_dir=tmp_path, advisor_id="fake")
    assert path is not None
    boundaries["artifact_file"] = path.read_text(encoding="utf-8")
    _print_advisor_result(artifact)
    boundaries["cli"] = capsys.readouterr().out

    assert not [name for name, text in boundaries.items() if SECRET in text]
    assert all(
        "[REDACTED_ENCODED_TEXT]" in boundaries[name]
        for name in ("preview", "client", "artifact", "artifact_file")
    )


@pytest.mark.parametrize("preview_only", [True, False], ids=["preview", "granted"])
def test_colliding_question_ids_still_produce_an_artifact(preview_only, tmp_path):
    # These are legitimate question ids, not credentials. Both hit the bounded
    # encoded-text marker, so the collision must stay a mapping instead of
    # turning `questions` into a string that `dict(...)` cannot consume.
    questions = {
        "a\\u0041": QuestionSpec("choice", "pick", ("scope",)),
        "b\\u0042": QuestionSpec("choice", "pick", ("scope",)),
    }
    state = DecisionState(fields={"request": "keep"})
    client = None if preview_only else _Client()
    artifact = advise(
        model="fake.v1",
        client=client,
        state=state,
        questions=questions,
        options=OPTIONS,
        policy=ThresholdPolicy(),
        access=AdvisorAccess(
            enabled=True,
            interactive=not preview_only,
            preview_only=preview_only,
            accepted=not preview_only,
            registered_endpoint_hosts=("example.invalid",),
            registered_state_fields=("request",),
        ),
    )

    marker = {"[REDACTED_KEY_COLLISION]": "[REDACTED_ENCODED_TEXT]"}
    assert artifact.questions == marker
    assert artifact.status == ("preview_only" if preview_only else "ok")
    if client is not None:
        assert client.calls[0]["questions"] == marker
    assert json.loads(artifact.to_json())["questions"] == marker

    path = write_artifact(artifact, run_dir=tmp_path, advisor_id="fake")
    assert path is not None
    assert "[REDACTED_KEY_COLLISION]" in path.read_text(encoding="utf-8")
