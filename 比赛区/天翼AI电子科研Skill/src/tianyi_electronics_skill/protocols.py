"""协议判别与声明式厂商二进制/CAN 适配器（仅标准库）。"""

from __future__ import annotations

import json
import math
import struct
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MAX_PROTOCOL_PAYLOAD_BYTES = 64 * 1024 * 1024
SAMPLE_FORMATS = {"f32": "f", "f64": "d", "i16": "h", "u16": "H", "i32": "i", "u32": "I"}


class ProtocolDecodeError(ValueError):
    """A payload or manifest cannot be safely interpreted as a declared protocol."""


def protocol_failure(error: BaseException, *, category: str = "parse_error") -> dict[str, Any]:
    """Stable, machine-readable failure shape for malformed protocol input."""
    return {
        "ok": False,
        "frame": None,
        "failures": [{"category": category, "message": str(error)}],
        "device_errors": [],
    }


def _finite_number(value: Any, *, field: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ProtocolDecodeError(f"{field} must be a finite number") from exc
    if not math.isfinite(number):
        raise ProtocolDecodeError(f"{field} must be a finite number")
    return number


def _samples(values: Any) -> list[float]:
    if not isinstance(values, (list, tuple)):
        raise ValueError("samples 必须是数组")
    samples = [_finite_number(value, field="samples") for value in values]
    if not samples:
        raise ValueError("samples 不能为空")
    if any(not math.isfinite(value) for value in samples):
        raise ValueError("samples 不能包含 NaN 或无穷大")
    return samples


def _sample_rate(value: Any) -> float | None:
    if value is None:
        return None
    rate = _finite_number(value, field="sample_rate_hz")
    if not math.isfinite(rate) or rate <= 0:
        raise ValueError("sample_rate_hz 必须是有限正数")
    return rate


def _device_errors(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)) or any(not isinstance(item, str) for item in value):
        raise ValueError("device_errors 必须是字符串数组")
    return list(value)


def _metadata_with_errors(metadata: dict[str, Any] | None, payload_errors: Any = None) -> tuple[dict[str, Any], list[str]]:
    copied = dict(metadata or {})
    metadata_errors = _device_errors(copied.pop("device_errors", None))
    errors = list(dict.fromkeys(metadata_errors + _device_errors(payload_errors)))
    return copied, errors


@dataclass
class NormalizedFrame:
    protocol: str
    source: str
    timestamp: float
    samples: list[float] = field(default_factory=list)
    sample_rate_hz: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    device_errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"protocol": self.protocol, "source": self.source, "timestamp": self.timestamp,
                "samples": self.samples, "sample_rate_hz": self.sample_rate_hz, "metadata": self.metadata,
                "device_errors": list(self.device_errors)}


class Adapter:
    name = "unknown"
    def match(self, payload: bytes | dict[str, Any], metadata: dict[str, Any] | None = None) -> float:
        return 0.0
    def decode(self, payload: bytes | dict[str, Any], source: str, metadata: dict[str, Any] | None = None) -> NormalizedFrame:
        raise NotImplementedError


class JsonWaveformAdapter(Adapter):
    name = "json-waveform"
    def match(self, payload, metadata=None) -> float:
        if isinstance(payload, dict):
            return 1.0 if "samples" in payload else 0.0
        try:
            value = json.loads(payload.decode("utf-8"))
            return 1.0 if isinstance(value, dict) and "samples" in value else 0.0
        except (AttributeError, UnicodeDecodeError, json.JSONDecodeError):
            return 0.0
    def decode(self, payload, source, metadata=None):
        value = payload if isinstance(payload, dict) else json.loads(payload.decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("JSON 波形必须是对象")
        embedded_metadata = value.get("metadata", {})
        if not isinstance(embedded_metadata, dict):
            raise ValueError("metadata 必须是对象")
        embedded_metadata = dict(embedded_metadata)
        embedded_errors = embedded_metadata.pop("device_errors", None)
        outer_metadata, device_errors = _metadata_with_errors(metadata, value.get("device_errors"))
        device_errors = list(dict.fromkeys(device_errors + _device_errors(embedded_errors)))
        return NormalizedFrame(self.name, source, time.time(), _samples(value["samples"]),
                               _sample_rate(value.get("sample_rate_hz")),
                               {**outer_metadata, **embedded_metadata}, device_errors)


class CanAdapter(Adapter):
    name = "can"
    def match(self, payload, metadata=None) -> float:
        if isinstance(payload, dict) and ("can_id" in payload or "arbitration_id" in payload):
            return 1.0
        return 0.0
    def decode(self, payload, source, metadata=None):
        if not isinstance(payload, dict):
            payload = json.loads(payload.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("CAN payload 必须是对象")
        raw = payload.get("data", [])
        try:
            data = bytes.fromhex(raw.replace("0x", "")) if isinstance(raw, str) else bytes(int(v) for v in raw)
            can_id = int(payload.get("can_id", payload.get("arbitration_id")))
        except (TypeError, ValueError) as exc:
            raise ValueError("CAN data 必须是十六进制字符串或 0..255 字节数组，且必须提供 CAN ID") from exc
        if not 0 <= can_id <= 0x1FFFFFFF:
            raise ValueError("CAN ID 必须位于 0..0x1FFFFFFF")
        samples = payload.get("samples", list(data))
        outer_metadata, device_errors = _metadata_with_errors(metadata, payload.get("device_errors"))
        return NormalizedFrame(self.name, source, time.time(), _samples(samples),
                               _sample_rate(payload.get("sample_rate_hz")),
                               {**outer_metadata, "can_id": can_id, "data_hex": data.hex()}, device_errors)


class BinaryManifestAdapter(Adapter):
    def __init__(self, config: dict[str, Any]):
        if not isinstance(config, dict):
            raise ProtocolDecodeError("protocol manifest must be an object")
        self.config = config
        self.name = str(config.get("name", "")).strip()
        if not self.name:
            raise ValueError("协议 manifest 缺少非空 name")
        match = config.get("match", {})
        if not isinstance(match, dict):
            raise ProtocolDecodeError(f"protocol {self.name} match must be an object")
        magic_hex = match.get("magic_hex", "")
        if not isinstance(magic_hex, str):
            raise ProtocolDecodeError(f"protocol {self.name} magic_hex must be a string")
        try:
            self.magic = bytes.fromhex(magic_hex)
        except ValueError as exc:
            raise ValueError(f"协议 {self.name} 的 magic_hex 非法") from exc
        if not self.magic:
            raise ValueError(f"协议 {self.name} 必须声明 magic_hex，避免误识别任意二进制数据")
        payload = config.get("payload", {})
        if not isinstance(payload, dict):
            raise ProtocolDecodeError(f"protocol {self.name} payload must be an object")
        header_bytes = payload.get("header_bytes", len(self.magic))
        if isinstance(header_bytes, bool) or not isinstance(header_bytes, int):
            raise ProtocolDecodeError(f"protocol {self.name} header_bytes must be an integer")
        self.header_bytes = header_bytes
        if self.header_bytes < len(self.magic):
            raise ValueError(f"协议 {self.name} 的 header_bytes 不能小于 magic 长度")
        sample_type = str(payload.get("sample_type", "f32"))
        if sample_type not in SAMPLE_FORMATS:
            raise ValueError(f"协议 {self.name} 的 sample_type 不受支持: {sample_type}")
        self.fmt = SAMPLE_FORMATS[sample_type]
        endian = str(payload.get("endian", "big")).lower()
        if endian not in {"big", "little"}:
            raise ValueError(f"协议 {self.name} 的 endian 必须是 big 或 little")
        self.endian = ">" if endian == "big" else "<"
        self.scale = _finite_number(payload.get("scale", 1), field="scale")
        self.offset = _finite_number(payload.get("offset", 0), field="offset")
        if not math.isfinite(self.scale) or not math.isfinite(self.offset):
            raise ValueError(f"协议 {self.name} 的 scale/offset 必须是有限数")
        self.rate = _sample_rate(payload.get("sample_rate_hz"))
        max_payload_bytes = payload.get("max_payload_bytes", MAX_PROTOCOL_PAYLOAD_BYTES)
        if isinstance(max_payload_bytes, bool) or not isinstance(max_payload_bytes, int):
            raise ProtocolDecodeError(f"protocol {self.name} max_payload_bytes must be an integer")
        self.max_payload_bytes = max_payload_bytes
        if self.max_payload_bytes <= 0 or self.max_payload_bytes > MAX_PROTOCOL_PAYLOAD_BYTES:
            raise ValueError(f"协议 {self.name} 的 max_payload_bytes 超出允许范围")
    def match(self, payload, metadata=None) -> float:
        return 0.98 if isinstance(payload, bytes) and self.magic and payload.startswith(self.magic) else 0.0
    def decode(self, payload, source, metadata=None):
        if not isinstance(payload, bytes):
            raise TypeError("二进制协议适配器需要 bytes")
        if not payload.startswith(self.magic):
            raise ValueError(f"数据不匹配协议 {self.name} 的 magic")
        if len(payload) > self.max_payload_bytes:
            raise ValueError(f"协议负载 {len(payload)} 字节超过上限 {self.max_payload_bytes}")
        if len(payload) < self.header_bytes:
            raise ValueError(f"协议负载短于 header_bytes={self.header_bytes}")
        body = payload[self.header_bytes:]
        size = struct.calcsize(self.fmt)
        if not body:
            raise ValueError("协议负载不包含样本数据")
        if len(body) % size:
            raise ValueError(f"样本区长度 {len(body)} 不是元素大小 {size} 的整数倍")
        values = [self.offset + self.scale * v for v in struct.unpack(f"{self.endian}{len(body)//size}{self.fmt}", body)]
        frame_metadata, device_errors = _metadata_with_errors(metadata)
        return NormalizedFrame(
            self.name, source, time.time(), _samples(values), self.rate, frame_metadata, device_errors
        )


class ProtocolRegistry:
    def __init__(self):
        self.adapters: list[Adapter] = [JsonWaveformAdapter(), CanAdapter()]
    def load_manifests(self, directory: str | Path) -> int:
        root = Path(directory)
        if not root.is_dir():
            raise ValueError(f"protocol manifest 目录不存在: {root}")
        count = 0
        names = {adapter.name for adapter in self.adapters}
        paths = sorted((*root.glob("*.json"), *root.glob("*.toml")))
        for path in paths:
            try:
                raw = path.read_text(encoding="utf-8")
                config = tomllib.loads(raw) if path.suffix.lower() == ".toml" else json.loads(raw)
            except (OSError, json.JSONDecodeError, tomllib.TOMLDecodeError) as exc:
                raise ProtocolDecodeError(f"protocol manifest cannot be parsed: {path.name}: {exc}") from exc
            if not isinstance(config, dict):
                raise ProtocolDecodeError(f"protocol manifest must be an object: {path.name}")
            if config.get("kind") == "binary-waveform":
                adapter = BinaryManifestAdapter(config)
                if adapter.name in names:
                    raise ValueError(f"重复的协议名称: {adapter.name}")
                self.adapters.append(adapter)
                names.add(adapter.name)
                count += 1
        return count
    def identify(self, payload, metadata=None) -> dict:
        scores = sorted(({"protocol": adapter.name, "confidence": adapter.match(payload, metadata)} for adapter in self.adapters),
                        key=lambda item: item["confidence"], reverse=True)
        return {"selected": scores[0] if scores and scores[0]["confidence"] else None, "candidates": scores}
    def decode(self, payload, source="unknown", metadata=None) -> NormalizedFrame:
        ranked = sorted(
            ((adapter.match(payload, metadata), adapter) for adapter in self.adapters),
            key=lambda item: item[0],
            reverse=True,
        )
        if not ranked or ranked[0][0] <= 0:
            raise ValueError("没有匹配的协议适配器，请提供 protocol manifest")
        return ranked[0][1].decode(payload, source, metadata)

    def decode_result(self, payload, source="unknown", metadata=None) -> dict[str, Any]:
        """Decode a protocol frame without making callers parse exception text."""
        try:
            frame = self.decode(payload, source, metadata).as_dict()
            return {"ok": True, "frame": frame, "failures": [], "device_errors": list(frame["device_errors"])}
        except (TypeError, ValueError, OverflowError, UnicodeError, json.JSONDecodeError) as exc:
            return protocol_failure(exc)


def read_payload(path: str | Path, max_bytes: int = MAX_PROTOCOL_PAYLOAD_BYTES) -> bytes | dict[str, Any]:
    file_path = Path(path)
    size = file_path.stat().st_size
    if size > max_bytes:
        raise ValueError(f"输入文件 {size} 字节超过上限 {max_bytes}")
    raw = file_path.read_bytes()
    if file_path.suffix.lower() == ".json":
        return json.loads(raw.decode("utf-8"))
    return raw


def capture_socketcan(channel: str = "can0", count: int = 16, timeout: float = 1.0) -> list[dict[str, Any]]:
    if not channel.strip():
        raise ValueError("SocketCAN channel 不能为空")
    if count <= 0:
        raise ValueError("count 必须大于 0")
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout 必须是有限正数")
    try:
        import can  # type: ignore
    except ImportError as exc:
        raise RuntimeError("SocketCAN 需要可选依赖 python-can") from exc
    bus = can.Bus(interface="socketcan", channel=channel)
    frames = []
    try:
        while len(frames) < count:
            message = bus.recv(timeout=timeout)
            if message is None:
                break
            frames.append({"can_id": int(message.arbitration_id), "data": list(message.data), "timestamp": float(message.timestamp)})
    finally:
        bus.shutdown()
    return frames
