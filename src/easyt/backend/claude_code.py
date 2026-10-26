"""Claude calls through the Claude Code CLI, billed to the Claude subscription instead of the API."""

import contextlib
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ClaudeSDKError,
    EffortLevel,
    RateLimitEvent,
    RateLimitInfo,
    ResultMessage,
    SystemMessage,
    TextBlock,
)

from easyt import log
from easyt.backend.base import Backend, Session
from easyt.backend.errors import BackendError, UsageLimitReached
from easyt.utils.spans import add

# The CLI loads auto memory for its cwd, so it runs where no project keeps any.
CLI_CWD = Path.home() / ".cache" / "easyt" / "claude-code"


class ClaudeCodeBackend(Backend):
    """Claude Code as a plain model without tools, on `model` at `effort`."""

    def __init__(self, *, model: str, effort: EffortLevel):
        self.model = model
        self.effort = effort

    @contextlib.asynccontextmanager
    async def session(self, system: str) -> AsyncGenerator[Session]:
        """One CLI process, whose turns share its history."""
        CLI_CWD.mkdir(parents=True, exist_ok=True)
        options = ClaudeAgentOptions(
            system_prompt=system,
            model=self.model,
            effort=self.effort,
            # What the API gives Opus 5.5 when `thinking` is left out; it rejects disabled thinking.
            thinking={"type": "adaptive"},
            tools=[],
            setting_sources=[],
            skills=[],
            strict_mcp_config=True,
            permission_mode="dontAsk",
            # Otherwise an @path in the prompt pulls that file in and a leading /word runs as a
            # command.
            verbatim_prompts=True,
            extra_args={"no-session-persistence": None},
            env={
                # The CLI prefers a key to the subscription, and .env has put one in os.environ.
                "ANTHROPIC_API_KEY": "",
                "ANTHROPIC_AUTH_TOKEN": "",
                # Update, telemetry and feature-flag traffic would add over a second to every run.
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                # A turn the CLI cuts off fails rather than continues, so allow Opus 5.5's most.
                "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "128000",
            },
            cwd=CLI_CWD,
            stderr=lambda line: log.debug(f"claude stderr: {line.rstrip()}"),
        )
        client = ClaudeSDKClient(options)
        try:
            await client.connect()
        except ClaudeSDKError as e:  # the CLI is missing or died before it was ready
            raise BackendError(f"Claude Code failed to start: {e}") from e
        try:
            yield ClaudeCodeSession(client)
        finally:
            await client.disconnect()


class ClaudeCodeSession(Session):
    def __init__(self, client: ClaudeSDKClient):
        self._client = client
        self._cost = 0.0  # each result reports what the whole session has cost so far

    async def ask(self, prompt: str) -> str:
        texts: list[str] = []
        errors: set[str] = set()
        limit: RateLimitInfo | None = None
        result: ResultMessage | None = None
        try:
            await self._client.query(prompt)
            async with contextlib.aclosing(self._client.receive_response()) as messages:
                async for message in messages:
                    if isinstance(message, AssistantMessage):
                        if message.error:
                            errors.add(message.error)
                        else:
                            texts += [b.text for b in message.content if isinstance(b, TextBlock)]
                    elif isinstance(message, SystemMessage) and message.subtype == "init":
                        if (source := message.data.get("apiKeySource", "none")) != "none":
                            raise BackendError(
                                f"Claude Code would bill an API key ({source}), not the subscription"
                            )
                    elif isinstance(message, RateLimitEvent):
                        if message.rate_limit_info.status == "rejected":
                            limit = message.rate_limit_info
                    elif isinstance(message, ResultMessage):
                        result = message
        except ClaudeSDKError as e:  # the CLI died or broke its stream before reporting a result
            raise BackendError(f"Claude Code failed: {e}") from e

        if result is not None and not result.is_error:
            cost = result.total_cost_usd or 0
            log.debug(
                f"Claude Code turn took {result.duration_ms / 1000:.1f}s; "
                f"the session has cost ${cost:.4f} at API prices"
            )
            add("calls")
            add("cost", cost - self._cost)
            self._cost = cost
            return "".join(texts)

        status = result.api_error_status if result else None
        if limit is not None or "rate_limit" in errors or status == 429:
            resets = ""
            if limit and limit.resets_at:
                resets_at = datetime.fromtimestamp(limit.resets_at, UTC).astimezone()
                resets = f", resets {resets_at:%m-%d %H:%M}"
            raise UsageLimitReached(f"Claude subscription usage limit reached{resets}")
        raise BackendError(f"Claude Code run failed: {result.result if result else 'no result'}")
