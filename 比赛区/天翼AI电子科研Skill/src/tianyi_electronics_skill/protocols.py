"""协议判别与声明式厂商二进制/CAN 适配器（仅标准库）。"""

from __future__ import annotations

import json
import struct
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class NormalizedFrame:
    protocol: str
    source: str
    timestamp: float
    samples: list[float] = field(default_factory=list)
    sample_rate_hz: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"protocol": self.protocol, "source": self.source, "timestamp": self.timestamp,
                "samples": self.samples, "sample_rate_hz": self.sample_rate_hz, "metadata": self.metadata}


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
        return NormalizedFrame(self.name, source, time.time(), [float(v) for v in value["samples"]],
                               float(value["sample_rate_hz"]) if value.get("sample_rate_hz") else None,
                               {**(metadata or {}), **value.get("metadata", {})})


class CanAdapter(Adapter):
    name = "can"
    def match(self, payload, metadata=None) -> float:
        if isinstance(payload, dict) and ("can_id" in payload or "arbitration_id" in payload):
            return 1.0
        return 0.0
    def decode(self, payload, source, metadata=None):
        if not isinstance(payload, dict):
            payload = json.loads(payload.decode("utf-8"))
        raw = payload.get("data", [])
        data = bytes.fromhex(raw.replace("0x", "")) if isinstance(raw, str) else bytes(int(v) & 0xff for v in raw)
        samples = payload.get("samples", list(data))
        return NormalizedFrame(self.name, source, time.time(), [float(v) for v in samples],
                               payload.get("sample_rate_hz"), {**(metadata or {}), "can_id": payload.get("can_id", payload.get("arbitration_id")), "data_hex": data.hex()})


class BinaryManifestAdapter(Adapter):
    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.name = str(config["name"])
        match = config.get("match", {})
        self.magic = bytes.fromhex(match.get("magic_hex", "")) if match.get("magic_hex") else b""
        payload = config.get("payload", {})
        self.header_bytes = int(payload.get("header_bytes", len(self.magic)))
        self.fmt = {"f32": "f", "f64": "d", "i16": "h", "u16": "H", "i32": "i", "u32": "I"}[payload.get("sample_type", "f32")]
        self.endian = ">" if payload.get("endian", "big").lower() == "big" else "<"
        self.scale, self.offset = float(payload.get("scale", 1)), float(payload.get("offset", 0))
        self.rate = payload.get("sample_rate_hz")
    def match(self, payload, metadata=None) -> float:
        return 0.98 if isinstance(payload, bytes) and self.magic and payload.startswith(self.magic) else 0.0
    def decode(self, payload, source, metadata=None):
        body = payload[self.header_bytes:]
        size = struct.calcsize(self.fmt)
        body = body[:len(body) - len(body) % size]
        values = [self.offset + self.scale * v for v in struct.unpack(f"{self.endian}{len(body)//size}{self.fmt}", body)]
        return NormalizedFrame(self.name, source, time.time(), values, float(self.rate) if self.rate else None, metadata or {})


class ProtocolRegistry:
    def __init__(self):
        self.adapters: list[Adapter] = [JsonWaveformAdapter(), CanAdapter()]
    def load_manifests(self, directory: str | Path) -> int:
        count = 0
        for path in sorted(Path(directory).glob("*.json")):
            config = json.loads(path.read_text(encoding="utf-8"))
            if config.get("kind") == "binary-waveform":
                self.adapters.append(BinaryManifestAdapter(config)); count += 1
        return count
    def identify(self, payload, metadata=None) -> dict:
        scores = sorted(({"protocol": adapter.name, "confidence": adapter.match(payload, metadata)} for adapter in self.adapters),
                        key=lambda item: item["confidence"], reverse=True)
        return {"selected": scores[0] if scores and scores[0]["confidence"] else None, "candidates": scores}
    def decode(self, payload, source="unknown", metadata=None) -> NormalizedFrame:
        ranked = sorted(self.adapters, key=lambda adapter: adapter.match(payload, metadata), reverse=True)
        if not ranked or ranked[0].match(payload, metadata) <= 0:
            raise ValueError("没有匹配的协议适配器，请提供 protocol manifest")
        return ranked[0].decode(payload, source, metadata)


def read_payload(path: str | Path) -> bytes | dict[str, Any]:
    raw = Path(path).read_bytes()
    if Path(path).suffix.lower() == ".json":
        return json.loads(raw.decode("utf-8"))
    return raw


def capture_socketcan(channel: str = "can0", count: int = 16, timeout: float = 1.0) -> list[dict[str, Any]]:
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
