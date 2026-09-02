"""Regression tests for the writable Chinese director workbench."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from types import SimpleNamespace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from backlot import server as server_mod
from backlot import state as state_mod
from backlot import subtitle_preferences as subtitle_preferences_mod
from backlot import workbench as workbench_mod
from backlot.workbench import _ffmpeg_available
from tools.base_tool import ToolStatus
from tools.video.video_compose import VideoCompose


def test_safe_relpath_accepts_cwd_relative_path_already_rooted_in_project(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    project = Path("projects") / "demo"
    output = project / "renders" / "preview.mp4"
    output.parent.mkdir(parents=True)
    output.write_bytes(b"video")

    assert workbench_mod._safe_relpath(project, str(output)) == "renders/preview.mp4"


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


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def test_daily_story_headlines_reuse_one_asset_for_all_scenes_in_story(tmp_path: Path):
    state = {
        "scenes": [
            {"story_id": "S01", "start_seconds": 0, "end_seconds": 5, "headline_overlay": {"mode": "two_line", "line_1": "机器人比赛", "line_2": "真刀真枪检验技术"}},
            {"story_id": "S01", "start_seconds": 5, "end_seconds": 10, "headline_overlay": {"mode": "two_line", "line_1": "机器人比赛", "line_2": "真刀真枪检验技术"}},
            {"story_id": "S02", "start_seconds": 10, "end_seconds": 15, "headline_overlay": {"mode": "one_line", "line_1": "模型大幅降价", "line_2": ""}},
            {"story_id": "", "start_seconds": 15, "end_seconds": 18},
        ]
    }

    overlays, report = workbench_mod._daily_story_headline_overlays(tmp_path, state, 1080, 1920)

    assert len(overlays) == 2
    assert overlays[0]["story_id"] == "S01"
    assert overlays[0]["start_seconds"] == 0
    assert overlays[0]["end_seconds"] == 10
    assert overlays[0]["x"] == int(1080 * .36)
    assert {item["story_id"] for item in report["assets"]} == {"S01", "S02"}
    assert all((tmp_path / item["path"]).is_file() for item in report["assets"])


def test_daily_story_headline_preserves_space_inside_latin_product_name(tmp_path: Path):
    state = {
        "scenes": [{
            "story_id": "S01", "start_seconds": 0, "end_seconds": 5,
            "headline_overlay": {"mode": "two_line", "line_1": "GPT-5.6 Sol", "line_2": "限时降价超过20%"},
        }]
    }

    _, report = workbench_mod._daily_story_headline_overlays(tmp_path, state, 1080, 1920)

    assert report["assets"][0]["line_1"] == "GPT-5.6 Sol"


def test_visual_timeline_hot_swap_keeps_scene_story_id():
    state = {"assets": [{"id": "S-001", "type": "video", "path": "assets/stock.mp4"}]}
    scene = {"id": "section-001", "story_id": "S01", "start_seconds": 0, "end_seconds": 5}

    blocks = workbench_mod._validated_visual_timeline(state, scene, [{
        "id": "VB-001", "start_seconds": 0, "end_seconds": 5,
        "source_mode": "web_download", "asset_id": "S-001",
    }])

    assert blocks[0]["story_id"] == "S01"


def test_subtitle_phrases_do_not_split_latin_product_group():
    phrases = workbench_mod._split_subtitle_phrases(
        "小米展示玄戒O100原型机和Xiaomi AI Cube Prototype端侧AI原型设备。"
    )

    assert any("Xiaomi AI Cube Prototype" in phrase for phrase in phrases)
    assert "".join(phrases).replace(" ", "") == "小米展示玄戒O100原型机和XiaomiAICubePrototype端侧AI原型设备。"


def test_video_loudness_normalization_targets_douyin_ready_level(tmp_path: Path):
    ffmpeg = _ffmpeg_available()
    if not ffmpeg:
        pytest.skip("ffmpeg unavailable")
    source = tmp_path / "quiet.mp4"
    created = subprocess.run(
        [
            ffmpeg, "-y", "-f", "lavfi", "-i", "color=c=black:s=320x568:r=25:d=2",
            "-f", "lavfi", "-i", "sine=frequency=1000:duration=2", "-filter:a", "volume=0.03",
            "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(source),
        ],
        capture_output=True,
        check=False,
    )
    assert created.returncode == 0

    report = workbench_mod._normalize_video_loudness(tmp_path, source, target_lufs=-14.0)

    assert -15.5 <= report["integrated_lufs"] <= -12.5
    assert report["true_peak_dbtp"] <= -1.0


def test_video_loudness_normalization_uses_measured_two_pass_and_peak_headroom(tmp_path, monkeypatch):
    source = tmp_path / "mixed-preview.mp4"
    source.write_bytes(b"mixed")
    captured: list[list[str]] = []

    monkeypatch.setattr(workbench_mod, "_ffmpeg_available", lambda: "ffmpeg")
    monkeypatch.setattr(workbench_mod, "_analyze_loudnorm", lambda *_args, **_kwargs: {
        "input_i": -15.3,
        "input_tp": -0.96,
        "input_lra": 2.6,
        "input_thresh": -25.5,
        "target_offset": 0.2,
    })
    monkeypatch.setattr(workbench_mod, "_measure_integrated_loudness", lambda *_args: {
        "integrated_lufs": -14.1,
        "true_peak_dbtp": -1.4,
        "loudness_range_lu": 2.5,
        "threshold_lufs": -24.0,
    })

    def fake_run(command):
        captured.append(list(command))
        Path(command[-1]).write_bytes(b"normalized")
        return True, "ok"

    monkeypatch.setattr(workbench_mod, "_run_media", fake_run)

    report = workbench_mod._normalize_video_loudness(tmp_path, source, target_lufs=-14.0)

    command = captured[0]
    loudnorm_filter = command[command.index("-af") + 1]
    assert "TP=-2.0" in loudnorm_filter
    assert "measured_I=-15.300000" in loudnorm_filter
    assert "measured_TP=-0.960000" in loudnorm_filter
    assert "offset=0.200000" in loudnorm_filter
    assert "linear=true" in loudnorm_filter
    assert source.read_bytes() == b"normalized"
    assert report["normalization_true_peak_target_dbtp"] == -2.0
    assert report["acceptance_true_peak_limit_dbtp"] == -1.0


def test_atomic_write_retries_transient_windows_access_denial(tmp_path, monkeypatch):
    destination = tmp_path / "artifacts" / "workbench.json"
    real_replace = os.replace
    attempts = 0
    delays: list[float] = []

    def flaky_replace(source, target):
        nonlocal attempts
        attempts += 1
        if attempts <= 2:
            error = PermissionError(13, "Access is denied", str(target))
            error.winerror = 5
            raise error
        return real_replace(source, target)

    monkeypatch.setattr(workbench_mod.os, "replace", flaky_replace)
    monkeypatch.setattr(workbench_mod.time, "sleep", delays.append)

    workbench_mod._atomic_write(destination, {"status": "generating", "completed_slots": 0})

    assert attempts == 3
    assert delays == [0.05, 0.1]
    assert json.loads(destination.read_text(encoding="utf-8")) == {
        "status": "generating",
        "completed_slots": 0,
    }
    assert not list(destination.parent.glob(".workbench.json.*.tmp"))


def test_autonomous_director_retries_metadata_then_downloads_only_selected_asset(projects_root, monkeypatch):
    from backlot.visual_director import DirectorDecision
    from tools.video.stock_sources.base import Candidate

    project = make_project(projects_root)
    state = workbench_mod.bootstrap_workbench(project)
    scene = next(item for item in state["scenes"] if item["id"] == "scene-b")
    block = {"id": "VB-001", "start_seconds": 0, "end_seconds": 5, "slot_text": "机器人进入工厂"}
    monkeypatch.setenv("PEXELS_API_KEY", "test-key")
    search_queries: list[str] = []
    downloads: list[str] = []

    candidate = Candidate(
        source="pexels", source_id="34775736", source_url="https://www.pexels.com/video/34775736/",
        download_url="https://videos.example/final.mp4?temporary-secret", kind="video",
        width=1080, height=1920, duration=8, source_tags="industrial robot factory assembly line",
        thumbnail_url="https://images.example/cover.jpg", extra={"preview_frames": ["https://images.example/1.jpg"]},
    )

    def fake_search(_self, query, _filters):
        search_queries.append(query)
        return [candidate]

    def fake_download(_self, selected, output):
        downloads.append(selected.source_id)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"final-only")
        return output

    decisions = [
        DirectorDecision(None, "retry", {"decision": "retry", "reason": "需要更具体镜头", "weighted_score": 0}, ("robot arm assembly line",)),
        DirectorDecision(candidate, "accept", {"decision": "accept", "reason": "合格", "weighted_score": 88, "semantic_score": 90, "confidence": .9}, ()),
    ]
    monkeypatch.setattr(workbench_mod.PexelsSource, "search", fake_search)
    monkeypatch.setattr(workbench_mod.PexelsSource, "download", fake_download)
    monkeypatch.setattr(workbench_mod, "decide_candidate", lambda *_args, **_kwargs: decisions.pop(0))
    monkeypatch.setattr(workbench_mod, "_screen_visual_candidate", lambda *_args: {"status": "passed", "score": 90, "reasons": [], "metrics": {}, "mode": "test"})
    item = {
        "scene_id": scene["id"], "block_id": block["id"], "query": "industrial robot factory",
        "query_ladder": [{"level": "精确检索", "query": "industrial robot factory"}],
        "candidate_limit": 4, "slot_text": "机器人进入工厂", "context_text": "机器人产业化",
        "visual_intent": "机器人装配", "search_role": "process",
        "director_ledger": {"attempts": [], "status": "pending"},
    }

    result, path, screening = workbench_mod._find_autonomous_pexels_candidate(
        project, state, item, scene, block, media_kind="video", query=item["query"], orientation="portrait",
        target_duration=5, content_rules=[], person_policy="balanced", used_provider_ids=set(),
    )

    assert result.success and screening["status"] == "passed"
    assert (project / path).read_bytes() == b"final-only"
    assert search_queries == ["industrial robot factory", "robot arm assembly line"]
    assert downloads == ["34775736"]
    assert item["director_ledger"]["status"] == "accepted"
    assert "temporary-secret" not in json.dumps(item["director_ledger"], ensure_ascii=False)


def test_contextual_broll_policy_accepts_topical_candidate_after_strict_retries(projects_root, monkeypatch):
    from backlot.visual_director import DirectorDecision
    from tools.video.stock_sources.base import Candidate

    project = make_project(projects_root)
    state = workbench_mod.bootstrap_workbench(project)
    scene = next(item for item in state["scenes"] if item["id"] == "scene-b")
    block = {"id": "VB-001", "start_seconds": 0, "end_seconds": 3, "slot_text": "双手机接收通知"}
    monkeypatch.setenv("PEXELS_API_KEY", "test-key")
    candidate = Candidate(
        source="pexels", source_id="phone-broll-1",
        source_url="https://www.pexels.com/video/phone-broll-1/",
        download_url="https://videos.example/phone.mp4", kind="video",
        width=1080, height=1920, duration=6, source_tags="smartphone notification close up",
    )
    monkeypatch.setattr(workbench_mod.PexelsSource, "search", lambda *_args, **_kwargs: [candidate])

    def fake_download(_self, _selected, output):
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"broll")
        return output

    monkeypatch.setattr(workbench_mod.PexelsSource, "download", fake_download)
    decisions = [
        DirectorDecision(None, "retry", {"decision": "retry", "reason": "不够字面匹配"}, ("smartphone notification",)),
        DirectorDecision(None, "fallback", {"decision": "fallback", "reason": "没有双机同屏"}, ()),
    ]
    monkeypatch.setattr(workbench_mod, "decide_candidate", lambda *_args, **_kwargs: decisions.pop(0))
    monkeypatch.setattr(
        workbench_mod, "_screen_visual_candidate",
        lambda *_args: {"status": "passed", "score": 88, "reasons": [], "metrics": {}, "mode": "test"},
    )
    item = {
        "scene_id": scene["id"], "block_id": block["id"], "query": "smartphone notification close up",
        "query_ladder": [], "candidate_limit": 4, "slot_text": "双手机接收通知",
        "context_text": "两台手机跨设备接电话和收验证码", "visual_intent": "手机通知场景",
        "semantic_tolerance": "contextual_broll",
        "director_ledger": {"attempts": [], "status": "pending"},
    }

    result, path, screening = workbench_mod._find_autonomous_pexels_candidate(
        project, state, item, scene, block, media_kind="video", query=item["query"],
        orientation="portrait", target_duration=3, content_rules=[], person_policy="balanced",
        used_provider_ids=set(),
    )

    assert result.success and screening["status"] == "passed"
    assert (project / path).read_bytes() == b"broll"
    assert item["director_ledger"]["status"] == "accepted"
    assert item["director_ledger"]["attempts"][-1]["contextual_broll_override"]["decision"] == "accept"


def test_autonomous_director_empty_candidates_renders_hyperframes_safety_fallback(projects_root, monkeypatch):
    project = make_project(projects_root)
    state = workbench_mod.bootstrap_workbench(project)
    scene = next(item for item in state["scenes"] if item["id"] == "scene-b")
    block = {"id": "VB-001", "start_seconds": 0, "end_seconds": 5, "status": "planned", "route": "stock_video"}
    scene["visual_timeline"] = {"blocks": [block], "planning_mode": "ai_director"}
    item = {
        "scene_id": scene["id"], "block_id": block["id"], "slot_index": 1, "status": "queued",
        "query": "industrial robot factory", "query_ladder": [], "attempt": 1,
        "target_duration_seconds": 5, "media_kind": "video", "source_mode": "web_download",
        "route": "stock_video", "planning_mode": "ai_director", "fallback_route": "hyperframes",
        "visual_intent": "机器人装配", "slot_text": "机器人进入工厂", "context_text": "机器人产业化",
        "scene_recipe": "headline_statement", "graphic_copy": {"headline": "机器人进工厂", "scene_goal": "自动化生产", "nodes": ["机器人", "工厂", "流程"]},
        "content_rules": [], "person_policy": "balanced", "candidate_limit": 4,
        "director_ledger": {"attempts": [], "status": "pending"},
    }
    state["automation"]["visual_batch"] = {
        "status": "queued", "job_id": "VBJ-fallback", "items": [item], "total_slots": 1,
        "completed_slots": 0, "failed_slots": 0, "current": None, "planning_mode": "ai_director",
    }
    workbench_mod._save(project, state)
    monkeypatch.setenv("PEXELS_API_KEY", "test-key")
    monkeypatch.setattr(workbench_mod.PexelsSource, "search", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(workbench_mod, "_generate_hyperframes_visual_block", lambda *_args, **_kwargs: {"id": "S-HF-001", "name": "安全信息图"})

    completed = workbench_mod.generate_visual_batch(project, "VBJ-fallback")
    result = completed["automation"]["visual_batch"]["items"][0]
    rendered_block = next(item for item in next(scene for scene in completed["scenes"] if scene["id"] == "scene-b")["visual_timeline"]["blocks"] if item["id"] == "VB-001")

    assert result["status"] == "completed"
    assert result["director_ledger"]["status"] == "fallback_rendered"
    assert rendered_block["status"] == "ready"
    assert rendered_block["asset_id"] == "S-HF-001"


def test_hyperframes_inspect_failure_falls_back_to_stock_video(projects_root, monkeypatch):
    project = make_project(projects_root)
    state = workbench_mod.bootstrap_workbench(project)
    scene = next(item for item in state["scenes"] if item["id"] == "scene-b")
    block = {"id": "VB-001", "start_seconds": 0, "end_seconds": 3, "status": "planned", "route": "hyperframes"}
    scene["visual_timeline"] = {"blocks": [block], "planning_mode": "ai_director"}
    item = {
        "scene_id": scene["id"], "block_id": block["id"], "slot_index": 1, "status": "queued",
        "query": "smartphone comments close up", "query_ladder": [], "attempt": 1,
        "target_duration_seconds": 3, "media_kind": "video", "source_mode": "hyperframes",
        "route": "hyperframes", "planning_mode": "ai_director", "fallback_route": "stock_video",
        "visual_intent": "评论互动", "slot_text": "评论区聊聊", "context_text": "科技选择题",
        "scene_recipe": "closing_question", "graphic_copy": {"headline": "科技选择题"},
        "content_rules": [], "person_policy": "balanced", "candidate_limit": 4,
        "director_ledger": {"attempts": [], "status": "pending"},
    }
    state["automation"]["visual_batch"] = {
        "status": "queued", "job_id": "VBJ-hf-stock-fallback", "items": [item], "total_slots": 1,
        "completed_slots": 0, "failed_slots": 0, "current": None, "planning_mode": "ai_director",
    }
    workbench_mod._save(project, state)
    monkeypatch.setattr(
        workbench_mod, "_generate_hyperframes_visual_block",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(workbench_mod.WorkbenchError("inspect failed")),
    )
    stock_path = Path("assets/video/pexels/fallback.mp4")
    (project / stock_path).parent.mkdir(parents=True, exist_ok=True)
    (project / stock_path).write_bytes(b"stock-fallback")
    stock_result = SimpleNamespace(success=True, data={
        "duration_seconds": 3, "width": 1080, "height": 1920, "video_id": "fallback-1",
        "pexels_url": "https://www.pexels.com/video/fallback-1/", "license": "Pexels License",
    })
    monkeypatch.setattr(
        workbench_mod, "_find_autonomous_pexels_candidate",
        lambda *_args, **_kwargs: (stock_result, str(stock_path), {"status": "passed", "score": 90}),
    )
    monkeypatch.setattr(workbench_mod, "_probe_duration_seconds", lambda *_args, **_kwargs: 3.0)

    completed = workbench_mod.generate_visual_batch(project, "VBJ-hf-stock-fallback")
    result = completed["automation"]["visual_batch"]["items"][0]
    rendered_block = next(
        item for item in next(scene for scene in completed["scenes"] if scene["id"] == "scene-b")["visual_timeline"]["blocks"]
        if item["id"] == "VB-001"
    )

    assert result["status"] == "completed"
    assert result["route"] == "stock_video"
    assert result["director_ledger"]["status"] == "fallback_rendered"
    assert rendered_block["status"] == "ready"
    assert rendered_block["route"] == "stock_video"


def make_project(root: Path) -> Path:
    project = root / "film"
    (project / "artifacts").mkdir(parents=True)
    (project / "assets" / "video").mkdir(parents=True)
    write_json(project / "project.json", {"project_id": "film", "title": "导演审核测试", "pipeline_type": "cinematic"})
    write_json(project / "artifacts" / "script.json", {
        "title": "导演审核测试", "sections": [
            {"id": "s1", "text": "开场说明", "start_seconds": 0, "end_seconds": 4},
            {"id": "s2", "text": "核心展示", "start_seconds": 4, "end_seconds": 9},
        ],
    })
    write_json(project / "artifacts" / "scene_plan.json", {"scenes": [
        {"id": "scene-a", "description": "开场", "start_seconds": 0, "end_seconds": 4, "script_section_id": "s1"},
        {"id": "scene-b", "description": "高潮", "start_seconds": 4, "end_seconds": 9, "script_section_id": "s2", "hero_moment": True},
    ]})
    write_json(project / "artifacts" / "asset_manifest.json", {"assets": [
        {"id": "opening", "type": "image", "path": "assets/opening.png", "scene_id": "scene-a", "source_tool": "provided_asset"},
    ]})
    (project / "assets" / "opening.png").write_bytes(b"image")
    (project / "assets" / "video" / "candidate.mp4").write_bytes(b"not-a-real-video")
    return project


def test_asset_library_audit_moves_only_safe_unused_assets_to_recycle_bin(projects_root):
    project = make_project(projects_root)
    (project / "assets" / "managed").mkdir()
    (project / "assets" / "managed" / "active.png").write_bytes(b"active")
    (project / "assets" / "managed" / "leftover.png").write_bytes(b"same-leftover")
    (project / "assets" / "managed" / "duplicate.png").write_bytes(b"same-leftover")
    state = workbench_mod.bootstrap_workbench(project)
    active = workbench_mod._append_asset(project, state, {
        "name": "正在使用的测试素材", "type": "image", "source_type": "web_download",
        "path": "assets/managed/active.png", "license": "test",
    })
    leftover = workbench_mod._append_asset(project, state, {
        "name": "可清理的测试素材", "type": "image", "source_type": "web_download",
        "path": "assets/managed/leftover.png", "license": "test",
    })
    duplicate = workbench_mod._append_asset(project, state, {
        "name": "重复的测试素材", "type": "image", "source_type": "ai_generated",
        "path": "assets/managed/duplicate.png", "license": "test",
    })
    workbench_mod._save(project, state)
    workbench_mod.assign_usage(project, {"scene_id": "scene-a", "asset_id": active["id"], "role": "visual"})

    audit = workbench_mod.audit_asset_library(project)
    rows = {item["id"]: item for item in audit["assets"]}
    assert rows[active["id"]]["status"] == "active"
    assert rows[leftover["id"]]["status"] == "unused"
    assert rows[leftover["id"]]["cleanup_eligible"] is True
    assert rows[leftover["id"]]["duplicate_count"] == 2
    assert audit["summary"]["duplicate_group_count"] >= 1

    with pytest.raises(workbench_mod.WorkbenchError, match="没有可安全"):
        workbench_mod.cleanup_unused_assets(project, {"asset_ids": [active["id"]], "confirmed": True})

    cleaned = workbench_mod.cleanup_unused_assets(project, {
        "asset_ids": [leftover["id"]], "confirmed": True,
    })
    cleaned_asset = next(item for item in cleaned["assets"] if item["id"] == leftover["id"])
    assert cleaned_asset["lifecycle"]["status"] == "trashed"
    assert not (project / "assets" / "managed" / "leftover.png").exists()
    assert (project / cleaned_asset["lifecycle"]["trash_path"]).is_file()
    assert (project / "assets" / "managed" / "active.png").is_file()

    restored = workbench_mod.restore_trashed_asset(project, leftover["id"])
    restored_asset = next(item for item in restored["assets"] if item["id"] == leftover["id"])
    assert restored_asset["lifecycle"]["status"] == "active"
    assert (project / "assets" / "managed" / "leftover.png").is_file()


def test_asset_library_audit_cleanup_and_restore_endpoints(client, projects_root):
    project = make_project(projects_root)
    (project / "assets" / "managed").mkdir()
    (project / "assets" / "managed" / "unused.png").write_bytes(b"unused")
    client.post("/api/project/film/workbench/bootstrap")
    registered = client.post("/api/project/film/workbench/assets", json={
        "name": "接口测试素材", "type": "image", "source_type": "web_download",
        "path": "assets/managed/unused.png", "license": "test",
    })
    assert registered.status_code == 200
    asset_id = registered.json()["assets"][-1]["id"]
    audit = client.get("/api/project/film/workbench/asset-library/audit")
    assert audit.status_code == 200
    candidate = next(item for item in audit.json()["assets"] if item["id"] == asset_id)
    assert candidate["cleanup_eligible"] is True
    cleaned = client.post("/api/project/film/workbench/asset-library/cleanup", json={
        "asset_ids": [asset_id], "confirmed": True,
    })
    assert cleaned.status_code == 200
    restored = client.post(f"/api/project/film/workbench/asset-library/assets/{asset_id}/restore")
    assert restored.status_code == 200
    assert next(item for item in restored.json()["assets"] if item["id"] == asset_id)["lifecycle"]["status"] == "active"


def test_subtitles_are_split_into_short_phrase_cues(projects_root):
    project = make_project(projects_root)
    scenes = [{"id": "scene-a", "start_seconds": 0, "end_seconds": 10, "script_section_id": "s1"}]
    sections = {"s1": {"text": "如果你总觉得一本书很厚、很难开始，不妨先别追求一次读完。你只要先读十分钟，今天就已经迈出第一步了。"}}

    subtitle_path = workbench_mod._write_subtitles(project, scenes, sections)
    content = subtitle_path.read_text(encoding="utf-8")

    assert content.count(" --> ") == 4
    assert "如果你总觉得一本书很厚、很难开始，" in content
    assert "不妨先别追求一次读完。" in content
    assert "你只要先读十分钟，" in content
    assert "今天就已经迈出第一步了。" in content
    assert all(len(line) <= 19 for line in content.splitlines() if line and "-->" not in line and not line.isdigit())


def test_scene_subtitle_edits_preserve_review_preview_and_phrase_timing(projects_root):
    project = make_project(projects_root)
    state = workbench_mod.bootstrap_workbench(project)
    scene = state["scenes"][0]
    scene["review_preview"] = {
        "status": "ready",
        "output_path": "renders/review-previews/existing.mp4",
        "caption_cues": [{"start_seconds": 0.0, "end_seconds": 4.0, "text": "开场说明"}],
    }
    state["automation"]["preview_render"] = {"status": "completed", "output_path": "renders/previews/old.mp4"}
    workbench_mod._save(project, state)

    updated = workbench_mod.update_scene_subtitles(project, "scene-a", {
        "style": {
            "font_size": 56,
            "text_color": "#42D8FF",
            "background_enabled": True,
            "position": {"x": .5, "y": .78, "width": .72, "anchor": "center"},
        },
        "cue_overrides": {"cue-001": "修改后的开场字幕"},
    })
    edited = updated["scenes"][0]

    assert edited["review_preview"]["status"] == "ready"
    assert edited["review_preview"]["output_path"] == "renders/review-previews/existing.mp4"
    assert edited["subtitles"]["cue_overrides"] == {"cue-001": "修改后的开场字幕"}
    assert edited["subtitles"]["style_override"]["font_size"] == 56
    assert edited["subtitles"]["style_override"]["position"]["y"] == .78
    assert updated["automation"]["preview_render"]["status"] == "needs_refresh"
    cues = workbench_mod._subtitle_cues(edited, "开场说明", relative_to_scene=True)
    assert cues[0]["id"] == "cue-001"
    assert cues[0]["text"] == "修改后的开场字幕"


def test_subtitle_template_applies_style_without_copying_words_or_narration(projects_root):
    project = make_project(projects_root)
    state = workbench_mod.bootstrap_workbench(project)
    first, second = state["scenes"]
    first["narration"]["text"] = "第一个片段原文"
    second["narration"]["text"] = "第二个片段原文"
    second["subtitles"] = {"template_id": "subtitle-default", "style_override": {}, "cue_overrides": {"cue-001": "只属于第二段"}}
    second["review_preview"] = {"status": "ready", "output_path": "renders/review-previews/second.mp4", "caption_cues": []}
    workbench_mod._save(project, state)

    updated = workbench_mod.update_subtitle_style_template(project, {
        "scene_id": first["id"],
        "template_id": "subtitle-default",
        "name": "新闻蓝字幕",
        "style": {"font_size": 50, "text_color": "#46D7FF", "position": {"x": .5, "y": .82, "width": .78, "anchor": "bottom-center"}},
        "apply_scope": "all",
        "set_default": True,
    })
    first_updated, second_updated = updated["scenes"]
    resolved = workbench_mod._resolved_scene_subtitle_style(updated, second_updated)

    assert resolved["font_size"] == 50
    assert resolved["text_color"] == "#46D7FF"
    assert first_updated["narration"]["text"] == "第一个片段原文"
    assert second_updated["narration"]["text"] == "第二个片段原文"
    assert second_updated["subtitles"]["cue_overrides"] == {"cue-001": "只属于第二段"}
    assert second_updated["review_preview"]["status"] == "ready"


def test_software_default_subtitle_style_only_applies_when_a_project_is_first_created(projects_root, tmp_path, monkeypatch):
    """A saved workstation default must not rewrite a previously reviewed job."""
    preferences_path = tmp_path / "subtitle_preferences.json"
    monkeypatch.setattr(subtitle_preferences_mod, "PREFERENCES_PATH", preferences_path)
    existing_project = make_project(projects_root)
    existing = workbench_mod.bootstrap_workbench(existing_project)
    assert workbench_mod._resolved_scene_subtitle_style(existing, existing["scenes"][0])["font_size"] == 42

    saved = workbench_mod.update_subtitle_preferences_settings({
        "style": {
            "font": "Noto Sans CJK SC",
            "font_size": 58,
            "text_color": "#42D8FF",
            "outline_width": 5,
            "background_enabled": True,
            "background_color": "#07111F",
            "background_opacity": 72,
            "position": {"x": .5, "y": .81, "width": .76, "anchor": "bottom-center"},
            "max_lines": 3,
        },
    })
    assert saved["style"]["font_size"] == 58
    assert preferences_path.exists()

    reloaded_existing = workbench_mod.read_workbench(existing_project)
    assert workbench_mod._resolved_scene_subtitle_style(reloaded_existing, reloaded_existing["scenes"][0])["font_size"] == 42

    future_root = projects_root / "future"
    future_root.mkdir()
    future_project = make_project(future_root)
    future = workbench_mod.bootstrap_workbench(future_project)
    future_style = workbench_mod._resolved_scene_subtitle_style(future, future["scenes"][0])
    assert future_style["font"] == "Noto Sans CJK SC"
    assert future_style["font_size"] == 58
    assert future_style["background_enabled"] is True
    assert future_style["position"]["y"] == .81


def test_subtitle_editor_endpoints_and_ass_scene_style_rendering(client, projects_root, tmp_path):
    make_project(projects_root)
    client.post("/api/project/film/workbench/bootstrap")
    saved = client.put("/api/project/film/workbench/scenes/scene-a/subtitles", json={
        "style": {"font_size": 48, "position": {"x": .5, "y": .84, "width": .8, "anchor": "bottom-center"}},
        "cue_overrides": {"cue-001": "接口改字"},
    })
    assert saved.status_code == 200
    assert saved.json()["scenes"][0]["subtitles"]["cue_overrides"]["cue-001"] == "接口改字"
    applied = client.post("/api/project/film/workbench/subtitle-styles", json={
        "scene_id": "scene-a", "template_id": "subtitle-default", "name": "统一字幕", "apply_scope": "all",
        "style": {"font_size": 52, "position": {"x": .5, "y": .86, "width": .82, "anchor": "bottom-center"}},
    })
    assert applied.status_code == 200
    assert {scene["subtitles"]["template_id"] for scene in applied.json()["scenes"]} == {"subtitle-default"}

    srt = tmp_path / "caption.srt"
    ass = tmp_path / "caption.ass"
    srt.write_text("1\n00:00:00,000 --> 00:00:01,000\n第一句\n\n2\n00:00:01,000 --> 00:00:02,000\n第二句\n", encoding="utf-8")
    VideoCompose._write_ass_subtitles(srt, ass, {
        "font": "Microsoft YaHei", "font_size_ratio": .045, "responsive": True,
        "scene_styles": [{
            "start_seconds": 0, "end_seconds": 1,
            "style": {"font": "Microsoft YaHei", "font_size_ratio": .05, "position_x_ratio": .4, "position_y_ratio": .8, "alignment": 2},
        }],
    }, 1080, 1920)
    content = ass.read_text(encoding="utf-8")
    assert "Style: Scene001" in content
    assert "Dialogue: 0,0:00:00.00,0:00:01.00,Scene001" in content
    assert r"\pos(432,1536)" in content


def test_subtitle_default_endpoint_persists_validated_style(client, tmp_path, monkeypatch):
    preferences_path = tmp_path / "subtitle_preferences.json"
    monkeypatch.setattr(subtitle_preferences_mod, "PREFERENCES_PATH", preferences_path)
    saved = client.put("/api/workbench/subtitle-defaults", json={
        "style": {"font_size": 57, "position": {"x": .5, "y": .83, "width": .8, "anchor": "bottom-center"}},
    })
    assert saved.status_code == 200
    assert saved.json()["style"]["font_size"] == 57
    current = client.get("/api/workbench/subtitle-defaults")
    assert current.status_code == 200
    assert current.json()["style"]["position"]["y"] == .83


def test_workbench_client_exposes_live_subtitle_editor_without_video_rebuild(client):
    script = client.get("/ui/workbench.js")
    assert script.status_code == 200
    assert "renderSubtitleEditor" in script.text
    assert "keepSavedSubtitleDraft" in script.text
    assert "invalidateAll = false" in script.text
    assert "keepSavedSubtitleDraft(scene.id, nextState, { invalidateAll: true })" in script.text
    assert '"/subtitle-styles"' in script.text
    assert "reviewCaptionControllers" in script.text


def test_ppt_information_card_is_task_tracked_registered_and_assignable(projects_root):
    project = make_project(projects_root)
    saved_plan = workbench_mod.update_scene_visual_plan(project, "scene-a", {
        "engine": "ppt_card",
        "prompt": "用数据卡说明本段科技新闻的重点，避开左上角数字人和底部字幕。",
    })
    assert saved_plan["scenes"][0]["visual_plan"]["engine"] == "ppt_card"
    saved_brief = workbench_mod.update_scene_ppt_card_brief(project, "scene-a", {
        "card_type": "headline_metrics",
        "theme": "tech_neon",
        "title": "科技简报重点",
        "takeaway": "用一张可编辑信息卡呈现本段重点。",
        "items": ["芯片进展", "机器人应用"],
        "metrics": [{"label": "重点", "value": "2 项"}],
    })
    assert saved_brief["scenes"][0]["ppt_card_brief"]["status"] == "saved"
    queued = workbench_mod.start_scene_ppt_card_generation(project, "scene-a", {"confirmed": True})
    scene = next(item for item in queued["scenes"] if item["id"] == "scene-a")
    job = scene["ppt_card_generation"]
    assert job["status"] == "queued"
    assert workbench_mod.read_task_center(project)["active_count"] == 1

    completed = workbench_mod.generate_scene_ppt_card(project, "scene-a", job["job_id"])
    scene = next(item for item in completed["scenes"] if item["id"] == "scene-a")
    candidate = scene["ppt_card_candidate"]
    asset = next(item for item in completed["assets"] if item["id"] == candidate["asset_id"])
    assert asset["generation"]["kind"] == "ppt_information_card"
    assert asset["provenance"]["source_tool"] == "ppt_card_provider"
    assert (project / asset["path"]).is_file()
    assert (project / asset["generation"]["source_svg_path"]).is_file()
    assert (project / asset["generation"]["spec_path"]).is_file()
    assert any(item["kind"] == "ppt_card" and item["status"] == "completed" for item in workbench_mod.read_task_center(project)["tasks"])

    assigned = workbench_mod.assign_usage(project, {"scene_id": "scene-a", "asset_id": asset["id"], "role": "visual"})
    assigned_scene = next(item for item in assigned["scenes"] if item["id"] == "scene-a")
    assert assigned_scene["visual_timeline"]["blocks"][0]["asset_id"] == asset["id"]
    assert assigned_scene["visual_timeline"]["blocks"][0]["source_mode"] == "ppt_card"


def test_task_center_and_ppt_card_endpoints(client, projects_root):
    make_project(projects_root)
    client.post("/api/project/film/workbench/bootstrap")
    brief = client.put("/api/project/film/workbench/scenes/scene-a/ppt-card-brief", json={
        "title": "接口信息卡", "takeaway": "任务中心不应刷新播放器。", "items": ["任务状态独立刷新", "播放器保持当前播放位置"],
        "card_type": "headline_metrics", "theme": "tech_neon",
    })
    assert brief.status_code == 200
    queued = client.post("/api/project/film/workbench/scenes/scene-a/ppt-cards/jobs", json={
        "confirmed": True,
    })
    assert queued.status_code == 200
    tasks = client.get("/api/project/film/workbench/tasks")
    assert tasks.status_code == 200
    assert any(item["kind"] == "ppt_card" for item in tasks.json()["tasks"])


def test_workbench_client_exposes_task_center_and_ppt_card_provider(client):
    script = client.get("/ui/workbench.js")
    assert script.status_code == 200
    assert "全局任务中心" in script.text
    assert "ppt-cards/jobs" in script.text
    assert "ppt-card-brief" in script.text
    assert "trackTaskCenter" in script.text


def test_workbench_client_exposes_persistent_light_dark_theme_switch(client, projects_root):
    make_project(projects_root)
    page = client.get("/p/film")
    script = client.get("/ui/workbench.js")
    stylesheet = client.get("/ui/workbench.css")

    assert page.status_code == 200
    assert script.status_code == 200
    assert stylesheet.status_code == 200
    assert 'localStorage.getItem("backlot.theme")' in page.text
    assert 'const THEME_KEY = "backlot.theme"' in script.text
    assert "renderThemeToggle()" in script.text
    assert "切换至${nextLabel}主题" in script.text
    assert ':root[data-theme="light"]' in stylesheet.text
    assert ".theme-toggle" in stylesheet.text


def test_workbench_client_uses_explicit_two_step_visual_batch_flow(client):
    script = client.get("/ui/workbench.js")
    stylesheet = client.get("/ui/workbench.css")

    assert script.status_code == 200
    assert stylesheet.status_code == 200
    assert "① AI 识别并给出推荐" in script.text
    assert "② 开始${operationLabel}" in script.text
    assert "请先完成第一步：AI 识别并给出推荐。" in script.text
    assert "改为“替换所选主体画面”并重新识别" in script.text
    assert "仅选缺少主体画面" in script.text
    assert "主体画面：待补" in script.text
    assert "数字人：已就绪" in script.text
    assert "AI 正在识别并规划画面" in script.text
    assert "通常需要 20–90 秒" in script.text
    assert "visualBatchPlanning" in script.text
    assert "本次没有可执行的画面推荐" in script.text
    assert "visualBatchDraft.operationMode" in script.text
    assert "智能语义节奏：约 6 秒/画面" in script.text
    assert "平衡叙事：实拍 60%–70%，HY 30%–40%" in script.text
    assert "默认只在网络视频与 HyperFrames 之间推荐" in script.text
    assert "按时长推荐" in script.text
    assert "批量画面已全部完成" in script.text
    assert "visualBatchPlan = null;\n    trackVisualBatch((state.automation || {}).visual_batch);\n    render();" in script.text
    action_index = script.text.index('class: "visual-batch-actions"')
    progress_index = script.text.index('data-visual-batch-island', action_index)
    plan_index = script.text.index("planningFeedback,", action_index)
    assert action_index < progress_index < plan_index
    assert "visual-batch-empty-plan" in stylesheet.text
    assert "visual-plan-balance-warning" in stylesheet.text
    assert "主体画面仍在生成（${done}/${total}）" in script.text
    assert "等待主体画面完成" in script.text


def test_ppt_card_brief_uses_narration_not_visual_prompt_and_persists(projects_root):
    project = make_project(projects_root)
    state = workbench_mod.bootstrap_workbench(project)
    scene = state["scenes"][0]
    scene["narration"]["text"] = "数字人不再只是屏幕里的形象，它已经能够承接直播、讲解和品牌服务。真正关键的是内容与角色可以稳定复用。"
    scene["visual_plan"]["prompt"] = "为视频片段设计一张科技画面，保留左上角数字人安全区，禁止第二主播。"
    workbench_mod._save(project, state)

    initial = workbench_mod.read_workbench(project)
    brief = initial["scenes"][0]["ppt_card_brief"]
    assert "为视频片段" not in brief["source_text"]
    assert "第二主播" not in " ".join(brief["items"])

    saved = workbench_mod.update_scene_ppt_card_brief(project, "scene-a", {
        "title": "数字人正在成为商业角色",
        "takeaway": "角色、内容和服务能力正在同步成熟。",
        "items": ["直播与讲解可持续承接", "品牌服务可以标准化复用", "商业价值来自稳定运营"],
        "card_type": "headline_metrics", "theme": "tech_neon",
    })
    scene = saved["scenes"][0]
    assert scene["ppt_card_brief"]["status"] == "saved"
    queued = workbench_mod.start_scene_ppt_card_generation(project, "scene-a", {"confirmed": True})
    spec = queued["scenes"][0]["ppt_card_generation"]["spec"]
    assert spec["title"] == "数字人正在成为商业角色"
    assert spec["items"] == ["直播与讲解可持续承接", "品牌服务可以标准化复用", "商业价值来自稳定运营"]
    assert "为视频片段" not in spec["summary"]


def test_live_subtitle_preview_uses_its_own_canvas_as_the_size_reference():
    """Live captions must use the same short-edge ruler as the ASS render."""
    workspace_root = Path(__file__).resolve().parents[2]
    client = (workspace_root / "backlot" / "ui" / "workbench.js").read_text(encoding="utf-8")
    styles = (workspace_root / "backlot" / "ui" / "workbench.css").read_text(encoding="utf-8")

    assert "scaleCaptionFontToCanvas" in client
    assert "ResizeObserver" in client
    assert "fontSize = `clamp(14px" not in client
    assert "container-type: size" not in styles


def test_avatar_full_preview_subtitles_reuse_scene_review_phrase_cues(projects_root):
    project = make_project(projects_root)
    state = workbench_mod.bootstrap_workbench(project)
    first, second = state["scenes"]
    first["review_preview"] = {
        "status": "ready",
        "caption_cues": [
            {"start_seconds": 0.0, "end_seconds": 1.5, "text": "\u7b2c\u4e00\u53e5\u3002"},
            {"start_seconds": 1.5, "end_seconds": 4.0, "text": "\u7b2c\u4e8c\u53e5\u3002"},
        ],
    }
    # Keyframe captions are image-review annotations and must never replace
    # the phrase timeline used by the actual scene player.
    first["keyframe_review"] = {
        "status": "approved",
        "timeline": [{
            "relative_start_seconds": 0.0,
            "relative_end_seconds": 4.0,
            "caption_text": "\u8fd9\u662f\u4e00\u6574\u6bb5\u4e0d\u5e94\u8be5\u8fdb\u5165\u6210\u7247\u7684\u5173\u952e\u5e27\u6279\u6ce8\u3002",
        }],
    }
    second["review_preview"] = {
        "status": "ready",
        "caption_cues": [
            {"start_seconds": 0.0, "end_seconds": 5.0, "text": "\u7b2c\u4e09\u53e5\u3002"},
        ],
    }

    subtitle_path = workbench_mod._write_avatar_review_subtitles(project, state)
    content = subtitle_path.read_text(encoding="utf-8")

    assert content.count(" --> ") == 3
    assert "00:00:00,000 --> 00:00:01,500" in content
    assert "00:00:01,500 --> 00:00:04,000" in content
    assert "00:00:04,000 --> 00:00:09,000" in content
    assert "\u7b2c\u4e00\u53e5\u3002" in content
    assert "\u7b2c\u4e09\u53e5\u3002" in content
    assert "\u5173\u952e\u5e27\u6279\u6ce8" not in content


def test_workbench_derives_a_non_persistent_review_model(client, projects_root):
    project = make_project(projects_root)

    response = client.get("/api/project/film/workbench")

    assert response.status_code == 200
    body = response.json()
    assert body["persisted"] is False
    assert [scene["id"] for scene in body["scenes"]] == ["scene-a", "scene-b"]
    assert body["assets"][0]["id"] == "S-001"
    assert body["segments"][0]["start_frame"] == 0
    assert {anchor["kind"] for anchor in body["scenes"][0]["anchors"]} >= {"first_frame", "climax_frame"}
    assert not (project / "artifacts" / "workbench.json").exists()


def test_visual_plan_is_editable_and_forbids_a_second_presenter(client, projects_root):
    make_project(projects_root)
    body = client.post("/api/project/film/workbench/bootstrap").json()
    plan = body["scenes"][0]["visual_plan"]
    assert "不要再生成主播" in plan["prompt"]
    assert "no_presenter" in plan["constraints"]

    saved = client.put("/api/project/film/workbench/scenes/scene-a/visual-plan", json={
        "engine": "hyperframes",
        "prompt": "只展示芯片、数据流和产品轮廓，不出现人物。",
        "structured_spec": {"headline": "芯片进展", "components": ["数据卡片"], "motion": "卡片依次进入", "palette": "深蓝和青色"},
    })
    assert saved.status_code == 200
    saved_plan = saved.json()["scenes"][0]["visual_plan"]
    assert saved_plan["engine"] == "hyperframes"
    assert saved_plan["status"] == "saved"
    assert saved_plan["revision"] == 2


def test_hyperframes_plan_freezes_style_pack_and_only_applies_subtitles_when_requested(client, projects_root):
    make_project(projects_root)
    client.post("/api/project/film/workbench/bootstrap")
    saved = client.put("/api/project/film/workbench/scenes/scene-a/visual-plan", json={
        "engine": "hyperframes",
        "prompt": "用关系图说明数字人和商业价值的关系，不出现第二主播。",
        "structured_spec": {
            "headline": "数字人正在影响现实",
            "components": ["固定形象", "粉丝关系", "宣传参与", "商业价值"],
            "scene_recipe": "relationship_map",
        },
        "style_pack_id": "tech-brief-v1",
        "subtitle_mode": "inherit",
    })
    assert saved.status_code == 200
    scene = saved.json()["scenes"][0]
    plan = scene["visual_plan"]
    assert plan["style_pack"]["id"] == "tech-brief-v1"
    assert plan["style_pack"]["subtitle_mode"] == "inherit"
    assert scene["subtitles"]["template_id"] == "subtitle-default"

    applied = client.put("/api/project/film/workbench/scenes/scene-a/visual-plan", json={
        "engine": "hyperframes",
        "prompt": "用关系图说明数字人和商业价值的关系，不出现第二主播。",
        "structured_spec": {**plan["structured_spec"], "scene_recipe": "relationship_map"},
        "style_pack_id": "tech-brief-v1",
        "subtitle_mode": "apply_recommended",
        "subtitle_apply_scope": "scene",
    })
    assert applied.status_code == 200
    assert applied.json()["scenes"][0]["subtitles"]["template_id"] == "subtitle-tech-brief-v1"


def test_hyperframes_plan_normalizes_layout_variant_to_the_frozen_pack(client, projects_root):
    make_project(projects_root)
    client.post("/api/project/film/workbench/bootstrap")
    saved = client.put("/api/project/film/workbench/scenes/scene-a/visual-plan", json={
        "engine": "hyperframes",
        "prompt": "用因果链说明数字人从形象到商业价值的过程，不出现第二主播。",
        "structured_spec": {
            "headline": "数字人正在影响现实",
            "components": ["固定形象", "粉丝关系", "宣传参与", "商业价值"],
            "scene_recipe": "relationship_map",
            "layout_variant": "causal_chain",
        },
        "style_pack_id": "tech-brief-v1",
    })
    assert saved.status_code == 200
    plan = saved.json()["scenes"][0]["visual_plan"]
    assert plan["structured_spec"]["layout_variant"] == "causal_chain"
    assert plan["structured_spec"]["motion_variant"] == "step_through"
    relationship = next(recipe for recipe in plan["style_pack"]["recipes"] if recipe["id"] == "relationship_map")
    assert {item["id"] for item in relationship["variants"]} >= {"radial_map", "causal_chain", "convergence"}

    invalid = client.put("/api/project/film/workbench/scenes/scene-a/visual-plan", json={
        "engine": "hyperframes",
        "prompt": "保持相同的主体画面目标。",
        "structured_spec": {**plan["structured_spec"], "layout_variant": "not-a-real-layout"},
        "style_pack_id": "tech-brief-v1",
    })
    assert invalid.status_code == 200
    normalized = invalid.json()["scenes"][0]["visual_plan"]["structured_spec"]
    assert normalized["layout_variant"] == "radial_map"
    assert normalized["motion_variant"] == "node_bloom"


def test_workbench_hides_ppt_card_from_new_visual_engine_picker(client, projects_root):
    make_project(projects_root)
    script = client.get("/ui/workbench.js")
    assert script.status_code == 200
    # Legacy data remains supported in the backend, but new users must not be
    # routed to the deliberately downgraded PPT information-card engine.
    design_panel = script.text.split("function renderVisualDesignPanel", 1)[1].split("function visualAssetIdentity", 1)[0]
    assert 'value: "ppt_card", selected: plan.engine' not in design_panel
    assert "科技快报风格包 V1" in design_panel
    assert "画面版式" in design_panel
    assert "HYPERFRAMES_LAYOUT_VARIANTS" in script.text


def test_visual_plan_runtime_switch_clears_stale_motion_candidate(client, projects_root):
    make_project(projects_root)
    state = client.post("/api/project/film/workbench/bootstrap").json()
    scene = state["scenes"][0]
    scene["source_strategy"] = "ai_generated"
    scene["motion_visual_candidate"] = {"asset_id": "S-009", "engine": "remotion", "status": "ready"}
    scene["motion_generation"] = {"status": "completed", "engine": "remotion", "asset_id": "S-009"}
    workbench_mod._save(projects_root / "film", state)

    saved = client.put("/api/project/film/workbench/scenes/scene-a/visual-plan", json={
        "engine": "hyperframes",
        "prompt": "只展示芯片、数据流和产品轮廓，不出现人物。",
    })

    assert saved.status_code == 200
    updated = saved.json()["scenes"][0]
    assert updated["visual_plan"]["engine"] == "hyperframes"
    assert updated["motion_visual_candidate"] is None
    assert updated["motion_generation"]["status"] == "idle"
    assert updated["motion_generation"]["engine"] == "hyperframes"


def test_source_switch_cancels_queued_motion_job(client, projects_root):
    make_project(projects_root)
    state = client.post("/api/project/film/workbench/bootstrap").json()
    scene = state["scenes"][0]
    scene["source_strategy"] = "ai_generated"
    scene["motion_generation"] = {
        "status": "generating", "job_id": "MVG-test", "engine": "remotion", "visual_plan_revision": 1,
    }
    workbench_mod._save(projects_root / "film", state)

    switched = client.patch("/api/project/film/workbench/scenes/scene-a", json={"source_strategy": "web_download"})

    assert switched.status_code == 200
    updated = switched.json()["scenes"][0]
    assert updated["source_strategy"] == "web_download"
    assert updated["motion_generation"]["status"] == "cancelled"


def test_motion_generation_requires_ai_source_strategy(client, projects_root):
    make_project(projects_root)
    state = client.post("/api/project/film/workbench/bootstrap").json()
    scene = state["scenes"][0]
    scene["visual_plan"] = {
        **scene["visual_plan"], "engine": "hyperframes", "prompt": "只展示数据卡片", "status": "saved",
    }
    workbench_mod._save(projects_root / "film", state)

    blocked = client.post("/api/project/film/workbench/scenes/scene-a/motion-visual/jobs", json={})

    assert blocked.status_code == 422
    assert "AI 生成" in blocked.json()["detail"]


def test_visual_timeline_rejects_gaps_and_saves_seamless_blocks(client, projects_root):
    project = make_project(projects_root)
    client.post("/api/project/film/workbench/bootstrap")
    (project / "assets" / "second.png").write_bytes(b"second")
    added = client.post("/api/project/film/workbench/assets", json={
        "name": "第二张图", "type": "image", "source_type": "human_provided", "path": "assets/second.png",
    }).json()["assets"][-1]
    state = client.get("/api/project/film/workbench").json()
    first = state["assets"][0]["id"]

    gap = client.put("/api/project/film/workbench/scenes/scene-a/visual-timeline", json={"blocks": [
        {"start_seconds": 0, "end_seconds": 1.5, "asset_id": first},
        {"start_seconds": 2, "end_seconds": 4, "asset_id": added["id"]},
    ]})
    assert gap.status_code == 422
    assert "空白" in gap.json()["detail"]

    saved = client.put("/api/project/film/workbench/scenes/scene-a/visual-timeline", json={"blocks": [
        {"start_seconds": 0, "end_seconds": 2, "asset_id": first, "source_mode": "project_library"},
        {"start_seconds": 2, "end_seconds": 4, "asset_id": added["id"], "source_mode": "human_provided"},
    ]})
    assert saved.status_code == 200
    blocks = saved.json()["scenes"][0]["visual_timeline"]["blocks"]
    assert [item["id"] for item in blocks] == ["VB-001", "VB-002"]
    assert blocks[0]["end_seconds"] == blocks[1]["start_seconds"] == 2
    assert all(item["usage_id"].startswith("U-") for item in blocks)
    assert len({item["usage_id"] for item in blocks}) == 2


def test_smart_visual_ranges_are_balanced_and_never_leave_a_tiny_tail():
    ten_seconds = workbench_mod._balanced_visual_ranges(0, 10.28, "auto")
    fifteen_seconds_auto = workbench_mod._balanced_visual_ranges(0, 15, "auto")
    fifteen_seconds = workbench_mod._balanced_visual_ranges(0, 15, "video")
    short = workbench_mod._balanced_visual_ranges(0, 2.1, "video")

    assert ten_seconds == [(0.0, 5.14), (5.14, 10.28)]
    assert fifteen_seconds_auto == [(0.0, 7.5), (7.5, 15.0)]
    assert fifteen_seconds == [(0.0, 5.0), (5.0, 10.0), (10.0, 15.0)]
    assert short == [(0.0, 2.1)]
    for ranges in (ten_seconds, fifteen_seconds_auto, fifteen_seconds, short):
        assert ranges[0][0] == 0
        assert all(left[1] == right[0] for left, right in zip(ranges, ranges[1:]))


def test_daily_news_visual_ranges_cut_roughly_every_three_seconds():
    nine_seconds = workbench_mod._balanced_visual_ranges(0, 9, "daily_news")
    six_point_five = workbench_mod._balanced_visual_ranges(0, 6.58, "daily_news")

    assert nine_seconds == [(0.0, 3.0), (3.0, 6.0), (6.0, 9.0)]
    assert six_point_five == [(0.0, 3.29), (3.29, 6.58)]
    assert max(end - start for start, end in nine_seconds + six_point_five) <= 3.5


def test_visual_batch_preview_selects_only_scenes_without_complete_visual(projects_root):
    project = make_project(projects_root)
    state = workbench_mod.bootstrap_workbench(project)
    # scene-a inherits S-001 from the source manifest; scene-b has no visual.
    preview = workbench_mod.preview_visual_batch_plan(project, {
        "selection_mode": "missing", "profile": "auto",
    })

    assert preview["scene_ids"] == ["scene-b"]
    assert preview["scene_count"] == 1
    assert preview["total_slots"] == 1
    assert preview["items"][0]["blocks"][0]["start_seconds"] == 0
    assert preview["items"][0]["blocks"][-1]["end_seconds"] == 5


def test_avatar_presenter_and_supporting_visual_have_independent_readiness(projects_root, monkeypatch):
    project = make_project(projects_root)
    state = workbench_mod.bootstrap_workbench(project)
    state["project"]["pipeline_type"] = "avatar-spokesperson"
    scene_a = next(item for item in state["scenes"] if item["id"] == "scene-a")
    scene_b = next(item for item in state["scenes"] if item["id"] == "scene-b")
    for scene in (scene_a, scene_b):
        scene.setdefault("presenter", {}).update({
            "treatment": "fullscreen",
            "source_path": "assets/avatar/presenter.mp4",
        })
    workbench_mod._save(project, state)

    assert workbench_mod._scene_has_presenter_media(state, scene_b) is True
    assert workbench_mod._scene_has_supporting_visual(state, scene_b) is False
    # A full-screen presenter is technically renderable, but it is still
    # missing independent supporting content for batch-fill purposes.
    assert workbench_mod._scene_is_renderable(state, scene_b) is True

    preview = workbench_mod.preview_visual_batch_plan(project, {
        "selection_mode": "missing",
        "operation_mode": "fill_missing",
        "profile": "auto",
        "planning_mode": "rule_mix",
    })

    assert preview["scene_ids"] == ["scene-b"]
    item = preview["items"][0]
    assert item["has_presenter_media"] is True
    assert item["has_supporting_visual"] is False
    assert item["is_renderable"] is True
    assert any(block["status"] == "planned" for block in item["blocks"])

    monkeypatch.setenv("PEXELS_API_KEY", "test-key")
    started = workbench_mod.start_visual_batch_generation(project, {
        "confirmed": True,
        "selection_mode": "custom",
        "scene_ids": ["scene-b"],
        "operation_mode": "fill_missing",
        "profile": "auto",
        "planning_mode": "rule_mix",
        "reviewed_plan": {
            "plan_id": preview["plan_id"],
            "planner": preview["planner"],
            "items": preview["items"],
        },
    })
    batch = started["automation"]["visual_batch"]
    assert batch["scene_ids"] == ["scene-b"]
    assert batch["total_slots"] >= 1


def test_avatar_picture_in_picture_requires_both_presenter_and_supporting_visual(projects_root):
    project = make_project(projects_root)
    state = workbench_mod.bootstrap_workbench(project)
    state["project"]["pipeline_type"] = "avatar-spokesperson"
    scene_b = next(item for item in state["scenes"] if item["id"] == "scene-b")
    scene_b.setdefault("presenter", {}).update({
        "treatment": "pip_top_left",
        "source_path": "assets/avatar/presenter.mp4",
    })

    assert workbench_mod._scene_has_presenter_media(state, scene_b) is True
    assert workbench_mod._scene_has_supporting_visual(state, scene_b) is False
    assert workbench_mod._scene_is_renderable(state, scene_b) is False


def test_visual_batch_smart_mix_is_planned_before_generation(projects_root):
    project = make_project(projects_root)
    state = workbench_mod.bootstrap_workbench(project)
    scene = next(item for item in state["scenes"] if item["id"] == "scene-b")
    scene["end_seconds"] = scene["start_seconds"] + 10.28
    workbench_mod._save(project, state)

    preview = workbench_mod.preview_visual_batch_plan(project, {
        "selection_mode": "custom", "scene_ids": ["scene-b"],
        "operation_mode": "replace_selected", "profile": "auto",
        "mix_strategy": "balanced", "image_source": "openai_image",
        "content_rules": ["no_frontal_face"],
    })

    blocks = [item for item in preview["items"][0]["blocks"] if item["status"] == "planned"]
    assert [(item["media_kind"], item["source_mode"]) for item in blocks] == [
        ("video", "web_download"), ("image", "openai_image"),
    ]
    assert preview["video_slots"] == 1
    assert preview["image_slots"] == 1
    assert preview["ai_image_slots"] == 1
    assert preview["policy"]["person_policy"] == "balanced"
    assert preview["policy"]["candidate_limit"] == 6


def test_ai_director_visual_plan_routes_slots_and_keeps_the_plan_editable(projects_root, monkeypatch):
    project = make_project(projects_root)
    state = workbench_mod.bootstrap_workbench(project)
    scene = next(item for item in state["scenes"] if item["id"] == "scene-b")
    scene["end_seconds"] = scene["start_seconds"] + 10.28
    workbench_mod._save(project, state)
    captured = {}
    monkeypatch.setattr(workbench_mod, "read_text_ai_config", lambda: {"configured": True})

    def fake_plan(context, *, allow_missing=False):
        captured.update(context)
        slots = context["scenes"][0]["slots"]
        return {
            "model": "mock-director", "fingerprint": "ai-plan", "generated_at": "now",
            "summary": "第一格需要真实运动，第二格展示抽象关系",
            "blocks": [
                {"scene_id": "scene-b", "block_id": slots[0]["block_id"], "route": "stock_video", "visual_intent": "机器人装配", "reason": "真实过程", "confidence": .9, "search_query": "industrial robot assembly line", "scene_recipe": "process", "fallback_route": "stock_image"},
                {"scene_id": "scene-b", "block_id": slots[1]["block_id"], "route": "hyperframes", "visual_intent": "价值关系图", "reason": "抽象关系", "confidence": .8, "search_query": "", "scene_recipe": "relationship_map", "fallback_route": "stock_video"},
            ],
        }

    monkeypatch.setattr(workbench_mod, "plan_visual_routes", fake_plan)
    preview = workbench_mod.preview_visual_batch_plan(project, {
        "selection_mode": "custom", "scene_ids": ["scene-b"],
        "operation_mode": "replace_selected", "profile": "auto",
        "planning_mode": "ai_director", "ai_planning_confirmed": True,
    })

    planned = [block for block in preview["items"][0]["blocks"] if block["status"] == "planned"]
    assert captured["caption_owner"] == "Haike Video 独立字幕层"
    assert captured["preferences"]["allowed_routes"] == ["hyperframes", "stock_video"]
    assert captured["preferences"]["primary_image_policy"] == "manual_only"
    assert [block["route"] for block in planned] == ["stock_video", "hyperframes"]
    assert planned[0]["query"] == "industrial robot assembly line"
    assert preview["route_counts"] == {"stock_video": 1, "stock_image": 0, "ai_image": 0, "hyperframes": 1}
    assert preview["planner"]["mode"] == "ai_director"


def test_ai_visual_balance_uses_duration_and_removes_default_image_routes():
    items = []
    for index in range(22):
        route = "stock_image" if index == 0 else "hyperframes"
        block = {
            "id": f"VB-{index + 1:03d}",
            "status": "planned",
            "start_seconds": index * 5.0,
            "end_seconds": (index + 1) * 5.0,
            "route": route,
            "source_mode": "web_download" if route == "stock_image" else "hyperframes",
            "media_kind": "image" if route == "stock_image" else "video",
            "scene_recipe": "relationship_map" if index % 3 else "headline_statement",
            "slot_text": f"测试台词 {index + 1}",
            "reason": "模拟 AI 推荐",
            "confidence": .55 + (index % 4) * .1,
            "graphic_copy": {"headline": f"标题 {index + 1}", "scene_goal": "解释当前信息"},
        }
        items.append({"scene_id": f"scene-{index + 1:02d}", "blocks": [block]})

    policy = workbench_mod._visual_batch_policy({
        "planning_mode": "ai_director",
        "mix_strategy": "balanced",
    })
    result = workbench_mod._rebalance_ai_visual_routes(items, policy)
    planned = workbench_mod._planned_visual_entries(items)

    assert {block["route"] for block in planned} <= {"stock_video", "hyperframes"}
    assert result["normalized_image_slots"] == 1
    assert result["adjusted_slots"] >= 1
    assert .60 <= result["duration_shares"]["stock_video"] <= .70
    assert .30 <= result["duration_shares"]["hyperframes"] <= .40
    assert result["total_planned_duration_seconds"] == 110.0
    assert round(sum(result["route_duration_seconds"].values()), 3) == 110.0


def test_hyperframes_layout_balancer_removes_adjacent_and_full_plan_overuse():
    items = [{
        "scene_id": "scene-a",
        "blocks": [
            {"id": "VB-001", "status": "planned", "route": "hyperframes", "scene_recipe": "relationship_map", "layout_variant": "radial_map"},
            {"id": "VB-002", "status": "planned", "route": "hyperframes", "scene_recipe": "relationship_map", "layout_variant": "radial_map"},
            {"id": "VB-003", "status": "planned", "route": "stock_video", "scene_recipe": "relationship_map"},
            {"id": "VB-004", "status": "planned", "route": "hyperframes", "scene_recipe": "relationship_map", "layout_variant": "radial_map"},
        ],
    }]

    outcome = workbench_mod._rebalance_hyperframes_layout_variants(items)
    blocks = items[0]["blocks"]
    assert blocks[0]["layout_variant"] in {"radial_map", "causal_chain", "convergence"}
    assert blocks[1]["layout_variant"] in {"causal_chain", "convergence"}
    # The stock clip breaks adjacency, but the full-plan pass still spreads
    # three relationship-map cards across all available layouts.
    assert len({blocks[0]["layout_variant"], blocks[1]["layout_variant"], blocks[3]["layout_variant"]}) == 3
    assert outcome["layout_adjusted_slots"] == 2
    assert all(block.get("motion_variant") for block in (blocks[0], blocks[1], blocks[3]))


def test_hyperframes_layout_balancer_preserves_a_user_locked_choice():
    items = [{
        "scene_id": "scene-a",
        "blocks": [
            {"id": "VB-001", "status": "planned", "route": "hyperframes", "scene_recipe": "comparison", "layout_variant": "balance_axis", "layout_variant_locked": True},
            {"id": "VB-002", "status": "planned", "route": "hyperframes", "scene_recipe": "comparison", "layout_variant": "balance_axis"},
        ],
    }]
    workbench_mod._rebalance_hyperframes_layout_variants(items)
    assert items[0]["blocks"][0]["layout_variant"] == "balance_axis"
    assert items[0]["blocks"][1]["layout_variant"] != "balance_axis"


def test_ai_visual_balance_presets_allow_different_video_motion_ranges():
    def build(route: str) -> list[dict]:
        return [{
            "scene_id": "scene",
            "blocks": [{
                "id": f"VB-{index + 1:03d}", "status": "planned",
                "start_seconds": index * 5.0, "end_seconds": (index + 1) * 5.0,
                "route": route, "source_mode": "hyperframes" if route == "hyperframes" else "web_download",
                "media_kind": "video", "scene_recipe": "headline_statement",
                "slot_text": "实体产品与抽象关系", "confidence": .5,
                "graphic_copy": {"headline": "测试", "scene_goal": "测试"},
            } for index in range(20)],
        }]

    video_first = build("hyperframes")
    motion_first = build("stock_video")
    video_result = workbench_mod._rebalance_ai_visual_routes(video_first, workbench_mod._visual_batch_policy({
        "planning_mode": "ai_director", "mix_strategy": "video_first",
    }))
    motion_result = workbench_mod._rebalance_ai_visual_routes(motion_first, workbench_mod._visual_batch_policy({
        "planning_mode": "ai_director", "mix_strategy": "motion_first",
    }))

    assert .70 <= video_result["duration_shares"]["stock_video"] <= .80
    assert .45 <= motion_result["duration_shares"]["stock_video"] <= .60


def test_visual_ai_planner_splits_large_episode_without_splitting_scenes(monkeypatch):
    calls = []

    def fake_plan(context, *, allow_missing=False):
        calls.append(context)
        blocks = []
        for scene in context["scenes"]:
            for slot in scene["slots"]:
                blocks.append({
                    "scene_id": scene["scene_id"],
                    "block_id": slot["block_id"],
                    "route": "stock_video",
                })
        return {
            "summary": f"第 {len(calls)} 批",
            "blocks": blocks,
            "model": "mock-planner",
        }

    monkeypatch.setattr(workbench_mod, "plan_visual_routes", fake_plan)
    scenes = []
    for scene_index in range(1, 8):
        scenes.append({
            "scene_id": f"scene-{scene_index}",
            "slots": [
                {"block_id": f"VB-{scene_index:02d}-{slot_index}", "start_seconds": slot_index, "end_seconds": slot_index + 1}
                for slot_index in range(3)
            ],
        })

    result = workbench_mod._plan_visual_routes_batched({
        "task": "visual_route_planning",
        "scenes": scenes,
    }, max_slots_per_request=6)

    assert result["batch_count"] == 4
    assert [sum(len(scene["slots"]) for scene in call["scenes"]) for call in calls] == [6, 6, 6, 3]
    assert len(result["blocks"]) == 21
    assert len({block["scene_id"] for block in result["blocks"]}) == 7


def test_visual_ai_planner_repairs_only_missing_slots_and_never_stops(monkeypatch):
    calls = []

    def fake_plan(context, *, allow_missing=False):
        calls.append(context)
        slots = [
            (scene["scene_id"], slot)
            for scene in context["scenes"] for slot in scene["slots"]
        ]
        # The first response deliberately drops the final slot. The repair
        # request receives only that slot and returns it successfully.
        selected = slots[:-1] if len(calls) == 1 else slots
        missing = slots[-1:] if len(calls) == 1 else []
        return {
            "summary": "模拟漏项",
            "model": "mock-planner",
            "blocks": [{
                "scene_id": scene_id,
                "block_id": slot["block_id"],
                "route": "stock_video",
            } for scene_id, slot in selected],
            "missing": [{"scene_id": scene_id, "block_id": slot["block_id"]} for scene_id, slot in missing],
        }

    monkeypatch.setattr(workbench_mod, "plan_visual_routes", fake_plan)
    result = workbench_mod._plan_visual_routes_batched({
        "task": "visual_route_planning",
        "scenes": [{
            "scene_id": "section-008",
            "slots": [
                {"block_id": "VB-001", "start_seconds": 0, "end_seconds": 4, "slot_text": "框架全部开源免费"},
                {"block_id": "VB-002", "start_seconds": 4, "end_seconds": 8, "slot_text": "快速搭建自主智能体"},
            ],
        }],
    })

    assert len(calls) == 2
    assert [slot["block_id"] for slot in calls[1]["scenes"][0]["slots"]] == ["VB-002"]
    assert {block["block_id"] for block in result["blocks"]} == {"VB-001", "VB-002"}
    assert result["repaired_slots"] == 1
    assert result["fallback_slots"] == 0


def test_visual_ai_planner_uses_rule_fallback_when_repair_still_omits_slot(monkeypatch):
    def fake_plan(context, *, allow_missing=False):
        scene = context["scenes"][0]
        slot = scene["slots"][0]
        return {
            "summary": "仍然漏项",
            "model": "mock-planner",
            "blocks": [],
            "missing": [{"scene_id": scene["scene_id"], "block_id": slot["block_id"]}],
        }

    monkeypatch.setattr(workbench_mod, "plan_visual_routes", fake_plan)
    result = workbench_mod._plan_visual_routes_batched({
        "task": "visual_route_planning",
        "scenes": [{
            "scene_id": "section-008",
            "slots": [{
                "block_id": "VB-002", "start_seconds": 3.93, "end_seconds": 7.86,
                "slot_text": "不用改动源码，就能快速搭建属于自己的自主智能体。",
            }],
        }],
    })

    assert result["repaired_slots"] == 0
    assert result["fallback_slots"] == 1
    assert result["blocks"][0]["block_id"] == "VB-002"
    assert result["blocks"][0]["reason"].startswith("AI 漏项后由确定性规则自动补齐")


def test_reviewed_visual_plan_is_frozen_as_a_project_contract(projects_root, monkeypatch):
    project = make_project(projects_root)
    workbench_mod.bootstrap_workbench(project)
    preview = workbench_mod.preview_visual_batch_plan(project, {
        "selection_mode": "custom", "scene_ids": ["scene-b"],
        "operation_mode": "replace_selected", "profile": "video",
    })
    block = next(block for block in preview["items"][0]["blocks"] if block["status"] == "planned")
    workbench_mod._apply_visual_route(block, "hyperframes")
    block.update({"visual_intent": "价值关系图", "reason": "人工改为动态图形", "scene_recipe": "relationship_map"})

    started = workbench_mod.start_visual_batch_generation(project, {
        "confirmed": True, "selection_mode": "custom", "scene_ids": ["scene-b"],
        "operation_mode": "replace_selected", "profile": "video", "reviewed_plan": preview,
    })

    batch = started["automation"]["visual_batch"]
    assert batch["planning_mode"] == "rule_mix"
    assert batch["items"][0]["route"] == "hyperframes"
    contract = project / batch["contract_path"]
    assert contract.is_file()
    saved = json.loads(contract.read_text(encoding="utf-8"))
    assert saved["items"][0]["route"] == "hyperframes"


def test_hyperframes_batch_slot_does_not_silently_fall_back_to_network(projects_root, monkeypatch):
    project = make_project(projects_root)
    workbench_mod.bootstrap_workbench(project)
    preview = workbench_mod.preview_visual_batch_plan(project, {
        "selection_mode": "custom", "scene_ids": ["scene-b"],
        "operation_mode": "replace_selected", "profile": "video",
    })
    block = next(block for block in preview["items"][0]["blocks"] if block["status"] == "planned")
    workbench_mod._apply_visual_route(block, "hyperframes")
    block["visual_intent"] = "抽象关系图"
    started = workbench_mod.start_visual_batch_generation(project, {
        "confirmed": True, "selection_mode": "custom", "scene_ids": ["scene-b"],
        "operation_mode": "replace_selected", "profile": "video", "reviewed_plan": preview,
    })

    def fake_hyperframes(project_dir, state, scene, block, item, duration):
        output = project_dir / "assets" / "video" / "hyperframes" / "mock.mp4"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"mock")
        return workbench_mod._append_asset(project_dir, state, {
            "name": "Mock HyperFrames", "type": "video", "source_type": "local_generated",
            "path": str(output), "duration_seconds": duration, "resolution": "1080x1920",
            "provider": "HyperFrames", "source_tool": "hyperframes_compose", "license": "test",
        })

    monkeypatch.setattr(workbench_mod, "_generate_hyperframes_visual_block", fake_hyperframes)
    monkeypatch.setattr(workbench_mod.PexelsVideo, "execute", lambda *args: pytest.fail("不应调用 Pexels"))
    completed = workbench_mod.generate_visual_batch(project, started["automation"]["visual_batch"]["job_id"])
    item = completed["automation"]["visual_batch"]["items"][0]
    assert item["status"] == "completed"
    assert item["tool"] == "hyperframes_compose"
    assert item["route"] == "hyperframes"


def test_person_screening_policy_allows_context_people_but_blocks_takeover_shots():
    contextual = {
        "max_face_ratio": .015, "max_person_ratio": .18,
        "face_centered": False, "person_centered": True,
        "person_frame_hits": 3,
    }
    takeover = {
        "max_face_ratio": .08, "max_person_ratio": .58,
        "face_centered": True, "person_centered": True,
        "person_frame_hits": 3,
    }

    assert workbench_mod._person_screening_decision(contextual, "balanced")["status"] == "passed"
    assert workbench_mod._person_screening_decision(takeover, "balanced")["status"] == "rejected"
    assert workbench_mod._person_screening_decision(contextual, "strict")["status"] == "rejected"
    assert workbench_mod._person_screening_decision(takeover, "relaxed")["status"] == "rejected"


def test_local_person_screening_result_is_json_serializable(tmp_path):
    pytest.importorskip("cv2")
    image_path = tmp_path / "screening.jpg"
    Image.new("RGB", (320, 180), "navy").save(image_path)

    result = workbench_mod._screen_visual_candidate(image_path, "image", [], "balanced")

    json.dumps(result, ensure_ascii=False)
    assert isinstance(result["metrics"]["face_centered"], bool)
    assert isinstance(result["metrics"]["person_centered"], bool)


def test_stock_search_plan_uses_concrete_topic_and_distinct_slot_roles():
    scene = {
        "id": "scene-robot",
        "title": "宇树科技与机器人量产",
        "description": "机器人从舞台表演走向工厂量产和产业应用",
        "shot_intent": "展示机器狗与自动化生产线",
    }
    strategy = {
        "theme": "AI 与高新科技",
        "preferred_keywords": ["机器人", "机械臂", "自动化生产线"],
        "cautious_topics": ["主播", "正面人物肖像"],
        "query_overrides": {},
    }

    first = workbench_mod._stock_search_plan_for_block(
        scene, surrounding_context="", slot_index=1, block_id="VB-001",
        strategy=strategy, rules=["no_presenter_studio"], person_policy="balanced",
    )
    second = workbench_mod._stock_search_plan_for_block(
        scene, surrounding_context="", slot_index=2, block_id="VB-002",
        strategy=strategy, rules=["no_presenter_studio"], person_policy="balanced",
    )

    assert first["topic"] == "机器人产业"
    assert first["role_label"] == "建立镜头"
    assert second["role_label"] == "过程镜头"
    assert first["query"] != second["query"]
    assert len(first["query_ladder"]) == 3
    assert [item["level"] for item in first["query_ladder"]] == ["精确检索", "行业检索", "兜底检索"]
    assert all("no people" not in item["query"] and "no presenter" not in item["query"] for item in first["query_ladder"])


def test_visual_batch_preview_exposes_editable_query_ladders(projects_root):
    project = make_project(projects_root)
    state = workbench_mod.bootstrap_workbench(project)
    scene = next(item for item in state["scenes"] if item["id"] == "scene-b")
    scene.update({
        "title": "宇树科技机器人",
        "description": "机器人进入工厂量产和产业应用",
        "end_seconds": scene["start_seconds"] + 10.28,
    })
    workbench_mod._save(project, state)

    preview = workbench_mod.preview_visual_batch_plan(project, {
        "selection_mode": "custom", "scene_ids": ["scene-b"],
        "operation_mode": "replace_selected", "profile": "auto",
        "search_theme": "AI 与高新科技",
        "preferred_keywords": "机器人、机械臂、自动化生产线",
        "cautious_topics": "主播、正面人物肖像",
        "query_overrides": {"scene-b": {"VB-001": "semiconductor wafer manufacturing macro"}},
    })

    planned = [item for item in preview["items"][0]["blocks"] if item["status"] == "planned"]
    assert preview["search_strategy"]["theme"] == "AI 与高新科技"
    assert preview["search_strategy"]["source"] == "custom"
    assert preview["search_strategy"]["preferred_keywords"] == ["机器人", "机械臂", "自动化生产线"]
    assert len(planned) == 2
    assert all(len(item["query_ladder"]) == 3 for item in planned)
    assert planned[0]["query"] != planned[1]["query"]
    assert planned[0]["query"] == "semiconductor wafer manufacturing macro"
    assert planned[0]["query_source"] == "manual"


def test_pexels_candidate_search_advances_to_next_query_when_exact_has_no_result(projects_root, monkeypatch):
    project = make_project(projects_root)
    state = workbench_mod.bootstrap_workbench(project)
    scene = next(item for item in state["scenes"] if item["id"] == "scene-b")
    block = {"id": "VB-001"}
    calls: list[dict] = []

    class FakeTool:
        def execute(self, inputs):
            calls.append(dict(inputs))
            if len(calls) == 1:
                return SimpleNamespace(success=False, artifacts=[], data={}, error="No videos found")
            output = Path(inputs["output_path"])
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b"candidate")
            return SimpleNamespace(success=True, artifacts=[str(output)], data={
                "video_id": "industry-result", "pexels_url": "https://www.pexels.com/video/2/",
            }, error=None)

    monkeypatch.setattr(workbench_mod, "_screen_visual_candidate", lambda *args: {
        "status": "passed", "score": 90, "reasons": [], "metrics": {}, "mode": "test",
    })
    item = {
        "candidate_limit": 4,
        "query_ladder": [
            {"level": "精确检索", "query": "quadruped robot factory"},
            {"level": "行业检索", "query": "industrial robot assembly line"},
            {"level": "兜底检索", "query": "automated manufacturing machinery"},
        ],
        "rejected_candidates": [],
    }

    result, path, screening = workbench_mod._find_screened_pexels_candidate(
        project, state, item, scene, block,
        media_kind="video", query="quadruped robot factory", orientation="portrait", page=1,
        target_duration=5, content_rules=[], person_policy="balanced",
        used_provider_ids=set(), tool=FakeTool(),
    )

    assert result.success and path and screening["status"] == "passed"
    assert [call["query"] for call in calls] == ["quadruped robot factory", "industrial robot assembly line"]
    assert item["accepted_candidate"]["query_level"] == "行业检索"
    assert item["query"] == "industrial robot assembly line"


def test_visual_batch_fill_preserves_existing_but_replace_replans_unlocked(projects_root):
    project = make_project(projects_root)
    state = workbench_mod.bootstrap_workbench(project)
    scene = next(item for item in state["scenes"] if item["id"] == "scene-a")
    existing_asset = scene["visual_timeline"]["blocks"][0]["asset_id"]
    workbench_mod._save(project, state)

    fill = workbench_mod.preview_visual_batch_plan(project, {
        "selection_mode": "custom", "scene_ids": ["scene-a"],
        "operation_mode": "fill_missing", "profile": "auto",
    })
    replace = workbench_mod.preview_visual_batch_plan(project, {
        "selection_mode": "custom", "scene_ids": ["scene-a"],
        "operation_mode": "replace_selected", "profile": "auto",
    })

    assert fill["total_slots"] == 0
    assert fill["items"][0]["blocks"][0]["asset_id"] == existing_asset
    assert replace["total_slots"] >= 1
    assert all(item.get("asset_id") is None for item in replace["items"][0]["blocks"])


def test_visual_batch_openai_slots_require_explicit_cost_confirmation(projects_root, monkeypatch):
    project = make_project(projects_root)
    monkeypatch.setenv("PEXELS_API_KEY", "test-key")
    workbench_mod.bootstrap_workbench(project)

    with pytest.raises(workbench_mod.WorkbenchError, match="可能产生的费用"):
        workbench_mod.start_visual_batch_generation(project, {
            "confirmed": True, "selection_mode": "custom", "scene_ids": ["scene-b"],
            "operation_mode": "replace_selected", "profile": "image",
            "image_source": "openai_image",
        })


@pytest.mark.skipif(not _ffmpeg_available(), reason="ffmpeg is required for mixed visual verification")
def test_visual_batch_executes_declared_video_and_openai_image_routes(projects_root, monkeypatch):
    project = make_project(projects_root)
    project_meta = json.loads((project / "project.json").read_text(encoding="utf-8"))
    project_meta["render_profile"] = {"width": 320, "height": 180}
    write_json(project / "project.json", project_meta)
    monkeypatch.setenv("PEXELS_API_KEY", "test-key")
    ffmpeg = _ffmpeg_available()
    video_calls: list[dict] = []
    image_calls: list[dict] = []

    def fake_video(self, inputs):
        video_calls.append(dict(inputs))
        output = Path(inputs["output_path"])
        output.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run([
            ffmpeg, "-y", "-f", "lavfi", "-i", "color=c=blue:s=320x180:r=30:d=7",
            "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(output),
        ], check=True, capture_output=True)
        return SimpleNamespace(success=True, artifacts=[str(output)], data={
            "duration_seconds": 7, "width": 320, "height": 180,
            "video_id": "mixed-video-1", "license": "Pexels License",
            "pexels_url": "https://www.pexels.com/video/1/",
        }, error=None)

    def fake_image(self, inputs):
        image_calls.append(dict(inputs))
        output = Path(inputs["output_path"])
        output.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (320, 180), "navy").save(output)
        return SimpleNamespace(success=True, artifacts=[str(output)], data={}, error=None)

    monkeypatch.setattr(workbench_mod.PexelsVideo, "execute", fake_video)
    monkeypatch.setattr(workbench_mod.OpenAIImage, "execute", fake_image)
    state = workbench_mod.bootstrap_workbench(project)
    scene = next(item for item in state["scenes"] if item["id"] == "scene-b")
    scene["end_seconds"] = 14.28
    workbench_mod._save(project, state)

    workbench_mod.start_visual_batch_generation(project, {
        "confirmed": True, "ai_generation_confirmed": True,
        "selection_mode": "custom", "scene_ids": ["scene-b"],
        "operation_mode": "replace_selected", "profile": "auto",
        "mix_strategy": "balanced", "image_source": "openai_image",
        "content_rules": [],
    })
    completed = workbench_mod.generate_visual_batch(project)

    blocks = next(item for item in completed["scenes"] if item["id"] == "scene-b")["visual_timeline"]["blocks"]
    assert [item["media_kind"] for item in blocks] == ["video", "image"]
    assert all(item["status"] == "ready" for item in blocks)
    assert len(video_calls) == 1
    assert len(image_calls) == 1
    assert image_calls[0]["model"] == "gpt-image-2"


@pytest.mark.skipif(not _ffmpeg_available(), reason="ffmpeg is required for candidate retry verification")
def test_visual_batch_rejects_candidates_then_automatically_tries_the_next(projects_root, monkeypatch):
    project = make_project(projects_root)
    project_meta = json.loads((project / "project.json").read_text(encoding="utf-8"))
    project_meta["render_profile"] = {"width": 320, "height": 180}
    write_json(project / "project.json", project_meta)
    monkeypatch.setenv("PEXELS_API_KEY", "test-key")
    ffmpeg = _ffmpeg_available()
    calls: list[dict] = []
    screenings: list[int] = []

    def fake_video(self, inputs):
        calls.append(dict(inputs))
        output = Path(inputs["output_path"])
        output.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run([
            ffmpeg, "-y", "-f", "lavfi", "-i", "color=c=navy:s=320x180:r=30:d=7",
            "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(output),
        ], check=True, capture_output=True)
        provider_id = f"candidate-{len(calls)}"
        return SimpleNamespace(success=True, artifacts=[str(output)], data={
            "duration_seconds": 7, "width": 320, "height": 180,
            "video_id": provider_id, "license": "Pexels License",
            "pexels_url": f"https://www.pexels.com/video/{provider_id}/",
        }, error=None)

    def fake_screen(path, media_kind, rules, person_policy):
        screenings.append(len(screenings) + 1)
        rejected = len(screenings) <= 2
        return {
            "status": "rejected" if rejected else "passed", "mode": "local_detector",
            "person_policy": person_policy, "score": 20 if rejected else 88,
            "reasons": ["人物占据画面主体"] if rejected else [], "metrics": {},
        }

    monkeypatch.setattr(workbench_mod.PexelsVideo, "execute", fake_video)
    monkeypatch.setattr(workbench_mod, "_screen_visual_candidate", fake_screen)
    state = workbench_mod.bootstrap_workbench(project)
    scene = next(item for item in state["scenes"] if item["id"] == "scene-b")
    scene["end_seconds"] = 9
    workbench_mod._save(project, state)
    workbench_mod.start_visual_batch_generation(project, {
        "confirmed": True, "selection_mode": "custom", "scene_ids": ["scene-b"],
        "operation_mode": "replace_selected", "profile": "video",
        "person_policy": "balanced", "candidate_limit": 4,
    })

    completed = workbench_mod.generate_visual_batch(project)
    item = completed["automation"]["visual_batch"]["items"][0]
    block = next(scene for scene in completed["scenes"] if scene["id"] == "scene-b")["visual_timeline"]["blocks"][0]
    assert item["status"] == "completed"
    assert item["candidate_attempt"] == 3
    assert len(item["rejected_candidates"]) == 2
    assert block["status"] == "ready"
    assert len(calls) == 3
    assert calls[0]["exclude_video_ids"] == []
    assert "candidate-1" in calls[1]["exclude_video_ids"]
    assert {"candidate-1", "candidate-2"}.issubset(set(calls[2]["exclude_video_ids"]))


def test_presenter_layout_batch_copy_does_not_copy_character_media(projects_root):
    project = make_project(projects_root)
    state = workbench_mod.bootstrap_workbench(project)
    state["project"]["pipeline_type"] = "avatar-spokesperson"
    first, second = state["scenes"]
    first["presenter"].update({
        "treatment": "custom", "layout_template_id": "pip_top_left",
        "layout_override": {"x": .12, "y": .09, "width": .31},
        "crop_bottom": .12,
        "turn_id": "T001", "source_path": "assets/avatar/yaya.mp4", "asset_id": "AV-YAYA",
    })
    second["presenter"].update({
        "treatment": "pip_top_left", "layout_template_id": "pip_top_right",
        "layout_override": None,
        "turn_id": "T002", "source_path": "assets/avatar/mengmeng.mp4", "asset_id": "AV-MENGMENG",
    })

    changed = workbench_mod._copy_presenter_layout_to_scenes(state, first["id"], [second["id"]])

    assert changed == 1
    assert second["presenter"]["treatment"] == "custom"
    assert second["presenter"]["layout_override"] == {"x": .12, "y": .09, "width": .31}
    assert second["presenter"]["crop_bottom"] == .12
    assert second["presenter"]["turn_id"] == "T002"
    assert second["presenter"]["source_path"] == "assets/avatar/mengmeng.mp4"


def test_apply_selected_presenter_layout_only_changes_presentation(projects_root):
    project = make_project(projects_root)
    state = workbench_mod.bootstrap_workbench(project)
    state["project"]["pipeline_type"] = workbench_mod.AVATAR_PIPELINE
    first, second = state["scenes"]
    first["presenter"].update({
        "treatment": "custom", "layout_template_id": "pip_top_left",
        "layout_override": {"x": .12, "y": .09, "width": .31}, "crop_bottom": .13,
    })
    second["presenter"].update({
        "treatment": "pip_top_left", "layout_template_id": "pip_top_left",
        "layout_override": None, "crop_bottom": 0.0, "source_path": "assets/avatar/mengmeng.mp4", "asset_id": "AV-MENGMENG",
    })
    original_timeline = {"version": 2, "blocks": [{"id": "VB-001", "asset_id": "asset-original"}]}
    second["visual_timeline"] = original_timeline
    workbench_mod._save(project, state)

    updated = workbench_mod.apply_presenter_layout_to_selected_scenes(project, {
        "source_scene_id": first["id"], "target_scene_ids": [first["id"], second["id"]],
    })
    copied = next(scene for scene in updated["scenes"] if scene["id"] == second["id"])

    assert copied["presenter"]["layout_override"] == {"x": .12, "y": .09, "width": .31}
    assert copied["presenter"]["crop_bottom"] == .13
    assert copied["presenter"]["source_path"] == "assets/avatar/mengmeng.mp4"
    assert copied["visual_timeline"]["blocks"] == original_timeline["blocks"]
    assert copied["review_preview"]["status"] == "idle"
    assert "批量复用数字人位置大小" in copied["review_preview"]["stale_reason"]
    assert copied["presenter"]["asset_id"] == "AV-MENGMENG"


def test_presenter_layout_apply_selected_endpoint_starts_preview_refresh(projects_root, client, monkeypatch):
    project = make_project(projects_root)
    state = workbench_mod.bootstrap_workbench(project)
    state["project"]["pipeline_type"] = workbench_mod.AVATAR_PIPELINE
    first, second = state["scenes"]
    first["presenter"].update({
        "treatment": "custom", "layout_override": {"x": .1, "y": .08, "width": .3}, "crop_bottom": .12,
    })
    workbench_mod._save(project, state)
    monkeypatch.setattr(server_mod, "generate_review_preview_sync", lambda *_args, **_kwargs: {})

    response = client.post("/api/project/film/workbench/presenter-layouts/apply-selected", json={
        "source_scene_id": first["id"], "target_scene_ids": [second["id"]],
    })

    assert response.status_code == 200
    body = response.json()
    assert body["automation"]["preview_sync"]["scene_ids"] == [second["id"]]
    synced = next(scene for scene in body["scenes"] if scene["id"] == second["id"])
    assert synced["presenter"]["layout_override"] == {"x": .1, "y": .08, "width": .3}
    assert synced["presenter"]["crop_bottom"] == .12


def test_new_projects_use_the_approved_circle_presenter_framing(projects_root):
    project = make_project(projects_root)
    state = workbench_mod.bootstrap_workbench(project)

    layouts = state["presenter_layouts"]
    template = next(item for item in layouts["templates"] if item["id"] == "pip_top_left")

    assert layouts["default_template_id"] == "pip_top_left"
    assert template["geometry"] == {"x": .04, "y": .03, "width": .29}
    assert template["shape"] == "circle"
    assert template["crop_bottom"] == 0.0
    assert template["face_crop"] == {"x": .48, "y": .38, "zoom": 1.15}


def test_avatar_pip_bottom_crop_preserves_person_proportions(tmp_path, monkeypatch):
    state = {
        "project": {"intake": {"aspect": "portrait"}},
        "settings": {"frame_rate": 30},
        "presenter_layouts": {
            "version": 1,
            "default_template_id": "pip_top_left",
            "templates": [{
                "id": "pip_top_left", "name": "左上角解说员",
                "geometry": {"x": .04, "y": .04, "width": .25},
                "crop_bottom": 0.0,
            }],
        },
    }
    scene = {"presenter": {"treatment": "custom", "layout_template_id": "pip_top_left", "layout_override": {"x": .04, "y": .04, "width": .25}, "crop_bottom": .12}}
    monkeypatch.setattr(workbench_mod, "_probe_video", lambda *_args, **_kwargs: {
        "streams": [{"codec_type": "video", "width": 1080, "height": 1920}],
    })

    geometry = workbench_mod._avatar_pip_geometry(tmp_path, state, scene, tmp_path / "avatar.mp4")
    filters = workbench_mod._avatar_pip_filter(geometry, fps=30, duration=8)

    assert geometry["cropped_source_height"] == 1688
    assert geometry["width"] == 270
    assert geometry["height"] == 422
    assert "crop=iw:1688:0:0" in filters
    assert "scale=270:422" in filters
    assert "trim=duration=8.000" in filters


def test_avatar_circle_mode_center_crops_4_by_5_without_stretching(tmp_path, monkeypatch):
    state = {
        "project": {"intake": {"aspect": "portrait"}},
        "settings": {"frame_rate": 30},
        "presenter_layouts": {
            "version": 1,
            "default_template_id": "pip_top_left",
            "templates": [{
                "id": "pip_top_left", "name": "左上角解说员",
                "geometry": {"x": .04, "y": .04, "width": .25},
                "crop_bottom": 0.0, "shape": "rounded",
                "face_crop": {"x": .5, "y": 0, "zoom": 1},
            }],
        },
    }
    scene = {"presenter": {
        "treatment": "custom", "layout_template_id": "pip_top_left",
        "layout_override": {"x": .04, "y": .04, "width": .25},
        "crop_bottom": 0.0, "shape": "circle",
    }}
    monkeypatch.setattr(workbench_mod, "_probe_video", lambda *_args, **_kwargs: {
        "streams": [{"codec_type": "video", "width": 800, "height": 1000}],
    })

    geometry = workbench_mod._avatar_pip_geometry(tmp_path, state, scene, tmp_path / "avatar.mp4")
    filters = workbench_mod._avatar_pip_filter(geometry)

    assert geometry["shape"] == "circle"
    assert geometry["crop_width"] == geometry["crop_height"] == 800
    assert geometry["width"] == geometry["height"]
    assert "crop=iw:800:0:0" in filters
    assert "format=rgba" in filters
    assert "min(W,H)" in filters


def test_avatar_circle_face_crop_can_pan_and_zoom_without_stretching(tmp_path, monkeypatch):
    state = {
        "project": {"intake": {"aspect": "portrait"}},
        "settings": {"frame_rate": 30},
        "presenter_layouts": {
            "version": 1,
            "default_template_id": "pip_top_left",
            "templates": [{
                "id": "pip_top_left", "name": "左上角解说员",
                "geometry": {"x": .04, "y": .04, "width": .25},
                "crop_bottom": 0.0, "shape": "circle",
                "face_crop": {"x": .8, "y": .35, "zoom": 2},
            }],
        },
    }
    scene = {"presenter": {"treatment": "custom", "layout_template_id": "pip_top_left", "shape": "circle"}}
    monkeypatch.setattr(workbench_mod, "_probe_video", lambda *_args, **_kwargs: {
        "streams": [{"codec_type": "video", "width": 800, "height": 1000}],
    })

    geometry = workbench_mod._avatar_pip_geometry(tmp_path, state, scene, tmp_path / "avatar.mp4")
    filters = workbench_mod._avatar_pip_filter(geometry)

    assert geometry["face_crop"] == {"x": .8, "y": .35, "zoom": 2.0}
    assert geometry["crop_width"] == geometry["crop_height"] == 400
    assert geometry["crop_x"] == 320
    assert geometry["crop_y"] == 210
    assert "crop=400:400:320:210" in filters
    assert f"scale={geometry['width']}:{geometry['height']}" in filters


def test_presenter_crop_can_be_saved_and_applied_to_all_avatar_scenes(projects_root):
    project = make_project(projects_root)
    state = workbench_mod.bootstrap_workbench(project)
    state["project"]["pipeline_type"] = "avatar-spokesperson"
    for scene in state["scenes"]:
        scene["presenter"].update({"treatment": "pip_top_left", "source_path": "assets/avatar/master.mp4"})
    workbench_mod._save(project, state)

    updated = workbench_mod.update_presenter_layout_template(project, {
        "scene_id": "scene-a",
        "template_id": "pip_top_left",
        "name": "去底部模糊区",
        "geometry": {"x": .04, "y": .04, "width": .25},
        "crop_bottom": .12,
        "face_crop": {"x": .55, "y": .18, "zoom": 1.7},
        "apply_scope": "all",
    })

    template = next(item for item in updated["presenter_layouts"]["templates"] if item["id"] == "pip_top_left")
    assert template["crop_bottom"] == .12
    assert template["face_crop"] == {"x": .55, "y": .18, "zoom": 1.7}
    assert {scene["presenter"]["crop_bottom"] for scene in updated["scenes"]} == {.12}
    assert all(scene["presenter"]["face_crop"] is None for scene in updated["scenes"])
    assert all(workbench_mod._presenter_layout(updated, scene["presenter"])["face_crop"] == template["face_crop"] for scene in updated["scenes"])


def test_visual_block_lock_and_refresh_guard(client, projects_root, monkeypatch):
    make_project(projects_root)
    monkeypatch.setenv("PEXELS_API_KEY", "test-key")
    client.post("/api/project/film/workbench/bootstrap")
    locked = client.patch(
        "/api/project/film/workbench/scenes/scene-a/visual-blocks/VB-001",
        json={"locked": True},
    )
    assert locked.status_code == 200
    block = locked.json()["scenes"][0]["visual_timeline"]["blocks"][0]
    assert block["locked"] is True

    rejected = client.post(
        "/api/project/film/workbench/scenes/scene-a/visual-blocks/VB-001/refresh/jobs",
        json={"confirmed": True},
    )
    assert rejected.status_code == 422
    assert "已锁定" in rejected.json()["detail"]


def test_stale_visual_batch_worker_cannot_overwrite_a_newer_job(projects_root):
    project = make_project(projects_root)
    state = workbench_mod.bootstrap_workbench(project)
    state["automation"]["visual_batch"] = {
        "status": "queued", "job_id": "VBJ-newer", "items": [],
        "total_slots": 0, "completed_slots": 0, "failed_slots": 0,
    }
    workbench_mod._save(project, state)

    untouched = workbench_mod.generate_visual_batch(project, "VBJ-older")

    batch = untouched["automation"]["visual_batch"]
    assert batch["job_id"] == "VBJ-newer"
    assert batch["status"] == "queued"


@pytest.mark.skipif(not _ffmpeg_available(), reason="ffmpeg is required for visual batch verification")
def test_visual_batch_generates_slot_assets_serially_and_refreshes_only_one_slot(projects_root, monkeypatch):
    project = make_project(projects_root)
    project_meta = json.loads((project / "project.json").read_text(encoding="utf-8"))
    project_meta["render_profile"] = {"width": 320, "height": 180}
    write_json(project / "project.json", project_meta)
    monkeypatch.setenv("PEXELS_API_KEY", "test-key")
    ffmpeg = _ffmpeg_available()
    calls: list[dict] = []

    def fake_video(self, inputs):
        calls.append(dict(inputs))
        output = Path(inputs["output_path"])
        output.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run([
            ffmpeg, "-y", "-f", "lavfi", "-i", "color=c=navy:s=320x180:r=30:d=7",
            "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(output),
        ], check=True, capture_output=True)
        return SimpleNamespace(success=True, artifacts=[str(output)], data={
            "duration_seconds": 7, "width": 320, "height": 180,
            "video_id": f"video-{len(calls)}", "license": "Pexels License",
            "pexels_url": f"https://www.pexels.com/video/{len(calls)}/",
        }, error=None)

    monkeypatch.setattr(workbench_mod.PexelsVideo, "execute", fake_video)
    state = workbench_mod.bootstrap_workbench(project)
    scene_b = next(scene for scene in state["scenes"] if scene["id"] == "scene-b")
    scene_b["end_seconds"] = 14
    workbench_mod._save(project, state)
    workbench_mod.start_visual_batch_generation(project, {
        "confirmed": True, "selection_mode": "custom", "scene_ids": ["scene-b"],
        "source_mode": "web_download", "profile": "video",
    })
    completed = workbench_mod.generate_visual_batch(project)
    scene_b = next(scene for scene in completed["scenes"] if scene["id"] == "scene-b")
    blocks = scene_b["visual_timeline"]["blocks"]
    assert len(blocks) == 2
    assert all(block["status"] == "ready" and block["asset_id"].startswith("S-") for block in blocks)
    assert blocks[0]["end_seconds"] == blocks[1]["start_seconds"] == 5
    before = [block["asset_id"] for block in blocks]

    workbench_mod.start_visual_block_refresh(project, "scene-b", blocks[1]["id"], {"confirmed": True})
    refreshed = workbench_mod.generate_visual_batch(project)
    refreshed_blocks = next(scene for scene in refreshed["scenes"] if scene["id"] == "scene-b")["visual_timeline"]["blocks"]
    assert refreshed_blocks[0]["asset_id"] == before[0]
    assert refreshed_blocks[1]["asset_id"] != before[1]
    assert refreshed_blocks[0]["id"] == blocks[0]["id"]
    assert refreshed_blocks[1]["id"] == blocks[1]["id"]
    assert len(calls) == 3
    assert calls[0]["exclude_video_ids"] == []
    assert "video-1" in calls[1]["exclude_video_ids"]
    assert {"video-1", "video-2"}.issubset(set(calls[2]["exclude_video_ids"]))


def test_review_preview_sync_processes_only_missing_scenes_and_survives_serial_saves(projects_root, monkeypatch):
    project = make_project(projects_root)
    state = workbench_mod.bootstrap_workbench(project)
    first, second = state["scenes"][:2]
    first["review_preview"] = {"status": "ready", "output_path": "renders/review-previews/current.mp4", "input_signature": "test"}
    (project / "renders" / "review-previews").mkdir(parents=True, exist_ok=True)
    (project / "renders" / "review-previews" / "current.mp4").write_bytes(b"preview")
    monkeypatch.setattr(workbench_mod, "_review_preview_is_current", lambda _project, _state, scene: scene["id"] == first["id"])
    workbench_mod._save(project, state)
    rendered: list[str] = []

    def fake_preview(project_dir, scene_id):
        rendered.append(scene_id)
        latest = workbench_mod._load_for_write(project_dir)
        scene = next(item for item in latest["scenes"] if item["id"] == scene_id)
        scene["review_preview"] = {"status": "ready", "output_path": f"renders/review-previews/{scene_id}.mp4"}
        return workbench_mod._save(project_dir, latest)

    monkeypatch.setattr(workbench_mod, "generate_scene_review_preview", fake_preview)
    queued = workbench_mod.start_review_preview_sync(project, {"confirmed": True, "selection_mode": "missing"})
    job_id = queued["automation"]["preview_sync"]["job_id"]
    completed = workbench_mod.generate_review_preview_sync(project, job_id)

    sync = completed["automation"]["preview_sync"]
    assert first["id"] not in rendered
    assert second["id"] in rendered
    assert sync["status"] == "completed"
    assert sync["completed_scenes"] == sync["total_scenes"]
    assert all(item["status"] == "completed" for item in sync["items"])


def test_review_preview_sync_waits_for_pending_visual_batch(projects_root):
    project = make_project(projects_root)
    state = workbench_mod.bootstrap_workbench(project)
    state["automation"]["visual_batch"] = {
        "status": "generating",
        "job_id": "VBJ-pending",
        "items": [{"scene_id": "scene-a", "block_id": "VB-001", "status": "generating"}],
        "current": {"scene_id": "scene-a", "block_id": "VB-001"},
    }
    workbench_mod._save(project, state)

    with pytest.raises(workbench_mod.WorkbenchError, match="主体画面仍在生成.*scene-a / VB-001"):
        workbench_mod.start_review_preview_sync(project, {"confirmed": True, "selection_mode": "missing"})


def test_server_restart_recovers_a_pending_visual_batch(projects_root, monkeypatch):
    project = make_project(projects_root)
    state = workbench_mod.bootstrap_workbench(project)
    state["automation"]["visual_batch"] = {
        "status": "generating",
        "job_id": "VBJ-recover",
        "items": [{"scene_id": "scene-a", "block_id": "VB-001", "status": "generating"}],
        "current": {"scene_id": "scene-a", "block_id": "VB-001"},
    }
    workbench_mod._save(project, state)
    calls: list[tuple[Path, str]] = []

    def fake_generate(path, expected_job_id):
        calls.append((path, expected_job_id))
        latest = workbench_mod._load_for_write(path)
        latest["automation"]["visual_batch"].update({"status": "completed", "current": None})
        latest["automation"]["visual_batch"]["items"][0]["status"] = "completed"
        return workbench_mod._save(path, latest)

    monkeypatch.setattr(server_mod, "generate_visual_batch", fake_generate)
    app = SimpleNamespace(state=SimpleNamespace(recovery_tasks=set()))

    async def recover() -> None:
        await server_mod._recover_workbench_background_jobs(app)
        await asyncio.gather(*app.state.recovery_tasks)

    asyncio.run(recover())
    assert calls == [(project, "VBJ-recover")]


def test_workbench_records_source_anchor_assets_and_usage(client, projects_root):
    project = make_project(projects_root)
    assert client.post("/api/project/film/workbench/bootstrap").status_code == 200

    scene = client.patch("/api/project/film/workbench/scenes/scene-a", json={
        "source_strategy": "human_provided", "anchor_kind": "first_frame", "anchor_status": "approved",
    })
    assert scene.status_code == 200
    scene_a = scene.json()["scenes"][0]
    assert scene_a["source_strategy"] == "human_provided"
    assert next(anchor for anchor in scene_a["anchors"] if anchor["kind"] == "first_frame")["status"] == "approved"

    asset = client.post("/api/project/film/workbench/assets", json={
        "name": "局部替换候选", "type": "video", "source_type": "human_provided",
        "path": "assets/video/candidate.mp4", "license": "已授权",
    })
    assert asset.status_code == 200
    added = asset.json()["assets"][-1]
    assert added["id"] == "S-002"

    usage = client.post("/api/project/film/workbench/usages", json={"scene_id": "scene-a", "asset_id": "S-002", "role": "visual"})
    assert usage.status_code == 200
    selected = [item for item in usage.json()["usages"] if item["scene_id"] == "scene-a" and item["selected"]]
    assert selected[-1]["id"] == "U-002"
    assert selected[-1]["asset_id"] == "S-002"
    assert (project / "artifacts" / "workbench.json").is_file()


def test_surgical_directive_is_scene_bound_and_invalidates_only_its_review_preview(client, projects_root):
    make_project(projects_root)
    assert client.post("/api/project/film/workbench/bootstrap").status_code == 200

    added = client.post("/api/project/film/workbench/scenes/scene-a/surgical-directives", json={
        "component_type": "text_callout",
        "position": "lower_third",
        "text": "关键数据：3 倍增长",
        "start_seconds": 1.25,
        "duration_seconds": 2.5,
    })

    assert added.status_code == 200, added.text
    scene_a = next(scene for scene in added.json()["scenes"] if scene["id"] == "scene-a")
    scene_b = next(scene for scene in added.json()["scenes"] if scene["id"] == "scene-b")
    directive = scene_a["surgical_directives"][0]
    assert directive["id"] == "RDX-001"
    assert directive["start_seconds"] == 1.25
    assert scene_a["review_preview"]["status"] in {"idle", "stale"}
    assert scene_a["review_status"] == "needs_adjustment"
    assert scene_b["surgical_directives"] == []

    removed = client.delete(f"/api/project/film/workbench/scenes/scene-a/surgical-directives/{directive['id']}")
    assert removed.status_code == 200, removed.text
    assert next(scene for scene in removed.json()["scenes"] if scene["id"] == "scene-a")["surgical_directives"] == []


def test_surgical_directive_validates_component_content_and_scene_range(client, projects_root):
    make_project(projects_root)
    assert client.post("/api/project/film/workbench/bootstrap").status_code == 200

    missing_text = client.post("/api/project/film/workbench/scenes/scene-a/surgical-directives", json={
        "component_type": "text_callout", "position": "center", "start_seconds": 1,
    })
    assert missing_text.status_code == 422
    assert "文字" in missing_text.json()["detail"]

    clamped = client.post("/api/project/film/workbench/scenes/scene-a/surgical-directives", json={
        "component_type": "focus_box", "position": "center", "start_seconds": 99, "duration_seconds": 99,
    })
    assert clamped.status_code == 200
    directive = next(scene for scene in clamped.json()["scenes"] if scene["id"] == "scene-a")["surgical_directives"][0]
    assert directive["start_seconds"] < 4
    assert directive["duration_seconds"] == 4


def test_focus_box_preserves_normalized_review_coordinates(client, projects_root):
    make_project(projects_root)
    assert client.post("/api/project/film/workbench/bootstrap").status_code == 200

    response = client.post("/api/project/film/workbench/scenes/scene-a/surgical-directives", json={
        "component_type": "focus_box", "position": "center", "start_seconds": 1,
        "box": {"x": .37, "y": .16, "width": .42, "height": .28},
    })

    assert response.status_code == 200, response.text
    directive = next(scene for scene in response.json()["scenes"] if scene["id"] == "scene-a")["surgical_directives"][0]
    assert directive["box"] == {"x": .37, "y": .16, "width": .42, "height": .28}


@pytest.mark.skipif(not _ffmpeg_available(), reason="ffmpeg is required for scene review preview verification")
def test_local_scene_review_preview_keeps_portrait_canvas_and_component(projects_root):
    project = make_project(projects_root)
    project_meta = json.loads((project / "project.json").read_text(encoding="utf-8"))
    project_meta["render_profile"] = {"width": 108, "height": 192, "fps": 24}
    project_meta["intake"] = {"aspect": "portrait"}
    write_json(project / "project.json", project_meta)
    source = project / "assets" / "video" / "portrait-review.mp4"
    ffmpeg = _ffmpeg_available()
    subprocess.run([
        ffmpeg, "-y", "-f", "lavfi", "-i", "color=c=#346c86:s=108x192:r=24:d=4",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=4",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(source),
    ], check=True, capture_output=True)

    workbench_mod.bootstrap_workbench(project)
    state = workbench_mod.add_asset(project, {
        "name": "竖屏审核素材", "type": "video", "source_type": "human_provided",
        "path": "assets/video/portrait-review.mp4", "duration_seconds": 4,
    })
    asset_id = state["assets"][-1]["id"]
    workbench_mod.assign_usage(project, {"scene_id": "scene-a", "asset_id": asset_id, "role": "visual"})
    workbench_mod.add_surgical_directive(project, "scene-a", {
        "component_type": "text_callout", "position": "lower_third", "text": "竖屏审核组件",
        "start_seconds": 1.0, "duration_seconds": 1.0,
    })

    state = workbench_mod.generate_scene_review_preview(project, "scene-a")
    scene = next(item for item in state["scenes"] if item["id"] == "scene-a")
    preview_path = project / scene["review_preview"]["output_path"]
    assert preview_path.is_file()
    assert scene["review_preview"]["resolution"] == "108x192"
    probe = workbench_mod._probe_video(preview_path, ffmpeg)
    stream = next(item for item in probe["streams"] if item.get("codec_type") == "video")
    assert (stream["width"], stream["height"]) == (108, 192)
    assert scene["review_preview"]["caption_cues"]


@pytest.mark.skipif(not _ffmpeg_available(), reason="ffmpeg is required for scene review preview verification")
def test_local_scene_review_preview_prefers_current_narration_audio(projects_root):
    project = make_project(projects_root)
    project_meta = json.loads((project / "project.json").read_text(encoding="utf-8"))
    project_meta["render_profile"] = {"width": 108, "height": 192, "fps": 24}
    write_json(project / "project.json", project_meta)
    ffmpeg = _ffmpeg_available()
    visual_path = project / "assets" / "video" / "silent-review.mp4"
    narration_path = project / "assets" / "audio" / "scene-a-review.m4a"
    narration_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([
        ffmpeg, "-y", "-f", "lavfi", "-i", "color=c=#346c86:s=108x192:r=24:d=4",
        "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(visual_path),
    ], check=True, capture_output=True)
    subprocess.run([
        ffmpeg, "-y", "-f", "lavfi", "-i", "sine=frequency=660:sample_rate=48000:duration=4",
        "-c:a", "aac", str(narration_path),
    ], check=True, capture_output=True)

    workbench_mod.bootstrap_workbench(project)
    state = workbench_mod.add_asset(project, {
        "name": "静音竖屏素材", "type": "video", "source_type": "human_provided",
        "path": "assets/video/silent-review.mp4", "duration_seconds": 4,
    })
    workbench_mod.assign_usage(project, {"scene_id": "scene-a", "asset_id": state["assets"][-1]["id"], "role": "visual"})
    state = workbench_mod._load_for_write(project)
    scene = next(item for item in state["scenes"] if item["id"] == "scene-a")
    scene["narration"]["versions"] = [{
        "id": "N-001", "status": "current", "audio_path": "assets/audio/scene-a-review.m4a", "duration_seconds": 4,
    }]
    scene["narration"]["current_version_id"] = "N-001"
    workbench_mod._save(project, state)

    state = workbench_mod.generate_scene_review_preview(project, "scene-a")
    scene = next(item for item in state["scenes"] if item["id"] == "scene-a")
    probe = workbench_mod._probe_video(project / scene["review_preview"]["output_path"], ffmpeg)
    assert any(stream.get("codec_type") == "audio" for stream in probe["streams"])


def test_workbench_persists_production_intake_before_script_generation(client, projects_root):
    make_project(projects_root)
    response = client.patch("/api/project/film/workbench/intake", json={
        "video_title": "每天读书十分钟",
        "source_text": "先从一个简单想法开始。",
        "script_status": "idea",
        "idea": "介绍每天读书十分钟为什么有用。",
        "materials_status": "partial",
        "style_status": "direction",
        "audience": "想提高阅读效率的学生",
        "content_goal": "让观众今天开始阅读。",
        "style_direction": "温暖、安静、纸张质感。",
    })

    assert response.status_code == 200
    intake = response.json()["project"]["intake"]
    assert intake["script_status"] == "idea"
    assert intake["video_title"] == "每天读书十分钟"
    assert intake["source_text"] == "先从一个简单想法开始。"
    assert intake["materials_status"] == "partial"
    assert intake["style_status"] == "direction"
    assert intake["idea"] == "介绍每天读书十分钟为什么有用。"


def test_script_draft_is_paid_confirmed_and_human_reviewable(client, projects_root, monkeypatch):
    project = make_project(projects_root)
    client.patch("/api/project/film/workbench/intake", json={
        "script_status": "idea", "idea": "介绍每天读书十分钟为什么有用。",
    })

    draft = {
        "version": "1.0", "title": "导演审核测试", "total_duration_seconds": 9,
        "sections": [{
            "id": "sec-01", "label": "开场", "text": "每天读书十分钟，会发生什么？",
            "start_seconds": 0, "end_seconds": 3, "speaker_directions": "自然提问",
            "enhancement_cues": [{"type": "broll", "description": "翻开书本", "timestamp_seconds": 0}],
        }],
    }

    received = {}

    def fake_execute(self, inputs):
        received.update(inputs)
        return SimpleNamespace(success=True, data={"model": "test-model", "script": draft}, error=None)

    monkeypatch.setattr(workbench_mod.OpenAIScript, "execute", fake_execute)

    not_confirmed = client.post("/api/project/film/workbench/script-draft", json={"mode": "expand_idea"})
    assert not_confirmed.status_code == 422
    assert "确认" in not_confirmed.json()["detail"]

    generated = client.post("/api/project/film/workbench/script-draft", json={
        "mode": "expand_idea",
        "video_title": "十分钟阅读计划",
        "source_text": "介绍每天读书十分钟为什么有用。",
        "confirmed": True,
    })
    assert generated.status_code == 200, generated.text
    assert generated.json()["project"]["script_draft"]["status"] == "draft"
    assert generated.json()["project"]["intake"]["video_title"] == "十分钟阅读计划"
    assert generated.json()["project"]["intake"]["source_text"] == "介绍每天读书十分钟为什么有用。"
    assert received["title"] == "十分钟阅读计划"
    assert received["idea"] == "介绍每天读书十分钟为什么有用。"
    assert received["script_text"] == ""

    approved = client.post("/api/project/film/workbench/script-draft/review", json={"action": "approve"})
    assert approved.status_code == 200
    assert approved.json()["project"]["script_draft"]["status"] == "approved"
    assert approved.json()["project"]["intake"]["script_status"] == "draft_approved"
    assert json.loads((project / "artifacts" / "script.json").read_text(encoding="utf-8")) == draft
    assert json.loads((project / "artifacts" / "script_draft.json").read_text(encoding="utf-8")) == draft


def test_script_draft_sentence_editor_is_atomic_versioned_and_model_free(
    client, projects_root, monkeypatch
):
    project = make_project(projects_root)
    model_calls = []
    generated_script = {
        "version": "1.0",
        "title": "逐句编辑测试",
        "total_duration_seconds": 8,
        "sections": [
            {
                "id": "sec-01",
                "label": "原段落",
                "text": "第一句。第二句。",
                "start_seconds": 0,
                "end_seconds": 8,
                "speaker_directions": "自然",
                "enhancement_cues": [
                    {"type": "broll", "description": "原画面", "timestamp_seconds": 4}
                ],
            }
        ],
    }

    def fake_execute(self, inputs):
        model_calls.append(inputs)
        return SimpleNamespace(
            success=True,
            data={"model": "test-model", "script": generated_script},
            error=None,
        )

    monkeypatch.setattr(workbench_mod.OpenAIScript, "execute", fake_execute)
    generated = client.post(
        "/api/project/film/workbench/script-draft",
        json={"mode": "organize_script", "video_title": "逐句编辑", "source_text": "第一句。第二句。", "confirmed": True},
    )
    assert generated.status_code == 200, generated.text
    draft = generated.json()["project"]["script_draft"]
    assert draft["revision"] == 1
    assert draft["original_script"] == generated_script

    saved = client.patch(
        "/api/project/film/workbench/script-draft/content",
        json={
            "expected_revision": 1,
            "title": "逐句编辑后的标题",
            "sections": [
                {
                    "id": "sec-01",
                    "label": "调整后的段落",
                    "sentences": ["第二句改写。", "新增一句"],
                },
                {
                    "label": "用户新增段落",
                    "sentences": ["最后一句！"],
                },
            ],
        },
    )

    assert saved.status_code == 200, saved.text
    edited = saved.json()["project"]["script_draft"]
    assert edited["revision"] == 2
    assert edited["status"] == "draft"
    assert len(model_calls) == 1, "人工保存不得再次调用脚本模型"
    sections = edited["script"]["sections"]
    assert [item["id"] for item in sections][0] == "sec-01"
    assert sections[1]["id"].startswith("sec-user-")
    assert sections[0]["text"] == "第二句改写。新增一句。"
    assert sections[1]["text"] == "最后一句！"
    assert sections[0]["speaker_directions"] == "自然"
    assert sections[0]["start_seconds"] == 0
    assert sections[0]["end_seconds"] <= sections[1]["start_seconds"]
    assert edited["script"]["total_duration_seconds"] == sections[-1]["end_seconds"]
    assert edited["original_script"] == generated_script
    persisted = json.loads((project / "artifacts" / "script_draft.json").read_text(encoding="utf-8"))
    assert persisted == edited["script"]

    stale = client.patch(
        "/api/project/film/workbench/script-draft/content",
        json={
            "expected_revision": 1,
            "sections": [{"id": "sec-01", "label": "过期", "sentences": ["不能覆盖。"]}],
        },
    )
    assert stale.status_code == 422
    assert "版本" in stale.json()["detail"]
    assert workbench_mod.read_workbench(project)["project"]["script_draft"]["revision"] == 2


def test_script_draft_approval_rejects_stale_revision(client, projects_root, monkeypatch):
    make_project(projects_root)
    script = {
        "version": "1.0",
        "title": "版本审核",
        "total_duration_seconds": 3,
        "sections": [
            {"id": "sec-01", "label": "正文", "text": "待审核。", "start_seconds": 0, "end_seconds": 3}
        ],
    }
    monkeypatch.setattr(
        workbench_mod.OpenAIScript,
        "execute",
        lambda self, inputs: SimpleNamespace(success=True, data={"model": "test-model", "script": script}, error=None),
    )
    assert client.post(
        "/api/project/film/workbench/script-draft",
        json={"mode": "from_scratch", "video_title": "版本审核", "confirmed": True},
    ).status_code == 200
    assert client.patch(
        "/api/project/film/workbench/script-draft/content",
        json={"expected_revision": 1, "sections": [{"id": "sec-01", "label": "正文", "sentences": ["已经修改。"]}]},
    ).status_code == 200

    stale = client.post(
        "/api/project/film/workbench/script-draft/review",
        json={"action": "approve", "expected_revision": 1},
    )
    assert stale.status_code == 422
    assert "版本" in stale.json()["detail"]
    approved = client.post(
        "/api/project/film/workbench/script-draft/review",
        json={"action": "approve", "expected_revision": 2},
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["project"]["script_draft"]["approved_revision"] == 2


def test_approved_script_can_reopen_only_before_downstream_work(
    client, projects_root, monkeypatch
):
    project = make_project(projects_root)
    (project / "artifacts" / "script.json").unlink()
    (project / "artifacts" / "scene_plan.json").unlink()
    script = {
        "version": "1.0",
        "title": "重新编辑",
        "total_duration_seconds": 3,
        "sections": [
            {"id": "sec-01", "label": "正文", "text": "原句。", "start_seconds": 0, "end_seconds": 3}
        ],
    }
    monkeypatch.setattr(
        workbench_mod.OpenAIScript,
        "execute",
        lambda self, inputs: SimpleNamespace(success=True, data={"model": "test-model", "script": script}, error=None),
    )
    assert client.post(
        "/api/project/film/workbench/script-draft",
        json={"mode": "from_scratch", "video_title": "重新编辑", "confirmed": True},
    ).status_code == 200
    assert client.post(
        "/api/project/film/workbench/script-draft/review",
        json={"action": "approve", "expected_revision": 1},
    ).status_code == 200

    reopened = client.post(
        "/api/project/film/workbench/script-draft/reopen",
        json={"expected_revision": 1},
    )
    assert reopened.status_code == 200, reopened.text
    assert reopened.json()["project"]["script_draft"]["status"] == "draft"
    assert reopened.json()["project"]["script_draft"]["revision"] == 2

    assert client.post(
        "/api/project/film/workbench/script-draft/review",
        json={"action": "approve", "expected_revision": 2},
    ).status_code == 200
    assert client.post("/api/project/film/workbench/scene-plan").status_code == 200
    blocked = client.post(
        "/api/project/film/workbench/script-draft/reopen",
        json={"expected_revision": 2},
    )
    assert blocked.status_code == 422
    assert "分镜" in blocked.json()["detail"] or "下游" in blocked.json()["detail"]


@pytest.mark.parametrize(
    ("mode", "source_text", "expected_idea", "expected_script"),
    [
        ("organize_script", "第一句。第二句。", "", "第一句。第二句。"),
        ("expand_idea", "聊聊机器人为什么越跑越快。", "聊聊机器人为什么越跑越快。", ""),
        ("from_scratch", "", "", ""),
    ],
)
def test_minimal_script_input_routes_all_three_builtin_model_modes(
    client, projects_root, monkeypatch, mode, source_text, expected_idea, expected_script,
):
    make_project(projects_root)
    received = {}
    draft = {
        "version": "1.0", "title": "极简入口测试", "total_duration_seconds": 6,
        "sections": [{
            "id": "sec-01", "label": "正文", "text": "这是一份可审核草案。",
            "start_seconds": 0, "end_seconds": 6, "speaker_directions": "自然",
            "enhancement_cues": [],
        }],
    }

    def fake_execute(self, inputs):
        received.update(inputs)
        return SimpleNamespace(success=True, data={"model": "test-model", "script": draft}, error=None)

    monkeypatch.setattr(workbench_mod.OpenAIScript, "execute", fake_execute)
    response = client.post("/api/project/film/workbench/script-draft", json={
        "mode": mode,
        "video_title": "极简脚本入口",
        "source_text": source_text,
        "confirmed": True,
    })

    assert response.status_code == 200, response.text
    assert received["mode"] == mode
    assert received["title"] == "极简脚本入口"
    assert received["idea"] == expected_idea
    assert received["script_text"] == expected_script
    if mode == "organize_script":
        assert received["organize_strength"] == "faithful"


def test_avatar_organize_script_preserves_every_txxx_turn_when_model_merges_sections(
    client, projects_root, monkeypatch,
):
    project = make_project(projects_root)
    write_json(project / "project.json", {
        "project_id": "film", "title": "双主持整理", "pipeline_type": "avatar-spokesperson",
    })
    merged = {
        "version": "1.0", "title": "模型错误合并稿", "total_duration_seconds": 20,
        "sections": [
            {"id": "S01", "label": "合并段一", "text": "模型把两个轮次合并了。", "start_seconds": 0, "end_seconds": 10},
            {"id": "S02", "label": "合并段二", "text": "模型又合并了。", "start_seconds": 10, "end_seconds": 20},
        ],
    }
    received = {}

    def fake_execute(self, inputs):
        received.update(inputs)
        return SimpleNamespace(success=True, data={"model": "test-model", "script": merged}, error=None)

    monkeypatch.setattr(workbench_mod.OpenAIScript, "execute", fake_execute)
    source = "\n".join((
        "T001 雅雅：第一条事实。",
        "T002 檬檬：第二条解释。",
        "T003 雅雅：第三条补充。",
        "T004 檬檬：第四条收束。",
    ))
    response = client.post("/api/project/film/workbench/script-draft", json={
        "mode": "organize_script", "organize_strength": "faithful",
        "video_title": "双主持整理", "source_text": source, "confirmed": True,
    })

    assert response.status_code == 200, response.text
    state = response.json()
    sections = state["project"]["script_draft"]["script"]["sections"]
    assert [item["turn_id"] for item in sections] == ["T001", "T002", "T003", "T004"]
    assert [item["speaker_id"] for item in sections] == ["yaya", "mengmeng", "yaya", "mengmeng"]
    assert [item["text"] for item in sections] == ["第一条事实。", "第二条解释。", "第三条补充。", "第四条收束。"]
    assert all(item["expected_asset_filename"].startswith(item["turn_id"]) for item in sections)
    assert received["avatar_turn_contract"][1]["speaker_id"] == "mengmeng"
    assert state["project"]["intake"]["script_mode"] == "organize_script"
    assert state["project"]["intake"]["organize_strength"] == "faithful"
    assert state["project"]["script_draft"]["script"]["metadata"]["model_turn_contract_complete"] is False


def test_script_mode_is_preserved_when_model_request_fails(client, projects_root, monkeypatch):
    make_project(projects_root)
    monkeypatch.setattr(
        workbench_mod.OpenAIScript,
        "execute",
        lambda self, inputs: SimpleNamespace(success=False, data={"model": "test-model"}, error="temporary failure"),
    )
    response = client.post("/api/project/film/workbench/script-draft", json={
        "mode": "organize_script", "organize_strength": "light_polish",
        "video_title": "失败恢复", "source_text": "第一句。", "confirmed": True,
    })

    assert response.status_code == 422
    state = workbench_mod.read_workbench(projects_root / "film")
    assert state["project"]["intake"]["script_mode"] == "organize_script"
    assert state["project"]["intake"]["organize_strength"] == "light_polish"


def test_approved_script_generates_persisted_scene_plan(client, projects_root, monkeypatch):
    project = make_project(projects_root)
    (project / "artifacts" / "script.json").unlink()
    (project / "artifacts" / "scene_plan.json").unlink()
    client.patch("/api/project/film/workbench/intake", json={
        "script_status": "idea", "idea": "介绍每天读书十分钟的习惯。",
    })
    draft = {
        "version": "1.0", "title": "导演审核测试", "total_duration_seconds": 9,
        "sections": [
            {"id": "sec-01", "label": "开场", "text": "每天读书十分钟。", "start_seconds": 0, "end_seconds": 3,
             "enhancement_cues": [{"type": "broll", "description": "翻开书本", "timestamp_seconds": 1.2}]},
            {"id": "sec-02", "label": "重点", "text": "先读，再记，再复盘。", "start_seconds": 3, "end_seconds": 9,
             "enhancement_cues": [{"type": "diagram", "description": "三步阅读法", "timestamp_seconds": 6.5}]},
        ],
    }

    def fake_execute(self, inputs):
        return SimpleNamespace(success=True, data={"model": "test-model", "script": draft}, error=None)

    monkeypatch.setattr(workbench_mod.OpenAIScript, "execute", fake_execute)
    assert client.post("/api/project/film/workbench/script-draft", json={"mode": "expand_idea", "confirmed": True}).status_code == 200
    assert client.post("/api/project/film/workbench/script-draft/review", json={"action": "approve"}).status_code == 200

    generated = client.post("/api/project/film/workbench/scene-plan")

    assert generated.status_code == 200, generated.text
    state = generated.json()
    assert len(state["scenes"]) == 2
    assert len(state["segments"]) == 2
    assert state["scenes"][0]["anchors"][1]["time_seconds"] == 1.2
    assert state["scenes"][1]["shot_intent"] == "三步阅读法"
    assert json.loads((project / "artifacts" / "scene_plan.json").read_text(encoding="utf-8"))["scenes"][1]["id"] == "sec-02"


def test_script_draft_exposes_localized_model_diagnostic(client, projects_root, monkeypatch):
    make_project(projects_root)
    client.patch("/api/project/film/workbench/intake", json={
        "script_status": "idea", "idea": "介绍每天阅读十分钟的习惯。",
    })

    def fake_execute(self, inputs):
        return SimpleNamespace(
            success=False,
            data={"model": "gpt-4o-mini"},
            error="脚本接口调用失败：Error code: 404 - model not found",
        )

    monkeypatch.setattr(workbench_mod.OpenAIScript, "execute", fake_execute)
    response = client.post("/api/project/film/workbench/script-draft", json={
        "mode": "expand_idea", "confirmed": True,
    })

    assert response.status_code == 422
    assert response.json()["detail"] == "当前脚本模型“gpt-4o-mini”不可用，请把 OPENAI_SCRIPT_MODEL 改为中转站支持的模型后重试"


def test_openai_image_generation_registers_stable_assets_without_auto_usage(client, projects_root, monkeypatch):
    project = make_project(projects_root)

    def fake_execute(self, inputs):
        paths = self._output_paths(inputs["output_path"], inputs["n"], "png")
        for index, path in enumerate(paths, 1):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(f"fake-image-{index}".encode())
        return SimpleNamespace(success=True, artifacts=[str(path) for path in paths], error=None)

    monkeypatch.setattr(workbench_mod.OpenAIImage, "execute", fake_execute)

    saved_plan = client.put("/api/project/film/workbench/scenes/scene-a/visual-plan", json={
        "engine": "openai_image", "prompt": "只展示科技产品，不出现主持人或文字。",
    })
    assert saved_plan.status_code == 200

    not_confirmed = client.post("/api/project/film/workbench/openai-images", json={
        "prompt": "产品棚拍", "model": "gpt-image-2", "n": 1,
    })
    assert not_confirmed.status_code == 422
    assert "确认" in not_confirmed.json()["detail"]

    generated = client.post("/api/project/film/workbench/openai-images", json={
        "confirmed": True,
        "name": "产品主视觉",
        "prompt": "一只白色保温杯，正面居中，白色无缝背景，柔和棚拍光",
        "model": "gpt-image-2",
        "size": "1536x1024",
        "quality": "medium",
        "n": 2,
    })

    assert generated.status_code == 200, generated.text
    state = generated.json()
    added = state["assets"][-2:]
    assert [asset["id"] for asset in added] == ["S-002", "S-003"]
    assert all(asset["source_type"] == "ai_generated" for asset in added)
    assert all(asset["generation"]["model"] == "gpt-image-2" for asset in added)
    assert all((project / asset["path"]).is_file() for asset in added)
    assert not any(usage["asset_id"] in {"S-002", "S-003"} for usage in state["usages"])
    assert "OPENAI_API_KEY" not in json.dumps(state)


def test_ai_scene_generates_real_keyframe_review_and_explicitly_adopts_the_visual(client, projects_root, monkeypatch):
    project = make_project(projects_root)
    client.post("/api/project/film/workbench/bootstrap")
    selected = client.patch("/api/project/film/workbench/scenes/scene-a", json={"source_strategy": "ai_generated"})
    assert selected.status_code == 200

    def fake_execute(self, inputs):
        paths = self._output_paths(inputs["output_path"], 1, "png")
        paths[0].parent.mkdir(parents=True, exist_ok=True)
        paths[0].write_bytes(b"fake-keyframe")
        return SimpleNamespace(success=True, artifacts=[str(paths[0])], error=None)

    monkeypatch.setattr(workbench_mod.OpenAIImage, "execute", fake_execute)

    saved_plan = client.put("/api/project/film/workbench/scenes/scene-a/visual-plan", json={
        "engine": "openai_image", "prompt": "只展示科技产品，不出现主持人或文字。",
    })
    assert saved_plan.status_code == 200

    not_confirmed = client.post("/api/project/film/workbench/scenes/scene-a/keyframes", json={})
    assert not_confirmed.status_code == 422
    assert "确认" in not_confirmed.json()["detail"]

    generated = client.post("/api/project/film/workbench/scenes/scene-a/keyframes", json={
        "confirmed": True, "model": "gpt-image-2", "quality": "low", "size": "1536x1024",
    })
    assert generated.status_code == 200, generated.text
    state = generated.json()
    scene = next(item for item in state["scenes"] if item["id"] == "scene-a")
    review = scene["keyframe_review"]
    assert review["status"] == "generated"
    assert [item["anchor_kind"] for item in review["timeline"]] == ["first_frame", "climax_frame"]
    assert review["hyperframes"]["status"] == "scaffolded"
    assert (project / review["hyperframes"]["index_path"]).is_file()
    assert [asset["id"] for asset in state["assets"][-2:]] == ["S-002", "S-003"]
    assert not any(usage["asset_id"] in {"S-002", "S-003"} for usage in state["usages"])

    adopted = client.post("/api/project/film/workbench/scenes/scene-a/ai-visual/adopt")
    assert adopted.status_code == 200, adopted.text
    adopted_state = adopted.json()
    adopted_scene = next(item for item in adopted_state["scenes"] if item["id"] == "scene-a")
    selected_visual = next(item for item in adopted_state["usages"] if item["scene_id"] == "scene-a" and item["role"] == "visual" and item["selected"])
    assert selected_visual["asset_id"] == "S-002"
    assert selected_visual["id"].startswith("U-")
    assert adopted_scene["review_preview"]["status"] in {"idle", "stale"}
    assert adopted_scene["visual_fit"]["source_asset_id"] == "S-002"

    blocked_scene_approval = client.patch("/api/project/film/workbench/scenes/scene-a", json={"review_status": "approved"})
    assert blocked_scene_approval.status_code == 422
    assert "关键帧" in blocked_scene_approval.json()["detail"]

    blocked_group = client.post("/api/project/film/workbench/scenes/scene-a/keyframes/review", json={"action": "approve"})
    assert blocked_group.status_code == 422
    assert "逐" in blocked_group.json()["detail"]

    for item in review["timeline"]:
        updated = client.post("/api/project/film/workbench/scenes/scene-a/keyframes/review", json={
            "action": "update", "items": [{"anchor_kind": item["anchor_kind"], "status": "approved"}],
        })
        assert updated.status_code == 200

    approved = client.post("/api/project/film/workbench/scenes/scene-a/keyframes/review", json={"action": "approve"})
    assert approved.status_code == 200, approved.text
    approved_state = approved.json()
    approved_scene = next(item for item in approved_state["scenes"] if item["id"] == "scene-a")
    assert approved_scene["keyframe_review"]["status"] == "approved"
    usages = [usage for usage in approved_state["usages"] if usage["scene_id"] == "scene-a" and usage["selected"]]
    assert {usage["role"] for usage in usages} >= {"visual_first_frame", "visual_climax_frame"}
    assert all(usage["id"].startswith("U-") for usage in usages)

    final_scene = client.patch("/api/project/film/workbench/scenes/scene-a", json={"review_status": "approved"})
    assert final_scene.status_code == 200


def test_keyframe_job_persists_first_frame_before_second_frame_fails_and_retries_only_failed_anchor(client, projects_root, monkeypatch):
    project = make_project(projects_root)
    client.post("/api/project/film/workbench/bootstrap")
    assert client.patch("/api/project/film/workbench/scenes/scene-a", json={"source_strategy": "ai_generated"}).status_code == 200
    assert client.put("/api/project/film/workbench/scenes/scene-a/visual-plan", json={
        "engine": "openai_image", "prompt": "只展示科技产品，不出现主持人或文字。",
    }).status_code == 200

    calls: list[str] = []

    def first_succeeds_second_fails(self, inputs):
        calls.append(inputs["prompt"])
        if len(calls) == 2:
            return SimpleNamespace(success=False, artifacts=[], error="Connection error.")
        paths = self._output_paths(inputs["output_path"], 1, "png")
        paths[0].parent.mkdir(parents=True, exist_ok=True)
        paths[0].write_bytes(f"frame-{len(calls)}".encode())
        return SimpleNamespace(success=True, artifacts=[str(paths[0])], error=None)

    monkeypatch.setattr(workbench_mod.OpenAIImage, "execute", first_succeeds_second_fails)
    started = workbench_mod.start_scene_keyframe_generation(project, "scene-a", {
        "confirmed": True, "model": "gpt-image-2", "quality": "low", "size": "1536x1024",
    })
    assert next(item for item in started["scenes"] if item["id"] == "scene-a")["keyframe_generation"]["status"] == "generating"
    first = workbench_mod.generate_scene_keyframes(project, "scene-a", {
        "confirmed": True, "model": "gpt-image-2", "quality": "low", "size": "1536x1024", "_single_anchor": True,
    })
    first_scene = next(item for item in first["scenes"] if item["id"] == "scene-a")
    first_job = first_scene["keyframe_generation"]
    assert first_job["status"] == "generating"
    assert first_job["anchors"]["first_frame"]["status"] == "completed"
    first_asset_id = first_job["anchors"]["first_frame"]["asset_id"]
    assert first_asset_id and any(asset["id"] == first_asset_id for asset in first["assets"])

    failed = workbench_mod.generate_scene_keyframes(project, "scene-a", {
        "confirmed": True, "model": "gpt-image-2", "quality": "low", "size": "1536x1024", "_single_anchor": True,
    })
    failed_scene = next(item for item in failed["scenes"] if item["id"] == "scene-a")
    failed_job = failed_scene["keyframe_generation"]
    assert failed_job["status"] == "completed_with_failures"
    assert failed_job["completed_count"] == 1
    assert failed_job["anchors"]["first_frame"]["asset_id"] == first_asset_id
    assert failed_job["anchors"]["climax_frame"]["status"] == "failed"
    assert len(calls) == 2

    resumed = workbench_mod.start_scene_keyframe_generation(project, "scene-a", {
        "confirmed": True, "resume_failed": True, "model": "gpt-image-2", "quality": "low", "size": "1536x1024",
    })
    assert next(item for item in resumed["scenes"] if item["id"] == "scene-a")["keyframe_generation"]["status"] == "generating"
    retry_state = workbench_mod.generate_scene_keyframes(project, "scene-a", {
        "confirmed": True, "model": "gpt-image-2", "quality": "low", "size": "1536x1024",
    })
    retry_scene = next(item for item in retry_state["scenes"] if item["id"] == "scene-a")
    retry_job = retry_scene["keyframe_generation"]
    assert retry_job["status"] == "completed"
    assert retry_job["anchors"]["first_frame"]["asset_id"] == first_asset_id
    assert retry_job["anchors"]["climax_frame"]["asset_id"]
    assert len(calls) == 3
    assert [item["anchor_kind"] for item in retry_scene["keyframe_review"]["timeline"]] == ["first_frame", "climax_frame"]


def test_keyframe_job_status_endpoint_exposes_only_task_state(client, projects_root):
    make_project(projects_root)
    client.post("/api/project/film/workbench/bootstrap")
    assert client.patch("/api/project/film/workbench/scenes/scene-a", json={"source_strategy": "ai_generated"}).status_code == 200
    assert client.put("/api/project/film/workbench/scenes/scene-a/visual-plan", json={
        "engine": "openai_image", "prompt": "只展示科技产品，不出现主持人或文字。",
    }).status_code == 200
    assert client.post("/api/project/film/workbench/scenes/scene-a/keyframes/jobs", json={
        "confirmed": True, "model": "gpt-image-2", "quality": "low", "size": "1536x1024",
    }).status_code == 200

    status = client.get("/api/project/film/workbench/scenes/scene-a/keyframes/jobs/current")
    assert status.status_code == 200
    body = status.json()
    assert set(body) == {"scene_id", "generation"}
    assert body["generation"]["status"] == "generating"
    assert set(body["generation"]["anchors"]) == {"first_frame", "climax_frame"}


def test_patch_isolated_by_frame_range_and_never_fakes_strict_render(client, projects_root):
    project = make_project(projects_root)
    client.post("/api/project/film/workbench/bootstrap")
    client.post("/api/project/film/workbench/assets", json={
        "name": "局部替换候选", "type": "video", "source_type": "human_provided",
        "path": "assets/video/candidate.mp4", "license": "已授权",
    })

    frozen = client.post("/api/project/film/workbench/segments/SEG-002/freeze", json={"frozen": True})
    assert frozen.status_code == 200
    assert frozen.json()["segments"][1]["freeze"]["input_hash"]

    created = client.post("/api/project/film/workbench/patches", json={
        "segment_id": "SEG-002", "candidate_asset_id": "S-002", "instruction": "仅替换高潮画面", "mode": "strict_freeze",
    })
    assert created.status_code == 200
    patch = created.json()["patches"][0]
    assert patch["id"] == "P-001"
    assert patch["dependencies"]["segment_snapshot"]
    assert patch["start_frame"] == 120
    assert patch["end_frame"] == 270

    overlap = client.post("/api/project/film/workbench/patches", json={
        "segment_id": "SEG-002", "instruction": "重复调整", "mode": "strict_freeze",
    })
    assert overlap.status_code == 422
    assert "重叠" in overlap.json()["detail"]

    rendered = client.post("/api/project/film/workbench/patches/P-001/render")
    assert rendered.status_code == 200
    result = rendered.json()["patches"][0]
    assert result["status"] == "blocked"
    strict_check = next(item for item in result["render_report"]["checks"] if item["name"] == "严格冻结承诺")
    assert strict_check["ok"] is False
    assert (project / "artifacts" / "boundary_reports" / "P-001.json").is_file()


def test_interactive_project_page_is_workbench_but_static_board_is_preserved(client, projects_root):
    make_project(projects_root)

    workbench = client.get("/p/film")
    static_board = client.get("/p/film?static=1")
    workbench_script = client.get("/ui/workbench.js")

    assert workbench.status_code == 200
    assert "海客视频工厂" in workbench.text
    assert "workbench.js" in workbench.text
    assert "board.js" in static_board.text


def test_workbench_preserves_review_position_but_never_auto_resumes_audio(client):
    script = client.get("/ui/workbench.js")
    workbench_script = script
    assert script.status_code == 200
    assert "stateFingerprint" in script.text
    assert "refreshInFlight" in script.text
    assert "data-review-audio-key" in script.text
    assert "captureReviewInteractionState" in script.text
    assert "restoreReviewInteractionState" in script.text
    assert "audio.play()" not in script.text
    assert "audio.pause()" in script.text
    assert 'autoplay: ""' not in script.text
    assert "数字人静态取景预览" in script.text
    assert "输入脚本/简单想法" in workbench_script.text
    assert "整理已有脚本" in workbench_script.text
    assert "扩写简单想法" in workbench_script.text
    assert "从零生成脚本" in workbench_script.text
    assert "制作前盘点" not in workbench_script.text
    assert "生成脚本草案" in workbench_script.text
    assert "生成分镜草案" in workbench_script.text
    assert "片段工作台" in workbench_script.text
    assert "生成候选配音" in workbench_script.text


def test_workbench_review_client_keeps_the_canvas_aspect_and_exposes_focus_review(client, projects_root):
    make_project(projects_root)

    script = client.get("/ui/workbench.js")
    stylesheet = client.get("/ui/workbench.css")

    assert script.status_code == 200
    assert stylesheet.status_code == 200
    assert "function reviewCanvasProfile()" in script.text
    assert "function reviewLayoutKind()" in script.text
    assert "function renderReviewFocusToolbar(scene)" in script.text
    assert "专注审核" in script.text
    assert "review-layout--${layoutKind}" in script.text
    assert "review-layout--portrait.is-focus" in stylesheet.text
    assert 'grid-template-areas: "stage side"' in stylesheet.text
    assert "position: sticky" in stylesheet.text
    assert "width: min(100%, var(--review-canvas-max-inline, 760px))" in stylesheet.text
    assert "max-height: none" in stylesheet.text


@pytest.mark.skipif(not _ffmpeg_available(), reason="ffmpeg is required for local splice verification")
def test_cached_strict_patch_reencodes_only_b_and_can_be_promoted(client, projects_root):
    project = make_project(projects_root)
    ffmpeg = _ffmpeg_available()
    final = project / "renders" / "final.mp4"
    final.parent.mkdir(exist_ok=True)
    candidate = project / "assets" / "video" / "candidate-real.mp4"
    for path, color, duration in ((final, "navy", "9"), (candidate, "gold", "5")):
        subprocess.run([
            ffmpeg, "-y", "-f", "lavfi", "-i", f"color=c={color}:s=320x180:r=30:d={duration}",
            "-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:duration={duration}",
            "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(path),
        ], check=True, capture_output=True)

    assert client.post("/api/project/film/workbench/bootstrap").status_code == 200
    registered = client.post("/api/project/film/workbench/assets", json={
        "name": "可替换候选", "type": "video", "source_type": "human_provided",
        "path": "assets/video/candidate-real.mp4", "license": "测试授权",
    })
    assert registered.status_code == 200
    cached = client.post("/api/project/film/workbench/baseline-cache")
    assert cached.status_code == 200, cached.text
    assert all(segment["versions"][0].get("artifact_path") for segment in cached.json()["segments"])

    client.post("/api/project/film/workbench/segments/SEG-002/freeze", json={"frozen": True})
    created = client.post("/api/project/film/workbench/patches", json={
        "segment_id": "SEG-002", "candidate_asset_id": "S-002", "instruction": "替换目标 B", "mode": "strict_freeze",
    })
    assert created.status_code == 200
    rendered = client.post("/api/project/film/workbench/patches/P-001/render")
    assert rendered.status_code == 200
    patch = rendered.json()["patches"][0]
    assert patch["status"] == "rendered"
    assert (project / patch["composition_candidate_path"]).is_file()
    concat_check = next(item for item in patch["render_report"]["checks"] if item["name"] == "A/B/C 合成")
    assert concat_check["ok"] is True

    promoted = client.post("/api/project/film/workbench/patches/P-001/promote")
    assert promoted.status_code == 200
    state = promoted.json()
    assert state["patches"][0]["status"] == "promoted"
    assert state["segments"][1]["current_version_id"].endswith("V002")
    assert (project / "artifacts" / "composition_manifest.json").is_file()


@pytest.mark.skipif(not _ffmpeg_available(), reason="ffmpeg is required for automatic production verification")
def test_scene_network_refresh_replaces_only_the_selected_scene(projects_root, monkeypatch):
    project = make_project(projects_root)
    project_meta = json.loads((project / "project.json").read_text(encoding="utf-8"))
    project_meta["render_profile"] = {"width": 320, "height": 180}
    write_json(project / "project.json", project_meta)
    monkeypatch.setenv("PEXELS_API_KEY", "test-key")
    ffmpeg = _ffmpeg_available()
    assert ffmpeg
    calls: list[dict] = []

    def fake_pexels_video(self, inputs):
        calls.append(dict(inputs))
        output = Path(inputs["output_path"])
        output.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run([
            ffmpeg, "-y", "-f", "lavfi", "-i", "color=c=navy:s=320x180:r=30:d=6",
            "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(output),
        ], check=True, capture_output=True)
        return SimpleNamespace(success=True, artifacts=[str(output)], data={
            "duration_seconds": 6, "width": 320, "height": 180,
            "video_id": f"test-video-{len(calls)}", "license": "Pexels License",
            "pexels_url": f"https://www.pexels.com/video/test-{len(calls)}/",
        }, error=None)

    monkeypatch.setattr(workbench_mod.PexelsVideo, "execute", fake_pexels_video)
    seeded = workbench_mod.bootstrap_workbench(project)
    seeded["automation"]["narration_generation"].update({"status": "completed"})
    workbench_mod._save(project, seeded)
    workbench_mod.start_network_asset_generation(project, {"confirmed": True, "fill_undecided": True})
    initial = workbench_mod.generate_network_assets(project)
    selected_before = {
        scene_id: next(item for item in initial["usages"] if item["scene_id"] == scene_id and item["role"] == "visual" and item["selected"])
        for scene_id in ("scene-a", "scene-b")
    }
    asset_ids_before = {asset["id"] for asset in initial["assets"]}

    started = workbench_mod.start_scene_network_asset_refresh(project, "scene-b", {
        "confirmed": True,
        "instruction": "Use a visibly different close-up angle.",
    })
    refresh_job = started["automation"]["asset_generation"]
    assert refresh_job["mode"] == "scene_refresh"
    assert refresh_job["scene_ids"] == ["scene-b"]
    assert refresh_job["refresh"]["previous_asset_id"] == selected_before["scene-b"]["asset_id"]
    assert next(scene for scene in started["scenes"] if scene["id"] == "scene-b")["review_status"] == "needs_adjustment"

    refreshed = workbench_mod.generate_network_assets(project)
    selected_after = {
        scene_id: next(item for item in refreshed["usages"] if item["scene_id"] == scene_id and item["role"] == "visual" and item["selected"])
        for scene_id in ("scene-a", "scene-b")
    }
    assert selected_after["scene-a"]["asset_id"] == selected_before["scene-a"]["asset_id"]
    assert selected_after["scene-a"]["id"] == selected_before["scene-a"]["id"]
    assert selected_after["scene-b"]["asset_id"] != selected_before["scene-b"]["asset_id"]
    assert selected_after["scene-b"]["id"] != selected_before["scene-b"]["id"]
    assert asset_ids_before <= {asset["id"] for asset in refreshed["assets"]}
    assert any(item["scene_id"] == "scene-b" and item["asset_id"] == selected_before["scene-b"]["asset_id"] and not item["selected"] for item in refreshed["usages"])
    assert calls[-1]["query"] != calls[1]["query"]
    assert calls[-1]["page"] == 1
    scene_b = next(scene for scene in refreshed["scenes"] if scene["id"] == "scene-b")
    assert scene_b["keyframe_review"]["status"] == "generated"
    assert all(anchor["status"] == "pending" for anchor in scene_b["anchors"])
    assert any(decision["category"] == "asset_refresh" and decision["subject"] == "scene-b 素材返工" for decision in refreshed["decisions"])


@pytest.mark.skipif(not _ffmpeg_available(), reason="ffmpeg is required for automatic production verification")
def test_separated_voicebox_narration_and_video_render_create_a_reviewable_final_video(projects_root, monkeypatch):
    """Narration can be listened to before the separately queued FFmpeg render."""
    project = make_project(projects_root)
    # The generic workbench fixture uses a byte placeholder for its many
    # metadata-only tests.  This integration test exercises the real FFmpeg
    # visual-timeline path and therefore needs a readable source image.
    Image.new("RGB", (320, 180), "navy").save(project / "assets" / "opening.png")
    project_meta = json.loads((project / "project.json").read_text(encoding="utf-8"))
    project_meta["render_profile"] = {"width": 320, "height": 180}
    write_json(project / "project.json", project_meta)
    monkeypatch.setenv("PEXELS_API_KEY", "test-key")
    ffmpeg = _ffmpeg_available()
    assert ffmpeg

    def fake_pexels_video(self, inputs):
        output = Path(inputs["output_path"])
        output.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run([
            ffmpeg, "-y", "-f", "lavfi", "-i", "color=c=navy:s=320x180:r=30:d=6",
            "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(output),
        ], check=True, capture_output=True)
        return SimpleNamespace(success=True, artifacts=[str(output)], data={
            "duration_seconds": 6, "width": 320, "height": 180, "video_id": "test-video",
            "license": "Pexels License", "pexels_url": "https://www.pexels.com/video/test/",
        }, error=None)

    def fake_voice_status(cls):
        return ToolStatus.AVAILABLE

    def fake_voicebox(self, inputs):
        output = Path(inputs["output_path"])
        output.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run([
            ffmpeg, "-y", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=2",
            "-c:a", "pcm_s16le", str(output),
        ], check=True, capture_output=True)
        return SimpleNamespace(success=True, artifacts=[str(output)], data={"output": str(output)}, error=None)

    monkeypatch.setattr(workbench_mod.VoiceboxTTS, "get_status", classmethod(fake_voice_status))
    monkeypatch.setattr(workbench_mod.VoiceboxTTS, "execute", fake_voicebox)
    monkeypatch.setattr(workbench_mod, "get_default_voice", lambda: {
        "id": "voice-yaya", "name": "雅雅", "default_engine": "qwen",
    })
    monkeypatch.setattr(workbench_mod.PexelsVideo, "execute", fake_pexels_video)
    workbench_mod.bootstrap_workbench(project)
    queued = workbench_mod.start_project_narration(project, {"confirmed": True})
    assert queued["automation"]["narration_generation"]["status"] == "generating"
    narrated = workbench_mod.generate_project_narration(project)
    assert narrated["automation"]["status"] == "narration_ready"
    assert narrated["automation"]["render"]["status"] == "awaiting_assets"
    assert (project / narrated["automation"]["narration_generation"]["audio_path"]).is_file()
    assert narrated["project"]["duration_seconds"] == 4
    first_take = next(scene for scene in narrated["scenes"] if scene["id"] == "scene-a")["narration"]["versions"][0]
    assert first_take["duration_seconds"] == 2
    assert not (project / "assets" / "audio" / "voicebox" / "scene-a-timed.wav").exists()

    started = workbench_mod.start_network_asset_generation(project, {"confirmed": True, "fill_undecided": True})
    assert started["automation"]["asset_generation"]["status"] == "generating"
    gathered = workbench_mod.generate_network_assets(project)
    assert gathered["automation"]["asset_generation"]["status"] == "completed", json.dumps(gathered["automation"]["asset_generation"], ensure_ascii=False)
    assert len([usage for usage in gathered["usages"] if usage["role"] == "visual" and usage["selected"]]) == 2
    assert all(scene["keyframe_review"]["status"] == "generated" for scene in gathered["scenes"])

    # This test covers the formal renderer itself; the full-preview approval
    # gate is covered separately without another expensive FFmpeg render.
    ready = workbench_mod._load_for_write(project)
    ready["narration_policy"]["playback_gain_db"] = 0.0
    for scene in ready["scenes"]:
        scene["review_status"] = "approved"
    workbench_mod._save(project, ready)
    render_queued = workbench_mod.start_project_video_render(project, {"confirmed": True})
    assert render_queued["automation"]["render"]["status"] == "generating"
    completed = workbench_mod.generate_project_video_render(project)
    assert completed["automation"]["status"] == "review_ready"
    assert completed["automation"]["render"]["runtime"] == "ffmpeg"
    assert (project / completed["automation"]["render"]["output_path"]).is_file()
    assert all(segment["versions"][0].get("artifact_path") for segment in completed["segments"])


def test_project_narration_freezes_cloud_provider_and_profile(projects_root, monkeypatch):
    project = make_project(projects_root)
    workbench_mod.bootstrap_workbench(project)
    monkeypatch.setattr(workbench_mod, "get_default_voice", lambda: {
        "id": "doubao:yaya",
        "name": "雅雅",
        "provider_id": "doubao",
        "provider_name": "豆包云端配音",
        "default_engine": "doubao_speech_2_0",
        "available": True,
    })

    queued = workbench_mod.start_project_narration(project, {"confirmed": True})

    assert queued["automation"]["voice"]["provider"] == "doubao"
    assert queued["automation"]["voice"]["provider_name"] == "豆包云端配音"
    assert queued["automation"]["voice"]["profile_id"] == "doubao:yaya"
    assert queued["automation"]["narration_generation"]["status"] == "generating"


def test_full_preview_is_independent_from_approvals_and_batch_confirmation(projects_root, monkeypatch):
    project = make_project(projects_root)
    state = workbench_mod.bootstrap_workbench(project)
    audio = project / "assets" / "audio" / "narration.wav"
    audio.parent.mkdir(parents=True, exist_ok=True)
    audio.write_bytes(b"audio")
    state["automation"]["narration_generation"].update({"status": "completed", "audio_path": "assets/audio/narration.wav"})
    state["narration_policy"]["playback_gain_db"] = 0.0
    state["assets"].append({"id": "S-002", "name": "second", "type": "image", "path": "assets/opening.png"})
    state["usages"].extend([
        {"id": "U-001", "scene_id": "scene-a", "asset_id": "S-001", "role": "visual", "selected": True},
        {"id": "U-002", "scene_id": "scene-b", "asset_id": "S-002", "role": "visual", "selected": True},
    ])
    workbench_mod._save(project, state)
    monkeypatch.setattr(workbench_mod.VideoCompose, "get_info", lambda _self: {"render_engines": {"ffmpeg": True, "remotion": False, "hyperframes": False}})

    queued = workbench_mod.start_full_preview_render(project, {"confirmed": True})
    assert queued["automation"]["preview_render"]["status"] == "generating"
    assert all(scene["review_status"] != "approved" for scene in queued["scenes"])
    with pytest.raises(workbench_mod.WorkbenchError, match="正式成片"):
        workbench_mod.start_project_video_render(project, {"confirmed": True})

    latest = workbench_mod._load_for_write(project)
    preview = project / "renders" / "previews" / "full-preview-v001.mp4"
    preview.parent.mkdir(parents=True, exist_ok=True)
    preview.write_bytes(b"candidate")
    latest["automation"]["preview_render"].update({"status": "completed", "output_path": "renders/previews/full-preview-v001.mp4", "version": 1})
    workbench_mod._save(project, latest)
    approved = workbench_mod.approve_full_preview_scenes(project, {"confirmed": True})
    assert all(scene["review_status"] == "approved" for scene in approved["scenes"])
    formal = workbench_mod.start_project_video_render(project, {"confirmed": True})
    assert formal["automation"]["render"]["status"] == "generating"

    workbench_mod._mark_render_needs_refresh(approved, "scene-a changed")
    assert approved["automation"]["preview_render"]["status"] == "needs_refresh"


def test_full_preview_failure_is_visible_in_derived_summary(projects_root):
    project = make_project(projects_root)
    state = workbench_mod.bootstrap_workbench(project)
    state["automation"]["preview_render"] = {
        "status": "generating",
        "runtime": "ffmpeg",
        "version": 4,
        "job_id": "PRJ-test",
        "output_path": None,
        "error": "",
    }
    workbench_mod._save(project, state)

    failed = workbench_mod.mark_full_preview_render_failed(
        project,
        workbench_mod.WorkbenchError("成片响度未达到发布容差"),
    )
    summary = workbench_mod.read_workbench(project)["full_preview"]

    assert failed["automation"]["preview_render"]["status"] == "failed"
    assert failed["automation"]["preview_render"]["version"] == 4
    assert "响度" in failed["automation"]["preview_render"]["error"]
    assert summary["preview"]["status"] == "failed"
    assert "响度" in summary["preview"]["error"]


def test_v2_full_track_sample_approval_migrates_only_when_signature_matches():
    def legacy_state(signature: str | None = None) -> dict:
        state = {
            "narration_policy": {"version": 1, "playback_gain_db": 6.0},
            "music_policy": {
                "version": 2,
                "enabled": True,
                "track_id": "news-opening-01",
                "playback_gain_db": -3.0,
                "loop": True,
                "fade_in_seconds": 0.8,
                "fade_out_seconds": 1.5,
                "sample": {"status": "approved", "policy_signature": signature},
            },
        }
        if signature is None:
            legacy_music = workbench_mod._legacy_music_policy_signature_v2(state["music_policy"])
            state["music_policy"]["sample"]["policy_signature"] = (
                workbench_mod._audio_mix_signature_for_music_signature(state, legacy_music)
            )
        return state

    matching = legacy_state()
    policy = workbench_mod._ensure_music_policy(matching)
    assert policy["version"] == 3
    assert policy["source_start_seconds"] == 0.0
    assert policy["source_end_seconds"] is None
    assert policy["sample"]["status"] == "approved"
    assert policy["sample"]["policy_signature"] == workbench_mod._audio_mix_signature(matching)

    mismatched = legacy_state("not-the-approved-audio-signature")
    untouched = workbench_mod._ensure_music_policy(mismatched)
    assert untouched["sample"]["policy_signature"] == "not-the-approved-audio-signature"


def test_background_music_policy_is_project_level_and_invalidates_whole_renders(projects_root, monkeypatch):
    project = make_project(projects_root)
    state = workbench_mod.bootstrap_workbench(project)
    old_preview = project / "renders" / "previews" / "old.mp4"
    old_preview.parent.mkdir(parents=True, exist_ok=True)
    old_preview.write_bytes(b"old")
    state["automation"]["preview_render"].update({
        "status": "completed", "output_path": "renders/previews/old.mp4", "version": 1,
    })
    workbench_mod._save(project, state)
    track_path = project / "news.wav"
    track_path.write_bytes(b"music")
    track = {
        "id": "news-opening-01", "title": "新闻传播序曲", "filename": "新闻传播序曲.wav",
        "source_calibration_db": -13.0, "playback_gain_db": 0.0,
        "duration_seconds": 60.0,
    }
    monkeypatch.setattr(workbench_mod, "resolve_music_track", lambda _track_id, _project_dir=None: (track_path, track))

    updated = workbench_mod.update_music_policy(project, {
        "enabled": True, "track_id": "news-opening-01", "playback_gain_db": -10,
    })
    assert updated["music_policy"]["enabled"] is True
    assert updated["music_policy"]["source_calibration_db"] == -13.0
    assert updated["music_policy"]["playback_gain_db"] == -10.0
    assert updated["music_policy"]["source_start_seconds"] == 0.0
    assert updated["music_policy"]["source_end_seconds"] == 60.0
    assert updated["music_policy"]["sample"]["status"] == "idle"
    assert updated["automation"]["preview_render"]["status"] == "needs_refresh"
    assert old_preview.is_file(), "changing BGM must not delete the previous preview"
    assert all(scene["review_status"] != "approved" for scene in updated["scenes"])


def test_narration_gain_is_independent_and_invalidates_only_render_derivatives(projects_root):
    project = make_project(projects_root)
    state = workbench_mod.bootstrap_workbench(project)
    # The workstation default is user-controlled and may already be +6 dB.
    # Freeze this project at unity so the test measures an actual change.
    state["narration_policy"]["playback_gain_db"] = 0.0
    immutable_voice = project / "assets" / "audio" / "voicebox-source.wav"
    immutable_avatar = project / "assets" / "video" / "avatar-source.mp4"
    immutable_voice.parent.mkdir(parents=True, exist_ok=True)
    immutable_avatar.parent.mkdir(parents=True, exist_ok=True)
    immutable_voice.write_bytes(b"voice-source")
    immutable_avatar.write_bytes(b"avatar-source")
    preview = project / "renders" / "previews" / "old.mp4"
    preview.parent.mkdir(parents=True, exist_ok=True)
    preview.write_bytes(b"old-preview")
    state["automation"]["preview_render"].update({"status": "completed", "output_path": "renders/previews/old.mp4"})
    state["music_policy"]["sample"].update({
        "status": "approved", "output_path": "renders/music-samples/old.mp4", "policy_signature": "old",
    })
    workbench_mod._save(project, state)

    updated = workbench_mod.update_narration_policy(project, {"playback_gain_db": 6.2})

    assert updated["narration_policy"]["playback_gain_db"] == 6.0
    assert updated["music_policy"]["sample"]["status"] == "stale"
    assert updated["automation"]["preview_render"]["status"] == "needs_refresh"
    assert immutable_voice.read_bytes() == b"voice-source"
    assert immutable_avatar.read_bytes() == b"avatar-source"
    assert preview.read_bytes() == b"old-preview"


def test_background_music_source_range_is_validated_and_invalidates_sample(projects_root, monkeypatch):
    project = make_project(projects_root)
    state = workbench_mod.bootstrap_workbench(project)
    track_path = project / "uploaded.wav"
    track_path.write_bytes(b"music")
    track = {
        "id": "project-music-demo",
        "title": "本地音乐",
        "filename": "project-music-demo.wav",
        "duration_seconds": 42.0,
        "scope": "project",
    }
    monkeypatch.setattr(
        workbench_mod,
        "resolve_music_track",
        lambda _track_id, _project_dir=None: (track_path, track),
    )
    state["music_policy"]["sample"].update({
        "status": "approved",
        "output_path": "renders/music-samples/old.mp4",
        "policy_signature": workbench_mod._audio_mix_signature(state),
    })
    workbench_mod._save(project, state)

    updated = workbench_mod.update_music_policy(project, {
        "enabled": True,
        "track_id": track["id"],
        "playback_gain_db": -8,
        "source_start_seconds": 12.5,
        "source_end_seconds": 31.25,
    })

    assert updated["music_policy"]["source_start_seconds"] == 12.5
    assert updated["music_policy"]["source_end_seconds"] == 31.25
    assert updated["music_policy"]["sample"]["status"] == "stale"
    with pytest.raises(workbench_mod.WorkbenchError, match="至少保留 1 秒"):
        workbench_mod.update_music_policy(project, {
            "enabled": True, "track_id": track["id"],
            "source_start_seconds": 2.0, "source_end_seconds": 2.5,
        })
    with pytest.raises(workbench_mod.WorkbenchError, match="不能超过音轨时长"):
        workbench_mod.update_music_policy(project, {
            "enabled": True, "track_id": track["id"],
            "source_start_seconds": 2.0, "source_end_seconds": 60.0,
        })


def test_audio_mix_signature_changes_for_narration_or_music(projects_root):
    project = make_project(projects_root)
    state = workbench_mod.bootstrap_workbench(project)
    # Do not depend on the user's current global narration preference.
    state["narration_policy"]["playback_gain_db"] = 0.0
    first = workbench_mod._audio_mix_signature(state)
    state["narration_policy"]["playback_gain_db"] = 6.0
    second = workbench_mod._audio_mix_signature(state)
    state["music_policy"]["playback_gain_db"] = -12.0
    third = workbench_mod._audio_mix_signature(state)

    assert first != second
    assert second != third


def test_legacy_project_migrates_to_unity_gain_not_new_global_default(projects_root, monkeypatch):
    project = make_project(projects_root)
    state = workbench_mod.bootstrap_workbench(project)
    state.pop("narration_policy", None)
    workbench_mod._save(project, state)
    monkeypatch.setattr(
        workbench_mod,
        "read_narration_preferences",
        lambda: {"version": 1, "playback_gain_db": 6.0},
    )

    migrated = workbench_mod.read_workbench(project)

    assert migrated["narration_policy"]["playback_gain_db"] == 0.0


@pytest.mark.skipif(not _ffmpeg_available(), reason="ffmpeg is required for narration gain measurement")
def test_narration_gain_changes_derivative_by_requested_db_without_touching_source(projects_root):
    project = make_project(projects_root)
    state = workbench_mod.bootstrap_workbench(project)
    ffmpeg = _ffmpeg_available()
    source = project / "source-with-voice.mp4"
    output = project / "renders" / "voice-plus-six.mp4"
    subprocess.run([
        ffmpeg, "-y", "-f", "lavfi", "-i", "color=c=navy:s=320x180:r=25:d=2",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=2",
        "-filter:a", "volume=-20dB", "-shortest", "-c:v", "libx264", "-c:a", "aac", str(source),
    ], check=True, capture_output=True)
    before_bytes = source.read_bytes()
    before = workbench_mod._measure_integrated_loudness(source, ffmpeg)["integrated_lufs"]
    state["narration_policy"]["playback_gain_db"] = 6.0

    report = workbench_mod._apply_project_narration_gain(project, state, source, output_path=output)
    after = workbench_mod._measure_integrated_loudness(output, ffmpeg)["integrated_lufs"]

    assert report["playback_gain_db"] == 6.0
    assert abs((after - before) - 6.0) <= 0.5
    assert source.read_bytes() == before_bytes


def test_background_music_sample_is_isolated_and_must_be_approved(projects_root, monkeypatch):
    project = make_project(projects_root)
    state = workbench_mod.bootstrap_workbench(project)
    preview = project / "renders" / "review-previews" / "scene-a-source.mp4"
    preview.parent.mkdir(parents=True, exist_ok=True)
    preview.write_bytes(b"voice-only-preview")
    scene = next(item for item in state["scenes"] if item["id"] == "scene-a")
    scene["review_preview"].update({"status": "ready", "output_path": "renders/review-previews/scene-a-source.mp4", "input_signature": "preview-v1"})
    state["narration_policy"]["playback_gain_db"] = 0.0
    workbench_mod._save(project, state)
    track_path = project / "news.wav"
    track_path.write_bytes(b"music")
    track = {"id": "news-opening-01", "title": "新闻传播序曲", "filename": "新闻传播序曲.wav", "duration_seconds": 60.0}
    monkeypatch.setattr(workbench_mod, "resolve_music_track", lambda _track_id, _project_dir=None: (track_path, track))
    monkeypatch.setattr(workbench_mod, "generate_scene_review_preview", lambda _project, _scene_id: workbench_mod._load_for_write(_project))

    def fake_mix(_project, _state, source, *, output_path=None):
        assert output_path is not None
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(source.read_bytes() + b"+mixed")
        return {"enabled": True, "output_path": workbench_mod._safe_relpath(_project, str(output_path))}

    monkeypatch.setattr(workbench_mod, "_apply_project_background_music", fake_mix)
    workbench_mod.update_music_policy(project, {"enabled": True, "track_id": "news-opening-01", "playback_gain_db": -12})
    queued = workbench_mod.start_music_sample(project, {})
    assert queued["music_policy"]["sample"]["status"] == "generating"
    rendered = workbench_mod.generate_music_sample(project)
    sample = rendered["music_policy"]["sample"]
    assert sample["status"] == "ready"
    assert (project / sample["output_path"]).read_bytes() == b"voice-only-preview+mixed"
    assert preview.read_bytes() == b"voice-only-preview", "sample must never overwrite the normal review preview"
    approved = workbench_mod.approve_music_sample(project, {"confirmed": True})
    assert approved["music_policy"]["sample"]["status"] == "approved"
    workbench_mod.update_music_policy(project, {"enabled": True, "track_id": "news-opening-01", "playback_gain_db": -9})
    stale = workbench_mod._load_for_write(project)["music_policy"]["sample"]
    assert stale["status"] == "stale"
    with pytest.raises(workbench_mod.WorkbenchError, match="第一段音量样板"):
        workbench_mod._require_approved_music_sample(workbench_mod._load_for_write(project))
    monkeypatch.setattr(workbench_mod, "_require_renderable_project", lambda *_args: None)
    with pytest.raises(workbench_mod.WorkbenchError, match="第一段音量样板"):
        workbench_mod.start_full_preview_render(project, {"confirmed": True})


@pytest.mark.skipif(not _ffmpeg_available(), reason="ffmpeg is required for narration timing verification")
def test_new_narration_marks_existing_short_visuals_for_refresh(projects_root, monkeypatch):
    """A changed voice clock must expose inadequate selected images/videos before render."""
    project = make_project(projects_root)
    ffmpeg = _ffmpeg_available()
    assert ffmpeg

    def fake_voice(self, inputs):
        output = Path(inputs["output_path"])
        output.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run([
            ffmpeg, "-y", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=2",
            "-c:a", "pcm_s16le", str(output),
        ], check=True, capture_output=True)
        return SimpleNamespace(success=True, artifacts=[str(output)], data={}, error=None)

    monkeypatch.setattr(workbench_mod.VoiceboxTTS, "get_status", classmethod(lambda cls: ToolStatus.AVAILABLE))
    monkeypatch.setattr(workbench_mod.VoiceboxTTS, "execute", fake_voice)
    monkeypatch.setattr(workbench_mod, "get_default_voice", lambda: {
        "id": "voice-yaya", "name": "雅雅", "default_engine": "qwen",
    })
    workbench_mod.bootstrap_workbench(project)
    for scene_id in ("scene-a", "scene-b"):
        visual = project / "assets" / "video" / f"{scene_id}-too-short.mp4"
        subprocess.run([
            ffmpeg, "-y", "-f", "lavfi", "-i", "color=c=navy:s=320x180:r=30:d=1",
            "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(visual),
        ], check=True, capture_output=True)
        state = workbench_mod.add_asset(project, {
            "name": f"{scene_id} 短素材", "type": "video", "source_type": "human_provided",
            "path": f"assets/video/{visual.name}", "duration_seconds": 1, "license": "测试授权",
        })
        workbench_mod.assign_usage(project, {"scene_id": scene_id, "asset_id": state["assets"][-1]["id"], "role": "visual"})

    workbench_mod.start_project_narration(project, {"confirmed": True})
    narrated = workbench_mod.generate_project_narration(project)

    assert narrated["automation"]["asset_generation"]["status"] == "needs_duration_refresh"
    assert narrated["automation"]["asset_generation"]["timing_issues"] == ["scene-a", "scene-b"]
    assert all(scene["visual_fit"]["strategy"] == "needs_replacement" for scene in narrated["scenes"])
    assert narrated["automation"]["render"]["status"] == "awaiting_assets"


@pytest.mark.skipif(not _ffmpeg_available(), reason="ffmpeg is required for scene narration hot-swap verification")
def test_scene_narration_candidate_is_auditioned_then_replaces_only_target_segment(projects_root, monkeypatch):
    """A Voicebox take remains a candidate until its B-only composition is promoted."""
    project = make_project(projects_root)
    ffmpeg = _ffmpeg_available()
    final = project / "renders" / "final.mp4"
    final.parent.mkdir(exist_ok=True)
    subprocess.run([
        ffmpeg, "-y", "-f", "lavfi", "-i", "color=c=navy:s=320x180:r=30:d=9",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=9",
        "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(final),
    ], check=True, capture_output=True)

    def fake_status(cls):
        return ToolStatus.AVAILABLE

    def fake_voice(self, inputs):
        output = Path(inputs["output_path"])
        output.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run([
            ffmpeg, "-y", "-f", "lavfi", "-i", "sine=frequency=660:sample_rate=48000:duration=2",
            "-c:a", "pcm_s16le", str(output),
        ], check=True, capture_output=True)
        return SimpleNamespace(success=True, data={"output": str(output)}, error=None)

    monkeypatch.setattr(workbench_mod.VoiceboxTTS, "get_status", classmethod(fake_status))
    monkeypatch.setattr(workbench_mod.VoiceboxTTS, "execute", fake_voice)
    monkeypatch.setattr(workbench_mod, "voice_catalog", lambda: {
        "provider": {"status": "available"},
        "default_voice": {"id": "voice-yaya", "name": "雅雅"},
        "profiles": [{"id": "voice-yaya", "name": "雅雅", "default_engine": "qwen"}],
    })

    workbench_mod.bootstrap_workbench(project)
    visual = project / "assets" / "video" / "scene-b-source.mp4"
    subprocess.run([
        ffmpeg, "-y", "-f", "lavfi", "-i", "color=c=teal:s=320x180:r=30:d=5",
        "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(visual),
    ], check=True, capture_output=True)
    with_visual = workbench_mod.add_asset(project, {
        "name": "场景 B 原始画面", "type": "video", "source_type": "human_provided",
        "path": "assets/video/scene-b-source.mp4", "duration_seconds": 5, "license": "测试授权",
    })
    workbench_mod.assign_usage(project, {"scene_id": "scene-b", "asset_id": with_visual["assets"][-1]["id"], "role": "visual"})
    cached = workbench_mod.build_baseline_cache(project)
    original_a = project / cached["segments"][0]["versions"][0]["artifact_path"]
    original_a_hash = __import__("hashlib").sha256(original_a.read_bytes()).hexdigest()

    queued = workbench_mod.start_scene_narration_candidate(project, "scene-b", {"profile_id": "voice-yaya", "text": "核心展示，换成雅雅的语音。"})
    assert next(scene for scene in queued["scenes"] if scene["id"] == "scene-b")["narration"]["job"]["status"] == "generating"
    candidate_state = workbench_mod.generate_scene_narration_candidate(project, "scene-b")
    scene_b = next(scene for scene in candidate_state["scenes"] if scene["id"] == "scene-b")
    candidate_id = scene_b["narration"]["candidate_version_id"]
    candidate = next(item for item in scene_b["narration"]["versions"] if item["id"] == candidate_id)
    assert candidate["status"] == "candidate"
    assert (project / candidate["audio_path"]).is_file()
    assert len(candidate["subtitle_cues"]) >= 1
    # A candidate from an older workbench may lack persisted timing.  Adoption
    # must measure its WAV rather than reverting to the old 5-second slot.
    candidate.pop("duration_seconds")
    candidate.pop("raw_duration_seconds")
    next(asset for asset in candidate_state["assets"] if asset["id"] == candidate["asset_id"]).pop("duration_seconds", None)
    workbench_mod._save(project, candidate_state)

    applying = workbench_mod.start_scene_narration_apply(project, "scene-b", candidate_id)
    patch = applying["patches"][-1]
    assert patch["change_scope"] == "audio"
    assert patch["mode"] == "ripple_timeline"
    assert patch["target_duration_seconds"] == 2
    assert patch["status"] == "rendering"
    rendered = workbench_mod.render_patch(project, patch["id"])
    patch = rendered["patches"][-1]
    assert patch["status"] == "rendered"
    assert __import__("hashlib").sha256(original_a.read_bytes()).hexdigest() == original_a_hash

    promoted = workbench_mod.promote_patch(project, patch["id"])
    scene_b = next(scene for scene in promoted["scenes"] if scene["id"] == "scene-b")
    assert scene_b["narration"]["current_version_id"] == candidate_id
    assert scene_b["end_seconds"] == 6
    assert promoted["project"]["duration_seconds"] == 6
    assert next(item for item in promoted["patches"] if item["id"] == patch["id"])["published_final_path"] == "renders/final.mp4"
    assert (project / "renders" / "final.mp4").is_file()


@pytest.mark.skipif(not _ffmpeg_available(), reason="ffmpeg is required for ripple narration verification")
def test_natural_scene_narration_ripple_keeps_a_and_c_content_while_retiming_c(projects_root, monkeypatch):
    """A shorter B take moves C without regenerating either neighbouring segment."""
    project = make_project(projects_root)
    write_json(project / "artifacts" / "script.json", {
        "title": "三段旁白时间线", "sections": [
            {"id": "s1", "text": "第一段", "start_seconds": 0, "end_seconds": 4},
            {"id": "s2", "text": "第二段", "start_seconds": 4, "end_seconds": 8},
            {"id": "s3", "text": "第三段", "start_seconds": 8, "end_seconds": 12},
        ],
    })
    write_json(project / "artifacts" / "scene_plan.json", {"scenes": [
        {"id": "scene-a", "description": "第一段画面", "start_seconds": 0, "end_seconds": 4, "script_section_id": "s1"},
        {"id": "scene-b", "description": "第二段画面", "start_seconds": 4, "end_seconds": 8, "script_section_id": "s2"},
        {"id": "scene-c", "description": "第三段画面", "start_seconds": 8, "end_seconds": 12, "script_section_id": "s3"},
    ]})
    ffmpeg = _ffmpeg_available()
    assert ffmpeg
    final = project / "renders" / "final.mp4"
    final.parent.mkdir(exist_ok=True)
    subprocess.run([
        ffmpeg, "-y", "-f", "lavfi", "-i", "color=c=navy:s=320x180:r=30:d=12",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=12",
        "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(final),
    ], check=True, capture_output=True)

    def fake_status(cls):
        return ToolStatus.AVAILABLE

    def fake_voice(self, inputs):
        output = Path(inputs["output_path"])
        output.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run([
            ffmpeg, "-y", "-f", "lavfi", "-i", "sine=frequency=660:sample_rate=48000:duration=2",
            "-c:a", "pcm_s16le", str(output),
        ], check=True, capture_output=True)
        return SimpleNamespace(success=True, data={"output": str(output)}, error=None)

    monkeypatch.setattr(workbench_mod.VoiceboxTTS, "get_status", classmethod(fake_status))
    monkeypatch.setattr(workbench_mod.VoiceboxTTS, "execute", fake_voice)
    monkeypatch.setattr(workbench_mod, "voice_catalog", lambda: {
        "provider": {"status": "available"}, "default_voice": {"id": "voice-yaya", "name": "雅雅"},
        "profiles": [{"id": "voice-yaya", "name": "雅雅", "default_engine": "qwen"}],
    })
    workbench_mod.bootstrap_workbench(project)
    visual = project / "assets" / "video" / "scene-b-source.mp4"
    subprocess.run([
        ffmpeg, "-y", "-f", "lavfi", "-i", "color=c=teal:s=320x180:r=30:d=5",
        "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(visual),
    ], check=True, capture_output=True)
    state = workbench_mod.add_asset(project, {
        "name": "B 段原始画面", "type": "video", "source_type": "human_provided",
        "path": "assets/video/scene-b-source.mp4", "duration_seconds": 5, "license": "测试授权",
    })
    workbench_mod.assign_usage(project, {"scene_id": "scene-b", "asset_id": state["assets"][-1]["id"], "role": "visual"})
    cached = workbench_mod.build_baseline_cache(project)
    before = {
        segment["id"]: __import__("hashlib").sha256((project / segment["versions"][0]["artifact_path"]).read_bytes()).hexdigest()
        for segment in cached["segments"] if segment["id"] in {"SEG-001", "SEG-003"}
    }

    workbench_mod.start_scene_narration_candidate(project, "scene-b", {"profile_id": "voice-yaya", "text": "更短的第二段。"})
    candidate_state = workbench_mod.generate_scene_narration_candidate(project, "scene-b")
    scene_b = next(scene for scene in candidate_state["scenes"] if scene["id"] == "scene-b")
    candidate_id = scene_b["narration"]["candidate_version_id"]
    candidate = next(item for item in scene_b["narration"]["versions"] if item["id"] == candidate_id)
    assert candidate["duration_seconds"] == 2
    assert candidate["timeline_impact"]["delta_seconds"] == -2
    assert candidate["subtitle_cues"][-1]["end_seconds"] == 2

    applying = workbench_mod.start_scene_narration_apply(project, "scene-b", candidate_id)
    patch = applying["patches"][-1]
    rendered = workbench_mod.render_patch(project, patch["id"])
    assert rendered["patches"][-1]["status"] == "rendered"
    promoted = workbench_mod.promote_patch(project, patch["id"])
    segments = {segment["id"]: segment for segment in promoted["segments"]}
    assert (segments["SEG-002"]["start_seconds"], segments["SEG-002"]["end_seconds"]) == (4, 6)
    assert (segments["SEG-003"]["start_seconds"], segments["SEG-003"]["end_seconds"]) == (6, 10)
    assert promoted["project"]["duration_seconds"] == 10
    assert __import__("hashlib").sha256((project / segments["SEG-001"]["versions"][0]["artifact_path"]).read_bytes()).hexdigest() == before["SEG-001"]
    assert __import__("hashlib").sha256((project / segments["SEG-003"]["versions"][0]["artifact_path"]).read_bytes()).hexdigest() == before["SEG-003"]
    assert workbench_mod._probe_duration_seconds(project / "renders" / "final.mp4", ffmpeg) == pytest.approx(10, abs=0.05)
