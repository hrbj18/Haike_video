"""Global audio-centre persistence and project-independent preview tests."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from backlot import audio_center
from tools.base_tool import ToolStatus


def test_audio_center_uses_real_default_voice_and_generates_project_independent_preview(tmp_path, monkeypatch):
    monkeypatch.setattr(audio_center, "AUDIO_CENTER_DIR", tmp_path / ".backlot" / "audio")
    monkeypatch.setattr(audio_center, "AUDIO_CENTER_FILE", audio_center.AUDIO_CENTER_DIR / "audio_center.json")
    monkeypatch.setattr(audio_center, "PREVIEW_DIRECTORY", audio_center.AUDIO_CENTER_DIR / "previews")
    profiles = [
        {"id": "serena-id", "name": "qwen serena", "language": "zh", "voice_type": "preset", "default_engine": "qwen_custom_voice", "description": "预设"},
        {"id": "yaya-id", "name": "雅雅", "language": "zh", "voice_type": "cloned", "default_engine": "qwen", "description": "克隆"},
    ]
    monkeypatch.setattr(audio_center.VoiceboxTTS, "get_status", classmethod(lambda cls: ToolStatus.AVAILABLE))
    monkeypatch.setattr(audio_center.VoiceboxTTS, "list_profiles", classmethod(lambda cls: profiles))

    initial = audio_center.read_audio_center()
    assert initial["default_voice"]["id"] == "yaya-id"

    selected = audio_center.set_default_voice({"profile_id": "serena-id"})
    assert selected["default_voice"]["id"] == "serena-id"
    queued = audio_center.start_preview({"profile_id": "serena-id", "text": "一段独立试听。"})
    assert queued["preview_job"]["status"] == "generating"

    def fake_execute(self, inputs):
        output = Path(inputs["output_path"])
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"RIFFpreview")
        return SimpleNamespace(success=True, data={"duration": 1.8}, error=None)

    monkeypatch.setattr(audio_center.VoiceboxTTS, "execute", fake_execute)
    completed = audio_center.generate_preview()

    assert completed["preview_job"]["status"] == "completed"
    assert completed["previews"][0]["profile_id"] == "serena-id"
    assert audio_center.preview_audio_path(completed["previews"][0]["id"]).read_bytes() == b"RIFFpreview"
