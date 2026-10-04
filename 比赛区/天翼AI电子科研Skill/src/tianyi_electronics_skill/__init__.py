"""天翼 AI 电子科研 TeleAgent Skill。"""

from .analysis import analyze_samples, demo_waveform
from .core import autoscale_scope, discover_devices, discover_mdns, inspect_device, measure_scope
from .waveform import ERROR_QUEUE_UNAVAILABLE, load_waveform, read_waveform, write_waveform
from .analysis import compute_dft_metrics, estimate_frequency_hysteresis
from .plot import waveform_svg, write_waveform_svg
from .protocols import NormalizedFrame, ProtocolRegistry

__all__ = [
    "analyze_samples",
    "demo_waveform",
    "discover_devices",
    "discover_mdns",
    "inspect_device",
    "load_waveform",
    "measure_scope",
    "autoscale_scope",
    "read_waveform",
    "write_waveform",
    "compute_dft_metrics",
    "estimate_frequency_hysteresis",
    "waveform_svg",
    "write_waveform_svg",
    "NormalizedFrame",
    "ProtocolRegistry",
    "ERROR_QUEUE_UNAVAILABLE",
]
