"""remake-spec-v1 → 本机项目产物的**纯函数**适配层。

`scripts/remake_build_project.py` 需要 ffmpeg、项目目录与状态写入，不能进单元测试。
但它的两段纯逻辑——「规格书 → script.json」与「规格书 → 逐段视觉时间线 blocks」——
必须与它保持一致，否则 spec 的帧口径在下游会漂移。这里把这两段抽成无副作用的
纯函数，供离线校验与后续 orchestrator 复用；**不改动 `scripts/remake_build_project.py`**。

帧口径保持与 `backlot/workbench.py::_validated_visual_timeline` 一致：
显示帧数 = 源区间帧数，块首尾相接覆盖整段。
"""

from __future__ import annotations

from typing import Any

from backlot.remake_editorial import frames_to_seconds, quantize_frames


def _duration_of(section: dict[str, Any]) -> float:
    shots = section.get("shots") or []
    if not shots:
        return 0.0
    return round(float(shots[-1].get("end_seconds") or 0.0), 3)


def build_script(spec: dict[str, Any], *, speaker_id: str | None = None) -> dict[str, Any]:
    """规格书 → 时间戳脚本（与 `scripts/remake_build_project.py::build_script` 同构）。

    段时长直接取 spec 的量化显示时长，保证脚本内部自洽（下游音频驱动重算另说）。
    """
    voice = spec.get("voice") or {}
    speaker = speaker_id or voice.get("role") or ""
    sections: list[dict[str, Any]] = []
    cursor = 0.0
    for section in spec.get("sections", []):
        duration = _duration_of(section)
        start = round(cursor, 3)
        end = round(cursor + duration, 3)
        cues = []
        beat = start
        for shot in section.get("shots", []):
            span = round(float(shot.get("end_seconds") or 0.0) - float(shot.get("start_seconds") or 0.0), 3)
            cues.append(
                {
                    "timestamp_seconds": round(beat + span / 2, 3),
                    "description": str(shot.get("intent") or ""),
                }
            )
            beat += span
        sections.append(
            {
                "id": section["id"],
                "turn_id": section.get("turn_id") or section["id"],
                "label": str(section.get("label") or ""),
                "text": section["text"],
                "speaker_id": speaker,
                "start_seconds": start,
                "end_seconds": end,
                "enhancement_cues": cues,
            }
        )
        cursor = end
    return {
        "version": "1.0",
        "title": spec.get("title") or "",
        "total_duration_seconds": round(cursor, 3),
        "sections": sections,
        "metadata": {
            "audio_mode": "narration",
            "remake_of": "episode-research-pack-v1（仅借鉴选题与信息结构，文案为重写）",
            "speaker": voice.get("profile_name") or speaker,
            "theme_id": (spec.get("theme") or {}).get("theme_id", ""),
        },
    }


def build_visual_blocks(
    spec: dict[str, Any],
    section: dict[str, Any],
    asset_id_by_key: dict[str, str],
    *,
    fps: int | None = None,
) -> list[dict[str, Any]]:
    """规格书 + 段 → 工作台视觉时间线 blocks（纯函数，不写盘）。

    ``asset_id_by_key`` 把 spec 的 ``sources[].key`` 映射到项目资产 ID；
    缺少映射直接 raise，避免静默绑错素材。
    """
    fps_value = int(fps if fps is not None else spec.get("fps") or 30)
    duration = _duration_of(section)
    blocks: list[dict[str, Any]] = []
    for index, shot in enumerate(section.get("shots", []), 1):
        key = str(shot.get("source") or "")
        asset_id = asset_id_by_key.get(key)
        if not asset_id:
            raise KeyError(f"缺少 source {key} 的资产映射")
        start = round(float(shot.get("start_seconds") or 0.0), 3)
        end = round(float(shot.get("end_seconds") or 0.0), 3)
        source_in = round(float(shot.get("in") or 0.0), 3)
        source_out = round(float(shot.get("out") or 0.0), 3)
        # 源窗口按帧回写，确保 abs(显示帧数 - 源帧数) == 0。
        source_in = frames_to_seconds(quantize_frames(source_in, fps_value), fps_value)
        source_out = frames_to_seconds(quantize_frames(source_out, fps_value), fps_value)
        blocks.append(
            {
                "id": f"VB-{index:03d}",
                "start_seconds": start,
                "end_seconds": end,
                "source_mode": "web_download",
                "asset_id": asset_id,
                "label": f"{key} {str(shot.get('intent') or '')}"[:160],
                "source_in_seconds": source_in,
                "source_out_seconds": source_out,
                "locked": False,
            }
        )
    if blocks and abs(blocks[-1]["end_seconds"] - duration) > 0.001:
        blocks[-1]["end_seconds"] = duration
    return blocks


def asset_key_map(spec: dict[str, Any], asset_id_by_key: dict[str, str]) -> dict[str, str]:
    """校验 spec 的每个 source 都有资产映射，返回规范化副本。"""
    missing = [source["key"] for source in spec.get("sources", []) if source["key"] not in asset_id_by_key]
    if missing:
        raise KeyError(f"缺少资产映射：{', '.join(missing)}")
    return {source["key"]: asset_id_by_key[source["key"]] for source in spec.get("sources", [])}


__all__ = ["asset_key_map", "build_script", "build_visual_blocks"]
