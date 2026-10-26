import logging
from typing import Literal

import pytest
from pydantic import BaseModel, Field, ValidationError

from easyt.parser.structured import (
    GuideError,
    append_format_guide,
    default_tag,
    parse_guided,
    render_format_guide,
)


class Leaf(BaseModel):
    name: str = Field(description="the name")
    count: int = 0


class Root(BaseModel):
    title: str
    leaves: list[Leaf]
    kind: Literal["a", "b"] = "a"


WELL_FORMED = '<root>{"title": "t", "leaves": [{"name": "x", "count": 2}], "kind": "b"}</root>'


def test_the_guide_sketches_the_models_types_not_sample_values():
    guide = render_format_guide(Root)
    assert guide.startswith("<root>\n") and guide.endswith("\n</root>")
    assert '"title": string,' in guide
    assert '"name": string, // the name' in guide
    assert '"kind": "a" | "b" // optional' in guide


def test_an_optional_list_of_objects_closes_its_list():
    class Tree(BaseModel):
        leaves: list[Leaf] | None = None

    guide = render_format_guide(Tree)
    assert guide.count("[{") == guide.count("}]") == 1


def test_the_guide_is_appended_after_a_break_and_leaves_the_prompt_intact():
    prompt = append_format_guide("Do it.\n", Root)
    assert prompt.startswith("Do it.\n\n---\n\n**Format Guide**")
    assert prompt.endswith("</root>\n")


def test_a_well_formed_block_parses():
    assert parse_guided(Root, WELL_FORMED) == Root(
        title="t", leaves=[Leaf(name="x", count=2)], kind="b"
    )


def test_prose_around_the_block_is_ignored_and_the_last_block_wins():
    echoed = '<root>{"title": "PLACEHOLDER", "leaves": []}</root>'
    assert parse_guided(Root, f"{echoed}\nThe answer:\n{WELL_FORMED}\nDone.").title == "t"


def test_a_markdown_fence_inside_the_block_is_stripped():
    fenced = '<root>\n```json\n{"title": "t", "leaves": []}\n```\n</root>'
    assert parse_guided(Root, fenced).title == "t"


def test_a_well_formed_payload_never_reaches_the_repair_heuristics(monkeypatch):
    class Tripwire:
        def loads(self, _payload):
            raise AssertionError("json_repair was used on a well-formed payload")

    monkeypatch.setattr("easyt.parser.structured.json_repair", Tripwire())
    assert parse_guided(Root, WELL_FORMED).title == "t"


def test_a_repaired_payload_is_never_silent(caplog):
    with caplog.at_level(logging.WARNING, logger="easyt"):
        root = parse_guided(Root, '<root>{"title": "t", "leaves": [],}</root>')
    assert root.title == "t"
    assert any("repaired a malformed" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("text", ["<root></root>", "<root>[1, 2]</root>", "No block here.", ""])
def test_a_reply_with_no_readable_object_raises_for_the_reask(text):
    with pytest.raises((GuideError, ValidationError)):
        parse_guided(Root, text)


def test_a_missing_required_field_fails_validation():
    with pytest.raises(ValidationError):
        parse_guided(Root, '<root>{"leaves": []}</root>')


def test_an_unnamed_tag_is_derived_from_the_model():
    class HTTPProbeResult(BaseModel):
        ok: bool = True

    assert default_tag(HTTPProbeResult) == "http_probe_result"
    assert "<leaf>" in append_format_guide("Do it.", Leaf)
    assert parse_guided(Leaf, '<leaf>{"name": "x"}</leaf>').name == "x"
