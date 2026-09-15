"""Keep console children from flashing a window on Windows.

Every FFmpeg / ffprobe / npx invocation in this project is a console program.
When the parent process has no console attached — which is the normal case for
the Backlot server, started detached so it survives the launching shell — Windows
allocates a *brand new console window* for each child.  A render spawns hundreds
of them, so the user sees windows flickering for the whole render.

Passing ``CREATE_NO_WINDOW`` makes the child run with an invisible console
instead.  Behaviour, exit codes, stdout and stderr are unchanged; only the
window is gone.

Usage::

    from lib.subprocess_window import no_window_kwargs
    subprocess.run(cmd, capture_output=True, **no_window_kwargs())
"""

from __future__ import annotations

import os
import subprocess

__all__ = ["no_window_kwargs", "NO_WINDOW_CREATIONFLAGS", "install_default"]

# Windows-only constant.  Python exposes it on every platform as an int, but it
# is only honoured on Windows.
NO_WINDOW_CREATIONFLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)

# ``CREATE_NO_WINDOW`` must not be combined with either of these; when they are
# present the flag is ignored anyway, so leave those callers untouched.
_CONSOLE_FLAGS = (
    getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
    | getattr(subprocess, "CREATE_NEW_CONSOLE", 0x00000010)
)

_INSTALLED_MARKER = "_haike_no_window_default"


def no_window_kwargs() -> dict[str, int]:
    """Return ``{"creationflags": CREATE_NO_WINDOW}`` on Windows, else ``{}``."""
    if os.name != "nt":
        return {}
    return {"creationflags": NO_WINDOW_CREATIONFLAGS}


def _merge(kwargs: dict) -> dict:
    flags = int(kwargs.get("creationflags") or 0)
    if flags & _CONSOLE_FLAGS:
        return kwargs
    kwargs["creationflags"] = flags | NO_WINDOW_CREATIONFLAGS
    return kwargs


def install_default() -> bool:
    """Make every later ``subprocess.Popen`` inherit ``CREATE_NO_WINDOW``.

    A process-wide switch for the long-running server, whose spawn sites are too
    many to touch one by one.  Idempotent, and a no-op off Windows.  Callers that
    explicitly ask for a detached process or a new console are left alone.
    """
    if os.name != "nt":
        return False
    original = subprocess.Popen.__init__
    if getattr(original, _INSTALLED_MARKER, False):
        return False

    def patched(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        return original(self, *args, **_merge(kwargs))

    setattr(patched, _INSTALLED_MARKER, True)
    subprocess.Popen.__init__ = patched  # type: ignore[method-assign]
    return True
