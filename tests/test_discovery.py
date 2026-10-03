import unittest

from unittest.mock import patch

from wavebench.discovery import (
    PortProbe,
    _probe_scpi_socket,
    discover_can_adapters,
    discover_instruments,
    discover_network,
    discover_serial_ports,
    parse_discovery_ports,
)
from wavebench.errors import ConfigError


class DiscoveryTests(unittest.TestCase):
    def test_scpi_probe_does_not_hide_lease_backend_failure(self):
        class BrokenLease:
            def __init__(self, **kwargs):
                pass

            def __enter__(self):
                raise ConfigError("lock backend unavailable")

            def __exit__(self, exc_type, exc, traceback):
                return False

        with patch("wavebench.discovery.ResourceLease", BrokenLease):
            with self.assertRaisesRegex(ConfigError, "lock backend unavailable"):
                _probe_scpi_socket("192.0.2.10", 5025, 0.01)

    def test_parse_discovery_ports_deduplicates_and_validates(self):
        self.assertEqual(parse_discovery_ports("5025, 5555,5025"), (5025, 5555))
        with self.assertRaisesRegex(ConfigError, "发现端口无效"):
            parse_discovery_ports("5025,nope")
        with self.assertRaisesRegex(ConfigError, "1..65535"):
            parse_discovery_ports("0")

    def test_discover_network_reports_scpi_idn_and_vxi11_candidate(self):
        def tcp_probe(address, port, timeout_s):
            return address == "192.0.2.11" and port == 111

        def scpi_probe(address, port, timeout_s):
            if address == "192.0.2.10" and port == 5025:
                return PortProbe(open=True, idn="RIGOL TECHNOLOGIES,DG4202,DG4E000000000,00.01.18")
            return PortProbe(open=False)

        results = discover_network(
            "192.0.2.8/29",
            ports=(5025, 111),
            timeout_ms=1,
            workers=2,
            query_idn=True,
            tcp_probe=tcp_probe,
            scpi_probe=scpi_probe,
        )

        resources = [item.resource for item in results]
        self.assertIn("TCPIP::192.0.2.10::5025::SOCKET", resources)
        self.assertIn("TCPIP::192.0.2.11::INSTR", resources)
        idn_result = next(item for item in results if item.address == "192.0.2.10")
        self.assertEqual(idn_result.status, "idn")
        self.assertIn("DG4202", idn_result.idn)

    def test_discover_network_idn_only_hides_open_only_ports(self):
        def scpi_probe(address, port, timeout_s):
            return PortProbe(open=True, idn=None, note="idn timeout")

        results = discover_network(
            "192.0.2.10/32",
            ports=(5025,),
            timeout_ms=1,
            query_idn=True,
            include_open=False,
            scpi_probe=scpi_probe,
        )
        self.assertEqual(results, [])

    def test_discover_network_refuses_large_subnet_by_default(self):
        with self.assertRaisesRegex(ConfigError, "raise --max-hosts"):
            discover_network("198.18.0.0/15", timeout_ms=1)

    def test_serial_enumeration_never_opens_or_probes_a_port(self):
        class Port:
            device = "COM7"
            description = "USB serial adapter"
            hwid = "USB VID:PID=1234:5678"

        result = discover_serial_ports(port_lister=lambda: [Port()])

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].protocol, "serial")
        self.assertEqual(result[0].resource, "COM7")
        self.assertIn("no probe bytes sent", result[0].note)

    def test_can_enumeration_never_opens_a_bus_or_sends_a_frame(self):
        result = discover_can_adapters(
            config_lister=lambda: [
                {"interface": "socketcan", "channel": "can0"},
                {"interface": "virtual", "channel": "test", "fd": True},
            ]
        )

        self.assertEqual([(item.protocol, item.resource) for item in result], [("can", "can0"), ("can-fd", "test")])
        self.assertTrue(all("no CAN frames sent" in item.note for item in result))

    def test_local_enumeration_does_not_trigger_tcp_scan(self):
        serial_result = discover_serial_ports(port_lister=lambda: [])
        can_result = discover_can_adapters(config_lister=lambda: [])
        with (
            patch("wavebench.discovery.discover_serial_ports", return_value=serial_result),
            patch("wavebench.discovery.discover_can_adapters", return_value=can_result),
            patch("wavebench.discovery.discover_network", side_effect=AssertionError("TCP must not scan")),
        ):
            self.assertEqual(
                discover_instruments(
                    subnet=None,
                    include_visa=False,
                    include_serial=True,
                    include_can=True,
                ),
                [],
            )


if __name__ == "__main__":
    unittest.main()
