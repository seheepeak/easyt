"""What the CLI and the Telegram bot run."""

import re
from types import SimpleNamespace

import pytest

import easyt.jobs
from easyt.gdrive import DriveAuthError
from easyt.jobs import article_to_translate, run_article, run_youtube


async def test_a_run_checks_the_drive_sign_in_before_any_work(monkeypatch):
    async def signed_out():
        raise DriveAuthError("Google Drive has no sign-in: run `easyt auth`")

    def load(*args, **kwargs):
        pytest.fail("the video was loaded before the sign-in was checked")

    monkeypatch.setattr(easyt.jobs, "check_drive_sign_in", signed_out)
    monkeypatch.setattr(easyt.jobs, "Loader", load)
    with pytest.raises(DriveAuthError):
        await run_youtube("https://youtu.be/Qmafkf_08uQ", "translate")


async def test_a_playlist_gets_a_copy_of_each_file_a_video_made(monkeypatch, tmp_path):
    async def signed_in():
        pass

    async def upload(folder):
        return "folder-id"

    async def single(video_id, parent_dir, **options):
        outdir = parent_dir / video_id
        outdir.mkdir(parents=True)
        paths = [outdir / name for name in ("v.mp4", "v.srt", "v.en.srt")]
        for path in paths:
            path.touch()
        return None, paths[0], paths[1:]

    monkeypatch.setattr(easyt.jobs, "check_drive_sign_in", signed_in)
    monkeypatch.setattr(easyt.jobs, "upload_to_gdrive", upload)
    monkeypatch.setattr(easyt.jobs, "run_pipeline_single", single)
    monkeypatch.setattr(easyt.jobs, "OUTPUTS_DIR", tmp_path)
    monkeypatch.setattr(
        easyt.jobs, "Loader", SimpleNamespace(get_playerlist_info=lambda _: ("List", ["a"]))
    )
    await run_youtube("https://www.youtube.com/playlist?list=PLabc", "translate")

    copies = {path.name for path in (tmp_path / "List").iterdir() if path.is_file()}
    assert copies == {"(1) v.mp4", "(1) v.srt", "(1) v.en.srt"}


async def test_an_article_is_kept_in_a_folder_named_by_the_day_its_site_and_title(
    monkeypatch, tmp_path
):
    made = []

    class Pipeline:
        def __init__(self, article, html_path, target_lang):
            made.append((html_path, target_lang))

        async def run(self):
            return None

    monkeypatch.setattr(easyt.jobs, "OUTPUTS_DIR", tmp_path)
    monkeypatch.setattr(easyt.jobs, "Pipeline_article", Pipeline)
    text = '---\nsite: "The Paper"\n---\n\n# Stocks Fell\n\nWords.\n\n<!-- EOF -->\n'
    await run_article(text)

    [(html_path, target_lang)] = made
    assert re.fullmatch(r"\[\d{4}-\d\d-\d\d\] \(The Paper\) Stocks Fell", html_path.parent.name)
    assert (html_path.name, target_lang) == ("Stocks Fell.html", "ko")
    assert (html_path.parent / "article.md").read_text(encoding="utf-8") == text


def test_an_article_already_in_the_host_language_is_refused(monkeypatch):
    with pytest.raises(ValueError, match="already in Korean"):
        article_to_translate("# 제목\n\n이 메모는 이번에 처음 알려졌다.")
    assert article_to_translate("# Title\n\nThe memo had not been reported before.").lang == "en"
    # The host's language, whatever script its locale implies.
    monkeypatch.setattr(easyt.jobs, "host_language", lambda: "zh-Hant")
    with pytest.raises(ValueError, match="already in Chinese"):
        article_to_translate("# 标题\n\n中国情报机构招募了一名女商人。")
