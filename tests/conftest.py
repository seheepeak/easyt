import contextlib
import json
from pathlib import Path

import pytest
from pydantic_ai.messages import TextPart, UserPromptPart
from pydantic_ai.models.function import DeltaThinkingPart, FunctionModel

import easyt.backend.pydantic_ai
import easyt.plugins
from easyt.backend import Backend, Session
from easyt.plugins import Command

fixtures_dir = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def _code_defaults(monkeypatch):
    """The code's own defaults, whatever ~/.easyt/.env tunes for real runs."""
    for name in ("MAX_LINE_LEN", "LLM_CONCURRENCY"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def source_sentences():
    with (fixtures_dir / "source_sentences.json").open("r") as f:
        return json.load(f)


@pytest.fixture(autouse=True)
def _no_installed_plugins(monkeypatch, tmp_path):
    """No plugins but a test's own, whatever PLUGINS_DIR holds."""
    monkeypatch.setattr(easyt.plugins, "PLUGINS_DIR", tmp_path / "plugins")
    easyt.plugins.commands.cache_clear()


@pytest.fixture
def echo_plugin(monkeypatch):
    """A plugin's command `echo WORD`, as if easyt had found it in PLUGINS_DIR. Returns the
    words it ran with."""
    ran = []

    async def run(args):
        ran.append(args.word)

    def job(words):
        async def work():
            ran.extend(words)
            return "Echoed"

        return "Echo", work

    echo = Command(
        name="echo",
        about="Echo a word",
        add_arguments=lambda parser: parser.add_argument("word"),
        run=run,
        bot_about="Echo a word: /echo WORD",
        job=job,
    )
    monkeypatch.setattr(easyt.plugins, "commands", lambda: {"echo": echo})
    return ran


@pytest.fixture
def fake_backend(monkeypatch):
    """Stand in for `open_backend` in a stage's module: every turn is answered by
    `run(system, prompt)`. Returns the prompts of each session it held."""

    def install(module, run):
        sessions = []

        class FakeSession(Session):
            def __init__(self, system):
                self.system = system
                self.prompts = []
                sessions.append(self.prompts)

            async def ask(self, prompt):
                self.prompts.append(prompt)
                return await run(self.system, prompt)

        class FakeBackend(Backend):
            @contextlib.asynccontextmanager
            async def session(self, system):
                yield FakeSession(system)

        monkeypatch.setattr(module, "open_backend", lambda stage: FakeBackend())
        return sessions

    return install


@pytest.fixture
def fake_answers(fake_backend):
    """`fake_backend` answering a stage's prompts with `replies` in turn."""

    def install(module, *replies):
        replies = list(replies)

        async def run(system, prompt):
            return replies.pop(0)

        return fake_backend(module, run)

    return install


class FakeModel(FunctionModel):
    """Streams each request the next answer: a text, a (text, finish reason) pair, or an
    exception to raise; an empty text streams thinking alone. `asked` keeps what each request
    was sent: the instructions, then every prompt and answer of the history."""

    def __init__(self, *answers):
        super().__init__(stream_function=self._stream)
        self.answers = list(answers)
        self.asked = []
        self._finish_reason = None

    async def _stream(self, messages, info):
        sent = [
            p.content for m in messages for p in m.parts if isinstance(p, UserPromptPart | TextPart)
        ]
        self.asked.append([info.instructions, *sent])
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        text, self._finish_reason = answer if isinstance(answer, tuple) else (answer, None)
        yield text or {0: DeltaThinkingPart(content="Thinking.")}

    @contextlib.asynccontextmanager
    async def request_stream(self, *args, **kwargs):
        async with super().request_stream(*args, **kwargs) as stream:
            # Set once the stream is open, by when its answer has been picked.
            stream.finish_reason = self._finish_reason
            yield stream


@pytest.fixture
def fake_model(monkeypatch):
    """Stand in for the model any name runs on: `install(*answers)` makes it a `FakeModel`
    answering with `answers`, and returns it."""

    def install(*answers):
        model = FakeModel(*answers)
        monkeypatch.setattr(easyt.backend.pydantic_ai, "infer_model", lambda name: model)
        return model

    return install
