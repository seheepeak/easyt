"""How a stage runs its model calls at once."""

import asyncio

import pytest

from easyt.stages.concurrency import run_concurrently
from easyt.utils.spans import span


async def test_results_come_in_the_jobs_order_with_at_most_the_limit_running(monkeypatch):
    monkeypatch.setenv("LLM_CONCURRENCY", "2")
    running, most = 0, 0

    async def job(n):
        nonlocal running, most
        running += 1
        most = max(most, running)
        await asyncio.sleep(0.01 * (5 - n))  # the later jobs finish first
        running -= 1
        return n

    assert await run_concurrently([lambda n=n: job(n) for n in range(5)]) == [0, 1, 2, 3, 4]
    assert most == 2


async def test_the_span_of_the_stage_counts_the_calls_done():
    async def one():
        return 1

    with span("Translation") as stage:
        await run_concurrently([one, one, one])
    assert (stage.done, stage.total) == (3, 3)


async def test_a_failure_lets_the_running_jobs_finish_but_starts_no_other(monkeypatch):
    monkeypatch.setenv("LLM_CONCURRENCY", "2")
    started, finished = [], []

    async def job(n):
        started.append(n)
        if n == 1:
            raise RuntimeError("usage limit reached")
        await asyncio.sleep(0.02)
        finished.append(n)

    with pytest.raises(RuntimeError, match="usage limit"):
        await run_concurrently([lambda n=n: job(n) for n in range(4)])
    assert started == [0, 1] and finished == [0]


async def test_a_job_that_cancels_itself_is_raised_not_returned():
    async def one():
        return 1

    async def cancels_itself():
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await run_concurrently([one, cancels_itself, one])


@pytest.mark.parametrize("value", ["0", "-1"])
async def test_a_concurrency_below_one_is_refused(monkeypatch, value):
    monkeypatch.setenv("LLM_CONCURRENCY", value)
    with pytest.raises(ValueError, match="LLM_CONCURRENCY must be 1 or more"):
        await run_concurrently([])
