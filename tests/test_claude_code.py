import pytest
from claude_agent_sdk import (
    AssistantMessage,
    CLINotFoundError,
    ProcessError,
    RateLimitEvent,
    RateLimitInfo,
    ResultMessage,
    SystemMessage,
    TextBlock,
)

import easyt.backend.claude_code
from easyt.backend import BackendError, UsageLimitReached
from easyt.backend.claude_code import ClaudeCodeBackend
from easyt.stages.translate import Translator


def _init(api_key_source: str | None = "none"):
    data = {} if api_key_source is None else {"apiKeySource": api_key_source}
    return SystemMessage(subtype="init", data=data)


def _assistant(text: str, error: str | None = None):
    return AssistantMessage(content=[TextBlock(text=text)], model="m", error=error)


def _result(is_error: bool = False, result: str = "", api_error_status: int | None = None):
    return ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=is_error,
        num_turns=1,
        session_id="s",
        result=result,
        api_error_status=api_error_status,
    )


@pytest.fixture
def fake_cli(monkeypatch):
    """Stand in for ClaudeSDKClient: each turn streams the given messages up to its ResultMessage,
    as the SDK's does, and raises any exception among them; one that comes first fails the start.
    Returns what the client was asked to do, in order."""
    calls = []

    def install(*messages):
        stream = list(messages)

        class FakeClient:
            def __init__(self, options):
                pass

            async def connect(self):
                calls.append("connect")
                if stream and isinstance(stream[0], Exception):
                    raise stream.pop(0)

            async def query(self, prompt):
                calls.append(("query", prompt))

            async def receive_response(self):
                try:
                    while stream:
                        message = stream.pop(0)
                        if isinstance(message, Exception):
                            raise message
                        yield message
                        if isinstance(message, ResultMessage):
                            return
                finally:
                    calls.append("closed")

            async def disconnect(self):
                calls.append("disconnect")

        monkeypatch.setattr(easyt.backend.claude_code, "ClaudeSDKClient", FakeClient)
        return calls

    return install


def _backend():
    return ClaudeCodeBackend(model="m", effort="low")


async def _run():
    return await _backend().ask("system", "prompt")


async def test_run_joins_messages_continued_past_max_tokens(fake_cli):
    fake_cli(_init(), _assistant('<segment id="1">on'), _assistant("e</segment>"), _result())
    assert await _run() == '<segment id="1">one</segment>'


async def test_a_session_runs_its_turns_on_one_cli(fake_cli):
    calls = fake_cli(
        _init(), _assistant("first"), _result(), _init(), _assistant("second"), _result()
    )
    async with _backend().session("system") as session:
        assert await session.ask("one") == "first"
        assert await session.ask("two") == "second"
    assert calls == [
        "connect",
        ("query", "one"),
        "closed",
        ("query", "two"),
        "closed",
        "disconnect",
    ]


@pytest.mark.parametrize(
    "messages",
    [
        [
            RateLimitEvent(
                rate_limit_info=RateLimitInfo(
                    status="rejected", resets_at=1792771200, rate_limit_type="five_hour"
                ),
                uuid="u",
                session_id="s",
            ),
            _result(is_error=True, result="You've hit your limit"),
        ],
        [
            _assistant("You've hit your limit", error="rate_limit"),
            _result(is_error=True, result="You've hit your limit"),
        ],
        [_result(is_error=True, result="API Error: 429", api_error_status=429)],
    ],
)
async def test_run_raises_usage_limit(fake_cli, messages):
    fake_cli(_init(), *messages)
    with pytest.raises(UsageLimitReached):
        await _run()


@pytest.mark.parametrize(
    "messages",
    [
        [
            _assistant("API Error: 529 Overloaded", error="server_error"),
            _result(is_error=True, result="API Error: 529 Overloaded", api_error_status=529),
        ],
        [
            _assistant("API Error: exceeded the output token maximum", error="max_output_tokens"),
            _result(is_error=True, result="API Error: exceeded the output token maximum"),
        ],
        [
            _assistant("Invalid API key", error="authentication_failed"),
            _result(is_error=True, result="Invalid API key"),
        ],
        [_result(is_error=True, result="API Error: 401", api_error_status=401)],
        [_result(is_error=True, result="API Error: something unexpected")],
        [_assistant("x"), ProcessError("Command failed", exit_code=1)],
    ],
)
async def test_a_failed_turn_raises(fake_cli, messages):
    calls = fake_cli(_init(), *messages)
    with pytest.raises(BackendError) as e:
        await _run()
    assert type(e.value) is BackendError
    assert calls[-1] == "disconnect"


@pytest.mark.parametrize("error", [CLINotFoundError(), ProcessError("Command failed", exit_code=1)])
async def test_a_cli_that_fails_to_start_raises(fake_cli, error):
    fake_cli(error)
    with pytest.raises(BackendError, match="failed to start"):
        await _run()


async def test_run_refuses_to_bill_an_api_key(fake_cli):
    calls = fake_cli(_init("ANTHROPIC_API_KEY"), _assistant("x"), _result())
    with pytest.raises(BackendError, match="API key"):
        await _run()
    assert calls[-2:] == ["closed", "disconnect"]  # the stream is closed before the CLI


async def test_run_accepts_an_init_without_api_key_source(fake_cli):
    fake_cli(_init(None), _assistant("x"), _result())
    assert await _run() == "x"


@pytest.mark.live
async def test_translation_on_a_live_claude_subscription(monkeypatch, source_sentences):
    monkeypatch.setenv("LLM_BACKEND", "claude-code")
    result = await Translator("en", "ko").translate(source_sentences[:5])
    assert len(result) == 5
    assert all(item["target"] != item["source"] for item in result)
