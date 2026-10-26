"""The Telegram bot: runs what the chat in TELEGRAM_CHAT_ID asks, one job at a time."""

import asyncio
import contextlib
import itertools
import logging
import os
import re
from collections import deque
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass, field, replace

from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    Message,
    Update,
)
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    Defaults,
    MessageHandler,
    filters,
)

from easyt import log, plugins
from easyt.jobs import Mode, run_youtube
from easyt.utils.spans import ICONS, Span, current_span, duration, span
from easyt.youtube import parse_youtube_url

DRIVE_FOLDER = "https://drive.google.com/drive/folders/"
# ASCII characters only, so words written right after a URL stay out of it.
URL = re.compile(r"https?://[\w\-.~:/?#\[\]@!$&'()*+,;=%]+", re.ASCII)
COMMANDS = {
    "aterm": "Advanced terms of an English video, as HTML: /aterm URL",
    "transcript": "The punctuated transcript of a video, as a txt file: /transcript URL",
    "status": "The running job and the ones waiting",
    "cancel": "Cancel the running job",
}
# Characters a line of a job's message keeps, as a phone shows about 36; a finished span's name
# keeps at most NAME_WIDTH of them, leaving room for its result.
LINE_WIDTH = 36
NAME_WIDTH = 28


def find_urls(text: str) -> list[str]:
    """The URLs in `text`, without the punctuation of a sentence around them."""
    return [url.rstrip(".,;:!?)'") for url in URL.findall(text)]


@dataclass(eq=False)
class Job:
    """A request of the chat, and the message that shows how far it is."""

    id: int
    label: str
    run: Callable[[], Awaitable[str]]  # what the finished message says
    status: Message
    warnings: list[str] = field(default_factory=list)
    root: Span | None = None  # the span the job runs in, once it started
    recent: dict[Span, deque[str]] = field(default_factory=dict)  # the last lines of each span
    task: asyncio.Task | None = None

    def progress(self) -> str:
        if self.root is None:
            return f"{self.label}\nStarted"
        tree = _tree(self.root, self.recent)
        return "\n".join([self.label, *tree, duration(self.root.seconds)])

    def summary(self) -> list[str]:
        """The spans of the finished job, then what they all came to."""
        if self.root is None or not self.root.children:
            return []
        return [*_tree(self.root, self.recent), " · ".join(self.root.outcome())]


# The job whose task a record is logged in, for `JobLog` to show it on that job's message.
_job: ContextVar[Job | None] = ContextVar("job", default=None)


class JobLog(logging.Handler):
    """Keeps the last lines a job logs under each span, for its message to show, and its
    warnings for the end."""

    def emit(self, record: logging.LogRecord) -> None:
        if (job := _job.get()) is None or record.levelno < logging.INFO:
            return
        if getattr(record, "span_event", None):  # the message draws the spans themselves
            return
        line = record.getMessage()
        if record.levelno >= logging.WARNING:
            job.warnings.append(line)
            line = f"⚠ {line}"
        if (s := current_span()) is not None:
            job.recent.setdefault(s, deque(maxlen=3)).append(line)


def _tree(root: Span, recent: dict[Span, deque[str]]) -> list[str]:
    """The spans under `root`, a line each, indented under their parents: the running ones with
    how far they are, the deepest of them with its `recent` lines. A parent that counts its spans,
    as a playlist its videos, leaves out those done, unless they failed: its count shows them."""
    lines = []

    def walk(parent: Span, depth: int) -> None:
        for s in list(parent.children):
            if s.status == "ok" and parent.total is not None:
                continue
            # A thread's span outlives its cancelled job until the thread's next checkpoint.
            stopped = s.status == "running" and parent.status != "running"
            view = replace(s, status="cancelled") if stopped else s
            lines.append("  " * depth + _line(view, LINE_WIDTH - 2 * depth))
            walk(view, depth + 1)
            if view.status == "running" and not any(c.status == "running" for c in s.children):
                # Copied first: a thread may log while this reads.
                shown = list(recent.get(s, ()))
                lines.extend(_cut("  " * (depth + 1) + line, LINE_WIDTH) for line in shown)

    walk(root, 0)
    return lines


def _line(s: Span, width: int) -> str:
    """`s` in `width` characters: running with how far it is, or finished with how it ended and
    its time, its name and result cut to fit. The counts are left to the totals line."""
    if s.status == "running":
        progress = f" · {s.progress()}" if s.total else ""
        return f"▸ {_cut(s.name, width - len(progress) - 2)}{progress}"
    *ending, time = s.outcome(counts=False)
    name = _cut(s.name, min(NAME_WIDTH, width - len(time) - 5))  # 5 for "✓ " and " · "
    rest = _cut(" · ".join(ending), width - len(name) - len(time) - 8)  # and another " · "
    return " · ".join(filter(None, [f"{ICONS[s.status]} {name}", rest, time]))


def _cut(text: str, width: int) -> str:
    """`text`, ending in … where it is cut to `width`; nothing if not even that fits."""
    if len(text) <= width:
        return text
    return text[: width - 1].rstrip() + "…" if width > 1 else ""


class Runner:
    """Runs the jobs one at a time, in the order they came."""

    def __init__(self) -> None:
        self.current: Job | None = None
        self.pending: list[Job] = []
        self._ids = itertools.count(1)
        self._added = asyncio.Event()

    async def add(self, request: Message, label: str, run: Callable[[], Awaitable[str]]) -> None:
        """Queue `run`, answering `request` with the message that will show the job."""
        job_id = next(self._ids)
        status = await request.reply_text(f"{label}\nWaiting", reply_markup=_cancel_button(job_id))
        self.pending.append(Job(job_id, label, run, status))
        self._added.set()

    def find(self, job_id: int) -> Job | None:
        return next((j for j in (self.current, *self.pending) if j and j.id == job_id), None)

    async def cancel(self, job: Job) -> None:
        if job is self.current:
            job.task.cancel()
        elif job in self.pending:
            self.pending.remove(job)
            await _edit(job, f"🛑 Cancelled\n{job.label}")

    async def work(self) -> None:
        while True:
            await self._added.wait()
            self._added.clear()
            while self.pending:
                self.current = self.pending.pop(0)
                try:
                    await self._run(self.current)
                except Exception:
                    # Else every job after this one would wait forever.
                    log.exception("The runner failed on a job; it goes on with the next one")
                finally:
                    self.current = None

    async def _run(self, job: Job) -> None:
        job.task = asyncio.create_task(_execute(job))
        done = asyncio.Event()
        shown = asyncio.create_task(_show(job, done))
        try:
            try:
                result = await job.task
            finally:
                # The last progress edit is answered before the final one is sent.
                done.set()
                await shown
        except asyncio.CancelledError:
            # Only the job was cancelled, unless the bot is stopping and cancelled the worker.
            if asyncio.current_task().cancelling():
                await _edit(job, f"🛑 Dropped as the bot stopped\n{job.label}")
                raise
            head, body = f"🛑 Cancelled\n{job.label}", ""
        except Exception as e:
            log.exception(f"Failed: {job.label}")
            reason = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
            head, body = f"💥 {job.label}", reason
        else:
            head, body = f"✅ {job.label}", result
        text = "\n".join(filter(None, [head, body, *job.summary()]))
        await _edit(job, _closing(text, job.warnings))


async def _execute(job: Job) -> str:
    """The job, in a task of its own, under a span whose tree shows on the job's message."""
    _job.set(job)
    with span(job.label) as job.root:
        return await job.run()


async def _show(job: Job, done: asyncio.Event) -> None:
    """Keep the job's message on how far it is, with a button to cancel it, until `done`."""
    shown = None
    while not done.is_set():
        if (text := job.progress()) != shown:
            await _edit(job, text, cancel=True)
            shown = text
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(done.wait(), timeout=5)


def _closing(text: str, warnings: list[str]) -> str:
    """The finished message: `text`, then the last warnings the job logged."""
    lines = [text, *(f"⚠️ {w}" for w in warnings[-10:])]
    if len(warnings) > 10:
        lines.insert(1, f"⚠️ {len(warnings) - 10} earlier warnings are in `easyt log`")
    return "\n".join(lines)


def _fit(text: str, limit: int = 4096) -> str:
    """`text` cut to Telegram's length limit, which counts UTF-16 code units: 2 for an emoji."""
    units = 0
    for i, char in enumerate(text):
        units += 2 if ord(char) > 0xFFFF else 1
        if units > limit:
            return text[:i]
    return text


def _cancel_button(job_id: int) -> InlineKeyboardMarkup:
    button = InlineKeyboardButton("Cancel", callback_data=f"cancel:{job_id}")
    return InlineKeyboardMarkup([[button]])


async def _edit(job: Job, text: str, *, cancel: bool = False) -> None:
    try:
        await job.status.edit_text(
            _fit(text), reply_markup=_cancel_button(job.id) if cancel else None
        )
    except TelegramError as e:  # such as an edit that changes nothing
        log.debug(f"Telegram edit failed: {e}")


async def _youtube(mode: Mode, url: str) -> str:
    title, folder = await run_youtube(url, mode)
    if mode != "translate":
        return title  # the pipeline has sent its file to the chat
    if not folder:
        return f"{title}\nNot uploaded to Google Drive"
    return f"{title}\n{DRIVE_FOLDER}{folder}"


def build_app(token: str, chat_id: int) -> Application:
    runner = Runner()
    # New messages of the one chat; edits of old ones are left alone.
    chat = filters.Chat(chat_id) & filters.UpdateType.MESSAGE
    commands = COMMANDS | {name: plugin.bot_about for name, plugin in plugins.commands().items()}
    help_text = "Send a YouTube video or playlist URL to translate it.\n" + "\n".join(
        f"/{command} - {about}" for command, about in commands.items()
    )

    async def add_youtube(request: Message, mode: Mode, url: str) -> None:
        try:
            parse_youtube_url(url)
        except ValueError as e:
            await request.reply_text(str(e))
            return
        await runner.add(request, f"{mode.capitalize()} {url}", lambda: _youtube(mode, url))

    async def on_text(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
        if not (urls := find_urls(update.message.text)):
            await update.message.reply_text(help_text)
        for url in urls:
            await add_youtube(update.message, "translate", url)

    def on_url_command(mode: Mode):
        async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            if not (urls := find_urls(" ".join(context.args))):
                await update.message.reply_text(f"/{mode} takes a YouTube URL")
            for url in urls:
                await add_youtube(update.message, mode, url)

        return handle

    def on_plugin(plugin: plugins.Command):
        async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            try:
                label, work = plugin.job(context.args)
            except ValueError as e:
                await update.message.reply_text(str(e))
                return
            await runner.add(update.message, label, work)

        return handle

    async def on_status(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
        lines = [runner.current.progress()] if runner.current else []
        lines += [f"Waiting: {job.label}" for job in runner.pending]
        await update.message.reply_text("\n\n".join(lines) or "Nothing is running")

    async def on_cancel(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
        if (job := runner.current) is None:
            await update.message.reply_text("Nothing is running")
            return
        await update.message.reply_text(f"Cancelling: {job.label}")
        await runner.cancel(job)

    async def on_cancel_button(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        job = None
        if update.effective_chat.id == chat_id:
            job = runner.find(int(query.data.removeprefix("cancel:")))
        if job:
            await runner.cancel(job)
        await query.answer("Cancelling" if job else "Already finished")

    async def start(app: Application) -> None:
        await app.bot.set_my_commands([BotCommand(c, about) for c, about in commands.items()])
        app.bot_data["worker"] = asyncio.create_task(runner.work())

    async def stop(app: Application) -> None:
        worker = app.bot_data["worker"]
        worker.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await worker
        for job in runner.pending:
            await _edit(job, f"🛑 Dropped as the bot stopped\n{job.label}")

    # The URL in a job's message would otherwise open into a large preview of the video.
    defaults = Defaults(link_preview_options=LinkPreviewOptions(is_disabled=True))
    app = (
        Application.builder()
        .token(token)
        .defaults(defaults)
        .post_init(start)
        .post_stop(stop)
        .build()
    )
    app.add_handler(CommandHandler("aterm", on_url_command("aterm"), filters=chat))
    app.add_handler(CommandHandler("transcript", on_url_command("transcript"), filters=chat))
    for name, plugin in plugins.commands().items():
        app.add_handler(CommandHandler(name, on_plugin(plugin), filters=chat))
    app.add_handler(CommandHandler("status", on_status, filters=chat))
    app.add_handler(CommandHandler("cancel", on_cancel, filters=chat))
    app.add_handler(MessageHandler(chat & filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(CallbackQueryHandler(on_cancel_button, pattern=r"^cancel:\d+$"))
    return app


def run() -> None:
    """Answer the chat until stopped."""
    log.addHandler(JobLog())
    app = build_app(os.environ["TELEGRAM_BOT_TOKEN"], int(os.environ["TELEGRAM_CHAT_ID"]))
    app.run_polling()
