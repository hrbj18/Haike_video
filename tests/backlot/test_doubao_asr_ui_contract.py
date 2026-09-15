"""Keep the cloud-ASR choice explicit in the material-library UI."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_material_library_exposes_explicit_cloud_asr_confirmation_without_secret_fields() -> None:
    script = (ROOT / "backlot" / "ui" / "workbench.js").read_text(encoding="utf-8")

    # The engine is chosen from the backend catalogue, so the library must render
    # whatever engine is configured and still ask for an explicit confirmation.
    assert "/workbench/transcript-providers" in script
    assert "startAssetCloudTranscript" in script
    assert "transcriptProviderLabel(provider)" in script
    assert "tencent（腾讯云 ASR）" in script
    assert "doubao（豆包 ASR）" in script
    # Cost + audio-egress consent must stay explicit, and the engine detail shown
    # to the user comes from the backend instead of a hardcoded resource id.
    assert "会消耗云端音频时长额度" in script
    assert "不会上传视频画面" in script
    assert "不会静默改用本地 Whisper" in script
    assert "transcriptProviderDetail(provider)" in script
    assert "remote_asr_confirmed" in script
    assert "DOUBAO_ASR_API_KEY" not in script
    assert "TENCENT_SECRET_KEY" not in script
