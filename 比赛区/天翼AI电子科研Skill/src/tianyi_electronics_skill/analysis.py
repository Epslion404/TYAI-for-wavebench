"""标准库信号分析：迟滞频率、边沿、过冲、有限点数 DFT/THD。"""

from __future__ import annotations

import math
import statistics
from collections.abc import Iterable

MAX_DFT_SAMPLES = 4096
MAX_DFT_HARMONIC = 5


def _finite_values(samples: Iterable[float]) -> list[float]:
    values = [float(value) for value in samples]
    if any(not math.isfinite(value) for value in values):
        raise ValueError("samples 不能包含 NaN 或无穷大")
    return values


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
    periods = [later - earlier for earlier, later in zip(crossings, crossings[1:]) if later > earlier]
    if not periods:
        return None, crossings
    # 中位周期能避免单个毛刺或丢失边沿把整段波形的频率拉偏。
    return sample_rate_hz / statistics.median(periods), crossings


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower, upper = math.floor(position), math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _rise_time(values: list[float], low_level: float, high_level: float, sample_rate_hz: float | None) -> float | None:
    if sample_rate_hz is None or high_level <= low_level:
        return None
    low_position: float | None = None
    for index in range(1, len(values)):
        previous, current = values[index - 1], values[index]
        if low_position is None:
            if previous < low_level <= current:
                low_position = _crossing(values, index, low_level)
                if previous < high_level <= current:
                    high_position = _crossing(values, index, high_level)
                    return (high_position - low_position) / sample_rate_hz
            continue
        if previous < high_level <= current:
            high_position = _crossing(values, index, high_level)
            return (high_position - low_position) / sample_rate_hz
        # 回落到 10% 以下说明这次边沿没有完成；下一次重新计时，
        # 避免把短毛刺的 10% 交点与后续真实边沿的 90% 交点拼接。
        if current < low_level:
            low_position = None
    return None


def compute_dft_metrics(values: list[float], sample_rate_hz: float | None, fundamental_hz: float | None) -> dict:
    if sample_rate_hz is None or fundamental_hz is None or len(values) > MAX_DFT_SAMPLES or fundamental_hz <= 0:
        return {
            "available": False,
            "reason": f"需要采样率、基波频率且样本数不超过 {MAX_DFT_SAMPLES}",
            "thd_available": False,
            "thd_percent": None,
        }
    nyquist_hz = sample_rate_hz / 2.0
    if fundamental_hz >= nyquist_hz:
        return {
            "available": False,
            "reason": "基波频率必须低于奈奎斯特频率",
            "thd_available": False,
            "thd_percent": None,
        }
    mean = statistics.fmean(values)
    centered = [value - mean for value in values]
    count = len(centered)

    def amplitude(harmonic: int) -> float:
        angle = 2.0 * math.pi * fundamental_hz * harmonic / sample_rate_hz
        real = sum(value * math.cos(angle * index) for index, value in enumerate(centered))
        imag = sum(value * math.sin(angle * index) for index, value in enumerate(centered))
        return 2.0 * math.hypot(real, imag) / count

    fundamental = amplitude(1)
    harmonic_numbers = [
        index
        for index in range(2, MAX_DFT_HARMONIC + 1)
        if fundamental_hz * index < nyquist_hz
    ]
    harmonics = {str(index): amplitude(index) for index in harmonic_numbers}
    thd = (
        math.sqrt(sum(value * value for value in harmonics.values())) / fundamental * 100
        if fundamental and harmonics
        else None
    )
    return {"available": True, "fundamental_hz": fundamental_hz, "fundamental_amplitude": fundamental,
            "nyquist_hz": nyquist_hz, "harmonics": harmonics,
            "thd_available": bool(harmonics), "thd_percent": thd}


def analyze_samples(samples: Iterable[float], sample_rate_hz: float | None, *, hysteresis: float = 0.05) -> dict:
    values = _finite_values(samples)
    if len(values) < 4:
        raise ValueError("至少需要 4 个采样点")
    if sample_rate_hz is not None and (not math.isfinite(sample_rate_hz) or sample_rate_hz <= 0):
        raise ValueError("sample_rate_hz 必须是有限正数")
    if not math.isfinite(hysteresis) or not 0 < hysteresis < 0.5:
        raise ValueError("hysteresis 必须位于 0 与 0.5 之间")
    sample_rate_hz = float(sample_rate_hz) if sample_rate_hz is not None else None
    peak = max(values, key=abs)
    mean = statistics.fmean(values)
    centered = [value - mean for value in values]
    frequency, crossings = estimate_frequency_hysteresis(values, sample_rate_hz, hysteresis)
    low_level, high_level = _percentile(values, 0.1), _percentile(values, 0.9)
    span = high_level - low_level or 1.0
    rise_time = _rise_time(
        values,
        low_level + 0.1 * span,
        low_level + 0.9 * span,
        sample_rate_hz,
    )
    # 保持早期分析接口的均值阈值语义；P10/P90 仅用于边沿/过冲，避免偏置波形的 duty 悄然漂移。
    decision_level = mean
    result = {
        "samples": len(values), "sample_rate_hz": sample_rate_hz,
        "peak_v": peak, "peak_to_peak_v": max(values) - min(values),
        "minimum_v": min(values), "maximum_v": max(values), "mean_v": mean,
        "rms_v": math.sqrt(statistics.fmean(value * value for value in centered)),
        "frequency_hz": frequency,
        "duty_cycle_percent": sum(value >= decision_level for value in values) / len(values) * 100,
        "hysteresis_fraction": hysteresis, "rising_crossings": len(crossings),
        "rise_time_10_90_s": rise_time,
        "overshoot_percent": max(0.0, (max(values) - high_level) / span * 100.0),
        "undershoot_percent": max(0.0, (low_level - min(values)) / span * 100.0),
    }
    result["dft"] = compute_dft_metrics(values, sample_rate_hz, frequency)
    result["fft"] = result["dft"]
    result["rise_time_s"] = rise_time
    result["thd_available"] = bool(result["dft"].get("thd_available"))
    result["thd_percent"] = result["dft"].get("thd_percent")
    return result


def demo_waveform(sample_rate_hz: float = 100_000, frequency_hz: float = 1_000, seconds: float = 0.01) -> dict:
    count = max(4, int(sample_rate_hz * seconds))
    samples = [0.8 * math.sin(2 * math.pi * frequency_hz * i / sample_rate_hz) + 0.02 for i in range(count)]
    return {"source": "built-in-demo", **analyze_samples(samples, sample_rate_hz)}


__all__ = ["analyze_samples", "compute_dft_metrics", "demo_waveform", "estimate_frequency_hysteresis"]
