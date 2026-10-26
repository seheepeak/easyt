from pathlib import Path

from easyt.notify import send_document
from easyt.stages.punctuate import Punctuator
from easyt.youtube import Loader


class Pipeline_base:
    def __init__(
        self,
        transcript: list[dict],  # List of dicts with keys "start", "duration", "text"
        loader: Loader,
        source_lang: str = "en",
        chunk_size: int = 4 * 1000,
        skip_exsisting: bool = True,
    ) -> None:
        self.input_transcript = transcript
        self.loader = loader
        self.source_lang = source_lang
        self.chunk_size = chunk_size
        self.skip_exsisting = skip_exsisting
        self.punct = Punctuator(
            loader.outdir,
            chunk_size=chunk_size,
            skip_exsisting=skip_exsisting,
        )

    async def send_document_to_telegram(self, path: Path) -> None:
        """Send `path` to the chat, with the start of the video's description."""
        description = self.loader.description or ""
        await send_document(
            path, description[:150] + "..." if len(description) > 150 else description
        )
