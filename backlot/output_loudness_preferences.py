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
# ★★ 2026-09-15 定案：-10.0 → **-9.0 LUFS**（2026-09-16 抖音端实测复核后**维持不变**：
#   平台只做 −1.1 dB 整体衰减，下调目标只会让整条更小、不会让 BGM 变清楚）。
#   本常量是成片响度的**唯一软件级真源头**；批次脚本/spec 生成器不得各存一份。
#   起因（用户）："在电脑听感觉声音够用，放在手机里听就会明显感觉别人的视频
#   bgm 声音更大，口播声音更清晰"——全项目通病。三条腿一起抬：
#     ① 口播：去掉削顶 + 限幅压波峰因数（可达响度 +1.8 dB）
#     ② BGM ：-14 → -9.5 dB（人声/BGM 差从 22 dB 收到 17.5 dB）
#        ★ 2026-09-16 再调到 **-6.0 dB**（见 backlot/music_preferences.py 注释）：
#          抖音端实测证实平台会**额外压低电平内容 4~5 dB**（P5/P25），
#          手机单喇叭 <300 Hz 再衰减 15~20 dB ⇒ BGM 增益是唯一有效杠杆。
#          注意：抬 BGM 会推高混音波峰因数 ⇒ **响度天花板下降**（逐期不同），
#          贴边期（gpu / shengteng / ram）须同步用 set_lufs8.py 下调 target_lufs。
#     ③ 成片：-10 → -9.0 LUFS
#   为什么 -9.0 是安全的：`lufs_plan8.py` 用各期数字人母版实测了**可达天花板**
#   （= -2.0 - 波峰因数，逐期不同），8 期落在 -8.60 ~ -10.00；target 取 -9.0 时
#   最差一期容差 1.0 dB，而发布闸门允许 1.5 dB ⇒ 全批可过。
#   另外 loudnorm 是**单增益**不是压缩器，抬 target 不会增加压扁感（LRA 实测不变）。
DEFAULT_OUTPUT_TARGET_LUFS = -9.0
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
