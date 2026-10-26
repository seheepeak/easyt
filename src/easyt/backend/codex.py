"""Model calls through the Codex CLI, billed to the ChatGPT subscription instead of the API."""

import contextlib
from collections.abc import AsyncGenerator
from pathlib import Path

from openai_codex import ApprovalMode, AsyncCodex, AsyncThread, CodexConfig, CodexError, Sandbox
from openai_codex.types import Personality, ReasoningEffort, TurnStatus

from easyt import log
from easyt.backend.base import Backend, Session
from easyt.backend.errors import BackendError, UsageLimitReached
from easyt.utils.spans import add

# Codex reads AGENTS.md and project config from its cwd, so it runs where no project has any.
CLI_CWD = Path.home() / ".cache" / "easyt" / "codex"

# Leave out the user's own MCP servers, the injected context, and every tool that can run something.
CONFIG_OVERRIDES = (
    "mcp_servers={}",
    "include_environment_context=false",
    "project_doc_max_bytes=0",
    "include_apps_instructions=false",
    "skills.include_instructions=false",
    "agents.enabled=false",
    'web_search="disabled"',
    *(
        f"features.{name}=false"
        for name in (
            "shell_tool",
            "unified_exec",
            "code_mode_host",
            "view_image",
            "sleep_tool",
            "tool_suggest",
            "skill_search",
            "apps",
            "browser_use",
            "computer_use",
            "plugins",
            "hooks",
            "image_generation",
            "goals",
            "multi_agent",
            "memories",
            # Otherwise an outage stalls the turn for good instead of failing it.
            "unbounded_connection_retries",
        )
    ),
)


class CodexBackend(Backend):
    """Codex as a plain model without tools, on `model` at `effort`."""

    def __init__(self, *, model: str, effort: ReasoningEffort | str):
        self.model = model
        self.effort = effort

    @contextlib.asynccontextmanager
    async def session(self, system: str) -> AsyncGenerator[Session]:
        """One Codex thread, whose turns share its history."""
        CLI_CWD.mkdir(parents=True, exist_ok=True)
        config = CodexConfig(
            cwd=str(CLI_CWD),
            # Codex would bill a key in its environment, and .env has put one in os.environ.
            env={"OPENAI_API_KEY": "", "CODEX_API_KEY": ""},
            config_overrides=CONFIG_OVERRIDES,
        )
        try:
            async with AsyncCodex(config) as codex:
                account = (await codex.account()).account
                if account is None or account.root.type != "chatgpt":
                    kind = account.root.type if account else "no login"
                    raise BackendError(f"Codex would bill {kind}, not the ChatGPT subscription")
                thread = await codex.thread_start(
                    # The server never asks, so the SDK's accept-everything approval default
                    # never runs.
                    approval_mode=ApprovalMode.deny_all,
                    base_instructions=system,
                    cwd=str(CLI_CWD),
                    ephemeral=True,
                    model=self.model,
                    # A provider named in the user's config would bill apart from the ChatGPT
                    # login.
                    model_provider="openai",
                    personality=Personality.none,
                    sandbox=Sandbox.read_only,
                )
                yield CodexSession(thread, self.effort)
        except CodexError as e:  # the app-server refused a request or died
            raise BackendError(f"Codex failed: {e}") from e


class CodexSession(Session):
    def __init__(self, thread: AsyncThread, effort: ReasoningEffort | str):
        self._thread = thread
        self._effort = effort

    async def ask(self, prompt: str) -> str:
        final: str | None = None
        unphased: str | None = None
        turn = None
        try:
            handle = await self._thread.turn(prompt, effort=self._effort)
            async with contextlib.aclosing(handle.stream()) as events:
                async for event in events:
                    if event.method == "item/completed":
                        item = event.payload.item.root
                        if item.type == "agentMessage":
                            phase = getattr(item.phase, "value", None)
                            if phase == "final_answer":
                                final = item.text
                            elif phase is None:
                                unphased = item.text
                    elif event.method == "turn/completed":
                        turn = event.payload.turn
        except CodexError as e:  # the app-server refused a request or died mid-turn
            raise BackendError(f"Codex failed: {e}") from e

        if turn is None:
            raise BackendError("Codex turn ended without completing")
        if turn.status == TurnStatus.completed:
            log.debug(f"Codex turn took {(turn.duration_ms or 0) / 1000:.1f}s")
            add("calls")
            # As the SDK's own final_response: the last final answer, else the last unphased one.
            return final if final is not None else unphased or ""

        error = turn.error
        failure = f"Codex turn {turn.status.value}: {error.message if error else 'no error'}"
        kind = error.codex_error_info if error else None
        # As sent on the wire: a bare kind such as this one, or a kind keyed by its name.
        if kind and kind.model_dump(mode="json", by_alias=True) == "usageLimitExceeded":
            raise UsageLimitReached(f"ChatGPT subscription usage limit reached ({failure})")
        raise BackendError(failure)
