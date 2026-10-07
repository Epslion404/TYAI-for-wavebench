"""Keep derived report writes separate from the run's original evidence."""

import json
from pathlib import Path
from urllib.parse import unquote, urlsplit

from wavebench.data.packages import RunPackage
from wavebench.errors import ConfigError


def report_manifest_paths(run: RunPackage, output_dir: Path) -> dict[str, Path]:
    """Read refreshable report names only from a manifest for this run."""
    manifest = output_dir / "report-assets" / "manifest.json"
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        source = urlsplit(payload.get("run_json", ""))
        if (payload.get("schema") != "wavebench.report_manifest.v1"
                or source.scheme or source.netloc
                or (output_dir / unquote(source.path)).resolve() != run.run_json_path.resolve()):
            return {}
        result = {}
        for field in ("report", "pdf"):
            raw_path = payload.get(field)
            if isinstance(raw_path, str):
                path = urlsplit(raw_path)
                if path.path and not path.scheme and not path.netloc:
                    result[field] = (output_dir / unquote(path.path)).resolve()
        return result
    except (OSError, ValueError, TypeError, AttributeError):
        return {}


def validate_report_output_pairs(targets: list[Path]) -> None:
    # All files in one export must be distinct, including existing hard links.
    for index, target in enumerate(targets):
        for other in targets[:index]:
            if target.resolve() == other.resolve() or (
                target.exists() and other.exists() and target.samefile(other)
            ):
                raise ConfigError("report outputs conflict; choose separate paths for each output")


def validate_run_report_paths(
    run: RunPackage, targets: list[Path], *, evidence_dirs=(), evidence_files=()
) -> None:
    validate_report_output_pairs(targets)
    root = run.path.resolve()
    report_files = {root / "report.html", root / "report.pdf"}
    # Preserve repeat exports with custom names recorded by our report manifest.
    for target in targets:
        report_files.update(report_manifest_paths(run, target.parent).values())

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
                # A backlink to the run root must not turn derived root reports
                # into evidence. Root files already have their own policy above.
                if resolved == root:
                    continue
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
