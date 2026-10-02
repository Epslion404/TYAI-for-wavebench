"""传输后端分派与串口连接配置的离线契约测试（RFC 阶段 1）。"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from wavebench.config import ConnectionConfig, DmmConfig, SerialConnectionConfig
from wavebench.errors import ConfigError
from wavebench.instruments.api import InstrumentDescriptor
from wavebench.instruments.factory import TRANSPORT_FACTORIES, _open_transport, open_instrument_driver
from wavebench.logging import CommandLogger


class SerialConnectionConfigTests(unittest.TestCase):
    def test_dmm_serial_connection_round_trip(self):
        dmm = DmmConfig(
            driver="dm3058",
            resource="COM7",
            backend="serial",
            baudrate=115200,
            bytesize=7,
            parity="E",
            stopbits=2,
            timeout_ms=2500,
            write_termination="crlf",
            read_termination="crlf",
            xonxoff=True,
            rtscts=False,
            dsrdtr=True,
        )

        connection = dmm.serial_connection()

        self.assertIsInstance(connection, SerialConnectionConfig)
        self.assertEqual(connection.resource, "COM7")
        self.assertEqual(connection.baudrate, 115200)
        self.assertEqual(connection.bytesize, 7)
        self.assertEqual(connection.parity, "E")
        self.assertEqual(connection.stopbits, 2)
        self.assertEqual(connection.timeout_ms, 2500)
        self.assertEqual(connection.write_termination, "crlf")
        self.assertEqual(connection.read_termination, "crlf")
        self.assertTrue(connection.xonxoff)
        self.assertTrue(connection.dsrdtr)

    def test_serial_connection_defaults_are_conservative(self):
        connection = SerialConnectionConfig(resource="COM1")

        self.assertEqual(connection.baudrate, 9600)
        self.assertEqual(connection.parity, "N")
        self.assertFalse(connection.xonxoff)
        self.assertFalse(connection.rtscts)
        self.assertFalse(connection.dsrdtr)

    def test_generic_connection_can_target_another_declared_serial_instrument(self):
        connection = ConnectionConfig(
            backend="serial",
            resource="COM3",
            timeout_ms=1000,
            opc_timeout_ms=2000,
            baudrate=57600,
            parity="O",
            rtscts=True,
        )

        serial = connection.serial_connection(resource="COM9")

        self.assertEqual(serial.resource, "COM9")
        self.assertEqual(serial.baudrate, 57600)
        self.assertEqual(serial.parity, "O")
        self.assertTrue(serial.rtscts)


class TransportBackendDispatchTests(unittest.TestCase):
    def test_backend_table_covers_the_known_transports(self):
        self.assertEqual(
            sorted(TRANSPORT_FACTORIES),
            [
                "pyvisa",
                "rsinstrument",
                "rsinstrument-pyvisa-py",
                "rsinstrument-rsvisa",
                "rsinstrument-socket",
                "serial",
            ],
        )

    def test_unknown_backend_fails_before_any_io(self):
        with self.assertRaisesRegex(ConfigError, "unsupported instrument transport backend: can"):
            _open_transport(
                backend="can",
                resource="can://can0",
                timeout_ms=1000,
                opc_timeout_ms=1000,
                read_retry_attempts=1,
                read_retry_delay_ms=200,
                logger=CommandLogger(),
                serial_config=None,
            )

    def test_serial_backend_requires_serial_configuration(self):
        with self.assertRaisesRegex(ConfigError, "requires serial configuration"):
            _open_transport(
                backend="serial",
                resource="COM3",
                timeout_ms=1000,
                opc_timeout_ms=1000,
                read_retry_attempts=1,
                read_retry_delay_ms=200,
                logger=CommandLogger(),
                serial_config=None,
            )

    def test_declared_serial_scope_opens_through_core_factory(self):
        captured = {}

        class Driver:
            def idn(self):
                return "EXAMPLE,SERIAL"

            def close(self):
                pass

        descriptor = InstrumentDescriptor(
            driver_id="example.serial.scope",
            kind="scope",
            display_name="Example serial scope",
            manufacturer="Example",
            models=("SERIAL",),
            aliases=(),
            capabilities=("scope.idn",),
            idn_patterns=("EXAMPLE,SERIAL",),
            backends=("serial",),
            resource_schemes=("com",),
            option_specs=(),
            permissions=("instrument.io",),
            factory=lambda context: (context.open_transport(), Driver())[1],
        )
        concrete = object()

        def open_serial(serial_config, **kwargs):
            captured["serial_config"] = serial_config
            return concrete

        with (
            patch(
                "wavebench.instruments.factory.resolve_instrument_descriptor",
                return_value=descriptor,
            ),
            patch("wavebench.instruments.factory.SerialTransport.open", side_effect=open_serial),
        ):
            opened = open_instrument_driver(
                driver_reference="example.serial.scope",
                expected_kind="scope",
                resource="COM8",
                configured_backend="serial",
                timeout_ms=1000,
                opc_timeout_ms=2000,
                read_retry_attempts=1,
                read_retry_delay_ms=0,
                logger=CommandLogger(),
                serial_config=ConnectionConfig(
                    "serial", "COM8", 1000, 2000, baudrate=115200
                ).serial_connection(),
            )

        self.assertEqual(opened.driver.idn(), "EXAMPLE,SERIAL")
        self.assertIs(opened.transport.inner, concrete)
        self.assertEqual(captured["serial_config"].baudrate, 115200)


if __name__ == "__main__":
    unittest.main()
