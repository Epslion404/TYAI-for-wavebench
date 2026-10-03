"""Keep derived report writes separate from the run's original evidence."""

import json
from pathlib import Path
from urllib.parse import unquote, urlsplit

from wavebench.data.packages import RunPackage
from wavebench.errors import ConfigError


def validate_run_report_paths(
    run: RunPackage, targets: list[Path], *, evidence_dirs=(), evidence_files=()
) -> None:
    root = run.path.resolve()
    report_files = {root / "report.html", root / "report.pdf"}
    # Preserve repeat exports with custom names recorded by our report manifest.
    for target in targets:
        manifest = target.parent / "report-assets" / "manifest.json"
        if manifest.is_file():
            try:
                payload = json.loads(manifest.read_text(encoding="utf-8"))
                report = urlsplit(payload.get("report", ""))
                source = urlsplit(payload.get("run_json", ""))
                if (payload.get("schema") == "wavebench.report_manifest.v1"
                        and not report.scheme and not report.netloc
                        and not source.scheme and not source.netloc
                        and (target.parent / unquote(source.path)).resolve() == run.run_json_path.resolve()):
                    report_files.add((target.parent / unquote(report.path)).resolve())
            except (OSError, ValueError, TypeError, AttributeError):
                pass

    files = {run.run_json_path.resolve(), root / "plan.toml", root / "summary.csv",
             root / "frequency_responses.json"}
    files.update(Path(path).resolve() for path in evidence_files)
    directories = {(root / name).resolve() for name in
                   ("steps", "captures", "processing", "frequency_response")}
    directories.update(Path(directory).resolve() for directory in evidence_dirs)
    for field in ("summary_csv_path", "frequency_response_csv_path", "frequency_response_fit_path",
                  "frequency_response_calibration_csv_path", "frequency_response_calibration_path",
                  "frequency_responses_manifest_path"):
        path = getattr(run, field)
        if path is not None:
            files.add(path.resolve())
    for response in run.frequency_responses:
        if response.directory.resolve() != root:
            directories.add(response.directory.resolve())
        for field in ("csv_path", "fit_path", "baseline_path", "calibration_csv_path", "calibration_path"):
            path = getattr(response, field)
            if path is not None:
                files.add(path.resolve())
    # Unrecognized files at the run root are evidence, regardless of extension.
    for path in root.iterdir():
        if path.is_file() and path.resolve() not in report_files:
            files.add(path.resolve())

    # Follow evidence directory aliases, including nested junctions, once per
    # resolved directory. This also collects identities for external hard links.
    pending = list(directories)
    visited = set()
    while pending:
        directory = pending.pop().resolve()
        if directory in visited:
            continue
        visited.add(directory)
        if not directory.is_dir():
            continue
        for path in directory.iterdir():
            if path.is_dir():
                resolved = path.resolve()
                directories.add(resolved)
                pending.append(resolved)
            elif path.is_file():
                files.add(path.resolve())

    for target in targets:
        resolved = target.resolve()
        if resolved == root or resolved in files or any(
            resolved.is_relative_to(directory) for directory in directories
        ):
            raise ConfigError("report output would overwrite original run evidence; choose a separate path")
        # Resolving links handles symlinks/junctions; hard links need inode identity.
        if target.exists():
            if any(path.exists() and target.samefile(path) for path in files):
                raise ConfigError("report output aliases original run evidence; choose a separate path")
