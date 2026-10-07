from __future__ import annotations

import ntpath
import os
from pathlib import Path
import runpy

import pytest

from wavebench.errors import ConfigError
from wavebench.plugins import install_paths
from wavebench.plugins.lifecycle import PluginLifecycle


def test_managed_distribution_ownership_checks_case_alias_before_remove(monkeypatch):
    monkeypatch.setattr(install_paths, "_path", ntpath)
    lifecycle = object.__new__(PluginLifecycle)
    monkeypatch.setattr(lifecycle, "_inventory", lambda **kwargs: {
        "managed-plugin": ({"files": ["Plugin/data.txt"]},)
    })
    monkeypatch.setattr(lifecycle, "_file_owners", lambda **kwargs: {
        "Plugin/data.txt": ("managed-plugin",),
        "PLUGIN/DATA.txt": ("foreign-plugin",),
    })
    with pytest.raises(ConfigError, match="shares files"):
        lifecycle._assert_distribution_file_ownership("managed-plugin")


@pytest.mark.skipif(os.name != "nt", reason="native Windows RECORD case-alias integration")
def test_native_windows_case_alias_blocks_status_and_mutations(tmp_path, monkeypatch):
    fixtures = runpy.run_path(str(Path(__file__).with_name("test_plugin_lifecycle.py")))
    python = fixtures["_target_venv"](tmp_path)
    wheel = fixtures["_plugin_wheel"]
    lifecycle = PluginLifecycle(python_executable=python)
    current = wheel(tmp_path, version="0.2.0")
    lifecycle.install(current)
    owners = lifecycle._file_owners(strict=True)
    owners["WAVEBENCH_EXAMPLE_SCOPE/__INIT__.PY"] = ("foreign-plugin",)
    monkeypatch.setattr(lifecycle, "_file_owners", lambda **kwargs: owners)
    ledger_before = lifecycle.ledger_path.read_bytes()
    pip_calls = []
    monkeypatch.setattr(lifecycle, "_pip_install", pip_calls.append)
    monkeypatch.setattr(lifecycle, "_pip_uninstall", lambda *args, **kwargs: pip_calls.append(args))

    status = lifecycle.info("example.scope")
    assert status.status == "broken"
    assert "shared ownership" in status.detail
    for dry_run in (True, False):
        for version, operation in (("0.3.0", lifecycle.upgrade), ("0.1.0", lifecycle.downgrade)):
            with pytest.raises(ConfigError, match="healthy"):
                operation(wheel(tmp_path, version=version), dry_run=dry_run)
        with pytest.raises(ConfigError, match="healthy"):
            lifecycle.remove("example.scope", dry_run=dry_run)
    assert pip_calls == []
    assert lifecycle.ledger_path.read_bytes() == ledger_before
    assert not lifecycle.journal_path.exists()
