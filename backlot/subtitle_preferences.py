"""Local, software-wide defaults for the Backlot subtitle editor.

Caption templates belong to a single project's review contract.  The visual
style a user wants to start *future* projects with belongs to the local
workstation instead.  Keeping this small preference under ``.backlot`` lets a
creator establish a reliable default without rewriting any project that has
already been reviewed.
"""

from __future__ import annotations

import json
import os
import tempfile
from copy import deepcopy
from pathlib import Path
from typing import Any

from backlot.state import REPO_ROOT


PREFERENCES_PATH = REPO_ROOT / ".backlot" / "subtitle_preferences.json"


def _default() -> dict[str, Any]:
    return {"version": 1, "style": {}}


def read_subtitle_preferences() -> dict[str, Any]:
    """Read the local default without allowing malformed state to block work."""
    value = _default()
    try:
        raw = json.loads(PREFERENCES_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return value
    if isinstance(raw, dict) and isinstance(raw.get("style"), dict):
        value["style"] = deepcopy(raw["style"])
    return value


def save_subtitle_preferences(payload: dict[str, Any]) -> dict[str, Any]:
    """Atomically persist a pre-validated subtitle style for future projects."""
    if not isinstance(payload.get("style"), dict):
        raise ValueError("默认字幕样式格式无效")
    value = {"version": 1, "style": deepcopy(payload["style"])}
    PREFERENCES_PATH.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(prefix=".subtitle-preferences-", suffix=".tmp", dir=PREFERENCES_PATH.parent)
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
