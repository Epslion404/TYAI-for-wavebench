"""Untrusted advisor responses fail closed without leaking diagnostic credentials."""

import json

import pytest

from wavebench.cli_output import _print_advisor_result as print_decision_artifact
from wavebench.services.advisor_seam import (
    AdvisorAccess, Answer, DecisionState, RoutingOption, SeamError,
    advise, apply_choice_threshold, build_questions, build_routing_question, parse_answers,
)
from wavebench.services.decision_artifacts import (
    DecisionArtifact, ThresholdPolicy, write_artifact as write_decision_artifact,
)


OPTIONS = (RoutingOption("scope", "Scope help", "wavebench scope --help", False),)
BAD_NUMBERS = [float("nan"), float("inf"), -float("inf"), -0.1, 1.5,
               "oops", "0.9", None, True, [], {}, 10**400]
ESCAPED_CREDENTIALS = [
    json.dumps({"api_key": value})
    for value in ('prefix"AUDIT_DUMMY', 'prefix\\"AUDIT_DUMMY\\tail', 'AUDIT_DUMMY\\')
] + [r"{'token': 'prefix\'AUDIT_DUMMY\\tail'}"]


def response(**overrides):
    entry = {"type": "choice", "choice": "scope", "probabilities": {"scope": 0.9}}
    entry.update(overrides)
    return {"model": "fake.v1", "answers": {"route": entry}}


def evaluate(raw):
    class Client:
        def system_one(self, **kwargs):
            return raw

    return advise(
        model="fake.v1", client=Client(), state=DecisionState(),
        questions=build_questions(build_routing_question(OPTIONS)), options=OPTIONS,
        policy=ThresholdPolicy(),
        access=AdvisorAccess(
            enabled=True, interactive=True, accepted=True,
            registered_endpoint_hosts=("example.invalid",),
            registered_state_fields=("request",),
        ),
    )


@pytest.mark.parametrize("value", BAD_NUMBERS)
def test_invalid_probability_returns_serializable_invalid_artifact(value):
    artifact = evaluate(response(probabilities={"scope": value}))
    assert artifact.status == "invalid"
    assert artifact.answers == {}
    assert artifact.recommendations == ()
    json.dumps(artifact.as_dict(), allow_nan=False)


@pytest.mark.parametrize("value", [v for v in BAD_NUMBERS if v is not None])
def test_invalid_confidence_is_not_silently_ignored(value):
    assert evaluate(response(confidence=value)).status == "invalid"


@pytest.mark.parametrize("overrides", [
    {"probabilities": None}, {"probabilities": []}, {"probabilities": {}},
    {"choice": []}, {"choice": None},
    {"probabilities": {"scope": 0.9, "dmm": 0.1}},
    {"probabilities": {"scope": 0.9, "other": float("nan")}},
])
def test_malformed_choice_or_unselected_probability_fails_closed(overrides):
    assert evaluate(response(**overrides)).status == "invalid"


@pytest.mark.parametrize("value", BAD_NUMBERS)
def test_public_threshold_helper_rejects_invalid_probability(value):
    with pytest.raises(SeamError):
        apply_choice_threshold(
            Answer("route", "choice", "scope", probabilities={"scope": value}),
            OPTIONS, ThresholdPolicy(),
        )


@pytest.mark.parametrize("kind,key,value", [
    ("noul", "noul", float("nan")), ("noul", "noul", 1.5),
    ("score", "score", "oops"), ("score", "score", float("inf")),
])
def test_other_typed_answers_reject_invalid_numbers(kind, key, value):
    with pytest.raises(SeamError):
        parse_answers({"answers": {"other": {"type": kind, key: value}}})


@pytest.mark.parametrize("probability", [0.0, 0.35, 0.6, 1.0])
def test_probability_boundaries_remain_valid(probability):
    artifact = evaluate(response(probabilities={"scope": probability}, confidence=None))
    assert artifact.status == "ok"
    if probability < 0.35:
        assert artifact.recommendations == ()
    elif probability < 0.6:
        assert artifact.recommendations[0]["action"] == "human_review"
    else:
        assert artifact.recommendations[0]["command"] == "wavebench scope --help"


@pytest.mark.parametrize("valid", [True, False])
@pytest.mark.parametrize("model", [
    "fake api_key=AUDIT_DUMMY", 'fake {"api_key": "AUDIT_DUMMY extra"}',
    "fake {'token': 'AUDIT_DUMMY extra'}",
    *("fake " + text + " diagnosis=keep" for text in ESCAPED_CREDENTIALS),
])
def test_response_diagnostics_are_safe_in_cli_and_persisted_artifact(valid, model, tmp_path, capsys):
    raw = response(choice="scope" if valid else "token=AUDIT_DUMMY")
    if not valid:
        raw["answers"]["route"]["probabilities"] = {"token=AUDIT_DUMMY": 0.9}
    raw["model"] = model
    artifact = evaluate(raw)
    assert artifact.status == ("ok" if valid else "invalid")
    print_decision_artifact(artifact)
    path = write_decision_artifact(artifact, run_dir=tmp_path, advisor_id="fake")
    assert "AUDIT_DUMMY" not in capsys.readouterr().out
    assert "AUDIT_DUMMY" not in path.read_text(encoding="utf-8")
    assert "AUDIT_DUMMY" not in str(artifact)
    assert "AUDIT_DUMMY" not in artifact.reported_model
    if "diagnosis=keep" in model:
        assert "diagnosis=keep" in artifact.reported_model


@pytest.mark.parametrize("credential", [
    "token=AUDIT_DUMMY", "api_key=AUDIT_DUMMY", "password=AUDIT_DUMMY",
    "authorization: Bearer AUDIT_DUMMY", *ESCAPED_CREDENTIALS,
])
def test_direct_artifact_diagnostics_are_redacted(credential, tmp_path, capsys):
    artifact = DecisionArtifact(
        status="invalid", requested_model=f"fake {credential} diagnosis=keep",
        reported_model=f"fake {credential} diagnosis=keep",
        reason=f"failure {credential} diagnosis=keep",
    )
    for diagnostic in (artifact.requested_model, artifact.reported_model, artifact.reason):
        assert "AUDIT_DUMMY" not in diagnostic
        assert "diagnosis=keep" in diagnostic
    print_decision_artifact(artifact)
    output = capsys.readouterr().out
    assert "AUDIT_DUMMY" not in output
    assert "diagnosis=keep" in output
    assert "AUDIT_DUMMY" not in artifact.to_json()
    path = write_decision_artifact(artifact, run_dir=tmp_path, advisor_id="fake")
    assert "AUDIT_DUMMY" not in path.read_text(encoding="utf-8")


def test_normal_artifact_diagnostics_are_preserved(capsys):
    artifact = DecisionArtifact(
        status="invalid", requested_model="fake.v1", reported_model="fake.v2",
        reason="invalid_advisor_response: answer probability must be within 0..1",
    )
    assert artifact.requested_model == "fake.v1"
    assert artifact.reported_model == "fake.v2"
    assert artifact.reason == "invalid_advisor_response: answer probability must be within 0..1"
    print_decision_artifact(artifact)
    output = capsys.readouterr().out
    assert "fake.v1" in output and "fake.v2" in output
    assert artifact.reason in output
