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


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="天翼AI电子科研 TeleAgent Skill")
    sub = parser.add_subparsers(dest="command", required=True)

    discover = sub.add_parser("discover", help="扫描网段并读取 SCPI *IDN?")
    discover.add_argument("--subnet", required=True)
    discover.add_argument("--ports", default="5025,5555,4000,502")
    discover.add_argument("--timeout", type=float, default=0.35, help="单次探测超时（秒）")

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

    fetch = sub.add_parser("fetch", help="读取原始波形（时间轴 + 离线指标）")
    fetch.add_argument("--address", required=True)
    fetch.add_argument("--port", type=int, default=5025)
    fetch.add_argument("--channel", default="CHAN1")
    fetch.add_argument("--timeout", type=float, default=5.0)
    fetch.add_argument("--points", default="auto", help="采样点数，如 dmax；auto 表示不改动设备设置")
    fetch.add_argument("--format", default="real", choices=("real", "ascii"))
    fetch.add_argument("--sample-rate", type=float, help="显式采样率（header 不可信时使用）")
    fetch.add_argument("--output", help="写入 *.csv 或 *.json；省略则只打印摘要")
    fetch.add_argument("--include-samples", action="store_true", help="把完整样本内联到 stdout")

    analyze = sub.add_parser("analyze", help="离线分析波形")
    analyze.add_argument("--file")
    analyze.add_argument("--sample-rate", type=float, default=100_000)
    analyze.add_argument("--demo", action="store_true")
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
            result = {"devices": discover_devices(args.subnet, ports, args.timeout)}
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
            if not args.include_samples:
                result.pop("samples", None)
        elif args.demo or not args.file:
            result = demo_waveform(args.sample_rate)
        else:
            samples, rate = load_waveform(args.file)
            result = {"source": args.file, **analyze_samples(samples, rate if rate != 1.0 else args.sample_rate)}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, KeyError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
