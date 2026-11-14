"""Subtitle screens by rule, with no model: for the sentences of a video in its own language."""

import math
from functools import cache
from itertools import batched, pairwise

# Characters a line holds, spaces too: the usual limit for English subtitles.
MAX_LINE = 42


def wrap(text: str) -> list[list[str]]:
    """`text` in screens of one or two lines of at most MAX_LINE characters, broken at spaces into
    lines as even as can be, after punctuation where it is near. A longer word has a line to
    itself, so a text with no spaces, such as Chinese, stays whole on one line."""
    words = text.split()
    whole = " ".join(words)
    if len(whole) <= MAX_LINE:
        return [[whole]]
    n = min(math.ceil(len(whole) / MAX_LINE), len(words))
    while (lines := _lines(words, n)) is None:  # ends, as a line per word always fits
        n += 1
    return [list(screen) for screen in batched(lines, 2)]


def _lines(words: list[str], n: int) -> list[str] | None:
    """The words in n lines of at most MAX_LINE characters, or of a longer word alone; None where
    they do not fit."""
    target = len(" ".join(words)) / n

    @cache
    def best(k: int, i: int) -> tuple[float, tuple[int, ...]]:
        """What words[i:] in k lines cost at best, in squared unevenness, and where each ends."""
        if k == 0:
            return (0.0, ()) if i == len(words) else (math.inf, ())
        found = (math.inf, ())
        width = -1
        for j in range(i + 1, len(words) + 1):
            width += len(words[j - 1]) + 1
            if width > MAX_LINE and j > i + 1:
                break
            cost, ends = best(k - 1, j)
            cost += (width - target) ** 2
            if j < len(words) and words[j - 1][-1] in ",;:?!-–—…":
                cost -= 180  # lines may be some ten characters off even to end after punctuation
            if cost < found[0]:
                found = (cost, (j, *ends))
        return found

    cost, ends = best(n, 0)
    return None if cost == math.inf else [" ".join(words[a:b]) for a, b in pairwise((0, *ends))]
