"""Durable interaction rough-cut candidates built from one reviewed index.

This orchestration layer remains project-local and provider-free: remote vision
and ASR have already finished before it runs.  It never edits the source and it
never approves or adopts a candidate on behalf of the user.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
from typing import Any

from backlot.material_audio_evidence import MaterialAudioEvidenceError, detect_silence
from backlot.material_interaction_edit import (
    InteractionEditConflict,
    InteractionEditError,
    apply_plan_action,
    attach_render_result,
    build_edit_plan,
    read_edit_plan,
    write_edit_plan,
)
from backlot.material_interaction_render import InteractionRenderError, render_interaction_candidate
from backlot.material_interaction_refinement import (
    InteractionRefinementError,
    analyze_pause_visual_activity,
    pause_visual_identity,
    refine_event,
)
from backlot.material_speech_activity import (
    SpeechActivityError,
    SpeechActivityUnavailable,
    detect_speech_activity,
)


VERSION = "interaction-candidates-v2"
PLAN_FILENAME = "interaction-edit-plan.json"


class InteractionCandidateError(RuntimeError):
    pass


class InteractionCandidateConflict(InteractionCandidateError):
    pass


def _project_relative(project_dir: Path, path: str | Path) -> str:
    resolved = Path(path).resolve()
    try:
        return resolved.relative_to(project_dir.resolve()).as_posix()
    except ValueError as exc:
        raise InteractionCandidateError("互动候选路径越出当前项目") from exc


def _review_event(review: dict[str, Any], event_id: str) -> dict[str, Any]:
    event = next((row for row in review.get("events") or [] if row.get("review_event_id") == event_id), None)
    if not isinstance(event, dict):
        raise InteractionCandidateError("未找到要生成的互动事件")
    return event


def _irrelevant_candidates(index: dict[str, Any], review_event: dict[str, Any]) -> list[dict[str, Any]]:
    source_ids = set(str(item) for item in review_event.get("source_event_ids") or [])
    rows: list[dict[str, Any]] = []
    for event in index.get("events") or []:
        if not isinstance(event, dict) or str(event.get("event_id") or "") not in source_ids:
            continue
        rows.extend(deepcopy(item) for item in event.get("irrelevant_segments") or [] if isinstance(item, dict))
    return rows


def _semantic_annotations(index: dict[str, Any], review_event: dict[str, Any],
                          pause_visual: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    rows = []
    for sequence, row in enumerate(_irrelevant_candidates(index, review_event), 1):
        rows.append({
            **row,
            "id": str(row.get("id") or f"IRR-{sequence:03d}"),
            "stage": "unrelated",
        })
    event_start, event_end = float(review_event["start"]), float(review_event["end"])
    for row in (pause_visual or {}).get("windows") or []:
        if not isinstance(row, dict):
            continue
        try:
            start, end = float(row["start"]), float(row["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if event_start <= start < end <= event_end:
            rows.append(deepcopy(row))
    return rows


def _speech_activity_cache_path(output_root: Path, source_fingerprint: str,
                                start: float, end: float) -> Path:
    payload = f"{source_fingerprint}:{start:.3f}:{end:.3f}"
    return output_root.resolve() / "speech-activity" / (hashlib.sha256(payload.encode()).hexdigest()[:20] + ".json")


def _pause_visual_activity(source: Path, *, source_fingerprint: str, output_root: Path,
                           index: dict[str, Any], ffmpeg: str, timeout: float) -> dict[str, Any]:
    payload = {
        "source_fingerprint": source_fingerprint,
        "index_signature": str(index.get("signature") or ""),
        "identity": pause_visual_identity(),
    }
    signature = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    path = output_root.resolve() / "pause-visual" / f"{signature[:20]}.json"
    if path.is_file():
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            cached = {}
        if cached.get("cache_signature") == signature and cached.get("status") in {"available", "partial"}:
            return {**cached, "cache_hit": True}
    result = analyze_pause_visual_activity(
        source, index, ffmpeg=ffmpeg, timeout=min(float(timeout), 180.0),
    )
    frozen = {**result, **payload, "cache_signature": signature, "cache_hit": False}
    if result.get("status") in {"available", "partial"}:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(frozen, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)
    return frozen


def _speech_activity(
    source: Path,
    *,
    source_fingerprint: str,
    output_root: Path,
    ffmpeg: str,
    start: float,
    end: float,
    timeout: float,
) -> dict[str, Any]:
    path = _speech_activity_cache_path(output_root, source_fingerprint, start, end)
    if path.is_file():
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            cached = {}
        if (cached.get("source_fingerprint") == source_fingerprint
                and cached.get("range") == {"start": round(start, 3), "end": round(end, 3)}
                and cached.get("status") in {"available", "no_audio_samples"}):
            return {**cached, "cache_hit": True}
    result = detect_speech_activity(
        source, ffmpeg=ffmpeg, start=start, end=end,
        timeout=min(float(timeout), 300.0),
    )
    payload = {**result, "source_fingerprint": source_fingerprint, "cache_hit": False}
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)
    return payload


def _plan_path(root: Path, plan_id: str) -> Path:
    if not plan_id.startswith("IEP-") or not plan_id[4:].isalnum():
        raise InteractionCandidateError("互动候选编号无效")
    return root.resolve() / plan_id / PLAN_FILENAME


def list_candidates(root: Path) -> list[dict[str, Any]]:
    if not root.is_dir():
        return []
    result = []
    for path in root.glob(f"IEP-*/{PLAN_FILENAME}"):
        try:
            result.append(read_edit_plan(path))
        except InteractionEditError:
            continue
    return sorted(result, key=lambda row: str(row.get("updated_at") or ""), reverse=True)


def _version_rank(value: Any) -> int:
    """Return a stable numeric rank without assuming every old plan is V2."""
    text = str(value or "")
    try:
        return int(text.rsplit("-v", 1)[1])
    except (IndexError, ValueError):
        return 0


def classify_candidate_lineage(plans: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Annotate immutable candidate history with one active plan per event.

    Plan files remain untouched.  The active choice is derived from the latest
    reviewed contract first, then the implementation generation and timestamps.
    """
    rows = [deepcopy(row) for row in plans if isinstance(row, dict)]
    by_event: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        event_id = str(row.get("event_id") or row.get("group_id") or row.get("plan_id") or "")
        by_event.setdefault(event_id, []).append(row)
    for event_rows in by_event.values():
        active = max(
            event_rows,
            key=lambda row: (
                int(row.get("review_revision") or 0),
                _version_rank(row.get("version")),
                str(row.get("created_at") or ""),
                str(row.get("updated_at") or ""),
                str(row.get("plan_id") or ""),
            ),
        )
        active_id = str(active.get("plan_id") or "")
        for row in event_rows:
            is_active = str(row.get("plan_id") or "") == active_id
            row["is_active"] = is_active
            row["lineage_status"] = "active" if is_active else "superseded"
            row["superseded_by"] = None if is_active else active_id
    return sorted(
        rows,
        key=lambda row: (
            row.get("is_active") is True,
            str(row.get("updated_at") or ""),
        ),
        reverse=True,
    )


def list_candidate_lineage(root: Path) -> list[dict[str, Any]]:
    return classify_candidate_lineage(list_candidates(root))


def read_candidate(root: Path, plan_id: str) -> dict[str, Any]:
    """Read one validated candidate without exposing the plan path contract."""
    try:
        return read_edit_plan(_plan_path(root, plan_id))
    except InteractionEditError as exc:
        raise InteractionCandidateError(str(exc)) from exc


def generate_candidate(
    *,
    project_dir: Path,
    source: Path,
    index: dict[str, Any],
    review: dict[str, Any],
    event_id: str,
    output_root: Path,
    ffmpeg: str,
    ffprobe: str,
    timeout: float = 900,
    target_gap: float = .3,
) -> dict[str, Any]:
    """Plan, render and persist one pending-review candidate."""
    event = _review_event(review, event_id)
    audio = index.get("audio") if isinstance(index.get("audio"), dict) else {}
    silences: list[dict[str, float]] = []
    speech_activity: dict[str, Any] | None = None
    pause_visual: dict[str, Any] | None = None
    vad_warning = None
    if audio.get("policy") == "doubao_transcript" and audio.get("status") == "available":
        try:
            source_fingerprint = str((index.get("source") or {}).get("fingerprint") or "")
            vad_start = max(0.0, float(event["start"]) - 6.0)
            vad_end = min(float(index.get("duration") or event["end"]), float(event["end"]) + 6.0)
            speech_activity = _speech_activity(
                source,
                source_fingerprint=source_fingerprint,
                output_root=output_root,
                ffmpeg=ffmpeg,
                start=vad_start,
                end=vad_end,
                timeout=timeout,
            )
            silences = detect_silence(
                source,
                ffmpeg=ffmpeg,
                start=float(event["start"]),
                end=float(event["end"]),
                timeout=min(float(timeout), 300.0),
            )
            pause_visual = _pause_visual_activity(
                source, source_fingerprint=source_fingerprint, output_root=output_root,
                index=index, ffmpeg=ffmpeg, timeout=timeout,
            )
        except (KeyError, TypeError, ValueError, MaterialAudioEvidenceError,
                SpeechActivityError, SpeechActivityUnavailable) as exc:
            # A missing local optional dependency must not destroy the already
            # valid interaction range.  It disables automatic internal edits.
            vad_warning = str(exc)
            speech_activity = {"status": "unavailable", "identity": None,
                               "speech_ranges": [], "non_speech_ranges": [], "error": vad_warning}
    try:
        refinement = refine_event(
            index,
            review,
            event_id,
            speech_ranges=(speech_activity or {}).get("speech_ranges") or [],
            semantic_annotations=_semantic_annotations(index, event, pause_visual),
        )
        visual_status = (pause_visual or {}).get("status")
        if vad_warning or (speech_activity or {}).get("status") != "available" or visual_status == "unavailable":
            refinement["safe_for_internal_edit"] = False
            refinement.setdefault("warnings", []).append(
                "本地精剪证据不完整，已保留连续互动范围："
                + (vad_warning or "局部画面证据不可用")
            )
            signature_payload = {key: value for key, value in refinement.items() if key != "signature"}
            refinement["signature"] = hashlib.sha256(
                json.dumps(signature_payload, sort_keys=True, ensure_ascii=False).encode()
            ).hexdigest()
        plan = build_edit_plan(
            index,
            review,
            event_id,
            silence_intervals=silences,
            irrelevant_candidates=_irrelevant_candidates(index, event),
            refinement=refinement,
            speech_activity=speech_activity,
            target_gap=target_gap,
        )
        plan["pause_visual"] = deepcopy(pause_visual)
        path = _plan_path(output_root, plan["plan_id"])
        if path.is_file():
            existing = read_edit_plan(path)
            # The plan identity includes evidence and rules.  Repeating the
            # same request therefore resumes the persisted user revision and
            # never overwrites restored pending edits.  A passed preview is a
            # true cache hit; a plan left before/during rendering resumes from
            # that safe point instead of becoming a permanent half-result.
            preview = existing.get("preview") if isinstance(existing.get("preview"), dict) else {}
            preview_path = project_dir / str(preview.get("path") or "")
            if (existing.get("qa") or {}).get("status") == "passed" and preview_path.is_file():
                return existing
            plan = existing
        else:
            write_edit_plan(path, plan)
        manifest = render_interaction_candidate(
            source,
            plan,
            output_root / "renders",
            ffmpeg=ffmpeg,
            ffprobe=ffprobe,
            timeout=timeout,
        )
        plan = attach_render_result(
            plan,
            manifest,
            expected_revision=int(plan["revision"]),
            preview_path=_project_relative(project_dir, manifest["path"]),
        )
        write_edit_plan(path, plan)
        return plan
    except InteractionEditConflict as exc:
        raise InteractionCandidateConflict(str(exc)) from exc
    except (InteractionEditError, InteractionRenderError, InteractionRefinementError) as exc:
        raise InteractionCandidateError(str(exc)) from exc


def update_candidate(
    *,
    project_dir: Path,
    source: Path,
    output_root: Path,
    plan_id: str,
    action: str,
    expected_revision: int,
    removal_id: str | None = None,
    removal_states: dict[str, bool] | None = None,
    ffmpeg: str,
    ffprobe: str,
    timeout: float = 900,
) -> dict[str, Any]:
    """Apply one CAS action and regenerate only when the edit became stale."""
    path = _plan_path(output_root, plan_id)
    try:
        plan = read_edit_plan(path)
        updated = apply_plan_action(
            plan,
            action=action,
            expected_revision=expected_revision,
            removal_id=removal_id,
            removal_states=removal_states,
        )
        if action in {"restore_removal", "reinstate_removal", "save_edits"}:
            manifest = render_interaction_candidate(
                source,
                updated,
                output_root / "renders",
                ffmpeg=ffmpeg,
                ffprobe=ffprobe,
                timeout=timeout,
            )
            updated = attach_render_result(
                updated,
                manifest,
                expected_revision=int(updated["revision"]),
                preview_path=_project_relative(project_dir, manifest["path"]),
            )
        write_edit_plan(path, updated)
        return updated
    except InteractionEditConflict as exc:
        raise InteractionCandidateConflict(str(exc)) from exc
    except (InteractionEditError, InteractionRenderError) as exc:
        raise InteractionCandidateError(str(exc)) from exc
