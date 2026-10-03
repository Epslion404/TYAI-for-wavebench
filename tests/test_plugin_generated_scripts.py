"""Validate pip-generated launchers before mutating the plugin environment."""

import os
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from test_plugin_lifecycle import _plugin_wheel, _target_venv
from test_plugin_package_inspect import _wheel
from wavebench.errors import ConfigError
from wavebench.plugins.lifecycle import PluginLifecycle
from wavebench.plugins.package_inspect import inspect_plugin_wheel


@pytest.mark.parametrize("group", ["console_scripts", "gui_scripts"])
@pytest.mark.parametrize("name", ["wavebench", "WAVEBENCH", "wavebench.exe"])
def test_core_generated_launcher_is_rejected_during_wheel_inspection(tmp_path, group, name):
    wheel = _wheel(tmp_path, entry_points=f'''[wavebench.instruments]
example.scope = example:descriptor
[{group}]
{name} = example:main
''')
    with pytest.raises(ConfigError, match="WaveBench launcher"):
        inspect_plugin_wheel(wheel)


@pytest.mark.parametrize("name", ["../evil", "a/b", "a\\b", "a?stream", "..", "trailing."])
def test_unsafe_generated_name_is_rejected(tmp_path, name):
    wheel = _wheel(tmp_path, entry_points=f'''[wavebench.instruments]
example.scope = example:descriptor
[console_scripts]
{name} = example:main
''')
    with pytest.raises(ConfigError):
        inspect_plugin_wheel(wheel)


def test_console_and_gui_same_target_is_rejected(tmp_path):
    wheel = _wheel(tmp_path, entry_points='''[wavebench.instruments]
example.scope = example:descriptor
[console_scripts]
example = example:main
[gui_scripts]
example = example:main
''')
    with pytest.raises(ConfigError, match="conflicting targets"):
        inspect_plugin_wheel(wheel)


@pytest.mark.skipif(os.name != "nt", reason="distlib Windows launcher naming")
@pytest.mark.parametrize("extension", [".py", ".pyw", ".python"])
def test_py_suffix_alias_cannot_replace_core_launcher(tmp_path, extension):
    wheel = _wheel(tmp_path, entry_points=f'''[wavebench.instruments]
example.scope = example:descriptor
[console_scripts]
wavebench{extension} = example:main
''')
    with pytest.raises(ConfigError, match="WaveBench launcher"):
        inspect_plugin_wheel(wheel)


@pytest.mark.skipif(os.name != "nt", reason="distlib Windows launcher naming")
def test_py_aliases_collide_but_uppercase_suffix_is_preserved(tmp_path):
    text = '''[wavebench.instruments]
example.scope = example:descriptor
[console_scripts]
example.py = example:main
example = example:main
'''
    with pytest.raises(ConfigError, match="conflicting targets"):
        inspect_plugin_wheel(_wheel(tmp_path, entry_points=text))
    package = inspect_plugin_wheel(_wheel(tmp_path, entry_points=text.replace("example.py", "example.PY")))
    assert package.generated_scripts == ("example.PY.exe", "example.exe")


@pytest.mark.parametrize("installer", ["pip", "easy_install"])
def test_installer_special_aliases_are_rejected(tmp_path, installer):
    text = f'''[wavebench.instruments]
example.scope = example:descriptor
[console_scripts]
{installer} = example:main
'''
    with pytest.raises(ConfigError, match="installer launchers"):
        inspect_plugin_wheel(_wheel(tmp_path, entry_points=text))


@pytest.mark.parametrize("ownership", ["foreign", "missing", "self", "merged"])
def test_generated_target_ownership_includes_scripts_directory(tmp_path, ownership):
    package = inspect_plugin_wheel(_wheel(tmp_path))
    name = "example.exe" if os.name == "nt" else "example"
    package = replace(package, generated_scripts=(name,))
    scripts = tmp_path / "env" / "Scripts"
    purelib = tmp_path / "env" / "Lib" / "site-packages"
    scripts.mkdir(parents=True)
    purelib.mkdir(parents=True)
    target = scripts / name
    target.write_bytes(b"original launcher")
    lifecycle = PluginLifecycle(python_executable=tmp_path / "python")
    lifecycle._environment = SimpleNamespace(purelib=str(purelib), scripts=str(scripts))
    relative = os.path.relpath(target, purelib).replace("\\", "/")
    owners = {} if ownership == "missing" else {relative: (
        (package.normalized_distribution,) if ownership == "self" else ("foreign",)
    )}
    if ownership == "merged":
        owners[relative.upper() if os.name == "nt" else relative] = (package.normalized_distribution, "foreign")
    if ownership == "self":
        lifecycle._assert_no_file_ownership_conflicts(package, owners,
                                                    ignored_distribution=package.distribution)
    else:
        with pytest.raises(ConfigError, match="launcher"):
            lifecycle._assert_no_file_ownership_conflicts(package, owners,
                                                        ignored_distribution=package.distribution)
    assert target.read_bytes() == b"original launcher"


def test_predicted_script_owner_blocks_even_missing_file(tmp_path):
    package = inspect_plugin_wheel(_wheel(tmp_path))
    package = replace(package, generated_scripts=("example",))
    purelib, scripts = tmp_path / "lib", tmp_path / "bin"
    lifecycle = PluginLifecycle(python_executable=tmp_path / "python")
    lifecycle._environment = SimpleNamespace(purelib=str(purelib), scripts=str(scripts))
    relative = os.path.relpath(scripts / "example", purelib).replace("\\", "/")
    with pytest.raises(ConfigError, match="overlaps"):
        lifecycle._assert_no_file_ownership_conflicts(package, {relative: ("foreign",)})


@pytest.mark.skipif(os.name != "nt", reason="distlib Windows locked-launcher fallback")
def test_foreign_deleteme_side_file_is_protected(tmp_path):
    package = replace(inspect_plugin_wheel(_wheel(tmp_path)), generated_scripts=("example.exe",))
    purelib, scripts = tmp_path / "lib", tmp_path / "Scripts"
    scripts.mkdir()
    side_file = scripts / "example.exe.deleteme"
    side_file.write_bytes(b"unmanaged side file")
    lifecycle = PluginLifecycle(python_executable=tmp_path / "python")
    lifecycle._environment = SimpleNamespace(purelib=str(purelib), scripts=str(scripts))
    with pytest.raises(ConfigError, match="existing launcher"):
        lifecycle._assert_no_file_ownership_conflicts(package, {})
    assert side_file.read_bytes() == b"unmanaged side file"


def test_native_lifecycle_script_install_upgrade_rollback_and_remove(tmp_path, monkeypatch):
    python = _target_venv(tmp_path)
    lifecycle = PluginLifecycle(python_executable=python)
    entries = '\n[console_scripts]\nexample-tool = wavebench_example_scope:descriptor\n'
    first = _plugin_wheel(tmp_path, version="0.1.0", script_entries=entries)
    desired = _plugin_wheel(tmp_path, version="0.2.0", script_entries=entries)
    broken = _plugin_wheel(tmp_path, version="0.3.0", script_entries=entries, broken_descriptor=True)
    assert lifecycle.install(first, dry_run=True).status == "would-install"
    lifecycle.install(first)
    script = os.path.join(lifecycle.environment().scripts,
                          "example-tool.exe" if os.name == "nt" else "example-tool")
    from pathlib import Path
    assert Path(script).is_file()
    conflict = _plugin_wheel(tmp_path, version="0.2.5", script_entries=(entries
        + "future-tool = wavebench_example_scope:descriptor\n"))
    foreign = Path(lifecycle.environment().scripts) / (
        "future-tool.exe" if os.name == "nt" else "future-tool"
    )
    foreign.write_bytes(b"unmanaged future launcher")
    before_upgrade = lifecycle.ledger_path.read_bytes()
    with patch.object(lifecycle, "_pip_install") as pip:
        for dry_run in (True, False):
            with pytest.raises(ConfigError, match="existing launcher"):
                lifecycle.upgrade(conflict, dry_run=dry_run)
        pip.assert_not_called()
    assert lifecycle.ledger_path.read_bytes() == before_upgrade
    assert foreign.read_bytes() == b"unmanaged future launcher"
    foreign.unlink()
    lifecycle.upgrade(desired)
    with pytest.raises(ConfigError):
        lifecycle.upgrade(broken)
    assert lifecycle.installed()[0].version == "0.2.0"
    lifecycle.downgrade(first)
    assert lifecycle.installed()[0].status == "healthy"
    lifecycle.remove("example.scope")
    assert not Path(script).exists()

    # An unmanaged script must be refused by formal and dry-run installation.
    Path(script).write_bytes(b"unmanaged launcher")
    pip = Mock()
    monkeypatch.setattr(lifecycle, "_pip_install", pip)
    before = lifecycle.ledger_path.read_bytes()
    for dry_run in (True, False):
        with pytest.raises(ConfigError, match="existing launcher"):
            lifecycle.install(first, dry_run=dry_run)
    pip.assert_not_called()
    assert lifecycle.ledger_path.read_bytes() == before
    assert Path(script).read_bytes() == b"unmanaged launcher"
