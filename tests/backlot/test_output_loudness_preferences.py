from __future__ import annotations

import json

from backlot import output_loudness_preferences as preferences


def test_output_loudness_preferences_default_and_clamp(tmp_path, monkeypatch):
    target = tmp_path / "output-loudness.json"
    monkeypatch.setattr(preferences, "PREFERENCES_PATH", target)

    # 2026-09-16 定案：软件级默认成片响度 -9.0 LUFS（抖音端实测后维持）。
    assert preferences.read_output_loudness_preferences()["target_lufs"] == -9.0
    saved = preferences.save_output_loudness_preferences({"target_lufs": -8.0})
    assert saved["target_lufs"] == -8.0
    assert json.loads(target.read_text(encoding="utf-8"))["true_peak_limit_dbtp"] == -1.0

    saved = preferences.save_output_loudness_preferences({"target_lufs": -12.24})
    assert saved["target_lufs"] == -12.0
