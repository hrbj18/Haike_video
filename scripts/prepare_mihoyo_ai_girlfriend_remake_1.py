"""Freeze the pre-production contract for mihoyo-ai-girlfriend-remake-1."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROJECT = ROOT / "projects" / "mihoyo-ai-girlfriend-remake-1"
STATE_PATH = PROJECT / "artifacts" / "workbench.json"
TARGET_DURATION = 120.0


def write_json(path: Path, value: dict) -> None:
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


def main() -> None:
    state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    project = state["project"]
    project["duration_seconds"] = TARGET_DURATION
    intake = project.setdefault("intake", {})
    intake.update({
        "brief": (
            "把199.421秒对标视频的既有观点压缩为约120秒中文口播。"
            "保留产品名称、角色设定、桌面陪伴、等待感、MIDI个性化演奏和已给出的Steam数据；"
            "删除口头禅、重复解释和直播引导，不新增事实。无数字人。"
        ),
        "duration_seconds": TARGET_DURATION,
        "duration_source": "user_target",
        "video_title": "最懂陪伴的AI女友，反而不秒回你",
        "script_status": "complete",
        "materials_status": "available",
        "style_status": "reference",
        "audience": "关注AI产品、游戏设计与科技趋势的中文短视频观众",
        "content_goal": "解释米哈游AI陪伴产品如何用距离感和个性化表演塑造关系感。",
        "style_reference": "参考S-001的信息结构与节奏；画面使用S-002和Pexels，不使用对标视频人物画面。",
        "style_direction": "竖屏科技产品解读，主体画面动态化，字幕清楚，少量重点标题。",
    })
    write_json(STATE_PATH, state)
    print(json.dumps({
        "status": "prepared",
        "project_id": project["id"],
        "target_duration_seconds": TARGET_DURATION,
        "visual_reference_asset": "S-001",
        "primary_visual_asset": "S-002",
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
