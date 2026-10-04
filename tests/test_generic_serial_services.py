from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from wavebench.config import (
    AutoscaleConfig,
    ConnectionConfig,
    OutputConfig,
    PowerConfig,
    ScopeConfig,
    SourceConfig,
    WaveBenchConfig,
    WaveformConfig,
)
from wavebench.instruments.factory import OpenedInstrument
from wavebench.logging import CommandLogger
from wavebench.services.power_service import PowerService
from wavebench.services.scope_service import ScopeService
from wavebench.services.source_service import SourceService


def _config() -> WaveBenchConfig:
    return WaveBenchConfig(
        connection=ConnectionConfig(
            backend="serial",
            resource="COM3",
            timeout_ms=1_000,
            opc_timeout_ms=2_000,
            baudrate=57_600,
            parity="E",
        ),
        scope=ScopeConfig("rtm2032", None, 1, False, True),
        autoscale=AutoscaleConfig(True, True),
        waveform=WaveformConfig("real", "lsbf", "dmax"),
        output=OutputConfig(Path("data/raw"), "timestamp_label", True, True, True, True, False),
        source_path=Path("wavebench.toml"),
        source=SourceConfig("dg4202", "COM4", 1, True, True, 0),
        power=PowerConfig("dp800", "COM5", 1, True, 0, 0),
    )


def _opened() -> OpenedInstrument:
    return OpenedInstrument(descriptor=object(), driver=object())


def test_scope_service_passes_generic_serial_config_to_declared_driver():
    with patch("wavebench.services.scope_service.open_instrument_driver", return_value=_opened()) as open_driver:
        ScopeService(_config(), CommandLogger()).open_session()

    serial = open_driver.call_args.kwargs["serial_config"]
    assert serial.resource == "COM3"
    assert serial.baudrate == 57_600
    assert serial.parity == "E"


def test_source_service_uses_its_own_resource_with_generic_serial_settings():
    with patch("wavebench.services.source_service.open_instrument_driver", return_value=_opened()) as open_driver:
        SourceService(_config(), CommandLogger()).open_session()

    serial = open_driver.call_args.kwargs["serial_config"]
    assert serial.resource == "COM4"
    assert serial.baudrate == 57_600
    assert serial.parity == "E"


def test_power_service_uses_its_own_resource_with_generic_serial_settings():
    with patch("wavebench.services.power_service.open_instrument_driver", return_value=_opened()) as open_driver:
        PowerService(_config(), CommandLogger()).open_session()

    serial = open_driver.call_args.kwargs["serial_config"]
    assert serial.resource == "COM5"
    assert serial.baudrate == 57_600
    assert serial.parity == "E"
