"""How spans nest and end, and the lines of the tree the log shows them as."""

import asyncio
import io
import logging
import os
import sys
from types import SimpleNamespace

import pytest

from easyt import log
from easyt.utils import spans
from easyt.utils.spans import SpanFormatter, add, current_span, span, stderr_is_journal
from easyt.utils.threads import in_thread


@pytest.fixture
def lines():
    """The lines the log shows meanwhile, as on a stderr that is no terminal."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(SpanFormatter())
    log.addHandler(handler)
    yield lambda: stream.getvalue().splitlines()
    log.removeHandler(handler)


def test_spans_nest_into_an_indented_tree(lines):
    with span("Video", "a16z · en → ko"):
        log.info("Transcript: en-orig (auto-generated)")
        with span("Translation") as s:
            log.warning("segment 5 put 4 lines on one screen")
            s.result = "470 sentences"
    assert lines() == [
        "▶ Video · a16z · en → ko",
        "  · Transcript: en-orig (auto-generated)",
        "  ▶ Translation",
        "    ⚠ segment 5 put 4 lines on one screen",
        "  ✓ Translation · 470 sentences · 0s",
        "✓ Video · 0s",
    ]


def test_the_later_lines_of_a_record_line_up_under_its_first(lines):
    with span("Translation"):
        log.warning("<output> reply unusable, reasking (2 left):\n1 validation error")
    assert lines()[1:3] == [
        "  ⚠ <output> reply unusable, reasking (2 left):",
        "    1 validation error",
    ]


def test_a_span_that_raises_ends_as_failed_or_as_cancelled(lines):
    with pytest.raises(RuntimeError), span("Upload"):
        raise RuntimeError("Drive is down")
    with pytest.raises(asyncio.CancelledError), span("Translation"):
        raise asyncio.CancelledError
    assert lines()[1::2] == ["✗ Upload · 0s", "✗ Translation · cancelled · 0s"]


def test_progress_shows_once_a_heartbeat_has_passed(lines, monkeypatch):
    with span("Line breaking", total=47) as s:
        s.advance(10)  # too soon after the start
        monkeypatch.setattr(spans, "HEARTBEAT_SECONDS", 0)
        s.advance(4)
    assert lines() == ["▶ Line breaking", "  · 14/47", "✓ Line breaking · 0s"]


def test_counts_add_up_into_the_parent():
    with span("Video") as video:
        with span("Punctuation"):
            add("calls")
            add("cost", 0.25)
        with span("Translation"):
            add("calls")
            add("cost", 0.5)
    assert video.outcome() == ["2 calls", "$0.75", "0s"]


async def test_tasks_and_threads_started_in_a_span_are_under_it():
    async def in_task():
        return current_span()

    with span("Stage") as stage:
        assert await asyncio.create_task(in_task()) is stage
        assert await in_thread(current_span) is stage
    assert current_span() is None


def test_each_journal_line_starts_with_its_syslog_level():
    journal = SpanFormatter(journal=True)
    warning = logging.LogRecord("easyt", logging.WARNING, __file__, 1, "Disk full", None, None)
    assert journal.format(warning) == "<4>⚠ Disk full"
    try:
        raise RuntimeError("Drive is down")
    except RuntimeError:
        error = logging.LogRecord(
            "easyt", logging.ERROR, __file__, 1, "Failed", None, sys.exc_info()
        )
    assert all(line.startswith("<3>") for line in journal.format(error).splitlines())


def test_stderr_is_the_journal_only_when_journal_stream_names_it(monkeypatch, tmp_path):
    stderr = (tmp_path / "stderr").open("w")
    monkeypatch.setattr(spans, "sys", SimpleNamespace(stderr=stderr))
    st = os.fstat(stderr.fileno())
    monkeypatch.setenv("JOURNAL_STREAM", f"{st.st_dev}:{st.st_ino}")
    assert stderr_is_journal()
    # A shell started under a service keeps the service's JOURNAL_STREAM.
    monkeypatch.setenv("JOURNAL_STREAM", f"{st.st_dev}:{st.st_ino + 1}")
    assert not stderr_is_journal()
    stderr.close()
