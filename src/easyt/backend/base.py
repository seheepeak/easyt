"""What every backend offers, whether it runs a model through pydantic-ai or an agent CLI."""

from abc import ABC, abstractmethod
from contextlib import AbstractAsyncContextManager

from pydantic import BaseModel, ValidationError

from easyt import log
from easyt.parser.structured import GuideError, append_format_guide, default_tag, parse_guided


class Session(ABC):
    """Turns that share one history."""

    @abstractmethod
    async def ask(self, prompt: str) -> str:
        """The answer to `prompt` as the next turn."""

    async def ask_structured[ModelT: BaseModel](
        self, prompt: str, model: type[ModelT], *, tag: str | None = None, retries: int = 3
    ) -> ModelT:
        """The `model` the answer to `prompt` carries, asking again on a reply it can't be read
        from: at most `retries` times, with the parser's complaint alone, as the reply is in the
        history already. The last failure raises."""
        text = await self.ask(append_format_guide(prompt, model, tag=tag))
        block = tag or default_tag(model)
        while True:
            try:
                return parse_guided(model, text, tag=tag)
            except (GuideError, ValidationError) as e:
                if retries <= 0:
                    raise
                retries -= 1
                log.warning(f"<{block}> reply unusable, reasking ({retries} left): {e}")
                text = await self.ask(
                    f"Your previous message did not carry a usable `<{block}>` block.\n"
                    f"The parser reported:\n{e}\n\n"
                    f"Reply with the `<{block}>` block only -- same shape as the "
                    "instructions, no analysis, no commentary."
                )


class Backend(ABC):
    """Where model calls run: a model on its API through pydantic-ai, or an agent CLI on a
    subscription. A failed call raises; nothing falls back to another backend."""

    @abstractmethod
    def session(self, system: str) -> AbstractAsyncContextManager[Session]:
        """Turns under `system` that share one history while the context lasts."""

    async def ask(self, system: str, prompt: str) -> str:
        """The answer to a single prompt."""
        async with self.session(system) as session:
            return await session.ask(prompt)
