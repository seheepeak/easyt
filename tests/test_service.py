"""When `easyt bot` would run a second bot beside the service."""

import os
import subprocess

import pytest

from easyt import service


@pytest.mark.parametrize(
    ("main_pid", "elsewhere"),
    [("", False), ("0", False), (str(os.getpid()), False), (str(os.getpid() + 1), True)],
)
def test_only_a_service_process_other_than_this_one_counts(monkeypatch, main_pid, elsewhere):
    shown = subprocess.CompletedProcess([], 0, stdout=f"{main_pid}\n")
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: shown)
    assert service.running_elsewhere() is elsewhere
