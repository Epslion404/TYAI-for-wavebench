"""离线信号指标计算（只用标准库）。"""

from __future__ import annotations

import math
import statistics
from collections.abc import Iterable


def analyze_samples(samples: Iterable[float], sample_rate_hz: float | None) -> dict:
    """从等间隔波形估计峰值、峰峰值、RMS、频率和占空比。

    采样率未知（None）时只给出时域指标，不编造频率。
    """
    values = [float(value) for value in samples]
    if len(values) < 4:
        raise ValueError("至少需要 4 个采样点")
    if sample_rate_hz is not None:
        if sample_rate_hz <= 0:
            raise ValueError("sample_rate_hz 必须大于 0")
        sample_rate_hz = float(sample_rate_hz)
    peak = max(values, key=abs)
    mean = statistics.fmean(values)
    centered = [value - mean for value in values]
    crossings = [index for index in range(1, len(centered)) if centered[index - 1] <= 0 < centered[index]]
    frequency = None
    if sample_rate_hz is not None and len(crossings) >= 2:
        periods = [b - a for a, b in zip(crossings, crossings[1:])]
        frequency = sample_rate_hz / statistics.fmean(periods)
    high = sum(1 for value in centered if value >= 0) / len(centered)
    return {
        "samples": len(values), "sample_rate_hz": sample_rate_hz,
        "peak_v": peak, "peak_to_peak_v": max(values) - min(values),
        "minimum_v": min(values), "maximum_v": max(values), "mean_v": mean,
        "rms_v": math.sqrt(statistics.fmean(value * value for value in centered)),
        "frequency_hz": frequency, "duty_cycle_percent": high * 100,
    }


def demo_waveform(sample_rate_hz: float = 100_000, frequency_hz: float = 1_000, seconds: float = 0.01) -> dict:
    count = max(4, int(sample_rate_hz * seconds))
    samples = [0.8 * math.sin(2 * math.pi * frequency_hz * i / sample_rate_hz) + 0.02 for i in range(count)]
    return {"source": "built-in-demo", **analyze_samples(samples, sample_rate_hz)}


__all__ = ["analyze_samples", "demo_waveform"]
