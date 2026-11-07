"""An English article read with its translation, as HTML: a sentence shows its translation when it
is clicked, and under each paragraph a box opens on the advanced terms it uses."""

import base64
import hashlib
import html
import urllib.request
from collections.abc import Iterable, Iterator
from dataclasses import replace
from functools import partial
from itertools import count
from pathlib import Path

from dominate import document, util
from dominate.tags import (
    a,
    details,
    div,
    figure,
    footer,
    h1,
    h2,
    h3,
    h4,
    h5,
    h6,
    img,
    input_,
    label,
    p,
    script,
    span,
    style,
    summary,
    table,
    tbody,
    td,
    th,
    thead,
    tr,
)
from pydantic import BaseModel

from easyt import log
from easyt.article import Article, Block
from easyt.backend import open_backend
from easyt.languages import language_name
from easyt.notify import send_document
from easyt.parser import XmlItemParser
from easyt.pipeline.page import page_head
from easyt.prompts import load_prompt
from easyt.stages.concurrency import run_concurrently
from easyt.stages.punctuate import SentSplitter
from easyt.stages.translate import Translator
from easyt.utils import spans
from easyt.utils.threads import checkpoint, in_thread

# Characters of the article that one translation call, and one term call, takes.
CHUNK_SIZE = 4 * 1000
# A larger image keeps its URL: the page goes to the chat, which takes 50 MB, and news images
# are far smaller.
MAX_IMAGE_BYTES = 5 * 2**20
# What a browser sends an image server: NYT's answers AVIF then, a tenth of the size of its PNG,
# and Wikipedia's refuses Python's own User-Agent.
_IMAGE_HEADERS = {
    "Accept": "image/avif,image/webp,image/*,*/*;q=0.8",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Version/18.0 Safari/605.1.15"
    ),
}

_HEADINGS = {"h1": h1, "h2": h2, "h3": h3, "h4": h4, "h5": h5, "h6": h6}
# The details of the front matter that the page shows above the article.
_SHOWN_DETAILS = ("site", "author", "published")

_CSS = """
:root {
  --bg: #fff; --fg: #222; --muted: #767676; --line: #e5e5e5; --box: #f6f6f6;
  --mark: #ffefa6; --tip-bg: #2a333e; --tip-fg: #f3f5f7; --accent: #4f74a3;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #17191d; --fg: #e3e3e3; --muted: #9b9b9b; --line: #393e46; --box: #20242a;
    --mark: #4d4422; --tip-bg: #e6ebf0; --tip-fg: #17191d; --accent: #8db0dc;
  }
}
body {
  max-width: var(--measure); margin: 0 auto; padding: 1.5em 1em 4em;
  background: var(--bg); color: var(--fg);
  font-family: var(--english); line-height: 1.75;
}
h1, h2, h3 { line-height: 1.35; }
a { color: var(--accent); }
.meta { margin-bottom: 0; color: var(--muted); font-size: .85rem; }
.item::before { content: "• "; }
figure { margin: 1.5em 0; }
figure img { max-width: 100%; height: auto; border-radius: 4px; }
.table { margin: 1.5em 0; overflow-x: auto; }
table { border-collapse: collapse; font-size: .9rem; line-height: 1.5; }
th, td {
  padding: .4em .8em; border-bottom: 1px solid var(--line);
  text-align: left; vertical-align: top; font-variant-numeric: tabular-nums;
}
th { border-bottom-width: 2px; }
.s { border-radius: 3px; cursor: pointer; }
.s.on, .t:checked + .s { background: var(--mark); }
/* A sentence's translation: the script's tooltip or, where scripts do not run, as in Telegram's
   preview on iPhone, a box under the sentence whose checkbox a tap ticked. */
#tip, .t:checked + .s::after {
  padding: .5em .75em; border-radius: 6px; background: var(--tip-bg); color: var(--tip-fg);
  font-family: var(--korean); font-size: .9rem; line-height: 1.6;
}
#tip {
  position: absolute; z-index: 1; box-sizing: border-box; max-width: min(30em, calc(100vw - 16px));
  box-shadow: 0 4px 14px rgba(0, 0, 0, .25); pointer-events: none;
}
.t { display: none; }
.t:checked + .s::after { content: attr(data-tr); display: block; margin: .35em 0 .5em; }
.terms {
  margin: -.5em 0 1.5em; border: 1px solid var(--line); border-radius: 6px;
  background: var(--box); font-family: var(--korean); font-size: .85rem; line-height: 1.6;
}
.terms summary { padding: .45em .8em; color: var(--muted); cursor: pointer; }
.term { padding: .6em .9em; border-top: 1px solid var(--line); }
.term h4 { margin: 0 0 .2em; font-size: 1rem; }
.term .pron { margin-left: .5em; color: var(--muted); font-weight: normal; }
.term p { margin: .15em 0; }
.term .ex, .term .ety { margin-top: .5em; }
.term .tr { color: var(--accent); }
.term .ety { color: var(--muted); }
footer { margin-top: 3em; font-size: .85rem; }
"""

_SCRIPT = """
(() => {
  // The tooltip takes over from the checkboxes, whose clicks would close it as they come.
  for (const box of document.querySelectorAll("input.t")) box.remove();
  const tip = document.getElementById("tip");
  const page = document.documentElement;
  let shown = null;

  function show(s) {
    hide();
    shown = s;
    s.classList.add("on");
    tip.textContent = s.dataset.tr;
    tip.style.left = tip.style.top = "0px";  // measured at full width, then placed
    tip.hidden = false;
    const lines = s.getClientRects();
    const first = lines[0], last = lines[lines.length - 1];
    const room = page.clientWidth - tip.offsetWidth - 8;
    let top = last.bottom + 6;
    if (top + tip.offsetHeight > innerHeight && first.top > tip.offsetHeight + 6) {
      top = first.top - tip.offsetHeight - 6;
    }
    tip.style.left = Math.max(8, Math.min(first.left, room)) + scrollX + "px";
    tip.style.top = top + scrollY + "px";
  }

  function hide() {
    shown?.classList.remove("on");
    shown = null;
    tip.hidden = true;
  }

  document.addEventListener("click", (e) => {
    if (!getSelection().isCollapsed) return;  // a drag that selected text, not a click to read
    const s = e.target.closest(".s");
    s && s !== shown ? show(s) : hide();
  });
  // A phone's browser resizes the window as a scroll hides its address bar, which moves no
  // sentence: only a new width does.
  let width = page.clientWidth;
  addEventListener("resize", () => {
    if (page.clientWidth !== width) hide();
    width = page.clientWidth;
  });
})();
"""


class ArticleTerm(BaseModel):
    paragraph: int  # the id of the first paragraph that uses it
    word: str
    pronunciation: str = ""
    definition: str
    examples: str | None = None  # English lines, each followed by its translation
    etymology: str | None = None


def _term_parser() -> XmlItemParser:
    return XmlItemParser("term", value=ArticleTerm)


def split_sentences(blocks: list[Block]) -> list[list[str]]:
    """The sentences of each of `blocks`, as SaT finds them. A heading stays whole, as SaT takes
    its capitalized words for the starts of sentences."""
    splitter = SentSplitter()
    return [[b.text] if b.kind in _HEADINGS else splitter.split(b.text) for b in blocks]


def embed_images(blocks: list[Block]) -> list[Block]:
    """`blocks`, each image put in the page as a data URL, as a phone's preview of a page, such as
    Telegram's on iPhone, loads nothing from the internet. One that does not download keeps its
    URL, which a computer's browser still loads."""
    embedded = []
    for block in blocks:
        if block.kind == "img" and block.src.startswith(("http://", "https://")):
            checkpoint()
            try:
                block = replace(block, src=_data_url(block.src))
            except Exception as e:
                log.warning(f"An image stays a link: {e}: {block.src}")
        embedded.append(block)
    return embedded


def _data_url(url: str) -> str:
    """The image at `url` as a data URL; raises if it does not download, is not an image, or is
    larger than MAX_IMAGE_BYTES."""
    request = urllib.request.Request(url, headers=_IMAGE_HEADERS)
    with urllib.request.urlopen(request, timeout=20) as response:
        kind, data = response.headers.get_content_type(), response.read(MAX_IMAGE_BYTES + 1)
    if not kind.startswith("image/"):
        raise ValueError(f"not an image but {kind}")
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError(f"larger than {MAX_IMAGE_BYTES // 2**20} MB")
    return f"data:{kind};base64,{base64.b64encode(data).decode()}"


def chunk_texts(sizes: list[int], size: int) -> list[list[int]]:
    """The indices of texts of `sizes` characters, in runs of about even length that is at most
    about `size`, each text in the run where most of it falls."""
    total = sum(sizes)
    step = total / max(1, -(-total // size))
    runs: list[list[int]] = [[]]
    done = 0
    for i, n in enumerate(sizes):
        if runs[-1] and done + n / 2 > step * len(runs):
            runs.append([])
        runs[-1].append(i)
        done += n
    return runs


class Pipeline_article:
    def __init__(self, article: Article, html_path: Path, target_lang: str) -> None:
        self.article = article
        self.html_path = html_path
        self.outdir = html_path.parent
        self.target_lang = target_lang
        self.translator = Translator("en", target_lang, prompt="article.translate")
        self.backend = open_backend("Term extraction")
        self.terms_prompt = load_prompt("article.terms").partial(
            target_lang=language_name(target_lang)
        )

    async def run(self) -> Path:
        blocks = [b for b in self.article.blocks if b.prose]
        texts = [b.text for b in blocks]
        with spans.span("Sentences") as s:
            sentences = await in_thread(split_sentences, blocks)
            s.result = f"{sum(map(len, sentences))} sentences"

        translate, extract = [], []
        for chunk in chunk_texts([len(t) for t in texts], CHUNK_SIZE):
            sents = [sentence for i in chunk for sentence in sentences[i]]
            paragraphs = {i + 1: texts[i] for i in chunk}
            translate.append(
                partial(self.translator.translate, sents, self._cache_path("translate", sents))
            )
            extract.append(
                partial(self._extract_terms, paragraphs, self._cache_path("terms", paragraphs))
            )
        # One stage, so that the calls of both kinds run at once, LLM_CONCURRENCY at a time.
        with spans.span("Translation and terms") as s:
            answers = await run_concurrently(translate + extract)
            translated = iter([t["target"] for found in answers[: len(translate)] for t in found])
            terms = _by_paragraph(t for found in answers[len(translate) :] for t in found)
            s.result = f"{sum(map(len, terms.values()))} terms"

        article = self.article
        if images := [b for b in article.blocks if b.kind == "img"]:
            with spans.span("Images") as s:
                article = replace(article, blocks=await in_thread(embed_images, article.blocks))
                inside = sum(b.src.startswith("data:") for b in article.blocks if b.kind == "img")
                s.result = f"{inside} of {len(images)} in the page"

        pairs = [[(source, next(translated)) for source in sents] for sents in sentences]
        self.html_path.write_text(generate_html(article, pairs, terms), encoding="utf-8")
        log.success(f"Article generated: {self.html_path}")
        await send_document(self.html_path, _caption(self.article))
        return self.html_path

    def _cache_path(self, kind: str, inputs: object) -> Path:
        """Named by a digest of the call's inputs, as an article sent again may have changed."""
        digest = hashlib.sha256(repr(inputs).encode()).hexdigest()[:8]
        return self.outdir / f"{kind}_cache_{self.target_lang}_{digest}.txt"

    async def _extract_terms(
        self, paragraphs: dict[int, str], cache_path: Path
    ) -> list[ArticleTerm]:
        """The terms of `paragraphs`, by their ids, each with the id of the first that uses it."""
        parser = _term_parser()
        cached = cache_path.read_text(encoding="utf-8") if cache_path.exists() else ""
        # A cache without terms, such as one an empty answer left, is asked again.
        if not parser.feed(cached):
            parser = _term_parser()
            # Escaped like the translator's segments, as the parser unescapes the answer.
            listed = "\n".join(
                f'<paragraph id="{i}">{html.escape(t, quote=False)}</paragraph>'
                for i, t in paragraphs.items()
            )
            system, human = self.terms_prompt.format(paragraphs=listed)
            parser.feed(await self.backend.ask(system, human))
            cache_path.write_text(parser.text, encoding="utf-8")

        if not (terms := parser.close()):
            log.warning("Term extraction: the answer has no terms, so the next run asks again")
        return terms


def _by_paragraph(terms: Iterable[ArticleTerm]) -> dict[int, list[ArticleTerm]]:
    """`terms` by the paragraph they are listed for; a term listed again, as another chunk may,
    stays where it came first."""
    seen: set[str] = set()
    grouped: dict[int, list[ArticleTerm]] = {}
    for term in terms:
        if (key := term.word.lower()) not in seen:
            seen.add(key)
            grouped.setdefault(term.paragraph, []).append(term)
    return grouped


def _caption(article: Article) -> str:
    """The start of the article's description, for the caption of its file in the chat."""
    description = article.details.get("description", "")
    return description[:150] + "..." if len(description) > 150 else description


def generate_html(
    article: Article,
    sentences: list[list[tuple[str, str]]],
    terms: dict[int, list[ArticleTerm]],
) -> str:
    """The page of `article`: the sentences of each block of prose with their translations, and
    under a block the terms listed for it, the blocks of prose counted from 1; images and tables
    as they are."""
    doc = document(title=article.title, lang="en")
    with doc.head:
        page_head()
        style(util.raw(_CSS))

    with doc:
        if shown := " · ".join(filter(None, map(article.details.get, _SHOWN_DETAILS))):
            p(shown, cls="meta")
        numbered = iter(enumerate(sentences, 1))
        boxes = count(1)
        for block in article.blocks:
            if block.kind == "img":
                figure(img(src=block.src, alt=block.text, loading="lazy"))
            elif block.kind == "table":
                _table(block.rows)
            else:
                paragraph, pairs = next(numbered)
                _text_block(block, pairs, boxes)
                if paragraph in terms:
                    _terms_box(terms[paragraph])
        if url := article.details.get("url"):
            footer(a("Original article", href=url, target="_blank"))
        div(id="tip", hidden=True)
        script(util.raw(_SCRIPT))

    return doc.render()


def _text_block(block: Block, pairs: list[tuple[str, str]], boxes: Iterator[int]) -> None:
    """The block's sentences, each with a checkbox that its label, the sentence, ticks when tapped:
    the CSS shows the translation of a ticked one where the page's script does not run."""
    with _HEADINGS.get(block.kind, p)(cls="item" if block.kind == "item" else None):
        for i, (source, target) in enumerate(pairs):
            if i:
                util.text(" ")
            # A sentence the answer left out keeps its source as its translation.
            if target == source:
                util.text(source)
                continue
            box = f"t{next(boxes)}"
            input_(type="checkbox", id=box, cls="t")
            label(source, fr=box, cls="s", data_tr=target)


def _table(rows: tuple[tuple[str, ...], ...]) -> None:
    """A table as the article has it, in a box that scrolls sideways when the table is wider."""
    head, *body = rows
    with div(cls="table"), table():
        with thead(), tr():
            for cell in head:
                th(cell)
        with tbody():
            for row in body:
                with tr():
                    for cell in row:
                        td(cell)


def _terms_box(terms: list[ArticleTerm]) -> None:
    with details(cls="terms"):
        summary(" · ".join(t.word for t in terms))
        for term in terms:
            with div(cls="term"):
                with h4(term.word):
                    if term.pronunciation:
                        span(term.pronunciation, cls="pron")
                p(term.definition)
                examples = [line.strip() for line in (term.examples or "").splitlines()]
                for i, line in enumerate(filter(None, examples)):
                    p(line, cls="tr" if i % 2 else "ex")
                if term.etymology:
                    p(term.etymology, cls="ety")
