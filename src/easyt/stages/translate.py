import html
from pathlib import Path

from easyt import log
from easyt.backend import open_backend
from easyt.languages import language_name
from easyt.parser import XmlItemParser
from easyt.prompts import load_prompt


class Translator:
    """Translates a chunk of sentences on LLM_BACKEND."""

    def __init__(self, source_lang: str, target_lang: str):
        self.backend = open_backend("Translation")
        self.prompt = load_prompt("translate").partial(
            source_lang=language_name(source_lang), target_lang=language_name(target_lang)
        )

    async def translate(self, sents: list[str], cache_path: Path | None = None) -> list[dict]:
        parser = _segment_parser()
        cached = (
            cache_path.read_text(encoding="utf-8") if cache_path and cache_path.exists() else ""
        )
        # A cache without segments, such as an older run's JSON, is translated again.
        if not parser.feed(cached):
            parser = _segment_parser()
            # Escaped, as the parser unescapes the answer: `&reg` in a text would come back as ®.
            segments = "\n".join(
                f'<segment id="{i}">{html.escape(s, quote=False)}</segment>'
                for i, s in enumerate(sents, 1)
            )
            system, human = self.prompt.format(
                input_text=f"<source_text>\n{segments}\n</source_text>\n"
            )
            parser.feed(await self.backend.ask(system, human))

            if cache_path:
                cache_path.write_text(parser.text, encoding="utf-8")

        targets = parser.close()
        if missing := [i for i in range(1, len(sents) + 1) if i not in targets]:
            redo = f"; delete {cache_path} to ask again" if cache_path else ""
            log.warning(
                f"Translation: segments {missing} got no answer and keep their source{redo}"
            )
        result = [{"source": s, "target": s} for s in sents]  # fallback: source as target
        for sent_id, target in targets.items():
            if 1 <= sent_id <= len(sents):
                result[sent_id - 1]["target"] = target
        return result


def _segment_parser() -> XmlItemParser:
    return XmlItemParser("segment", key=("id", int))
