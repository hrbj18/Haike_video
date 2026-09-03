"""Provider-neutral Backlot TTS runtime contracts."""

from __future__ import annotations

from pathlib import Path

from backlot import tts_runtime
from tools.base_tool import ToolResult


def test_local_profile_keeps_existing_voicebox_contract(tmp_path, monkeypatch):
    captured = {}

    def fake_execute(self, inputs):
        captured.update(inputs)
        Path(inputs["output_path"]).write_bytes(b"RIFFlocal")
        return ToolResult(success=True, data={"duration": 1.2})

    monkeypatch.setattr(tts_runtime.VoiceboxTTS, "execute", fake_execute)
    output = tmp_path / "local.wav"

    result = tts_runtime.generate_voice_audio(
        text="本地配音。",
        profile={"id": "local-yaya", "provider_id": "voicebox_tts"},
        output_path=output,
    )

    assert result.success
    assert captured["profile_id"] == "local-yaya"
    assert output.read_bytes() == b"RIFFlocal"


def test_cloud_profile_is_forced_to_doubao_and_normalised_to_wav(tmp_path, monkeypatch):
    captured = {}

    def fake_execute(self, inputs):
        captured.update(inputs)
        Path(inputs["output_path"]).write_bytes(b"cloud-mp3")
        Path(inputs["metadata_path"]).write_text("{}", encoding="utf-8")
        return ToolResult(
            success=True,
            data={"output": inputs["output_path"], "metadata_path": inputs["metadata_path"], "sentences": []},
            artifacts=[inputs["output_path"], inputs["metadata_path"]],
            cost_usd=0.01,
        )

    def fake_convert(source, target):
        assert source.read_bytes() == b"cloud-mp3"
        target.write_bytes(b"RIFFcloud")
        return None

    monkeypatch.setattr(tts_runtime.DoubaoTTS, "execute", fake_execute)
    monkeypatch.setattr(tts_runtime, "_convert_to_wav", fake_convert)
    output = tmp_path / "cloud.wav"

    result = tts_runtime.generate_voice_audio(
        text="云端配音。",
        profile={
            "id": "doubao:yaya",
            "name": "雅雅",
            "provider_id": "doubao",
            "provider_voice_id": "cloud-yaya",
            "speech_rate": 1.25,
        },
        output_path=output,
    )

    assert result.success
    assert captured["voice_id"] == "cloud-yaya"
    assert captured["speech_rate"] == 25
    assert captured["enable_timestamp"] is True
    assert result.data["provider_id"] == "doubao"
    assert result.data["normalised_format"] == "wav_pcm_s16le_mono_24000"
    assert result.data["playback_rate"] == 1.25
    assert output.read_bytes() == b"RIFFcloud"
    assert not (tmp_path / ".cloud.doubao.mp3").exists()


def test_unknown_provider_never_silently_falls_back(tmp_path):
    result = tts_runtime.generate_voice_audio(
        text="不要换声音。",
        profile={"id": "bad", "provider_id": "unknown"},
        output_path=tmp_path / "bad.wav",
    )
    assert not result.success
    assert "不支持" in str(result.error)
