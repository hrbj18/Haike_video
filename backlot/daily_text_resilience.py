"""Bounded recovery for daily selection and script generation.

This module orchestrates text-only recovery.  It never creates projects and
never calls Voicebox, RunningHub, or media workers.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from backlot.daily_automation import RUNS_ROOT, _atomic_json, _now, _read_json
from backlot.daily_script_v2 import DailyScriptV2ValidationError, generate_script_v2


LEDGER_VERSION = "daily-text-resilience-v2"
LEDGER_FILENAME = "daily_text_attempts.json"
DEFAULT_POLICY = {
    "max_episode_combinations": 3,
    "max_editorial_reviews": 2,
    "max_text_attempts": 3,
    "max_rescue_research_rounds": 1,
    "max_wall_clock_seconds": 720,
}

ProgressCallback = Callable[[str, str, dict[str, Any]], None]
ScriptGenerator = Callable[..., dict[str, Any]]


def _clean(value: Any) -> str:
    return " ".join(str(value or "").split())


def _fingerprint(selection: dict[str, Any], research: dict[str, Any]) -> str:
    payload = {
        "selection_version": selection.get("version"),
        "prompt_version": selection.get("prompt_version"),
        "target_date": selection.get("target_date"),
        "combination_ids": [
            item.get("combination_id")
            for item in selection.get("episode_combinations") or []
            if isinstance(item, dict)
        ],
        "selected_event_ids": [
            item.get("event_id")
            for item in selection.get("selected_stories") or []
            if isinstance(item, dict)
        ],
        "manual_recovery_revision": selection.get("manual_recovery_revision", 0),
        "locked_event_ids": ((selection.get("manual_preferences") or {}).get("locked_event_ids") or []),
        "candidate_ids": [
            item.get("candidate_id")
            for item in research.get("candidates") or []
            if isinstance(item, dict)
        ],
    }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _run_dir(selection: dict[str, Any], explicit: Path | None) -> Path:
    if explicit is not None:
        return explicit
    target = _clean(selection.get("target_date"))
    return RUNS_ROOT / target


def _policy(value: dict[str, Any] | None) -> dict[str, int]:
    merged = dict(DEFAULT_POLICY)
    for key in merged:
        if isinstance((value or {}).get(key), (int, float)):
            merged[key] = int((value or {})[key])
    merged["max_episode_combinations"] = min(3, max(1, merged["max_episode_combinations"]))
    merged["max_editorial_reviews"] = min(2, max(1, merged["max_editorial_reviews"]))
    merged["max_text_attempts"] = min(4, max(1, merged["max_text_attempts"]))
    merged["max_rescue_research_rounds"] = min(1, max(0, merged["max_rescue_research_rounds"]))
    merged["max_wall_clock_seconds"] = min(1800, max(60, merged["max_wall_clock_seconds"]))
    return merged


def _new_ledger(selection: dict[str, Any], research: dict[str, Any], policy: dict[str, int]) -> dict[str, Any]:
    return {
        "version": LEDGER_VERSION,
        "target_date": _clean(selection.get("target_date")),
        "input_fingerprint": _fingerprint(selection, research),
        "created_at": _now(),
        "updated_at": _now(),
        "policy": policy,
        "attempts": [],
        "events": [],
        "unique_attempt_count": 0,
        "duplicate_attempts_suppressed": 0,
        "force_fresh_combination_ids": [],
        "best_candidate": {},
        "recovery_state": "drafting",
        "safe_resume_point": "initial_combination",
        "editorial_reviews_used": 0,
        "rescue_research_rounds_used": 0,
        "next_action": "generate",
        "next_combination_id": "",
    }


def _persist(path: Path, ledger: dict[str, Any]) -> None:
    ledger["updated_at"] = _now()
    _atomic_json(path, ledger)


def _notify(callback: ProgressCallback | None, state: str, message: str, ledger: dict[str, Any]) -> None:
    ledger["recovery_state"] = state
    if callback:
        callback(
            state,
            message,
            {
                "editorial_reviews_used": ledger.get("editorial_reviews_used", 0),
                "max_editorial_reviews": ledger["policy"]["max_editorial_reviews"],
                "attempt_count": len(ledger.get("attempts") or []),
                "best_score": ((ledger.get("best_candidate") or {}).get("editorial_score") or 0),
            },
        )


def _selection_for_combination(selection: dict[str, Any], combination: dict[str, Any]) -> dict[str, Any]:
    value = deepcopy(selection)
    stories = deepcopy(combination.get("selected_stories") or [])
    value["selected_stories"] = stories
    summary = value.setdefault("selection_summary", {})
    summary.update(
        {
            "selected_count": len(stories),
            "selected_combination_id": combination.get("combination_id"),
            "episode_score": combination.get("episode_score"),
            "duration_profile": combination.get("duration_profile"),
            "planned_units_total": sum(int(story.get("allocated_planning_units") or 0) for story in stories),
            "heat_distribution": {
                level: sum(story.get("heat_level") == level for story in stories)
                for level in ("H4", "H3", "H2", "H1")
            },
            "public_heat_selected_count": sum(bool(story.get("external_heat_matches")) for story in stories),
        }
    )
    return value


def _fallback_combination(selection: dict[str, Any]) -> dict[str, Any]:
    stories = deepcopy(selection.get("selected_stories") or [])
    digest = hashlib.sha256(
        "|".join(_clean(item.get("event_id")) for item in stories).encode("utf-8")
    ).hexdigest()[:12]
    return {
        "combination_id": f"EC-{digest.upper()}",
        "rank": 1,
        "episode_score": (selection.get("selection_summary") or {}).get("episode_score", 0),
        "story_count": len(stories),
        "duration_profile": (selection.get("selection_summary") or {}).get("duration_profile", "full_episode"),
        "event_ids": [_clean(item.get("event_id")) for item in stories],
        "blocking_issues": [],
        "selected_stories": stories,
    }


def _combinations(selection: dict[str, Any], maximum: int) -> list[dict[str, Any]]:
    rows = [
        deepcopy(item)
        for item in selection.get("episode_combinations") or []
        if isinstance(item, dict) and item.get("selected_stories")
    ]
    if not rows:
        rows = [_fallback_combination(selection)]
    locked = {
        _clean(value)
        for value in ((selection.get("manual_preferences") or {}).get("locked_event_ids") or [])
        if _clean(value)
    }
    locked_rows = [item for item in rows if locked <= set(item.get("event_ids") or [])]
    if locked and locked_rows:
        rows = locked_rows
    rows.sort(key=lambda item: (int(item.get("rank") or 99), -float(item.get("episode_score") or 0)))
    return rows[:maximum]


def _checkpoint(run_dir: Path) -> dict[str, Any]:
    return _read_json(run_dir / "daily_script_v2_last_rejected.json") or {}


def classify_recovery(checkpoint: dict[str, Any], selection: dict[str, Any]) -> dict[str, Any]:
    """Classify the next action without another model call."""
    status = _clean(checkpoint.get("status"))
    if status == "structural_rejected":
        return {
            "failure_class": "structural",
            "action": "repair_same_combination",
            "reason": "结构校验未通过，先复用拒稿检查点进行确定性修复。",
        }
    review = checkpoint.get("editorial_review") if isinstance(checkpoint.get("editorial_review"), dict) else {}
    structured = review.get("structured_issues") if isinstance(review.get("structured_issues"), list) else []
    codes = {
        _clean(item.get("code"))
        for item in structured
        if isinstance(item, dict) and _clean(item.get("code"))
    }
    selected = selection.get("selected_stories") or []
    weak_groups: dict[tuple[str, str], int] = {}
    for story in selected:
        if not isinstance(story, dict) or story.get("heat_level") != "H1" or story.get("external_heat_matches"):
            continue
        key = (_clean(story.get("topic_family")), _clean(story.get("event_form")))
        weak_groups[key] = weak_groups.get(key, 0) + 1
    deterministic_homogeneity = any(count > 1 for count in weak_groups.values())
    if "episode_topic_homogeneity" in codes or deterministic_homogeneity:
        return {
            "failure_class": "systemic_editorial",
            "action": "reselect",
            "reason": "整期后半段题材或事件形式重复，替换最低贡献选题。",
        }
    if "episode_duration_over_target" in codes:
        return {
            "failure_class": "global_duration",
            "action": "repair_same_combination",
            "reason": "整稿预计时长过长，先修点名台词再执行全局压缩。",
        }
    if status == "editorial_rejected":
        return {
            "failure_class": "local_editorial",
            "action": "repair_same_combination",
            "reason": "组合可用，仅定点修复事实边界、对话承接或互动问题。",
        }
    return {
        "failure_class": "unrecoverable",
        "action": "stop",
        "reason": "没有可复用的结构有效拒稿检查点。",
    }


def _candidate_from_checkpoint(checkpoint: dict[str, Any], combination: dict[str, Any]) -> dict[str, Any]:
    script = checkpoint.get("script") if isinstance(checkpoint.get("script"), dict) else None
    review = checkpoint.get("editorial_review") if isinstance(checkpoint.get("editorial_review"), dict) else {}
    if not script:
        return {}
    # A rejected checkpoint stores its cold review beside the validated script.
    # The downstream media release gate reads the review from the script
    # itself, so preserve it when promoting the best reliable draft. Without
    # this copy a real 78—84 candidate is serialized as an apparent zero-score
    # script and can never reach fallback_review_candidate.
    script = deepcopy(script)
    script["editorial_review"] = deepcopy(review)
    return {
        "combination_id": combination.get("combination_id"),
        "event_ids": combination.get("event_ids") or [],
        "editorial_score": int(review.get("total") or 0),
        "hard_fact_boundary": any(
            isinstance(item, dict) and item.get("hard_fact_boundary")
            for item in review.get("structured_issues") or []
        ),
        "script": script,
        "editorial_review": review,
        "saved_at": _now(),
    }


def _is_better(candidate: dict[str, Any], current: dict[str, Any]) -> bool:
    if not candidate:
        return False
    if not current:
        return True
    candidate_key = (not bool(candidate.get("hard_fact_boundary")), int(candidate.get("editorial_score") or 0))
    current_key = (not bool(current.get("hard_fact_boundary")), int(current.get("editorial_score") or 0))
    return candidate_key > current_key


def _attempt_record(
    *,
    index: int,
    combination: dict[str, Any],
    started_at: str,
    status: str,
    checkpoint: dict[str, Any] | None = None,
    result: dict[str, Any] | None = None,
    recovery: dict[str, Any] | None = None,
    error: str = "",
    attempt_fingerprint: str = "",
    progress_fingerprint: str = "",
    request_mode: str = "",
) -> dict[str, Any]:
    review = (result or {}).get("editorial_review") or (checkpoint or {}).get("editorial_review") or {}
    audit = (result or {}).get("generation_audit") or {}
    return {
        "attempt_id": f"TA-{index:02d}",
        "combination_id": combination.get("combination_id"),
        "event_ids": combination.get("event_ids") or [],
        "episode_score": combination.get("episode_score"),
        "started_at": started_at,
        "finished_at": _now(),
        "status": status,
        "editorial_score": int(review.get("total") or 0),
        "editorial_issues": review.get("structured_issues") or review.get("issues") or [],
        "writer_model": audit.get("writer_model") or "",
        "reviewer_model": audit.get("reviewer_model") or review.get("model") or "",
        "recovery": recovery or {},
        "attempt_fingerprint": attempt_fingerprint,
        "progress_fingerprint": progress_fingerprint,
        "request_mode": request_mode,
        "error": _clean(error)[:1000],
        "media_cost_cny": 0.0,
    }


def _raw_dialogue_payload(checkpoint: dict[str, Any]) -> list[dict[str, str]]:
    raw = checkpoint.get("raw_script") if isinstance(checkpoint.get("raw_script"), dict) else {}
    return [
        {"turn_id": _clean(item.get("turn_id")), "text": str(item.get("text") or "")}
        for item in raw.get("lines") or []
        if isinstance(item, dict)
    ]


def _hash_payload(payload: Any) -> str:
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _attempt_fingerprints(
    combination: dict[str, Any],
    checkpoint: dict[str, Any],
    recovery: dict[str, Any],
    *,
    request_mode: str,
) -> tuple[str, str]:
    raw = checkpoint.get("raw_script") if isinstance(checkpoint.get("raw_script"), dict) else {}
    dialogue_sha = _clean(checkpoint.get("raw_dialogue_sha256")) or _hash_payload(_raw_dialogue_payload(checkpoint))
    metadata_sha = _clean(checkpoint.get("metadata_sha256")) or _hash_payload(
        {
            "story_identities": raw.get("story_identities") or [],
            "story_headlines": raw.get("story_headlines") or [],
        }
    )
    issues = sorted(
        _clean(item)
        for item in (checkpoint.get("issue_codes") or checkpoint.get("issues") or [])
        if _clean(item)
    )
    progress = {
        "combination_id": combination.get("combination_id"),
        "dialogue_sha256": dialogue_sha,
        "metadata_sha256": metadata_sha,
        "issues": issues,
        "checkpoint_status": checkpoint.get("status"),
        "editorial_score": int(((checkpoint.get("editorial_review") or {}).get("total") or 0)),
    }
    attempt = {
        **progress,
        "strategy": (recovery or {}).get("action"),
        "request_mode": request_mode,
        "repair_rule_version": checkpoint.get("repair_rule_version") or "",
    }
    return _hash_payload(attempt), _hash_payload(progress)


def _append_event(ledger: dict[str, Any], event_type: str, **details: Any) -> None:
    ledger.setdefault("events", []).append({"type": event_type, "at": _now(), **details})


def generate_resilient_script_v2(
    selection: dict[str, Any],
    research: dict[str, Any],
    *,
    run_dir: Path | None = None,
    policy: dict[str, Any] | None = None,
    progress: ProgressCallback | None = None,
    generator: ScriptGenerator = generate_script_v2,
) -> dict[str, Any]:
    """Generate with at most two editorial reviews and preserve the best valid draft."""
    target_dir = _run_dir(selection, run_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    ledger_path = target_dir / LEDGER_FILENAME
    effective_policy = _policy(policy)
    fingerprint = _fingerprint(selection, research)
    ledger = _read_json(ledger_path) or {}
    if ledger.get("version") != LEDGER_VERSION or ledger.get("input_fingerprint") != fingerprint:
        ledger = _new_ledger(selection, research, effective_policy)
        _persist(ledger_path, ledger)
    if ledger.get("recovery_state") == "passed" and isinstance((ledger.get("best_candidate") or {}).get("script"), dict):
        return {
            "status": "passed",
            "script": ledger["best_candidate"]["script"],
            "selection": ledger["best_candidate"].get("selection") or selection,
            "ledger": ledger,
            "ledger_path": str(ledger_path),
            "reused": True,
        }

    combinations = _combinations(selection, effective_policy["max_episode_combinations"])
    if not combinations:
        raise DailyScriptV2ValidationError(["没有可用于脚本恢复的候选组合"])
    started_clock = datetime.fromisoformat(_clean(ledger.get("created_at")) or _now())
    used_reviews = int(ledger.get("editorial_reviews_used") or 0)
    next_combination_id = _clean(ledger.get("next_combination_id"))
    current_index = next(
        (index for index, item in enumerate(combinations) if item.get("combination_id") == next_combination_id),
        0,
    )
    last_error = ""

    while (
        used_reviews < effective_policy["max_editorial_reviews"]
        and int(ledger.get("unique_attempt_count") or len(ledger.get("attempts") or []))
        < effective_policy["max_text_attempts"]
    ):
        elapsed = (datetime.fromisoformat(_now()) - started_clock).total_seconds()
        if elapsed >= effective_policy["max_wall_clock_seconds"]:
            break
        combination = combinations[min(current_index, len(combinations) - 1)]
        attempt_selection = _selection_for_combination(selection, combination)
        state = "reselecting" if ledger.get("attempts") else "drafting"
        message = (
            f"正在生成候选组合 {combination.get('rank') or current_index + 1}："
            f"{len(combination.get('selected_stories') or [])} 条新闻"
        )
        _notify(progress, state, message, ledger)
        ledger["next_combination_id"] = combination.get("combination_id")
        _persist(ledger_path, ledger)
        attempt_started = _now()
        force_fresh_ids = set(ledger.get("force_fresh_combination_ids") or [])
        force_fresh = _clean(combination.get("combination_id")) in force_fresh_ids
        request_mode = "fresh_writer_request" if force_fresh else "checkpoint_or_initial"
        try:
            script = generator(
                attempt_selection,
                research,
                max_revision_rounds=0,
                reuse_structural_checkpoint=not force_fresh,
            )
            used_reviews += 1
            ledger["editorial_reviews_used"] = used_reviews
            candidate = {
                "combination_id": combination.get("combination_id"),
                "event_ids": combination.get("event_ids") or [],
                "editorial_score": int(((script.get("editorial_review") or {}).get("total") or 0)),
                "hard_fact_boundary": False,
                "script": script,
                "selection": attempt_selection,
                "saved_at": _now(),
            }
            ledger["best_candidate"] = candidate
            ledger["attempts"].append(
                _attempt_record(
                    index=len(ledger["attempts"]) + 1,
                    combination=combination,
                    started_at=attempt_started,
                    status="passed",
                    result=script,
                    request_mode=request_mode,
                )
            )
            ledger["unique_attempt_count"] = len(ledger["attempts"])
            ledger["recovery_state"] = "passed"
            ledger["safe_resume_point"] = "text_complete"
            ledger["next_action"] = "complete"
            _persist(ledger_path, ledger)
            _notify(progress, "passed", "脚本已通过结构与传播双门。", ledger)
            _persist(ledger_path, ledger)
            return {
                "status": "passed",
                "script": script,
                "selection": attempt_selection,
                "ledger": ledger,
                "ledger_path": str(ledger_path),
                "reused": False,
            }
        except DailyScriptV2ValidationError as exc:
            last_error = str(exc)
            checkpoint = _checkpoint(target_dir)
            if checkpoint.get("status") == "editorial_rejected":
                used_reviews += 1
                ledger["editorial_reviews_used"] = used_reviews
            candidate = _candidate_from_checkpoint(checkpoint, combination)
            if candidate:
                candidate["selection"] = attempt_selection
            if _is_better(candidate, ledger.get("best_candidate") or {}):
                ledger["best_candidate"] = candidate
            recovery = classify_recovery(checkpoint, attempt_selection)
            attempt_fp, progress_fp = _attempt_fingerprints(
                combination,
                checkpoint,
                recovery,
                request_mode=request_mode,
            )
            seen_attempts = {
                _clean(item.get("attempt_fingerprint"))
                for item in ledger.get("attempts") or []
                if isinstance(item, dict) and _clean(item.get("attempt_fingerprint"))
            }
            duplicate = attempt_fp in seen_attempts
            if duplicate:
                ledger["duplicate_attempts_suppressed"] = int(ledger.get("duplicate_attempts_suppressed") or 0) + 1
                _append_event(
                    ledger,
                    "no_progress_detected",
                    fingerprint=attempt_fp,
                    progress_fingerprint=progress_fp,
                    combination_id=combination.get("combination_id"),
                    next_action="next_combination" if current_index + 1 < len(combinations) else "stop",
                )
            else:
                ledger["attempts"].append(
                    _attempt_record(
                        index=len(ledger["attempts"]) + 1,
                        combination=combination,
                        started_at=attempt_started,
                        status="rejected",
                        checkpoint=checkpoint,
                        recovery=recovery,
                        error=str(exc),
                        attempt_fingerprint=attempt_fp,
                        progress_fingerprint=progress_fp,
                        request_mode=request_mode,
                    )
                )
                ledger["unique_attempt_count"] = len(ledger["attempts"])
                if checkpoint.get("status") == "structural_rejected":
                    _append_event(
                        ledger,
                        "deterministic_metadata_repair",
                        changed=bool((checkpoint.get("structural_metadata_repair") or {}).get("changed")),
                        dialogue_preserved=bool((checkpoint.get("structural_metadata_repair") or {}).get("dialogue_preserved")),
                        changed_fields=(checkpoint.get("structural_metadata_repair") or {}).get("changed_fields") or [],
                        combination_id=combination.get("combination_id"),
                    )
            if used_reviews >= effective_policy["max_editorial_reviews"] or recovery["action"] == "stop":
                _persist(ledger_path, ledger)
                break
            if checkpoint.get("status") == "structural_rejected":
                combination_id = _clean(combination.get("combination_id"))
                if not force_fresh and not duplicate:
                    force_fresh_ids.add(combination_id)
                    ledger["force_fresh_combination_ids"] = sorted(force_fresh_ids)
                    ledger["next_action"] = "fresh_writer_request"
                    ledger["next_combination_id"] = combination_id
                    _notify(progress, "repairing", "结构元数据仍未通过，正在发起一次新鲜写稿请求。", ledger)
                    _persist(ledger_path, ledger)
                    continue
                if current_index + 1 < len(combinations):
                    current_index += 1
                    ledger["next_action"] = "reselect"
                    ledger["next_combination_id"] = combinations[current_index].get("combination_id")
                    _notify(progress, "reselecting", "检测到重复拒稿，正在切换下一个有效组合。", ledger)
                    _persist(ledger_path, ledger)
                    continue
                _persist(ledger_path, ledger)
                break
            if recovery["action"] == "reselect":
                current_index = min(current_index + 1, len(combinations) - 1)
                ledger["next_action"] = "reselect"
                ledger["next_combination_id"] = combinations[current_index].get("combination_id")
                _notify(progress, "reselecting", recovery["reason"], ledger)
            else:
                ledger["next_action"] = "repair_same_combination"
                ledger["next_combination_id"] = combination.get("combination_id")
                _notify(progress, "repairing", recovery["reason"], ledger)
            _persist(ledger_path, ledger)

    best = ledger.get("best_candidate") or {}
    ledger["recovery_state"] = "awaiting_human" if best.get("script") else "failed"
    ledger["safe_resume_point"] = "human_review_best_candidate" if best.get("script") else "no_valid_script"
    ledger["next_action"] = "human_review" if best.get("script") else "stop"
    ledger["terminal_reason"] = (
        "已用完有界文本恢复额度，保留最佳结构有效稿等待人工处理。"
        if best.get("script")
        else f"未得到结构有效脚本：{last_error or '未知错误'}"
    )
    _persist(ledger_path, ledger)
    if not best.get("script"):
        raise DailyScriptV2ValidationError([ledger["terminal_reason"]])
    _notify(progress, "awaiting_human", ledger["terminal_reason"], ledger)
    _persist(ledger_path, ledger)
    return {
        "status": "awaiting_human",
        "script": best["script"],
        "selection": best.get("selection") or selection,
        "ledger": ledger,
        "ledger_path": str(ledger_path),
        "reused": False,
    }
