"""Server/API tests for Backlot.

These cover the deterministic eval surface in internal/evals/BACKLOT_EVAL_PLAN.md:
API shape, path safety, media/thumb serving, range requests, and loose
performance budgets.
"""

from __future__ import annotations

import io
import json
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from backlot import server as server_mod
from backlot import state as state_mod


@pytest.fixture
def projects_root(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    root.mkdir()
    monkeypatch.setattr(state_mod, "PROJECTS_DIR", root)
    monkeypatch.setattr(server_mod, "PROJECTS_DIR", root)
    monkeypatch.setattr(server_mod, "_summary_cache", {})
    monkeypatch.setattr(server_mod, "_PROJECTS_ROOT_STR", __import__("os").path.normcase(str(root.resolve())))
    monkeypatch.setattr(server_mod, "THUMB_CACHE_DIR", tmp_path / "thumbs")
    return root


@pytest.fixture
def client(projects_root, monkeypatch):
    async def no_watch():
        return None

    monkeypatch.setattr(server_mod, "_watch_projects", no_watch)
    with TestClient(server_mod.create_app()) as c:
        yield c


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _make_project(root: Path, project_id: str = "film") -> Path:
    project = root / project_id
    (project / "artifacts").mkdir(parents=True)
    (project / "assets" / "images").mkdir(parents=True)
    (project / "assets" / "video").mkdir(parents=True)
    (project / "renders").mkdir(parents=True)
    _write_json(
        project / "project.json",
        {
            "project_id": project_id,
            "title": "Film",
            "pipeline_type": "cinematic",
            "created_at": "2026-07-02T00:00:00Z",
        },
    )
    _write_json(
        project / "checkpoint_script.json",
        {
            "version": "1.0",
            "project_id": project_id,
            "pipeline_type": "cinematic",
            "stage": "script",
            "status": "awaiting_human",
            "timestamp": "2026-07-02T00:01:00Z",
            "artifacts": {},
        },
    )
    return project


def _write_png(path: Path, color: tuple[int, int, int] = (200, 40, 80)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (24, 16), color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    path.write_bytes(buf.getvalue())


class TestBacklotServerApi:
    def test_health(self, client):
        response = client.get("/api/health")
        assert response.status_code == 200
        assert response.json() == {"ok": True, "app": "backlot"}

    def test_material_vision_details_endpoint_is_project_scoped(self, client, projects_root, monkeypatch):
        project = _make_project(projects_root, "vision-api")
        calls = []

        def fake_read(current_project: Path, asset_id: str, *, limit: int):
            calls.append((current_project, asset_id, limit))
            return {"asset_id": asset_id, "status": "completed", "shot_count": 1, "shots": []}

        monkeypatch.setattr(server_mod, "read_asset_material_vision", fake_read)
        response = client.get("/api/project/vision-api/workbench/assets/S-001/media-index/vision?limit=12")

        assert response.status_code == 200
        assert response.json()["shot_count"] == 1
        assert calls == [(project, "S-001", 12)]

    def test_narration_gain_api_is_independent_from_music(self, client, projects_root, monkeypatch):
        _make_project(projects_root, "voice-level")
        monkeypatch.setattr(
            server_mod,
            "update_narration_policy",
            lambda project, payload: {
                "project": {"id": project.name},
                "narration_policy": {"playback_gain_db": float(payload["playback_gain_db"])},
                "music_policy": {"playback_gain_db": -8.0},
            },
        )

        response = client.put(
            "/api/project/voice-level/workbench/narration-policy",
            json={"playback_gain_db": 3.5},
        )

        assert response.status_code == 200
        assert response.json()["narration_policy"]["playback_gain_db"] == 3.5
        assert response.json()["music_policy"]["playback_gain_db"] == -8.0

    def test_project_music_upload_streams_into_current_project(self, client, projects_root, monkeypatch):
        project = _make_project(projects_root, "local-bgm")

        def fake_prepare(current_project: Path, filename: str) -> Path:
            assert current_project == project
            assert filename == "我的音乐.wav"
            temporary = project / "assets" / "audio" / "music" / "uploads" / ".incoming-test.wav"
            temporary.parent.mkdir(parents=True, exist_ok=True)
            temporary.touch()
            return temporary

        def fake_complete(current_project: Path, temporary: Path, filename: str):
            assert current_project == project
            assert filename == "我的音乐.wav"
            assert temporary.read_bytes() == b"local-music-bytes"
            track = {
                "id": "project-music-demo",
                "title": "我的音乐",
                "filename": "project-music-demo.wav",
                "scope": "project",
                "media_url": "music/project-tracks/project-music-demo",
                "duration_seconds": 12.0,
            }
            return temporary, track

        monkeypatch.setattr(server_mod, "prepare_project_music_upload", fake_prepare)
        monkeypatch.setattr(server_mod, "complete_project_music_upload", fake_complete)
        monkeypatch.setattr(server_mod, "read_music_catalog", lambda _project: {
            "tracks": [{"id": "project-music-demo", "scope": "project"}],
        })

        response = client.post(
            "/api/project/local-bgm/workbench/music/uploads?filename=%E6%88%91%E7%9A%84%E9%9F%B3%E4%B9%90.wav",
            content=b"local-music-bytes",
            headers={"Content-Type": "application/octet-stream"},
        )

        assert response.status_code == 200, response.text
        assert response.json()["track"]["id"] == "project-music-demo"
        assert response.json()["catalog"]["tracks"][0]["scope"] == "project"

    def test_project_music_upload_rejects_oversized_stream_before_finalize(self, client, projects_root, monkeypatch):
        project = _make_project(projects_root, "large-bgm")
        temporary = project / "assets" / "audio" / "music" / "uploads" / ".incoming-large.wav"
        temporary.parent.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(server_mod, "MAX_PROJECT_MUSIC_BYTES", 4)
        monkeypatch.setattr(
            server_mod,
            "prepare_project_music_upload",
            lambda _project, _filename: (temporary.touch() or temporary),
        )
        finalized: list[bool] = []
        monkeypatch.setattr(
            server_mod,
            "complete_project_music_upload",
            lambda *_args, **_kwargs: finalized.append(True),
        )

        response = client.post(
            "/api/project/large-bgm/workbench/music/uploads?filename=large.wav",
            content=b"12345",
            headers={"Content-Type": "application/octet-stream"},
        )

        assert response.status_code == 413
        assert finalized == []
        assert not temporary.exists()

    def test_project_music_media_resolution_is_project_scoped(self, client, projects_root, monkeypatch):
        project = _make_project(projects_root, "scoped-bgm")
        audio = project / "assets" / "audio" / "music" / "uploads" / "track.wav"
        audio.parent.mkdir(parents=True, exist_ok=True)
        audio.write_bytes(b"wave")
        calls: list[tuple[Path, str]] = []

        def fake_resolve(current_project: Path, track_id: str) -> Path:
            calls.append((current_project, track_id))
            return audio

        monkeypatch.setattr(server_mod, "music_track_path", fake_resolve)

        response = client.get(
            "/api/project/scoped-bgm/workbench/music/project-tracks/project-music-demo"
        )

        assert response.status_code == 200
        assert calls == [(project, "project-music-demo")]

    @pytest.mark.skipif(
        not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
        reason="ffmpeg/ffprobe required",
    )
    def test_project_music_upload_real_audio_round_trip(self, client, projects_root, tmp_path):
        """Real HTTP body -> FFprobe validation -> project media response."""
        project = _make_project(projects_root, "real-local-bgm")
        source = tmp_path / "two-tone.wav"
        subprocess.run(
            [
                "ffmpeg", "-y", "-f", "lavfi", "-i",
                "sine=frequency=740:duration=1.2:sample_rate=48000", str(source),
            ],
            check=True,
            capture_output=True,
            timeout=30,
        )

        upload = client.post(
            "/api/project/real-local-bgm/workbench/music/uploads?filename=local-tone.wav",
            content=source.read_bytes(),
            headers={"Content-Type": "application/octet-stream"},
        )

        assert upload.status_code == 200, upload.text
        track = upload.json()["track"]
        assert track["scope"] == "project"
        assert track["duration_seconds"] >= 1.0
        assert track["id"].startswith("project-music-")
        assert str(project.resolve()) not in upload.text
        stored = project / "assets" / "audio" / "music" / "uploads" / track["filename"]
        assert stored.is_file()

        media = client.get(
            f"/api/project/real-local-bgm/workbench/music/project-tracks/{track['id']}"
        )
        assert media.status_code == 200
        assert media.content == stored.read_bytes()

    def test_projects_shape_and_state(self, client, projects_root):
        _make_project(projects_root, "film")

        projects = client.get("/api/projects")
        assert projects.status_code == 200
        body = projects.json()
        assert len(body) == 1
        assert body[0]["project_id"] == "film"
        assert body[0]["awaiting_human"] is True
        assert "stage_states" in body[0]

        state = client.get("/api/project/film/state")
        assert state.status_code == 200
        state_body = state.json()
        assert state_body["project_id"] == "film"
        assert state_body["title"] == "Film"
        assert state_body["stages"]

    def test_library_creates_a_project_with_a_reusable_intake(self, client, projects_root):
        response = client.post("/api/projects", json={
            "project_id": "robot-lamp-demo",
            "title": "机器人台灯试片",
            "brief": "向电商消费者展示机器人台灯的自动追光功能。",
            "duration_seconds": 15,
            "pipeline_type": "animated-explainer",
            "aspect": "landscape",
            "style_playbook": "clean-professional",
        })

        assert response.status_code == 201
        assert response.json()["project_id"] == "robot-lamp-demo"
        marker = json.loads((projects_root / "robot-lamp-demo" / "project.json").read_text(encoding="utf-8"))
        assert marker["intake"]["brief"] == "向电商消费者展示机器人台灯的自动追光功能。"
        assert marker["intake"]["duration_seconds"] == 15
        assert marker["render_profile"]["width"] == 1920
        assert (projects_root / "robot-lamp-demo" / "assets" / "images").is_dir()

        workbench = client.get("/api/project/robot-lamp-demo/workbench")
        assert workbench.status_code == 200
        assert workbench.json()["project"]["intake"]["aspect"] == "landscape"
        assert workbench.json()["project"]["duration_seconds"] == 15
        assert workbench.json()["scenes"] == []

        duplicate = client.post("/api/projects", json={
            "project_id": "robot-lamp-demo", "title": "重复项目", "pipeline_type": "animated-explainer",
        })
        assert duplicate.status_code == 409

    def test_library_exposes_avatar_spokesperson_as_a_real_workflow(self, client, projects_root):
        response = client.post("/api/projects", json={
            "project_id": "avatar-demo",
            "title": "数字人口播试片",
            "brief": "由数字人讲解产品使用方法。",
            "duration_seconds": 15,
            "pipeline_type": "avatar-spokesperson",
            "aspect": "portrait",
            "style_playbook": "clean-professional",
            "avatar_source_status": "planned",
            "avatar_import_mode": "per_turn",
            "avatar_default_treatment": "pip_top_left",
            "avatar_background_mode": "opaque",
        })

        assert response.status_code == 201, response.text
        workbench = client.get("/api/project/avatar-demo/workbench")
        assert workbench.status_code == 200
        payload = workbench.json()
        assert payload["project"]["pipeline_type"] == "avatar-spokesperson"
        assert payload["avatar"]["status"] == "not_configured"
        assert payload["project"]["intake"]["aspect"] == "portrait"
        assert payload["project"]["intake"]["avatar"]["default_treatment"] == "pip_top_left"
        assert payload["project"]["intake"]["avatar"]["generation_mode"] == "runninghub_longcat"

    def test_library_quick_create_uses_only_workflow_and_aspect_inputs(self, client, projects_root):
        response = client.post("/api/projects", json={
            "project_id": "quick-avatar-demo",
            "title": "快速数字人口播项目",
            "pipeline_type": "avatar-spokesperson",
            "aspect": "portrait",
        })

        assert response.status_code == 201, response.text
        marker = json.loads((projects_root / "quick-avatar-demo" / "project.json").read_text(encoding="utf-8"))
        assert marker["pipeline_type"] == "avatar-spokesperson"
        assert marker["render_profile"]["width"] == 1080
        assert marker["intake"]["duration_source"] == "audio_driven"
        assert "duration_seconds" not in marker["intake"]
        assert "brief" not in marker["intake"]
        assert marker["intake"]["avatar"]["source_status"] == "planned"
        assert marker["intake"]["avatar"]["generation_mode"] == "runninghub_longcat"

        workbench = client.get("/api/project/quick-avatar-demo/workbench")
        assert workbench.status_code == 200
        assert workbench.json()["project"]["pipeline_type"] == "avatar-spokesperson"
        assert workbench.json()["project"]["intake"]["duration_source"] == "audio_driven"

    def test_library_quick_create_rejects_hidden_legacy_workflows(self, client):
        response = client.post("/api/projects", json={
            "project_id": "hidden-hybrid-demo",
            "title": "不应创建的旧工作流",
            "pipeline_type": "hybrid",
            "aspect": "portrait",
        })

        assert response.status_code == 422
        assert "无数字人口播" in response.json()["detail"]

    def test_library_quick_create_page_only_exposes_two_workflows(self, client):
        library = client.get("/")

        assert library.status_code == 200
        assert library.text.count('name="pipeline_type"') == 1
        assert library.text.count('<option value="animated-explainer"') == 1
        assert library.text.count('<option value="avatar-spokesperson"') == 1
        assert "项目代号" not in library.text
        assert '<input id="projectId" name="project_id" type="hidden">' in library.text

        css = client.get("/ui/board.css")
        assert css.status_code == 200
        assert re.search(r"\.project-dialog\s*\{[^}]*margin:\s*auto;", css.text, re.DOTALL)
        assert 'id="projectDuration"' not in library.text
        assert 'id="projectBrief"' not in library.text
        assert 'id="avatarWorkflowInputs"' not in library.text

    def test_library_deletion_preview_and_confirmed_delete_remove_complete_project(self, client, projects_root):
        project = _make_project(projects_root, "delete-me")
        (project / "assets" / "video" / "source.mp4").write_bytes(b"video-bytes")
        (project / "renders" / "final.mp4").write_bytes(b"render-bytes")

        preview = client.get("/api/projects/delete-me/deletion-preview")
        assert preview.status_code == 200, preview.text
        body = preview.json()
        assert body["project_id"] == "delete-me"
        assert body["title"] == "Film"
        assert body["can_delete"] is True
        assert body["storage"]["file_count"] >= 4
        assert body["storage"]["categories"]["assets"] == len(b"video-bytes")
        assert body["storage"]["categories"]["renders"] == len(b"render-bytes")

        wrong = client.request("DELETE", "/api/projects/delete-me", json={
            "confirm_project_id": "delete-me",
            "confirmation": "",
            "permanent": True,
        })
        assert wrong.status_code == 422
        assert project.is_dir()

        deleted = client.request("DELETE", "/api/projects/delete-me", json={
            "confirm_project_id": "delete-me",
            "confirmation": "DELETE_PROJECT",
            "permanent": True,
        })
        assert deleted.status_code == 200, deleted.text
        assert deleted.json()["deleted"] is True
        assert not project.exists()
        assert all(item["project_id"] != "delete-me" for item in client.get("/api/projects").json())

    def test_library_refuses_to_delete_project_while_task_is_running(self, client, projects_root):
        project = _make_project(projects_root, "busy-project")
        checkpoint = json.loads((project / "checkpoint_script.json").read_text(encoding="utf-8"))
        checkpoint["status"] = "in_progress"
        _write_json(project / "checkpoint_script.json", checkpoint)

        preview = client.get("/api/projects/busy-project/deletion-preview")
        assert preview.status_code == 200
        assert preview.json()["can_delete"] is False
        assert preview.json()["active_tasks"]

        response = client.request("DELETE", "/api/projects/busy-project", json={
            "confirm_project_id": "busy-project",
            "confirmation": "DELETE_PROJECT",
            "permanent": True,
        })
        assert response.status_code == 409
        assert "正在运行" in response.json()["detail"]
        assert project.is_dir()

    def test_avatar_handoff_starts_assembly_before_applying_timeline(self, client, projects_root, monkeypatch):
        _make_project(projects_root, "avatar-flow")
        calls: list[str] = []

        def start(project_dir, payload):
            calls.append("start")
            return {}

        def assemble(project_dir, payload):
            calls.append("assemble")
            return {}

        def apply(project_dir, payload):
            calls.append("apply")
            return {}

        monkeypatch.setattr(server_mod, "start_avatar_assembly", start)
        monkeypatch.setattr(server_mod, "assemble_avatar_package", assemble)
        monkeypatch.setattr(server_mod, "apply_avatar_package_to_timeline", apply)
        response = client.post("/api/project/avatar-flow/workbench/avatar-package/handoff/jobs", json={"default_treatment": "custom"})

        assert response.status_code == 200
        assert calls and calls[0] == "start"

    def test_library_rejects_an_unsafe_or_incomplete_project_request(self, client):
        response = client.post("/api/projects", json={
            "project_id": "../outside", "title": "", "pipeline_type": "not-real",
        })
        assert response.status_code == 422

        library = client.get("/")
        assert library.status_code == 200
        assert "新建视频项目" in library.text

    def test_two_step_automation_endpoints_queue_network_assets_and_final_render(self, client, projects_root, monkeypatch):
        _make_project(projects_root, "film")
        calls: list[tuple[str, str]] = []

        def start_network(project_dir, payload):
            calls.append(("network-start", payload["confirmed"]))
            return {"automation": {"asset_generation": {"status": "generating"}}}

        def start_scene_refresh(project_dir, scene_id, payload):
            calls.append(("scene-refresh-start", scene_id, payload["confirmed"]))
            return {"automation": {"asset_generation": {"status": "generating", "mode": "scene_refresh"}}}

        def generate_network(project_dir):
            calls.append(("network-worker", project_dir.name))

        def start_final(project_dir, payload):
            calls.append(("final-start", payload["profile_name"]))
            return {"automation": {"final_generation": {"status": "generating"}}}

        def generate_final(project_dir):
            calls.append(("final-worker", project_dir.name))

        monkeypatch.setattr(server_mod, "start_network_asset_generation", start_network)
        monkeypatch.setattr(server_mod, "start_scene_network_asset_refresh", start_scene_refresh)
        monkeypatch.setattr(server_mod, "generate_network_assets", generate_network)
        monkeypatch.setattr(server_mod, "start_auto_final_generation", start_final)
        monkeypatch.setattr(server_mod, "generate_auto_final_video", generate_final)

        asset_job = client.post(
            "/api/project/film/workbench/automation/network-assets/jobs",
            json={"confirmed": True, "fill_undecided": True},
        )
        refresh_job = client.post(
            "/api/project/film/workbench/scenes/scene-b/network-assets/jobs",
            json={"confirmed": True, "instruction": "A different shot"},
        )
        final_job = client.post(
            "/api/project/film/workbench/automation/finalize/jobs",
            json={"confirmed": True, "profile_name": "qwen serena", "voice_label": "默认雅雅"},
        )

        assert asset_job.status_code == 200
        assert asset_job.json()["automation"]["asset_generation"]["status"] == "generating"
        assert refresh_job.status_code == 200
        assert refresh_job.json()["automation"]["asset_generation"]["mode"] == "scene_refresh"
        assert final_job.status_code == 200
        assert final_job.json()["automation"]["final_generation"]["status"] == "generating"
        assert ("network-start", True) in calls
        assert ("scene-refresh-start", "scene-b", True) in calls
        assert ("final-start", "qwen serena") in calls

    @pytest.mark.parametrize(
        ("url", "status"),
        [
            ("/api/project/../state", 404),
            ("/api/project/C:/state", 400),
            ("/api/project/nope/state", 404),
        ],
    )
    def test_project_id_rejects_bad_or_unknown_ids(self, client, url, status):
        response = client.get(url)
        assert response.status_code == status

    def test_media_rejects_path_traversal(self, client, projects_root):
        _make_project(projects_root, "film")
        response = client.get("/media/film/%2E%2E/project.json")
        assert response.status_code == 403

    def test_media_serves_range_requests(self, client, projects_root):
        project = _make_project(projects_root, "film")
        media = project / "renders" / "final.mp4"
        media.write_bytes(b"0123456789")

        response = client.get("/media/film/renders/final.mp4", headers={"Range": "bytes=2-5"})

        assert response.status_code == 206
        assert response.content == b"2345"
        assert response.headers["content-range"].startswith("bytes 2-5/10")

    def test_thumb_downscales_image_and_passes_through_non_media(self, client, projects_root):
        project = _make_project(projects_root, "film")
        _write_png(project / "assets" / "images" / "sc1.png")
        text = project / "artifacts" / "note.txt"
        text.write_text("hello", encoding="utf-8")

        image = client.get("/thumb/film/assets/images/sc1.png?w=320")
        assert image.status_code == 200
        assert image.headers["content-type"] == "image/jpeg"
        assert image.content.startswith(b"\xff\xd8")

        passthrough = client.get("/thumb/film/artifacts/note.txt")
        assert passthrough.status_code == 200
        assert passthrough.content == b"hello"


class TestBacklotPerformanceBudgets:
    def test_projects_and_state_stay_within_loose_budgets(self, client, projects_root):
        for i in range(25):
            project = _make_project(projects_root, f"film-{i:02d}")
            _write_json(
                project / "artifacts" / "scene_plan.json",
                {"version": "1.0", "scenes": [{"id": "sc1", "start_seconds": 0, "end_seconds": 1}]},
            )

        t0 = time.perf_counter()
        cold = client.get("/api/projects")
        cold_s = time.perf_counter() - t0
        assert cold.status_code == 200
        assert cold_s < 2.0

        t1 = time.perf_counter()
        warm = client.get("/api/projects")
        warm_s = time.perf_counter() - t1
        assert warm.status_code == 200
        assert warm_s < 0.150

        t2 = time.perf_counter()
        state = client.get("/api/project/film-00/state")
        state_s = time.perf_counter() - t2
        assert state.status_code == 200
        assert state_s < 0.400

    def test_image_thumb_generation_stays_within_budget(self, client, projects_root):
        project = _make_project(projects_root, "film")
        _write_png(project / "assets" / "images" / "sc1.png")

        t0 = time.perf_counter()
        response = client.get("/thumb/film/assets/images/sc1.png?w=640")
        elapsed = time.perf_counter() - t0

        assert response.status_code == 200
        assert elapsed < 1.5


class TestFindingsFixes:
    """Regression tests for dogfood findings F-03 (thumb video fallback)."""

    def test_thumb_never_serves_raw_video_bytes(self, client, projects_root):
        p = _make_project(projects_root, "vid")
        fake_video = p / "renders" / "final.mp4"
        fake_video.parent.mkdir(parents=True, exist_ok=True)
        # Not a real video: ffmpeg poster extraction will fail.
        fake_video.write_bytes(b"\x00" * 4096)
        res = client.get("/thumb/vid/renders/final.mp4")
        assert res.status_code == 404  # never the raw video bytes (F-03)


class TestDailyNewsSelectionV2Api:
    def test_reads_independent_selection_without_touching_script_pipeline(self, client, monkeypatch):
        monkeypatch.setattr(server_mod, "read_news_selection_v2_run", lambda target: {"status": "succeeded", "target_date": target.isoformat()})
        monkeypatch.setattr(server_mod, "read_news_selection_v2", lambda target: {"version": "2.0", "selected_stories": []})

        response = client.get("/api/daily-automation/news-selection-v2?target_date=2026-08-23")

        assert response.status_code == 200
        assert response.json()["run"]["status"] == "succeeded"
        assert response.json()["selection"]["version"] == "2.0"

    def test_manual_weak_story_replacement_is_exposed_as_a_text_only_action(self, client, monkeypatch):
        called = []

        def replace(target):
            called.append(target.isoformat())
            return {"target_date": target.isoformat(), "status": "queued", "current_stage": "script"}

        monkeypatch.setattr(server_mod, "request_text_story_replacement", replace)

        response = client.post("/api/daily-automation/runs/2026-08-26/replace-weak-story")

        assert response.status_code == 200
        assert response.json()["accepted"] is True
        assert response.json()["run"]["current_stage"] == "script"
        assert called == ["2026-08-26"]
