"""Keep legacy acceptance FFTs and raw report previews within analysis budgets."""

import json
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pytest

from wavebench.data.analysis_resources import AnalysisLimits
from wavebench.data.fft import analyze_fft
from wavebench.data.packages import RunPackage
from wavebench.report import html
from wavebench.services.run_analysis import capture_fft_summary, evaluate_expect, step_status


def waveform_file(tmp_path, samples=128, *, dtype=np.float64, fortran=False):
    time = np.arange(samples) / samples
    waveform = np.column_stack((time, 1.25 * np.cos(2 * np.pi * 8 * time))).astype(dtype)
    if np.dtype(dtype).kind in "iu":
        waveform[:, 0] = np.arange(samples)
    path = tmp_path / "waveform.npy"
    np.save(path, np.asfortranarray(waveform) if fortran else waveform)
    return path, waveform


def report_run(tmp_path, source, *, metadata_path=None):
    metadata_path = metadata_path or tmp_path / "metadata.json"
    metadata_path.write_text(json.dumps({
        "operation": {"channel": 1}, "waveform": {"summary": {"channel": 1}},
        "files": {"npy": source.name},
    }), encoding="utf-8")
    return RunPackage(
        path=tmp_path, run_json_path=tmp_path / "run.json", summary_csv_path=None,
        summary_rows=[], run={"status": "ok", "steps": [{
            "index": 1, "kind": "scope.capture", "status": "ok",
            "artifact": {"package": str(tmp_path), "metadata": str(metadata_path)},
        }]},
    )


@pytest.mark.parametrize("values,dimension", [
    ({"max_input_samples": 4}, "max_input_samples"),
    ({"max_fft_length": 4}, "max_fft_length"),
    ({"max_working_bytes": 1024}, "max_working_bytes"),
    ({"max_work_units": 4}, "max_work_units"),
])
def test_fft_budget_failure_retains_evidence_and_fails_acceptance(tmp_path, values, dimension):
    path, _ = waveform_file(tmp_path)
    limits = AnalysisLimits(**values)
    with patch("numpy.load", side_effect=AssertionError("unbounded load")) as load, patch(
        "wavebench.services.run_analysis.analyze_fft", side_effect=AssertionError("FFT ran")
    ) as fft:
        result = capture_fft_summary(SimpleNamespace(npy_path=path), analysis_limits=limits)
    load.assert_not_called()
    fft.assert_not_called()
    assert result["status"] == "failed"
    assert result["error_code"] == "resource_limit_exceeded"
    assert result["resource_error"]["dimension"] == dimension
    assert result["resource_error"]["limit"] == getattr(limits, dimension)
    assert result["resource_error"]["requested"] > getattr(limits, dimension)
    assert result["resources"] == limits.evidence()
    expected = evaluate_expect(result, {"peak_frequency_hz": {"min": 0}})
    assert expected["status"] == "failed"
    assert step_status({"fft": result, "expect_fft": expected}) == "failed"


def test_default_fft_rejects_declared_12_5_million_samples_before_mapping(tmp_path):
    source = tmp_path / "large-header.npy"
    # Only write a header: no large array or payload allocation is needed to
    # prove that the admission check rejects this real capture length.
    with source.open("wb") as file:
        np.lib.format.write_array_header_1_0(file, {
            "descr": "<f8", "fortran_order": False, "shape": (12_500_000, 2),
        })
    with patch("numpy.memmap", side_effect=AssertionError("mapped over-budget source")) as mapped, patch(
        "wavebench.services.run_analysis.analyze_fft", side_effect=AssertionError("FFT ran")
    ) as fft:
        result = capture_fft_summary(SimpleNamespace(npy_path=source))
    mapped.assert_not_called()
    fft.assert_not_called()
    assert result["status"] == "failed"
    assert result["error_code"] == "resource_limit_exceeded"


@pytest.mark.parametrize("dtype,fortran", [(np.float64, False), (np.float32, True), (np.int32, False)])
def test_fft_budget_preserves_full_length_metrics(tmp_path, dtype, fortran):
    source, waveform = waveform_file(tmp_path, dtype=dtype, fortran=fortran)
    baseline = analyze_fft(waveform)
    with patch("numpy.load", side_effect=AssertionError("unbounded load")), patch(
        "wavebench.data.analysis_io.np.memmap", wraps=np.memmap
    ) as mapped:
        result = capture_fft_summary(SimpleNamespace(npy_path=source))
    assert result["status"] == "ok"
    for name, value in baseline.items():
        assert result[name] == value
    assert result["samples"] == len(waveform)
    assert mapped.call_args.kwargs["mode"] == "r"
    assert result["resources"]["estimated_working_bytes"] > waveform.nbytes


def test_fft_checks_original_length_before_finite_filtering(tmp_path):
    source, waveform = waveform_file(tmp_path)
    waveform[8:, 1] = np.nan
    np.save(source, waveform)
    with patch("wavebench.services.run_analysis.analyze_fft") as fft:
        result = capture_fft_summary(
            SimpleNamespace(npy_path=source), analysis_limits=AnalysisLimits(max_fft_length=8)
        )
    fft.assert_not_called()
    assert result["resource_error"]["requested"] == 128
    assert result["resource_error"]["dimension"] == "max_fft_length"


@pytest.mark.parametrize("invalid", ["object", "shape", "truncated", "trailing"])
def test_invalid_npy_is_rejected_before_mapping(tmp_path, invalid):
    source, _ = waveform_file(tmp_path)
    if invalid == "object":
        np.save(source, np.array([[object(), object()]], dtype=object))
    elif invalid == "shape":
        np.save(source, np.ones((4, 3)))
    elif invalid == "truncated":
        source.write_bytes(source.read_bytes()[:-1])
    else:
        with source.open("ab") as file:
            file.write(b"extra")
    run = report_run(tmp_path, source)
    with patch("numpy.memmap", side_effect=AssertionError("invalid source mapped")) as mapped:
        fft = capture_fft_summary(SimpleNamespace(npy_path=source))
        [preview] = html._collect_waveform_previews(run)
    mapped.assert_not_called()
    assert fft["status"] == "unavailable"
    assert "DataError" in fft["error"]
    assert preview.svg == ""
    assert "DataError" in preview.warning


@pytest.mark.parametrize("values,dimension", [
    ({"max_input_samples": 4}, "max_input_samples"),
    ({"max_working_bytes": 80_000}, "max_working_bytes"),
    ({"max_output_bytes": 64}, "max_output_bytes"),
    ({"max_work_units": 4}, "max_work_units"),
    ({"max_metadata_bytes": 4}, "max_metadata_bytes"),
])
def test_full_report_threads_limits_and_explains_unavailable_preview(tmp_path, values, dimension):
    source, _ = waveform_file(tmp_path)
    run = report_run(tmp_path, source, metadata_path=tmp_path / "custom-metadata.json")
    with patch("numpy.load", side_effect=AssertionError("unbounded load")) as load, patch(
        "wavebench.report.html._waveform_svg", side_effect=AssertionError("preview ran")
    ) as svg:
        rendered = html.render_run_report_html(run, analysis_limits=AnalysisLimits(**values))
    load.assert_not_called()
    svg.assert_not_called()
    assert "waveform preview unavailable" in rendered
    assert dimension in rendered
    assert 'aria-label="waveform preview"' not in rendered


def test_preview_input_budget_rejects_before_mapping(tmp_path):
    source, _ = waveform_file(tmp_path)
    run = report_run(tmp_path, source)
    with patch("numpy.memmap", side_effect=AssertionError("over-budget source mapped")) as mapped:
        [preview] = html._collect_waveform_previews(run, analysis_limits=AnalysisLimits(max_input_samples=4))
    mapped.assert_not_called()
    assert "max_input_samples" in preview.warning


@pytest.mark.parametrize("dtype,fortran", [(np.float64, False), (np.float32, True)])
def test_preview_uses_bounded_blocks_and_preserves_finite_sample_selection(tmp_path, dtype, fortran):
    source, waveform = waveform_file(tmp_path, 9000, dtype=dtype, fortran=fortran)
    waveform[::7, 1] = np.nan
    waveform[::11, 0] = np.inf
    np.save(source, np.asfortranarray(waveform) if fortran else waveform)
    expected = html._waveform_svg(waveform)
    run = report_run(tmp_path, source)
    # Enough for the bounded preview, far below the cost of a full copy/filter.
    limits = AnalysisLimits(max_working_bytes=700_000)
    with patch("numpy.load", side_effect=AssertionError("unbounded load")), patch(
        "wavebench.report.html.np.asarray", wraps=np.asarray
    ) as asarray:
        [preview] = html._collect_waveform_previews(run, analysis_limits=limits)
    assert preview.svg == expected
    assert preview.warning == ""
    assert all(len(call.args[0]) <= 4096 for call in asarray.call_args_list)


def test_full_report_budget_within_limits_still_draws_preview(tmp_path):
    source, _ = waveform_file(tmp_path)
    rendered = html.render_run_report_html(report_run(tmp_path, source), analysis_limits=AnalysisLimits())
    assert 'aria-label="waveform preview"' in rendered
    assert "waveform preview unavailable" not in rendered


@pytest.mark.parametrize("values,dimension", [
    ({"max_work_units": 256}, "max_work_units"),
    ({"max_report_curves": 1}, "max_report_curves"),
])
def test_report_preview_budget_is_cumulative(tmp_path, values, dimension):
    source, _ = waveform_file(tmp_path)
    run = report_run(tmp_path, source)
    run.run["steps"].append({**run.run["steps"][0], "index": 2})
    first, second = html._collect_waveform_previews(run, analysis_limits=AnalysisLimits(**values))
    assert first.svg
    assert first.warning == ""
    assert second.svg == ""
    assert dimension in second.warning


def test_run_capture_fft_uses_execution_profile_and_stops_after_budget_failure(tmp_path):
    from test_run_service import fake_capture, make_config, write_plan
    from wavebench.logging import CommandLogger
    from wavebench.services.run_plan import load_run_plan
    from wavebench.services.run_service import RunService

    plan = load_run_plan(write_plan(tmp_path, '''
[[steps]]
kind = "scope.capture"
label = "budget"
[steps.expect_fft]
peak_frequency_hz = { min = 990, max = 1010 }

[[steps]]
kind = "sleep"
duration_s = 0.01
'''))
    capture = fake_capture(tmp_path, "budget", [])
    service = RunService(config=make_config(tmp_path), logger=CommandLogger(),
                         analysis_limits=AnalysisLimits(max_fft_length=4))
    with patch("wavebench.services.run_service.ScopeService") as scope, patch(
        "wavebench.services.run_analysis.analyze_fft", side_effect=AssertionError("FFT exceeded budget")
    ) as fft:
        scope.return_value.capture_waveform.return_value = capture
        result = service.run(plan)
    fft.assert_not_called()
    assert result.status == "failed"
    assert len(result.steps) == 1
    assert result.steps[0].artifact["fft"]["resource_error"]["dimension"] == "max_fft_length"
    assert result.steps[0].artifact["expect_fft"]["status"] == "failed"
