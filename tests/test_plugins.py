"""How easyt finds the plugins in its plugins folder."""

import sys

import easyt.plugins

COMMAND = """from easyt.plugins import Command

command = Command("echo", "Echo a word", print, print, "Echo a word: /echo WORD", print)
"""


def plugins_folder(monkeypatch, folder, **packages):
    """Make `folder` easyt's plugins folder, with a package for each keyword whose command.py is
    its value."""
    for name, command_py in packages.items():
        (folder / name).mkdir()
        (folder / name / "__init__.py").touch()
        (folder / name / "command.py").write_text(command_py)
        for module in (name, f"{name}.command"):
            monkeypatch.delitem(sys.modules, module, raising=False)
    monkeypatch.setattr(easyt.plugins, "PLUGINS_DIR", folder)
    monkeypatch.setattr(sys, "path", list(sys.path))


def test_a_package_whose_command_py_names_a_command_is_a_plugin(monkeypatch, tmp_path):
    plugins_folder(monkeypatch, tmp_path, echo_plugin=COMMAND)
    (tmp_path / "notes").mkdir()  # no command.py: not a plugin

    assert list(easyt.plugins.commands()) == ["echo"]


def test_a_plugin_that_fails_to_load_is_left_out_with_a_warning(monkeypatch, tmp_path, caplog):
    plugins_folder(monkeypatch, tmp_path, broken_plugin="def oops(:\n", echo_plugin=COMMAND)

    assert list(easyt.plugins.commands()) == ["echo"]
    assert "Plugin broken_plugin failed to load: SyntaxError" in caplog.text


def test_no_plugins_folder_means_no_plugins():
    assert easyt.plugins.commands() == {}
