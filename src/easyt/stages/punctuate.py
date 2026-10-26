import asyncio
import json
import re
import string
import threading
from functools import cache, partial
from pathlib import Path

import numpy as np

from easyt import log
from easyt.backend import open_backend
from easyt.parser import DelimitedOutputParser
from easyt.prompts import load_prompt
from easyt.stages.concurrency import run_concurrently
from easyt.utils.align import char_map
from easyt.utils.spans import span
from easyt.utils.threads import in_thread

"""
Previously used structured output with LLM's support of sentence splitting. Keeping this for future reference.

class PunctuationResult(BaseModel):
    sentences: List[str]

def _create_llm_punct():
    MODEL_NAME = "gpt-4o"
    get_num_tokens = ChatOpenAI(model_name=MODEL_NAME).get_num_tokens
    client = OpenAI()

    def _run(x: str) -> list[str]:
        # For now, streaming for structured output is not supported in langchain-openai.
        # So, we have to use the low-level API.
        with client.beta.chat.completions.stream(
            model=MODEL_NAME,
            messages=[
                {"role": "system", "content": _PUNCT_SYSTEM_MESSAGE},
                {"role": "user", "content": f"\"\"\"{x}\"\"\""},
            ],
            max_tokens=4096,
            response_format=PunctuationResult,
        ) as stream, tqdm(desc="[Punctuation]", total=get_num_tokens(x)) as pbar:
            for event in stream:
                if event.type == "content.delta":
                    if pbar.total > pbar.n:
                        pbar.update(1)
            result = stream.get_final_completion().choices[0].message.parsed

        return result.sentences

    return _run
"""


def _create_llm_punct():
    backend = open_backend("Punctuation")
    prompt = load_prompt("punctuate")
    output_parser = DelimitedOutputParser(delim="output")

    async def _run(x: str) -> str | None:
        """`x` punctuated, or None if the answer has nothing in an <output> block."""
        system, human = prompt.format(input_text=x)
        text = await backend.ask(system, human)
        if not output_parser.is_complete(text):
            return None
        return output_parser.parse(text) or None

    return _run


class SentSplitter:
    """Where sentences end: as the model puts them, a line each, and as SaT finds them, in a raw
    transcript and in a line too long to be one sentence."""

    EMPTY_SENTENCE = re.compile(f"^[{re.escape(string.whitespace + string.punctuation)}]*$")
    # A longer line holds several sentences: only 1 in 26,815 past ones was longer.
    LONGEST_SENTENCE = 1000
    # How far a cut may move to a sentence end: 99% of raw transcript sentences are shorter.
    CUT_WINDOW = 300
    # One call into the model at a time, as a cancelled job's thread may still be using it.
    _lock = threading.Lock()

    @staticmethod
    @cache
    def _sat():
        """The model, loaded once for every splitter, on the GPU if there is one."""
        import torch
        from wtpsplit import SaT

        sat = SaT("sat-12l-sm")
        if torch.cuda.is_available():
            sat.half().to("cuda")
        return sat

    def split_lines(self, text: str) -> list[str]:
        """The sentences of the model's punctuated `text`: a line each, but SaT's sentences of a
        line too long to be one, and of the only line, as the model may have left the text on one
        line; SaT keeps a line that is one sentence whole."""
        lines = [line for line in text.splitlines() if line.strip()]
        sents = []
        for line in lines:
            if len(lines) == 1 or len(line) > self.LONGEST_SENTENCE:
                sents += self.split(line)
            else:
                sents.append(line)
        return self._tidy(sents)

    def split(self, raw_text: str) -> list[str]:
        """The sentences of `raw_text`, as SaT finds them."""
        with self._lock:
            split = self._sat().split(raw_text)
        return self._tidy(split)

    def _tidy(self, sents: list[str]) -> list[str]:
        """`sents` with tidy whitespace, without those of whitespace and punctuation alone."""
        sents = [re.sub(r"\s+", " ", s).strip() for s in sents]
        return [s for s in sents if not self.EMPTY_SENTENCE.match(s)]

    def cut_points(self, text: str, chunk_size: int) -> list[int]:
        """Where to cut `text` into even chunks of about `chunk_size`: around each cut, within
        CUT_WINDOW, after the word SaT is surest a sentence ends, the nearest on a tie. The
        transcript has no punctuation yet, which SaT's -sm models are trained to read."""
        count = -(-len(text) // chunk_size)
        if count <= 1:
            return []
        with self._lock:
            probs = np.asarray(self._sat().predict_proba(text))
        cuts = []
        for k in range(1, count):
            target = len(text) * k // count
            lo, hi = max(target - self.CUT_WINDOW, 0), min(target + self.CUT_WINDOW, len(text) - 1)
            # SaT also scores the ends of subword tokens, which would cut inside a word.
            ends = [p for p in range(lo, hi) if text[p] != " " and text[p + 1] == " "]
            best = max(ends, key=lambda p: (probs[p], -abs(p - target)), default=target - 1)
            cuts.append(best + 1)
        return cuts


class Punctuator:
    def __init__(
        self,
        outdir: Path,
        chunk_size: int = 4000,
        skip_exsisting: bool = True,
    ) -> None:
        self.outdir = outdir
        self.chunk_size = chunk_size
        self.skip_exsisting = skip_exsisting
        self._sent_splitter = SentSplitter()
        self._punct = _create_llm_punct()

    def _clean_raw_sub(self, sub: str) -> str:
        sub = re.sub(r"\s+", " ", sub)
        sub = re.sub(r'""+', '"', sub)
        return sub.strip()

    async def run(self, input: str) -> list[dict]:
        with span("Punctuation") as s:
            results = await self._results(input)
            sentences = sum(len(r["sentences"]) for r in results)
            lowest = min((r["edit_ratio"] for r in results), default=1)
            s.result = f"{len(results)} chunks, {sentences} sentences, edit ratio ≥ {lowest:.3f}"
        return results

    async def _results(self, input: str) -> list[dict]:
        output_path = self.outdir / "punctuate_results.json"
        if output_path.exists() and self.skip_exsisting:
            results_text = output_path.read_text(encoding="utf-8")
            results = json.loads(results_text)
            return results

        spans = await self._chunks(input)
        texts = await run_concurrently(
            [partial(self._punctuate, i, input[start:end]) for i, (start, end) in enumerate(spans)],
        )
        results = []
        for i, ((start, end), text) in enumerate(zip(spans, texts, strict=True)):
            result = await self._sentences(i, input[start:end], text)
            result["chunk_start"], result["chunk_end"] = start, end
            results.append(result)

        # save results
        with output_path.open("w", encoding="utf-8") as file:
            json.dump(results, file, ensure_ascii=False, indent=4)
        for i in range(len(spans)):
            self._cache_path(i).unlink(missing_ok=True)
        (self.outdir / "punctuate_cuts.json").unlink(missing_ok=True)

        return results

    async def _chunks(self, text: str) -> list[tuple[int, int]]:
        """The (start, end) of each chunk of `text`, cut where SaT finds sentences end. The cuts
        are saved: SaT on another device may cut elsewhere, and a resumed run must cut the chunks
        it saved."""
        path = self.outdir / "punctuate_cuts.json"
        if path.exists() and self.skip_exsisting:
            cuts = json.loads(path.read_text(encoding="utf-8"))
        else:
            cuts = await in_thread(self._sent_splitter.cut_points, text, self.chunk_size)
            path.write_text(json.dumps(cuts), encoding="utf-8")
        spans = []
        for start, end in zip([0, *cuts], [*cuts, len(text)], strict=True):
            while start < end and text[start] == " ":
                start += 1
            if start < end:
                spans.append((start, end))
        return spans

    async def _punctuate(self, i: int, chunk: str) -> str | None:
        """Chunk `i` punctuated, a sentence a line, saved the moment the model answers, so a
        stopped run keeps it. None without an answer."""
        cache = self._cache_path(i)
        if cache.exists() and self.skip_exsisting:
            return cache.read_text(encoding="utf-8")
        text = await self._punct(chunk)
        if text is None:
            log.warning(
                f"Punctuation: chunk {i + 1}'s answer has nothing in an <output> block, so it"
                " stays unpunctuated and a rerun asks again"
            )
            return None
        cache.write_text(text, encoding="utf-8")
        return text

    def _cache_path(self, i: int) -> Path:
        return self.outdir / f"punctuate_cache_{i + 1}.txt"

    async def _sentences(self, i: int, input: str, punct_text: str | None) -> dict:
        """The sentences of chunk `i`, each with the input it is: the lines of `punct_text`, or
        SaT's sentences of `input` when there is no punctuated text."""
        if punct_text is None:
            split_sents = await in_thread(self._sent_splitter.split, input)
        else:
            split_sents = await in_thread(self._sent_splitter.split_lines, punct_text)

        # to_input maps each output index to the input index it matches
        punct_text = " ".join(split_sents)
        to_input = char_map(input, punct_text)
        matches = sum(1 for k in to_input if k >= 0)
        edit_ratio = 2 * matches / (len(input) + len(punct_text))
        log.debug(f"Chunk {i + 1}: {len(split_sents)} sentences, edit ratio {edit_ratio:.3f}")

        # organize input/output pairs for each sentence
        sent_pairs = []
        output_index = 0
        for s in split_sents:
            src_indices = list(
                filter(lambda x: x >= 0, to_input[output_index : output_index + len(s)])
            )
            sent_pairs.append(
                (input[min(src_indices) : max(src_indices) + 1] if src_indices else "", s)
            )
            output_index += len(s) + 1

        return {
            "sentences": sent_pairs,
            "edit_ratio": edit_ratio,
        }


if __name__ == "__main__":
    outdir = Path("./outputs/test")
    outdir.mkdir(parents=True, exist_ok=True)
    p = Punctuator(outdir, chunk_size=4000, skip_exsisting=False)
    result = asyncio.run(
        p.run(
            "have heard before that large language models especially models like chart GPT cannot do reasoning very well this is a complaint that a lot of people have been telling about chat GPT gpt3 and other large language models but I'm going to offer you a solution called chain of thoughts so this is called chain of thoughts so I'm going to show you how you can use this concept called chain of thoughts and then you can ask a large language model a question with some reasoning that's what we call chain of thoughts and it can solve the same problem which it could not before so I'm going to give you an example first the first example is let's say we are asked chat GPT take the last letters of the words in Lady Gaga and concatenate them so as a human being you know the first word is Lady and the second word is Gaga and the first the last letter of the first word is Lay Y and the last letter of the second word is a so what would be the output the output would be yeah Y and a but what does the charge GPT do charge GPT thinks that it would be AGA Gaga something so it it did not understand what we exactly asked so now if this is the kind of problem that you want to solve and you want to solve using chart GPT then we are going to give chain of thoughts an intermediate step with some reasoning and explanation that can educate the large language model to solve so what are we going to do we're going to give an example we're going to tell charge apt the take the last letters of the words the same thing like exactly the same thing that you just give in the first case you just gave the question and you expected an answer which was wrong in the second case you are going to introduce chain of thoughts now this is your input question now this is where you are going to give the Chain of Thought and then based on the Chain of Thought you are going to give your actual question now let's start again so I've given that there is a question here and for that I am actually explaining charging PT that what should be the answer so I'm saying last letter of ladies y then the last letter of Gaga is a concatenating them is y a so the answer is yeah and then I'm asking my actual question so I'm saying take the last letters of the words in New Delhi and Concord coordinate them and then it exactly does what I wanted to do the last letter of new is W the last letter of daily is I and concatenating them is we and the answer is V and I can go on and ask more of this again I can give the same question I can ask you to do Lady Gaga or I can I can probably ask another question I can say take the last letter of the words uh let's say you for what does new or lens or lens and I can send it send this and then you can see what it does it says last letter of new is W the last letter of audience is s so it does ws and this is not only for this case so chain of thoughts work really good for math reasoning for logical reasoning and lot of other cases for example if you want charge APD to count something let's say you wanted to do a simple math problem you can say one little coder had 10 apples and he ate two of them how many apples did he finally have now most likely chargpt would give you the right answer because recently they have improved the math capability but if it does not have that math capability instead of now simply giving the question and then expecting an answer what you can do is you can give an example so I can say Q question I can give this question but instead of one little quota I can I can say this guy had 10 apples and he had two of them and in the answer you can describe he initially initially initially had 10 apples out of that he ate two that means 10 minus 2 that is equal to 8 so he had eight apples eight is the answer and then you ask your next question you ask the actual question that you want to ask so you can say one little coder had 100 computers out of that he through 10 computers he gave another 10 to his friends how many computers did he finally have",
        )
    )
    for chunk in result:
        print("=" * 32)
        for sent in chunk["sentences"]:
            print(sent)
