"""Work handed to a thread stops when the task waiting for it is cancelled."""

import asyncio
import threading
import time
from contextvars import ContextVar

import pytest

from easyt.utils.threads import Stopped, checkpoint, in_thread

_seen: ContextVar[str] = ContextVar("seen", default="nothing")


async def test_the_task_gets_what_the_function_returns_or_raises():
    assert await in_thread(lambda a, b: a + b, 1, b=2) == 3
    with pytest.raises(ZeroDivisionError):
        await in_thread(lambda: 1 / 0)


async def test_the_work_sees_the_context_of_the_task():
    _seen.set("the job")
    assert await in_thread(_seen.get) == "the job"


async def test_the_process_does_not_wait_for_the_thread_to_end():
    assert await in_thread(lambda: threading.current_thread().daemon)


async def test_a_cancelled_task_stops_its_thread_at_the_next_checkpoint():
    started, stopped = threading.Event(), threading.Event()

    def work():
        started.set()
        try:
            while True:
                checkpoint()
                time.sleep(0.01)
        except Stopped:
            stopped.set()
            raise

    task = asyncio.create_task(in_thread(work))
    assert await asyncio.to_thread(started.wait, 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await asyncio.to_thread(stopped.wait, 5)


def test_a_checkpoint_outside_in_thread_lets_the_work_go_on():
    checkpoint()
