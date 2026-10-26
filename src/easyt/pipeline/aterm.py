from functools import partial
from pathlib import Path

from dominate import document
from dominate.tags import *
from dominate.util import raw
from pydantic import BaseModel

from easyt import log
from easyt.backend import open_backend
from easyt.parser import XmlItemParser
from easyt.pipeline.base import Pipeline_base
from easyt.prompts import load_prompt
from easyt.stages.concurrency import run_concurrently
from easyt.utils import spans
from easyt.youtube import Loader

_CSS = """
html { font-size: 28px; }
body { font-family: Arial, sans-serif; padding: 20px; line-height: 1.6; }
h1 { border-bottom: 2px solid #333; padding-bottom: 5px; }
section { margin-bottom: 40px; }
#transcript { background-color: #f5f5f5; padding: 20px; border-radius: 5px; }
#transcript p { margin: 8px 0; }
article.term { border: 1px solid #ccc; padding: 15px; border-radius: 5px; margin-bottom: 20px; }
#advanced-terms article.term:nth-child(odd) { background-color: #f8f0ff; }
#advanced-terms article.term:nth-child(even) { background-color: #f0f8ff; }
article.term h2[data-pron] { margin-top: 0; }
article.term h2[data-pron]::after {
    content: " " attr(data-pron);
    font-size: 0.9em;
    font-weight: normal;
    color: #666;
}
article.term p { margin: 5px 0; }
article.term p.translation { color: #7a96b9; }
"""


class AdvancedTerm(BaseModel):
    word: str
    pronunciation: str
    definition: str
    excerpt: str
    additional_examples: str | None = None
    etymology: str | None = None


def _term_parser() -> XmlItemParser:
    return XmlItemParser("term", value=AdvancedTerm)


class Pipeline_aterm(Pipeline_base):
    def __init__(self, transcript: list[dict], loader: Loader) -> None:
        super().__init__(transcript, loader)
        self.backend = open_backend("Term extraction")

    async def run(self) -> Path:
        input = " ".join([sub["text"] for sub in self.input_transcript])
        punct_results = await self.punct.run(input)
        chunks = [[p for _, p in src["sentences"]] for src in punct_results]
        advanced_terms = await self._extract_terms(chunks)

        # generate html
        html_path = self.loader.video_path.with_suffix(".html")
        sentences = [sentence for chunk in chunks for sentence in chunk]
        html_path.write_text(self.generate_html(sentences, advanced_terms))

        # send html to telegram
        await self.send_document_to_telegram(html_path)
        return html_path

    async def _extract_terms(self, chunks: list[list[str]]) -> list[AdvancedTerm]:
        """The terms in the chunks of sentences, each chunk asked at once, in the chunks' order."""
        with spans.span("Term extraction") as s:
            found = await run_concurrently(
                [
                    partial(
                        self._extract_advanced_terms,
                        self.loader.outdir / f"aterm_cache_{i}.txt",
                        "\n".join(chunk),
                    )
                    for i, chunk in enumerate(chunks, 1)
                ]
            )
            # a term found in more than one chunk keeps its first definition
            unique = {}
            for term in (t for terms in found for t in terms):
                unique.setdefault(term.word, term)
            s.result = f"{len(unique)} terms"
        return list(unique.values())

    async def _extract_advanced_terms(self, cache_path: Path, chunk: str) -> list[AdvancedTerm]:
        parser = _term_parser()
        cached = cache_path.read_text(encoding="utf-8") if cache_path.exists() else ""
        # A cache without terms, such as one an empty answer left, is asked again.
        if not parser.feed(cached):
            parser = _term_parser()
            system, step1 = load_prompt("aterm.step1").format(
                input_transcript=f"...\n{chunk.strip()}\n..."
            )
            [final_step] = load_prompt("aterm.final_step").format()
            async with self.backend.session(system) as session:
                # The terms step 1 lists stay in the history for the final step to define.
                await session.ask(step1)
                parser.feed(await session.ask(final_step))
            cache_path.write_text(parser.text, encoding="utf-8")

        if not (terms := parser.close()):
            log.warning("Term extraction: the answer has no terms, so the next run asks again")
        return terms

    def generate_html(self, sentences: list[str], advanced_terms: list[AdvancedTerm]):
        doc = document(title=self.loader.title)

        with doc.head:
            meta(charset="UTF-8")
            style(raw(_CSS))

        with doc:
            with section(id="transcript"):
                h1("Transcript")
                for sentence in sentences:
                    p(sentence)

            with section(id="advanced-terms"):
                h1("Advanced Terms")
                total = len(advanced_terms)
                for idx, aterm in enumerate(advanced_terms, 1):
                    with article(cls="term"):
                        h2(f"{idx}/{total} ", aterm.word, data_pron=aterm.pronunciation)

                        p(aterm.definition)
                        if aterm.etymology:
                            p(strong("Etymology: "), aterm.etymology)

                        _paired_lines("Excerpt", aterm.excerpt)
                        if aterm.additional_examples:
                            _paired_lines("Examples", aterm.additional_examples)

            with div():
                a(f"Original video: {self.loader.title}", href=self.loader.url, target="_blank")

        return doc.render()


def _paired_lines(heading: str, text: str) -> None:
    """`text` under `heading`, each English line followed by its Korean translation."""
    h4(heading)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for i, line in enumerate(lines):
        p(line, cls="translation" if i % 2 else None)
