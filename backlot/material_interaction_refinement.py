"""Deterministic evidence planning for outdoor-interaction fine cuts."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import subprocess
from typing import Any


VERSION = "interaction-refinement-v2"
BOUNDARY_EXTENSION_SECONDS = 6.0
PROTECTION_PADDING_SECONDS = .15
STAGES = {"greeting", "dialogue", "action", "reaction", "waiting", "departure", "unrelated", "unknown"}
PAUSE_VISUAL_VERSION = "interaction-pause-visual-v1"
PAUSE_VISUAL_FPS = 2.0
PAUSE_VISUAL_WIDTH = 96
PAUSE_VISUAL_HEIGHT = 54
PAUSE_VISUAL_MAX_WINDOWS = 12
PAUSE_VISUAL_MAX_SECONDS = 2.0
PAUSE_VISUAL_LOW_MOTION = .035


class InteractionRefinementError(ValueError):
    pass


def pause_visual_identity() -> dict[str, Any]:
    identity = {
        "version": PAUSE_VISUAL_VERSION, "fps": PAUSE_VISUAL_FPS,
        "width": PAUSE_VISUAL_WIDTH, "height": PAUSE_VISUAL_HEIGHT,
        "max_windows": PAUSE_VISUAL_MAX_WINDOWS, "max_seconds": PAUSE_VISUAL_MAX_SECONDS,
        "low_motion_threshold": PAUSE_VISUAL_LOW_MOTION,
    }
    identity["signature"] = _digest(identity)
    return identity


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _number(value: Any, *, minimum: float, maximum: float, label: str) -> float:
    if isinstance(value, bool):
        raise InteractionRefinementError(f"{label}格式无效")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise InteractionRefinementError(f"{label}格式无效") from exc
    if not math.isfinite(result) or not minimum <= result <= maximum:
        raise InteractionRefinementError(f"{label}超出范围")
    return result


def _intersects(a: float, b: float, c: float, d: float) -> bool:
    return a < d and c < b


def _valid_utterances(index: dict[str, Any]) -> list[dict[str, Any]]:
    duration = float(index.get("duration") or 0)
    result = []
    for row in (index.get("audio") or {}).get("utterances") or []:
        if not isinstance(row, dict):
            continue
        try:
            start = _number(row.get("start"), minimum=0, maximum=duration, label="分句开始")
            end = _number(row.get("end"), minimum=0, maximum=duration, label="分句结束")
        except InteractionRefinementError:
            continue
        if end > start:
            result.append({**deepcopy(row), "start": round(start, 3), "end": round(end, 3)})
    return sorted(result, key=lambda item: (item["start"], item["end"], str(item.get("id") or "")))


def merge_ranges(rows: list[dict[str, Any]], *, duration: float, padding: float = 0) -> list[dict[str, Any]]:
    normalized = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            start = max(0.0, float(row["start"]) - padding)
            end = min(duration, float(row["end"]) + padding)
        except (KeyError, TypeError, ValueError):
            continue
        if end <= start:
            continue
        normalized.append({"start": start, "end": end, "evidence_ids": list(row.get("evidence_ids") or [])})
    merged: list[dict[str, Any]] = []
    for row in sorted(normalized, key=lambda item: (item["start"], item["end"])):
        if not merged or row["start"] > merged[-1]["end"]:
            merged.append(row)
        else:
            merged[-1]["end"] = max(merged[-1]["end"], row["end"])
            merged[-1]["evidence_ids"] = list(dict.fromkeys(merged[-1]["evidence_ids"] + row["evidence_ids"]))
    return [{**row, "start": round(row["start"], 3), "end": round(row["end"], 3)} for row in merged]


def validate_semantic_annotations(rows: Any, *, event_start: float, event_end: float,
                                  allowed_frame_ids: set[str] | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Retain valid local/remote annotations and make every rejection explicit."""
    valid: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for sequence, row in enumerate(rows if isinstance(rows, list) else []):
        reason = None
        if not isinstance(row, dict):
            reason = "结构不是对象"
        else:
            try:
                start = _number(row.get("start"), minimum=event_start, maximum=event_end, label="标注开始")
                end = _number(row.get("end"), minimum=event_start, maximum=event_end, label="标注结束")
                confidence = _number(row.get("confidence", 0), minimum=0, maximum=1, label="标注置信度")
                stage = str(row.get("stage") or "unknown")
                evidence = list(dict.fromkeys(str(item) for item in (row.get("evidence_frame_ids") or [])))
                if end <= start:
                    reason = "区间为空或倒序"
                elif stage not in STAGES:
                    reason = "阶段枚举无效"
                elif allowed_frame_ids is not None and any(item not in allowed_frame_ids for item in evidence):
                    reason = "引用了未发送的画面证据"
                else:
                    valid.append({
                        **deepcopy(row), "id": str(row.get("id") or f"A{sequence+1:03d}"),
                        "start": round(start, 3), "end": round(end, 3),
                        "confidence": round(confidence, 3), "stage": stage,
                        "evidence_frame_ids": evidence,
                    })
            except InteractionRefinementError as exc:
                reason = str(exc)
        if reason:
            rejected.append({"sequence": sequence, "reason": reason, "raw": deepcopy(row)})
    return valid, rejected


def plan_pause_visual_windows(index: dict[str, Any], *, max_windows: int = PAUSE_VISUAL_MAX_WINDOWS,
                              max_seconds: float = PAUSE_VISUAL_MAX_SECONDS) -> list[dict[str, Any]]:
    """Choose one bounded visual evidence window for the longest ASR gaps.

    The budget is global to the source index, not renewed for every event.  A
    two-second sample can only authorize edits inside that exact sample.
    """
    if not 1 <= int(max_windows) <= PAUSE_VISUAL_MAX_WINDOWS:
        raise InteractionRefinementError("局部画面疑点预算超出范围")
    max_seconds = _number(max_seconds, minimum=.5, maximum=PAUSE_VISUAL_MAX_SECONDS, label="局部画面窗口")
    utterances = _valid_utterances(index)
    candidates = []
    for left, right in zip(utterances, utterances[1:]):
        gap_start, gap_end = float(left["end"]), float(right["start"])
        available_start, available_end = gap_start + PROTECTION_PADDING_SECONDS, gap_end - PROTECTION_PADDING_SECONDS
        if available_end - available_start < .5:
            continue
        duration = min(max_seconds, available_end - available_start)
        center = (available_start + available_end) / 2
        start, end = center - duration / 2, center + duration / 2
        candidates.append({
            "start": round(start, 3), "end": round(end, 3),
            "gap_start": round(gap_start, 3), "gap_end": round(gap_end, 3),
            "gap_seconds": round(gap_end - gap_start, 3),
            "left_utterance_id": str(left.get("id") or ""),
            "right_utterance_id": str(right.get("id") or ""),
        })
    selected = sorted(candidates, key=lambda row: (-row["gap_seconds"], row["start"]))[:int(max_windows)]
    selected.sort(key=lambda row: row["start"])
    return [{**row, "id": f"PV{sequence:03d}"} for sequence, row in enumerate(selected, 1)]


def analyze_pause_visual_activity(
    source: Path, index: dict[str, Any], *, ffmpeg: str,
    timeout: float = 180.0,
    runner: Any = subprocess.run,
) -> dict[str, Any]:
    """Classify bounded pause windows using local low-resolution motion only.

    This is intentionally conservative and semantic-free: low motion may
    authorize a short waiting edit, while high motion and decode uncertainty
    only protect/retain content.  It never claims to identify a person/action.
    """
    source = Path(source).resolve()
    if not source.is_file():
        raise InteractionRefinementError("局部画面源素材不存在")
    windows = plan_pause_visual_windows(index)
    try:
        import numpy as np
    except ImportError:
        return {
            "version": PAUSE_VISUAL_VERSION, "status": "unavailable", "identity": None,
            "windows": [{
                **row, "stage": "unknown", "confidence": 0.0, "safe_to_shorten": False,
                "evidence_frame_ids": [], "local_evidence_ids": [], "frame_count": 0,
                "reason": "本地画面分析依赖不可用，保守保留",
            } for row in windows],
            "metadata": {"window_count": len(windows), "failed_windows": len(windows), "frame_count": 0},
        }
    annotations = []
    failures = 0
    for row in windows:
        command = [
            ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error",
            "-ss", f"{row['start']:.6f}", "-to", f"{row['end']:.6f}", "-i", str(source),
            "-an", "-vf", f"fps={PAUSE_VISUAL_FPS:g},scale={PAUSE_VISUAL_WIDTH}:{PAUSE_VISUAL_HEIGHT}:flags=area,format=gray",
            "-f", "rawvideo", "pipe:1",
        ]
        try:
            completed = runner(
                command, capture_output=True, timeout=max(30.0, min(float(timeout), 180.0)), check=False,
            )
            raw = bytes(completed.stdout or b"")
            frame_size = PAUSE_VISUAL_WIDTH * PAUSE_VISUAL_HEIGHT
            if completed.returncode != 0 or len(raw) % frame_size:
                raise ValueError("decode")
            frames = np.frombuffer(raw, dtype=np.uint8).reshape((-1, frame_size)).astype(np.float32)
            if len(frames) < 2:
                raise ValueError("few-frames")
            scores = np.mean(np.abs(np.diff(frames, axis=0)), axis=1) / 255.0
            max_motion = float(np.max(scores))
            median_motion = float(np.median(scores))
            low_motion = max_motion <= PAUSE_VISUAL_LOW_MOTION
            stage = "waiting" if low_motion else "action"
            annotation = {
                **row, "stage": stage,
                "confidence": round(max(.85, 1.0 - max_motion), 3) if low_motion else .5,
                "safe_to_shorten": low_motion,
                "evidence_frame_ids": [],
                "local_evidence_ids": [f"{row['id']}-F{number+1:02d}" for number in range(len(frames))],
                "frame_count": int(len(frames)),
                "motion": {"maximum": round(max_motion, 6), "median": round(median_motion, 6),
                           "low_motion_threshold": PAUSE_VISUAL_LOW_MOTION},
                "reason": "本地低分辨率采样显示画面稳定" if low_motion else "局部画面存在活动，保守保留",
            }
        except (OSError, subprocess.TimeoutExpired, ValueError):
            failures += 1
            annotation = {
                **row, "stage": "unknown", "confidence": 0.0, "safe_to_shorten": False,
                "evidence_frame_ids": [], "local_evidence_ids": [], "frame_count": 0,
                "reason": "局部画面证据不足，保守保留",
            }
        annotations.append(annotation)
    identity = pause_visual_identity()
    return {
        "version": PAUSE_VISUAL_VERSION,
        "status": "available" if not failures else ("partial" if failures < len(windows) else "unavailable"),
        "identity": identity, "windows": annotations,
        "metadata": {"window_count": len(windows), "failed_windows": failures,
                     "frame_count": sum(int(row.get("frame_count") or 0) for row in annotations)},
    }


def refine_event(
    index: dict[str, Any],
    review: dict[str, Any],
    event_id: str,
    *,
    speech_ranges: list[dict[str, Any]] | None = None,
    semantic_annotations: list[dict[str, Any]] | None = None,
    allowed_frame_ids: set[str] | None = None,
    max_extension: float = BOUNDARY_EXTENSION_SECONDS,
    protection_padding: float = PROTECTION_PADDING_SECONDS,
) -> dict[str, Any]:
    if index.get("status") != "completed":
        raise InteractionRefinementError("互动索引尚未完成")
    duration = _number(index.get("duration"), minimum=.001, maximum=24 * 3600, label="素材时长")
    event = next((item for item in review.get("events") or [] if item.get("review_event_id") == event_id), None)
    if not isinstance(event, dict):
        raise InteractionRefinementError("未找到要精剪的互动事件")
    original_start = _number(event.get("start"), minimum=0, maximum=duration, label="事件开始")
    original_end = _number(event.get("end"), minimum=0, maximum=duration, label="事件结束")
    if original_end <= original_start:
        raise InteractionRefinementError("互动事件范围无效")

    utterances = _valid_utterances(index)
    boundary_hits = [
        row for row in utterances
        if (row["start"] < original_start < row["end"]) or (row["start"] < original_end < row["end"])
    ]
    proposed_start, proposed_end = original_start, original_end
    for row in boundary_hits:
        if original_start - max_extension <= row["start"] < original_start:
            proposed_start = min(proposed_start, row["start"])
        if original_end < row["end"] <= original_end + max_extension:
            proposed_end = max(proposed_end, row["end"])

    conflicts = []
    for other in review.get("events") or []:
        if not isinstance(other, dict) or other.get("review_event_id") == event_id or other.get("status") == "discarded":
            continue
        try:
            other_start, other_end = float(other["start"]), float(other["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if str(other.get("group_id") or "") != str(event.get("group_id") or "") and _intersects(
            proposed_start, proposed_end, other_start, other_end
        ):
            conflicts.append(str(other.get("review_event_id") or "未知事件"))
    if conflicts:
        proposed_start, proposed_end = original_start, original_end

    valid_annotations, rejected_annotations = validate_semantic_annotations(
        semantic_annotations or [], event_start=proposed_start, event_end=proposed_end,
        allowed_frame_ids=allowed_frame_ids,
    )
    utterance_protection = [
        {"start": row["start"], "end": row["end"], "evidence_ids": [str(row.get("id") or "ASR")]}
        for row in utterances if _intersects(proposed_start, proposed_end, row["start"], row["end"])
    ]
    vad_protection = [
        {"start": float(row["start"]), "end": float(row["end"]),
         "evidence_ids": [f"vad:{float(row['start']):.3f}-{float(row['end']):.3f}"]}
        for row in speech_ranges or []
        if isinstance(row, dict) and _intersects(proposed_start, proposed_end, float(row.get("start", 0)), float(row.get("end", 0)))
    ]
    action_protection = [
        {"start": row["start"], "end": row["end"],
         "evidence_ids": [str(row.get("id") or "action"), *row.get("evidence_frame_ids", [])]}
        for row in valid_annotations if row["stage"] in {"greeting", "dialogue", "action", "reaction", "departure"}
    ]
    protected = merge_ranges(
        utterance_protection + vad_protection + action_protection,
        duration=duration, padding=protection_padding,
    )
    warnings = []
    if conflicts:
        warnings.append(f"边界扩展会进入其他互动组：{'、'.join(conflicts)}；已保留原范围并阻止自动内部精剪")
    if rejected_annotations:
        warnings.append(f"有{len(rejected_annotations)}条局部标注因证据无效被拒绝")
    if boundary_hits and proposed_start == original_start and proposed_end == original_end and not conflicts:
        warnings.append("检测到边界穿过分句，但未能在允许的6秒范围内安全扩展")
    if (index.get("audio") or {}).get("status") != "available":
        warnings.append("没有可用分句转写；只生成连续互动候选")

    result = {
        "version": VERSION,
        "event_id": event_id,
        "group_id": event.get("group_id"),
        "original_range": {"start": round(original_start, 3), "end": round(original_end, 3)},
        "protected_range": {"start": round(proposed_start, 3), "end": round(proposed_end, 3)},
        "boundary_changes": {
            "start_seconds": round(proposed_start - original_start, 3),
            "end_seconds": round(proposed_end - original_end, 3),
            "evidence_utterance_ids": [str(row.get("id") or "") for row in boundary_hits],
        },
        "protected_ranges": protected,
        "annotations": valid_annotations,
        "rejected_annotations": rejected_annotations,
        "warnings": warnings,
        "safe_for_internal_edit": not conflicts and (index.get("audio") or {}).get("status") == "available",
    }
    result["signature"] = _digest(result)
    return result
