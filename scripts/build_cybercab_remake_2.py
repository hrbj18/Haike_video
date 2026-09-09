"""Materialize the second Cybercab remake as an auditable OpenMontage project."""

from __future__ import annotations

import json
import shutil
import sys
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backlot.text_overlay_composition import normalize_text_overlay_composition


PROJECT_ID = "cybercab-remake-2"
PROJECT = ROOT / "projects" / PROJECT_ID
SOURCE_PROJECT = ROOT / "projects" / "cybercab-remake-1"
ARTIFACTS = PROJECT / "artifacts"
REVISION_ID = "cybercab-remake-2-v003"
REVISION = ARTIFACTS / "revisions" / REVISION_ID
DURATION = 31.669
INTRO_DURATION = 11.669
MUSIC_TRACK_ID = "project-music-35750d6f847d6c709833242ea7ffc04e9a7e14bde4573340023b14330073985f"


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def text_layer(
    layer_id: str,
    text: str,
    *,
    start: float,
    y: float,
    height: float,
    font_size: int,
    color: str,
    background_color: str,
    background_opacity: float,
    background_radius: int,
    enter_animation: str,
    enter_duration: float,
    z_index: int,
) -> dict:
    return {
        "id": layer_id,
        "text": text,
        "start_seconds": start,
        "end_seconds": DURATION,
        "x": 0.07,
        "y": y,
        "width": 0.86,
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


def visual_blocks(
    scene_id: str,
    specs: list[tuple[str, str, float, float, float]],
) -> tuple[list[dict], list[dict]]:
    blocks: list[dict] = []
    usages: list[dict] = []
    cursor = 0.0
    for index, (asset_id, label, duration, source_in, source_out) in enumerate(specs, start=1):
        block_id = f"VB-{index:03d}"
        usage_id = f"U-C2-{scene_id[-2:]}-{index:02d}"
        end = round(cursor + duration, 3)
        blocks.append({
            "id": block_id,
            "start_seconds": cursor,
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
                "start_seconds": cursor,
                "end_seconds": end,
                "block_id": block_id,
            },
            "created_at": datetime.now(UTC).isoformat(),
        })
        cursor = end
    return blocks, usages


def reuse_existing_indexes(state: dict, now: str) -> None:
    source_state = read_json(SOURCE_PROJECT / "artifacts" / "workbench.json")
    source_assets = {str(item.get("id")): item for item in source_state.get("assets", [])}
    target_assets = {str(item.get("id")): item for item in state.get("assets", [])}
    for asset_id in ("S-001", "S-002", "S-003"):
        source_dir = SOURCE_PROJECT / "artifacts" / "media-index" / asset_id
        target_dir = PROJECT / "artifacts" / "media-index" / asset_id
        if source_dir.is_dir() and not target_dir.exists():
            shutil.copytree(source_dir, target_dir)
        source_index = deepcopy((source_assets.get(asset_id) or {}).get("media_index"))
        if source_index and asset_id in target_assets:
            source_index["reused_from_project"] = "cybercab-remake-1"
            source_index["reused_at"] = now
            target_assets[asset_id]["media_index"] = source_index


def main() -> None:
    state_path = ARTIFACTS / "workbench.json"
    if not state_path.is_file():
        raise SystemExit("请先通过工作台创建并初始化 cybercab-remake-2")
    state = read_json(state_path)
    now = datetime.now(UTC).isoformat()
    preview_version = int(
        ((state.get("automation") or {}).get("preview_render") or {}).get("version") or 0
    )

    required_assets = {"S-001", "S-002", "S-003", "S-004", "S-005"}
    actual_assets = {str(item.get("id")) for item in state.get("assets", [])}
    missing_assets = sorted(required_assets - actual_assets)
    if missing_assets:
        raise SystemExit(f"项目缺少素材：{', '.join(missing_assets)}")

    track_meta = PROJECT / "assets" / "audio" / "music" / "uploads" / f"{MUSIC_TRACK_ID}.json"
    if not track_meta.is_file():
        raise SystemExit("项目主音轨尚未登记")

    reuse_existing_indexes(state, now)

    sections = [
        {"id": "section_01", "label": "采访与笑声", "text": "", "start_seconds": 0.0, "end_seconds": INTRO_DURATION},
        {"id": "section_02", "label": "Cybercab破局而来", "text": "", "start_seconds": INTRO_DURATION, "end_seconds": 21.669},
        {"id": "section_03", "label": "产品与现实答复", "text": "", "start_seconds": 21.669, "end_seconds": DURATION},
    ]
    block_specs = {
        "section_01": [
            ("S-004", "马斯克采访原声开头", 11.669, 0.0, 11.669),
        ],
        "section_02": [
            ("S-002", "低角度车头破局亮相", 2.5, 5.0, 7.5),
            ("S-001", "白天车身侧面推进", 2.0, 26.5, 28.5),
            ("S-003", "贯穿式灯带细节", 2.5, 5.0, 7.5),
            ("S-002", "蝶翼门与乘坐展示", 3.0, 14.5, 17.5),
        ],
        "section_03": [
            ("S-003", "无方向盘座舱与中控屏", 2.5, 27.5, 30.0),
            ("S-002", "双座座舱广角", 2.5, 32.5, 35.0),
            ("S-002", "双门展开实车展示", 2.5, 37.5, 40.0),
            ("S-002", "建筑前道路行驶收束", 2.5, 42.5, 45.0),
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
            "shot_intent": "复用既有Cybercab干净素材，重新选择源区间与镜头顺序。",
            "hero_moment": order == 2,
            "source_strategy": "human_provided",
            "review_status": "needs_review",
            "anchors": [],
            "keyframe_review": None,
            "keyframe_generation": None,
            "review_preview": {
                "status": "needs_refresh",
                "output_path": None,
                "error": "第二条Cybercab复刻待生成",
                "input_signature": None,
                "duration_seconds": None,
                "resolution": None,
                "caption_cues": [],
                "generated_at": None,
                "stale_reason": "new_revision",
            },
            "surgical_directives": [],
            "presenter": {
                "treatment": "hidden",
                "asset_id": None,
                "asset_version_id": None,
                "source_path": None,
                "source_start_seconds": None,
                "source_end_seconds": None,
                "turn_id": None,
                "audio_mode": "native_avatar_audio",
                "timeline_revision": None,
                "layout_template_id": "pip_top_right",
                "layout_override": None,
                "shape": "rounded",
                "face_crop": None,
                "crop_bottom": 0.0,
            },
            "narration": {
                "status": "idle",
                "text": "",
                "versions": [],
                "current_version_id": None,
                "candidate_version_id": None,
                "job": {"status": "idle", "error": ""},
            },
            "notes": [],
            "timing": {
                "authority": "music",
                "planned_start_seconds": section["start_seconds"],
                "planned_end_seconds": section["end_seconds"],
                "planned_duration_seconds": round(section["end_seconds"] - section["start_seconds"], 3),
                "voice_duration_seconds": None,
                "committed_duration_seconds": round(section["end_seconds"] - section["start_seconds"], 3),
                "duration_source": "project_audio_master",
                "timeline_revision": 1,
            },
            "subtitles": {"template_id": "subtitle-default", "style_override": {}, "cue_overrides": {}},
            "visual_plan": None,
            "visual_timeline": {"version": 1, "revision": 1, "blocks": blocks, "updated_at": now},
            "visual_composition": {
                "version": 1,
                "revision": 1,
                "layout_recipe": "full_bleed",
                "background": {"source": "visual_timeline", "treatment": "normal"},
                "overlays": [],
                "frame_style": {
                    "width_ratio": 0.82,
                    "height_ratio": 0.56,
                    "border_radius_ratio": 0.025,
                    "border_color": "#D9F3FF",
                    "shadow": "soft",
                },
                "updated_at": now,
            },
            "ppt_card_generation": None,
            "ppt_card_candidate": None,
            "ppt_card_brief": None,
        })
        scenes.append(scene)
        usages.extend(block_usages)

    composition = normalize_text_overlay_composition({
        "version": 1,
        "revision": 3,
        "preset": "cybercab_breakthrough_three_title_v1",
        "reference_asset_id": "S-005",
        "layers": [
            text_layer(
                "TXT-CYBERCAB2-BREAKTHROUGH",
                "十年之后 Cybercab 破局而来",
                start=11.669,
                y=0.270,
                height=0.055,
                font_size=48,
                color="#FFFFFF",
                background_color="#050505",
                background_opacity=0.76,
                background_radius=0,
                enter_animation="slide_down",
                enter_duration=0.42,
                z_index=10,
            ),
            text_layer(
                "TXT-CYBERCAB2-SPECS",
                "无方向盘｜无踏板｜双座全自动驾驶",
                start=11.969,
                y=0.340,
                height=0.060,
                font_size=43,
                color="#080808",
                background_color="#C8C8C8",
                background_opacity=0.96,
                background_radius=34,
                enter_animation="scale",
                enter_duration=0.48,
                z_index=20,
            ),
            text_layer(
                "TXT-CYBERCAB2-ANSWER",
                "所有嘲笑，现实给出最有力答复",
                start=12.269,
                y=0.410,
                height=0.055,
                font_size=44,
                color="#FFFFFF",
                background_color="#050505",
                background_opacity=0.74,
                background_radius=0,
                enter_animation="slide_up",
                enter_duration=0.52,
                z_index=30,
            ),
        ],
    })

    state["scenes"] = scenes
    state["usages"] = usages
    state["segments"] = []
    state["patches"] = []
    state["timeline"] = {
        "duration_seconds": DURATION,
        "scene_ids": [item["id"] for item in sections],
        "authority": "music",
        "duration_policy": "exact",
        "target_duration_seconds": DURATION,
        "committed_duration_seconds": DURATION,
        "revision": 1,
        "last_change": "cybercab-remake-2-v003",
    }
    state["text_overlay_composition"] = composition
    state["project"]["duration_seconds"] = DURATION
    intake = state["project"].setdefault("intake", {})
    intake.update({
        "brief": (
            "31.669秒竖屏无旁白Cybercab复刻审核预览。完整保留马斯克采访原声，"
            "后20秒复用cybercab复刻视频1的同款音乐；后段只用S-001至S-003并重新选段。"
        ),
        "duration_seconds": DURATION,
        "content_goal": "用采访现场的笑声建立冲突，再用Cybercab无方向盘实车画面给出回应。",
        "style_reference": "白字、银灰胶囊和白字收束；三层位于中上部、错峰入场并持续同屏，上下保留空白。",
        "script_status": "approved",
        "script_mode": "music_only",
        "materials_status": "ready",
        "style_status": "approved",
        "updated_at": now,
    })
    state["project"]["script_draft"] = {
        "status": "approved",
        "mode": "music_only",
        "revision": 1,
        "script": {
            "version": "1.0",
            "title": "Cybercab破局而来",
            "total_duration_seconds": DURATION,
            "sections": sections,
        },
        "approved_at": now,
    }

    automation = state.setdefault("automation", {})
    automation.update({"status": "idle", "audio_mode": "music_only"})
    automation["narration_generation"] = {
        "status": "not_required",
        "stage": "disabled",
        "completed_scenes": 0,
        "total_scenes": 0,
        "audio_path": None,
        "subtitle_path": None,
        "error": "",
        "reason": "项目主音轨含采访原声和背景音乐；禁止生成TTS",
    }
    automation["preview_render"] = {
        "status": "idle",
        "runtime": None,
        "output_path": None,
        "version": preview_version,
        "error": "",
    }
    automation["render"] = {"status": "idle", "runtime": None, "output_path": None}
    automation["review_preview_pipeline"] = {
        "version": "1.0",
        "job_id": None,
        "script_hash": None,
        "input_fingerprint": None,
        "request_fingerprint": None,
        "status": "idle",
        "stage": "preflight",
        "counts": {"total": 0, "completed": 0, "failed": 0},
        "current": None,
        "gate": None,
        "error": None,
        "safe_resume_point": None,
        "result": None,
        "frozen_input": None,
        "phases": {},
        "worker_token": None,
    }

    state["music_policy"].update({
        "enabled": True,
        "category": "project_upload",
        "track_id": MUSIC_TRACK_ID,
        "playback_gain_db": 0.0,
        "source_calibration_db": None,
        "loop": False,
        "source_start_seconds": 0.0,
        "source_end_seconds": DURATION,
        "fade_in_seconds": 0.0,
        "fade_out_seconds": 0.0,
        "sample": {
            "status": "not_required",
            "job_id": None,
            "scene_id": None,
            "output_path": None,
            "policy_signature": None,
            "generated_at": None,
            "approved_at": None,
            "error": "",
            "stale_reason": "music_only主音轨无需旁白混音样板",
        },
        "updated_at": now,
    })
    state["full_preview"] = {
        "status": "needs_refresh",
        "version": preview_version,
        "output_path": None,
        "generated_at": None,
        "approved_at": None,
        "error": "",
        "stale_reason": "cybercab-remake-2-v001",
    }
    state["updated_at"] = now

    script = {
        "version": "1.0",
        "title": "Cybercab破局而来",
        "total_duration_seconds": DURATION,
        "sections": sections,
    }
    scene_plan = {
        "version": "1.0",
        "title": "Cybercab破局而来",
        "total_duration_seconds": DURATION,
        "scenes": [{
            "id": item["id"],
            "description": item["label"],
            "start_seconds": item["start_seconds"],
            "end_seconds": item["end_seconds"],
            "script_section_id": item["id"],
        } for item in sections],
    }
    project_manifest = read_json(PROJECT / "project.json")
    project_manifest["title"] = "cybercab复刻视频2"
    project_manifest["intake"].update({
        "brief": intake["brief"],
        "duration_seconds": DURATION,
        "duration_source": "project_audio_master",
    })
    revision_manifest = {
        "revision_id": REVISION_ID,
        "status": "active_review_revision",
        "created_at": now,
        "duration_seconds": DURATION,
        "audio": {
            "mode": "music_only",
            "track_id": MUSIC_TRACK_ID,
            "intro_source_asset_id": "S-004",
            "intro_source_start_seconds": 0.0,
            "intro_source_end_seconds": INTRO_DURATION,
            "reused_music_from_project": "cybercab-remake-1",
            "music_source_start_seconds": 0.0,
            "music_duration_seconds": 20.0,
            "tts_forbidden": True,
        },
        "visual_source_allowlist": ["S-001", "S-002", "S-003", "S-004"],
        "tail_visual_source_allowlist": ["S-001", "S-002", "S-003"],
        "visual_source_denylist": ["S-005"],
        "tail_recut": True,
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
        "status": "activated",
        "project_id": PROJECT_ID,
        "revision_id": REVISION_ID,
        "duration_seconds": DURATION,
        "scene_count": len(scenes),
        "title_layer_count": len(composition["layers"]),
        "tail_visual_assets": ["S-001", "S-002", "S-003"],
        "reference_visual_forbidden": "S-005",
        "audio_mode": "music_only",
        "tts": "disabled",
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
