"""Validated semantic units for a selected outdoor interaction.

The model may classify and group supplied evidence IDs.  It never owns source
timestamps: OpenMontage derives every range from the frozen ASR evidence and
the parent candidate's allowed source ranges.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
from typing import Any, Callable


VERSION = "interaction-story-v2"
PROMPT_VERSION = "interaction-story-prompt-v2"
GROUP_TYPES = {
    "greeting", "question_answer", "follow_up", "setup", "punchline",
    "reaction", "action", "result", "farewell", "repetition", "other",
}
DECISIONS = {"keep", "drop"}
MAX_GROUPS = 80
MAX_HOOKS = 3
MAX_VISUAL_EVENTS = 3
MAX_VISUAL_FRAMES = 12
MAX_PAUSE_WINDOWS = 12
EDGE_GUARD_SECONDS = .15


class InteractionStoryError(ValueError):
    pass


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _number(value: Any, *, minimum: float, maximum: float, label: str) -> float:
    if isinstance(value, bool):
        raise InteractionStoryError(f"{label}格式无效")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise InteractionStoryError(f"{label}格式无效") from exc
    if not math.isfinite(result) or not minimum <= result <= maximum:
        raise InteractionStoryError(f"{label}超出范围")
    return result


def _text(value: Any, maximum: int, fallback: str = "") -> str:
    result = str(value or "").strip()
    return (result or fallback)[:maximum]


def _allowed_ranges(parent_plan: dict[str, Any]) -> list[dict[str, float]]:
    rows = []
    for row in parent_plan.get("keep_ranges") or []:
        try:
            start, end = float(row["start"]), float(row["end"])
        except (KeyError, TypeError, ValueError) as exc:
            raise InteractionStoryError("父候选保留范围无效") from exc
        if not math.isfinite(start) or not math.isfinite(end) or end <= start:
            raise InteractionStoryError("父候选保留范围无效")
        if rows and start < rows[-1]["end"]:
            raise InteractionStoryError("父候选保留范围重叠或倒序")
        rows.append({"start": round(start, 3), "end": round(end, 3)})
    if not rows:
        raise InteractionStoryError("父候选没有可用内容")
    return rows


def _containing_range(start: float, end: float, allowed: list[dict[str, float]]) -> dict[str, float] | None:
    return next((row for row in allowed if start >= row["start"] and end <= row["end"]), None)


def story_utterances(index: dict[str, Any], parent_plan: dict[str, Any]) -> list[dict[str, Any]]:
    allowed = _allowed_ranges(parent_plan)
    result = []
    seen: set[str] = set()
    for raw in (index.get("audio") or {}).get("utterances") or []:
        if not isinstance(raw, dict):
            continue
        utterance_id = _text(raw.get("id"), 80)
        try:
            start, end = float(raw["start"]), float(raw["end"])
        except (KeyError, TypeError, ValueError):
            continue
        text = _text(raw.get("text"), 1500)
        if not utterance_id or utterance_id in seen or not text or end <= start:
            continue
        if _containing_range(start, end, allowed) is None:
            continue
        seen.add(utterance_id)
        result.append({"id": utterance_id, "start": round(start, 3), "end": round(end, 3), "text": text})
    return sorted(result, key=lambda row: (row["start"], row["end"], row["id"]))


def _visual_evidence(index: dict[str, Any], parent_plan: dict[str, Any]) -> dict[str, Any]:
    """Expose bounded, path-free evidence from the completed vision pass."""
    parent_group = _text(parent_plan.get("group_id"), 120)
    parent_event = _text(parent_plan.get("event_id"), 120)
    matched = []
    for raw in index.get("events") or []:
        if not isinstance(raw, dict):
            continue
        if parent_group and _text(raw.get("group_id"), 120) == parent_group:
            matched.append(raw)
        elif parent_event and _text(raw.get("event_id"), 120) == parent_event:
            matched.append(raw)
    matched = matched[:MAX_VISUAL_EVENTS]
    evidence_frame_ids: list[str] = []
    events = []
    for raw in matched:
        frame_ids = [
            _text(value, 120) for value in raw.get("evidence_frame_ids") or []
            if _text(value, 120)
        ][:MAX_VISUAL_FRAMES]
        evidence_frame_ids.extend(frame_ids)
        highlights = []
        for item in (raw.get("highlights") or [])[:MAX_VISUAL_FRAMES]:
            if isinstance(item, dict):
                highlights.append({
                    "label": _text(item.get("label"), 160),
                    "frame_id": _text(item.get("frame_id"), 120),
                    "time": item.get("time") if isinstance(item.get("time"), (int, float)) else None,
                })
                if highlights[-1]["frame_id"]:
                    evidence_frame_ids.append(highlights[-1]["frame_id"])
            elif item:
                highlights.append({"label": _text(item, 240), "frame_id": "", "time": None})
        events.append({
            "event_id": _text(raw.get("event_id"), 120),
            "group_id": _text(raw.get("group_id"), 120),
            "participants": _text(raw.get("participants"), 400),
            "summary": _text(raw.get("summary"), 800),
            "recommend_reason": _text(raw.get("recommend_reason"), 600),
            "evidence_frame_ids": frame_ids,
            "highlights": highlights,
            "unknowns": [_text(value, 240) for value in (raw.get("unknowns") or [])[:8] if value],
        })
    unique_frame_ids = list(dict.fromkeys(value for value in evidence_frame_ids if value))[:MAX_VISUAL_FRAMES]
    frame_lookup = {
        _text(row.get("id"), 120): row for row in index.get("frames") or []
        if isinstance(row, dict) and _text(row.get("id"), 120)
    }
    frames = []
    for frame_id in unique_frame_ids:
        row = frame_lookup.get(frame_id) or {}
        frames.append({
            "id": frame_id,
            "pts": row.get("pts") if isinstance(row.get("pts"), (int, float)) else None,
        })
    pause_windows = []
    refinement = parent_plan.get("refinement") if isinstance(parent_plan.get("refinement"), dict) else {}
    for raw in (refinement.get("annotations") or [])[:MAX_PAUSE_WINDOWS]:
        if not isinstance(raw, dict):
            continue
        pause_windows.append({
            "id": _text(raw.get("id"), 120),
            "left_utterance_id": _text(raw.get("left_utterance_id"), 120),
            "right_utterance_id": _text(raw.get("right_utterance_id"), 120),
            "stage": _text(raw.get("stage"), 80),
            "safe_to_shorten": raw.get("safe_to_shorten") is True,
            "reason": _text(raw.get("reason"), 300),
        })
    return {"events": events, "frames": frames, "pause_windows": pause_windows}


def build_story_context(index: dict[str, Any], parent_plan: dict[str, Any], options: dict[str, Any]) -> dict[str, Any]:
    utterances = story_utterances(index, parent_plan)
    if not utterances:
        raise InteractionStoryError("所选互动没有可用的带时间戳转写，无法自动提取对白精华")
    context = {
        "version": PROMPT_VERSION,
        "task": "outdoor_interaction_second_pass",
        "group_id": parent_plan.get("group_id"),
        "allowed_source_ranges": _allowed_ranges(parent_plan),
        "options": deepcopy(options),
        "utterances": deepcopy(utterances),
        "visual_evidence": _visual_evidence(index, parent_plan),
        "contract": {
            "cover_every_utterance_once": True,
            "groups_must_be_contiguous": True,
            "body_order": "source_chronological",
            "hook_target_seconds": [3, 5],
            "hook_candidates_maximum": MAX_HOOKS,
            "timestamps_are_not_model_owned": True,
        },
    }
    context["signature"] = _digest(context)
    return context


def _group_ranges(rows: list[dict[str, Any]], allowed: list[dict[str, float]]) -> list[dict[str, float]]:
    """Clip a semantic group to every parent-approved occurrence range.

    A first-pass candidate may already have removed a safe pause between two
    consecutive utterances.  Treating the group as one envelope would either
    resurrect that deleted media or reject an otherwise valid semantic group.
    """
    start = max(0.0, float(rows[0]["start"]) - EDGE_GUARD_SECONDS)
    end = float(rows[-1]["end"]) + EDGE_GUARD_SECONDS
    ranges = [
        {"start": round(max(start, row["start"]), 3), "end": round(min(end, row["end"]), 3)}
        for row in allowed
        if max(start, row["start"]) < min(end, row["end"])
    ]
    if not ranges or any(_containing_range(float(row["start"]), float(row["end"]), allowed) is None for row in rows):
        raise InteractionStoryError("一个对话组包含父候选不允许使用的分句")
    return ranges


def _range_envelope(ranges: list[dict[str, float]]) -> dict[str, float]:
    return {"start": ranges[0]["start"], "end": ranges[-1]["end"]}


def _ensure_acyclic(groups: list[dict[str, Any]]) -> None:
    graph = {row["id"]: list(row.get("depends_on") or []) for row in groups}
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(group_id: str) -> None:
        if group_id in visiting:
            raise InteractionStoryError("对话组依赖形成循环")
        if group_id in visited:
            return
        visiting.add(group_id)
        for dependency in graph[group_id]:
            visit(dependency)
        visiting.remove(group_id)
        visited.add(group_id)

    for group_id in graph:
        visit(group_id)


def _dependency_closure(groups: list[dict[str, Any]]) -> None:
    by_id = {row["id"]: row for row in groups}
    changed = True
    while changed:
        changed = False
        for row in groups:
            if not row["selected"]:
                continue
            for dependency in row.get("depends_on") or []:
                if not by_id[dependency]["selected"]:
                    by_id[dependency]["selected"] = True
                    by_id[dependency]["decision"] = "keep"
                    by_id[dependency]["reason"] = "作为已保留内容的必要前提，已保守保留"
                    changed = True


def normalize_story_analysis(raw: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise InteractionStoryError("内容精选结果不是对象")
    utterances = context.get("utterances") or []
    if not utterances:
        raise InteractionStoryError("内容精选上下文缺少转写")
    by_utterance = {row["id"]: row for row in utterances}
    order = {row["id"]: index for index, row in enumerate(utterances)}
    allowed = context.get("allowed_source_ranges") or []
    raw_groups = raw.get("groups")
    if not isinstance(raw_groups, list) or not raw_groups or len(raw_groups) > MAX_GROUPS:
        raise InteractionStoryError("内容精选没有有效的对话分组")

    groups: list[dict[str, Any]] = []
    used_utterances: set[str] = set()
    used_groups: set[str] = set()
    warnings: list[str] = []
    visual = context.get("visual_evidence") if isinstance(context.get("visual_evidence"), dict) else {}
    valid_evidence_ids = {
        _text(row.get("id"), 120)
        for row in (visual.get("frames") or []) + (visual.get("pause_windows") or [])
        if isinstance(row, dict) and _text(row.get("id"), 120)
    }
    for sequence, item in enumerate(raw_groups, 1):
        if not isinstance(item, dict):
            raise InteractionStoryError("对话分组结构无效")
        group_id = _text(item.get("id"), 80, f"G{sequence:03d}")
        if not group_id or group_id in used_groups or not group_id.replace("-", "").replace("_", "").isalnum():
            raise InteractionStoryError("对话组编号重复或格式无效")
        utterance_ids = list(dict.fromkeys(_text(value, 80) for value in item.get("utterance_ids") or []))
        if not utterance_ids or any(value not in by_utterance for value in utterance_ids):
            raise InteractionStoryError("对话组引用了不存在的分句")
        if any(value in used_utterances for value in utterance_ids):
            raise InteractionStoryError("同一分句不能属于多个对话组")
        positions = [order[value] for value in utterance_ids]
        if positions != sorted(positions) or positions != list(range(min(positions), max(positions) + 1)):
            raise InteractionStoryError("每个对话组必须由连续、原序分句组成")
        used_utterances.update(utterance_ids)
        used_groups.add(group_id)
        rows = [by_utterance[value] for value in utterance_ids]
        decision = _text(item.get("decision"), 20, "keep")
        if decision not in DECISIONS:
            decision = "keep"
        group_type = _text(item.get("type"), 40, "other")
        if group_type not in GROUP_TYPES:
            group_type = "other"
        score = _number(item.get("hook_score", 0), minimum=0, maximum=1, label="开场评分")
        evidence_ids = list(dict.fromkeys(_text(value, 120) for value in item.get("evidence_ids") or []))
        if any(value not in valid_evidence_ids for value in evidence_ids):
            raise InteractionStoryError("对话组引用了不存在的画面证据")
        source_ranges = _group_ranges(rows, allowed)
        groups.append({
            "id": group_id,
            "sequence": sequence,
            "type": group_type,
            "utterance_ids": utterance_ids,
            "summary": _text(item.get("summary"), 240, " / ".join(row["text"] for row in rows)[:240]),
            "decision": decision,
            "selected": decision == "keep",
            "locked": False,
            "reason": _text(item.get("reason"), 300, "模型未提供理由，已保守保留"),
            "depends_on": list(dict.fromkeys(_text(value, 80) for value in item.get("depends_on") or [])),
            "hook_eligible": item.get("hook_eligible") is True,
            "hook_score": round(score, 4),
            "evidence_ids": evidence_ids,
            "source_ranges": source_ranges,
            "source_range": _range_envelope(source_ranges),
        })

    missing = [row for row in utterances if row["id"] not in used_utterances]
    if missing:
        warnings.append(f"模型遗漏 {len(missing)} 条分句，系统已逐条保守保留")
    for row in missing:
        group_id = f"G-AUTO-{order[row['id']]+1:03d}"
        source_ranges = _group_ranges([row], allowed)
        groups.append({
            "id": group_id, "sequence": order[row["id"]] + 1, "type": "other",
            "utterance_ids": [row["id"]], "summary": row["text"], "decision": "keep",
            "selected": True, "locked": False, "reason": "模型未分组，系统保守保留",
            "depends_on": [], "hook_eligible": False, "hook_score": 0.0,
            "evidence_ids": [],
            "source_ranges": source_ranges,
            "source_range": _range_envelope(source_ranges),
        })
    groups.sort(key=lambda row: min(order[value] for value in row["utterance_ids"]))
    for sequence, row in enumerate(groups, 1):
        row["sequence"] = sequence

    group_ids = {row["id"] for row in groups}
    for row in groups:
        dependencies = row["depends_on"]
        if any(value not in group_ids or value == row["id"] for value in dependencies):
            raise InteractionStoryError("对话组依赖引用无效")
    _ensure_acyclic(groups)

    options = context.get("options") or {}
    if options.get("extract_highlights") is False:
        for row in groups:
            row["selected"], row["decision"] = True, "keep"
            row["reason"] = "用户未启用提取精华，保留完整对话组"
    if options.get("trim_head") is not False:
        for row in groups:
            if row["type"] != "greeting":
                break
            row["selected"], row["decision"] = False, "drop"
            row["reason"] = "用户启用掐头，已删除开场独立寒暄"
    if options.get("trim_tail") is not False:
        for row in reversed(groups):
            if row["type"] != "farewell":
                break
            row["selected"], row["decision"] = False, "drop"
            row["reason"] = "用户启用去尾，已删除结尾独立告别"
    if options.get("trim_head") is False:
        groups[0]["selected"], groups[0]["decision"] = True, "keep"
    if options.get("trim_tail") is False:
        groups[-1]["selected"], groups[-1]["decision"] = True, "keep"
    _dependency_closure(groups)
    if not any(row["selected"] for row in groups):
        raise InteractionStoryError("内容精选删除了全部对话，已阻止生成")

    raw_hooks = raw.get("hook_candidates") if isinstance(raw.get("hook_candidates"), list) else []
    hooks = []
    by_group = {row["id"]: row for row in groups}
    if options.get("hook_enabled") is not False:
        for sequence, item in enumerate(raw_hooks[:MAX_HOOKS], 1):
            if not isinstance(item, dict):
                continue
            group_ids_for_hook = list(dict.fromkeys(_text(value, 80) for value in item.get("group_ids") or []))
            if not group_ids_for_hook or any(value not in by_group or not by_group[value]["selected"] for value in group_ids_for_hook):
                continue
            selected_groups = sorted((by_group[value] for value in group_ids_for_hook), key=lambda row: row["sequence"])
            if [row["sequence"] for row in selected_groups] != list(range(selected_groups[0]["sequence"], selected_groups[-1]["sequence"] + 1)):
                continue
            ranges = []
            for group in selected_groups:
                for source_range in group.get("source_ranges") or [group["source_range"]]:
                    if ranges and float(source_range["start"]) <= float(ranges[-1]["end"]):
                        ranges[-1]["end"] = max(float(ranges[-1]["end"]), float(source_range["end"]))
                    else:
                        ranges.append({"start": float(source_range["start"]), "end": float(source_range["end"])})
            start, end = ranges[0]["start"], ranges[-1]["end"]
            duration = sum(float(row["end"]) - float(row["start"]) for row in ranges)
            if not 2 <= duration <= 8:
                continue
            hooks.append({
                "id": _text(item.get("id"), 80, f"H{sequence:03d}"),
                "group_ids": [row["id"] for row in selected_groups],
                "source_range": {"start": start, "end": end},
                "source_ranges": ranges,
                "duration": round(duration, 3),
                "reason": _text(item.get("reason"), 300, "完整且可独立理解的互动亮点"),
                "score": round(sum(row["hook_score"] for row in selected_groups) / len(selected_groups), 4),
            })
        hooks.sort(key=lambda row: (-row["score"], abs(row["duration"] - 4), row["id"]))
    if options.get("hook_enabled") is not False and not hooks:
        warnings.append("未找到完整、独立且长度合适的3—5秒开场，已保留正常正文")

    result = {
        "version": VERSION,
        "context_signature": context.get("signature"),
        "groups": groups,
        "hook_candidates": hooks,
        "recommended_hook_id": hooks[0]["id"] if hooks else None,
        "warnings": warnings,
        "summary": _text(raw.get("summary"), 600, "已按完整对话与动作单元形成二次剪辑建议"),
    }
    result["signature"] = _digest(result)
    return result


def analyze_story(
    index: dict[str, Any],
    parent_plan: dict[str, Any],
    options: dict[str, Any],
    *,
    analyze: Callable[[dict[str, Any]], tuple[dict[str, Any], str]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    context = build_story_context(index, parent_plan, options)
    raw, model = analyze(deepcopy(context))
    normalized = normalize_story_analysis(raw, context)
    return normalized, {
        "provider": "default",
        "model": _text(model, 160),
        "prompt_version": PROMPT_VERSION,
        "context_signature": context["signature"],
        "analysis_signature": normalized["signature"],
    }
