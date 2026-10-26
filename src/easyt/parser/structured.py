"""Prompt-carried structured output — both directions in one module.

Provider-native schema enforcement exists on only some backends, and forcing a
whole schema onto a reasoning turn measurably costs correctness. So the shape
travels in the prompt instead:

    prompt = append_format_guide(prompt, ScoutOutput, tag="outline")   # -> model
    value = parse_guided(ScoutOutput, reply, tag="outline")            # <- model

Both halves derive from the same Pydantic model — names, nesting,
required-ness, descriptions — so the instruction cannot drift from what the
parser accepts.

The guide is **appended**, never substituted into a placeholder: a prompt file
stays a plain prompt, and a placeholder fails silently in both directions —
dropped from the template it formats cleanly while sending no shape at all,
dropped from the call it raises ``KeyError`` at dispatch.

Copied from max (max/parsers/structured.py); change the two together.
"""

from __future__ import annotations

import json
import re
import types
import typing

import json_repair
from pydantic import BaseModel
from pydantic.fields import FieldInfo

from easyt import log

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$")

_SCALARS = {str: "string", int: "integer", float: "number", bool: "boolean"}


class GuideError(ValueError):
    """The reply carried no payload this guide could read."""


def default_tag(model: type[BaseModel]) -> str:
    """The block tag a model gets when the caller does not name one.

    Both halves derive it the same way, so they cannot end up on different
    tags. Pass ``tag=`` when a better word exists for the block.
    """
    name = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", model.__name__)
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name).lower()


# ── prompt side ───────────────────────────────────────────────────────


def append_format_guide(prompt: str, model: type[BaseModel], *, tag: str | None = None) -> str:
    """Append ``model``'s output contract to the end of ``prompt``.

    The seam is a break plus a bold lead-in rather than a header, because a
    header *level* claims a place in the host prompt's hierarchy that the
    appender cannot know: `##` under an all-`###` prompt closes every section
    above it. Not an XML wrapper either — the payload is already tag-delimited,
    and wrapping the example invites the model to echo the outer tag.
    """
    tag = tag or default_tag(model)
    return (
        f"{prompt.rstrip()}\n\n"
        "---\n\n"
        f"**Format Guide**: close your reply with a single `<{tag}>` block of exactly this shape, "
        'filled in. In the shape, `[T]` is a list of zero or more `T`, and `"a" | "b"` is one '
        "of those values. Fields marked `optional` may be omitted; everything else must "
        "be present. Emit the block once, at the very end.\n\n"
        f"{render_format_guide(model, tag=tag)}\n"
    )


def render_format_guide(model: type[BaseModel], *, tag: str | None = None) -> str:
    """Render ``model`` as a compressed type sketch wrapped in ``<tag>``.

    A type sketch rather than a filled sample: a sample invites the model to
    copy its literal values, and JSON Schema is verbose enough to crowd out the
    reasoning it is attached to.
    """
    tag = tag or default_tag(model)
    body = "\n".join(_render_object(model, indent=1))
    return f"<{tag}>\n{{\n{body}\n}}\n</{tag}>"


def _render_object(model: type[BaseModel], *, indent: int) -> list[str]:
    """Render one object's fields, separators included.

    The sketch is JSON with comments, not JSON-ish prose: strip the `//`, fill in
    the type slots (`string`, or a Literal's `"a" | "b"`), and it parses. A model
    asked for "exactly this shape" can copy the punctuation it sees, and a
    comma-less copy lands in ``json_repair`` — which succeeds, so
    the only trace is the warning that exists to flag *truncation*. Nothing
    else in this module leans on repair being rare, and this is the one place
    that could make it routine.
    """
    pad = "  " * indent
    fields = list(model.model_fields.items())
    lines: list[str] = []
    for position, (name, field) in enumerate(fields):
        rendered, nested = _render_type(field.annotation, indent=indent)
        # No trailing comma after the last field -- strict `json.loads` rejects
        # one, and it is the parser's first attempt.
        sep = "," if position < len(fields) - 1 else ""
        if nested:
            # The comma rides the closing brace, past the nested block.
            lines.extend(_comment(f'{pad}"{name}": {rendered}', field))
            lines.extend(nested)
            closer = "}]" if _is_sequence(_unwrap_optional(field.annotation)) else "}"
            lines.append(f"{pad}{closer}{sep}")
        else:
            lines.extend(_comment(f'{pad}"{name}": {rendered}{sep}', field))
    return lines


def _comment(head: str, field: FieldInfo) -> list[str]:
    notes = [] if field.is_required() else ["optional"]
    if field.description:
        notes.append(field.description)
    if not notes:
        return [head]
    return [f"{head} // {' -- '.join(notes)}"]


def _is_sequence(annotation: typing.Any) -> bool:
    return typing.get_origin(annotation) in (list, tuple, set)


def _unwrap_optional(annotation: typing.Any) -> typing.Any:
    """`X | None` carries no extra shape information for the guide."""
    if typing.get_origin(annotation) in (typing.Union, types.UnionType):
        args = [a for a in typing.get_args(annotation) if a is not type(None)]
        if len(args) == 1:
            return args[0]
    return annotation


def _render_type(annotation: typing.Any, *, indent: int) -> tuple[str, list[str]]:
    """Return the inline rendering plus any nested block lines."""
    annotation = _unwrap_optional(annotation)

    if _is_sequence(annotation):
        (item,) = typing.get_args(annotation) or (str,)
        item = _unwrap_optional(item)
        if isinstance(item, type) and issubclass(item, BaseModel):
            return "[{", _render_object(item, indent=indent + 1)
        inner, _ = _render_type(item, indent=indent)
        return f"[{inner}]", []

    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return "{", _render_object(annotation, indent=indent + 1)

    if typing.get_origin(annotation) is typing.Literal:
        return " | ".join(
            json.dumps(v, ensure_ascii=False) for v in typing.get_args(annotation)
        ), []

    return _SCALARS.get(annotation, "string"), []


# ── reply side ────────────────────────────────────────────────────────


def parse_guided[ModelT: BaseModel](
    model: type[ModelT], text: str, *, tag: str | None = None
) -> ModelT:
    """Read back what ``render_format_guide`` asked for.

    Strict first: a well-formed reply is parsed by ``json.loads`` and never
    touches a heuristic. Only a reply that fails strict parsing is handed to
    ``json_repair``, and that is logged — repair can close a truncated payload
    into something that validates, which would otherwise be silent data loss.

    Raises ``GuideError`` when no object can be read, ``ValidationError`` when
    the object does not satisfy ``model``; both feed the caller's reask.
    """
    tag = tag or default_tag(model)
    blocks = re.findall(rf"<{tag}>(.*?)</{tag}>", text, re.DOTALL)
    # Last block wins: a model may quote the guide before answering with it.
    payload = _FENCE_RE.sub("", (blocks[-1] if blocks else text).strip())
    if not payload:
        raise GuideError(f"reply carried no <{tag}> payload")

    try:
        data = json.loads(payload)
    except ValueError as strict_error:
        data = json_repair.loads(payload)
        if not isinstance(data, dict):
            raise GuideError(f"<{tag}> payload is not an object: {strict_error}") from None
        # Loud on purpose: repair silently completes truncated payloads, so a
        # run that needed it may have dropped list entries the model did emit.
        log.warning(
            f"structured: repaired a malformed <{tag}> payload ({strict_error}) "
            f"-- keys={sorted(data)}"
        )

    if not isinstance(data, dict):
        raise GuideError(f"<{tag}> payload is not an object: {type(data).__name__}")
    return model.model_validate(data)
