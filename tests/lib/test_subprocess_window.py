"""The no-console default that keeps FFmpeg / npx children from flashing windows.

The Backlot server is launched through ``pythonw.exe`` and then calls
``FreeConsole``, so it owns no console at all.  Every console child it spawns --
ffmpeg, ffprobe, npx -- would otherwise be handed a brand new console *window* by
Windows, and a single render spawns hundreds of them.  These tests pin the two
switches that stop it.
"""

from __future__ import annotations

import os
import subprocess

import pytest

from lib.subprocess_window import (
    NO_WINDOW_CREATIONFLAGS,
    install_default,
    no_window_kwargs,
)


@pytest.fixture
def restore_popen_init():
    original = subprocess.Popen.__init__
    try:
        yield original
    finally:
        subprocess.Popen.__init__ = original


def test_kwargs_are_empty_off_windows(monkeypatch) -> None:
    monkeypatch.setattr(os, "name", "posix")
    assert no_window_kwargs() == {}


def test_kwargs_carry_create_no_window_on_windows(monkeypatch) -> None:
    monkeypatch.setattr(os, "name", "nt")
    assert no_window_kwargs() == {"creationflags": NO_WINDOW_CREATIONFLAGS}


def test_install_is_idempotent(restore_popen_init, monkeypatch) -> None:
    monkeypatch.setattr(os, "name", "nt")
    assert install_default() is True
    assert install_default() is False, "installing twice must not stack wrappers"


def _capture_spawn_kwargs(monkeypatch) -> dict:
    """Install a recorder under our patch and return the kwargs it receives.

    The recorder aborts instead of starting a child: these tests are about the
    keyword arguments, not about running FFmpeg.
    """
    captured: dict = {}

    def recorder(self, *args, **kwargs):  # pragma: no cover - never runs the child
        captured.update(kwargs)
        raise RuntimeError("child not started")

    monkeypatch.setattr(os, "name", "nt")
    subprocess.Popen.__init__ = recorder
    install_default()
    return captured


def test_installed_default_adds_the_flag(restore_popen_init, monkeypatch) -> None:
    captured = _capture_spawn_kwargs(monkeypatch)
    with pytest.raises(RuntimeError):
        subprocess.Popen(["ffmpeg", "-version"])
    assert captured["creationflags"] == NO_WINDOW_CREATIONFLAGS


def test_installed_default_keeps_an_existing_flag(restore_popen_init, monkeypatch) -> None:
    captured = _capture_spawn_kwargs(monkeypatch)
    with pytest.raises(RuntimeError):
        subprocess.Popen(["ffmpeg", "-version"], creationflags=0x00000080)
    assert captured["creationflags"] == (0x00000080 | NO_WINDOW_CREATIONFLAGS)


def test_installed_default_leaves_detached_children_alone(restore_popen_init, monkeypatch) -> None:
    """CREATE_NO_WINDOW is a no-op next to DETACHED_PROCESS, so do not mix them."""
    detached = getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
    captured = _capture_spawn_kwargs(monkeypatch)
    with pytest.raises(RuntimeError):
        subprocess.Popen(["python", "-c", "pass"], creationflags=detached)
    assert captured["creationflags"] == detached
