import json
import struct

import pytest

from tianyi_electronics_skill.analysis import analyze_samples
from tianyi_electronics_skill.plot import waveform_svg
from tianyi_electronics_skill.protocols import ProtocolRegistry


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
