"""What the CLI and the Telegram bot run: a pipeline on each video of a YouTube URL, then the
Google Drive upload."""

import functools
import shutil
from pathlib import Path
from typing import Literal

from yt_dlp.utils import sanitize_filename

from easyt import OUTPUTS_DIR, log
from easyt.gdrive import check_drive_sign_in, upload_to_gdrive
from easyt.languages import host_language
from easyt.pipeline.aterm import Pipeline_aterm
from easyt.pipeline.standard import Pipeline
from easyt.utils.spans import span
from easyt.utils.threads import in_thread
from easyt.youtube import Loader, parse_youtube_url

type Mode = Literal["translate", "aterm", "transcript"]


async def run_pipeline_single(
    video_id: str,
    parent_dir: Path,
    mode: Mode,
    target_lang: str,
    resolution: str,
):
    loader = await in_thread(Loader, video_id, parent_dir, resolution=resolution)
    lang = f"{loader.lang} → {target_lang}" if mode == "translate" else loader.lang
    with span(loader.title, f"{loader.author} · {lang}") as s:

        def skip(why: str, video_path: Path | None = None):
            log.warning(f"{why}. Skipping...")
            s.result = "skipped"
            return loader, video_path, None

        if loader.lang is None:
            return skip("YouTube names no caption track in the video's own language")
        if mode == "aterm" and loader.lang != "en":
            return skip(f"aterm picks out English terms, and this video is in {loader.lang}")

        # load video and transcript
        video_path, transcript = await in_thread(loader.load, transcript_only=mode != "translate")
        if transcript is None:
            return skip("No transcript found", video_path)

        # run pipeline
        if mode == "aterm":
            return loader, None, await Pipeline_aterm(transcript, loader).run()
        pipeline = Pipeline(
            transcript,
            loader,
            source_lang=loader.lang,
            target_lang=target_lang,
            transcript_only=mode == "transcript",
        )
        return loader, video_path, await pipeline.run()


async def run_youtube(
    url: str,
    mode: Mode,
    *,
    target_lang: str | None = None,
    resolution: str = "1080p",
) -> tuple[str, str | None]:
    """Run `mode` on the video or playlist at `url`, translating into `target_lang` or else the
    host's language, and upload what it made to Google Drive. Returns the title and the Drive
    folder id, which is None if nothing was uploaded."""
    video_id, playlist_id, picks = parse_youtube_url(url)
    await check_drive_sign_in()
    run_single = functools.partial(
        run_pipeline_single,
        mode=mode,
        target_lang=target_lang or host_language(),
        resolution=resolution,
    )
    if video_id:
        loader, _, _ = await run_single(video_id, OUTPUTS_DIR)
        return loader.title, await upload_to_gdrive(loader.outdir)

    title, playlist = await in_thread(Loader.get_playerlist_info, playlist_id)
    if len(playlist) == 0:
        raise RuntimeError(f"Cannot find any video in the playlist {playlist_id}")

    parent_dir = OUTPUTS_DIR / sanitize_filename(title)
    picked = range(len(playlist))[picks]
    with span(title, total=len(picked)) as s:
        for idx in picked:
            _, video_path, out_path = await run_single(playlist[idx], parent_dir)

            # copy video and srt to the playlist directory
            for path in (video_path, out_path):
                if path is not None:
                    await in_thread(shutil.copy, path, parent_dir / f"({idx + 1}) {path.name}")
            s.advance()

    return title, await upload_to_gdrive(parent_dir)
