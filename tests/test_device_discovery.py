"""Offline device-discovery contracts: serial/CAN enumeration and protocol tags.

Uses fake enumerators so no real serial port or CAN adapter is touched.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from wavebench.discovery import (
    MODBUS_TCP_PORTS,
    _open_port_result,
    discover_can_adapters,
    discover_local_devices,
    discover_serial_ports,
)
from wavebench.errors import ConfigError


class FakePort:
    def __init__(self, device, description="", hwid=""):
        self.device = device
        self.description = description
        self.hwid = hwid


class SerialDiscoveryTests(unittest.TestCase):
    def test_enumerates_serial_ports_with_protocol_tag(self):
        results = discover_serial_ports(
            list_ports=lambda: [
                FakePort("COM3", "USB Serial Port", "USB VID:PID=1A86:7523"),
                FakePort("COM10", "PCI Serial Port", "PCI\\VEN_1"),
            ]
        )

        by_resource = {item.resource: item for item in results}
        self.assertEqual(set(by_resource), {"COM3", "COM10"})
        com3 = by_resource["COM3"]
        self.assertEqual(com3.source, "serial")
        self.assertEqual(com3.protocol, "serial")
        self.assertEqual(com3.status, "listed")
        self.assertEqual(com3.note, "USB serial adapter")
        self.assertEqual(by_resource["COM10"].resource, "COM10")

    def test_missing_pyserial_is_structured_error(self):
        with patch.dict("sys.modules", {"serial": None, "serial.tools": None, "serial.tools.list_ports": None}):
            with self.assertRaisesRegex(ConfigError, "pyserial"):
                discover_serial_ports()

    def test_injected_serial_enumerator_never_imports_pyserial(self):
        with patch.dict("sys.modules", {"serial": None, "serial.tools": None, "serial.tools.list_ports": None}):
            results = discover_serial_ports(list_ports=lambda: [FakePort("COM9")])
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].resource, "COM9")

    def test_enumerator_failure_is_structured_error(self):
        def boom():
            raise PermissionError("access denied")

        with self.assertRaisesRegex(ConfigError, "unable to enumerate"):
            discover_serial_ports(list_ports=boom)


class CanDiscoveryTests(unittest.TestCase):
    def test_enumerates_can_interfaces(self):
        results = discover_can_adapters(
            list_interfaces=lambda: ["virtual", "socketcan", "pcan"],
        )
        by_resource = {item.resource: item for item in results}
        self.assertEqual(set(by_resource), {"can://virtual", "can://socketcan", "can://pcan"})
        virtual = by_resource["can://virtual"]
        self.assertEqual(virtual.source, "can")
        self.assertEqual(virtual.protocol, "can")
        self.assertEqual(virtual.status, "listed")

    def test_default_enumerates_detected_configs(self):
        try:
            import can
            can.detect_available_configs()
        except ImportError:
            self.skipTest("python-can not installed")
        results = discover_can_adapters()
        # Real detection yields interface[/channel] resources, not bare backend names.
        self.assertTrue(any(item.protocol == "can" and item.status == "listed" for item in results))
        self.assertTrue(all(item.resource.startswith("can://") for item in results))

    def test_injected_enumerator_never_imports_python_can(self):
        # Providing a fake enumerator must not import the optional package.
        with patch.dict("sys.modules", {"can": None}):
            results = discover_can_adapters(
                list_interfaces=lambda: [{"interface": "socketcan", "channel": "can0"}],
            )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].resource, "can://socketcan/can0")
        self.assertEqual(results[0].status, "listed")

    def test_injected_detected_config_shape(self):
        class FakeDetect:
            @staticmethod
            def detect_available_configs():
                return [{"interface": "pcan", "channel": "PCAN_USBBUS1"}]

        with patch.dict("sys.modules", {"can": None}):
            results = discover_can_adapters(can_module=FakeDetect)
        self.assertEqual([item.resource for item in results], ["can://pcan/PCAN_USBBUS1"])

    def test_no_adapter_yields_structured_none_result(self):
        class FakeDetect:
            @staticmethod
            def detect_available_configs():
                return []

        results = discover_can_adapters(can_module=FakeDetect)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].status, "none")
        self.assertIn("no CAN adapter", results[0].note)

    def test_missing_python_can_is_structured_error(self):
        with patch.dict("sys.modules", {"can": None}):
            with self.assertRaisesRegex(ConfigError, "python-can"):
                discover_can_adapters()


class LocalDeviceDiscoveryTests(unittest.TestCase):
    def test_combines_serial_and_can(self):
        results = discover_local_devices(
            list_ports=lambda: [FakePort("COM5")],
            list_can_interfaces=lambda: ["virtual"],
        )
        sources = {item.source for item in results}
        self.assertEqual(sources, {"serial", "can"})

    def test_all_disabled_returns_empty(self):
        results = discover_local_devices(include_serial=False, include_can=False)
        self.assertEqual(results, [])


class ProtocolTagTests(unittest.TestCase):
    def test_modbus_port_is_tagged_not_instrument(self):
        result = _open_port_result("192.0.2.50", 502)
        self.assertEqual(result.protocol, "modbus-tcp")
        self.assertIn("not treated as a SCPI instrument", result.note)
        self.assertIn(502, MODBUS_TCP_PORTS)

    def test_unknown_port_is_tagged_unknown(self):
        result = _open_port_result("192.0.2.51", 9999)
        self.assertEqual(result.protocol, "unknown")
        self.assertIn("not treated as an instrument", result.note)

    def test_vxi11_candidate_keeps_protocol(self):
        result = _open_port_result("192.0.2.52", 111)
        self.assertEqual(result.protocol, "vxi11-candidate")


if __name__ == "__main__":
    unittest.main()