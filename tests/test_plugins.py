"""How easyt finds the plugins in its plugins folder."""

import sys

import easyt.plugins

COMMAND = """from easyt.plugins import Command

command = Command("echo", "Echo a word", print, print, "Echo a word: /echo WORD", print)
"""


def test_a_package_whose_command_py_names_a_command_is_a_plugin(monkeypatch, tmp_path):
    package = tmp_path / "echo_plugin"
    package.mkdir()
    (package / "__init__.py").touch()
    (package / "command.py").write_text(COMMAND)
    (tmp_path / "notes").mkdir()  # no command.py: not a plugin
    monkeypatch.setattr(easyt.plugins, "PLUGINS_DIR", tmp_path)
    monkeypatch.setattr(sys, "path", list(sys.path))
    for name in ("echo_plugin", "echo_plugin.command"):
        monkeypatch.delitem(sys.modules, name, raising=False)

    assert list(easyt.plugins.commands()) == ["echo"]


def test_no_plugins_folder_means_no_plugins():
    assert easyt.plugins.commands() == {}
