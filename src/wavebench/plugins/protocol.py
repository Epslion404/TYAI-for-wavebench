"""Offline declarative binary frame contracts (Core device RFC, stage 2).

Wire layout is header + payload + optional CRC16/Modbus + optional delimiter.
The length field counts payload bytes, or header + payload when
length_includes_header=true; it never counts CRC or delimiter. CRC covers the
header and payload, is little endian, and precedes the delimiter. Fixed length
counts the whole frame. All framings require the complete declared header.

Delimiter framing terminates at the FIRST delimiter, including one in the header
or CRC: embedded delimiters and concatenated frames are rejected. For a length
prefixed frame a delimiter is a trailer, so payload delimiters are allowed.
escape_hex is unsupported and fails closed at declaration time: no byte-stuffing
encoding has been agreed. Drivers must not interpret it as an inert annotation.

Manifests are bounded to 1 MiB, headers/frames to 64 MiB. Length fields occupy
1, 2 or 4 bytes wholly inside the header and count bytes without scaling. Timing
values are integers up to 2**31-1 ms (timeout positive, gap nonnegative). Required
fields never default to None; booleans are not accepted as integer quantities.
Sample/endian/checksum declarations use closed allowlists below. A supplied
length is checked against these bounds before any payload allocation or decoding.

This module returns original wire bytes and opens no transport. Request mapping,
payload decoding, stream splitting and executable driver selection are outside
this contract. Parsing failures use Core's bounded, payload-free transport error
attributes; they describe a supplied response, not an actual transmission here.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import tomllib
from typing import Any

from wavebench.errors import ConfigError, TransportIOError
from wavebench.transport.contracts import (
    CommandTransmission,
    ReplayPolicy,
    ResponseProgress,
    Synchronization,
    TransportPhase,
)

from .api import SUPPORTED_PLUGIN_API_VERSION, VALID_PLUGIN_KINDS, InstrumentPlugin
from .registry import PluginRegistry, plugin_doctor_records


MAX_DECLARED_FRAME_BYTES = 64 * 1024 * 1024
MAX_PROTOCOL_MANIFEST_BYTES = 1024 * 1024
MAX_PROTOCOL_TIME_MS = 2**31 - 1
_FRAMINGS = {"fixed", "length_prefixed", "delimiter"}
_ENDIANS = {"little", "big"}
_SAMPLE_TYPES = {"f32", "f64", "i16", "u16", "i32", "u32"}


@dataclass(frozen=True)
class FrameDefinition:
    framing: str
    magic: bytes
    header_bytes: int
    max_frame_bytes: int
    response_timeout_ms: int
    inter_frame_gap_ms: int
    sample_type: str
    endian: str
    checksum: str = "none"
    fixed_length: int | None = None
    length_offset: int | None = None
    length_bytes: int | None = None
    length_endian: str | None = None
    length_includes_header: bool = False
    delimiter: bytes | None = None
    escape: bytes | None = None

    def __post_init__(self) -> None:
        if self.escape is not None:
            raise ConfigError("protocol.escape_hex is unsupported; byte-stuffing encoding is not defined")

    def validate_frame(self, frame: bytes) -> bytes:
        """Validate exactly one complete wire frame without allocating from its length."""
        if not isinstance(frame, bytes):
            raise ConfigError("protocol frame must be bytes")
        size = len(frame)
        if size > self.max_frame_bytes:
            raise _frame_error("frame_too_large", size)
        if size < self.header_bytes:
            raise _frame_error("frame_header_incomplete", size)
        if not frame.startswith(self.magic):
            raise _frame_error("frame_magic_mismatch", size)
        trailer = len(self.delimiter or b"")
        crc_bytes = 2 if self.checksum == "crc16_modbus" else 0
        minimum = self.header_bytes + crc_bytes + trailer
        if self.framing == "fixed":
            if size != self.fixed_length:
                raise _frame_error("frame_length_invalid", size)
        elif self.framing == "length_prefixed":
            assert self.length_offset is not None and self.length_bytes is not None
            assert self.length_endian is not None
            end = self.length_offset + self.length_bytes
            declared = int.from_bytes(frame[self.length_offset:end], self.length_endian)
            body = declared if self.length_includes_header else self.header_bytes + declared
            if body < self.header_bytes:
                raise _frame_error("frame_length_invalid", size)
            expected = body + crc_bytes + trailer
            if expected > self.max_frame_bytes:
                raise _frame_error("frame_too_large", size)
            if size != expected:
                raise _frame_error("frame_length_invalid", size)
        if size < minimum:
            raise _frame_error("frame_length_invalid", size)
        if self.delimiter is not None:
            if not frame.endswith(self.delimiter):
                raise _frame_error("frame_delimiter_missing", size)
            if self.framing == "delimiter" and frame.find(self.delimiter) != size - trailer:
                raise _frame_error("frame_multiple_delimiters", size)
        if crc_bytes:
            payload_end = size - trailer - crc_bytes
            expected_crc = int.from_bytes(frame[payload_end:payload_end + 2], "little")
            if _crc16_modbus(frame[:payload_end]) != expected_crc:
                raise _frame_error("frame_checksum_failed", size)
        return frame


@dataclass(frozen=True)
class DeclarativeProtocolPlugin:
    plugin: InstrumentPlugin
    frame: FrameDefinition


def load_protocol_plugin(path: str | Path) -> DeclarativeProtocolPlugin:
    """Read at most 1 MiB, then validate metadata and all required frame fields."""
    plugin_path = Path(path)
    try:
        with plugin_path.open("rb") as stream:
            data = stream.read(MAX_PROTOCOL_MANIFEST_BYTES + 1)
        if len(data) > MAX_PROTOCOL_MANIFEST_BYTES:
            raise ConfigError("protocol plugin manifest exceeds maximum size")
        raw = tomllib.loads(data.decode("utf-8"))
    # tomllib may also expose Python's integer-digit limit or recursion limit.
    except (OSError, ValueError, RecursionError) as exc:
        raise ConfigError(f"unable to parse protocol plugin {plugin_path}: {exc}") from exc
    return _parse_protocol_plugin(raw)


def protocol_plugin_doctor_records(path: str | Path) -> list[tuple[str, str, str]]:
    try:
        declaration = load_protocol_plugin(path)
    except ConfigError as exc:
        return [("error", str(path), str(exc))]
    records = plugin_doctor_records(PluginRegistry((declaration.plugin,)))
    return [(record.severity, record.subject, record.message) for record in records]


def has_protocol_doctor_errors(records: list[tuple[str, str, str]]) -> bool:
    return any(severity == "error" for severity, _, _ in records)


def _parse_protocol_plugin(raw: Any) -> DeclarativeProtocolPlugin:
    if not isinstance(raw, dict) or not isinstance(raw.get("protocol"), dict):
        raise ConfigError("protocol plugin field 'protocol' must be a table")
    # Validate required frame fields before any shape arithmetic.
    frame = _parse_frame_definition(raw["protocol"])
    try:
        plugin = InstrumentPlugin(
            driver_id=_text(raw, "driver_id"),
            kind=_text(raw, "kind"),
            display_name=_text(raw, "display_name"),
            manufacturer=_text(raw, "manufacturer"),
            models=_texts(raw, "models", required=True),
            capabilities=_texts(raw, "capabilities", required=True),
            summary=_text(raw, "summary"),
            api_version=_text(raw, "api_version", SUPPORTED_PLUGIN_API_VERSION),
            package=_text(raw, "package", "local"),
            origin="local",
            idn_patterns=_texts(raw, "idn_patterns"),
            config_fields=_texts(raw, "config_fields"),
        )
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc
    if plugin.kind not in VALID_PLUGIN_KINDS:
        raise ConfigError(f"unsupported kind {plugin.kind!r}")
    if plugin.api_version != SUPPORTED_PLUGIN_API_VERSION:
        raise ConfigError(f"unsupported api_version {plugin.api_version!r}")
    return DeclarativeProtocolPlugin(plugin, frame)


def _parse_frame_definition(raw: dict[str, Any]) -> FrameDefinition:
    framing = _choice(raw, "framing", _FRAMINGS)
    magic = _hex(raw, "magic_hex", required=True)
    header = _integer(raw, "header_bytes", 1, MAX_DECLARED_FRAME_BYTES)
    maximum = _integer(raw, "max_frame_bytes", 1, MAX_DECLARED_FRAME_BYTES)
    sample_type = _choice(raw, "sample_type", _SAMPLE_TYPES)
    endian = _choice(raw, "endian", _ENDIANS)
    checksum = _choice(raw, "checksum", {"none", "crc16_modbus"}, "none")
    assert magic is not None
    if not len(magic) <= header <= maximum:
        raise ConfigError("protocol.header_bytes must be within magic length..max_frame_bytes")
    if "escape_hex" in raw:
        raise ConfigError("protocol.escape_hex is unsupported; byte-stuffing encoding is not defined")
    fixed = _optional_integer(raw, "fixed_length", 1, maximum)
    offset = _optional_integer(raw, "length_offset", 0, header)
    width = _optional_integer(raw, "length_bytes", 1, 4)
    length_endian = _choice(raw, "length_endian", _ENDIANS) if "length_endian" in raw else None
    includes = raw.get("length_includes_header", False)
    if not isinstance(includes, bool):
        raise ConfigError("protocol.length_includes_header must be a boolean")
    delimiter = _hex(raw, "delimiter_hex")
    crc_bytes = 2 if checksum == "crc16_modbus" else 0
    minimum = header + crc_bytes + len(delimiter or b"")
    if minimum > maximum:
        raise ConfigError("protocol header and trailers exceed max_frame_bytes")
    length_keys = {"length_offset", "length_bytes", "length_endian"}
    if framing == "fixed":
        if fixed is None or fixed < minimum:
            raise ConfigError("protocol.fixed_length must include header and checksum")
        if length_keys.intersection(raw) or includes or delimiter is not None:
            raise ConfigError("fixed framing must not declare length fields or delimiter")
    elif framing == "length_prefixed":
        if offset is None or width is None or length_endian is None:
            raise ConfigError("length_prefixed framing requires length_offset, length_bytes, and length_endian")
        if width not in {1, 2, 4}:
            raise ConfigError("protocol.length_bytes must be one of: 1, 2, 4")
        if offset + width > header:
            raise ConfigError("protocol length field must be fully contained in header_bytes")
        if fixed is not None:
            raise ConfigError("length_prefixed framing must not declare fixed_length")
    else:
        if delimiter is None:
            raise ConfigError("delimiter framing requires delimiter_hex")
        if fixed is not None or length_keys.intersection(raw) or includes:
            raise ConfigError("delimiter framing must not declare fixed or length fields")
    return FrameDefinition(
        framing=framing, magic=magic, header_bytes=header, max_frame_bytes=maximum,
        response_timeout_ms=_integer(raw, "response_timeout_ms", 1, MAX_PROTOCOL_TIME_MS, 1000),
        inter_frame_gap_ms=_integer(raw, "inter_frame_gap_ms", 0, MAX_PROTOCOL_TIME_MS, 0),
        sample_type=sample_type, endian=endian, checksum=checksum, fixed_length=fixed,
        length_offset=offset, length_bytes=width, length_endian=length_endian,
        length_includes_header=includes, delimiter=delimiter,
    )


def _integer(raw: dict[str, Any], key: str, minimum: int, maximum: int,
             default: int | None = None) -> int:
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ConfigError(f"protocol.{key} must be an integer within {minimum}..{maximum}")
    return value


def _optional_integer(raw: dict[str, Any], key: str, minimum: int, maximum: int) -> int | None:
    return _integer(raw, key, minimum, maximum) if key in raw else None


def _choice(raw: dict[str, Any], key: str, choices: set[str], default: str | None = None) -> str:
    value = raw.get(key, default)
    if not isinstance(value, str) or value not in choices:
        raise ConfigError(f"protocol.{key} must be one of: {', '.join(sorted(choices))}")
    return value


def _hex(raw: dict[str, Any], key: str, *, required: bool = False) -> bytes | None:
    if key not in raw and not required:
        return None
    value = raw.get(key)
    if not isinstance(value, str) or not value or len(value) % 2:
        raise ConfigError(f"protocol.{key} must be a non-empty even-length hexadecimal string")
    try:
        result = bytes.fromhex(value)
    except ValueError as exc:
        raise ConfigError(f"protocol.{key} must be hexadecimal") from exc
    if not result:
        raise ConfigError(f"protocol.{key} must be non-empty")
    return result


def _text(raw: dict[str, Any], key: str, default: str | None = None) -> str:
    value = raw.get(key, default)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"protocol plugin field {key!r} must be a non-empty string")
    return value.strip()


def _texts(raw: dict[str, Any], key: str, *, required: bool = False) -> tuple[str, ...]:
    value = raw.get(key, [])
    if not isinstance(value, list) or (required and not value):
        raise ConfigError(f"protocol plugin field {key!r} must contain a list of strings")
    return tuple(_text({key: item}, key) for item in value)


def _frame_error(reason_code: str, size: int) -> TransportIOError:
    return TransportIOError(
        "declared protocol frame failed validation", operation="protocol.validate_frame",
        phase=TransportPhase.PARSING, replay_policy=ReplayPolicy.NO_REPLAY,
        command_transmission=CommandTransmission.SENT,
        response_progress=ResponseProgress.COMPLETE if size else ResponseProgress.NONE,
        synchronization=Synchronization.LOST if size else Synchronization.UNPROVEN,
        attempts=1, reason_code=reason_code, consumed_bytes=size, discarded_bytes=size,
    )


def _crc16_modbus(data: bytes) -> int:
    crc = 0xFFFF
    for value in data:
        crc ^= value
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc & 0xFFFF


__all__ = [
    "DeclarativeProtocolPlugin", "FrameDefinition", "MAX_DECLARED_FRAME_BYTES",
    "MAX_PROTOCOL_MANIFEST_BYTES", "MAX_PROTOCOL_TIME_MS", "load_protocol_plugin",
    "protocol_plugin_doctor_records", "has_protocol_doctor_errors",
]
