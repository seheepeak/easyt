import pytest

import easyt.stages.translate
from easyt.backend.pydantic_ai import PydanticAIBackend
from easyt.stages.translate import Translator

ONE = '<segment id="1">uno</segment>'
TRANSLATED = [{"source": "one", "target": "uno"}]


@pytest.fixture
def translator_answering(fake_backend):
    """Make a Translator whose backend answers each chunk with the next of the given answers.
    Returns it and the prompts of each session it held."""

    def make(*answers):
        answers = list(answers)

        async def run(system, prompt):
            return answers.pop(0)

        sessions = fake_backend(easyt.stages.translate, run)
        return Translator("en", "es"), sessions

    return make


async def test_a_chunk_is_translated_once_and_then_read_from_its_cache(
    translator_answering, tmp_path
):
    translator, sessions = translator_answering(ONE)
    cache = tmp_path / "cache.txt"
    assert await translator.translate(["one"], cache) == TRANSLATED
    assert cache.read_text(encoding="utf-8") == ONE
    assert await translator.translate(["one"], cache) == TRANSLATED
    assert len(sessions) == 1


async def test_a_sentence_the_answer_leaves_out_keeps_its_source(translator_answering, caplog):
    translator, _ = translator_answering(ONE)  # and nothing for "two"
    result = await translator.translate(["one", "two"])
    assert [item["target"] for item in result] == ["uno", "two"]
    assert "segments [2] got no answer" in caplog.text


async def test_a_cache_without_segments_is_translated_again(translator_answering, tmp_path):
    translator, sessions = translator_answering(ONE)
    cache = tmp_path / "cache.txt"
    cache.write_text('[{"source": "one", "target": "uno"}]', encoding="utf-8")  # an older run's
    assert await translator.translate(["one"], cache) == TRANSLATED
    assert len(sessions) == 1


@pytest.mark.parametrize(
    "code, name",
    [("en", "English"), ("pt", "Portuguese"), ("zh-Hant", "Chinese (Traditional)"), ("xx", "xx")],
)
def test_the_prompt_names_the_source_language(fake_backend, code, name):
    fake_backend(easyt.stages.translate, run=None)  # nothing is asked
    assert Translator(code, "ko").prompt.partials["source_lang"] == name


async def test_a_text_comes_back_as_it_went_out_whatever_it_holds(fake_backend):
    async def run(system, prompt):
        return prompt.split("<source_text>")[1].split("</source_text>")[0]  # kept as it came

    fake_backend(easyt.stages.translate, run)
    text = "price&regulation < 5 & rising"
    assert await Translator("en", "ko").translate([text]) == [{"source": text, "target": text}]


@pytest.mark.live
async def test_translation_on_the_api(monkeypatch, source_sentences):
    monkeypatch.setenv("LLM_BACKEND", "anthropic:claude-opus-5-5")
    translator = Translator("en", "ko")
    assert isinstance(translator.backend, PydanticAIBackend)
    result = await translator.translate(source_sentences[:5])
    assert len(result) == 5
    assert all(isinstance(item["source"], str) for item in result)
    assert all(isinstance(item["target"], str) for item in result)
