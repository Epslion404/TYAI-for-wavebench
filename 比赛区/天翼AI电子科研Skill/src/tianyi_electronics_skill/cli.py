"""命令行入口：所有结果均输出 JSON，便于 TeleAgent 解析。"""

from __future__ import annotations

import argparse
import json
import sys

from .core import (
    analyze_samples,
    demo_waveform,
    discover_devices,
    inspect_device,
    load_waveform,
    measure_scope,
    read_waveform,
    write_waveform,
)


def _local_result(*, device_errors_status: str = "not_applicable", **payload: object) -> dict[str, object]:
    """Mark local/offline output as not having queried an instrument error queue."""
    return {**payload, "device_errors": [], "device_errors_status": device_errors_status}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="天翼AI电子科研 TeleAgent Skill")
    sub = parser.add_subparsers(dest="command", required=True)

    discover = sub.add_parser("discover", help="扫描网段并读取 SCPI *IDN?")
    discover.add_argument("--subnet", required=True)
    discover.add_argument("--ports", default="5025,5555,4000,502")
    discover.add_argument("--timeout", type=float, default=0.35, help="单次探测超时（秒）")
    discover.add_argument("--workers", type=int, default=64)
    discover.add_argument("--force", action="store_true", help="允许超过 max-targets 的 IPv4 扫描；IPv6 仍受上限保护")
    discover.add_argument("--probe-non-scpi", action="store_true", help="对非 SCPI 端口也发送 *IDN?")
    discover.add_argument("--mdns", action="store_true", help="额外查询 _scpi._tcp.local")

    inspect = sub.add_parser("inspect", help="只读检查设备（身份、错误队列、通道状态）")
    inspect.add_argument("--address", required=True)
    inspect.add_argument("--port", type=int, default=5025)
    inspect.add_argument("--timeout", type=float, default=2.0)

    measure = sub.add_parser("measure", help="读取示波器测量值（方言探测 + 错误队列校验）")
    measure.add_argument("--address", required=True)
    measure.add_argument("--port", type=int, default=5025)
    measure.add_argument("--channel", default="CHAN1")
    measure.add_argument("--timeout", type=float, default=2.0)
    measure.add_argument(
        "--no-fetch-fallback",
        action="store_true",
        help="全部指标不支持时不降级为「读波形 + 离线分析」",
    )

    autoscale = sub.add_parser("autoscale", help="探测厂商 autoset 或计算等效示波器设置")
    autoscale.add_argument("--address", required=True)
    autoscale.add_argument("--port", type=int, default=5025)
    autoscale.add_argument("--channel", default="CHAN1")
    autoscale.add_argument("--timeout", type=float, default=2.0)
    autoscale.add_argument("--apply", action="store_true", help="明确授权后才写入设备设置")

    advise = sub.add_parser("advise", help="离线给出下一步建议，不连接或控制设备")
    advise.add_argument("--request", required=True, help="用户的实验或排障目标")

    fetch = sub.add_parser("fetch", help="读取原始波形（时间轴 + 离线指标）")
    fetch.add_argument("--address", required=True)
    fetch.add_argument("--port", type=int, default=5025)
    fetch.add_argument("--channel", default="CHAN1")
    fetch.add_argument("--timeout", type=float, default=5.0)
    fetch.add_argument("--points", default="auto", help="采样点数，如 dmax；auto 不写 DATA:POINTS，但仍会设置传输格式")
    fetch.add_argument("--format", default="real", choices=("real", "ascii"))
    fetch.add_argument("--sample-rate", type=float, help="显式采样率（header 不可信时使用）")
    fetch.add_argument("--output", help="写入 *.csv 或 *.json；省略则只打印摘要")
    fetch.add_argument("--plot-output", help="额外生成标注 SVG 单帧图")
    fetch.add_argument("--include-samples", action="store_true", help="把完整样本内联到 stdout")

    analyze = sub.add_parser("analyze", help="离线分析波形")
    analyze.add_argument("--file")
    analyze.add_argument("--sample-rate", type=float, default=100_000)
    analyze.add_argument("--demo", action="store_true")
    plot = sub.add_parser("plot", help="生成纯 SVG 单帧波形图")
    plot.add_argument("--file", required=True)
    plot.add_argument("--output", default="waveform.svg")
    plot.add_argument("--sample-rate", type=float, default=100_000)
    plot.add_argument("--title", default="波形单帧")

    protocol = sub.add_parser("protocol", help="识别和解析厂商自定义协议")
    protocol_sub = protocol.add_subparsers(dest="protocol_command", required=True)
    identify = protocol_sub.add_parser("identify")
    identify.add_argument("--file", required=True)
    identify.add_argument("--manifest-dir")
    decode = protocol_sub.add_parser("decode")
    decode.add_argument("--file", required=True)
    decode.add_argument("--manifest-dir")
    decode.add_argument("--sample-rate", type=float)
    can_capture = protocol_sub.add_parser("capture-can")
    can_capture.add_argument("--channel", default="can0")
    can_capture.add_argument("--count", type=int, default=16)
    can_capture.add_argument("--timeout", type=float, default=1.0)

    bridge = sub.add_parser("bridge", help="跨主机反向 TCP 桥接")
    bridge_sub = bridge.add_subparsers(dest="bridge_command", required=True)
    serve = bridge_sub.add_parser("serve")
    serve.add_argument("--listen", default="0.0.0.0:9000")
    expose = bridge_sub.add_parser("expose")
    expose.add_argument("--server", required=True)
    expose.add_argument("--token", required=True)
    expose.add_argument("--local", required=True)
    connect = bridge_sub.add_parser("connect")
    connect.add_argument("--server", required=True)
    connect.add_argument("--token", required=True)
    connect.add_argument("--listen", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    # TeleAgent 的工作区可能使用 GBK/CP1252 控制台；JSON 与中文路径统一用 UTF-8 输出。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, OSError):
            pass
    args = _parser().parse_args(argv)
    try:
        if args.command == "discover":
            ports = tuple(int(port.strip()) for port in args.ports.split(",") if port.strip())
            result = _local_result(
                devices=discover_devices(args.subnet, ports, args.timeout, force=args.force, workers=args.workers,
                                         probe_non_scpi=args.probe_non_scpi),
                device_errors_status="not_queried_during_discovery",
            )
            if args.mdns:
                from .core import discover_mdns
                result["mdns"] = discover_mdns(args.timeout)
        elif args.command == "inspect":
            result = inspect_device(args.address, args.port, args.timeout)
        elif args.command == "measure":
            result = measure_scope(
                args.address,
                args.port,
                args.channel,
                args.timeout,
                fetch_fallback=not args.no_fetch_fallback,
            )
        elif args.command == "autoscale":
            from .core import autoscale_scope
            result = autoscale_scope(args.address, args.port, args.channel, args.timeout, apply=args.apply)
        elif args.command == "advise":
            from .core import advise_next_step
            result = advise_next_step(args.request)
        elif args.command == "fetch":
            result = read_waveform(
                args.address,
                args.port,
                args.channel,
                args.timeout,
                points=args.points,
                waveform_format=args.format,
                sample_rate=args.sample_rate,
            )
            if args.output:
                result["output"] = write_waveform(result, args.output)
            if args.plot_output and result.get("samples"):
                from .plot import write_waveform_svg
                result["plot_output"] = write_waveform_svg(
                    [float(v) for v in result["samples"]], result.get("sample_rate_hz"), args.plot_output,
                    title=f"{args.address} {args.channel}", metrics=result.get("metrics"),
                )
            if not args.include_samples:
                result.pop("samples", None)
        elif args.command == "plot":
            from .plot import write_waveform_svg
            samples, rate = load_waveform(args.file)
            rate = rate if rate != 1.0 else args.sample_rate
            metrics = analyze_samples(samples, rate)
            result = _local_result(
                source=args.file,
                metrics=metrics,
                output=write_waveform_svg(samples, rate, args.output, title=args.title, metrics=metrics),
            )
        elif args.command == "protocol":
            from .protocols import ProtocolRegistry, _sample_rate, capture_socketcan, protocol_failure, read_payload
            if args.protocol_command == "capture-can":
                result = _local_result(
                    channel=args.channel,
                    frames=capture_socketcan(args.channel, args.count, args.timeout),
                    device_errors_status="not_supported_for_can_capture",
                )
            else:
                try:
                    registry = ProtocolRegistry()
                    if getattr(args, "manifest_dir", None):
                        registry.load_manifests(args.manifest_dir)
                    payload = read_payload(args.file)
                    if args.protocol_command == "identify":
                        result = _local_result(**registry.identify(payload, {"source": args.file}))
                    else:
                        result = registry.decode_result(payload, args.file)
                        if result["ok"] and args.sample_rate is not None:
                            try:
                                result["frame"]["sample_rate_hz"] = _sample_rate(args.sample_rate)
                            except ValueError as exc:
                                result = protocol_failure(exc, category="configuration_error")
                except (OSError, TypeError, ValueError, UnicodeError, KeyError) as exc:
                    result = protocol_failure(exc)
        elif args.command == "bridge":
            from .bridge import BridgeServer, run_connect, run_expose
            if args.bridge_command == "serve":
                host, port = args.listen.rsplit(":", 1)
                BridgeServer(host, int(port)).serve_forever()
            elif args.bridge_command == "expose":
                run_expose(args.server, args.token, args.local)
            else:
                run_connect(args.server, args.token, args.listen)
            return 0
        elif args.demo or not args.file:
            result = _local_result(**demo_waveform(args.sample_rate))
        else:
            samples, rate = load_waveform(args.file)
            result = {"source": args.file, **analyze_samples(samples, rate if rate != 1.0 else args.sample_rate), "device_errors": []}
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        if args.command == "autoscale":
            if not args.apply:
                # A dry run never claims to have changed the device.  It is
                # successful when the inspection itself completed; an actual
                # transport/parse failure remains an execution failure.
                return 2 if result.get("errors") or result.get("failures") else 0
            if result.get("applied"):
                return 2 if result.get("errors") else 0
            if result.get("partially_applied"):
                return 3
            return 2
        failed_fetch = args.command == "fetch" and result.get("sample_count") == 0 and bool(result.get("errors") or result.get("failures"))
        return 2 if result.get("ok") is False or failed_fetch else 0
    except (OSError, RuntimeError, ValueError, TypeError, OverflowError, UnicodeError, KeyError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
