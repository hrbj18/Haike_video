"""Keep the cloud-ASR choice explicit in the material-library UI."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_material_library_exposes_explicit_doubao_asr_confirmation_without_secret_fields() -> None:
    script = (ROOT / "backlot" / "ui" / "workbench.js").read_text(encoding="utf-8")

    assert "豆包语音识别（确认）" in script
    assert "录音文件识别 1.0 极速版" in script
    assert "volc.bigasr.auc_turbo" in script
    assert "不会上传视频画面" in script
    assert "remote_asr_confirmed" in script
    assert "不会静默改用本地 Whisper" in script
    assert "DOUBAO_ASR_API_KEY" not in script
