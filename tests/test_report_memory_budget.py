"""Measure real text allocations and keep admission ahead of report copies."""
import json
import tracemalloc
from unittest.mock import patch

import pytest

from test_legacy_analysis_limits import report_run, waveform_file
from wavebench.data.analysis_io import read_json_bounded
from wavebench.data.analysis_resources import AnalysisLimits, AnalysisResourceError
from wavebench.report import html
from wavebench.report.html_budget import bounded_format, retained_size


def repeated_run(tmp_path, count=100):
    source, _ = waveform_file(tmp_path, 2)
    run = report_run(tmp_path, source)
    run.run["steps"] = [{**run.run["steps"][0], "index": i} for i in range(count)]
    return run


def test_hundred_two_point_previews_refuse_before_card_copies(tmp_path):
    run = repeated_run(tmp_path)
    # Explicitly admit every curve for this reproduction.
    previews = html._collect_waveform_previews(
        run, analysis_limits=AnalysisLimits(max_report_curves=128),
    )
    assert len(previews) == 100
    assert all(item.svg and not item.warning for item in previews)
    limits = AnalysisLimits(max_working_bytes=120_000, max_report_curves=128)
    with patch("wavebench.report.html._waveform_preview_card") as card:
        with pytest.raises(AnalysisResourceError) as refused:
            html._waveform_previews_block(previews, analysis_limits=limits)
    card.assert_not_called()
    assert refused.value.evidence["stage"] == "waveform preview HTML assembly/encoding"
    assert refused.value.evidence["limit"] == 120_000
    assert refused.value.evidence["requested"] > 120_000


def test_full_hundred_curve_report_refuses_without_relaxing_profile(tmp_path):
    limits = AnalysisLimits(max_working_bytes=120_000, max_report_curves=128)
    run = repeated_run(tmp_path)
    with pytest.raises(AnalysisResourceError) as refused:
        html.render_run_report_html(run, analysis_limits=limits)
    assert refused.value.evidence["dimension"] == "max_working_bytes"
    assert refused.value.evidence["limit"] == 120_000
    assert limits.max_working_bytes == 120_000


def test_budget_refusal_leaves_existing_html_unchanged(tmp_path):
    run = repeated_run(tmp_path)
    output = tmp_path / "existing.html"
    output.write_text("previous report", encoding="utf-8")
    with pytest.raises(AnalysisResourceError):
        html.write_run_report_html(
            run, output,
            analysis_limits=AnalysisLimits(max_working_bytes=120_000, max_report_curves=128),
        )
    assert output.read_text(encoding="utf-8") == "previous report"
    assert not (tmp_path / "report-assets/manifest.json").exists()


def test_admitted_preview_block_measured_copies_and_encoding_fit(tmp_path):
    run = repeated_run(tmp_path)
    limits = AnalysisLimits(max_working_bytes=2_000_000, max_report_curves=128)
    previews = html._collect_waveform_previews(run, analysis_limits=limits)
    retained = retained_size(previews)
    tracemalloc.start()
    try:
        block = html._waveform_previews_block(previews, analysis_limits=limits)
        encoded = block.encode("utf-8")
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert block.count('aria-label="waveform preview"') == 100
    assert encoded.decode("utf-8") == block
    assert retained + peak < limits.max_working_bytes


def test_final_assembly_refuses_before_format_and_accounts_for_unicode_encoding():
    class Template(str):
        calls = 0

        def format_map(self, values):
            self.calls += 1
            return super().format_map(values)

    template = Template("<html>{body}</html>")
    values = {"body": "😀<&中" * 5000}
    with pytest.raises(AnalysisResourceError) as refused:
        bounded_format(template, values, AnalysisLimits(max_working_bytes=120_000))
    assert template.calls == 0
    assert refused.value.evidence["stage"] == "final report HTML assembly/encoding"
    limits = AnalysisLimits(max_working_bytes=600_000)
    retained = retained_size([template, *values.values()])
    tracemalloc.start()
    try:
        result = bounded_format(template, values, limits)
        encoded = result.encode("utf-8")
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert encoded.decode("utf-8") == result
    assert retained + peak < limits.max_working_bytes


def test_compact_report_also_checks_final_assembly(tmp_path):
    run = repeated_run(tmp_path, count=0)
    with pytest.raises(AnalysisResourceError) as refused:
        html.render_run_report_html(
            run, compact=True, analysis_limits=AnalysisLimits(max_working_bytes=64_000),
        )
    assert refused.value.evidence["stage"] == "final report HTML assembly/encoding"


def test_metadata_parsing_includes_previously_retained_svg_before_json_load(tmp_path):
    run = repeated_run(tmp_path, count=2)
    metadata = tmp_path / "metadata.json"
    payload = json.loads(metadata.read_text(encoding="utf-8"))
    payload["padding"] = "a" * 1800
    metadata.write_text(json.dumps(payload), encoding="utf-8")
    limits = AnalysisLimits(max_working_bytes=150_000)
    with patch("wavebench.report.html._waveform_svg", return_value="s" * 40_000), patch(
        "wavebench.data.analysis_io.json.loads", wraps=json.loads,
    ) as parse:
        first, second = html._collect_waveform_previews(run, analysis_limits=limits)
    assert first.svg and not first.warning
    assert not second.svg
    assert "metadata parsing" in second.warning
    assert "max_working_bytes" in second.warning
    assert parse.call_count == 1


def test_small_metadata_does_not_allocate_default_eight_megabyte_read_buffer(tmp_path):
    metadata = tmp_path / "metadata.json"
    metadata.write_text('{"channel": 1}', encoding="utf-8")
    limits = AnalysisLimits(max_working_bytes=120_000)
    tracemalloc.start()
    try:
        result = read_json_bounded(metadata, limits)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert result == {"channel": 1}
    assert peak < limits.max_working_bytes
