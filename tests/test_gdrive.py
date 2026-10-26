"""Where the Google Drive sign-in is read from, and what happens without one."""

import pytest

import easyt.gdrive
from easyt.gdrive import DriveAuthError, GDriveUploader, check_drive_sign_in, upload_to_gdrive


def test_a_relative_credentials_path_is_read_from_easyt_home(monkeypatch, tmp_path):
    monkeypatch.setattr(easyt.gdrive, "EASYT_HOME", tmp_path)
    monkeypatch.setenv("GOOGLE_OAUTH_CREDENTIALS_FILE", ".credentials/client_secret.json")
    assert GDriveUploader().credentials_file == tmp_path / ".credentials/client_secret.json"


def test_without_a_token_drive_asks_for_easyt_auth_rather_than_a_browser(monkeypatch, tmp_path):
    monkeypatch.setenv("GOOGLE_OAUTH_CREDENTIALS_FILE", str(tmp_path / "client_secret.json"))
    with pytest.raises(DriveAuthError, match="easyt auth"):
        _ = GDriveUploader().service


async def test_an_upload_without_a_sign_in_fails_rather_than_being_skipped(monkeypatch, tmp_path):
    monkeypatch.setenv("GOOGLE_OAUTH_CREDENTIALS_FILE", str(tmp_path / "client_secret.json"))
    monkeypatch.setenv("GOOGLE_DRIVE_FOLDER_ID", "folder")
    with pytest.raises(DriveAuthError):
        await upload_to_gdrive(tmp_path)
    with pytest.raises(DriveAuthError):
        await check_drive_sign_in()


async def test_without_drive_set_up_there_is_no_sign_in_to_check(monkeypatch):
    monkeypatch.delenv("GOOGLE_DRIVE_FOLDER_ID", raising=False)
    await check_drive_sign_in()
