from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from wavebench.data.package import new_package_dir, write_new_file
from wavebench.services import decision_artifacts as artifacts


def test_same_second_package_creation_is_independent(tmp_path):
    with patch("wavebench.data.package.datetime") as clock:
        clock.now.return_value = datetime(2026, 10, 3, 12, 0, 0)

        def create(_):
            path = new_package_dir(tmp_path, "same label")
            path.mkdir()
            return path

        with ThreadPoolExecutor(max_workers=8) as pool:
            paths = list(pool.map(create, range(32)))
    assert len(set(paths)) == 32
    assert all(p.name.startswith("20261003_120000_") for p in paths)
    assert all(p.name.endswith("_same_label") for p in paths)


def test_same_second_decisions_are_independent_and_parseable(tmp_path):
    def create(index):
        return artifacts.write_artifact(
            artifacts.DecisionArtifact(status="ok", requested_model=str(index)),
            tmp_path, advisor_id="rule", timestamp="20261003T120000Z",
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        paths = list(pool.map(create, range(32)))
    assert len(set(paths)) == 32
    models = {json.loads(p.read_text(encoding="utf-8"))["advisory"]["requested_model"]
              for p in paths}
    assert models == {str(i) for i in range(32)}
    assert len(list((tmp_path / "decisions").iterdir())) == 32


def test_concurrent_publication_has_one_winner_and_never_overwrites(tmp_path):
    target = tmp_path / "evidence.json"

    def publish(index):
        content = json.dumps({"index": index}).encode()
        try:
            write_new_file(target, content)
        except FileExistsError:
            return None
        return content

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(publish, range(32)))
    [winner] = [r for r in results if r is not None]
    assert target.read_bytes() == winner
    assert list(tmp_path.iterdir()) == [target]


@pytest.mark.parametrize("stage", ["write", "flush", "publish"])
def test_failed_publication_leaves_no_final_or_temporary_file(tmp_path, stage):
    target = tmp_path / "evidence.json"
    if stage == "write":
        # Real buffered output writes a prefix before an injected write failure.
        from wavebench.data import package
        real_temporary = package.tempfile.NamedTemporaryFile

        def temporary(*args, **kwargs):
            handle = real_temporary(*args, **kwargs)

            def fail(content):
                handle.file.write(content[:2])
                raise OSError("partial write")

            handle.write = fail
            return handle

        context = patch.object(package.tempfile, "NamedTemporaryFile", side_effect=temporary)
    else:
        context = patch("wavebench.data.package.os." +
                        ("fsync" if stage == "flush" else "link"),
                        side_effect=OSError("injected failure"))
    with context, pytest.raises(OSError):
        write_new_file(target, b'{"complete": true}')
    assert list(tmp_path.iterdir()) == []


def test_final_is_absent_until_complete_temporary_is_closed(tmp_path):
    target = tmp_path / "evidence.json"
    content = b'{"complete": true}'
    real_link = os.link

    def publish(source, destination):
        assert not target.exists()
        assert Path(source).read_bytes() == content
        # Opening again for write proves the staging handle is closed on Windows.
        with Path(source).open("ab"):
            pass
        real_link(source, destination)

    with patch("wavebench.data.package.os.link", side_effect=publish):
        write_new_file(target, content)
    assert target.read_bytes() == content
    assert list(tmp_path.iterdir()) == [target]


def test_serialization_failure_does_not_publish_decision(tmp_path):
    artifact = artifacts.DecisionArtifact(status="ok", requested_model="rule")
    with patch.object(artifacts.DecisionArtifact, "to_json", side_effect=ValueError("encode")):
        with pytest.raises(ValueError):
            artifacts.write_artifact(artifact, tmp_path, advisor_id="rule")
    assert list((tmp_path / "decisions").iterdir()) == []
