"""The Telegram bot as a systemd user service, and the commands that drive it."""

import os
import subprocess
import sys
from pathlib import Path
from typing import NoReturn

from easyt import EASYT_HOME, log

UNIT = "easyt-bot"
UNIT_FILE = Path.home() / ".config" / "systemd" / "user" / f"{UNIT}.service"


def unit_text(easyt: Path) -> str:
    """The unit that runs `easyt bot` from `easyt` in ~/.easyt, restarted when it fails."""
    return f"""\
[Unit]
Description=EasyT Telegram bot

[Service]
ExecStart={easyt} bot
WorkingDirectory={EASYT_HOME}
Environment=PYTHONUNBUFFERED=1
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
"""


def install() -> None:
    """Write the unit for the easyt running now, and (re)start it, also at every boot."""
    EASYT_HOME.mkdir(exist_ok=True)
    UNIT_FILE.parent.mkdir(parents=True, exist_ok=True)
    UNIT_FILE.write_text(unit_text(Path(sys.executable).with_name("easyt")))
    _systemctl("daemon-reload")
    _systemctl("enable", UNIT)
    _systemctl("restart", UNIT)
    log.success(f"{UNIT} is running: `easyt log` follows it")


def uninstall() -> None:
    """Stop the service and remove its unit. ~/.easyt stays."""
    if not UNIT_FILE.exists():
        log.warning(f"{UNIT} is not installed")
        return
    _systemctl("disable", "--now", UNIT)
    UNIT_FILE.unlink()
    _systemctl("daemon-reload")
    log.success(f"{UNIT} is stopped and removed")


def systemctl(command: str) -> NoReturn:
    """`systemctl --user COMMAND` on the service: start, stop, restart or status."""
    os.execvp("systemctl", ["systemctl", "--user", command, UNIT])


def follow_log() -> NoReturn:
    os.execvp("journalctl", ["journalctl", "--user", "-u", UNIT, "-f"])


def running_elsewhere() -> bool:
    """Whether the service runs a bot in another process. Telegram hands each update to one of
    the two, so a bot run by hand would miss what the service takes."""
    shown = subprocess.run(
        ["systemctl", "--user", "show", UNIT, "--property=MainPID", "--value"],
        capture_output=True,
        text=True,
        check=False,
    )
    return shown.stdout.strip() not in ("", "0", str(os.getpid()))


def _systemctl(*args: str) -> None:
    subprocess.run(["systemctl", "--user", *args], check=True)
