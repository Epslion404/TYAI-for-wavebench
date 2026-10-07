"""advisor 同意门的离线契约测试（无网络、无 API key、无第三方 SDK）。"""

from __future__ import annotations

import ast
import json
import unittest
from pathlib import Path

from wavebench.services import advisor_consent as consent

MODULE_PATH = Path(consent.__file__)
ALLOWED_IMPORTS = {"__future__", "dataclasses", "hashlib", "json", "re", "typing"}

STATE = {"fields": {"run_status": "failed", "cycles_bucket": "few_cycles"}}
QUESTIONS = {"route": {"kind": "choice", "options": ["run check", "run verify"]}}


class AdvisorConsentTests(unittest.TestCase):
    def _preview(self, *, state=None, questions=None):
        return consent.build_preview(
            model="jev-1.13.0",
            state=state if state is not None else STATE,
            questions=questions if questions is not None else QUESTIONS,
        )

    def _resolve(self, *, preview=None, **overrides):
        kwargs = {
            "preview": preview if preview is not None else self._preview(),
            "endpoint_hosts": ("api.typesafe.ai",),
            "state_fields": ("run_status", "cycles_bucket"),
            "enabled": True,
            "registered_endpoint_hosts": ("api.typesafe.ai",),
            "registered_state_fields": ("run_status", "cycles_bucket"),
            "interactive": True,
            "accepted": True,
        }
        kwargs.update(overrides)
        return consent.resolve_consent(**kwargs)

    def test_preview_is_deterministic_and_counts_untrusted_sources(self):
        state = {
            "fields": {"run_status": "failed"},
            "untrusted": [
                {"source": "instrument.response", "text": "IDN: ..."},
                {"source": "operator.utterance", "text": "信号不对"},
                {"source": "instrument.response", "text": "dup"},
            ],
        }
        first = self._preview(state=state)
        second = self._preview(state=state)

        self.assertEqual(first.payload_sha256, second.payload_sha256)
        self.assertEqual(first.payload_bytes, len(json.dumps(first.payload, ensure_ascii=False,
                                                             sort_keys=True).encode("utf-8")))
        self.assertEqual(first.untrusted_sources, ("instrument.response", "operator.utterance"))

    def test_preview_changes_when_payload_changes(self):
        self.assertNotEqual(
            self._preview().payload_sha256,
            self._preview(state={"fields": {"run_status": "ok"}}).payload_sha256,
        )

    def test_disabled_gate_refuses(self):
        outcome = self._resolve(enabled=False)

        self.assertEqual(outcome.status, consent.STATUS_REFUSED)
        self.assertEqual(outcome.reason, consent.REASON_DISABLED)
        self.assertIsNone(outcome.decision)
        self.assertFalse(outcome.granted)

    def test_unregistered_state_field_refuses_with_field_name(self):
        outcome = self._resolve(state_fields=("run_status", "cycles_bucket", "raw_voltage_v"))

        self.assertEqual(outcome.reason, "state_field_not_registered: raw_voltage_v")
        self.assertIsNone(outcome.decision)

    def test_unregistered_endpoint_refuses_with_host_name(self):
        outcome = self._resolve(endpoint_hosts=("api.typesafe.ai", "evil.example"))

        self.assertEqual(outcome.reason, "endpoint_not_registered: evil.example")
        self.assertIsNone(outcome.decision)

    def test_preview_only_never_grants_consent(self):
        outcome = self._resolve(preview_only=True, accepted=True)

        self.assertEqual(outcome.status, consent.STATUS_PREVIEW_ONLY)
        self.assertEqual(outcome.reason, consent.REASON_PREVIEW_ONLY)
        self.assertIsNone(outcome.decision)
        self.assertFalse(outcome.granted)

    def test_missing_confirmation_refuses(self):
        outcome = self._resolve(accepted=False)

        self.assertEqual(outcome.reason, consent.REASON_NOT_GRANTED)
        self.assertIsNone(outcome.decision)

    def test_public_gate_fails_closed_without_registered_allowlists(self):
        outcome = self._resolve(
            registered_endpoint_hosts=(),
            registered_state_fields=(),
            endpoint_hosts=("evil.example",),
            state_fields=("run_status", "secret_field"),
        )

        self.assertEqual(outcome.status, consent.STATUS_REFUSED)
        self.assertEqual(outcome.reason, consent.REASON_ALLOWLIST_MISSING)
        self.assertFalse(outcome.granted)

    def test_non_interactive_and_invalid_scope_have_distinct_reasons(self):
        non_interactive = self._resolve(interactive=False)
        invalid_scope = self._resolve(valid_for="forever")

        self.assertEqual(non_interactive.reason, consent.REASON_NON_INTERACTIVE)
        self.assertEqual(invalid_scope.reason, consent.REASON_INVALID_SCOPE)

    def test_preview_is_immutable_and_redacts_sensitive_values(self):
        preview = self._preview(
            state={
                "apiKey": "not-for-preview",
                "nested": [{"API_KEY": "hidden"}, {"api-key": "also-hidden"}],
                "untrusted": [{"source": "instrument.response", "text": "token=raw-value"}],
            },
            questions={"prompt": "authorization: Bearer secret-value"},
        )

        self.assertEqual(preview.payload["state"]["apiKey"], "[REDACTED]")
        self.assertEqual(preview.payload["state"]["nested"][0]["API_KEY"], "[REDACTED]")
        self.assertEqual(preview.payload["state"]["nested"][1]["api-key"], "[REDACTED]")
        self.assertEqual(preview.payload["state"]["untrusted"][0]["text"], "[REDACTED_UNTRUSTED_TEXT]")
        with self.assertRaises(TypeError):
            preview.payload["state"]["apiKey"] = "changed"
        serialized = json.dumps(preview.as_dict(), ensure_ascii=False)
        self.assertNotIn("not-for-preview", serialized)
        self.assertNotIn("raw-value", serialized)
        self.assertNotIn("secret-value", serialized)

    def test_preview_only_is_available_without_interaction_or_allowlists(self):
        outcome = self._resolve(
            interactive=False,
            registered_endpoint_hosts=(),
            registered_state_fields=(),
            preview_only=True,
        )

        self.assertEqual(outcome.status, consent.STATUS_PREVIEW_ONLY)
        self.assertEqual(outcome.reason, consent.REASON_PREVIEW_ONLY)
        self.assertFalse(outcome.granted)

    def test_structured_sensitive_fields_keep_adjacent_non_sensitive_values(self):
        preview = self._preview(state={
            "fields": {"request": "diagnosis=keep"},
            "nested": {"password": "prefix,AUDIT_DUMMY", "api_key": "prefix}AUDIT_DUMMY",
                       "diagnosis": "keep", "count": 2},
        })
        serialized = json.dumps(preview.as_dict(), ensure_ascii=False)
        self.assertNotIn("AUDIT_DUMMY", serialized)
        self.assertEqual(preview.payload["state"]["nested"]["password"], "[REDACTED]")
        self.assertEqual(preview.payload["state"]["nested"]["api_key"], "[REDACTED]")
        self.assertEqual(preview.payload["state"]["nested"]["diagnosis"], "keep")
        self.assertEqual(preview.payload["state"]["nested"]["count"], 2)

    def test_quoted_credentials_consume_punctuation_without_hiding_following_text(self):
        for text in ('password="prefix,AUDIT_DUMMY" diagnosis=keep',
                     "api_key='prefix}AUDIT_DUMMY' diagnosis=keep"):
            with self.subTest(text=text):
                preview = self._preview(state={"fields": {"request": text}})
                request = preview.payload["state"]["fields"]["request"]
                self.assertEqual(request, "[REDACTED] diagnosis=keep")

    def test_granted_consent_records_scope_and_allowlists(self):
        outcome = self._resolve(granted_by="operator", granted_at="2026-09-29T10:00:00Z")

        self.assertTrue(outcome.granted)
        self.assertIsNotNone(outcome.decision)
        assert outcome.decision is not None
        self.assertEqual(outcome.decision.valid_for, "invocation")
        self.assertIsNone(outcome.decision.run_id)
        self.assertEqual(outcome.decision.endpoint_hosts, ("api.typesafe.ai",))
        self.assertEqual(
            outcome.decision.allowed_state_fields, ("cycles_bucket", "run_status")
        )
        self.assertEqual(outcome.as_payload()["granted_by"], "operator")

    def test_run_scoped_consent_requires_run_target(self):
        outcome = self._resolve(valid_for="run", run_id=None)

        self.assertEqual(outcome.reason, consent.REASON_RUN_REQUIRED)
        self.assertIsNone(outcome.decision)

    def test_run_scoped_consent_is_bound_to_run_id(self):
        granted = self._resolve(valid_for="run", run_id="20260929_0740_loop_gain")
        assert granted.decision is not None
        self.assertEqual(granted.decision.run_id, "20260929_0740_loop_gain")

        covered, reason = consent.consent_covers(
            granted.decision,
            endpoint_hosts=("api.typesafe.ai",),
            allowed_state_fields=("cycles_bucket", "run_status"),
            run_id="20260929_0800_other_run",
            interactive=True,
        )
        self.assertFalse(covered)
        self.assertEqual(reason, consent.REASON_RUN_CHANGED)

    def test_consent_invalidated_by_endpoint_or_field_change(self):
        granted = self._resolve()
        assert granted.decision is not None

        covered, reason = consent.consent_covers(
            granted.decision,
            endpoint_hosts=("api.typesafe.ai", "cdn.typesafe.ai"),
            allowed_state_fields=("cycles_bucket", "run_status"),
            run_id=None,
            interactive=True,
        )
        self.assertFalse(covered)
        self.assertEqual(reason, consent.REASON_ENDPOINT_CHANGED)

        covered, reason = consent.consent_covers(
            granted.decision,
            endpoint_hosts=("api.typesafe.ai",),
            allowed_state_fields=("run_status",),
            run_id=None,
            interactive=True,
        )
        self.assertFalse(covered)
        self.assertEqual(reason, consent.REASON_FIELD_CHANGED)

    def test_invocation_consent_covers_repeated_calls(self):
        granted = self._resolve()
        assert granted.decision is not None

        covered, reason = consent.consent_covers(
            granted.decision,
            endpoint_hosts=("api.typesafe.ai",),
            allowed_state_fields=("cycles_bucket", "run_status"),
            run_id=None,
            interactive=True,
        )
        self.assertTrue(covered)
        self.assertIsNone(reason)

    def test_ungranted_decision_is_not_reusable(self):
        decision = consent.ConsentDecision(granted=False, endpoint_hosts=("api.typesafe.ai",))

        covered, reason = consent.consent_covers(
            decision,
            endpoint_hosts=("api.typesafe.ai",),
            allowed_state_fields=(),
            run_id=None,
            interactive=True,
        )
        self.assertFalse(covered)
        self.assertEqual(reason, consent.REASON_NOT_GRANTED)

    def test_run_scoped_consent_cannot_be_reused_non_interactively(self):
        granted = self._resolve(valid_for="run", run_id="20261001_run")
        assert granted.decision is not None

        covered, reason = consent.consent_covers(
            granted.decision,
            endpoint_hosts=("api.typesafe.ai",),
            allowed_state_fields=("cycles_bucket", "run_status"),
            run_id="20261001_run",
            interactive=False,
        )

        self.assertFalse(covered)
        self.assertEqual(reason, consent.REASON_NON_INTERACTIVE)

    def test_consent_record_contains_required_keys_without_secrets(self):
        record = self._resolve().as_payload()

        for key in (
            "granted",
            "valid_for",
            "run_id",
            "granted_by",
            "granted_at",
            "endpoint_hosts",
            "allowed_state_fields",
            "payload_sha256",
            "payload_bytes",
            "accepted_data_leaves_machine",
        ):
            self.assertIn(key, record)
        self.assertNotIn("api_key", json.dumps(record).lower())

    def test_module_has_no_network_or_third_party_imports(self):
        tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])

        self.assertTrue(
            imported <= ALLOWED_IMPORTS,
            f"unexpected imports in advisor_consent: {sorted(imported - ALLOWED_IMPORTS)}",
        )
        for banned in ("socket", "urllib", "http", "requests", "httpx", "typesafe_sdk"):
            self.assertNotIn(banned, imported)


if __name__ == "__main__":
    unittest.main()
