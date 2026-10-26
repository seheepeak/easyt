"""YouTube: which video or playlist a URL names, and the caption track and file of a video."""

import json
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import yt_dlp

from easyt import log
from easyt.utils.spans import current_span, span
from easyt.utils.threads import checkpoint

_PICKS = re.compile(r"\[(-?\d*):(-?\d*)\]$")
_VIDEO_ID = re.compile(r"[\w-]{11}")


def parse_youtube_url(url: str) -> tuple[str | None, str | None, slice]:
    """The video id or the playlist id `url` names, the other one None, and which of the
    playlist's videos a trailing `[start:end]` picks."""
    picks = slice(None)
    if m := _PICKS.search(url):
        picks = slice(int(m[1]) if m[1] else None, int(m[2]) if m[2] else None)
        url = url[: m.start()]
    parts = urlparse(url)
    host = parts.netloc.lower().removeprefix("www.").removeprefix("m.")
    path = parts.path.strip("/").split("/")
    query = parse_qs(parts.query)

    video = playlist = None
    if host == "youtu.be":
        video = path[0]
    elif host == "youtube.com" and path == ["watch"]:
        # A video opened from a playlist also names the playlist; the video is what was picked.
        video = query.get("v", [None])[0]
    elif host == "youtube.com" and path[0] in ("shorts", "live") and len(path) == 2:
        video = path[1]
    elif host == "youtube.com" and path == ["playlist"]:
        playlist = query.get("list", [None])[0]

    if video and _VIDEO_ID.fullmatch(video):
        if picks != slice(None):
            raise ValueError(f"[start:end] picks videos of a playlist, not of one video: {url}")
        return video, None, picks
    if playlist:
        return None, playlist, picks
    raise ValueError(f"Not the URL of a YouTube video or playlist: {url}")


def _original_track(info: dict) -> str | None:
    """The one caption track to fetch: the video's own language, never a machine translation.

    The video's language is the language of the audio track YouTube labels
    original; yt-dlp ranks that track first by language_preference. A video
    with a single audio track has no such label, so yt-dlp gives that audio
    the language of the captions YouTube generated from it. Human-written
    subtitles in that language come first, then the generated captions,
    "<lang>-orig". A dubbed video has an "-orig" track for every dub, so the
    suffix alone does not mark the original. Any other track may be
    machine-translated, and YouTube answers those with HTTP 429 (yt-dlp
    issue #13831).

    With one audio track and no generated captions, nothing names the
    language. English subtitles are taken then, or the only human-written
    track if there is just one.
    """
    # yt-dlp lists the chat of a live stream as a subtitle track.
    manual = [k for k in info.get("subtitles") or {} if k != "live_chat"]
    audio = [
        f for f in info.get("formats") or [] if f.get("language") and f.get("acodec") != "none"
    ]
    lang = "en"
    if audio:
        lang = max(audio, key=lambda f: f.get("language_preference") or -1)["language"]
    base = lang.split("-")[0]
    # "en-US" falls back to "en", then to any other English subtitles.
    for key in (lang, base):
        if key in manual:
            return key
    for key in manual:
        if key.split("-")[0] == base:
            return key
    captions = info.get("automatic_captions") or {}
    for key in (f"{lang}-orig", f"{base}-orig"):
        if key in captions:
            return key
    if not audio and len(manual) == 1:
        return manual[0]
    return None


def _download_progress(status: dict) -> None:
    """How far yt-dlp's download of a file is, on the span open in its thread. A download in a
    thread also stops here once its job is cancelled."""
    checkpoint()
    total = status.get("total_bytes") or status.get("total_bytes_estimate")
    if status["status"] == "downloading" and total and (s := current_span()) is not None:
        s.update(100 * status.get("downloaded_bytes", 0) / total)


def _json3_cues(track: dict) -> list[dict]:
    """A json3 caption track as subtitles, one per cue: the track's own segmentation."""
    cues = []
    for event in track.get("events", []):
        segs = event.get("segs")
        if not segs:
            continue
        if text := " ".join("".join(s.get("utf8", "") for s in segs).split()):
            cues.append(
                {
                    "text": text,
                    "start": event.get("tStartMs", 0) / 1000,
                    "duration": event.get("dDurationMs", 0) / 1000,
                }
            )
    return cues


class Loader:
    def __init__(
        self,
        video_id: str,
        parent_dir: Path,
        resolution: str,
        skip_exsisting: bool = True,
    ) -> None:
        self.video_id = video_id
        self.url = f"https://www.youtube.com/watch?v={video_id}"
        self.parent_dir = parent_dir
        self.resolution = resolution
        self.skip_exsisting = skip_exsisting
        # The output folder's date, fixed so a download after midnight lands in the same folder.
        self.date = datetime.now().astimezone().strftime("%Y-%m-%d")

        # Downloads use shallow copies: yt-dlp writes into its info; deepcopy breaks on lazy lists.
        with yt_dlp.YoutubeDL(self.create_ydl_opts()) as ydl:
            self.info_dict = ydl.extract_info(self.url, download=False)
            self.video_path = Path(ydl.prepare_filename(self.info_dict))

        self.track = _original_track(self.info_dict)
        # The video's own language, as its caption track names it.
        self.lang = self.track.split("-")[0] if self.track else None

        self.outdir = self.video_path.parent
        self.outdir.mkdir(parents=True, exist_ok=True)

    @property
    def title(self):
        return self.info_dict.get("title", None)

    @property
    def author(self):
        return self.info_dict.get("uploader", None)

    @property
    def description(self):
        return self.info_dict.get("description", None)

    def load(self, transcript_only: bool = False) -> tuple[Path | None, list[dict] | None]:
        with span("Transcript") as s:
            transcript = self._download_transcript()

            # validate transcript
            if transcript is not None:
                transcript = self._validate_subtitles(transcript)
                validated_transcript_path = self.outdir / "validated_transcript.json"
                with validated_transcript_path.open("w", encoding="utf-8") as file:
                    json.dump(transcript, file, ensure_ascii=False, indent=4)
                s.result = f"{len(transcript)} subtitles"
        if transcript_only:
            return None, transcript
        with span("Download", self.resolution, percent=True) as s:
            video_path = self._download_video_by_yt_dlp()
            s.result = f"{video_path.stat().st_size / 1e6:.0f} MB"
        return video_path, transcript

    def _download_transcript(self) -> list[dict] | None:
        # check if the transcript is already downloaded
        orginal_transcript_path = self.outdir / "original_transcript.json"
        if orginal_transcript_path.exists() and self.skip_exsisting:
            return json.loads(orginal_transcript_path.read_text(encoding="utf-8"))
        if self.track is None:
            return None

        # json3 rather than the other formats: its cues do not overlap, so
        # nothing has to be deduplicated afterwards.
        ydl_opts = {
            "skip_download": True,
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "writesubtitles": True,
            "writeautomaticsub": True,
            "subtitleslangs": [self.track],
            "subtitlesformat": "json3",
            "outtmpl": str(self.outdir / "original_transcript.%(ext)s"),
        }
        # Downloaded from the info the track was picked from. Extracting again
        # would cost more requests, and YouTube may answer them with other tracks.
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.process_ie_result(dict(self.info_dict), download=True)

        subtitles = info.get("requested_subtitles") or {}
        if not subtitles:
            return None
        track, data = next(iter(subtitles.items()))
        kind = "manual" if track in (info.get("subtitles") or {}) else "auto-generated"
        log.info(f"Transcript: {track} ({kind})")

        raw = json.loads(Path(data["filepath"]).read_text(encoding="utf-8"))
        json_subs = _json3_cues(raw)
        orginal_transcript_path.write_text(
            json.dumps(json_subs, ensure_ascii=False, indent=4), encoding="utf-8"
        )
        return json_subs

    def _validate_subtitles(self, subtitles: list[dict]) -> list[dict]:
        """Validate subtitles content and delete line breaks

        Args:
            subtitles (List[Dict]): List of subtitles {"start": float, "duration": float, "text": str}

        Returns:
            List[Dict]: Same list of subtitles, but validated
        """
        re_tag = re.compile(r"<.*?>")
        re_square_bracket = re.compile(r"\[.*?\]")
        re_ws_multiple = re.compile(r"\s+")
        re_quote_multiple = re.compile(r'""+')

        for i, sub in enumerate(subtitles):
            text = sub["text"]
            text = re_tag.sub("", text)
            text = re_square_bracket.sub("", text)
            text = re_ws_multiple.sub(" ", text).strip()
            text = re_quote_multiple.sub('"', text)
            sub["text"] = text

            if i + 1 < len(subtitles):
                start, duration = sub["start"], sub["duration"]
                next_start = subtitles[i + 1]["start"]
                if start + duration > next_start:
                    sub["duration"] = next_start - start

        return subtitles

    def create_ydl_opts(self):
        resolution = m.group(1) if (m := re.match(r"(\d+)p?", self.resolution)) else None
        video_format = "bestvideo" if resolution is None else f"bestvideo[height<={resolution}]"
        audio_format = "bestaudio"
        merged_format = "best" if resolution is None else f"best[height<={resolution}]"
        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "format": f"{video_format}+{audio_format}/{merged_format}",
            # ".NB" truncates a field to N bytes: a path component must stay
            # under the 255-byte filesystem limit, and Korean costs 3 bytes per character.
            "outtmpl": str(
                self.parent_dir
                / f"[{self.date}] (%(uploader).50B) %(title).150B"
                / "%(title).150B.%(ext)s"
            ),
            "postprocessors": [
                {
                    "key": "FFmpegVideoConvertor",
                    "preferedformat": "mp4",
                }
            ],
            "merge_output_format": "mp4",
            "progress_hooks": [_download_progress],
            # "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        }
        return ydl_opts

    def _download_video_by_yt_dlp(self):
        if self.video_path.exists() and self.skip_exsisting:
            return self.video_path
        self.video_path.unlink(missing_ok=True)

        # download the video/audio from the info already extracted
        with yt_dlp.YoutubeDL(self.create_ydl_opts()) as ydl:
            ydl.process_ie_result(dict(self.info_dict), download=True)
        return self.video_path

    @staticmethod
    def get_playerlist_info(playlist_id: str):
        ydl_opts = {
            "no_warnings": True,
            "extract_flat": True,
            "quiet": True,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            url = f"https://www.youtube.com/playlist?list={playlist_id}"
            playlist_info = ydl.extract_info(url, download=False)
            if not playlist_info:
                raise RuntimeError(f"Cannot find the playlist {playlist_id}")
            title = playlist_info.get("title", "")
            entries = playlist_info.get("entries", [])
        return title, [e.get("id", "") for e in entries]
