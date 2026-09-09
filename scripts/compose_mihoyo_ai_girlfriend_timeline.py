"""Commit the approved mixed-source visual and title contracts for this project."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backlot.workbench import (
    _load_for_write,
    update_scene,
    update_scene_visual_timeline,
    update_text_overlay_composition,
)


PROJECT = ROOT / "projects" / "mihoyo-ai-girlfriend-remake-1"


def _block(
    index: int,
    start: float,
    end: float,
    asset_id: str,
    source_in: float,
    label: str,
    *,
    source_mode: str = "human_provided",
    query: str = "",
) -> dict:
    duration = round(end - start, 3)
    return {
        "id": f"VB-{index:03d}",
        "start_seconds": round(start, 3),
        "end_seconds": round(end, 3),
        "source_mode": source_mode,
        "asset_id": asset_id,
        "source_in_seconds": round(source_in, 3),
        "source_out_seconds": round(source_in + duration, 3),
        "label": label,
        "query": query,
        "context_text": "按对标视频语义时间和素材理解证据选取；保持1倍速、无循环、素材原声静音。",
        "locked": True,
    }


def _local_blocks(asset_id: str, duration: float, source_starts: list[float], label: str) -> list[dict]:
    cut = round(duration / len(source_starts), 3)
    blocks: list[dict] = []
    cursor = 0.0
    for index, source_in in enumerate(source_starts, 1):
        end = duration if index == len(source_starts) else round(cursor + cut, 3)
        blocks.append(_block(index, cursor, end, asset_id, source_in, f"{label} · 源{source_in:.2f}s"))
        cursor = end
    return blocks


def main() -> None:
    state = _load_for_write(PROJECT)
    assets = {str(asset.get("id")): asset for asset in state.get("assets", [])}
    local_id = "S-002"
    if local_id not in assets:
        raise RuntimeError("主视觉素材 S-002 不存在")
    roles = {
        str((asset.get("generation") or {}).get("project_role") or ""): str(asset.get("id"))
        for asset in state.get("assets", [])
        if isinstance(asset.get("generation"), dict)
    }
    waiting_id = roles.get("pexels_waiting_message")
    laptop_id = roles.get("pexels_laptop_companion")
    if not waiting_id or not laptop_id:
        raise RuntimeError("Pexels 补充素材尚未登记")

    scenes = {str(scene["id"]): scene for scene in state.get("scenes", [])}
    durations = {scene_id: round(float(scene["end_seconds"]) - float(scene["start_seconds"]), 3) for scene_id, scene in scenes.items()}
    plans: dict[str, list[dict]] = {
        "sec_01": _local_blocks(local_id, durations["sec_01"], [0.28, 6.12, 12.0, 15.65], "角色与钢琴开场"),
        "sec_02": _local_blocks(local_id, durations["sec_02"], [17.97, 22.01, 27.9, 33.0], "桌面生活与昼夜变化"),
        "sec_03": _local_blocks(local_id, durations["sec_03"], [63.47, 66.47, 70.8, 75.27], "桌面陪伴与独立生活"),
        "sec_05": _local_blocks(local_id, durations["sec_05"], [117.57, 125.95, 132.91, 139.03], "MIDI与钢琴动作"),
        "sec_06": _local_blocks(local_id, durations["sec_06"], [150.2, 154.0, 158.0, 162.0], "角色专属演奏"),
    }

    d4 = durations["sec_04"]
    plans["sec_04"] = [
        _block(1, 0.0, 3.936, local_id, 90.04, "不秒回的产品设计 · 原片语义段"),
        _block(
            2,
            3.936,
            9.936,
            waiting_id,
            0.0,
            "Pexels · 夜间等待消息",
            source_mode="web_download",
            query="woman waiting smartphone message at night",
        ),
        _block(3, 9.936, d4, local_id, 98.4, "距离感与关系 · 原片语义段"),
    ]

    d7 = durations["sec_07"]
    plans["sec_07"] = [
        _block(1, 0.0, 4.2, local_id, 166.47, "历史评论数据 · 原片语义段"),
        _block(2, 4.2, 8.4, local_id, 171.56, "同时在线数据 · 原片语义段"),
        _block(
            3,
            8.4,
            12.6,
            laptop_id,
            5.0,
            "Pexels · 桌面工作陪伴",
            source_mode="web_download",
            query="woman working laptop at home night",
        ),
        _block(4, 12.6, d7, local_id, 180.0, "存在感与分寸感 · 原片语义段"),
    ]

    for scene_id in [f"sec_{index:02d}" for index in range(1, 8)]:
        update_scene(PROJECT, scene_id, {"source_strategy": "mixed"})
        update_scene_visual_timeline(PROJECT, scene_id, {"blocks": plans[scene_id]})

    state = _load_for_write(PROJECT)
    composition = state.get("text_overlay_composition") if isinstance(state.get("text_overlay_composition"), dict) else {"revision": 0}
    total = round(float(state["timeline"]["committed_duration_seconds"]), 3)
    layers = [
        {
            "id": "TXT-MIHOYO-TOPIC",
            "text": "拆解 AI 陪伴赛道",
            "start_seconds": 0.0,
            "end_seconds": total,
            "x": 0.08,
            "y": 0.045,
            "width": 0.84,
            "height": 0.055,
            "font_family": "Microsoft YaHei",
            "font_size": 47.0,
            "font_weight": 700,
            "color": "#FFFFFF",
            "stroke_color": "#111111",
            "stroke_width": 2.0,
            "shadow_color": "#000000A0",
            "shadow_blur": 5.0,
            "shadow_offset_x": 2.0,
            "shadow_offset_y": 3.0,
            "line_height": 1.0,
            "text_align": "center",
            "background_color": "#111111",
            "background_opacity": 0.72,
            "background_radius": 26.0,
            "padding_x": 16.0,
            "padding_y": 8.0,
            "enter_animation": "slide_down",
            "enter_duration_seconds": 0.45,
            "exit_animation": "fade",
            "exit_duration_seconds": 0.35,
            "z_index": 100,
            "locked": True,
        },
        {
            "id": "TXT-MIHOYO-NAME",
            "text": "米哈游 AI 女友「林离」",
            "start_seconds": 0.2,
            "end_seconds": total,
            "x": 0.08,
            "y": 0.112,
            "width": 0.84,
            "height": 0.055,
            "font_family": "Microsoft YaHei",
            "font_size": 45.0,
            "font_weight": 700,
            "color": "#FFE36E",
            "stroke_color": "#111111",
            "stroke_width": 2.0,
            "shadow_color": "#000000A0",
            "shadow_blur": 5.0,
            "shadow_offset_x": 2.0,
            "shadow_offset_y": 3.0,
            "line_height": 1.0,
            "text_align": "center",
            "background_color": "#111111",
            "background_opacity": 0.72,
            "background_radius": 26.0,
            "padding_x": 16.0,
            "padding_y": 8.0,
            "enter_animation": "scale",
            "enter_duration_seconds": 0.48,
            "exit_animation": "fade",
            "exit_duration_seconds": 0.35,
            "z_index": 110,
            "locked": True,
        },
    ]
    update_text_overlay_composition(
        PROJECT,
        {
            "expected_revision": int(composition.get("revision") or 0),
            "composition": {"version": 1, "revision": int(composition.get("revision") or 0), "layers": layers},
        },
    )
    final_state = _load_for_write(PROJECT)
    print(
        json.dumps(
            {
                "duration_seconds": total,
                "scene_blocks": {scene_id: len(plan) for scene_id, plan in plans.items()},
                "text_overlay_revision": final_state.get("text_overlay_composition", {}).get("revision"),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
