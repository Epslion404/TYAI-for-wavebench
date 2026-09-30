"""设备发现、只读检查与示波器测量。

只用标准库；失败一律显式上报（`unsupported` / `parse_error` / `transport_error`），
不把"命令被拒绝"伪装成"解析失败"，也不把空响应当成有效状态。
"""

from __future__ import annotations

import ipaddress
import socket
import time
from collections.abc import Iterable
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

from .analysis import analyze_samples, demo_waveform
from .dialects import AUTOSCALE_CANDIDATES, MEASUREMENT_ITEMS, detect_vendor, measure_candidates
from .scpi import ScpiSession, classify_exception, parse_first_float
from .waveform import (
    load_waveform,
    read_waveform,
    read_waveform_in_session,
    write_waveform,
)

COMMON_PORTS = (5025, 5555, 4000, 502)
PORT_PROTOCOLS = {5025: "SCPI/TCP", 5555: "SCPI/TCP", 4000: "instrument TCP", 502: "Modbus/TCP", 1715: "VXI-11", 4880: "HiSLIP"}


def _host_count(network: ipaddress.IPv4Network | ipaddress.IPv6Network) -> int:
    if network.prefixlen == network.max_prefixlen:
        return 1
    if isinstance(network, ipaddress.IPv4Network) and network.prefixlen == 31:
        return 2
    if isinstance(network, ipaddress.IPv4Network) and network.prefixlen <= 30:
        return max(0, network.num_addresses - 2)
    return max(0, network.num_addresses - 1)


def discover_devices(
    subnet: str,
    ports: Iterable[int] = COMMON_PORTS,
    timeout: float = 0.35,
    *,
    force: bool = False,
    max_targets: int = 4096,
    workers: int = 64,
    probe_non_scpi: bool = False,
) -> list[dict]:
    """扫描 CIDR 网段，区分 SCPI、Modbus、VXI-11、HiSLIP 等端口。

    大网段必须显式 `force=True`，避免 Agent 在误解用户意图时扫描整个校园网。
    """
    if timeout <= 0:
        raise ValueError("timeout 必须大于 0")
    if workers <= 0:
        raise ValueError("workers 必须大于 0")
    if max_targets <= 0:
        raise ValueError("max_targets 必须大于 0")
    network = ipaddress.ip_network(subnet, strict=False)
    host_count = _host_count(network)
    if host_count > max_targets and not force:
        raise ValueError(f"目标数 {host_count} 超过上限 {max_targets}；确认扫描范围后使用 --force")
    normalized_ports = tuple(dict.fromkeys(int(port) for port in ports))
    if not normalized_ports:
        raise ValueError("至少需要一个扫描端口")
    invalid_ports = [port for port in normalized_ports if not 1 <= port <= 65535]
    if invalid_ports:
        raise ValueError(f"端口必须位于 1..65535: {invalid_ports}")
    targets = ((str(ip), port) for ip in network.hosts() for port in normalized_ports)

    def probe(target: tuple[str, int]) -> dict | None:
        host, port = target
        protocol = PORT_PROTOCOLS.get(port, "unknown TCP")
        try:
            if protocol == "SCPI/TCP" or probe_non_scpi:
                with ScpiSession(host, port, timeout) as session:
                    ident = session.query("*IDN?")
                return {"ip": host, "port": port, "protocol": protocol, "idn": ident or None,
                        "reachable": True, "probe": "idn", "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
            with socket.create_connection((host, port), timeout=timeout):
                pass
            return {"ip": host, "port": port, "protocol": protocol, "idn": None,
                    "reachable": True, "probe": "tcp-connect-only", "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        except (OSError, UnicodeError, ConnectionError):
            return None

    found: list[dict] = []
    max_workers = min(workers, max(1, host_count * len(normalized_ports)))
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        pending = set()

        def refill() -> None:
            while len(pending) < max_workers * 2:
                try:
                    target = next(targets)
                except StopIteration:
                    return
                pending.add(pool.submit(probe, target))

        refill()
        while pending:
            completed, pending = wait(pending, return_when=FIRST_COMPLETED)
            for future in completed:
                result = future.result()
                if result:
                    found.append(result)
            refill()
    return sorted(found, key=lambda item: (int(ipaddress.ip_address(item["ip"])), item["port"]))


def discover_mdns(timeout: float = 1.0) -> list[dict]:
    """发送最小 DNS-SD PTR 查询，返回响应主机；不猜测服务端口。"""
    if timeout <= 0:
        raise ValueError("timeout 必须大于 0")
    name = b"\x05_scpi\x04_tcp\x05local\x00"
    query = b"\x00\x00\x00\x00\x00\x01\x00\x00\x00\x00\x00\x00" + name + b"\x00\x0c\x00\x01"
    results: dict[str, dict] = {}
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP) as sock:
        sock.settimeout(timeout)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
        sock.sendto(query, ("224.0.0.251", 5353))
        while True:
            try:
                _, address = sock.recvfrom(9000)
            except TimeoutError:
                break
            results[address[0]] = {
                "ip": address[0],
                "port": None,
                "protocol": "mDNS/DNS-SD",
                "service": "_scpi._tcp.local",
                "reachable": True,
            }
    return [results[key] for key in sorted(results, key=lambda value: int(ipaddress.ip_address(value)))]


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
        "failures": [],
    }
    try:
        with ScpiSession(host, port, timeout) as session:
            for key, command in (("idn", "*IDN?"), ("opc", "*OPC?")):
                try:
                    result[key] = session.query(command)
                except (OSError, ConnectionError, TimeoutError) as exc:
                    category = classify_exception(exc)
                    result["transport_errors"].append(f"{command}: {category}: {exc}")
                    result["failures"].append({"category": category, "command": command, "message": str(exc)})
            result["vendor"] = detect_vendor(result["idn"] or "")
            try:
                result["device_errors"] = session.drain_errors()
            except (OSError, ConnectionError, TimeoutError) as exc:
                result["failures"].append({"category": classify_exception(exc), "command": "SYST:ERR?", "message": str(exc)})
            for channel in ("CHAN1", "CHAN2", "CHAN3", "CHAN4"):
                command = f":{channel}:DISP?"
                try:
                    raw = session.query(command)
                except (OSError, ConnectionError, TimeoutError) as exc:
                    category = classify_exception(exc)
                    result["channels"][channel] = {"display": None, "unsupported": f"{category}: {exc}"}
                    result["failures"].append({"category": category, "command": command, "message": str(exc)})
                    continue
                errors = session.drain_errors()
                if errors:
                    result["device_errors"].extend(errors)
                    result["channels"][channel] = {"display": None, "unsupported": "; ".join(errors)}
                elif not raw:
                    result["channels"][channel] = {"display": None, "unsupported": "设备返回空响应"}
                else:
                    result["channels"][channel] = {"display": raw}
    except (OSError, ConnectionError, TimeoutError, ValueError) as exc:
        category = classify_exception(exc)
        result["transport_errors"].append(f"connect: {category}: {exc}")
        result["failures"].append({"category": category, "command": "connect", "message": str(exc)})
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
        "failures": [],
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
                except (OSError, ConnectionError, TimeoutError) as exc:
                    category = classify_exception(exc)
                    attempts[metric].append(f"{style}: {category}: {exc}")
                    result["failures"].append({
                        "category": category,
                        "command": command,
                        "metric": metric,
                        "message": str(exc),
                    })
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
    except (OSError, ValueError, ConnectionError, TimeoutError) as exc:
        category = classify_exception(exc)
        result["errors"].append(f"{category}: {exc}")
        result["failures"].append({"category": category, "command": "connect", "message": str(exc)})
    return result


def autoscale_scope(
    host: str,
    port: int = 5025,
    channel: str = "CHAN1",
    timeout: float = 2.0,
    *,
    apply: bool = False,
) -> dict:
    """探测厂商 autoset；全部不支持时用波形推导安全建议。

    `apply=False` 只返回建议，不改变仪器；真实写入必须显式 `apply=True`。
    """
    result = {
        "address": f"{host}:{port}",
        "channel": channel,
        "mode": "dry_run" if not apply else "apply",
        "applied": False,
        "partially_applied": False,
        "commands": [],
        "write_results": [],
        "autoset_candidates": list(AUTOSCALE_CANDIDATES),
        "unsupported": [],
        "device_errors": [],
        "suggestions": {},
        "warnings": [],
        "errors": [],
        "failures": [],
    }
    try:
        with ScpiSession(host, port, timeout) as session:
            result["idn"] = session.query("*IDN?")
            result["vendor"] = detect_vendor(result["idn"] or "")
            if apply:
                for command in AUTOSCALE_CANDIDATES:
                    session.write(command)
                    errors = session.drain_errors()
                    result["device_errors"].extend(errors)
                    if errors:
                        result["unsupported"].append({"command": command, "reason": "; ".join(errors)})
                        result["write_results"].append({"command": command, "ok": False, "device_errors": errors})
                        continue
                    result["commands"].append(command)
                    result["write_results"].append({
                        "command": command,
                        "ok": True,
                        "verification": "device_error_queue_clean",
                    })
                    result["warnings"].append("通用 autoset 无可移植状态回读；成功仅表示设备错误队列为空")
                    result["applied"] = True
                    result["mode"] = "vendor_autoset"
                    return result
            waveform = read_waveform_in_session(session, channel, sample_rate=None)
            metrics = waveform.get("metrics") or {}
            frequency = metrics.get("frequency_hz")
            vpp = metrics.get("peak_to_peak_v")
            if frequency and frequency > 0:
                result["suggestions"]["timebase_range_s"] = 5.0 / frequency
            if metrics.get("minimum_v") is not None and metrics.get("maximum_v") is not None:
                result["suggestions"]["channel_offset_v"] = -(metrics["minimum_v"] + metrics["maximum_v"]) / 2.0
            if vpp:
                result["suggestions"]["vertical_range_v"] = max(vpp * 1.25, 1e-6)
            result["waveform"] = {"sample_count": waveform.get("sample_count", 0), "metrics": metrics,
                                  "warnings": waveform.get("warnings", []), "errors": waveform.get("errors", []),
                                  "failures": waveform.get("failures", [])}
            if waveform.get("device_errors"):
                result["device_errors"].extend(waveform["device_errors"])
            if not result["suggestions"]:
                result["warnings"].append("未取得足够波形指标，无法生成等效 autoscale 建议")
            if apply and result["suggestions"]:
                writes = []
                if "timebase_range_s" in result["suggestions"]:
                    writes.append(f"TIMebase:RANGe {result['suggestions']['timebase_range_s']:.12g}")
                if "channel_offset_v" in result["suggestions"]:
                    writes.append(f":{channel}:OFFS {result['suggestions']['channel_offset_v']:.12g}")
                for command in writes:
                    session.write(command)
                    errors = session.drain_errors()
                    result["device_errors"].extend(errors)
                    ok = not errors
                    result["write_results"].append({"command": command, "ok": ok, "device_errors": errors})
                    if ok:
                        result["commands"].append(command)
                    else:
                        result["unsupported"].append({"command": command, "reason": "; ".join(errors)})
                successful = sum(1 for item in result["write_results"] if item["ok"])
                result["applied"] = successful == len(writes) and bool(writes)
                result["partially_applied"] = 0 < successful < len(writes)
                result["mode"] = "derived_settings"
    except (OSError, ValueError, ConnectionError, TimeoutError) as exc:
        category = classify_exception(exc)
        result["errors"].append(f"{category}: {exc}")
        result["failures"].append({"category": category, "command": "autoscale", "message": str(exc)})
    return result


__all__ = [
    "COMMON_PORTS",
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
]
