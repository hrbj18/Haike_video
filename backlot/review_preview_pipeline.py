"""Resumable parent job for a no-avatar human-review preview.

The parent owns orchestration and durable checkpoints only.  Media work is
delegated to the existing workbench functions and the sentence ledger.  Its
terminal state is ``preview_ready``; scene approval and formal rendering are
intentionally outside this module.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from backlot import audio_center
from backlot import narration_lines
from backlot import workbench as wb
from backlot.ai_text import read_text_ai_config
from tools.audio.voicebox_tts import VoiceboxTTS
from tools.video.hyperframes_compose import HyperFramesCompose
from tools.video.stock_sources.pexels import PexelsSource


PIPELINE_VERSION = "1.2"
ACTIVE_STATUSES = {"queued", "running", "awaiting_human"}
TERMINAL_STATUSES = {"failed", "cancelled", "completed"}
STAGES = (
    "preflight",
    "scene_plan",
    "line_plan",
    "narration",
    "audio_timeline",
    "subtitles",
    "visual_plan",
    "visual_generation",
    "audio_sample",
    "full_preview",
    "review_ready",
)
DEFAULT_VISUAL_INPUT = {
    "profile": "auto",
    "operation_mode": "fill_missing",
    "mix_strategy": "balanced",
    "image_source": "web_download",
    "person_policy": "balanced",
    "candidate_limit": 6,
    "planning_mode": "ai_director",
}
ALLOWED_NO_AVATAR_PIPELINES = {"animated-explainer"}


class ReviewPreviewError(ValueError):
    """A safe, user-facing parent-pipeline error."""


class ReviewPreviewConflict(ReviewPreviewError):
    """A stable conflict category for HTTP 409 integration."""


class StaleReviewPreviewWorker(ReviewPreviewConflict):
    """Raised when an obsolete worker attempts a state transition."""


class AmbiguousExternalOperation(ReviewPreviewError):
    """Signal that a network/chargeable submission outcome is unknown."""


class NonRetryableReviewPreviewError(ReviewPreviewError):
    """A frozen-input, format, or safety failure that requires a new job."""


class InputDriftError(NonRetryableReviewPreviewError):
    """The current project no longer matches the frozen job contract."""


class PreviewEvidenceError(ReviewPreviewError):
    """The review preview/report evidence must be rebuilt from full_preview."""


class CompletedAudioEvidenceError(NonRetryableReviewPreviewError):
    """Sentence or aggregate narration evidence vanished before completion."""


class VisualGenerationIncomplete(ReviewPreviewError):
    """One or more visual slots failed while completed slots were preserved."""

    def __init__(self, message: str, *, completed_slots: int, failed_slots: int) -> None:
        super().__init__(message)
        self.completed_slots = completed_slots
        self.failed_slots = failed_slots


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _json_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _request_fingerprint(frozen_input: dict[str, Any]) -> str:
    """Hash stable user intent; planned scene creation may refresh this atomically."""
    return _json_hash(
        {
            "versions": frozen_input["versions"],
            "project_type": frozen_input["project_type"],
            "script_hash": frozen_input["script_hash"],
            "voice": frozen_input["voice"],
            "visual": frozen_input["visual"],
            "visual_generation_required": frozen_input["visual_generation_required"],
            "authorizations": frozen_input["authorizations"],
            "audio_mix_signature": frozen_input["audio_mix_signature"],
            "subtitle_style_signature": (
                frozen_input.get("subtitle_style_signature") or frozen_input["subtitle_signature"]
            ),
            "render_profile_signature": frozen_input["render_profile_signature"],
        }
    )


def _matches_stable_request_intent(
    active: dict[str, Any],
    candidate: dict[str, Any],
) -> bool:
    stable_keys = (
        "versions",
        "project_type",
        "script_hash",
        "voice",
        "audio_mix_signature",
        "subtitle_style_signature",
        "render_profile_signature",
    )
    if any(active.get(key) != candidate.get(key) for key in stable_keys):
        return False

    dynamic_visual_keys = {
        "selection_mode",
        "scene_ids",
        "visual_target_scene_count",
        "visual_scope_pending_scene_plan",
        "visual_generation_required",
        "pexels_network",
        "text_ai_model",
        "hyperframes_fallback",
    }
    active_visual = active.get("visual") if isinstance(active.get("visual"), dict) else {}
    candidate_visual = (
        candidate.get("visual") if isinstance(candidate.get("visual"), dict) else {}
    )
    if {
        key: value for key, value in active_visual.items() if key not in dynamic_visual_keys
    } != {
        key: value for key, value in candidate_visual.items() if key not in dynamic_visual_keys
    }:
        return False

    candidate_required = bool(candidate.get("visual_generation_required"))
    active_authorizations = (
        active.get("authorizations") if isinstance(active.get("authorizations"), dict) else {}
    )
    if candidate_required and active_authorizations.get("pexels_network") is not True:
        return False
    if (
        candidate_required
        and candidate_visual.get("planning_mode") == "ai_director"
        and active_authorizations.get("text_ai") is not True
    ):
        return False
    return True


def _matches_pending_scene_plan_refreeze(
    job: dict[str, Any],
    candidate: dict[str, Any],
) -> bool:
    """Recognize the narrow duplicate-start race while a scene plan is being frozen.

    The scene-plan worker writes the deterministic scenes before it can CAS the
    parent job's anticipated section-id scope to the actual scene-id scope. A
    duplicate click in that interval must reuse the leased parent, while all
    ordinary active-job conflicts remain rejected.
    """
    active = job.get("frozen_input") if isinstance(job.get("frozen_input"), dict) else {}
    phase = (job.get("phases") or {}).get("scene_plan") or {}
    return bool(
        job.get("status") in ACTIVE_STATUSES
        and job.get("stage") == "scene_plan"
        and phase.get("status") == "running"
        and job.get("worker_token")
        and active.get("scene_plan_was_missing") is True
        and _matches_stable_request_intent(active, candidate)
    )


def _project_lock(project_dir: Path):
    return wb._project_transaction_lock(project_dir)


def _empty_job() -> dict[str, Any]:
    return {
        "version": PIPELINE_VERSION,
        "job_id": None,
        "script_hash": None,
        "input_fingerprint": None,
        "request_fingerprint": None,
        "status": "idle",
        "stage": "preflight",
        "counts": {"total": 0, "completed": 0, "failed": 0},
        "current": None,
        "gate": None,
        "error": None,
        "safe_resume_point": None,
        "result": None,
        "frozen_input": None,
        "phases": {},
        "worker_token": None,
    }


def default_review_preview_state() -> dict[str, Any]:
    """Public factory used by the workbench's additive state migration."""
    return _empty_job()


def _pipeline(state: dict[str, Any]) -> dict[str, Any]:
    automation = wb._automation(state)
    job = automation.get("review_preview_pipeline")
    if not isinstance(job, dict):
        job = _empty_job()
        automation["review_preview_pipeline"] = job
    defaults = _empty_job()
    for key, value in defaults.items():
        job.setdefault(key, deepcopy(value))
    if not job.get("script_hash") and isinstance(job.get("frozen_input"), dict):
        job["script_hash"] = job["frozen_input"].get("script_hash")
    phases = job.get("phases") if isinstance(job.get("phases"), dict) else {}
    job["phases"] = phases
    for stage, phase in phases.items():
        if not isinstance(phase, dict):
            phases[stage] = phase = {}
        phase.setdefault("status", "pending")
        phase.setdefault("attempts", 0)
        phase.setdefault("started_at", None)
        phase.setdefault("output", {})
        phase.setdefault("error", None)
        phase.setdefault("finished_at", None)
        phase.setdefault("input_fingerprint", job.get("input_fingerprint"))
        phase.setdefault("retryable", phase.get("status") != "completed")
        phase.setdefault("safe_resume_point", stage if phase.get("status") != "completed" else job.get("safe_resume_point"))
    return job


def _job_response(job: dict[str, Any], *, launch_required: bool = False) -> dict[str, Any]:
    response = deepcopy(job)
    response.pop("worker_token", None)
    response.pop("tts_terminal_retry_authorized", None)
    response["launch_required"] = bool(launch_required)
    return response


def _raw_project(project_dir: Path) -> dict[str, Any]:
    payload = wb._read_json(project_dir / "project.json") or {}
    return payload if isinstance(payload, dict) else {}


def _is_avatar_project_type(project_type: object) -> bool:
    return str(project_type or "") == wb.AVATAR_PIPELINE


def _is_allowed_project_type(project_type: object) -> bool:
    return str(project_type or "") in ALLOWED_NO_AVATAR_PIPELINES


def _project_type_error(project_dir: Path, job: dict[str, Any]) -> InputDriftError | None:
    current = str(_raw_project(project_dir).get("pipeline_type") or "")
    frozen = str((job.get("frozen_input") or {}).get("project_type") or "")
    if current == frozen and _is_allowed_project_type(current):
        return None
    return InputDriftError(
        f"项目类型已从冻结的“{frozen or '未知'}”变为“{current or '未知'}”；"
        "仅 animated-explainer 可继续，请重新预检启动新任务"
    )


def _current_contract_versions() -> dict[str, str]:
    return {
        "pipeline": PIPELINE_VERSION,
        "line_planner": narration_lines.PLANNER_VERSION,
        "line_ledger": narration_lines.LEDGER_VERSION,
    }


def _frozen_version_error(job: dict[str, Any]) -> InputDriftError | None:
    frozen_versions = (job.get("frozen_input") or {}).get("versions")
    if frozen_versions == _current_contract_versions():
        return None
    return InputDriftError("审核预览管线或逐句账本版本已升级；旧语义任务禁止续跑，请重新预检启动新任务")


def _assert_project_type_unchanged(project_dir: Path, job: dict[str, Any]) -> None:
    error = _project_type_error(project_dir, job)
    if error is not None:
        raise error
    version_error = _frozen_version_error(job)
    if version_error is not None:
        raise version_error


def _script_from_project(project_dir: Path, state: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    script = wb._read_json(project_dir / "artifacts" / "script.json") or {}
    if not isinstance(script, dict):
        script = {}
    project = state.get("project") if isinstance(state.get("project"), dict) else {}
    draft = project.get("script_draft") if isinstance(project.get("script_draft"), dict) else {}
    intake = project.get("intake") if isinstance(project.get("intake"), dict) else {}
    approved = bool(script) and (
        draft.get("status") == "approved" or intake.get("script_status") == "draft_approved"
    )
    return script, approved


def _script_review_status(state: dict[str, Any], approved: bool) -> str:
    project = state.get("project") if isinstance(state.get("project"), dict) else {}
    draft = project.get("script_draft") if isinstance(project.get("script_draft"), dict) else {}
    intake = project.get("intake") if isinstance(project.get("intake"), dict) else {}
    return "approved" if approved else str(draft.get("status") or intake.get("script_status") or "missing")


def _safe_status_value(value: object) -> str:
    return str(getattr(value, "value", value) or "unavailable")


def collect_review_preview_capabilities(
    *,
    include_visual_runtime: bool = True,
) -> dict[str, Any]:
    """Collect capability facts without returning credentials or invoking media.

    HyperFrames performs an npm/CLI runtime probe and the visual providers may
    inspect network-backed configuration.  A project whose frozen local
    visuals are already complete must not touch any of those paths merely to
    prove that TTS and FFmpeg are ready.
    """
    persisted = audio_center._load()
    tool = VoiceboxTTS()
    status = _safe_status_value(tool.get_status())
    profiles: list[dict[str, Any]] = []
    if status == "available":
        try:
            profiles = list(tool.list_profiles())
        except Exception:
            status = "unavailable"
    ffmpeg = wb._ffmpeg_available()
    ffprobe = wb._ffprobe_available(ffmpeg)
    if include_visual_runtime:
        pexels_available = bool(PexelsSource().is_available())
        text_ai = read_text_ai_config()
        try:
            hyperframes_info = HyperFramesCompose().get_info()
            hyperframes_runtime = (
                hyperframes_info.get("hyperframes_runtime")
                if isinstance(hyperframes_info.get("hyperframes_runtime"), dict)
                else {}
            )
            hyperframes_status = "available" if hyperframes_runtime.get("runtime_available") else "unavailable"
        except Exception:
            hyperframes_status = "unavailable"
            hyperframes_runtime = {
                "reason_code": "hyperframes_probe_failed",
                "user_message": "HyperFrames 运行时检查失败。请重启工作台服务后重新预检。",
                "reasons": ["runtime probe raised an exception"],
            }
    else:
        pexels_available = False
        text_ai = {}
        hyperframes_status = "skipped_not_required"
        hyperframes_runtime = {}
    return {
        "tts": {
            "available": status == "available",
            "status": status,
            "profiles": [
                {
                    "id": item.get("id"),
                    "name": item.get("name"),
                    "language": item.get("language"),
                    "voice_type": item.get("voice_type"),
                    "default_engine": item.get("default_engine"),
                    "voice_signature": item.get("voice_signature"),
                }
                for item in profiles
                if isinstance(item, dict)
            ],
            "persisted_default_profile_id": persisted.get("default_profile_id"),
            "explicit_default": bool(
                persisted.get("default_profile_id") and persisted.get("default_updated_at")
            ),
        },
        "ffmpeg": {"available": bool(ffmpeg), "path": ffmpeg},
        "ffprobe": {"available": bool(ffprobe), "path": ffprobe},
        "pexels": {
            "available": pexels_available,
            "network": True,
            "paid": False,
            "status": "checked" if include_visual_runtime else "skipped_not_required",
        },
        "text_ai": {
            "available": bool(text_ai.get("configured")),
            "status": (
                "available"
                if text_ai.get("configured")
                else ("unavailable" if include_visual_runtime else "skipped_not_required")
            ),
            "provider": text_ai.get("provider") or "configured_text_ai",
            "model": text_ai.get("model"),
            "network": True,
        },
        "hyperframes": {
            "available": hyperframes_status == "available",
            "status": hyperframes_status,
            "reason_code": hyperframes_runtime.get("reason_code"),
            "user_message": (
                hyperframes_runtime.get("user_message")
                or (
                    "当前项目不需要补画面，已跳过 HyperFrames 检查。"
                    if hyperframes_status == "skipped_not_required"
                    else "HyperFrames 当前不可用，请修复本地运行时后重新预检。"
                )
            ),
            "version": hyperframes_runtime.get("npm_package_version"),
            "source": hyperframes_runtime.get("cli_source"),
            "diagnostics": {
                "node_major": hyperframes_runtime.get("node_major"),
                "ffmpeg_available": hyperframes_runtime.get("ffmpeg_available"),
                "cli_probe_status": hyperframes_runtime.get("cli_probe_status"),
                "reasons": [str(value)[:240] for value in hyperframes_runtime.get("reasons") or []],
            },
        },
        "avatar": {"used": False, "providers": []},
    }


def _freeze_voice(capabilities: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    tts = capabilities.get("tts") if isinstance(capabilities.get("tts"), dict) else {}
    profiles = [item for item in tts.get("profiles") or [] if isinstance(item, dict)]
    explicit = bool(tts.get("explicit_default"))
    selected: dict[str, Any] | None
    if explicit:
        selected_id = str(tts.get("persisted_default_profile_id") or "")
        selected = next((item for item in profiles if str(item.get("id") or "") == selected_id), None)
        if selected is None:
            return None, "通用配音中心中用户明确选择的默认音色已不存在，请重新选择后再试"
    else:
        selected = next((item for item in profiles if str(item.get("name") or "") == "雅雅"), None)
        if selected is None:
            return None, "未检测到精确名称为“雅雅”的内置音色；未显式选择其他音色时禁止静默回退"
    frozen = {
        "provider": "voicebox_tts",
        "profile_id": selected.get("id"),
        "profile_name": selected.get("name"),
        "engine": selected.get("default_engine") or "qwen_custom_voice",
        "voice_type": selected.get("voice_type") or "preset",
        "voice_signature": selected.get("voice_signature"),
        "selection_source": "explicit_global_default" if explicit else "required_yaya_default",
    }
    frozen["fingerprint"] = narration_lines.voice_fingerprint(frozen)
    return frozen, None


def _visual_input(payload: dict[str, Any] | None) -> dict[str, Any]:
    raw = payload.get("visual") if isinstance(payload, dict) and isinstance(payload.get("visual"), dict) else {}
    visual = {**DEFAULT_VISUAL_INPUT, **deepcopy(raw)}
    if visual.get("image_source") != "web_download":
        raise ReviewPreviewError("无数字人口播一键预览禁止使用 OpenAI 生图；图片来源只能是已审核网络素材")
    if visual.get("planning_mode") not in {"rule_mix", "ai_director"}:
        raise ReviewPreviewError("画面规划方式只能是规则混合或已明确授权的 AI 智能导演")
    for forbidden in ("avatar", "runninghub", "dashscope_avatar", "openai_image"):
        if visual.get(forbidden):
            raise ReviewPreviewError("无数字人口播一键预览禁止数字人和 OpenAI 生图能力")
    return visual


def _visual_generation_scope(state: dict[str, Any], script: dict[str, Any] | None = None) -> dict[str, Any]:
    """Resolve the exact fill-missing scope before any provider operation.

    An approved script may not have a persisted scene plan yet.  The scene
    builder deterministically binds one scene to each script section, so those
    section ids are a truthful, conservative preflight scope.  Once scenes
    exist, the scope uses the real scene ids and final renderability checks.
    """
    scenes = [item for item in state.get("scenes") or [] if isinstance(item, dict)]
    pending_scene_plan = not bool(scenes)
    if scenes:
        scene_ids = [
            str(scene.get("id") or "")
            for scene in scenes
            if (
                str(scene.get("id") or "")
                and wb._scene_needs_main_visual(scene)
                and not wb._scene_has_renderable_visual(state, scene)
            )
        ]
    else:
        sections = [
            item
            for item in ((script or {}).get("sections") or [])
            if isinstance(item, dict)
        ]
        scene_ids = [str(item.get("id") or "") for item in sections if str(item.get("id") or "")]
    scene_ids = list(dict.fromkeys(scene_ids))
    return {
        "required": bool(scene_ids),
        "scene_ids": scene_ids,
        "scene_count": len(scene_ids),
        "pending_scene_plan": pending_scene_plan and bool(scene_ids),
    }


def _visual_strategy_with_scope(
    visual: dict[str, Any],
    scope: dict[str, Any],
    *,
    text_ai_model: object = None,
) -> dict[str, Any]:
    required = bool(scope.get("required"))
    planning_mode = str(visual.get("planning_mode") or "ai_director")
    return {
        **deepcopy(visual),
        "operation_mode": "fill_missing",
        "selection_mode": "custom",
        "scene_ids": list(scope.get("scene_ids") or []),
        "visual_target_scene_count": int(scope.get("scene_count") or 0),
        "visual_scope_pending_scene_plan": bool(scope.get("pending_scene_plan")),
        "openai_image": False,
        "avatar": False,
        "visual_generation_required": required,
        "pexels_network": required,
        "text_ai_model": text_ai_model if required and planning_mode == "ai_director" else None,
        "hyperframes_fallback": required,
    }


def review_preview_preflight(
    project_dir: Path,
    payload: dict[str, Any] | None = None,
    *,
    capabilities: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a truthful, non-secret readiness report for the frozen contract."""
    payload = payload or {}
    raw_project = _raw_project(project_dir)
    project_type = str(raw_project.get("pipeline_type") or "")
    if _is_avatar_project_type(project_type):
        return {
            "ready": False,
            "blockers": ["当前项目是数字人口播项目；本流程硬性拒绝且不会读取或修改数字人状态"],
            "warnings": [],
            "project_type": project_type,
            "script_review_status": "not_applicable",
            "script_hash": None,
            "line_count": 0,
            "visual_generation_required": False,
            "visual_target_scene_ids": [],
            "visual_target_scene_count": 0,
            "visual_scope_pending_scene_plan": False,
            "frozen_voice": None,
            "capabilities": {"avatar": {"used": False, "providers": []}},
            "visual_strategy": {"avatar": False, "openai_image": False},
            "music_gate": None,
        }
    if not _is_allowed_project_type(project_type):
        return {
            "ready": False,
            "blockers": [f"当前 pipeline_type“{project_type or '未知'}”不是明确支持的无数字人口播类型；禁止按旧版或未知合同运行"],
            "warnings": [],
            "project_type": project_type,
            "script_review_status": "unknown",
            "script_hash": None,
            "line_count": 0,
            "visual_generation_required": False,
            "visual_target_scene_ids": [],
            "visual_target_scene_count": 0,
            "visual_scope_pending_scene_plan": False,
            "frozen_voice": None,
            "capabilities": {"avatar": {"used": False, "providers": []}},
            "visual_strategy": {"avatar": False, "openai_image": False},
            "music_gate": None,
        }
    state = wb.read_workbench(project_dir)
    script, approved = _script_from_project(project_dir, state)
    visual_scope = _visual_generation_scope(state, script)
    visual_generation_required = bool(visual_scope["required"])
    script_review_status = _script_review_status(state, approved)
    script_hash = _json_hash(script) if script else None
    sections = script.get("sections") if isinstance(script.get("sections"), list) else []
    if script:
        try:
            narration_lines.build_line_plan(sections, {})
        except narration_lines.NarrationLineError as exc:
            raise ReviewPreviewError(f"正式脚本合同无效：{exc}") from exc
    capability_report = (
        deepcopy(capabilities)
        if capabilities is not None
        else collect_review_preview_capabilities(
            include_visual_runtime=visual_generation_required,
        )
    )
    blockers: list[str] = []
    warnings: list[str] = []
    if not approved:
        blockers.append("请先完成人工脚本审核并通过正式脚本草案")
    frozen_voice, voice_error = _freeze_voice(capability_report)
    if not (capability_report.get("tts") or {}).get("available"):
        blockers.append("OpenMontage 本地 Qwen3-TTS 当前不可用")
    if voice_error:
        blockers.append(voice_error)
    if frozen_voice is not None and not frozen_voice.get("voice_signature"):
        blockers.append("冻结音色缺少 voice_signature，无法保证可恢复 TTS 使用同一音色")
    if not (capability_report.get("ffmpeg") or {}).get("available"):
        blockers.append("本机未发现 FFmpeg，无法拼接逐句音频或生成审核预览")
    if not (capability_report.get("ffprobe") or {}).get("available"):
        blockers.append("本机未发现 ffprobe，无法验证真实音频和预览媒体")
    if visual_generation_required and not (capability_report.get("pexels") or {}).get("available"):
        blockers.append("Pexels 尚未配置；在任何画面任务开始前必须阻断")
    visual = _visual_input(payload)
    if visual_generation_required and visual["planning_mode"] == "ai_director":
        if not (capability_report.get("text_ai") or {}).get("available"):
            blockers.append("已选择 AI 智能导演，但项目文本模型尚未配置")
        else:
            warnings.append("AI 智能导演会调用已配置的文本模型，开始任务时需冻结明确授权")
    elif visual_generation_required:
        warnings.append("当前使用规则混合画面规划，不调用文本模型")
    else:
        warnings.append("所有场景已有合格本地画面；本次复用冻结素材，不调用文本模型、Pexels 或 HyperFrames")
    if visual_generation_required and not (capability_report.get("hyperframes") or {}).get("available"):
        hyperframes = capability_report.get("hyperframes") or {}
        blockers.append(
            str(hyperframes.get("user_message") or "HyperFrames 当前不可用；画面主路线或 Pexels 安全回退无法执行")
        )

    try:
        plan = narration_lines.build_line_plan(sections, frozen_voice or {}) if script and frozen_voice else {"line_count": 0}
    except narration_lines.NarrationLineError as exc:
        raise ReviewPreviewError(f"正式脚本合同无效：{exc}") from exc
    if approved and not plan.get("line_count"):
        blockers.append("正式脚本没有可配音的有效句子")
    music_gate = _audio_gate_policy(state, frozen_voice)
    visual_strategy = _visual_strategy_with_scope(
        visual,
        visual_scope,
        text_ai_model=(capability_report.get("text_ai") or {}).get("model"),
    )
    return {
        "ready": not blockers,
        "blockers": blockers,
        "warnings": warnings,
        "project_type": project_type,
        "script_review_status": script_review_status,
        "script_hash": script_hash,
        "line_count": int(plan.get("line_count") or 0),
        "visual_generation_required": visual_generation_required,
        "visual_target_scene_ids": list(visual_scope["scene_ids"]),
        "visual_target_scene_count": int(visual_scope["scene_count"]),
        "visual_scope_pending_scene_plan": bool(visual_scope["pending_scene_plan"]),
        "frozen_voice": frozen_voice,
        "capabilities": capability_report,
        "visual_strategy": visual_strategy,
        "music_gate": music_gate,
        "will_pause_for_audio_sample": bool(music_gate["will_pause"]),
        "declaration": "不调用 RunningHub、DashScope 数字人、其他数字人服务、ASR 或 OpenAI 生图",
    }


def _manual_conflicts(state: dict[str, Any]) -> list[str]:
    automation = wb._automation(state)
    conflicts: list[str] = []
    checks = (
        ("narration_generation", {"generating"}, "手动项目旁白"),
        ("visual_batch", {"queued", "generating"}, "手动画面批量"),
        ("preview_render", {"generating"}, "手动全片预览"),
        ("preview_sync", {"queued", "generating"}, "片段审核预览同步"),
    )
    for key, statuses, label in checks:
        if str((automation.get(key) or {}).get("status") or "") in statuses:
            conflicts.append(label)
    sample = (wb._ensure_music_policy(state).get("sample") or {})
    if sample.get("status") == "generating":
        conflicts.append("手动声音样板")
    for scene in state.get("scenes") or []:
        narration = scene.get("narration") if isinstance(scene, dict) and isinstance(scene.get("narration"), dict) else {}
        if (narration.get("job") or {}).get("status") in {"queued", "generating", "rendering"}:
            conflicts.append("场景配音")
            break
    return conflicts


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _persisted_workbench_revision(project_dir: Path) -> str | None:
    """Return an exact revision of the durable workbench, never derived state.

    ``read_workbench()`` intentionally adds compatibility projections in
    memory.  Those projections must not participate in optimistic concurrency
    control because they are not a durable project mutation.  Hashing the
    persisted bytes gives start() a stable CAS revision while still detecting
    every real workbench write between its snapshot and locked commit.
    """
    path = project_dir / wb.WORKBENCH_FILE
    return _sha256_file(path) if path.is_file() else None


def _scene_contract(project_dir: Path, state: dict[str, Any], script: dict[str, Any], script_hash: str) -> dict[str, Any]:
    sections = [item for item in script.get("sections") or [] if isinstance(item, dict)]
    section_ids = [str(item.get("id") or "") for item in sections]
    if not section_ids or any(not item for item in section_ids) or len(set(section_ids)) != len(section_ids):
        raise NonRetryableReviewPreviewError("正式脚本 section_id 缺失或重复，无法建立唯一分镜合同；请修复脚本后启动新任务")
    scenes = [item for item in state.get("scenes") or [] if isinstance(item, dict)]
    scene_sections = [str(item.get("script_section_id") or "") for item in scenes]
    if len(scenes) != len(sections) or scene_sections != section_ids or len(set(scene_sections)) != len(scene_sections):
        raise NonRetryableReviewPreviewError(
            "现有分镜与冻结脚本的分段集合或顺序不一致，无法保证音频、画面和字幕同序消费；请重建分镜后启动新任务"
        )
    artifact = wb._read_json(project_dir / "artifacts" / "scene_plan.json")
    recorded_hash = str((state.get("project") or {}).get("scene_plan_script_hash") or "")
    if isinstance(artifact, dict):
        recorded_hash = str(artifact.get("script_sha256") or recorded_hash)
    if recorded_hash != script_hash:
        raise NonRetryableReviewPreviewError("现有分镜不能证明来自当前冻结脚本；请确定性重建分镜后启动新任务")
    return {
        "script_hash": script_hash,
        "mapping": [{"scene_id": str(scene.get("id") or ""), "section_id": str(scene.get("script_section_id") or "")} for scene in scenes],
    }


def _current_input_contract(project_dir: Path, state: dict[str, Any], script: dict[str, Any]) -> dict[str, Any]:
    script_hash = _json_hash(script)
    # Avoid mutating the workbench while hashing.  Only already-persisted
    # review-affecting fields are included.
    scenes = [item for item in state.get("scenes") or [] if isinstance(item, dict)]
    visual_rows: list[dict[str, Any]] = []
    asset_lookup = {str(item.get("id") or ""): item for item in state.get("assets") or [] if isinstance(item, dict)}
    selected = [item for item in state.get("usages") or [] if isinstance(item, dict) and item.get("selected")]
    for scene in scenes:
        scene_id = str(scene.get("id") or "")
        asset_rows: list[dict[str, Any]] = []
        for usage in selected:
            if str(usage.get("scene_id") or "") != scene_id or str(usage.get("role") or "") not in {"visual", "visual_block"}:
                continue
            asset = asset_lookup.get(str(usage.get("asset_id") or "")) or {}
            relative = str(asset.get("path") or "")
            path = project_dir / relative if relative else None
            asset_rows.append(
                {
                    "usage": {key: usage.get(key) for key in ("id", "role", "asset_id", "block_id")},
                    "asset": {key: asset.get(key) for key in ("id", "type", "path", "duration_seconds", "source_type")},
                    "file": {
                        "size_bytes": path.stat().st_size,
                        "sha256": _sha256_file(path),
                    } if path is not None and path.is_file() else None,
                }
            )
        timeline = deepcopy(scene.get("visual_timeline") or {})
        if isinstance(timeline, dict):
            timeline.pop("updated_at", None)
            timeline.pop("revision", None)
            timeline["blocks"] = [
                {
                    key: value
                    for key, value in block.items()
                    if key not in {"start_seconds", "end_seconds", "duration_seconds", "updated_at"}
                }
                for block in timeline.get("blocks") or []
                if isinstance(block, dict)
            ]
        visual_rows.append(
            {
                "scene_id": scene_id,
                "section_id": str(scene.get("script_section_id") or ""),
                "visual_timeline": timeline,
                "assets": sorted(asset_rows, key=lambda item: str((item.get("usage") or {}).get("id") or "")),
            }
        )
    project = state.get("project") if isinstance(state.get("project"), dict) else {}
    return {
        "script_hash": script_hash,
        "scene_visual_signature": _json_hash(visual_rows),
        "subtitle_signature": _json_hash(
            {
                "styles": state.get("subtitle_styles"),
                "scenes": [
                    {
                        "scene_id": str(scene.get("id") or ""),
                        "template_id": (scene.get("subtitles") or {}).get("template_id"),
                        "style_override": deepcopy((scene.get("subtitles") or {}).get("style_override") or {}),
                    }
                    for scene in scenes
                ],
            }
        ),
        "render_profile_signature": _json_hash({"settings": state.get("settings"), "render_profile": project.get("render_profile")}),
        "audio_mix_signature": wb._audio_mix_signature(state),
    }


def _probe_preview_evidence(project_dir: Path, preview_path: str, report_path: str) -> dict[str, Any]:
    preview_file = project_dir / preview_path
    report_file = project_dir / report_path
    if not preview_path or not preview_file.is_file() or not report_file.is_file():
        raise PreviewEvidenceError("全片审核预览或报告不存在")
    report = wb._read_json(report_file) or {}
    if not isinstance(report, dict) or report.get("status") != "completed":
        raise PreviewEvidenceError("全片审核预览报告无效或尚未完成")
    probe = wb._probe_video(preview_file, wb._ffmpeg_available())
    streams = probe.get("streams") if isinstance(probe, dict) and isinstance(probe.get("streams"), list) else []
    duration = float(((probe or {}).get("format") or {}).get("duration") or 0) if isinstance(probe, dict) else 0
    if (
        not isinstance(probe, dict)
        or not any(item.get("codec_type") == "video" for item in streams if isinstance(item, dict))
        or not any(item.get("codec_type") == "audio" for item in streams if isinstance(item, dict))
        or duration <= 0
    ):
        raise PreviewEvidenceError("全片审核预览媒体探测未发现完整音视频流")
    return {
        "preview_sha256": _sha256_file(preview_file),
        "preview_size_bytes": preview_file.stat().st_size,
        "report_sha256": _sha256_file(report_file),
        "media_probe": deepcopy(probe),
    }


def _completed_audio_evidence_valid(
    project_dir: Path,
    state: dict[str, Any],
    job: dict[str, Any],
) -> bool:
    """Validate the sentence ledger and project narration before cache reuse."""
    frozen = job.get("frozen_input") if isinstance(job.get("frozen_input"), dict) else {}
    versions = frozen.get("versions") if isinstance(frozen.get("versions"), dict) else {}
    if versions != _current_contract_versions():
        return False
    ledger_path = project_dir / narration_lines.LEDGER_PATH
    if not ledger_path.is_file():
        return False
    try:
        ledger = narration_lines.load_ledger(project_dir)
        expected_plan = narration_lines.build_line_plan(
            (frozen.get("script") or {}).get("sections") or [],
            frozen.get("voice") or {},
        )
    except narration_lines.NarrationLineError:
        return False
    if (
        ledger.get("version") != versions.get("line_ledger")
        or ledger.get("planner_version") != versions.get("line_planner")
        or ledger.get("parent_job_id") != job.get("job_id")
        or ledger.get("plan_fingerprint") != expected_plan.get("plan_fingerprint")
        or (((job.get("phases") or {}).get("line_plan") or {}).get("output") or {}).get("plan_fingerprint")
        != expected_plan.get("plan_fingerprint")
    ):
        return False
    expected_lines = expected_plan.get("lines") or []
    actual_lines = ledger.get("lines") or []
    if len(actual_lines) != len(expected_lines) or int(ledger.get("completed_count") or 0) != len(expected_lines):
        return False
    actual_by_id = {
        str(item.get("line_id") or ""): item
        for item in actual_lines
        if isinstance(item, dict) and item.get("line_id")
    }
    if len(actual_by_id) != len(expected_lines):
        return False
    ordered_records: list[dict[str, Any]] = []
    for planned in expected_lines:
        record = actual_by_id.get(str(planned.get("line_id") or ""))
        if (
            not record
            or record.get("status") != "completed"
            or record.get("input_fingerprint") != planned.get("input_fingerprint")
        ):
            return False
        try:
            path, relative = narration_lines._safe_line_output(project_dir, record.get("output_path"))
            media = narration_lines.inspect_pcm_wav(path)
        except narration_lines.NarrationLineError:
            return False
        if relative != record.get("output_path") or any(media.get(key) != record.get(key) for key in media):
            return False
        ordered_records.append(record)
    ordered_records.sort(key=lambda item: int(item.get("project_ordinal") or 0))
    aggregate_fingerprint = _json_hash(
        [
            {"input_fingerprint": item.get("input_fingerprint"), "sha256": item.get("sha256")}
            for item in ordered_records
        ]
    )
    expected_duration = sum(float(item.get("duration_seconds") or 0) for item in ordered_records)
    narration = wb._automation(state).get("narration_generation") or {}
    raw_audio_path = str(narration.get("audio_path") or "").replace("\\", "/")
    candidate = Path(raw_audio_path)
    if not raw_audio_path or candidate.is_absolute() or ".." in candidate.parts:
        return False
    aggregate_path = (project_dir / candidate).resolve()
    try:
        aggregate_path.relative_to(project_dir.resolve())
        aggregate_media = narration_lines.inspect_pcm_wav(aggregate_path)
    except (ValueError, narration_lines.NarrationLineError):
        return False
    audio_phase = (((job.get("phases") or {}).get("audio_timeline") or {}).get("output") or {})
    return bool(
        narration.get("status") == "completed"
        and narration.get("line_ledger_path") == narration_lines.LEDGER_PATH.as_posix()
        and _aggregate_media_reusable(
            aggregate_media,
            narration,
            aggregate_fingerprint,
            expected_duration,
        )
        and audio_phase.get("audio_path") == raw_audio_path
        and audio_phase.get("sha256") == aggregate_media.get("sha256")
        and abs(float(audio_phase.get("duration_seconds") or 0) - expected_duration)
        <= max(0.05, expected_duration * 0.001)
    )


def _preview_result_valid(project_dir: Path, state: dict[str, Any], job: dict[str, Any]) -> bool:
    result = job.get("result") if isinstance(job.get("result"), dict) else {}
    if result.get("readiness") != "preview_ready":
        return False
    preview_job = wb._automation(state).get("preview_render") or {}
    if preview_job.get("status") != "completed" or preview_job.get("needs_refresh"):
        return False
    preview_path = str(result.get("preview_path") or "")
    report_path = str(result.get("report_path") or "")
    preview_file = project_dir / preview_path
    report_file = project_dir / report_path
    if not preview_file.is_file() or not report_file.is_file():
        return False
    report = wb._read_json(report_file) or {}
    probe = result.get("media_probe") if isinstance(result.get("media_probe"), dict) else {}
    streams = probe.get("streams") if isinstance(probe.get("streams"), list) else []
    evidence_valid = (
        report.get("status") == "completed"
        and any(item.get("codec_type") == "video" for item in streams if isinstance(item, dict))
        and any(item.get("codec_type") == "audio" for item in streams if isinstance(item, dict))
        and float((probe.get("format") or {}).get("duration") or 0) > 0
    )
    return bool(
        evidence_valid
        and _completed_audio_evidence_valid(project_dir, state, job)
        and _sha256_file(preview_file) == result.get("preview_sha256")
        and _sha256_file(report_file) == result.get("report_sha256")
        and preview_file.stat().st_size == result.get("preview_size_bytes")
    )


def start_review_preview_job(
    project_dir: Path,
    payload: dict[str, Any],
    *,
    capabilities: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create or reuse one idempotent project-local parent job."""
    if payload.get("confirmed") is not True:
        raise ReviewPreviewError("请先确认预检摘要后再生成审核预览")
    visual = _visual_input(payload)
    preflight = review_preview_preflight(project_dir, payload, capabilities=capabilities)
    if not preflight["ready"]:
        raise ReviewPreviewError("；".join(preflight["blockers"]))
    revision_before_read = _persisted_workbench_revision(project_dir)
    state = wb.read_workbench(project_dir)
    state_revision = _persisted_workbench_revision(project_dir)
    if revision_before_read != state_revision:
        raise ReviewPreviewConflict("项目输入在读取期间已变化，请基于最新状态重新开始")
    script, _approved = _script_from_project(project_dir, state)
    visual_scope = _visual_generation_scope(state, script)
    visual_generation_required = bool(visual_scope["required"])
    if visual_generation_required != bool(preflight.get("visual_generation_required")):
        raise ReviewPreviewConflict("画面就绪状态在预检后已变化，请基于最新状态重新开始")
    if (
        list(visual_scope["scene_ids"]) != list(preflight.get("visual_target_scene_ids") or [])
        or bool(visual_scope["pending_scene_plan"])
        != bool(preflight.get("visual_scope_pending_scene_plan"))
    ):
        raise ReviewPreviewConflict("待补画面范围在预检后已变化，请基于最新状态重新开始")
    if visual_generation_required and payload.get("network_confirmed") is not True:
        raise ReviewPreviewError("画面生产会使用 Pexels 网络检索，请明确确认后再开始")
    if (
        visual_generation_required
        and visual["planning_mode"] == "ai_director"
        and payload.get("text_ai_confirmed") is not True
    ):
        raise ReviewPreviewError("AI 智能导演会调用已配置文本模型，请明确确认后再开始")
    input_contract = _current_input_contract(project_dir, state, script)
    frozen_input = {
        "versions": _current_contract_versions(),
        "project_type": preflight["project_type"],
        "script": deepcopy(script),
        "script_hash": preflight["script_hash"],
        "voice": deepcopy(preflight["frozen_voice"]),
        "visual": deepcopy(preflight["visual_strategy"]),
        "authorizations": {
            "pexels_network": visual_generation_required,
            "text_ai": visual_generation_required and visual["planning_mode"] == "ai_director",
            "openai_image": False,
            "avatar": False,
        },
        "audio_mix_signature": wb._audio_mix_signature(state),
        "scene_visual_signature": input_contract["scene_visual_signature"],
        "visual_generation_required": visual_generation_required,
        "scene_plan_was_missing": not bool(state.get("scenes")),
        "subtitle_signature": input_contract["subtitle_signature"],
        "subtitle_style_signature": _json_hash(state.get("subtitle_styles")),
        "render_profile_signature": input_contract["render_profile_signature"],
        "music_gate": deepcopy(preflight["music_gate"]),
    }
    request_fingerprint = _request_fingerprint(frozen_input)
    fingerprint = _json_hash(frozen_input)
    snapshot_job = deepcopy(_pipeline(state))
    completed_contract_is_current = bool(
        snapshot_job.get("status") == "completed"
        and _preview_result_valid(project_dir, state, snapshot_job)
        and _json_hash(input_contract)
        == str((snapshot_job.get("result") or {}).get("cache_input_fingerprint") or "")
    )
    exact_completed_request = bool(
        snapshot_job.get("input_fingerprint") == fingerprint
        and snapshot_job.get("request_fingerprint") == request_fingerprint
    )
    completed_visual_fill_request = bool(
        (snapshot_job.get("frozen_input") or {}).get("visual_generation_required") is True
        and frozen_input.get("visual_generation_required") is False
        and _matches_stable_request_intent(snapshot_job.get("frozen_input") or {}, frozen_input)
    )
    completed_cache_reusable = bool(
        completed_contract_is_current
        and (exact_completed_request or completed_visual_fill_request)
    )
    with _project_lock(project_dir):
        locked_revision = _persisted_workbench_revision(project_dir)
        if locked_revision != state_revision:
            raise ReviewPreviewConflict("项目输入在预检后已变化，请基于最新状态重新开始")
        state = wb._load_for_write(project_dir)
        job = _pipeline(state)
        if job.get("status") in ACTIVE_STATUSES:
            if job.get("request_fingerprint") == request_fingerprint:
                return _job_response(job, launch_required=False)
            if _matches_pending_scene_plan_refreeze(job, frozen_input):
                return _job_response(job, launch_required=False)
            raise ReviewPreviewConflict("同一项目已有一键审核预览任务运行或等待人工处理，请先完成或取消当前任务")
        if (
            completed_cache_reusable
            and job.get("job_id") == snapshot_job.get("job_id")
            and locked_revision == state_revision
        ):
            return _job_response(job, launch_required=False)
        locked_script, _locked_approved = _script_from_project(project_dir, state)
        locked_project_type = str(_raw_project(project_dir).get("pipeline_type") or "")
        if (
            _json_hash(locked_script) != frozen_input["script_hash"]
            or locked_project_type != frozen_input["project_type"]
        ):
            raise ReviewPreviewConflict("项目输入在预检后已变化，请基于最新状态重新开始")
        conflicts = _manual_conflicts(state)
        if conflicts:
            raise ReviewPreviewConflict("当前存在互斥任务：" + "、".join(conflicts))
        job_id = f"RPP-{uuid4().hex[:12]}"
        new_job = _empty_job()
        new_job.update(
            {
                "job_id": job_id,
                "script_hash": preflight["script_hash"],
                "input_fingerprint": fingerprint,
                "request_fingerprint": request_fingerprint,
                "status": "queued",
                "stage": "preflight",
                "counts": {"total": preflight["line_count"], "completed": 0, "failed": 0},
                "current": {"kind": "stage", "id": "preflight", "label": "预检已通过，等待执行"},
                "safe_resume_point": "preflight",
                "frozen_input": frozen_input,
                "preflight": deepcopy(preflight),
                "created_at": _now(),
                "updated_at": _now(),
            }
        )
        wb._automation(state)["review_preview_pipeline"] = new_job
        wb._activity(state, "review_preview_pipeline_started", f"一键审核预览任务 {job_id} 已建立；终点仅为待审预览", job_id=job_id)
        wb._save(project_dir, state)
        return _job_response(new_job, launch_required=True)


def _read_job_internal(project_dir: Path) -> dict[str, Any]:
    raw_project = _raw_project(project_dir)
    if _is_avatar_project_type(raw_project.get("pipeline_type")):
        return _empty_job()
    state = wb.read_workbench(project_dir)
    return deepcopy(_pipeline(state))


def read_review_preview_job(project_dir: Path) -> dict[str, Any]:
    return _job_response(_read_job_internal(project_dir), launch_required=False)


def _mutate_job(
    project_dir: Path,
    job_id: str,
    worker_token: str | None,
    mutator: Callable[[dict[str, Any], dict[str, Any]], None],
) -> dict[str, Any]:
    with _project_lock(project_dir):
        state = wb._load_for_write(project_dir)
        job = _pipeline(state)
        if job.get("job_id") != job_id:
            raise StaleReviewPreviewWorker("旧 worker 对应的父任务已被替换，拒绝写入")
        if worker_token is not None and job.get("worker_token") != worker_token:
            raise StaleReviewPreviewWorker("旧 worker 租约已失效，拒绝写入")
        mutator(state, job)
        job["updated_at"] = _now()
        wb._save(project_dir, state)
        return deepcopy(job)


def _is_current_worker(project_dir: Path, job_id: str, worker_token: str) -> bool:
    state = wb.read_workbench(project_dir)
    job = deepcopy(_pipeline(state))
    current = (
        job.get("job_id") == job_id
        and job.get("worker_token") == worker_token
        and job.get("status") == "running"
    )
    if not current:
        return False
    _assert_project_type_unchanged(project_dir, job)
    return True


def _assert_worker_execution_contract(
    project_dir: Path,
    job_id: str,
    worker_token: str,
) -> dict[str, Any]:
    """Revalidate the lease and frozen project type immediately before long work."""
    state = wb.read_workbench(project_dir)
    job = deepcopy(_pipeline(state))
    if (
        job.get("job_id") != job_id
        or job.get("worker_token") != worker_token
        or job.get("status") != "running"
    ):
        raise StaleReviewPreviewWorker("父任务租约已变化，拒绝启动旧 worker 的子任务")
    _assert_project_type_unchanged(project_dir, job)
    return job


def _commit_line_ledger(
    project_dir: Path,
    job_id: str,
    worker_token: str,
    action: Callable[[], None],
) -> None:
    """Commit one WAV promotion and ledger replace inside the parent lease."""
    with _project_lock(project_dir):
        state = wb._load_for_write(project_dir)
        job = _pipeline(state)
        if job.get("job_id") != job_id or job.get("worker_token") != worker_token or job.get("status") != "running":
            raise narration_lines.StaleNarrationWorker("旧逐句配音 worker 租约已失效，拒绝覆盖账本")
        _assert_project_type_unchanged(project_dir, job)
        action()


def _phase_begin(project_dir: Path, job_id: str, worker_token: str, stage: str, label: str) -> dict[str, Any]:
    def mutate(_state: dict[str, Any], job: dict[str, Any]) -> None:
        phase = job.setdefault("phases", {}).setdefault(stage, {})
        phase.update(
            {
                "status": "running",
                "attempts": int(phase.get("attempts") or 0) + 1,
                "started_at": _now(),
                "finished_at": None,
                "error": None,
                "input_fingerprint": job.get("input_fingerprint"),
                "retryable": True,
                "safe_resume_point": stage,
            }
        )
        job["stage"] = stage
        job["safe_resume_point"] = stage
        job["current"] = {"kind": "stage", "id": stage, "label": label}
    return _mutate_job(project_dir, job_id, worker_token, mutate)


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
        phase.update(
            {
                "status": "completed",
                "finished_at": _now(),
                "output": deepcopy(output or {}),
                "error": None,
                "input_fingerprint": job.get("input_fingerprint"),
                "retryable": False,
                "safe_resume_point": next_stage,
            }
        )
        job["stage"] = next_stage
        job["safe_resume_point"] = next_stage
        job["current"] = {"kind": "stage", "id": next_stage, "label": next_stage}
        if stage == "narration":
            job.pop("tts_terminal_retry_authorized", None)
    return _mutate_job(project_dir, job_id, worker_token, mutate)


def _fail_job(
    project_dir: Path,
    job_id: str,
    worker_token: str,
    error: Exception,
    *,
    ambiguous: bool = False,
) -> dict[str, Any]:
    retryable = (
        not ambiguous
        and not isinstance(error, (NonRetryableReviewPreviewError, narration_lines.NarrationEvidenceDriftError))
    )
    def mutate(_state: dict[str, Any], job: dict[str, Any]) -> None:
        stage = str(job.get("stage") or "preflight")
        resume_stage = (
            "full_preview"
            if isinstance(error, PreviewEvidenceError)
            else "narration"
            if isinstance(error, CompletedAudioEvidenceError)
            else stage
        )
        message = str(error or "一键审核预览任务失败")[:1200]
        phase = job.setdefault("phases", {}).setdefault(stage, {})
        phase.update(
            {
                "status": "awaiting_human" if ambiguous else "failed",
                "finished_at": _now(),
                "error": message,
                "input_fingerprint": job.get("input_fingerprint"),
                "retryable": retryable,
                "safe_resume_point": resume_stage,
            }
        )
        job["status"] = "awaiting_human" if ambiguous else "failed"
        job["worker_token"] = None
        job["stage"] = resume_stage
        job["safe_resume_point"] = resume_stage
        job["error"] = {
            # Keep the public error category stable for existing clients;
            # structured `code` and slot counts carry the new subtype detail.
            "type": "ReviewPreviewError" if isinstance(error, VisualGenerationIncomplete) else type(error).__name__,
            "message": message,
            "retryable": retryable,
            "ambiguous_external_operation": ambiguous,
        }
        if isinstance(error, VisualGenerationIncomplete):
            job["error"].update(
                {
                    "code": "visual_generation_incomplete",
                    "preserved_completed_slots": error.completed_slots,
                    "retry_failed_slots": error.failed_slots,
                    "required_action": "修复本地画面运行时后，从安全点继续；系统只重试失败画面",
                }
            )
        if ambiguous:
            job["gate"] = {
                "reason": "外部任务提交结果不明，禁止自动重提",
                "required_action": "确认供应商侧任务状态后再选择继续",
                "stage": stage,
            }
        if not retryable:
            _cleanup_owned_active_children(_state, job, message)
        job["counts"]["failed"] = int(job.get("counts", {}).get("failed") or 0) + 1
    return _mutate_job(project_dir, job_id, worker_token, mutate)


def _cleanup_owned_active_children(state: dict[str, Any], job: dict[str, Any], message: str) -> None:
    """Fail only active children provably owned by this frozen parent contract."""
    automation = wb._automation(state)
    job_id = job.get("job_id")
    request_fingerprint = job.get("request_fingerprint")
    input_fingerprint = job.get("input_fingerprint")
    frozen_audio = str((job.get("frozen_input") or {}).get("audio_mix_signature") or "")
    child_error = f"父任务已安全终止：{message}"[:1200]

    visual = automation.get("visual_batch") if isinstance(automation.get("visual_batch"), dict) else {}
    if (
        visual.get("status") in {"queued", "generating"}
        and visual.get("parent_job_id") == job_id
        and visual.get("request_fingerprint") == request_fingerprint
    ):
        visual.update({"status": "failed", "finished_at": _now(), "error": child_error})

    preview = automation.get("preview_render") if isinstance(automation.get("preview_render"), dict) else {}
    if (
        preview.get("status") == "generating"
        and preview.get("parent_job_id") == job_id
        and preview.get("input_fingerprint") == input_fingerprint
    ):
        preview.update({"status": "failed", "finished_at": _now(), "error": child_error})

    sample = wb._ensure_music_policy(state).get("sample") or {}
    if (
        sample.get("status") == "generating"
        and sample.get("parent_job_id") == job_id
        and sample.get("request_fingerprint") == request_fingerprint
        and sample.get("policy_signature") == frozen_audio
    ):
        sample.update({"status": "failed", "generated_at": _now(), "error": child_error})


def _record_nonretryable_failure_locked(
    state: dict[str, Any],
    job: dict[str, Any],
    error: Exception,
) -> None:
    """Terminate a queued parent before any worker lease or child side effect."""
    stage = str(job.get("stage") or "preflight")
    message = str(error or "冻结输入已变化，请重新预检启动新任务")[:1200]
    job.setdefault("phases", {}).setdefault(stage, {}).update(
        {
            "status": "failed",
            "finished_at": _now(),
            "error": message,
            "input_fingerprint": job.get("input_fingerprint"),
            "retryable": False,
            "safe_resume_point": stage,
        }
    )
    job.update(
        {
            "status": "failed",
            "worker_token": None,
            "safe_resume_point": stage,
            "gate": None,
            "error": {
                "type": type(error).__name__,
                "message": message,
                "retryable": False,
                "ambiguous_external_operation": False,
            },
            "updated_at": _now(),
        }
    )
    _cleanup_owned_active_children(state, job, message)
    job.setdefault("counts", {})["failed"] = int((job.get("counts") or {}).get("failed") or 0) + 1


def _dependencies(overrides: dict[str, Any] | None) -> dict[str, Any]:
    defaults: dict[str, Any] = {
        "generate_scene_plan": wb.generate_scene_plan_from_script,
        "synthesize_line": None,
        "tts_client": VoiceboxTTS(),
        "concat_audio": wb._concat_audio,
        "preview_visual_plan": wb.preview_visual_batch_plan,
        "start_visual_generation": wb.start_visual_batch_generation,
        "generate_visuals": wb.generate_visual_batch,
        "start_audio_sample": wb.start_music_sample,
        "generate_audio_sample": wb.generate_music_sample,
        "approve_audio_sample": wb.approve_music_sample,
        "start_full_preview": wb.start_full_preview_render,
        "generate_full_preview": wb.generate_full_preview_render,
        "probe_preview": _probe_preview_evidence,
        "collect_capabilities": collect_review_preview_capabilities,
        "requeue_failed_visuals": wb.requeue_failed_visual_batch,
    }
    defaults.update(overrides or {})
    return defaults


def _assert_script_unchanged(project_dir: Path, job: dict[str, Any]) -> None:
    current = wb._read_json(project_dir / "artifacts" / "script.json") or {}
    expected = (job.get("frozen_input") or {}).get("script_hash")
    if not expected or _json_hash(current) != expected:
        raise InputDriftError("正式脚本在任务开始后发生变化；已保留完成成果，请重新启动新的审核预览任务")


def _assert_audio_contract_unchanged(project_dir: Path, job: dict[str, Any]) -> None:
    state = wb.read_workbench(project_dir)
    expected = str((job.get("frozen_input") or {}).get("audio_mix_signature") or "")
    if expected and wb._audio_mix_signature(state) != expected:
        raise InputDriftError("人物增益或背景音乐设置在任务开始后发生变化；已保留成果，请重新启动新的审核预览任务")


def _frozen_visual_reuse_error(
    project_dir: Path,
    job: dict[str, Any],
    *,
    state: dict[str, Any] | None = None,
    current_contract: dict[str, Any] | None = None,
) -> InputDriftError | None:
    """Fail closed when a frozen zero-network visual contract no longer holds."""
    frozen = job.get("frozen_input") if isinstance(job.get("frozen_input"), dict) else {}
    if frozen.get("visual_generation_required") is not False:
        return None
    current_state = state if isinstance(state, dict) else wb.read_workbench(project_dir)
    contract = current_contract or _current_input_contract(
        project_dir,
        current_state,
        frozen.get("script") or {},
    )
    if (
        _needs_visual_generation(current_state)
        or contract["scene_visual_signature"] != frozen.get("scene_visual_signature")
    ):
        return InputDriftError(
            "任务冻结为复用本地画面的零网络路线，但所选画面已缺失或变化；"
            "禁止临时联网补图，请重新预检启动新任务"
        )
    return None


def _assert_static_inputs_unchanged(project_dir: Path, job: dict[str, Any]) -> None:
    state = wb.read_workbench(project_dir)
    script = (job.get("frozen_input") or {}).get("script") or {}
    current = _current_input_contract(project_dir, state, script)
    frozen = job.get("frozen_input") or {}
    if current["subtitle_signature"] != frozen.get("subtitle_signature"):
        raise InputDriftError("字幕样式在任务开始后发生变化；请重新启动新的审核预览任务")
    if current["render_profile_signature"] != frozen.get("render_profile_signature"):
        raise InputDriftError("渲染规格在任务开始后发生变化；请重新启动新的审核预览任务")
    visual_error = _frozen_visual_reuse_error(
        project_dir,
        job,
        state=state,
        current_contract=current,
    )
    if visual_error is not None:
        raise visual_error


def _subtitle_cues_for_line_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cues: list[dict[str, Any]] = []
    cursor = 0.0
    cue_index = 0
    for record in records:
        duration = max(0.001, float(record.get("duration_seconds") or 0))
        phrases = wb._split_subtitle_phrases(str(record.get("text") or ""))
        if not phrases:
            cursor += duration
            continue
        weights = [max(1, len(re.sub(r"[^\w\u4e00-\u9fff]", "", phrase))) for phrase in phrases]
        total_weight = sum(weights)
        line_end = cursor + duration
        phrase_cursor = cursor
        for phrase_position, (phrase, weight) in enumerate(zip(phrases, weights)):
            cue_index += 1
            cue_end = line_end if phrase_position == len(phrases) - 1 else phrase_cursor + duration * weight / total_weight
            cues.append(
                {
                    "id": f"LC-{cue_index:04d}",
                    "line_id": record.get("line_id"),
                    "start_seconds": round(phrase_cursor, 3),
                    "end_seconds": round(cue_end, 3),
                    "text": phrase,
                }
            )
            phrase_cursor = cue_end
        cursor = line_end
    return cues


def _validate_line_ledger_contract(
    project_dir: Path,
    job: dict[str, Any],
    worker_token: str,
    ledger: dict[str, Any],
) -> None:
    if ledger.get("parent_job_id") != job.get("job_id") or ledger.get("worker_token") != worker_token:
        raise InputDriftError("逐句音频账本不属于当前父任务租约；请启动新的审核预览任务")
    planned = (((job.get("phases") or {}).get("line_plan") or {}).get("output") or {}).get("plan_fingerprint")
    if not planned or ledger.get("plan_fingerprint") != planned:
        raise InputDriftError("逐句音频账本与冻结拆句计划不一致；请启动新的审核预览任务")
    for record in ledger.get("lines") or []:
        if record.get("status") != "completed":
            continue
        path, relative = narration_lines._safe_line_output(project_dir, record.get("output_path"))
        media = narration_lines.inspect_pcm_wav(path)
        if relative != record.get("output_path") or any(media.get(key) != record.get(key) for key in media):
            raise InputDriftError("逐句音频或媒体证据在生成后发生漂移；请启动新的审核预览任务")


def _aggregate_media_reusable(
    media: dict[str, Any],
    evidence: dict[str, Any],
    input_fingerprint: str,
    expected_duration_seconds: float,
) -> bool:
    """Require complete persisted evidence before reusing an aggregate WAV."""
    tolerance = max(0.05, expected_duration_seconds * 0.001)
    try:
        recorded_duration = float(evidence.get("expected_duration_seconds"))
        actual_duration = float(media.get("duration_seconds"))
    except (TypeError, ValueError):
        return False
    return bool(
        evidence.get("aggregate_input_fingerprint") == input_fingerprint
        and evidence.get("aggregate_sha256") == media.get("sha256")
        and abs(recorded_duration - expected_duration_seconds) <= tolerance
        and abs(actual_duration - expected_duration_seconds) <= tolerance
    )


def _prepare_aggregate_audio(
    project_dir: Path,
    output: Path,
    parts: list[Path],
    input_fingerprint: str,
    expected_duration_seconds: float,
    evidence: dict[str, Any],
    job_id: str,
    worker_token: str,
    deps: dict[str, Any],
) -> tuple[dict[str, Any], Path | None]:
    if output.is_file():
        try:
            existing = narration_lines.inspect_pcm_wav(output)
        except narration_lines.NarrationLineError:
            existing = None
        if existing and _aggregate_media_reusable(existing, evidence, input_fingerprint, expected_duration_seconds):
            return existing, None
    output.parent.mkdir(parents=True, exist_ok=True)
    safe_token = re.sub(r"[^A-Za-z0-9_-]+", "", worker_token)[:16]
    temporary = output.with_name(f".{output.stem}-{job_id}-{safe_token}.tmp.wav")
    temporary.unlink(missing_ok=True)
    deps["concat_audio"](project_dir, parts, output_path=temporary)
    try:
        media = narration_lines.inspect_pcm_wav(temporary)
    except narration_lines.NarrationLineError as exc:
        temporary.unlink(missing_ok=True)
        raise narration_lines.NarrationOutputValidationError(
            f"本次新聚合音频校验失败，可从音频时间线阶段安全重试：{exc}"
        ) from exc
    tolerance = max(0.05, expected_duration_seconds * 0.001)
    if abs(float(media.get("duration_seconds") or 0) - expected_duration_seconds) > tolerance:
        temporary.unlink(missing_ok=True)
        raise narration_lines.NarrationOutputValidationError(
            "本次新聚合音频时长与逐句实测总时长不一致，可从音频时间线阶段安全重试"
        )
    return media, temporary


def _aggregate_line_audio(
    project_dir: Path,
    job_id: str,
    worker_token: str,
    job: dict[str, Any],
    ledger: dict[str, Any],
    deps: dict[str, Any],
) -> dict[str, Any]:
    state = wb.read_workbench(project_dir)
    frozen_voice = deepcopy((job.get("frozen_input") or {}).get("voice") or {})
    completed = [item for item in ledger.get("lines") or [] if item.get("status") == "completed"]
    if len(completed) != len(ledger.get("lines") or []):
        raise ReviewPreviewError("逐句音频尚未全部完成，不能建立真实时间线")
    if len({str(item.get("line_id") or "") for item in completed}) != len(completed):
        raise NonRetryableReviewPreviewError("逐句账本包含重复 line_id，无法保证每句只消费一次；请启动新任务")
    by_section: dict[str, list[dict[str, Any]]] = {}
    for record in completed:
        by_section.setdefault(str(record.get("section_id") or ""), []).append(record)
    for records in by_section.values():
        records.sort(key=lambda item: int(item.get("line_ordinal") or 0))

    scenes = [scene for scene in state.get("scenes") or [] if isinstance(scene, dict)]
    if not scenes:
        raise ReviewPreviewError("没有场景可承载逐句音频")
    scene_sections = [str(scene.get("script_section_id") or "") for scene in scenes]
    if len(set(scene_sections)) != len(scene_sections) or set(scene_sections) != set(by_section):
        raise NonRetryableReviewPreviewError("分镜与逐句账本不能一一对应，禁止按位置错绑；请重建分镜并启动新任务")

    bundles: list[dict[str, Any]] = []
    for scene in scenes:
        section_id = str(scene.get("script_section_id") or "")
        records = by_section.get(section_id)
        if not records:
            raise ReviewPreviewError(f"{scene.get('id')} 没有对应的逐句音频")
        parts = [narration_lines._safe_line_output(project_dir, record.get("output_path"))[0] for record in records]
        aggregate_fingerprint = _json_hash(
            [{"input_fingerprint": record["input_fingerprint"], "sha256": record.get("sha256")} for record in records]
        )
        expected_duration = sum(float(record.get("duration_seconds") or 0) for record in records)
        scene_output = project_dir / "assets" / "audio" / "voicebox" / "scenes" / f"{scene['id']}-{aggregate_fingerprint[:12]}.wav"
        scene_relative = wb._safe_relpath(project_dir, str(scene_output))
        prior_asset = next(
            (
                asset for asset in state.get("assets") or []
                if isinstance(asset, dict) and asset.get("path") == scene_relative
            ),
            {},
        )
        media, temporary = _prepare_aggregate_audio(
            project_dir,
            scene_output,
            parts,
            aggregate_fingerprint,
            expected_duration,
            (prior_asset.get("generation") or {}) if isinstance(prior_asset, dict) else {},
            job_id,
            worker_token,
            deps,
        )
        bundles.append(
            {
                "scene_id": str(scene.get("id") or ""),
                "records": records,
                "fingerprint": aggregate_fingerprint,
                "output": scene_output,
                "media": media,
                "temporary": temporary,
                "expected_duration_seconds": expected_duration,
            }
        )

    ordered = sorted(completed, key=lambda item: int(item.get("project_ordinal") or 0))
    project_parts = [narration_lines._safe_line_output(project_dir, item.get("output_path"))[0] for item in ordered]
    project_fingerprint = _json_hash(
        [{"input_fingerprint": item["input_fingerprint"], "sha256": item.get("sha256")} for item in ordered]
    )
    project_expected_duration = sum(float(item.get("duration_seconds") or 0) for item in ordered)
    project_output = project_dir / "assets" / "audio" / "voicebox" / f"project-narration-{project_fingerprint[:12]}.wav"
    project_evidence = wb._automation(state).get("narration_generation") or {}
    project_media, project_temporary = _prepare_aggregate_audio(
        project_dir,
        project_output,
        project_parts,
        project_fingerprint,
        project_expected_duration,
        project_evidence,
        job_id,
        worker_token,
        deps,
    )

    # Long media operations are complete.  Reload the newest workbench and
    # merge only this stage's audio/timeline fields under a lease CAS.
    with _project_lock(project_dir):
        state = wb._load_for_write(project_dir)
        active = _pipeline(state)
        if active.get("job_id") != job_id or active.get("worker_token") != worker_token or active.get("status") != "running":
            raise StaleReviewPreviewWorker("父任务租约已变化，拒绝提交聚合音频")
        if _json_hash(narration_lines.load_ledger(project_dir)) != _json_hash(ledger):
            raise InputDriftError("逐句音频账本在聚合期间发生变化；请启动新的审核预览任务")
        _validate_line_ledger_contract(project_dir, active, worker_token, ledger)
        latest_scenes = {str(item.get("id") or ""): item for item in state.get("scenes") or [] if isinstance(item, dict)}
        if set(latest_scenes) != {item["scene_id"] for item in bundles}:
            raise InputDriftError("场景集合在音频聚合期间发生变化；请启动新的审核预览任务")
        for bundle in bundles:
            if bundle["temporary"] is not None:
                os.replace(bundle["temporary"], bundle["output"])
        if project_temporary is not None:
            os.replace(project_temporary, project_output)
        for bundle in bundles:
            scene = latest_scenes[bundle["scene_id"]]
            records = bundle["records"]
            aggregate_fingerprint = bundle["fingerprint"]
            scene_output = bundle["output"]
            media = bundle["media"]
            if str(scene.get("script_section_id") or "") != str(records[0].get("section_id") or ""):
                raise InputDriftError("场景与脚本分段在音频聚合期间发生变化；请启动新的审核预览任务")
            existing_asset = next(
                (
                    asset
                    for asset in state.get("assets") or []
                    if isinstance(asset, dict)
                    and (asset.get("generation") or {}).get("line_plan_fingerprint") == aggregate_fingerprint
                    and asset.get("path") == wb._safe_relpath(project_dir, str(scene_output))
                ),
                None,
            )
            asset = existing_asset or wb._append_asset(
                project_dir,
                state,
                {
                    "name": f"{scene.get('title') or scene['id']} · {frozen_voice.get('profile_name')}逐句旁白",
                    "type": "audio",
                    "source_type": "local_generated",
                    "path": str(scene_output),
                    "duration_seconds": media["duration_seconds"],
                    "provider": "OpenMontage 本地配音",
                    "source_tool": "voicebox_tts",
                    "license": "本机 Qwen3-TTS 生成；按项目发布规范复核",
                    "generation": {
                        "profile_id": frozen_voice.get("profile_id"),
                        "profile_name": frozen_voice.get("profile_name"),
                        "voice_fingerprint": frozen_voice.get("fingerprint"),
                        "scene_id": scene.get("id"),
                        "line_ids": [record["line_id"] for record in records],
                        "line_plan_fingerprint": aggregate_fingerprint,
                        "generated_at": _now(),
                        "timing_mode": "measured_sentence_audio",
                    },
                },
            )
            asset["duration_seconds"] = media["duration_seconds"]
            generation = asset.get("generation") if isinstance(asset.get("generation"), dict) else {}
            generation.update(
                {
                    "aggregate_input_fingerprint": aggregate_fingerprint,
                    "aggregate_sha256": media["sha256"],
                    "expected_duration_seconds": bundle["expected_duration_seconds"],
                    "line_plan_fingerprint": aggregate_fingerprint,
                }
            )
            asset["generation"] = generation
            narration = scene.get("narration") if isinstance(scene.get("narration"), dict) else wb._scene_narration_default()
            scene["narration"] = narration
            current = next(
                (
                    version
                    for version in narration.get("versions") or []
                    if version.get("line_plan_fingerprint") == aggregate_fingerprint
                ),
                None,
            )
            if current is None:
                current = {
                    "id": wb._narration_version_id(scene),
                    "status": "candidate",
                    "text": "".join(str(record.get("text") or "") for record in records),
                    "asset_id": asset["id"],
                    "audio_path": asset["path"],
                    "profile_id": frozen_voice.get("profile_id"),
                    "profile_name": frozen_voice.get("profile_name"),
                    "duration_seconds": media["duration_seconds"],
                    "raw_duration_seconds": media["duration_seconds"],
                    "timing_mode": "measured_sentence_audio",
                    "line_ids": [record["line_id"] for record in records],
                    "line_plan_fingerprint": aggregate_fingerprint,
                    "subtitle_cues": _subtitle_cues_for_line_records(records),
                    "created_at": _now(),
                    "source": "review_preview_sentence_ledger",
                }
                narration.setdefault("versions", []).append(current)
            else:
                current.update(
                    {
                        "duration_seconds": media["duration_seconds"],
                        "raw_duration_seconds": media["duration_seconds"],
                        "subtitle_cues": _subtitle_cues_for_line_records(records),
                        "audio_path": asset["path"],
                    }
                )
            wb._promote_scene_narration_version(state, scene, str(current["id"]))

        timeline_update = wb._commit_narration_timeline(state, reason="review_preview_sentence_ledger")
        visual_timing = wb._refresh_visual_timing_status(project_dir, state)
        automation = wb._automation(state)
        automation["voice"] = {
        "provider": "voicebox_tts",
        "source": "review_preview_frozen_input",
        "label": frozen_voice.get("profile_name"),
        "profile_id": frozen_voice.get("profile_id"),
        "profile_name": frozen_voice.get("profile_name"),
        "default_engine": frozen_voice.get("engine"),
        "voice_fingerprint": frozen_voice.get("fingerprint"),
    }
        automation["narration_generation"].update(
        {
            "status": "completed",
            "stage": "audio_timeline",
            "finished_at": _now(),
            "completed_scenes": len(bundles),
            "total_scenes": len(bundles),
            "audio_path": wb._safe_relpath(project_dir, str(project_output)),
            "line_ledger_path": narration_lines.LEDGER_PATH.as_posix(),
            "line_plan_fingerprint": project_fingerprint,
            "aggregate_input_fingerprint": project_fingerprint,
            "aggregate_sha256": project_media["sha256"],
            "expected_duration_seconds": project_expected_duration,
            "timeline_update": timeline_update,
            "visual_timing": visual_timing,
            "error": "",
        }
    )
        automation["status"] = "narration_ready"
        automation["render"] = {"status": "awaiting_assets", "runtime": "ffmpeg", "output_path": None, "error": ""}
        wb._mark_render_needs_refresh(state, "逐句旁白与真实音频时间线已更新")
        wb._save(project_dir, state)
    return {
        "audio_path": wb._safe_relpath(project_dir, str(project_output)),
        "duration_seconds": project_media["duration_seconds"],
        "sha256": project_media["sha256"],
        "timeline_update": timeline_update,
    }


def _write_sentence_subtitles(project_dir: Path, job_id: str, worker_token: str) -> dict[str, Any]:
    snapshot = wb.read_workbench(project_dir)
    final_path = project_dir / "assets" / "subtitles.srt"
    safe_token = re.sub(r"[^A-Za-z0-9_-]+", "", worker_token)[:16]
    temporary = final_path.with_name(f".{final_path.stem}-{job_id}-{safe_token}.tmp.srt")
    snapshot_signature = _json_hash(
        {"scenes": snapshot.get("scenes") or [], "subtitle_styles": snapshot.get("subtitle_styles")}
    )
    try:
        wb._write_subtitles(
            project_dir,
            snapshot.get("scenes") or [],
            wb._script_sections(project_dir, snapshot),
            output_path=temporary,
        )
        subtitle_sha256 = _sha256_file(temporary)
        with _project_lock(project_dir):
            state = wb._load_for_write(project_dir)
            job = _pipeline(state)
            if job.get("job_id") != job_id or job.get("worker_token") != worker_token or job.get("status") != "running":
                raise StaleReviewPreviewWorker("父任务租约已变化，拒绝提交字幕")
            current_signature = _json_hash(
                {"scenes": state.get("scenes") or [], "subtitle_styles": state.get("subtitle_styles")}
            )
            if current_signature != snapshot_signature:
                raise InputDriftError("字幕生成期间场景或字幕样式发生变化；请启动新的审核预览任务")
            final_path.parent.mkdir(parents=True, exist_ok=True)
            os.replace(temporary, final_path)
            relative = wb._safe_relpath(project_dir, str(final_path))
            automation = wb._automation(state)
            automation["narration_generation"]["subtitle_path"] = relative
            automation["narration_generation"]["stage"] = "ready_to_render"
            wb._save(project_dir, state)
        return {"subtitle_path": relative, "sha256": subtitle_sha256}
    finally:
        temporary.unlink(missing_ok=True)


def _needs_visual_generation(state: dict[str, Any]) -> bool:
    return any(
        wb._scene_needs_main_visual(scene) and not wb._scene_has_renderable_visual(state, scene)
        for scene in state.get("scenes") or []
        if isinstance(scene, dict)
    )


def _audio_gate_policy(
    state: dict[str, Any],
    frozen_voice: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the explicit sample-gate policy for one frozen parent run.

    A new no-avatar project deliberately inherits the workstation's built-in
    Yaya narration gain.  That known default is part of the one-click product
    promise, so it must not create a surprise human pause merely because the
    calibrated gain is non-zero.  Any project-level gain edit, non-Yaya voice,
    or enabled BGM restores the existing real-mix audition gate.
    """
    narration_policy = wb._ensure_narration_policy(state)
    music_policy = wb._ensure_music_policy(state)
    narration_gain = wb.clamp_narration_gain_db(narration_policy.get("playback_gain_db"))
    music_enabled = bool(music_policy.get("enabled"))
    voice_name = str((frozen_voice or {}).get("profile_name") or "").strip()
    unity_mix = narration_gain == 0.0 and not music_enabled
    trusted_default = bool(
        not unity_mix
        and not music_enabled
        and not narration_policy.get("updated_at")
        and voice_name == "雅雅"
    )
    required = bool(not unity_mix and not trusted_default)
    if trusted_default:
        reason = "使用项目内置雅雅默认配音、继承的人声增益且未启用背景音乐，无需声音样板暂停"
    elif unity_mix:
        reason = "人物增益为 0 dB 且背景音乐关闭，无需声音样板暂停"
    elif music_enabled:
        reason = "已启用背景音乐，需要先试听第一段真实混音样板"
    elif narration_policy.get("updated_at"):
        reason = "项目人物增益已由用户调整，需要先试听第一段声音样板"
    else:
        reason = "当前音色不是项目内置雅雅默认配音，需要先试听第一段声音样板"
    return {
        "required": required,
        "requires_human_gate": required,
        "will_pause": required,
        "will_pause_for_audio_sample": required,
        "trusted_default": trusted_default,
        "reason": reason,
        "narration_gain_db": narration_gain,
        "background_music_enabled": music_enabled,
        "auto_approve": False,
    }


def _audio_gate_required(
    state: dict[str, Any], frozen_voice: dict[str, Any] | None = None
) -> bool:
    return bool(_audio_gate_policy(state, frozen_voice)["required"])


def _complete_parent(project_dir: Path, job_id: str, worker_token: str, deps: dict[str, Any]) -> dict[str, Any]:
    # Preview probing may invoke ffprobe and must never hold the shared project
    # transaction lock.  The mutation below revalidates the exact evidence
    # hashes and the lease before committing completion.
    state_snapshot = wb.read_workbench(project_dir)
    job_snapshot = deepcopy(_pipeline(state_snapshot))
    if (
        job_snapshot.get("job_id") != job_id
        or job_snapshot.get("worker_token") != worker_token
        or job_snapshot.get("status") != "running"
    ):
        raise StaleReviewPreviewWorker("父任务租约已变化，拒绝完成旧审核预览")
    _assert_project_type_unchanged(project_dir, job_snapshot)
    preview_snapshot = wb._automation(state_snapshot).get("preview_render") or {}
    preview_path = str(preview_snapshot.get("output_path") or "")
    report_path = str(preview_snapshot.get("report_path") or wb.AUTOMATION_PREVIEW_RENDER_REPORT)
    if preview_snapshot.get("status") != "completed" or preview_snapshot.get("needs_refresh"):
        raise PreviewEvidenceError("全片审核预览未完成或已需要刷新，不能标记为就绪")
    if not _completed_audio_evidence_valid(project_dir, state_snapshot, job_snapshot):
        raise CompletedAudioEvidenceError("逐句账本或项目旁白证据缺失/漂移，禁止标记审核预览就绪")
    evidence = deps["probe_preview"](project_dir, preview_path, report_path)
    frozen_script = (job_snapshot.get("frozen_input") or {}).get("script") or {}
    cache_input_fingerprint = _json_hash(_current_input_contract(project_dir, state_snapshot, frozen_script))

    def mutate(state: dict[str, Any], job: dict[str, Any]) -> None:
        if not _completed_audio_evidence_valid(project_dir, state, job):
            raise CompletedAudioEvidenceError("逐句账本或项目旁白证据在完成提交前缺失/漂移")
        preview = wb._automation(state).get("preview_render") or {}
        if (
            preview.get("status") != "completed"
            or preview.get("needs_refresh")
            or str(preview.get("output_path") or "") != preview_path
            or str(preview.get("report_path") or wb.AUTOMATION_PREVIEW_RENDER_REPORT) != report_path
        ):
            raise PreviewEvidenceError("全片审核预览状态在探测期间变化，请从全片预览阶段恢复")
        preview_file = project_dir / preview_path
        report_file = project_dir / report_path
        if not preview_file.is_file() or not report_file.is_file():
            raise PreviewEvidenceError("全片审核预览证据在提交前丢失，请从全片预览阶段恢复")
        if (
            _sha256_file(preview_file) != evidence.get("preview_sha256")
            or preview_file.stat().st_size != evidence.get("preview_size_bytes")
            or _sha256_file(report_file) != evidence.get("report_sha256")
        ):
            raise PreviewEvidenceError("全片审核预览证据在探测后被替换，请从全片预览阶段恢复")
        current_cache_fingerprint = _json_hash(_current_input_contract(project_dir, state, frozen_script))
        if current_cache_fingerprint != cache_input_fingerprint:
            raise InputDriftError("审核预览输入在完成提交前发生变化；请启动新的审核预览任务")
        phase = job.setdefault("phases", {}).setdefault("review_ready", {})
        phase.update(
            {
                "status": "completed",
                "attempts": max(1, int(phase.get("attempts") or 0)),
                "started_at": phase.get("started_at") or job.get("started_at"),
                "finished_at": _now(),
                "output": {
                    "preview_path": preview_path,
                    "report_path": report_path,
                    "preview_sha256": evidence.get("preview_sha256"),
                    "report_sha256": evidence.get("report_sha256"),
                },
                "error": None,
                "input_fingerprint": job.get("input_fingerprint"),
                "retryable": False,
                "safe_resume_point": None,
            }
        )
        job.update(
            {
                "status": "completed",
                "stage": "review_ready",
                "safe_resume_point": None,
                "current": {"kind": "result", "id": "preview_ready", "label": "审核预览已就绪，等待人工观看"},
                "gate": {"reason": "必须人工观看审核预览", "required_action": "人工审核；不会自动批准场景或生成正式成片"},
                "error": None,
                "result": {
                    "readiness": "preview_ready",
                    "preview_path": preview_path,
                    "report_path": report_path,
                    "script_hash": job.get("script_hash"),
                    "cache_input_fingerprint": cache_input_fingerprint,
                    **evidence,
                    "voice": deepcopy((job.get("frozen_input") or {}).get("voice") or {}),
                    "line_ledger_path": narration_lines.LEDGER_PATH.as_posix(),
                },
                "worker_token": None,
                "finished_at": _now(),
            }
        )
        wb._activity(state, "review_preview_pipeline_ready", f"一键审核预览任务 {job_id} 已到达待审终点；未批准场景，未生成正式成片", job_id=job_id, output_path=preview_path)
    return _mutate_job(project_dir, job_id, worker_token, mutate)


def run_review_preview_job(
    project_dir: Path,
    expected_job_id: str | None = None,
    *,
    dependencies: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run or resume the parent until completion, failure, or a human gate."""
    deps = _dependencies(dependencies)
    with _project_lock(project_dir):
        state = wb._load_for_write(project_dir)
        job = _pipeline(state)
        if expected_job_id and job.get("job_id") != expected_job_id:
            raise StaleReviewPreviewWorker("父任务编号已变化，旧 worker 不再执行")
        if job.get("status") in {"completed", "awaiting_human", "failed", "cancelled", "idle"}:
            return _job_response(job, launch_required=False)
        if job.get("worker_token"):
            return _job_response(job, launch_required=False)
        contract_error = _project_type_error(project_dir, job) or _frozen_version_error(job)
        if contract_error is not None:
            current_type = str(_raw_project(project_dir).get("pipeline_type") or "")
            _record_nonretryable_failure_locked(state, job, contract_error)
            wb._save(project_dir, state)
            if _is_avatar_project_type(current_type):
                return _job_response(_empty_job(), launch_required=False)
            return _job_response(job, launch_required=False)
        worker_token = uuid4().hex
        job["worker_token"] = worker_token
        job["status"] = "running"
        job["started_at"] = job.get("started_at") or _now()
        job["updated_at"] = _now()
        job_id = str(job.get("job_id") or "")
        wb._save(project_dir, state)

    try:
        while True:
            job = _assert_worker_execution_contract(project_dir, job_id, worker_token)
            _assert_script_unchanged(project_dir, job)
            _assert_audio_contract_unchanged(project_dir, job)
            _assert_static_inputs_unchanged(project_dir, job)
            stage = str(job.get("stage") or "preflight")
            frozen = job.get("frozen_input") or {}

            if stage == "preflight":
                _phase_begin(project_dir, job_id, worker_token, stage, "核对冻结输入")
                _phase_complete(project_dir, job_id, worker_token, stage, "scene_plan", {"script_hash": frozen.get("script_hash")})
                continue

            if stage == "scene_plan":
                _phase_begin(project_dir, job_id, worker_token, stage, "建立或复用分镜草案")
                state = wb.read_workbench(project_dir)
                if not state.get("scenes"):
                    _assert_worker_execution_contract(project_dir, job_id, worker_token)
                    deps["generate_scene_plan"](project_dir)
                if not _is_current_worker(project_dir, job_id, worker_token):
                    raise StaleReviewPreviewWorker("分镜完成后父任务已变化")
                state = wb.read_workbench(project_dir)
                contract = _scene_contract(project_dir, state, frozen.get("script") or {}, str(frozen.get("script_hash") or ""))
                if frozen.get("scene_plan_was_missing"):
                    refreshed_contract = _current_input_contract(project_dir, state, frozen.get("script") or {})
                    refreshed_visual_scope = _visual_generation_scope(state, frozen.get("script") or {})
                    frozen_authorizations = frozen.get("authorizations") if isinstance(frozen.get("authorizations"), dict) else {}
                    frozen_visual = frozen.get("visual") if isinstance(frozen.get("visual"), dict) else {}
                    if refreshed_visual_scope["required"] and not frozen_authorizations.get("pexels_network"):
                        raise InputDriftError(
                            "自动建立分镜后确认需要主体画面，但父任务没有冻结 Pexels 网络授权；"
                            "禁止静默升级授权，请重新预检启动新任务"
                        )
                    if (
                        refreshed_visual_scope["required"]
                        and frozen_visual.get("planning_mode") == "ai_director"
                        and not frozen_authorizations.get("text_ai")
                    ):
                        raise InputDriftError(
                            "自动建立分镜后确认需要 AI 画面规划，但父任务没有冻结文本模型授权；"
                            "禁止静默升级授权，请重新预检启动新任务"
                        )

                    def refreeze_planned_scene_contract(_state: dict[str, Any], active: dict[str, Any]) -> None:
                        frozen_active = active.get("frozen_input") if isinstance(active.get("frozen_input"), dict) else {}
                        active_visual = (
                            frozen_active.get("visual")
                            if isinstance(frozen_active.get("visual"), dict)
                            else {}
                        )
                        scoped_visual = _visual_strategy_with_scope(
                            active_visual,
                            refreshed_visual_scope,
                            text_ai_model=active_visual.get("text_ai_model"),
                        )
                        refreshed_required = bool(refreshed_visual_scope["required"])
                        refreshed_authorizations = {
                            "pexels_network": refreshed_required,
                            "text_ai": (
                                refreshed_required
                                and active_visual.get("planning_mode") == "ai_director"
                            ),
                            "openai_image": False,
                            "avatar": False,
                        }
                        frozen_active.update(
                            {
                                "scene_visual_signature": refreshed_contract["scene_visual_signature"],
                                "subtitle_signature": refreshed_contract["subtitle_signature"],
                                "render_profile_signature": refreshed_contract["render_profile_signature"],
                                "visual": scoped_visual,
                                "authorizations": refreshed_authorizations,
                                "visual_generation_required": refreshed_required,
                                "scene_plan_was_missing": False,
                            }
                        )
                        active["frozen_input"] = frozen_active
                        active["input_fingerprint"] = _json_hash(frozen_active)
                        active["request_fingerprint"] = _request_fingerprint(frozen_active)
                        preflight = active.get("preflight") if isinstance(active.get("preflight"), dict) else {}
                        preflight.update(
                            {
                                "visual_generation_required": bool(refreshed_visual_scope["required"]),
                                "visual_target_scene_ids": list(refreshed_visual_scope["scene_ids"]),
                                "visual_target_scene_count": int(refreshed_visual_scope["scene_count"]),
                                "visual_scope_pending_scene_plan": False,
                                "visual_strategy": deepcopy(scoped_visual),
                            }
                        )
                        active["preflight"] = preflight

                    _mutate_job(project_dir, job_id, worker_token, refreeze_planned_scene_contract)
                _phase_complete(project_dir, job_id, worker_token, stage, "line_plan", {"scene_count": len(contract["mapping"]), "contract": contract})
                continue

            if stage == "line_plan":
                _phase_begin(project_dir, job_id, worker_token, stage, "确定性拆句并建立逐句计划")
                sections = (frozen.get("script") or {}).get("sections") or []
                plan = narration_lines.build_line_plan(sections, frozen.get("voice") or {})
                if not plan.get("line_count"):
                    raise ReviewPreviewError("冻结脚本没有可生成的逐句旁白")
                _phase_complete(
                    project_dir,
                    job_id,
                    worker_token,
                    stage,
                    "narration",
                    {"plan_fingerprint": plan["plan_fingerprint"], "line_count": plan["line_count"]},
                )
                continue

            if stage == "narration":
                _phase_begin(project_dir, job_id, worker_token, stage, "逐句生成并校验本地配音")
                sections = (frozen.get("script") or {}).get("sections") or []
                plan = narration_lines.build_line_plan(sections, frozen.get("voice") or {})

                def progress(record: dict[str, Any]) -> None:
                    def mutate(_state: dict[str, Any], active: dict[str, Any]) -> None:
                        ledger = narration_lines.load_ledger(project_dir)
                        active["counts"] = {
                            "total": len(ledger.get("lines") or []),
                            "completed": sum(1 for item in ledger.get("lines") or [] if item.get("status") == "completed"),
                            "failed": sum(1 for item in ledger.get("lines") or [] if item.get("status") == "failed"),
                        }
                        active["current"] = {
                            "kind": "line",
                            "id": record.get("line_id"),
                            "label": f"逐句配音 {record.get('project_ordinal')}/{len(plan.get('lines') or [])}",
                            "status": record.get("status"),
                        }
                    _mutate_job(project_dir, job_id, worker_token, mutate)

                _assert_worker_execution_contract(project_dir, job_id, worker_token)
                ledger = narration_lines.materialize_line_audio(
                    project_dir,
                    plan,
                    deps["synthesize_line"],
                    tts_client=None if deps.get("synthesize_line") is not None else deps.get("tts_client"),
                    is_current=lambda: _is_current_worker(project_dir, job_id, worker_token),
                    commit=lambda action: _commit_line_ledger(project_dir, job_id, worker_token, action),
                    parent_job_id=job_id,
                    worker_token=worker_token,
                    allow_terminal_retry=bool(job.get("tts_terminal_retry_authorized")),
                    on_progress=progress,
                )
                _phase_complete(
                    project_dir,
                    job_id,
                    worker_token,
                    stage,
                    "audio_timeline",
                    {"ledger_path": narration_lines.LEDGER_PATH.as_posix(), "completed": ledger.get("completed_count")},
                )
                continue

            if stage == "audio_timeline":
                _phase_begin(project_dir, job_id, worker_token, stage, "按逐句实测时长建立场景与项目主时间线")
                _assert_worker_execution_contract(project_dir, job_id, worker_token)
                ledger = narration_lines.load_ledger(project_dir)
                _validate_line_ledger_contract(project_dir, job, worker_token, ledger)
                output = _aggregate_line_audio(project_dir, job_id, worker_token, job, ledger, deps)
                _phase_complete(project_dir, job_id, worker_token, stage, "subtitles", output)
                continue

            if stage == "subtitles":
                _phase_begin(project_dir, job_id, worker_token, stage, "从逐句时长直接生成字幕边界")
                _assert_worker_execution_contract(project_dir, job_id, worker_token)
                output = _write_sentence_subtitles(project_dir, job_id, worker_token)
                _phase_complete(project_dir, job_id, worker_token, stage, "visual_plan", output)
                continue

            if stage == "visual_plan":
                _phase_begin(project_dir, job_id, worker_token, stage, "建立不含数字人和 OpenAI 生图的画面计划")
                state = wb.read_workbench(project_dir)
                if frozen.get("visual_generation_required") is False:
                    visual_error = _frozen_visual_reuse_error(project_dir, job, state=state)
                    if visual_error is not None:
                        raise visual_error
                    visual_signature = _current_input_contract(
                        project_dir,
                        state,
                        frozen.get("script") or {},
                    )["scene_visual_signature"]
                    _phase_complete(
                        project_dir,
                        job_id,
                        worker_token,
                        stage,
                        "audio_sample",
                        {
                            "reused_existing_visuals": True,
                            "visual_generation_required": False,
                            "visual_signature": visual_signature,
                        },
                    )
                    continue
                if not _needs_visual_generation(state):
                    visual_signature = _current_input_contract(project_dir, state, frozen.get("script") or {})["scene_visual_signature"]
                    _phase_complete(
                        project_dir,
                        job_id,
                        worker_token,
                        stage,
                        "audio_sample",
                        {
                            "reused_existing_visuals": True,
                            "visual_generation_required": True,
                            "visual_signature": visual_signature,
                        },
                    )
                    continue
                visual_payload = deepcopy(frozen.get("visual") or DEFAULT_VISUAL_INPUT)
                visual_payload.update(
                    {
                        "confirmed": True,
                        "ai_planning_confirmed": bool((frozen.get("authorizations") or {}).get("text_ai")),
                        "ai_generation_confirmed": False,
                        "_review_preview_job_id": job_id,
                        "_review_preview_worker_token": worker_token,
                        "_review_preview_internal_capability": wb._REVIEW_PREVIEW_INTERNAL_CAPABILITY,
                        "_review_preview_request_fingerprint": job.get("request_fingerprint"),
                    }
                )
                batch = wb._automation(state).get("visual_batch") or {}
                if batch.get("status") in {"queued", "generating", "completed", "completed_with_warnings"}:
                    if (
                        batch.get("parent_job_id") != job_id
                        or batch.get("request_fingerprint") != job.get("request_fingerprint")
                    ):
                        raise ReviewPreviewConflict("已有画面子任务不属于当前父任务或冻结计划，禁止接管")
                    _phase_complete(
                        project_dir,
                        job_id,
                        worker_token,
                        stage,
                        "visual_generation",
                        {
                            "plan_id": batch.get("preview_plan_id"),
                            "visual_job_id": batch.get("job_id"),
                            "recovered_child": True,
                        },
                    )
                    continue
                current_visual_scope = _visual_generation_scope(state, frozen.get("script") or {})
                frozen_scene_ids = [str(value) for value in visual_payload.get("scene_ids") or [] if str(value)]
                if visual_payload.get("selection_mode") != "custom" or not frozen_scene_ids:
                    raise InputDriftError(
                        "冻结画面合同缺少精确的 custom scene_ids；禁止动态扩大选择范围，请重新预检启动新任务"
                    )
                if frozen_scene_ids != list(current_visual_scope["scene_ids"]):
                    raise InputDriftError(
                        "待补画面场景范围在任务开始后发生变化；已保留现有成果，请重新预检启动新任务"
                    )
                authorizations = frozen.get("authorizations") if isinstance(frozen.get("authorizations"), dict) else {}
                if not authorizations.get("pexels_network"):
                    raise InputDriftError("冻结任务缺少 Pexels 网络授权；禁止在运行中静默升级，请重新预检启动新任务")
                if visual_payload.get("planning_mode") == "ai_director" and not authorizations.get("text_ai"):
                    raise InputDriftError("冻结任务缺少文本模型授权；禁止在运行中静默升级，请重新预检启动新任务")
                reviewed: dict[str, Any] | None = None
                if visual_payload.get("planning_mode") == "ai_director":
                    current_job = _assert_worker_execution_contract(project_dir, job_id, worker_token)
                    visual_phase = (current_job.get("phases") or {}).get("visual_plan") or {}
                    stored_reviewed = visual_phase.get("reviewed_plan")
                    stored_hash = str(visual_phase.get("reviewed_plan_hash") or "")
                    if isinstance(stored_reviewed, dict) and stored_hash == _json_hash(stored_reviewed):
                        reviewed = deepcopy(stored_reviewed)
                    elif visual_phase.get("planner_status") == "dispatched_ambiguous":
                        raise AmbiguousExternalOperation(
                            "AI 视觉导演请求已发出但没有可验证结果；请人工核对文本模型侧状态后再决定是否重试"
                        )
                    else:
                        planner_attempt = int(visual_phase.get("planner_attempt") or 0) + 1
                        planner_request_id = "RVP-" + _json_hash(
                            {
                                "job_id": job_id,
                                "request_fingerprint": current_job.get("request_fingerprint"),
                                "attempt": planner_attempt,
                            }
                        )[:20]

                        def mark_planner_dispatched(_state: dict[str, Any], active: dict[str, Any]) -> None:
                            phase = active.setdefault("phases", {}).setdefault("visual_plan", {})
                            phase.update(
                                {
                                    "planner_attempt": planner_attempt,
                                    "planner_request_id": planner_request_id,
                                    "planner_status": "dispatched_ambiguous",
                                    "reviewed_plan": None,
                                    "reviewed_plan_hash": None,
                                }
                            )

                        _mutate_job(project_dir, job_id, worker_token, mark_planner_dispatched)
                        visual_payload["_review_preview_planner_request_id"] = planner_request_id
                        _assert_worker_execution_contract(project_dir, job_id, worker_token)
                        reviewed = deps["preview_visual_plan"](project_dir, visual_payload)
                        if not isinstance(reviewed, dict):
                            raise ReviewPreviewError("AI 视觉导演未返回可持久化的审核计划")

                        def checkpoint_reviewed_plan(_state: dict[str, Any], active: dict[str, Any]) -> None:
                            phase = active.setdefault("phases", {}).setdefault("visual_plan", {})
                            if phase.get("planner_request_id") != planner_request_id:
                                raise StaleReviewPreviewWorker("AI 视觉计划请求身份已变化，拒绝旧结果落账")
                            phase.update(
                                {
                                    "planner_status": "completed",
                                    "reviewed_plan": deepcopy(reviewed),
                                    "reviewed_plan_hash": _json_hash(reviewed),
                                }
                            )

                        _mutate_job(project_dir, job_id, worker_token, checkpoint_reviewed_plan)
                else:
                    _assert_worker_execution_contract(project_dir, job_id, worker_token)
                    reviewed = deps["preview_visual_plan"](project_dir, visual_payload)
                if not isinstance(reviewed, dict):
                    raise ReviewPreviewError("画面计划未返回可审核结果")
                visual_payload["reviewed_plan"] = reviewed
                _assert_worker_execution_contract(project_dir, job_id, worker_token)
                deps["start_visual_generation"](project_dir, visual_payload)
                state = wb.read_workbench(project_dir)
                batch = wb._automation(state).get("visual_batch") or {}
                _phase_complete(
                    project_dir,
                    job_id,
                    worker_token,
                    stage,
                    "visual_generation",
                    {"plan_id": reviewed.get("plan_id"), "visual_job_id": batch.get("job_id")},
                )
                continue

            if stage == "visual_generation":
                _phase_begin(project_dir, job_id, worker_token, stage, "串行生成并验证主体画面")
                state = wb.read_workbench(project_dir)
                batch = wb._automation(state).get("visual_batch") or {}
                if batch.get("status") in {"queued", "generating"}:
                    if (
                        batch.get("parent_job_id") != job_id
                        or batch.get("request_fingerprint") != job.get("request_fingerprint")
                    ):
                        raise ReviewPreviewConflict("画面子任务不属于当前父任务或冻结计划，禁止当前任务接管")
                    _assert_worker_execution_contract(project_dir, job_id, worker_token)
                    try:
                        if deps["generate_visuals"] is wb.generate_visual_batch:
                            deps["generate_visuals"](
                                project_dir,
                                expected_job_id=batch.get("job_id"),
                                expected_parent_job_id=job_id,
                                expected_worker_token=worker_token,
                                expected_request_fingerprint=job.get("request_fingerprint"),
                                expected_contract_versions=_current_contract_versions(),
                            )
                        else:
                            deps["generate_visuals"](project_dir, expected_job_id=batch.get("job_id"))
                    except wb.WorkbenchError:
                        # Convert project-type/version drift discovered inside
                        # a long child loop into the parent's non-retryable
                        # contract error instead of a generic media failure.
                        _assert_worker_execution_contract(project_dir, job_id, worker_token)
                        raise
                if not _is_current_worker(project_dir, job_id, worker_token):
                    raise StaleReviewPreviewWorker("画面生成完成后父任务已变化")
                state = wb.read_workbench(project_dir)
                batch = wb._automation(state).get("visual_batch") or {}
                if int(batch.get("failed_slots") or 0) or batch.get("status") not in {"completed", "completed_with_warnings"}:
                    completed_slots = int(batch.get("completed_slots") or 0)
                    failed_slots = int(batch.get("failed_slots") or 0)
                    failed_reasons = list(
                        dict.fromkeys(
                            str(item.get("error") or "").strip()
                            for item in batch.get("items") or []
                            if isinstance(item, dict)
                            and item.get("status") == "failed"
                            and str(item.get("error") or "").strip()
                        )
                    )
                    reason = failed_reasons[0][:360] if failed_reasons else str(batch.get("error") or "画面服务未返回可用素材")[:360]
                    raise VisualGenerationIncomplete(
                        f"仍有 {failed_slots or '部分'} 个主体画面未成功生成；"
                        f"已保留 {completed_slots} 个成功画面。修复后从安全点继续时只重试失败槽。"
                        f"首要原因：{reason}",
                        completed_slots=completed_slots,
                        failed_slots=failed_slots,
                    )
                if _needs_visual_generation(state):
                    raise ReviewPreviewError("画面任务结束后仍有不可合成场景，禁止用错误素材填空")
                _phase_complete(
                    project_dir,
                    job_id,
                    worker_token,
                    stage,
                    "audio_sample",
                    {
                        "completed_slots": batch.get("completed_slots"),
                        "failed_slots": batch.get("failed_slots"),
                        "visual_signature": _current_input_contract(project_dir, state, frozen.get("script") or {})["scene_visual_signature"],
                    },
                )
                continue

            if stage == "audio_sample":
                _phase_begin(project_dir, job_id, worker_token, stage, "检查声音样板人工门")
                state = wb.read_workbench(project_dir)
                if not _audio_gate_required(state, frozen.get("voice") or {}):
                    _phase_complete(project_dir, job_id, worker_token, stage, "full_preview", {"required": False})
                    continue
                sample = wb._ensure_music_policy(state).get("sample") or {}
                policy_signature = wb._audio_mix_signature(state)
                if sample.get("status") != "approved" or sample.get("policy_signature") != policy_signature:
                    if sample.get("status") == "generating":
                        if (
                            sample.get("parent_job_id") != job_id
                            or sample.get("policy_signature") != policy_signature
                        ):
                            raise ReviewPreviewConflict("正在生成的声音样板不属于当前父任务或冻结声音设置，禁止接管")
                        _assert_worker_execution_contract(project_dir, job_id, worker_token)
                        deps["generate_audio_sample"](project_dir)
                    elif sample.get("status") != "ready" or sample.get("policy_signature") != policy_signature:
                        _assert_worker_execution_contract(project_dir, job_id, worker_token)
                        deps["start_audio_sample"](
                            project_dir,
                            {
                                "_review_preview_job_id": job_id,
                                "_review_preview_worker_token": worker_token,
                                "_review_preview_internal_capability": wb._REVIEW_PREVIEW_INTERNAL_CAPABILITY,
                                "_review_preview_request_fingerprint": job.get("request_fingerprint"),
                            },
                        )
                        _assert_worker_execution_contract(project_dir, job_id, worker_token)
                        deps["generate_audio_sample"](project_dir)
                    state = wb.read_workbench(project_dir)
                    sample = wb._ensure_music_policy(state).get("sample") or {}
                    if sample.get("status") not in {"ready", "approved"} or sample.get("policy_signature") != policy_signature:
                        raise ReviewPreviewError("声音样板子任务未生成可试听结果，请从声音样板阶段安全恢复")

                    def await_human(_state: dict[str, Any], active: dict[str, Any]) -> None:
                        latest_sample = wb._ensure_music_policy(_state).get("sample") or {}
                        active["status"] = "awaiting_human"
                        active["worker_token"] = None
                        active["gate"] = {
                            "reason": "声音设置需要人工试听确认",
                            "required_action": "试听第一段声音样板并确认后继续",
                            "stage": "audio_sample",
                            "sample_path": latest_sample.get("output_path"),
                        }
                        active["safe_resume_point"] = "audio_sample"
                        active["current"] = {"kind": "gate", "id": "audio_sample", "label": "等待人工试听声音样板"}
                        active.setdefault("phases", {}).setdefault("audio_sample", {}).update(
                            {
                                "status": "awaiting_human",
                                "input_fingerprint": active.get("input_fingerprint"),
                                "retryable": True,
                                "safe_resume_point": "audio_sample",
                            }
                        )
                    return _mutate_job(project_dir, job_id, worker_token, await_human)
                _phase_complete(project_dir, job_id, worker_token, stage, "full_preview", {"required": True, "approved": True})
                continue

            if stage == "full_preview":
                _phase_begin(project_dir, job_id, worker_token, stage, "生成全片审核预览，不改变场景审核状态")
                state = wb.read_workbench(project_dir)
                phases = job.get("phases") if isinstance(job.get("phases"), dict) else {}
                visual_output = ((phases.get("visual_generation") or {}).get("output") or {})
                if not visual_output.get("visual_signature"):
                    visual_output = ((phases.get("visual_plan") or {}).get("output") or {})
                expected_visual = str(visual_output.get("visual_signature") or "")
                current_visual = _current_input_contract(project_dir, state, frozen.get("script") or {})["scene_visual_signature"]
                if not expected_visual or current_visual != expected_visual:
                    raise InputDriftError("场景或视觉素材在预览合成前发生变化；请重新启动新的审核预览任务")
                preview = wb._automation(state).get("preview_render") or {}
                if preview.get("status") != "completed" or preview.get("needs_refresh") or not (project_dir / str(preview.get("output_path") or "")).is_file():
                    if preview.get("status") == "generating":
                        if (
                            preview.get("parent_job_id") != job_id
                            or preview.get("input_fingerprint") != job.get("input_fingerprint")
                        ):
                            raise ReviewPreviewConflict("正在生成的全片预览不属于当前父任务或冻结输入，禁止接管")
                    else:
                        _assert_worker_execution_contract(project_dir, job_id, worker_token)
                        deps["start_full_preview"](
                            project_dir,
                            {
                                "confirmed": True,
                                "_review_preview_job_id": job_id,
                                "_review_preview_worker_token": worker_token,
                                "_review_preview_internal_capability": wb._REVIEW_PREVIEW_INTERNAL_CAPABILITY,
                                "_review_preview_input_fingerprint": job.get("input_fingerprint"),
                                "_review_preview_trusted_default_audio": bool(
                                    ((frozen.get("music_gate") or {}).get("trusted_default"))
                                ),
                            },
                        )
                    _assert_worker_execution_contract(project_dir, job_id, worker_token)
                    deps["generate_full_preview"](project_dir)
                if not _is_current_worker(project_dir, job_id, worker_token):
                    raise StaleReviewPreviewWorker("预览生成完成后父任务已变化")
                state = wb.read_workbench(project_dir)
                preview = wb._automation(state).get("preview_render") or {}
                preview_path = str(preview.get("output_path") or "")
                report_path = str(preview.get("report_path") or wb.AUTOMATION_PREVIEW_RENDER_REPORT)
                _assert_worker_execution_contract(project_dir, job_id, worker_token)
                evidence = deps["probe_preview"](project_dir, preview_path, report_path)
                _phase_complete(
                    project_dir,
                    job_id,
                    worker_token,
                    stage,
                    "review_ready",
                    {"preview_path": preview_path, "report_path": report_path, **evidence},
                )
                continue

            if stage == "review_ready":
                _assert_worker_execution_contract(project_dir, job_id, worker_token)
                return _complete_parent(project_dir, job_id, worker_token, deps)

            raise ReviewPreviewError(f"未知的一键审核预览阶段：{stage}")
    except StaleReviewPreviewWorker:
        raise
    except AmbiguousExternalOperation as exc:
        return _fail_job(project_dir, job_id, worker_token, exc, ambiguous=True)
    except Exception as exc:
        # Existing workbench starters persist their own in-flight markers.
        # Convert those markers to a retryable terminal state before recording
        # the parent failure, otherwise a safe resume would be rejected as a
        # duplicate manual job.
        try:
            active_stage = str(_read_job_internal(project_dir).get("stage") or "")
            if active_stage == "full_preview" or (
                active_stage == "review_ready" and isinstance(exc, PreviewEvidenceError)
            ):
                preview = wb._automation(wb.read_workbench(project_dir)).get("preview_render") or {}
                preview_owned = (
                    preview.get("parent_job_id") == job_id
                    and preview.get("input_fingerprint") == job.get("input_fingerprint")
                )
                if preview_owned or (
                    not isinstance(exc, NonRetryableReviewPreviewError)
                    and preview.get("parent_job_id") is None
                ):
                    wb.mark_full_preview_render_failed(project_dir, exc)
            elif active_stage == "audio_sample":
                sample = wb._ensure_music_policy(wb.read_workbench(project_dir)).get("sample") or {}
                sample_owned = (
                    sample.get("parent_job_id") == job_id
                    and sample.get("request_fingerprint") == job.get("request_fingerprint")
                    and sample.get("policy_signature")
                    == str((job.get("frozen_input") or {}).get("audio_mix_signature") or "")
                )
                if sample_owned or (
                    not isinstance(exc, NonRetryableReviewPreviewError)
                    and sample.get("parent_job_id") is None
                ):
                    wb.mark_music_sample_failed(project_dir, exc)
        except Exception:
            pass
        return _fail_job(project_dir, job_id, worker_token, exc)


def resume_review_preview_job(
    project_dir: Path,
    job_id: str,
    payload: dict[str, Any] | None = None,
    *,
    dependencies: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Requeue a failed/human-gated job at its recorded safe point."""
    payload = payload or {}
    deps = _dependencies(dependencies)
    job = _read_job_internal(project_dir)
    if job.get("job_id") != job_id:
        raise ReviewPreviewConflict("未找到指定的一键审核预览任务")
    if job.get("status") == "completed":
        return _job_response(job, launch_required=False)
    if job.get("status") not in {"failed", "awaiting_human"}:
        raise ReviewPreviewConflict("当前任务不处于可恢复状态")
    if job.get("status") == "failed" and (job.get("error") or {}).get("retryable") is False:
        raise ReviewPreviewConflict("该失败由冻结输入漂移或不可修复格式/安全错误导致，禁止续跑；请启动新任务")
    gate = job.get("gate") if isinstance(job.get("gate"), dict) else {}
    ambiguous_retry_authorized = False
    if job.get("status") == "awaiting_human" and gate.get("stage") == "audio_sample":
        if payload.get("confirmed") is not True:
            raise ReviewPreviewError("请先试听声音样板并确认后再继续")
        with _project_lock(project_dir):
            locked_state = wb._load_for_write(project_dir)
            locked_job = _pipeline(locked_state)
            locked_gate = locked_job.get("gate") if isinstance(locked_job.get("gate"), dict) else {}
            if locked_job.get("job_id") != job_id or locked_job.get("status") != "awaiting_human" or locked_gate.get("stage") != "audio_sample":
                raise ReviewPreviewConflict("声音样板门已变化，拒绝批准旧任务")
            frozen_signature = str((locked_job.get("frozen_input") or {}).get("audio_mix_signature") or "")
            current_signature = wb._audio_mix_signature(locked_state)
            if not frozen_signature or current_signature != frozen_signature:
                error = InputDriftError("声音设置已变化，请重新预检启动新任务")
                _record_nonretryable_failure_locked(locked_state, locked_job, error)
                locked_job["gate"] = None
                wb._save(project_dir, locked_state)
                raise ReviewPreviewConflict(str(error))
            sample = wb._ensure_music_policy(locked_state).get("sample") or {}
            if sample.get("policy_signature") != frozen_signature:
                raise ReviewPreviewConflict("声音样板与冻结声音设置不一致，请重新生成新任务")
            if sample.get("status") == "ready":
                deps["approve_audio_sample"](project_dir, {"confirmed": True})
                locked_state = wb._load_for_write(project_dir)
                locked_job = _pipeline(locked_state)
                locked_gate = locked_job.get("gate") if isinstance(locked_job.get("gate"), dict) else {}
                if locked_job.get("job_id") != job_id or locked_job.get("status") != "awaiting_human" or locked_gate.get("stage") != "audio_sample":
                    raise ReviewPreviewConflict("声音样板批准期间父任务门已变化")
                sample = wb._ensure_music_policy(locked_state).get("sample") or {}
            elif sample.get("status") != "approved":
                raise ReviewPreviewConflict("声音样板状态已变化，无法批准旧人工门")
            if sample.get("status") != "approved" or sample.get("policy_signature") != frozen_signature:
                raise ReviewPreviewConflict("声音样板批准结果与冻结声音设置不一致")
            locked_job.update(
                {
                    "status": "queued",
                    "stage": "full_preview",
                    "safe_resume_point": "full_preview",
                    "current": {"kind": "stage", "id": "full_preview", "label": "已从声音样板门排队恢复"},
                    "gate": None,
                    "error": None,
                    "worker_token": None,
                    "resumed_at": _now(),
                    "updated_at": _now(),
                }
            )
            locked_job.pop("tts_terminal_retry_authorized", None)
            wb._save(project_dir, locked_state)
            return _job_response(locked_job, launch_required=True)
    elif job.get("status") == "awaiting_human" and (job.get("error") or {}).get("ambiguous_external_operation"):
        if payload.get("external_state_confirmed") is not True or payload.get("safe_to_retry") is not True:
            raise ReviewPreviewError("外部提交结果不明；必须先确认未创建任务，才能从安全点重试")
        resume_stage = str(job.get("safe_resume_point") or job.get("stage") or "preflight")
        ambiguous_retry_authorized = True
    else:
        resume_stage = str(job.get("safe_resume_point") or job.get("stage") or "preflight")
    expected_status = str(job.get("status") or "")
    expected_updated_at = job.get("updated_at")
    visual_resume_summary: dict[str, int] | None = None
    if resume_stage == "visual_generation":
        state = wb.read_workbench(project_dir)
        batch = wb._automation(state).get("visual_batch") or {}
        if (
            str(batch.get("parent_job_id") or "") != job_id
            or str(batch.get("request_fingerprint") or "")
            != str(job.get("request_fingerprint") or "")
        ):
            raise ReviewPreviewConflict("画面子任务不属于当前父任务或冻结请求，拒绝恢复")
        failed_items = [
            item for item in batch.get("items") or []
            if isinstance(item, dict) and item.get("status") == "failed"
        ]
        if failed_items:
            capabilities = deps["collect_capabilities"](include_visual_runtime=True)
            blockers: list[str] = []
            if not (capabilities.get("ffmpeg") or {}).get("available"):
                blockers.append("本机未发现 FFmpeg，无法恢复画面或生成最终预览")
            if not (capabilities.get("ffprobe") or {}).get("available"):
                blockers.append("本机未发现 ffprobe，无法验证恢复后的媒体")
            uses_pexels = any(
                str(item.get("route") or "") in {"stock_video", "stock_image"}
                or str(item.get("fallback_route") or "") in {"stock_video", "stock_image"}
                for item in failed_items
            )
            uses_hyperframes = any(
                str(item.get("route") or "") == "hyperframes"
                or str(item.get("fallback_route") or "") == "hyperframes"
                for item in failed_items
            )
            if uses_pexels and not (capabilities.get("pexels") or {}).get("available"):
                blockers.append("Pexels 当前不可用，失败画面中的网络素材路线无法继续")
            if uses_hyperframes and not (capabilities.get("hyperframes") or {}).get("available"):
                hyperframes = capabilities.get("hyperframes") or {}
                blockers.append(
                    str(hyperframes.get("user_message") or "HyperFrames 当前不可用，失败画面的本地信息图路线无法继续")
                )
            if blockers:
                completed_slots = int(batch.get("completed_slots") or 0)
                raise ReviewPreviewError(
                    f"暂不能从画面阶段恢复；已保留 {completed_slots} 个成功画面，未启动新 worker。"
                    + "；".join(blockers)
                )
            deps["requeue_failed_visuals"](
                project_dir,
                expected_job_id=str(batch.get("job_id") or ""),
                expected_parent_job_id=job_id,
                expected_request_fingerprint=str(job.get("request_fingerprint") or ""),
            )
            state = wb.read_workbench(project_dir)
            batch = wb._automation(state).get("visual_batch") or {}
        if batch.get("status") not in {"queued", "completed", "completed_with_warnings"}:
            raise ReviewPreviewConflict("画面子任务未进入可继续状态，请先检查失败槽记录")
        visual_resume_summary = {
            "preserved_completed_slots": int(batch.get("completed_slots") or 0),
            "retry_failed_slots": int(batch.get("retry_slot_count") or len(failed_items)),
        }
    terminal_tts_retry = False
    if resume_stage == "narration":
        ledger = narration_lines.load_ledger(project_dir)
        terminal_tts_retry = any(
            str(item.get("tts_status") or "").lower() in {"failed", "cancelled", "canceled", "error"}
            for item in ledger.get("lines") or []
            if isinstance(item, dict)
        )

    def mutate(_state: dict[str, Any], active: dict[str, Any]) -> None:
        if active.get("status") != expected_status or active.get("updated_at") != expected_updated_at:
            raise ReviewPreviewConflict("任务状态已被另一恢复请求更新，拒绝重复恢复")
        resume_label = "已从安全点排队恢复"
        if visual_resume_summary is not None:
            resume_label = (
                f"已保留 {visual_resume_summary['preserved_completed_slots']} 个成功画面，"
                f"仅重试 {visual_resume_summary['retry_failed_slots']} 个失败槽"
            )
        active.update(
            {
                "status": "queued",
                "stage": resume_stage,
                "safe_resume_point": resume_stage,
                "current": {"kind": "stage", "id": resume_stage, "label": resume_label},
                "gate": None,
                "error": None,
                "worker_token": None,
                "resumed_at": _now(),
            }
        )
        if visual_resume_summary is not None:
            active["resume_scope"] = deepcopy(visual_resume_summary)
        if terminal_tts_retry:
            active["tts_terminal_retry_authorized"] = True
        else:
            active.pop("tts_terminal_retry_authorized", None)
        if ambiguous_retry_authorized and resume_stage == "visual_plan":
            phase = active.setdefault("phases", {}).setdefault("visual_plan", {})
            phase["planner_status"] = "retry_authorized"
    return _job_response(_mutate_job(project_dir, job_id, None, mutate), launch_required=True)


def recover_review_preview_job(project_dir: Path) -> dict[str, Any]:
    """Convert an interrupted in-memory worker into a durable queued resume."""
    raw_project = _raw_project(project_dir)
    project_type = raw_project.get("pipeline_type")
    if _is_avatar_project_type(project_type):
        return _job_response(_empty_job(), launch_required=False)
    job = _read_job_internal(project_dir)
    if job.get("status") not in {"queued", "running"}:
        return _job_response(job, launch_required=False)
    contract_error = (
        _project_type_error(project_dir, job)
        or _frozen_version_error(job)
        or _frozen_visual_reuse_error(project_dir, job)
    )
    if contract_error is not None:
        expected_job_id = job.get("job_id")
        expected_status = job.get("status")
        expected_worker_token = job.get("worker_token")
        expected_updated_at = job.get("updated_at")
        with _project_lock(project_dir):
            state = wb._load_for_write(project_dir)
            active = _pipeline(state)
            if (
                active.get("job_id") != expected_job_id
                or active.get("status") != expected_status
                or active.get("worker_token") != expected_worker_token
                or active.get("updated_at") != expected_updated_at
            ):
                raise ReviewPreviewConflict("恢复检查期间父任务状态已变化，拒绝覆盖最新状态")
            locked_error = (
                _project_type_error(project_dir, active)
                or _frozen_version_error(active)
                or _frozen_visual_reuse_error(project_dir, active, state=state)
            )
            if locked_error is None:
                raise ReviewPreviewConflict("恢复检查期间冻结合同已变化，请基于最新状态重试")
            _record_nonretryable_failure_locked(state, active, locked_error)
            active["gate"] = None
            wb._save(project_dir, state)
            return _job_response(active, launch_required=False)
    was_running = job.get("status") == "running"
    if not was_running:
        # A queued job is durable before dispatch.  After a process crash no
        # in-memory launcher exists, so the caller must dispatch it again;
        # run_review_preview_job's worker-token CAS prevents double workers.
        return _job_response(job, launch_required=not bool(job.get("worker_token")))
    job_id = str(job.get("job_id") or "")
    expected_worker_token = job.get("worker_token")
    expected_updated_at = job.get("updated_at")

    def mutate(_state: dict[str, Any], active: dict[str, Any]) -> None:
        if (
            active.get("status") != "running"
            or active.get("worker_token") != expected_worker_token
            or active.get("updated_at") != expected_updated_at
        ):
            raise ReviewPreviewConflict("运行任务租约已变化，拒绝用旧恢复请求覆盖")
        active["status"] = "queued"
        active["worker_token"] = None
        active["safe_resume_point"] = str(active.get("safe_resume_point") or active.get("stage") or "preflight")
        active["current"] = {
            "kind": "recovery",
            "id": active["safe_resume_point"],
            "label": "服务重启后已从最近安全点排队恢复",
        }
        active["recovered_at"] = _now()
    return _job_response(_mutate_job(project_dir, job_id, None, mutate), launch_required=True)
