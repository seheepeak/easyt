import json
import re
from datetime import timedelta
from functools import partial
from itertools import batched
from pathlib import Path

import numpy as np
import srt

from easyt import log
from easyt.pipeline.base import Pipeline_base
from easyt.stages.concurrency import run_concurrently
from easyt.stages.line_break import LLMLineBreaker, count_letters
from easyt.stages.translate import Translator
from easyt.utils.align import char_map
from easyt.utils.spans import span
from easyt.utils.threads import in_thread
from easyt.youtube import Loader

# Seconds a subtitle stays on screen at least, unless its text is spoken faster than that.
MIN_SCREEN_SECONDS = 1.0


async def do_sent_break(out_path: Path, translated: list[dict], line_breaker: LLMLineBreaker):
    with span("Line breaking") as s:
        subtitles = await _subtitles(translated, line_breaker)
        s.result = f"{len(subtitles)} subtitles"
    out_path.write_text(srt.compose(subtitles), encoding="utf-8")


async def _subtitles(translated: list[dict], line_breaker: LLMLineBreaker) -> list[srt.Subtitle]:
    input_texts = []
    for t in translated:
        text = t["text"]
        text = re.sub(r"\s+", " ", text).strip()
        input_texts.append(text)

    # process line break, ten texts a call
    broken = await run_concurrently(
        [partial(line_breaker, list(texts)) for texts in batched(input_texts, 10)],
    )
    screens_per_text = [screens for batch in broken for screens in batch]

    # One subtitle per screen, each up a second at least so a short line can be read.
    subtitles: list[srt.Subtitle] = []
    for src, screens in zip(translated, screens_per_text, strict=True):
        start, duration = src["start"], src["duration"]
        floor = min(MIN_SCREEN_SECONDS, duration / len(screens))
        spare = duration - floor * len(screens)
        letters = [count_letters("".join(screen)) for screen in screens]
        for screen, count in zip(screens, letters, strict=True):
            # A screen of symbols alone, such as ♪, has no letters to share by.
            share = count / sum(letters) if sum(letters) else 1 / len(screens)
            seconds = floor + spare * share
            subtitles.append(
                srt.Subtitle(
                    len(subtitles) + 1,
                    timedelta(seconds=start),
                    timedelta(seconds=start + seconds),
                    "\n".join(screen),
                )
            )
            start += seconds
    return subtitles


class Pipeline(Pipeline_base):
    def __init__(
        self,
        transcript: list[dict],  # List of dicts with keys "start", "duration", "text"
        loader: Loader,
        source_lang: str,
        target_lang: str,
        chunk_size: int = 4 * 1000,
        skip_exsisting: bool = True,
        transcript_only: bool = False,
    ) -> None:
        super().__init__(
            transcript,
            loader,
            source_lang,
            chunk_size,
            skip_exsisting,
        )
        self.target_lang = target_lang
        self.translator = Translator(
            source_lang=source_lang,
            target_lang=target_lang,
        )
        # Made now, so a bad MAX_LINE_LEN fails before any model call.
        self.line_breaker = LLMLineBreaker(target_lang)
        self.transcript_only = transcript_only

    async def run(self) -> Path:
        input = " ".join([sub["text"] for sub in self.input_transcript])
        punct_results = await self.punct.run(input)

        if self.transcript_only:
            txt_path = self.loader.video_path.with_suffix(".txt")
            with txt_path.open("w", encoding="utf-8") as file:
                for chunk in punct_results:
                    for _, p in chunk["sentences"]:
                        file.write(p + "\n")
            log.success(f"Transcript generated: {txt_path}")
            await self.send_document_to_telegram(txt_path)
            return txt_path

        translated = await self._translate(punct_results)
        translate_output_path = self.loader.outdir / f"translate_results_{self.target_lang}.json"
        with translate_output_path.open("w", encoding="utf-8") as file:
            json.dump(translated, file, ensure_ascii=False, indent=4)

        # generate translated transcript (without wrap-line)
        with span("Timestamps") as s:
            _, target_lang_results = await in_thread(
                self._inverse_map_timestamp, input, punct_results, translated
            )
            s.result = f"{len(target_lang_results)} sentences"

        srt_path = self.loader.video_path.with_suffix(f".{self.target_lang}.srt")
        await do_sent_break(srt_path, target_lang_results, self.line_breaker)
        return srt_path

    async def _translate(self, punct_results: list[dict]) -> list[dict]:
        """Each punctuated sentence with its translation, or itself where the video is already in
        the target language."""
        if self.source_lang == self.target_lang:
            log.info("Already in the target language, so not translating")
            return [
                {"source": s, "target": s, "chunk_idx": i}
                for i, src in enumerate(punct_results)
                for _, s in src["sentences"]
            ]

        with span("Translation") as s:
            chunks = await run_concurrently(
                [
                    partial(
                        self.translator.translate,
                        [p for _, p in src["sentences"]],
                        # By language: every run of a video on one day shares its folder.
                        self.loader.outdir / f"translate_cache_{self.target_lang}_{i + 1}.txt",
                    )
                    for i, src in enumerate(punct_results)
                ],
            )
            translated = [{**o, "chunk_idx": i} for i, chunk in enumerate(chunks) for o in chunk]
            s.result = f"{len(translated)} sentences"
        return translated

    def _inverse_map_timestamp(self, input: str, p_results: list[dict], t_results: list[dict]):
        source_lang_results_path = self.loader.outdir / "source_lang_results.json"
        target_lang_results_path = (
            self.loader.outdir / f"target_lang_results_{self.target_lang}.json"
        )
        if (
            source_lang_results_path.exists()
            and target_lang_results_path.exists()
            and self.skip_exsisting
        ):
            with source_lang_results_path.open("r", encoding="utf-8") as file:
                source_lang_results = json.load(file)
            with target_lang_results_path.open("r", encoding="utf-8") as file:
                target_lang_results = json.load(file)
            return source_lang_results, target_lang_results

        # timespans (start, end) per character
        char_timespans = []
        for i, sub in enumerate(self.input_transcript):
            length = len(sub["text"])
            start, duration = sub["start"], sub["duration"]
            times = np.linspace(start, start + duration, length + 1).tolist()
            char_timespans.extend([(times[j], times[j + 1]) for j in range(length)])
            if i + 1 < len(self.input_transcript):
                char_timespans.append(None)
        assert len(char_timespans) == len(input)

        sent_timespans = []
        for chunk_idx, p in enumerate(p_results):
            chunk_start = p["chunk_start"]
            chunk_end = p["chunk_end"]
            src_chunk = input[chunk_start:chunk_end]

            dst_sentences = [t["source"] for t in t_results if t["chunk_idx"] == chunk_idx]
            dst_chunk = "\n".join(dst_sentences)

            # dst2src_map is [index of dst_chunk] to [index of src_chunk] map
            dst2src_map = char_map(src_chunk, dst_chunk)

            # calculate timespans for each sentence
            dst_offset = 0
            for dst_sent in dst_sentences:
                start, end = None, None
                mapped_timespans = [
                    char_timespans[chunk_start + i]
                    for i in dst2src_map[dst_offset : dst_offset + len(dst_sent)]
                    if i >= 0 and char_timespans[chunk_start + i] is not None
                ]
                if mapped_timespans:
                    start = min(t[0] for t in mapped_timespans)
                    end = max(t[1] for t in mapped_timespans)
                sent_timespans.append((start, end))
                dst_offset += len(dst_sent) + 1
        assert len(sent_timespans) == len(t_results)

        source_lang_results = []
        target_lang_results = []
        for i, t in enumerate(t_results):
            start, end = sent_timespans[i]
            if start is None or end is None:
                continue
            source_lang_results.append(
                {"text": t["source"], "start": start, "duration": end - start}
            )
            target_lang_results.append(
                {"text": t["target"], "start": start, "duration": end - start}
            )

        # save results
        with source_lang_results_path.open("w", encoding="utf-8") as file:
            json.dump(source_lang_results, file, ensure_ascii=False, indent=4)
        with target_lang_results_path.open("w", encoding="utf-8") as file:
            json.dump(target_lang_results, file, ensure_ascii=False, indent=4)
        return source_lang_results, target_lang_results
