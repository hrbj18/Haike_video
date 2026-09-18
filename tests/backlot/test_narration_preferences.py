from __future__ import annotations

import json

from backlot import narration_preferences


def test_narration_gain_clamps_to_supported_half_db_steps():
    assert narration_preferences.clamp_narration_gain_db(-99) == -12.0
    assert narration_preferences.clamp_narration_gain_db(99) == 12.0
    assert narration_preferences.clamp_narration_gain_db(3.24) == 3.0
    assert narration_preferences.clamp_narration_gain_db(3.26) == 3.5
    # 非法输入的兜底是**软件级默认**（2026-09-15 由 0.0 定为 +8.0；
    # 原断言写死 0.0，默认值改后一直红着，2026-09-16 修正）。
    assert narration_preferences.clamp_narration_gain_db("bad") == 8.0
    assert narration_preferences.clamp_narration_gain_db(None) == 8.0
    assert narration_preferences.clamp_narration_gain_db("bad", fallback=0.0) == 0.0


def test_narration_preferences_are_atomic_and_future_project_only(tmp_path, monkeypatch):
    target = tmp_path / "narration_preferences.json"
    monkeypatch.setattr(narration_preferences, "PREFERENCES_PATH", target)

    saved = narration_preferences.save_narration_preferences({"playback_gain_db": 4.26})

    assert saved["playback_gain_db"] == 4.5
    assert narration_preferences.read_narration_preferences()["playback_gain_db"] == 4.5
    assert json.loads(target.read_text(encoding="utf-8"))["playback_gain_db"] == 4.5

