"""Recoverable one-click review preview for long-form avatar projects.

This parent job owns the project-local path from an approved one- or two-presenter
script through TTS, one sequential RunningHub long-form video per active role, local ASR
alignment, supporting visuals and a human-review preview.  It deliberately
shares the workbench's existing ``review_preview_pipeline`` slot so manual
media mutations remain mutually exclusive and the task center has one source
of truth.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import time
import wave
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from backlot import workbench as wb
from backlot.audio_center import get_voice_profile
from backlot.avatar_audio_clock import (
    AVATAR_VIDEO_FPS,
    AvatarAudioClockError,
    align_pcm_wav_to_frame_clock,
    inspect_frame_clock_wav,
    nearest_video_frame,
)
from backlot.avatar_import import (
    AvatarImportError,
    _find_binary as _find_media_binary,
    _run as _run_media_command,
    approve_exact_clock_manifest_cuts,
    approve_high_confidence_longform_cuts,
    apply_longform_timing_manifest,
    assemble_avatar_package,
    ensure_exact_clock_assembly_duration_limit,
    finalize_upload,
    initialize_avatar_package,
    list_local_whisper_models,
    preflight_local_whisper,
    prepare_upload,
    probe_media,
    read_avatar_package,
    run_avatar_asr,
    start_avatar_asr,
    start_avatar_assembly,
)
from backlot.daily_automation import classify_runninghub_failure
from backlot.daily_pipeline import (
    PRODUCTION_WORKFLOW_ID,
    PRODUCTION_WORKFLOW_PROFILE,
    ROLE_LABELS,
    ROLE_RESERVATION_CNY,
    STANDARD_RATE_CNY_PER_HOUR,
    _voicebox_profiles,
)
from backlot.avatar_roles import (
    AvatarRoleError,
    avatar_role_asset_file,
    find_avatar_role_by_voice_profile,
    list_avatar_roles,
    role_front_reference,
)
from backlot.review_preview_pipeline import _probe_preview_evidence, collect_review_preview_capabilities
from backlot.runninghub_config import read_runninghub_config
from backlot.tts_runtime import generate_voice_audio
from tools.avatar.runninghub_avatar import RunningHubLongCatClient


PIPELINE_VERSION = "avatar-review-preview-v1.5"
TURN_TIMING_MANIFEST_VERSION = "avatar-turn-timing-v2"
AVATAR_RECOVERY_POLICY_VERSION = "runninghub-oom-recovery-v1"
OPENING_SILENCE_MS = 100
TURN_TRAILING_SILENCE_MS = 150
BETWEEN_SOURCE_SILENCE_MS = 500
SPEAKER_CHANGE_GAP_MS = 250
SAME_SPEAKER_GAP_MS = 300
DEFAULT_BUDGET_LIMIT_CNY = 5.0
ABSOLUTE_BUDGET_LIMIT_CNY = 8.0
PLUS_RATE_CNY_PER_HOUR = 6.0
POLL_INTERVAL_SECONDS = 20.0
POLL_TIMEOUT_SECONDS = 8 * 60 * 60
MAX_TRANSIENT_POLL_ERRORS = 3
MAX_TRANSIENT_POLL_BACKOFF_SECONDS = 5.0
MAX_TRAILING_CLOCK_PAD_FRAMES = 5
ONE_CLICK_AVATAR_MAX_DURATION_SECONDS = 300.0
# The configured text relay may keep an SSE connection alive with heartbeat
# events even after it stops producing a usable plan. A visual plan is
# advisory; never let that one call hold the paid-avatar parent job forever.
VISUAL_AI_PLANNING_TIMEOUT_SECONDS = 75.0
VOICE_DIRECTORY = Path("assets/audio/avatar-review-preview")
AVATAR_DIRECTORY = Path("assets/video/avatar-review-preview")
PRESENTER_BINDINGS_PATH = Path("artifacts/avatar-review-presenter-bindings.json")
PRESENTER_BINDINGS_VERSION = "1.0"
ALLOWED_PROJECT_TYPE = "avatar-spokesperson"


class AvatarReviewPreviewError(ValueError):
    """Safe user-facing parent-job failure."""


class AvatarReviewPreviewConflict(AvatarReviewPreviewError):
    """The active job or worker lease changed."""


class StaleAvatarReviewPreviewWorker(AvatarReviewPreviewConflict):
    """An obsolete worker attempted to write current state."""


class AvatarInputDriftError(AvatarReviewPreviewError):
    """A frozen script, role, provider or local model changed."""


class AmbiguousAvatarOperation(AvatarReviewPreviewError):
    """RunningHub may have accepted the request; automatic retry is unsafe."""


class NonRetryableAvatarOperation(AvatarReviewPreviewError):
    """A durable external terminal condition requires a new human decision."""


class AvatarRecoveryExhaustedError(NonRetryableAvatarOperation):
    """All authorized paid attempts for the current presenter were consumed."""


class AvatarProviderTerminalError(NonRetryableAvatarOperation):
    """The provider definitively failed for a reason outside the OOM policy."""


class AvatarBudgetBlockedError(NonRetryableAvatarOperation):
    """The frozen budget cannot safely reserve another paid attempt."""


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _json_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _empty_job() -> dict[str, Any]:
    return {
        "version": PIPELINE_VERSION,
        "pipeline_kind": "avatar_review_preview",
        "job_id": None,
        "script_hash": None,
        "input_fingerprint": None,
        "request_fingerprint": None,
        "status": "idle",
        "stage": "preflight",
        "counts": {"total": 7, "completed": 0, "failed": 0},
        "current": None,
        "gate": None,
        "error": None,
        "safe_resume_point": None,
        "result": None,
        "frozen_input": None,
        "phases": {},
        "budget": {
            "limit": DEFAULT_BUDGET_LIMIT_CNY,
            "absolute_limit": ABSOLUTE_BUDGET_LIMIT_CNY,
            "reserved": 0.0,
            "spent": 0.0,
            "entries": [],
        },
        "paid_operations": {},
        "worker_token": None,
    }


def _pipeline(state: dict[str, Any]) -> dict[str, Any]:
    automation = wb._automation(state)
    job = automation.get("review_preview_pipeline")
    if not isinstance(job, dict) or (
        job.get("pipeline_kind") not in {None, "avatar_review_preview"}
        and job.get("status") not in {None, "idle"}
    ):
        return job if isinstance(job, dict) else _empty_job()
    if not isinstance(job, dict) or not job.get("job_id"):
        job = _empty_job()
        automation["review_preview_pipeline"] = job
    defaults = _empty_job()
    for key, value in defaults.items():
        job.setdefault(key, deepcopy(value))
    job["pipeline_kind"] = "avatar_review_preview"
    return job


def _public(job: dict[str, Any], *, launch_required: bool = False) -> dict[str, Any]:
    value = deepcopy(job)
    value.pop("worker_token", None)
    value["launch_required"] = bool(launch_required)
    return value


def _project(project_dir: Path) -> dict[str, Any]:
    value = _read_json(project_dir / "project.json")
    if str(value.get("pipeline_type") or "") != ALLOWED_PROJECT_TYPE:
        raise AvatarReviewPreviewError("有数字人一键审核预览仅适用于“数字人口播”项目")
    return value


def _approved_script(project_dir: Path, state: dict[str, Any]) -> dict[str, Any]:
    draft = ((state.get("project") or {}).get("script_draft") or {})
    script = _read_json(project_dir / "artifacts" / "script.json")
    if draft.get("status") != "approved" or not script:
        raise AvatarReviewPreviewError("请先整理并通过脚本草案，再启动有数字人审核预览")
    sections = script.get("sections") if isinstance(script.get("sections"), list) else []
    if not sections:
        raise AvatarReviewPreviewError("正式脚本没有可生产的台词轮次")
    seen_turns: set[str] = set()
    seen_speakers: set[str] = set()
    for index, section in enumerate(sections, 1):
        if not isinstance(section, dict):
            raise AvatarReviewPreviewError(f"正式脚本第 {index} 个轮次格式无效")
        turn_id = str(section.get("turn_id") or "").upper()
        speaker_id = str(section.get("speaker_id") or "").lower()
        if not turn_id or turn_id in seen_turns:
            raise AvatarReviewPreviewError("正式脚本的 Txxx 轮次编号缺失或重复")
        if speaker_id not in ROLE_LABELS:
            raise AvatarReviewPreviewError("当前一键路线只支持雅雅或檬檬；请先确认说话人映射")
        if not str(section.get("text") or "").strip():
            raise AvatarReviewPreviewError(f"{turn_id} 台词为空")
        seen_turns.add(turn_id)
        seen_speakers.add(speaker_id)
    if not seen_speakers:
        raise AvatarReviewPreviewError("正式脚本至少需要一位已映射的主持人")
    return script


def _active_roles(script: dict[str, Any]) -> tuple[str, ...]:
    """Return the supported presenters actually present in script order."""
    seen = {
        str(section.get("speaker_id") or "").lower()
        for section in (script.get("sections") or [])
        if isinstance(section, dict)
    }
    return tuple(role for role in ROLE_LABELS if role in seen)


def _role_texts(script: dict[str, Any], roles: tuple[str, ...] | None = None) -> dict[str, str]:
    sections = script.get("sections") or []
    active_roles = roles or _active_roles(script)
    return {
        role: "\n".join(
            str(section.get("text") or "").strip()
            for section in sections
            if isinstance(section, dict) and str(section.get("speaker_id") or "").lower() == role
        ).strip()
        for role in active_roles
    }


def _requested_voice_profile_ids(payload: dict[str, Any] | None) -> dict[str, str]:
    raw = (payload or {}).get("voice_profiles")
    if not isinstance(raw, dict):
        return {}
    return {
        role: str(raw.get(role) or "").strip()
        for role in ROLE_LABELS
        if str(raw.get(role) or "").strip()
    }


def _bound_role_voice_candidates() -> dict[str, list[dict[str, Any]]]:
    """Return complete, available voice/avatar pairs without exposing secrets."""
    candidates: dict[str, list[dict[str, Any]]] = {role: [] for role in ROLE_LABELS}
    roles = list_avatar_roles().get("roles") or []
    for avatar_role in roles:
        if not isinstance(avatar_role, dict):
            continue
        matched_role = next(
            (
                role
                for role, label in ROLE_LABELS.items()
                if str(avatar_role.get("name") or "").strip() == label
            ),
            None,
        )
        binding = avatar_role.get("voice_binding") if isinstance(avatar_role.get("voice_binding"), dict) else {}
        profile_id = str(binding.get("profile_id") or "").strip()
        if not matched_role or not profile_id:
            continue
        try:
            role_front_reference(avatar_role)
        except AvatarRoleError:
            continue
        profile = get_voice_profile(profile_id)
        if not profile or profile.get("available") is False:
            continue
        candidates[matched_role].append(profile)
    return candidates


def _avatar_voice_profiles(
    payload: dict[str, Any] | None = None,
    *,
    roles: tuple[str, ...] | None = None,
) -> dict[str, dict[str, Any]]:
    """Resolve the deliberate role voices used by an avatar parent job."""
    requested = _requested_voice_profile_ids(payload)
    candidates = _bound_role_voice_candidates()
    resolved: dict[str, dict[str, Any]] = {}
    legacy: dict[str, dict[str, Any]] | None = None
    active_roles = tuple(ROLE_LABELS) if roles is None else roles
    for role in active_roles:
        label = ROLE_LABELS[role]
        requested_id = requested.get(role)
        if requested_id:
            profile = get_voice_profile(requested_id)
            if not profile:
                raise AvatarReviewPreviewError(f"{label}选择的配音中心音色已不存在")
            if profile.get("available") is False:
                raise AvatarReviewPreviewError(f"{label}选择的{profile.get('provider_name') or '配音服务'}当前不可用")
            avatar_role = find_avatar_role_by_voice_profile(requested_id)
            if not avatar_role or str(avatar_role.get("name") or "").strip() != label:
                raise AvatarReviewPreviewError(f"{label}选择的音色尚未关联同名数字人角色")
            role_front_reference(avatar_role)
            resolved[role] = profile
            continue
        role_candidates = candidates.get(role) or []
        if len(role_candidates) == 1:
            resolved[role] = role_candidates[0]
            continue
        if len(role_candidates) > 1:
            names = "、".join(str(item.get("name") or item.get("id")) for item in role_candidates)
            raise AvatarReviewPreviewError(f"{label}存在多个完整角色音色（{names}），请明确选择本次音色")
        # Preserve existing local-only installs. The presenter-binding check
        # below still prevents an unbound or image-less local profile running.
        if legacy is None:
            legacy = _voicebox_profiles()
        resolved[role] = legacy[role]
    return resolved


def _presenter_binding_path(project_dir: Path) -> Path:
    return project_dir / PRESENTER_BINDINGS_PATH


def _read_presenter_bindings(project_dir: Path) -> dict[str, Any]:
    value = _read_json(_presenter_binding_path(project_dir))
    if not isinstance(value.get("roles"), dict):
        value["roles"] = {}
    value.setdefault("version", PRESENTER_BINDINGS_VERSION)
    return value


def _materialize_role_presenter(project_dir: Path, role: str, profile: dict[str, Any]) -> tuple[Path, dict[str, Any]]:
    label = ROLE_LABELS[role]
    profile_id = str(profile.get("id") or "").strip()
    try:
        avatar_role = find_avatar_role_by_voice_profile(profile_id)
    except AvatarRoleError as exc:
        raise AvatarReviewPreviewError(str(exc)) from exc
    if not avatar_role:
        raise AvatarReviewPreviewError(
            f"配音中心尚未为{label}当前音色“{profile.get('name') or profile_id}”关联数字人角色档案"
        )
    try:
        reference = role_front_reference(avatar_role)
        source = avatar_role_asset_file(str(avatar_role["role_id"]), str(reference["path"]))
    except AvatarRoleError as exc:
        raise AvatarReviewPreviewError(str(exc)) from exc
    digest = _sha256_file(source)
    if digest != str(reference.get("sha256") or ""):
        raise AvatarReviewPreviewError(f"角色“{avatar_role.get('name') or label}”的正面出镜图已变化，请重新上传后再试")
    extension = source.suffix.lower()
    target = project_dir / "assets" / "images" / "avatar-review-presenters" / role / f"presenter_{digest[:12]}{extension}"
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.is_file() or _sha256_file(target) != digest:
        temporary = target.parent / f".{target.name}.{uuid4().hex}.tmp"
        try:
            shutil.copy2(source, temporary)
            if _sha256_file(temporary) != digest:
                raise AvatarReviewPreviewError(f"{label}角色图复制校验失败")
            temporary.replace(target)
        finally:
            if temporary.exists():
                temporary.unlink(missing_ok=True)
    return target, {
        "role_id": str(avatar_role["role_id"]),
        "role_name": str(avatar_role.get("name") or label),
        "voice_profile_id": profile_id,
        "voice_profile_name": str(profile.get("name") or profile_id),
        "voice_signature": str(profile.get("voice_signature") or "") or None,
        "reference_slot": "front",
        "reference_path": str(reference["path"]),
        "reference_sha256": digest,
        "presenter_path": str(target.relative_to(project_dir)).replace("\\", "/"),
        "presenter_filename": target.name,
        "presenter_sha256": digest,
        "materialized_at": _now(),
    }


def _resolve_presenter_images(
    project_dir: Path,
    profiles: dict[str, dict[str, Any]],
    *,
    refresh_from_role_library: bool,
) -> tuple[dict[str, Path], dict[str, dict[str, Any]]]:
    """Resolve explicit voice-to-role bindings into immutable project inputs."""
    store = _read_presenter_bindings(project_dir)
    records = store["roles"]
    images: dict[str, Path] = {}
    resolved: dict[str, dict[str, Any]] = {}
    changed = False
    for role in profiles:
        profile = profiles[role]
        existing = records.get(role) if isinstance(records.get(role), dict) else None
        profile_id = str(profile.get("id") or "")
        if refresh_from_role_library or not existing:
            image, binding = _materialize_role_presenter(project_dir, role, profile)
            records[role] = binding
            existing = binding
            changed = True
        else:
            if str(existing.get("voice_profile_id") or "") != profile_id:
                raise AvatarInputDriftError(f"{ROLE_LABELS[role]}的音色已变化；请启动新任务")
            candidate = project_dir / str(existing.get("presenter_path") or "")
            if not candidate.is_file() or _sha256_file(candidate) != str(existing.get("presenter_sha256") or ""):
                raise AvatarInputDriftError(f"{ROLE_LABELS[role]}的项目内角色图缺失或已变化；请启动新任务")
            image = candidate
        images[role] = image
        resolved[role] = dict(existing or {})
    if changed:
        store["updated_at"] = _now()
        wb._atomic_write(_presenter_binding_path(project_dir), store)
    return images, resolved


def _role_evidence(
    script: dict[str, Any],
    profiles: dict[str, dict[str, Any]],
    images: dict[str, Path],
    presenter_bindings: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    texts = _role_texts(script, tuple(profiles))
    evidence: dict[str, Any] = {}
    for role in profiles:
        label = ROLE_LABELS[role]
        text = texts.get(role) or ""
        profile = profiles[role]
        image = images[role]
        binding = presenter_bindings[role]
        evidence[role] = {
            "role": role,
            "label": label,
            "turn_count": sum(
                1 for item in script.get("sections") or []
                if isinstance(item, dict) and str(item.get("speaker_id") or "").lower() == role
            ),
            "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "character_count": len(text.replace("\n", "")),
            "profile_id": str(profile.get("id") or ""),
            "profile_name": str(profile.get("name") or label),
            "provider_id": str(profile.get("provider_id") or "voicebox_tts"),
            "provider_name": str(profile.get("provider_name") or "Haike Video 本地配音"),
            "voice_signature": str(profile.get("voice_signature") or "") or None,
            "role_id": binding.get("role_id"),
            "role_name": binding.get("role_name"),
            "presenter_path": binding.get("presenter_path"),
            "presenter_source": "audio_center_role_binding",
            "presenter_filename": image.name,
            "presenter_sha256": _sha256_file(image),
        }
    return evidence


def _timing_contract() -> dict[str, Any]:
    value = {
        "version": TURN_TIMING_MANIFEST_VERSION,
        "opening_silence_ms": OPENING_SILENCE_MS,
        "turn_trailing_silence_ms": TURN_TRAILING_SILENCE_MS,
        "between_source_silence_ms": BETWEEN_SOURCE_SILENCE_MS,
        "speaker_change_gap_ms": SPEAKER_CHANGE_GAP_MS,
        "same_speaker_gap_ms": SAME_SPEAKER_GAP_MS,
        "video_fps": AVATAR_VIDEO_FPS,
        "audio_format": "PCM16 mono WAV",
        "frame_alignment": "final_role_track_once",
        "frame_formula": "ceil(total_sample_frames / samples_per_video_frame)",
    }
    value["signature"] = _json_hash(value)
    return value


def _audio_contract(state: dict[str, Any]) -> dict[str, Any]:
    """Freeze the exact audible settings covered by the up-front confirmation."""
    narration = wb._ensure_narration_policy(state)
    music = wb._ensure_music_policy(state)
    return {
        "authorization_mode": "upfront_one_click",
        "audio_mix_signature": wb._audio_mix_signature(state),
        "narration_gain_db": wb.clamp_narration_gain_db(narration.get("playback_gain_db")),
        "music_enabled": bool(music.get("enabled")),
        "music_track_id": music.get("track_id"),
        "music_gain_db": wb.clamp_playback_gain_db(music.get("playback_gain_db")),
        "source_start_seconds": round(float(music.get("source_start_seconds") or 0.0), 3),
        "source_end_seconds": (
            round(float(music.get("source_end_seconds")), 3)
            if music.get("source_end_seconds") is not None else None
        ),
    }


def _avatar_recovery_policy(*, plus_48gb_authorized: bool, budget_limit_cny: float) -> dict[str, Any]:
    return {
        "version": AVATAR_RECOVERY_POLICY_VERSION,
        "automatic": True,
        "oom_only": True,
        "standard_max_attempts": 2,
        "plus_max_attempts": 1,
        "plus_48gb_authorized": bool(plus_48gb_authorized),
        "ambiguous_policy": "stop",
        "budget_recheck_each_attempt": True,
        "preserve_completed_roles": True,
        "budget_limit_cny": float(budget_limit_cny),
    }


def _runninghub_preflight(*, allow_plus_on_oom: bool = False) -> dict[str, Any]:
    status = read_runninghub_config()
    issues = list(status.get("issues") or [])
    if not status.get("configured"):
        issues.append("RunningHub 尚未完成安全配置")
    if str(status.get("workflow_id") or "") != PRODUCTION_WORKFLOW_ID:
        issues.append(f"RunningHub 工作流必须为 {PRODUCTION_WORKFLOW_ID}")
    if str(status.get("workflow_profile") or "") != PRODUCTION_WORKFLOW_PROFILE:
        issues.append(f"RunningHub 配置档必须为 {PRODUCTION_WORKFLOW_PROFILE}")
    return {
        "ready": not issues,
        "provider": "RunningHub",
        "workflow_id": PRODUCTION_WORKFLOW_ID,
        "workflow_profile": PRODUCTION_WORKFLOW_PROFILE,
        "resolution": "448x560",
        "fps": AVATAR_VIDEO_FPS,
        "frame_clock": "final_pcm_samples_exact",
        "instance_type": "default",
        "instance_label": "Standard 24GB",
        "plus_allowed": bool(allow_plus_on_oom),
        "plus_fallback_only": True,
        "plus_instance_type": "plus" if allow_plus_on_oom else None,
        "plus_instance_label": "Plus 48GB" if allow_plus_on_oom else None,
        "recovery_sequence": ["default", "default", "plus"] if allow_plus_on_oom else ["default", "default"],
        "max_concurrency": 1,
        "issues": list(dict.fromkeys(str(item) for item in issues if item)),
    }


def avatar_review_preview_preflight(
    project_dir: Path,
    payload: dict[str, Any] | None = None,
    *,
    load_whisper: bool = False,
) -> dict[str, Any]:
    payload = payload or {}
    blockers: list[str] = []
    warnings: list[str] = []
    project = _project(project_dir)
    state = wb.read_workbench(project_dir)
    capabilities = collect_review_preview_capabilities(include_visual_runtime=True)
    try:
        script = _approved_script(project_dir, state)
    except AvatarReviewPreviewError as exc:
        script = {}
        blockers.append(str(exc))
    active_roles = _active_roles(script) if script else ()
    planning_mode = str(((payload.get("visual") or {}).get("planning_mode") or "ai_director"))
    if planning_mode not in {"ai_director", "rule_mix"}:
        blockers.append("画面规划方式只能是 AI 智能导演或规则混合")
    if planning_mode == "ai_director" and not (capabilities.get("text_ai") or {}).get("available"):
        blockers.append("AI 智能导演所需文本模型尚未配置；系统不会自动切换规则模式")
    if not (capabilities.get("pexels") or {}).get("available"):
        blockers.append("Pexels 尚未配置，无法保证主体画面完整")
    for binary, label in (("ffmpeg", "FFmpeg"), ("ffprobe", "ffprobe")):
        if not (capabilities.get(binary) or {}).get("available"):
            blockers.append(f"本机未找到 {label}")
    if not (capabilities.get("hyperframes") or {}).get("available"):
        blockers.append(str((capabilities.get("hyperframes") or {}).get("user_message") or "HyperFrames 当前不可用"))
    budget_limit = float(payload.get("budget_limit_cny") or DEFAULT_BUDGET_LIMIT_CNY)
    allow_plus_on_oom = payload.get("allow_plus_on_oom") is True
    recovery = _avatar_recovery_policy(
        plus_48gb_authorized=allow_plus_on_oom,
        budget_limit_cny=budget_limit,
    )
    runninghub = _runninghub_preflight(allow_plus_on_oom=allow_plus_on_oom)
    blockers.extend(runninghub["issues"])
    try:
        asr = preflight_local_whisper(load_test=load_whisper)
    except AvatarImportError as exc:
        asr = {"status": "blocked", "local_only": True, "load_tested": False}
        blockers.append(str(exc))
    try:
        profiles = _avatar_voice_profiles(payload, roles=active_roles)
        images, presenter_bindings = _resolve_presenter_images(
            project_dir, profiles, refresh_from_role_library=True,
        )
        roles = _role_evidence(script, profiles, images, presenter_bindings) if script else {}
    except Exception as exc:  # noqa: BLE001 - converted to safe Chinese blocker
        images, profiles, presenter_bindings, roles = {}, {}, {}, {}
        blockers.append(str(exc))
    selected_providers = sorted({
        str(profile.get("provider_name") or profile.get("provider_id") or "配音服务")
        for profile in profiles.values()
        if isinstance(profile, dict)
    })
    capabilities["tts"] = {
        "available": bool(active_roles) and len(profiles) == len(active_roles),
        "status": "available" if active_roles and len(profiles) == len(active_roles) else "unavailable",
        "provider": "、".join(selected_providers),
        "user_message": "本次使用：" + "、".join(selected_providers) if selected_providers else "尚未解析脚本主持人音色",
    }
    capabilities["asr"] = asr
    capabilities["avatar"] = {
        "available": runninghub["ready"],
        "status": "available" if runninghub["ready"] else "unavailable",
        "provider": "RunningHub",
        "instance": "Standard 24GB",
        "plus_allowed": allow_plus_on_oom,
        "plus_fallback_only": True,
    }
    script_hash = _json_hash(script) if script else None
    existing = (state.get("automation") or {}).get("review_preview_pipeline") or {}
    if existing.get("pipeline_kind") == "avatar_review_preview" and existing.get("status") in {"queued", "running", "awaiting_human", "ambiguous"}:
        warnings.append(f"已有任务 {existing.get('job_id') or ''} 正在处理或等待人工核对")
    if budget_limit <= 0 or budget_limit > DEFAULT_BUDGET_LIMIT_CNY:
        blockers.append("本轮首次完整执行预算必须大于 0 且不超过 5 元")
    audio_contract = _audio_contract(state)
    return {
        "ready": not blockers,
        "project_id": project_dir.name,
        "project_title": str(project.get("title") or project_dir.name),
        "project_type": ALLOWED_PROJECT_TYPE,
        "script_hash": script_hash,
        "line_count": len(script.get("sections") or []) if script else 0,
        "speaker_count": len(roles),
        "active_roles": list(active_roles),
        "roles": roles,
        "capabilities": capabilities,
        "asr": asr,
        "avatar_contract": runninghub,
        "avatar_recovery": recovery,
        "budget": {
            "limit_cny": budget_limit,
            "absolute_user_limit_cny": ABSOLUTE_BUDGET_LIMIT_CNY,
            "reservation_per_role_cny": ROLE_RESERVATION_CNY,
        },
        "visual_strategy": {
            "planning_mode": planning_mode,
            "label": "AI 智能导演" if planning_mode == "ai_director" else "规则混合",
            "pexels_required": True,
        },
        "visual_generation_required": True,
        "visual_target_scene_count": len(script.get("sections") or []) if script else 0,
        "visual_scope_pending_scene_plan": not bool(state.get("scenes")),
        "music_contract": {
            **audio_contract,
            "enabled": audio_contract["music_enabled"],
            "reason": "声音设置将在本次付费确认中冻结；正常流程不会在配音后暂停试听",
            "will_pause_for_audio_sample": False,
            "requires_human_gate": False,
        },
        "review_gates": ["Whisper 仅作诊断，不覆盖精确帧切点", "最终审核预览需人工观看", "正式发布始终需人工确认"],
        "blockers": list(dict.fromkeys(blockers)),
        "warnings": list(dict.fromkeys(warnings)),
    }


def _mutate(
    project_dir: Path,
    job_id: str,
    worker_token: str | None,
    action: Callable[[dict[str, Any], dict[str, Any]], None],
) -> dict[str, Any]:
    with wb._project_transaction_lock(project_dir):
        state = wb._load_for_write(project_dir)
        job = _pipeline(state)
        if str(job.get("job_id") or "") != str(job_id):
            raise StaleAvatarReviewPreviewWorker("旧 worker 对应的父任务已被替换，拒绝写入")
        if worker_token is not None and str(job.get("worker_token") or "") != str(worker_token):
            raise StaleAvatarReviewPreviewWorker("旧 worker 租约已失效，拒绝写入")
        action(state, job)
        job["updated_at"] = _now()
        wb._save(project_dir, state)
        return deepcopy(job)


def _read_internal(project_dir: Path) -> dict[str, Any]:
    _project(project_dir)
    state = wb.read_workbench(project_dir)
    job = (state.get("automation") or {}).get("review_preview_pipeline")
    if not isinstance(job, dict) or job.get("pipeline_kind") != "avatar_review_preview":
        return _empty_job()
    return deepcopy(job)


def read_avatar_review_preview_job(project_dir: Path) -> dict[str, Any]:
    return _public(_read_internal(project_dir))


def start_avatar_review_preview_job(project_dir: Path, payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("confirmed") is not True:
        raise AvatarReviewPreviewError("请先确认供应商、OOM 有限恢复、实例、预算和人工门，再启动任务")
    preflight = avatar_review_preview_preflight(project_dir, payload, load_whisper=False)
    if preflight["blockers"]:
        raise AvatarReviewPreviewError("预检未通过：" + "；".join(preflight["blockers"]))
    with wb._project_transaction_lock(project_dir):
        state = wb._load_for_write(project_dir)
        current = (state.get("automation") or {}).get("review_preview_pipeline") or {}
        if current.get("status") in {"queued", "running", "awaiting_human", "ambiguous"}:
            if current.get("pipeline_kind") == "avatar_review_preview":
                return _public(current, launch_required=False)
            raise AvatarReviewPreviewConflict("当前项目已有另一条一键审核预览任务，不能并发启动")
        script = _approved_script(project_dir, state)
        active_roles = _active_roles(script)
        profiles = _avatar_voice_profiles(payload, roles=active_roles)
        images, presenter_bindings = _resolve_presenter_images(
            project_dir, profiles, refresh_from_role_library=True,
        )
        roles = _role_evidence(script, profiles, images, presenter_bindings)
        model = preflight["asr"]
        frozen = {
            "project_type": ALLOWED_PROJECT_TYPE,
            "script": script,
            "script_hash": preflight["script_hash"],
            "active_roles": list(active_roles),
            "roles": roles,
            "asr": model,
            "provider": preflight["avatar_contract"],
            "avatar_recovery": preflight["avatar_recovery"],
            "budget_limit_cny": float(preflight["budget"]["limit_cny"]),
            "visual": preflight["visual_strategy"],
            "audio": _audio_contract(state),
            "turn_timing": _timing_contract(),
            "versions": {
                "pipeline": PIPELINE_VERSION,
                "avatar_package": "1.0",
                "visual_contract": "review-preview-shared-v1",
            },
        }
        request_fingerprint = _json_hash(frozen)
        job_id = f"ARP-{uuid4().hex[:16]}"
        job = _empty_job()
        job.update({
            "job_id": job_id,
            "script_hash": preflight["script_hash"],
            "input_fingerprint": request_fingerprint,
            "request_fingerprint": request_fingerprint,
            "status": "queued",
            "stage": "preflight",
            "current": {"kind": "stage", "id": "preflight", "label": "已冻结输入，等待执行付费前硬预检"},
            "safe_resume_point": "preflight",
            "frozen_input": frozen,
            "preflight": preflight,
            "created_at": _now(),
            "updated_at": _now(),
        })
        job["budget"]["limit"] = float(preflight["budget"]["limit_cny"])
        wb._automation(state)["review_preview_pipeline"] = job
        wb._activity(state, "avatar_review_preview_started", f"有数字人一键审核预览任务 {job_id} 已建立；终点仅为待审预览", job_id=job_id)
        wb._save(project_dir, state)
        return _public(job, launch_required=True)


def _assert_frozen(project_dir: Path, job: dict[str, Any], *, load_whisper: bool = False) -> dict[str, Any]:
    project = _project(project_dir)
    state = wb.read_workbench(project_dir)
    frozen = job.get("frozen_input") or {}
    if frozen.get("turn_timing") != _timing_contract():
        raise AvatarInputDriftError("逐轮配音静音合同或清单版本已变化；请启动新任务")
    if frozen.get("audio") != _audio_contract(state):
        raise AvatarInputDriftError("人声、背景音乐或音乐区间在确认后发生变化；请启动新任务重新确认")
    if project.get("pipeline_type") != frozen.get("project_type"):
        raise AvatarInputDriftError("项目类型在任务启动后发生变化，拒绝继续")
    script = _approved_script(project_dir, state)
    if _json_hash(script) != frozen.get("script_hash"):
        raise AvatarInputDriftError("正式脚本在任务启动后发生变化；请创建新的审核预览任务")
    frozen_profile_ids = {
        role: str(expected.get("profile_id") or "")
        for role, expected in (frozen.get("roles") or {}).items()
        if isinstance(expected, dict)
    }
    active_roles = tuple(
        role for role in frozen.get("active_roles") or tuple(frozen_profile_ids)
        if role in ROLE_LABELS
    )
    if not active_roles:
        raise AvatarInputDriftError("冻结任务缺少有效主持人清单；请创建新的审核预览任务")
    if tuple(_active_roles(script)) != active_roles:
        raise AvatarInputDriftError("正式脚本中的主持人集合在任务启动后发生变化；请创建新的审核预览任务")
    profiles = _avatar_voice_profiles({"voice_profiles": frozen_profile_ids}, roles=active_roles)
    images, presenter_bindings = _resolve_presenter_images(
        project_dir, profiles, refresh_from_role_library=False,
    )
    current_roles = _role_evidence(script, profiles, images, presenter_bindings)
    for role, expected in (frozen.get("roles") or {}).items():
        current = current_roles.get(role) or {}
        for key in ("text_sha256", "profile_id", "provider_id", "voice_signature", "presenter_sha256", "role_id"):
            if str(current.get(key) or "") != str(expected.get(key) or ""):
                raise AvatarInputDriftError(f"{ROLE_LABELS.get(role, role)} 的台词、音色或角色图已变化；请启动新任务")
    recovery = frozen.get("avatar_recovery") or _avatar_recovery_policy(
        plus_48gb_authorized=False,
        budget_limit_cny=float(frozen.get("budget_limit_cny") or DEFAULT_BUDGET_LIMIT_CNY),
    )
    provider = _runninghub_preflight(
        allow_plus_on_oom=recovery.get("plus_48gb_authorized") is True,
    )
    if not provider["ready"]:
        raise AvatarInputDriftError("RunningHub 配置已变化：" + "；".join(provider["issues"]))
    expected_provider = frozen.get("provider") or {}
    for key in ("workflow_id", "workflow_profile", "resolution", "fps"):
        if str(provider.get(key) or "") != str(expected_provider.get(key) or ""):
            raise AvatarInputDriftError("RunningHub 生产合同在任务启动后发生变化；付费任务未继续")
    asr = preflight_local_whisper(load_test=load_whisper)
    expected_asr = frozen.get("asr") or {}
    for key in ("model_id", "snapshot_revision", "fingerprint"):
        if str(asr.get(key) or "") != str(expected_asr.get(key) or ""):
            raise AvatarInputDriftError("本地 Whisper 模型在任务启动后发生变化；付费任务未继续")
    return {"script": script, "profiles": profiles, "images": images, "asr": asr}


def _phase_begin(project_dir: Path, job_id: str, worker_token: str, stage: str, label: str) -> dict[str, Any]:
    def mutate(_state: dict[str, Any], job: dict[str, Any]) -> None:
        phase = job.setdefault("phases", {}).setdefault(stage, {})
        phase.update({
            "status": "running",
            "attempts": int(phase.get("attempts") or 0) + 1,
            "started_at": phase.get("started_at") or _now(),
            "finished_at": None,
            "error": None,
            "retryable": True,
            "safe_resume_point": stage,
        })
        job["stage"] = stage
        job["safe_resume_point"] = stage
        job["current"] = {"kind": "stage", "id": stage, "label": label}
    return _mutate(project_dir, job_id, worker_token, mutate)


def _phase_complete(
    project_dir: Path,
    job_id: str,
    worker_token: str,
    stage: str,
    next_stage: str,
    output: dict[str, Any] | None = None,
) -> dict[str, Any]:
    def mutate(_state: dict[str, Any], job: dict[str, Any]) -> None:
        phase = job.setdefault("phases", {}).setdefault(stage, {})
        phase.update({
            "status": "completed", "finished_at": _now(), "error": None,
            "retryable": False, "safe_resume_point": next_stage,
            "output": deepcopy(output or phase.get("output") or {}),
        })
        job["stage"] = next_stage
        job["safe_resume_point"] = next_stage
        job["counts"]["completed"] = sum(1 for item in job.get("phases", {}).values() if item.get("status") == "completed")
        job["current"] = {"kind": "stage", "id": next_stage, "label": next_stage}
    return _mutate(project_dir, job_id, worker_token, mutate)


def _fail(project_dir: Path, job_id: str, worker_token: str, error: Exception) -> dict[str, Any]:
    ambiguous = isinstance(error, AmbiguousAvatarOperation)
    nonretryable = ambiguous or isinstance(error, (AvatarInputDriftError, NonRetryableAvatarOperation))

    def mutate(state: dict[str, Any], job: dict[str, Any]) -> None:
        stage = str(job.get("stage") or "preflight")
        phase = job.setdefault("phases", {}).setdefault(stage, {})
        phase.update({
            "status": "failed", "finished_at": _now(), "error": str(error),
            "retryable": not nonretryable, "safe_resume_point": None if nonretryable else stage,
        })
        job.update({
            "status": "ambiguous" if ambiguous else "failed",
            "error": {
                "message": str(error),
                "type": "ambiguous_external_operation" if ambiguous else type(error).__name__,
                "retryable": not nonretryable,
            },
            "safe_resume_point": None if nonretryable else stage,
            "worker_token": None,
            "finished_at": _now(),
        })
        job["counts"]["failed"] = 1
        wb._activity(state, "avatar_review_preview_failed", f"有数字人审核预览停在 {stage}：{error}", job_id=job_id)
    return _mutate(project_dir, job_id, worker_token, mutate)


def _voice_output_path(project_dir: Path, role: str) -> Path:
    return project_dir / VOICE_DIRECTORY / f"{role}-longform.wav"


def _turn_voice_output_path(project_dir: Path, turn_id: str, role: str) -> Path:
    return project_dir / VOICE_DIRECTORY / "turns" / f"{turn_id}-{role}.wav"


def _wav_facts(path: Path) -> dict[str, Any]:
    try:
        clock = inspect_frame_clock_wav(path, fps=AVATAR_VIDEO_FPS)
        return {
            "channels": clock["channels"],
            "sample_width": clock["sample_width"],
            "sample_rate": clock["sample_rate"],
            "frame_count": clock["sample_frame_count"],
            "sample_frame_count": clock["sample_frame_count"],
            "duration_seconds": clock["duration_seconds"],
        }
    except AvatarAudioClockError as exc:
        raise AvatarReviewPreviewError(f"轮次配音不是可拼接的 PCM WAV：{path.name}") from exc


def _compose_role_track(project_dir: Path, role: str, records: list[dict[str, Any]]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if not records:
        raise AvatarReviewPreviewError(f"{ROLE_LABELS.get(role, role)} 没有可拼接的轮次配音")
    facts = [_wav_facts(project_dir / record["path"]) for record in records]
    audio_format = (facts[0]["channels"], facts[0]["sample_width"], facts[0]["sample_rate"])
    if any((item["channels"], item["sample_width"], item["sample_rate"]) != audio_format for item in facts):
        raise AvatarReviewPreviewError(f"{ROLE_LABELS.get(role, role)} 的轮次 WAV 格式不一致，拒绝隐式重采样")
    channels, sample_width, sample_rate = audio_format
    opening_frames = round(sample_rate * OPENING_SILENCE_MS / 1000)
    trailing_frames = round(sample_rate * TURN_TRAILING_SILENCE_MS / 1000)
    later_leading_frames = round(sample_rate * (BETWEEN_SOURCE_SILENCE_MS - TURN_TRAILING_SILENCE_MS) / 1000)
    silence_frame = b"\0" * channels * sample_width
    target = _voice_output_path(project_dir, role)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".wav.tmp")
    manifest_turns: list[dict[str, Any]] = []
    cursor = 0
    with wave.open(str(temporary), "wb") as output:
        output.setnchannels(channels)
        output.setsampwidth(sample_width)
        output.setframerate(sample_rate)
        for index, (record, fact) in enumerate(zip(records, facts)):
            leading_frames = opening_frames if index == 0 else later_leading_frames
            source_start = cursor
            output.writeframesraw(silence_frame * leading_frames)
            cursor += leading_frames
            speech_start = cursor
            with wave.open(str(project_dir / record["path"]), "rb") as source:
                audio = source.readframes(source.getnframes())
            output.writeframesraw(audio)
            cursor += fact["frame_count"]
            speech_end = cursor
            output.writeframesraw(silence_frame * trailing_frames)
            cursor += trailing_frames
            manifest_turns.append({
                **record,
                "sample_rate": sample_rate,
                "natural_source_start_sample": source_start,
                "speech_start_sample": speech_start,
                "speech_end_sample": speech_end,
                "natural_source_end_sample": cursor,
                "raw_speech_duration_seconds": round(fact["duration_seconds"], 6),
                "speech_start_seconds": round(speech_start / sample_rate, 6),
                "speech_end_seconds": round(speech_end / sample_rate, 6),
                "silence": {
                    "planned_leading_ms": round(leading_frames * 1000 / sample_rate, 3),
                    "planned_trailing_ms": round(trailing_frames * 1000 / sample_rate, 3),
                    "between_source_minimum_ms": BETWEEN_SOURCE_SILENCE_MS,
                },
            })
    temporary.replace(target)
    try:
        clock = align_pcm_wav_to_frame_clock(target, fps=AVATAR_VIDEO_FPS)
    except AvatarAudioClockError as exc:
        raise AvatarReviewPreviewError(str(exc)) from exc
    samples_per_frame = int(clock["samples_per_video_frame"])
    video_frame_count = int(clock["video_frame_count"])
    for index, turn in enumerate(manifest_turns):
        if index == 0:
            source_start_frame = 0
        else:
            source_start_frame = nearest_video_frame(
                int(turn["natural_source_start_sample"]), samples_per_frame,
            )
        if index + 1 < len(manifest_turns):
            source_end_frame = nearest_video_frame(
                int(manifest_turns[index + 1]["natural_source_start_sample"]),
                samples_per_frame,
            )
        else:
            source_end_frame = video_frame_count
        source_start_sample = source_start_frame * samples_per_frame
        source_end_sample = source_end_frame * samples_per_frame
        if not (
            source_start_sample <= int(turn["speech_start_sample"])
            < int(turn["speech_end_sample"]) <= source_end_sample
        ):
            raise AvatarReviewPreviewError(
                f"{turn['turn_id']} 的帧对齐切点越过了有效语音；拒绝生成可能截字的数字人"
            )
        turn.update({
            "source_start_sample": source_start_sample,
            "source_end_sample": source_end_sample,
            "source_start_frame": source_start_frame,
            "source_end_frame_exclusive": source_end_frame,
            "source_start_seconds": round(source_start_frame / AVATAR_VIDEO_FPS, 6),
            "source_end_seconds": round(source_end_frame / AVATAR_VIDEO_FPS, 6),
        })
        turn["silence"].update({
            "leading_ms": round(
                (int(turn["speech_start_sample"]) - source_start_sample) * 1000 / sample_rate,
                3,
            ),
            "trailing_ms": round(
                (source_end_sample - int(turn["speech_end_sample"])) * 1000 / sample_rate,
                3,
            ),
        })
    return ({
        "status": "completed",
        "path": str(target.relative_to(project_dir)).replace("\\", "/"),
        "duration_seconds": round(float(clock["duration_seconds"]), 6),
        "content_duration_seconds": round(cursor / sample_rate, 6),
        "sample_rate": sample_rate,
        "channels": channels,
        "sample_width": sample_width,
        "content_sample_frames": int(clock["content_sample_frames"]),
        "final_padding_sample_frames": int(clock["final_padding_sample_frames"]),
        "sample_frame_count": int(clock["sample_frame_count"]),
        "samples_per_video_frame": samples_per_frame,
        "video_fps": AVATAR_VIDEO_FPS,
        "video_frame_count": video_frame_count,
        "sha256": _sha256_file(target),
        "turn_count": len(records),
        "completed_at": _now(),
    }, manifest_turns)


def _generate_voice_tracks(
    project_dir: Path,
    job_id: str,
    worker_token: str,
    context: dict[str, Any],
    *,
    tts_factory: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    script = context["script"]
    profiles = context["profiles"]
    output: dict[str, Any] = deepcopy(((_read_internal(project_dir).get("phases") or {}).get("voice") or {}).get("output") or {})
    roles = output.setdefault("roles", {})
    turns = output.setdefault("turns", {})
    timing_contract = ((_read_internal(project_dir).get("frozen_input") or {}).get("turn_timing") or {})
    sections = [item for item in script.get("sections") or [] if isinstance(item, dict)]
    tts = tts_factory() if tts_factory is not None else None
    for section in sections:
        turn_id = str(section["turn_id"]).upper()
        role = str(section["speaker_id"]).lower()
        label = ROLE_LABELS[role]
        text = str(section["text"]).strip()
        profile = profiles[role]
        target = _turn_voice_output_path(project_dir, turn_id, role)
        text_sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
        provider_id = str(profile.get("provider_id") or "voicebox_tts")
        voice_signature = str(profile.get("voice_signature") or "") or _json_hash({
            "profile_id": profile["id"], "profile_name": profile.get("name") or label,
            "provider_id": provider_id,
        })
        turn_signature = _json_hash({
            "turn_id": turn_id,
            "speaker_id": role,
            "text_sha256": text_sha256,
            "profile_id": profile["id"],
            "provider_id": provider_id,
            "voice_signature": voice_signature,
            "timing_signature": timing_contract.get("signature"),
        })
        record = turns.get(turn_id) or {}
        reusable = record.get("status") == "completed" and record.get("input_signature") == turn_signature and target.is_file() and record.get("wav_sha256") == _sha256_file(target)
        if reusable:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if tts is None:
            result = generate_voice_audio(
                text=text,
                profile=profile,
                output_path=target,
                language="zh",
            )
        else:
            result = tts.execute({
                "text": text,
                "profile_id": profile["id"],
                "profile_name": profile.get("name") or label,
                "language": "zh",
                "output_path": str(target),
                "timeout_seconds": 7200,
                "poll_seconds": 3,
            })
        if getattr(result, "success", None) is False or not target.is_file():
            raise AvatarReviewPreviewError(str(getattr(result, "error", None) or f"{label}本地配音失败"))
        record = {
            "status": "completed",
            "turn_id": turn_id,
            "speaker_id": role,
            "path": str(target.relative_to(project_dir)).replace("\\", "/"),
            "text_sha256": text_sha256,
            "profile_id": profile["id"],
            "profile_name": profile.get("name") or label,
            "provider_id": provider_id,
            "provider_name": profile.get("provider_name") or "Haike Video 本地配音",
            "voice_signature": voice_signature,
            "input_signature": turn_signature,
            "wav_sha256": _sha256_file(target),
            "completed_at": _now(),
        }
        _wav_facts(target)
        turns[turn_id] = record

        def persist(_state: dict[str, Any], job: dict[str, Any], snapshot=deepcopy(output), current_label=label, current_turn=turn_id) -> None:
            job.setdefault("phases", {}).setdefault("voice", {})["output"] = snapshot
            job["current"] = {"kind": "turn", "id": current_turn, "label": f"{current_turn} {current_label}配音已完成"}
        _mutate(project_dir, job_id, worker_token, persist)
    manifest = {"version": TURN_TIMING_MANIFEST_VERSION, "contract": timing_contract, "roles": {}, "turns": []}
    for role in _active_roles(script):
        label = ROLE_LABELS[role]
        role_records = [turns[str(item["turn_id"]).upper()] for item in sections if str(item["speaker_id"]).lower() == role]
        role_record, timed_turns = _compose_role_track(project_dir, role, role_records)
        expected = ((_read_internal(project_dir).get("frozen_input") or {}).get("roles") or {}).get(role) or {}
        roles[role] = {
            **role_record,
            "text_sha256": expected.get("text_sha256"),
            "profile_id": expected.get("profile_id"),
            "profile_name": expected.get("profile_name"),
            "provider_id": expected.get("provider_id"),
            "provider_name": expected.get("provider_name"),
            "voice_signature": expected.get("voice_signature"),
        }
        manifest["roles"][role] = {
            key: role_record[key]
            for key in (
                "path", "sha256", "duration_seconds", "content_duration_seconds",
                "sample_rate", "channels", "sample_width", "content_sample_frames",
                "final_padding_sample_frames", "sample_frame_count",
                "samples_per_video_frame", "video_fps", "video_frame_count",
            )
        }
        manifest["turns"].extend(timed_turns)
    order = {str(item["turn_id"]).upper(): index for index, item in enumerate(sections)}
    manifest["turns"].sort(key=lambda item: order[item["turn_id"]])
    manifest["input_signature"] = _json_hash({
        "contract": timing_contract,
        "roles": manifest["roles"],
        "turns": [
            {key: item[key] for key in (
                "turn_id", "speaker_id", "text_sha256", "voice_signature", "wav_sha256",
                "source_start_sample", "source_end_sample", "source_start_frame",
                "source_end_frame_exclusive",
            )}
            for item in manifest["turns"]
        ],
    })
    manifest_path = project_dir / VOICE_DIRECTORY / "timing-manifest.json"
    manifest_temporary = manifest_path.with_suffix(".json.tmp")
    manifest_temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    manifest_temporary.replace(manifest_path)
    output["timing_manifest"] = {
        "version": manifest["version"],
        "path": str(manifest_path.relative_to(project_dir)).replace("\\", "/"),
        "sha256": _sha256_file(manifest_path),
        "input_signature": manifest["input_signature"],
        "contract": manifest["contract"],
        "roles": manifest["roles"],
        "turns": manifest["turns"],
    }
    return output


def _budget_committed(job: dict[str, Any]) -> float:
    budget = job.get("budget") or {}
    return round(float(budget.get("reserved") or 0) + float(budget.get("spent") or 0), 4)


def _reserve_budget(
    project_dir: Path,
    job_id: str,
    worker_token: str,
    operation_id: str,
    purpose: str,
    *,
    requested_instance: str,
) -> None:
    def mutate(_state: dict[str, Any], job: dict[str, Any]) -> None:
        operations = job.setdefault("paid_operations", {})
        existing = operations.get(operation_id)
        if existing and existing.get("state") != "released":
            return
        budget = job["budget"]
        if _budget_committed(job) + ROLE_RESERVATION_CNY > float(budget["limit"]) + 1e-9:
            raise AvatarBudgetBlockedError(f"提交 {purpose} 将超过 {float(budget['limit']):g} 元预算，已停止")
        budget["reserved"] = round(float(budget.get("reserved") or 0) + ROLE_RESERVATION_CNY, 4)
        budget.setdefault("entries", []).append({
            "at": _now(), "type": "reserve", "provider": "runninghub", "purpose": purpose,
            "amount": ROLE_RESERVATION_CNY, "operation_id": operation_id,
        })
        operation = existing or {
            "operation_id": operation_id, "provider": "runninghub", "history": [],
        }
        previous = str(operation.get("state") or "planned")
        operation.update({
            "state": "reserved", "purpose": purpose, "reserved_cny": ROLE_RESERVATION_CNY,
            "requested_instance": requested_instance, "settled": False,
        })
        operation.setdefault("history", []).append({"at": _now(), "from": previous, "to": "reserved"})
        operations[operation_id] = operation
    _mutate(project_dir, job_id, worker_token, mutate)


def _transition_operation(
    project_dir: Path,
    job_id: str,
    worker_token: str,
    operation_id: str,
    state_value: str,
    **fields: Any,
) -> dict[str, Any]:
    def mutate(_state: dict[str, Any], job: dict[str, Any]) -> None:
        operation = job.setdefault("paid_operations", {}).setdefault(operation_id, {"operation_id": operation_id, "history": []})
        previous = str(operation.get("state") or "planned")
        operation.update(fields)
        operation["state"] = state_value
        operation["updated_at"] = _now()
        operation.setdefault("history", []).append({"at": _now(), "from": previous, "to": state_value})
    return _mutate(project_dir, job_id, worker_token, mutate)


def _release_budget(project_dir: Path, job_id: str, worker_token: str, operation_id: str, reason: str) -> None:
    def mutate(_state: dict[str, Any], job: dict[str, Any]) -> None:
        operation = (job.get("paid_operations") or {}).get(operation_id) or {}
        if operation.get("state") == "released":
            return
        amount = float(operation.get("reserved_cny") or 0)
        budget = job["budget"]
        budget["reserved"] = round(max(0.0, float(budget.get("reserved") or 0) - amount), 4)
        budget.setdefault("entries", []).append({
            "at": _now(), "type": "release", "provider": "runninghub", "amount": amount,
            "operation_id": operation_id, "reason": reason[:300],
        })
        operation.update({"state": "released", "released_at": _now(), "reason": reason[:300], "reserved_cny": 0.0})
    _mutate(project_dir, job_id, worker_token, mutate)


def _settle_budget(
    project_dir: Path,
    job_id: str,
    worker_token: str,
    operation_id: str,
    result: dict[str, Any],
    elapsed_seconds: float,
) -> float:
    exact = result.get("consume_money_cny")
    existing = ((_read_internal(project_dir).get("paid_operations") or {}).get(operation_id) or {})
    if existing.get("settled"):
        if not isinstance(existing.get("actual_cost_cny"), (int, float)):
            raise AvatarBudgetBlockedError("RunningHub 付费操作已结算但缺少金额，账本需要人工核对")
        return float(existing["actual_cost_cny"])
    over_limit = {"value": False, "limit": 0.0, "spent": 0.0}

    def mutate(_state: dict[str, Any], job: dict[str, Any]) -> None:
        operation = (job.get("paid_operations") or {}).get(operation_id) or {}
        if operation.get("settled"):
            return
        reserved = float(operation.get("reserved_cny") or 0)
        requested_instance = str(operation.get("requested_instance") or "default")
        rate = PLUS_RATE_CNY_PER_HOUR if requested_instance == "plus" else STANDARD_RATE_CNY_PER_HOUR
        actual = float(exact) if isinstance(exact, (int, float)) else round(
            min(reserved, max(0.01, elapsed_seconds * rate / 3600.0)), 4
        )
        budget = job["budget"]
        budget["reserved"] = round(max(0.0, float(budget.get("reserved") or 0) - reserved), 4)
        next_spent = round(float(budget.get("spent") or 0) + actual, 4)
        budget["spent"] = next_spent
        exceeded = next_spent > float(budget["limit"]) + 1e-9
        if exceeded:
            budget["over_limit"] = {
                "detected_at": _now(), "spent_cny": next_spent,
                "limit_cny": float(budget["limit"]), "operation_id": operation_id,
            }
            over_limit.update({"value": True, "limit": float(budget["limit"]), "spent": next_spent})
        budget.setdefault("entries", []).append({
            "at": _now(), "type": "settle", "provider": "runninghub", "actual": actual,
            "reserved": reserved, "operation_id": operation_id, "task_id": operation.get("task_id"),
            "over_limit": exceeded,
        })
        operation.update({
            "actual_cost_cny": actual,
            "cost_source": "provider" if isinstance(exact, (int, float)) else "conservative_elapsed_estimate",
            "fallback_rate_cny_per_hour": rate,
            "settled": True, "reserved_cny": 0.0,
        })
    updated = _mutate(project_dir, job_id, worker_token, mutate)
    operation = (updated.get("paid_operations") or {}).get(operation_id) or {}
    if over_limit["value"]:
        raise AvatarBudgetBlockedError(
            f"RunningHub 实际累计费用 {over_limit['spent']:.4f} 元超过冻结上限 "
            f"{over_limit['limit']:.4f} 元；事实账本已保存，禁止后续角色"
        )
    return float(operation.get("actual_cost_cny") or 0)


def _avatar_records(project_dir: Path) -> dict[str, Any]:
    phase = ((_read_internal(project_dir).get("phases") or {}).get("avatar_generation") or {})
    return deepcopy((phase.get("output") or {}).get("roles") or {})


def _persist_avatar_records(
    project_dir: Path,
    job_id: str,
    worker_token: str,
    records: dict[str, Any],
    label: str,
) -> None:
    def mutate(_state: dict[str, Any], job: dict[str, Any]) -> None:
        job.setdefault("phases", {}).setdefault("avatar_generation", {}).setdefault("output", {})["roles"] = deepcopy(records)
        job["current"] = {"kind": "provider", "id": "runninghub", "label": label}
    _mutate(project_dir, job_id, worker_token, mutate)


def _persist_avatar_submission(
    project_dir: Path,
    job_id: str,
    worker_token: str,
    records: dict[str, Any],
    *,
    operation_id: str,
    task_id: str,
    requested_instance: str,
    label: str,
) -> None:
    """Atomically persist the remote task identity and owning role record."""
    def mutate(_state: dict[str, Any], job: dict[str, Any]) -> None:
        operation = job.setdefault("paid_operations", {}).setdefault(
            operation_id,
            {"operation_id": operation_id, "history": []},
        )
        previous = str(operation.get("state") or "planned")
        operation.update({
            "state": "submitted", "task_id": task_id,
            "requested_instance": requested_instance, "updated_at": _now(),
        })
        operation.setdefault("history", []).append({
            "at": _now(), "from": previous, "to": "submitted",
        })
        job.setdefault("phases", {}).setdefault("avatar_generation", {}).setdefault("output", {})[
            "roles"
        ] = deepcopy(records)
        job["current"] = {"kind": "provider", "id": "runninghub", "label": label}

    _mutate(project_dir, job_id, worker_token, mutate)


def _validate_exact_clock_avatar_output(
    media: dict[str, Any],
    *,
    exact_total_frames: int,
    expected_sample_rate: int,
    label: str,
) -> dict[str, Any]:
    """Close the loop between the submitted PCM clock and downloaded MP4."""
    expected_duration = exact_total_frames / AVATAR_VIDEO_FPS
    video = media.get("video") if isinstance(media.get("video"), dict) else {}
    audio = media.get("audio") if isinstance(media.get("audio"), dict) else {}
    mismatches: list[str] = []
    if not video.get("present") or not audio.get("present"):
        mismatches.append("缺少视频流或原声音频流")
    if (int(video.get("width") or 0), int(video.get("height") or 0)) != (448, 560):
        mismatches.append(f"画面规格为 {int(video.get('width') or 0)}x{int(video.get('height') or 0)}，预期 448x560")
    if abs(float(video.get("fps") or 0) - AVATAR_VIDEO_FPS) > 1e-6:
        mismatches.append(f"视频帧率为 {float(video.get('fps') or 0):g}，预期 {AVATAR_VIDEO_FPS}")
    actual_frames = int(video.get("frame_count") or 0)
    if actual_frames != exact_total_frames:
        mismatches.append(f"视频实际 {actual_frames} 帧，预期 {exact_total_frames} 帧")
    for stream_label, actual_duration in (
        ("容器", float(media.get("duration_seconds") or 0)),
        ("视频流", float(video.get("duration_seconds") or 0)),
        ("音频流", float(audio.get("duration_seconds") or 0)),
    ):
        if abs(actual_duration - expected_duration) > 0.001:
            mismatches.append(f"{stream_label}时长 {actual_duration:.6f} 秒，预期 {expected_duration:.6f} 秒")
    if int(audio.get("sample_rate") or 0) != expected_sample_rate:
        mismatches.append(f"输出音频采样率为 {int(audio.get('sample_rate') or 0)}，预期 {expected_sample_rate}")
    if int(audio.get("channels") or 0) != 1:
        mismatches.append(f"输出音频声道数为 {int(audio.get('channels') or 0)}，预期单声道")
    if mismatches:
        raise AvatarInputDriftError(f"{label}数字人输出没有遵守精确帧合同：{'；'.join(mismatches)}")
    return {
        "status": "passed",
        "expected_frames": exact_total_frames,
        "actual_frames": actual_frames,
        "fps": AVATAR_VIDEO_FPS,
        "expected_duration_seconds": round(expected_duration, 6),
        "video_duration_seconds": round(float(video.get("duration_seconds") or 0), 6),
        "audio_duration_seconds": round(float(audio.get("duration_seconds") or 0), 6),
        "sample_rate": int(audio.get("sample_rate") or 0),
        "channels": int(audio.get("channels") or 0),
    }


def _verified_voice_timing_manifest(project_dir: Path, voice_output: dict[str, Any]) -> dict[str, Any]:
    metadata = voice_output.get("timing_manifest") or {}
    relative = Path(str(metadata.get("path") or ""))
    manifest_path = relative.resolve() if relative.is_absolute() else (project_dir / relative).resolve()
    if not manifest_path.is_relative_to(project_dir.resolve()) or not manifest_path.is_file():
        raise AvatarInputDriftError("逐轮时间清单不存在或越出当前项目")
    manifest_sha256 = _sha256_file(manifest_path)
    if manifest_sha256 != str(metadata.get("sha256") or ""):
        raise AvatarInputDriftError("逐轮时间清单内容已变化；禁止继续使用旧尾部静音边界")
    manifest = _read_json(manifest_path)
    if (
        str(manifest.get("version") or "") != TURN_TIMING_MANIFEST_VERSION
        or str(manifest.get("input_signature") or "") != str(metadata.get("input_signature") or "")
    ):
        raise AvatarInputDriftError("逐轮时间清单版本或输入签名已变化")
    voice_roles = voice_output.get("roles") if isinstance(voice_output.get("roles"), dict) else {}
    active_roles = tuple(role for role in ROLE_LABELS if role in voice_roles)
    if not active_roles:
        raise AvatarInputDriftError("逐轮时间清单没有有效主持人音频，不能继续")
    for role in active_roles:
        persisted_role = (manifest.get("roles") or {}).get(role) or {}
        phase_role = (voice_output.get("roles") or {}).get(role) or {}
        for key in ("path", "sha256", "sample_rate", "sample_frame_count", "video_frame_count"):
            if str(persisted_role.get(key) or "") != str(phase_role.get(key) or ""):
                raise AvatarInputDriftError(f"{ROLE_LABELS[role]}时间清单与配音阶段账本不一致")
        role_turns = [
            item for item in (manifest.get("turns") or [])
            if isinstance(item, dict) and str(item.get("speaker_id") or "").lower() == role
        ]
        if len(role_turns) != int(phase_role.get("turn_count") or 0):
            raise AvatarInputDriftError(f"{ROLE_LABELS[role]}时间清单轮次数与配音阶段账本不一致")
        cursor_sample = 0
        cursor_frame = 0
        samples_per_frame = int(persisted_role.get("samples_per_video_frame") or 0)
        for turn in role_turns:
            start_sample = int(turn.get("source_start_sample") or 0)
            end_sample = int(turn.get("source_end_sample") or 0)
            start_frame = int(turn.get("source_start_frame") or 0)
            end_frame = int(turn.get("source_end_frame_exclusive") or 0)
            speech_end_sample = int(turn.get("speech_end_sample") or 0)
            if (
                samples_per_frame <= 0
                or start_sample != cursor_sample
                or start_frame != cursor_frame
                or start_sample != start_frame * samples_per_frame
                or end_sample != end_frame * samples_per_frame
                or speech_end_sample > end_sample
                or end_sample <= start_sample
            ):
                raise AvatarInputDriftError(f"{ROLE_LABELS[role]}时间清单不是连续的整数帧区间")
            cursor_sample = end_sample
            cursor_frame = end_frame
        if (
            cursor_sample != int(persisted_role.get("sample_frame_count") or 0)
            or cursor_frame != int(persisted_role.get("video_frame_count") or 0)
        ):
            raise AvatarInputDriftError(f"{ROLE_LABELS[role]}时间清单末端没有覆盖完整冻结音频")
    manifest["sha256"] = manifest_sha256
    return manifest


def _validate_one_click_avatar_duration(project_dir: Path, voice_output: dict[str, Any]) -> dict[str, Any]:
    """Block overlong exact-frame plans before any new RunningHub submission."""
    manifest = _verified_voice_timing_manifest(project_dir, voice_output)
    roles = manifest.get("roles") or {}
    turns = manifest.get("turns") or []
    if not isinstance(roles, dict) or not isinstance(turns, list) or not turns:
        raise AvatarInputDriftError("逐轮时间清单不完整，不能提交数字人任务")
    role_duration = 0.0
    active_roles = tuple(role for role in ROLE_LABELS if role in roles)
    if not active_roles:
        raise AvatarInputDriftError("逐轮时间清单没有有效主持人，不能提交数字人任务")
    for role in active_roles:
        record = roles.get(role) or {}
        try:
            frames = int(record.get("video_frame_count") or 0)
        except (TypeError, ValueError) as exc:
            raise AvatarInputDriftError(f"{ROLE_LABELS[role]}精确帧时长账本无效") from exc
        if frames <= 0:
            raise AvatarInputDriftError(f"{ROLE_LABELS[role]}精确帧时长账本为空")
        role_duration += frames / AVATAR_VIDEO_FPS
    gap_duration = 0.0
    previous_role: str | None = None
    for turn in turns:
        if not isinstance(turn, dict):
            raise AvatarInputDriftError("逐轮时间清单包含无效轮次")
        role = str(turn.get("speaker_id") or "").lower()
        if role not in ROLE_LABELS:
            raise AvatarInputDriftError("逐轮时间清单包含未知角色")
        if previous_role is not None:
            gap_duration += (SAME_SPEAKER_GAP_MS if role == previous_role else SPEAKER_CHANGE_GAP_MS) / 1000
        previous_role = role
    planned = role_duration + gap_duration
    if planned > ONE_CLICK_AVATAR_MAX_DURATION_SECONDS + 1e-6:
        raise AvatarReviewPreviewError(
            f"精确帧音频计划约 {planned:.2f} 秒，超过有数字人一键生成 "
            f"{ONE_CLICK_AVATAR_MAX_DURATION_SECONDS:.0f} 秒安全上限；未提交 RunningHub"
        )
    duration_plan = {
        "status": "passed",
        "planned_master_seconds": round(planned, 6),
        "role_track_seconds": round(role_duration, 6),
        "inter_turn_gap_seconds": round(gap_duration, 6),
        "maximum_seconds": ONE_CLICK_AVATAR_MAX_DURATION_SECONDS,
        "timing_manifest_sha256": manifest["sha256"],
    }
    voice_output["avatar_duration_plan"] = duration_plan
    return duration_plan


def _verify_contiguous_video_prefix(path: Path, expected_frames: int) -> dict[str, Any]:
    ffprobe = _find_media_binary("ffprobe")
    if not ffprobe:
        raise AvatarReviewPreviewError("需要核验供应商逐帧时间戳，但未发现 ffprobe")
    result = _run_media_command([
        ffprobe, "-v", "error", "-select_streams", "v:0",
        "-show_frames", "-show_entries", "frame=best_effort_timestamp_time",
        "-of", "csv=p=0", str(path),
    ])
    if result.returncode != 0:
        raise AvatarReviewPreviewError(f"ffprobe 无法核验供应商逐帧时间戳：{(result.stderr or '')[-1000:]}")
    timestamps: list[float] = []
    for raw_line in (result.stdout or "").splitlines():
        token = raw_line.strip().split(",", 1)[0]
        if not token:
            continue
        try:
            timestamps.append(float(token))
        except ValueError as exc:
            raise AvatarInputDriftError("供应商视频返回了无法解析的逐帧时间戳") from exc
    if len(timestamps) != expected_frames:
        raise AvatarInputDriftError(
            f"供应商视频逐帧时间戳数量 {len(timestamps)} 与回读帧数 {expected_frames} 不一致"
        )
    tolerance = 0.001
    for index, timestamp in enumerate(timestamps):
        expected = index / AVATAR_VIDEO_FPS
        if abs(timestamp - expected) > tolerance:
            raise AvatarInputDriftError(
                f"供应商视频第 {index + 1} 帧时间戳 {timestamp:.6f} 秒不连续，预期 {expected:.6f} 秒"
            )
    return {
        "status": "passed",
        "frame_count": len(timestamps),
        "first_timestamp_seconds": round(timestamps[0], 6),
        "last_timestamp_seconds": round(timestamps[-1], 6),
        "step_seconds": round(1 / AVATAR_VIDEO_FPS, 6),
    }


def _verify_pcm_tail_is_silent(
    source_audio: Path,
    *,
    start_sample: int,
    expected_sample_rate: int,
    expected_sample_frames: int,
) -> dict[str, Any]:
    try:
        with wave.open(str(source_audio), "rb") as handle:
            if (
                handle.getnchannels() != 1
                or handle.getsampwidth() != 2
                or handle.getframerate() != expected_sample_rate
                or handle.getnframes() != expected_sample_frames
            ):
                raise AvatarInputDriftError("冻结原音频不是预期的 PCM16 单声道帧时钟")
            if start_sample < 0 or start_sample >= expected_sample_frames:
                raise AvatarInputDriftError("尾部静音核验起点越出冻结原音频")
            handle.setpos(start_sample)
            payload = handle.readframes(expected_sample_frames - start_sample)
    except (wave.Error, OSError) as exc:
        raise AvatarInputDriftError("无法读取冻结原音频的尾部静音区间") from exc
    if any(payload):
        raise AvatarInputDriftError("供应商缺失尾帧对应的冻结音频并非全零静音，禁止克隆画面")
    return {
        "status": "passed",
        "start_sample": start_sample,
        "silent_sample_frames": expected_sample_frames - start_sample,
        "sample_rate": expected_sample_rate,
        "pcm_format": "s16le_mono",
    }


def _trailing_silence_padding_plan(
    media: dict[str, Any],
    *,
    role: str,
    exact_total_frames: int,
    expected_sample_rate: int,
    sample_frame_count: int,
    samples_per_video_frame: int,
    timing_turns: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Return a bounded local repair plan for provider-only tail truncation.

    InfiniteTalk may occasionally omit a few *video* frames after the final
    spoken sample while returning the full-length audio stream.  It is safe to
    clone the final frame only when every missing frame lies inside the frozen
    trailing-silence window.  Any missing speech frame, wrong geometry/rate,
    long truncation, or audio-clock drift remains a hard contract failure.
    """

    video = media.get("video") if isinstance(media.get("video"), dict) else {}
    audio = media.get("audio") if isinstance(media.get("audio"), dict) else {}
    if not video.get("present") or not audio.get("present"):
        return None
    if (int(video.get("width") or 0), int(video.get("height") or 0)) != (448, 560):
        return None
    if abs(float(video.get("fps") or 0) - AVATAR_VIDEO_FPS) > 1e-6:
        return None
    if int(audio.get("sample_rate") or 0) != expected_sample_rate or int(audio.get("channels") or 0) != 1:
        return None
    actual_frames = int(video.get("frame_count") or 0)
    missing_frames = int(exact_total_frames) - actual_frames
    if missing_frames <= 0 or missing_frames > MAX_TRAILING_CLOCK_PAD_FRAMES:
        return None
    if samples_per_video_frame <= 0 or sample_frame_count != exact_total_frames * samples_per_video_frame:
        return None
    role_turns = [
        item for item in timing_turns
        if isinstance(item, dict) and str(item.get("speaker_id") or "").lower() == role
    ]
    if not role_turns:
        return None
    try:
        last_speech_end_sample = max(int(item["speech_end_sample"]) for item in role_turns)
    except (KeyError, TypeError, ValueError):
        return None
    last_speech_end_frame = (
        last_speech_end_sample + samples_per_video_frame - 1
    ) // samples_per_video_frame
    # The frame cloned by tpad must itself begin at or after speech end.  Merely
    # covering the final spoken sample is insufficient because cloning that
    # frame could visibly freeze a mouth pose that still belongs to speech.
    if actual_frames <= 0 or last_speech_end_sample > (actual_frames - 1) * samples_per_video_frame:
        return None
    trailing_silence_frames = exact_total_frames - last_speech_end_frame
    if missing_frames > trailing_silence_frames:
        return None
    actual_video_duration = float(video.get("duration_seconds") or 0)
    if abs(actual_video_duration - actual_frames / AVATAR_VIDEO_FPS) > 0.001:
        return None
    expected_duration = exact_total_frames / AVATAR_VIDEO_FPS
    if abs(float(media.get("duration_seconds") or 0) - expected_duration) > 0.001:
        return None
    if abs(float(audio.get("duration_seconds") or 0) - expected_duration) > 0.001:
        return None
    return {
        "reason": "provider_video_tail_short_within_frozen_silence",
        "source_frames": actual_frames,
        "expected_frames": exact_total_frames,
        "added_frames": missing_frames,
        "last_speech_end_frame": last_speech_end_frame,
        "trailing_silence_frames": trailing_silence_frames,
    }


def _validate_or_normalize_exact_clock_avatar_output(
    target: Path,
    source_audio: Path,
    media: dict[str, Any],
    *,
    role: str,
    exact_total_frames: int,
    expected_sample_rate: int,
    sample_frame_count: int,
    samples_per_video_frame: int,
    timing_turns: list[dict[str, Any]],
    label: str,
    expected_provider_sha256: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    target_sha256 = _sha256_file(target)
    raw_identity = (expected_provider_sha256 or target_sha256)[:12]
    raw_target = target.with_name(f"{target.stem}.provider-raw-{raw_identity}{target.suffix}")
    legacy_raw_target = target.with_name(f"{target.stem}.provider-raw{target.suffix}")
    provider_source = target
    if expected_provider_sha256 and target_sha256 != expected_provider_sha256:
        if raw_target.is_file() and _sha256_file(raw_target) == expected_provider_sha256:
            provider_source = raw_target
        elif legacy_raw_target.is_file() and _sha256_file(legacy_raw_target) == expected_provider_sha256:
            provider_source = legacy_raw_target
            raw_target = legacy_raw_target
        else:
            raise AvatarInputDriftError(f"{label}下载结果已被替换，且找不到匹配账本哈希的供应商原片")
    source_clock = inspect_frame_clock_wav(source_audio, fps=AVATAR_VIDEO_FPS, require_aligned=True)
    if (
        int(source_clock["sample_rate"]) != expected_sample_rate
        or int(source_clock["sample_frame_count"]) != sample_frame_count
        or int(source_clock["video_frame_count"]) != exact_total_frames
    ):
        raise AvatarInputDriftError(f"{label}冻结原音频已变化，禁止本地补齐供应商尾帧")
    try:
        evidence = _validate_exact_clock_avatar_output(
            media,
            exact_total_frames=exact_total_frames,
            expected_sample_rate=expected_sample_rate,
            label=label,
        )
        if provider_source != target:
            raw_media = probe_media(provider_source)
            recovered_plan = _trailing_silence_padding_plan(
                raw_media,
                role=role,
                exact_total_frames=exact_total_frames,
                expected_sample_rate=expected_sample_rate,
                sample_frame_count=sample_frame_count,
                samples_per_video_frame=samples_per_video_frame,
                timing_turns=timing_turns,
            )
            if recovered_plan is None:
                raise AvatarInputDriftError(f"{label}已有规范化文件，但供应商原片不满足尾部静音修复合同")
            recovered_plan["pts_validation"] = _verify_contiguous_video_prefix(
                provider_source,
                int((raw_media.get("video") or {}).get("frame_count") or 0),
            )
            recovered_plan["pcm_tail_validation"] = _verify_pcm_tail_is_silent(
                source_audio,
                start_sample=(int(recovered_plan["source_frames"]) - 1) * samples_per_video_frame,
                expected_sample_rate=expected_sample_rate,
                expected_sample_frames=sample_frame_count,
            )
            recovered_plan.update({
                "status": "recovered_after_atomic_replace",
                "raw_output_path": raw_target.name,
                "raw_sha256": expected_provider_sha256,
                "normalized_sha256": target_sha256,
            })
            evidence["normalization"] = recovered_plan
        return media, evidence
    except AvatarInputDriftError:
        if provider_source != target:
            raise
        plan = _trailing_silence_padding_plan(
            media,
            role=role,
            exact_total_frames=exact_total_frames,
            expected_sample_rate=expected_sample_rate,
            sample_frame_count=sample_frame_count,
            samples_per_video_frame=samples_per_video_frame,
            timing_turns=timing_turns,
        )
        if plan is None:
            raise
        plan["pts_validation"] = _verify_contiguous_video_prefix(
            target,
            int((media.get("video") or {}).get("frame_count") or 0),
        )

    plan["pcm_tail_validation"] = _verify_pcm_tail_is_silent(
        source_audio,
        start_sample=(int(plan["source_frames"]) - 1) * samples_per_video_frame,
        expected_sample_rate=expected_sample_rate,
        expected_sample_frames=sample_frame_count,
    )
    ffmpeg = _find_media_binary("ffmpeg")
    if not ffmpeg:
        raise AvatarReviewPreviewError(f"{label}需要本地补齐尾部静音帧，但未发现 FFmpeg")
    raw_sha256 = target_sha256
    if raw_target.is_file() and _sha256_file(raw_target) != raw_sha256:
        raise AvatarInputDriftError(f"{label}原始供应商视频备份与当前下载结果不一致")
    temporary = target.with_name(f".{target.stem}.clock-normalized-{uuid4().hex}{target.suffix}")
    command = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(target), "-i", str(source_audio),
        "-filter_complex",
        (
            f"[0:v]tpad=stop_mode=clone:stop={int(plan['added_frames'])},"
            f"trim=end_frame={exact_total_frames},setpts=N/({AVATAR_VIDEO_FPS}*TB)[v]"
        ),
        "-map", "[v]", "-map", "1:a:0",
        "-c:v", "libx264", "-preset", "medium", "-crf", "18",
        "-pix_fmt", "yuv420p", "-r", str(AVATAR_VIDEO_FPS),
        "-fps_mode", "cfr",
        "-frames:v", str(exact_total_frames),
        "-c:a", "aac", "-ar", str(expected_sample_rate), "-ac", "1",
        "-movflags", "+faststart", str(temporary),
    ]
    result = _run_media_command(command)
    if result.returncode != 0 or not temporary.is_file():
        temporary.unlink(missing_ok=True)
        raise AvatarReviewPreviewError(
            f"{label}本地尾部静音帧补齐失败：{(result.stderr or 'FFmpeg 未生成输出')[-1000:]}"
        )
    try:
        normalized_media = probe_media(temporary)
        evidence = _validate_exact_clock_avatar_output(
            normalized_media,
            exact_total_frames=exact_total_frames,
            expected_sample_rate=expected_sample_rate,
            label=label,
        )
        evidence["pts_validation"] = _verify_contiguous_video_prefix(temporary, exact_total_frames)
        if not raw_target.is_file():
            shutil.copy2(target, raw_target)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    plan.update({
        "status": "applied",
        "raw_output_path": raw_target.name,
        "raw_sha256": raw_sha256,
        "normalized_sha256": _sha256_file(target),
    })
    evidence["normalization"] = plan
    return normalized_media, evidence


def _generate_runninghub_avatars(
    project_dir: Path,
    job_id: str,
    worker_token: str,
    context: dict[str, Any],
    voice_output: dict[str, Any],
    *,
    client_factory: Callable[[], Any] = RunningHubLongCatClient,
    poll_interval: float = POLL_INTERVAL_SECONDS,
    poll_timeout: float = POLL_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    images = context["images"]
    client = client_factory()
    records = _avatar_records(project_dir)
    timing_manifest = _verified_voice_timing_manifest(project_dir, voice_output)
    timing_turns = list(timing_manifest.get("turns") or [])
    timing_manifest_sha256 = str(timing_manifest["sha256"])
    frozen = _read_internal(project_dir).get("frozen_input") or {}
    recovery = frozen.get("avatar_recovery") or _avatar_recovery_policy(
        plus_48gb_authorized=False,
        budget_limit_cny=float(frozen.get("budget_limit_cny") or DEFAULT_BUDGET_LIMIT_CNY),
    )
    standard_max = int(recovery.get("standard_max_attempts") or 2)
    plus_max = int(recovery.get("plus_max_attempts") or 0)
    plus_authorized = recovery.get("plus_48gb_authorized") is True
    max_attempts = standard_max + (plus_max if plus_authorized else 0)
    output_dir = project_dir / AVATAR_DIRECTORY
    output_dir.mkdir(parents=True, exist_ok=True)
    active_roles = tuple(role for role in ROLE_LABELS if role in (voice_output.get("roles") or {}))
    if not active_roles:
        raise AvatarReviewPreviewError("没有可提交的主持人长音频")
    role_order = sorted(
        active_roles,
        key=lambda role: float((((voice_output.get("roles") or {}).get(role) or {}).get("duration_seconds") or 0)),
    )
    for role in role_order:
        label = ROLE_LABELS[role]
        record = records.setdefault(role, {"role": role, "label": label, "history": []})
        target = output_dir / f"{role}-longform.mp4"
        if record.get("status") == "ambiguous":
            raise AmbiguousAvatarOperation(f"{label}提交结果未知，禁止自动重提；请先在 RunningHub 核对任务编号")
        audio_record = (voice_output.get("roles") or {}).get(role) or {}
        audio = project_dir / str(audio_record.get("path") or "")
        if not audio.is_file():
            raise AvatarReviewPreviewError(f"{label}长音频不存在，不能提交数字人")
        try:
            clock = inspect_frame_clock_wav(
                audio,
                fps=AVATAR_VIDEO_FPS,
                require_aligned=True,
            )
        except AvatarAudioClockError as exc:
            raise AvatarInputDriftError(f"{label}长音频不符合精确帧时钟：{exc}") from exc
        audio_sha256 = _sha256_file(audio)
        expected_clock = {
            "sha256": audio_sha256,
            "sample_rate": int(clock["sample_rate"]),
            "sample_frame_count": int(clock["sample_frame_count"]),
            "samples_per_video_frame": int(clock["samples_per_video_frame"]),
            "video_fps": int(clock["video_fps"]),
            "video_frame_count": int(clock["video_frame_count"]),
        }
        for key, actual in expected_clock.items():
            recorded = audio_record.get(key)
            if recorded is None or (str(recorded) if key == "sha256" else int(recorded)) != actual:
                raise AvatarInputDriftError(
                    f"{label}长音频的 {key} 与配音阶段账本不一致；已在上传和扣费前停止"
                )
        input_contract = {
            "audio_sha256": audio_sha256,
            "sample_rate": clock["sample_rate"],
            "sample_frame_count": clock["sample_frame_count"],
            "video_fps": clock["video_fps"],
            "video_frame_count": clock["video_frame_count"],
            "timing_manifest_version": TURN_TIMING_MANIFEST_VERSION,
            "presenter_sha256": ((context.get("roles") or {}).get(role) or {}).get("presenter_sha256"),
            "workflow_id": PRODUCTION_WORKFLOW_ID,
            "workflow_profile": PRODUCTION_WORKFLOW_PROFILE,
            "recovery_policy_version": str(recovery.get("version") or ""),
        }
        legacy_input_hash = _json_hash(input_contract)
        input_hash = _json_hash({
            **input_contract,
            "timing_manifest_sha256": timing_manifest_sha256,
            "timing_manifest_input_signature": str(timing_manifest.get("input_signature") or ""),
        })
        history = record.setdefault("history", [])
        last_attempt = history[-1] if history else {}
        if record.get("status") == "failed" and last_attempt.get("terminal_reason") == "output_contract_drift":
            operation_id = str(record.get("operation_id") or "")
            operation = ((_read_internal(project_dir).get("paid_operations") or {}).get(operation_id) or {})
            if (
                not operation_id
                or operation.get("settled") is not True
                or str(operation.get("task_id") or "") != str(record.get("task_id") or "")
            ):
                raise AvatarInputDriftError(f"{label}尾帧修复候选缺少已结算的原付费任务证据")
            if (
                str(record.get("input_hash") or "") not in {input_hash, legacy_input_hash}
                or str(record.get("audio_sha256") or "") != audio_sha256
                or not target.is_file()
            ):
                raise AvatarInputDriftError(f"{label}尾帧修复候选与当前冻结输入不一致")
            media = probe_media(target)
            media, clock_evidence = _validate_or_normalize_exact_clock_avatar_output(
                target,
                audio,
                media,
                role=role,
                exact_total_frames=int(clock["video_frame_count"]),
                expected_sample_rate=int(clock["sample_rate"]),
                sample_frame_count=int(clock["sample_frame_count"]),
                samples_per_video_frame=int(clock["samples_per_video_frame"]),
                timing_turns=timing_turns,
                label=label,
                expected_provider_sha256=str(record.get("video_sha256") or "") or None,
            )
            normalization = deepcopy(clock_evidence.get("normalization") or {})
            last_attempt.update({
                "status": "succeeded",
                "finished_at": _now(),
                "terminal_reason": "local_trailing_silence_normalized",
                "normalization": normalization,
            })
            record.update({
                "status": "completed", "finished_at": _now(),
                "actual_cost_cny": float(operation.get("actual_cost_cny") or 0),
                "observed_instance": record.get("observed_instance") or "unverified",
                "video_sha256": _sha256_file(target),
                "clock_validation": clock_evidence,
                "recovery_state": "completed",
                "duration_seconds": media.get("duration_seconds"),
                "expected_duration_seconds": round(int(clock["video_frame_count"]) / AVATAR_VIDEO_FPS, 6),
                "duration_delta_seconds": round(
                    float(media.get("duration_seconds") or 0)
                    - int(clock["video_frame_count"]) / AVATAR_VIDEO_FPS,
                    6,
                ),
                "provider_output_contract_error": record.get("output_contract_error"),
                "timing_manifest_sha256": timing_manifest_sha256,
            })
            record.pop("output_contract_error", None)
            if normalization.get("raw_output_path"):
                record["provider_raw_output_path"] = str(
                    (target.parent / normalization["raw_output_path"]).relative_to(project_dir)
                ).replace("\\", "/")
            _transition_operation(
                project_dir,
                job_id,
                worker_token,
                operation_id,
                "succeeded",
                output_path=record["output_path"],
                local_normalization=normalization,
            )
            _persist_avatar_records(project_dir, job_id, worker_token, records, f"{label}已复用原付费结果并补齐尾部静音帧")
            continue
        if record.get("status") == "completed":
            if (
                not target.is_file() or target.stat().st_size <= 4096
                or str(record.get("input_hash") or "") not in {input_hash, legacy_input_hash}
                or str(record.get("audio_sha256") or "") != audio_sha256
                or str(record.get("video_sha256") or "") != _sha256_file(target)
            ):
                raise AvatarInputDriftError(f"{label}已完成结果与当前冻结输入不一致；拒绝重新付费覆盖")
            continue

        while record.get("status") != "completed":
            if record.get("status") == "ambiguous":
                raise AmbiguousAvatarOperation(f"{label}提交结果未知，禁止自动重提；请先在 RunningHub 核对任务编号")
            active_task = record.get("status") in {"submitted", "running"} and bool(record.get("task_id"))
            if not active_task and (record.get("attempts_exhausted") is True or len(history) >= max_attempts):
                raise AvatarRecoveryExhaustedError(
                    f"{label}已用尽本任务授权的 {max_attempts} 次数字人尝试；禁止第 {max_attempts + 1} 次自动提交"
                )

            if active_task:
                attempt_no = int(record.get("attempt_no") or len(history) or 1)
                requested_instance = str(record.get("requested_instance") or "default")
                operation_id = str(record.get("operation_id") or "")
                if not operation_id:
                    raise AmbiguousAvatarOperation(f"{label}已有任务号但缺少付费操作编号，禁止自动重提")
            else:
                if record.get("status") == "failed":
                    last_failure = record.get("last_failure") or {}
                    if (
                        last_failure.get("kind") != "oom"
                        or last_failure.get("is_oom") is not True
                        or last_failure.get("explicit") is not True
                    ):
                        raise AvatarProviderTerminalError(
                            f"{label}上次为明确非 OOM 失败；有限自动恢复不会创建新付费任务"
                        )
                attempt_no = len(history) + 1
                requested_instance = "default" if attempt_no <= standard_max else "plus"
                if requested_instance == "plus" and not plus_authorized:
                    record.update({
                        "attempts_exhausted": True,
                        "recovery_state": "exhausted",
                        "terminal_reason": "plus_not_authorized_after_standard_oom",
                    })
                    _persist_avatar_records(project_dir, job_id, worker_token, records, f"{label}未授权 Plus 48GB，已停止")
                    raise AvatarRecoveryExhaustedError(
                        f"{label}两次 Standard 24GB 均明确 OOM，但本任务未冻结 Plus 48GB 授权"
                    )
                instance_label = "Plus 48GB" if requested_instance == "plus" else "Standard 24GB"
                operation_id = (
                    f"runninghub:{project_dir.name}:{role}:{input_hash[:20]}:"
                    f"a{attempt_no}:{requested_instance}"
                )
                existing_operation = (
                    (_read_internal(project_dir).get("paid_operations") or {}).get(operation_id) or {}
                )
                existing_task_id = str(existing_operation.get("task_id") or "")
                if existing_task_id:
                    record.update({
                        "status": "submitted", "task_id": existing_task_id,
                        "operation_id": operation_id, "attempt_no": attempt_no,
                        "input_hash": input_hash, "requested_instance": requested_instance,
                        "instance": requested_instance, "audio_sha256": audio_sha256,
                        "sample_rate": int(clock["sample_rate"]),
                        "sample_frame_count": int(clock["sample_frame_count"]),
                        "samples_per_video_frame": int(clock["samples_per_video_frame"]),
                        "video_fps": int(clock["video_fps"]),
                        "exact_total_frames": int(clock["video_frame_count"]),
                        "timing_manifest_sha256": timing_manifest_sha256,
                        "started_monotonic": time.monotonic(),
                        "started_at": existing_operation.get("updated_at") or _now(),
                        "output_path": str(target.relative_to(project_dir)).replace("\\", "/"),
                        "recovery_state": "active",
                    })
                    if not any(item.get("operation_id") == operation_id for item in history):
                        history.append({
                            "attempt_no": attempt_no, "operation_id": operation_id,
                            "task_id": existing_task_id,
                            "submitted_at": existing_operation.get("updated_at") or _now(),
                            "requested_instance": requested_instance,
                            "instance": requested_instance, "status": "submitted",
                            "reconciled_from_paid_operation": True,
                        })
                    _persist_avatar_records(
                        project_dir, job_id, worker_token, records,
                        f"{label}已从付费账本恢复原任务号，只会继续查询该任务",
                    )
                    continue
                if str(existing_operation.get("state") or "") in {"submitting", "ambiguous"}:
                    record.update({
                        "status": "ambiguous", "operation_id": operation_id,
                        "attempt_no": attempt_no, "requested_instance": requested_instance,
                        "last_failure": {
                            "kind": "unknown", "is_oom": False,
                            "message": "服务在付费提交期间中断，未可靠取得任务编号",
                        },
                    })
                    _transition_operation(
                        project_dir, job_id, worker_token, operation_id, "ambiguous",
                        error="付费提交期间中断且没有可靠任务编号",
                    )
                    _persist_avatar_records(
                        project_dir, job_id, worker_token, records,
                        f"{label}付费提交状态不明，已停止自动重提",
                    )
                    raise AmbiguousAvatarOperation(
                        f"{label}付费提交期间中断且没有可靠任务编号；禁止自动重提"
                    )
                _reserve_budget(
                    project_dir, job_id, worker_token, operation_id,
                    f"{label} {instance_label} 数字人第 {attempt_no} 次尝试",
                    requested_instance=requested_instance,
                )
                try:
                    image_remote = client.upload_file(images[role], file_type="image")
                    audio_remote = client.upload_file(audio, file_type="audio")
                except Exception as exc:  # noqa: BLE001 - no paid task exists yet
                    _release_budget(project_dir, job_id, worker_token, operation_id, "输入上传失败，未建立付费任务")
                    _transition_operation(project_dir, job_id, worker_token, operation_id, "released", error=str(exc)[:500])
                    raise AvatarReviewPreviewError(f"{label} RunningHub 输入上传失败，未提交付费任务：{exc}") from exc
                _transition_operation(
                    project_dir, job_id, worker_token, operation_id, "submitting",
                    role=role, input_hash=input_hash, attempt_no=attempt_no,
                    requested_instance=requested_instance,
                )
                try:
                    submitted = client.submit(
                        presenter_filename=image_remote,
                        audio_filename=audio_remote,
                        instance_type=requested_instance,
                        exact_total_frames=int(clock["video_frame_count"]),
                    )
                except Exception as exc:  # noqa: BLE001 - request may already exist remotely
                    failure = classify_runninghub_failure(exc)
                    _transition_operation(project_dir, job_id, worker_token, operation_id, "ambiguous", error=str(exc)[:500])
                    record.update({
                        "status": "ambiguous", "operation_id": operation_id,
                        "attempt_no": attempt_no, "requested_instance": requested_instance,
                        "last_failure": failure,
                    })
                    _persist_avatar_records(project_dir, job_id, worker_token, records, f"{label}提交结果未知，已停止自动重提")
                    raise AmbiguousAvatarOperation(f"{label}提交响应中断，远端可能已接单；禁止自动重提") from exc
                if not isinstance(submitted, dict) or not str(submitted.get("task_id") or ""):
                    _transition_operation(
                        project_dir, job_id, worker_token, operation_id, "ambiguous",
                        error="RunningHub 提交响应缺少任务编号",
                    )
                    record.update({
                        "status": "ambiguous", "operation_id": operation_id,
                        "attempt_no": attempt_no, "requested_instance": requested_instance,
                        "last_failure": {"kind": "unknown", "is_oom": False, "message": "提交响应缺少任务编号"},
                    })
                    _persist_avatar_records(project_dir, job_id, worker_token, records, f"{label}提交响应缺少任务编号，已停止自动重提")
                    raise AmbiguousAvatarOperation(f"{label} RunningHub 返回缺少任务编号，禁止自动重提")
                task_id = str(submitted["task_id"])
                for stale_key in (
                    "actual_cost_cny", "observed_instance", "last_failure", "finished_at",
                    "video_sha256", "duration_seconds", "output_contract_error", "clock_validation",
                ):
                    record.pop(stale_key, None)
                record.update({
                    "status": "submitted", "task_id": task_id, "operation_id": operation_id,
                    "attempt_no": attempt_no, "input_hash": input_hash,
                    "requested_instance": requested_instance, "instance": requested_instance,
                    "audio_sha256": audio_sha256,
                    "sample_rate": int(clock["sample_rate"]),
                    "sample_frame_count": int(clock["sample_frame_count"]),
                    "samples_per_video_frame": int(clock["samples_per_video_frame"]),
                    "video_fps": int(clock["video_fps"]),
                    "exact_total_frames": int(clock["video_frame_count"]),
                    "timing_manifest_sha256": timing_manifest_sha256,
                    "started_monotonic": time.monotonic(), "started_at": _now(),
                    "output_path": str(target.relative_to(project_dir)).replace("\\", "/"),
                    "recovery_state": "active",
                })
                history.append({
                    "attempt_no": attempt_no, "operation_id": operation_id, "task_id": task_id,
                    "submitted_at": _now(), "requested_instance": requested_instance,
                    "instance": requested_instance, "status": "submitted",
                })
                _persist_avatar_submission(
                    project_dir, job_id, worker_token, records,
                    operation_id=operation_id, task_id=task_id,
                    requested_instance=requested_instance,
                    label=f"已提交 {label} {instance_label} 第 {attempt_no} 次数字人任务",
                )

            deadline = time.monotonic() + poll_timeout
            terminal_result: dict[str, Any] | None = None
            while time.monotonic() < deadline:
                try:
                    result = client.poll(str(record["task_id"]))
                except Exception as exc:  # noqa: BLE001
                    record["status"] = "running"
                    error_count = int(record.get("transient_poll_error_count") or 0) + 1
                    record.update({
                        "transient_poll_error_count": error_count,
                        "last_transient_poll_error": str(exc)[:500],
                    })
                    _persist_avatar_records(
                        project_dir,
                        job_id,
                        worker_token,
                        records,
                        f"{label}状态查询短暂中断（{error_count}/{MAX_TRANSIENT_POLL_ERRORS}）；只会继续查询原任务",
                    )
                    if error_count < MAX_TRANSIENT_POLL_ERRORS:
                        time.sleep(max(0.0, min(MAX_TRANSIENT_POLL_BACKOFF_SECONDS, poll_interval)))
                        continue
                    raise AvatarReviewPreviewError(
                        f"{label}状态查询连续 {error_count} 次失败；任务号已保存，只会继续轮询同一任务：{exc}"
                    ) from exc
                record.pop("transient_poll_error_count", None)
                record.pop("last_transient_poll_error", None)
                provider_status = str(result.get("status") or "").upper()
                if provider_status in {"RUNNING", "QUEUED", "PENDING"}:
                    record["status"] = "running"
                    _transition_operation(project_dir, job_id, worker_token, operation_id, "running", task_id=record.get("task_id"))
                    _persist_avatar_records(
                        project_dir, job_id, worker_token, records,
                        f"{label}数字人第 {attempt_no} 次生成中（{'Plus 48GB' if requested_instance == 'plus' else 'Standard 24GB'}）",
                    )
                    time.sleep(max(0.0, poll_interval))
                    continue
                if provider_status not in {"SUCCEEDED", "FAILED"}:
                    record["status"] = "running"
                    _persist_avatar_records(project_dir, job_id, worker_token, records, f"{label}返回未知状态；保留任务号等待安全恢复")
                    raise AvatarReviewPreviewError(f"{label} RunningHub 状态未知；只会继续查询原任务，不会重新提交")
                terminal_result = result
                break
            if terminal_result is None:
                record["status"] = "running"
                _persist_avatar_records(project_dir, job_id, worker_token, records, f"{label}等待超时；任务号已保存")
                raise AvatarReviewPreviewError(f"{label}数字人等待超时；任务号已保存，可继续追踪")

            elapsed = max(0.0, time.monotonic() - float(record.get("started_monotonic") or time.monotonic()))
            actual = _settle_budget(project_dir, job_id, worker_token, operation_id, terminal_result, elapsed)
            billing = terminal_result.get("billing") if isinstance(terminal_result.get("billing"), dict) else {}
            observed = str(billing.get("observed_instance") or "unverified")
            allowed_observed = (
                {"unverified", "standard_24gb", "default"}
                if requested_instance == "default"
                else {"unverified", "plus_48gb", "plus"}
            )
            if observed not in allowed_observed:
                record.update({"status": "failed", "actual_cost_cny": actual, "observed_instance": observed})
                _transition_operation(project_dir, job_id, worker_token, operation_id, "failed", error="供应商实际实例不符合冻结合同")
                _persist_avatar_records(project_dir, job_id, worker_token, records, f"{label}实例不符合冻结合同")
                raise AvatarInputDriftError(
                    f"RunningHub 请求 {requested_instance}，但账单显示 {observed}；已停止后续角色"
                )

            attempt_history = next(
                (item for item in reversed(history) if item.get("operation_id") == operation_id),
                None,
            )
            if terminal_result.get("status") == "SUCCEEDED":
                if not terminal_result.get("video_url"):
                    if attempt_history is not None:
                        attempt_history.update({"status": "failed", "finished_at": _now(), "terminal_reason": "missing_video_url"})
                    record.update({"status": "failed", "actual_cost_cny": actual, "observed_instance": observed})
                    _transition_operation(project_dir, job_id, worker_token, operation_id, "failed", error="成功响应缺少视频地址")
                    _persist_avatar_records(project_dir, job_id, worker_token, records, f"{label}成功响应缺少视频地址，禁止新付费任务")
                    raise AvatarProviderTerminalError(f"{label} RunningHub 已结束但没有返回视频地址；禁止重新付费提交")
                client.download(str(terminal_result["video_url"]), target)
                media = probe_media(target)
                try:
                    media, clock_evidence = _validate_or_normalize_exact_clock_avatar_output(
                        target,
                        audio,
                        media,
                        role=role,
                        exact_total_frames=int(record["exact_total_frames"]),
                        expected_sample_rate=int(record["sample_rate"]),
                        sample_frame_count=int(record["sample_frame_count"]),
                        samples_per_video_frame=int(record["samples_per_video_frame"]),
                        timing_turns=timing_turns,
                        label=label,
                    )
                except AvatarInputDriftError as exc:
                    record.update({
                        "status": "failed", "finished_at": _now(), "actual_cost_cny": actual,
                        "observed_instance": observed, "video_sha256": _sha256_file(target),
                        "duration_seconds": media.get("duration_seconds"),
                        "output_contract_error": str(exc),
                    })
                    if attempt_history is not None:
                        attempt_history.update({"status": "failed", "finished_at": _now(), "terminal_reason": "output_contract_drift"})
                    _transition_operation(
                        project_dir, job_id, worker_token, operation_id, "failed",
                        error=str(exc), output_path=record["output_path"],
                    )
                    _persist_avatar_records(project_dir, job_id, worker_token, records, f"{label}输出未通过精确帧回读")
                    raise
                if attempt_history is not None:
                    attempt_history.update({
                        "status": "succeeded", "finished_at": _now(),
                        "actual_cost_cny": actual, "observed_instance": observed,
                        "normalization": deepcopy(clock_evidence.get("normalization") or {}),
                    })
                record.update({
                    "status": "completed", "finished_at": _now(), "actual_cost_cny": actual,
                    "observed_instance": observed, "video_sha256": _sha256_file(target),
                    "clock_validation": clock_evidence, "recovery_state": "completed",
                    "duration_seconds": media.get("duration_seconds"),
                    "expected_duration_seconds": round(int(record["exact_total_frames"]) / AVATAR_VIDEO_FPS, 6),
                    "duration_delta_seconds": round(
                        float(media.get("duration_seconds") or 0)
                        - int(record["exact_total_frames"]) / AVATAR_VIDEO_FPS,
                        6,
                    ),
                    "timing_manifest_sha256": timing_manifest_sha256,
                })
                normalization = clock_evidence.get("normalization") or {}
                if normalization.get("raw_output_path"):
                    record["provider_raw_output_path"] = str(
                        (target.parent / normalization["raw_output_path"]).relative_to(project_dir)
                    ).replace("\\", "/")
                _transition_operation(project_dir, job_id, worker_token, operation_id, "succeeded", output_path=record["output_path"])
                _persist_avatar_records(project_dir, job_id, worker_token, records, f"{label}数字人已完成")
                break

            failure = classify_runninghub_failure({
                "status": terminal_result.get("status"),
                "error": terminal_result.get("error"),
                "failure_details": terminal_result.get("failure_details") or {},
            })
            failure["provider_details"] = deepcopy(terminal_result.get("failure_details") or {})
            failure["terminal_status"] = "FAILED"
            if attempt_history is not None:
                attempt_history.update({
                    "status": "failed", "finished_at": _now(), "actual_cost_cny": actual,
                    "observed_instance": observed, "failure": deepcopy(failure),
                })
            record.update({
                "status": "failed", "last_failure": failure, "actual_cost_cny": actual,
                "observed_instance": observed,
            })
            _transition_operation(
                project_dir, job_id, worker_token, operation_id, "failed",
                error=failure.get("message"), terminal_status="FAILED",
            )
            if (
                failure.get("kind") != "oom"
                or failure.get("is_oom") is not True
                or failure.get("explicit") is not True
            ):
                record.update({"recovery_state": "stopped_non_oom", "terminal_reason": "terminal_non_oom_failure"})
                _persist_avatar_records(project_dir, job_id, worker_token, records, f"{label}明确非 OOM 失败，已停止自动恢复")
                raise AvatarProviderTerminalError(
                    f"{label}数字人明确失败但不是 OOM；不会创建新付费任务：{failure.get('message')}"
                )
            if attempt_no >= max_attempts:
                record.update({
                    "attempts_exhausted": True, "recovery_state": "exhausted",
                    "terminal_reason": "plus_failed_after_3_attempts" if requested_instance == "plus" else "authorized_attempts_exhausted",
                })
                _persist_avatar_records(project_dir, job_id, worker_token, records, f"{label}已用尽有限 OOM 自动恢复次数")
                raise AvatarRecoveryExhaustedError(
                    f"{label}第 {attempt_no} 次仍明确 OOM；已用尽授权次数，等待人工处理"
                )
            _persist_avatar_records(
                project_dir, job_id, worker_token, records,
                f"{label}第 {attempt_no} 次明确 OOM；预算允许，将自动尝试下一档有限恢复",
            )
    return {"roles": records}


def _prepare_longform_package(
    project_dir: Path,
    avatar_output: dict[str, Any],
    voice_output: dict[str, Any] | None = None,
) -> dict[str, Any]:
    package = read_avatar_package(project_dir)
    settings = (package or {}).get("settings") or {}
    timing_settings_match = (
        abs(float(settings.get("speaker_change_gap_seconds") or 0) - SPEAKER_CHANGE_GAP_MS / 1000) <= 1e-9
        and abs(float(settings.get("same_speaker_gap_seconds") or 0) - SAME_SPEAKER_GAP_MS / 1000) <= 1e-9
    )
    if (
        not package
        or package.get("generation_mode") != "runninghub_longform"
        or package.get("import_mode") != "longform"
        or not timing_settings_match
    ):
        package = initialize_avatar_package(project_dir, {
            "replace": True,
            "generation_mode": "runninghub_longform",
            "import_mode": "longform",
            "require_asr": True,
            "speaker_change_gap_seconds": SPEAKER_CHANGE_GAP_MS / 1000,
            "same_speaker_gap_seconds": SAME_SPEAKER_GAP_MS / 1000,
            "default_treatment": "custom",
            "background_mode": "opaque",
        })
    records = avatar_output.get("roles") or {}
    for role in tuple(role for role in ROLE_LABELS if role in records):
        label = ROLE_LABELS[role]
        source = project_dir / str((records.get(role) or {}).get("output_path") or "")
        if not source.is_file():
            raise AvatarReviewPreviewError(f"{label}数字人长视频不存在")
        current = next((item for item in package.get("speakers") or [] if item.get("speaker_id") == role), {})
        existing = project_dir / str((current.get("source") or {}).get("path") or "")
        if existing.is_file() and (current.get("source") or {}).get("sha256") == _sha256_file(source):
            continue
        temporary, target = prepare_upload(project_dir, source.name, speaker_id=role)
        shutil.copy2(source, temporary)
        package = finalize_upload(project_dir, temporary, target, source.name, speaker_id=role)
    timing_manifest = (voice_output or {}).get("timing_manifest")
    if isinstance(timing_manifest, dict) and timing_manifest.get("turns"):
        manifest_relative = Path(str(timing_manifest.get("path") or ""))
        manifest_path = (
            manifest_relative.resolve()
            if manifest_relative.is_absolute()
            else (project_dir / manifest_relative).resolve()
        )
        if not manifest_path.is_relative_to(project_dir.resolve()) or not manifest_path.is_file():
            raise AvatarInputDriftError("逐轮时间清单路径已漂移或文件不存在")
        if _sha256_file(manifest_path) != str(timing_manifest.get("sha256") or ""):
            raise AvatarInputDriftError("逐轮时间清单内容已改变；拒绝用旧切点处理新数字人")
        manifest_payload = _read_json(manifest_path)
        if (
            not manifest_payload
            or str(manifest_payload.get("version") or "") != TURN_TIMING_MANIFEST_VERSION
            or str(manifest_payload.get("input_signature") or "")
            != str(timing_manifest.get("input_signature") or "")
        ):
            raise AvatarInputDriftError("逐轮时间清单版本或输入签名已漂移")
        manifest_payload.update({
            "path": str(manifest_relative).replace("\\", "/"),
            "sha256": str(timing_manifest.get("sha256") or ""),
        })
        package = apply_longform_timing_manifest(project_dir, manifest_payload)
    return package


def _align_avatar_package(project_dir: Path, avatar_output: dict[str, Any], asr_model_id: str, voice_output: dict[str, Any] | None = None) -> dict[str, Any]:
    package = _prepare_longform_package(project_dir, avatar_output, voice_output)
    timing_manifest = (((package.get("asr") or {}).get("summary") or {}).get("timing_manifest") or {})
    exact_clock = str(timing_manifest.get("version") or "") == TURN_TIMING_MANIFEST_VERSION
    if package.get("asr", {}).get("status") != "passed":
        try:
            if package.get("asr", {}).get("status") != "running":
                start_avatar_asr(project_dir, {"model": asr_model_id})
            package = run_avatar_asr(project_dir, {"model": asr_model_id})
        except Exception as exc:  # noqa: BLE001 - exact cuts remain deterministic; ASR is diagnostics only
            if not exact_clock:
                raise
            package = approve_exact_clock_manifest_cuts(
                project_dir,
                diagnostic_error=exc,
                model_name=asr_model_id,
            )
        if package.get("asr", {}).get("status") != "passed":
            if exact_clock:
                package = approve_exact_clock_manifest_cuts(
                    project_dir,
                    diagnostic_error=package.get("asr", {}).get("error") or "Whisper 诊断未通过",
                    model_name=asr_model_id,
                )
            else:
                raise AvatarReviewPreviewError("数字人长视频 ASR 未通过；已保留诊断，请人工核对")
    if exact_clock and (package.get("cut_plan") or {}).get("status") == "approved":
        return package
    return approve_high_confidence_longform_cuts(project_dir)


def _cut_plan_ready(package: dict[str, Any]) -> bool:
    return str((package.get("cut_plan") or {}).get("status") or "") == "approved"


def _set_cut_gate(project_dir: Path, job_id: str, worker_token: str, package: dict[str, Any]) -> dict[str, Any]:
    summary = (package.get("cut_plan") or {}).get("summary") or {}
    pending = int(summary.get("needs_manual") or 0) + int(summary.get("pending_review") or 0)

    def mutate(state: dict[str, Any], job: dict[str, Any]) -> None:
        job.update({
            "status": "awaiting_human",
            "stage": "avatar_alignment",
            "safe_resume_point": "avatar_alignment",
            "worker_token": None,
            "current": {"kind": "gate", "id": "avatar_cut_review", "label": f"仍有 {pending} 个切点需要人工核对"},
            "gate": {
                "stage": "avatar_cut_review",
                "reason": f"Whisper 已完成；仍有 {pending} 个低置信度切点需要人工核对",
                "required_action": "进入数字人素材页调整并批准剩余切点，然后从安全点继续",
                "action_label": "完成切点复核后继续",
            },
            "error": None,
        })
        wb._activity(state, "avatar_cut_review_required", f"有数字人一键任务等待人工核对 {pending} 个切点", job_id=job_id)
    return _mutate(project_dir, job_id, worker_token, mutate)


def _assemble_and_apply(project_dir: Path) -> dict[str, Any]:
    package = read_avatar_package(project_dir) or {}
    if not _cut_plan_ready(package):
        raise AvatarReviewPreviewError("仍有数字人切点未批准")
    timing_manifest = (((package.get("asr") or {}).get("summary") or {}).get("timing_manifest") or {})
    if str(timing_manifest.get("version") or "") == TURN_TIMING_MANIFEST_VERSION:
        package = ensure_exact_clock_assembly_duration_limit(
            project_dir,
            maximum_seconds=ONE_CLICK_AVATAR_MAX_DURATION_SECONDS,
        )
    # A freshly approved script can enter the one-click avatar pipeline before
    # the user has visited the scene-planning step.  Applying the finished
    # avatar master requires those scene records, so create the ordinary
    # project-local scene plan here when it is still missing.  The workbench
    # helper is idempotent and preserves an existing/reviewed scene plan.
    if not (wb.read_workbench(project_dir).get("scenes") or []):
        wb.generate_scene_plan_from_script(project_dir)
    if package.get("assembly", {}).get("status") != "passed":
        if package.get("assembly", {}).get("status") != "running":
            start_avatar_assembly(project_dir)
        package = assemble_avatar_package(project_dir)
    if package.get("assembly", {}).get("status") != "passed":
        issues = package.get("assembly", {}).get("issues") or []
        detail = next(
            (str(item.get("message") or "") for item in issues if isinstance(item, dict) and item.get("message")),
            "数字人原声母版未通过本地媒体检查",
        )
        raise AvatarReviewPreviewError(f"数字人原声母版合成未通过：{detail}")
    # ``pip_top_right`` is a geometry template, not an avatar-package
    # treatment.  ``custom`` preserves the project's default right-top
    # template while remaining valid in the avatar import contract.
    state = wb.apply_avatar_package_to_timeline(project_dir, {"default_treatment": "custom"})
    return {
        "assembly": package.get("assembly"),
        "timeline_revision": (state.get("timeline") or {}).get("revision"),
        "scene_count": len(state.get("scenes") or []),
    }


def _visuals_complete(state: dict[str, Any]) -> bool:
    scenes = state.get("scenes") or []
    return bool(scenes) and all(
        any(block.get("status") == "ready" and block.get("asset_id") for block in ((scene.get("visual_timeline") or {}).get("blocks") or []))
        for scene in scenes
    )


def _preview_supporting_visual_plan(
    project_dir: Path,
    policy: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], str | None]:
    """Return a bounded AI plan or a recorded deterministic fallback.

    The parent job must remain recoverable when a compatible text relay sends
    endless SSE heartbeats. Planning is read-only up to this point, so a
    timed-out planner thread cannot alter the project after the fallback has
    continued. The actual execution policy and the reason are persisted into
    the visual-batch contract for later review.
    """
    if policy.get("planning_mode") != "ai_director":
        return wb.preview_visual_batch_plan(project_dir, policy), policy, None

    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="avatar-visual-plan")
    fallback_reason: str | None = None
    try:
        future = executor.submit(wb.preview_visual_batch_plan, project_dir, policy)
        try:
            return future.result(timeout=VISUAL_AI_PLANNING_TIMEOUT_SECONDS), policy, None
        except FutureTimeout:
            fallback_reason = (
                f"AI 智能导演在 {int(VISUAL_AI_PLANNING_TIMEOUT_SECONDS)} 秒内未返回可执行计划，"
                "已自动改用规则混合规划；数字人、本地素材和时间轴均未改变"
            )
        except wb.WorkbenchError as exc:
            if not str(exc).startswith("AI 画面规划失败："):
                raise
            fallback_reason = (
                f"{exc}；已自动改用规则混合规划；"
                "数字人、本地素材和时间轴均未改变"
            )
    finally:
        # ``wait=False`` is intentional: an SSE relay can ignore the client
        # deadline. The planner only reads project state, while the parent
        # continues with an explicit, auditable non-AI plan.
        executor.shutdown(wait=False, cancel_futures=True)

    fallback_policy = {
        **policy,
        "planning_mode": "rule_mix",
        "ai_planning_confirmed": False,
    }
    reviewed = wb.preview_visual_batch_plan(project_dir, fallback_policy)
    planner = reviewed.setdefault("planner", {})
    planner.update({
        "mode": "rule_mix",
        "fallback_from": "ai_director",
        "fallback_reason": fallback_reason,
    })
    return reviewed, fallback_policy, fallback_reason


def _generate_supporting_visuals(project_dir: Path, job_id: str, worker_token: str, frozen: dict[str, Any]) -> dict[str, Any]:
    state = wb.read_workbench(project_dir)
    if _visuals_complete(state):
        return {"reused": True, "completed_slots": sum(len(((scene.get("visual_timeline") or {}).get("blocks") or [])) for scene in state.get("scenes") or [])}
    policy = {
        "selection_mode": "missing",
        "operation_mode": "fill_missing",
        "profile": "daily_news",
        "mix_strategy": "video_first",
        "image_source": "web_download",
        "content_rules": ["no_frontal_face", "no_large_text_watermark"],
        "person_policy": "balanced",
        "candidate_limit": 6,
        "planning_mode": str((frozen.get("visual") or {}).get("planning_mode") or "ai_director"),
    }
    policy["ai_planning_confirmed"] = policy["planning_mode"] == "ai_director"
    child = (state.get("automation") or {}).get("visual_batch") or {}
    if child.get("status") in {"queued", "generating"} and child.get("parent_job_id") == job_id:
        child_job_id = str(child.get("job_id") or "")
        execution_policy = policy
        planning_fallback = None
    else:
        reviewed, execution_policy, planning_fallback = _preview_supporting_visual_plan(project_dir, policy)
        started = wb.start_visual_batch_generation(project_dir, {
            **execution_policy,
            "confirmed": True,
            "reviewed_plan": reviewed,
            "copy_presenter_layout": False,
            "_review_preview_job_id": job_id,
            "_review_preview_worker_token": worker_token,
            "_review_preview_internal_capability": wb._REVIEW_PREVIEW_INTERNAL_CAPABILITY,
            "_review_preview_request_fingerprint": str(_read_internal(project_dir).get("request_fingerprint") or ""),
        })
        child_job_id = str((((started.get("automation") or {}).get("visual_batch") or {}).get("job_id") or ""))
    result = wb.generate_visual_batch(
        project_dir,
        expected_job_id=child_job_id,
        expected_parent_job_id=job_id,
        expected_worker_token=worker_token,
        expected_request_fingerprint=str(_read_internal(project_dir).get("request_fingerprint") or ""),
        expected_contract_versions=frozen.get("versions") or {},
    )
    batch = (result.get("automation") or {}).get("visual_batch") or {}
    if batch.get("status") not in {"completed", "completed_with_warnings"}:
        raise AvatarReviewPreviewError(str(batch.get("error") or "主体画面批量生成未完成"))
    if int(batch.get("failed_slots") or 0) > 0:
        raise AvatarReviewPreviewError("主体画面仍有失败槽；已保留成功画面，可从失败槽继续")
    return {
        "job_id": child_job_id,
        "total_slots": batch.get("total_slots"),
        "completed_slots": batch.get("completed_slots"),
        "failed_slots": batch.get("failed_slots"),
        "planning_mode": execution_policy["planning_mode"],
        "planning_fallback": planning_fallback,
    }


def _render_preview(project_dir: Path, job_id: str, worker_token: str) -> dict[str, Any]:
    state = wb.read_workbench(project_dir)
    preview = (state.get("automation") or {}).get("preview_render") or {}
    if preview.get("status") != "completed" or preview.get("needs_refresh"):
        if preview.get("status") != "generating":
            state = wb.start_full_preview_render(project_dir, {
                "confirmed": True,
                "_review_preview_job_id": job_id,
                "_review_preview_worker_token": worker_token,
                "_review_preview_internal_capability": wb._REVIEW_PREVIEW_INTERNAL_CAPABILITY,
                "_review_preview_trusted_default_audio": True,
                "_review_preview_upfront_audio_signature": str(
                    (((_read_internal(project_dir).get("frozen_input") or {}).get("audio") or {}).get("audio_mix_signature") or "")
                ),
                "_review_preview_input_fingerprint": str(_read_internal(project_dir).get("input_fingerprint") or ""),
            })
        state = wb.generate_full_preview_render(project_dir)
        preview = (state.get("automation") or {}).get("preview_render") or {}
    if preview.get("status") != "completed" or preview.get("needs_refresh"):
        raise AvatarReviewPreviewError(str(preview.get("error") or "全片审核预览未完成"))
    output_path = str(preview.get("output_path") or "")
    report_path = str(preview.get("report_path") or wb.AUTOMATION_PREVIEW_RENDER_REPORT)
    evidence = _probe_preview_evidence(project_dir, output_path, report_path)
    return {"preview_path": output_path, "report_path": report_path, **evidence}


def _complete(project_dir: Path, job_id: str, worker_token: str, evidence: dict[str, Any]) -> dict[str, Any]:
    def mutate(state: dict[str, Any], job: dict[str, Any]) -> None:
        phase = job.setdefault("phases", {}).setdefault("review_ready", {})
        phase.update({"status": "completed", "finished_at": _now(), "output": deepcopy(evidence), "retryable": False})
        job.update({
            "status": "completed",
            "stage": "review_ready",
            "safe_resume_point": None,
            "current": {"kind": "result", "id": "preview_ready", "label": "有数字人审核预览已就绪，等待人工观看"},
            "gate": {"reason": "必须人工观看审核预览", "required_action": "人工审核；不会自动批准或发布"},
            "error": None,
            "result": {
                "readiness": "preview_ready",
                "script_hash": job.get("script_hash"),
                "budget": deepcopy(job.get("budget") or {}),
                **deepcopy(evidence),
            },
            "worker_token": None,
            "finished_at": _now(),
        })
        job["counts"]["completed"] = 7
        job["counts"]["failed"] = 0
        wb._activity(state, "avatar_review_preview_ready", f"有数字人一键审核预览任务 {job_id} 已到达待审终点", job_id=job_id, output_path=evidence.get("preview_path"))
    return _mutate(project_dir, job_id, worker_token, mutate)


def _acquire_worker(project_dir: Path, expected_job_id: str | None) -> tuple[str, str] | None:
    with wb._project_transaction_lock(project_dir):
        state = wb._load_for_write(project_dir)
        job = _pipeline(state)
        job_id = str(job.get("job_id") or "")
        if expected_job_id and job_id != expected_job_id:
            return None
        if job.get("status") == "completed":
            return None
        if job.get("status") == "running" and job.get("worker_token"):
            return None
        if job.get("status") not in {"queued", "failed"}:
            return None
        worker_token = uuid4().hex
        job.update({"status": "running", "worker_token": worker_token, "started_at": job.get("started_at") or _now(), "error": None, "gate": None})
        wb._save(project_dir, state)
        return job_id, worker_token


def run_avatar_review_preview_job(
    project_dir: Path,
    expected_job_id: str | None = None,
    *,
    overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    overrides = overrides or {}
    acquired = _acquire_worker(project_dir, expected_job_id)
    if acquired is None:
        return read_avatar_review_preview_job(project_dir)
    job_id, worker_token = acquired
    try:
        job = _read_internal(project_dir)
        start_stage = str(job.get("safe_resume_point") or job.get("stage") or "preflight")
        order = ["preflight", "voice", "avatar_generation", "avatar_alignment", "avatar_assembly", "visual_generation", "preview_render"]
        start_index = order.index(start_stage) if start_stage in order else 0

        context: dict[str, Any] | None = None
        if start_index <= 0:
            _phase_begin(project_dir, job_id, worker_token, "preflight", "正在验证本地模型清单、角色素材和付费合同")
            # The exact-frame v2 manifest is the cut authority.  Loading the
            # full Whisper model here provides no additional paid-task safety,
            # but can temporarily duplicate the resident TTS model and exhaust
            # host RAM before any provider work starts.  Freeze and verify the
            # local model inventory here; the later diagnostic stage attempts
            # the real load and already degrades safely to exact-frame cuts.
            context = _assert_frozen(project_dir, _read_internal(project_dir), load_whisper=False)
            context["roles"] = ((_read_internal(project_dir).get("frozen_input") or {}).get("roles") or {})
            _phase_complete(project_dir, job_id, worker_token, "preflight", "voice", {"asr": context["asr"], "status": "passed"})
        else:
            context = _assert_frozen(project_dir, _read_internal(project_dir), load_whisper=False)
            context["roles"] = ((_read_internal(project_dir).get("frozen_input") or {}).get("roles") or {})

        if start_index <= 1:
            _phase_begin(project_dir, job_id, worker_token, "voice", "正在串行生成雅雅、檬檬长配音")
            voice = _generate_voice_tracks(
                project_dir, job_id, worker_token, context,
                tts_factory=overrides.get("tts_factory"),
            )
            _validate_one_click_avatar_duration(project_dir, voice)
            _phase_complete(project_dir, job_id, worker_token, "voice", "avatar_generation", voice)
        else:
            voice = (((_read_internal(project_dir).get("phases") or {}).get("voice") or {}).get("output") or {})

        if start_index <= 2:
            _phase_begin(project_dir, job_id, worker_token, "avatar_generation", "正在串行生成 RunningHub 数字人 0/2")
            avatar = _generate_runninghub_avatars(
                project_dir, job_id, worker_token, context, voice,
                client_factory=overrides.get("runninghub_client_factory", RunningHubLongCatClient),
                poll_interval=float(overrides.get("poll_interval", POLL_INTERVAL_SECONDS)),
                poll_timeout=float(overrides.get("poll_timeout", POLL_TIMEOUT_SECONDS)),
            )
            _phase_complete(project_dir, job_id, worker_token, "avatar_generation", "avatar_alignment", avatar)
        else:
            avatar = (((_read_internal(project_dir).get("phases") or {}).get("avatar_generation") or {}).get("output") or {})

        if start_index <= 3:
            _phase_begin(project_dir, job_id, worker_token, "avatar_alignment", "正在验证精确帧切点并运行本地 Whisper 诊断")
            model_options = preflight_local_whisper(load_test=False)
            model_path = next(
                (item.get("id") for item in list_local_whisper_models()
                 if item.get("label") == model_options.get("model_id")
                 and Path(str(item.get("id") or "")).name == model_options.get("snapshot_revision")),
                None,
            )
            if not model_path:
                raise AvatarInputDriftError("冻结的 Whisper 模型已无法解析")
            package = _align_avatar_package(project_dir, avatar, str(model_path), voice)
            if not _cut_plan_ready(package):
                return _public(_set_cut_gate(project_dir, job_id, worker_token, package))
            _phase_complete(project_dir, job_id, worker_token, "avatar_alignment", "avatar_assembly", {
                "asr": package.get("asr"), "cut_plan": package.get("cut_plan"),
            })

        if start_index <= 4:
            _phase_begin(project_dir, job_id, worker_token, "avatar_assembly", "正在切割并合成数字人原声母版")
            assembly = _assemble_and_apply(project_dir)
            _phase_complete(project_dir, job_id, worker_token, "avatar_assembly", "visual_generation", assembly)

        if start_index <= 5:
            _phase_begin(project_dir, job_id, worker_token, "visual_generation", "正在复用批量补全画面生成主体画面")
            frozen = _read_internal(project_dir).get("frozen_input") or {}
            visuals = (
                overrides["visual_runner"](project_dir, job_id, worker_token, frozen)
                if overrides.get("visual_runner")
                else _generate_supporting_visuals(project_dir, job_id, worker_token, frozen)
            )
            _phase_complete(project_dir, job_id, worker_token, "visual_generation", "preview_render", visuals)

        if start_index <= 6:
            _phase_begin(project_dir, job_id, worker_token, "preview_render", "正在合成并校验完整有数字人审核预览")
            evidence = (
                overrides["preview_runner"](project_dir, job_id, worker_token)
                if overrides.get("preview_runner")
                else _render_preview(project_dir, job_id, worker_token)
            )
            _phase_complete(project_dir, job_id, worker_token, "preview_render", "review_ready", evidence)
        return _public(_complete(project_dir, job_id, worker_token, evidence))
    except (StaleAvatarReviewPreviewWorker, AvatarReviewPreviewConflict):
        return read_avatar_review_preview_job(project_dir)
    except Exception as exc:  # noqa: BLE001 - durable parent records the safe failure
        try:
            return _public(_fail(project_dir, job_id, worker_token, exc))
        except StaleAvatarReviewPreviewWorker:
            return read_avatar_review_preview_job(project_dir)


def _has_local_tail_repair_candidate(project_dir: Path, job: dict[str, Any]) -> bool:
    frozen = job.get("frozen_input") or {}
    if str((frozen.get("turn_timing") or {}).get("version") or "") != TURN_TIMING_MANIFEST_VERSION:
        return False
    if str(job.get("stage") or "") != "avatar_generation":
        return False
    roles = ((((job.get("phases") or {}).get("avatar_generation") or {}).get("output") or {}).get("roles") or {})
    operations = job.get("paid_operations") or {}
    project_root = project_dir.resolve()
    for record in roles.values():
        if not isinstance(record, dict) or record.get("status") != "failed":
            continue
        history = record.get("history") or []
        if not history or history[-1].get("terminal_reason") != "output_contract_drift":
            continue
        operation = operations.get(str(record.get("operation_id") or "")) or {}
        if (
            operation.get("settled") is not True
            or not str(operation.get("task_id") or "")
            or str(operation.get("task_id") or "") != str(record.get("task_id") or "")
        ):
            continue
        target = (project_dir / str(record.get("output_path") or "")).resolve()
        if target.is_relative_to(project_root) and target.is_file():
            return True
    return False


def resume_avatar_review_preview_job(project_dir: Path, job_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = payload or {}
    snapshot = _read_internal(project_dir)
    if (
        str(snapshot.get("job_id") or "") == str(job_id)
        and snapshot.get("status") == "failed"
        and str(snapshot.get("safe_resume_point") or snapshot.get("stage") or "") == "visual_generation"
    ):
        child = ((wb.read_workbench(project_dir).get("automation") or {}).get("visual_batch") or {})
        if child.get("status") in {"failed", "completed_with_failures"} and int(child.get("failed_slots") or 0) > 0:
            wb.requeue_failed_visual_batch(
                project_dir,
                expected_job_id=str(child.get("job_id") or ""),
                expected_parent_job_id=str(job_id),
                expected_request_fingerprint=str(snapshot.get("request_fingerprint") or ""),
            )
    with wb._project_transaction_lock(project_dir):
        state = wb._load_for_write(project_dir)
        job = _pipeline(state)
        if str(job.get("job_id") or "") != str(job_id):
            raise AvatarReviewPreviewConflict("任务编号已变化，拒绝恢复旧任务")
        if job.get("status") == "completed" and job.get("stage") == "review_ready":
            # Recovered failures remain in phase/history evidence, but the
            # headline counter describes the current terminal outcome.
            job.setdefault("counts", {})["completed"] = 7
            job["counts"]["failed"] = 0
            wb._save(project_dir, state)
            return _public(job)
        if job.get("status") == "ambiguous":
            raise AmbiguousAvatarOperation("供应商提交结果未知；请先核对 RunningHub 任务列表，不能自动恢复")
        if job.get("status") == "awaiting_human":
            package = read_avatar_package(project_dir) or {}
            if not _cut_plan_ready(package):
                raise AvatarReviewPreviewError("仍有数字人切点未批准；请先完成切点复核")
            phase = job.setdefault("phases", {}).setdefault("avatar_alignment", {})
            phase.update({"status": "completed", "finished_at": _now(), "retryable": False, "output": {"asr": package.get("asr"), "cut_plan": package.get("cut_plan")}})
            job["safe_resume_point"] = "avatar_assembly"
            job["stage"] = "avatar_assembly"
        elif job.get("status") != "failed":
            return _public(job)
        if not job.get("safe_resume_point") and _has_local_tail_repair_candidate(project_dir, job):
            phase = job.setdefault("phases", {}).setdefault("avatar_generation", {})
            phase.update({
                "retryable": True,
                "safe_resume_point": "avatar_generation",
            })
            job["stage"] = "avatar_generation"
            job["safe_resume_point"] = "avatar_generation"
        if not job.get("safe_resume_point"):
            raise AvatarReviewPreviewError("当前任务没有安全恢复点")
        job.update({"status": "queued", "worker_token": None, "error": None, "gate": None, "finished_at": None})
        wb._activity(state, "avatar_review_preview_resumed", f"有数字人一键任务 {job_id} 将从 {job.get('safe_resume_point')} 继续", job_id=job_id)
        wb._save(project_dir, state)
        return _public(job, launch_required=True)


def recover_avatar_review_preview_job(project_dir: Path) -> dict[str, Any]:
    job = _read_internal(project_dir)
    if not job.get("job_id") or job.get("status") in {"idle", "completed", "awaiting_human", "ambiguous"}:
        return _public(job)
    if job.get("status") == "running":
        def mutate(_state: dict[str, Any], current: dict[str, Any]) -> None:
            current.update({
                "status": "queued", "worker_token": None,
                "current": {"kind": "recovery", "id": current.get("stage"), "label": "服务重启后将从安全点恢复"},
                "safe_resume_point": current.get("safe_resume_point") or current.get("stage") or "preflight",
            })
        job = _mutate(project_dir, str(job["job_id"]), None, mutate)
    return _public(job, launch_required=job.get("status") == "queued")
