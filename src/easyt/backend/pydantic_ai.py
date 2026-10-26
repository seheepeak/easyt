"""The backend that runs a pydantic-ai `provider:model` name on that provider's paid API."""

import contextlib
from collections.abc import AsyncGenerator, AsyncIterable

import pydantic_ai
from pydantic_ai import Agent, AgentStreamEvent, RunContext
from pydantic_ai.conversation import Conversation
from pydantic_ai.exceptions import UnexpectedModelBehavior
from pydantic_ai.models import Model, infer_model
from pydantic_ai.settings import ModelSettings

from easyt import log
from easyt.backend.base import Backend, Session
from easyt.backend.errors import BackendError
from easyt.utils.spans import add

# easyt prints its own console, so pydantic-ai's first-run banner stays out of it.
pydantic_ai.BANNER_ENABLED = False


class PydanticAIBackend(Backend):
    """A model, such as anthropic:claude-opus-5-5, on its API at high effort and sent the whole
    history on every turn. No answer is capped below the model's own output limit."""

    def __init__(self, model: Model | str):
        self.model = infer_model(model)

    @contextlib.asynccontextmanager
    async def session(self, system: str) -> AsyncGenerator[Session]:
        # With `None` allowed, an answer without text is taken as it is instead of asked again.
        agent = Agent(
            self.model,
            instructions=system,
            output_type=str | None,
            model_settings=ModelSettings(thinking="high"),
        )
        yield PydanticAISession(agent)


class PydanticAISession(Session):
    def __init__(self, agent: Agent[None, str | None]):
        self._agent = agent
        self._conversation: Conversation | None = None

    async def ask(self, prompt: str) -> str:
        try:
            # A run with a handler streams: a plain request gets nothing back until the answer
            # is whole, and the OpenAI client gives up after 10 minutes of that.
            result = await self._agent.run(
                prompt, conversation=self._conversation, event_stream_handler=_ignore
            )
        except UnexpectedModelBehavior as e:  # such as thinking alone using up the output
            raise BackendError(f"The model gave no usable answer: {e}") from e
        if result.response.finish_reason == "length":
            log.warning("The model stopped at a token limit, so the answer stays cut")
        add("calls")
        # Kept only once answered, so a failed turn leaves no question hanging in the history.
        self._conversation = result.conversation
        return result.output or ""


async def _ignore(ctx: RunContext, events: AsyncIterable[AgentStreamEvent]) -> None:
    """An event stream handler that reads nothing: pydantic-ai drains the stream itself."""
