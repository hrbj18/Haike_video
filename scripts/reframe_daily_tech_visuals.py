"""Reframe one daily-tech project with HyperFrames image cards + unique Pexels B-roll.

This is intentionally project-scoped and idempotent: reruns reuse generated card
and stock assets by their stable names instead of downloading or rendering twice.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from backlot.workbench import add_asset, read_workbench, update_scene_visual_composition, update_scene_visual_timeline
from tools.video.hyperframes_compose import HyperFramesCompose
from tools.video.pexels_video import PexelsVideo


CARD_MAP = {
    "section-001": "S-006",
    "section-003": "S-001",
    "section-005": "S-002",
    "section-006": "S-003",
    "section-007": "S-004",
    "section-009": "S-005",
}

PEXELS_QUERIES = {
    "section-001": "foldable smartphone technology comparison",
    "section-002": "semiconductor processor chip laboratory",
    "section-003": "premium foldable smartphone close up",
    "section-004": "computer chip manufacturing technology",
    "section-005": "foldable smartphone hands close up",
    "section-006": "mobile artificial intelligence processor",
    "section-007": "artificial intelligence research office",
    "section-008": "artificial intelligence data center servers",
    "section-009": "technology company campus building",
    "section-010": "computer servers artificial intelligence",
    "section-011": "smartphone technology news studio",
}


def _duration(scene: dict) -> float:
    return round(float(scene["end_seconds"]) - float(scene["start_seconds"]), 3)


def _asset_by_id(state: dict, asset_id: str) -> dict:
    return next(item for item in state["assets"] if item["id"] == asset_id)


def _asset_by_name(state: dict, name: str) -> dict | None:
    return next((item for item in state["assets"] if item.get("name") == name), None)


def _register(project_dir: Path, payload: dict) -> dict:
    state = add_asset(project_dir, payload)
    return _asset_by_name(state, payload["name"])


def _render_card(project_dir: Path, source_asset: dict) -> dict:
    stable_name = f"HF居中卡片 · {source_asset['id']}"
    state = read_workbench(project_dir)
    existing = _asset_by_name(state, stable_name)
    if existing and (project_dir / existing["path"]).is_file():
        return existing

    output = project_dir / "assets" / "generated" / "hyperframes-cards" / f"{source_asset['id']}.mp4"
    workspace = project_dir / "artifacts" / "motion_compositions" / "daily-image-cards" / source_asset["id"]
    source_path = (project_dir / source_asset["path"]).resolve()
    edit_decisions = {
        "version": "1.0",
        "renderer_family": "explainer-data",
        "render_runtime": "hyperframes",
        "composition_mode": "templated",
        "metadata": {
            "title": stable_name,
            "proposal_render_runtime": "hyperframes",
            "compose_target": {"width": 1080, "height": 1920, "fit": "contain"},
            "target_duration_seconds": 3.0,
        },
        "cuts": [{
            "id": f"card-{source_asset['id']}",
            "type": "image_card",
            "source": source_asset["id"],
            "in_seconds": 0,
            "out_seconds": 3.0,
        }],
    }
    result = HyperFramesCompose().execute({
        "operation": "render",
        "workspace_path": str(workspace),
        "output_path": str(output),
        "edit_decisions": edit_decisions,
        "asset_manifest": {"version": "1.0", "assets": [{
            "id": source_asset["id"], "type": "image", "path": str(source_path),
        }]},
        "playbook": {},
        "profile": "tiktok",
        "quality": "draft",
        "fps": 30,
        "strict": True,
    })
    if not result.success or not output.is_file():
        raise RuntimeError(f"HyperFrames card failed for {source_asset['id']}: {result.error}")
    return _register(project_dir, {
        "name": stable_name,
        "type": "video",
        "source_type": "local_generated",
        "path": str(output.relative_to(project_dir)),
        "duration_seconds": 3.0,
        "resolution": "1080x1920",
        "provider": "HyperFrames",
        "source_tool": "hyperframes_compose",
        "license": "由用户提供图片在本地可复现合成",
        "generation": {"source_asset_id": source_asset["id"], "layout": "center_card", "max_display_seconds": 3.0},
    })


def _download_pexels(project_dir: Path, scene_id: str, query: str, excluded: set[str]) -> dict:
    stable_name = f"Pexels补充画面 · {scene_id}"
    state = read_workbench(project_dir)
    existing = _asset_by_name(state, stable_name)
    if existing and (project_dir / existing["path"]).is_file():
        video_id = str((existing.get("generation") or {}).get("video_id") or "")
        if video_id:
            excluded.add(video_id)
        return existing

    output = project_dir / "assets" / "video" / "pexels" / f"{scene_id}.mp4"
    result = PexelsVideo().execute({
        "query": query,
        "orientation": "portrait",
        "size": "medium",
        "per_page": 30,
        "exclude_video_ids": sorted(excluded),
        "preferred_quality": "hd",
        "output_path": str(output),
    })
    if not result.success or not output.is_file():
        raise RuntimeError(f"Pexels failed for {scene_id}: {result.error}")
    data = result.data or {}
    video_id = str(data.get("video_id") or "")
    if video_id:
        excluded.add(video_id)
    return _register(project_dir, {
        "name": stable_name,
        "type": "video",
        "source_type": "web_download",
        "path": str(output.relative_to(project_dir)),
        "duration_seconds": float(data.get("duration_seconds") or 0),
        "resolution": f"{data.get('width') or '?'}x{data.get('height') or '?'}",
        "provider": "Pexels",
        "source_tool": "pexels_video",
        "source_url": data.get("pexels_url"),
        "license": data.get("license") or "Pexels License (free, no attribution required)",
        "generation": {"query": query, "video_id": data.get("video_id"), "scene_id": scene_id},
    })


def run(project_dir: Path) -> None:
    state = read_workbench(project_dir)
    cards = {
        scene_id: _render_card(project_dir, _asset_by_id(state, source_id))
        for scene_id, source_id in CARD_MAP.items()
    }
    excluded: set[str] = set()
    pexels = {
        scene_id: _download_pexels(project_dir, scene_id, query, excluded)
        for scene_id, query in PEXELS_QUERIES.items()
    }

    state = read_workbench(project_dir)
    scenes = {scene["id"]: scene for scene in state["scenes"]}
    for scene_id in PEXELS_QUERIES:
        duration = _duration(scenes[scene_id])
        card = cards.get(scene_id)
        blocks = []
        cursor = 0.0
        if card:
            card_end = min(3.0, duration)
            blocks.append({
                "id": "VB-001", "start_seconds": 0, "end_seconds": card_end,
                "asset_id": card["id"], "source_mode": "local_generated",
                "source_in_seconds": 0, "source_out_seconds": card_end,
                "label": card["name"], "locked": True,
            })
            cursor = card_end
        if duration - cursor >= 0.4:
            blocks.append({
                "id": f"VB-{len(blocks) + 1:03d}", "start_seconds": cursor, "end_seconds": duration,
                "asset_id": pexels[scene_id]["id"], "source_mode": "web_download",
                "source_in_seconds": 0, "source_out_seconds": duration - cursor,
                "label": pexels[scene_id]["name"], "locked": True,
                "query": PEXELS_QUERIES[scene_id],
            })
        update_scene_visual_timeline(project_dir, scene_id, {"blocks": blocks})
        latest = read_workbench(project_dir)
        scene = next(item for item in latest["scenes"] if item["id"] == scene_id)
        composition = scene.get("visual_composition") or {}
        update_scene_visual_composition(project_dir, scene_id, {
            "expected_revision": composition.get("revision", 1),
            "version": 1,
            "layout_recipe": "full_bleed",
            "overlays": [],
            "frame_style": composition.get("frame_style") or {},
        })
        print(f"prepared {scene_id}: card={bool(card)} duration={duration:.2f}s")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("project_dir", type=Path)
    args = parser.parse_args()
    run(args.project_dir.resolve())
