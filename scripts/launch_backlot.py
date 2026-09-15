"""Start the Backlot server as a detached, windowless background process.

Why this script exists
----------------------
The workbench used to be launched from ``scripts/start_backlot.ps1`` with::

    & $python -m backlot open --no-browser

The call operator keeps the server attached to the console window that
``更新并重启工作台.bat`` opens, so closing that window kills the workbench and
the port goes dead without leaving any log behind.

Windows PowerShell cannot take over the launching either: both ``Start-Process``
and ``ProcessStartInfo`` copy the inherited environment into a case-insensitive
dictionary and abort with *"An item with the same key has already been added"*
as soon as the host injected the same variable twice with different casing
(``Path``/``PATH``, ``http_proxy``/``HTTP_PROXY``, ...).  Rewriting the process
environment does not help, because the underlying Windows API matches variable
names case-insensitively.

Plain ``subprocess`` flags are not affected by any of that, so the detach is
performed here instead: the server gets no console at all, inherits nothing
from the launcher window, and writes its own log files.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _server_interpreter() -> str:
    """Pick the interpreter that cannot create a console window.

    The venv's ``python.exe`` is a trampoline: it re-runs the real interpreter as
    a child process, and that child gets a console window of its own even when
    this launcher asked for a detached process.  A console window is a thing a
    user can close, and closing it kills the server, so the window-subsystem
    twin (``pythonw.exe``) is preferred: it creates no console anywhere in the
    chain, in any launch context.  ``BACKLOT_DETACHED`` + FreeConsole in
    ``backlot/__main__.py`` stay as a second line of defence.
    """
    if os.name == "nt":
        candidate = Path(sys.executable).with_name("pythonw.exe")
        if candidate.is_file():
            return str(candidate)
    return sys.executable


def main() -> int:
    port = os.environ.get("BACKLOT_PORT", "4754")
    log_dir = ROOT / ".backlot" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    flags = 0
    if os.name == "nt":
        # DETACHED_PROCESS gives the child no console at all, so it neither
        # shows a window nor dies when the launcher window is closed.
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | getattr(
            subprocess, "DETACHED_PROCESS", 0x00000008
        )

    interpreter = _server_interpreter()

    # BACKLOT_DETACHED tells the server it is a background service: it then
    # drops any console the interpreter trampoline in .venv\Scripts hands to
    # its child process, so closing that window can never kill it.
    env = os.environ.copy()
    env["BACKLOT_DETACHED"] = "1"

    with open(log_dir / "backlot.out.log", "ab") as out, open(
        log_dir / "backlot.err.log", "ab"
    ) as err:
        process = subprocess.Popen(
            [interpreter, "-m", "backlot", "serve", "--port", str(port)],
            cwd=str(ROOT),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=err,
            creationflags=flags,
            close_fds=True,
        )

    print(
        f"backlot: launched detached server pid={process.pid} "
        f"port={port} interpreter={Path(interpreter).name}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
