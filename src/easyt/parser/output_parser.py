import re

from easyt import log


class DelimitedOutputParser:
    """Extracts text between delimiters from LLM output.

    Supports tag names ("output" → <output>...</output>) and bracket pairs ("()", "[]", "{}",
    "<>"); a delimiter that opens and closes alike is refused, as its ends can't be told apart.

    Gracefully degrades: missing closing → take rest of text,
    missing opening → return full text.
    """

    def __init__(self, delim: str):
        if delim in ["()", "[]", "{}", "<>"]:
            self.delim_start, self.delim_end = delim[0], delim[1]
        elif re.match(r"^[a-zA-Z]\w*$", delim):
            self.delim_start, self.delim_end = f"<{delim}>", f"</{delim}>"
        else:
            raise ValueError(f"{delim!r} is neither a tag name nor a bracket pair")

    def is_complete(self, text: str) -> bool:
        """Whether `text` holds both delimiters, so `parse` need not guess where it was cut."""
        pattern = re.escape(self.delim_start) + r".*?" + re.escape(self.delim_end)
        return re.search(pattern, text, re.DOTALL | re.IGNORECASE) is not None

    def parse(self, text: str) -> str:
        # 1) Both delimiters found → extract between them
        m = re.search(
            re.escape(self.delim_start) + r"(.*?)" + re.escape(self.delim_end),
            text,
            re.DOTALL | re.IGNORECASE,
        )
        if m:
            return m.group(1).strip()

        # 2) Opening only → closing was truncated (e.g. token limit)
        m = re.search(
            re.escape(self.delim_start) + r"(.*)$",
            text,
            re.DOTALL | re.IGNORECASE,
        )
        if m:
            log.warning(f"Closing delimiter {self.delim_end} is missing.")
            return m.group(1).strip()

        # 3) Closing only → opening was truncated
        m = re.search(
            r"^(.*?)" + re.escape(self.delim_end),
            text,
            re.DOTALL | re.IGNORECASE,
        )
        if m:
            log.warning(f"Opening delimiter {self.delim_start} is missing.")
            return m.group(1).strip()

        # 4) No delimiters found at all
        log.warning(f"No delimiter {self.delim_start} found. Returning full text.")
        return text.strip()
