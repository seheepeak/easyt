"""How an article sent as Markdown is read, and the page made of it."""

import email.message
import io
import re
import urllib.error
import urllib.request

import pytest

import easyt.pipeline.article
import easyt.stages.translate
from easyt.article import Block, article_part, read_article
from easyt.pipeline.article import Pipeline_article, chunk_texts, embed_images, split_sentences
from easyt.stages.punctuate import SentSplitter

ARTICLE = """---
site: "The Daily Paper"
author: "Jane Doe"
published: "2026-11-06 19:02"
url: "https://example.com/stocks"
description: "A bust would hurt."
---

# Opinion | Stocks Fell. Is the Economy Next?

### Stocks Had a Terrible Day

![A chart.](https://example.com/chart.png)

*Credit...Illustration by _The Paper_*

| Year | Fall |
| --- | --- |
| 2008 | **38%** |

We’ve been here before. The damage could run
deep, as a [boom](https://example.com/boom) goes bust.

- First item
- Second item

* * *

<!-- EOF -->
"""

# The terms of the paragraph that starts "We’ve been here before", the fourth of the blocks
# that are not images or tables.
TERMS = """<terms>
<term>
<paragraph>4</paragraph>
<word>run deep</word>
<pronunciation>/rʌn diːp/</pronunciation>
<definition>깊이 퍼져 있다</definition>
<examples>
Old rivalries run deep.
오래된 경쟁심은 뿌리가 깊다.
</examples>
</term>
</terms>"""


class SplitsAfterStops:
    """Cuts after each period."""

    def split(self, text):
        return re.split(r"(?<=\.)\s+", text)


class FakeResponse(io.BytesIO):
    def __init__(self, kind, data):
        super().__init__(data)
        self.headers = email.message.Message()
        self.headers["Content-Type"] = kind


@pytest.fixture(autouse=True)
def image_server(monkeypatch):
    """What an image URL answers, by URL: a (media type, bytes) pair or an error to raise, and
    else a PNG of 3 bytes; no test goes online."""
    answers = {}

    def urlopen(request, timeout):
        assert request.get_header("Accept").startswith("image/avif")  # a tenth of a PNG's size
        assert request.get_header("User-agent").startswith("Mozilla/")  # as Wikipedia asks
        answer = answers.get(request.full_url, ("image/png", b"PNG"))
        if isinstance(answer, Exception):
            raise answer
        return FakeResponse(*answer)

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    return answers


@pytest.fixture
def sat(monkeypatch):
    monkeypatch.setattr(SentSplitter, "_sat", staticmethod(SplitsAfterStops))


@pytest.fixture
def sent(monkeypatch):
    """The files the pipeline sends to the chat, with their captions."""
    files = []

    async def send(path, caption):
        files.append((path, caption))

    monkeypatch.setattr(easyt.pipeline.article, "send_document", send)
    return files


async def _translate(system, prompt):
    """Each segment, as its translation, after `KO `."""
    segments = re.findall(r'<segment id="(\d+)">(.*?)</segment>', prompt)
    return "\n".join(f'<segment id="{i}">KO {text}</segment>' for i, text in segments)


@pytest.mark.parametrize(
    ("text", "part"),
    [
        (ARTICLE, "whole"),
        (ARTICLE.split("We’ve")[0], "first"),
        ("We’ve" + ARTICLE.split("We’ve")[1], "last"),
        ("A part between the first and the last, as any other message.", None),
    ],
)
def test_a_message_holds_a_whole_article_or_a_part_of_one_telegram_cut(text, part):
    assert article_part(text) == part


def test_the_front_matter_holds_the_details_and_the_first_h1_is_the_title():
    article = read_article(ARTICLE)
    assert article.title == "Opinion | Stocks Fell. Is the Economy Next?"
    assert article.details == {
        "site": "The Daily Paper",
        "author": "Jane Doe",
        "published": "2026-11-06 19:02",
        "url": "https://example.com/stocks",
        "description": "A bust would hurt.",
    }


def test_the_article_is_its_headings_images_and_paragraphs_in_plain_text():
    assert read_article(ARTICLE).blocks == [
        Block("h1", "Opinion | Stocks Fell. Is the Economy Next?"),
        Block("h3", "Stocks Had a Terrible Day"),
        Block("img", "A chart.", "https://example.com/chart.png"),
        Block("p", "Credit...Illustration by The Paper"),
        Block("table", rows=(("Year", "Fall"), ("2008", "38%"))),
        Block("p", "We’ve been here before. The damage could run deep, as a boom goes bust."),
        Block("item", "First item"),
        Block("item", "Second item"),
    ]


def test_a_file_may_leave_out_the_front_matter_and_the_eof_line():
    article = read_article("# Title\r\n\r\nWords.\r\n")  # as a Windows editor saved it
    assert (article.title, article.details) == ("Title", {})
    assert article.blocks == [Block("h1", "Title"), Block("p", "Words.")]


@pytest.mark.parametrize(
    "text",
    [
        '---\nsite: "The Daily Paper"\n---\n\n![A chart.](https://example.com/chart.png)\n',
        '---\nsite: "The Daily Paper\n---\n\n# Title\n\nWords.\n',  # a quote left open
    ],
)
def test_an_article_of_no_text_or_of_broken_front_matter_is_refused(text):
    with pytest.raises(ValueError):
        read_article(text)


def test_a_numbered_item_keeps_its_number():
    blocks = read_article("# Title\n\n- A bullet\n1. First step\n2) Second step").blocks
    assert blocks[1:] == [
        Block("item", "A bullet"),
        Block("p", "1. First step"),
        Block("p", "2) Second step"),
    ]


def test_the_markdown_reads_as_commonmark_has_it():
    """Escapes, a URL with parentheses and an image in a link, as clippers write them; code is
    left out."""
    text = r"""# Title

He said \[the plan\] works, as [Foo](https://en.wikipedia.org/wiki/Foo_(bar)) shows.

[![A chart.](https://example.com/chart.png)](https://example.com/chart)

```
# install
pip install easyt
```"""
    assert read_article(text).blocks == [
        Block("h1", "Title"),
        Block("p", "He said [the plan] works, as Foo shows."),
        Block("img", "A chart.", "https://example.com/chart.png"),
    ]


def test_a_heading_stays_whole(sat):
    assert split_sentences([Block("h1", "Stocks Fell. Is the Economy Next?")]) == [
        ["Stocks Fell. Is the Economy Next?"]
    ]


def test_an_image_goes_in_the_page_and_one_that_does_not_download_stays_a_link(
    image_server, monkeypatch
):
    monkeypatch.setattr(easyt.pipeline.article, "MAX_IMAGE_BYTES", 4)
    image_server["https://x.org/page"] = ("text/html", b"<html>")
    image_server["https://x.org/big.png"] = ("image/png", b"12345")
    image_server["https://x.org/gone.png"] = urllib.error.URLError("Not Found")
    blocks = [
        Block("img", "fine", "https://x.org/fine.png"),
        Block("img", "page", "https://x.org/page"),
        Block("img", "big", "https://x.org/big.png"),
        Block("img", "gone", "https://x.org/gone.png"),
        Block("img", "inside", "data:image/gif;base64,R0lG"),
        Block("p", "Words."),
    ]
    assert [b.src for b in embed_images(blocks)] == [
        "data:image/png;base64,UE5H",  # b"PNG"
        "https://x.org/page",
        "https://x.org/big.png",
        "https://x.org/gone.png",
        "data:image/gif;base64,R0lG",
        "",
    ]


def test_paragraphs_are_chunked_evenly_and_whole():
    assert chunk_texts([400] * 20, 4000) == [list(range(10)), list(range(10, 20))]
    assert chunk_texts([3000, 3000, 2000], 4000) == [[0], [1, 2]]
    assert chunk_texts([100, 100], 4000) == [[0, 1]]


async def test_each_sentence_shows_its_translation_and_each_paragraph_its_terms(
    sat, sent, fake_backend, tmp_path
):
    async def terms(system, prompt):
        return TERMS

    translations = fake_backend(easyt.stages.translate, _translate)
    term_lists = fake_backend(easyt.pipeline.article, terms)
    html_path = await Pipeline_article(read_article(ARTICLE), tmp_path / "page.html", "ko").run()

    page = html_path.read_text(encoding="utf-8")
    # A sentence labels a checkbox, whose tick shows its translation where scripts do not run.
    assert re.search(
        r'<input class="t" id="(t\d+)" type="checkbox">\s*<label class="s" '
        r'data-tr="KO We’ve been here before." for="\1">We’ve been here before.</label>',
        page,
    )
    after = page.split("as a boom goes bust.</label>")[1]
    assert re.match(r'\s*</p>\s*<details class="terms">\s*<summary>run deep</summary>', after)
    assert '<p class="tr">오래된 경쟁심은 뿌리가 깊다.</p>' in page
    assert page.count("<details") == 1
    assert 'src="data:image/png;base64,UE5H"' in page  # the chart, inside the page
    assert "<th>Year</th>" in page and "<td>38%</td>" in page  # the table, as it is
    assert "Year" not in str(translations + term_lists)
    assert '<p class="meta">The Daily Paper · Jane Doe · 2026-11-06 19:02</p>' in page
    assert 'href="https://example.com/stocks"' in page
    assert sent == [(html_path, "A bust would hurt.")]  # the description, as the caption

    await Pipeline_article(read_article(ARTICLE), tmp_path / "page.html", "ko").run()
    assert (len(translations), len(term_lists)) == (1, 1)  # read back from their caches


async def test_an_article_that_changed_is_asked_again_in_the_same_folder(
    sat, sent, fake_backend, tmp_path
):
    async def terms(system, prompt):
        return TERMS

    translations = fake_backend(easyt.stages.translate, _translate)
    term_lists = fake_backend(easyt.pipeline.article, terms)
    await Pipeline_article(read_article(ARTICLE), tmp_path / "page.html", "ko").run()
    # Updated later that day: same site, same title, so the same folder.
    changed = read_article(ARTICLE.replace("We’ve been here before.", "We were here."))
    html_path = await Pipeline_article(changed, tmp_path / "page.html", "ko").run()

    assert (len(translations), len(term_lists)) == (2, 2)
    assert 'data-tr="KO We were here."' in html_path.read_text(encoding="utf-8")


async def test_a_term_that_two_chunks_list_is_explained_once_where_it_came_first(
    sat, sent, fake_backend, monkeypatch, tmp_path
):
    monkeypatch.setattr(easyt.pipeline.article, "CHUNK_SIZE", 40)

    async def terms(system, prompt):
        first = re.search(r'<paragraph id="(\d+)">', prompt)[1]
        return (
            f"<term><paragraph>{first}</paragraph><word>bust</word>"
            f"<definition>{first}</definition></term>"
        )

    fake_backend(easyt.stages.translate, _translate)
    term_lists = fake_backend(easyt.pipeline.article, terms)
    html_path = await Pipeline_article(read_article(ARTICLE), tmp_path / "page.html", "ko").run()

    page = html_path.read_text(encoding="utf-8")
    assert len(term_lists) > 1
    assert page.count("<summary>bust</summary>") == 1
    assert "<p>1</p>" in page  # the definition the first chunk gave, under the title
