"""Which video or playlist a YouTube URL names, which caption track the Loader asks YouTube for,
and how it reads it."""

import pytest

from easyt.youtube import _json3_cues, _original_track, parse_youtube_url


def _audio(lang: str | None, preference: int = -1) -> dict:
    return {"acodec": "opus", "language": lang, "language_preference": preference}


def _track(lang: str, tlang: str | None = None) -> list[dict]:
    url = f"https://www.youtube.com/api/timedtext?v=x&lang={lang}&fmt=json3"
    if tlang:
        url += f"&tlang={tlang}"
    return [{"ext": "json3", "url": url}]


def test_a_dubbed_video_gets_the_captions_of_its_original_audio():
    """What hit HTTP 429: "en" also held the Arabic dub's captions translated into English."""
    info = {
        "formats": [_audio("en-US", 10), _audio("ar"), _audio("de-DE")],
        "automatic_captions": {
            "en": _track("en") + _track("ar", tlang="en"),
            "en-orig": _track("en"),
            "ar-orig": _track("ar"),
            "de-DE-orig": _track("de-DE"),
        },
    }
    assert _original_track(info) == "en-orig"


def test_a_korean_video_with_dubs_gets_korean_captions():
    info = {
        "formats": [_audio("en-US"), _audio("id"), _audio("ko", 10)],
        "automatic_captions": {
            "en-orig": _track("en"),
            "id-orig": _track("id"),
            "ko-orig": _track("ko"),
        },
    }
    assert _original_track(info) == "ko-orig"


def test_a_single_audio_track_takes_the_language_of_its_captions():
    info = {
        "formats": [_audio("ko")],
        "automatic_captions": {"en": _track("ko", tlang="en"), "ko-orig": _track("ko")},
    }
    assert _original_track(info) == "ko-orig"


def test_human_written_subtitles_come_before_generated_ones():
    info = {
        "formats": [_audio("en")],
        "subtitles": {"ar": _track("ar"), "de": _track("de"), "en": _track("en")},
        "automatic_captions": {"en-orig": _track("en")},
    }
    assert _original_track(info) == "en"


def test_a_regional_language_falls_back_to_the_base_language():
    info = {
        "formats": [_audio("en-US", 10)],
        "subtitles": {"en-GB": _track("en-GB"), "en": _track("en")},
    }
    assert _original_track(info) == "en"


def test_a_translation_is_never_taken():
    info = {"formats": [_audio("ko")], "automatic_captions": {"ko": _track("en", tlang="ko")}}
    assert _original_track(info) is None


def test_the_chat_of_a_live_stream_is_not_a_transcript():
    info = {"formats": [_audio(None)], "subtitles": {"live_chat": [{"ext": "json"}]}}
    assert _original_track(info) is None


def test_without_a_language_english_is_the_default():
    """Me at the zoo: human-written English and German subtitles, and nothing else."""
    assert _original_track({"subtitles": {"de": _track("de"), "en": _track("en")}}) == "en"


def test_without_a_language_a_lone_human_written_track_is_taken():
    assert _original_track({"subtitles": {"ko": _track("ko")}}) == "ko"


def test_without_a_language_several_other_tracks_are_not_guessed_between():
    assert _original_track({"subtitles": {"de": _track("de"), "fr": _track("fr")}}) is None


def test_a_json3_track_becomes_one_subtitle_per_cue():
    track = {
        "events": [
            {"tStartMs": 0, "dDurationMs": 2500},  # the caption window, with no text
            {
                "tStartMs": 100,
                "dDurationMs": 2400,
                "segs": [{"utf8": "Hello"}, {"utf8": " there", "tOffsetMs": 400}],
            },
            {"tStartMs": 2500, "dDurationMs": 10, "aAppend": 1, "segs": [{"utf8": "\n"}]},
            {"tStartMs": 2500, "dDurationMs": 3000, "segs": [{"utf8": "  and welcome\nback "}]},
        ]
    }
    assert _json3_cues(track) == [
        {"text": "Hello there", "start": 0.1, "duration": 2.4},
        {"text": "and welcome back", "start": 2.5, "duration": 3.0},
    ]


PLAYLIST = "https://www.youtube.com/playlist?list=PLx0sYbCqOb8TBPRdmBHs5Iftvv9TPboYG"


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/watch?v=Qmafkf_08uQ",
        "https://youtube.com/watch?v=Qmafkf_08uQ&t=42s",
        "https://m.youtube.com/watch?v=Qmafkf_08uQ",
        "https://youtu.be/Qmafkf_08uQ?si=AbCdEfGh",
        "https://www.youtube.com/shorts/Qmafkf_08uQ",
        "https://www.youtube.com/live/Qmafkf_08uQ?feature=share",
    ],
)
def test_a_video_url_names_the_video(url):
    assert parse_youtube_url(url) == ("Qmafkf_08uQ", None, slice(None))


def test_a_video_opened_from_a_playlist_is_that_video():
    url = "https://www.youtube.com/watch?v=Qmafkf_08uQ&list=PLx0sYbCqOb8TBPRdmBHs5Iftvv9TPboYG"
    assert parse_youtube_url(url) == ("Qmafkf_08uQ", None, slice(None))


def test_a_playlist_url_names_the_playlist_and_may_pick_some_of_its_videos():
    assert parse_youtube_url(PLAYLIST) == (None, "PLx0sYbCqOb8TBPRdmBHs5Iftvv9TPboYG", slice(None))
    assert parse_youtube_url(PLAYLIST + "[2:5]")[2] == slice(2, 5)
    assert parse_youtube_url(PLAYLIST + "[-3:]")[2] == slice(-3, None)


@pytest.mark.parametrize(
    "url",
    [
        "Qmafkf_08uQ",
        "https://www.youtube.com/@channel",
        "https://vimeo.com/123456789",
        "https://www.youtube.com/watch?v=tooshort",
        "https://youtu.be/Qmafkf_08uQ[0:2]",
        "https://www.youtube.com/embed/videoseries?list=PLx0sYbCqOb8TBPRdmBHs5Iftvv9TPboYG",
    ],
)
def test_anything_else_is_refused(url):
    with pytest.raises(ValueError):
        parse_youtube_url(url)
