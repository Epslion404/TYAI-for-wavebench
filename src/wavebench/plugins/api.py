from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

PluginKind = Literal["scope", "source", "rf_source", "power", "dmm", "sweep_analyzer"]
PluginOrigin = Literal["builtin", "entry_point", "local"]
DiagnosticSeverity = Literal["ok", "warning", "error"]

SUPPORTED_PLUGIN_API_VERSION = "wavebench.instrument.v1"
VALID_PLUGIN_KINDS: tuple[str, ...] = (
    "scope",
    "source",
    "rf_source",
    "power",
    "dmm",
    "sweep_analyzer",
)


@dataclass(frozen=True)
class InstrumentPlugin:
    driver_id: str
    kind: PluginKind
    display_name: str
    manufacturer: str
    models: tuple[str, ...]
    capabilities: tuple[str, ...]
    summary: str
    api_version: str = SUPPORTED_PLUGIN_API_VERSION
    package: str = "wavebench"
    origin: PluginOrigin = "builtin"
    idn_patterns: tuple[str, ...] = field(default_factory=tuple)
    config_fields: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not self.driver_id or self.driver_id.strip() != self.driver_id:
            raise ValueError("plugin driver_id must be non-empty and trimmed")
        if not self.capabilities:
            raise ValueError(f"plugin {self.driver_id!r} must declare at least one capability")
        if not self.models:
            raise ValueError(f"plugin {self.driver_id!r} must declare at least one model")

    @property
    def model_text(self) -> str:
        return ", ".join(self.models)

    @property
    def capability_text(self) -> str:
        return ", ".join(self.capabilities)


SUPPORTED_ADVISOR_API_VERSION = "wavebench.advisor.v1"
ADVISOR_CAPABILITY_PREFIX = "advisor."
ADVISOR_EXTERNAL_STATE_CAPABILITY = "advisor.external_state"
VALID_ADVISOR_CAPABILITIES: tuple[str, ...] = (
    "advisor.route",
    "advisor.triage",
    "advisor.cause",
    "advisor.rank",
    ADVISOR_EXTERNAL_STATE_CAPABILITY,
)


@dataclass(frozen=True)
class EgressDeclaration:
    """advisor 自行声明"数据会不会离开本机"。

    同意门归 Core；插件只能声明事实，不能自己判定是否已获得授权。
    """

    transmits_off_machine: bool
    purpose: str
    endpoint_hosts: tuple[str, ...] = ()
    allowed_state_fields: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.purpose or self.purpose.strip() != self.purpose:
            raise ValueError("egress purpose must be non-empty and trimmed")
        if self.transmits_off_machine:
            if not self.endpoint_hosts:
                raise ValueError("a transmitting advisor must declare at least one endpoint host")
            if not self.allowed_state_fields:
                raise ValueError("a transmitting advisor must declare allowed_state_fields")
        elif self.endpoint_hosts:
            raise ValueError("a non-transmitting advisor must not declare endpoint hosts")
        for host in self.endpoint_hosts:
            if not host or host.strip() != host or "/" in host:
                raise ValueError(f"invalid endpoint host: {host!r}")
        for name in self.allowed_state_fields:
            if not name or name.strip() != name:
                raise ValueError(f"invalid allowed state field: {name!r}")


@dataclass(frozen=True)
class AdvisorPlugin:
    """advisor 插件的元数据与契约；实现由 client 适配层提供。

    与仪器插件的差别是刻意的：没有 kind、没有型号、没有 SCPI，capability 走
    `advisor.` 前缀，且必须自报 egress 事实。校验分工同仪器侧——构造期只做结构性
    校验，capability 白名单与 egress 一致性由 `registry.validate_advisor` 在加载期判定。
    """

    advisor_id: str
    display_name: str
    provider: str
    capabilities: tuple[str, ...]
    summary: str
    egress: EgressDeclaration
    api_version: str = SUPPORTED_ADVISOR_API_VERSION
    package: str = "wavebench"
    origin: PluginOrigin = "builtin"

    def __post_init__(self) -> None:
        if not self.advisor_id or self.advisor_id.strip() != self.advisor_id:
            raise ValueError("advisor_id must be non-empty and trimmed")
        for label, value in (("display_name", self.display_name), ("provider", self.provider)):
            if not value or value.strip() != value:
                raise ValueError(f"advisor {self.advisor_id!r} must declare a trimmed {label}")
        if not self.capabilities:
            raise ValueError(f"advisor {self.advisor_id!r} must declare at least one capability")
        if len(set(self.capabilities)) != len(self.capabilities):
            raise ValueError(f"advisor {self.advisor_id!r} has duplicate capabilities")

    @property
    def capability_text(self) -> str:
        return ", ".join(self.capabilities)

    @property
    def transmits_off_machine(self) -> bool:
        return self.egress.transmits_off_machine


@dataclass(frozen=True)
class PluginLoadError:
    source: str
    message: str


@dataclass(frozen=True)
class PluginDoctorRecord:
    severity: DiagnosticSeverity
    subject: str
    message: str
