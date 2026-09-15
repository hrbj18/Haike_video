"""Durable orchestration for one reviewed interaction's second-pass edit."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
from typing import Any, Callable

from backlot.ai_text import TextAIError, plan_interaction_story
from backlot.material_interaction_second_pass import (
    InteractionSecondPassConflict,
    InteractionSecondPassError,
    apply_second_pass_action,
    attach_render_result,
    build_second_pass_plan,
    list_second_pass_plans,
    plan_path,
    read_second_pass_plan,
    write_second_pass_plan,
)
from backlot.material_interaction_second_pass_render import (
    InteractionSecondPassRenderError,
    render_second_pass_candidate,
)
from backlot.material_interaction_story import (
    InteractionStoryError,
    PROMPT_VERSION,
    build_story_context,
    normalize_story_analysis,
    story_utterances,
)
from backlot.material_interaction_units import build_spoken_units, units_signature


VERSION = "interaction-second-pass-candidates-v1"


class InteractionSecondPassCandidateError(RuntimeError):
    pass


class InteractionSecondPassCandidateConflict(InteractionSecondPassCandidateError):
    pass


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
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


def _project_relative(project_dir: Path, path: str | Path) -> str:
    resolved = Path(path).resolve()
    try:
        return resolved.relative_to(project_dir.resolve()).as_posix()
    except ValueError as exc:
        raise InteractionSecondPassCandidateError("二次剪辑路径越出当前项目") from exc


def _analysis_record_path(output_root: Path, signature: str) -> Path:
    return output_root.resolve() / "analysis" / signature[:24] / "story-analysis.json"


def _ambiguous(error: Exception) -> bool:
    message = str(error).lower()
    return any(token in message for token in ("超时", "timeout", "timed out", "连接中断", "connection", "（5", "(5"))


def _resolve_story(
    *,
    index: dict[str, Any],
    parent_plan: dict[str, Any],
    options: dict[str, Any],
    output_root: Path,
    runtime_identity: dict[str, Any],
    analyze: Callable[[dict[str, Any]], tuple[dict[str, Any], str]],
    units: list[dict[str, Any]] | None = None,
    unit_degradations: list[str] | None = None,
) -> tuple[dict[str, Any], dict[str, Any], bool]:
    context = build_story_context(index, parent_plan, options, units=units)
    request_signature = _digest({
        "version": VERSION, "context_signature": context["signature"],
        # The unit derivation is part of the request: better atoms are a
        # different question, and reusing the old answer would silently keep the
        # 60-second grouping this build exists to replace.
        "unit_signature": units_signature(units) if units else "",
        "runtime_identity": runtime_identity,
    })
    path = _analysis_record_path(output_root, request_signature)
    if path.is_file():
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            record = {}
        if record.get("request_signature") == request_signature:
            if record.get("status") == "completed" and isinstance(record.get("story"), dict):
                return deepcopy(record["story"]), deepcopy(record.get("identity") or {}), True
            if record.get("status") in {"submitting", "ambiguous"}:
                raise InteractionSecondPassCandidateError("内容精选请求受理状态待核对，已阻止重复计费请求")
    record = {
        "version": VERSION, "status": "submitting", "request_signature": request_signature,
        "context_signature": context["signature"], "runtime_identity": deepcopy(runtime_identity),
        "unit_source": context["unit_source"], "unit_count": context["unit_count"],
        "unit_degradations": list(unit_degradations or []),
        "attempts": 1, "safe_resume_point": "story_request_submitting",
    }
    _atomic_json(path, record)
    analysis_started = time.perf_counter()
    try:
        raw, model = analyze(deepcopy(context))
        story = normalize_story_analysis(raw, context)
    except (TextAIError, InteractionStoryError, RuntimeError) as exc:
        record["status"] = "ambiguous" if _ambiguous(exc) else "failed"
        record["error"] = str(exc)[:500]
        record["analysis_elapsed_seconds"] = round(time.perf_counter() - analysis_started, 3)
        record["safe_resume_point"] = "verify_story_request_acceptance" if record["status"] == "ambiguous" else "story_request_failed"
        _atomic_json(path, record)
        if record["status"] == "ambiguous":
            raise InteractionSecondPassCandidateError("内容精选请求受理状态待核对，已阻止重复计费请求") from exc
        raise InteractionSecondPassCandidateError(str(exc)) from exc
    identity = {
        "provider": str(runtime_identity.get("provider") or "default")[:80], "model": str(model)[:160],
        "endpoint_hash": str(runtime_identity.get("endpoint_hash") or "")[:128],
        "maximum_model_calls": min(1, int(runtime_identity.get("maximum_model_calls") or 1)),
        "confirmed": runtime_identity.get("confirmed") is True,
        "prompt_version": PROMPT_VERSION,
        "context_signature": context["signature"], "analysis_signature": story["signature"],
        "request_signature": request_signature,
        "unit_source": context["unit_source"], "unit_count": context["unit_count"],
        "model_calls": 1,
        "analysis_elapsed_seconds": round(time.perf_counter() - analysis_started, 3),
    }
    _atomic_json(path, {
        **record, "status": "completed", "safe_resume_point": "story_analysis_completed",
        "story": story, "identity": identity,
    })
    return story, identity, False


def _parent_speech_ranges(parent_plan: dict[str, Any]) -> list[dict[str, float]]:
    """VAD speech evidence the first pass already paid for.

    Derived here rather than demanded from the caller: every pause cut is
    validated against it, so forgetting it would silently disable the safest of
    the two permissions.
    """
    state = parent_plan.get("speech_activity")
    state = state if isinstance(state, dict) else {}
    rows: list[dict[str, float]] = []
    for raw in state.get("speech_ranges") or []:
        if not isinstance(raw, dict):
            continue
        try:
            start, end = float(raw["start"]), float(raw["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if end > start:
            rows.append({"start": start, "end": end})
    return sorted(rows, key=lambda row: (row["start"], row["end"]))


def generate_second_pass_candidate(
    *,
    project_dir: Path,
    source: Path,
    index: dict[str, Any],
    parent_plan: dict[str, Any],
    output_root: Path,
    options: dict[str, Any],
    runtime_identity: dict[str, Any],
    ffmpeg: str,
    ffprobe: str,
    analyze: Callable[[dict[str, Any]], tuple[dict[str, Any], str]] = plan_interaction_story,
    pause_evidence: dict[str, Any] | None = None,
    render_source: Path | None = None,
    timeout: float = 900,
) -> dict[str, Any]:
    operation_started = time.perf_counter()
    try:
        speech_ranges = _parent_speech_ranges(parent_plan)
        units, unit_degradations = build_spoken_units(
            (index.get("audio") or {}).get("utterances"),
            speech_ranges,
            allowed=[row for row in parent_plan.get("keep_ranges") or [] if isinstance(row, dict)],
        )
        story, identity, analysis_cache_hit = _resolve_story(
            index=index, parent_plan=parent_plan, options=options, output_root=output_root,
            runtime_identity=runtime_identity, analyze=analyze,
            units=units or None, unit_degradations=unit_degradations,
        )
        plan = build_second_pass_plan(
            parent_plan=parent_plan, story=story,
            utterances=story_utterances(index, parent_plan), options=options, story_identity=identity,
            pause_evidence=pause_evidence, speech_ranges=speech_ranges,
            units=units or None,
        )
        if unit_degradations:
            # ``warnings`` is frozen input (never re-derived), unlike
            # ``degradations``, which the plan rebuilds from the pause evidence —
            # writing a unit note there would make the stored plan disagree with
            # its own reconstruction on the next read.
            plan["warnings"] = list(dict.fromkeys(list(plan.get("warnings") or []) + unit_degradations))
        path = plan_path(output_root, plan["plan_id"])
        if path.is_file():
            plan = read_second_pass_plan(path)
            preview = plan.get("preview") if isinstance(plan.get("preview"), dict) else {}
            preview_path = project_dir / str(preview.get("path") or "")
            if (plan.get("qa") or {}).get("status") == "passed" and preview_path.is_file() and preview.get("stale") is not True:
                return {**plan, "analysis_cache_hit": True, "render_cache_hit": True}
        else:
            write_second_pass_plan(path, plan)
        render_started = time.perf_counter()
        manifest = render_second_pass_candidate(
            source, plan, output_root / "renders", ffmpeg=ffmpeg, ffprobe=ffprobe,
            render_source=render_source, timeout=timeout,
        )
        render_elapsed = round(time.perf_counter() - render_started, 3)
        plan = attach_render_result(
            plan, manifest, expected_revision=int(plan["revision"]),
            preview_path=_project_relative(project_dir, manifest["path"]),
        )
        plan["usage"] = {
            "semantic_model_calls": 0 if analysis_cache_hit else 1,
            "analysis_cache_hit": analysis_cache_hit,
            "render_cache_hit": bool(manifest.get("cache_hit")),
            "analysis_elapsed_seconds": float(identity.get("analysis_elapsed_seconds") or 0),
            "render_elapsed_seconds": render_elapsed,
            "total_elapsed_seconds": round(time.perf_counter() - operation_started, 3),
        }
        write_second_pass_plan(path, plan)
        return {**plan, "analysis_cache_hit": analysis_cache_hit, "render_cache_hit": bool(manifest.get("cache_hit"))}
    except InteractionSecondPassConflict as exc:
        raise InteractionSecondPassCandidateConflict(str(exc)) from exc
    except (InteractionStoryError, InteractionSecondPassError, InteractionSecondPassRenderError) as exc:
        raise InteractionSecondPassCandidateError(str(exc)) from exc


def update_second_pass_candidate(
    *,
    project_dir: Path,
    source: Path,
    output_root: Path,
    plan_id: str,
    action: str,
    expected_revision: int,
    group_states: dict[str, bool] | None,
    locked_states: dict[str, bool] | None,
    speed: float | None,
    hook_candidate_id: str | None,
    hook_mode: str | None = None,
    ffmpeg: str,
    ffprobe: str,
    render_source: Path | None = None,
    timeout: float = 900,
) -> dict[str, Any]:
    path = plan_path(output_root, plan_id)
    operation_started = time.perf_counter()
    try:
        plan = read_second_pass_plan(path)
        updated = apply_second_pass_action(
            plan, action=action, expected_revision=expected_revision,
            group_states=group_states, locked_states=locked_states,
            speed=speed, hook_candidate_id=hook_candidate_id, hook_mode=hook_mode,
        )
        if action == "save_edits":
            render_started = time.perf_counter()
            manifest = render_second_pass_candidate(
                source, updated, output_root / "renders", ffmpeg=ffmpeg, ffprobe=ffprobe,
                render_source=render_source, timeout=timeout,
            )
            updated = attach_render_result(
                updated, manifest, expected_revision=int(updated["revision"]),
                preview_path=_project_relative(project_dir, manifest["path"]),
            )
            previous_usage = plan.get("usage") if isinstance(plan.get("usage"), dict) else {}
            updated["usage"] = {
                **deepcopy(previous_usage),
                "semantic_model_calls": 0,
                "analysis_cache_hit": True,
                "render_cache_hit": bool(manifest.get("cache_hit")),
                "render_elapsed_seconds": round(time.perf_counter() - render_started, 3),
                "total_elapsed_seconds": round(time.perf_counter() - operation_started, 3),
            }
        write_second_pass_plan(path, updated)
        return updated
    except InteractionSecondPassConflict as exc:
        raise InteractionSecondPassCandidateConflict(str(exc)) from exc
    except (InteractionSecondPassError, InteractionSecondPassRenderError) as exc:
        raise InteractionSecondPassCandidateError(str(exc)) from exc


__all__ = [
    "InteractionSecondPassCandidateConflict", "InteractionSecondPassCandidateError",
    "generate_second_pass_candidate", "list_second_pass_plans", "read_second_pass_plan",
    "update_second_pass_candidate", "write_second_pass_plan",
]
