"""Spans: pieces of work with a start and an end, nested into a tree, and the log lines that
show them.

A span is open while its `with span(...)` block runs. It is the current span of the code in the
block, and of the tasks and threads that code hands work to, as they copy the context. Records
logged meanwhile belong to it and are indented under it. Its start, its end and, now and then,
its progress are logged too, as records every handler gets.
"""

import asyncio
import logging
import os
import sys
import time
from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

from easyt.utils.threads import Stopped

# How often, at most, the progress of a span is logged while it runs.
HEARTBEAT_SECONDS = 30
# Between INFO and WARNING, for successes and spans that ended well: a quiet run keeps them.
SUCCESS = logging.INFO + 5
logging.addLevelName(SUCCESS, "SUCCESS")

_log = logging.getLogger(__name__)
_current: ContextVar["Span | None"] = ContextVar("span", default=None)


@dataclass(eq=False)
class Span:
    """A piece of work: what it is, how far it got, and how it ended."""

    name: str
    parent: "Span | None" = None
    status: str = "running"  # then "ok", "error" or "cancelled"
    result: str = ""  # what its end line says after the name
    done: float = 0
    total: float | None = None
    percent: bool = False
    counts: dict[str, float] = field(default_factory=dict)
    children: list["Span"] = field(default_factory=list)
    start: float = field(default_factory=time.monotonic)
    end: float | None = None

    def __post_init__(self) -> None:
        if self.percent:
            self.total = 100
        self._reported = self.start

    @property
    def depth(self) -> int:
        return 0 if self.parent is None else self.parent.depth + 1

    @property
    def seconds(self) -> float:
        return (self.end or time.monotonic()) - self.start

    def advance(self, amount: float = 1) -> None:
        self.update(self.done + amount)

    def update(self, done: float) -> None:
        """Set how far it got, which is logged once HEARTBEAT_SECONDS have passed since the start
        or the last time."""
        self.done = done if self.total is None else min(done, self.total)
        now = time.monotonic()
        if self.end is None and now - self._reported >= HEARTBEAT_SECONDS:
            self._reported = now
            _log.info(self.progress(), extra={"span": self, "span_event": "progress"})

    def progress(self) -> str:
        """How far it got: `14/47`, or `64%` in percent."""
        if self.percent:
            return f"{self.done:.0f}%"
        return f"{self.done:g}" if self.total is None else f"{self.done:g}/{self.total:g}"

    def outcome(self, counts: bool = True) -> list[str]:
        """What its end line says after the name: how it ended, its result, its counts unless
        left out, and its time last."""
        shown = [f"${v:.2f}" if k == "cost" else f"{v:g} {k}" for k, v in self.counts.items()]
        cancelled = ["cancelled"] if self.status == "cancelled" else []
        return [
            *cancelled,
            *filter(None, [self.result]),
            *(shown if counts else []),
            duration(self.seconds),
        ]


@contextmanager
def span(
    name: str, detail: str = "", *, total: float | None = None, percent: bool = False
) -> Generator[Span]:
    """A span under the current one, open for the block, counting to `total`, or to 100 in
    `percent`. Its start, with `detail`, and its end are logged; a block that raises ends it as
    failed, or as cancelled when its job was."""
    parent = _current.get()
    s = Span(name, parent, total=total, percent=percent)
    if parent is not None:
        parent.children.append(s)
    token = _current.set(s)
    _log.info(" · ".join(filter(None, [name, detail])), extra={"span": s, "span_event": "start"})
    try:
        yield s
        s.status = "ok"
    except (asyncio.CancelledError, Stopped):
        s.status = "cancelled"
        raise
    except BaseException:
        s.status = "error"
        raise
    finally:
        s.end = time.monotonic()
        _current.reset(token)
        if parent is not None:
            for key, value in s.counts.items():
                parent.counts[key] = parent.counts.get(key, 0) + value
        level = {"ok": SUCCESS, "cancelled": logging.WARNING}.get(s.status, logging.ERROR)
        _log.log(level, " · ".join([name, *s.outcome()]), extra={"span": s, "span_event": "end"})


def current_span() -> Span | None:
    """The innermost span open here, if any."""
    return _current.get()


def add(key: str, amount: float = 1) -> None:
    """Add `amount` to a count of the current span, such as `calls` or `cost` in US dollars. A
    span adds its counts to its parent's when it ends."""
    if (s := _current.get()) is not None:
        s.counts[key] = s.counts.get(key, 0) + amount


def duration(seconds: float) -> str:
    """`seconds` as `49s`, `3m 21s` or `1h 05m`."""
    seconds = round(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60:02d}s"
    return f"{seconds // 3600}h {seconds % 3600 // 60:02d}m"


ICONS = {"start": "▶", "ok": "✓", "error": "✗", "cancelled": "✗"}
_COLORS = {"▶": "36", "✓": "32", "✗": "31", "⚠": "33"}  # ANSI: cyan, green, red, yellow


class SpanFormatter(logging.Formatter):
    """A record as a line of the span tree: indented under the spans it was logged in, after an
    icon for its kind. On a terminal the icons are colored; for the systemd journal each line
    starts with its syslog level, such as <4> for a warning, which journalctl colors and filters.
    """

    def __init__(self, *, color: bool = False, journal: bool = False) -> None:
        super().__init__()
        self.color = color
        self.journal = journal

    def format(self, record: logging.LogRecord) -> str:
        s, event = getattr(record, "span", None), getattr(record, "span_event", None)
        if event in ("start", "end"):
            depth, icon = s.depth, ICONS["start" if event == "start" else s.status]
        else:
            under = s if event == "progress" else _current.get()
            depth = 0 if under is None else under.depth + 1
            icon = _level_icon(record.levelno)
        if self.color and icon in _COLORS:
            icon = f"\033[{_COLORS[icon]}m{icon}\033[0m"
        text = super().format(record)
        # Later lines, such as a traceback's, line up under the first one's text.
        indent = "  " * depth
        text = f"{indent}{icon} " + text.replace("\n", f"\n{indent}  ")
        if self.color and record.levelno < logging.INFO:
            text = f"\033[2m{text}\033[0m"
        if self.journal:
            text = "\n".join(
                f"<{_syslog_level(record.levelno)}>{line}" for line in text.split("\n")
            )
        return text


def _level_icon(levelno: int) -> str:
    if levelno >= logging.ERROR:
        return "✗"
    if levelno >= logging.WARNING:
        return "⚠"
    return "✓" if levelno >= SUCCESS else "·"


def _syslog_level(levelno: int) -> int:
    """3 for an error, 4 a warning, 5 a notice such as a success, 6 information, 7 debugging."""
    for floor, level in (
        (logging.CRITICAL, 2),
        (logging.ERROR, 3),
        (logging.WARNING, 4),
        (SUCCESS, 5),
        (logging.INFO, 6),
    ):
        if levelno >= floor:
            return level
    return 7


def stderr_is_journal() -> bool:
    """Whether stderr is the systemd journal. systemd names the stream it gives a service in
    JOURNAL_STREAM, as device:inode; a shell started under a service may inherit the name."""
    try:
        st = os.fstat(sys.stderr.fileno())
    except (AttributeError, OSError, ValueError):
        return False
    return os.environ.get("JOURNAL_STREAM") == f"{st.st_dev}:{st.st_ino}"
