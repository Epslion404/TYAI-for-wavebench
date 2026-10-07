"""Exercise drift verdicts against real, isolated git trees."""

from pathlib import Path
import os
import subprocess
import sys

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_snapshot_drift.py"
PREFIX = "比赛区/天翼AI电子科研Skill/wavebench/"


def git(repo, *args):
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, encoding="utf-8"
    ).stdout.strip()


def commit(repo, files):
    for name, content in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
        "-c", "commit.gpgsign=false", "commit", "-m", "fixture")
    return git(repo, "rev-parse", "HEAD")


def check(repo, *args):
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args], cwd=repo, capture_output=True,
        encoding="utf-8", env={**os.environ, "PYTHONIOENCODING": "utf-8"}, timeout=20,
    )


@pytest.mark.parametrize("kind", ["sync", "extra", "missing", "stale"])
def test_same_ref_verdict(tmp_path, kind):
    git(tmp_path, "init")
    files = {"src/core.py": "same", PREFIX + "src/core.py": "same", "AGENTS.md": "root only"}
    if kind == "extra":
        files[PREFIX + "retired.py"] = "obsolete"
    elif kind == "missing":
        files["new.py"] = "new"
    elif kind == "stale":
        files[PREFIX + "src/core.py"] = "old"
    sha = commit(tmp_path, files)
    result = check(tmp_path)
    assert result.returncode == (0 if kind == "sync" else 1), result.stderr
    assert sha in result.stdout
    assert ("IN SYNC" if kind == "sync" else "DRIFT:") in result.stdout
    if kind == "extra":
        assert "1 个快照多余" in result.stdout


def test_cross_ref_uses_root_baseline(tmp_path):
    git(tmp_path, "init")
    older = commit(tmp_path, {"src/core.py": "old", PREFIX + "src/core.py": "old"})
    newer = commit(tmp_path, {"src/core.py": "new"})
    assert check(tmp_path, "--ref", older).returncode == 0
    result = check(tmp_path, "--root-ref", newer, "--snapshot-ref", older, "--quiet")
    assert result.returncode == 1, result.stderr
    assert older in result.stdout and newer in result.stdout
    assert "1 个内容落后" in result.stdout


def test_absent_snapshot_is_explicit(tmp_path):
    git(tmp_path, "init")
    commit(tmp_path, {"src/core.py": "core"})
    assert check(tmp_path).returncode == 0
    result = check(tmp_path, "--snapshot-ref", "HEAD")
    assert result.returncode == 2
    assert "no packaged snapshot" in result.stdout


def test_invalid_ref_fails(tmp_path):
    git(tmp_path, "init")
    commit(tmp_path, {"src/core.py": "core"})
    result = check(tmp_path, "--root-ref", "does-not-exist")
    assert result.returncode != 0
    assert "failed:" in result.stderr
