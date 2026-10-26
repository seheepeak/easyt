"""How the bot runs the jobs of the chat, and what their messages say."""

import asyncio
import contextlib
from collections import deque

import pytest
from telegram.ext import CommandHandler

from easyt import log
from easyt.bot import JobLog, Runner, _closing, _fit, _tree, build_app, find_urls
from easyt.utils.spans import Span, span

PLAYLIST = "https://www.youtube.com/playlist?list=PLx0sYbCqOb8TBPRdmBHs5Iftvv9TPboYG"


class FakeMessage:
    """A Telegram message: the request, or the job message the bot answers it with and edits."""

    def __init__(self, text=""):
        self.text = text
        self.replies = []

    async def reply_text(self, text, reply_markup=None):
        reply = type(self)(text)
        self.replies.append(reply)
        return reply

    async def edit_text(self, text, reply_markup=None):
        self.text = text


class SlowMessage(FakeMessage):
    """Edits that take a moment to be answered, recorded when sent and when answered."""

    def __init__(self, text=""):
        super().__init__(text)
        self.edits = []

    async def edit_text(self, text, reply_markup=None):
        self.edits.append(("sent", text))
        await asyncio.sleep(0.05)
        self.edits.append(("answered", text))
        self.text = text


class BrokenMessage(FakeMessage):
    async def edit_text(self, text, reply_markup=None):
        raise RuntimeError("not a Telegram error")


async def _until(condition):
    for _ in range(500):
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("timed out")


async def _forever():
    await asyncio.Event().wait()


@pytest.fixture
async def runner():
    handler = JobLog()
    log.addHandler(handler)
    runner = Runner()
    runner.worker = asyncio.create_task(runner.work())
    yield runner
    runner.worker.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await runner.worker
    log.removeHandler(handler)


async def test_jobs_run_one_at_a_time_in_the_order_they_came(runner):
    request, ran, gate = FakeMessage(), [], asyncio.Event()

    async def first():
        ran.append("first started")
        await gate.wait()
        ran.append("first finished")
        return "one"

    async def second():
        ran.append("second")
        return "two"

    await runner.add(request, "First", first)
    await runner.add(request, "Second", second)
    await _until(lambda: ran)
    assert request.replies[1].text == "Second\nWaiting"

    gate.set()
    await _until(lambda: request.replies[1].text.startswith("✅"))
    assert ran == ["first started", "first finished", "second"]
    assert [r.text for r in request.replies] == ["✅ First\none", "✅ Second\ntwo"]


async def test_the_message_shows_how_far_the_running_span_is_and_its_last_lines(runner):
    request, shown, gate = FakeMessage(), asyncio.Event(), asyncio.Event()

    async def translate():
        with span("Video title"):
            with span("Download"):
                pass
            with span("Translation", total=10) as s:
                log.info("10 calls, 4 at a time")
                s.advance(4)
                shown.set()
                await gate.wait()
                log.warning("Translation: segments [3] got no answer")
        return "Title"

    await runner.add(request, "Translate URL", translate)
    await shown.wait()
    assert runner.current.progress().splitlines() == [
        "Translate URL",
        "▸ Video title",
        "  ✓ Download · 0s",
        "  ▸ Translation · 4/10",
        "    10 calls, 4 at a time",
        "0s",
    ]

    gate.set()
    await _until(lambda: request.replies[0].text.startswith("✅"))
    assert request.replies[0].text.splitlines() == [
        "✅ Translate URL",
        "Title",
        "✓ Video title · 0s",
        "  ✓ Download · 0s",
        "  ✓ Translation · 0s",
        "0s",  # what they all came to: no model calls here
        "⚠️ Translation: segments [3] got no answer",
    ]


def test_a_finished_line_cuts_its_name_and_result_to_keep_its_time():
    job = Span("Job")
    title = "Amazon CEO Andy Jassy on the $220 Billion AI Buildout"
    video = Span(title, parent=job, status="ok", start=0, end=548)
    result = "15 chunks, 470 sentences, edit ratio ≥ 0.981"
    stage = Span("Punctuation", parent=video, status="ok", result=result, start=0, end=23)
    job.children.append(video)
    video.children.append(stage)
    assert _tree(job, {}) == [
        "✓ Amazon CEO Andy Jassy on… · 9m 08s",
        "  ✓ Punctuation · 15 chunks,… · 23s",
    ]


def test_a_span_in_percent_shows_how_far_it_is_as_a_percentage():
    job = Span("Job")
    download = Span("Download", parent=job, percent=True, done=43.4)
    job.children.append(download)
    assert download.progress() == "43%"
    assert _tree(job, {}) == ["▸ Download · 43%"]


def test_a_threads_span_still_running_after_a_cancel_shows_as_cancelled():
    job = Span("Job", status="cancelled")
    video = Span("Video", parent=job, status="cancelled")
    download = Span("Download", parent=video, percent=True, done=43)
    recent = {download: deque(["still downloading"])}
    job.children.append(video)
    video.children.append(download)
    assert _tree(job, recent) == ["✗ Video · cancelled · 0s", "  ✗ Download · cancelled · 0s"]


def test_a_playlist_leaves_its_finished_videos_to_its_count_but_shows_a_failed_one():
    with span("Job") as job, span("Playlist", total=3) as playlist:
        with span("First video"):
            pass
        with contextlib.suppress(RuntimeError), span("Second video"):
            raise RuntimeError("no captions")
        playlist.advance(2)
        with span("Third video"):
            assert _tree(job, {}) == [
                "▸ Playlist · 2/3",
                "  ✗ Second video · 0s",
                "  ▸ Third video",
            ]


async def test_the_progress_edit_in_flight_is_answered_before_the_final_one_is_sent(runner):
    request = SlowMessage()

    async def quick():
        await asyncio.sleep(0.01)  # while the first progress edit is in flight
        return "done"

    await runner.add(request, "Quick", quick)
    await _until(lambda: request.replies[0].text.startswith("✅"))
    edits = request.replies[0].edits
    final = edits.index(("sent", "✅ Quick\ndone"))
    sent_before = [text for kind, text in edits[:final] if kind == "sent"]
    assert sent_before
    assert all(("answered", text) in edits[:final] for text in sent_before)


async def test_a_cancelled_job_stops_and_the_next_one_runs(runner):
    request = FakeMessage()

    async def after():
        return "done"

    await runner.add(request, "Long", _forever)
    await runner.add(request, "Next", after)
    await _until(lambda: runner.current and runner.current.label == "Long")
    await runner.cancel(runner.current)

    await _until(lambda: request.replies[1].text.startswith("✅"))
    assert request.replies[0].text == "🛑 Cancelled\nLong"


async def test_a_waiting_job_that_is_cancelled_never_runs(runner):
    request, ran = FakeMessage(), []

    async def never():
        ran.append("never")

    await runner.add(request, "Long", _forever)
    await runner.add(request, "Never", never)
    await _until(lambda: runner.current is not None)
    await runner.cancel(runner.pending[0])

    assert request.replies[1].text == "🛑 Cancelled\nNever"
    assert runner.pending == [] and ran == []


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (RuntimeError("Google Drive has no sign-in"), "RuntimeError: Google Drive has no sign-in"),
        (AssertionError(), "AssertionError"),
    ],
)
async def test_a_failed_job_says_why(runner, error, reason):
    request = FakeMessage()

    async def fail():
        raise error

    await runner.add(request, "Translate URL", fail)
    await _until(lambda: request.replies[0].text.startswith("💥"))
    assert request.replies[0].text == f"💥 Translate URL\n{reason}"


async def test_the_runner_goes_on_after_an_error_of_its_own(runner):
    async def done():
        return "done"

    await runner.add(FakeMessage(), "Lost", done)
    runner.pending[0].status = BrokenMessage()
    request = FakeMessage()
    await runner.add(request, "Next", done)
    await _until(lambda: request.replies[0].text.startswith("✅"))


async def test_the_job_running_when_the_bot_stops_is_dropped(runner):
    request = FakeMessage()
    await runner.add(request, "Long", _forever)
    await _until(lambda: runner.current is not None)

    runner.worker.cancel()
    with pytest.raises(asyncio.CancelledError):
        await runner.worker
    assert request.replies[0].text == "🛑 Dropped as the bot stopped\nLong"


def test_the_bot_answers_the_commands_of_plugins(echo_plugin):
    app = build_app("123:abc", 1)
    answered = {c for h in app.handlers[0] if isinstance(h, CommandHandler) for c in h.commands}
    assert answered >= {"aterm", "transcript", "echo", "status", "cancel"}


def test_the_bot_shows_no_link_previews():
    assert build_app("123:abc", 1).bot.defaults.link_preview_options.is_disabled


def test_the_finished_message_keeps_the_last_ten_warnings():
    text = _closing("✅ Job", [f"warning {i}" for i in range(12)])
    assert text.splitlines()[:3] == [
        "✅ Job",
        "⚠️ 2 earlier warnings are in `easyt log`",
        "⚠️ warning 2",
    ]


def test_a_message_is_cut_to_telegrams_limit_in_utf16_units():
    assert _fit("🛑" * 3000) == "🛑" * 2048
    assert _fit("é" * 4096) == "é" * 4096


@pytest.mark.parametrize(
    ("text", "urls"),
    [
        ("https://youtu.be/Qmafkf_08uQ", ["https://youtu.be/Qmafkf_08uQ"]),
        ("Translate this: https://youtu.be/Qmafkf_08uQ.", ["https://youtu.be/Qmafkf_08uQ"]),
        ("(https://youtu.be/Qmafkf_08uQ?si=AbCd)", ["https://youtu.be/Qmafkf_08uQ?si=AbCd"]),
        ("https://youtu.be/Qmafkf_08uQ—translate it", ["https://youtu.be/Qmafkf_08uQ"]),
        (f"{PLAYLIST}[0:5]", [f"{PLAYLIST}[0:5]"]),
        ("no link here", []),
    ],
)
def test_urls_are_found_without_the_sentence_around_them(text, urls):
    assert find_urls(text) == urls
