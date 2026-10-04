"""Plugin registry support for WaveBench."""

from .api import AdvisorPlugin, InstrumentPlugin
from .registry import (
    AdvisorRegistry,
    PluginRegistry,
    build_advisor_registry,
    build_plugin_registry,
    builtin_advisor_registry,
    builtin_plugin_registry,
)

__all__ = [
    "AdvisorPlugin",
    "AdvisorRegistry",
    "InstrumentPlugin",
    "PluginRegistry",
    "build_advisor_registry",
    "build_plugin_registry",
    "builtin_advisor_registry",
    "builtin_plugin_registry",
]
