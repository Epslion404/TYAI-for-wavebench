from __future__ import annotations

from dataclasses import dataclass
from dataclasses import replace
from importlib.metadata import EntryPoint, entry_points
import re
from typing import Any

from wavebench.errors import ConfigError

from .api import (
    ADVISOR_CAPABILITY_PREFIX,
    ADVISOR_EXTERNAL_STATE_CAPABILITY,
    SUPPORTED_ADVISOR_API_VERSION,
    SUPPORTED_PLUGIN_API_VERSION,
    VALID_ADVISOR_CAPABILITIES,
    VALID_PLUGIN_KINDS,
    AdvisorPlugin,
    InstrumentPlugin,
    PluginDoctorRecord,
    PluginKind,
    PluginLoadError,
)
from .builtin import BUILTIN_ADVISORS, BUILTIN_PLUGINS

ENTRY_POINT_GROUP = "wavebench.drivers"
ADVISOR_ENTRY_POINT_GROUP = "wavebench.advisor"
_CAPABILITY_PATTERN = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")


@dataclass(frozen=True)
class PluginRegistryLoadResult:
    registry: PluginRegistry
    load_errors: tuple[PluginLoadError, ...] = ()


@dataclass(frozen=True)
class PluginRegistry:
    plugins: tuple[InstrumentPlugin, ...]

    def __post_init__(self) -> None:
        seen: set[str] = set()
        duplicates: list[str] = []
        for plugin in self.plugins:
            if plugin.driver_id in seen:
                duplicates.append(plugin.driver_id)
            seen.add(plugin.driver_id)
        if duplicates:
            joined = ", ".join(sorted(set(duplicates)))
            raise ConfigError(f"duplicate plugin driver_id: {joined}")

    def list_plugins(self, *, kind: PluginKind | None = None) -> list[InstrumentPlugin]:
        plugins = self.plugins
        if kind is not None:
            plugins = tuple(plugin for plugin in plugins if plugin.kind == kind)
        return sorted(plugins, key=lambda item: (item.kind, item.driver_id))

    def get(self, driver_id: str) -> InstrumentPlugin:
        normalized = driver_id.strip()
        for plugin in self.plugins:
            if plugin.driver_id == normalized:
                return plugin
        raise ConfigError(f"unknown plugin driver_id: {driver_id}")


def builtin_plugin_registry() -> PluginRegistry:
    return PluginRegistry(BUILTIN_PLUGINS)


def build_plugin_registry(*, include_entry_points: bool = False) -> PluginRegistryLoadResult:
    plugins = list(BUILTIN_PLUGINS)
    load_errors: list[PluginLoadError] = []
    seen = {plugin.driver_id for plugin in plugins}

    if include_entry_points:
        for plugin, load_error in load_entry_point_plugins():
            if load_error is not None:
                load_errors.append(load_error)
                continue
            if plugin is None:
                continue
            if plugin.driver_id in seen:
                load_errors.append(
                    PluginLoadError(
                        source=f"entry_point:{plugin.driver_id}",
                        message=f"duplicate plugin driver_id: {plugin.driver_id}",
                    )
                )
                continue
            seen.add(plugin.driver_id)
            plugins.append(plugin)

    return PluginRegistryLoadResult(
        registry=PluginRegistry(tuple(plugins)),
        load_errors=tuple(load_errors),
    )


def load_entry_point_plugins() -> list[tuple[InstrumentPlugin | None, PluginLoadError | None]]:
    results: list[tuple[InstrumentPlugin | None, PluginLoadError | None]] = []
    for entry_point in _select_driver_entry_points():
        try:
            loaded = entry_point.load()
            plugin = _plugin_from_loaded_entry_point(entry_point, loaded)
        except Exception as exc:
            results.append((None, PluginLoadError(source=_entry_point_source(entry_point), message=str(exc))))
            continue
        results.append((plugin, None))
    return results


def plugin_doctor_records(
    registry: PluginRegistry,
    *,
    load_errors: tuple[PluginLoadError, ...] = (),
) -> list[PluginDoctorRecord]:
    records: list[PluginDoctorRecord] = []
    seen: set[str] = set()
    for error in load_errors:
        records.append(PluginDoctorRecord("error", error.source, error.message))
    for plugin in registry.plugins:
        plugin_errors = _validate_plugin(plugin)
        if plugin.driver_id in seen:
            plugin_errors.append(f"duplicate plugin driver_id: {plugin.driver_id}")
        seen.add(plugin.driver_id)
        if plugin_errors:
            for message in plugin_errors:
                records.append(PluginDoctorRecord("error", plugin.driver_id, message))
        else:
            records.append(PluginDoctorRecord("ok", plugin.driver_id, "metadata valid"))
    if not records:
        records.append(PluginDoctorRecord("warning", "registry", "no plugins found"))
    return records


def has_doctor_errors(records: list[PluginDoctorRecord]) -> bool:
    return any(record.severity == "error" for record in records)


def _validate_plugin(plugin: InstrumentPlugin) -> list[str]:
    errors: list[str] = []
    if plugin.api_version != SUPPORTED_PLUGIN_API_VERSION:
        errors.append(
            f"unsupported api_version {plugin.api_version!r}; expected {SUPPORTED_PLUGIN_API_VERSION!r}"
        )
    if plugin.kind not in VALID_PLUGIN_KINDS:
        errors.append(f"unsupported kind {plugin.kind!r}; expected one of {', '.join(VALID_PLUGIN_KINDS)}")
    for capability in plugin.capabilities:
        if not _CAPABILITY_PATTERN.match(capability):
            errors.append(f"invalid capability name: {capability!r}")
        elif not capability.startswith(f"{plugin.kind}."):
            errors.append(f"capability {capability!r} must start with {plugin.kind!r} prefix")
    return errors


def _select_driver_entry_points() -> list[EntryPoint]:
    discovered = entry_points()
    if hasattr(discovered, "select"):
        return list(discovered.select(group=ENTRY_POINT_GROUP))
    return list(discovered.get(ENTRY_POINT_GROUP, ()))


def _plugin_from_loaded_entry_point(entry_point: EntryPoint, loaded: Any) -> InstrumentPlugin:
    candidate = loaded() if callable(loaded) and not isinstance(loaded, InstrumentPlugin) else loaded
    if not isinstance(candidate, InstrumentPlugin):
        raise TypeError(
            f"entry point {entry_point.name!r} did not return an InstrumentPlugin "
            f"(got {type(candidate).__name__})"
        )
    return replace(candidate, origin="entry_point", package=_entry_point_package(entry_point, candidate))


def _entry_point_package(entry_point: EntryPoint, plugin: InstrumentPlugin | AdvisorPlugin) -> str:
    dist = getattr(entry_point, "dist", None)
    if dist is not None:
        metadata = getattr(dist, "metadata", {})
        name = metadata.get("Name") if hasattr(metadata, "get") else None
        if name:
            return str(name)
    return plugin.package


def _entry_point_source(entry_point: EntryPoint) -> str:
    return f"entry_point:{entry_point.name}"


@dataclass(frozen=True)
class AdvisorRegistryLoadResult:
    registry: "AdvisorRegistry"
    load_errors: tuple[PluginLoadError, ...] = ()


@dataclass(frozen=True)
class AdvisorRegistry:
    plugins: tuple[AdvisorPlugin, ...]

    def __post_init__(self) -> None:
        seen: set[str] = set()
        duplicates: list[str] = []
        for plugin in self.plugins:
            if plugin.advisor_id in seen:
                duplicates.append(plugin.advisor_id)
            seen.add(plugin.advisor_id)
        if duplicates:
            joined = ", ".join(sorted(set(duplicates)))
            raise ConfigError(f"duplicate advisor_id: {joined}")

    def list_advisors(self, *, capability: str | None = None) -> list[AdvisorPlugin]:
        plugins = self.plugins
        if capability is not None:
            plugins = tuple(plugin for plugin in plugins if capability in plugin.capabilities)
        return sorted(plugins, key=lambda item: item.advisor_id)

    def get(self, advisor_id: str) -> AdvisorPlugin:
        normalized = advisor_id.strip()
        for plugin in self.plugins:
            if plugin.advisor_id == normalized:
                return plugin
        raise ConfigError(f"unknown advisor_id: {advisor_id}")


def builtin_advisor_registry() -> AdvisorRegistry:
    return AdvisorRegistry(BUILTIN_ADVISORS)


def build_advisor_registry(*, include_entry_points: bool = False) -> AdvisorRegistryLoadResult:
    plugins = list(BUILTIN_ADVISORS)
    load_errors: list[PluginLoadError] = []
    seen = {plugin.advisor_id for plugin in plugins}

    if include_entry_points:
        for plugin, load_error in load_advisor_entry_points():
            if load_error is not None:
                load_errors.append(load_error)
                continue
            if plugin is None:
                continue
            if plugin.advisor_id in seen:
                load_errors.append(
                    PluginLoadError(
                        source=f"entry_point:{plugin.advisor_id}",
                        message=f"duplicate advisor_id: {plugin.advisor_id}",
                    )
                )
                continue
            seen.add(plugin.advisor_id)
            plugins.append(plugin)

    return AdvisorRegistryLoadResult(
        registry=AdvisorRegistry(tuple(plugins)),
        load_errors=tuple(load_errors),
    )


def load_advisor_entry_points() -> list[tuple[AdvisorPlugin | None, PluginLoadError | None]]:
    results: list[tuple[AdvisorPlugin | None, PluginLoadError | None]] = []
    for entry_point in _select_advisor_entry_points():
        try:
            loaded = entry_point.load()
            plugin = _advisor_from_loaded_entry_point(entry_point, loaded)
            errors = validate_advisor(plugin)
            if errors:
                raise ConfigError("; ".join(errors))
        except Exception as exc:
            results.append((None, PluginLoadError(source=_entry_point_source(entry_point), message=str(exc))))
            continue
        results.append((plugin, None))
    return results


def validate_advisor(plugin: AdvisorPlugin) -> list[str]:
    errors: list[str] = []
    if plugin.api_version != SUPPORTED_ADVISOR_API_VERSION:
        errors.append(
            f"unsupported api_version {plugin.api_version!r}; expected {SUPPORTED_ADVISOR_API_VERSION!r}"
        )
    for capability in plugin.capabilities:
        if not capability.startswith(ADVISOR_CAPABILITY_PREFIX):
            errors.append(
                f"capability {capability!r} must start with {ADVISOR_CAPABILITY_PREFIX!r} prefix"
            )
        elif capability not in VALID_ADVISOR_CAPABILITIES:
            errors.append(f"unknown advisor capability: {capability!r}")
    declares_external = ADVISOR_EXTERNAL_STATE_CAPABILITY in plugin.capabilities
    if plugin.transmits_off_machine and not declares_external:
        errors.append(
            f"a transmitting advisor must declare {ADVISOR_EXTERNAL_STATE_CAPABILITY!r}"
        )
    if not plugin.transmits_off_machine and declares_external:
        errors.append(
            f"a non-transmitting advisor must not declare {ADVISOR_EXTERNAL_STATE_CAPABILITY!r}"
        )
    return errors


def advisor_doctor_records(
    registry: AdvisorRegistry,
    *,
    load_errors: tuple[PluginLoadError, ...] = (),
) -> list[PluginDoctorRecord]:
    records: list[PluginDoctorRecord] = []
    seen: set[str] = set()
    for error in load_errors:
        records.append(PluginDoctorRecord("error", error.source, error.message))
    for plugin in registry.plugins:
        plugin_errors = validate_advisor(plugin)
        if plugin.advisor_id in seen:
            plugin_errors.append(f"duplicate advisor_id: {plugin.advisor_id}")
        seen.add(plugin.advisor_id)
        if plugin_errors:
            for message in plugin_errors:
                records.append(PluginDoctorRecord("error", plugin.advisor_id, message))
        else:
            records.append(PluginDoctorRecord("ok", plugin.advisor_id, "metadata valid"))
    if not records:
        records.append(PluginDoctorRecord("warning", "registry", "no advisors found"))
    return records


def has_advisor_doctor_errors(records: list[PluginDoctorRecord]) -> bool:
    return any(record.severity == "error" for record in records)


def _select_advisor_entry_points() -> list[EntryPoint]:
    discovered = entry_points()
    if hasattr(discovered, "select"):
        return list(discovered.select(group=ADVISOR_ENTRY_POINT_GROUP))
    return list(discovered.get(ADVISOR_ENTRY_POINT_GROUP, ()))


def _advisor_from_loaded_entry_point(entry_point: EntryPoint, loaded: Any) -> AdvisorPlugin:
    candidate = loaded() if callable(loaded) and not isinstance(loaded, AdvisorPlugin) else loaded
    if not isinstance(candidate, AdvisorPlugin):
        raise TypeError(
            f"entry point {entry_point.name!r} did not return an AdvisorPlugin "
            f"(got {type(candidate).__name__})"
        )
    if candidate.advisor_id != entry_point.name:
        raise ValueError(
            f"entry point name {entry_point.name!r} does not match advisor_id {candidate.advisor_id!r}"
        )
    return replace(candidate, origin="entry_point", package=_entry_point_package(entry_point, candidate))
