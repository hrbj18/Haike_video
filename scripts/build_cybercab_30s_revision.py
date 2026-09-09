"""Activate the audited 30-second Cybercab revision from generic contracts."""

from __future__ import annotations

import json
import sys
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backlot.text_overlay_composition import normalize_text_overlay_composition


PROJECT = ROOT / "projects" / "cybercab-remake-1"
ARTIFACTS = PROJECT / "artifacts"
REVISION = ARTIFACTS / "revisions" / "cybercab-30s-v001"


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def layer(
    layer_id: str,
    text: str,
    *,
    y: float,
    height: float,
    font_size: int,
    color: str = "#FFFFFF",
    background_color: str = "#050505",
    background_opacity: float = 0.72,
    background_radius: int = 0,
    enter_animation: str = "fade",
    enter_duration: float = 0.45,
    z_index: int,
) -> dict:
    return {
        "id": layer_id,
        "text": text,
        "start_seconds": 0.0,
        "end_seconds": 30.0,
        "x": 0.08,
        "y": y,
        "width": 0.84,
        "height": height,
        "font_family": "SimSun",
        "font_size": font_size,
        "font_weight": 700,
        "color": color,
        "stroke_color": "#111111",
        "stroke_width": 2.0 if color == "#FFFFFF" else 0.0,
        "shadow_color": "#000000B0",
        "shadow_blur": 5.0,
        "shadow_offset_x": 2.0,
        "shadow_offset_y": 3.0,
        "line_height": 1.0,
        "text_align": "center",
        "background_color": background_color,
        "background_opacity": background_opacity,
        "background_radius": background_radius,
        "padding_x": 16.0,
        "padding_y": 8.0,
        "enter_animation": enter_animation,
        "enter_duration_seconds": enter_duration,
        "exit_animation": "fade",
        "exit_duration_seconds": 0.35,
        "z_index": z_index,
        "locked": True,
    }


def visual_blocks(scene_id: str, specs: list[tuple[str, str, float, float]]) -> tuple[list[dict], list[dict]]:
    blocks: list[dict] = []
    usages: list[dict] = []
    for index, (asset_id, label, source_in, source_out) in enumerate(specs, start=1):
        start = (index - 1) * 2.5
        end = index * 2.5
        block_id = f"VB-{index:03d}"
        usage_id = f"U-30-{scene_id[-2:]}-{index:02d}"
        blocks.append({
            "id": block_id,
            "start_seconds": start,
            "end_seconds": end,
            "story_id": "",
            "source_mode": "human_provided",
            "asset_id": asset_id,
            "label": label,
            "status": "ready",
            "locked": True,
            "query": "",
            "context_text": "",
            "attempt": 0,
            "error": "",
            "source_in_seconds": source_in,
            "source_out_seconds": source_out,
            "usage_id": usage_id,
        })
        usages.append({
            "id": usage_id,
            "asset_id": asset_id,
            "scene_id": scene_id,
            "role": "visual_block",
            "selected": True,
            "transform": {
                "crop": None,
                "scale": 1,
                "speed": 1,
                "start_seconds": start,
                "end_seconds": end,
                "block_id": block_id,
            },
            "created_at": datetime.now(UTC).isoformat(),
        })
    return blocks, usages


def main() -> None:
    state_path = ARTIFACTS / "workbench.json"
    state = read_json(state_path)
    now = datetime.now(UTC).isoformat()

    sections = [
        {"id": "section_01", "label": "产品亮相", "text": "", "start_seconds": 0.0, "end_seconds": 10.0},
        {"id": "section_02", "label": "道路行驶", "text": "", "start_seconds": 10.0, "end_seconds": 20.0},
        {"id": "section_03", "label": "座舱与未来出行", "text": "", "start_seconds": 20.0, "end_seconds": 30.0},
    ]
    block_specs = {
        "section_01": [
            ("S-003", "金色车身与车门外观", 0.0, 2.5),
            ("S-002", "Cybercab 开门展示", 0.0, 2.5),
            ("S-001", "城市道路外观", 10.5, 13.0),
            ("S-003", "城市行驶", 10.5, 13.0),
        ],
        "section_02": [
            ("S-001", "城市行驶推进", 14.0, 16.5),
            ("S-002", "道路行驶", 9.5, 12.0),
            ("S-003", "街道行驶", 15.0, 17.5),
            ("S-001", "桥梁与城市路况", 19.0, 21.5),
        ],
        "section_03": [
            ("S-003", "车内屏幕与乘客", 19.0, 21.5),
            ("S-001", "车内乘坐体验", 15.7, 18.2),
            ("S-002", "座舱与中控屏", 28.5, 31.0),
            ("S-003", "Cybercab 品牌收束", 44.0, 46.5),
        ],
    }

    scene_templates = {str(item.get("id")): item for item in state.get("scenes", [])}
    scenes: list[dict] = []
    usages: list[dict] = []
    for order, section in enumerate(sections, start=1):
        scene = deepcopy(scene_templates.get(section["id"]) or {})
        blocks, block_usages = visual_blocks(section["id"], block_specs[section["id"]])
        scene.update({
            "id": section["id"],
            "order": order,
            "title": section["label"],
            "description": section["label"],
            "start_seconds": section["start_seconds"],
            "end_seconds": section["end_seconds"],
            "script_section_id": section["id"],
            "source_strategy": "undecided",
            "review_status": "needs_review",
            "visual_timeline": {"version": 1, "revision": 1, "blocks": blocks, "updated_at": now},
            "visual_composition": None,
            "review_preview": {"status": "needs_refresh", "output_path": None, "error": "30 秒修订版待生成"},
            "surgical_directives": [],
            "subtitles": [],
            "notes": [],
        })
        scenes.append(scene)
        usages.extend(block_usages)

    composition = normalize_text_overlay_composition({
        "version": 1,
        "revision": 1,
        "preset": "cybercab_reference_four_title_v1",
        "reference_asset_id": "S-004",
        "layers": [
            layer(
                "TXT-CYBERCAB-DATE-PRODUCT", "2026年9月3日特斯拉无人驾驶",
                y=0.055, height=0.052, font_size=48, enter_animation="slide_down",
                enter_duration=0.45, z_index=10,
            ),
            layer(
                "TXT-CYBERCAB-SERVICE", "出租车正式商业化服务",
                y=0.108, height=0.052, font_size=52, enter_animation="slide_up",
                enter_duration=0.50, z_index=20,
            ),
            layer(
                "TXT-CYBERCAB-CAPSULE", "未来已来！让我们放弃争辩",
                y=0.172, height=0.060, font_size=45, color="#080808",
                background_color="#B8B8B8", background_opacity=0.96,
                background_radius=34, enter_animation="scale", enter_duration=0.55,
                z_index=30,
            ),
            layer(
                "TXT-CYBERCAB-FUTURE", "拥抱这个世界的下一个精彩的20年",
                y=0.242, height=0.052, font_size=40, enter_animation="fade",
                enter_duration=0.65, z_index=40,
            ),
        ],
    })

    state["scenes"] = scenes
    state["usages"] = usages
    state["segments"] = []
    state["patches"] = []
    state["timeline"] = {"duration_seconds": 30.0, "scene_ids": [item["id"] for item in sections]}
    state["text_overlay_composition"] = composition
    state["project"]["duration_seconds"] = 30.0
    intake = state["project"].setdefault("intake", {})
    intake.update({
        "brief": "30 秒无旁白 Cybercab 复刻审核预览。画面仅允许 S-001、S-002、S-003；S-004 仅提供标题参考与同起点连续 30 秒原音轨，禁止使用其画面；不生成 TTS，不自动发布。",
        "duration_seconds": 30,
        "content_goal": "用三条已理解的 Cybercab 素材完成 30 秒音乐驱动混剪，并以四个通用文字图层复刻对标顶部标题组。",
        "style_reference": "S-004：两行白色宋体主标题、银灰胶囊标题、白色收束句；四层全程同屏并带独立入退场动画。",
        "updated_at": now,
    })
    state["project"]["script_draft"] = {
        "status": "approved", "mode": "music_only", "revision": 2,
        "script": {"version": "1.0", "title": "Cybercab 30 秒无旁白复刻", "total_duration_seconds": 30, "sections": sections},
        "approved_at": now,
    }
    automation = state["automation"]
    automation.update({"status": "idle", "audio_mode": "music_only"})
    automation["narration_generation"] = {
        "status": "not_required", "stage": "disabled", "completed_scenes": 0,
        "total_scenes": 0, "audio_path": None, "subtitle_path": None,
        "error": "", "reason": "30 秒修订版为纯音乐；禁止生成 TTS",
    }
    automation["preview_render"] = {
        "status": "idle", "runtime": None, "output_path": None,
        "version": 0, "error": "",
    }
    automation["render"] = {"status": "idle", "runtime": None, "output_path": None}
    automation["review_preview_pipeline"] = {
        "version": "1.0", "job_id": None, "script_hash": None,
        "input_fingerprint": None, "request_fingerprint": None,
        "status": "idle", "stage": "preflight", "counts": {"total": 0, "completed": 0, "failed": 0},
        "current": None, "gate": None, "error": None, "safe_resume_point": None,
        "result": None, "frozen_input": None, "phases": {}, "worker_token": None,
    }
    policy = state["music_policy"]
    policy.update({
        "enabled": True, "playback_gain_db": 0.0, "loop": False,
        "source_start_seconds": 0.0, "source_end_seconds": 30.0,
        "fade_in_seconds": 0.0, "fade_out_seconds": 0.0,
        "sample": {
            "status": "not_required", "job_id": None, "scene_id": None,
            "output_path": None, "policy_signature": None, "generated_at": None,
            "approved_at": None, "error": "", "stale_reason": "music_only 无人声混音比例",
        },
        "updated_at": now,
    })
    state["updated_at"] = now

    script = {"title": "Cybercab 30 秒无旁白复刻", "total_duration_seconds": 30, "sections": sections}
    scene_plan = {
        "title": "Cybercab 30 秒无旁白复刻",
        "total_duration_seconds": 30,
        "scenes": [{
            "id": item["id"], "description": item["label"],
            "start_seconds": item["start_seconds"], "end_seconds": item["end_seconds"],
            "script_section_id": item["id"],
        } for item in sections],
    }
    project_manifest = read_json(PROJECT / "project.json")
    project_manifest["intake"].update({
        "brief": intake["brief"], "duration_seconds": 30, "duration_source": "user_target",
    })
    revision_manifest = {
        "revision_id": "cybercab-30s-v001",
        "status": "active_review_revision",
        "created_at": now,
        "previous_revision": "previous-55s",
        "duration_seconds": 30.0,
        "audio": {
            "mode": "music_only", "source_asset_id": "S-004", "source_start_seconds": 0.0,
            "source_end_seconds": 30.0, "continuous": True, "tts_forbidden": True,
        },
        "visual_source_allowlist": ["S-001", "S-002", "S-003"],
        "visual_source_denylist": ["S-004"],
        "text_overlay_composition": composition,
        "publish": {"automatic": False, "status": "forbidden"},
    }

    write_json(ARTIFACTS / "script.json", script)
    write_json(ARTIFACTS / "script_draft.json", state["project"]["script_draft"])
    write_json(ARTIFACTS / "scene_plan.json", scene_plan)
    write_json(PROJECT / "project.json", project_manifest)
    write_json(REVISION / "revision-manifest.json", revision_manifest)
    write_json(state_path, state)
    print(json.dumps({
        "status": "activated", "revision_id": revision_manifest["revision_id"],
        "duration_seconds": 30, "scene_count": len(scenes), "title_layer_count": len(composition["layers"]),
        "visual_assets": sorted({block[0] for specs in block_specs.values() for block in specs}),
        "audio_mode": automation["audio_mode"], "tts": "disabled",
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
