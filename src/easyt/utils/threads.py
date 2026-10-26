"""Blocking work run off the event loop, which stops when the task waiting for it is cancelled."""

import asyncio
import contextlib
import threading
from collections.abc import Callable
from contextvars import ContextVar, copy_context

_stop: ContextVar[threading.Event | None] = ContextVar("stop", default=None)


class Stopped(Exception):
    """Raised by `checkpoint()` in work whose task was cancelled."""


def checkpoint() -> None:
    """Raise `Stopped` if the task that handed this work to `in_thread` was cancelled."""
    if (stop := _stop.get()) is not None and stop.is_set():
        raise Stopped


async def in_thread[**P, T](func: Callable[P, T], /, *args: P.args, **kwargs: P.kwargs) -> T:
    """`func` in a thread of its own, with the caller's context. A thread cannot be cancelled:
    a cancel stops `func` at its next `checkpoint()`, and as the thread is a daemon, the process
    does not wait for work that has no checkpoint, as it would after asyncio.to_thread."""
    loop = asyncio.get_running_loop()
    done = loop.create_future()
    stop = threading.Event()
    context = copy_context()
    context.run(_stop.set, stop)

    def work() -> None:
        result, error = None, None
        try:
            result = context.run(func, *args, **kwargs)
        except BaseException as e:
            error = e
        # The loop is closed if the process ended without waiting for this thread.
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(_settle, done, result, error)

    threading.Thread(target=work, daemon=True).start()
    try:
        return await done
    except asyncio.CancelledError:
        stop.set()
        raise


def _settle(done: asyncio.Future, result: object, error: BaseException | None) -> None:
    if done.cancelled():
        return
    if error is None:
        done.set_result(result)
    else:
        done.set_exception(error)
