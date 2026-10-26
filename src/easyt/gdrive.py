import os
import socket
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

from easyt import EASYT_HOME, log
from easyt.utils.spans import current_span, span
from easyt.utils.threads import checkpoint, in_thread

socket.setdefaulttimeout(300)

# Small enough for a cancelled job to stop within seconds; googleapiclient's default is 100 MB.
_CHUNK_SIZE = 8 * 1024 * 1024


class DriveAuthError(Exception):
    """Google Drive has no usable sign-in, and `easyt auth` has to make one."""


class GDriveUploader:
    """Upload files and directories to Google Drive using OAuth 2.0 authentication."""

    SCOPES: ClassVar[list[str]] = [
        "https://www.googleapis.com/auth/drive.file",
        "https://www.googleapis.com/auth/drive.readonly",
    ]

    def __init__(self):
        """Initialize the Google Drive uploader with OAuth credentials."""
        credentials_file = os.getenv("GOOGLE_OAUTH_CREDENTIALS_FILE")
        # A relative path is read from ~/.easyt, wherever easyt runs.
        self.credentials_file = (
            EASYT_HOME / Path(credentials_file).expanduser() if credentials_file else None
        )
        self.target_folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
        self._service = None
        self._creds = None

    @property
    def is_configured(self) -> bool:
        """Check if Google Drive upload is properly configured."""
        return bool(self.credentials_file and self.target_folder_id)

    @property
    def token_file(self) -> Path:
        """Get token file path (same directory as credentials file)."""
        return self.credentials_file.parent / "gdrive_token.json"

    def _get_credentials(self) -> Credentials:
        """The saved OAuth credentials, refreshed if they expired. Signing in is left to
        `login()`: the bot has no browser, and would wait for the sign-in forever."""
        if self._creds is not None:
            return self._creds

        creds = None
        if self.token_file.exists():
            creds = Credentials.from_authorized_user_file(str(self.token_file), self.SCOPES)
        if creds is None or not creds.valid:
            if not (creds and creds.expired and creds.refresh_token):
                raise DriveAuthError("Google Drive has no sign-in: run `easyt auth`")
            try:
                creds.refresh(Request())
            except RefreshError as e:
                raise DriveAuthError(f"Google Drive sign-in expired ({e}): run `easyt auth`") from e
            self.token_file.write_text(creds.to_json())

        self._creds = creds
        return creds

    def login(self) -> None:
        """Sign in through the browser, and save the token later runs refresh."""
        flow = InstalledAppFlow.from_client_secrets_file(str(self.credentials_file), self.SCOPES)
        self._creds = flow.run_local_server(port=0)
        self.token_file.write_text(self._creds.to_json())
        log.success(f"Saved OAuth token to: {self.token_file}")

    @property
    def service(self):
        """Lazy-load the Google Drive service."""
        if self._service is None:
            creds = self._get_credentials()
            self._service = build("drive", "v3", credentials=creds)
        return self._service

    def create_folder(self, name: str, parent_id: str) -> str:
        """Create a folder in Google Drive and return its ID."""
        file_metadata = {
            "name": name,
            "mimeType": "application/vnd.google-apps.folder",
            "parents": [parent_id],
        }
        folder = self.service.files().create(body=file_metadata, fields="id").execute()
        return folder.get("id")

    def download_file(self, file_id: str, dest_path: Path) -> Path:
        """Download a file from Google Drive to a local path, in percent on the current span."""
        request = self.service.files().get_media(fileId=file_id, supportsAllDrives=True)
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        with open(dest_path, "wb") as f:
            downloader = MediaIoBaseDownload(f, request, chunksize=_CHUNK_SIZE)
            done = False
            while not done:
                checkpoint()
                status, done = downloader.next_chunk()
                if status and (s := current_span()) is not None:
                    s.update(status.progress() * 100)
        return dest_path

    def upload_file(self, file_path: Path, parent_id: str) -> str:
        """Upload a single file to Google Drive and return its ID."""
        file_metadata = {
            "name": file_path.name,
            "parents": [parent_id],
        }
        media = MediaFileUpload(str(file_path), chunksize=_CHUNK_SIZE, resumable=True)
        request = self.service.files().create(body=file_metadata, media_body=media, fields="id")
        # Chunk by chunk rather than execute(), so a cancelled job stops uploading.
        file = None
        while file is None:
            checkpoint()
            _, file = request.next_chunk()
        return file.get("id")

    def upload_directory(self, directory: Path, parent_folder_id: str | None = None) -> str:
        """
        Upload an entire directory to Google Drive, preserving structure.

        Args:
            directory: Path to the local directory to upload
            parent_folder_id: Google Drive folder ID to upload into (uses default if None)

        Returns:
            The Google Drive folder ID of the uploaded directory
        """
        if parent_folder_id is None:
            parent_folder_id = self.target_folder_id

        # Create the root folder for this directory
        folder_id = self.create_folder(directory.name, parent_folder_id)
        log.info(f"Created Google Drive folder: {directory.name}")

        # Upload all files in the directory
        for item in sorted(directory.iterdir()):
            if item.is_file():
                self.upload_file(item, folder_id)
                log.debug(f"Uploaded: {item.name}")
                if (s := current_span()) is not None:
                    s.advance()
            elif item.is_dir():
                # Recursively upload subdirectories
                self.upload_directory(item, folder_id)

        return folder_id

    def list_videos(self, folder_id: str, since: datetime, keywords: list[str]) -> list[dict]:
        """The videos in `folder_id` made after `since` whose names hold every one of `keywords`,
        newest first."""
        date_filter = since.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S")

        query = (
            f"'{folder_id}' in parents"
            f" and mimeType contains 'video/'"
            f" and createdTime > '{date_filter}'"
            f" and trashed = false"
        )

        # AND keyword filter on filename
        for kw in keywords:
            query += f" and name contains '{kw}'"

        files = []
        page_token = None

        while True:
            response = (
                self.service.files()
                .list(
                    q=query,
                    fields="nextPageToken, files(id, name, createdTime, mimeType, size)",
                    orderBy="createdTime desc",
                    pageToken=page_token,
                    pageSize=100,
                    supportsAllDrives=True,
                    includeItemsFromAllDrives=True,
                )
                .execute()
            )
            files.extend(response.get("files", []))
            page_token = response.get("nextPageToken")
            if not page_token:
                break

        return files


async def check_drive_sign_in() -> None:
    """Raise DriveAuthError now if the upload would, rather than after the work before it."""
    uploader = GDriveUploader()
    if uploader.is_configured:
        await in_thread(uploader._get_credentials)


async def upload_to_gdrive(outdir: Path) -> str | None:
    """
    Upload the output directory to Google Drive.

    Returns:
        The Google Drive folder ID if successful, None otherwise. A sign-in that is missing or
        expired raises DriveAuthError instead, for the user to run `easyt auth`.
    """
    uploader = GDriveUploader()

    if not uploader.is_configured:
        log.warning(
            "GOOGLE_OAUTH_CREDENTIALS_FILE or GOOGLE_DRIVE_FOLDER_ID is not set. "
            "Skipping Google Drive upload."
        )
        return None

    files = sum(1 for path in outdir.rglob("*") if path.is_file())
    try:
        with span("Upload", total=files) as s:
            folder_id = await in_thread(uploader.upload_directory, outdir)
            s.result = f"{files} files"
        return folder_id
    except DriveAuthError:
        raise
    except Exception as e:
        log.error(f"Failed to upload to Google Drive: {e}")
        return None
