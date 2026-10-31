"""Offline contract regressions informed by #90's cases and independent review.

Expected CRC comes from the standard Modbus check vector rather than repeating
the implementation. No transport, executable plugin, pip or hardware is used.
"""

import json

import pytest

from wavebench.cli import main
from wavebench.errors import ConfigError, TransportIOError, error_envelope
from wavebench.plugins.protocol import (
    MAX_DECLARED_FRAME_BYTES,
    MAX_PROTOCOL_MANIFEST_BYTES,
    MAX_PROTOCOL_TIME_MS,
    _parse_protocol_plugin,
    load_protocol_plugin,
    protocol_plugin_doctor_records,
)


METADATA = {
    "driver_id": "example.bus-scope",
    "kind": "scope",
    "display_name": "Example Bus Scope",
    "manufacturer": "Example",
    "models": ["BUS1"],
    "capabilities": ["scope.idn"],
    "summary": "Offline binary frame contract.",
}
COMMON = {
    "framing": "fixed", "magic_hex": "41", "header_bytes": 1,
    "max_frame_bytes": 64, "sample_type": "u16", "endian": "little",
}
SHAPES = {
    "fixed": {"fixed_length": 4},
    "length_prefixed": {"length_offset": 1, "length_bytes": 1, "length_endian": "little",
                        "header_bytes": 8},
    "delimiter": {"delimiter_hex": "0A"},
}


def raw_declaration(framing="fixed", **changes):
    protocol = {**COMMON, **SHAPES[framing], "framing": framing, **changes}
    return {**METADATA, "protocol": protocol}


def write_declaration(path, raw):
    def lines(table):
        # JSON strings, arrays, booleans and finite integer literals are also TOML.
        return [f"{key} = {json.dumps(value)}" for key, value in table.items()]

    path.write_text("\n".join([
        *lines({k: v for k, v in raw.items() if k != "protocol"}),
        "[protocol]", *lines(raw["protocol"]),
    ]), encoding="utf-8")
    return path


def definition(framing="fixed", **changes):
    return _parse_protocol_plugin(raw_declaration(framing, **changes)).frame


REQUIRED = [("metadata", key) for key in METADATA] + [
    ("protocol", key) for key in COMMON
]


@pytest.mark.parametrize("framing", SHAPES)
@pytest.mark.parametrize("section,key", REQUIRED)
@pytest.mark.parametrize("missing", [True, False], ids=["missing", "none"])
def test_required_matrix_never_leaks_none_or_typeerror(framing, section, key, missing):
    raw = raw_declaration(framing)
    table = raw if section == "metadata" else raw["protocol"]
    if missing:
        table.pop(key)
    else:
        table[key] = None
    with pytest.raises(ConfigError, match=key):
        _parse_protocol_plugin(raw)


@pytest.mark.parametrize("framing,key", [
    ("fixed", "fixed_length"), ("delimiter", "delimiter_hex"),
    ("length_prefixed", "length_offset"), ("length_prefixed", "length_bytes"),
    ("length_prefixed", "length_endian"),
])
@pytest.mark.parametrize("missing", [True, False], ids=["missing", "none"])
def test_shape_required_matrix(framing, key, missing):
    raw = raw_declaration(framing)
    if missing:
        raw["protocol"].pop(key)
    else:
        raw["protocol"][key] = None
    with pytest.raises(ConfigError, match=key):
        _parse_protocol_plugin(raw)


@pytest.mark.parametrize("section,key", REQUIRED)
def test_missing_required_doctor_and_cli_json(tmp_path, capsys, section, key):
    raw = raw_declaration()
    (raw if section == "metadata" else raw["protocol"]).pop(key)
    path = write_declaration(tmp_path / "missing.toml", raw)
    records = protocol_plugin_doctor_records(path)
    assert len(records) == 1
    assert records[0][0:2] == ("error", str(path))
    assert key in records[0][2]

    assert main(["plugin", "protocol", "check", str(path), "--json"]) == 2
    output = capsys.readouterr()
    payload = json.loads(output.out)
    assert not output.err
    assert payload["schema"] == "wavebench.cli.result.v1"
    assert payload["status"] == "failed" and payload["exit_code"] == 2
    assert payload["result"] == [{"severity": "error", "subject": str(path), "message": records[0][2]}]

    assert main(["--json", "plugin", "protocol", "info", str(path)]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == "wavebench.error.v1"
    assert payload["code"] == "config_error" and key in payload["message"]


def assert_frame_error(contract, wire, reason):
    with pytest.raises(TransportIOError) as raised:
        contract.validate_frame(wire)
    payload = error_envelope(raised.value)
    assert payload["code"] == "transport_io_error"
    assert payload["details"]["reason_code"] == reason
    assert payload["details"]["phase"] == "parsing"
    assert payload["details"]["replay_policy"] == "no_replay"
    assert payload["details"]["consumed_bytes"] == len(wire)
    assert payload["details"]["discarded_bytes"] == len(wire)
    assert payload["details"]["attempts"] == 1
    return payload


@pytest.mark.parametrize("framing", SHAPES)
@pytest.mark.parametrize("wire", [b"", b"A", b"A\x02", b"A\x02\n"])
def test_all_framings_require_complete_header(framing, wire):
    changes = {"header_bytes": 8}
    if framing == "fixed":
        changes["fixed_length"] = 8
    if framing == "length_prefixed":
        changes["length_includes_header"] = True
    assert_frame_error(definition(framing, **changes), wire, "frame_header_incomplete")


@pytest.mark.parametrize("checksum,delimiter,wire", [
    ("none", None, b"A\x02" + b"\x00" * 6),
    ("crc16_modbus", None, b"A\x06" + b"\x00" * 6),
    ("none", "0D0A", b"A\x06" + b"\x00" * 6),
])
def test_prefix_length_includes_header_cannot_borrow_trailer_bytes(checksum, delimiter, wire):
    changes = {"checksum": checksum, "length_includes_header": True}
    if delimiter:
        changes["delimiter_hex"] = delimiter
    assert_frame_error(definition("length_prefixed", **changes), wire, "frame_length_invalid")


@pytest.mark.parametrize("wire", [b"A1\nA2\n", b"A\n\n", b"A1\nextra\n"])
def test_delimiter_rejects_multiple_frames_and_internal_terminators(wire):
    assert_frame_error(definition("delimiter"), wire, "frame_multiple_delimiters")


def test_multibyte_overlapping_delimiter_is_not_a_single_frame():
    assert_frame_error(definition("delimiter", delimiter_hex="414241"),
                       b"AxxABABA", "frame_multiple_delimiters")


@pytest.mark.parametrize("framing", ["fixed", "length_prefixed"])
def test_escape_declaration_fails_for_non_delimiter_framing(tmp_path, capsys, framing):
    raw = raw_declaration(framing, escape_hex="5C")
    path = write_declaration(tmp_path / "escaped.toml", raw)
    with pytest.raises(ConfigError, match="escape_hex is only supported for delimiter"):
        load_protocol_plugin(path)
    assert protocol_plugin_doctor_records(path)[0][0] == "error"
    assert main(["plugin", "protocol", "info", str(path), "--json"]) == 2
    assert json.loads(capsys.readouterr().out)["code"] == "config_error"


def test_escape_declaration_is_supported_for_delimiter_framing(tmp_path):
    path = write_declaration(tmp_path / "escaped.toml", raw_declaration(
        "delimiter", escape_hex="5C",
    ))
    declaration = load_protocol_plugin(path)
    assert declaration.frame.escape == b"\\"
    assert declaration.frame.validate_frame(b"A1\\\n2\n") == b"A1\\\n2\n"


def test_escape_requires_single_byte_delimiter(tmp_path):
    raw = raw_declaration("delimiter", delimiter_hex="0D0A", escape_hex="5C")
    path = write_declaration(tmp_path / "multibyte-delim-escape.toml", raw)
    with pytest.raises(ConfigError, match="single-byte delimiter"):
        load_protocol_plugin(path)


def test_backslash_escape_is_not_silent_for_unescaped_delimiter():
    assert_frame_error(definition("delimiter"), b"A1\\\n2\n", "frame_multiple_delimiters")


@pytest.mark.parametrize("wire,expected", [
    (b"A1\\\n2\n", b"A1\n2\n"),
    (b"A123\\\n", b"A123\n"),
])
def test_escape_unescape_wire(wire, expected):
    contract = definition("delimiter", escape_hex="5C")
    assert contract._unescape_wire(wire) == expected


def test_escape_rejects_isolated_escape_byte():
    contract = definition("delimiter", escape_hex="5C")
    with pytest.raises(TransportIOError) as raised:
        contract._unescape_wire(b"A1\\")
    payload = error_envelope(raised.value)
    assert payload["details"]["reason_code"] == "frame_escape_isolated"


def test_escape_encode_stuffs_delimiter_and_escape_bytes():
    contract = definition("delimiter", delimiter_hex="0A", escape_hex="5C")
    # Payload bytes A1 0A 5C 32 0A become wire A1 5C0A 5C5C 32 5C0A.
    encoded = contract._encode_escape(b"A1\n\\2\n")
    assert encoded == b"A1\x5c\x0a\x5c\x5c2\x5c\x0a"


def test_encode_request_keeps_unescaped_terminator():
    contract = definition("delimiter", escape_hex="5C")
    assert contract.encode_request(b"A123") == b"A123\n"


def test_validate_unescapes_checksum_region_before_crc():
    # Logical header+payload 416500 has CRC16/Modbus little-endian 0a84; the
    # valid escaped wire 4165005c0a840a must validate (CRC byte 0a is escaped).
    contract = definition("delimiter", escape_hex="5C", checksum="crc16_modbus")
    wire = bytes.fromhex("4165005c0a840a")
    assert contract.validate_frame(wire) == wire


def test_extract_rejects_oversized_candidate_before_delimiter():
    contract = definition("delimiter", escape_hex="5C", max_frame_bytes=16)
    with pytest.raises(TransportIOError) as raised:
        contract.extract_stream_frame(b"A" + b"x" * 100)
    payload = error_envelope(raised.value)
    assert payload["details"]["reason_code"] == "frame_too_large"


@pytest.mark.parametrize("framing,changes,wire", [
    ("fixed", {}, b"A123"),
    ("delimiter", {}, b"A123\n"),
    ("delimiter", {"header_bytes": 4}, b"A123\n"),
    ("length_prefixed", {}, b"A\x02" + b"\x00" * 6 + b"12"),
    ("length_prefixed", {"length_includes_header": True}, b"A\x0A" + b"\x00" * 6 + b"12"),
    ("length_prefixed", {"delimiter_hex": "0A"}, b"A\x02" + b"\x00" * 6 + b"1\n\n"),
    ("length_prefixed", {"length_includes_header": True}, b"A\x08" + b"\x00" * 6),
])
def test_legal_layouts_return_original_wire_bytes(framing, changes, wire):
    assert definition(framing, **changes).validate_frame(wire) is wire


@pytest.mark.parametrize("framing,changes,wire", [
    ("fixed", {"fixed_length": 11}, b"123456789\x37\x4b"),
    ("delimiter", {"delimiter_hex": "0A"}, b"123456789\x37\x4b\n"),
])
def test_crc_known_modbus_check_vector(framing, changes, wire):
    contract = definition(framing, magic_hex="31", checksum="crc16_modbus", **changes)
    assert contract.validate_frame(wire) == wire
    assert_frame_error(contract, wire.replace(b"\x37\x4b", b"\x00\x00"), "frame_checksum_failed")


def test_length_prefixed_crc_with_trailer(tmp_path):
    # Standard Modbus read request 01 03 0000 000A has wire CRC C5 CD.
    # Its zero byte at offset 2 counts zero payload after the six-byte header.
    path = write_declaration(tmp_path / "crc.toml", raw_declaration(
        "length_prefixed", magic_hex="0103", header_bytes=6, length_offset=2,
        length_bytes=1, checksum="crc16_modbus", delimiter_hex="0D0A",
    ))
    wire = bytes.fromhex("01030000000AC5CD0D0A")
    assert load_protocol_plugin(path).frame.validate_frame(wire) == wire


def test_frame_failures_and_declared_length_overflow():
    contract = definition("length_prefixed", length_bytes=4)
    wire = b"A" + (2**32 - 1).to_bytes(4, "little") + b"\x00" * 3
    assert_frame_error(contract, wire, "frame_too_large")
    assert_frame_error(definition(), b"A" * 65, "frame_too_large")
    assert_frame_error(definition(), b"B123", "frame_magic_mismatch")
    assert_frame_error(definition(), b"A12", "frame_length_invalid")
    assert_frame_error(definition("delimiter"), b"A12", "frame_delimiter_missing")
    with pytest.raises(ConfigError, match="must be bytes"):
        definition().validate_frame(bytearray(b"A123"))


@pytest.mark.parametrize("key,value", [
    ("max_frame_bytes", MAX_DECLARED_FRAME_BYTES + 1), ("header_bytes", 65),
    ("header_bytes", True), ("header_bytes", -1), ("fixed_length", 65),
    ("response_timeout_ms", MAX_PROTOCOL_TIME_MS + 1), ("response_timeout_ms", float("inf")),
    ("inter_frame_gap_ms", float("nan")), ("inter_frame_gap_ms", -1),
    ("sample_type", "f128"), ("endian", "middle"), ("checksum", "crc32"),
    ("magic_hex", "  "), ("magic_hex", "GG"),
])
def test_declaration_bounds_and_finite_attributes(key, value):
    with pytest.raises(ConfigError, match=key):
        _parse_protocol_plugin(raw_declaration(**{key: value}))


@pytest.mark.parametrize("changes,expected", [
    ({"length_bytes": 3}, "length_bytes"),
    ({"length_offset": 7, "length_bytes": 2}, "fully contained"),
    ({"header_bytes": 64, "checksum": "crc16_modbus"}, "trailers"),
])
def test_length_header_and_trailer_declaration_bounds(changes, expected):
    with pytest.raises(ConfigError, match=expected):
        definition("length_prefixed", **changes)


@pytest.mark.parametrize("data", [
    b"\xff", b"[protocol", b"x" * (MAX_PROTOCOL_MANIFEST_BYTES + 1),
    b"x=" + b"9" * 5000, b"x=" + b"[" * 2000 + b"0" + b"]" * 2000,
], ids=["invalid_utf8", "invalid_toml", "oversize", "integer_overflow", "nesting_overflow"])
def test_manifest_parse_and_size_errors_stay_structured(tmp_path, data):
    path = tmp_path / "bad.toml"
    path.write_bytes(data)
    with pytest.raises(ConfigError):
        load_protocol_plugin(path)
    assert protocol_plugin_doctor_records(path)[0][0] == "error"


@pytest.mark.parametrize("raw", [None, {}, {"protocol": None}, {"protocol": []}])
def test_protocol_table_is_required(raw):
    with pytest.raises(ConfigError, match="protocol.*table"):
        _parse_protocol_plugin(raw)


@pytest.mark.parametrize("field,value", [("kind", "unknown"), ("api_version", "999")])
def test_existing_core_metadata_kind_and_api_contract(field, value):
    raw = raw_declaration()
    raw[field] = value
    with pytest.raises(ConfigError, match=f"unsupported {field}"):
        _parse_protocol_plugin(raw)


def test_legal_cli_json_text_and_offline_doctor(tmp_path, capsys, monkeypatch):
    path = write_declaration(tmp_path / "legal.toml", raw_declaration())
    # Neither CLI entry may discover/execute installed plugins as a side effect.
    monkeypatch.setattr("wavebench.cli.build_plugin_registry", lambda **kw: pytest.fail("registry discovery"))
    assert protocol_plugin_doctor_records(path) == [("ok", "example.bus-scope", "metadata valid")]
    for command in ("check", "info"):
        assert main(["plugin", "protocol", command, str(path), "--json"]) == 0
        output = capsys.readouterr()
        payload = json.loads(output.out)
        assert not output.err
        assert payload["schema"] == "wavebench.cli.result.v1" and payload["status"] == "ok"
        if command == "info":
            assert payload["result"]["protocol"]["magic_hex"] == "41"
            assert payload["result"]["protocol"]["fixed_length"] == 4
            assert payload["result"]["plugin"]["driver_id"] == "example.bus-scope"
        assert main(["plugin", "protocol", command, str(path)]) == 0
        text = capsys.readouterr().out
        assert "example.bus-scope" in text
        if command == "info":
            assert "protocol_fixed_length=4" in text
