"""What the CLI and the Telegram bot run."""

import pytest

import easyt.jobs
from easyt.gdrive import DriveAuthError
from easyt.jobs import run_youtube


async def test_a_run_checks_the_drive_sign_in_before_any_work(monkeypatch):
    async def signed_out():
        raise DriveAuthError("Google Drive has no sign-in: run `easyt auth`")

    def load(*args, **kwargs):
        pytest.fail("the video was loaded before the sign-in was checked")

    monkeypatch.setattr(easyt.jobs, "check_drive_sign_in", signed_out)
    monkeypatch.setattr(easyt.jobs, "Loader", load)
    with pytest.raises(DriveAuthError):
        await run_youtube("https://youtu.be/Qmafkf_08uQ", "translate")
