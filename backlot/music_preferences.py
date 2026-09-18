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
# 2026-09-15 第一次调整：短视频平台做响度归一化后，人声/BGM 相差 22 dB
# （旧值 +8/−14）会让 BGM 几乎不可闻。BGM 提到 −9.5 dB。
#
# 2026-09-16 第二次调整（依据 = 把已发布的成片从抖音下载回来做分带实测）：
#   · 抖音**不做**大幅整体衰减 —— 全频 I 只降 1.1 dB（"声音被调小"不成立）；
#   · 抖音**专门削低电平内容** —— P5 降 4.4 dB、P25 降 4.3 dB。P5/P25 正是
#     人声句间停顿，也就是 BGM 唯一露头的地方 ⇒ 平台把我们垫底的 BGM 再拉开 4~5 dB；
#   · 手机单喇叭 <300 Hz 衰减 15~20 dB ⇒ BGM 低频主体在手机上直接消失；
#   · 我们混音无 ducking、BGM 恒定增益 ⇒ 抬 playback_gain_db 是**唯一有效杠杆**。
#   ⇒ −9.5 → −6.0（人声/BGM 比 7.4 → ~4 dB），把人声 +8 与成片响度目标一律不动：
#     主动降 target_lufs 只会让整条视频更小，不会让 BGM 变清楚。
DEFAULT_PLAYBACK_GAIN_DB = -6.0
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
