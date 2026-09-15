"""Evidence-gated edit plans for reviewable outdoor-interaction candidates."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
import os
import tempfile
import time
from typing import Any
from pathlib import Path

from backlot.material_audio_evidence import audio_policy


VERSION = "interaction-edit-plan-v2"
SUPPORTED_VERSIONS = {"interaction-edit-plan-v1", VERSION}
MIN_GAP_SECONDS = .8
TARGET_GAP_SECONDS = .3
SPEECH_GUARD_SECONDS = .15
VISUAL_DROP_CONFIDENCE = .9


class InteractionEditError(ValueError):
    pass


class InteractionEditConflict(InteractionEditError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
        # On Windows a concurrent API poll or virus scanner can briefly hold the
        # destination open. Keep the write atomic and retry only that transient
        # sharing violation; never expose a partially-written plan.
        for attempt in range(12):
            try:
                os.replace(temporary, path)
                return
            except PermissionError:
                if attempt == 11:
                    raise
                time.sleep(.01 * (attempt + 1))
    finally:
        temporary.unlink(missing_ok=True)


def _number(value: Any, *, minimum: float, maximum: float, label: str) -> float:
    if isinstance(value, bool):
        raise InteractionEditError(f"{label}格式无效")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise InteractionEditError(f"{label}格式无效") from exc
    if not math.isfinite(result) or not minimum <= result <= maximum:
        raise InteractionEditError(f"{label}超出范围")
    return round(result, 3)


def _intersects(left_start: float, left_end: float, right_start: float, right_end: float) -> bool:
    return left_start < right_end and right_start < left_end


def _utterances_for_event(index: dict[str, Any], event: dict[str, Any], *,
                          event_start: float | None = None, event_end: float | None = None) -> list[dict[str, Any]]:
    """Return source-global overlapping utterances without clipping their edges.

    The model-provided ``utterance_ids`` remain useful evidence metadata but are
    not a deletion safety boundary: a missed ID must never allow speech to be
    cut.  V1 clipped rows to the event range, which hid boundary crossings.
    """
    rows = (index.get("audio") or {}).get("utterances") or []
    start_limit = float(event["start"] if event_start is None else event_start)
    end_limit = float(event["end"] if event_end is None else event_end)
    selected = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            start, end = float(row["start"]), float(row["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if end > start_limit and start < end_limit:
            selected.append({**row, "start": start, "end": end})
    return sorted(selected, key=lambda row: (row["start"], row["end"]))


def _covering_silence(silences: list[dict[str, Any]], start: float, end: float) -> dict[str, float] | None:
    matches = []
    for row in silences:
        try:
            s, e = float(row["start"]), float(row["end"])
        except (KeyError, TypeError, ValueError):
            continue
        overlap = max(0.0, min(end, e) - max(start, s))
        if overlap:
            matches.append((overlap, {"start": s, "end": e}))
    return max(matches, key=lambda row: row[0])[1] if matches else None


def _subtract_protected(start: float, end: float,
                        protected_ranges: list[dict[str, Any]]) -> list[tuple[float, float]]:
    pieces = [(start, end)]
    for protected in sorted(protected_ranges, key=lambda row: float(row.get("start", 0))):
        try:
            left, right = float(protected["start"]), float(protected["end"])
        except (KeyError, TypeError, ValueError):
            continue
        next_pieces = []
        for piece_start, piece_end in pieces:
            if not _intersects(piece_start, piece_end, left, right):
                next_pieces.append((piece_start, piece_end))
                continue
            if piece_start < left:
                next_pieces.append((piece_start, min(piece_end, left)))
            if right < piece_end:
                next_pieces.append((max(piece_start, right), piece_end))
        pieces = next_pieces
    return [(left, right) for left, right in pieces if right - left >= .04]


def _pause_removals(event: dict[str, Any], utterances: list[dict[str, Any]],
                     silences: list[dict[str, Any]], protected_ranges: list[dict[str, Any]],
                     target_gap: float, semantic_annotations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    removals = []
    for left, right in zip(utterances, utterances[1:]):
        gap_start, gap_end = float(left["end"]), float(right["start"])
        if gap_end - gap_start <= MIN_GAP_SECONDS:
            continue
        silence = _covering_silence(silences, gap_start, gap_end)
        if not silence:
            continue
        safe_start = max(gap_start + SPEECH_GUARD_SECONDS, silence["start"])
        safe_end = min(gap_end - SPEECH_GUARD_SECONDS, silence["end"])
        pieces = _subtract_protected(safe_start, safe_end, protected_ranges)
        visual_waits = [row for row in semantic_annotations
                        if row.get("stage") == "waiting" and row.get("safe_to_shorten") is True
                        and float(row.get("confidence") or 0) >= .85]
        visually_confirmed = []
        for piece_start, piece_end in pieces:
            for row in visual_waits:
                visual_start, visual_end = float(row["start"]), float(row["end"])
                left_edge, right_edge = max(piece_start, visual_start), min(piece_end, visual_end)
                if right_edge - left_edge >= .08:
                    visually_confirmed.append((left_edge, right_edge, row))
        if not visually_confirmed:
            continue
        # Choose the longest protected-free evidence interval.  Never stitch
        # two safe pieces across speech or an action to hit a duration target.
        safe_start, safe_end, visual = max(
            visually_confirmed, key=lambda piece: piece[1] - piece[0]
        )
        maximum_remove = max(0.0, gap_end - gap_start - target_gap)
        remove_end = min(safe_end, safe_start + maximum_remove)
        if remove_end - safe_start < .08:
            continue
        output_gap = gap_end - gap_start - (remove_end - safe_start)
        removals.append({
            "id": f"D{len(removals)+1:03d}", "start": round(safe_start, 3),
            "end": round(remove_end, 3), "reason_code": "confirmed_silent_wait",
            "reason": "分句间隔、语音活动与本地低运动画面共同确认的无效等待",
            "evidence_ids": [str(left.get("id")), str(right.get("id")),
                             f"silence:{silence['start']:.3f}-{silence['end']:.3f}",
                             str(visual.get("id") or "local-visual-wait")],
            "restored": False,
            "decision": "shorten", "original_gap_seconds": round(gap_end - gap_start, 3),
            "output_gap_seconds": round(output_gap, 3),
        })
    return removals


def _visual_removals(event: dict[str, Any], utterances: list[dict[str, Any]],
                     rows: list[dict[str, Any]], start_sequence: int,
                     protected_ranges: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            start = _number(row.get("start"), minimum=float(event["start"]), maximum=float(event["end"]), label="无关段开始")
            end = _number(row.get("end"), minimum=float(event["start"]), maximum=float(event["end"]), label="无关段结束")
            confidence = _number(row.get("confidence"), minimum=0, maximum=1, label="无关段置信度")
        except InteractionEditError:
            continue
        evidence = row.get("evidence_frame_ids")
        gates = (confidence >= VISUAL_DROP_CONFIDENCE, row.get("no_related_speech") is True,
                 row.get("no_key_action") is True, row.get("context_preserved") is True,
                 isinstance(evidence, list) and len(set(evidence)) >= 2, end - start >= .08)
        if not all(gates):
            continue
        # A transcript hit is a hard veto even when a visual model says no speech.
        if any(_intersects(start, end, float(item["start"]), float(item["end"])) for item in utterances):
            continue
        if any(_intersects(start, end, float(item["start"]), float(item["end"])) for item in protected_ranges):
            continue
        result.append({
            "id": f"D{start_sequence + len(result):03d}", "start": start, "end": end,
            "reason_code": "high_confidence_irrelevant_visual",
            "reason": str(row.get("reason") or "音画证据支持的无关画面区间")[:240],
            "evidence_ids": list(dict.fromkeys(str(item) for item in evidence)), "restored": False,
            "decision": "drop",
        })
    return result


def _merge_removals(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    for row in sorted(rows, key=lambda item: (item["start"], item["end"])):
        if not merged or row["start"] >= merged[-1]["end"]:
            merged.append(deepcopy(row))
            continue
        # Overlapping independent reasons are joined; all evidence remains auditable.
        old = merged[-1]
        old["end"] = max(old["end"], row["end"])
        old["reason_code"] = "combined_evidence"
        old["reason"] = "；".join(dict.fromkeys([old["reason"], row["reason"]]))
        old["evidence_ids"] = list(dict.fromkeys(old["evidence_ids"] + row["evidence_ids"]))
    for sequence, row in enumerate(merged, 1):
        row["id"] = f"D{sequence:03d}"
    return merged


def keep_ranges(event_start: float, event_end: float, removals: list[dict[str, Any]]) -> list[dict[str, float]]:
    cursor = event_start
    result = []
    for row in sorted((item for item in removals if not item.get("restored")), key=lambda item: item["start"]):
        if row["start"] > cursor:
            result.append({"start": round(cursor, 3), "end": round(row["start"], 3)})
        cursor = max(cursor, row["end"])
    if cursor < event_end:
        result.append({"start": round(cursor, 3), "end": round(event_end, 3)})
    return [row for row in result if row["end"] - row["start"] >= .04]


def timeline_mapping(ranges: list[dict[str, float]]) -> list[dict[str, float]]:
    cursor = 0.0
    result = []
    for row in ranges:
        duration = float(row["end"]) - float(row["start"])
        result.append({
            "source_start": round(float(row["start"]), 3),
            "source_end": round(float(row["end"]), 3),
            "output_start": round(cursor, 3),
            "output_end": round(cursor + duration, 3),
        })
        cursor += duration
    return result


def build_edit_plan(index: dict[str, Any], review: dict[str, Any], event_id: str, *,
                    silence_intervals: list[dict[str, Any]] | None = None,
                    irrelevant_candidates: list[dict[str, Any]] | None = None,
                    refinement: dict[str, Any] | None = None,
                    speech_activity: dict[str, Any] | None = None,
                    target_gap: float = TARGET_GAP_SECONDS) -> dict[str, Any]:
    if index.get("status") != "completed":
        raise InteractionEditError("互动索引尚未完成")
    review_source = review.get("source") if isinstance(review.get("source"), dict) else {}
    review_index_signature = review_source.get("index_signature") or review.get("index_signature")
    if review_index_signature != index.get("signature"):
        raise InteractionEditError("互动审核与分析索引已经变化")
    event = next((row for row in review.get("events") or [] if row.get("review_event_id") == event_id), None)
    if not event:
        raise InteractionEditError("未找到要精剪的互动事件")
    if event.get("status") == "discarded":
        raise InteractionEditError("已弃用事件不能生成精剪候选")
    refined_range = (refinement or {}).get("protected_range") if isinstance(refinement, dict) else None
    start_value = refined_range.get("start") if isinstance(refined_range, dict) else event.get("start")
    end_value = refined_range.get("end") if isinstance(refined_range, dict) else event.get("end")
    start = _number(start_value, minimum=0, maximum=float(index.get("duration") or 0), label="事件开始")
    end = _number(end_value, minimum=0, maximum=float(index.get("duration") or 0), label="事件结束")
    if end <= start:
        raise InteractionEditError("互动事件时间范围无效")
    target_gap = _number(target_gap, minimum=.3, maximum=.8, label="目标停顿")
    utterances = _utterances_for_event(index, event, event_start=start, event_end=end)
    audio_enabled = (index.get("audio") or {}).get("status") == "available"
    protected_ranges = deepcopy((refinement or {}).get("protected_ranges") or [])
    internal_safe = bool((refinement or {}).get("safe_for_internal_edit", True))
    evidence_intervals = deepcopy(silence_intervals or [])
    if isinstance(speech_activity, dict) and speech_activity.get("status") == "available":
        for row in speech_activity.get("non_speech_ranges") or []:
            if isinstance(row, dict):
                evidence_intervals.append({**row, "source": "vad_non_speech"})
    removals = []
    if audio_enabled and utterances and internal_safe:
        removals.extend(_pause_removals(
            event, utterances, evidence_intervals, protected_ranges, target_gap,
            list((refinement or {}).get("annotations") or []),
        ))
        removals.extend(_visual_removals(event, utterances, irrelevant_candidates or [], len(removals) + 1,
                                        protected_ranges))
    removals = _merge_removals(removals)
    source = deepcopy(index.get("source") or {})
    frozen = {
        "version": VERSION, "source": source, "index_signature": index.get("signature"),
        "review_revision": review.get("revision"), "event_id": event_id, "group_id": event.get("group_id"),
        "event_start": start, "event_end": end,
        "audio_policy": (index.get("audio") or {}).get("policy") or audio_policy(audio_enabled),
        "audio_status": (index.get("audio") or {}).get("status"),
        "asr_identity": (index.get("audio") or {}).get("provider"),
        "transcript_signature": _digest(utterances),
        "refinement_signature": (refinement or {}).get("signature"),
        "speech_activity_signature": ((speech_activity or {}).get("identity") or {}).get("signature"),
        "evidence_signature": _digest({"silence": evidence_intervals,
                                        "irrelevant": irrelevant_candidates or [],
                                        "protected": protected_ranges}),
        "edit_policy": {"minimum_gap": MIN_GAP_SECONDS, "target_gap": target_gap,
                        "speech_guard": SPEECH_GUARD_SECONDS, "visual_drop_confidence": VISUAL_DROP_CONFIDENCE},
    }
    keeps = keep_ranges(start, end, removals)
    plan_id = "IEP-" + _digest({**frozen, "removed_ranges": removals, "keep_ranges": keeps})[:16]
    plan = {
        **frozen, "plan_id": plan_id, "revision": 0, "status": "pending_review",
        "created_at": _now(), "updated_at": _now(), "removed_ranges": removals, "keep_ranges": keeps,
        "timeline_mapping": timeline_mapping(keeps),
        "original_range": deepcopy((refinement or {}).get("original_range") or {"start": start, "end": end}),
        "protected_range": {"start": start, "end": end}, "protected_ranges": protected_ranges,
        "refinement": deepcopy(refinement), "speech_activity": deepcopy(speech_activity),
        "warnings": list((refinement or {}).get("warnings") or []),
        "source_duration": round(end - start, 3),
        "output_duration": round(sum(row["end"] - row["start"] for row in keeps), 3),
        "qa": {"status": "not_rendered"}, "preview": None, "history": [],
    }
    validate_edit_plan(plan)
    return plan


def validate_edit_plan(plan: dict[str, Any]) -> None:
    if plan.get("version") not in SUPPORTED_VERSIONS:
        raise InteractionEditError("精剪清单版本不受支持")
    start, end = float(plan.get("event_start", -1)), float(plan.get("event_end", -1))
    if not 0 <= start < end:
        raise InteractionEditError("精剪清单事件范围无效")
    removals = plan.get("removed_ranges")
    if not isinstance(removals, list):
        raise InteractionEditError("精剪清单删减范围无效")
    active = sorted((row for row in removals if not row.get("restored")), key=lambda row: row.get("start", -1))
    previous_end = start
    ids = set()
    for row in active:
        row_start = _number(row.get("start"), minimum=start, maximum=end, label="删减开始")
        row_end = _number(row.get("end"), minimum=start, maximum=end, label="删减结束")
        if row_end <= row_start or row_start < previous_end:
            raise InteractionEditError("删减范围重叠、倒序或为空")
        if not row.get("id") or row["id"] in ids or not row.get("evidence_ids"):
            raise InteractionEditError("删减范围缺少唯一编号或证据")
        ids.add(row["id"])
        previous_end = row_end
    expected = keep_ranges(start, end, removals)
    if plan.get("keep_ranges") != expected:
        raise InteractionEditError("保留范围与删减范围不一致")
    expected_duration = round(sum(row["end"] - row["start"] for row in expected), 3)
    if abs(float(plan.get("output_duration", -1)) - expected_duration) > .002:
        raise InteractionEditError("精剪输出时长与清单不一致")
    if plan.get("version") == VERSION:
        protected = plan.get("protected_ranges")
        if not isinstance(protected, list):
            raise InteractionEditError("精剪清单缺少保护区")
        for row in active:
            if any(_intersects(float(row["start"]), float(row["end"]),
                               float(item["start"]), float(item["end"])) for item in protected):
                raise InteractionEditError("删减范围与对白或动作保护区冲突")
        if plan.get("timeline_mapping") != timeline_mapping(expected):
            raise InteractionEditError("原片与候选时间映射不一致")
    history = plan.get("history", [])
    if not isinstance(history, list) or len(history) > 50 or any(not isinstance(row, dict) for row in history):
        raise InteractionEditError("精剪方案撤销记录无效")


def read_edit_plan(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise InteractionEditError("精剪方案不存在")
    try:
        plan = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InteractionEditError("精剪方案损坏") from exc
    validate_edit_plan(plan)
    return plan


def write_edit_plan(path: Path, plan: dict[str, Any]) -> dict[str, Any]:
    validate_edit_plan(plan)
    _atomic_write(path, plan)
    return plan


def _snapshot(plan: dict[str, Any]) -> dict[str, Any]:
    return deepcopy({key: plan.get(key) for key in (
        "removed_ranges", "keep_ranges", "timeline_mapping", "output_duration", "status", "qa", "preview",
    )})


def apply_plan_action(plan: dict[str, Any], *, action: str, expected_revision: int,
                      removal_id: str | None = None,
                      removal_states: dict[str, bool] | None = None) -> dict[str, Any]:
    validate_edit_plan(plan)
    if int(plan.get("revision", -1)) != int(expected_revision):
        raise InteractionEditConflict("精剪方案已被其他操作更新，请刷新后重试")
    if plan.get("status") in {"approved", "rejected"}:
        raise InteractionEditError("已确认或弃用的候选不可继续改写；请调整互动目录后生成新候选")
    result = deepcopy(plan)
    if action == "undo":
        history = result.get("history") or []
        if not history:
            raise InteractionEditError("没有可以撤销的精剪操作")
        snapshot = history.pop()
        for key in ("removed_ranges", "keep_ranges", "timeline_mapping", "output_duration", "status", "qa", "preview"):
            result[key] = deepcopy(snapshot.get(key))
        result["history"] = history
    elif action in {"restore_removal", "reinstate_removal"}:
        result.setdefault("history", []).append(_snapshot(result))
        result["history"] = result["history"][-50:]
        row = next((item for item in result["removed_ranges"] if item.get("id") == removal_id), None)
        if not row:
            raise InteractionEditError("未找到要恢复的删减区间")
        row["restored"] = action == "restore_removal"
        result["status"] = "pending_review"
        result["qa"] = {"status": "stale"}
        result["preview"] = deepcopy(result.get("preview"))
        if isinstance(result["preview"], dict):
            result["preview"]["stale"] = True
    elif action == "save_edits":
        if not isinstance(removal_states, dict) or not removal_states:
            raise InteractionEditError("没有需要保存的精剪调整")
        known = {str(row.get("id")) for row in result["removed_ranges"]}
        if any(str(item) not in known or not isinstance(value, bool) for item, value in removal_states.items()):
            raise InteractionEditError("精剪调整包含未知删减区间")
        result.setdefault("history", []).append(_snapshot(result))
        result["history"] = result["history"][-50:]
        for row in result["removed_ranges"]:
            if str(row.get("id")) in removal_states:
                row["restored"] = bool(removal_states[str(row.get("id"))])
        result["status"] = "pending_review"
        result["qa"] = {"status": "stale"}
        result["preview"] = deepcopy(result.get("preview"))
        if isinstance(result["preview"], dict):
            result["preview"]["stale"] = True
    elif action in {"approve", "reject"}:
        if (result.get("qa") or {}).get("status") != "passed" and action == "approve":
            raise InteractionEditError("候选预览尚未通过 QA，不能确认入库")
        result.setdefault("history", []).append(_snapshot(result))
        result["history"] = result["history"][-50:]
        result["status"] = "approved" if action == "approve" else "rejected"
    else:
        raise InteractionEditError("不支持的精剪操作")
    result["revision"] = int(result["revision"]) + 1
    result["updated_at"] = _now()
    result["keep_ranges"] = keep_ranges(result["event_start"], result["event_end"], result["removed_ranges"])
    if result.get("version") == VERSION:
        result["timeline_mapping"] = timeline_mapping(result["keep_ranges"])
    result["output_duration"] = round(sum(row["end"] - row["start"] for row in result["keep_ranges"]), 3)
    validate_edit_plan(result)
    return result


def attach_render_result(plan: dict[str, Any], manifest: dict[str, Any], *,
                         expected_revision: int, preview_path: str) -> dict[str, Any]:
    validate_edit_plan(plan)
    if int(plan.get("revision", -1)) != int(expected_revision):
        raise InteractionEditConflict("精剪方案已被其他操作更新，旧预览不会覆盖新方案")
    if manifest.get("plan_id") != plan.get("plan_id") or int(manifest.get("plan_revision", -1)) != int(plan["revision"]):
        raise InteractionEditError("候选预览不属于当前精剪方案")
    if (manifest.get("qa") or {}).get("status") != "passed" or not str(manifest.get("signature") or ""):
        raise InteractionEditError("候选预览尚未通过 QA")
    result = deepcopy(plan)
    result["qa"] = deepcopy(manifest["qa"])
    result["preview"] = {
        "path": str(preview_path), "signature": manifest["signature"],
        "output_duration": manifest.get("output_duration"),
    }
    result["status"] = "pending_review"
    result["revision"] = int(result["revision"]) + 1
    result["updated_at"] = _now()
    validate_edit_plan(result)
    return result
