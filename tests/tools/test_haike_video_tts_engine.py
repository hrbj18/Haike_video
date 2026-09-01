from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from types import ModuleType, SimpleNamespace
from uuid import uuid5

import pytest

from tools.audio.haike_video_tts_engine import (
    IdempotencyConflict,
    JobLedgerError,
    ProfileError,
    ProfileStore,
    QwenTTSRuntime,
    REQUEST_NAMESPACE,
    SerialTTSJobs,
    split_text,
)


def test_clean_profile_store_exposes_checked_in_chinese_presets(tmp_path):
    store = ProfileStore(data_dir=tmp_path)

    profiles = store.public_profiles()

    assert {profile["id"] for profile in profiles} >= {
        "haike_video-qwen-serena",
        "haike_video-qwen-vivian",
        "haike_video-qwen-dylan",
    }
    assert all(profile["voice_type"] == "preset" for profile in profiles)
    assert all(profile["available"] is True for profile in profiles)
    assert all(len(profile["voice_signature"]) == 64 for profile in profiles)


def test_clone_signature_changes_with_private_sample_or_reference_without_exposing_them(tmp_path):
    sample = tmp_path / "sample.wav"
    sample.write_bytes(b"private-audio-a")
    profile_file = tmp_path / "profiles.json"
    profile_file.write_text(json.dumps({
        "profiles": [{
            "id": "yaya",
            "name": "雅雅",
            "voice_type": "cloned",
            "preferred_sample_id": "s1",
            "samples": [{"id": "s1", "audio_path": "sample.wav", "reference_text": "私有参考原文"}],
        }]
    }, ensure_ascii=False), encoding="utf-8")
    store = ProfileStore(data_dir=tmp_path, default_file=tmp_path / "missing.json")

    first = store.public_profiles()[0]
    sample.write_bytes(b"private-audio-b")
    second = store.public_profiles()[0]
    data = json.dumps(first, ensure_ascii=False)

    assert first["available"] is True
    assert first["voice_signature"] != second["voice_signature"]
    assert "sample.wav" not in data
    assert "私有参考原文" not in data


def test_invalid_clone_does_not_hide_other_public_profiles(tmp_path):
    profile_file = tmp_path / "profiles.json"
    profile_file.write_text(json.dumps({
        "profiles": [
            {"id": "good", "voice_type": "preset", "preset_voice_id": "Serena"},
            {"id": "bad", "voice_type": "cloned", "samples": []},
        ]
    }), encoding="utf-8")
    profiles = ProfileStore(data_dir=tmp_path, default_file=tmp_path / "missing.json").public_profiles()

    by_id = {profile["id"]: profile for profile in profiles}
    assert by_id["good"]["available"] is True
    assert by_id["bad"]["available"] is False
    assert by_id["bad"]["voice_signature"] is None


def test_cloned_profile_must_resolve_inside_private_data_directory(tmp_path):
    outside = tmp_path.parent / "outside.wav"
    outside.write_bytes(b"RIFF")
    (tmp_path / "profiles.json").write_text(json.dumps({
        "profiles": [{
            "id": "unsafe",
            "name": "unsafe",
            "voice_type": "cloned",
            "samples": [{"id": "s1", "audio_path": "../outside.wav", "reference_text": "测试"}],
        }]
    }), encoding="utf-8")

    with pytest.raises(ProfileError, match="越过本地数据目录"):
        ProfileStore(data_dir=tmp_path).get("unsafe")


def test_split_text_preserves_every_character_and_honours_sentence_boundaries():
    text = "第一句很短。第二句也很短！第三句用于确认长文本会分段。"

    chunks = split_text(text, 15)

    assert "".join(chunks) == text
    assert len(chunks) >= 2
    assert all(len(chunk) <= 15 for chunk in chunks)


class _Store:
    def get(self, profile_id):
        if profile_id != "yaya":
            raise ProfileError("missing")
        return {"id": "yaya"}

    def public_profiles(self):
        return [{"id": "yaya", "name": "雅雅"}]

    def voice_signature(self, profile_id):
        assert profile_id == "yaya"
        return "a" * 64


class _Runtime:
    def __init__(self):
        self.store = _Store()
        self.loaded_models = []
        self.calls = []

    def generate(self, **kwargs):
        self.calls.append(kwargs["text"])
        time.sleep(0.02)
        kwargs["output_path"].parent.mkdir(parents=True, exist_ok=True)
        kwargs["output_path"].write_bytes(b"RIFFtest")
        return {"duration": 1.0, "sample_rate": 24000, "chunks": 1}


def test_serial_jobs_persist_and_complete_in_submission_order(tmp_path):
    runtime = _Runtime()
    jobs = SerialTTSJobs(tmp_path, runtime=runtime)
    first = jobs.submit({"text": "第一条", "profile": "yaya"})
    second = jobs.submit({"text": "第二条", "profile": "yaya"})
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if jobs.status(second["id"])["status"] == "completed":
            break
        time.sleep(0.01)

    assert runtime.calls == ["第一条", "第二条"]
    assert jobs.status(first["id"])["status"] == "completed"
    assert jobs.output_file(second["id"]).read_bytes() == b"RIFFtest"


def test_restart_marks_stale_jobs_failed(tmp_path):
    job_dir = tmp_path / "generations" / "old"
    job_dir.mkdir(parents=True)
    (job_dir / "job.json").write_text(json.dumps({"id": "old", "status": "generating"}), encoding="utf-8")

    jobs = SerialTTSJobs(tmp_path, runtime=_Runtime())

    assert jobs.status("old")["status"] == "failed"
    assert "重启" in jobs.status("old")["error"]


def test_request_id_is_idempotent_and_conflicting_payload_is_rejected(tmp_path):
    runtime = _Runtime()
    jobs = SerialTTSJobs(tmp_path, runtime=runtime)
    payload = {"text": "同一条", "profile": "yaya", "request_id": "project.line.attempt-1"}

    first = jobs.submit(payload)
    duplicate = jobs.submit(payload)
    with pytest.raises(IdempotencyConflict, match="不同"):
        jobs.submit({**payload, "text": "另一条"})
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and jobs.status(first["id"])["status"] != "completed":
        time.sleep(0.01)

    assert duplicate["id"] == first["id"]
    assert first["request_id"] == "project.line.attempt-1"
    assert first["voice_signature"] == "a" * 64
    assert runtime.calls == ["同一条"]


def test_submission_rejects_drifted_frozen_voice_signature(tmp_path):
    jobs = SerialTTSJobs(tmp_path, runtime=_Runtime())

    with pytest.raises(ProfileError, match="冻结音色签名"):
        jobs.submit({
            "text": "测试",
            "profile": "yaya",
            "request_id": "project.line.attempt-2",
            "voice_signature": "b" * 64,
        })


def test_one_data_directory_cannot_be_owned_by_two_tts_instances(tmp_path):
    first = SerialTTSJobs(tmp_path, runtime=_Runtime())
    try:
        with pytest.raises(RuntimeError, match="禁止并发"):
            SerialTTSJobs(tmp_path, runtime=_Runtime())
    finally:
        first.close()

    replacement = SerialTTSJobs(tmp_path, runtime=_Runtime())
    replacement.close()


def test_model_cache_key_tracks_effective_model_identity(tmp_path, monkeypatch):
    import tools.audio.haike_video_tts_engine as engine_module

    fake_torch = ModuleType("torch")
    fake_torch.cuda = SimpleNamespace(is_available=lambda: False, empty_cache=lambda: None)
    fake_torch.float32 = "float32"
    fake_torch.bfloat16 = "bfloat16"
    fake_hub = ModuleType("huggingface_hub")
    fake_hub.snapshot_download = lambda repo_id, **_kwargs: str(tmp_path / repo_id.replace("/", "-"))
    calls: list[str] = []

    class _FakeQwen:
        @classmethod
        def from_pretrained(cls, model_path, **_kwargs):
            calls.append(str(model_path))
            return object()

    fake_qwen = ModuleType("qwen_tts")
    fake_qwen.Qwen3TTSModel = _FakeQwen
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    monkeypatch.setitem(sys.modules, "huggingface_hub", fake_hub)
    monkeypatch.setitem(sys.modules, "qwen_tts", fake_qwen)
    monkeypatch.setattr(engine_module, "runtime_dependencies", lambda: {"qwen_tts": True, "torch": True, "soundfile": True, "numpy": True})
    runtime = QwenTTSRuntime(tmp_path)

    monkeypatch.setenv("HAIKE_VIDEO_TTS_MAX_LOADED_MODELS", "2")
    monkeypatch.setenv("HAIKE_VIDEO_TTS_CUSTOM_MODEL", "local/model-a")
    first = runtime._load_model("custom")
    assert runtime._load_model("custom") is first
    assert len(calls) == 1
    monkeypatch.setenv("HAIKE_VIDEO_TTS_CUSTOM_MODEL", "local/model-b")
    second = runtime._load_model("custom")

    assert first is not second
    assert runtime.loaded_models == ["local/model-a", "local/model-b"]
    assert len(calls) == 2


def test_clone_prompt_cache_key_includes_full_voice_signature(tmp_path):
    sample = tmp_path / "sample.wav"
    sample.write_bytes(b"sample")

    class _PromptStore:
        def preferred_sample(self, _profile):
            return {"resolved_audio_path": str(sample), "reference_text": "参考原文"}

    class _PromptModel:
        def __init__(self):
            self.calls = 0

        def create_voice_clone_prompt(self, **_kwargs):
            self.calls += 1
            return f"prompt-{self.calls}"

    runtime = QwenTTSRuntime(tmp_path)
    runtime.store = _PromptStore()
    model = _PromptModel()
    profile = {"id": "yaya"}

    first = runtime._clone_prompt(model, profile, "a" * 64)
    duplicate = runtime._clone_prompt(model, profile, "a" * 64)
    changed = runtime._clone_prompt(model, profile, "b" * 64)

    assert first == duplicate
    assert changed != first
    assert model.calls == 2


def test_atomic_json_replace_retries_windows_sharing_violation(tmp_path, monkeypatch):
    import tools.audio.haike_video_tts_engine as engine_module

    destination = tmp_path / "job.json"
    real_replace = engine_module.os.replace
    attempts = 0

    def flaky_replace(source, target):
        nonlocal attempts
        attempts += 1
        if attempts <= 2:
            error = PermissionError(13, "sharing violation", str(target))
            error.winerror = 5
            raise error
        return real_replace(source, target)

    monkeypatch.setattr(engine_module.os, "replace", flaky_replace)
    engine_module._write_json_atomic(destination, {"status": "completed"})

    assert attempts == 3
    assert json.loads(destination.read_text(encoding="utf-8"))["status"] == "completed"


def test_terminal_save_failure_does_not_kill_the_only_queue_worker(tmp_path, monkeypatch):
    runtime = _Runtime()
    jobs = SerialTTSJobs(tmp_path, runtime=runtime)
    real_save = jobs._save
    failed_once = False

    def flaky_save(job):
        nonlocal failed_once
        if job.get("status") == "completed" and not failed_once:
            failed_once = True
            raise PermissionError("simulated terminal save failure")
        return real_save(job)

    monkeypatch.setattr(jobs, "_save", flaky_save)
    jobs.submit({"text": "第一条", "profile": "yaya"})
    second = jobs.submit({"text": "第二条", "profile": "yaya"})
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if len(runtime.calls) == 2 and jobs.status(second["id"])["status"] == "completed":
            break
        time.sleep(0.01)

    assert failed_once is True
    assert runtime.calls == ["第一条", "第二条"]
    assert jobs.status(second["id"])["status"] == "completed"
    jobs.close()


def test_corrupt_existing_idempotency_ledger_is_never_overwritten(tmp_path):
    jobs = SerialTTSJobs(tmp_path, runtime=_Runtime())
    request_id = "project.line.corrupt-attempt"
    job_id = str(uuid5(REQUEST_NAMESPACE, request_id))
    job_file = jobs._job_file(job_id)
    job_file.parent.mkdir(parents=True, exist_ok=True)
    job_file.write_text("{broken-json", encoding="utf-8")

    with pytest.raises(JobLedgerError, match="禁止覆盖"):
        jobs.submit({"text": "不能重复生成", "profile": "yaya", "request_id": request_id})

    assert job_file.read_text(encoding="utf-8") == "{broken-json"
    assert jobs.runtime.calls == []
    jobs.close()
