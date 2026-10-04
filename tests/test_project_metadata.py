import tomllib
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_dev_toolchain_is_reproducible() -> None:
    metadata = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dev_dependencies = metadata["project"]["optional-dependencies"]["dev"]

    assert "hatchling==1.30.1" in dev_dependencies
    assert "ruff==0.15.20" in dev_dependencies
    assert metadata["tool"]["ruff"]["lint"]["select"] == ["E4", "E7", "E9", "F"]


def test_can_backend_is_an_optional_dependency() -> None:
    metadata = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert all("python-can" not in dependency for dependency in metadata["project"]["dependencies"])
    assert "python-can>=4.3" in metadata["project"]["optional-dependencies"]["can"]
