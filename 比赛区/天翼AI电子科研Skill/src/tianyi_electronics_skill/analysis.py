"""标准库信号分析：迟滞频率、边沿、过冲、有限点数 DFT/THD。"""

from __future__ import annotations

import math
import statistics
from collections.abc import Iterable


def _crossing(values: list[float], index: int, level: float) -> float:
    left, right = values[index - 1], values[index]
    if right == left:
        return float(index)
    return (index - 1) + (level - left) / (right - left)


def estimate_frequency_hysteresis(values: list[float], sample_rate_hz: float | None, hysteresis: float = 0.05) -> tuple[float | None, list[float]]:
    if sample_rate_hz is None or len(values) < 3:
        return None, []
    low, high = min(values), max(values)
    span = high - low
    if span <= 0 or not 0 < hysteresis < 0.5:
        return None, []
    mean = statistics.fmean(values)
    rise_level = mean + hysteresis * span
    fall_level = mean - hysteresis * span
    crossings: list[float] = []
    armed = True
    for index in range(1, len(values)):
        if armed and values[index - 1] <= rise_level < values[index]:
            crossings.append(_crossing(values, index, rise_level))
            armed = False
        elif not armed and values[index - 1] >= fall_level > values[index]:
            armed = True
    if len(crossings) < 2:
        return None, crossings
    periods = [later - earlier for earlier, later in zip(crossings, crossings[1:])]
    return sample_rate_hz / statistics.fmean(periods), crossings


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower, upper = math.floor(position), math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _edge_time(values: list[float], level: float, start: int, sample_rate_hz: float | None) -> float | None:
    for index in range(max(1, start), len(values)):
        if values[index - 1] < level <= values[index]:
            crossing = _crossing(values, index, level)
            return crossing / sample_rate_hz if sample_rate_hz else None
    return None


def compute_dft_metrics(values: list[float], sample_rate_hz: float | None, fundamental_hz: float | None) -> dict:
    if sample_rate_hz is None or fundamental_hz is None or len(values) > 4096 or fundamental_hz <= 0:
        return {"available": False, "reason": "需要采样率、基波频率且样本数不超过 4096"}
    centered = [value - statistics.fmean(values) for value in values]
    count = len(centered)
    def amplitude(harmonic: int) -> float:
        angle = 2.0 * math.pi * fundamental_hz * harmonic / sample_rate_hz
        real = sum(value * math.cos(angle * index) for index, value in enumerate(centered))
        imag = sum(value * math.sin(angle * index) for index, value in enumerate(centered))
        return 2.0 * math.hypot(real, imag) / count
    fundamental = amplitude(1)
    harmonics = {str(index): amplitude(index) for index in range(2, 6)}
    thd = math.sqrt(sum(value * value for value in harmonics.values())) / fundamental * 100 if fundamental else None
    return {"available": True, "fundamental_hz": fundamental_hz, "fundamental_amplitude": fundamental,
            "harmonics": harmonics, "thd_percent": thd}


def analyze_samples(samples: Iterable[float], sample_rate_hz: float | None, *, hysteresis: float = 0.05) -> dict:
    values = [float(value) for value in samples]
    if len(values) < 4:
        raise ValueError("至少需要 4 个采样点")
    if sample_rate_hz is not None and sample_rate_hz <= 0:
        raise ValueError("sample_rate_hz 必须大于 0")
    sample_rate_hz = float(sample_rate_hz) if sample_rate_hz is not None else None
    peak = max(values, key=abs)
    mean = statistics.fmean(values)
    centered = [value - mean for value in values]
    frequency, crossings = estimate_frequency_hysteresis(values, sample_rate_hz, hysteresis)
    low_level, high_level = _percentile(values, 0.1), _percentile(values, 0.9)
    span = high_level - low_level or 1.0
    rise_start = _edge_time(values, low_level + 0.1 * span, 0, sample_rate_hz)
    rise_end = _edge_time(values, low_level + 0.9 * span, 0, sample_rate_hz)
    rise_time = (rise_end - rise_start) if rise_start is not None and rise_end is not None and rise_end >= rise_start else None
    result = {
        "samples": len(values), "sample_rate_hz": sample_rate_hz,
        "peak_v": peak, "peak_to_peak_v": max(values) - min(values),
        "minimum_v": min(values), "maximum_v": max(values), "mean_v": mean,
        "rms_v": math.sqrt(statistics.fmean(value * value for value in centered)),
        "frequency_hz": frequency, "duty_cycle_percent": sum(value >= mean for value in values) / len(values) * 100,
        "hysteresis_fraction": hysteresis, "rising_crossings": len(crossings),
        "rise_time_10_90_s": rise_time,
        "overshoot_percent": max(0.0, (max(values) - high_level) / span * 100.0),
        "undershoot_percent": max(0.0, (low_level - min(values)) / span * 100.0),
    }
    result["dft"] = compute_dft_metrics(values, sample_rate_hz, frequency)
    result["fft"] = result["dft"]
    result["rise_time_s"] = rise_time
    result["thd_percent"] = result["dft"].get("thd_percent")
    return result


def demo_waveform(sample_rate_hz: float = 100_000, frequency_hz: float = 1_000, seconds: float = 0.01) -> dict:
    count = max(4, int(sample_rate_hz * seconds))
    samples = [0.8 * math.sin(2 * math.pi * frequency_hz * i / sample_rate_hz) + 0.02 for i in range(count)]
    return {"source": "built-in-demo", **analyze_samples(samples, sample_rate_hz)}


__all__ = ["analyze_samples", "compute_dft_metrics", "demo_waveform", "estimate_frequency_hysteresis"]
