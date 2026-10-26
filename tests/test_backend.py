import json
from typing import Literal

import httpx2
import pytest
from pydantic import BaseModel
from pydantic_ai.exceptions import ModelHTTPError, UserError
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.openai import OpenAIResponsesModel
from pydantic_ai.models.openrouter import OpenRouterModel

from easyt.backend import BackendError, open_backend
from easyt.backend.claude_code import ClaudeCodeBackend
from easyt.backend.codex import CodexBackend
from easyt.backend.pydantic_ai import PydanticAIBackend
from easyt.parser.structured import GuideError


@pytest.fixture(autouse=True)
def api_keys(monkeypatch):
    """The models can't be built without their keys, though nothing here calls an API."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-key")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
    monkeypatch.setenv("OPENROUTER_API_KEY", "openrouter-key")


@pytest.mark.parametrize(
    "name, model_class",
    [
        ("anthropic:claude-opus-5-5", AnthropicModel),
        ("openai:gpt-5.6-terra", OpenAIResponsesModel),
        ("openrouter:google/gemini-3-pro", OpenRouterModel),
    ],
)
def test_a_provider_model_name_runs_on_that_provider(monkeypatch, name, model_class):
    monkeypatch.setenv("LLM_BACKEND", name)
    backend = open_backend("Test")
    assert isinstance(backend, PydanticAIBackend)
    assert isinstance(backend.model, model_class)
    assert backend.model.model_name == name.split(":", 1)[1]


def test_openrouter_runs_on_its_own_key_not_openai_s():
    model = PydanticAIBackend("openrouter:google/gemini-3-pro").model
    assert model.client.api_key == "openrouter-key"


def test_openrouter_needs_its_own_key(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY")
    with pytest.raises(UserError, match="OPENROUTER_API_KEY"):
        PydanticAIBackend("openrouter:google/gemini-3-pro")


@pytest.mark.parametrize("backend", ["claude_code", "Codex", "claude-opus-5-5"])
def test_a_name_neither_an_agent_cli_nor_a_provider_model_is_refused(monkeypatch, backend):
    monkeypatch.setenv("LLM_BACKEND", backend)
    with pytest.raises(UserError, match="Unknown model"):
        open_backend("Test")


def test_the_backend_defaults_to_claude_code(monkeypatch):
    monkeypatch.delenv("LLM_BACKEND", raising=False)
    assert isinstance(open_backend("Test"), ClaudeCodeBackend)


@pytest.mark.parametrize(
    "name, backend_class, model",
    [("claude-code", ClaudeCodeBackend, "opus"), ("codex", CodexBackend, "gpt-6.1-sol")],
)
def test_an_agent_cli_runs_its_model_at_high_effort(monkeypatch, name, backend_class, model):
    monkeypatch.setenv("LLM_BACKEND", name)
    backend = open_backend("Test")
    assert isinstance(backend, backend_class)
    assert (backend.model, backend.effort) == (model, "high")


@pytest.fixture
def sent(monkeypatch):
    """The JSON body of every request the APIs would be sent, each refused instead of sent."""
    bodies = []

    async def refuse(client, request, **kwargs):
        bodies.append(json.loads(request.content))
        error = {"error": {"type": "invalid_request_error", "message": "refused by the test"}}
        return httpx2.Response(400, json=error, request=request)

    monkeypatch.setattr(httpx2.AsyncClient, "send", refuse)
    return bodies


async def _body_sent_for(name, sent):
    with pytest.raises(ModelHTTPError):
        await PydanticAIBackend(name).ask("system", "prompt")
    [body] = sent
    return body


async def test_claude_is_sent_high_effort(sent):
    body = await _body_sent_for("anthropic:claude-opus-5-5", sent)
    assert body["output_config"] == {"effort": "high"}


async def test_openai_is_sent_high_effort_and_no_output_cap(sent):
    body = await _body_sent_for("openai:gpt-6.1-sol", sent)
    assert body["reasoning"]["effort"] == "high"
    assert "max_output_tokens" not in body


async def test_openrouter_is_sent_high_effort_and_no_output_cap(sent):
    body = await _body_sent_for("openrouter:meta-llama/llama-3.3-70b-instruct", sent)
    assert body["reasoning"]["effort"] == "high"
    assert "max_tokens" not in body


async def test_an_answer_cut_off_at_a_token_limit_is_kept_as_it_is(fake_model, caplog):
    model = fake_model(("<output>Hel", "length"))
    answer = await PydanticAIBackend(model).ask("system", "Say hello.")
    assert answer == "<output>Hel"
    assert len(model.asked) == 1
    assert "stopped at a token limit" in caplog.text


async def test_an_answer_without_text_is_taken_without_asking_again(fake_model):
    model = fake_model("")
    assert await PydanticAIBackend(model).ask("system", "Say hello.") == ""
    assert len(model.asked) == 1


@pytest.mark.parametrize(
    "failure, error",
    [
        (RuntimeError("overloaded"), RuntimeError),
        (("", "length"), BackendError),  # thinking alone used up the output
        (("", "content_filter"), BackendError),
    ],
)
async def test_a_failed_turn_raises_and_leaves_no_question_in_the_history(
    fake_model, failure, error
):
    model = fake_model(failure, "answer")
    async with PydanticAIBackend(model).session("system") as session:
        with pytest.raises(error):
            await session.ask("one")
        assert await session.ask("two") == "answer"
    assert model.asked[1] == ["system", "two"]


async def test_a_pydantic_ai_session_sends_the_history_with_every_turn(fake_model):
    model = fake_model("first", "second")
    async with PydanticAIBackend(model).session("system") as session:
        assert await session.ask("one") == "first"
        assert await session.ask("two") == "second"
    assert model.asked == [["system", "one"], ["system", "one", "first", "two"]]


async def test_a_single_prompt_is_a_session_of_one_turn(fake_model):
    model = fake_model("answer")
    assert await PydanticAIBackend(model).ask("system", "one") == "answer"
    assert model.asked == [["system", "one"]]


class Speaker(BaseModel):
    speaker: Literal["SPEAKER_1", "SPEAKER_2"]


async def test_a_structured_answer_is_read_from_the_guided_block(fake_model):
    model = fake_model('<speaker>{"speaker": "SPEAKER_2"}</speaker>')
    async with PydanticAIBackend(model).session("system") as session:
        assert await session.ask_structured("Who?", Speaker) == Speaker(speaker="SPEAKER_2")
    prompt = model.asked[0][-1]
    assert prompt.startswith("Who?") and "<speaker>" in prompt  # the guide rides on the prompt


async def test_an_unreadable_structured_answer_is_asked_again_with_the_complaint(fake_model):
    model = fake_model(
        '<speaker>{"speaker": "SPEAKER_3"}</speaker>',
        '<speaker>{"speaker": "SPEAKER_1"}</speaker>',
    )
    async with PydanticAIBackend(model).session("system") as session:
        assert await session.ask_structured("Who?", Speaker) == Speaker(speaker="SPEAKER_1")
    reask = model.asked[1][-1]
    assert "SPEAKER_3" in reask and "<speaker>" in reask


async def test_a_structured_answer_still_unreadable_after_the_retries_raises(fake_model):
    model = fake_model("no block", "still none")
    async with PydanticAIBackend(model).session("system") as session:
        with pytest.raises(GuideError):
            await session.ask_structured("Who?", Speaker, retries=1)


@pytest.mark.live
@pytest.mark.parametrize("backend", ["claude-code", "codex"])
async def test_a_session_on_a_live_subscription(monkeypatch, backend):
    monkeypatch.setenv("LLM_BACKEND", backend)
    system = "You answer in as few words as possible."
    async with open_backend("Test").session(system) as session:
        await session.ask("The code word is ZEBRA. Reply with OK only.")
        assert "ZEBRA" in (await session.ask("What is the code word?")).upper()
