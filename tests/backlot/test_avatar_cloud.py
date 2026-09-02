"""Tests for the durable multi-speaker cloud avatar contract (no cloud calls)."""

from __future__ import annotations

import json
import os
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from backlot import avatar_cloud as cloud_mod
from backlot import avatar_import as avatar_mod
from backlot import avatar_roles as roles_mod
from backlot import server as server_mod
from backlot import state as state_mod
from backlot.avatar_import import read_avatar_package
from schemas.artifacts import validate_artifact
from tools.base_tool import ToolStatus


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def make_project(root: Path) -> Path:
    project = root / "cloud-avatar"
    (project / "artifacts").mkdir(parents=True)
    write_json(project / "project.json", {"project_id": "cloud-avatar", "title": "云端双角色数字人", "pipeline_type": "avatar-spokesperson"})
    write_json(project / "artifacts" / "script.json", {
        "sections": [
            {"turn_id": "T001", "speaker_id": "yaya", "speaker_name": "雅雅", "text": "欢迎来到数字人工作台。"},
            {"turn_id": "T002", "speaker_id": "mengmeng", "speaker_name": "檬檬", "text": "现在开始播报第二条消息。"},
            {"turn_id": "T003", "speaker_id": "yaya", "speaker_name": "雅雅", "text": "接下来是今天的重点。"},
            {"turn_id": "T004", "speaker_id": "mengmeng", "speaker_name": "檬檬", "text": "感谢收看，我们下期见。"},
        ],
    })
    return project


@pytest.fixture
def projects_root(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    root.mkdir()
    monkeypatch.setattr(state_mod, "PROJECTS_DIR", root)
    monkeypatch.setattr(server_mod, "PROJECTS_DIR", root)
    monkeypatch.setattr(server_mod, "_summary_cache", {})
    monkeypatch.setattr(server_mod, "_PROJECTS_ROOT_STR", os.path.normcase(str(root.resolve())))
    return root


@pytest.fixture
def client(projects_root, monkeypatch):
    async def no_watch():
        return None

    monkeypatch.setattr(server_mod, "_watch_projects", no_watch)
    with TestClient(server_mod.create_app()) as instance:
        yield instance


@pytest.fixture
def project(tmp_path, monkeypatch):
    role_root = tmp_path / "avatar_roles"
    monkeypatch.setattr(roles_mod, "ROLE_DIRECTORY", role_root)
    monkeypatch.setattr(roles_mod, "ROLE_FILE", role_root / "roles.json")
    monkeypatch.setattr(roles_mod, "ROLE_ASSET_DIRECTORY", role_root / "assets")
    return make_project(tmp_path)


def fake_video_probe(path: Path) -> dict:
    return {
        "duration_seconds": 7.86,
        "size_bytes": path.stat().st_size,
        "video": {"present": True, "codec": "h264", "width": 1080, "height": 1920, "fps": 25.0, "pixel_format": "yuv420p"},
        "audio": {"present": True, "codec": "aac", "sample_rate": 48000, "channels": 1},
    }


def create_role(tmp_path: Path, speaker_id: str, name: str) -> dict:
    role = roles_mod.create_avatar_role({"name": name, "license": "本人授权"})
    image = tmp_path / f"{speaker_id}-front.png"
    Image.new("RGBA", (640, 640), "#4a90a4" if speaker_id == "yaya" else "#d06b78").save(image)
    temporary, target = roles_mod.prepare_role_reference_upload(role["role_id"], "front", image.name)
    temporary.write_bytes(image.read_bytes())
    return roles_mod.finalize_role_reference_upload(role["role_id"], "front", temporary, target, image.name)


def upload_presenter(project: Path, tmp_path: Path, speaker_id: str, color: str) -> None:
    presenter = tmp_path / f"{speaker_id}-presenter.png"
    Image.new("RGB", (720, 1280), color).save(presenter)
    temporary, target = cloud_mod.prepare_presenter_upload(project, speaker_id, presenter.name)
    temporary.write_bytes(presenter.read_bytes())
    cloud_mod.finalize_presenter_upload(project, speaker_id, temporary, target, presenter.name)


def upload_landscape_presenter(project: Path, tmp_path: Path, speaker_id: str, color: str) -> None:
    presenter = tmp_path / f"{speaker_id}-landscape.png"
    Image.new("RGB", (1600, 900), color).save(presenter)
    temporary, target = cloud_mod.prepare_presenter_upload(project, speaker_id, presenter.name)
    temporary.write_bytes(presenter.read_bytes())
    cloud_mod.finalize_presenter_upload(project, speaker_id, temporary, target, presenter.name)


def setup_ready_cloud_package(project: Path, tmp_path: Path, monkeypatch) -> dict:
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")
    monkeypatch.setenv("DASHSCOPE_WORKSPACE_ID", "test-workspace")
    package = avatar_mod.initialize_avatar_package(project, {"generation_mode": "dashscope_wan_s2v"})
    assert package["settings"]["require_asr"] is False
    yaya = create_role(tmp_path, "yaya", "雅雅")
    mengmeng = create_role(tmp_path, "mengmeng", "檬檬")
    cloud_mod.select_cloud_avatar_role(project, "yaya", yaya["role_id"])
    cloud_mod.select_cloud_avatar_role(project, "mengmeng", mengmeng["role_id"])
    upload_presenter(project, tmp_path, "yaya", "#2e4769")
    upload_presenter(project, tmp_path, "mengmeng", "#6b2e4e")

    monkeypatch.setattr(cloud_mod, "_probe_audio", lambda path: {"duration_seconds": 7.86, "codec": "mp3", "sample_rate": 48000, "channels": 1})
    for turn_id in ("T001", "T002", "T003", "T004"):
        temporary, target = cloud_mod.prepare_driving_audio_upload(project, turn_id, f"{turn_id}.mp3")
        temporary.write_bytes(f"fake-audio-{turn_id}".encode("utf-8"))
        cloud_mod.finalize_driving_audio_upload(project, turn_id, temporary, target, f"{turn_id}.mp3")
    return read_avatar_package(project) or {}


def test_runninghub_package_is_provider_distinct_and_schema_valid(project: Path, monkeypatch):
    monkeypatch.setenv("RUNNINGHUB_API_KEY", "test-key")
    monkeypatch.setenv("RUNNINGHUB_WORKFLOW_ID", "123456")
    monkeypatch.setenv("RUNNINGHUB_WORKFLOW_TEMPLATE", str(Path("config/runninghub/longcat_avatar_api.json").resolve()))
    package = avatar_mod.initialize_avatar_package(project, {"generation_mode": "runninghub_longcat"})
    assert package["generation_mode"] == "runninghub_longcat"
    assert package["import_mode"] == "per_turn"
    assert package["settings"]["require_asr"] is False
    assert package["cloud"]["provider"] == "runninghub_longcat"
    assert package["cloud"]["model"] == "InfiniteTalk-exact-clock-v2"
    assert package["cloud"]["resolution"] == "448x560"
    validate_artifact("avatar_source_package", package)


def test_runninghub_presenter_input_uses_exact_workflow_4x5_frame(project: Path, tmp_path: Path, monkeypatch):
    monkeypatch.setattr(cloud_mod, "runninghub_configuration", lambda: {
        "configured": True,
        "api_key_configured": True,
        "workflow_id_configured": True,
        "workflow_id": "2094449979141218305",
        "workflow_id_suffix": "218305",
        "workflow_profile": "infinitetalk_448x560_exact_clock_v2",
        "template_sha256": "0" * 64,
        "issues": [],
    })
    avatar_mod.initialize_avatar_package(project, {"generation_mode": "runninghub_longcat"})
    upload_presenter(project, tmp_path, "yaya", "#2e4769")
    upload_presenter(project, tmp_path, "mengmeng", "#6b2e4e")
    package = cloud_mod.configure_cloud_render_spec(project, {
        "aspect_ratio": "portrait",
        "resolution": "448x560",
        "default_fit_mode": "contain_blur",
    })
    for binding in package["speaker_bindings"]:
        provider_input = binding["aspect_fit"]["provider_input"]
        assert provider_input["media"] == {"width": 448, "height": 560, "format": "PNG"}
        assert binding["aspect_fit"]["target_label"] == "供应商输入 4:5 · 448×560"
    validate_artifact("avatar_source_package", package)


def test_runninghub_old_package_contract_is_blocked_before_queue_or_paid_submit(project: Path, tmp_path: Path, monkeypatch):
    monkeypatch.setattr(cloud_mod, "runninghub_configuration", lambda: {
        "configured": True,
        "api_key_configured": True,
        "workflow_id_configured": True,
        "workflow_id": "2094449979141218305",
        "workflow_id_suffix": "218305",
        "workflow_profile": "infinitetalk_448x560_exact_clock_v2",
        "template_sha256": "0" * 64,
        "issues": [],
    })
    avatar_mod.initialize_avatar_package(project, {"generation_mode": "runninghub_longcat"})
    upload_presenter(project, tmp_path, "yaya", "#2e4769")
    package = read_avatar_package(project) or {}
    package["cloud"]["model"] = "LongCat-1.5"
    package["cloud"]["resolution"] = "320x576"
    avatar_mod._save_package(project, package)
    with pytest.raises(cloud_mod.AvatarCloudError, match="旧 RunningHub 输入合同"):
        cloud_mod.queue_cloud_turn(project, "T001", purpose="sample")


def setup_ready_cloud_package_without_role_library(project: Path, tmp_path: Path, monkeypatch) -> dict:
    """The paid provider needs presenter images and audio, not identity profiles."""
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")
    monkeypatch.setenv("DASHSCOPE_WORKSPACE_ID", "test-workspace")
    avatar_mod.initialize_avatar_package(project, {"generation_mode": "dashscope_wan_s2v"})
    upload_presenter(project, tmp_path, "yaya", "#2e4769")
    upload_presenter(project, tmp_path, "mengmeng", "#6b2e4e")
    monkeypatch.setattr(cloud_mod, "_probe_audio", lambda path: {
        "duration_seconds": 7.86, "codec": "mp3", "sample_rate": 48000, "channels": 1,
    })
    for turn_id in ("T001", "T002", "T003", "T004"):
        temporary, target = cloud_mod.prepare_driving_audio_upload(project, turn_id, f"{turn_id}.mp3")
        temporary.write_bytes(f"fake-audio-{turn_id}".encode("utf-8"))
        cloud_mod.finalize_driving_audio_upload(project, turn_id, temporary, target, f"{turn_id}.mp3")
    return read_avatar_package(project) or {}


class FakeClient:
    uploaded: list[str] = []

    def upload_file(self, path: Path) -> dict:
        assert path.is_file()
        self.uploaded.append(path.as_posix())
        return {"oss_url": f"oss://temporary/{path.name}"}

    def detect_face(self, image_url: str) -> dict:
        assert image_url.startswith("oss://")
        return {"output": {"check_pass": True}}

    def submit(self, image_url: str, audio_url: str, *, resolution: str) -> dict:
        assert resolution == "480P"
        return {"task_id": f"task-{len(self.uploaded)}"}

    def poll(self, task_id: str) -> dict:
        return {"status": "SUCCEEDED", "video_url": "https://example.invalid/video.mp4"}

    def download(self, url: str, target: Path) -> None:
        target.write_bytes(b"fake-video-result")


class FakeRunningHubClient:
    uploads: list[tuple[str, str]] = []

    def upload_file(self, path: Path, *, file_type: str) -> str:
        assert path.is_file()
        self.uploads.append((path.as_posix(), file_type))
        return f"remote-{path.name}"

    def submit(self, *, presenter_filename: str, audio_filename: str, exact_total_frames: int) -> dict:
        assert presenter_filename.startswith("remote-")
        assert audio_filename.startswith("remote-")
        assert exact_total_frames > 0
        return {"task_id": "rh-task-001"}

    def poll(self, task_id: str) -> dict:
        assert task_id == "rh-task-001"
        return {"status": "SUCCEEDED", "video_url": "https://example.invalid/runninghub.mp4", "consume_coins": 8}

    def download(self, url: str, target: Path) -> None:
        target.write_bytes(b"fake-runninghub-video")


def test_runninghub_turn_uses_frozen_inputs_and_persists_provider_task(project, tmp_path, monkeypatch):
    monkeypatch.setenv("RUNNINGHUB_API_KEY", "test-key")
    monkeypatch.setenv("RUNNINGHUB_WORKFLOW_ID", "123456")
    monkeypatch.setenv("RUNNINGHUB_WORKFLOW_TEMPLATE", str(Path("config/runninghub/longcat_avatar_api.json").resolve()))
    avatar_mod.initialize_avatar_package(project, {"generation_mode": "runninghub_longcat"})
    upload_presenter(project, tmp_path, "yaya", "#2e4769")
    upload_presenter(project, tmp_path, "mengmeng", "#6b2e4e")
    monkeypatch.setattr(cloud_mod, "_probe_audio", lambda path: {"duration_seconds": 1.0, "codec": "pcm_s16le", "sample_rate": 24000, "channels": 1})
    for turn_id in ("T001", "T002", "T003", "T004"):
        temporary, target = cloud_mod.prepare_driving_audio_upload(project, turn_id, f"{turn_id}.wav")
        with wave.open(str(temporary), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(24_000)
            output.writeframes(b"\x01\x00" * 24_000)
        cloud_mod.finalize_driving_audio_upload(project, turn_id, temporary, target, f"{turn_id}.wav")
    # This test exercises the immutable input/provider-task ledger contract.
    # Keep queue validation isolated from whichever real RunningHub workflow is
    # configured on the developer machine; configuration has dedicated tests.
    monkeypatch.setattr(cloud_mod, "runninghub_configuration", lambda: {
        "configured": True,
        "api_key_configured": True,
        "workflow_id_configured": True,
        "workflow_id": "2094449979141218305",
        "workflow_id_suffix": "218305",
        "workflow_profile": "infinitetalk_448x560_exact_clock_v2",
        "template_sha256": "0" * 64,
        "issues": [],
    })
    cloud_mod.configure_cloud_render_spec(project, {
        "aspect_ratio": "portrait",
        "resolution": "448x560",
        "default_fit_mode": "cover_crop",
    })
    cloud_mod.queue_cloud_turn(project, "T001", purpose="sample")
    monkeypatch.setattr(avatar_mod, "probe_media", fake_video_probe)
    FakeRunningHubClient.uploads = []
    monkeypatch.setattr(cloud_mod, "RunningHubLongCatClient", FakeRunningHubClient)

    completed = cloud_mod.run_cloud_turn(project, "T001", poll_interval=0, poll_timeout=1)

    job = completed["turns"][0]["cloud_job"]
    assert job["status"] == "succeeded"
    assert job["provider_task_id"] == "rh-task-001"
    assert job["result_path"].endswith("T001_YAYA.mp4")
    assert completed["turns"][0]["source"]["original_filename"] == "T001_runninghub.mp4"
    assert [file_type for _path, file_type in FakeRunningHubClient.uploads] == ["image", "audio"]
    assert completed["speaker_bindings"][0]["sample"]["status"] == "awaiting_approval"
    validate_artifact("avatar_source_package", completed)


def test_cloud_package_binds_each_speaker_to_independent_role_presenter_and_audio(project, tmp_path, monkeypatch):
    package = setup_ready_cloud_package(project, tmp_path, monkeypatch)

    validate_artifact("avatar_source_package", package)
    assert package["generation_mode"] == "dashscope_wan_s2v"
    bindings = {item["speaker_id"]: item for item in package["speaker_bindings"]}
    assert set(bindings) == {"yaya", "mengmeng"}
    assert bindings["yaya"]["role"]["name"] == "雅雅"
    assert bindings["mengmeng"]["role"]["name"] == "檬檬"
    assert "/yaya/" in bindings["yaya"]["presenter_shot"]["path"]
    assert "/mengmeng/" in bindings["mengmeng"]["presenter_shot"]["path"]
    assert all(turn["driving_audio"]["duration_seconds"] == 7.86 for turn in package["turns"])
    assert package["cloud"]["status"] == "ready"


def test_cloud_generation_is_ready_without_optional_role_library(project, tmp_path, monkeypatch):
    package = setup_ready_cloud_package_without_role_library(project, tmp_path, monkeypatch)
    bindings = {item["speaker_id"]: item for item in package["speaker_bindings"]}
    assert all("role" not in binding for binding in bindings.values())
    assert all(binding["status"] == "ready" for binding in bindings.values())
    assert package["cloud"]["status"] == "ready"


def test_aspect_preflight_blocks_mismatched_presenter_until_a_reviewable_provider_input_exists(project, tmp_path, monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")
    monkeypatch.setenv("DASHSCOPE_WORKSPACE_ID", "test-workspace")
    avatar_mod.initialize_avatar_package(project, {"generation_mode": "dashscope_wan_s2v"})
    upload_landscape_presenter(project, tmp_path, "yaya", "#2e4769")
    upload_landscape_presenter(project, tmp_path, "mengmeng", "#6b2e4e")
    monkeypatch.setattr(cloud_mod, "_probe_audio", lambda path: {"duration_seconds": 7.86, "codec": "mp3", "sample_rate": 48000, "channels": 1})
    for turn_id in ("T001", "T002", "T003", "T004"):
        temporary, target = cloud_mod.prepare_driving_audio_upload(project, turn_id, f"{turn_id}.mp3")
        temporary.write_bytes(f"fake-audio-{turn_id}".encode("utf-8"))
        cloud_mod.finalize_driving_audio_upload(project, turn_id, temporary, target, f"{turn_id}.mp3")

    package = read_avatar_package(project) or {}
    assert {binding["aspect_fit"]["status"] for binding in package["speaker_bindings"]} == {"needs_choice"}
    with pytest.raises(cloud_mod.AvatarCloudError, match="输出画幅与清晰度"):
        cloud_mod.queue_cloud_samples(project)

    configured = cloud_mod.configure_cloud_render_spec(project, {
        "aspect_ratio": "portrait", "resolution": "720P", "default_fit_mode": "contain_blur",
    })
    bindings = {binding["speaker_id"]: binding for binding in configured["speaker_bindings"]}
    assert all(binding["aspect_fit"]["status"] == "prepared" for binding in bindings.values())
    assert all((project / binding["aspect_fit"]["provider_input"]["path"]).is_file() for binding in bindings.values())
    queued, ids = cloud_mod.queue_cloud_samples(project)
    assert ids == ["T001", "T002"]
    assert queued["cloud"]["resolution"] == "720P"
    assert queued["turns"][0]["binding_snapshot"]["provider_input"]["media"] == {"width": 1080, "height": 1920, "format": "PNG"}
    validate_artifact("avatar_source_package", queued)


def test_render_spec_endpoint_updates_project_canvas_without_provider_call(client, projects_root, tmp_path):
    project = projects_root / "aspect-api"
    (project / "artifacts").mkdir(parents=True)
    write_json(project / "project.json", {"project_id": "aspect-api", "title": "画幅测试", "pipeline_type": "avatar-spokesperson", "render_profile": {"aspect_ratio": "portrait", "width": 1080, "height": 1920, "fps": 25, "audio_sample_rate": 48000}})
    write_json(project / "artifacts" / "script.json", {"sections": [{"turn_id": "T001", "speaker_id": "yaya", "speaker_name": "雅雅", "text": "测试。"}]})
    assert client.post("/api/project/aspect-api/workbench/avatar-package/initialize", json={"generation_mode": "dashscope_wan_s2v"}).status_code == 200
    response = client.post("/api/project/aspect-api/workbench/avatar-package/cloud/render-spec", json={"aspect_ratio": "landscape", "resolution": "720P", "default_fit_mode": "cover_crop"})
    assert response.status_code == 200
    package = response.json()["avatar_package"]
    assert package["settings"]["width"] == 1920 and package["settings"]["height"] == 1080
    assert package["cloud"]["aspect_ratio"] == "landscape"
    assert package["cloud"]["resolution"] == "720P"
    marker = json.loads((project / "project.json").read_text(encoding="utf-8"))
    assert marker["render_profile"]["aspect_ratio"] == "landscape"


def test_refresh_reconciliation_never_writes_over_an_active_cloud_worker(project, tmp_path, monkeypatch):
    package = setup_ready_cloud_package_without_role_library(project, tmp_path, monkeypatch)
    cloud_mod.queue_cloud_turn(project, "T001", purpose="sample")
    queued = read_avatar_package(project) or {}
    revision = queued["revision"]
    reconciled = cloud_mod.reconcile_cloud_avatar_package(project) or {}
    assert reconciled["revision"] == revision
    saved = read_avatar_package(project) or {}
    assert saved["revision"] == revision
    assert saved["turns"][0]["cloud_job"]["status"] == "queued"
    assert "缺少角色" not in package["cloud"]["message"]


def test_optional_role_selection_does_not_change_generation_hash_or_invalidate_sample(project, tmp_path, monkeypatch):
    package = setup_ready_cloud_package_without_role_library(project, tmp_path, monkeypatch)
    before_hash = cloud_mod._input_hash(package, package["turns"][0])
    package["speaker_bindings"][0]["sample"] = {
        "status": "approved", "turn_id": "T001", "input_hash": before_hash, "approved": True,
    }
    avatar_mod._save_package(project, package)
    role = create_role(tmp_path, "yaya", "雅雅")

    updated = cloud_mod.select_cloud_avatar_role(project, "yaya", role["role_id"])
    assert updated["speaker_bindings"][0]["sample"]["approved"] is True
    assert cloud_mod._input_hash(updated, updated["turns"][0]) == before_hash

    unlinked = cloud_mod.select_cloud_avatar_role(project, "yaya", "")
    assert "role" not in unlinked["speaker_bindings"][0]
    assert unlinked["speaker_bindings"][0]["sample"]["approved"] is True


def test_reconcile_migrates_legacy_role_hash_without_rebilling_completed_clip(project, tmp_path, monkeypatch):
    package = setup_ready_cloud_package(project, tmp_path, monkeypatch)
    snapshot = cloud_mod._binding_snapshot(package, package["turns"][0])
    legacy = "|".join([
        snapshot["presenter_shot"]["sha256"], snapshot["driving_audio"]["sha256"],
        snapshot["role"]["role_id"], str(snapshot["role"]["version"]),
        package["cloud"]["model"], package["cloud"]["resolution"], package["turns"][0]["text"],
    ])
    legacy_hash = cloud_mod.hashlib.sha256(legacy.encode("utf-8")).hexdigest()
    package["turns"][0]["binding_snapshot"] = snapshot
    package["turns"][0]["source"] = {
        "path": "assets/incoming/avatar/yaya/T001_YAYA.mp4", "original_filename": "T001.mp4",
        "sha256": "a" * 64, "size_bytes": 10, "uploaded_at": cloud_mod._now(),
        "media": fake_video_probe(project / package["turns"][0]["driving_audio"]["path"]),
    }
    package["turns"][0]["cloud_job"] = {
        "job_id": "AVJ-legacy12345678", "status": "succeeded", "stage": "已完成",
        "input_hash": legacy_hash, "binding_hash": "b" * 64, "purpose": "sample", "attempt": 1,
        "provider_task_id": "legacy-provider-task", "provider_status": "SUCCEEDED",
        "requested_at": cloud_mod._now(), "finished_at": cloud_mod._now(),
    }
    avatar_mod._save_package(project, package)

    migrated = cloud_mod.reconcile_cloud_avatar_package(project) or {}
    assert migrated["turns"][0]["cloud_job"]["input_hash"] == cloud_mod._input_hash(migrated, migrated["turns"][0])
    assert migrated["turns"][0]["cloud_job"]["input_hash"] != legacy_hash


def test_stale_package_save_is_rejected_instead_of_overwriting_newer_state(project):
    avatar_mod.initialize_avatar_package(project, {"generation_mode": "dashscope_wan_s2v"})
    first = read_avatar_package(project) or {}
    stale = json.loads(json.dumps(first))
    first["cloud"]["message"] = "较新的状态"
    avatar_mod._save_package(project, first)
    stale["cloud"]["message"] = "过期状态"
    with pytest.raises(avatar_mod.AvatarImportError, match="旧状态覆盖新结果"):
        avatar_mod._save_package(project, stale)


def test_voicebox_take_is_auditioned_before_becoming_cloud_driving_audio(project, monkeypatch):
    """Local TTS stays a candidate; only an explicit adoption changes cloud input."""
    avatar_mod.initialize_avatar_package(project, {"generation_mode": "dashscope_wan_s2v"})
    monkeypatch.setattr(cloud_mod, "_probe_audio", lambda _path: {
        "duration_seconds": 4.25, "codec": "pcm_s16le", "sample_rate": 48000, "channels": 1,
    })
    monkeypatch.setattr(cloud_mod, "read_audio_center", lambda: {
        "provider": {"status": "available"},
        "default_voice": {"id": "voice-yaya", "name": "雅雅"},
        "profiles": [{"id": "voice-yaya", "name": "雅雅"}, {"id": "voice-mengmeng", "name": "檬檬"}],
    })
    monkeypatch.setattr(cloud_mod.VoiceboxTTS, "get_status", classmethod(lambda cls: ToolStatus.AVAILABLE))

    def fake_voicebox(_self, payload):
        output = Path(payload["output_path"])
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"RIFFfake-voicebox-take")
        return SimpleNamespace(success=True, error=None)

    monkeypatch.setattr(cloud_mod.VoiceboxTTS, "execute", fake_voicebox)

    queued = cloud_mod.start_voicebox_driving_audio_candidate(project, "T001", {"profile_id": "voice-yaya"})
    assert queued["turns"][0]["driving_audio_job"]["status"] == "generating"
    assert "driving_audio" not in queued["turns"][0]

    ready = cloud_mod.generate_voicebox_driving_audio_candidate(project, "T001")
    turn = ready["turns"][0]
    candidate = turn["driving_audio_candidates"][0]
    assert turn["driving_audio_job"]["status"] == "completed"
    assert candidate["state"] == "candidate"
    assert candidate["source_type"] == "voicebox_generated"
    assert candidate["profile_name"] == "雅雅"
    assert candidate["duration_seconds"] == 4.25
    assert "driving_audio" not in turn
    validate_artifact("avatar_source_package", ready)

    adopted = cloud_mod.apply_voicebox_driving_audio_candidate(project, "T001", candidate["id"])
    applied_turn = adopted["turns"][0]
    assert applied_turn["driving_audio"]["id"] == candidate["id"]
    assert applied_turn["driving_audio"]["state"] == "current"
    assert applied_turn["status"] == "audio_ready"
    assert (project / applied_turn["driving_audio"]["path"]).is_file()
    validate_artifact("avatar_source_package", adopted)


def test_doubao_take_uses_frozen_cloud_profile_without_local_fallback(project, monkeypatch):
    avatar_mod.initialize_avatar_package(project, {"generation_mode": "dashscope_wan_s2v"})
    monkeypatch.setattr(cloud_mod, "_probe_audio", lambda _path: {
        "duration_seconds": 3.25, "codec": "pcm_s16le", "sample_rate": 24000, "channels": 1,
    })
    cloud_profile = {
        "id": "doubao:yaya",
        "name": "雅雅",
        "provider_id": "doubao",
        "provider_name": "豆包云端配音",
        "provider_voice_id": "cloud-yaya",
        "available": True,
    }
    monkeypatch.setattr(cloud_mod, "read_audio_center", lambda: {
        "provider": {"status": "available"},
        "default_voice": cloud_profile,
        "profiles": [cloud_profile],
    })
    monkeypatch.setattr(cloud_mod, "get_voice_profile", lambda profile_id: cloud_profile if profile_id == "doubao:yaya" else None)
    calls = []

    def fake_generate_voice_audio(**kwargs):
        calls.append(kwargs)
        output = Path(kwargs["output_path"])
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"RIFFcloud")
        return SimpleNamespace(success=True, error=None, data={"metadata_path": "timing.json"})

    monkeypatch.setattr(cloud_mod, "generate_voice_audio", fake_generate_voice_audio)

    queued = cloud_mod.start_voicebox_driving_audio_candidate(project, "T001", {"profile_id": "doubao:yaya"})
    assert queued["turns"][0]["driving_audio_job"]["provider_id"] == "doubao"
    ready = cloud_mod.generate_voicebox_driving_audio_candidate(project, "T001")
    candidate = ready["turns"][0]["driving_audio_candidates"][0]

    assert calls[0]["profile"]["provider_voice_id"] == "cloud-yaya"
    assert candidate["source_type"] == "cloud_tts_generated"
    assert candidate["voice_provider_id"] == "doubao"
    validate_artifact("avatar_source_package", ready)


def _voicebox_catalog() -> dict:
    return {
        "provider": {"status": "available"},
        "default_voice": {"id": "voice-yaya", "name": "雅雅"},
        "profiles": [
            {"id": "voice-yaya", "name": "雅雅"},
            {"id": "voice-mengmeng", "name": "檬檬"},
            {"id": "voice-narrator", "name": "旁白"},
        ],
    }


def _prepare_fake_voicebox(monkeypatch, calls: list[tuple[str, str]]) -> None:
    monkeypatch.setattr(cloud_mod, "_probe_audio", lambda _path: {
        "duration_seconds": 3.5, "codec": "pcm_s16le", "sample_rate": 48000, "channels": 1,
    })
    monkeypatch.setattr(cloud_mod, "read_audio_center", _voicebox_catalog)
    monkeypatch.setattr(cloud_mod.VoiceboxTTS, "get_status", classmethod(lambda cls: ToolStatus.AVAILABLE))

    def fake_voicebox(_self, payload):
        calls.append((str(payload["profile_id"]), str(payload["text"])))
        output = Path(payload["output_path"])
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"RIFFfake-voicebox-batch")
        return SimpleNamespace(success=True, error=None)

    monkeypatch.setattr(cloud_mod.VoiceboxTTS, "execute", fake_voicebox)


def test_voicebox_same_name_routing_wins_over_default_and_single_turn_uses_it(project, monkeypatch):
    avatar_mod.initialize_avatar_package(project, {"generation_mode": "dashscope_wan_s2v"})
    calls: list[tuple[str, str]] = []
    _prepare_fake_voicebox(monkeypatch, calls)

    refreshed = cloud_mod.refresh_voicebox_speaker_mappings(project)
    mappings = {item["speaker_id"]: item for item in refreshed["voicebox"]["speaker_mappings"]}
    assert mappings["yaya"]["profile_id"] == "voice-yaya"
    assert mappings["yaya"]["selection_source"] == "same_name"
    assert mappings["mengmeng"]["profile_id"] == "voice-mengmeng"
    assert mappings["mengmeng"]["selection_source"] == "same_name"

    # The omitted profile means “use this speaker's persisted route”, not the
    # global default.  This guards against all dialogue being voiced by 雅雅.
    queued = cloud_mod.start_voicebox_driving_audio_candidate(project, "T002")
    assert queued["turns"][1]["driving_audio_job"]["profile_id"] == "voice-mengmeng"
    assert queued["turns"][1]["driving_audio_job"]["voice_selection_source"] == "same_name"
    validate_artifact("avatar_source_package", queued)


def test_voicebox_batch_is_script_order_serial_and_preserves_current_audio_in_candidate_mode(project, monkeypatch):
    avatar_mod.initialize_avatar_package(project, {"generation_mode": "dashscope_wan_s2v"})
    calls: list[tuple[str, str]] = []
    _prepare_fake_voicebox(monkeypatch, calls)

    planned = cloud_mod.start_voicebox_driving_audio_batch(project, {"mode": "missing_and_apply"})
    batch_id = planned["voicebox"]["batch"]["batch_id"]
    completed = cloud_mod.run_voicebox_driving_audio_batch(project, batch_id)
    batch = completed["voicebox"]["batch"]
    assert batch["status"] == "completed"
    assert [item["turn_id"] for item in batch["items"]] == ["T001", "T002", "T003", "T004"]
    assert [profile_id for profile_id, _text in calls] == ["voice-yaya", "voice-mengmeng", "voice-yaya", "voice-mengmeng"]
    assert [item["outcome"] for item in batch["items"]] == ["adopted", "adopted", "adopted", "adopted"]
    assert all(turn["driving_audio"]["state"] == "current" for turn in completed["turns"])
    first_take = completed["turns"][0]["driving_audio"]["id"]

    candidate_plan = cloud_mod.start_voicebox_driving_audio_batch(project, {"mode": "all_candidates"})
    candidate_done = cloud_mod.run_voicebox_driving_audio_batch(project, candidate_plan["voicebox"]["batch"]["batch_id"])
    candidate_batch = candidate_done["voicebox"]["batch"]
    assert candidate_batch["status"] == "completed"
    assert all(item["outcome"] == "candidate" for item in candidate_batch["items"])
    assert candidate_done["turns"][0]["driving_audio"]["id"] == first_take
    assert candidate_done["turns"][0]["driving_audio_candidates"][-1]["state"] == "candidate"
    validate_artifact("avatar_source_package", candidate_done)


def test_duplicate_same_name_voicebox_profiles_require_a_manual_choice(project, monkeypatch):
    avatar_mod.initialize_avatar_package(project, {"generation_mode": "dashscope_wan_s2v"})
    calls: list[tuple[str, str]] = []
    _prepare_fake_voicebox(monkeypatch, calls)
    duplicate = _voicebox_catalog()
    duplicate["profiles"].append({"id": "voice-mengmeng-v2", "name": "檬檬"})
    monkeypatch.setattr(cloud_mod, "read_audio_center", lambda: duplicate)

    refreshed = cloud_mod.refresh_voicebox_speaker_mappings(project)
    mengmeng = next(item for item in refreshed["voicebox"]["speaker_mappings"] if item["speaker_id"] == "mengmeng")
    assert mengmeng["status"] == "needs_attention"
    with pytest.raises(cloud_mod.AvatarCloudError, match="檬檬"):
        cloud_mod.start_voicebox_driving_audio_batch(project, {"mode": "missing_and_apply"})

    saved = cloud_mod.set_voicebox_speaker_mapping(project, "mengmeng", {"profile_id": "voice-mengmeng-v2"})
    mapping = next(item for item in saved["voicebox"]["speaker_mappings"] if item["speaker_id"] == "mengmeng")
    assert mapping["selection_source"] == "manual"
    assert mapping["profile_id"] == "voice-mengmeng-v2"


def test_voicebox_mapping_and_batch_http_routes_return_project_workbench(client, projects_root, monkeypatch):
    project = projects_root / "avatar-voicebox-http"
    (project / "artifacts").mkdir(parents=True)
    write_json(project / "project.json", {"project_id": "avatar-voicebox-http", "title": "配音接口", "pipeline_type": "avatar-spokesperson"})
    write_json(project / "artifacts" / "script.json", {"sections": [
        {"turn_id": "T001", "speaker_id": "yaya", "speaker_name": "雅雅", "text": "第一段。"},
        {"turn_id": "T002", "speaker_id": "mengmeng", "speaker_name": "檬檬", "text": "第二段。"},
    ]})
    assert client.post("/api/project/avatar-voicebox-http/workbench/avatar-package/initialize", json={"generation_mode": "dashscope_wan_s2v"}).status_code == 200
    monkeypatch.setattr(cloud_mod, "read_audio_center", _voicebox_catalog)
    monkeypatch.setattr(cloud_mod.VoiceboxTTS, "get_status", classmethod(lambda cls: ToolStatus.AVAILABLE))
    monkeypatch.setattr(cloud_mod, "_probe_audio", lambda _path: {"duration_seconds": 3.5, "codec": "pcm_s16le"})

    def fake_voicebox(_self, payload):
        output = Path(payload["output_path"])
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"RIFFfake-http-batch")
        return SimpleNamespace(success=True, error=None)

    monkeypatch.setattr(cloud_mod.VoiceboxTTS, "execute", fake_voicebox)

    refreshed = client.post("/api/project/avatar-voicebox-http/workbench/avatar-package/voicebox/mappings/refresh")
    assert refreshed.status_code == 200
    assert refreshed.json()["avatar_package"]["voicebox"]["speaker_mappings"][1]["profile_name"] == "檬檬"
    started = client.post("/api/project/avatar-voicebox-http/workbench/avatar-package/voicebox/batch/jobs", json={"mode": "missing_and_apply"})
    assert started.status_code == 200
    assert started.json()["avatar_package"]["voicebox"]["batch"]["mode"] == "missing_and_apply"


def test_voicebox_candidate_cannot_replace_audio_while_cloud_turn_is_running(project, tmp_path, monkeypatch):
    setup_ready_cloud_package(project, tmp_path, monkeypatch)
    monkeypatch.setattr(cloud_mod, "read_audio_center", lambda: {
        "provider": {"status": "available"}, "default_voice": {"id": "voice-yaya", "name": "雅雅"},
        "profiles": [{"id": "voice-yaya", "name": "雅雅"}],
    })
    monkeypatch.setattr(cloud_mod.VoiceboxTTS, "get_status", classmethod(lambda cls: ToolStatus.AVAILABLE))

    def fake_voicebox(_self, payload):
        output = Path(payload["output_path"])
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"RIFFcandidate")
        return SimpleNamespace(success=True, error=None)

    monkeypatch.setattr(cloud_mod.VoiceboxTTS, "execute", fake_voicebox)
    cloud_mod.start_voicebox_driving_audio_candidate(project, "T001", {"profile_id": "voice-yaya"})
    candidate = cloud_mod.generate_voicebox_driving_audio_candidate(project, "T001")["turns"][0]["driving_audio_candidates"][-1]
    cloud_mod.queue_cloud_turn(project, "T001", purpose="sample")

    with pytest.raises(cloud_mod.AvatarCloudError, match="正在生成云端数字人"):
        cloud_mod.apply_voicebox_driving_audio_candidate(project, "T001", candidate["id"])


def test_cloud_batch_requires_an_approved_sample_for_every_speaker(project, tmp_path, monkeypatch):
    setup_ready_cloud_package(project, tmp_path, monkeypatch)
    _package, turn_ids = cloud_mod.queue_cloud_samples(project)
    assert turn_ids == ["T001", "T002"]

    with pytest.raises(cloud_mod.AvatarCloudError, match="确认.*试片"):
        cloud_mod.queue_cloud_batch(project)


def test_cloud_samples_then_batch_use_the_correct_speaker_snapshot_and_keep_script_order(project, tmp_path, monkeypatch):
    setup_ready_cloud_package(project, tmp_path, monkeypatch)
    _package, sample_turn_ids = cloud_mod.queue_cloud_samples(project)
    monkeypatch.setattr(avatar_mod, "probe_media", fake_video_probe)
    FakeClient.uploaded = []
    monkeypatch.setattr(cloud_mod, "DashscopeWanS2VClient", FakeClient)

    sampled = cloud_mod.run_cloud_batch(project, sample_turn_ids)
    bindings = {item["speaker_id"]: item for item in sampled["speaker_bindings"]}
    assert bindings["yaya"]["sample"]["status"] == "awaiting_approval"
    assert bindings["mengmeng"]["sample"]["status"] == "awaiting_approval"
    assert "/yaya/" in sampled["turns"][0]["binding_snapshot"]["presenter_shot"]["path"]
    assert "/mengmeng/" in sampled["turns"][1]["binding_snapshot"]["presenter_shot"]["path"]

    cloud_mod.approve_cloud_sample(project, "yaya")
    with pytest.raises(cloud_mod.AvatarCloudError, match="檬檬"):
        cloud_mod.queue_cloud_batch(project)
    cloud_mod.approve_cloud_sample(project, "mengmeng")
    _package, remaining = cloud_mod.queue_cloud_batch(project)
    assert remaining == ["T003", "T004"]
    completed = cloud_mod.run_cloud_batch(project, remaining)

    assert [turn["turn_id"] for turn in completed["turns"]] == ["T001", "T002", "T003", "T004"]
    assert all((turn.get("cloud_job") or {}).get("status") == "succeeded" for turn in completed["turns"])
    assert completed["cloud"]["status"] == "completed"
    assert "/yaya/" in completed["turns"][2]["binding_snapshot"]["presenter_shot"]["path"]
    assert "/mengmeng/" in completed["turns"][3]["binding_snapshot"]["presenter_shot"]["path"]
    validate_artifact("avatar_source_package", completed)


def test_replacing_one_speaker_binding_preserves_other_speaker_files_and_invalidates_only_its_sample(project, tmp_path, monkeypatch):
    package = setup_ready_cloud_package(project, tmp_path, monkeypatch)
    old_yaya = next(item for item in package["speaker_bindings"] if item["speaker_id"] == "yaya")["presenter_shot"]
    mengmeng_sample = next(item for item in package["speaker_bindings"] if item["speaker_id"] == "mengmeng")["sample"]
    upload_presenter(project, tmp_path, "yaya", "#1a2b3c")
    updated = read_avatar_package(project) or {}
    bindings = {item["speaker_id"]: item for item in updated["speaker_bindings"]}
    assert (project / old_yaya["path"]).is_file()
    assert bindings["yaya"]["sample"]["status"] == "not_started"
    assert bindings["mengmeng"]["sample"] == mengmeng_sample
    assert bindings["yaya"]["presenter_shot"]["sha256"] != old_yaya["sha256"]


def test_failed_turn_cancels_unsubmitted_following_turns_instead_of_leaving_them_stuck(project, tmp_path, monkeypatch):
    setup_ready_cloud_package(project, tmp_path, monkeypatch)
    _package, turn_ids = cloud_mod.queue_cloud_samples(project)

    class FailingClient(FakeClient):
        def poll(self, task_id: str) -> dict:
            return {"status": "FAILED"}

    monkeypatch.setattr(cloud_mod, "DashscopeWanS2VClient", FailingClient)
    result = cloud_mod.run_cloud_batch(project, turn_ids)
    jobs = {turn["turn_id"]: turn.get("cloud_job") or {} for turn in result["turns"]}
    assert jobs["T001"]["status"] == "failed"
    assert jobs["T002"]["status"] == "cancelled"
    assert result["cloud"]["status"] == "failed"


def test_saved_provider_task_can_resume_without_requeuing(project, tmp_path, monkeypatch):
    setup_ready_cloud_package(project, tmp_path, monkeypatch)
    cloud_mod.queue_cloud_turn(project, "T001", purpose="sample")
    package = read_avatar_package(project) or {}
    package["turns"][0]["cloud_job"].update({"status": "running", "stage": "阿里云正在生成", "provider_task_id": "task-resume"})
    avatar_mod._save_package(project, package)

    resumed = cloud_mod.assert_cloud_turn_resumable(project, "T001")
    assert resumed["turns"][0]["cloud_job"]["provider_task_id"] == "task-resume"


def test_cloud_poll_window_expiry_with_provider_id_stays_resumable(project, tmp_path, monkeypatch):
    setup_ready_cloud_package(project, tmp_path, monkeypatch)
    cloud_mod.queue_cloud_turn(project, "T001", purpose="sample")

    class StillRunningClient(FakeClient):
        def poll(self, task_id: str) -> dict:
            return {"status": "RUNNING"}

    monkeypatch.setattr(cloud_mod, "DashscopeWanS2VClient", StillRunningClient)
    result = cloud_mod.run_cloud_turn(project, "T001", poll_interval=0, poll_timeout=0.001)
    job = result["turns"][0]["cloud_job"]

    assert job["status"] == "running"
    assert str(job["provider_task_id"]).startswith("task-")
    assert job["provider_status"] == "RUNNING"
    assert job["error"] == ""


def test_legacy_local_timeout_failure_resumes_the_same_provider_task(project, tmp_path, monkeypatch):
    setup_ready_cloud_package(project, tmp_path, monkeypatch)
    cloud_mod.queue_cloud_turn(project, "T001", purpose="sample")
    package = read_avatar_package(project) or {}
    package["turns"][0]["cloud_job"].update({
        "status": "failed",
        "stage": "失败",
        "provider_task_id": "task-already-paid",
        "provider_status": "RUNNING",
        "finished_at": "2026-08-12T07:15:31Z",
        "error": "等待阿里云任务超时；任务编号已保存，可稍后继续跟踪",
    })
    package["turns"][0]["status"] = "cloud_failed"
    package["speaker_bindings"][0]["sample"] = {
        "status": "failed", "turn_id": "T001", "input_hash": package["turns"][0]["cloud_job"]["input_hash"],
        "approved": False, "error": "本地轮询超时",
    }
    avatar_mod._save_package(project, package)

    resumed = cloud_mod.assert_cloud_turn_resumable(project, "T001")

    job = resumed["turns"][0]["cloud_job"]
    assert job["status"] == "running"
    assert job["provider_task_id"] == "task-already-paid"
    assert job["error"] == ""
    assert "finished_at" not in job
    assert resumed["speaker_bindings"][0]["sample"]["status"] == "queued"


def test_active_resumed_provider_task_clears_legacy_finished_timestamp(project, tmp_path, monkeypatch):
    setup_ready_cloud_package(project, tmp_path, monkeypatch)
    cloud_mod.queue_cloud_turn(project, "T001", purpose="sample")
    package = read_avatar_package(project) or {}
    package["turns"][0]["cloud_job"].update({
        "status": "running", "provider_task_id": "task-still-running",
        "provider_status": "RUNNING", "finished_at": "2026-08-12T07:15:31Z",
    })
    avatar_mod._save_package(project, package)

    resumed = cloud_mod.assert_cloud_turn_resumable(project, "T001")

    assert "finished_at" not in resumed["turns"][0]["cloud_job"]


def test_cloud_batch_keeps_polling_the_same_provider_task_after_a_local_window(project, tmp_path, monkeypatch):
    setup_ready_cloud_package(project, tmp_path, monkeypatch)
    cloud_mod.queue_cloud_turn(project, "T001", purpose="sample")
    calls: list[str] = []

    def fake_run(_project: Path, turn_id: str) -> dict:
        calls.append(turn_id)
        package = read_avatar_package(project) or {}
        job = package["turns"][0]["cloud_job"]
        if len(calls) == 1:
            job.update({"status": "running", "provider_task_id": "task-already-paid", "provider_status": "RUNNING"})
        else:
            job.update({"status": "succeeded", "provider_task_id": "task-already-paid", "provider_status": "SUCCEEDED"})
        return avatar_mod._save_package(project, package)

    monkeypatch.setattr(cloud_mod, "run_cloud_turn", fake_run)
    result = cloud_mod.run_cloud_batch(project, ["T001"])

    assert calls == ["T001", "T001"]
    assert result["turns"][0]["cloud_job"]["status"] == "succeeded"


def test_restart_recovery_resumes_safe_jobs_and_blocks_ambiguous_resubmission(project, tmp_path, monkeypatch):
    setup_ready_cloud_package_without_role_library(project, tmp_path, monkeypatch)
    cloud_mod.queue_cloud_turn(project, "T001", purpose="sample")
    cloud_mod.queue_cloud_turn(project, "T002", purpose="sample")
    package = read_avatar_package(project) or {}
    package["turns"][0]["cloud_job"].update({
        "status": "running", "stage": "阿里云正在生成", "provider_task_id": "task-safe-resume",
    })
    package["turns"][1]["cloud_job"].update({
        "status": "uploading", "stage": "上传中", "provider_task_id": None,
    })
    avatar_mod._save_package(project, package)

    recovery = cloud_mod.recover_interrupted_avatar_jobs(project)
    assert recovery["cloud_turn_ids"] == ["T001"]
    saved = read_avatar_package(project) or {}
    assert saved["turns"][0]["cloud_job"]["status"] == "running"
    assert saved["turns"][1]["cloud_job"]["status"] == "failed"
    assert "避免重复计费" in saved["turns"][1]["cloud_job"]["error"]


def test_cloud_routes_reject_generation_without_configured_provider(client, projects_root, tmp_path, monkeypatch):
    from backlot import avatar_roles as api_roles

    role_root = tmp_path / "api_roles"
    monkeypatch.setattr(api_roles, "ROLE_DIRECTORY", role_root)
    monkeypatch.setattr(api_roles, "ROLE_FILE", role_root / "roles.json")
    monkeypatch.setattr(api_roles, "ROLE_ASSET_DIRECTORY", role_root / "assets")
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    monkeypatch.delenv("DASHSCOPE_WORKSPACE_ID", raising=False)
    project = projects_root / "avatar-show"
    (project / "artifacts").mkdir(parents=True)
    write_json(project / "project.json", {"project_id": "avatar-show", "title": "接口数字人", "pipeline_type": "avatar-spokesperson"})
    write_json(project / "artifacts" / "script.json", {"sections": [{"turn_id": "T001", "speaker_id": "yaya", "speaker_name": "雅雅", "text": "测试。"}]})
    response = client.post("/api/project/avatar-show/workbench/avatar-package/initialize", json={"generation_mode": "dashscope_wan_s2v"})
    assert response.status_code == 200
    assert response.json()["avatar_package"]["speaker_bindings"][0]["speaker_id"] == "yaya"
    response = client.post("/api/project/avatar-show/workbench/avatar-package/cloud/sample/jobs", json={})
    assert response.status_code == 422
    assert "DASHSCOPE_API_KEY" in response.json()["detail"]


def test_runninghub_paid_route_requires_explicit_confirmation(client, projects_root, monkeypatch):
    project = projects_root / "runninghub-paid-gate"
    (project / "artifacts").mkdir(parents=True)
    write_json(project / "project.json", {"project_id": "runninghub-paid-gate", "title": "积分确认", "pipeline_type": "avatar-spokesperson"})
    write_json(project / "artifacts" / "script.json", {"sections": [{"turn_id": "T001", "speaker_id": "yaya", "speaker_name": "雅雅", "text": "测试。"}]})
    response = client.post("/api/project/runninghub-paid-gate/workbench/avatar-package/initialize", json={"generation_mode": "runninghub_longcat"})
    assert response.status_code == 200

    response = client.post("/api/project/runninghub-paid-gate/workbench/avatar-package/cloud/sample/jobs", json={})

    assert response.status_code == 422
    assert "消耗积分" in response.json()["detail"]
