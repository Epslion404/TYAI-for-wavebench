"""Output protection follows the files actually consumed by analysis rendering."""

from hashlib import sha256
import json
import os
from unittest.mock import patch

import numpy as np
import pytest

from wavebench.cli import main
from wavebench.data.analysis_io import read_json_bounded
from wavebench.data.analysis_resources import AnalysisLimits, AnalysisResourceError
from wavebench.data.packages import load_run_package
from wavebench.errors import ConfigError
from wavebench.report.html import (
    plan_run_report_outputs, render_run_report_html, write_run_report_html,
    write_run_report_manifest, write_run_report_pdf,
)


def snapshot(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def analysis_run(tmp_path, kind):
    run = tmp_path / "run"
    custom = run / "custom-evidence"
    custom.mkdir(parents=True)
    processing = run / "processing"
    processing.mkdir()
    manifest = {"schema": "wavebench.analysis_pipeline.v1", "source": {}, "sampling": {}, "exports": []}
    pipeline = {"manifest": "processing/manifest.json", "exports": []}
    if kind == "peak":
        source = custom / "peaks.json"
        source.write_text(json.dumps({"signal_sha256": "abc", "peaks": [{"position": 1., "value": 2.}]}), encoding="utf-8")
        item = {"name": "probe", "json": "custom-evidence/peaks.json",
                "json_sha256": sha256(source.read_bytes()).hexdigest(), "count": 1, "retained_count": 1}
        manifest["peaks"] = [item]
        pipeline["peaks"] = [item]
        marker = "peaks=1"
    elif kind == "pair":
        from wavebench.data.pair_analysis import PAIR_COLUMNS

        source = custom / "pair.npy"
        np.save(source, np.array([[1., 1., 0., 0., 0., 1., 1., 1.],
                                  [2., 2., 0., 6., 0., 1., 1., 1.]]))
        manifest["schema"] = "wavebench.analysis_pair.v1"
        manifest["exports"] = [{"name": "pair", "format": "npy", "columns": PAIR_COLUMNS,
            "path": "custom-evidence/pair.npy", "sha256": sha256(source.read_bytes()).hexdigest()}]
        marker = "有效 bin / Valid bins"
    else:
        source = custom / "signal.npy"
        np.save(source, np.array([[0., 0.], [1., 2.], [2., 1.]]))
        manifest["exports"] = [{"name": "probe", "format": "npy", "columns": ["time_s", "voltage_v"],
            "path": "custom-evidence/signal.npy", "sha256": sha256(source.read_bytes()).hexdigest()}]
        marker = "<polyline"
    manifest_path = processing / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    (run / "run.json").write_text(json.dumps({"status": "ok", "steps": [{"index": 0, "id": "probe",
        "kind": "analysis.pair" if kind == "pair" else "analysis.pipeline", "status": "ok",
        "artifact": {"analysis_pipeline": pipeline}}]}), encoding="utf-8")
    return load_run_package(run), source, manifest_path, marker


@pytest.mark.parametrize("kind", ["manifest-export", "peak", "pair"])
@pytest.mark.parametrize("mode", ["api-direct", "api-hardlink-sidecar", "cli-hardlink", "json-cli-hardlink",
                                  "pdf", "manifest"])
def test_actual_renderer_evidence_is_protected(tmp_path, capsys, kind, mode):
    package, source, _, marker = analysis_run(tmp_path, kind)
    rendered = render_run_report_html(package)
    assert marker in rendered
    assert "analysis unavailable" not in rendered and "curve unavailable" not in rendered
    output = source
    if mode != "api-direct":
        assets = tmp_path / "export" / "report-assets"
        assets.mkdir(parents=True)
        output = assets.parent / "report.html"
        os.link(source, assets / "manifest.json" if mode in ("api-hardlink-sidecar", "manifest") else output)
    before = snapshot(tmp_path)
    with pytest.raises(ConfigError, match="original run evidence"):
        plan_run_report_outputs(package, output)
    if "cli" in mode:
        args = ["run", "report", str(package.path), "--output", str(output)]
        assert main((["--json"] if mode.startswith("json") else []) + args) == 2
        printed = capsys.readouterr()
        if mode.startswith("json"):
            assert json.loads(printed.out)["type"] == "ConfigError"
    else:
        with pytest.raises(ConfigError, match="original run evidence"):
            if mode == "pdf":
                write_run_report_pdf(package, output)
            elif mode == "manifest":
                write_run_report_manifest(package, output_dir=output.parent)
            else:
                write_run_report_html(package, output)
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("origin", ["run-artifact", "manifest"])
@pytest.mark.parametrize("field,key", [("exports", "path"), ("peaks", "json")])
def test_missing_explicit_analysis_reference_remains_protected(tmp_path, origin, field, key):
    package, _, manifest_path, _ = analysis_run(tmp_path, "manifest-export")
    missing = package.path / "custom-evidence" / "missing.html"
    if origin == "manifest":
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest[field] = [{key: "custom-evidence/missing.html"}]
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    else:
        package.steps[0]["artifact"]["analysis_pipeline"][field] = [{key: str(missing)}]
    before = snapshot(tmp_path)
    with pytest.raises(ConfigError, match="original run evidence"):
        write_run_report_html(package, missing)
    assert not missing.exists()
    assert snapshot(tmp_path) == before


def test_manifest_paths_use_run_root_without_parent_fallback(tmp_path):
    from wavebench.report.analysis import artifact_file

    package, source, manifest_path, _ = analysis_run(tmp_path, "manifest-export")
    decoy = manifest_path.parent / "custom-evidence" / source.name
    decoy.parent.mkdir()
    decoy.write_bytes(b"not consumed by renderer")
    assert artifact_file(package.path, "custom-evidence/signal.npy") == source.resolve()
    rendered = render_run_report_html(package)
    assert "<polyline" in rendered and "curve unavailable" not in rendered
    assert decoy.read_bytes() == b"not consumed by renderer"


@pytest.mark.parametrize("failure", ["json", "encoding", "shape", "exports-shape", "read-error", "timeout",
                                    "metadata-budget", "retained-budget"])
@pytest.mark.parametrize("mode", ["html", "pdf", "manifest"])
def test_inventory_read_failure_refuses_every_writer_before_any_write(tmp_path, failure, mode):
    package, _, manifest_path, _ = analysis_run(tmp_path, "manifest-export")
    if failure == "json":
        manifest_path.write_bytes(b"{broken JSON")
    elif failure == "encoding":
        manifest_path.write_bytes(b"\xff")
    elif failure == "shape":
        manifest_path.write_bytes(b"[]")
    elif failure == "exports-shape":
        manifest_path.write_bytes(b'{"exports": {"path": "hidden"}}')
    limits = (AnalysisLimits(max_metadata_bytes=1) if failure == "metadata-budget" else
              AnalysisLimits(max_working_bytes=manifest_path.stat().st_size * 32 + 65536)
              if failure == "retained-budget" else AnalysisLimits())
    output_dir = tmp_path / "new-export"
    before = snapshot(tmp_path)

    def read(path, profile, **kwargs):
        assert profile is limits
        assert kwargs["retained_bytes"] > 0
        if failure in ("timeout", "read-error"):
            raise TimeoutError("inventory deadline") if failure == "timeout" else OSError("inventory read failed")
        return read_json_bounded(path, profile, **kwargs)

    error = AnalysisResourceError if "budget" in failure else ConfigError
    with patch("wavebench.data.analysis_io.read_json_bounded", side_effect=read), patch(
        "wavebench.report.html._write_run_report_pdf"
    ) as renderer:
        with pytest.raises(error):
            if mode == "html":
                write_run_report_html(package, output_dir / "report.html", analysis_limits=limits)
            elif mode == "pdf":
                write_run_report_pdf(package, output_dir / "report.pdf", analysis_limits=limits)
            else:
                write_run_report_manifest(package, output_dir=output_dir, analysis_limits=limits)
        renderer.assert_not_called()
    assert not output_dir.exists()
    assert snapshot(tmp_path) == before


def test_guard_profile_limits_refresh_manifest_read(tmp_path):
    package, _, _, _ = analysis_run(tmp_path, "manifest-export")
    export = tmp_path / "export"
    assets = export / "report-assets"
    assets.mkdir(parents=True)
    manifest = assets / "manifest.json"
    manifest.write_bytes(b" " * 2048)
    before = snapshot(tmp_path)
    with pytest.raises(AnalysisResourceError, match="max_metadata_bytes"):
        write_run_report_html(package, export / "report.html", analysis_limits=AnalysisLimits(max_metadata_bytes=1024))
    assert snapshot(tmp_path) == before


def test_malformed_capture_metadata_cannot_bypass_guard(tmp_path):
    package, _, _, _ = analysis_run(tmp_path, "manifest-export")
    capture = tmp_path / "capture"
    capture.mkdir()
    metadata = capture / "metadata.json"
    metadata.write_bytes(b"{invalid")
    package.steps.append({"artifact": {"package": str(capture), "metadata": str(metadata)}})
    before = snapshot(tmp_path)
    with pytest.raises(ConfigError, match="cannot inventory original run evidence"):
        write_run_report_html(package, tmp_path / "report.html")
    assert snapshot(tmp_path) == before


def test_guard_and_renderer_json_use_profile_and_retained_admission(tmp_path):
    package, peak_path, manifest_path, _ = analysis_run(tmp_path, "peak")
    limits = AnalysisLimits()
    output = tmp_path / "export" / "report.html"
    with patch("wavebench.data.analysis_io.read_json_bounded", wraps=read_json_bounded) as inventory:
        plan_run_report_outputs(package, output, analysis_limits=limits)
    [call] = inventory.call_args_list
    assert call.args == (manifest_path.resolve(), limits)
    assert call.kwargs["retained_bytes"] > 0
    with patch("wavebench.report.analysis.read_json_bounded", wraps=read_json_bounded) as renderer:
        rendered = render_run_report_html(package, analysis_limits=limits)
    assert "peaks=1" in rendered
    assert [call.args[0] for call in renderer.call_args_list] == [manifest_path.resolve(), peak_path.resolve()]
    assert all(call.args[1] is limits and call.kwargs["retained_bytes"] > 0
               for call in renderer.call_args_list)


def test_cancelled_inventory_never_starts_writer(tmp_path):
    from threading import Event
    from wavebench.data.analysis_control import AnalysisCancelled, cancel_signal

    package, _, _, _ = analysis_run(tmp_path, "manifest-export")
    event = Event()
    event.set()
    token = cancel_signal.set(event)
    before = snapshot(tmp_path)
    try:
        with pytest.raises(AnalysisCancelled):
            write_run_report_html(package, tmp_path / "new-export" / "report.html")
    finally:
        cancel_signal.reset(token)
    assert snapshot(tmp_path) == before
    assert not (tmp_path / "new-export").exists()
