"""Local, software-wide defaults for Backlot background-music mixing.

Project music choices belong in a project workbench contract.  The preferred
starting gain is a workstation preference: it should make *future* projects
predictable without unexpectedly rewriting an existing project's approved
mix.  The file intentionally lives under ``.backlot`` which is local cache / 
user state and is ignored by Git.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from backlot.state import REPO_ROOT


PREFERENCES_PATH = REPO_ROOT / ".backlot" / "music_preferences.json"
DEFAULT_PLAYBACK_GAIN_DB = -8.0
MIN_PLAYBACK_GAIN_DB = -24.0
MAX_PLAYBACK_GAIN_DB = 0.0


def clamp_playback_gain_db(value: object, *, fallback: float = DEFAULT_PLAYBACK_GAIN_DB) -> float:
    """Return a safe, user-facing mix gain with one-decimal stability."""
    try:
        gain = float(value)
    except (TypeError, ValueError):
        gain = fallback
    return round(max(MIN_PLAYBACK_GAIN_DB, min(MAX_PLAYBACK_GAIN_DB, gain)), 1)


def _default() -> dict[str, Any]:
    return {"version": 1, "playback_gain_db": DEFAULT_PLAYBACK_GAIN_DB}


def read_music_preferences() -> dict[str, Any]:
    """Read the local default without allowing malformed state to block work."""
    value = _default()
    try:
        raw = json.loads(PREFERENCES_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return value
    if isinstance(raw, dict):
        value["playback_gain_db"] = clamp_playback_gain_db(raw.get("playback_gain_db"))
    return value


def save_music_preferences(payload: dict[str, Any]) -> dict[str, Any]:
    """Atomically persist just the software-wide default mix gain."""
    value = _default()
    value["playback_gain_db"] = clamp_playback_gain_db(payload.get("playback_gain_db"))
    PREFERENCES_PATH.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(prefix=".music-preferences-", suffix=".tmp", dir=PREFERENCES_PATH.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as file:
            json.dump(value, file, ensure_ascii=False, indent=2)
            file.write("\n")
        Path(temporary_name).replace(PREFERENCES_PATH)
    except Exception:
        try:
            Path(temporary_name).unlink()
        except OSError:
            pass
        raise
    return value
