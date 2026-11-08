"""The LLM prompts, a TOML file per stage, named by the first part of a prompt's name."""

import tomllib
from dataclasses import dataclass, field, replace
from functools import reduce
from operator import getitem
from pathlib import Path


@dataclass(frozen=True)
class Prompt:
    """A prompt's messages in order, the system message first, each a `str.format` template, so a
    literal brace is doubled."""

    templates: tuple[str, ...]
    partials: dict[str, str] = field(default_factory=dict)

    def partial(self, **values: str) -> "Prompt":
        """This prompt with `values` filled in ahead of the rest."""
        return replace(self, partials={**self.partials, **values})

    def format(self, **values: str) -> list[str]:
        """Each message's text, its placeholders filled from the partials and `values`."""
        return [template.format(**{**self.partials, **values}) for template in self.templates]


def _find(name: str, directory: Path):
    """What the dotted `name` holds in the file of `directory` its first part names."""
    keys = name.split(".")
    tables = tomllib.loads((directory / f"{keys[0]}.toml").read_text(encoding="utf-8"))
    return reduce(getitem, keys, tables)


def load_prompt(name: str, directory: Path = Path(__file__).parent) -> Prompt:
    """The prompt at the dotted `name`, such as `terms.article`, from `directory`: easyt's own, or
    a plugin's."""
    return Prompt(tuple(message["content"] for message in _find(name, directory)["messages"]))


def load_text(name: str, default: str | None = None) -> str:
    """The text at the dotted `name`, such as `translate.pairs.en.ko`, from easyt's own prompts;
    `default` where there is none, or KeyError without one."""
    try:
        return _find(name, Path(__file__).parent)
    except KeyError:
        if default is None:
            raise
        return default
