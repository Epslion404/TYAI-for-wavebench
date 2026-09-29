"""天翼 AI 电子科研 TeleAgent Skill。"""

from .analysis import analyze_samples, demo_waveform
from .core import discover_devices, inspect_device, measure_scope
from .waveform import load_waveform, read_waveform, write_waveform

__all__ = [
    "analyze_samples",
    "demo_waveform",
    "discover_devices",
    "inspect_device",
    "load_waveform",
    "measure_scope",
    "read_waveform",
    "write_waveform",
]
