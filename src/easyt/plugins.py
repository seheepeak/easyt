"""Commands other packages add to easyt, found by their `easyt.commands` entry points: each is
`easyt NAME ...` on the command line and /NAME in the Telegram bot."""

import argparse
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import cache
from importlib.metadata import entry_points

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
    """The commands of the installed plugins, by name."""
    found = {}
    for entry_point in entry_points(group="easyt.commands"):
        command = entry_point.load()
        found[command.name] = command
    return found
