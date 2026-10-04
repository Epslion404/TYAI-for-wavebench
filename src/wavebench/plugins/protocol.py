"""Declarative, non-SCPI frame definitions for local instrument plugins.

The module deliberately describes and validates frames only.  Opening a transport,
reading bytes, and choosing a driver remain Core responsibilities; callers pass a
complete frame from ``read_exact`` or ``read_until`` to ``validate_frame``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
import tomllib

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
_FRAMINGS = {"fixed", "length_prefixed", "delimiter"}
_CHECKSUMS = {"none", "crc16_modbus"}
_SAMPLE_TYPES = {"f32", "f64", "i16", "u16", "i32", "u32"}
_ENDIANS = {"little", "big"}


@dataclass(frozen=True)
class FrameDefinition:
    """A bounded binary frame contract declared by a local TOML plugin."""

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

    def validate_frame(self, frame: bytes) -> bytes:
        """Return one proven frame or raise a structured parsing failure.

        The returned bytes are the original on-wire frame.  Payload extraction and
        vendor field decoding belong to the executable driver, so declarations do
        not gain implicit write or device-control authority.
        """

        if not isinstance(frame, bytes):
            raise ConfigError("protocol frame must be bytes")
        if len(frame) > self.max_frame_bytes:
            raise _frame_error("frame_too_large", len(frame))
        if len(frame) < len(self.magic) or not frame.startswith(self.magic):
            raise _frame_error("frame_magic_mismatch", len(frame))
        if self.framing == "fixed":
            if len(frame) != self.fixed_length:
                raise _frame_error("frame_length_invalid", len(frame))
        elif self.framing == "length_prefixed":
            self._validate_length_prefixed_frame(frame)
        else:
            self._validate_delimiter_frame(frame)
        self._validate_checksum(frame)
        return frame

    def _validate_length_prefixed_frame(self, frame: bytes) -> None:
        assert self.length_offset is not None
        assert self.length_bytes is not None
        assert self.length_endian is not None
        field_end = self.length_offset + self.length_bytes
        if len(frame) < field_end:
            raise _frame_error("frame_header_incomplete", len(frame))
        declared = int.from_bytes(frame[self.length_offset:field_end], self.length_endian)
        trailer_bytes = 0
        if self.delimiter is not None:
            trailer_bytes = len(self.delimiter)
        checksum_bytes = 2 if self.checksum == "crc16_modbus" else 0
        expected = declared + checksum_bytes + trailer_bytes
        if not self.length_includes_header:
            expected += self.header_bytes
        if expected > self.max_frame_bytes:
            raise _frame_error("frame_too_large", len(frame))
        if len(frame) != expected:
            raise _frame_error("frame_length_invalid", len(frame))
        if self.delimiter is not None and not frame.endswith(self.delimiter):
            raise _frame_error("frame_delimiter_missing", len(frame))

    def _validate_delimiter_frame(self, frame: bytes) -> None:
        assert self.delimiter is not None
        if not frame.endswith(self.delimiter):
            raise _frame_error("frame_delimiter_missing", len(frame))

    def _validate_checksum(self, frame: bytes) -> None:
        if self.checksum == "none":
            return
        trailer = len(self.delimiter or b"")
        if len(frame) < trailer + 2:
            raise _frame_error("frame_checksum_missing", len(frame))
        payload_end = len(frame) - trailer - 2
        expected = int.from_bytes(frame[payload_end : payload_end + 2], "little")
        if _crc16_modbus(frame[:payload_end]) != expected:
            raise _frame_error("frame_checksum_failed", len(frame))


@dataclass(frozen=True)
class DeclarativeProtocolPlugin:
    plugin: InstrumentPlugin
    frame: FrameDefinition


def load_protocol_plugin(path: str | Path) -> DeclarativeProtocolPlugin:
    """Read and validate a local non-SCPI protocol TOML declaration."""

    plugin_path = Path(path)
    try:
        raw = tomllib.loads(plugin_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError) as exc:
        raise ConfigError(f"unable to read protocol plugin {plugin_path}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid protocol plugin TOML {plugin_path}: {exc}") from exc
    return _parse_protocol_plugin(raw)


def protocol_plugin_doctor_records(path: str | Path) -> list[tuple[str, str, str]]:
    try:
        protocol_plugin = load_protocol_plugin(path)
    except ConfigError as exc:
        return [("error", str(path), str(exc))]
    records = plugin_doctor_records(PluginRegistry((protocol_plugin.plugin,)))
    return [(record.severity, record.subject, record.message) for record in records]


def has_protocol_doctor_errors(records: list[tuple[str, str, str]]) -> bool:
    return any(record[0] == "error" for record in records)


def _parse_protocol_plugin(raw: Any) -> DeclarativeProtocolPlugin:
    if not isinstance(raw, dict):
        raise ConfigError("protocol plugin file must contain a TOML table")
    protocol = raw.get("protocol")
    if not isinstance(protocol, dict):
        raise ConfigError("protocol plugin field 'protocol' must be a table")
    plugin = _parse_plugin_metadata(raw)
    return DeclarativeProtocolPlugin(plugin=plugin, frame=_parse_frame_definition(protocol))


def _parse_plugin_metadata(raw: dict[str, Any]) -> InstrumentPlugin:
    try:
        plugin = InstrumentPlugin(
            driver_id=_required_text(raw, "driver_id"),
            kind=_required_text(raw, "kind"),
            display_name=_required_text(raw, "display_name"),
            manufacturer=_required_text(raw, "manufacturer"),
            models=_required_text_tuple(raw, "models"),
            capabilities=_required_text_tuple(raw, "capabilities"),
            summary=_required_text(raw, "summary"),
            api_version=_optional_text(raw, "api_version", SUPPORTED_PLUGIN_API_VERSION),
            package=_optional_text(raw, "package", "local"),
            origin="local",
            idn_patterns=_optional_text_tuple(raw, "idn_patterns"),
            config_fields=_optional_text_tuple(raw, "config_fields"),
        )
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc
    if plugin.kind not in VALID_PLUGIN_KINDS:
        raise ConfigError(f"unsupported kind {plugin.kind!r}; expected one of {', '.join(VALID_PLUGIN_KINDS)}")
    if plugin.api_version != SUPPORTED_PLUGIN_API_VERSION:
        raise ConfigError(
            f"unsupported api_version {plugin.api_version!r}; expected {SUPPORTED_PLUGIN_API_VERSION!r}"
        )
    return plugin


def _parse_frame_definition(raw: dict[str, Any]) -> FrameDefinition:
    framing = _required_choice(raw, "framing", _FRAMINGS)
    magic = _required_hex(raw, "magic_hex")
    max_frame_bytes = _required_positive_int(raw, "max_frame_bytes")
    if max_frame_bytes > MAX_DECLARED_FRAME_BYTES:
        raise ConfigError(f"protocol.max_frame_bytes must be <= {MAX_DECLARED_FRAME_BYTES}")
    header_bytes = _required_positive_int(raw, "header_bytes")
    if header_bytes < len(magic) or header_bytes > max_frame_bytes:
        raise ConfigError("protocol.header_bytes must be within magic length..max_frame_bytes")
    checksum = _optional_choice(raw, "checksum", "none", _CHECKSUMS)
    definition = FrameDefinition(
        framing=framing,
        magic=magic,
        header_bytes=header_bytes,
        max_frame_bytes=max_frame_bytes,
        response_timeout_ms=_optional_positive_int(raw, "response_timeout_ms", 1_000),
        inter_frame_gap_ms=_optional_nonnegative_int(raw, "inter_frame_gap_ms", 0),
        sample_type=_required_choice(raw, "sample_type", _SAMPLE_TYPES),
        endian=_required_choice(raw, "endian", _ENDIANS),
        checksum=checksum,
        fixed_length=_optional_positive_int(raw, "fixed_length", None),
        length_offset=_optional_nonnegative_int(raw, "length_offset", None),
        length_bytes=_optional_choice_int(raw, "length_bytes", None, {1, 2, 4}),
        length_endian=_optional_choice(raw, "length_endian", None, {"little", "big"}),
        length_includes_header=_optional_bool(raw, "length_includes_header", False),
        delimiter=_optional_hex(raw, "delimiter_hex"),
        escape=_optional_hex(raw, "escape_hex"),
    )
    _validate_frame_shape(definition)
    return definition


def _validate_frame_shape(frame: FrameDefinition) -> None:
    if frame.framing == "fixed":
        if frame.fixed_length is None:
            raise ConfigError("protocol.fixed_length is required for fixed framing")
        if not frame.header_bytes <= frame.fixed_length <= frame.max_frame_bytes:
            raise ConfigError("protocol.fixed_length must be within header_bytes..max_frame_bytes")
        if any(value is not None for value in (frame.length_offset, frame.length_bytes, frame.length_endian)):
            raise ConfigError("fixed framing must not declare length-prefixed fields")
        if frame.delimiter is not None or frame.escape is not None:
            raise ConfigError("fixed framing must not declare delimiter or escape")
        return
    if frame.framing == "length_prefixed":
        if None in (frame.length_offset, frame.length_bytes, frame.length_endian):
            raise ConfigError("length_prefixed framing requires length_offset, length_bytes, and length_endian")
        assert frame.length_offset is not None and frame.length_bytes is not None
        if frame.length_offset + frame.length_bytes > frame.header_bytes:
            raise ConfigError("protocol length field must be fully contained in header_bytes")
        if frame.fixed_length is not None:
            raise ConfigError("length_prefixed framing must not declare fixed_length")
        return
    if frame.delimiter is None:
        raise ConfigError("delimiter framing requires delimiter_hex")
    if frame.header_bytes != len(frame.magic):
        raise ConfigError("delimiter framing header_bytes must equal the magic length")
    if frame.escape is not None and frame.escape == frame.delimiter:
        raise ConfigError("protocol.escape_hex must differ from delimiter_hex")
    if any(value is not None for value in (frame.fixed_length, frame.length_offset, frame.length_bytes, frame.length_endian)):
        raise ConfigError("delimiter framing must not declare fixed or length-prefixed fields")


def _frame_error(reason_code: str, consumed_bytes: int) -> TransportIOError:
    return TransportIOError(
        "declared protocol frame failed validation",
        operation="protocol.validate_frame",
        phase=TransportPhase.PARSING,
        replay_policy=ReplayPolicy.NO_REPLAY,
        command_transmission=CommandTransmission.SENT,
        response_progress=ResponseProgress.COMPLETE if consumed_bytes else ResponseProgress.NONE,
        synchronization=Synchronization.LOST if consumed_bytes else Synchronization.UNPROVEN,
        attempts=1,
        reason_code=reason_code,
        consumed_bytes=consumed_bytes,
        discarded_bytes=consumed_bytes,
    )


def _crc16_modbus(data: bytes) -> int:
    crc = 0xFFFF
    for value in data:
        crc ^= value
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc & 0xFFFF


def _required_text(raw: dict[str, Any], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"protocol plugin field {key!r} must be a non-empty string")
    return value.strip()


def _optional_text(raw: dict[str, Any], key: str, default: str) -> str:
    return _required_text({key: raw.get(key, default)}, key)


def _required_text_tuple(raw: dict[str, Any], key: str) -> tuple[str, ...]:
    result = _optional_text_tuple(raw, key)
    if not result:
        raise ConfigError(f"protocol plugin field {key!r} must contain at least one string")
    return result


def _optional_text_tuple(raw: dict[str, Any], key: str) -> tuple[str, ...]:
    values = raw.get(key, [])
    if not isinstance(values, list):
        raise ConfigError(f"protocol plugin field {key!r} must be a list")
    if any(not isinstance(value, str) or not value.strip() for value in values):
        raise ConfigError(f"protocol plugin field {key!r} must contain strings")
    return tuple(value.strip() for value in values)


def _required_positive_int(raw: dict[str, Any], key: str) -> int:
    return _optional_positive_int(raw, key, None)


def _optional_positive_int(raw: dict[str, Any], key: str, default: int | None) -> int | None:
    value = raw.get(key, default)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ConfigError(f"protocol.{key} must be a positive integer")
    return value


def _optional_nonnegative_int(raw: dict[str, Any], key: str, default: int | None) -> int | None:
    value = raw.get(key, default)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ConfigError(f"protocol.{key} must be a non-negative integer")
    return value


def _required_choice(raw: dict[str, Any], key: str, choices: set[str]) -> str:
    return _optional_choice(raw, key, None, choices)


def _optional_choice(raw: dict[str, Any], key: str, default: str | None, choices: set[str]) -> str | None:
    value = raw.get(key, default)
    if value is None:
        return None
    if not isinstance(value, str) or value not in choices:
        raise ConfigError(f"protocol.{key} must be one of: {', '.join(sorted(choices))}")
    return value


def _optional_choice_int(raw: dict[str, Any], key: str, default: int | None, choices: set[int]) -> int | None:
    value = raw.get(key, default)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value not in choices:
        raise ConfigError(f"protocol.{key} must be one of: {', '.join(str(item) for item in sorted(choices))}")
    return value


def _optional_bool(raw: dict[str, Any], key: str, default: bool) -> bool:
    value = raw.get(key, default)
    if not isinstance(value, bool):
        raise ConfigError(f"protocol.{key} must be a boolean")
    return value


def _required_hex(raw: dict[str, Any], key: str) -> bytes:
    value = _optional_hex(raw, key)
    if value is None:
        raise ConfigError(f"protocol.{key} must be a non-empty hexadecimal string")
    return value


def _optional_hex(raw: dict[str, Any], key: str) -> bytes | None:
    value = raw.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) % 2:
        raise ConfigError(f"protocol.{key} must be a non-empty even-length hexadecimal string")
    try:
        return bytes.fromhex(value)
    except ValueError as exc:
        raise ConfigError(f"protocol.{key} must be hexadecimal") from exc


__all__ = [
    "DeclarativeProtocolPlugin",
    "FrameDefinition",
    "MAX_DECLARED_FRAME_BYTES",
    "has_protocol_doctor_errors",
    "load_protocol_plugin",
    "protocol_plugin_doctor_records",
]
