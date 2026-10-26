import functools
import html
import re
from xml.etree import ElementTree

from pydantic import BaseModel, ValidationError

from easyt import log

FieldSpec = str | tuple[str, type]
ValueSpec = list[FieldSpec] | type[BaseModel]


def _parse_field_spec(spec: FieldSpec) -> tuple[str, type]:
    if isinstance(spec, str):
        return spec, str
    return spec


def _coerce(value: str, typ: type):
    if typ in (int, float):
        try:
            return typ(value)
        except ValueError:
            return None
    return value


def _snippet(text: str, limit: int = 200) -> str:
    """Quote a fragment of model output for a warning."""
    return repr(text[:limit])


def _coerce_key(raw: str, typ: type):
    """Coerce an attribute value to the key type.

    A lost closing quote runs the value on into the attribute behind it (`1 speaker=`), so a
    numeric key can only be the first token.
    """
    if typ in (int, float):
        head = raw.split()
        return _coerce(head[0], typ) if head else None
    return _coerce(raw, typ)


# Attribute value, matched inside a tag's attribute region (which already stops at ">").
# Order is the rule: a proper close wins, then a mismatched one, then the region itself ends it.
_ATTR_VALUE = r"""(?: "([^"]*)"             # "..."
                    | '([^']*)'             # '...'
                    | "([^"']*)'            # mismatched close
                    | '([^"']*)"            # mismatched close
                    | "([^"]*)              # unclosed; the region ends it
                    | '([^']*)              # unclosed; the region ends it
                    | ([^"'=]*?)(?=["'])    # unopened but closed -- keeps spaces
                    | ([^\s"']*)            # bare
                  )"""


@functools.cache
def _loose_attr(name: str) -> re.Pattern:
    return re.compile(rf"\s{re.escape(name)}\s*=\s*{_ATTR_VALUE}", re.VERBOSE | re.IGNORECASE)


def _strict_attrs(attrs: str) -> dict[str, str] | None:
    """Read the attribute region as XML, or None if it is not well formed.

    The stdlib has no attribute parser, so the region rides on a throwaway element. Being
    all-or-nothing is what makes the answer worth trusting over a search.
    """
    try:
        return ElementTree.fromstring(f"<x{attrs.rstrip('/')}/>").attrib
    except ElementTree.ParseError:
        return None


def _read_attr(attrs: str, name: str) -> str | None:
    """Take `name` from a well-formed attribute region, else fall back to a loose search.

    A region that parses is taken at its word -- searching on could only carve a key out of
    another attribute's value. The search is for regions that did not parse, where an
    unclosed value can absorb `id=...` into the attribute before it.
    """
    strict = _strict_attrs(attrs)
    if strict is not None:
        for key, value in strict.items():
            if key.lower() == name.lower():
                return value.strip()
        return None

    found = _loose_attr(name).search(attrs)
    return (
        html.unescape(next(g for g in found.groups() if g is not None).strip()) if found else None
    )


@functools.cache
def _opening_tag(names: str) -> re.Pattern:
    """Match an opening tag for any of `names`, a `|`-joined alternation of escaped names.

    A tag name ends at whitespace, `/`, or `>`, so `<word>` never opens on `<word-class>`.
    `empty` is the slash of an empty-element tag, kept out of `attrs`.
    """
    return re.compile(
        rf"<(?P<name>{names})(?=[\s/>])(?P<attrs>[^>]*?)(?P<empty>/?)>", re.IGNORECASE
    )


@functools.cache
def _closing_tag(name: str) -> re.Pattern:
    return re.compile(rf"</{re.escape(name)}\s*>", re.IGNORECASE)


def _elements(text: str, names: str, *, pos: int = 0, final: bool = True):
    """Walk `text` once, left to right, yielding (opening tag, body, resume position).

    An element ends at its own closing tag, which only a same-name sibling opening first can
    preempt. With no closing tag anywhere -- routine in truncated output -- the next opening
    tag of any name stands in, keeping every element ahead of the damage. Only the closing
    tag is consumed; a next opening tag has to still be there for the step after.

    `final=False` means `text` is a stream buffer, so its end proves nothing: a trailing
    element with no boundary yet is held back rather than closed. The caller keeps the
    buffer whole and passes the last yielded position back as `pos` once more text arrives.

    Nesting is not supported: a sub-element of a different name stays in its parent's value
    as text.
    """
    opener = _opening_tag(names)
    while opened := opener.search(text, pos):
        pos = opened.end()
        if opened.group("empty"):
            yield opened, "", pos
            continue

        name = opened.group("name")
        close = _closing_tag(name).search(text, pos)
        sibling = _opening_tag(re.escape(name)).search(text, pos)
        if close and (not sibling or close.start() < sibling.start()):
            end, pos = close.start(), close.end()
        else:
            nxt = opener.search(text, pos)
            if not nxt and not final:
                return
            end = pos = nxt.start() if nxt else len(text)
        yield opened, text[opened.end() : end], pos


class XmlItemParser:
    """Regex-based XML parser for LLM outputs, fed a chunk at a time. Tolerant of malformed XML.

    `feed()` reports how many items became complete on that call; `close()` returns every item.

        parser = XmlItemParser("segment", key=("id", int))
        parser.feed(answer)
        items = parser.close()

    An element still waiting for its boundary stays in the buffer, so a stream can be fed in
    chunks cut anywhere -- the parser sees the same bytes it would have seen from the joined
    string.

    key controls the outer tag matching and what `close()` returns:
      key=None          → <tag>...</tag>            → returns list
      key="id"          → <tag id="3">...</tag>     → returns dict (str key)
      key=("id", int)   → <tag id="3">...</tag>     → returns dict (int key)

    value controls how the inner body is parsed:
      value=None         → plain text               → str
      value=["word"]     → <word>...</word>          → dict
      value=[("n", int)] → <n>...</n>                → dict (with type coercion)
      value=MyModel      → <field>...</field>        → BaseModel instance

    Examples, each shown with the items `close()` gives back:
      # <topic>some text</topic> → ["some text", ...]
      XmlItemParser("topic")

      # <turn id="1"><text>hello</text></turn> → {1: {"text": "hello"}, ...}
      XmlItemParser("turn", key=("id", int), value=["text"])

      # <term><word>elaborate</word><meaning>to explain in detail</meaning></term>
      # → [{"word": "elaborate", "meaning": "to explain in detail"}, ...]
      XmlItemParser("term", value=["word", "meaning"])

      # With BaseModel — field names become tag names, optional fields tolerate missing tags
      # → [AdvancedTerm(word="elaborate", ...), ...]
      XmlItemParser("term", value=AdvancedTerm)
    """

    def __init__(self, tag: str, key: FieldSpec | None = None, value: ValueSpec | None = None):
        self.tag = tag
        self.key_attr, self.key_type = _parse_field_spec(key) if key else (None, None)

        self.model_cls = None
        if isinstance(value, type) and issubclass(value, BaseModel):
            self.model_cls = value
            self.fields = list(value.model_fields.keys())
        else:
            self.fields = value

        if self.fields and any(_parse_field_spec(f)[0].lower() == tag.lower() for f in self.fields):
            # The item tag would swallow its own field, leaving every item empty.
            raise ValueError(f"field name collides with item tag {tag!r}")

        self.items = {} if self.key_attr else []
        self._names = re.escape(tag)
        self._buf = ""  # every chunk fed so far, kept whole
        self._pos = 0  # how far into it the walk has confirmed
        self._warnings: list[str] = []  # logged by close()

    @property
    def text(self) -> str:
        """Everything fed so far, verbatim -- the caller need not keep its own copy."""
        return self._buf

    def feed(self, text: str) -> int:
        """Take more output; return how many items became complete on this call."""
        before = len(self.items)
        self._buf += text
        self._consume(final=False)
        return len(self.items) - before

    def close(self) -> dict | list:
        """Close the held tail -- its end is proven now -- and return every item."""
        self._consume(final=True)
        if not self.items and self._buf.strip():
            self._warnings.append(
                f"No <{self.tag}> items found in {len(self._buf)} chars of output"
            )
        for message in self._warnings:
            log.warning(message)
        self._warnings.clear()
        return self.items

    def _consume(self, *, final: bool) -> None:
        for opened, body, pos in _elements(self._buf, self._names, pos=self._pos, final=final):
            self._pos = pos
            self._assemble(opened, body)

    def _assemble(self, opened: re.Match, body: str) -> None:
        """Record one element as an item, or warn and drop it."""
        obj = self._parse_body(body)
        if not obj:
            self._warnings.append(
                f"Skipping empty <{self.tag}> item: {_snippet(opened.group(0) + body)}"
            )
            return

        if self.model_cls:
            try:
                obj = self.model_cls.model_validate(obj)
            except ValidationError as e:
                problems = ", ".join(
                    f"{'.'.join(str(part) for part in err['loc']) or '<body>'}: {err['type']}"
                    for err in e.errors()
                )
                self._warnings.append(f"Skipping malformed <{self.tag}> item ({problems})")
                return

        if not self.key_attr:
            self.items.append(obj)
            return

        raw = _read_attr(opened.group("attrs"), self.key_attr)
        if not raw:
            self._warnings.append(
                f"Skipping <{self.tag}> item with no {self.key_attr}: {_snippet(opened.group(0))}"
            )
            return

        key_obj = _coerce_key(raw, self.key_type)
        if key_obj is None:
            self._warnings.append(
                f"Skipping <{self.tag}> item, unreadable {self.key_attr}: {_snippet(raw)}"
            )
            return

        if key_obj in self.items:
            self._warnings.append(f"Duplicate <{self.tag}> key {key_obj!r}; keeping the later one")
        self.items[key_obj] = obj

    def _parse_body(self, body: str):
        """Take one field per element in the body.

        Entities are decoded here: `&amp;` reaching an SRT is wrong, and reaching dominate is
        worse -- it escapes again and shows `&amp;` on screen.
        """
        if not self.fields:
            return html.unescape(body.strip())

        specs = {name.lower(): (name, typ) for name, typ in map(_parse_field_spec, self.fields)}

        obj = {}
        for opened, raw, _ in _elements(body, "|".join(map(re.escape, specs))):
            # A self-closing field carries no value; leave it unset so optionals stay optional.
            if opened.group("empty"):
                continue

            name, typ = specs[opened.group("name").lower()]
            value = _coerce(html.unescape(raw.strip()), typ)
            if value is None:
                continue
            if name in obj:
                self._warnings.append(f"Repeated <{name}> field; keeping the later one")
            obj[name] = value
        return obj
