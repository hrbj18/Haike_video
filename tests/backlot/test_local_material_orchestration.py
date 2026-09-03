"""Focused contracts for the isolated local-material orchestration planner."""

from __future__ import annotations

import copy
import json
from pathlib import Path
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

from backlot.local_material_orchestration import (
    LocalMaterialOrchestrationError,
    build_orchestration_draft,
)
from backlot.media_index import media_content_fingerprint
from backlot import workbench as workbench_mod
from backlot import server as server_mod
from backlot import state as state_mod


def _state() -> dict:
    return {
        "assets": [{
            "id": "LOCAL-DUCK", "name": "机械鸭抓取", "type": "video", "source_type": "human_provided",
            "resolution": "1920x1080",
        }],
        "scenes": [
            {"id": "scene-a", "start_seconds": 0, "end_seconds": 5, "title": "机械鸭抓起零件", "description": "机械鸭抓起零件后放入收纳框。"},
            {"id": "scene-b", "start_seconds": 5, "end_seconds": 10, "title": "系统能力解释", "description": "这一段解释自动化系统的工作流程。"},
        ],
    }


def _index() -> dict:
    return {
        "signature": "a" * 64,
        "status": "completed",
        "source": {"fingerprint": "b" * 64},
        "vision": {"status": "completed"},
        "shots": [{
            "shot_id": "SHOT-0001", "start_seconds": 2, "end_seconds": 5.5,
            "frames": [{"frame_id": "FRAME-00001", "selected_for_vision": True}],
            "description": {
                "summary": "白色机械鸭抓起小零件并放进收纳框。",
                "entities": [{"name": "机械鸭"}, {"name": "零件"}],
                "actions": [{"name": "抓起"}, {"name": "放入"}],
                "unknowns": [],
            },
        }],
    }


def test_existing_script_is_preserved_and_local_evidence_becomes_a_draft_only():
    state = _state()
    before = copy.deepcopy(state["scenes"])

    draft = build_orchestration_draft(state, {"LOCAL-DUCK": _index()}, {"input_mode": "existing_script"})

    assert state["scenes"] == before
    assert draft["preparation"]["script_status"] == "provided"
    assert draft["scene_plans"][0]["status"] == "ready_for_adoption"
    assert draft["scene_plans"][0]["visual_role"] == "local_focus_card"
    assert draft["scene_plans"][1]["status"] == "needs_background"
    assert draft["material_capability_map"][0]["cut_policy"] == "safe_cut"


def test_generic_robot_words_cannot_misplace_local_footage_and_short_landscape_uses_a_focus_card():
    state = {
        "assets": [{"id": "GROUP", "name": "四色机器鸭", "type": "video", "resolution": "1920x1080"}],
        "scenes": [
            {"id": "specs", "start_seconds": 0, "end_seconds": 8, "title": "机器人的硬件规格", "description": "介绍电机和传感器。"},
            {"id": "keywords", "start_seconds": 8, "end_seconds": 16, "title": "四个关键词", "description": "训练、比较、复用、分享。"},
            {"id": "group", "start_seconds": 16, "end_seconds": 24, "title": "四色机器人同框", "description": "群体展示，不延伸能力判断。"},
        ],
    }
    index = _index()
    index["shots"][0]["description"] = {
        "summary": "四个颜色不同的小型机器人在木地板上同框。",
        "entities": [{"name": "四个小型机器人"}],
        "actions": [{"name": "坐姿停留"}],
        "unknowns": [],
    }

    draft = build_orchestration_draft(state, {"GROUP": index}, {"input_mode": "existing_script"})

    assert draft["scene_plans"][0]["status"] == "needs_background"
    assert draft["scene_plans"][1]["status"] == "needs_background"
    assert draft["scene_plans"][2]["status"] == "ready_for_adoption"
    assert draft["scene_plans"][2]["visual_role"] == "local_focus_card"
    assert "tag:group_display" in draft["scene_plans"][2]["matched_terms"]


def test_atomic_event_requires_explicit_confirmation_and_is_never_split_by_fast_cut_rule():
    state = _state()

    draft = build_orchestration_draft(state, {"LOCAL-DUCK": _index()}, {
        "input_mode": "existing_script",
        "continuity_confirmations": [{
            "asset_id": "LOCAL-DUCK", "shot_id": "SHOT-0001", "confirmed": True,
            "source_in_seconds": 2, "source_out_seconds": 5.5,
        }],
    })

    sequence = draft["sequences"][0]
    assert sequence["cut_policy"] == "atomic"
    assert sequence["source_out_seconds"] - sequence["source_in_seconds"] == sequence["display_end_seconds"] - sequence["display_start_seconds"]
    assert sequence["display_end_seconds"] == 3.5
    assert len([item for item in draft["sequences"] if item["scene_id"] == "scene-a"]) == 1


def test_atomic_event_that_does_not_fit_scene_is_warned_instead_of_trimmed_or_slowed():
    state = _state()
    state["scenes"][0]["end_seconds"] = 3

    draft = build_orchestration_draft(state, {"LOCAL-DUCK": _index()}, {
        "input_mode": "existing_script",
        "continuity_confirmations": [{
            "asset_id": "LOCAL-DUCK", "shot_id": "SHOT-0001", "confirmed": True,
            "source_in_seconds": 2, "source_out_seconds": 5.5,
        }],
    })

    assert draft["scene_plans"][0]["status"] == "needs_timing_decision"
    assert not draft["sequences"]


def test_materials_only_requires_a_human_direction_before_it_claims_to_have_a_script_plan():
    draft = build_orchestration_draft(_state(), {"LOCAL-DUCK": _index()}, {"input_mode": "materials_only"})

    assert draft["status"] == "needs_direction"
    assert draft["sequences"] == []
    assert draft["preparation"]["direction_status"] == "needs_direction"


def test_topic_mode_requires_direction_and_rejects_unconfirmed_atomic_claims():
    with pytest.raises(LocalMaterialOrchestrationError, match="主题"):
        build_orchestration_draft(_state(), {"LOCAL-DUCK": _index()}, {"input_mode": "topic_with_materials"})
    with pytest.raises(LocalMaterialOrchestrationError, match="明确确认"):
        build_orchestration_draft(_state(), {"LOCAL-DUCK": _index()}, {
            "input_mode": "existing_script",
            "continuity_confirmations": [{
                "asset_id": "LOCAL-DUCK", "shot_id": "SHOT-0001", "confirmed": False,
                "source_in_seconds": 2, "source_out_seconds": 5.5,
            }],
        })


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "film"
    (project / "artifacts").mkdir(parents=True)
    (project / "assets" / "video").mkdir(parents=True)
    (project / "assets" / "opening.png").write_bytes(b"image")
    (project / "assets" / "video" / "duck.mp4").write_bytes(b"private-test-bytes")
    (project / "project.json").write_text(json.dumps({"project_id": "film", "title": "机械鸭测试", "render_profile": {"width": 1920, "height": 1080}}), encoding="utf-8")
    (project / "artifacts" / "script.json").write_text(json.dumps({"title": "机械鸭测试", "sections": [{"id": "s1", "text": "机械鸭抓起零件后放入收纳框。", "start_seconds": 0, "end_seconds": 4}]}), encoding="utf-8")
    (project / "artifacts" / "scene_plan.json").write_text(json.dumps({"scenes": [{"id": "scene-a", "title": "机械鸭抓取", "description": "机械鸭抓起零件后放入收纳框。", "start_seconds": 0, "end_seconds": 4, "script_section_id": "s1"}]}), encoding="utf-8")
    (project / "artifacts" / "asset_manifest.json").write_text(json.dumps({"assets": [{"id": "opening", "type": "image", "path": "assets/opening.png", "scene_id": "scene-a", "source_tool": "provided_asset"}]}), encoding="utf-8")
    return project


def _attach_completed_vision(project: Path, resolution: str = "1920x1080") -> tuple[dict, dict]:
    state = workbench_mod.bootstrap_workbench(project)
    duck = workbench_mod._append_asset(project, state, {
        "name": "机械鸭抓取", "type": "video", "source_type": "human_provided",
        "path": "assets/video/duck.mp4", "duration_seconds": 5.5, "resolution": resolution, "license": "test",
    })
    source = project / duck["path"]
    index_path = project / "artifacts" / "media-index" / duck["id"] / "vision.json"
    index_path.parent.mkdir(parents=True)
    index = _index()
    index["source"] = {"fingerprint": media_content_fingerprint(source), "name": source.name}
    index_path.write_text(json.dumps(index), encoding="utf-8")
    duck["media_index"] = {"vision_index_path": str(index_path.relative_to(project)).replace("\\", "/"), "status": "completed"}
    workbench_mod._save(project, state)
    return state, duck


def test_workbench_draft_then_adopt_changes_only_target_scene_and_preserves_explicit_source_window(tmp_path: Path):
    project = _project(tmp_path)
    state, duck = _attach_completed_vision(project)
    original_timeline = copy.deepcopy(state["scenes"][0]["visual_timeline"])

    drafted = workbench_mod.create_local_material_orchestration(project, {
        "input_mode": "existing_script",
        "continuity_confirmations": [{
            "asset_id": duck["id"], "shot_id": "SHOT-0001", "confirmed": True,
            "source_in_seconds": 2, "source_out_seconds": 5.5,
        }],
    })
    scene = drafted["scenes"][0]
    assert scene["visual_timeline"] == original_timeline
    draft = drafted["local_material_orchestration"]
    plan = draft["scene_plans"][0]
    assert plan["visual_role"] == "local_full_bleed"

    adopted = workbench_mod.adopt_local_material_orchestration_scene(project, "scene-a", {
        "expected_orchestration_revision": draft["revision"],
        "expected_timeline_revision": scene["visual_timeline"]["revision"],
        "expected_composition_revision": scene["visual_composition"]["revision"],
    })
    saved_scene = adopted["scenes"][0]
    block = saved_scene["visual_timeline"]["blocks"][0]
    assert block["asset_id"] == duck["id"]
    assert block["source_in_seconds"] == 2
    assert block["source_out_seconds"] == 5.5
    assert block["cut_policy"] == "atomic"
    assert block["planner_evidence"]["source"] == "local_material_orchestration_v1"
    assert saved_scene["review_preview"]["status"] in {"idle", "stale"}
    assert adopted["local_material_orchestration"]["scene_plans"][0]["status"] == "adopted"


def test_workbench_rejects_stale_script_or_locked_scene_before_it_overwrites_anything(tmp_path: Path):
    project = _project(tmp_path)
    state, duck = _attach_completed_vision(project)
    drafted = workbench_mod.create_local_material_orchestration(project, {"input_mode": "existing_script"})
    draft = drafted["local_material_orchestration"]
    # The plan is generated first, then a human changes the scene.  Adoption
    # must fail rather than writing an old semantic decision over new copy.
    changed = workbench_mod._load_for_write(project)
    changed["scenes"][0]["description"] = "人工已经改写过的台词。"
    workbench_mod._save(project, changed)
    with pytest.raises(workbench_mod.WorkbenchConflict, match="脚本"):
        workbench_mod.adopt_local_material_orchestration_scene(project, "scene-a", {
            "expected_orchestration_revision": draft["revision"],
            "expected_timeline_revision": drafted["scenes"][0]["visual_timeline"]["revision"],
            "expected_composition_revision": drafted["scenes"][0]["visual_composition"]["revision"],
        })


def test_workbench_rejects_locked_timeline_and_focus_card_reuses_existing_layer_contract(tmp_path: Path):
    project = _project(tmp_path)
    state, duck = _attach_completed_vision(project, "720x1280")
    drafted = workbench_mod.create_local_material_orchestration(project, {"input_mode": "existing_script"})
    draft = drafted["local_material_orchestration"]
    scene = drafted["scenes"][0]
    assert draft["scene_plans"][0]["visual_role"] == "local_focus_card"

    adopted = workbench_mod.adopt_local_material_orchestration_scene(project, "scene-a", {
        "expected_orchestration_revision": draft["revision"],
        "expected_timeline_revision": scene["visual_timeline"]["revision"],
        "expected_composition_revision": scene["visual_composition"]["revision"],
    })
    overlay = adopted["scenes"][0]["visual_composition"]["overlays"][0]
    assert overlay["asset_id"] == duck["id"]
    assert overlay["muted"] is True and overlay["playback_rate"] == 1
    assert overlay["planner_evidence"]["sequence_id"].startswith("LMS-")

    locked_project = _project(tmp_path / "locked")
    locked_state, locked_duck = _attach_completed_vision(locked_project)
    locked_draft_state = workbench_mod.create_local_material_orchestration(locked_project, {
        "input_mode": "existing_script",
        "continuity_confirmations": [{
            "asset_id": locked_duck["id"], "shot_id": "SHOT-0001", "confirmed": True,
            "source_in_seconds": 2, "source_out_seconds": 5.5,
        }],
    })
    locked_draft = locked_draft_state["local_material_orchestration"]
    changed = workbench_mod._load_for_write(locked_project)
    changed["scenes"][0]["visual_timeline"]["blocks"][0]["locked"] = True
    workbench_mod._save(locked_project, changed)
    with pytest.raises(workbench_mod.WorkbenchError, match="锁定"):
        workbench_mod.adopt_local_material_orchestration_scene(locked_project, "scene-a", {
            "expected_orchestration_revision": locked_draft["revision"],
            "expected_timeline_revision": locked_draft_state["scenes"][0]["visual_timeline"]["revision"],
            "expected_composition_revision": locked_draft_state["scenes"][0]["visual_composition"]["revision"],
        })


def test_workbench_client_makes_material_driven_draft_and_adoption_visible_without_a_hidden_auto_publish():
    client = (Path(__file__).parents[2] / "backlot" / "ui" / "workbench.js").read_text(encoding="utf-8")

    assert "renderLocalMaterialOrchestrationPanel" in client
    assert '"/local-material-orchestration"' in client
    assert "/local-material-orchestration/scenes/${encodeURIComponent(scene.id)}/adopt" in client
    assert "确认完整动作" in client
    assert "expected_orchestration_revision" in client
    assert "localMaterialContinuityConfirmations" in client


def test_workbench_http_routes_create_a_draft_before_any_scene_adoption(tmp_path: Path, monkeypatch):
    projects_root = tmp_path / "projects"
    projects_root.mkdir()
    project = _project(projects_root)
    _attach_completed_vision(project)
    monkeypatch.setattr(state_mod, "PROJECTS_DIR", projects_root)
    monkeypatch.setattr(server_mod, "PROJECTS_DIR", projects_root)
    monkeypatch.setattr(server_mod, "_summary_cache", {})
    monkeypatch.setattr(server_mod, "_PROJECTS_ROOT_STR", __import__("os").path.normcase(str(projects_root.resolve())))

    async def no_watch():
        return None

    monkeypatch.setattr(server_mod, "_watch_projects", no_watch)
    with TestClient(server_mod.create_app()) as client:
        response = client.post("/api/project/film/workbench/local-material-orchestration", json={"input_mode": "existing_script"})
        body = response.json()
        scene = body["scenes"][0]
        adopted = client.post(
            "/api/project/film/workbench/local-material-orchestration/scenes/scene-a/adopt",
            json={
                "expected_orchestration_revision": body["local_material_orchestration"]["revision"],
                "expected_timeline_revision": scene["visual_timeline"]["revision"],
                "expected_composition_revision": scene["visual_composition"]["revision"],
            },
        )

    assert response.status_code == 200, response.text
    assert body["local_material_orchestration"]["status"] == "draft"
    assert body["scenes"][0]["visual_composition"]["layout_recipe"] == "full_bleed"
    assert adopted.status_code == 200, adopted.text
    adopted_scene = adopted.json()["scenes"][0]
    assert adopted_scene["visual_composition"]["layout_recipe"] == "focus_card"
    assert adopted_scene["visual_composition"]["overlays"][0]["asset_id"]


def test_local_full_bleed_materializer_trims_confirmed_source_window_without_looping(tmp_path: Path, monkeypatch):
    project = _project(tmp_path)
    source = project / "assets" / "video" / "duck.mp4"
    state = workbench_mod.bootstrap_workbench(project)
    asset = workbench_mod._append_asset(project, state, {
        "name": "机械鸭", "type": "video", "source_type": "human_provided", "path": "assets/video/duck.mp4",
        "duration_seconds": 5.5, "resolution": "1920x1080", "license": "test",
    })
    scene = state["scenes"][0]
    scene["end_seconds"] = 3.5
    scene["visual_timeline"] = {"version": 2, "revision": 1, "blocks": [{
        "id": "VB-001", "start_seconds": 0, "end_seconds": 3.5, "asset_id": asset["id"], "source_mode": "human_provided",
        "source_in_seconds": 2, "source_out_seconds": 5.5, "visual_role": "local_full_bleed", "cut_policy": "atomic", "sequence_id": "LMS-001",
        "planner_evidence": {"source": "local_material_orchestration_v1", "shot_id": "SHOT-0001", "index_fingerprint": "a" * 64, "sequence_id": "LMS-001", "cut_policy": "atomic"},
    }]}
    captured: list[list[str]] = []

    def fake_run(command: list[str]):
        captured.append(command)
        Path(command[-1]).write_bytes(b"silent-visual")
        return True, "ok"

    monkeypatch.setattr(workbench_mod, "_run_media", fake_run)
    output = workbench_mod._materialize_scene_visual_timeline(project, state, scene, "ffmpeg")

    assert output.is_file()
    command = captured[0]
    assert "-ss" in command and command[command.index("-ss") + 1] == "2.000000"
    assert "-stream_loop" not in command


def test_local_full_bleed_materializer_renders_a_real_non_looping_synthetic_clip(tmp_path: Path):
    """Exercise the actual FFmpeg path without using any live provider or user media."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("FFmpeg is not installed in this isolated test environment")
    project = _project(tmp_path)
    source = project / "assets" / "video" / "duck.mp4"
    subprocess.run([
        ffmpeg, "-y", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=25", "-t", "6",
        "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(source),
    ], check=True, capture_output=True, timeout=30)
    state = workbench_mod.bootstrap_workbench(project)
    asset = workbench_mod._append_asset(project, state, {
        "name": "合成机械鸭", "type": "video", "source_type": "human_provided", "path": "assets/video/duck.mp4",
        "duration_seconds": 6, "resolution": "320x180", "license": "synthetic-test",
    })
    scene = state["scenes"][0]
    scene["end_seconds"] = 3.5
    scene["visual_timeline"] = {"version": 2, "revision": 1, "blocks": [{
        "id": "VB-001", "start_seconds": 0, "end_seconds": 3.5, "asset_id": asset["id"], "source_mode": "human_provided",
        "source_in_seconds": 2, "source_out_seconds": 5.5, "visual_role": "local_full_bleed", "cut_policy": "atomic", "sequence_id": "LMS-001",
        "planner_evidence": {"source": "local_material_orchestration_v1", "shot_id": "SHOT-0001", "index_fingerprint": "a" * 64, "sequence_id": "LMS-001", "cut_policy": "atomic"},
    }]}

    output = workbench_mod._materialize_scene_visual_timeline(project, state, scene, ffmpeg)

    assert output.is_file()
    assert workbench_mod._probe_duration_seconds(output, ffmpeg, 0) == pytest.approx(3.5, abs=0.12)
