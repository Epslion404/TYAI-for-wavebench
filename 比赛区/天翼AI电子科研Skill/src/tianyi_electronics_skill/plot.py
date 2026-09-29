"""不依赖第三方库的单帧 SVG 波形图。"""

from __future__ import annotations

from pathlib import Path
from xml.sax.saxutils import escape

from .analysis import analyze_samples


def waveform_svg(samples: list[float], sample_rate_hz: float | None, *, title: str = "波形单帧", metrics: dict | None = None) -> str:
    if not samples:
        raise ValueError("不能绘制空波形")
    metrics = metrics or analyze_samples(samples, sample_rate_hz)
    width, height, margin = 1200, 640, 72
    plot_width, plot_height = width - margin * 2, height - margin * 2
    low, high = min(samples), max(samples)
    span = high - low or 1.0
    step = max(1, len(samples) // 1800)
    points = []
    for index in range(0, len(samples), step):
        x = margin + plot_width * index / max(1, len(samples) - 1)
        y = margin + plot_height * (high - samples[index]) / span
        points.append(f"{x:.2f},{y:.2f}")
    frequency = metrics.get("frequency_hz")
    frequency_text = "未知" if frequency is None else f"{frequency:.6g} Hz"
    caption = (
        f"峰值 {metrics['peak_v']:.6g} V   Vpp {metrics['peak_to_peak_v']:.6g} V   "
        f"RMS {metrics['rms_v']:.6g} V   频率 {frequency_text}"
    )
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">
<rect width="100%" height="100%" fill="#0b1220"/>
<text x="{margin}" y="34" fill="#ffffff" font-size="22">{escape(title)}</text>
<text x="{margin}" y="58" fill="#a9bfd5" font-size="15">{escape(caption)}</text>
<rect x="{margin}" y="{margin + 12}" width="{plot_width}" height="{plot_height - 12}" fill="#111c2e" stroke="#52708f"/>
<line x1="{margin}" y1="{margin + plot_height / 2}" x2="{margin + plot_width}" y2="{margin + plot_height / 2}" stroke="#3b5674" stroke-dasharray="5,5"/>
<polyline fill="none" stroke="#43d5ff" stroke-width="2" points="{' '.join(points)}"/>
<text x="{margin}" y="{height - 22}" fill="#a9bfd5" font-size="13">采样点 {len(samples)}；采样率 {sample_rate_hz if sample_rate_hz else '未知'} Hz</text>
</svg>'''


def write_waveform_svg(samples: list[float], sample_rate_hz: float | None, path: str | Path, *, title: str = "波形单帧", metrics: dict | None = None) -> str:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(waveform_svg(samples, sample_rate_hz, title=title, metrics=metrics), encoding="utf-8")
    return str(target)


__all__ = ["waveform_svg", "write_waveform_svg"]
