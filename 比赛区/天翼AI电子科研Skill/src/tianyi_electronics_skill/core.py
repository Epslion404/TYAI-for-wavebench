"""设备发现、只读检查与示波器测量。

只用标准库；失败一律显式上报（`unsupported` / `parse_error` / `transport_error`），
不把"命令被拒绝"伪装成"解析失败"，也不把空响应当成有效状态。
"""

from __future__ import annotations

import ipaddress
import time
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed

from .analysis import analyze_samples, demo_waveform
from .dialects import MEASUREMENT_ITEMS, detect_vendor, measure_candidates
from .scpi import ScpiSession, parse_first_float, query_once
from .waveform import (
    load_waveform,
    read_waveform,
    read_waveform_in_session,
    write_waveform,
)

COMMON_PORTS = (5025, 5555, 4000, 502)


def discover_devices(subnet: str, ports: Iterable[int] = COMMON_PORTS, timeout: float = 0.35) -> list[dict]:
    """扫描 CIDR 网段并尝试读取 *IDN?，返回可序列化设备记录。"""
    network = ipaddress.ip_network(subnet, strict=False)
    targets = [(str(ip), int(port)) for ip in network.hosts() for port in ports]

    def probe(target: tuple[str, int]) -> dict | None:
        host, port = target
        try:
            ident = query_once(host, port, "*IDN?", timeout)
            return {"ip": host, "port": port, "protocol": "SCPI/TCP", "idn": ident or None,
                    "reachable": True, "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        except (OSError, UnicodeError):
            return None

    found: list[dict] = []
    with ThreadPoolExecutor(max_workers=min(64, max(1, len(targets)))) as pool:
        futures = [pool.submit(probe, target) for target in targets]
        for future in as_completed(futures):
            result = future.result()
            if result:
                found.append(result)
    return sorted(found, key=lambda item: (item["ip"], item["port"]))


def inspect_device(host: str, port: int = 5025, timeout: float = 2.0) -> dict:
    """只读检查：身份、`*OPC?`、错误队列与示波器通道状态。

    通道查询返回空串或伴随错误时记为 `display: null` + `unsupported`，不再静默丢状态。
    """
    result: dict = {
        "address": f"{host}:{port}",
        "transport": "SCPI/TCP",
        "timestamp": time.time(),
        "idn": None,
        "opc": None,
        "vendor": None,
        "channels": {},
        "device_errors": [],
        "transport_errors": [],
    }
    with ScpiSession(host, port, timeout) as session:
        for key, command in (("idn", "*IDN?"), ("opc", "*OPC?")):
            try:
                result[key] = session.query(command)
            except (OSError, ConnectionError) as exc:
                result["transport_errors"].append(f"{command}: {exc}")
        result["vendor"] = detect_vendor(result["idn"] or "")
        result["device_errors"] = session.drain_errors()
        for channel in ("CHAN1", "CHAN2", "CHAN3", "CHAN4"):
            command = f":{channel}:DISP?"
            try:
                raw = session.query(command)
            except (OSError, ConnectionError) as exc:
                result["channels"][channel] = {"display": None, "unsupported": f"transport_error: {exc}"}
                continue
            errors = session.drain_errors()
            if errors:
                result["device_errors"].extend(errors)
                result["channels"][channel] = {"display": None, "unsupported": "; ".join(errors)}
            elif not raw:
                result["channels"][channel] = {"display": None, "unsupported": "设备返回空响应"}
            else:
                result["channels"][channel] = {"display": raw}
    return result


def measure_scope(
    host: str,
    port: int = 5025,
    channel: str = "CHAN1",
    timeout: float = 2.0,
    *,
    fetch_fallback: bool = True,
) -> dict:
    """按候选方言逐个探测测量命令，只接受"可解析且错误队列干净"的响应。

    全部指标都不支持时（例如固件没有远程测量命令），默认降级为"取波形 → 离线分析"。
    """
    result: dict = {
        "address": f"{host}:{port}",
        "channel": channel,
        "timestamp": time.time(),
        "idn": None,
        "vendor": None,
        "measurements": {},
        "commands": {},
        "unsupported": {},
        "device_errors": [],
        "fetch_fallback": None,
        "errors": [],
    }
    attempts: dict[str, list[str]] = {metric: [] for metric in MEASUREMENT_ITEMS}
    try:
        with ScpiSession(host, port, timeout) as session:
            result["idn"] = session.query("*IDN?")
            result["vendor"] = detect_vendor(result["idn"])
            for metric, style, command in measure_candidates(channel):
                if metric in result["measurements"]:
                    continue
                try:
                    raw = session.query(command)
                except (OSError, ConnectionError) as exc:
                    attempts[metric].append(f"{style}: transport_error: {exc}")
                    continue
                errors = session.drain_errors()
                result["device_errors"].extend(errors)
                value = parse_first_float(raw)
                if errors:
                    attempts[metric].append(f"{style}: unsupported: {'; '.join(errors)}")
                    continue
                if not raw.strip():
                    attempts[metric].append(f"{style}: no_response: 设备未返回数据且错误队列为空")
                    continue
                if value is None:
                    attempts[metric].append(f"{style}: parse_error: 响应非数值 {raw!r}")
                    continue
                result["measurements"][metric] = value
                result["commands"][metric] = command

            result["unsupported"] = {
                metric: reasons
                for metric, reasons in attempts.items()
                if metric not in result["measurements"]
            }
            if fetch_fallback and not result["measurements"]:
                fallback = read_waveform_in_session(session, channel)
                result["fetch_fallback"] = {
                    "ok": fallback["sample_count"] > 0,
                    "metrics": fallback["metrics"],
                    "sample_count": fallback["sample_count"],
                    "warnings": fallback["warnings"],
                    "errors": fallback["errors"],
                }
    except (OSError, ValueError, ConnectionError) as exc:
        result["errors"].append(f"{type(exc).__name__}: {exc}")
    return result


__all__ = [
    "COMMON_PORTS",
    "analyze_samples",
    "demo_waveform",
    "discover_devices",
    "inspect_device",
    "load_waveform",
    "measure_scope",
    "read_waveform",
    "write_waveform",
]
