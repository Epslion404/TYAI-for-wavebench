"""波形读取、时间轴校验与落盘（只用标准库）。"""

from __future__ import annotations

import csv
import json
import struct
import time
from pathlib import Path

from .analysis import analyze_samples
from .dialects import DATA_HEADER_QUERY, DATA_QUERY, detect_vendor
from .scpi import DEFAULT_MAX_BLOCK_BYTES, ScpiSession, classify_exception, validate_channel

# `DATA:HEAD?` 的字段顺序随厂商/固件变化，当前按 `xstart,xstop,xinc` 解析并做一致性校验；
# 实机确认过其它顺序后在此追加候选，而不是直接改写解析结果。
HEADER_FIELD_ORDER = "xstart,xstop,xinc"
FORMAT_COMMANDS: dict[str, tuple[str, ...]] = {
    "real": ("FORM REAL", "FORM:BORD LSBF"),
    "ascii": ("FORM ASC",),
}
ERROR_QUEUE_UNAVAILABLE = "error_queue_unavailable"


class UnsupportedCommandError(RuntimeError):
    """设备错误队列明确拒绝了配置命令。"""


def _append_failure(result: dict, category: str, message: str) -> None:
    failure = {"category": category, "message": message}
    result["failures"].append(failure)
    result["errors"].append(f"{category}: {message}")


def parse_waveform_header(text: str, sample_count: int) -> dict:
    """解析 `DATA:HEAD?` 文本，仅在字段自洽时给出采样率与起点。"""
    raw = (text or "").strip()
    numbers: list[float] = []
    for token in raw.replace(";", ",").split(","):
        token = token.strip()
        if not token:
            continue
        try:
            numbers.append(float(token))
        except ValueError:
            continue

    info: dict = {
        "header_raw": raw,
        "xstart_s": None,
        "xinc_s": None,
        "header_consistent": False,
        "warnings": [],
    }
    if len(numbers) < 3:
        info["warnings"].append(f"header 字段不足（需要 {HEADER_FIELD_ORDER}）: {raw!r}")
        return info

    xstart, xstop, xinc = numbers[0], numbers[1], numbers[2]
    info["xstart_s"] = xstart
    info["xinc_s"] = xinc
    if xinc <= 0:
        info["warnings"].append(f"header 的 xinc 非正: {xinc!r}")
        return info
    if sample_count <= 0:
        info["warnings"].append("样本数为 0，无法校验 header")
        return info

    expected = (xstop - xstart) / xinc
    if abs(expected - sample_count) > 2:
        info["warnings"].append(
            f"header 与样本数不一致: (xstop-xstart)/xinc={expected:.3f}, 样本数={sample_count}"
        )
        return info

    info["header_consistent"] = True
    return info


def decode_waveform_block(payload: bytes) -> list[float]:
    """按小端 float32 解析 REAL 波形块；长度不是 4 的倍数直接失败。"""
    if len(payload) % 4:
        raise ValueError(f"波形块长度 {len(payload)} 不是 4 字节的整数倍")
    return list(struct.unpack(f"<{len(payload) // 4}f", payload))


def decode_ascii_waveform(text: str) -> list[float]:
    """解析 `FORM ASC` 的文本列表，跳过非数值片段。"""
    values: list[float] = []
    for token in (text or "").replace(";", ",").replace("\n", ",").split(","):
        token = token.strip()
        if not token:
            continue
        try:
            values.append(float(token))
        except ValueError:
            continue
    return values


def _new_result(
    host: str,
    port: int,
    channel: str,
    points: str,
    waveform_format: str,
    sample_rate: float | None,
) -> dict:
    return {
        "address": f"{host}:{port}",
        "channel": channel,
        "timestamp": time.time(),
        "idn": None,
        "vendor": None,
        "format": waveform_format,
        "points": points,
        "header_raw": None,
        "xstart_s": None,
        "xinc_s": None,
        "header_consistent": False,
        "sample_rate_hz": float(sample_rate) if sample_rate else None,
        "sample_count": 0,
        "samples": [],
        "metrics": None,
        "warnings": [],
        "errors": [],
        "failures": [],
        "device_errors": [],
    }


def _collect_waveform(
    result: dict,
    session: ScpiSession,
    channel: str,
    *,
    points: str = "auto",
    waveform_format: str = "real",
    max_bytes: int = DEFAULT_MAX_BLOCK_BYTES,
) -> None:
    """在已打开的会话里完成一次波形读取（读取失败抛异常，由调用方记录）。"""
    channel = validate_channel(channel)
    result["channel"] = channel
    result["idn"] = session.query("*IDN?")
    result["vendor"] = detect_vendor(result["idn"])
    for command in FORMAT_COMMANDS[waveform_format]:
        session.write(command)
        rejected = session.drain_errors()
        if rejected:
            result["device_errors"].extend(rejected)
            raise UnsupportedCommandError(f"{command}: {'; '.join(rejected)}")
    if points != "auto":
        session.write(f"{channel}:DATA:POINts {points}")
        rejected = session.drain_errors()
        if rejected:
            result["device_errors"].extend(rejected)
            result["warnings"].append(f"points={points} 被设备拒绝: {'; '.join(rejected)}")

    header = session.query(DATA_HEADER_QUERY.format(channel=channel))
    command = DATA_QUERY.format(channel=channel)
    if waveform_format == "real":
        samples = decode_waveform_block(session.query_block(command, max_bytes))
    else:
        samples = decode_ascii_waveform(session.query(command))
    if not samples:
        raise ValueError("设备返回了空波形或无法解析的波形数据")

    info = parse_waveform_header(header, len(samples))
    result["header_raw"] = info["header_raw"]
    result["xstart_s"] = info["xstart_s"]
    result["xinc_s"] = info["xinc_s"]
    result["header_consistent"] = info["header_consistent"]
    result["warnings"].extend(info["warnings"])
    result["samples"] = samples
    result["sample_count"] = len(samples)

    if result["sample_rate_hz"] is None and info["header_consistent"] and info["xinc_s"]:
        result["sample_rate_hz"] = 1.0 / info["xinc_s"]
    if samples:
        result["metrics"] = analyze_samples(samples, result["sample_rate_hz"])


def read_waveform(
    host: str,
    port: int = 5025,
    channel: str = "CHAN1",
    timeout: float = 2.0,
    *,
    points: str = "auto",
    waveform_format: str = "real",
    sample_rate: float | None = None,
    max_bytes: int = DEFAULT_MAX_BLOCK_BYTES,
) -> dict:
    """读取指定通道波形，附带时间轴、一致性校验和离线指标。

    读取失败不会伪造数据：`errors` 记录失败原因，`sample_count` 保持 0。
    """
    if waveform_format not in FORMAT_COMMANDS:
        raise ValueError(f"不支持的波形格式: {waveform_format!r}（可选 {', '.join(FORMAT_COMMANDS)}）")
    channel = validate_channel(channel)

    result = _new_result(host, port, channel, points, waveform_format, sample_rate)
    try:
        with ScpiSession(host, port, timeout) as session:
            _collect_waveform(
                result,
                session,
                channel,
                points=points,
                waveform_format=waveform_format,
                max_bytes=max_bytes,
            )
            result["device_errors"].extend(session.drain_errors())
    except UnsupportedCommandError as exc:
        _append_failure(result, "unsupported", str(exc))
    except (OSError, ValueError, ConnectionError, TimeoutError) as exc:
        _append_failure(result, classify_exception(exc), str(exc))
    return result


def read_waveform_in_session(
    session: ScpiSession,
    channel: str = "CHAN1",
    *,
    points: str = "auto",
    waveform_format: str = "real",
    sample_rate: float | None = None,
    max_bytes: int = DEFAULT_MAX_BLOCK_BYTES,
) -> dict:
    """复用已打开的会话读取波形（供 measure 的降级路径使用）。"""
    channel = validate_channel(channel)
    result = _new_result(session.host, session.port, channel, points, waveform_format, sample_rate)
    try:
        _collect_waveform(
            result,
            session,
            channel,
            points=points,
            waveform_format=waveform_format,
            max_bytes=max_bytes,
        )
    except UnsupportedCommandError as exc:
        _append_failure(result, "unsupported", str(exc))
    except (OSError, ValueError, ConnectionError, TimeoutError) as exc:
        _append_failure(result, classify_exception(exc), str(exc))
    finally:
        try:
            result["device_errors"].extend(session.drain_errors())
        except (OSError, ValueError, ConnectionError, TimeoutError) as exc:
            _append_failure(result, ERROR_QUEUE_UNAVAILABLE, f"SYST:ERR?: {exc}")
    return result


def write_waveform(result: dict, path: str | Path) -> str:
    """把波形写到 CSV（time_s,value_v）或 JSON（保留时间轴与指标）。"""
    target = Path(path)
    if target.parent and not target.parent.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
    samples = [float(value) for value in result.get("samples") or []]

    if target.suffix.lower() == ".json":
        target.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
        return str(target)

    rate = result.get("sample_rate_hz")
    step = (1.0 / rate) if rate else float(result.get("xinc_s") or 1.0)
    start = float(result.get("xstart_s") or 0.0)
    with target.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["time_s", "value_v"])
        for index, value in enumerate(samples):
            writer.writerow([f"{start + index * step:.12g}", f"{value:.12g}"])
    return str(target)


def load_waveform(path: str | Path) -> tuple[list[float], float]:
    """读取 CSV（time,value 或单列 value）/JSON（samples 与 sample_rate_hz）。

    CSV 带时间列时用相邻时间的中间差值推导采样率，省去手工传 `--sample-rate`。
    """
    file_path = Path(path)
    if file_path.suffix.lower() == ".json":
        payload = json.loads(file_path.read_text(encoding="utf-8-sig"))
        return [float(v) for v in payload["samples"]], float(payload.get("sample_rate_hz") or 1.0)

    with file_path.open(newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.reader(handle))

    values: list[float] = []
    times: list[float] = []
    paired = True
    for row in rows:
        if not row:
            continue
        try:
            value = float(row[-1])
        except ValueError:
            continue
        values.append(value)
        if len(row) >= 2:
            try:
                times.append(float(row[0]))
            except ValueError:
                paired = False
        else:
            paired = False

    rate = 1.0
    if paired and len(times) == len(values) and len(values) >= 2:
        deltas = sorted(later - earlier for earlier, later in zip(times, times[1:]) if later > earlier)
        if deltas:
            median = deltas[len(deltas) // 2]
            if median > 0:
                rate = 1.0 / median
    return values, rate


__all__ = [
    "FORMAT_COMMANDS",
    "HEADER_FIELD_ORDER",
    "decode_ascii_waveform",
    "decode_waveform_block",
    "ERROR_QUEUE_UNAVAILABLE",
    "load_waveform",
    "parse_waveform_header",
    "read_waveform",
    "read_waveform_in_session",
    "write_waveform",
    "UnsupportedCommandError",
]
