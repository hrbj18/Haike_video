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
    # Keep the catalogue deterministic regardless of the developer's local
    # .env.secrets.local; Tencent presets stay listed but read as unavailable.
    monkeypatch.setenv("TENCENT_SECRET_ID", "")
    monkeypatch.setenv("TENCENT_SECRET_KEY", "")

    center = audio_center.read_audio_center()

    # The six curated 腾讯云 presets are part of the visible catalogue.
    tencent_ids = {voice["profile_id"] for voice in audio_center.TENCENT_PRESET_VOICES}
    assert {item["id"] for item in center["profiles"]} == {
        "local-yaya", "doubao:yaya", "doubao:mengmeng",
    } | tencent_ids
    assert {item["id"] for item in center["providers"]} == {"voicebox_tts", "doubao", "tencent"}
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
    monkeypatch.setenv("TENCENT_SECRET_ID", "")
    monkeypatch.setenv("TENCENT_SECRET_KEY", "")

    center = audio_center.read_audio_center()

    assert [item["name"] for item in center["profiles"]] == [
        "雅雅", "檬檬", "雅雅（强情感版）", "檬檬（强情感版）", "豆包雅雅", "豆包檬檬",
        *[voice["name"] for voice in audio_center.TENCENT_PRESET_VOICES],
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


def test_custom_tencent_voice_uses_pasted_voice_type_id_and_snaps_to_speed_anchors(tmp_path, monkeypatch):
    monkeypatch.setattr(audio_center, "AUDIO_CENTER_DIR", tmp_path / ".backlot" / "audio")
    monkeypatch.setattr(audio_center, "AUDIO_CENTER_FILE", audio_center.AUDIO_CENTER_DIR / "audio_center.json")
    monkeypatch.setattr(audio_center, "PREVIEW_DIRECTORY", audio_center.AUDIO_CENTER_DIR / "previews")
    monkeypatch.setattr(audio_center.VoiceboxTTS, "get_status", classmethod(lambda cls: ToolStatus.UNAVAILABLE))

    created = audio_center.add_custom_cloud_voice({
        "provider": "tencent",
        "name": "腾讯自定义童声",
        "voice_id": "601010",
        "playback_rate": 1.10,
    })
    custom = next(item for item in created["profiles"] if item["name"] == "腾讯自定义童声")

    assert custom["id"].startswith("tencent:custom:")
    assert custom["provider_id"] == "tencent"
    assert custom["is_custom_cloud_voice"] is True
    # Tencent only plays its discrete Speed anchors, so 1.10x must be stored as
    # the achievable 1.20x rather than a value the provider never renders.
    assert custom["speech_rate"] == 1.20
    assert custom["provider_speech_rate"] == 1
    assert custom["speech_rate_options"] == [0.6, 0.8, 1.0, 1.2, 1.5, 1.7, 2.0]
    assert audio_center.get_voice_profile(custom["id"])["provider_voice_id"] == "601010"

    updated = audio_center.set_cloud_voice_playback_rate(custom["id"], {"playback_rate": 1.05})
    changed = next(item for item in updated["profiles"] if item["id"] == custom["id"])
    assert changed["speech_rate"] == 1.00
    assert changed["provider_speech_rate"] == 0

    with pytest.raises(audio_center.AudioCenterError, match="纯数字"):
        audio_center.add_custom_cloud_voice({"provider": "tencent", "name": "非法 ID", "voice_id": "TTS-601010"})
    with pytest.raises(audio_center.AudioCenterError, match="已存在"):
        audio_center.add_custom_cloud_voice({"provider": "tencent", "name": "重复预设", "voice_id": "502001"})
    with pytest.raises(audio_center.AudioCenterError, match="已存在"):
        audio_center.add_custom_cloud_voice({"provider": "tencent", "name": "重复自定义", "voice_id": "601010"})
    with pytest.raises(audio_center.AudioCenterError, match="内置"):
        audio_center.remove_custom_cloud_voice("tencent:voice:502001")

    removed = audio_center.remove_custom_cloud_voice(custom["id"])
    assert custom["id"] not in {item["id"] for item in removed["profiles"]}
    assert audio_center._load()["cloud_voice_rates"].get(custom["id"]) is None


def test_custom_cloud_records_written_before_providers_existed_stay_doubao(tmp_path, monkeypatch):
    monkeypatch.setattr(audio_center, "AUDIO_CENTER_DIR", tmp_path / ".backlot" / "audio")
    monkeypatch.setattr(audio_center, "AUDIO_CENTER_FILE", audio_center.AUDIO_CENTER_DIR / "audio_center.json")
    monkeypatch.setattr(audio_center, "PREVIEW_DIRECTORY", audio_center.AUDIO_CENTER_DIR / "previews")
    monkeypatch.setattr(audio_center.VoiceboxTTS, "get_status", classmethod(lambda cls: ToolStatus.UNAVAILABLE))
    state = audio_center._load()
    state["custom_cloud_profiles"] = [
        {"id": "doubao:custom:legacy", "name": "旧记录", "voice_id": "zh_male_legacy", "resource_id": "seed-tts-2.0"},
        {"id": "tencent:custom:broken", "name": "没有 provider 字段", "voice_id": "601010", "resource_id": ""},
    ]
    audio_center._write(state)

    profiles = audio_center._catalog_profiles()
    legacy = next(item for item in profiles if item["id"] == "doubao:custom:legacy")
    assert legacy["provider_id"] == "doubao"
    assert legacy["is_custom_cloud_voice"] is True
    # A Tencent record without its provider marker is not a valid Doubao record.
    assert all(item["id"] != "tencent:custom:broken" for item in profiles)


def _prepare_two_local_voices(tmp_path, monkeypatch) -> None:
    """Two visible local voices and a writable audio centre, no network."""
    monkeypatch.setattr(audio_center, "AUDIO_CENTER_DIR", tmp_path / ".backlot" / "audio")
    monkeypatch.setattr(audio_center, "AUDIO_CENTER_FILE", audio_center.AUDIO_CENTER_DIR / "audio_center.json")
    monkeypatch.setattr(audio_center, "PREVIEW_DIRECTORY", audio_center.AUDIO_CENTER_DIR / "previews")
    monkeypatch.setattr(audio_center.VoiceboxTTS, "get_status", classmethod(lambda cls: ToolStatus.AVAILABLE))
    monkeypatch.setattr(audio_center.VoiceboxTTS, "list_profiles", classmethod(lambda cls: [
        {"id": "yaya-id", "name": "雅雅", "language": "zh", "voice_type": "preset", "default_engine": "qwen", "description": "预设"},
        {"id": "mengmeng-id", "name": "檬檬", "language": "zh", "voice_type": "preset", "default_engine": "qwen", "description": "预设"},
    ]))


def _stub_local_take(self, inputs):
    output = Path(inputs["output_path"])
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(b"RIFFpreview")
    return SimpleNamespace(success=True, data={"duration": 1.8}, error=None)


@pytest.fixture(autouse=True)
def reset_live_preview_registry():
    """Preview liveness is process state; keep it from leaking between tests."""
    audio_center._LIVE_PREVIEWS.clear()
    yield
    audio_center._LIVE_PREVIEWS.clear()


def test_a_preview_interrupted_by_a_restart_is_released_by_the_next_read(tmp_path, monkeypatch):
    """Closing the console mid-preview must not block every later preview."""
    _prepare_two_local_voices(tmp_path, monkeypatch)
    monkeypatch.setattr(audio_center.VoiceboxTTS, "execute", _stub_local_take)

    queued = audio_center.start_preview({"profile_id": "yaya-id", "text": "第一段试听。"})
    assert queued["preview_job"]["status"] == "generating"

    # Simulate a restart: the worker is gone but the file still says "generating".
    audio_center._LIVE_PREVIEWS.clear()

    recovered = audio_center.read_audio_center()
    assert recovered["preview_job"]["status"] == "failed"
    assert "中断" in recovered["preview_job"]["error"]

    retried = audio_center.start_preview({"profile_id": "mengmeng-id", "text": "第二段试听。"})
    assert retried["preview_job"]["status"] == "generating"
    assert retried["preview_job"]["profile_id"] == "mengmeng-id"

    finished = audio_center.generate_preview()
    assert finished["preview_job"]["status"] == "completed"
    assert finished["previews"][0]["profile_name"] == "檬檬"


def test_starting_a_preview_also_recovers_an_interrupted_one_directly(tmp_path, monkeypatch):
    _prepare_two_local_voices(tmp_path, monkeypatch)
    monkeypatch.setattr(audio_center.VoiceboxTTS, "execute", _stub_local_take)

    first = audio_center.start_preview({"profile_id": "yaya-id", "text": "第一段试听。"})
    assert first["preview_job"]["status"] == "generating"
    audio_center._LIVE_PREVIEWS.clear()

    # No intervening read: start_preview itself must clear the dead slot.
    second = audio_center.start_preview({"profile_id": "mengmeng-id", "text": "第二段试听。"})
    assert second["preview_job"]["status"] == "generating"
    assert second["preview_job"]["id"] != first["preview_job"]["id"]


def test_a_genuinely_running_preview_still_refuses_a_second_request(tmp_path, monkeypatch):
    _prepare_two_local_voices(tmp_path, monkeypatch)

    audio_center.start_preview({"profile_id": "yaya-id", "text": "第一段试听。"})
    with pytest.raises(audio_center.AudioCenterError, match="已有试听正在生成"):
        audio_center.start_preview({"profile_id": "mengmeng-id", "text": "第二段试听。"})


def test_a_hung_worker_past_the_stale_window_is_released(tmp_path, monkeypatch):
    _prepare_two_local_voices(tmp_path, monkeypatch)

    audio_center.start_preview({"profile_id": "yaya-id", "text": "第一段试听。"})
    # The worker never returns but still holds its slot in the registry.
    monkeypatch.setattr(audio_center, "_preview_started_epoch", lambda job: 0.0)

    released = audio_center.read_audio_center()
    assert released["preview_job"]["status"] == "failed"


def test_finishing_a_preview_frees_the_slot_for_the_next_one(tmp_path, monkeypatch):
    _prepare_two_local_voices(tmp_path, monkeypatch)
    monkeypatch.setattr(audio_center.VoiceboxTTS, "execute", _stub_local_take)

    audio_center.start_preview({"profile_id": "yaya-id", "text": "第一段试听。"})
    audio_center.generate_preview()

    # The completed job must not leave a stale liveness entry behind.
    assert not audio_center._preview_worker_is_live(str(audio_center._load()["preview_job"]["id"]))
    third = audio_center.start_preview({"profile_id": "mengmeng-id", "text": "第二段试听。"})
    assert third["preview_job"]["status"] == "generating"


def test_mark_preview_failed_leaves_a_superseding_job_alone(tmp_path, monkeypatch):
    _prepare_two_local_voices(tmp_path, monkeypatch)

    stale = audio_center.start_preview({"profile_id": "yaya-id", "text": "第一段试听。"})
    stale_id = stale["preview_job"]["id"]
    audio_center._LIVE_PREVIEWS.clear()
    audio_center.start_preview({"profile_id": "mengmeng-id", "text": "第二段试听。"})

    state = audio_center.mark_preview_failed("旧任务失败", stale_id)

    assert state["preview_job"]["status"] == "generating"
    assert state["preview_job"]["profile_id"] == "mengmeng-id"


def test_tencent_voice_rate_is_configurable_and_snapped_to_playable_anchors(tmp_path, monkeypatch):
    """Tencent voices own a rate control too; unplayable rates must not be stored."""
    monkeypatch.setattr(audio_center, "AUDIO_CENTER_DIR", tmp_path / ".backlot" / "audio")
    monkeypatch.setattr(audio_center, "AUDIO_CENTER_FILE", audio_center.AUDIO_CENTER_DIR / "audio_center.json")
    monkeypatch.setattr(audio_center, "PREVIEW_DIRECTORY", audio_center.AUDIO_CENTER_DIR / "previews")
    monkeypatch.setattr(audio_center, "provider_status", lambda provider_id: ToolStatus.AVAILABLE)

    center = audio_center.read_audio_center()
    voice = next(item for item in center["profiles"] if item["id"] == "tencent:voice:502005")
    assert voice["speech_rate_options"] == [0.6, 0.8, 1.0, 1.2, 1.5, 1.7, 2.0]

    updated = audio_center.set_cloud_voice_playback_rate("tencent:voice:502005", {"playback_rate": 1.10})
    changed = next(item for item in updated["profiles"] if item["id"] == "tencent:voice:502005")
    untouched = next(item for item in updated["profiles"] if item["id"] == "tencent:voice:501001")

    # 1.10x has no Tencent Speed anchor; 1.20x is the closest playable rate.
    assert changed["speech_rate"] == 1.20
    assert changed["provider_speech_rate"] == 1
    assert untouched["speech_rate"] == 1.25
