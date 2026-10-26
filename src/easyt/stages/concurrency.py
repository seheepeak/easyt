"""Model calls a stage runs at once."""

import asyncio
import os
from collections.abc import Awaitable, Callable, Sequence

from easyt import log
from easyt.utils.spans import current_span


def llm_concurrency() -> int:
    """How many model calls a stage runs at once: LLM_CONCURRENCY, or 4."""
    value = int(os.getenv("LLM_CONCURRENCY") or 4)
    if value < 1:
        raise ValueError(f"LLM_CONCURRENCY must be 1 or more, not {value}")
    return value


async def run_concurrently[T](jobs: Sequence[Callable[[], Awaitable[T]]]) -> list[T]:
    """Each job's result, in the jobs' order, with at most llm_concurrency() of them running at a
    time and the current span counting them done. After a job fails no other one starts, and the
    first error is raised once the running ones finish, so what they cost is kept."""
    at_once = llm_concurrency()
    limit = asyncio.Semaphore(at_once)
    errors: list[BaseException] = []
    stage = current_span()
    if stage is not None:
        stage.total = len(jobs)
    log.info(f"{len(jobs)} calls, {at_once} at a time")

    async def run(job: Callable[[], Awaitable[T]]) -> T | None:
        async with limit:
            if errors:
                return None
            try:
                result = await job()
            except BaseException as e:  # a call can cancel itself, and gather would return that
                errors.append(e)
                raise
            if stage is not None:
                stage.advance()
            return result

    results = await asyncio.gather(*(run(job) for job in jobs), return_exceptions=True)
    if errors:
        raise errors[0]
    return results
