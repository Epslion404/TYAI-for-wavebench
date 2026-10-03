import json
import ipaddress
import socket
import struct
import threading

import pytest

from tianyi_electronics_skill.analysis import analyze_samples, compute_dft_metrics
from tianyi_electronics_skill.bridge import run_expose
from tianyi_electronics_skill.core import _host_count
from tianyi_electronics_skill.plot import waveform_svg
from tianyi_electronics_skill.protocols import (
    BinaryManifestAdapter,
    CanAdapter,
    JsonWaveformAdapter,
    ProtocolDecodeError,
    ProtocolRegistry,
)
from tianyi_electronics_skill.scpi import ConfigurationError, ScpiConfigurationError, ScpiSession, classify_exception
from tianyi_electronics_skill import waveform


def test_hysteresis_and_dft_metrics_are_reported():
    samples = [0.8 * __import__("math").sin(2 * __import__("math").pi * 1000 * i / 100000) for i in range(1000)]
    result = analyze_samples(samples, 100000)
    assert result["frequency_hz"] == pytest.approx(1000, abs=2)
    assert result["dft"]["available"] is True
    assert result["dft"]["thd_percent"] < 1e-6


def test_plot_escapes_title():
    svg = waveform_svg([0, 1, 0, -1, 0], 1000, title="<unsafe>")
    assert "&lt;unsafe&gt;" in svg
    assert "<unsafe>" not in svg


def test_vendor_binary_manifest(tmp_path):
    manifest_dir = tmp_path / "protocols"
    manifest_dir.mkdir()
    (manifest_dir / "vendor.json").write_text(json.dumps({
        "kind": "binary-waveform", "name": "vendor", "match": {"magic_hex": "57415645"},
        "payload": {"header_bytes": 4, "sample_type": "f32", "endian": "little", "sample_rate_hz": 1000},
    }), encoding="utf-8")
    registry = ProtocolRegistry()
    assert registry.load_manifests(manifest_dir) == 1
    frame = registry.decode(b"WAVE" + struct.pack("<4f", 1, -1, 1, -1))
    assert frame.protocol == "vendor"
    assert frame.samples == [1.0, -1.0, 1.0, -1.0]


def test_vendor_toml_manifest_uses_the_same_binary_validation(tmp_path):
    manifest_dir = tmp_path / "protocols"
    manifest_dir.mkdir()
    (manifest_dir / "vendor.toml").write_text(
        """
kind = "binary-waveform"
name = "vendor-toml"

[match]
magic_hex = "57415645"

[payload]
header_bytes = 4
sample_type = "i16"
endian = "little"
sample_rate_hz = 1000
""".strip(),
        encoding="utf-8",
    )

    registry = ProtocolRegistry()
    assert registry.load_manifests(manifest_dir) == 1
    frame = registry.decode(b"WAVE" + struct.pack("<2h", 1, -1))

    assert frame.protocol == "vendor-toml"
    assert frame.samples == [1.0, -1.0]


@pytest.mark.parametrize(
    "payload",
    [
        {"name": "missing-magic", "match": {}, "payload": {}},
        {"name": "short-header", "match": {"magic_hex": "ABCD"}, "payload": {"header_bytes": 1}},
        {"name": "bad-type", "match": {"magic_hex": "ABCD"}, "payload": {"sample_type": "x128"}},
        {"name": "bad-endian", "match": {"magic_hex": "ABCD"}, "payload": {"endian": "middle"}},
    ],
)
def test_binary_manifest_rejects_invalid_declared_shape(payload):
    with pytest.raises((ProtocolDecodeError, ValueError)):
        BinaryManifestAdapter(payload)


def test_protocol_decode_result_structures_invalid_and_oversized_frames():
    registry = ProtocolRegistry()
    registry.adapters.append(
        BinaryManifestAdapter(
            {
                "name": "limited",
                "match": {"magic_hex": "4C494D"},
                "payload": {"header_bytes": 3, "sample_type": "i16", "endian": "little", "max_payload_bytes": 4},
            }
        )
    )

    invalid = registry.decode_result(b"unknown")
    oversized = registry.decode_result(b"LIM" + struct.pack("<h", 1))

    assert invalid["ok"] is False
    assert oversized["ok"] is False
    assert invalid["failures"][0]["category"] == "parse_error"
    assert oversized["failures"][0]["category"] == "parse_error"


def test_protocol_adapters_preserve_reported_device_errors():
    with pytest.raises(ValueError):
        CanAdapter().decode({"data": [1, 2, 3]}, "test")

    json_frame = JsonWaveformAdapter().decode(
        {"samples": [1], "device_errors": ["json-error"]}, "test"
    )
    can_frame = CanAdapter().decode(
        {"can_id": 1, "data": [1], "device_errors": ["can-error"]}, "test"
    )
    registry = ProtocolRegistry()
    registry.adapters.append(
        __import__("tianyi_electronics_skill.protocols", fromlist=["BinaryManifestAdapter"]).BinaryManifestAdapter(
            {"name": "bin", "match": {"magic_hex": "42494e"},
             "payload": {"header_bytes": 3, "sample_type": "i16", "endian": "little"}}
        )
    )
    binary_frame = registry.decode(b"BIN" + struct.pack("<h", 1), metadata={"device_errors": ["bin-error"]})

    assert json_frame.as_dict()["device_errors"] == ["json-error"]
    assert can_frame.as_dict()["device_errors"] == ["can-error"]
    assert binary_frame.as_dict()["device_errors"] == ["bin-error"]


def test_dft_does_not_report_zero_thd_when_no_harmonic_is_observable():
    metrics = compute_dft_metrics([0.0, 1.0, 0.0, -1.0] * 16, 100.0, 30.0)

    assert metrics["available"] is True
    assert metrics["harmonics"] == {}
    assert metrics["thd_available"] is False
    assert metrics["thd_percent"] is None
    assert analyze_samples([0.0, 1.0, 0.0, -1.0] * 16, 100.0)["thd_available"] is False


def test_duty_cycle_uses_the_signal_mean_as_its_decision_level():
    samples = [0.0] * 90 + [1.0] * 9 + [100.0]

    assert analyze_samples(samples, 1_000)["duty_cycle_percent"] == pytest.approx(1.0)


def test_ipv6_host_count_matches_ipaddress_hosts():
    network = ipaddress.ip_network("2001:db8::/127")

    assert _host_count(network) == len(list(network.hosts())) == 2


def test_waveform_error_drain_is_reported_without_masking_the_read_failure(monkeypatch):
    class Session:
        host = "127.0.0.1"
        port = 5025

        @staticmethod
        def drain_errors():
            raise ValueError("oversized response")

    def fail_collect(*_args, **_kwargs):
        raise ValueError("invalid waveform")

    monkeypatch.setattr(waveform, "_collect_waveform", fail_collect)
    result = waveform.read_waveform_in_session(Session())

    assert [failure["category"] for failure in result["failures"]] == ["parse_error", "error_queue_unavailable"]
    assert "invalid waveform" in result["failures"][0]["message"]
    assert "SYST:ERR?" in result["failures"][1]["message"]


def test_scpi_configuration_error_is_not_a_response_parse_error():
    with pytest.raises(ScpiConfigurationError) as raised:
        ScpiSession("", timeout=1.0)

    assert classify_exception(raised.value) == "configuration_error"
    assert classify_exception(ConfigurationError("bad scan input")) == "configuration_error"


def test_expose_rejection_honours_reconnect_delay():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        listener.settimeout(0.05)
        host, port = listener.getsockname()[:2]
        attempts = 0
        stop = threading.Event()
        retry_called = threading.Event()
        observed_delays = []

        def reject_registrations():
            nonlocal attempts
            while not stop.is_set():
                try:
                    connection, _ = listener.accept()
                except TimeoutError:
                    continue
                except OSError:
                    return
                with connection:
                    connection.recv(512)
                    attempts += 1
                    connection.sendall(b"ERROR invalid-handshake\n")

        thread = threading.Thread(target=reject_registrations, daemon=True)
        thread.start()
        expose_thread = threading.Thread(
            target=run_expose,
            args=(f"{host}:{port}", "device-1", "127.0.0.1:9"),
            kwargs={
                "reconnect_delay": 0.1,
                "stop_event": stop,
                "retry_wait": lambda delay: (observed_delays.append(delay), retry_called.set(), stop.set()),
            },
            daemon=True,
        )
        expose_thread.start()
        assert retry_called.wait(timeout=1.0)
        expose_thread.join(timeout=1.0)
        stop.set()
        thread.join(timeout=1.0)

    assert attempts == 1
    assert observed_delays == [0.1]


def test_expose_normal_session_reregisters_without_backoff(monkeypatch):
    import tianyi_electronics_skill.bridge as bridge

    stop = threading.Event()

    class Socket:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def settimeout(self, _timeout):
            pass

        def sendall(self, _data):
            pass

    sockets = [Socket(), Socket()]
    monkeypatch.setattr(bridge.socket, "create_connection", lambda *_args, **_kwargs: sockets.pop(0))
    replies = iter((b"OPEN", b"START"))
    monkeypatch.setattr(bridge, "_recv_line", lambda _sock: next(replies))
    monkeypatch.setattr(bridge, "_relay", lambda *_args: stop.set())
    monkeypatch.setattr(bridge, "_close", lambda _sock: None)

    run_expose(
        "127.0.0.1:9000",
        "device-1",
        "127.0.0.1:5025",
        reconnect_delay=0.1,
        stop_event=stop,
        retry_wait=lambda _delay: pytest.fail("normal session must not back off"),
    )
