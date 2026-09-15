"""Contract, persistence, compatibility, and real FFmpeg tests for text layers."""

from __future__ import annotations

import json
import os
import subprocess
from copy import deepcopy
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backlot import server as server_mod
from backlot import state as state_mod
from backlot import workbench as workbench_mod
from backlot.text_overlay_composition import (
    TextOverlayValidationError,
    _font_path,
    _font_variation,
    assert_locked_layer_transition,
    build_text_overlay_assets,
    composition_layers_for_window,
    normalize_text_overlay_composition,
)
from tools.video.video_compose import VideoCompose


def layer(layer_id: str, **overrides) -> dict:
    value = {
        "id": layer_id, "text": f"标题 {layer_id}",
        "start_seconds": 0, "end_seconds": 30,
        "x": .06, "y": .06, "width": .4, "height": .08,
        "font_family": "Microsoft YaHei", "font_size": 54, "font_weight": 700,
        "color": "#FFFFFF", "stroke_color": "#111111", "stroke_width": 2,
        "shadow_color": "#00000080", "shadow_blur": 2, "shadow_offset_x": 2, "shadow_offset_y": 3,
        "line_height": 1.15, "text_align": "center",
        "background_color": "#D81E06", "background_opacity": .85,
        "background_radius": 30, "padding_x": 18, "padding_y": 10,
        "enter_animation": "fade", "enter_duration_seconds": .3,
        "exit_animation": "fade", "exit_duration_seconds": .3,
        "z_index": 0, "locked": False,
    }
    value.update(overrides)
    return value


def composition(*layers: dict, revision: int = 0) -> dict:
    return {"version": 1, "revision": revision, "layers": list(layers)}


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def make_project(root: Path) -> Path:
    project = root / "film"
    (project / "artifacts").mkdir(parents=True)
    (project / "assets").mkdir(parents=True)
    write_json(project / "project.json", {"project_id": "film", "title": "标题测试", "pipeline_type": "cinematic"})
    write_json(project / "artifacts" / "script.json", {"title": "标题测试", "sections": [
        {"id": "s1", "text": "一", "start_seconds": 0, "end_seconds": 4},
        {"id": "s2", "text": "二", "start_seconds": 4, "end_seconds": 9},
    ]})
    write_json(project / "artifacts" / "scene_plan.json", {"scenes": [
        {"id": "scene-a", "description": "一", "start_seconds": 0, "end_seconds": 4, "script_section_id": "s1"},
        {"id": "scene-b", "description": "二", "start_seconds": 4, "end_seconds": 9, "script_section_id": "s2"},
    ]})
    write_json(project / "artifacts" / "asset_manifest.json", {"assets": []})
    return project


@pytest.fixture
def projects_root(tmp_path: Path, monkeypatch):
    root = tmp_path / "projects"
    root.mkdir()
    monkeypatch.setattr(state_mod, "PROJECTS_DIR", root)
    monkeypatch.setattr(server_mod, "PROJECTS_DIR", root)
    monkeypatch.setattr(server_mod, "_summary_cache", {})
    monkeypatch.setattr(server_mod, "_PROJECTS_ROOT_STR", os.path.normcase(str(root.resolve())))
    return root


@pytest.fixture
def client(projects_root: Path, monkeypatch):
    async def no_watch():
        return None
    monkeypatch.setattr(server_mod, "_watch_projects", no_watch)
    with TestClient(server_mod.create_app()) as instance:
        yield instance


def test_four_layers_normalize_with_independent_style_and_unknown_fields() -> None:
    raw = composition(
        layer("T1", text="第一行\n第二行", color="#FF0000", z_index=4, future_field="keep"),
        layer("T2", x=.52, color="#00FF00", background_color="#001122"),
        layer("T3", y=.2, font_size=72, enter_animation="slide_up"),
        layer("T4", y=.32, text_align="right", exit_animation="scale"),
    )
    normalized = normalize_text_overlay_composition(raw)
    assert len(normalized["layers"]) == 4
    assert normalized["layers"][0]["text"] == "第一行\n第二行"
    assert normalized["layers"][0]["future_field"] == "keep"
    assert {item["color"] for item in normalized["layers"]} == {"#FF0000", "#00FF00", "#FFFFFF"}


@pytest.mark.parametrize("field,value", [
    ("x", 1.1), ("width", 0), ("enter_animation", "spin"), ("end_seconds", 0),
])
def test_contract_rejects_invalid_geometry_time_and_animation(field: str, value) -> None:
    with pytest.raises(TextOverlayValidationError):
        normalize_text_overlay_composition(composition(layer("T1", **{field: value})))


def test_scene_window_clips_project_clock_and_disables_already_started_animation() -> None:
    normalized = normalize_text_overlay_composition(composition(layer("T1", start_seconds=2, end_seconds=8)))
    visible = composition_layers_for_window(normalized, 4, 6)
    assert visible[0]["start_seconds"] == 0
    assert visible[0]["end_seconds"] == 2
    assert visible[0]["enter_animation"] == "none"
    assert visible[0]["exit_animation"] == "none"


def test_locked_layer_requires_standalone_unlock() -> None:
    previous = normalize_text_overlay_composition(composition(layer("T1", locked=True), layer("T2"), revision=1))
    changed = normalize_text_overlay_composition(composition(layer("T1", locked=True, text="改动"), layer("T2"), revision=1))
    with pytest.raises(TextOverlayValidationError, match="请先解锁"):
        assert_locked_layer_transition(previous, changed)
    unlocked_plus_change = normalize_text_overlay_composition(composition(layer("T1", locked=False), layer("T2", text="同时改"), revision=1))
    with pytest.raises(TextOverlayValidationError, match="解锁必须单独保存"):
        assert_locked_layer_transition(previous, unlocked_plus_change)
    unlocked = normalize_text_overlay_composition(composition(layer("T1", locked=False), layer("T2"), revision=1))
    assert_locked_layer_transition(previous, unlocked)


def test_save_persists_revision_and_invalidates_only_intersecting_scene(projects_root: Path) -> None:
    project = make_project(projects_root)
    state = workbench_mod.bootstrap_workbench(project)
    for scene in state["scenes"]:
        scene["review_preview"] = {"status": "ready", "output_path": f"renders/{scene['id']}.mp4"}
    narration_before = json.loads(json.dumps(state["automation"]["narration_generation"]))
    workbench_mod._save(project, state)
    updated = workbench_mod.update_text_overlay_composition(project, {
        "expected_revision": 0,
        "composition": composition(layer("T1", start_seconds=.5, end_seconds=3.5)),
    })
    assert updated["text_overlay_composition"]["revision"] == 1
    assert updated["scenes"][0]["review_preview"]["status"] == "stale"
    assert updated["scenes"][1]["review_preview"]["status"] == "ready"
    assert updated["automation"]["narration_generation"] == narration_before
    persisted = json.loads((project / "artifacts" / "workbench.json").read_text(encoding="utf-8"))
    assert persisted["text_overlay_composition"]["layers"][0]["id"] == "T1"


def test_api_returns_409_for_stale_text_overlay_revision(projects_root: Path, client: TestClient) -> None:
    project = make_project(projects_root)
    workbench_mod.bootstrap_workbench(project)
    url = "/api/project/film/workbench/text-overlay-composition"
    first = client.put(url, json={"expected_revision": 0, "composition": composition(layer("T1"))})
    assert first.status_code == 200
    stale = client.put(url, json={"expected_revision": 0, "composition": composition(layer("T2"))})
    assert stale.status_code == 409
    assert "当前版本 1" in stale.json()["detail"]
    current = client.get(url)
    assert current.status_code == 200
    assert current.json()["revision"] == 1


def test_legacy_news_headline_is_adapted_without_state_migration(tmp_path: Path) -> None:
    state = {"scenes": [{
        "story_id": "S01", "start_seconds": 0, "end_seconds": 5,
        "headline_overlay": {"mode": "two_line", "line_1": "全球首个", "line_2": "太空算力云开放服务"},
    }]}
    legacy, legacy_report = workbench_mod._daily_story_headline_overlays(tmp_path, state, 1080, 1920)
    combined, report = workbench_mod._project_text_overlay_overlays(tmp_path, state, 1080, 1920)
    assert "text_overlay_composition" not in state
    assert combined[0]["asset_path"] == legacy[0]["asset_path"]
    assert combined[0]["text_preset_id"] == "daily_news_headline_v1"
    assert report["presets"]["daily_news_headline_v1"]["assets"] == legacy_report["assets"]


def _configure_two_news_stories(project: Path) -> dict:
    state = workbench_mod.bootstrap_workbench(project)
    state["scenes"][0].update({
        "story_id": "S01",
        "headline_overlay": {
            "mode": "one_line", "line_1": "第一条科技新闻",
            "line_2": "", "style_id": "daily_news_headline_v1",
        },
        "review_preview": {"status": "ready", "output_path": "renders/s01.mp4"},
        "review_status": "approved",
    })
    state["scenes"][1].update({
        "story_id": "S02",
        "headline_overlay": {
            "mode": "one_line", "line_1": "第二条科技新闻",
            "line_2": "", "style_id": "daily_news_headline_v1",
        },
        "review_preview": {"status": "ready", "output_path": "renders/s02.mp4"},
        "review_status": "approved",
    })
    return workbench_mod._save(project, state)


def test_news_headlines_are_projected_as_stable_managed_editor_layers(projects_root: Path) -> None:
    project = make_project(projects_root)
    state = _configure_two_news_stories(project)
    editor = state["text_overlay_editor_composition"]
    assert [item["id"] for item in editor["layers"]] == ["news:S01:1", "news:S02:1"]
    assert [item["source_story_id"] for item in editor["layers"]] == ["S01", "S02"]
    assert [item["text"] for item in editor["layers"]] == ["第一条科技新闻", "第二条科技新闻"]
    assert [(item["start_seconds"], item["end_seconds"]) for item in editor["layers"]] == [(0.0, 4.0), (4.0, 9.0)]
    persisted = json.loads((project / "artifacts" / "workbench.json").read_text(encoding="utf-8"))
    assert "text_overlay_editor_composition" not in persisted
    assert "text_overlay_composition" not in persisted


def test_save_news_layer_style_is_independent_and_only_invalidates_its_story(projects_root: Path) -> None:
    project = make_project(projects_root)
    state = _configure_two_news_stories(project)
    editor = deepcopy(state["text_overlay_editor_composition"])
    first = editor["layers"][0]
    first.update({
        "font_size_mode": "fixed", "font_size": 82,
        "color": "#19A974", "x": .08, "y": .1, "width": .7, "height": .14,
    })
    narration_before = deepcopy(state["automation"]["narration_generation"])
    updated = workbench_mod.update_text_overlay_composition(project, {
        "expected_revision": 0, "composition": editor,
    })
    effective = updated["text_overlay_editor_composition"]
    first_after, second_after = effective["layers"]
    assert first_after["font_size_mode"] == "fixed"
    assert first_after["font_size"] == 82
    assert first_after["color"] == "#19A974"
    assert (first_after["x"], first_after["y"], first_after["width"], first_after["height"]) == (.08, .1, .7, .14)
    assert second_after["color"] == "#FFD400"
    assert updated["text_overlay_composition"] == {"version": 1, "revision": 1, "layers": []}
    assert set(updated["story_headline_overrides"]) == {"news:S01:1"}
    assert updated["scenes"][0]["review_preview"]["status"] == "stale"
    assert updated["scenes"][1]["review_preview"]["status"] == "ready"
    assert updated["automation"]["narration_generation"] == narration_before


def test_managed_news_layer_cannot_be_deleted_or_retimed(projects_root: Path) -> None:
    project = make_project(projects_root)
    state = _configure_two_news_stories(project)
    editor = deepcopy(state["text_overlay_editor_composition"])
    with pytest.raises(workbench_mod.WorkbenchError, match="不能新增或删除"):
        workbench_mod.update_text_overlay_composition(project, {
            "expected_revision": 0,
            "composition": {**editor, "layers": editor["layers"][1:]},
        })
    changed_time = deepcopy(editor)
    changed_time["layers"][0]["end_seconds"] = 3.5
    with pytest.raises(workbench_mod.WorkbenchError, match="文案和时间由新闻脚本管理"):
        workbench_mod.update_text_overlay_composition(project, {
            "expected_revision": 0, "composition": changed_time,
        })


def test_news_style_override_changes_local_render_asset_and_geometry(projects_root: Path) -> None:
    from PIL import Image

    project = make_project(projects_root)
    state = _configure_two_news_stories(project)
    editor = deepcopy(state["text_overlay_editor_composition"])
    editor["layers"][0].update({
        "font_size_mode": "fixed", "font_size": 72, "color": "#00FF00",
        "x": .1, "y": .12, "width": .65, "height": .15,
    })
    updated = workbench_mod.update_text_overlay_composition(project, {
        "expected_revision": 0, "composition": editor,
    })
    overlays, report = workbench_mod._daily_story_headline_overlays(project, updated, 1080, 1920)
    assert len(overlays) == 2
    assert overlays[0]["text_layer_id"] == "news:S01:1"
    assert (overlays[0]["x"], overlays[0]["y"], overlays[0]["width"], overlays[0]["height"]) == (108, 230, 702, 288)
    assert report["assets"][0]["font_size"] == 72
    assert report["assets"][0]["font_size_mode"] == "fixed"
    assert report["assets"][0]["color"] == "#00FF00"
    asset_path = project / report["assets"][0]["path"]
    assert asset_path.is_file()
    with Image.open(asset_path).convert("RGBA") as image:
        assert image.size == (702, 288)
        rgba = image.tobytes()
        assert any(rgba[index + 1] > 220 and rgba[index] < 80 and rgba[index + 2] < 80 and rgba[index + 3] > 0 for index in range(0, len(rgba), 4))
    combined, _ = workbench_mod._project_text_overlay_overlays(project, updated, 1080, 1920)
    assert len(combined) == 2
    assert [item["text_layer_id"] for item in combined] == ["news:S01:1", "news:S02:1"]


def test_text_overlay_api_get_includes_managed_news_layers(projects_root: Path, client: TestClient) -> None:
    project = make_project(projects_root)
    _configure_two_news_stories(project)
    response = client.get("/api/project/film/workbench/text-overlay-composition")
    assert response.status_code == 200
    assert [item["id"] for item in response.json()["layers"]] == ["news:S01:1", "news:S02:1"]


def test_video_compose_really_renders_four_animated_layers(tmp_path: Path) -> None:
    ffmpeg = workbench_mod._ffmpeg_available()
    if not ffmpeg:
        pytest.skip("ffmpeg unavailable")
    base = tmp_path / "base.mp4"
    made = subprocess.run([
        ffmpeg, "-y", "-f", "lavfi", "-i", "color=c=#152033:s=320x568:r=30:d=2",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(base),
    ], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60, check=False)
    assert made.returncode == 0, made.stderr
    contract = normalize_text_overlay_composition(composition(
        layer("T1", end_seconds=2, width=.38, height=.1, enter_animation="fade"),
        layer("T2", end_seconds=2, x=.52, width=.4, height=.1, enter_animation="slide_left", color="#00FF00", z_index=2),
        layer("T3", end_seconds=2, y=.22, width=.4, height=.1, enter_animation="scale", color="#00AAFF", z_index=3),
        layer("T4", end_seconds=2, x=.52, y=.22, width=.4, height=.1, enter_animation="slide_up", color="#FFD400", z_index=4),
    ))
    overlays, report = build_text_overlay_assets(tmp_path, contract, 320, 568)
    output = tmp_path / "four-layers.mp4"
    result = VideoCompose().execute({
        "operation": "overlay", "input_path": str(base), "overlays": overlays,
        "output_path": str(output), "codec": "libx264", "crf": 22,
    })
    assert result.success, result.error
    assert output.is_file() and output.stat().st_size > 1000


def test_noto_family_resolves_to_the_variable_font_with_a_weight_named_instance() -> None:
    resolved = _font_path("Noto Sans SC", True)
    assert resolved is not None and resolved.name == "NotoSansSC-VF.ttf"
    assert _font_variation(layer("T1", font_family="Noto Sans SC", font_weight=900)) == "Black"
    assert _font_variation(layer("T1", font_family="思源黑体", font_weight=700)) == "Bold"


def test_non_variable_family_must_not_gain_a_variation() -> None:
    """A stray variation name would silently reraster every existing project."""
    assert _font_variation(layer("T1", font_family="Microsoft YaHei", font_weight=900)) == ""
    assert _font_variation(layer("T1", font_family="SimHei", font_weight=900)) == ""


def test_explicit_font_variation_wins_and_survives_normalization() -> None:
    contract = normalize_text_overlay_composition(composition(
        layer("T1", font_family="Noto Sans SC", font_weight=900, font_variation="Medium"),
    ))
    assert contract["layers"][0]["font_variation"] == "Medium"
    assert _font_variation(contract["layers"][0]) == "Medium"


def test_two_tone_hook_headline_stays_inside_its_lane(tmp_path: Path) -> None:
    """A raster layer holds one fill colour, so a two-tone headline is two layers.

    This pins the invariants the hook headline actually depends on: the two
    lines keep a real gap, and neither runs under the presenter picture-in-picture
    that starts at x=599 on a 1080-wide canvas.
    """
    from PIL import Image

    contract = normalize_text_overlay_composition(composition(
        layer("L1", text="英伟达天价收购", y=.0974, height=.055, width=.47, x=.05, font_family="Noto Sans SC",
              font_size=56, font_weight=900, color="#5CE1FF", stroke_color="#111111", stroke_width=7,
              line_height=1.05, text_align="left", background_opacity=0, padding_x=8, padding_y=10),
        layer("L2", text="爆款是只机械鸭", y=.1396, height=.065, width=.47, x=.05, font_family="Noto Sans SC",
              font_size=66, font_weight=900, color="#FFD400", stroke_color="#111111", stroke_width=7,
              line_height=1.05, text_align="left", background_opacity=0, padding_x=8, padding_y=10,
              z_index=1),
    ))
    overlays, _ = build_text_overlay_assets(tmp_path, contract, 1080, 1920, window_end_seconds=5)
    assert [item["text_layer_id"] for item in overlays] == ["L1", "L2"]
    assert contract["layers"][0]["color"] != contract["layers"][1]["color"]

    first = Image.open(overlays[0]["asset_path"]).convert("RGBA").split()[-1].getbbox()
    second = Image.open(overlays[1]["asset_path"]).convert("RGBA").split()[-1].getbbox()
    assert overlays[1]["y"] + second[1] > overlays[0]["y"] + first[3], "lines must not overlap"
    assert overlays[0]["x"] + first[2] < 599
    assert overlays[1]["x"] + second[2] < 599
