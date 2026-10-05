import pytest

import tianyi_electronics_skill.core as core
from tianyi_electronics_skill.core import analyze_samples, demo_waveform, load_waveform


def test_demo_frequency_and_levels():
    result = demo_waveform(sample_rate_hz=100_000, frequency_hz=1_000, seconds=0.01)
    assert abs(result["frequency_hz"] - 1_000) < 2
    assert 1.5 < result["peak_to_peak_v"] < 1.7
    assert result["rms_v"] > 0.5


def test_analyze_rejects_invalid_rate():
    try:
        analyze_samples([0, 1, 0, -1], 0)
    except ValueError as exc:
        assert "sample_rate" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_load_json_waveform(tmp_path):
    path = tmp_path / "wave.json"
    path.write_text('{"sample_rate_hz": 10, "samples": [0, 1, 0, -1]}', encoding="utf-8")
    samples, rate = load_waveform(path)
    assert samples == [0.0, 1.0, 0.0, -1.0]
    assert rate == 10


def test_autoscale_readback_failure_is_not_reported_as_applied(monkeypatch):
    class FakeSession:
        def __init__(self, *_args, **_kwargs):
            self.writes: list[str] = []

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def query(self, command: str) -> str:
            if command == "*IDN?":
                return "ACME,SCOPE,1,1"
            if command == "TIMebase:RANGe?":
                return "0.1"
            raise AssertionError(f"unexpected query: {command}")

        def write(self, command: str) -> None:
            self.writes.append(command)

        def drain_errors(self) -> list[str]:
            if self.writes[-1] == "AUTOSET":
                return ['-113,"Undefined header"']
            return []

    monkeypatch.setattr(core, "ScpiSession", FakeSession)
    monkeypatch.setattr(core, "AUTOSCALE_CANDIDATES", ())
    monkeypatch.setattr(
        core,
        "read_waveform_in_session",
        lambda *_args, **_kwargs: {
            "sample_count": 16,
            "metrics": {"frequency_hz": 1000.0},
            "warnings": [],
            "errors": [],
            "failures": [],
            "device_errors": [],
        },
    )

    result = core.autoscale_scope("example.test", apply=True)

    item = result["write_results"][-1]
    assert item["write_accepted"] is True
    assert item["verified"] is False
    assert item["ok"] is False
    assert result["applied"] is False
    assert result["partially_applied"] is False
    assert result["failures"] == [{
        "category": "verification_failed",
        "command": "TIMebase:RANGe?",
        "message": "设置后的回读值 0.1 与请求值 0.005 不匹配",
    }]


@pytest.mark.parametrize(
    ("readback", "expected_category", "expected_message"),
    [
        ("", "no_response", "设置后的回读响应为空"),
        ("not-a-number", "parse_error", "设置后的回读响应无法解析为数值"),
    ],
)
def test_autoscale_readback_failures_are_structured(monkeypatch, readback, expected_category, expected_message):
    class FakeSession:
        def __init__(self, *_args, **_kwargs):
            self.writes: list[str] = []

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def query(self, command: str) -> str:
            if command == "*IDN?":
                return "ACME,SCOPE,1,1"
            if command == "TIMebase:RANGe?":
                return readback
            raise AssertionError(f"unexpected query: {command}")

        def write(self, command: str) -> None:
            self.writes.append(command)

        def drain_errors(self) -> list[str]:
            if self.writes[-1] == "AUTOSET":
                return ['-113,"Undefined header"']
            return []

    monkeypatch.setattr(core, "ScpiSession", FakeSession)
    monkeypatch.setattr(core, "AUTOSCALE_CANDIDATES", ())
    monkeypatch.setattr(
        core,
        "read_waveform_in_session",
        lambda *_args, **_kwargs: {
            "sample_count": 16,
            "metrics": {"frequency_hz": 1000.0},
            "warnings": [],
            "errors": [],
            "failures": [],
            "device_errors": [],
        },
    )

    result = core.autoscale_scope("example.test", apply=True)

    assert result["applied"] is False
    assert result["failures"][0]["category"] == expected_category
    assert result["failures"][0]["command"] == "TIMebase:RANGe?"
    assert expected_message in result["failures"][0]["message"]


def test_autoscale_rejected_write_is_structured_failure(monkeypatch):
    class FakeSession:
        def __init__(self, *_args, **_kwargs):
            self.writes: list[str] = []

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def query(self, command: str) -> str:
            if command == "*IDN?":
                return "ACME,SCOPE,1,1"
            raise AssertionError(f"unexpected query: {command}")

        def write(self, command: str) -> None:
            self.writes.append(command)

        def drain_errors(self) -> list[str]:
            if self.writes[-1] == "AUTOSET":
                return ['-113,"Undefined header"']
            return []

    monkeypatch.setattr(core, "ScpiSession", FakeSession)
    monkeypatch.setattr(core, "AUTOSCALE_CANDIDATES", ("AUTOSET",))
    monkeypatch.setattr(
        core,
        "read_waveform_in_session",
        lambda *_args, **_kwargs: {
            "sample_count": 0,
            "metrics": {},
            "warnings": [],
            "errors": [],
            "failures": [],
            "device_errors": [],
        },
    )

    result = core.autoscale_scope("example.test", apply=True)

    assert result["applied"] is False
    assert result["failures"] == [{
        "category": "unsupported",
        "command": "AUTOSET",
        "message": '-113,"Undefined header"',
    }]


def test_autoscale_partial_apply_counts_only_derived_writes(monkeypatch):
    class FakeSession:
        def __init__(self, *_args, **_kwargs):
            self.writes: list[str] = []

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def query(self, command: str) -> str:
            responses = {
                "*IDN?": "ACME,SCOPE,1,1",
                "TIMebase:RANGe?": "0.005",
                ":CHAN1:OFFS?": "1.0",
            }
            return responses[command]

        def write(self, command: str) -> None:
            self.writes.append(command)

        def drain_errors(self) -> list[str]:
            if self.writes[-1] == "AUTOSET":
                return ['-113,"Undefined header"']
            return []

    monkeypatch.setattr(core, "ScpiSession", FakeSession)
    monkeypatch.setattr(core, "AUTOSCALE_CANDIDATES", ("AUTOSET",))
    monkeypatch.setattr(
        core,
        "read_waveform_in_session",
        lambda *_args, **_kwargs: {
            "sample_count": 16,
            "metrics": {"frequency_hz": 1000.0, "minimum_v": -1.0, "maximum_v": 1.0},
            "warnings": [],
            "errors": [],
            "failures": [],
            "device_errors": [],
        },
    )

    result = core.autoscale_scope("example.test", apply=True)

    assert result["applied"] is False
    assert result["partially_applied"] is True
    assert [item["ok"] for item in result["write_results"][-2:]] == [True, False]
