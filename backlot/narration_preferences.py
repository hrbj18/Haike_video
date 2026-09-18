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
# ★★ 2026-09-15 定案：**增益保持 +8 dB，削顶问题改由处理链解决**。
#   起因：手机外放听口播不够清晰，且"别人的 BGM 更大声"。
#   排查结论 —— 单纯抬增益（旧实现只有 volume=NdB）会把音轨推过 0 dBFS：
#     旧 volume=+8dB 实测 true peak 冲到 +0.6 dB(gpu) / +2.9 dB(microduck)，
#     波形被切平 ⇒ 既失真、又不清晰，而响度看着"够大"是拿削顶换的。
#   所以增益语义（用户可见的那个数字）保留 +8 dB，另在
#   `workbench._narration_processing_chain` 里叠加固定驱动量并过限幅器，
#   既去掉削顶、又把波峰因数压下来（可达响度因此提升 ~1.8 dB）。
#   ⇒ 这里**不要**为了"防削顶"去降增益，那会白丢响度。
DEFAULT_NARRATION_GAIN_DB = 8.0
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
