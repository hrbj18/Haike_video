"""Local defaults for project narration gain.

The preference is deliberately separate from background-music gain.  It is
captured when a project workbench is first created and never rewrites older
projects, Voicebox sources, or paid avatar media.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from backlot.state import REPO_ROOT


PREFERENCES_PATH = REPO_ROOT / ".backlot" / "narration_preferences.json"
DEFAULT_NARRATION_GAIN_DB = 0.0
MIN_NARRATION_GAIN_DB = -12.0
MAX_NARRATION_GAIN_DB = 12.0
NARRATION_GAIN_STEP_DB = 0.5


def clamp_narration_gain_db(value: object, *, fallback: float = DEFAULT_NARRATION_GAIN_DB) -> float:
    """Return a bounded gain snapped to the UI's half-decibel steps."""
    try:
        gain = float(value)
    except (TypeError, ValueError):
        gain = fallback
    gain = max(MIN_NARRATION_GAIN_DB, min(MAX_NARRATION_GAIN_DB, gain))
    snapped = round(gain / NARRATION_GAIN_STEP_DB) * NARRATION_GAIN_STEP_DB
    return 0.0 if abs(snapped) < 0.001 else round(snapped, 1)


def _default() -> dict[str, Any]:
    return {"version": 1, "playback_gain_db": DEFAULT_NARRATION_GAIN_DB}


def read_narration_preferences() -> dict[str, Any]:
    value = _default()
    try:
        raw = json.loads(PREFERENCES_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return value
    if isinstance(raw, dict):
        value["playback_gain_db"] = clamp_narration_gain_db(raw.get("playback_gain_db"))
    return value


def save_narration_preferences(payload: dict[str, Any]) -> dict[str, Any]:
    value = _default()
    value["playback_gain_db"] = clamp_narration_gain_db(payload.get("playback_gain_db"))
    PREFERENCES_PATH.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(
        prefix=".narration-preferences-", suffix=".tmp", dir=PREFERENCES_PATH.parent
    )
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
