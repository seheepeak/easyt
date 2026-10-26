import asyncio
import re

import numpy as np
import pytest

import easyt.stages.punctuate
from easyt.backend import BackendError
from easyt.stages.punctuate import Punctuator, SentSplitter, _create_llm_punct

# Raw transcript words, a sentence ending after each `endN`.
TEXT = " ".join(f"end{i}" if i % 3 == 2 else f"w{i}" for i in range(800))


class FakeSaT:
    """Sure a sentence ends after each `endN` word, and nowhere else; splits nothing."""

    def predict_proba(self, text):
        probs = np.zeros(len(text))
        for m in re.finditer(r"end\d+", text):
            probs[m.end() - 1] = 1.0
        return probs

    def split(self, text):
        return [text]


@pytest.fixture
def sat(monkeypatch):
    monkeypatch.setattr(SentSplitter, "_sat", staticmethod(FakeSaT))


def _chunk(prompt: str) -> str:
    return prompt.split("<input>")[1].split("</input>")[0].strip("\n")


async def test_punctuation_runs_on_the_backend(fake_backend):
    async def run(system, prompt):
        return "<output>From the backend.</output>"

    sessions = fake_backend(easyt.stages.punctuate, run)
    assert await _create_llm_punct()("from the backend") == "From the backend."
    assert len(sessions) == 1


@pytest.mark.parametrize("answer", ["I can't do that.", "<output>\n</output>"])
async def test_a_chunk_with_no_answer_is_not_saved_and_sat_splits_it_as_it_came(
    sat, fake_backend, tmp_path, answer
):
    async def run(system, prompt):
        return answer

    fake_backend(easyt.stages.punctuate, run)
    punctuator = Punctuator(tmp_path)
    assert await punctuator._punctuate(0, "so today we will learn") is None
    assert not list(tmp_path.glob("punctuate_cache_*"))  # so a rerun asks again
    [result] = await punctuator.run("so today we will learn")
    assert [s for _, s in result["sentences"]] == ["so today we will learn"]  # FakeSaT's


async def test_each_line_of_the_answer_is_a_sentence_quotation_and_all(sat, fake_backend, tmp_path):
    async def run(system, prompt):
        return '<output>\nAnd we say, "Okay, great. We sign."\n\nSo we did.\n</output>'

    fake_backend(easyt.stages.punctuate, run)
    [result] = await Punctuator(tmp_path).run("and we say okay great we sign so we did")
    assert result["sentences"] == [
        ("and we say okay great we sign", 'And we say, "Okay, great. We sign."'),
        ("so we did", "So we did."),
    ]


class SplitsAtPeriods:
    """Ends a sentence at each period."""

    def split(self, text):
        return re.findall(r"[^.]+\.", text)


def test_an_answer_left_on_one_line_is_split_by_sat(monkeypatch):
    monkeypatch.setattr(SentSplitter, "_sat", staticmethod(SplitsAtPeriods))
    assert SentSplitter().split_lines("\nHi there. We sign today.\n") == [
        "Hi there.",
        "We sign today.",
    ]


def test_a_line_too_long_to_be_one_sentence_is_split_by_sat(monkeypatch):
    monkeypatch.setattr(SentSplitter, "_sat", staticmethod(SplitsAtPeriods))
    together = " ".join(f"Sentence {n} stays here." for n in range(60))  # left on one line
    sents = SentSplitter().split_lines(f'He said, "Stop. Go home."\n{together}')
    assert sents[0] == 'He said, "Stop. Go home."' and len(sents) == 61


def test_chunks_are_cut_after_a_word_sat_is_sure_ends_a_sentence(sat):
    cuts = SentSplitter().cut_points(TEXT, 1000)
    assert len(cuts) == -(-len(TEXT) // 1000) - 1
    assert all(re.search(r"end\d+$", TEXT[:cut]) for cut in cuts)


def test_a_text_one_chunk_holds_is_not_cut(sat):
    assert SentSplitter().cut_points("so today we will learn", 4000) == []


def test_with_no_sentence_end_in_sight_a_cut_goes_between_the_words_nearest_it(monkeypatch):
    unsure = FakeSaT()
    unsure.predict_proba = lambda text: np.zeros(len(text))
    monkeypatch.setattr(SentSplitter, "_sat", staticmethod(lambda: unsure))
    cuts = SentSplitter().cut_points(TEXT, 1000)
    for k, cut in enumerate(cuts, 1):
        target = len(TEXT) * k // (len(cuts) + 1)
        assert TEXT[cut] == " " and TEXT[cut - 1] != " "
        assert abs(cut - target) < 8  # words here are shorter


async def test_chunks_are_punctuated_at_once_and_put_back_in_order(
    sat, fake_backend, monkeypatch, tmp_path
):
    monkeypatch.setenv("LLM_CONCURRENCY", "2")
    running, most = 0, 0

    async def run(system, prompt):
        nonlocal running, most
        running += 1
        most = max(most, running)
        await asyncio.sleep(0.01)
        running -= 1
        return f"<output>{_chunk(prompt).upper()}</output>"

    fake_backend(easyt.stages.punctuate, run)
    results = await Punctuator(tmp_path, chunk_size=1000).run(TEXT)

    assert len(results) > 2 and most == 2
    assert " ".join(s for r in results for _, s in r["sentences"]) == TEXT.upper()
    assert results[0]["chunk_start"] == 0 and results[-1]["chunk_end"] == len(TEXT)
    assert (tmp_path / "punctuate_results.json").exists()


async def test_a_failed_chunk_lets_the_running_one_finish_and_a_rerun_keeps_it(
    sat, fake_backend, monkeypatch, tmp_path
):
    monkeypatch.setenv("LLM_CONCURRENCY", "2")
    asked, fail = [], True

    async def run(system, prompt):
        asked.append(_chunk(prompt))
        if fail and len(asked) == 2:
            raise BackendError("usage limit reached")
        await asyncio.sleep(0.02)  # still running when the other one fails
        return f"<output>{_chunk(prompt)}</output>"

    fake_backend(easyt.stages.punctuate, run)
    with pytest.raises(BackendError):
        await Punctuator(tmp_path, chunk_size=1000).run(TEXT)
    first = asked[0]
    assert len(asked) == 2  # no chunk starts after the failure
    assert (tmp_path / "punctuate_cache_1.txt").read_text() == first

    asked.clear()
    fail = False
    results = await Punctuator(tmp_path, chunk_size=1000).run(TEXT)
    assert asked and first not in asked  # the finished chunk is not asked again
    assert " ".join(s for r in results for _, s in r["sentences"]) == TEXT
    assert not list(tmp_path.glob("punctuate_cache_*"))
    assert not (tmp_path / "punctuate_cuts.json").exists()


async def test_punctuation_runs_on_a_named_model(monkeypatch, fake_model):
    monkeypatch.setenv("LLM_BACKEND", "openai:gpt-5.6-terra")
    model = fake_model("<output>From the chat model.</output>")
    assert await _create_llm_punct()("from the chat model") == "From the chat model."
    assert len(model.asked) == 1


@pytest.mark.live
@pytest.mark.parametrize("backend", ["claude-code", "codex"])
async def test_punctuation_on_a_live_subscription(monkeypatch, backend):
    monkeypatch.setenv("LLM_BACKEND", backend)
    text = await _create_llm_punct()("so today we will learn how to write tests they help a lot")
    assert "." in text
