"""An article sent as Markdown, as a web clipper writes it: YAML front matter of its details, such
as site, author, published, url and description, then the article under its `# title`, and an
`<!-- EOF -->` line last, which shows whether a message holds all of it."""

import re
from dataclasses import dataclass
from typing import Literal

import yaml
from markdown_it import MarkdownIt
from markdown_it.tree import SyntaxTreeNode

_FRONT_MATTER = re.compile(r"---\n(.*?)\n---[ \t]*(?:\n|\Z)", re.DOTALL)
_EOF = "<!-- EOF -->"
_MARKDOWN = MarkdownIt("commonmark").enable("table")


@dataclass(frozen=True)
class Block:
    kind: str  # "h1" to "h6", "p", "item" (bulleted), "img" or "table"
    text: str = ""  # an image's alt text
    src: str = ""  # an image's URL
    rows: tuple[tuple[str, ...], ...] = ()  # a table's cells, row by row, its header first

    @property
    def prose(self) -> bool:
        """Whether the page shows it with its translation, as it does all but images and tables."""
        return self.kind not in ("img", "table")


@dataclass(frozen=True)
class Article:
    title: str  # its first h1
    details: dict[str, str]  # its front matter, such as site, author, published, url, description
    blocks: list[Block]


def article_part(text: str) -> Literal["whole", "first", "last"] | None:
    """Which part of an article the message `text` is: "whole" with its front matter and its
    `<!-- EOF -->` line, or, as Telegram cuts a text longer than a message holds into parts,
    "first" with the front matter alone and "last" with the line alone. None with neither, as
    any other message, or a part between the first and the last."""
    text = _normalized(text)
    starts, ends = bool(_FRONT_MATTER.match(text)), text.endswith(_EOF)
    if starts and ends:
        return "whole"
    return "first" if starts else "last" if ends else None


def read_article(text: str) -> Article:
    """The article in `text`, whose front matter and `<!-- EOF -->` line a file may leave out.
    Raises ValueError if the front matter does not read as YAML, or there is no text."""
    text = _normalized(text).removesuffix(_EOF)
    details = {}
    if found := _FRONT_MATTER.match(text):
        details = _details(found[1])
        text = text[found.end() :]
    blocks = _blocks(text)
    if not any(b.prose for b in blocks):
        raise ValueError("This article has no text to read")
    title = next((b.text for b in blocks if b.kind == "h1"), "")
    return Article(title, details, blocks)


def _normalized(text: str) -> str:
    """`text` stripped, with the line ends of a file a Windows editor saved as Unix ones."""
    return text.replace("\r\n", "\n").strip()


def _details(front_matter: str) -> dict[str, str]:
    """The fields of `front_matter`, each as text, a list's items joined by commas."""
    try:
        fields = yaml.safe_load(front_matter)
    except yaml.YAMLError as e:
        raise ValueError(f"The front matter does not read as YAML: {e}") from e
    details = {}
    for key, value in (fields if isinstance(fields, dict) else {}).items():
        if isinstance(value, list):
            value = ", ".join(map(str, value))
        details[str(key)] = "" if value is None else str(value)
    return details


def _blocks(markdown: str) -> list[Block]:
    """The headings, paragraphs, images and tables of `markdown`, read as CommonMark with GitHub's
    tables. A list item is a paragraph of its own, a numbered one keeping its number, a quote's
    paragraphs read as any other, and a paragraph of images alone is those images. Code, rules
    and HTML are left out."""
    blocks: list[Block] = []

    def read(node: SyntaxTreeNode, kind: str = "p", number: str = "") -> None:
        for child in node.children:
            if child.type == "heading":
                blocks.append(Block(child.tag, _plain(child.children[0])))
            elif child.type == "paragraph":
                inline = child.children[0]
                if text := _plain(inline):
                    blocks.append(Block(kind, number + text))
                    number = ""
                else:  # images alone, linked or not
                    images = (n for n in inline.walk() if n.type == "image")
                    blocks.extend(Block("img", n.content, str(n.attrs["src"])) for n in images)
            elif child.type == "table":
                rows = tuple(
                    tuple(_plain(cell.children[0]) for cell in row.children)
                    for row in child.walk()
                    if row.type == "tr"
                )
                blocks.append(Block("table", rows=rows))
            elif child.type == "bullet_list":
                read(child, "item")
            elif child.type == "ordered_list":
                for item in child.children:
                    read(item, "p", f"{item.info}{item.markup} ")
            elif child.type in ("list_item", "blockquote"):
                read(child, kind)

    read(SyntaxTreeNode(_MARKDOWN.parse(markdown)))
    # A paragraph of marks alone, such as "◆ ◆ ◆", has nothing to translate.
    return [b for b in blocks if not b.prose or re.search(r"\w", b.text)]


def _plain(inline: SyntaxTreeNode) -> str:
    """The text of `inline` on one line: links as their text, and images and marks left out."""

    def text(node: SyntaxTreeNode) -> str:
        if node.type in ("text", "code_inline"):
            return node.content
        if node.type in ("softbreak", "hardbreak"):
            return " "
        if node.type == "image":
            return ""
        return "".join(map(text, node.children))

    return " ".join(text(inline).split())
