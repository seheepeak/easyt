"""Each stage's prompt, kept in a `*_prompts.toml` file as an array of tables, one per message."""

import tomllib
from dataclasses import dataclass, field, replace
from functools import cache, reduce
from operator import getitem
from pathlib import Path


@dataclass(frozen=True)
class Prompt:
    """A prompt's messages in order, the system message first if it has one, each a `str.format`
    template, so a literal brace is doubled."""

    templates: tuple[str, ...]
    partials: dict[str, str] = field(default_factory=dict)

    def partial(self, **values: str) -> "Prompt":
        """This prompt with `values` filled in ahead of the rest."""
        return replace(self, partials={**self.partials, **values})

    def format(self, **values: str) -> list[str]:
        """Each message's text, its placeholders filled from the partials and `values`."""
        return [template.format(**{**self.partials, **values}) for template in self.templates]


@cache
def _prompts(directory: Path) -> dict:
    """The tables of every `*_prompts.toml` file in `directory`, merged."""
    prompts = {}
    for path in directory.glob("*.toml"):
        prompts |= tomllib.loads(path.read_text(encoding="utf-8"))
    return prompts


def load_prompt(name: str, directory: Path = Path(__file__).parent) -> Prompt:
    """The prompt at the dotted `name`, such as `aterm.step1`, from the `*_prompts.toml` files in
    `directory`: easyt's own, or a plugin's."""
    messages = reduce(getitem, name.split("."), _prompts(directory))
    return Prompt(tuple(message["content"] for message in messages))
