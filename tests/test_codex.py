from types import SimpleNamespace

import pytest
from openai_codex import ApprovalMode, Sandbox, ServerBusyError, TransportClosedError
from openai_codex.types import TurnError, TurnStatus

import easyt.backend.codex
from easyt.backend import BackendError, UsageLimitReached
from easyt.backend.codex import CodexBackend
from easyt.stages.translate import Translator


def _event(method: str, **payload):
    return SimpleNamespace(method=method, payload=SimpleNamespace(**payload))


def _delta(text: str):
    return _event("item/agentMessage/delta", delta=text)


def _message(text: str, phase: str | None = "final_answer"):
    item = SimpleNamespace(
        type="agentMessage", phase=phase and SimpleNamespace(value=phase), text=text
    )
    return _event("item/completed", item=SimpleNamespace(root=item))


def _completed(status: TurnStatus = TurnStatus.completed, error: dict | None = None):
    error = error and TurnError.model_validate(error)
    return _event("turn/completed", turn=SimpleNamespace(status=status, error=error, duration_ms=1))


def _failed(kind: str | dict, message: str = "turn failed"):
    return _completed(TurnStatus.failed, {"message": message, "codexErrorInfo": kind})


@pytest.fixture
def fake_codex(monkeypatch):
    """Stand in for AsyncCodex: each turn streams the given events up to its turn/completed and
    raises any exception among them. Returns the keyword arguments of each thread it started and
    the prompts of its turns."""
    seen = SimpleNamespace(threads=[], prompts=[])

    def install(*events, account_type: str | None = "chatgpt"):
        stream = list(events)

        class FakeCodex:
            def __init__(self, config):
                self.config = config

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return None

            async def account(self):
                if account_type is None:
                    return SimpleNamespace(account=None)
                root = SimpleNamespace(type=account_type)
                return SimpleNamespace(account=SimpleNamespace(root=root))

            async def thread_start(self, **kwargs):
                seen.threads.append(kwargs)
                return self

            async def turn(self, prompt, *, effort):
                seen.prompts.append(prompt)
                return self

            async def stream(self):
                while stream:
                    event = stream.pop(0)
                    if isinstance(event, Exception):
                        raise event
                    yield event
                    if event.method == "turn/completed":
                        return

        monkeypatch.setattr(easyt.backend.codex, "AsyncCodex", FakeCodex)
        return seen

    return install


def _backend():
    return CodexBackend(model="m", effort="low")


async def _run():
    return await _backend().ask("system", "prompt")


async def test_run_returns_the_final_answer(fake_codex):
    codex = fake_codex(
        _delta('<segment id="1">on'),
        _delta("e</segment>"),
        _message("Translating now.", phase="commentary"),
        _message('<segment id="1">on', phase="partial_answer"),
        _message('<segment id="1">one</segment>'),
        _completed(),
    )
    assert await _run() == '<segment id="1">one</segment>'
    [thread_kwargs] = codex.threads
    assert thread_kwargs["base_instructions"] == "system"
    assert thread_kwargs["approval_mode"] == ApprovalMode.deny_all
    assert thread_kwargs["sandbox"] == Sandbox.read_only
    assert thread_kwargs["model_provider"] == "openai"
    assert thread_kwargs["ephemeral"] is True


async def test_run_takes_the_last_unphased_message_without_a_final_answer(fake_codex):
    fake_codex(_message("first", phase=None), _message("second", phase=None), _completed())
    assert await _run() == "second"


async def test_a_session_runs_its_turns_on_one_thread(fake_codex):
    codex = fake_codex(_message("first"), _completed(), _message("second"), _completed())
    async with _backend().session("system") as session:
        assert await session.ask("one") == "first"
        assert await session.ask("two") == "second"
    assert len(codex.threads) == 1
    assert codex.prompts == ["one", "two"]


async def test_run_raises_usage_limit(fake_codex):
    fake_codex(_failed("usageLimitExceeded", "You've hit your usage limit"))
    with pytest.raises(UsageLimitReached):
        await _run()


@pytest.mark.parametrize(
    "kind",
    [
        "rateLimitExceeded",
        "serverOverloaded",
        "other",
        {"responseStreamDisconnected": {"httpStatusCode": None}},
        {"responseTooManyFailedAttempts": {"httpStatusCode": 429}},
        "unauthorized",
        {"httpConnectionFailed": {"httpStatusCode": 401}},
    ],
)
async def test_a_failed_turn_raises(fake_codex, kind):
    fake_codex(_failed(kind))
    with pytest.raises(BackendError) as e:
        await _run()
    assert type(e.value) is BackendError


@pytest.mark.parametrize(
    "events",
    [
        [_delta("x"), ServerBusyError(-32001, "Server overloaded")],
        [_delta("x"), TransportClosedError("app-server closed")],
        [_delta("x")],
    ],
)
async def test_a_turn_that_breaks_off_raises(fake_codex, events):
    fake_codex(*events)
    with pytest.raises(BackendError):
        await _run()


@pytest.mark.parametrize("account_type", ["apiKey", None])
async def test_run_refuses_anything_but_a_chatgpt_login(fake_codex, account_type):
    fake_codex(_message("x"), _completed(), account_type=account_type)
    with pytest.raises(BackendError, match="not the ChatGPT subscription"):
        await _run()


@pytest.mark.live
async def test_translation_on_a_live_chatgpt_subscription(monkeypatch, source_sentences):
    monkeypatch.setenv("LLM_BACKEND", "codex")
    result = await Translator("en", "ko").translate(source_sentences[:5])
    assert len(result) == 5
    assert all(item["target"] != item["source"] for item in result)
