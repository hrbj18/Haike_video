"""Materialize the third Cybercab remake as an auditable OpenMontage project."""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backlot.text_overlay_composition import normalize_text_overlay_composition


PROJECT_ID = "cybercab-remake-3"
PROJECT = ROOT / "projects" / PROJECT_ID
SOURCE_PROJECT = ROOT / "projects" / "cybercab-remake-2"
ARTIFACTS = PROJECT / "artifacts"
REVISION_ID = "cybercab-remake-3-v001"
REVISION = ARTIFACTS / "revisions" / REVISION_ID
DURATION = 52.629
MUSIC_TRACK_ID = "project-music-1ec7601a4b775ade9e28af517575089266172c07262285797fee819a0a8d26e4"


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temporary_name, path)
    finally:
        temporary = Path(temporary_name)
        if temporary.exists():
            temporary.unlink()


def text_layer(
    layer_id: str,
    text: str,
    *,
    start: float,
    end: float,
    y: float,
    height: float,
    font_size: int,
    color: str = "#FFFFFF",
    background_color: str = "#050505",
    background_opacity: float = 0.72,
    background_radius: int = 22,
    enter_animation: str = "fade",
    enter_duration: float = 0.30,
    z_index: int = 10,
) -> dict:
    return {
        "id": layer_id,
        "text": text,
        "start_seconds": start,
        "end_seconds": end,
        "x": 0.07,
        "y": y,
        "width": 0.86,
        "height": height,
        "font_family": "Microsoft YaHei",
        "font_size": font_size,
        "font_weight": 700,
        "color": color,
        "stroke_color": "#111111",
        "stroke_width": 2.0 if color == "#FFFFFF" else 0.0,
        "shadow_color": "#000000A8",
        "shadow_blur": 5.0,
        "shadow_offset_x": 2.0,
        "shadow_offset_y": 3.0,
        "line_height": 1.10,
        "text_align": "center",
        "background_color": background_color,
        "background_opacity": background_opacity,
        "background_radius": background_radius,
        "padding_x": 18.0,
        "padding_y": 10.0,
        "enter_animation": enter_animation,
        "enter_duration_seconds": enter_duration,
        "exit_animation": "fade",
        "exit_duration_seconds": 0.30,
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
    scene_number = int(scene_id.rsplit("_", 1)[-1])
    for index, (asset_id, label, duration, source_in, source_out) in enumerate(specs, start=1):
        block_id = f"VB-C3-{scene_number:02d}-{index:02d}"
        usage_id = f"U-C3-{scene_number:02d}-{index:02d}"
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
            "context_text": "复用既有镜头理解证据；S-004禁止作为画面来源。",
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
            source_index["reused_from_project"] = "cybercab-remake-2"
            source_index["reused_at"] = now
            target_assets[asset_id]["media_index"] = source_index


def main() -> None:
    state_path = ARTIFACTS / "workbench.json"
    if not state_path.is_file():
        raise SystemExit("请先通过工作台创建并初始化 cybercab-remake-3")
    state = read_json(state_path)
    now = datetime.now(UTC).isoformat()
    preview_version = int(
        ((state.get("automation") or {}).get("preview_render") or {}).get("version") or 0
    )

    required_assets = {"S-001", "S-002", "S-003", "S-004"}
    actual_assets = {str(item.get("id")) for item in state.get("assets", [])}
    missing_assets = sorted(required_assets - actual_assets)
    if missing_assets:
        raise SystemExit(f"项目缺少素材：{', '.join(missing_assets)}")

    track_meta = PROJECT / "assets" / "audio" / "music" / "uploads" / f"{MUSIC_TRACK_ID}.json"
    if not track_meta.is_file():
        raise SystemExit("项目原音轨尚未登记")

    reuse_existing_indexes(state, now)

    sections = [
        {"id": "section_01", "label": "无人监督自动驾驶定义", "text": "", "start_seconds": 0.0, "end_seconds": 6.46},
        {"id": "section_02", "label": "无方向盘与踏板", "text": "", "start_seconds": 6.46, "end_seconds": 20.0},
        {"id": "section_03", "label": "把时间还给乘客", "text": "", "start_seconds": 20.0, "end_seconds": 36.0},
        {"id": "section_04", "label": "奥斯汀正式运营", "text": "", "start_seconds": 36.0, "end_seconds": DURATION},
    ]
    block_specs = {
        "section_01": [
            ("S-001", "暗场车头灯带揭示", 3.20, 0.0, 3.20),
            ("S-003", "车灯与整车近景亮相", 3.26, 6.25, 9.51),
        ],
        "section_02": [
            ("S-002", "双门开启与乘客接近", 2.70, 0.0, 2.70),
            ("S-001", "手机端叫车界面", 1.50, 13.5, 15.0),
            ("S-003", "多人乘坐与生活场景", 2.70, 14.0, 16.7),
            ("S-001", "日夜城市道路行驶", 2.00, 9.0, 11.0),
            ("S-002", "城市道路跟车镜头", 2.70, 8.5, 11.2),
            ("S-001", "桥梁道路移动镜头", 1.94, 11.0, 12.94),
        ],
        "section_03": [
            ("S-001", "座舱与道路切换", 3.00, 15.0, 18.0),
            ("S-003", "儿童乘客与座舱屏幕", 3.00, 18.0, 21.0),
            ("S-002", "乘客上车与车门闭合", 3.00, 12.0, 15.0),
            ("S-003", "欢迎乘客中控界面", 1.00, 28.5, 29.5),
            ("S-002", "车身侧面与道路运动", 3.00, 24.5, 27.5),
            ("S-001", "白天车身与尾灯细节", 3.00, 26.5, 29.5),
        ],
        "section_04": [
            ("S-001", "路边停车开门迎客", 3.20, 30.0, 33.2),
            ("S-002", "手机叫车与上车展示", 3.20, 21.0, 24.2),
            ("S-003", "夜间车外到双座空间", 3.30, 29.5, 32.8),
            ("S-002", "双门展开实车展示", 3.30, 37.5, 40.8),
            ("S-003", "未来城市与品牌收束", 3.629, 43.0, 46.629),
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
            "shot_intent": "只用S-001至S-003复刻节奏；S-004只提供原音轨和字幕参考。",
            "hero_moment": order in (1, 4),
            "source_strategy": "human_provided",
            "review_status": "needs_review",
            "anchors": [],
            "keyframe_review": None,
            "keyframe_generation": None,
            "review_preview": {
                "status": "needs_refresh", "output_path": None,
                "error": "第三条Cybercab复刻待生成", "input_signature": None,
                "duration_seconds": None, "resolution": None, "caption_cues": [],
                "generated_at": None, "stale_reason": "new_revision",
            },
            "surgical_directives": [],
            "presenter": {
                "treatment": "hidden", "asset_id": None, "asset_version_id": None,
                "source_path": None, "source_start_seconds": None, "source_end_seconds": None,
                "turn_id": None, "audio_mode": "native_avatar_audio", "timeline_revision": None,
                "layout_template_id": "pip_top_right", "layout_override": None,
                "shape": "rounded", "face_crop": None, "crop_bottom": 0.0,
            },
            "narration": {
                "status": "idle", "text": "", "versions": [],
                "current_version_id": None, "candidate_version_id": None,
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
                "duration_source": "project_audio_master", "timeline_revision": 1,
            },
            "subtitles": {"template_id": "subtitle-default", "style_override": {}, "cue_overrides": {}},
            "visual_plan": None,
            "visual_timeline": {"version": 1, "revision": 1, "blocks": blocks, "updated_at": now},
            "visual_composition": {
                "version": 1, "revision": 1, "layout_recipe": "full_bleed",
                "background": {"source": "visual_timeline", "treatment": "normal"},
                "overlays": [],
                "frame_style": {
                    "width_ratio": 0.82, "height_ratio": 0.56,
                    "border_radius_ratio": 0.025, "border_color": "#D9F3FF", "shadow": "soft",
                },
                "updated_at": now,
            },
            "ppt_card_generation": None, "ppt_card_candidate": None, "ppt_card_brief": None,
        })
        scenes.append(scene)
        usages.extend(block_usages)

    composition = normalize_text_overlay_composition({
        "version": 1,
        "revision": 1,
        "preset": "cybercab_austin_source_audio_v1",
        "reference_asset_id": "S-004",
        "layers": [
            text_layer(
                "TXT-C3-CAPTION-01", "第一辆专为无人监督的",
                start=0.62, end=3.50, y=0.720, height=0.070, font_size=50,
                background_opacity=0.68, enter_animation="fade", z_index=30,
            ),
            text_layer(
                "TXT-C3-CAPTION-02", "完全自动驾驶而生的汽车\n叫作 Cybercab",
                start=3.50, end=6.46, y=0.675, height=0.125, font_size=46,
                background_opacity=0.68, enter_animation="fade", z_index=30,
            ),
            text_layer(
                "TXT-C3-TITLE-01", "无需方向盘｜无需踏板｜无需后视镜",
                start=8.0, end=18.0, y=0.300, height=0.065, font_size=42,
                background_radius=0, enter_animation="slide_up", enter_duration=0.42, z_index=10,
            ),
            text_layer(
                "TXT-C3-TITLE-02", "为无人驾驶而生",
                start=20.0, end=32.0, y=0.330, height=0.070, font_size=54,
                background_opacity=0.76, enter_animation="scale", enter_duration=0.45, z_index=12,
            ),
            text_layer(
                "TXT-C3-TITLE-03", "正式驶入奥斯汀",
                start=34.0, end=45.0, y=0.330, height=0.070, font_size=52,
                color="#090909", background_color="#C9C9C9", background_opacity=0.96,
                background_radius=32, enter_animation="slide_up", enter_duration=0.44, z_index=14,
            ),
            text_layer(
                "TXT-C3-TITLE-04", "把路上的时间，还给自己",
                start=45.0, end=51.083, y=0.330, height=0.070, font_size=50,
                background_opacity=0.76, enter_animation="fade", enter_duration=0.38, z_index=16,
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
        "authority": "music", "duration_policy": "exact",
        "target_duration_seconds": DURATION, "committed_duration_seconds": DURATION,
        "revision": 1, "last_change": REVISION_ID,
    }
    state["text_overlay_composition"] = composition
    state["project"]["duration_seconds"] = DURATION
    intake = state["project"].setdefault("intake", {})
    intake.update({
        "brief": (
            "52.629秒竖屏Cybercab复刻审核预览。完整使用S-004连续原音轨作为主时钟；"
            "画面只用S-001至S-003重新编排；保留开头英文原声并显示中文字幕。"
        ),
        "duration_seconds": DURATION,
        "content_goal": "用自有Cybercab素材配合对标原音轨，呈现为无人驾驶而生的产品与奥斯汀运营场景。",
        "style_reference": "中上部短标题、下部口播字幕；顶部和底部留白；无数字人。",
        "script_status": "approved", "script_mode": "music_only",
        "materials_status": "ready", "style_status": "approved", "updated_at": now,
    })
    state["project"]["script_draft"] = {
        "status": "approved", "mode": "music_only", "revision": 1,
        "script": {
            "version": "1.0", "title": "汽车史上第一次：Cybercab驶入奥斯汀",
            "total_duration_seconds": DURATION, "sections": sections,
        },
        "approved_at": now,
    }

    automation = state.setdefault("automation", {})
    automation.update({"status": "idle", "audio_mode": "music_only"})
    automation["narration_generation"] = {
        "status": "not_required", "stage": "disabled",
        "completed_scenes": 0, "total_scenes": 0,
        "audio_path": None, "subtitle_path": None, "error": "",
        "reason": "S-004连续原音轨是项目主时钟；禁止生成TTS或数字人音频",
    }
    automation["preview_render"] = {
        "status": "idle", "runtime": None, "output_path": None,
        "version": preview_version, "error": "",
    }
    automation["render"] = {"status": "idle", "runtime": None, "output_path": None}
    automation["review_preview_pipeline"] = {
        "version": "1.0", "job_id": None, "script_hash": None,
        "input_fingerprint": None, "request_fingerprint": None,
        "status": "idle", "stage": "preflight",
        "counts": {"total": 0, "completed": 0, "failed": 0},
        "current": None, "gate": None, "error": None,
        "safe_resume_point": None, "result": None, "frozen_input": None,
        "phases": {}, "worker_token": None,
    }
    state["music_policy"].update({
        "enabled": True, "category": "project_upload", "track_id": MUSIC_TRACK_ID,
        "playback_gain_db": 0.0, "source_calibration_db": None, "loop": False,
        "source_start_seconds": 0.0, "source_end_seconds": DURATION,
        "fade_in_seconds": 0.0, "fade_out_seconds": 0.0,
        "sample": {
            "status": "not_required", "job_id": None, "scene_id": None,
            "output_path": None, "policy_signature": None, "generated_at": None,
            "approved_at": None, "error": "",
            "stale_reason": "source_audio_only主音轨无需旁白混音样板",
        },
        "updated_at": now,
    })
    state["full_preview"] = {
        "status": "needs_refresh", "version": preview_version,
        "output_path": None, "generated_at": None, "approved_at": None,
        "error": "", "stale_reason": REVISION_ID,
    }
    state["updated_at"] = now

    script = {
        "version": "1.0", "title": "汽车史上第一次：Cybercab驶入奥斯汀",
        "total_duration_seconds": DURATION, "sections": sections,
    }
    scene_plan = {
        "version": "1.0", "title": script["title"], "total_duration_seconds": DURATION,
        "scenes": [{
            "id": item["id"], "description": item["label"],
            "start_seconds": item["start_seconds"], "end_seconds": item["end_seconds"],
            "script_section_id": item["id"],
        } for item in sections],
    }
    transcript_path = ARTIFACTS / "asr" / "S-004" / "transcription.json"
    transcript = read_json(transcript_path)
    subtitle_contract = {
        "version": 1, "source_asset_id": "S-004", "audio_master_track_id": MUSIC_TRACK_ID,
        "language": "zh-CN", "source_language": "en-US", "tts_forbidden": True,
        "source_transcription": transcript,
        "cues": [
            {"id": "CAP-001", "start_seconds": 0.62, "end_seconds": 3.50, "text": "第一辆专为无人监督的"},
            {"id": "CAP-002", "start_seconds": 3.50, "end_seconds": 6.46, "text": "完全自动驾驶而生的汽车，叫作 Cybercab"},
        ],
    }
    project_manifest = read_json(PROJECT / "project.json")
    project_manifest["title"] = "cybercab复刻视频3"
    project_manifest["intake"].update({
        "brief": intake["brief"], "duration_seconds": DURATION,
        "duration_source": "project_audio_master",
    })
    revision_manifest = {
        "revision_id": REVISION_ID, "status": "active_review_revision", "created_at": now,
        "duration_seconds": DURATION,
        "audio": {
            "mode": "source_audio_only", "renderer_compatibility_mode": "music_only",
            "track_id": MUSIC_TRACK_ID, "source_asset_id": "S-004",
            "source_start_seconds": 0.0, "source_end_seconds": DURATION,
            "continuous": True, "loop": False, "speed": 1.0,
            "tts_forbidden": True, "user_authorized_direct_use": True,
        },
        "visual_source_allowlist": ["S-001", "S-002", "S-003"],
        "visual_source_denylist": ["S-004"],
        "text_overlay_composition": composition,
        "subtitle_contract_path": "artifacts/subtitle-contract.json",
        "render": {"width": 1080, "height": 1920, "fps": 30, "runtime": "ffmpeg"},
        "publish": {"automatic": False, "status": "forbidden"},
    }

    write_json(ARTIFACTS / "script.json", script)
    write_json(ARTIFACTS / "script_draft.json", state["project"]["script_draft"])
    write_json(ARTIFACTS / "scene_plan.json", scene_plan)
    write_json(ARTIFACTS / "subtitle-contract.json", subtitle_contract)
    write_json(PROJECT / "project.json", project_manifest)
    write_json(REVISION / "revision-manifest.json", revision_manifest)
    write_json(state_path, state)
    print(json.dumps({
        "status": "activated", "project_id": PROJECT_ID, "revision_id": REVISION_ID,
        "duration_seconds": DURATION, "scene_count": len(scenes),
        "title_layer_count": len(composition["layers"]),
        "visual_assets": ["S-001", "S-002", "S-003"],
        "reference_visual_forbidden": "S-004", "audio_mode": "source_audio_only",
        "tts": "disabled",
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
