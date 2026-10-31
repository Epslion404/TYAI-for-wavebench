import pytest

from wavebench.errors import ConfigError, TransportIOError, error_envelope
from wavebench.plugins.protocol import load_protocol_plugin, protocol_plugin_doctor_records


def write_protocol_plugin(path, protocol):
    path.write_text(
        "\n".join(
            [
                'driver_id = "example.bus-scope"',
                'kind = "scope"',
                'display_name = "Example Bus Scope"',
                'manufacturer = "Example"',
                'models = ["BUS1"]',
                'capabilities = ["scope.idn"]',
                'summary = "Example declarative binary protocol."',
                'config_fields = ["resource"]',
                "",
                "[protocol]",
                protocol,
            ]
        ),
        encoding="utf-8",
    )


def test_load_protocol_plugin_validates_length_prefixed_frame(tmp_path):
    path = tmp_path / "plugin.toml"
    write_protocol_plugin(
        path,
        '''
framing = "length_prefixed"
magic_hex = "AA55"
header_bytes = 4
length_offset = 2
length_bytes = 2
length_endian = "little"
max_frame_bytes = 32
response_timeout_ms = 250
inter_frame_gap_ms = 5
checksum = "crc16_modbus"
sample_type = "u16"
endian = "little"
'''.strip(),
    )

    plugin = load_protocol_plugin(path)
    prefix = b"\xAA\x55\x02\x00"
    crc = _crc16_modbus(prefix + b"\x01\x02").to_bytes(2, "little")
    frame = prefix + b"\x01\x02" + crc

    assert plugin.plugin.driver_id == "example.bus-scope"
    assert plugin.frame.response_timeout_ms == 250
    assert plugin.frame.validate_frame(frame) == frame


@pytest.mark.parametrize(
    "protocol, expected",
    [
        ('framing = "fixed"\nheader_bytes = 1\nfixed_length = 4\nmax_frame_bytes = 8\n'
         'sample_type = "u16"\nendian = "little"', "magic_hex"),
        (
            'framing = "length_prefixed"\nmagic_hex = "AA"\nheader_bytes = 1\n'
            'length_offset = 0\nlength_bytes = 2\nlength_endian = "little"\nmax_frame_bytes = 8\n'
            'sample_type = "u16"\nendian = "little"',
            "fully contained",
        ),
        (
            'framing = "delimiter"\nmagic_hex = "AA"\nheader_bytes = 1\n'
            'delimiter_hex = "0D0A"\nescape_hex = "0D0A"\nmax_frame_bytes = 8\n'
            'sample_type = "u16"\nendian = "little"',
            "escape_hex is unsupported",
        ),
    ],
)
def test_load_protocol_plugin_rejects_invalid_frame_shapes(tmp_path, protocol, expected):
    path = tmp_path / "plugin.toml"
    write_protocol_plugin(path, protocol)

    with pytest.raises(ConfigError, match=expected):
        load_protocol_plugin(path)


def test_protocol_frame_failure_has_a_stable_structured_transport_envelope(tmp_path):
    path = tmp_path / "plugin.toml"
    write_protocol_plugin(
        path,
        '''
framing = "fixed"
magic_hex = "AA55"
header_bytes = 2
fixed_length = 4
max_frame_bytes = 4
sample_type = "u16"
endian = "little"
'''.strip(),
    )
    definition = load_protocol_plugin(path).frame

    with pytest.raises(TransportIOError) as raised:
        definition.validate_frame(b"\xAA\x55\x01")

    payload = error_envelope(raised.value)
    assert payload["code"] == "transport_io_error"
    assert payload["details"]["phase"] == "parsing"
    assert payload["details"]["reason_code"] == "frame_length_invalid"
    assert payload["details"]["discarded_bytes"] == 3


@pytest.mark.parametrize(
    ("frame", "reason_code"),
    [
        (b"\xAA\x55" + b"\x00" * 31, "frame_too_large"),
        (b"\xAA\x55\x02\x00\x01\x02\x00\x00", "frame_checksum_failed"),
    ],
)
def test_protocol_frame_limits_and_checksum_fail_with_structured_reasons(tmp_path, frame, reason_code):
    path = tmp_path / "plugin.toml"
    write_protocol_plugin(
        path,
        '''
framing = "length_prefixed"
magic_hex = "AA55"
header_bytes = 4
length_offset = 2
length_bytes = 2
length_endian = "little"
max_frame_bytes = 16
checksum = "crc16_modbus"
sample_type = "u16"
endian = "little"
'''.strip(),
    )
    definition = load_protocol_plugin(path).frame

    with pytest.raises(TransportIOError) as raised:
        definition.validate_frame(frame)

    assert error_envelope(raised.value)["details"]["reason_code"] == reason_code


def test_protocol_plugin_doctor_is_offline_metadata_validation(tmp_path):
    path = tmp_path / "plugin.toml"
    write_protocol_plugin(
        path,
        '''
framing = "delimiter"
magic_hex = "A5"
header_bytes = 1
delimiter_hex = "0D0A"
max_frame_bytes = 64
sample_type = "u16"
endian = "little"
'''.strip(),
    )

    assert protocol_plugin_doctor_records(path) == [
        ("ok", "example.bus-scope", "metadata valid")
    ]


@pytest.mark.parametrize(
    ("sample_type", "endian", "expected"),
    [
        ("f128", "little", "sample_type must be one of"),
        ("u16", "middle", "endian must be one of"),
    ],
)
def test_protocol_plugin_requires_supported_sample_layout(tmp_path, sample_type, endian, expected):
    path = tmp_path / "plugin.toml"
    write_protocol_plugin(
        path,
        "\n".join(
            (
                'framing = "fixed"',
                'magic_hex = "AA55"',
                "header_bytes = 2",
                "fixed_length = 4",
                "max_frame_bytes = 4",
                f'sample_type = "{sample_type}"',
                f'endian = "{endian}"',
            )
        ),
    )

    with pytest.raises(ConfigError, match=expected):
        load_protocol_plugin(path)


def _crc16_modbus(data):
    crc = 0xFFFF
    for value in data:
        crc ^= value
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc & 0xFFFF
