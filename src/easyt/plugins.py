"""Commands that plugins add to easyt: each is `easyt NAME ...` on the command line and /NAME in
the Telegram bot. A plugin is a package in PLUGINS_DIR whose `command.py` names its `command`."""

import argparse
import importlib
import os
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from easyt import EASYT_HOME, log

PLUGINS_DIR = EASYT_HOME / Path(os.getenv("PLUGINS_DIR") or "plugins").expanduser()

type Work = Callable[[], Awaitable[str]]  # a bot job's work, which returns its finished message


@dataclass(frozen=True)
class Command:
    name: str
    about: str  # the command line's help
    add_arguments: Callable[[argparse.ArgumentParser], None]
    run: Callable[[argparse.Namespace], Awaitable[None]]
    bot_about: str  # the bot's line for /NAME, with what it takes
    # The bot's job for the words after /NAME, as its label and work; a ValueError's message is
    # the bot's answer instead.
    job: Callable[[list[str]], tuple[str, Work]]


@cache
def commands() -> dict[str, Command]:
    """The commands of the plugins in PLUGINS_DIR, by name."""
    found = {}
    if not PLUGINS_DIR.is_dir():
        return found
    sys.path.append(str(PLUGINS_DIR))
    for package in sorted(PLUGINS_DIR.iterdir()):
        if (package / "command.py").is_file():
            try:
                command = importlib.import_module(f"{package.name}.command").command
            except Exception as error:  # a plugin being written must not stop easyt's own commands
                log.warning(
                    f"Plugin {package.name} failed to load: {type(error).__name__}: {error}"
                )
                continue
            found[command.name] = command
    return found
