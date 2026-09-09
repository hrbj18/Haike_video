"""Versioned second-pass plans for hook-led outdoor interaction edits."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
import time
from typing import Any


VERSION = "interaction-second-pass-plan-v2"
LEGACY_VERSION = "interaction-second-pass-plan-v1"
SUPPORTED_VERSIONS = {LEGACY_VERSION, VERSION}
PLAN_FILENAME = "interaction-second-pass-plan.json"
ALLOWED_SPEEDS = (1.0, 1.1, 1.25)
ALLOWED_STATUSES = {"pending_review", "approved", "rejected"}
ALLOWED_HOOK_MODES = {"move", "repeat", "none"}


class InteractionSecondPassError(ValueError):
    pass


class InteractionSecondPassConflict(InteractionSecondPassError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _number(value: Any, *, minimum: float, maximum: float, label: str) -> float:
    if isinstance(value, bool):
        raise InteractionSecondPassError(f"{label}格式无效")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise InteractionSecondPassError(f"{label}格式无效") from exc
    if not math.isfinite(result) or not minimum <= result <= maximum:
        raise InteractionSecondPassError(f"{label}超出范围")
    return result


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
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


def normalize_options(raw: dict[str, Any] | None, *, legacy: bool = False) -> dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    requested_speed = _number(raw.get("speed", 1.1), minimum=1.0, maximum=1.25, label="播放速度")
    speed = requested_speed
    speed = min(ALLOWED_SPEEDS, key=lambda candidate: abs(candidate - speed))
    if abs(requested_speed - speed) > .001:
        raise InteractionSecondPassError("播放速度只支持 1.0、1.1 或 1.25 倍")
    minimum = _number(raw.get("target_min_seconds", 45), minimum=15, maximum=180, label="目标最短时长")
    maximum = _number(raw.get("target_max_seconds", 60), minimum=15, maximum=180, label="目标最长时长")
    if maximum < minimum:
        raise InteractionSecondPassError("目标最长时长不能小于最短时长")
    hook_enabled = raw.get("hook_enabled") is not False
    default_hook_mode = "repeat" if legacy else "move"
    hook_mode = str(raw.get("hook_mode") or default_hook_mode)
    if hook_mode not in ALLOWED_HOOK_MODES:
        raise InteractionSecondPassError("精彩前置方式无效")
    if not hook_enabled:
        hook_mode = "none"
    return {
        "preset": "outdoor_interaction_fine_cut",
        "trim_head": raw.get("trim_head") is not False,
        "trim_tail": raw.get("trim_tail") is not False,
        "extract_highlights": raw.get("extract_highlights") is not False,
        "hook_enabled": hook_mode != "none",
        "hook_mode": hook_mode,
        "speed": speed,
        "target_min_seconds": round(minimum, 3),
        "target_max_seconds": round(maximum, 3),
    }


def _range_within_allowed(start: float, end: float, allowed: list[dict[str, Any]]) -> bool:
    return any(start >= float(row["start"]) and end <= float(row["end"]) for row in allowed)


def _selected_groups(plan: dict[str, Any]) -> list[dict[str, Any]]:
    return [row for row in (plan.get("story") or {}).get("groups") or [] if row.get("selected") is True]


def _validate_dependencies(groups: list[dict[str, Any]]) -> None:
    by_id = {str(row.get("id")): row for row in groups}
    selected = {group_id for group_id, row in by_id.items() if row.get("selected") is True}
    for row in groups:
        for dependency in row.get("depends_on") or []:
            if dependency not in by_id:
                raise InteractionSecondPassError("对话组依赖引用无效")
            if row.get("selected") is True and dependency not in selected:
                raise InteractionSecondPassError(
                    f"“{row.get('summary') or row.get('id')}”依赖前文“{by_id[dependency].get('summary') or dependency}”，请一并保留"
                )


def _body_occurrences(groups: list[dict[str, Any]], speed: float, *, excluded_group_ids: set[str] | None = None) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    excluded_group_ids = excluded_group_ids or set()
    for row in sorted(
        (item for item in groups if item.get("selected") is True and str(item.get("id")) not in excluded_group_ids),
        key=lambda item: int(item.get("sequence") or 0),
    ):
        ranges = row.get("source_ranges") or [row.get("source_range") or {}]
        for source_range in ranges:
            start, end = float(source_range["start"]), float(source_range["end"])
            if result and start <= result[-1]["source_end"]:
                result[-1]["source_end"] = round(max(result[-1]["source_end"], end), 3)
                if str(row["id"]) not in result[-1]["group_ids"]:
                    result[-1]["group_ids"].append(str(row["id"]))
                continue
            result.append({
                "occurrence_id": f"O-BODY-{len(result)+1:03d}",
                "role": "body", "group_ids": [str(row["id"])],
                "source_start": round(start, 3), "source_end": round(end, 3), "speed": speed,
            })
    return result


def build_occurrences(plan: dict[str, Any]) -> list[dict[str, Any]]:
    story = plan.get("story") or {}
    groups = story.get("groups") or []
    _validate_dependencies(groups)
    speed = float((plan.get("options") or {}).get("speed") or 1.0)
    legacy = plan.get("version") == LEGACY_VERSION
    options = normalize_options(plan.get("options"), legacy=legacy)
    hook_mode = options["hook_mode"]
    result = []
    moved_group_ids: set[str] = set()
    hook_id = plan.get("selected_hook_id")
    if hook_id and hook_mode != "none":
        hook = next((row for row in story.get("hook_candidates") or [] if row.get("id") == hook_id), None)
        if not hook:
            raise InteractionSecondPassError("选中的精彩开场不存在")
        by_id = {row["id"]: row for row in groups}
        if any(group_id not in by_id or by_id[group_id].get("selected") is not True for group_id in hook.get("group_ids") or []):
            raise InteractionSecondPassError("精彩开场引用了已经删除的对话组")
        hook_group_ids = {str(group_id) for group_id in hook.get("group_ids") or []}
        selected_group_ids = {str(row.get("id")) for row in groups if row.get("selected") is True}
        # A one-group story is already front-loaded.  Emitting a separate hook
        # would either duplicate the only content or leave the plan body-less.
        should_frontload = hook_mode == "repeat" or bool(selected_group_ids - hook_group_ids)
        if should_frontload:
            if hook_mode == "move":
                moved_group_ids.update(hook_group_ids)
            hook_ranges = hook.get("source_ranges") or [hook.get("source_range") or {}]
            for sequence, source_range in enumerate(hook_ranges, 1):
                result.append({
                    "occurrence_id": f"O-HOOK-{sequence:03d}", "role": "hook",
                    "group_ids": list(hook["group_ids"]),
                    "source_start": float(source_range["start"]),
                    "source_end": float(source_range["end"]), "speed": speed,
                })
    result.extend(_body_occurrences(groups, speed, excluded_group_ids=moved_group_ids))
    if not any(row["role"] == "body" for row in result):
        raise InteractionSecondPassError("二次剪辑没有可用正文")
    return result


def timeline_mapping(occurrences: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cursor = 0.0
    result = []
    for occurrence in occurrences:
        start, end = float(occurrence["source_start"]), float(occurrence["source_end"])
        speed = float(occurrence["speed"])
        duration = (end - start) / speed
        result.append({
            "occurrence_id": occurrence["occurrence_id"], "role": occurrence["role"],
            "group_ids": list(occurrence.get("group_ids") or []),
            "source_start": round(start, 6), "source_end": round(end, 6),
            "output_start": round(cursor, 6), "output_end": round(cursor + duration, 6),
            "speed": round(speed, 6),
        })
        cursor += duration
    return result


def subtitle_cues(plan: dict[str, Any], mapping: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_group = {row["id"]: row for row in (plan.get("story") or {}).get("groups") or []}
    by_utterance = {row["id"]: row for row in plan.get("utterances") or []}
    result = []
    for occurrence in mapping:
        utterance_ids = []
        for group_id in occurrence.get("group_ids") or []:
            utterance_ids.extend((by_group.get(group_id) or {}).get("utterance_ids") or [])
        for utterance_id in dict.fromkeys(utterance_ids):
            utterance = by_utterance.get(utterance_id)
            if not utterance:
                continue
            source_start = max(float(utterance["start"]), float(occurrence["source_start"]))
            source_end = min(float(utterance["end"]), float(occurrence["source_end"]))
            if source_end <= source_start:
                continue
            output_start = float(occurrence["output_start"]) + (source_start - float(occurrence["source_start"])) / float(occurrence["speed"])
            output_end = float(occurrence["output_start"]) + (source_end - float(occurrence["source_start"])) / float(occurrence["speed"])
            result.append({
                "cue_id": f"{occurrence['occurrence_id']}:{utterance_id}",
                "occurrence_id": occurrence["occurrence_id"], "role": occurrence["role"],
                "utterance_id": utterance_id, "text": str(utterance["text"]),
                "source_start": round(source_start, 6), "source_end": round(source_end, 6),
                "output_start": round(output_start, 6), "output_end": round(output_end, 6),
            })
    return result


def _unique_source_duration(occurrences: list[dict[str, Any]], *, role: str = "body") -> float:
    ranges = sorted((float(row["source_start"]), float(row["source_end"])) for row in occurrences if row["role"] == role)
    merged: list[list[float]] = []
    for start, end in ranges:
        if not merged or start > merged[-1][1]:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    return sum(end - start for start, end in merged)


def _cross_role_overlap(occurrences: list[dict[str, Any]]) -> float:
    hooks = [(float(row["source_start"]), float(row["source_end"])) for row in occurrences if row["role"] == "hook"]
    bodies = [(float(row["source_start"]), float(row["source_end"])) for row in occurrences if row["role"] == "body"]
    return sum(max(0.0, min(hook_end, body_end) - max(hook_start, body_start))
               for hook_start, hook_end in hooks for body_start, body_end in bodies)


def _content_qa(result: dict[str, Any]) -> dict[str, Any]:
    output_duration = float(result.get("output_duration") or 0)
    maximum = float((result.get("options") or {}).get("target_max_seconds") or 24 * 3600)
    target_ok = output_duration <= maximum
    hook_mode = str((result.get("options") or {}).get("hook_mode") or "repeat")
    repeated = float(result.get("repeated_source_seconds") or 0)
    repetition_ok = repeated <= .001 or hook_mode == "repeat"
    checks = [
        {"name": "target_duration", "ok": target_ok,
         "detail": "输出未超过目标最长时长" if target_ok else "输出超过目标最长时长，请调整语义组、倍速或目标范围"},
        {"name": "source_repetition", "ok": repetition_ok,
         "detail": ("未发现非预期的源片段重复" if repetition_ok else f"发现 {repeated:.3f} 秒非预期源片段重复")},
    ]
    return {"status": "passed" if all(row["ok"] for row in checks) else "needs_adjustment", "checks": checks}


def _rebuild(plan: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(plan)
    occurrences = build_occurrences(result)
    allowed = result.get("allowed_source_ranges") or []
    ids: set[str] = set()
    previous_body_start = -1.0
    for row in occurrences:
        start = _number(row.get("source_start"), minimum=0, maximum=24 * 3600, label="片段开始")
        end = _number(row.get("source_end"), minimum=0, maximum=24 * 3600, label="片段结束")
        speed = _number(row.get("speed"), minimum=1.0, maximum=1.25, label="片段倍速")
        if end <= start or not _range_within_allowed(start, end, allowed):
            raise InteractionSecondPassError("播放片段越出父候选允许范围")
        if speed not in ALLOWED_SPEEDS:
            raise InteractionSecondPassError("播放片段倍速无效")
        if row["occurrence_id"] in ids:
            raise InteractionSecondPassError("播放片段出现编号重复")
        ids.add(row["occurrence_id"])
        if row["role"] == "body":
            if start < previous_body_start:
                raise InteractionSecondPassError("正文片段必须保持原片顺序")
            previous_body_start = start
    result["occurrences"] = occurrences
    result["timeline_mapping"] = timeline_mapping(occurrences)
    result["subtitle_cues"] = subtitle_cues(result, result["timeline_mapping"])
    result["body_source_duration"] = round(_unique_source_duration(occurrences), 3)
    result["hook_source_duration"] = round(sum(row["source_end"] - row["source_start"] for row in occurrences if row["role"] == "hook"), 3)
    result["repeated_source_seconds"] = round(_cross_role_overlap(occurrences), 3)
    result["output_duration"] = round(sum(row["output_end"] - row["output_start"] for row in result["timeline_mapping"]), 3)
    result["removed_source_seconds"] = round(max(0.0, float(result["source_duration"]) - result["body_source_duration"]), 3)
    minimum = float((result.get("options") or {}).get("target_min_seconds") or 0)
    maximum = float((result.get("options") or {}).get("target_max_seconds") or 24 * 3600)
    result["target_duration_status"] = "within_target" if minimum <= result["output_duration"] <= maximum else "outside_target"
    result["content_qa"] = _content_qa(result)
    return result


def build_second_pass_plan(
    *,
    parent_plan: dict[str, Any],
    story: dict[str, Any],
    utterances: list[dict[str, Any]],
    options: dict[str, Any],
    story_identity: dict[str, Any],
) -> dict[str, Any]:
    options = normalize_options(options)
    parent_id = str(parent_plan.get("plan_id") or "")
    source = deepcopy(parent_plan.get("source") or {})
    if not parent_id.startswith("IEP-") or not source.get("fingerprint"):
        raise InteractionSecondPassError("父候选合同不完整")
    allowed = deepcopy(parent_plan.get("keep_ranges") or [])
    frozen = {
        "version": VERSION,
        "parent": {
            "plan_id": parent_id, "version": parent_plan.get("version"),
            "revision": int(parent_plan.get("revision") or 0),
            "preview_signature": ((parent_plan.get("preview") or {}).get("signature") if isinstance(parent_plan.get("preview"), dict) else None),
        },
        "source": source,
        "index_signature": parent_plan.get("index_signature"),
        "review_revision": parent_plan.get("review_revision"),
        "event_id": parent_plan.get("event_id"), "group_id": parent_plan.get("group_id"),
        "allowed_source_ranges": allowed,
        "source_duration": round(sum(float(row["end"]) - float(row["start"]) for row in allowed), 3),
        "options": options,
        "story_identity": deepcopy(story_identity),
        "story": deepcopy(story),
        "utterances": deepcopy(utterances),
    }
    plan_id = "ISP-" + _digest(frozen)[:16]
    plan = {
        **frozen, "plan_id": plan_id, "revision": 0, "status": "pending_review",
        "selected_hook_id": story.get("recommended_hook_id") if options["hook_enabled"] else None,
        "warnings": list(story.get("warnings") or []), "history": [],
        "qa": {"status": "not_rendered"}, "preview": None,
        "created_at": _now(), "updated_at": _now(),
    }
    plan = _rebuild(plan)
    validate_second_pass_plan(plan)
    return plan


def validate_second_pass_plan(plan: dict[str, Any]) -> None:
    if plan.get("version") not in SUPPORTED_VERSIONS:
        raise InteractionSecondPassError("二次剪辑清单版本不受支持")
    plan_id = str(plan.get("plan_id") or "")
    if not plan_id.startswith("ISP-") or not plan_id[4:].isalnum():
        raise InteractionSecondPassError("二次剪辑编号无效")
    if plan.get("status") not in ALLOWED_STATUSES:
        raise InteractionSecondPassError("二次剪辑状态无效")
    legacy = plan.get("version") == LEGACY_VERSION
    normalize_options(plan.get("options"), legacy=legacy)
    allowed = plan.get("allowed_source_ranges")
    groups = (plan.get("story") or {}).get("groups")
    if not isinstance(allowed, list) or not allowed or not isinstance(groups, list) or not groups:
        raise InteractionSecondPassError("二次剪辑缺少父范围或语义组")
    group_ids = [str(row.get("id") or "") for row in groups]
    if any(not value for value in group_ids) or len(group_ids) != len(set(group_ids)):
        raise InteractionSecondPassError("二次剪辑语义组编号无效")
    for group in groups:
        ranges = group.get("source_ranges")
        if not isinstance(ranges, list) or not ranges:
            raise InteractionSecondPassError("二次剪辑语义组缺少父范围内的播放片段")
        previous_end = -1.0
        normalized_ranges = []
        for source_range in ranges:
            if not isinstance(source_range, dict):
                raise InteractionSecondPassError("二次剪辑语义组片段格式无效")
            start = _number(source_range.get("start"), minimum=0, maximum=24 * 3600, label="语义组开始")
            end = _number(source_range.get("end"), minimum=0, maximum=24 * 3600, label="语义组结束")
            if end <= start or start < previous_end or not _range_within_allowed(start, end, allowed):
                raise InteractionSecondPassError("二次剪辑语义组片段越出父候选范围或顺序无效")
            normalized_ranges.append({"start": round(start, 3), "end": round(end, 3)})
            previous_end = end
        expected_envelope = {
            "start": normalized_ranges[0]["start"],
            "end": normalized_ranges[-1]["end"],
        }
        if group.get("source_range") != expected_envelope:
            raise InteractionSecondPassError("二次剪辑语义组展示范围与播放片段不一致")
    _validate_dependencies(groups)
    derived_keys = {
        "occurrences", "timeline_mapping", "subtitle_cues", "body_source_duration", "hook_source_duration",
        "output_duration", "removed_source_seconds", "target_duration_status", "repeated_source_seconds", "content_qa",
    }
    expected = _rebuild({key: deepcopy(value) for key, value in plan.items() if key not in derived_keys})
    compare_keys = ("occurrences", "timeline_mapping", "subtitle_cues", "body_source_duration", "hook_source_duration",
                    "output_duration", "removed_source_seconds", "target_duration_status")
    if not legacy:
        compare_keys += ("repeated_source_seconds", "content_qa")
    for key in compare_keys:
        if plan.get(key) != expected.get(key):
            raise InteractionSecondPassError(f"二次剪辑{key}与语义选择不一致")
    if not isinstance(plan.get("history"), list) or len(plan["history"]) > 50:
        raise InteractionSecondPassError("二次剪辑撤销记录无效")


def plan_path(root: Path, plan_id: str) -> Path:
    if not str(plan_id).startswith("ISP-") or not str(plan_id)[4:].isalnum():
        raise InteractionSecondPassError("二次剪辑编号无效")
    return root.resolve() / str(plan_id) / PLAN_FILENAME


def write_second_pass_plan(path: Path, plan: dict[str, Any]) -> dict[str, Any]:
    validate_second_pass_plan(plan)
    _atomic_write(path, plan)
    return plan


def read_second_pass_plan(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise InteractionSecondPassError("二次剪辑方案不存在")
    try:
        plan = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InteractionSecondPassError("二次剪辑方案损坏") from exc
    validate_second_pass_plan(plan)
    return plan


def list_second_pass_plans(root: Path) -> list[dict[str, Any]]:
    if not root.is_dir():
        return []
    result = []
    for path in root.glob(f"ISP-*/{PLAN_FILENAME}"):
        try:
            result.append(read_second_pass_plan(path))
        except InteractionSecondPassError:
            continue
    return sorted(result, key=lambda row: str(row.get("updated_at") or ""), reverse=True)


def _snapshot(plan: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "story", "selected_hook_id", "options", "warnings", "occurrences", "timeline_mapping",
        "subtitle_cues", "body_source_duration", "hook_source_duration", "output_duration",
        "removed_source_seconds", "target_duration_status", "repeated_source_seconds", "content_qa", "status", "qa", "preview",
    )
    return {key: deepcopy(plan.get(key)) for key in keys}


def apply_second_pass_action(
    plan: dict[str, Any],
    *,
    action: str,
    expected_revision: int,
    group_states: dict[str, bool] | None = None,
    locked_states: dict[str, bool] | None = None,
    speed: float | None = None,
    hook_candidate_id: str | None = None,
    hook_mode: str | None = None,
) -> dict[str, Any]:
    validate_second_pass_plan(plan)
    if int(plan.get("revision", -1)) != int(expected_revision):
        raise InteractionSecondPassConflict("二次剪辑方案已被其他操作更新，请刷新后重试")
    if plan.get("status") in {"approved", "rejected"}:
        raise InteractionSecondPassError("已确认或弃用的二次剪辑不可继续改写")
    result = deepcopy(plan)
    if action == "undo":
        history = result.get("history") or []
        if not history:
            raise InteractionSecondPassError("没有可以撤销的二次剪辑操作")
        snapshot = history.pop()
        for key, value in snapshot.items():
            result[key] = deepcopy(value)
        result["history"] = history
    elif action == "save_edits":
        if not any((isinstance(group_states, dict), isinstance(locked_states, dict), speed is not None,
                    hook_candidate_id is not None, hook_mode is not None)):
            raise InteractionSecondPassError("没有需要保存的二次剪辑调整")
        result.setdefault("history", []).append(_snapshot(result))
        result["history"] = result["history"][-50:]
        groups = (result.get("story") or {}).get("groups") or []
        by_id = {str(row["id"]): row for row in groups}
        group_states = group_states if isinstance(group_states, dict) else {}
        locked_states = locked_states if isinstance(locked_states, dict) else {}
        if any(str(key) not in by_id or not isinstance(value, bool) for key, value in {**group_states, **locked_states}.items()):
            raise InteractionSecondPassError("二次剪辑调整包含未知对话组")
        future_locks = {group_id: bool(locked_states.get(group_id, row.get("locked"))) for group_id, row in by_id.items()}
        for group_id, selected in group_states.items():
            row = by_id[str(group_id)]
            if row.get("locked") is True and future_locks[str(group_id)] and row.get("selected") is not selected:
                raise InteractionSecondPassError(f"对话组“{row.get('summary') or group_id}”已锁定，请先解锁")
            row["selected"] = selected
            row["decision"] = "keep" if selected else "drop"
            row["reason"] = "用户在二次剪辑审核中选择保留" if selected else "用户在二次剪辑审核中选择删除"
        for group_id, locked in locked_states.items():
            by_id[str(group_id)]["locked"] = locked
        _validate_dependencies(groups)
        if speed is not None:
            result["options"]["speed"] = normalize_options({**result["options"], "speed": speed})["speed"]
        if hook_mode is not None:
            result["options"] = normalize_options({**result["options"], "hook_mode": hook_mode})
            if result["options"]["hook_mode"] == "none":
                result["selected_hook_id"] = None
        if hook_candidate_id is not None:
            normalized_hook = None if hook_candidate_id in {"", "none", "__none__"} else str(hook_candidate_id)
            if normalized_hook and not any(row.get("id") == normalized_hook for row in (result.get("story") or {}).get("hook_candidates") or []):
                raise InteractionSecondPassError("选中的精彩开场不存在")
            result["selected_hook_id"] = normalized_hook
        current_hook = next((row for row in (result.get("story") or {}).get("hook_candidates") or [] if row.get("id") == result.get("selected_hook_id")), None)
        if current_hook and any(by_id[group_id].get("selected") is not True for group_id in current_hook.get("group_ids") or []):
            result["selected_hook_id"] = None
            result.setdefault("warnings", []).append("当前精彩开场包含已删除的对话组，已关闭精彩前置")
        result = _rebuild(result)
        result["status"] = "pending_review"
        result["qa"] = {"status": "stale"}
        if isinstance(result.get("preview"), dict):
            result["preview"] = {**result["preview"], "stale": True}
    elif action in {"approve", "reject"}:
        legacy = result.get("version") == LEGACY_VERSION
        if action == "approve" and (result.get("qa") or {}).get("status") != "passed":
            raise InteractionSecondPassError("二次剪辑预览尚未通过媒体 QA，不能确认入库")
        if action == "approve" and not legacy and (result.get("content_qa") or {}).get("status") != "passed":
            raise InteractionSecondPassError("二次剪辑仍超出目标或存在非预期重复，请调整后再确认入库")
        result.setdefault("history", []).append(_snapshot(result))
        result["history"] = result["history"][-50:]
        result["status"] = "approved" if action == "approve" else "rejected"
    else:
        raise InteractionSecondPassError("不支持的二次剪辑操作")
    result["revision"] = int(result["revision"]) + 1
    result["updated_at"] = _now()
    validate_second_pass_plan(result)
    return result


def attach_render_result(
    plan: dict[str, Any], manifest: dict[str, Any], *, expected_revision: int, preview_path: str,
) -> dict[str, Any]:
    validate_second_pass_plan(plan)
    if int(plan.get("revision", -1)) != int(expected_revision):
        raise InteractionSecondPassConflict("二次剪辑方案已变化，旧预览不会覆盖新方案")
    if manifest.get("plan_id") != plan.get("plan_id") or int(manifest.get("plan_revision", -1)) != int(plan["revision"]):
        raise InteractionSecondPassError("二次剪辑预览不属于当前方案")
    if (manifest.get("qa") or {}).get("status") != "passed" or not manifest.get("signature"):
        raise InteractionSecondPassError("二次剪辑预览尚未通过 QA")
    result = deepcopy(plan)
    result["qa"] = deepcopy(manifest["qa"])
    result["preview"] = {
        "path": str(preview_path), "signature": str(manifest["signature"]),
        "output_duration": manifest.get("output_duration"), "stale": False,
    }
    result["status"] = "pending_review"
    result["revision"] = int(result["revision"]) + 1
    result["updated_at"] = _now()
    validate_second_pass_plan(result)
    return result


def source_time_to_outputs(plan: dict[str, Any], seconds: float) -> list[dict[str, Any]]:
    value = float(seconds)
    return [
        {
            "occurrence_id": row["occurrence_id"], "role": row["role"],
            "output_seconds": round(float(row["output_start"]) + (value - float(row["source_start"])) / float(row["speed"]), 6),
        }
        for row in plan.get("timeline_mapping") or []
        if float(row["source_start"]) <= value <= float(row["source_end"])
    ]


def output_time_to_source(plan: dict[str, Any], seconds: float) -> dict[str, Any] | None:
    value = float(seconds)
    for row in plan.get("timeline_mapping") or []:
        if float(row["output_start"]) <= value <= float(row["output_end"]):
            return {
                "occurrence_id": row["occurrence_id"], "role": row["role"],
                "source_seconds": round(float(row["source_start"]) + (value - float(row["output_start"])) * float(row["speed"]), 6),
            }
    return None
