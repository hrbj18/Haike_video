"""Paid and local-media stages for the unattended daily tech brief.

The free research/script/project stages live in :mod:`daily_automation` so
they can be tested without providers.  This module resumes that durable run,
generates two long Voicebox tracks, creates two long RunningHub avatars on
the frozen Standard 24GB workflow, aligns/cuts them, fills supporting visuals and renders a
review candidate.  Every externally billed task is persisted before polling.
"""

from __future__ import annotations

import json
import hashlib
import os
import re
import shutil
import subprocess
import time
import wave
from datetime import date, datetime
from pathlib import Path
from typing import Any

from backlot.ai_text import read_text_ai_config
from backlot.avatar_audio_clock import (
    AVATAR_VIDEO_FPS,
    AvatarAudioClockError,
    align_pcm_wav_to_frame_clock,
    inspect_frame_clock_wav,
)
from backlot.avatar_import import (
    approve_high_confidence_longform_cuts,
    approve_longform_cut,
    assemble_avatar_package,
    finalize_upload,
    initialize_avatar_package,
    list_local_whisper_models,
    prepare_upload,
    read_avatar_package,
    run_avatar_asr,
    start_avatar_assembly,
    start_avatar_asr,
)
from backlot.daily_automation import (
    BudgetLedger,
    DailyAutomationError,
    RUNS_ROOT,
    STAGE_ORDER,
    _atomic_json,
    _read_json,
    _save_run,
    classify_runninghub_failure,
    daily_billing_safety,
    ensure_paid_operation,
    evaluate_media_release,
    fallback_approval_matches,
    heartbeat_stage,
    provider_media_eligibility,
    read_config,
    read_run,
    run_research_and_script,
    transition_paid_operation,
    speaker_pure_text,
    update_stage,
)
from backlot.music_library import list_music_tracks
from backlot.music_preferences import read_music_preferences
from backlot.runninghub_config import read_runninghub_config
from backlot.state import PROJECTS_DIR
from backlot.workbench import (
    apply_avatar_package_to_timeline,
    approve_music_sample,
    generate_full_preview_render,
    generate_music_sample,
    generate_visual_batch,
    preview_visual_batch_plan,
    read_workbench,
    start_full_preview_render,
    start_music_sample,
    start_visual_block_refresh,
    start_visual_batch_generation,
    update_music_policy,
    update_presenter_layout_template,
)
from tools.audio.voicebox_tts import VoiceboxTTS
from tools.avatar.runninghub_avatar import (
    INFINITETALK_448X560_EXACT_CLOCK_PROFILE,
    INFINITETALK_448X560_EXACT_CLOCK_WORKFLOW_ID,
    RunningHubLongCatClient,
)


ROLE_LABELS = {"yaya": "雅雅", "mengmeng": "檬檬"}
ROLE_PROFILE_IDS = {
    "yaya": os.environ.get("OPENMONTAGE_TTS_YAYA_PROFILE_ID", "").strip(),
    "mengmeng": os.environ.get("OPENMONTAGE_TTS_MENGMENG_PROFILE_ID", "").strip(),
}
ROLE_PRESET_FALLBACK_IDS = {
    "yaya": "openmontage-qwen-serena",
    "mengmeng": "openmontage-qwen-dylan",
}
LITE_RATE_CNY_PER_HOUR = 0.4
STANDARD_RATE_CNY_PER_HOUR = 4.0
ROLE_RESERVATION_CNY = 2.5
PRODUCTION_WORKFLOW_ID = INFINITETALK_448X560_EXACT_CLOCK_WORKFLOW_ID
PRODUCTION_WORKFLOW_PROFILE = INFINITETALK_448X560_EXACT_CLOCK_PROFILE
TRANSIENT_RETRY_DELAYS_SECONDS = (30, 90, 180)


def _voice_text_sha256(text: str) -> str:
    """Return a stable identity for one role's exact narration text."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_voice_manifest(output_dir: Path) -> dict[str, Any]:
    path = output_dir / "manifest.json"
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _can_reuse_voice_track(
    manifest: dict[str, Any],
    *,
    role: str,
    target: Path,
    profile_id: str,
    text_sha256: str,
) -> bool:
    """Never reuse a long-form track produced for different dialogue text."""
    track = ((manifest.get("tracks") or {}).get(role) or {}) if isinstance(manifest, dict) else {}
    decodable = False
    if target.is_file() and target.stat().st_size > 44:
        try:
            with wave.open(str(target), "rb") as stream:
                decodable = stream.getnchannels() > 0 and stream.getframerate() > 0 and stream.getnframes() > 0
        except (OSError, EOFError, wave.Error):
            decodable = False
    return bool(
        isinstance(track, dict)
        and decodable
        and str(track.get("profile_id") or "") == profile_id
        and str(track.get("text_sha256") or "") == text_sha256
    )


def _project_dir(run: dict[str, Any]) -> Path:
    project_id = str(run.get("project_id") or "")
    if not project_id:
        raise DailyAutomationError("每日任务尚未创建项目")
    path = PROJECTS_DIR / project_id
    if not path.is_dir():
        raise DailyAutomationError("每日任务项目目录不存在")
    return path


def _run_artifact(run: dict[str, Any], name: str) -> dict[str, Any]:
    from backlot.daily_automation import _read_json, _run_path

    return _read_json(_run_path(str(run["target_date"])).parent / name) or {}


def ensure_voicebox_ready(*, start_if_needed: bool = True) -> dict[str, Any]:
    """Return embedded TTS profiles, starting the local service once if needed."""
    first_error = ""
    try:
        profiles = VoiceboxTTS.list_profiles()
        return {"started": False, "profiles": profiles}
    except Exception as exc:  # noqa: BLE001 - converted into a Chinese preflight error below.
        first_error = str(exc)
    if not start_if_needed:
        raise DailyAutomationError(f"OpenMontage 本地配音当前不可用：{first_error}")
    if os.name != "nt":
        raise DailyAutomationError(f"OpenMontage 本地配音当前不可用，且非Windows环境不能自动启动：{first_error}")
    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    starter = Path(__file__).resolve().parents[1] / "scripts" / "start_local_tts.ps1"
    if not powershell or not starter.is_file():
        raise DailyAutomationError(
            "OpenMontage 本地配音当前不可用，自动启动器缺失；请查看 .backlot/daily-runs/scheduler.log"
        )
    result = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(starter),
            "-TimeoutSeconds",
            "45",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or first_error or "未知错误").strip()
        raise DailyAutomationError(
            f"OpenMontage 本地配音自动启动失败：{detail[:500]}；日志：.backlot/daily-runs/scheduler.log"
        )
    try:
        profiles = VoiceboxTTS.list_profiles()
    except Exception as exc:  # noqa: BLE001
        raise DailyAutomationError(
            f"OpenMontage 本地配音已执行启动但仍不可用：{exc}；日志：.backlot/daily-runs/scheduler.log"
        ) from exc
    return {"started": True, "profiles": profiles}


def _voicebox_profiles() -> dict[str, dict[str, Any]]:
    profiles = ensure_voicebox_ready()["profiles"]
    by_id = {str(item.get("id") or ""): item for item in profiles}
    resolved: dict[str, dict[str, Any]] = {}
    for role, profile_id in ROLE_PROFILE_IDS.items():
        profile = by_id.get(profile_id)
        if not profile:
            # Exact same-name matching is allowed, but the opposite role is
            # never silently replaced by the workstation default.
            label = ROLE_LABELS[role]
            profile = next((item for item in profiles if str(item.get("name") or "").strip() == label), None)
        if not profile:
            # A clean GitHub clone can still run end to end with distinct
            # checked-in presets. Importing the private role voice pack later
            # restores the original stable IDs without changing this flow.
            profile = by_id.get(ROLE_PRESET_FALLBACK_IDS[role])
        if not profile:
            raise DailyAutomationError(f"OpenMontage 本地配音中找不到与角色同名的“{ROLE_LABELS[role]}”音色")
        resolved[role] = profile
    return resolved


def preflight_daily_media(run: dict[str, Any], *, persist: bool = True) -> dict[str, Any]:
    """Validate every free local prerequisite before entering media stages."""
    config = read_config()
    runninghub = read_runninghub_config()
    issues: list[str] = []
    warnings: list[str] = []
    if not runninghub.get("configured"):
        issues.append("RunningHub 尚未完成安全配置")
    if str(runninghub.get("workflow_id") or "") != PRODUCTION_WORKFLOW_ID:
        issues.append(f"RunningHub 工作流必须为 {PRODUCTION_WORKFLOW_ID}")
    if str(runninghub.get("workflow_profile") or "") != PRODUCTION_WORKFLOW_PROFILE:
        issues.append(f"RunningHub 配置档必须为 {PRODUCTION_WORKFLOW_PROFILE}")
    runninghub_policy = config.get("runninghub") if isinstance(config.get("runninghub"), dict) else {}
    if str(runninghub_policy.get("primary_instance") or "") != "default":
        issues.append("正式生产必须使用 Standard 24GB（instanceType=default）")
    if runninghub_policy.get("allow_plus") is not False:
        issues.append("Plus 48GB 必须保持禁用")
    if float(config.get("max_budget_cny") or 0) > 5.0:
        issues.append("每日预算不得超过5元")
    if not shutil.which("ffmpeg"):
        issues.append("本机未找到 FFmpeg")
    if not shutil.which("node"):
        issues.append("本机未找到 Node.js，HyperFrames 无法工作")
    try:
        presenters = _presenter_images()
    except DailyAutomationError as exc:
        issues.append(str(exc))
        presenters = {}
    try:
        voicebox = ensure_voicebox_ready()
        profiles = _voicebox_profiles()
    except DailyAutomationError as exc:
        issues.append(str(exc))
        voicebox = {"started": False, "profiles": []}
        profiles = {}
    if (config.get("background_music") or {}).get("enabled"):
        if not (list_music_tracks().get("tracks") or []):
            issues.append("新闻背景音乐已启用，但本机没有可用曲目")
    if not os.environ.get("PEXELS_API_KEY"):
        warnings.append("Pexels未配置；网络实拍不足时将按合同降级到HyperFrames")
    if issues:
        raise DailyAutomationError("媒体启动预检未通过：" + "；".join(issues))
    result = {
        "status": "passed",
        "checked_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "workflow_id": PRODUCTION_WORKFLOW_ID,
        "workflow_profile": PRODUCTION_WORKFLOW_PROFILE,
        "instance_type": "default",
        "allow_plus": False,
        "budget_limit_cny": float(config.get("max_budget_cny") or 5.0),
        "voicebox_started": bool(voicebox.get("started")),
        "voicebox_profiles": {role: str(item.get("id") or "") for role, item in profiles.items()},
        "presenter_images": {role: path.name for role, path in presenters.items()},
        "warnings": warnings,
    }
    run["media_preflight"] = result
    if persist:
        _save_run(run)
    return result


def _transient_stage_error(error: object) -> bool:
    return bool(
        re.search(
            r"SSL|EOF|连接(?:中断|重置|失败)|timeout|timed out|HTTP\s*(?:429|50[234])|\b(?:429|502|503|504)\b",
            str(error),
            re.IGNORECASE,
        )
    )


def _run_stage_with_retry(
    run: dict[str, Any],
    stage_name: str,
    worker: Any,
    *,
    sleeper: Any = time.sleep,
) -> Any:
    """Retry only safe, idempotent stages; paid ambiguous submits never repeat."""
    attempts = 0
    while True:
        try:
            return worker(run)
        except Exception as exc:  # noqa: BLE001
            if (
                stage_name == "avatar"
                or not _transient_stage_error(exc)
                or attempts >= len(TRANSIENT_RETRY_DELAYS_SECONDS)
            ):
                raise
            delay = TRANSIENT_RETRY_DELAYS_SECONDS[attempts]
            attempts += 1
            heartbeat_stage(
                run,
                stage_name,
                message=f"检测到临时连接故障；{delay}秒后只重试当前阶段（第{attempts}次）",
                output={
                    "retry": {
                        "classification": "transient_transport",
                        "attempt": attempts,
                        "next_delay_seconds": delay,
                        "safe_resume_stage": stage_name,
                    }
                },
            )
            sleeper(delay)


def generate_long_voice_tracks(run: dict[str, Any]) -> dict[str, Any]:
    project_dir = _project_dir(run)
    script = _run_artifact(run, "daily_script.json")
    profiles = _voicebox_profiles()
    output_dir = project_dir / "assets" / "audio" / "daily-voice"
    output_dir.mkdir(parents=True, exist_ok=True)
    existing_manifest = _read_voice_manifest(output_dir)

    def generate(role: str) -> tuple[str, dict[str, Any]]:
        text = speaker_pure_text(script, role)
        if not text:
            raise DailyAutomationError(f"{ROLE_LABELS[role]}纯净版台词为空")
        target = output_dir / f"{role}-longform.wav"
        text_sha256 = _voice_text_sha256(text)
        operation_id = f"voicebox:{run['target_date']}:{role}:{str(profiles[role]['id'])}:{text_sha256[:16]}"
        reused = _can_reuse_voice_track(
            existing_manifest,
            role=role,
            target=target,
            profile_id=str(profiles[role]["id"]),
            text_sha256=text_sha256,
        )
        result = None
        if not reused:
            result = VoiceboxTTS().execute({
                "text": text,
                "profile_id": profiles[role]["id"],
                "profile_name": ROLE_LABELS[role],
                "language": "zh",
                "output_path": str(target),
                "timeout_seconds": 7200,
                "poll_seconds": 3,
            })
            if not result.success or not target.is_file():
                raise DailyAutomationError(str(result.error or f"{ROLE_LABELS[role]} Voicebox 配音失败"))
        try:
            clock = align_pcm_wav_to_frame_clock(target, fps=AVATAR_VIDEO_FPS)
        except AvatarAudioClockError as exc:
            raise DailyAutomationError(f"{ROLE_LABELS[role]} 长音频无法对齐 25FPS：{exc}") from exc
        return role, {
            "status": "completed", "path": str(target.relative_to(project_dir)),
            "profile_id": profiles[role]["id"], "profile_name": ROLE_LABELS[role],
            "generation_id": ((result.data or {}).get("generation_id") if result else None),
            "duration_seconds": round(float(clock["duration_seconds"]), 6),
            "text_sha256": text_sha256, "reused": reused,
            "operation_id": operation_id,
            "sha256": _file_sha256(target),
            **{
                key: clock[key]
                for key in (
                    "sample_rate", "sample_frame_count", "samples_per_video_frame",
                    "video_fps", "video_frame_count", "content_sample_frames",
                    "final_padding_sample_frames",
                )
            },
        }

    # The workstation Voicebox service runs Qwen cloned voices on CPU.  Long
    # tracks must be generated serially: concurrent requests contend for the
    # same model process and can leave both roles stalled or failed.  Keep the
    # deterministic role order so a completed first track remains reusable if
    # the second role needs a retry.
    outputs: dict[str, Any] = {}
    for role in ROLE_LABELS:
        generated_role, value = generate(role)
        outputs[generated_role] = value
    _atomic_json(output_dir / "manifest.json", {
        "version": "2.0-exact-clock",
        "tracks": {
            role: {
                "profile_id": str(value.get("profile_id") or ""),
                "text_sha256": str(value.get("text_sha256") or ""),
                "path": str(value.get("path") or ""),
                "sha256": str(value.get("sha256") or ""),
                "sample_rate": int(value.get("sample_rate") or 0),
                "sample_frame_count": int(value.get("sample_frame_count") or 0),
                "samples_per_video_frame": int(value.get("samples_per_video_frame") or 0),
                "video_fps": int(value.get("video_fps") or 0),
                "video_frame_count": int(value.get("video_frame_count") or 0),
                "content_sample_frames": int(value.get("content_sample_frames") or 0),
                "final_padding_sample_frames": int(value.get("final_padding_sample_frames") or 0),
            }
            for role, value in outputs.items()
        },
    })
    return outputs


def _presenter_images() -> dict[str, Path]:
    source = Path(str((read_config().get("avatar") or {}).get("source_directory") or ""))
    if not source.is_dir():
        raise DailyAutomationError("4:5 数字人角色图目录不存在，请在每日自动化设置中重新指定")
    files = [item for item in source.iterdir() if item.is_file() and item.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}]
    result: dict[str, Path] = {}
    for role, label in ROLE_LABELS.items():
        matches = [item for item in files if label in item.name]
        if len(matches) != 1:
            raise DailyAutomationError(f"4:5 角色图目录中必须且只能有一张包含“{label}”的图片")
        result[role] = matches[0]
    return result


def _estimated_cost(instance: str, elapsed_seconds: float, reserved: float) -> float:
    rate = STANDARD_RATE_CNY_PER_HOUR if instance == "default" else LITE_RATE_CNY_PER_HOUR
    # A small minimum keeps an absent provider billing field from being
    # mistaken for a free request, while the reservation remains the cap.
    return round(min(reserved, max(0.01, elapsed_seconds * rate / 3600.0)), 4)


def _settle_task(ledger: BudgetLedger, record: dict[str, Any], result: dict[str, Any], *, purpose: str) -> None:
    reserved = float(record.get("reserved_cny") or 0)
    exact = result.get("consume_money_cny")
    actual = float(exact) if isinstance(exact, (int, float)) else _estimated_cost(
        str(record.get("instance") or "lite"), max(0.0, time.time() - float(record.get("started_at") or time.time())), reserved,
    )
    operation_id = str(record.get("operation_id") or "")
    if operation_id:
        ledger.settle_once(operation_id, reserved, actual, purpose=purpose, task_id=str(record.get("task_id") or ""))
    else:
        ledger.settle(reserved, actual, purpose=purpose, task_id=str(record.get("task_id") or ""))
    record["actual_cost_cny"] = actual
    record["cost_source"] = "provider" if isinstance(exact, (int, float)) else "conservative_elapsed_estimate"
    billing = result.get("billing") if isinstance(result.get("billing"), dict) else {}
    if billing:
        # Keep only safe, structured evidence.  Raw API payloads can contain
        # signed output URLs and must never be copied into durable artifacts.
        record["billing"] = billing
        record["observed_instance"] = str(billing.get("observed_instance") or "unverified")
    record["reserved_cny"] = 0.0


def _unexpected_instance_error(record: dict[str, Any]) -> str | None:
    """Return a safe blocker if provider usage contradicts Lite-only policy."""
    billing = record.get("billing") if isinstance(record.get("billing"), dict) else {}
    observed = str(billing.get("observed_instance") or "unverified")
    requested = str(record.get("requested_instance") or "auto_lite")
    if requested == "auto_lite" and observed in {"standard_24gb", "plus_48gb"}:
        return (
            f"平台账单按 {observed.replace('_', ' ')} 费率结算，而本次未授权升级；"
            "已停止提交后续角色，避免继续产生高算力费用。请先在 RunningHub 确认企业 Lite 的实际调度策略。"
        )
    if requested == "auto_lite" and observed == "unverified":
        return "RunningHub 未返回可核验的时长与费用字段，已停止提交后续角色，避免无法审计的自动扣费。"
    return None


def _initial_avatar_instance(run: dict[str, Any]) -> tuple[str | None, str, str]:
    """Resolve the explicitly authorized initial RunningHub instance.

    ``default`` is RunningHub's Standard 24GB API value.  Omitting the field
    remains the only supported auto-scheduled Lite request form, but a
    particular durable run may explicitly authorize Standard after a failed
    Lite billing verification.  Keeping that choice in the run manifest makes
    a paid resume auditable without weakening the global Lite safety gate.
    """
    provider_policy = run.get("provider_policy") if isinstance(run.get("provider_policy"), dict) else {}
    approval_policy = run.get("approval_policy") if isinstance(run.get("approval_policy"), dict) else {}
    requested = str(
        provider_policy.get("authorized_instance")
        or approval_policy.get("authorized_instance")
        or ""
    ).strip().lower()
    if requested in {"default", "standard", "standard_24gb"}:
        return "default", "default", "Standard 24GB"
    return None, "auto_lite", "企业 Lite（自动调度）"


def generate_runninghub_avatars(run: dict[str, Any]) -> dict[str, Any]:
    """Generate or resume both durable RunningHub avatar tasks."""
    project_dir = _project_dir(run)
    voice = (run.get("stages", {}).get("voice") or {}).get("output") or {}
    images = _presenter_images()
    ledger = BudgetLedger(run)
    stage = run["stages"]["avatar"]
    provider_policy = run.get("provider_policy") if isinstance(run.get("provider_policy"), dict) else {}
    lite_only = provider_policy.get("lite_only") is True
    initial_instance_type, initial_instance, initial_label = _initial_avatar_instance(run)
    if lite_only and provider_policy.get("lite_verified") is not True:
        safety = daily_billing_safety()
        if safety.get("auto_schedule_eligible") is not True:
            raise DailyAutomationError(str(safety.get("message") or "RunningHub Lite 尚未通过实际账单验证"))
    client = RunningHubLongCatClient()
    records = stage.setdefault("output", {}).setdefault("roles", {})
    output_dir = project_dir / "assets" / "video" / "daily-avatar"
    output_dir.mkdir(parents=True, exist_ok=True)

    # Submit only one new paid task at a time.  For auto-Lite, the first task's
    # documented usage must prove the Lite rate before the second role starts.
    # A durable run may instead contain explicit Standard 24GB authorization;
    # that request uses instanceType=default and is still sequential so resume
    # cannot accidentally duplicate either presenter.
    for role, label in ROLE_LABELS.items():
        record = records.setdefault(role, {"role": role, "label": label, "history": []})
        target = output_dir / f"{role}-longform.mp4"
        if record.get("status") == "completed" and target.is_file() and target.stat().st_size > 0:
            # A completed durable record is authoritative during recursive
            # resume.  Do not resubmit just because a test/proxy produced a
            # compact MP4; later media QA owns content validation.
            continue
        if target.is_file() and target.stat().st_size > 4096:
            record.update({"status": "completed", "output_path": str(target.relative_to(project_dir)), "reused": True})
            continue
        if record.get("status") in {"submitted", "running"} and record.get("task_id"):
            continue
        if record.get("status") == "ambiguous":
            raise DailyAutomationError(
                f"{label}提交结果未知，禁止自动重提。请先在 RunningHub 核对任务列表并人工确认任务编号。"
            )
        audio_rel = str((voice.get(role) or {}).get("path") or "")
        audio_path = project_dir / audio_rel
        if not audio_path.is_file():
            raise DailyAutomationError(f"{label}的长音频不存在，不能提交数字人")
        try:
            clock = inspect_frame_clock_wav(
                audio_path,
                fps=AVATAR_VIDEO_FPS,
                require_aligned=True,
            )
        except AvatarAudioClockError as exc:
            raise DailyAutomationError(f"{label}的长音频不符合精确帧时钟：{exc}") from exc
        audio_sha256 = _file_sha256(audio_path)
        voice_record = voice.get(role) or {}
        if (
            str(voice_record.get("sha256") or "") != audio_sha256
            or int(voice_record.get("sample_frame_count") or 0) != int(clock["sample_frame_count"])
            or int(voice_record.get("video_frame_count") or 0) != int(clock["video_frame_count"])
        ):
            raise DailyAutomationError(f"{label}的长音频与配音账本不一致，已在上传和扣费前停止")
        input_hash = hashlib.sha256(
            (
                str(voice_record.get("text_sha256") or "") + "|" + audio_sha256 + "|"
                + str(clock["video_frame_count"]) + "|" + str(images[role].resolve()) + "|" + initial_instance
            ).encode("utf-8")
        ).hexdigest()
        operation_id = f"runninghub:{run['target_date']}:{role}:{input_hash[:20]}"
        operation = ensure_paid_operation(
            run, operation_id, stage="avatar", role=role, provider="runninghub",
            input_hash=input_hash, requested_instance=initial_instance,
        )
        if operation.get("state") == "ambiguous":
            record.update({"status": "ambiguous", "operation_id": operation_id})
            raise DailyAutomationError(f"{label}提交结果未知，禁止自动重提")
        image_remote = client.upload_file(images[role], file_type="image")
        audio_remote = client.upload_file(audio_path, file_type="audio")
        purpose = f"{label}{initial_label}数字人"
        ledger.reserve_once(operation_id, ROLE_RESERVATION_CNY, purpose=purpose)
        transition_paid_operation(run, operation_id, "reserved", reserved_cny=ROLE_RESERVATION_CNY)
        transition_paid_operation(run, operation_id, "submitting")
        try:
            submitted = client.submit(
                presenter_filename=image_remote,
                audio_filename=audio_remote,
                instance_type=initial_instance_type,
                exact_total_frames=int(clock["video_frame_count"]),
            )
        except Exception as exc:
            classification = classify_runninghub_failure(exc)
            if classification.get("kind") == "transient":
                transition_paid_operation(run, operation_id, "ambiguous", error=str(exc)[:500])
                record.update({"status": "ambiguous", "operation_id": operation_id, "last_failure": classification})
                heartbeat_stage(run, "avatar", message=f"{label}提交结果未知，已停止自动重提", output={"roles": records})
                raise DailyAutomationError(f"{label}提交响应中断，远端可能已接单；已进入模糊状态，禁止自动重提") from exc
            ledger.release_once(operation_id, ROLE_RESERVATION_CNY, purpose=purpose, reason="供应商明确拒绝，未建立任务")
            transition_paid_operation(run, operation_id, "released", error=str(exc)[:500])
            raise
        record.update({
            "status": "submitted", "task_id": submitted["task_id"], "instance": initial_instance,
            "requested_instance": initial_instance,
            "presenter_remote": image_remote, "audio_remote": audio_remote,
            "reserved_cny": ROLE_RESERVATION_CNY, "started_at": time.time(),
            "output_path": str(target.relative_to(project_dir)),
            "reused": False,
            "operation_id": operation_id,
            "audio_sha256": audio_sha256,
            "sample_rate": int(clock["sample_rate"]),
            "sample_frame_count": int(clock["sample_frame_count"]),
            "samples_per_video_frame": int(clock["samples_per_video_frame"]),
            "video_fps": int(clock["video_fps"]),
            "exact_total_frames": int(clock["video_frame_count"]),
        })
        transition_paid_operation(run, operation_id, "submitted", task_id=submitted["task_id"])
        record["history"].append({
            "task_id": submitted["task_id"], "instance": initial_instance,
            "submitted_at": time.time(), "reason": "run_authorized_initial_instance",
        })
        heartbeat_stage(run, "avatar", message=f"已提交 {label}{initial_label}数字人任务", output={"roles": records})
        # Do not risk duplicate paid work. Subsequent daily runs can reuse
        # already-completed long videos without contacting the provider.
        break

    deadline = time.monotonic() + 8 * 60 * 60
    while time.monotonic() < deadline:
        pending = [role for role, record in records.items() if record.get("status") not in {"completed", "failed"}]
        if not pending:
            break
        for role in pending:
            label = ROLE_LABELS[role]
            record = records[role]
            result = client.poll(str(record["task_id"]))
            if result["status"] == "RUNNING":
                record["status"] = "running"
                if record.get("operation_id"):
                    transition_paid_operation(run, str(record["operation_id"]), "running", task_id=record.get("task_id"))
                continue
            _settle_task(ledger, record, result, purpose=f"{label}{'Standard 24GB' if record.get('instance') == 'default' else '企业 Lite（自动调度）'}数字人")
            instance_blocker = _unexpected_instance_error(record)
            if instance_blocker:
                record["status"] = "failed"
                record["billing_blocker"] = instance_blocker
                heartbeat_stage(
                    run,
                    "avatar",
                    message=f"{label}账单不是企业 Lite；已停止后续角色",
                    output={"roles": records},
                )
                raise DailyAutomationError(instance_blocker)
            if result["status"] == "SUCCEEDED" and result.get("video_url"):
                target = project_dir / str(record["output_path"])
                client.download(str(result["video_url"]), target)
                record.update({"status": "completed", "video_url_recorded": True, "finished_at": time.time()})
                if record.get("operation_id"):
                    transition_paid_operation(run, str(record["operation_id"]), "succeeded", task_id=record.get("task_id"), output_path=record.get("output_path"))
                heartbeat_stage(run, "avatar", message=f"{label}数字人已完成；继续等待另一角色", output={"roles": records})
                continue
            failure = classify_runninghub_failure(result.get("error"))
            record["last_failure"] = failure
            if record.get("operation_id"):
                transition_paid_operation(run, str(record["operation_id"]), "failed", error=failure.get("message"))
            if record.get("instance") == "auto_lite" and failure["may_upgrade_to_standard"]:
                if lite_only:
                    record["status"] = "failed"
                    raise DailyAutomationError(
                        f"{label}企业 Lite 明确显存不足；本次用户只授权 0.4 元/小时 Lite，"
                        "已停止且不会升级 Standard 24GB。已完成资产与任务记录均已保留。"
                    )
                remaining = max(0.0, ledger.limit - ledger.committed)
                if remaining < 0.05:
                    record["status"] = "failed"
                    raise DailyAutomationError(f"{label}企业 Lite 明确显存不足，但剩余预算不足以升级 Standard 24GB")
                reservation = min(ROLE_RESERVATION_CNY, remaining)
                retry_operation_id = f"{str(record.get('operation_id') or '')}:standard:{len(record.get('history') or []) + 1}"
                ensure_paid_operation(run, retry_operation_id, stage="avatar", role=role, provider="runninghub", requested_instance="default")
                ledger.reserve_once(retry_operation_id, reservation, purpose=f"{label}显存不足后 Standard 24GB 重试")
                transition_paid_operation(run, retry_operation_id, "reserved", reserved_cny=reservation)
                transition_paid_operation(run, retry_operation_id, "submitting")
                try:
                    submitted = client.submit(
                        presenter_filename=str(record["presenter_remote"]),
                        audio_filename=str(record["audio_remote"]),
                        instance_type="default",
                        exact_total_frames=int(record["exact_total_frames"]),
                    )
                except Exception:
                    ledger.release_once(retry_operation_id, reservation, purpose=f"{label}显存不足后 Standard 24GB 重试", reason="Standard 提交失败，未建立任务")
                    transition_paid_operation(run, retry_operation_id, "released")
                    raise
                record.update({
                    "status": "submitted", "task_id": submitted["task_id"], "instance": "default",
                    "requested_instance": "default",
                    "reserved_cny": reservation, "started_at": time.time(),
                    "operation_id": retry_operation_id,
                })
                transition_paid_operation(run, retry_operation_id, "submitted", task_id=submitted["task_id"])
                record["history"].append({
                    "task_id": submitted["task_id"], "instance": "default", "submitted_at": time.time(),
                    "reason": "confirmed_oom_only",
                })
                heartbeat_stage(run, "avatar", message=f"{label}企业 Lite 明确显存不足，已按规则升级 Standard 24GB", output={"roles": records})
                continue
            if failure["kind"] == "transient" and record.get("instance") == "auto_lite":
                max_attempts = int((read_config().get("runninghub") or {}).get("max_lite_attempts") or 3)
                lite_attempts = sum(item.get("instance") == "auto_lite" for item in record.get("history") or [])
                if lite_attempts < max_attempts:
                    remaining = max(0.0, ledger.limit - ledger.committed)
                    reservation = min(ROLE_RESERVATION_CNY, remaining)
                    if reservation < 0.05:
                        record["status"] = "failed"
                        raise DailyAutomationError(f"{label}企业 Lite 可重试，但每日预算已不足")
                    retry_operation_id = f"{str(record.get('operation_id') or '')}:lite:{lite_attempts + 1}"
                    ensure_paid_operation(run, retry_operation_id, stage="avatar", role=role, provider="runninghub", requested_instance="auto_lite")
                    ledger.reserve_once(retry_operation_id, reservation, purpose=f"{label}企业 Lite 第 {lite_attempts + 1} 次尝试")
                    transition_paid_operation(run, retry_operation_id, "reserved", reserved_cny=reservation)
                    transition_paid_operation(run, retry_operation_id, "submitting")
                    try:
                        submitted = client.submit(
                            presenter_filename=str(record["presenter_remote"]),
                            audio_filename=str(record["audio_remote"]),
                            instance_type=None,
                            exact_total_frames=int(record["exact_total_frames"]),
                        )
                    except Exception:
                        ledger.release_once(retry_operation_id, reservation, purpose=f"{label}企业 Lite 第 {lite_attempts + 1} 次尝试", reason="Lite 重试提交失败，未建立任务")
                        transition_paid_operation(run, retry_operation_id, "released")
                        raise
                    record.update({
                        "status": "submitted", "task_id": submitted["task_id"], "instance": "auto_lite",
                        "requested_instance": "auto_lite",
                        "reserved_cny": reservation, "started_at": time.time(),
                        "operation_id": retry_operation_id,
                    })
                    transition_paid_operation(run, retry_operation_id, "submitted", task_id=submitted["task_id"])
                    record["history"].append({
                        "task_id": submitted["task_id"], "instance": "auto_lite", "submitted_at": time.time(),
                        "reason": failure["kind"],
                    })
                    heartbeat_stage(run, "avatar", message=f"{label}遇到排队、超时或网络问题，正在保持企业 Lite 重试 {lite_attempts + 1}/{max_attempts}", output={"roles": records})
                    continue
                record["status"] = "failed"
                raise DailyAutomationError(f"{label}企业 Lite 已重试 {max_attempts} 次；没有升级 Standard：{failure['message']}")
            record["status"] = "failed"
            raise DailyAutomationError(f"{label}数字人生成失败：{failure['message']}")
        heartbeat_stage(run, "avatar", message=f"{initial_label}数字人生成中：{sum(records[r].get('status') == 'completed' for r in records)}/2", output={"roles": records})
        # The second role is only submitted after the first role completed.
        # Auto-Lite additionally passes the billing guard above before resume.
        unsubmitted = next((role for role in ROLE_LABELS if records.get(role, {}).get("status") not in {"completed", "failed", "submitted", "running"}), None)
        active_submission = any(
            record.get("status") in {"submitted", "running"}
            for record in records.values()
        )
        if unsubmitted and not active_submission:
            # Re-enter through the durable function after persisting the
            # verified pilot.  This keeps all submission code in one place.
            heartbeat_stage(run, "avatar", message=f"{initial_label}首个角色已完成，正在提交另一位角色", output={"roles": records})
            return generate_runninghub_avatars(run)
        if any(records[role].get("status") not in {"completed", "failed"} for role in records):
            time.sleep(20)
    unfinished = [ROLE_LABELS[role] for role, record in records.items() if record.get("status") != "completed"]
    if unfinished:
        raise DailyAutomationError(f"数字人等待超过 8 小时：{'、'.join(unfinished)}；任务编号已保存，可继续追踪")
    return records


def align_and_apply_avatars(run: dict[str, Any]) -> dict[str, Any]:
    project_dir = _project_dir(run)
    package = initialize_avatar_package(project_dir)
    if package.get("import_mode") != "longform":
        raise DailyAutomationError("每日自动化项目必须使用每位角色一条长视频的切割模式")
    roles = (run["stages"]["avatar"].get("output") or {}).get("roles") or {}
    for role, label in ROLE_LABELS.items():
        record = roles.get(role) or {}
        source = project_dir / str(record.get("output_path") or "")
        if not source.is_file():
            raise DailyAutomationError(f"{label}数字人长视频不存在")
        current = next((item for item in package.get("speakers", []) if item.get("speaker_id") == role), {})
        existing_path = project_dir / str((current.get("source") or {}).get("path") or "")
        if existing_path.is_file() and existing_path.stat().st_size == source.stat().st_size:
            continue
        temporary, target = prepare_upload(project_dir, source.name, speaker_id=role)
        shutil.copy2(source, temporary)
        package = finalize_upload(project_dir, temporary, target, source.name, speaker_id=role)

    if package.get("asr", {}).get("status") != "passed":
        local_models = list_local_whisper_models()
        if not local_models:
            raise DailyAutomationError("本机没有已安装的 faster-whisper 模型；无法在无人值守模式核对数字人台词")
        preferred = next((item for item in local_models if "small" in str(item.get("label") or "").lower()), local_models[0])
        model_path = str(preferred["id"])
        # A process can stop after ``start_avatar_asr`` persisted the running
        # marker but before transcription began.  Resume that marker directly
        # instead of treating it as a concurrent job or resetting the package.
        if package.get("asr", {}).get("status") != "running":
            start_avatar_asr(project_dir, {"model": model_path})
        package = run_avatar_asr(project_dir, {"model": model_path})
        if package.get("asr", {}).get("status") != "passed":
            raise DailyAutomationError("数字人长视频 ASR 未通过；已保留诊断，需早间人工核对")
    package = approve_high_confidence_longform_cuts(project_dir)
    turn_by_id = {str(item.get("turn_id")): item for item in package.get("turns", [])}
    for item in (package.get("cut_plan") or {}).get("items") or []:
        if item.get("status") != "pending_review":
            continue
        turn = turn_by_id.get(str(item.get("turn_id"))) or {}
        if (
            float(turn.get("asr_similarity") or 0) >= float(package["settings"].get("minimum_turn_similarity") or .86)
            and float(turn.get("asr_coverage") or 0) >= float(package["settings"].get("minimum_turn_coverage") or .8)
            and isinstance(item.get("start_seconds"), (int, float))
            and isinstance(item.get("end_seconds"), (int, float))
            and float(item["end_seconds"]) > float(item["start_seconds"])
        ):
            package = approve_longform_cut(project_dir, str(item["turn_id"]))
    if (package.get("cut_plan") or {}).get("status") != "approved":
        summary = (package.get("cut_plan") or {}).get("summary") or {}
        raise DailyAutomationError(f"仍有 {summary.get('needs_manual', 0) + summary.get('pending_review', 0)} 个切点需人工核对")
    if package.get("assembly", {}).get("status") != "passed":
        start_avatar_assembly(project_dir)
        package = assemble_avatar_package(project_dir)
    state = apply_avatar_package_to_timeline(project_dir, {"default_treatment": "pip_top_left"})
    configured_shape = str(((read_config().get("avatar") or {}).get("shape") or "rounded"))
    layouts = state.get("presenter_layouts") or {}
    template_id = str(layouts.get("default_template_id") or "pip_top_left")
    template = next((item for item in layouts.get("templates") or [] if item.get("id") == template_id), None) or {}
    state = update_presenter_layout_template(project_dir, {
        "template_id": template_id,
        "name": template.get("name") or "左上角解说员",
        "geometry": template.get("geometry") or {"x": .035, "y": .04, "width": .29},
        "crop_bottom": template.get("crop_bottom") or 0.0,
        "shape": configured_shape,
        "apply_scope": "all",
        "set_default": True,
    })
    return {
        "assembly": package.get("assembly"),
        "timeline_revision": (state.get("timeline") or {}).get("revision"),
        "scene_count": len(state.get("scenes") or []),
    }


def generate_supporting_visuals(run: dict[str, Any]) -> dict[str, Any]:
    project_dir = _project_dir(run)
    existing_state = read_workbench(project_dir)
    existing_batch = ((existing_state.get("automation") or {}).get("visual_batch") or {})
    failed_items = [
        item for item in (existing_batch.get("items") or [])
        if isinstance(item, dict) and item.get("status") == "failed"
    ]
    if failed_items:
        recovered: list[dict[str, Any]] = []
        for failed_item in failed_items:
            scene_id = str(failed_item.get("scene_id") or "")
            block_id = str(failed_item.get("block_id") or "")
            if not scene_id or not block_id:
                raise DailyAutomationError("失败画面槽缺少 scene_id 或 block_id，无法安全恢复")
            started = start_visual_block_refresh(project_dir, scene_id, block_id, {
                "confirmed": True,
                "instruction": "自动恢复上次失败槽；优先使用不重复的实拍素材，失败时保留现有成功画面",
            })
            job_id = str(((started.get("automation") or {}).get("visual_batch") or {}).get("job_id") or "")
            state = generate_visual_batch(project_dir, expected_job_id=job_id)
            refreshed = (state.get("automation") or {}).get("visual_batch") or {}
            if refreshed.get("status") not in {"completed", "completed_with_warnings"}:
                detail = next(
                    (str(item.get("error") or "") for item in (refreshed.get("items") or []) if item.get("status") == "failed"),
                    "失败画面槽恢复未完成",
                )
                raise DailyAutomationError(detail or "失败画面槽恢复未完成")
            recovered.append({"scene_id": scene_id, "block_id": block_id, "job_id": job_id})
        final_state = read_workbench(project_dir)
        ready_blocks = sum(
            1
            for scene in (final_state.get("scenes") or [])
            for block in (((scene.get("visual_timeline") or {}).get("blocks") or []))
            if block.get("status") == "ready" and block.get("asset_id")
        )
        return {
            "planning_mode": "failed_slot_resume",
            "recovered_slots": recovered,
            "completed_slots": ready_blocks,
            "failed_slots": 0,
        }
    visual_policy = run.get("visual_policy") if isinstance(run.get("visual_policy"), dict) else {}
    requested_mode = str(visual_policy.get("planning_mode") or "").strip()
    if requested_mode in {"ai_director", "rule_mix"}:
        planning_mode = requested_mode
    else:
        planning_mode = "ai_director" if read_text_ai_config().get("configured") else "rule_mix"
    payload = {
        "selection_mode": "missing",
        "operation_mode": "fill_missing",
        "profile": "daily_news",
        # 60—70% network footage leaves roughly 30—40% for semantic
        # HyperFrames, measured by duration rather than slot count.
        "mix_strategy": "video_first",
        "image_source": "web_download",
        "content_rules": ["no_frontal_face", "no_large_text_watermark"],
        "person_policy": "balanced",
        "candidate_limit": 6,
        "planning_mode": planning_mode,
        "ai_planning_confirmed": planning_mode == "ai_director",
    }
    preview = preview_visual_batch_plan(project_dir, payload)
    started = start_visual_batch_generation(project_dir, {
        **payload,
        "confirmed": True,
        "reviewed_plan": preview,
        "copy_presenter_layout": False,
    })
    job_id = str(((started.get("automation") or {}).get("visual_batch") or {}).get("job_id") or "")
    state = generate_visual_batch(project_dir, expected_job_id=job_id)
    batch = (state.get("automation") or {}).get("visual_batch") or {}
    if batch.get("status") not in {"completed", "completed_with_warnings"}:
        raise DailyAutomationError(str(batch.get("error") or "主体画面批量生成未完成"))
    return {
        "planning_mode": planning_mode,
        "plan_id": preview.get("plan_id"),
        "total_slots": batch.get("total_slots"),
        "completed_slots": batch.get("completed_slots"),
        "failed_slots": batch.get("failed_slots"),
    }


def validate_daily_review_candidate(project_dir: Path, output_path: str | Path) -> dict[str, Any]:
    """Probe the actual final preview and persist a morning-review QA report."""
    target = Path(output_path)
    if not target.is_absolute():
        target = project_dir / target
    issues: list[str] = []
    ffprobe = shutil.which("ffprobe")
    ffmpeg = shutil.which("ffmpeg")
    if not target.is_file() or target.stat().st_size < 4096:
        raise DailyAutomationError("全片预览文件不存在或为空，不能进入早间审核")
    if not ffprobe or not ffmpeg:
        raise DailyAutomationError("未找到 ffmpeg/ffprobe，无法执行最终媒体质检")
    probe_result = subprocess.run(
        [ffprobe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(target)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60, check=False,
    )
    if probe_result.returncode != 0:
        raise DailyAutomationError("ffprobe 无法读取最终全片预览")
    probe = json.loads(probe_result.stdout)
    streams = probe.get("streams") or []
    video = next((row for row in streams if row.get("codec_type") == "video"), {})
    audio = next((row for row in streams if row.get("codec_type") == "audio"), {})
    duration = float((probe.get("format") or {}).get("duration") or 0.0)
    width, height = int(video.get("width") or 0), int(video.get("height") or 0)
    if (width, height) != (1080, 1920):
        issues.append(f"最终画幅应为 1080×1920，实际为 {width}×{height}")
    if duration <= 1.0:
        issues.append("最终视频时长异常")
    if not audio:
        issues.append("最终视频没有音频流")
    loudness: dict[str, Any] = {}
    if audio:
        loudness_probe = subprocess.run(
            [ffmpeg, "-hide_banner", "-i", str(target), "-af", "loudnorm=I=-14:LRA=11:TP=-1.5:print_format=json", "-f", "null", "-"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180, check=False,
        )
        matches = re.findall(r"\{\s*\"input_i\".*?\}", loudness_probe.stderr, re.DOTALL)
        if matches:
            measured = json.loads(matches[-1])
            loudness = {
                "integrated_lufs": float(measured.get("input_i")),
                "true_peak_dbtp": float(measured.get("input_tp")),
                "loudness_range_lu": float(measured.get("input_lra")),
                "target_lufs": -14.0,
                "true_peak_limit_dbtp": -1.5,
            }
            if not -15.5 <= loudness["integrated_lufs"] <= -12.5:
                issues.append(f"最终响度应接近 -14 LUFS，实际为 {loudness['integrated_lufs']:.1f} LUFS")
            if loudness["true_peak_dbtp"] > -1.0:
                issues.append(f"最终 True Peak 过高：{loudness['true_peak_dbtp']:.1f} dBTP")
        else:
            issues.append("无法解析最终成片响度报告")

    state = read_workbench(project_dir)
    scenes = state.get("scenes") or []
    expected_duration = max((float(row.get("end_seconds") or 0.0) for row in scenes), default=0.0)
    tolerance = max(1.5, expected_duration * 0.03)
    if expected_duration and abs(duration - expected_duration) > tolerance:
        issues.append(f"全片时长与时间线偏差过大：{duration:.2f}s / {expected_duration:.2f}s")
    missing_presenter = [str(row.get("id")) for row in scenes if not (row.get("presenter") or {}).get("source_path")]
    missing_visual = [
        str(row.get("id")) for row in scenes
        if not any(block.get("status") == "ready" and block.get("asset_id") for block in ((row.get("visual_timeline") or {}).get("blocks") or []))
    ]
    if missing_presenter:
        issues.append(f"{len(missing_presenter)} 个片段缺少数字人时间线")
    if missing_visual:
        issues.append(f"{len(missing_visual)} 个片段缺少主体画面")

    asset_lookup = {
        str(row.get("id") or ""): row for row in (state.get("assets") or []) if isinstance(row, dict)
    }
    visual_durations = {"stock_video": 0.0, "hyperframes": 0.0, "other": 0.0}
    used_stock_identities: dict[str, dict[str, str]] = {}
    duplicate_stock_usages: list[dict[str, str]] = []
    visual_cadence = {
        "stock_video_target_seconds": 3.0,
        "stock_video_max_seconds": 3.6,
        "hyperframes_max_seconds": 5.0,
        "blocks": [],
    }
    for scene in scenes:
        for block in ((scene.get("visual_timeline") or {}).get("blocks") or []):
            if not isinstance(block, dict) or block.get("status") != "ready":
                continue
            asset = asset_lookup.get(str(block.get("asset_id") or ""), {})
            path = str(asset.get("path") or "").replace("\\", "/").lower()
            source_tool = str(asset.get("source_tool") or "").lower()
            provider = str(asset.get("provider") or "").lower()
            source_type = str(asset.get("source_type") or "").lower()
            if "/hyperframes/" in path or "hyperframes" in source_tool:
                route = "hyperframes"
            elif (
                source_type == "web_download" or provider == "pexels" or "/pexels/" in path
                or source_tool in {"pexels_video", "pexels_image", "pexels_curated_hotswap", "stock_image_motion"}
            ):
                route = "stock_video"
            else:
                route = "other"
            block_duration = max(
                0.0, float(block.get("end_seconds") or 0.0) - float(block.get("start_seconds") or 0.0)
            )
            visual_durations[route] += block_duration
            cadence_row = {
                "scene_id": str(scene.get("id") or ""),
                "block_id": str(block.get("id") or ""),
                "asset_id": str(block.get("asset_id") or ""),
                "route": route,
                "duration_seconds": round(block_duration, 3),
            }
            visual_cadence["blocks"].append(cadence_row)
            if route == "stock_video" and block_duration > visual_cadence["stock_video_max_seconds"] + .001:
                issues.append(
                    f"{cadence_row['scene_id']}/{cadence_row['block_id']} 实拍素材连续 {block_duration:.2f}s；"
                    "短视频实拍镜头应约3秒切换一次"
                )
            if route == "hyperframes" and block_duration > visual_cadence["hyperframes_max_seconds"] + .001:
                issues.append(
                    f"{cadence_row['scene_id']}/{cadence_row['block_id']} HyperFrames连续 {block_duration:.2f}s；"
                    "单段渲染画面不得超过5秒"
                )
            if route == "stock_video":
                generation = asset.get("generation") if isinstance(asset.get("generation"), dict) else {}
                provider_id = str(generation.get("video_id") or generation.get("photo_id") or "").strip()
                source_url = str(asset.get("source_url") or generation.get("source_url") or "").strip()
                identity = f"provider:{provider_id}" if provider_id else (f"url:{source_url}" if source_url else f"asset:{cadence_row['asset_id']}")
                previous = used_stock_identities.get(identity)
                if previous:
                    duplicate = {
                        "identity": identity,
                        "first_scene_id": previous["scene_id"],
                        "first_block_id": previous["block_id"],
                        "duplicate_scene_id": cadence_row["scene_id"],
                        "duplicate_block_id": cadence_row["block_id"],
                    }
                    duplicate_stock_usages.append(duplicate)
                    issues.append(
                        f"网络素材重复使用：{previous['scene_id']}/{previous['block_id']} 与 "
                        f"{cadence_row['scene_id']}/{cadence_row['block_id']} 指向同一素材"
                    )
                else:
                    used_stock_identities[identity] = {
                        "scene_id": cadence_row["scene_id"], "block_id": cadence_row["block_id"]
                    }
    classified_duration = visual_durations["stock_video"] + visual_durations["hyperframes"]
    stock_share = visual_durations["stock_video"] / classified_duration if classified_duration else 0.0
    hyperframes_share = visual_durations["hyperframes"] / classified_duration if classified_duration else 0.0
    visual_mix = {
        "stock_video_duration_seconds": round(visual_durations["stock_video"], 3),
        "hyperframes_duration_seconds": round(visual_durations["hyperframes"], 3),
        "other_duration_seconds": round(visual_durations["other"], 3),
        "stock_video_share": round(stock_share, 4),
        "hyperframes_share": round(hyperframes_share, 4),
        "basis": "resolved_asset_provenance",
    }
    if expected_duration and classified_duration >= expected_duration * 0.8:
        if not 0.60 - 0.001 <= stock_share <= 0.70 + 0.001:
            issues.append(f"实拍网络素材占比应为60%—70%，实际为{stock_share * 100:.1f}%")
        if not 0.30 - 0.001 <= hyperframes_share <= 0.40 + 0.001:
            issues.append(f"HyperFrames占比应为30%—40%，实际为{hyperframes_share * 100:.1f}%")

    frame_dir = project_dir / "artifacts" / "daily-delivery-qa" / "frames"
    frame_dir.mkdir(parents=True, exist_ok=True)
    frames: list[dict[str, Any]] = []
    for index, fraction in enumerate((0.02, 0.25, 0.50, 0.75, 0.98), 1):
        timestamp = min(max(0.0, duration * fraction), max(0.0, duration - 0.05))
        frame_path = frame_dir / f"frame-{index:02d}.jpg"
        extracted = subprocess.run(
            [ffmpeg, "-y", "-ss", f"{timestamp:.3f}", "-i", str(target), "-frames:v", "1", "-q:v", "2", str(frame_path)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90, check=False,
        )
        valid = extracted.returncode == 0 and frame_path.is_file() and frame_path.stat().st_size > 1024
        if not valid:
            issues.append(f"无法抽取第 {index} 张质检帧")
        frames.append({"index": index, "timestamp_seconds": round(timestamp, 3), "path": str(frame_path.relative_to(project_dir)), "valid": valid})

    render_report_path = project_dir / "artifacts" / "full_preview_render_report.json"
    render_report = json.loads(render_report_path.read_text(encoding="utf-8")) if render_report_path.is_file() else {}
    expected_story_ids = list(dict.fromkeys(str(row.get("story_id") or "") for row in scenes if row.get("story_id")))
    headline_report = render_report.get("story_headlines") if isinstance(render_report.get("story_headlines"), dict) else {}
    headline_assets = headline_report.get("assets") if isinstance(headline_report.get("assets"), list) else []
    rendered_story_ids = {str(row.get("story_id") or "") for row in headline_assets if isinstance(row, dict)}
    missing_headlines = [story_id for story_id in expected_story_ids if story_id not in rendered_story_ids]
    if missing_headlines:
        issues.append(f"{len(missing_headlines)} 条新闻缺少固定小标题：{','.join(missing_headlines)}")
    for asset in headline_assets:
        if not isinstance(asset, dict):
            continue
        title_path = project_dir / str(asset.get("path") or "")
        if not title_path.is_file():
            issues.append(f"新闻小标题资产不存在：{asset.get('story_id') or 'unknown'}")
    final_review_status = str((((render_report.get("data") or {}).get("final_review") or {}).get("status") or ""))
    if final_review_status != "pass":
        issues.append("底层合成器最终审查未通过")
    qa = {
        "version": "1.0",
        "status": "passed" if not issues else "failed",
        "output_path": str(target.relative_to(project_dir)),
        "technical_probe": {
            "duration_seconds": round(duration, 3), "expected_duration_seconds": round(expected_duration, 3),
            "width": width, "height": height, "video_codec": video.get("codec_name"),
            "audio_codec": audio.get("codec_name"), "has_audio": bool(audio), "file_size_bytes": target.stat().st_size,
            "loudness": loudness,
        },
        "timeline_checks": {
            "scene_count": len(scenes), "missing_presenter_scene_ids": missing_presenter,
            "missing_supporting_visual_scene_ids": missing_visual, "actual_visual_mix": visual_mix,
            "visual_cadence": visual_cadence,
            "duplicate_stock_usages": duplicate_stock_usages,
        },
        "frames": frames,
        "compose_final_review_status": final_review_status,
        "issues": issues,
    }
    _atomic_json(project_dir / "artifacts" / "daily_delivery_qa.json", qa)
    if issues:
        raise DailyAutomationError("最终媒体质检未通过：" + "；".join(issues))
    return qa


def render_review_candidate(run: dict[str, Any]) -> dict[str, Any]:
    project_dir = _project_dir(run)
    config = read_config()
    if (config.get("background_music") or {}).get("enabled"):
        catalog = list_music_tracks()
        tracks = catalog.get("tracks") or []
        if not tracks:
            raise DailyAutomationError("已要求使用默认新闻背景音乐，但本地新闻曲库为空")
        update_music_policy(project_dir, {
            "enabled": True,
            "track_id": tracks[0]["id"],
            "playback_gain_db": read_music_preferences().get("playback_gain_db", -8.0),
        })
        start_music_sample(project_dir)
        generate_music_sample(project_dir)
        # The user explicitly pre-authorized the workstation's confirmed
        # default track/gain for unattended drafts; morning review remains the
        # publication gate.
        approve_music_sample(project_dir, {"confirmed": True})
    start_full_preview_render(project_dir, {"confirmed": True})
    state = generate_full_preview_render(project_dir)
    preview = ((state.get("automation") or {}).get("preview_render") or {})
    if preview.get("status") != "completed":
        raise DailyAutomationError(str(preview.get("error") or "全片预览合成未完成"))
    qa = validate_daily_review_candidate(project_dir, str(preview.get("output_path") or ""))
    return {
        "output_path": preview.get("output_path"),
        "version": preview.get("version"),
        "music_enabled": bool((state.get("music_policy") or {}).get("enabled")),
        "qa_path": "artifacts/daily_delivery_qa.json",
        "qa_status": qa.get("status"),
    }


def run_daily_pipeline(target: date | str, *, trigger: str = "manual") -> dict[str, Any]:
    """Run or resume the complete unattended draft pipeline."""
    run_research_and_script(target, trigger=trigger)
    run = read_run(target)
    if not run:
        raise DailyAutomationError("每日任务状态未建立")
    if run.get("status") == "awaiting_human" and run.get("current_stage") == "script":
        # Text recovery exhausted its bounded attempts and deliberately stopped
        # before project creation, Voicebox, or RunningHub.
        return run
    script_path = RUNS_ROOT / str(run.get("target_date") or target) / "daily_script.json"
    script = _read_json(script_path) or {}
    release = evaluate_media_release(script)
    run["media_release_decision"] = release
    if release["decision"] == "blocked":
        run["status"] = "blocked"
        run["current_stage"] = "voice"
        _save_run(run)
        return read_run(target) or run
    if release["decision"] == "awaiting_human" and not fallback_approval_matches(run, script):
        run["status"] = "awaiting_human"
        run["current_stage"] = "voice"
        _save_run(run)
        return read_run(target) or run
    preflight_daily_media(run)
    run = read_run(target) or run
    _, requested_instance, _ = _initial_avatar_instance(run)
    avatar_message = (
        "正在依次提交本次已授权的 RunningHub Standard 24GB 数字人"
        if requested_instance == "default"
        else "正在依次提交企业 Lite 数字人；Standard 仅限显存不足"
    )
    stages: list[tuple[str, str, Any]] = [
        ("voice", "正在用同名 Voicebox 音色生成两条长音频", generate_long_voice_tracks),
        ("avatar", avatar_message, generate_runninghub_avatars),
        ("align", "正在按编号台词切割并排列数字人原声时间线", align_and_apply_avatars),
        ("visuals", "正在按 AI 画面规划补全网络视频与 HyperFrames 动态画面", generate_supporting_visuals),
        ("compose", "正在应用默认字幕与新闻背景音乐并合成全片预览", render_review_candidate),
    ]
    for stage_name, message, worker in stages:
        if run["stages"][stage_name].get("status") == "succeeded":
            continue
        if stage_name == "avatar":
            eligibility = provider_media_eligibility(run)
            run["provider_eligibility"] = eligibility
            if eligibility.get("eligible") is not True:
                run["status"] = "awaiting_provider_authorization"
                run["current_stage"] = "avatar"
                run["stages"]["avatar"]["status"] = "awaiting_provider_authorization"
                run["stages"]["avatar"]["message"] = str(eligibility.get("reason") or "供应商资格尚未满足")
                _save_run(run)
                return read_run(target) or run
        update_stage(run, stage_name, "running", message=message)
        try:
            output = _run_stage_with_retry(run, stage_name, worker)
        except Exception as exc:
            if stage_name == "avatar":
                roles = (((run.get("stages") or {}).get("avatar") or {}).get("output") or {}).get("roles") or {}
                if any(isinstance(item, dict) and item.get("status") == "ambiguous" for item in roles.values()):
                    run["status"] = "ambiguous"
                    run["current_stage"] = "avatar"
                    run["stages"]["avatar"]["status"] = "ambiguous"
                    run["stages"]["avatar"]["message"] = "供应商提交结果未知，已禁止自动重提"
                    run["stages"]["avatar"]["error"] = str(exc)[:1000]
                    _save_run(run)
                    return read_run(target) or run
            update_stage(run, stage_name, "failed", message="本阶段失败；已保留进度，可从这里继续", error=str(exc))
            raise
        update_stage(run, stage_name, "succeeded", message=f"{message.replace('正在', '')}完成", output=output if isinstance(output, dict) else {})
        run = read_run(target) or run
    if run["stages"]["review_ready"].get("status") != "succeeded":
        update_stage(run, "review_ready", "running", message="正在整理早间人工审核入口")
        project_dir = _project_dir(run)
        state = read_workbench(project_dir)
        preview = ((state.get("automation") or {}).get("preview_render") or {})
        compose_output = ((run.get("stages") or {}).get("compose") or {}).get("output") or {}
        update_stage(run, "review_ready", "succeeded", message="全片预览已就绪，等待人工审核后再发布", output={
            "project_id": run.get("project_id"),
            "preview_path": preview.get("output_path"),
            "qa_path": compose_output.get("qa_path"),
            "qa_status": compose_output.get("qa_status"),
            "publish_requires_human": True,
        })
    return read_run(target) or run
