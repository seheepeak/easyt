import asyncio
from pathlib import Path
from unittest.mock import Mock

import pytest

import easyt.pipeline.aterm
from easyt.parser import XmlItemParser
from easyt.pipeline.aterm import AdvancedTerm, Pipeline_aterm
from easyt.prompts import load_prompt
from easyt.youtube import Loader

CHUNK = "People decry it."
TERMS = (
    "<advanced_terms>\n<term>\n<word>decry</word>\n<pronunciation>/dɪˈkraɪ/</pronunciation>\n"
    "<definition>to criticize openly</definition>\n"
    "<excerpt>\nPeople decry it.\nLa gente lo critica.\n</excerpt>\n</term>\n</advanced_terms>"
)


@pytest.fixture
def mock_loader():
    loader = Mock(spec=Loader)
    loader.outdir = Path("outputs/test")
    loader.video_path = Path("outputs/test/Natalie Portman.mp4")
    loader.title = "NATALIE PORTMAN: Don't Doubt Yourself"
    loader.url = "https://www.youtube.com/watch?v=example"
    return loader


class TestPipelineAterm:
    async def test_the_html_shows_the_sentences_and_each_term_with_its_translation(
        self, fake_backend, mock_loader
    ):
        fake_backend(easyt.pipeline.aterm, run=None)  # nothing is asked
        parser = XmlItemParser("term", value=AdvancedTerm)
        parser.feed(TERMS)
        pipeline = Pipeline_aterm(transcript=[], loader=mock_loader)
        html = pipeline.generate_html([CHUNK], parser.close())
        assert f"<p>{CHUNK}</p>" in html
        assert "to criticize openly" in html
        assert '<p class="translation">La gente lo critica.</p>' in html

    async def test_chunks_are_extracted_at_once_and_merged_in_order(
        self, fake_backend, monkeypatch, mock_loader
    ):
        monkeypatch.setenv("LLM_CONCURRENCY", "3")
        fake_backend(easyt.pipeline.aterm, run=None)  # nothing is asked
        pipeline = Pipeline_aterm(transcript=[], loader=mock_loader)
        running, most, caches = 0, 0, {}

        async def extract(cache_path, chunk):
            nonlocal running, most
            running += 1
            most = max(most, running)
            caches[chunk] = cache_path.name
            await asyncio.sleep(0.03 if chunk == "first" else 0.01)  # the first chunk ends last
            running -= 1
            return [
                AdvancedTerm(word=word, pronunciation="", definition=chunk, excerpt="")
                for word in ("shared", chunk)
            ]

        monkeypatch.setattr(pipeline, "_extract_advanced_terms", extract)
        terms = await pipeline._extract_terms([["first"], ["second"], ["third"], ["fourth"]])

        assert [(t.word, t.definition) for t in terms] == [
            ("shared", "first"),  # the first chunk's definition, though that chunk ended last
            ("first", "first"),
            ("second", "second"),
            ("third", "third"),
            ("fourth", "fourth"),
        ]
        assert caches == {
            "first": "aterm_cache_1.txt",
            "second": "aterm_cache_2.txt",
            "third": "aterm_cache_3.txt",
            "fourth": "aterm_cache_4.txt",
        }
        assert most == 3

    async def test_the_final_step_defines_the_terms_in_one_session(
        self, fake_answers, mock_loader, tmp_path
    ):
        sessions = fake_answers(easyt.pipeline.aterm, "decry", TERMS)
        pipeline = Pipeline_aterm(transcript=[], loader=mock_loader)
        terms = await pipeline._extract_advanced_terms(tmp_path / "aterm_cache_1.txt", CHUNK)
        assert [t.word for t in terms] == ["decry"]
        [expected] = load_prompt("aterm.final_step").format()
        [[step1, final_step]] = sessions
        assert CHUNK in step1
        assert final_step == expected

    async def test_an_extracted_chunk_is_read_back_from_its_cache(
        self, fake_answers, mock_loader, tmp_path
    ):
        sessions = fake_answers(easyt.pipeline.aterm, "decry", TERMS)
        pipeline = Pipeline_aterm(transcript=[], loader=mock_loader)
        cache_path = tmp_path / "aterm_cache_1.txt"
        extracted = await pipeline._extract_advanced_terms(cache_path, CHUNK)
        assert await pipeline._extract_advanced_terms(cache_path, CHUNK) == extracted
        assert len(sessions) == 1

    async def test_a_cache_without_terms_is_asked_again(self, fake_answers, mock_loader, tmp_path):
        cache_path = tmp_path / "aterm_cache_1.txt"
        cache_path.write_text("")
        sessions = fake_answers(easyt.pipeline.aterm, "decry", TERMS)
        pipeline = Pipeline_aterm(transcript=[], loader=mock_loader)
        terms = await pipeline._extract_advanced_terms(cache_path, CHUNK)
        assert [t.word for t in terms] == ["decry"]
        assert len(sessions) == 1
