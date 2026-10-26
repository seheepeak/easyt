import asyncio
import html
import os
import re

from easyt import log
from easyt.backend import open_backend
from easyt.languages import language_name
from easyt.parser import DelimitedOutputParser, XmlItemParser
from easyt.prompts import load_prompt

# Letters and digits a line holds (MAX_LINE_LEN overrides): 20 fits CJK, an alphabet about 36.
MAX_LINE_LEN = 20
# Lines a screen shows at most; how many it shows is the model's call, by meaning.
MAX_SCREEN_LINES = 3
# A line of dashes ends a screen; blank lines do not, so the model's spacing cannot move one.
_SCREEN_BREAK = re.compile(r"-{3,}")


def count_letters(text: str) -> int:
    """Letters and digits in `text`: what line lengths and screen times are measured in."""
    return sum(1 for c in text if c.isalnum())


class LLMLineBreaker:
    """Breaks texts into lines, and groups the lines into screens: what a subtitle shows at once."""

    def __init__(self, lang: str):
        self.lang = lang
        self.max_line_len = int(os.getenv("MAX_LINE_LEN") or MAX_LINE_LEN)
        if self.max_line_len < 1:
            raise ValueError(f"MAX_LINE_LEN must be 1 or more, not {self.max_line_len}")
        self.backend = open_backend("Line breaking")
        self.output_parser = DelimitedOutputParser(delim="output")

    def _measure_lines(self, text: str):
        char_count = count_letters(text)
        if char_count < self.max_line_len:
            return 1, 1
        elif char_count < self.max_line_len * 3 // 2:
            return 1, 2
        elif char_count < self.max_line_len * 2:
            return 2, 2
        else:
            min_lines = char_count // self.max_line_len
            return min_lines, min_lines + 1

    async def __call__(self, texts: list[str]) -> list[list[list[str]]]:
        """Each text's screens, each a list of lines. A text that one line holds is not asked
        about, and one the answer leaves out or changes stays whole on one line."""
        screens = [[[text]] for text in texts]
        asked = []  # the index in texts of each segment, whose id is its place here from 1
        segments = []
        for i, text in enumerate(texts):
            min_lines, max_lines = self._measure_lines(text)
            if max_lines <= 1:
                continue
            asked.append(i)
            lines = f"{min_lines}-{max_lines}" if min_lines < max_lines else f"{max_lines}"
            # Escaped, as the parser unescapes the answer: `&reg` in a text would come back as ®.
            body = html.escape(text, quote=False)
            segments.append(f'<segment id="{len(asked)}" lines="{lines}">{body}</segment>')
        if not asked:
            return screens

        system, human = load_prompt("line_break").format(
            language=language_name(self.lang), input_text="\n".join(segments)
        )
        answer = await self.backend.ask(system, human)
        if self.output_parser.is_complete(answer):
            answer = self.output_parser.parse(answer)
        parser = XmlItemParser("segment", key=("id", int))
        parser.feed(answer)
        answered = parser.close()

        missing = []
        for item_id, i in enumerate(asked, 1):
            if found := _screens(answered.get(item_id, "")):
                screens[i] = _fit(item_id, texts[i], found)
            else:
                missing.append(item_id)
        if missing:
            log.warning(f"Line breaking: segments {missing} got no answer and stay on one line")
        return screens


def _screens(body: str) -> list[list[str]]:
    """A segment's answer as its screens, each a list of lines."""
    screens = [[]]
    for line in map(str.strip, body.splitlines()):
        if _SCREEN_BREAK.fullmatch(line):
            screens.append([])
        elif line:
            screens[-1].append(line)
    return [screen for screen in screens if screen]


def _fit(item_id: int, text: str, screens: list[list[str]]) -> list[list[str]]:
    """`screens`, unless their text is not `text`, as when the answer was cut off or mixed up:
    then `text` whole on one line. A screen over MAX_SCREEN_LINES is split into even ones."""
    squash = lambda s: re.sub(r"\s+", "", s)
    if squash("".join(line for screen in screens for line in screen)) != squash(text):
        log.warning(f"Line breaking: segment {item_id} came back changed and stays on one line")
        return [[text]]
    fitted = []
    for screen in screens:
        if len(screen) <= MAX_SCREEN_LINES:
            fitted.append(screen)
            continue
        log.warning(f"Line breaking: segment {item_id} put {len(screen)} lines on one screen")
        count = -(-len(screen) // MAX_SCREEN_LINES)
        size, longer = divmod(len(screen), count)
        start = 0
        for k in range(count):
            end = start + size + (k < longer)
            fitted.append(screen[start:end])
            start = end
    return fitted


if __name__ == "__main__":
    import pprint

    input_texts = [
        "Today we write tests, and see why they matter.",
        "Next time, we will run the tests on their own.",
    ]

    processor = LLMLineBreaker("en")
    pp = pprint.PrettyPrinter(indent=4)
    results = asyncio.run(processor(input_texts))
    pp.pprint(results)
    print()
