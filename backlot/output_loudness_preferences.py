"""Local defaults for final-program loudness.

The software preference is captured by newly bootstrapped workbenches, and it is
also the value used to backfill a project that stores no loudness policy at all
(``workbench._ensure_output_loudness_policy``).  A project that already carries an
explicit target keeps it, so changing the workstation default never rewrites an
already reviewed mix.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from backlot.state import REPO_ROOT


PREFERENCES_PATH = REPO_ROOT / ".backlot" / "output_loudness_preferences.json"
DEFAULT_OUTPUT_TARGET_LUFS = -10.0
# Historical target of projects created before this policy existed.  Reference
# only: the render path no longer falls back to it, because doing so silently
# pinned every policy-less project to the old target.
LEGACY_OUTPUT_TARGET_LUFS = -14.0
MIN_OUTPUT_TARGET_LUFS = -16.0
MAX_OUTPUT_TARGET_LUFS = -8.0
OUTPUT_TARGET_STEP_LUFS = 0.5
OUTPUT_TRUE_PEAK_LIMIT_DBTP = -1.0


def clamp_output_target_lufs(
    value: object, *, fallback: float = DEFAULT_OUTPUT_TARGET_LUFS
) -> float:
    """Return a bounded half-LU target suitable for the UI and FFmpeg."""
    try:
        target = float(value)
    except (TypeError, ValueError):
        target = fallback
    target = max(MIN_OUTPUT_TARGET_LUFS, min(MAX_OUTPUT_TARGET_LUFS, target))
    snapped = round(target / OUTPUT_TARGET_STEP_LUFS) * OUTPUT_TARGET_STEP_LUFS
    return round(snapped, 1)


def _default() -> dict[str, Any]:
    return {
        "version": 1,
        "target_lufs": DEFAULT_OUTPUT_TARGET_LUFS,
        "true_peak_limit_dbtp": OUTPUT_TRUE_PEAK_LIMIT_DBTP,
    }


def read_output_loudness_preferences() -> dict[str, Any]:
    value = _default()
    try:
        raw = json.loads(PREFERENCES_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return value
    if isinstance(raw, dict):
        value["target_lufs"] = clamp_output_target_lufs(raw.get("target_lufs"))
    return value


def save_output_loudness_preferences(payload: dict[str, Any]) -> dict[str, Any]:
    value = _default()
    value["target_lufs"] = clamp_output_target_lufs(payload.get("target_lufs"))
    PREFERENCES_PATH.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(
        prefix=".output-loudness-preferences-",
        suffix=".tmp",
        dir=PREFERENCES_PATH.parent,
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
