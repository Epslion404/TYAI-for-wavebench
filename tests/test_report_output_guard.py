"""Report exports must leave original offline run evidence intact."""

import json
import os
from unittest.mock import patch

import pytest

from wavebench.cli import main
from wavebench.data.packages import load_run_package
from wavebench.errors import ConfigError
from wavebench.report.html import (
    plan_run_report_outputs,
    write_run_report_html,
    write_run_report_manifest,
    write_run_report_pdf,
    write_run_report,
)


@pytest.fixture
def run_dir(tmp_path):
    root = tmp_path / "run"
    root.mkdir()
    (root / "run.json").write_text(json.dumps({"status": "ok", "steps": []}), encoding="utf-8")
    (root / "summary.csv").write_text("index,kind,status\n", encoding="utf-8")
    (root / "plan.toml").write_text("# original plan", encoding="utf-8")
    for name in ("steps/00.json", "captures/cap/metadata.json", "processing/p/metrics.json"):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}", encoding="utf-8")
    (root / "captures/cap/ch1.npy").write_bytes(b"original waveform bytes")
    (root / "original.html").write_text("original evidence with HTML extension", encoding="utf-8")
    return root


def snapshot(root):
    return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}


@pytest.mark.parametrize("json_output", [False, True])
@pytest.mark.parametrize("target", ["run.json", "summary.csv", "plan.toml", "steps/00.json",
                                  "captures/cap/ch1.npy", "processing/p/metrics.json", "original.html"])
def test_cli_rejects_evidence_output_before_any_write(run_dir, capsys, target, json_output):
    before = snapshot(run_dir)
    args = ["run", "report", str(run_dir), "--output", str(run_dir / target)]
    if json_output:
        args.insert(0, "--json")
    assert main(args) == 2
    output = capsys.readouterr()
    if json_output:
        envelope = json.loads(output.out)
        assert envelope["schema"] == "wavebench.error.v1"
        assert envelope["exit_code"] == 2
        assert envelope["type"] == "ConfigError"
    assert snapshot(run_dir) == before


@pytest.mark.parametrize("alias", ["dotdot", "case", "symlink", "hardlink"])
def test_file_aliases_are_rejected(run_dir, tmp_path, alias):
    original = run_dir / "run.json"
    if alias == "dotdot":
        (run_dir / "child").mkdir()
        target = run_dir / "child" / ".." / "run.json"
    elif alias == "case":
        if os.name != "nt":
            pytest.skip("case-insensitive Windows path")
        target = run_dir / "RUN.JSON"
    else:
        target = tmp_path / "alias.html"
        if alias == "hardlink":
            os.link(original, target)
        else:
            try:
                target.symlink_to(original)
            except OSError:
                pytest.skip("symlink creation unavailable")
    before = snapshot(run_dir)
    with pytest.raises(ConfigError, match="original run evidence"):
        write_run_report_html(load_run_package(run_dir), target)
    assert snapshot(run_dir) == before


def test_pdf_rejects_evidence_before_loading_optional_renderer(run_dir):
    before = snapshot(run_dir)
    with pytest.raises(ConfigError, match="original run evidence"):
        write_run_report_pdf(load_run_package(run_dir), run_dir / "run.json")
    assert snapshot(run_dir) == before


def test_cli_preflights_pdf_target_before_writing_html(run_dir, tmp_path):
    before = snapshot(run_dir)
    html = tmp_path / "export" / "report.html"
    assert main(["run", "report", str(run_dir), "--output", str(html), "--pdf",
                 "--pdf-output", str(run_dir / "run.json")]) == 2
    assert not html.parent.exists()
    assert snapshot(run_dir) == before


def test_directory_junction_cannot_alias_run_evidence(run_dir, tmp_path):
    if os.name != "nt":
        pytest.skip("Windows junction")
    import subprocess
    alias = tmp_path / "junction"
    result = subprocess.run(["cmd", "/c", "mklink", "/J", str(alias),
                             str(run_dir / "steps")], capture_output=True)
    assert result.returncode == 0
    try:
        with pytest.raises(ConfigError, match="original run evidence"):
            write_run_report_html(load_run_package(run_dir), alias / "00.json")
        assert (run_dir / "steps" / "00.json").read_bytes() == b"{}"
    finally:
        alias.rmdir()


def test_external_capture_directory_is_protected(run_dir, tmp_path):
    capture = tmp_path / "external-capture"
    capture.mkdir()
    metadata = capture / "metadata.json"
    metadata.write_text("{}", encoding="utf-8")
    run_data = {"status": "ok", "steps": [{"index": 0, "kind": "scope.capture", "status": "ok",
                "artifact": {"package": str(capture), "metadata": str(metadata)}}]}
    (run_dir / "run.json").write_text(json.dumps(run_data), encoding="utf-8")
    with pytest.raises(ConfigError, match="original run evidence"):
        write_run_report_html(load_run_package(run_dir), capture / "report.html")
    assert metadata.read_text(encoding="utf-8") == "{}"
    assert not (capture / "report.html").exists()


def test_sidecar_alias_is_rejected_before_report_write(run_dir, tmp_path):
    export = tmp_path / "export"
    export.mkdir()
    assets = export / "report-assets"
    assets.mkdir()
    os.link(run_dir / "run.json", assets / "manifest.json")
    before = snapshot(run_dir)
    with pytest.raises(ConfigError, match="original run evidence"):
        write_run_report_html(load_run_package(run_dir), export / "report.html")
    assert not (export / "report.html").exists()
    assert snapshot(run_dir) == before


def test_plotly_sidecar_alias_is_rejected_before_any_report_write(run_dir, tmp_path):
    export = tmp_path / "export"
    assets = export / "report-assets"
    assets.mkdir(parents=True)
    os.link(run_dir / "run.json", assets / "plotly.min.js")
    run = load_run_package(run_dir)
    # A surface fixture is unnecessary here; exercise the same writer branch.
    from dataclasses import replace
    from types import SimpleNamespace
    run = replace(run, frequency_responses=[SimpleNamespace(
        rows=[], label="test", directory=run.path, csv_path=None, fit_path=None,
        baseline_path=None, calibration_csv_path=None, calibration_path=None,
    )])
    with patch("wavebench.report.html.build_surface_payload", return_value={"surface": True}), patch(
        "wavebench.report.html.plotly_asset_available", return_value=True
    ):
        with pytest.raises(ConfigError, match="original run evidence"):
            write_run_report_html(run, export / "report.html")
    assert not (export / "report.html").exists()


@pytest.mark.parametrize("destination", ["default", "custom", "external"])
def test_separate_reports_can_be_generated_and_refreshed(run_dir, tmp_path, destination):
    before = snapshot(run_dir)
    path = None if destination == "default" else (
        run_dir / "custom.html" if destination == "custom" else tmp_path / "export" / "custom.html"
    )
    first = write_run_report_html(load_run_package(run_dir), path)
    second = write_run_report_html(load_run_package(run_dir), path)
    assert first == second
    assert "<!doctype html>" in first.read_text(encoding="utf-8").lower()
    for name, content in before.items():
        assert (run_dir / name).read_bytes() == content


@pytest.mark.parametrize("field", ["manifest", "metrics", "export"])
@pytest.mark.parametrize("relative", [False, True])
@pytest.mark.parametrize("mode", ["api", "cli", "json-cli", "pdf", "manifest"])
def test_referenced_external_processing_files_are_protected(run_dir, tmp_path, field, relative, mode):
    from wavebench.report.html import write_run_report_manifest

    evidence = tmp_path / "external-processing" / "report-assets" / "manifest.json"
    evidence.parent.mkdir(parents=True)
    original = b'{"schema": "original-processing-evidence"}'
    evidence.write_bytes(original)
    reference = os.path.relpath(evidence, run_dir) if relative else str(evidence)
    pipeline = {"exports": [{"path": reference}]} if field == "export" else {field: reference}
    payload = {"status": "ok", "steps": [{"index": 0, "kind": "analysis.pipeline",
        "status": "ok", "artifact": {"analysis_pipeline": pipeline}}]}
    (run_dir / "run.json").write_text(json.dumps(payload), encoding="utf-8")
    before = snapshot(tmp_path)
    package = load_run_package(run_dir)
    if mode in ("cli", "json-cli"):
        args = ["run", "report", str(run_dir), "--output", str(evidence)]
        assert main((["--json"] if mode == "json-cli" else []) + args) == 2
    else:
        with pytest.raises(ConfigError, match="original run evidence"):
            if mode == "api":
                write_run_report_html(package, evidence)
            elif mode == "pdf":
                write_run_report_pdf(package, evidence)
            else:
                write_run_report_manifest(package, output_dir=evidence.parent.parent)
    assert snapshot(tmp_path) == before


def test_referenced_report_name_overrides_refresh_exemption(run_dir):
    evidence = run_dir / "report.html"
    evidence.write_bytes(b"original analysis export")
    payload = {"status": "ok", "steps": [{"index": 0, "kind": "analysis.pipeline",
        "status": "ok", "artifact": {"analysis_pipeline": {
            "exports": [{"path": "report.html"}]}}}]}
    (run_dir / "run.json").write_text(json.dumps(payload), encoding="utf-8")
    before = snapshot(run_dir)
    with pytest.raises(ConfigError, match="original run evidence"):
        write_run_report_html(load_run_package(run_dir))
    assert snapshot(run_dir) == before


@pytest.mark.parametrize("hardlink", [False, True])
def test_nested_junction_evidence_is_protected(run_dir, tmp_path, hardlink):
    if os.name != "nt":
        pytest.skip("Windows junction")
    import subprocess

    external = tmp_path / "external"
    external.mkdir()
    original = external / "metrics.json"
    original.write_bytes(b"original processing evidence")
    junction = run_dir / "processing" / "external"
    cycle = external / "cycle"
    for alias, directory in ((junction, external), (cycle, run_dir / "processing")):
        result = subprocess.run(["cmd", "/c", "mklink", "/J", str(alias), str(directory)],
                                capture_output=True, timeout=10)
        assert result.returncode == 0
    try:
        target = original
        if hardlink:
            target = tmp_path / "alias.html"
            os.link(original, target)
        with pytest.raises(ConfigError, match="original run evidence"):
            write_run_report_html(load_run_package(run_dir), target)
        assert original.read_bytes() == b"original processing evidence"
    finally:
        cycle.rmdir()
        junction.rmdir()


def test_junction_back_to_run_root_allows_report_refresh(run_dir):
    if os.name != "nt":
        pytest.skip("Windows junction")
    import subprocess

    junction = run_dir / "processing" / "back"
    result = subprocess.run(["cmd", "/c", "mklink", "/J", str(junction), str(run_dir)],
                            capture_output=True, timeout=10)
    assert result.returncode == 0
    try:
        for target in (run_dir / "report.html", run_dir / "custom.html"):
            for _ in range(2):
                assert write_run_report_html(load_run_package(run_dir), target) == target
        with pytest.raises(ConfigError, match="original run evidence"):
            write_run_report_html(load_run_package(run_dir), junction / "run.json")
    finally:
        junction.rmdir()


@pytest.mark.parametrize("field", ["metadata", "screenshot", "npy", "channel-csv"])
@pytest.mark.parametrize("mode", ["api", "cli", "json-cli", "pdf", "manifest"])
@pytest.mark.parametrize("alias", [False, True])
def test_external_capture_files_are_protected(run_dir, tmp_path, field, mode, alias):
    capture = tmp_path / "capture"
    capture.mkdir()
    evidence = tmp_path / "external" / "report-assets" / "manifest.json"
    evidence.parent.mkdir(parents=True)
    evidence.write_bytes(b'{"schema": "original-capture-evidence"}')
    metadata = evidence if field == "metadata" else capture / "metadata.json"
    files = ({"1": {"csv": str(evidence)}} if field == "channel-csv"
             else {field: str(evidence)})
    if field != "metadata":
        metadata.write_text(json.dumps({"files": files}), encoding="utf-8")
    payload = {"status": "ok", "steps": [{"index": 0, "kind": "scope.capture",
        "status": "ok", "artifact": {"package": str(capture), "metadata": str(metadata)}}]}
    (run_dir / "run.json").write_text(json.dumps(payload), encoding="utf-8")
    target = evidence
    if alias:
        target = tmp_path / "alias" / "report-assets" / "manifest.json"
        target.parent.mkdir(parents=True)
        os.link(evidence, target)
    before = snapshot(tmp_path)
    package = load_run_package(run_dir)
    if mode in ("cli", "json-cli"):
        args = ["run", "report", str(run_dir), "--output", str(target)]
        assert main((["--json"] if mode == "json-cli" else []) + args) == 2
    else:
        with pytest.raises(ConfigError, match="original run evidence"):
            if mode == "api":
                write_run_report_html(package, target)
            elif mode == "pdf":
                write_run_report_pdf(package, target)
            else:
                write_run_report_manifest(package, output_dir=target.parent.parent)
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("field", ["metadata", "screenshot", "npy"])
@pytest.mark.parametrize("fallback", [False, True])
def test_capture_explicit_and_fallback_paths_are_protected(run_dir, tmp_path, field, fallback):
    capture = tmp_path / "capture"
    capture.mkdir()
    # Relative metadata is project-root relative; waveform files also support
    # package-relative fallback. Screenshots fall back to the fixed package name.
    explicit = tmp_path / "missing" / f"{field}.json"
    if field == "metadata":
        metadata_ref = str(explicit)
        metadata = capture / "metadata.json"
        metadata.write_text("{}", encoding="utf-8")
        actual = metadata
    else:
        file_text = "missing/shot.png" if field == "screenshot" else "missing/ch1.npy"
        explicit = tmp_path / file_text
        actual = capture / ("screenshot.png" if field == "screenshot" else file_text)
        actual.parent.mkdir(parents=True, exist_ok=True)
        actual.write_bytes(b"original capture evidence")
        metadata = capture / "metadata.json"
        metadata.write_text(json.dumps({"files": {field: file_text}}), encoding="utf-8")
        metadata_ref = str(metadata)
    payload = {"status": "ok", "steps": [{"index": 0, "kind": "scope.capture",
        "status": "ok", "artifact": {"package": str(capture), "metadata": metadata_ref}}]}
    (run_dir / "run.json").write_text(json.dumps(payload), encoding="utf-8")
    before = snapshot(tmp_path)
    with pytest.raises(ConfigError, match="original run evidence"):
        write_run_report_html(load_run_package(run_dir), actual if fallback else explicit)
    assert snapshot(tmp_path) == before
    assert not explicit.exists()


def test_shared_capture_package_preserves_each_step_metadata(run_dir, tmp_path):
    capture = tmp_path / "capture"
    capture.mkdir()
    metadata = tmp_path / "second.json"
    metadata.write_text("{}", encoding="utf-8")
    steps = [{"index": index, "kind": "scope.capture", "status": "ok",
              "artifact": {"package": str(capture), "metadata": path}}
             for index, path in enumerate([str(capture / "metadata.json"), str(metadata)])]
    (run_dir / "run.json").write_text(json.dumps({"steps": steps}), encoding="utf-8")
    before = snapshot(tmp_path)
    with pytest.raises(ConfigError, match="original run evidence"):
        write_run_report_html(load_run_package(run_dir), metadata)
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("field", ["metadata", "screenshot"])
def test_capture_evidence_report_name_overrides_refresh_exemption(run_dir, tmp_path, field):
    capture = tmp_path / "capture"
    capture.mkdir()
    evidence = run_dir / "report.html"
    evidence.write_text("{}", encoding="utf-8")
    metadata = evidence if field == "metadata" else capture / "metadata.json"
    if field == "screenshot":
        metadata.write_text(json.dumps({"files": {"screenshot": str(evidence)}}), encoding="utf-8")
    payload = {"steps": [{"artifact": {"package": str(capture), "metadata": str(metadata)}}]}
    (run_dir / "run.json").write_text(json.dumps(payload), encoding="utf-8")
    before = snapshot(tmp_path)
    with pytest.raises(ConfigError, match="original run evidence"):
        write_run_report_html(load_run_package(run_dir))
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("mode", ["api", "manifest", "cli", "json-cli"])
def test_html_manifest_hardlink_rejected_before_any_write(run_dir, tmp_path, mode):
    export = tmp_path / "export"
    assets = export / "report-assets"
    assets.mkdir(parents=True)
    html = export / "report.html"
    html.write_bytes(b"previous report")
    os.link(html, assets / "manifest.json")
    before = snapshot(tmp_path)
    package = load_run_package(run_dir)
    if mode in ("cli", "json-cli"):
        args = ["run", "report", str(run_dir), "--output", str(html)]
        assert main((["--json"] if mode == "json-cli" else []) + args) == 2
    else:
        with pytest.raises(ConfigError, match="outputs conflict"):
            if mode == "api":
                write_run_report_html(package, html)
            else:
                write_run_report_manifest(package, output_dir=export)
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("mode", ["plan-api", "api", "cli", "json-cli"])
@pytest.mark.parametrize("other", ["html", "manifest", "plotly"])
@pytest.mark.parametrize("hardlink", [False, True])
def test_pdf_conflicts_with_export_outputs_before_any_write(run_dir, tmp_path, mode, other, hardlink):
    from dataclasses import replace
    from types import SimpleNamespace

    export = tmp_path / "export"
    html = export / "report.html"
    paths = {"html": html, "manifest": export / "report-assets" / "manifest.json",
             "plotly": export / "report-assets" / "plotly.min.js"}
    pdf = paths[other]
    if hardlink:
        pdf.parent.mkdir(parents=True, exist_ok=True)
        pdf.write_bytes(b"previous output")
        alias = export / "alias.pdf"
        os.link(pdf, alias)
        pdf = alias
    package = load_run_package(run_dir)
    if other == "plotly":
        package = replace(package, frequency_responses=[SimpleNamespace(rows=[], label="surface")])
    before = snapshot(tmp_path)
    with patch("wavebench.cli.load_run_package", return_value=package), patch(
        "wavebench.report.html.build_surface_payload", return_value={"surface": True}
    ), patch("wavebench.report.html.plotly_asset_available", return_value=True), patch(
        "wavebench.report.html._write_run_report_pdf"
    ) as renderer:
        if mode in ("plan-api", "api"):
            with pytest.raises(ConfigError, match="outputs conflict"):
                writer = plan_run_report_outputs if mode == "plan-api" else write_run_report
                writer(package, html, pdf_output_path=pdf)
        else:
            args = ["run", "report", str(run_dir), "--output", str(html), "--pdf",
                    "--pdf-output", str(pdf)]
            assert main((["--json"] if mode == "json-cli" else []) + args) == 2
        renderer.assert_not_called()
    assert snapshot(tmp_path) == before


def test_noninteractive_export_does_not_reserve_plotly_output(run_dir, tmp_path):
    import sys
    from types import SimpleNamespace

    html = tmp_path / "export" / "report.html"
    pdf = html.parent / "report-assets" / "plotly.min.js"

    class FakeHTML:
        def __init__(self, **kwargs):
            pass

        def write_pdf(self, path):
            pdf.write_bytes(b"%PDF-fake")

    before = snapshot(run_dir)
    with patch.dict(sys.modules, {"weasyprint": SimpleNamespace(HTML=FakeHTML)}):
        assert main(["run", "report", str(run_dir), "--output", str(html), "--pdf",
                     "--pdf-output", str(pdf)]) == 0
    assert "<!doctype html>" in html.read_text(encoding="utf-8").lower()
    assert json.loads((pdf.parent / "manifest.json").read_text(encoding="utf-8"))["schema"] == "wavebench.report_manifest.v1"
    assert pdf.read_bytes() == b"%PDF-fake"
    assert snapshot(run_dir) == before


@pytest.mark.parametrize("pair", [("html", "plotly"), ("manifest", "plotly")])
@pytest.mark.parametrize("mode", ["api", "cli", "json-cli"])
def test_interactive_sidecar_hardlinks_rejected_before_any_write(run_dir, tmp_path, pair, mode):
    from dataclasses import replace
    from types import SimpleNamespace

    html = tmp_path / "export" / "report.html"
    assets = html.parent / "report-assets"
    assets.mkdir(parents=True)
    paths = {"html": html, "manifest": assets / "manifest.json", "plotly": assets / "plotly.min.js"}
    paths[pair[0]].write_bytes(b"previous output")
    os.link(paths[pair[0]], paths[pair[1]])
    package = replace(load_run_package(run_dir), frequency_responses=[
        SimpleNamespace(rows=[], label="surface")])
    before = snapshot(tmp_path)
    with patch("wavebench.cli.load_run_package", return_value=package), patch(
        "wavebench.report.html.build_surface_payload", return_value={"surface": True}
    ), patch("wavebench.report.html.plotly_asset_available", return_value=True), patch(
        "wavebench.report.html.write_plotly_asset"
    ) as asset_writer:
        if mode == "api":
            with pytest.raises(ConfigError, match="outputs conflict"):
                write_run_report_html(package, html)
        else:
            args = ["run", "report", str(run_dir), "--output", str(html)]
            assert main((["--json"] if mode == "json-cli" else []) + args) == 2
        asset_writer.assert_not_called()
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("destination", ["default", "custom", "external"])
def test_combined_html_pdf_export_can_refresh(run_dir, tmp_path, destination):
    import sys
    from pathlib import Path
    from types import SimpleNamespace

    html = (run_dir / "report.html" if destination == "default" else
            run_dir / "custom.html" if destination == "custom" else
            tmp_path / "export" / "custom.html")
    pdf = html.with_suffix(".pdf")
    before = snapshot(run_dir)

    class FakeHTML:
        def __init__(self, **kwargs):
            pass

        def write_pdf(self, path):
            Path(path).write_bytes(b"%PDF-fake")

    with patch.dict(sys.modules, {"weasyprint": SimpleNamespace(HTML=FakeHTML)}):
        for _ in range(2):
            outputs = write_run_report(load_run_package(run_dir), html, pdf_output_path=pdf)
            assert outputs.html == html
            assert outputs.pdf == pdf
    assert "<!doctype html>" in html.read_text(encoding="utf-8").lower()
    assert pdf.read_bytes() == b"%PDF-fake"
    for name, content in before.items():
        assert (run_dir / name).read_bytes() == content


def test_referenced_pdf_overrides_manifest_refresh_exemption(run_dir, tmp_path):
    import sys
    from pathlib import Path
    from types import SimpleNamespace

    html = run_dir / "custom.html"
    pdf = run_dir / "custom.pdf"

    class FakeHTML:
        def __init__(self, **kwargs):
            pass

        def write_pdf(self, path):
            Path(path).write_bytes(b"%PDF-fake")

    with patch.dict(sys.modules, {"weasyprint": SimpleNamespace(HTML=FakeHTML)}):
        write_run_report(load_run_package(run_dir), html, pdf_output_path=pdf)
    capture = tmp_path / "capture"
    capture.mkdir()
    metadata = capture / "metadata.json"
    metadata.write_text(json.dumps({"files": {"csv": str(pdf)}}), encoding="utf-8")
    payload = {"steps": [{"artifact": {"package": str(capture), "metadata": str(metadata)}}]}
    (run_dir / "run.json").write_text(json.dumps(payload), encoding="utf-8")
    before = snapshot(tmp_path)
    with pytest.raises(ConfigError, match="original run evidence"):
        write_run_report(load_run_package(run_dir), html, pdf_output_path=pdf)
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("sidecar", ["manifest.json", "plotly.min.js"])
@pytest.mark.parametrize("hardlink", [False, True])
def test_standalone_pdf_preserves_existing_report_sidecars(run_dir, tmp_path, sidecar, hardlink):
    from dataclasses import replace
    from types import SimpleNamespace

    export = tmp_path / "export"
    assets = export / "report-assets"
    assets.mkdir(parents=True)
    target = assets / sidecar
    target.write_bytes(b"original report sidecar")
    if hardlink:
        alias = export / "alias.pdf"
        os.link(target, alias)
        target = alias
    package = load_run_package(run_dir)
    if sidecar == "plotly.min.js":
        package = replace(package, frequency_responses=[SimpleNamespace(
            rows=[], label="surface", directory=run_dir, csv_path=None, fit_path=None,
            baseline_path=None, calibration_csv_path=None, calibration_path=None,
        )])
    before = snapshot(tmp_path)
    with patch("wavebench.report.html.build_surface_payload", return_value={"surface": True}), patch(
        "wavebench.report.html.plotly_asset_available", return_value=True
    ), patch(
        "wavebench.report.html._write_run_report_pdf"
    ) as renderer:
        with pytest.raises(ConfigError, match="outputs conflict"):
            write_run_report_pdf(package, target)
        renderer.assert_not_called()
    assert snapshot(tmp_path) == before


def test_surface_without_plotly_does_not_reserve_unused_asset(run_dir, tmp_path):
    from dataclasses import replace
    from types import SimpleNamespace

    package = replace(load_run_package(run_dir), frequency_responses=[SimpleNamespace(
        rows=[], label="surface", directory=run_dir, csv_path=None, fit_path=None,
        baseline_path=None, calibration_csv_path=None, calibration_path=None,
    )])
    html = tmp_path / "export" / "report.html"
    pdf = html.parent / "report-assets" / "plotly.min.js"
    with patch("wavebench.report.html.build_surface_payload", return_value={"surface": True}), patch(
        "wavebench.report.html.plotly_asset_available", return_value=False
    ):
        outputs = plan_run_report_outputs(package, html, pdf_output_path=pdf)
    assert outputs.plotly is None
    assert outputs.pdf == pdf
    assert not html.parent.exists()
