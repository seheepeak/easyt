import warnings
from pathlib import Path

from dotenv import load_dotenv

from easyt.utils.logging_config import SUCCESS, log, setup_logging

__all__ = ["EASYT_HOME", "OUTPUTS_DIR", "SUCCESS", "log", "setup_logging"]

# Where .env, the Google Drive credentials and the outputs live, whatever directory easyt runs in.
EASYT_HOME = Path.home() / ".easyt"
OUTPUTS_DIR = EASYT_HOME / "outputs"

load_dotenv(EASYT_HOME / ".env")

# ignore FutureWarnings
warnings.simplefilter(action="ignore", category=FutureWarning)
