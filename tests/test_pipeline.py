import asyncio
import re
from datetime import timedelta
from types import SimpleNamespace

import pytest
import srt

import easyt.stages.line_break
import easyt.stages.translate
from easyt.pipeline.standard import Pipeline, do_sent_break
from easyt.stages.line_break import LLMLineBreaker

PUNCTUATED = [{"sentences": [[0, "Hola."], [1, "Mucho gusto."]]}]


def _pipeline(tmp_path, source_lang, target_lang):
    loader = SimpleNamespace(outdir=tmp_path)
    return Pipeline([], loader, source_lang=source_lang, target_lang=target_lang)


async def test_a_video_already_in_the_target_language_is_not_translated(fake_backend, tmp_path):
    sessions = fake_backend(easyt.stages.translate, run=None)  # never asked
    translated = await _pipeline(tmp_path, "es", "es")._translate(PUNCTUATED)
    assert [(t["source"], t["target"]) for t in translated] == [
        ("Hola.", "Hola."),
        ("Mucho gusto.", "Mucho gusto."),
    ]
    assert sessions == []


async def test_a_video_in_another_language_is_translated_chunk_by_chunk(fake_backend, tmp_path):
    async def run(system, prompt):
        return '<segment id="1">Hello.</segment><segment id="2">Nice to meet you.</segment>'

    sessions = fake_backend(easyt.stages.translate, run)
    translated = await _pipeline(tmp_path, "es", "en")._translate(PUNCTUATED)
    assert [t["target"] for t in translated] == ["Hello.", "Nice to meet you."]
    assert len(sessions) == 1


async def test_each_target_language_has_its_own_translation(fake_backend, tmp_path):
    async def run(system, prompt):
        return '<segment id="1">Hello.</segment><segment id="2">Nice to meet you.</segment>'

    sessions = fake_backend(easyt.stages.translate, run)
    await _pipeline(tmp_path, "es", "en")._translate(PUNCTUATED)
    await _pipeline(tmp_path, "es", "ja")._translate(PUNCTUATED)  # the same day's folder
    assert len(sessions) == 2


def test_a_line_length_below_one_fails_before_any_model_call(monkeypatch, tmp_path):
    monkeypatch.setenv("MAX_LINE_LEN", "0")
    with pytest.raises(ValueError, match="MAX_LINE_LEN must be 1 or more"):
        _pipeline(tmp_path, "en", "ko")


async def _subtitles(fake_backend, tmp_path, screens, start, duration):
    """The SRT a text of `screens` makes when the model answers with those screens."""

    async def run(system, prompt):
        return '<segment id="1">\n' + "\n---\n".join(screens) + "\n</segment>"

    fake_backend(easyt.stages.line_break, run)
    out = tmp_path / "video.srt"
    translated = [{"text": " ".join(screens), "start": start, "duration": duration}]
    await do_sent_break(out, translated, LLMLineBreaker("en"))
    return list(srt.parse(out.read_text()))


async def test_each_screen_becomes_one_subtitle_timed_by_its_letters(fake_backend, tmp_path):
    first = "Today we write tests,"  # 17 of the 36 letters
    second = "and see why they matter."
    subtitles = await _subtitles(fake_backend, tmp_path, [first, second], 10.0, 7.4)

    assert [s.content for s in subtitles] == [first, second]
    assert subtitles[0].start == timedelta(seconds=10)
    # A second each, then the 5.4 seconds left shared out by letters.
    assert subtitles[0].end.total_seconds() == pytest.approx(10 + 1 + 5.4 * 17 / 36, abs=0.01)
    assert subtitles[1].end.total_seconds() == pytest.approx(17.4, abs=0.01)


async def test_every_screen_stays_up_long_enough_to_read(fake_backend, tmp_path):
    screens = ["Yes.", "♪", "and see why they matter."]
    subtitles = await _subtitles(fake_backend, tmp_path, screens, 0.0, 6.0)

    assert [s.content for s in subtitles] == screens
    assert all((s.end - s.start).total_seconds() >= 1.0 for s in subtitles)
    assert subtitles[-1].end.total_seconds() == pytest.approx(6.0, abs=0.01)


async def test_chunks_are_translated_at_once_and_kept_in_order(fake_backend, monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_CONCURRENCY", "3")
    running, most = 0, 0

    async def run(system, prompt):
        nonlocal running, most
        running += 1
        most = max(most, running)
        source = re.search(r'<segment id="1">(.*?)</segment>', prompt)[1]
        await asyncio.sleep(0.03 if source == "first" else 0.01)  # the first chunk ends last
        running -= 1
        return f'<segment id="1">{source.upper()}</segment>'

    fake_backend(easyt.stages.translate, run)
    punctuated = [{"sentences": [[0, word]]} for word in ("first", "second", "third", "fourth")]
    translated = await _pipeline(tmp_path, "en", "ko")._translate(punctuated)

    assert [(t["target"], t["chunk_idx"]) for t in translated] == [
        ("FIRST", 0),
        ("SECOND", 1),
        ("THIRD", 2),
        ("FOURTH", 3),
    ]
    assert most == 3


async def test_texts_are_broken_into_lines_ten_a_call_at_once(fake_backend, monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_CONCURRENCY", "2")
    running, most, calls = 0, 0, 0

    async def run(system, prompt):
        nonlocal running, most, calls
        running += 1
        calls += 1
        most = max(most, running)
        await asyncio.sleep(0.01)
        running -= 1
        segments = re.findall(r'<segment id="(\d+)" lines="[^"]*">(.*?)</segment>', prompt)
        return "".join(
            f'<segment id="{i}">\n' + text.replace(", ", ",\n") + "\n</segment>"
            for i, text in segments
        )

    fake_backend(easyt.stages.line_break, run)
    texts = [f"Today we write test {n}, and see why it matters." for n in range(25)]
    translated = [{"text": t, "start": float(n), "duration": 1.0} for n, t in enumerate(texts)]
    out = tmp_path / "video.srt"
    await do_sent_break(out, translated, LLMLineBreaker("en"))

    subtitles = list(srt.parse(out.read_text()))
    assert [s.content.replace(",\n", ", ") for s in subtitles] == texts
    assert calls == 3 and most == 2
