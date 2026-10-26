"""How the line breaker asks for lines and screens, and reads them back."""

import logging

import pytest

import easyt.stages.line_break
from easyt.stages.line_break import LLMLineBreaker

# Long enough for two lines, so the breaker asks the model.
TEXT = "Today we write tests, and see why they matter."
FIRST = "Today we write tests,"
SECOND = "and see why they matter."


def _answer(*bodies: str) -> str:
    segments = "".join(f'<segment id="{i}">\n{b}\n</segment>\n' for i, b in enumerate(bodies, 1))
    return f"<output>\n{segments}</output>"


@pytest.fixture
def answered(fake_backend):
    """Break `texts` with the model answering `answer`; returns the screens and what was asked."""

    async def run_with(answer, texts=(TEXT,), lang="en"):
        asked = []

        async def run(system, prompt):
            asked.append((system, prompt))
            return answer

        fake_backend(easyt.stages.line_break, run)
        return await LLMLineBreaker(lang)(list(texts)), asked

    return run_with


async def test_a_text_goes_out_as_a_segment_and_comes_back_as_screens(answered):
    screens, asked = await answered(_answer(f"{FIRST}\n---\n{SECOND}"))
    assert screens == [[[FIRST], [SECOND]]]
    [(_, prompt)] = asked
    assert f'<segment id="1" lines="2">{TEXT}</segment>' in prompt


async def test_blank_lines_mean_nothing_and_stray_dashes_make_no_empty_screen(answered):
    head, tail = "Today we", "write tests,"
    screens, _ = await answered(_answer(f"---\n{head}\n\n{tail}\n\n-----\n\n{SECOND}\n---"))
    assert screens == [[[head, tail], [SECOND]]]


async def test_a_screen_over_three_lines_is_split_into_even_screens(answered, caplog):
    lines = [
        "Today we",
        "write tests,",
        "and see",
        "why they",
        "matter.",
    ]
    screens, _ = await answered(_answer("\n".join(lines)))
    assert screens == [[lines[:3], lines[3:]]]
    assert "put 5 lines on one screen" in caplog.text


async def test_a_changed_text_falls_back_to_the_source_on_one_line(answered, caplog):
    screens, _ = await answered(_answer("Today we talk about\nsomething else."))
    assert screens == [[[TEXT]]]
    assert "segment 1 came back changed" in caplog.text


async def test_a_cut_off_answer_keeps_its_finished_segments_but_not_the_cut_one(answered):
    other = "Next time, we will run the tests on their own."
    cut = f'<output>\n<segment id="1">\n{FIRST}\n{SECOND}\n</segment>\n<segment id="2">\nNext'
    screens, _ = await answered(cut, [TEXT, other])
    assert screens == [[[FIRST, SECOND]], [[other]]]


async def test_an_answer_with_crlf_line_ends_reads_the_same(answered):
    screens, _ = await answered(_answer(f"{FIRST}\n---\n{SECOND}").replace("\n", "\r\n"))
    assert screens == [[[FIRST], [SECOND]]]


async def test_an_ampersand_goes_out_escaped_and_comes_back_as_it_was(answered):
    text = "R&D budgets and price&regulation are today's main topic."
    first, second = (
        "R&D budgets and price&regulation",
        "are today's main topic.",
    )
    echoed = f"{first}\n{second}".replace("&", "&amp;")
    screens, asked = await answered(_answer(echoed), [text])
    [(_, prompt)] = asked
    assert "price&amp;regulation" in prompt
    assert screens == [[[first, second]]]


async def test_a_text_the_answer_leaves_out_stays_whole_on_one_line(answered, caplog):
    screens, _ = await answered("1. Today we write\n2. and see why")
    assert screens == [[[TEXT]]]
    assert "segments [1] got no answer" in caplog.text


async def test_segments_are_read_without_their_output_block(answered):
    screens, _ = await answered(f'<segment id="1">\n{FIRST}\n{SECOND}\n</segment>')
    assert screens == [[[FIRST, SECOND]]]


async def test_max_line_len_in_the_environment_sets_what_a_line_holds(answered, monkeypatch):
    monkeypatch.setenv("MAX_LINE_LEN", "40")
    screens, asked = await answered("nothing is asked")
    assert screens == [[[TEXT]]]  # 36 letters fit on one line
    assert asked == []


async def test_line_breaking_follows_the_target_language(answered, monkeypatch):
    monkeypatch.setenv("MAX_LINE_LEN", "36")  # an English line
    short = "We need to finish this by tomorrow."  # 28 letters: one line
    long = "If you look closely at the data, you will notice a trend."
    screens, asked = await answered(
        _answer("If you look closely at the data,\nyou will notice a trend."), [short, long], "en"
    )
    assert screens == [
        [[short]],
        [["If you look closely at the data,", "you will notice a trend."]],
    ]
    [(system, prompt)] = asked
    assert "subtitle segmenter for English" in system
    assert short not in prompt


async def test_line_breaking_runs_on_a_named_model(monkeypatch, fake_model):
    monkeypatch.setenv("LLM_BACKEND", "openai:gpt-5.6-terra")
    model = fake_model(_answer(f"{FIRST}\n{SECOND}"))
    assert await LLMLineBreaker("en")([TEXT]) == [[[FIRST, SECOND]]]
    assert len(model.asked) == 1


@pytest.mark.live
@pytest.mark.parametrize("backend", ["claude-code", "codex"])
async def test_line_breaking_on_a_live_subscription(monkeypatch, caplog, backend):
    monkeypatch.setenv("LLM_BACKEND", backend)
    [screens] = await LLMLineBreaker("en")([TEXT])
    # No warning: the model kept the text and the 3-line screens itself, with nothing put back.
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len([line for screen in screens for line in screen]) >= 2
