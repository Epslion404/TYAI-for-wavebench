"""advisor 插件类别（RFC「advisor 插件类别」J1a）的离线契约测试。

覆盖 RFC 验收门里的类别契约：未知 capability、缺 purpose、`transmits=False` 却声明
endpoint、api_version 不符，均在加载期拒绝；entry point 名与 advisor_id 不一致被拒；
以及 advisor 类别不影响既有仪器插件路径。
"""

from __future__ import annotations

import pytest

from wavebench.cli import build_parser, main
from wavebench.errors import ConfigError
from wavebench.plugins.api import (
    ADVISOR_EXTERNAL_STATE_CAPABILITY,
    AdvisorPlugin,
    EgressDeclaration,
    PluginLoadError,
)
from wavebench.plugins.builtin import RULE_ADVISOR_ID
from wavebench.plugins.registry import (
    ADVISOR_ENTRY_POINT_GROUP,
    AdvisorRegistry,
    advisor_doctor_records,
    build_advisor_registry,
    build_plugin_registry,
    builtin_advisor_registry,
    has_advisor_doctor_errors,
    validate_advisor,
)

INSTRUMENT_ENTRY_POINT_GROUP = "wavebench.drivers"


class FakeEntryPoint:
    def __init__(self, name, loaded, *, group=ADVISOR_ENTRY_POINT_GROUP):
        self.name = name
        self.group = group
        self._loaded = loaded
        self.dist = None

    def load(self):
        if isinstance(self._loaded, Exception):
            raise self._loaded
        return self._loaded


class FakeEntryPoints(list):
    def select(self, *, group):
        return [entry_point for entry_point in self if entry_point.group == group]


def local_egress(**overrides):
    kwargs = {"transmits_off_machine": False, "purpose": "Local baseline; state stays here."}
    kwargs.update(overrides)
    return EgressDeclaration(**kwargs)


def advisor(**overrides):
    kwargs = {
        "advisor_id": "example.advisor",
        "display_name": "Example advisor",
        "provider": "example",
        "capabilities": ("advisor.route",),
        "summary": "Example advisor.",
        "egress": local_egress(),
    }
    kwargs.update(overrides)
    return AdvisorPlugin(**kwargs)


def transmitting_egress():
    return EgressDeclaration(
        transmits_off_machine=True,
        purpose="Send the whitelisted state to the Jev endpoint.",
        endpoint_hosts=("api.example.com",),
        allowed_state_fields=("run_status",),
    )


def patch_entry_points(monkeypatch, entry_points):
    monkeypatch.setattr(
        "wavebench.plugins.registry.entry_points", lambda: FakeEntryPoints(entry_points)
    )


def test_builtin_advisor_registry_lists_rule_advisor():
    registry = builtin_advisor_registry()

    assert [item.advisor_id for item in registry.list_advisors()] == [RULE_ADVISOR_ID]
    plugin = registry.get(RULE_ADVISOR_ID)
    assert plugin.provider == "wavebench"
    assert plugin.transmits_off_machine is False
    assert plugin.capabilities == ("advisor.route",)
    assert plugin.origin == "builtin"


def test_builtin_advisor_registry_filters_by_capability():
    registry = builtin_advisor_registry()

    assert [item.advisor_id for item in registry.list_advisors(capability="advisor.route")] == [
        RULE_ADVISOR_ID
    ]
    assert registry.list_advisors(capability="advisor.cause") == []


def test_advisor_doctor_reports_builtin_ok():
    records = advisor_doctor_records(builtin_advisor_registry())

    assert records
    assert not has_advisor_doctor_errors(records)
    assert all(record.severity == "ok" for record in records)
    assert records[0].subject == RULE_ADVISOR_ID


def test_unknown_advisor_id_raises_config_error():
    with pytest.raises(ConfigError, match="unknown advisor_id"):
        builtin_advisor_registry().get("missing.advisor")


def test_registry_rejects_duplicate_advisor_ids():
    once = advisor()

    with pytest.raises(ConfigError, match="duplicate advisor_id"):
        AdvisorRegistry((once, once))


def test_advisor_rejects_blank_identity_fields():
    with pytest.raises(ValueError, match="advisor_id must be non-empty"):
        advisor(advisor_id=" example.advisor")
    with pytest.raises(ValueError, match="trimmed display_name"):
        advisor(display_name="  ")
    with pytest.raises(ValueError, match="trimmed provider"):
        advisor(provider="")


def test_advisor_requires_unique_capabilities():
    with pytest.raises(ValueError, match="at least one capability"):
        advisor(capabilities=())
    with pytest.raises(ValueError, match="duplicate capabilities"):
        advisor(capabilities=("advisor.route", "advisor.route"))


def test_egress_requires_a_trimmed_purpose():
    with pytest.raises(ValueError, match="purpose must be non-empty"):
        EgressDeclaration(transmits_off_machine=False, purpose="   ")


def test_transmitting_advisor_must_declare_endpoint_and_fields():
    with pytest.raises(ValueError, match="at least one endpoint host"):
        EgressDeclaration(transmits_off_machine=True, purpose="Send state.")
    with pytest.raises(ValueError, match="allowed_state_fields"):
        EgressDeclaration(
            transmits_off_machine=True,
            purpose="Send state.",
            endpoint_hosts=("api.example.com",),
        )


def test_non_transmitting_advisor_must_not_declare_endpoints():
    with pytest.raises(ValueError, match="must not declare endpoint hosts"):
        EgressDeclaration(
            transmits_off_machine=False,
            purpose="Local only.",
            endpoint_hosts=("api.example.com",),
        )


def test_egress_rejects_malformed_hosts_and_fields():
    with pytest.raises(ValueError, match="invalid endpoint host"):
        EgressDeclaration(
            transmits_off_machine=True,
            purpose="Send state.",
            endpoint_hosts=("https://api.example.com",),
            allowed_state_fields=("run_status",),
        )
    with pytest.raises(ValueError, match="invalid allowed state field"):
        EgressDeclaration(
            transmits_off_machine=True,
            purpose="Send state.",
            endpoint_hosts=("api.example.com",),
            allowed_state_fields=(" run_status",),
        )


def test_validate_advisor_accepts_the_builtin_contract():
    assert validate_advisor(builtin_advisor_registry().get(RULE_ADVISOR_ID)) == []


def test_validate_advisor_rejects_unprefixed_and_unknown_capabilities():
    unprefixed = validate_advisor(advisor(capabilities=("scope.idn",)))
    assert any("must start with 'advisor.' prefix" in message for message in unprefixed)

    unknown = validate_advisor(advisor(capabilities=("advisor.typo",)))
    assert any("unknown advisor capability" in message for message in unknown)


def test_validate_advisor_gates_the_api_version():
    errors = validate_advisor(advisor(api_version="wavebench.advisor.v2"))

    assert any("unsupported api_version" in message for message in errors)


def test_validate_advisor_ties_egress_to_the_external_state_capability():
    missing_declaration = validate_advisor(
        advisor(
            egress=transmitting_egress(),
        )
    )
    assert any(
        ADVISOR_EXTERNAL_STATE_CAPABILITY in message and "must declare" in message
        for message in missing_declaration
    )

    undeclared_egress = validate_advisor(
        advisor(capabilities=("advisor.route", ADVISOR_EXTERNAL_STATE_CAPABILITY))
    )
    assert any("must not declare" in message for message in undeclared_egress)

    consistent = validate_advisor(
        advisor(
            capabilities=("advisor.route", ADVISOR_EXTERNAL_STATE_CAPABILITY),
            egress=transmitting_egress(),
        )
    )
    assert consistent == []


def test_advisor_doctor_reports_invalid_metadata_and_load_errors():
    records = advisor_doctor_records(
        AdvisorRegistry((advisor(capabilities=("advisor.typo",)),)),
        load_errors=(PluginLoadError("entry_point:broken", "boom"),),
    )

    messages = [record.message for record in records]
    assert has_advisor_doctor_errors(records)
    assert any("boom" in message for message in messages)
    assert any("unknown advisor capability" in message for message in messages)


def test_build_advisor_registry_does_not_load_entry_points_by_default(monkeypatch):
    patch_entry_points(
        monkeypatch,
        [FakeEntryPoint("example.advisor", RuntimeError("should not load"))],
    )

    result = build_advisor_registry()

    assert not result.load_errors
    assert [item.advisor_id for item in result.registry.list_advisors()] == [RULE_ADVISOR_ID]


def test_build_advisor_registry_loads_entry_points_when_requested(monkeypatch):
    patch_entry_points(monkeypatch, [FakeEntryPoint("example.advisor", advisor())])

    result = build_advisor_registry(include_entry_points=True)

    plugin = result.registry.get("example.advisor")
    assert plugin.origin == "entry_point"
    assert not result.load_errors


def test_build_advisor_registry_records_bad_entry_point(monkeypatch):
    patch_entry_points(monkeypatch, [FakeEntryPoint("broken", RuntimeError("boom"))])

    result = build_advisor_registry(include_entry_points=True)

    assert result.load_errors[0].source == "entry_point:broken"
    assert "boom" in result.load_errors[0].message


@pytest.mark.parametrize("overrides, diagnostic", [
    ({"api_version": "wavebench.advisor.v2"}, "unsupported api_version"),
    ({"capabilities": ("scope.idn",)}, "must start with"),
    ({"capabilities": ("advisor.typo",)}, "unknown advisor capability"),
    ({"egress": transmitting_egress()}, "must declare"),
    ({"capabilities": ("advisor.route", ADVISOR_EXTERNAL_STATE_CAPABILITY)}, "must not declare"),
])
def test_loader_rejects_invalid_contract_before_registration(monkeypatch, overrides, diagnostic):
    patch_entry_points(monkeypatch, [FakeEntryPoint("example.advisor", advisor(**overrides))])
    result = build_advisor_registry(include_entry_points=True)
    assert [p.advisor_id for p in result.registry.list_advisors()] == [RULE_ADVISOR_ID]
    assert result.load_errors[0].source == "entry_point:example.advisor"
    assert diagnostic in result.load_errors[0].message
    assert has_advisor_doctor_errors(
        advisor_doctor_records(result.registry, load_errors=result.load_errors)
    )


def test_build_advisor_registry_rejects_mismatched_entry_point_name(monkeypatch):
    patch_entry_points(monkeypatch, [FakeEntryPoint("other.advisor", advisor())])

    result = build_advisor_registry(include_entry_points=True)

    assert "does not match advisor_id" in result.load_errors[0].message


def test_build_advisor_registry_records_duplicate_advisor_id(monkeypatch):
    patch_entry_points(
        monkeypatch,
        [FakeEntryPoint(RULE_ADVISOR_ID, advisor(advisor_id=RULE_ADVISOR_ID))],
    )

    result = build_advisor_registry(include_entry_points=True)

    assert result.load_errors[0].source == f"entry_point:{RULE_ADVISOR_ID}"
    assert "duplicate advisor_id" in result.load_errors[0].message


def test_build_advisor_registry_ignores_instrument_entry_points(monkeypatch):
    patch_entry_points(
        monkeypatch,
        [FakeEntryPoint("example.scope", RuntimeError("not an advisor"), group=INSTRUMENT_ENTRY_POINT_GROUP)],
    )

    result = build_advisor_registry(include_entry_points=True)

    assert not result.load_errors
    assert [item.advisor_id for item in result.registry.list_advisors()] == [RULE_ADVISOR_ID]


def test_advisor_category_is_isolated_from_the_instrument_registry():
    instrument = build_plugin_registry().registry
    instrument_ids = [plugin.driver_id for plugin in instrument.list_plugins()]
    advisor_ids = [item.advisor_id for item in build_advisor_registry().registry.list_advisors()]

    assert instrument_ids == [
        "rigol.dm3000",
        "rigol.dp800",
        "rigol.ds1104",
        "rohde-schwarz.rtm2032",
        "rigol.dg4202",
    ]
    assert advisor_ids == [RULE_ADVISOR_ID]
    assert not set(advisor_ids) & set(instrument_ids)


def test_plugin_parser_defaults_to_the_instrument_category():
    parser = build_parser()

    assert parser.parse_args(["plugin", "list"]).category == "instrument"
    assert parser.parse_args(["plugin", "doctor"]).category == "instrument"
    assert parser.parse_args(["plugin", "list", "--category", "advisor"]).category == "advisor"


def test_plugin_list_category_advisor_prints_the_builtin_advisor(capsys):
    assert main(["plugin", "list", "--category", "advisor"]) == 0

    output = capsys.readouterr().out
    assert "advisor_id\tprovider\torigin\ttransmits_off_machine\tcapabilities" in output
    assert RULE_ADVISOR_ID in output
    assert "advisor.route" in output


def test_plugin_doctor_category_advisor_prints_ok(capsys):
    assert main(["plugin", "doctor", "--category", "advisor"]) == 0

    assert capsys.readouterr().out.strip() == f"ok\t{RULE_ADVISOR_ID}\tmetadata valid"


def test_plugin_list_defaults_to_the_instrument_category(capsys):
    assert main(["plugin", "list"]) == 0

    output = capsys.readouterr().out
    assert output.startswith("driver_id\tkind\torigin\tmodels\tcapabilities")
    assert RULE_ADVISOR_ID not in output


def test_plugin_category_rejects_instrument_only_flags(capsys):
    assert main(["plugin", "list", "--category", "advisor", "--kind", "scope"]) == 2
    assert "--kind" in capsys.readouterr().err

    assert main(["plugin", "doctor", "--category", "advisor", "--load"]) == 2
    assert "--load" in capsys.readouterr().err
