"""Where model calls run: LLM_BACKEND names an agent CLI (claude-code, codex) or a pydantic-ai
`provider:model`.

The agent CLIs are agent apps, but run here as plain models: no tools, just prompts and their
answers, billed to the user's Claude or ChatGPT subscription instead of an API key.
"""

import os

from easyt import log
from easyt.backend.base import Backend, Session
from easyt.backend.errors import BackendError, UsageLimitReached

__all__ = ["Backend", "BackendError", "Session", "UsageLimitReached", "open_backend"]


def open_backend(stage: str) -> Backend:
    """The backend LLM_BACKEND names, for `stage`."""
    backend = os.getenv("LLM_BACKEND") or "claude-code"
    log.info(f"{stage} backend: {backend}")
    # Each option's SDK loads only when it is picked, as every one of them is slow to import.
    if backend == "claude-code":
        from easyt.backend.claude_code import ClaudeCodeBackend

        return ClaudeCodeBackend(model="opus", effort="high")
    if backend == "codex":
        from easyt.backend.codex import CodexBackend

        # Codex has no alias for its latest model; ~/.codex/models_cache.json lists the names.
        return CodexBackend(model="gpt-6.1-sol", effort="high")
    from easyt.backend.pydantic_ai import PydanticAIBackend

    return PydanticAIBackend(backend)
