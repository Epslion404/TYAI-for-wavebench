from __future__ import annotations

from base64 import urlsafe_b64encode
import csv
from hashlib import sha256
import io
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from wavebench.errors import ConfigError
from wavebench.plugins.lifecycle import InstalledPlugin, LifecycleResult, PluginLifecycle
from wavebench.plugins.package_inspect import inspect_plugin_wheel, inspect_source_project
from wavebench.plugins import registry
from wavebench.cli_output import (
    _print_installed_plugins,
    _print_lifecycle_result,
    _print_plugin_package,
)

from test_plugin_lifecycle import _target_venv


def _advisor_wheel(
    root: Path,
    *,
    version: str = "0.1.0",
    advisor_id: str = "example.advisor",
    distribution: str = "wavebench-example-advisor",
    capabilities: tuple[str, ...] = ("advisor.route",),
    api_version: str = "wavebench.advisor.v1",
    requires_dist: tuple[str, ...] = ("wavebench>=0.8,<0.9",),
    entry_points: str | None = None,
) -> Path:
    filename_name = distribution.replace("-", "_")
    dist_info = f"{filename_name}-{version}.dist-info"
    module_name = "wavebench_example_advisor"
    path = root / f"{filename_name}-{version}-py3-none-any.whl"
    dependency_lines = "".join(f"Requires-Dist: {dependency}\n" for dependency in requires_dist)
    metadata = (
        "Metadata-Version: 2.1\n"
        f"Name: {distribution}\n"
        f"Version: {version}\n"
        "Requires-Python: >=3.11\n"
        f"{dependency_lines}\n"
    ).encode()
    module = f'''from wavebench.plugins.api import AdvisorPlugin, EgressDeclaration


def advisor():
    return AdvisorPlugin(
        advisor_id={advisor_id!r},
        display_name="Example Advisor",
        provider="Example",
        capabilities={capabilities!r},
        summary="Example advisor.",
        egress=EgressDeclaration(
            transmits_off_machine=False,
            purpose="local analysis",
        ),
        api_version={api_version!r},
    )
'''.encode()
    if entry_points is None:
        entry_points = f"[wavebench.advisor]\n{advisor_id} = {module_name}:advisor\n"
    members = {
        f"{dist_info}/METADATA": metadata,
        f"{dist_info}/WHEEL": b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        f"{dist_info}/entry_points.txt": entry_points.encode(),
        f"{module_name}/__init__.py": module,
    }
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    for member, payload in members.items():
        digest = urlsafe_b64encode(sha256(payload).digest()).rstrip(b"=").decode()
        writer.writerow((member, f"sha256={digest}", len(payload)))
    writer.writerow((f"{dist_info}/RECORD", "", ""))
    members[f"{dist_info}/RECORD"] = output.getvalue().encode()
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        for member, payload in members.items():
            archive.writestr(member, payload)
    return path


def test_inspect_advisor_wheel_reports_category(tmp_path):
    wheel = _advisor_wheel(tmp_path)

    package = inspect_plugin_wheel(wheel)

    assert package.category == "advisor"
    assert package.driver_ids == ("example.advisor",)
    assert package.entry_points[0].group == "wavebench.advisor"


def test_inspect_source_project_accepts_advisor_category(tmp_path):
    project = tmp_path / "advisor-project"
    project.mkdir()
    (project / "pyproject.toml").write_text(
        """
[build-system]
requires = ["setuptools"]
build-backend = "setuptools.build_meta"

[project]
name = "wavebench-example-advisor"
version = "0.1.0"

[project.entry-points."wavebench.advisor"]
"example.advisor" = "wavebench_example_advisor:advisor"
""",
        encoding="utf-8",
    )

    source = inspect_source_project(project)

    assert source.category == "advisor"
    assert [entry.driver_id for entry in source.entry_points] == ["example.advisor"]
    assert source.entry_points[0].group == "wavebench.advisor"


def test_inspect_rejects_mixed_category_wheel(tmp_path):
    wheel = _advisor_wheel(
        tmp_path,
        entry_points=(
            "[wavebench.instruments]\n"
            "example.scope = wavebench_example_advisor:advisor\n"
            "[wavebench.advisor]\n"
            "example.advisor = wavebench_example_advisor:advisor\n"
        ),
    )

    with pytest.raises(ConfigError, match="只允许声明一个类别"):
        inspect_plugin_wheel(wheel)


def test_inspect_rejects_wheel_without_a_plugin_category(tmp_path):
    wheel = _advisor_wheel(
        tmp_path,
        entry_points="[console_scripts]\nexample = wavebench_example_advisor:advisor\n",
    )

    with pytest.raises(ConfigError, match="does not provide"):
        inspect_plugin_wheel(wheel)


def test_advisor_package_cli_output_names_advisor_ids(tmp_path, capsys):
    package = inspect_plugin_wheel(_advisor_wheel(tmp_path))

    _print_plugin_package(package)

    output = capsys.readouterr().out
    assert "category=advisor" in output
    assert "advisor_ids=example.advisor" in output
    assert "driver_ids=" not in output


def test_advisor_cli_rows_include_category(capsys):
    _print_installed_plugins(
        (
            InstalledPlugin(
                "example.advisor",
                "wavebench-example-advisor",
                "0.1.0",
                "healthy",
                category="advisor",
            ),
        )
    )
    _print_lifecycle_result(
        LifecycleResult(
            "installed",
            "example.advisor",
            "wavebench-example-advisor",
            "0.1.0",
            category="advisor",
        )
    )

    output = capsys.readouterr().out
    assert "category\tdriver_id\tdistribution\tversion\tstatus\tdetail" in output
    assert "advisor\texample.advisor\twavebench-example-advisor\t0.1.0\thealthy" in output
    assert "advisor_id=example.advisor" in output
    assert "driver_id=" not in output


def test_instrument_cli_rows_are_unchanged(capsys):
    _print_installed_plugins(
        (
            InstalledPlugin(
                "example.scope",
                "wavebench-example-scope",
                "0.1.0",
                "healthy",
            ),
        )
    )
    _print_lifecycle_result(
        LifecycleResult(
            "installed",
            "example.scope",
            "wavebench-example-scope",
            "0.1.0",
        )
    )

    output = capsys.readouterr().out
    assert "driver_id\tdistribution\tversion\tstatus\tdetail" in output
    assert "example.scope\twavebench-example-scope\t0.1.0\thealthy" in output
    assert "driver_id=example.scope" in output
    assert "category" not in output


def test_advisor_install_status_and_remove_round_trip(tmp_path):
    python = _target_venv(tmp_path)
    wheel = _advisor_wheel(tmp_path)
    lifecycle = PluginLifecycle(python_executable=python)

    installed = lifecycle.install(wheel)
    statuses = lifecycle.installed()

    assert installed.status == "installed"
    assert installed.category == "advisor"
    assert installed.driver_id == "example.advisor"
    assert [
        (item.driver_id, item.category, item.version, item.status)
        for item in statuses
    ] == [("example.advisor", "advisor", "0.1.0", "healthy")]
    assert lifecycle.info("example.advisor").category == "advisor"
    assert lifecycle.remove("example.advisor").category == "advisor"
    assert lifecycle.installed() == ()


def test_advisor_upgrade_preserves_category(tmp_path):
    python = _target_venv(tmp_path)
    v1 = _advisor_wheel(tmp_path, version="0.1.0")
    v2 = _advisor_wheel(tmp_path, version="0.2.0")
    lifecycle = PluginLifecycle(python_executable=python)

    assert lifecycle.install(v1).category == "advisor"
    upgraded = lifecycle.upgrade(v2)

    assert upgraded.status == "upgraded"
    assert upgraded.category == "advisor"
    assert [
        (item.driver_id, item.category, item.version, item.status)
        for item in lifecycle.installed()
    ] == [("example.advisor", "advisor", "0.2.0", "healthy")]


def test_advisor_drift_is_reported(tmp_path):
    python = _target_venv(tmp_path)
    wheel = _advisor_wheel(tmp_path)
    lifecycle = PluginLifecycle(python_executable=python)
    lifecycle.install(wheel)
    module = Path(lifecycle.environment().purelib) / "wavebench_example_advisor/__init__.py"
    module.write_text(module.read_text(encoding="utf-8") + "\n# drifted\n", encoding="utf-8")

    statuses = lifecycle.installed()

    assert [(item.driver_id, item.category, item.status) for item in statuses] == [
        ("example.advisor", "advisor", "drifted")
    ]


def test_advisor_recover_commits_desired_state(tmp_path):
    python = _target_venv(tmp_path)
    wheel = _advisor_wheel(tmp_path)
    lifecycle = PluginLifecycle(python_executable=python)
    environment = lifecycle.environment()
    before = lifecycle.empty_ledger(environment)
    with lifecycle._inspected_input(wheel) as package:
        cached = lifecycle._cache_wheel(package)
        record = lifecycle._package_record(package, cached)
    journal = lifecycle._journal(
        environment=environment,
        operation="install",
        stage="pip_finished",
        before_ledger=before,
        package=record,
    )
    lifecycle.state_dir.mkdir(mode=0o700, exist_ok=True)
    lifecycle._write_json(lifecycle.journal_path, journal)
    lifecycle._pip_install(cached)

    result = lifecycle.recover()

    assert result.status == "recovered-to-desired"
    assert result.category == "advisor"
    assert lifecycle.info("example.advisor").category == "advisor"
    assert not lifecycle.journal_path.exists()


def test_advisor_postflight_rejects_invalid_metadata(tmp_path):
    python = _target_venv(tmp_path)
    wheel = _advisor_wheel(tmp_path, capabilities=("scope.idn",))
    lifecycle = PluginLifecycle(python_executable=python)

    with pytest.raises(ConfigError, match="advisor validation failed"):
        lifecycle.install(wheel)

    assert lifecycle.installed() == ()


class _FakeMetadata:
    def __init__(self, name: str, requires: tuple[str, ...]) -> None:
        self._name = name
        self._requires = requires

    def get(self, key: str, default=None):
        if key == "Name":
            return self._name
        return default

    def get_all(self, key: str):
        if key == "Requires-Dist":
            return list(self._requires)
        return []


class _FakeEntryPoint:
    group = "wavebench.advisor"
    name = "example.advisor"
    value = "wavebench_example_advisor:advisor"


class _FakeDistribution:
    metadata = _FakeMetadata(
        "wavebench-example-advisor",
        (
            "wavebench>=0.8,<0.9",
            "missing-example>=1",
            "optional-example; extra == 'dev'",
        ),
    )
    entry_points = (_FakeEntryPoint(),)


def test_advisor_dependency_doctor_reports_missing_without_network(monkeypatch):
    monkeypatch.setattr(registry, "distributions", lambda: [_FakeDistribution()])

    def missing(name):
        raise registry.PackageNotFoundError(name)

    monkeypatch.setattr(registry, "distribution_version", missing)

    records = registry.advisor_dependency_doctor_records()

    assert [
        (record.severity, record.subject, record.message)
        for record in records
    ] == [
        (
            "error",
            "example.advisor",
            "missing dependency: missing-example>=1",
        )
    ]
