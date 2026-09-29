"""厂商识别与测量命令方言。

方言表是"候选命令"而不是权威语法：每条候选命令都必须用 `SYST:ERR?` 验证后才算有效。
实机确认过的型号请把命中命令记录到本文件的候选表首位，供后续复用。
"""

from __future__ import annotations

MEASUREMENT_ITEMS: dict[str, str] = {
    "frequency_hz": "FREQuency",
    "vpp_v": "VPP",
    "vmax_v": "VMAX",
    "vmin_v": "VMIN",
    "vavg_v": "VAVG",
    "vrms_v": "VRMS",
}

# (风格名, 命令模板)；`{item}` 与 `{channel}` 是占位符。
DIALECT_STYLES: tuple[tuple[str, str], ...] = (
    ("measure_scalar", "MEASure:{item}? {channel}"),
    ("measure_item", "MEASure:ITEM? {item},{channel}"),
)

VENDOR_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("r&s", ("rohde", "r&s", "rtm", "rtb", "mxo")),
    ("keysight", ("keysight", "agilent")),
    ("tektronix", ("tektronix", "tek ")),
    ("rigol", ("rigol",)),
    ("siglent", ("siglent", "sds")),
)

DATA_HEADER_QUERY = "{channel}:DATA:HEAD?"
DATA_QUERY = "{channel}:DATA?"
AUTOSCALE_CANDIDATES: tuple[str, ...] = ("AUToscale", "AUTOSet EXECute", "AUToset", ":AUToscale")


def detect_vendor(idn: str) -> str:
    """按 `*IDN?` 文本归类厂商；无法识别返回 `unknown`。"""
    text = (idn or "").lower()
    for vendor, patterns in VENDOR_PATTERNS:
        if any(pattern in text for pattern in patterns):
            return vendor
    return "unknown"


def measure_candidates(channel: str) -> tuple[tuple[str, str, str], ...]:
    """展开测量候选：(指标名, 风格名, 完整命令)。"""
    candidates: list[tuple[str, str, str]] = []
    for metric, item in MEASUREMENT_ITEMS.items():
        for style, template in DIALECT_STYLES:
            candidates.append((metric, style, template.format(item=item, channel=channel)))
    return tuple(candidates)


__all__ = [
    "DATA_HEADER_QUERY",
    "DATA_QUERY",
    "AUTOSCALE_CANDIDATES",
    "DIALECT_STYLES",
    "MEASUREMENT_ITEMS",
    "VENDOR_PATTERNS",
    "detect_vendor",
    "measure_candidates",
]
