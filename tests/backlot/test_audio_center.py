"""Global audio-centre persistence and project-independent preview tests."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from backlot import audio_center
from tools.base_tool import ToolResult, ToolStatus


@pytest.fixture(autouse=True)
def isolate_machine_specific_cloud_profile_state(monkeypatch):
    for variable in (
        "DOUBAO_SPEECH_YAYA_VOICE_TYPE",
        "DOUBAO_SPEECH_YAYA_RESOURCE_ID",
        "DOUBAO_SPEECH_YAYA_ENABLED",
        "DOUBAO_SPEECH_MENGMENG_VOICE_TYPE",
        "DOUBAO_SPEECH_MENGMENG_RESOURCE_ID",
        "DOUBAO_SPEECH_MENGMENG_ENABLED",
        "DOUBAO_SPEECH_PUBLIC_VOICE_TYPE",
        "DOUBAO_SPEECH_PUBLIC_RESOURCE_ID",
        "DOUBAO_SPEECH_PUBLIC_ENABLED",
        "DOUBAO_SPEECH_PUBLIC_MALE_VOICE_TYPE",
        "DOUBAO_SPEECH_PUBLIC_MALE_RESOURCE_ID",
        "DOUBAO_SPEECH_PUBLIC_MALE_ENABLED",
        "HAIKE_VIDEO_TTS_PROVIDER",
    ):
        monkeypatch.delenv(variable, raising=False)


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

    selected = audio_center.set_default_voice({"profile_id": "yaya-id"})
    assert selected["default_voice"]["id"] == "yaya-id"
    queued = audio_center.start_preview({"profile_id": "yaya-id", "text": "一段独立试听。"})
    assert queued["preview_job"]["status"] == "generating"

    def fake_execute(self, inputs):
        output = Path(inputs["output_path"])
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"RIFFpreview")
        return SimpleNamespace(success=True, data={"duration": 1.8}, error=None)

    monkeypatch.setattr(audio_center.VoiceboxTTS, "execute", fake_execute)
    completed = audio_center.generate_preview()

    assert completed["preview_job"]["status"] == "completed"
    assert completed["previews"][0]["profile_id"] == "yaya-id"
    assert audio_center.preview_audio_path(completed["previews"][0]["id"]).read_bytes() == b"RIFFpreview"


def test_audio_center_exposes_configured_cloud_voices_without_removing_local_provider(tmp_path, monkeypatch):
    monkeypatch.setattr(audio_center, "AUDIO_CENTER_DIR", tmp_path / ".backlot" / "audio")
    monkeypatch.setattr(audio_center, "AUDIO_CENTER_FILE", audio_center.AUDIO_CENTER_DIR / "audio_center.json")
    monkeypatch.setattr(audio_center, "PREVIEW_DIRECTORY", audio_center.AUDIO_CENTER_DIR / "previews")
    monkeypatch.setattr(audio_center.VoiceboxTTS, "get_status", classmethod(lambda cls: ToolStatus.AVAILABLE))
    monkeypatch.setattr(audio_center.VoiceboxTTS, "list_profiles", classmethod(lambda cls: [
        {"id": "local-yaya", "name": "雅雅", "language": "zh", "available": True},
    ]))
    monkeypatch.setenv("DOUBAO_SPEECH_API_KEY", "test-only-secret")
    monkeypatch.setenv("DOUBAO_SPEECH_YAYA_VOICE_TYPE", "cloud-yaya")
    monkeypatch.setenv("DOUBAO_SPEECH_MENGMENG_VOICE_TYPE", "cloud-mengmeng")

    center = audio_center.read_audio_center()

    assert {item["id"] for item in center["profiles"]} == {
        "local-yaya", "doubao:yaya", "doubao:mengmeng",
    }
    assert {item["id"] for item in center["providers"]} == {"voicebox_tts", "doubao"}
    assert all("provider_voice_id" not in item for item in center["profiles"])


def test_cloud_clone_profiles_use_icl_resource_and_can_be_disabled_after_live_preflight(tmp_path, monkeypatch):
    monkeypatch.setattr(audio_center, "AUDIO_CENTER_DIR", tmp_path / ".backlot" / "audio")
    monkeypatch.setattr(audio_center, "AUDIO_CENTER_FILE", audio_center.AUDIO_CENTER_DIR / "audio_center.json")
    monkeypatch.setattr(audio_center, "PREVIEW_DIRECTORY", audio_center.AUDIO_CENTER_DIR / "previews")
    monkeypatch.setattr(audio_center.VoiceboxTTS, "get_status", classmethod(lambda cls: ToolStatus.UNAVAILABLE))
    monkeypatch.setenv("DOUBAO_SPEECH_API_KEY", "test-only-secret")
    monkeypatch.setenv("DOUBAO_SPEECH_YAYA_VOICE_TYPE", "S_clone")
    monkeypatch.setenv("DOUBAO_SPEECH_YAYA_ENABLED", "false")

    center = audio_center.read_audio_center()
    profile = next(item for item in center["profiles"] if item["id"] == "doubao:yaya")

    assert profile["resource_id"] == "seed-icl-2.0"
    assert profile["available"] is False


def test_public_cloud_profile_uses_speech_resource(tmp_path, monkeypatch):
    monkeypatch.setattr(audio_center, "AUDIO_CENTER_DIR", tmp_path / ".backlot" / "audio")
    monkeypatch.setattr(audio_center, "AUDIO_CENTER_FILE", audio_center.AUDIO_CENTER_DIR / "audio_center.json")
    monkeypatch.setattr(audio_center, "PREVIEW_DIRECTORY", audio_center.AUDIO_CENTER_DIR / "previews")
    monkeypatch.setattr(audio_center.VoiceboxTTS, "get_status", classmethod(lambda cls: ToolStatus.UNAVAILABLE))
    monkeypatch.setenv("DOUBAO_SPEECH_API_KEY", "test-only-secret")
    monkeypatch.setenv("DOUBAO_SPEECH_PUBLIC_VOICE_TYPE", "zh_female_vv_uranus_bigtts")
    monkeypatch.setenv("DOUBAO_SPEECH_PUBLIC_MALE_VOICE_TYPE", "zh_male_kailangxuezhang_uranus_bigtts")

    center = audio_center.read_audio_center()
    female = next(item for item in center["profiles"] if item["id"] == "doubao:public_female")
    male = next(item for item in center["profiles"] if item["id"] == "doubao:public_male")

    assert female["resource_id"] == "seed-tts-2.0"
    assert female["name"] == "豆包雅雅"
    assert female["available"] is True
    assert male["resource_id"] == "seed-tts-2.0"
    assert male["name"] == "豆包檬檬"
    assert male["available"] is True
    assert female["speech_rate"] == 1.25
    assert female["provider_speech_rate"] == 25


def test_catalog_only_exposes_the_approved_six_voices_and_preserves_hidden_runtime_profiles(tmp_path, monkeypatch):
    monkeypatch.setattr(audio_center, "AUDIO_CENTER_DIR", tmp_path / ".backlot" / "audio")
    monkeypatch.setattr(audio_center, "AUDIO_CENTER_FILE", audio_center.AUDIO_CENTER_DIR / "audio_center.json")
    monkeypatch.setattr(audio_center, "PREVIEW_DIRECTORY", audio_center.AUDIO_CENTER_DIR / "previews")
    monkeypatch.setattr(audio_center.VoiceboxTTS, "get_status", classmethod(lambda cls: ToolStatus.AVAILABLE))
    monkeypatch.setattr(audio_center.VoiceboxTTS, "list_profiles", classmethod(lambda cls: [
        {"id": "local-yaya", "name": "雅雅", "language": "zh"},
        {"id": "local-mengmeng", "name": "檬檬", "language": "zh"},
        {"id": "local-yaya-emotion", "name": "雅雅（强情感版）", "language": "zh"},
        {"id": "local-mengmeng-emotion", "name": "檬檬（强情感版）", "language": "zh"},
        {"id": "legacy-serena", "name": "Qwen Serena", "language": "zh"},
    ]))
    monkeypatch.setenv("DOUBAO_SPEECH_API_KEY", "test-only-secret")
    monkeypatch.setenv("DOUBAO_SPEECH_PUBLIC_VOICE_TYPE", "zh_female_vv_uranus_bigtts")
    monkeypatch.setenv("DOUBAO_SPEECH_PUBLIC_MALE_VOICE_TYPE", "zh_male_kailangxuezhang_uranus_bigtts")

    center = audio_center.read_audio_center()

    assert [item["name"] for item in center["profiles"]] == [
        "雅雅", "檬檬", "雅雅（强情感版）", "檬檬（强情感版）", "豆包雅雅", "豆包檬檬",
    ]
    assert audio_center.get_voice_profile("legacy-serena")["name"] == "Qwen Serena"


def test_cloud_rate_is_visible_and_frozen_into_a_preview_job(tmp_path, monkeypatch):
    monkeypatch.setattr(audio_center, "AUDIO_CENTER_DIR", tmp_path / ".backlot" / "audio")
    monkeypatch.setattr(audio_center, "AUDIO_CENTER_FILE", audio_center.AUDIO_CENTER_DIR / "audio_center.json")
    monkeypatch.setattr(audio_center, "PREVIEW_DIRECTORY", audio_center.AUDIO_CENTER_DIR / "previews")
    monkeypatch.setattr(audio_center.VoiceboxTTS, "get_status", classmethod(lambda cls: ToolStatus.UNAVAILABLE))
    monkeypatch.setenv("DOUBAO_SPEECH_API_KEY", "test-only-secret")
    monkeypatch.setenv("DOUBAO_SPEECH_PUBLIC_VOICE_TYPE", "zh_female_vv_uranus_bigtts")

    with pytest.raises(audio_center.AudioCenterError, match="所选音色"):
        audio_center.start_preview({"profile_id": "does-not-exist", "text": "不能偷偷换声。"})
    updated = audio_center.set_cloud_playback_rate({"playback_rate": 1.25})
    assert updated["cloud_playback_rate"] == 1.25
    assert updated["profiles"][0]["provider_speech_rate"] == 25
    queued = audio_center.start_preview({"profile_id": "doubao:public_female", "text": "云端试听。"})
    assert queued["preview_job"]["playback_rate"] == 1.25
    assert queued["preview_job"]["provider_speech_rate"] == 25
    with pytest.raises(audio_center.AudioCenterError, match="0.50"):
        audio_center.set_cloud_playback_rate({"playback_rate": 2.1})


def test_cloud_preview_freezes_provider_and_uses_unified_runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(audio_center, "AUDIO_CENTER_DIR", tmp_path / ".backlot" / "audio")
    monkeypatch.setattr(audio_center, "AUDIO_CENTER_FILE", audio_center.AUDIO_CENTER_DIR / "audio_center.json")
    monkeypatch.setattr(audio_center, "PREVIEW_DIRECTORY", audio_center.AUDIO_CENTER_DIR / "previews")
    monkeypatch.setattr(audio_center.VoiceboxTTS, "get_status", classmethod(lambda cls: ToolStatus.UNAVAILABLE))
    monkeypatch.setenv("DOUBAO_SPEECH_API_KEY", "test-only-secret")
    monkeypatch.setenv("DOUBAO_SPEECH_YAYA_VOICE_TYPE", "cloud-yaya")
    calls = []

    def fake_generate_voice_audio(**kwargs):
        calls.append(kwargs)
        output = Path(kwargs["output_path"])
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"RIFFcloud-preview")
        return ToolResult(success=True, data={"audio_duration_seconds": 2.4, "metadata_path": "timing.json"})

    monkeypatch.setattr(audio_center, "generate_voice_audio", fake_generate_voice_audio)
    audio_center.set_default_voice({"profile_id": "doubao:yaya"})
    queued = audio_center.start_preview({"profile_id": "doubao:yaya", "text": "这是一段云端试听。"})
    assert queued["preview_job"]["provider_id"] == "doubao"
    assert "profile_snapshot" not in queued["preview_job"]
    monkeypatch.setenv("DOUBAO_SPEECH_YAYA_VOICE_TYPE", "cloud-yaya-after-queue")
    audio_center.set_cloud_playback_rate({"playback_rate": 1.50})

    completed = audio_center.generate_preview()

    assert calls[0]["profile"]["provider_voice_id"] == "cloud-yaya"
    assert calls[0]["profile"]["speech_rate"] == 1.25
    assert calls[0]["sample_mode"] is True
    assert Path(calls[0]["output_path"]).suffix == ".mp3"
    assert completed["previews"][0]["provider_id"] == "doubao"
    assert completed["previews"][0]["duration_seconds"] == 2.4


def test_audio_center_redacts_cloud_api_key(monkeypatch):
    monkeypatch.setenv("DOUBAO_SPEECH_API_KEY", "never-show-this")
    assert "never-show-this" not in audio_center._safe_error("failed never-show-this")


def test_temporary_cloud_outage_does_not_silently_replace_the_saved_default(tmp_path, monkeypatch):
    monkeypatch.setattr(audio_center, "AUDIO_CENTER_DIR", tmp_path / ".backlot" / "audio")
    monkeypatch.setattr(audio_center, "AUDIO_CENTER_FILE", audio_center.AUDIO_CENTER_DIR / "audio_center.json")
    monkeypatch.setattr(audio_center, "PREVIEW_DIRECTORY", audio_center.AUDIO_CENTER_DIR / "previews")
    monkeypatch.setattr(audio_center.VoiceboxTTS, "get_status", classmethod(lambda cls: ToolStatus.AVAILABLE))
    monkeypatch.setattr(audio_center.VoiceboxTTS, "list_profiles", classmethod(lambda cls: [
        {"id": "local-yaya", "name": "雅雅", "language": "zh", "available": True},
    ]))
    monkeypatch.setenv("DOUBAO_SPEECH_YAYA_VOICE_TYPE", "cloud-yaya")
    monkeypatch.setenv("DOUBAO_SPEECH_API_KEY", "available-first")
    audio_center.set_default_voice({"profile_id": "doubao:yaya"})

    monkeypatch.delenv("DOUBAO_SPEECH_API_KEY")
    center = audio_center.read_audio_center()

    assert center["default_voice"]["id"] == "doubao:yaya"
    assert center["default_voice"]["available"] is False
    assert audio_center._load()["default_profile_id"] == "doubao:yaya"


def test_custom_doubao_voice_keeps_its_own_rate_and_never_exposes_provider_voice_id(tmp_path, monkeypatch):
    monkeypatch.setattr(audio_center, "AUDIO_CENTER_DIR", tmp_path / ".backlot" / "audio")
    monkeypatch.setattr(audio_center, "AUDIO_CENTER_FILE", audio_center.AUDIO_CENTER_DIR / "audio_center.json")
    monkeypatch.setattr(audio_center, "PREVIEW_DIRECTORY", audio_center.AUDIO_CENTER_DIR / "previews")
    monkeypatch.setattr(audio_center.VoiceboxTTS, "get_status", classmethod(lambda cls: ToolStatus.UNAVAILABLE))
    monkeypatch.setenv("DOUBAO_SPEECH_API_KEY", "test-only-secret")
    monkeypatch.setenv("DOUBAO_SPEECH_PUBLIC_VOICE_TYPE", "zh_female_vv_uranus_bigtts")
    monkeypatch.setenv("DOUBAO_SPEECH_PUBLIC_MALE_VOICE_TYPE", "zh_male_kailangxuezhang_uranus_bigtts")

    created = audio_center.add_custom_cloud_voice({
        "name": "自定义新闻女声", "voice_id": "zh_female_custom_bigtts", "playback_rate": 1.10,
    })
    custom = next(item for item in created["profiles"] if item["name"] == "自定义新闻女声")
    public = next(item for item in created["profiles"] if item["id"] == "doubao:public_female")

    assert custom["is_custom_cloud_voice"] is True
    assert custom["resource_id"] == "seed-tts-2.0"
    assert custom["speech_rate"] == 1.10
    assert custom["provider_speech_rate"] == 10
    assert "provider_voice_id" not in custom
    assert audio_center.get_voice_profile(custom["id"])["provider_voice_id"] == "zh_female_custom_bigtts"

    updated = audio_center.set_cloud_voice_playback_rate(custom["id"], {"playback_rate": 1.50})
    changed = next(item for item in updated["profiles"] if item["id"] == custom["id"])
    unchanged = next(item for item in updated["profiles"] if item["id"] == public["id"])
    assert changed["speech_rate"] == 1.50
    assert changed["provider_speech_rate"] == 50
    assert unchanged["speech_rate"] == 1.25

    with pytest.raises(audio_center.AudioCenterError, match="已存在"):
        audio_center.add_custom_cloud_voice({"name": "另一个名字", "voice_id": "zh_female_custom_bigtts"})
    with pytest.raises(audio_center.AudioCenterError, match="内置"):
        audio_center.remove_custom_cloud_voice("doubao:public_female")

    monkeypatch.setattr(audio_center, "find_avatar_role_by_voice_profile", lambda profile_id: {"name": "测试角色"} if profile_id == custom["id"] else None)
    with pytest.raises(audio_center.AudioCenterError, match="先解除关联"):
        audio_center.remove_custom_cloud_voice(custom["id"])
    monkeypatch.setattr(audio_center, "find_avatar_role_by_voice_profile", lambda _profile_id: None)

    removed = audio_center.remove_custom_cloud_voice(custom["id"])
    assert custom["id"] not in {item["id"] for item in removed["profiles"]}


def test_custom_clone_voice_defaults_to_icl_and_preview_stays_frozen_after_removal(tmp_path, monkeypatch):
    monkeypatch.setattr(audio_center, "AUDIO_CENTER_DIR", tmp_path / ".backlot" / "audio")
    monkeypatch.setattr(audio_center, "AUDIO_CENTER_FILE", audio_center.AUDIO_CENTER_DIR / "audio_center.json")
    monkeypatch.setattr(audio_center, "PREVIEW_DIRECTORY", audio_center.AUDIO_CENTER_DIR / "previews")
    monkeypatch.setattr(audio_center.VoiceboxTTS, "get_status", classmethod(lambda cls: ToolStatus.UNAVAILABLE))
    monkeypatch.setenv("DOUBAO_SPEECH_API_KEY", "test-only-secret")
    calls = []

    def fake_generate_voice_audio(**kwargs):
        calls.append(kwargs)
        output = Path(kwargs["output_path"])
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"custom-cloud-preview")
        return ToolResult(success=True, data={"audio_duration_seconds": 2.0, "metadata_path": "timing.json"})

    monkeypatch.setattr(audio_center, "generate_voice_audio", fake_generate_voice_audio)
    created = audio_center.add_custom_cloud_voice({
        "name": "自定义复刻声", "voice_id": "S_custom_clone", "playback_rate": 1.25,
    })
    custom = next(item for item in created["profiles"] if item["name"] == "自定义复刻声")
    assert custom["resource_id"] == "seed-icl-2.0"

    queued = audio_center.start_preview({"profile_id": custom["id"], "text": "冻结后的自定义试听。"})
    assert queued["preview_job"]["playback_rate"] == 1.25
    assert "profile_snapshot" not in queued["preview_job"]
    audio_center.set_cloud_voice_playback_rate(custom["id"], {"playback_rate": 1.75})
    audio_center.remove_custom_cloud_voice(custom["id"])

    completed = audio_center.generate_preview()

    assert calls[0]["profile"]["provider_voice_id"] == "S_custom_clone"
    assert calls[0]["profile"]["resource_id"] == "seed-icl-2.0"
    assert calls[0]["profile"]["speech_rate"] == 1.25
    assert completed["previews"][0]["profile_name"] == "自定义复刻声"
