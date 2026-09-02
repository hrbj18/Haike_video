"""Durable, multi-speaker cloud-avatar orchestration.

The provider receives one actual presenter image and one driving-audio file for
each generated clip.  This module therefore keeps the project contract at the
same granularity: a speaker binding owns identity references, a presenter shot
and one sample approval; a turn snapshots those inputs at queue time.  A later
replacement of a role image or an audio take never mutates a task already sent
to the provider.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import tempfile
import time
import unicodedata
from pathlib import Path
from threading import RLock
from typing import Any
from uuid import uuid4

from PIL import Image, ImageFilter, ImageOps, UnidentifiedImageError

from backlot.audio_center import get_voice_profile, read_audio_center
from backlot.tts_runtime import generate_voice_audio
from backlot.avatar_audio_clock import (
    AVATAR_VIDEO_FPS,
    AvatarAudioClockError,
    align_pcm_wav_to_frame_clock,
    inspect_frame_clock_wav,
)
from backlot.avatar_import import (
    AvatarImportError,
    _file_sha256,
    _find_binary,
    _find_turn,
    _now,
    _run,
    _safe_project_file,
    _safe_relative,
    _save_package,
    finalize_upload,
    prepare_upload,
    read_avatar_package,
)
from backlot.avatar_roles import AvatarRoleError, get_avatar_role
from tools.audio.voicebox_tts import VoiceboxTTS
from tools.avatar.dashscope_avatar import DashscopeAvatarError, DashscopeWanS2VClient
from tools.avatar.runninghub_avatar import (
    INFINITETALK_448X560_EXACT_CLOCK_PROFILE,
    INFINITETALK_448X560_EXACT_CLOCK_WORKFLOW_ID,
    RunningHubAvatarError,
    RunningHubLongCatClient,
    runninghub_configuration,
)


PRESENTER_DIRECTORY = Path("assets/incoming/avatar/presenter")
DRIVING_AUDIO_DIRECTORY = Path("assets/incoming/avatar/driving_audio")
NORMALIZED_AUDIO_DIRECTORY = Path("assets/audio/avatar_driving")
NORMALIZED_PRESENTER_DIRECTORY = Path("assets/incoming/avatar/cloud_input")
VOICEBOX_CANDIDATE_DIRECTORY = Path("assets/audio/avatar_driving")
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
AUDIO_EXTENSIONS = {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg"}
MAX_PRESENTER_IMAGE_BYTES = 25 * 1024 * 1024
MAX_DRIVING_AUDIO_BYTES = 15 * 1024 * 1024
MAX_DRIVING_AUDIO_SECONDS = 20.0
POLL_INTERVAL_SECONDS = 15.0
POLL_TIMEOUT_SECONDS = 20 * 60
ACTIVE_JOB_STATES = {"queued", "uploading", "detecting", "submitted", "running", "downloading"}
CLOUD_GENERATION_MODES = {"dashscope_wan_s2v", "runninghub_longcat"}
VOICEBOX_BATCH_MODES = {"missing_and_apply", "all_candidates", "failed_only_and_apply"}
# Voicebox is a local service with limited capacity.  Both batch and manual
# candidates share this re-entrant lock, so a direct click cannot create a
# concurrent synthesis request while a script-order batch is in progress.
VOICEBOX_BATCH_LOCK = RLock()
CLOUD_TURN_LOCKS: dict[str, RLock] = {}
CLOUD_TURN_LOCKS_GUARD = RLock()
ASPECT_PROFILES = {
    "portrait": {"label": "竖版 9:16", "width": 1080, "height": 1920},
    "landscape": {"label": "横版 16:9", "width": 1920, "height": 1080},
    "square": {"label": "方形 1:1", "width": 1080, "height": 1080},
}
ASPECT_FIT_MODES = {"cover_crop", "contain_blur"}
ASPECT_MATCH_TOLERANCE_PERCENT = 1.0
RUNNINGHUB_EXACT_CLOCK_MODEL = "InfiniteTalk-exact-clock-v2"
RUNNINGHUB_EXACT_CLOCK_RESOLUTION = "448x560"
RUNNINGHUB_EXACT_CLOCK_INPUT = {"label": "供应商输入 4:5 · 448×560", "width": 448, "height": 560}


class AvatarCloudError(AvatarImportError):
    """A correctable cloud-avatar workflow error."""


def _package(project_dir: Path) -> dict:
    package = read_avatar_package(project_dir)
    if not package:
        raise AvatarCloudError("请先建立数字人素材包")
    if package.get("generation_mode") not in CLOUD_GENERATION_MODES:
        raise AvatarCloudError("当前素材包不是云端数字人口播模式")
    _ensure_speaker_bindings(package)
    return package


def _cloud(package: dict) -> dict:
    cloud = package.get("cloud")
    if not isinstance(cloud, dict):
        raise AvatarCloudError("数字人云端配置缺失，请重新建立素材包")
    cloud.setdefault("sample_turn_id", None)
    cloud.setdefault("sample_turn_ids", [])
    cloud.setdefault("sample_approved", False)
    cloud.setdefault("batch_started", False)
    settings = package.get("settings") if isinstance(package.get("settings"), dict) else {}
    width, height = int(settings.get("width") or 1080), int(settings.get("height") or 1920)
    cloud.setdefault("aspect_ratio", _aspect_from_dimensions(width, height))
    cloud.setdefault("input_fit_mode", "cover_crop")
    cloud.setdefault("render_spec_revision", 1)
    if package.get("generation_mode") == "runninghub_longcat":
        status = runninghub_configuration()
        cloud["configuration"] = {
            key: status.get(key)
            for key in ("configured", "api_key_configured", "workflow_id_configured", "workflow_id", "workflow_id_suffix", "workflow_profile", "template_sha256", "issues")
        }
    return cloud


def _provider_label(package: dict) -> str:
    return "RunningHub" if package.get("generation_mode") == "runninghub_longcat" else "阿里云"


def _aspect_from_dimensions(width: int, height: int) -> str:
    if width <= 0 or height <= 0:
        return "portrait"
    if abs((width / height) - 1.0) <= 0.03:
        return "square"
    return "portrait" if height > width else "landscape"


def _target_aspect(package: dict) -> tuple[str, dict[str, int | str]]:
    settings = package.get("settings") if isinstance(package.get("settings"), dict) else {}
    width, height = int(settings.get("width") or 1080), int(settings.get("height") or 1920)
    aspect = _aspect_from_dimensions(width, height)
    return aspect, {"label": ASPECT_PROFILES[aspect]["label"], "width": width, "height": height}


def _provider_target(package: dict) -> tuple[str, dict[str, int | str]]:
    if package.get("generation_mode") == "runninghub_longcat":
        return "runninghub_4x5", copy.deepcopy(RUNNINGHUB_EXACT_CLOCK_INPUT)
    return _target_aspect(package)


def _sync_project_render_profile(project_dir: Path, aspect: str) -> None:
    """Make an explicit avatar canvas choice the project's actual delivery canvas."""
    marker = project_dir / "project.json"
    try:
        project = json.loads(marker.read_text(encoding="utf-8")) if marker.is_file() else {}
    except (OSError, json.JSONDecodeError):
        project = {}
    if not isinstance(project, dict):
        raise AvatarCloudError("项目配置文件无效，无法更新数字人输出画幅")
    profile = ASPECT_PROFILES[aspect]
    current = project.get("render_profile") if isinstance(project.get("render_profile"), dict) else {}
    project["render_profile"] = {
        **current,
        "aspect_ratio": aspect,
        "width": int(profile["width"]),
        "height": int(profile["height"]),
    }
    intake = project.get("intake") if isinstance(project.get("intake"), dict) else {}
    intake.update({"aspect": aspect, "aspect_label": str(profile["label"])})
    project["intake"] = intake
    temporary = marker.with_suffix(".json.avatar-spec.tmp")
    try:
        temporary.write_text(json.dumps(project, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, marker)
    finally:
        try:
            temporary.unlink()
        except OSError:
            pass


def _source_ratio(source: dict) -> float:
    media = source.get("media") if isinstance(source.get("media"), dict) else {}
    width, height = int(media.get("width") or 0), int(media.get("height") or 0)
    if width <= 0 or height <= 0:
        raise AvatarCloudError("无法读取出镜图尺寸，请重新上传图片")
    return width / height


def _aspect_fit_record(package: dict, source: dict, *, fit_mode: str | None = None, provider_input: dict | None = None) -> dict:
    aspect, target = _provider_target(package)
    source_ratio = _source_ratio(source)
    target_ratio = int(target["width"]) / int(target["height"])
    difference = abs(source_ratio - target_ratio) / target_ratio * 100
    if difference <= ASPECT_MATCH_TOLERANCE_PERCENT:
        return {
            "status": "matched", "target_aspect": aspect, "target_label": str(target["label"]),
            "source_ratio": round(source_ratio, 6), "target_ratio": round(target_ratio, 6),
            "difference_percent": round(difference, 2), "fit_mode": "native",
            "message": "出镜图与供应商输入画幅一致。保存输出规格后仍会冻结一份精确尺寸的上传图。",
        }
    if provider_input and fit_mode in ASPECT_FIT_MODES:
        labels = {"cover_crop": "居中裁切填满画幅", "contain_blur": "保留全图并用模糊背景补边"}
        return {
            "status": "prepared", "target_aspect": aspect, "target_label": str(target["label"]),
            "source_ratio": round(source_ratio, 6), "target_ratio": round(target_ratio, 6),
            "difference_percent": round(difference, 2), "fit_mode": fit_mode,
            "provider_input": copy.deepcopy(provider_input), "prepared_at": _now(),
            "message": f"已生成 {labels[fit_mode]} 的云端输入图；提交前可在此预览核对。",
        }
    return {
        "status": "needs_choice", "target_aspect": aspect, "target_label": str(target["label"]),
        "source_ratio": round(source_ratio, 6), "target_ratio": round(target_ratio, 6),
        "difference_percent": round(difference, 2), "fit_mode": "native",
        "message": "出镜图与项目画幅不一致。请先选择裁切或补边并生成输入图预览，系统不会提交付费任务。",
    }


def _binding_template(speaker: dict, *, role: dict | None = None, presenter_shot: dict | None = None) -> dict:
    binding: dict[str, Any] = {
        "speaker_id": str(speaker["speaker_id"]).lower(),
        "name": str(speaker.get("name") or speaker["speaker_id"]),
        "status": "not_ready",
        "sample": {"status": "not_started", "turn_id": None, "input_hash": None, "approved": False},
        "updated_at": _now(),
    }
    if role:
        binding["role"] = copy.deepcopy(role)
    if presenter_shot:
        binding["presenter_shot"] = copy.deepcopy(presenter_shot)
    return binding


def _ensure_speaker_bindings(package: dict) -> list[dict]:
    """Backfill safe bindings without guessing a multi-speaker assignment.

    Older packages stored one global role and one global presenter image.  They
    can be migrated automatically only for a one-speaker script.  For a dialog,
    we deliberately create blank bindings instead of assigning one character to
    every person silently.
    """
    speakers = [item for item in package.get("speakers", []) if isinstance(item, dict)]
    raw = package.get("speaker_bindings")
    current = {
        str(item.get("speaker_id") or "").lower(): item
        for item in raw if isinstance(item, dict) and item.get("speaker_id")
    } if isinstance(raw, list) else {}
    legacy_single = len(speakers) == 1
    bindings: list[dict] = []
    for speaker in speakers:
        speaker_id = str(speaker.get("speaker_id") or "").lower()
        binding = current.get(speaker_id)
        if not binding:
            binding = _binding_template(
                speaker,
                role=package.get("role") if legacy_single and isinstance(package.get("role"), dict) else None,
                presenter_shot=package.get("presenter_shot") if legacy_single and isinstance(package.get("presenter_shot"), dict) else None,
            )
        binding["speaker_id"] = speaker_id
        binding["name"] = str(speaker.get("name") or speaker_id)
        sample = binding.get("sample") if isinstance(binding.get("sample"), dict) else {}
        binding["sample"] = {
            "status": str(sample.get("status") or "not_started"),
            "turn_id": sample.get("turn_id"),
            "input_hash": sample.get("input_hash"),
            "approved": bool(sample.get("approved")),
            **({"requested_at": sample["requested_at"]} if sample.get("requested_at") else {}),
            **({"approved_at": sample["approved_at"]} if sample.get("approved_at") else {}),
            **({"error": str(sample["error"])} if sample.get("error") else {}),
        }
        bindings.append(binding)
    package["speaker_bindings"] = bindings
    _refresh_binding_statuses(package)
    return bindings


def _binding(package: dict, speaker_id: str) -> dict:
    for item in _ensure_speaker_bindings(package):
        if item.get("speaker_id") == speaker_id.lower():
            return item
    raise AvatarCloudError(f"脚本中不存在说话人：{speaker_id}")


def _binding_for_turn(package: dict, turn: dict) -> dict:
    return _binding(package, str(turn.get("speaker_id") or ""))


def _set_cloud_message(package: dict, status: str, message: str) -> None:
    cloud = _cloud(package)
    changed = cloud.get("status") != status or cloud.get("message") != message
    cloud["status"] = status
    cloud["message"] = message
    if changed:
        cloud["updated_at"] = _now()


def _refresh_binding_statuses(package: dict) -> None:
    for binding in package.get("speaker_bindings", []):
        sample = binding.get("sample") or {}
        if sample.get("approved"):
            binding["status"] = "approved"
        elif sample.get("status") == "awaiting_approval":
            binding["status"] = "awaiting_sample_approval"
        elif sample.get("status") in {"queued", "generating"}:
            binding["status"] = "sample_generating"
        elif sample.get("status") == "failed":
            binding["status"] = "failed"
        elif binding.get("presenter_shot"):
            binding["status"] = "ready"
        else:
            binding["status"] = "not_ready"


def _image_metadata(path: Path) -> dict[str, Any]:
    try:
        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            width, height = image.size
            if width < 400 or height < 400:
                raise AvatarCloudError("项目出镜图至少需要 400×400 像素")
            if width > 7000 or height > 7000:
                raise AvatarCloudError("项目出镜图最长边不能超过 7000 像素")
            return {"width": int(width), "height": int(height), "format": str(image.format or "").upper()}
    except (UnidentifiedImageError, OSError) as exc:
        raise AvatarCloudError("项目出镜图不是可读取的图片") from exc


def _role_reference(role: dict) -> dict:
    return {
        "role_id": str(role["role_id"]),
        "name": str(role["name"]),
        "version": int(role["version"]),
        "reference_count": len(role.get("references") or []),
        "selected_at": _now(),
    }


def _reset_sample(binding: dict) -> None:
    binding["sample"] = {"status": "not_started", "turn_id": None, "input_hash": None, "approved": False}


def _invalidate_binding(package: dict, speaker_id: str, reason: str) -> None:
    binding = _binding(package, speaker_id)
    _reset_sample(binding)
    for turn in package.get("turns", []):
        if turn.get("speaker_id") != speaker_id:
            continue
        job = turn.get("cloud_job") if isinstance(turn.get("cloud_job"), dict) else None
        if job and job.get("status") in {"queued", "uploading", "detecting"}:
            job.update({"status": "cancelled", "stage": "输入已变更，等待重新排队", "finished_at": _now(), "error": reason})
            turn["status"] = "audio_ready" if turn.get("driving_audio") else "missing"


def _invalidate_all_cloud_inputs(package: dict, reason: str) -> None:
    """Cancel only unsent work after a project-level render-contract change.

    Submitted provider jobs are intentionally left untouched: cancelling them
    cannot reliably prevent billing, and their immutable snapshots remain an
    auditable record.  They are simply no longer treated as current inputs.
    """
    for binding in _ensure_speaker_bindings(package):
        _reset_sample(binding)
    for turn in package.get("turns", []):
        job = turn.get("cloud_job") if isinstance(turn.get("cloud_job"), dict) else None
        if job and job.get("status") in {"queued", "uploading", "detecting"}:
            job.update({"status": "cancelled", "stage": "输出规格已变更，等待重新排队", "finished_at": _now(), "error": reason})
            turn["status"] = "audio_ready" if turn.get("driving_audio") else "missing"


def _render_presenter_input(source_path: Path, target_path: Path, *, width: int, height: int, fit_mode: str) -> None:
    """Materialise the exact project-local image that DashScope will receive."""
    if fit_mode not in ASPECT_FIT_MODES:
        raise AvatarCloudError("出镜图适配方式只能是居中裁切或模糊补边")
    try:
        with Image.open(source_path) as loaded:
            image = ImageOps.exif_transpose(loaded).convert("RGB")
    except (UnidentifiedImageError, OSError) as exc:
        raise AvatarCloudError("无法读取出镜图，不能生成云端输入预览") from exc
    source_width, source_height = image.size
    if source_width <= 0 or source_height <= 0:
        raise AvatarCloudError("出镜图尺寸无效")
    if fit_mode == "cover_crop":
        scale = max(width / source_width, height / source_height)
        resized = image.resize((round(source_width * scale), round(source_height * scale)), Image.Resampling.LANCZOS)
        left = max(0, (resized.width - width) // 2)
        top = max(0, (resized.height - height) // 2)
        output = resized.crop((left, top, left + width, top + height))
    else:
        background_scale = max(width / source_width, height / source_height)
        background = image.resize((round(source_width * background_scale), round(source_height * background_scale)), Image.Resampling.LANCZOS)
        left = max(0, (background.width - width) // 2)
        top = max(0, (background.height - height) // 2)
        background = background.crop((left, top, left + width, top + height)).filter(ImageFilter.GaussianBlur(radius=max(12, min(width, height) // 35)))
        foreground_scale = min(width / source_width, height / source_height)
        foreground = image.resize((max(1, round(source_width * foreground_scale)), max(1, round(source_height * foreground_scale))), Image.Resampling.LANCZOS)
        output = background
        output.paste(foreground, ((width - foreground.width) // 2, (height - foreground.height) // 2))
    target_path.parent.mkdir(parents=True, exist_ok=True)
    output.save(target_path, format="PNG", optimize=True)


def _provider_input_record(project_dir: Path, package: dict, binding: dict, *, fit_mode: str) -> dict:
    source = binding.get("presenter_shot")
    if not isinstance(source, dict):
        raise AvatarCloudError(f"请先为“{binding['name']}”上传实际单人出镜图")
    aspect, target = _provider_target(package)
    source_path = _safe_project_file(project_dir, str(source.get("path") or ""))
    if not source_path.is_file():
        raise AvatarCloudError(f"{binding['name']} 的出镜图文件不存在，请重新上传")
    digest = str(source.get("sha256") or _file_sha256(source_path))
    target_path = project_dir / NORMALIZED_PRESENTER_DIRECTORY / str(binding["speaker_id"]) / f"{digest[:12]}_{aspect}_{fit_mode}.png"
    _render_presenter_input(source_path, target_path, width=int(target["width"]), height=int(target["height"]), fit_mode=fit_mode)
    metadata = _image_metadata(target_path)
    return {
        "path": _safe_relative(project_dir, target_path),
        "original_filename": f"云端输入图_{Path(str(source.get('original_filename') or 'presenter')).stem}_{target['label']}_{fit_mode}.png",
        "sha256": _file_sha256(target_path), "size_bytes": target_path.stat().st_size,
        "uploaded_at": _now(), "media": metadata,
    }


def configure_cloud_render_spec(project_dir: Path, payload: dict) -> dict:
    """Confirm the project avatar output contract and prepare reviewable inputs.

    This is local, free preprocessing only.  A caller may change the project
    canvas deliberately; the action invalidates unsent samples so no old
    aspect contract can slip into a later batch.
    """
    package = _package(project_dir)
    cloud = _cloud(package)
    requested_aspect = str(payload.get("aspect_ratio") or cloud.get("aspect_ratio") or "portrait")
    resolution = str(payload.get("resolution") or cloud.get("resolution") or "480P")
    if package.get("generation_mode") == "runninghub_longcat":
        if requested_aspect != "portrait":
            raise AvatarCloudError("当前 RunningHub InfiniteTalk 精确帧工作流只接入竖版项目画布")
        resolution = RUNNINGHUB_EXACT_CLOCK_RESOLUTION
    if requested_aspect not in ASPECT_PROFILES:
        raise AvatarCloudError("数字人画幅只能选择竖版 9:16、横版 16:9 或方形 1:1")
    allowed_resolutions = {RUNNINGHUB_EXACT_CLOCK_RESOLUTION} if package.get("generation_mode") == "runninghub_longcat" else {"480P", "720P"}
    if resolution not in allowed_resolutions:
        raise AvatarCloudError("当前数字人提供商不支持所选清晰度")
    default_fit_mode = str(payload.get("default_fit_mode") or cloud.get("input_fit_mode") or "cover_crop")
    if default_fit_mode not in ASPECT_FIT_MODES:
        raise AvatarCloudError("出镜图默认适配方式只能选择居中裁切或模糊补边")
    if any(_is_cloud_turn_active(turn) for turn in package.get("turns", [])):
        raise AvatarCloudError("已有云端数字人任务正在执行，不能修改画幅或清晰度；请等待任务完成后再新建试片")
    profile = ASPECT_PROFILES[requested_aspect]
    settings = package["settings"]
    changed = (
        int(settings.get("width") or 0) != int(profile["width"])
        or int(settings.get("height") or 0) != int(profile["height"])
        or cloud.get("resolution") != resolution
    )
    settings["width"], settings["height"] = int(profile["width"]), int(profile["height"])
    cloud["aspect_ratio"], cloud["resolution"] = requested_aspect, resolution
    if package.get("generation_mode") == "runninghub_longcat":
        cloud["model"] = RUNNINGHUB_EXACT_CLOCK_MODEL
    _sync_project_render_profile(project_dir, requested_aspect)
    fit_modes = payload.get("fit_modes") if isinstance(payload.get("fit_modes"), dict) else {}
    inputs_changed = False
    for binding in _ensure_speaker_bindings(package):
        source = binding.get("presenter_shot")
        if not isinstance(source, dict):
            binding.pop("aspect_fit", None)
            continue
        previous_fit = copy.deepcopy(binding.get("aspect_fit")) if isinstance(binding.get("aspect_fit"), dict) else None
        selected_mode = str(fit_modes.get(binding["speaker_id"]) or default_fit_mode)
        baseline = _aspect_fit_record(package, source)
        if baseline["status"] == "matched" and package.get("generation_mode") == "runninghub_longcat":
            provider_input = _provider_input_record(project_dir, package, binding, fit_mode="cover_crop")
            binding["aspect_fit"] = _aspect_fit_record(package, source, fit_mode="cover_crop", provider_input=provider_input)
        elif baseline["status"] == "matched":
            binding["aspect_fit"] = baseline
        elif selected_mode in ASPECT_FIT_MODES:
            provider_input = _provider_input_record(project_dir, package, binding, fit_mode=selected_mode)
            binding["aspect_fit"] = _aspect_fit_record(package, source, fit_mode=selected_mode, provider_input=provider_input)
        else:
            binding["aspect_fit"] = baseline
        current_fit = binding.get("aspect_fit") if isinstance(binding.get("aspect_fit"), dict) else {}
        previous_input = (previous_fit or {}).get("provider_input") if isinstance(previous_fit, dict) else {}
        current_input = current_fit.get("provider_input") if isinstance(current_fit, dict) else {}
        if (
            (previous_fit or {}).get("fit_mode") != current_fit.get("fit_mode")
            or (previous_input or {}).get("sha256") != (current_input or {}).get("sha256")
        ):
            inputs_changed = True
    cloud["input_fit_mode"] = default_fit_mode
    if changed or inputs_changed:
        cloud["render_spec_revision"] = int(cloud.get("render_spec_revision") or 1) + 1
        _invalidate_all_cloud_inputs(package, "项目数字人输出规格已变更，请用新画幅重新生成试片")
    _refresh_readiness(package)
    return _save_package(project_dir, package)


def prepare_presenter_upload(project_dir: Path, speaker_id: str, original_filename: str) -> tuple[Path, Path]:
    package = _package(project_dir)
    _binding(package, speaker_id)
    filename = Path(original_filename).name
    extension = Path(filename).suffix.lower()
    if extension not in IMAGE_EXTENSIONS:
        raise AvatarCloudError("项目出镜图仅支持 PNG、JPG、JPEG 或 WEBP")
    directory = project_dir / PRESENTER_DIRECTORY / speaker_id.lower()
    directory.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=".presenter-upload-", suffix=extension, dir=directory)
    os.close(handle)
    return Path(temporary), directory / f"presenter{extension}"


def finalize_presenter_upload(project_dir: Path, speaker_id: str, temporary: Path, target: Path, original_filename: str) -> dict:
    package = _package(project_dir)
    binding = _binding(package, speaker_id)
    if not temporary.is_file() or temporary.stat().st_size <= 0:
        raise AvatarCloudError("项目出镜图文件为空")
    if temporary.stat().st_size > MAX_PRESENTER_IMAGE_BYTES:
        raise AvatarCloudError("项目出镜图不能超过 25MB")
    metadata = _image_metadata(temporary)
    digest = _file_sha256(temporary)
    target = target.parent / f"presenter_{digest[:12]}{temporary.suffix.lower()}"
    target.parent.mkdir(parents=True, exist_ok=True)
    os.replace(temporary, target)
    previous = binding.get("presenter_shot") if isinstance(binding.get("presenter_shot"), dict) else None
    binding["presenter_shot"] = {
        "path": _safe_relative(project_dir, target),
        "original_filename": Path(original_filename).name,
        "sha256": digest,
        "size_bytes": target.stat().st_size,
        "uploaded_at": _now(),
        "media": metadata,
    }
    binding["aspect_fit"] = _aspect_fit_record(package, binding["presenter_shot"])
    if not previous or previous.get("sha256") != digest:
        _invalidate_binding(package, speaker_id.lower(), "已替换项目出镜图")
    _refresh_readiness(package)
    return _save_package(project_dir, package)


def select_cloud_avatar_role(project_dir: Path, speaker_id: str, role_id: str) -> dict:
    package = _package(project_dir)
    binding = _binding(package, speaker_id)
    if not str(role_id or "").strip():
        binding.pop("role", None)
        _refresh_readiness(package)
        return _save_package(project_dir, package)
    try:
        role = get_avatar_role(role_id)
    except AvatarRoleError as exc:
        raise AvatarCloudError(str(exc)) from exc
    binding["role"] = _role_reference(role)
    # A reusable role is identity/provenance metadata.  It is not sent to
    # DashScope and therefore must not invalidate an approved sample or a
    # generated clip whose actual presenter image did not change.
    _refresh_readiness(package)
    return _save_package(project_dir, package)


def _probe_audio(path: Path) -> dict[str, Any]:
    ffprobe = _find_binary("ffprobe")
    if not ffprobe:
        raise AvatarCloudError("未发现 ffprobe，无法检查驱动音频")
    result = _run([
        ffprobe, "-v", "error", "-show_entries", "format=duration,size:stream=codec_type,codec_name,sample_rate,channels",
        "-of", "json", str(path),
    ])
    if result.returncode != 0:
        raise AvatarCloudError("无法读取驱动音频，请上传 WAV、MP3 或可转换的音频文件")
    import json
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise AvatarCloudError("音频探测返回了无效数据") from exc
    audio = next((item for item in value.get("streams", []) if item.get("codec_type") == "audio"), None)
    duration = float((value.get("format") or {}).get("duration") or 0)
    if not audio or duration <= 0:
        raise AvatarCloudError("驱动音频必须包含可读取的声音和有效时长")
    return {
        "duration_seconds": round(duration, 3),
        "codec": str(audio.get("codec_name") or "") or None,
        "sample_rate": int(audio.get("sample_rate") or 0),
        "channels": int(audio.get("channels") or 0),
    }


def prepare_driving_audio_upload(project_dir: Path, turn_id: str, original_filename: str) -> tuple[Path, Path]:
    package = _package(project_dir)
    turn = _find_turn(package, turn_id.upper())
    filename = Path(original_filename).name
    extension = Path(filename).suffix.lower()
    if extension not in AUDIO_EXTENSIONS:
        raise AvatarCloudError("驱动音频支持 WAV、MP3、M4A、AAC、FLAC 或 OGG")
    directory = project_dir / DRIVING_AUDIO_DIRECTORY / str(turn["speaker_id"])
    directory.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=".driving-audio-upload-", suffix=extension, dir=directory)
    os.close(handle)
    return Path(temporary), directory / f"{turn['turn_id']}{extension}"


def _normalise_audio_for_provider(
    project_dir: Path,
    source: Path,
    turn_id: str,
    digest: str,
    *,
    exact_clock: bool = False,
) -> Path:
    if not exact_clock:
        if source.suffix.lower() in {".mp3", ".wav"}:
            return source
        ffmpeg = _find_binary("ffmpeg")
        if not ffmpeg:
            raise AvatarCloudError("该音频需要转为 MP3，但本机未发现 ffmpeg")
        directory = project_dir / NORMALIZED_AUDIO_DIRECTORY
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"{turn_id}_{digest[:12]}.mp3"
        result = _run([
            ffmpeg, "-y", "-i", str(source), "-vn", "-ar", "48000", "-ac", "1",
            "-b:a", "192k", str(target),
        ])
        if result.returncode != 0 or not target.is_file() or target.stat().st_size <= 0:
            try:
                target.unlink()
            except OSError:
                pass
            raise AvatarCloudError("驱动音频转码为 MP3 失败")
        return target
    ffmpeg = _find_binary("ffmpeg")
    if not ffmpeg:
        raise AvatarCloudError("驱动音频需要转为精确帧 PCM WAV，但本机未发现 ffmpeg")
    directory = project_dir / NORMALIZED_AUDIO_DIRECTORY
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{turn_id}_{digest[:12]}_25fps.wav"
    result = _run([
        ffmpeg, "-y", "-i", str(source), "-vn", "-ar", "24000", "-ac", "1",
        "-c:a", "pcm_s16le", str(target),
    ])
    if result.returncode != 0 or not target.is_file() or target.stat().st_size <= 0:
        try:
            target.unlink()
        except OSError:
            pass
        raise AvatarCloudError("驱动音频转码为 PCM WAV 失败")
    try:
        align_pcm_wav_to_frame_clock(target, fps=AVATAR_VIDEO_FPS)
    except AvatarAudioClockError as exc:
        raise AvatarCloudError(f"驱动音频无法对齐 25FPS：{exc}") from exc
    return target


def _script_text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _safe_voicebox_error(error: object) -> str:
    """Return a user-facing Voicebox failure without leaking local secrets."""
    message = str(error or "Haike Video 本地配音未返回可试听的音频文件")
    for variable in ("VOICEBOX_API_KEY", "OPENAI_API_KEY", "DOUBAO_SPEECH_API_KEY"):
        secret = str(os.environ.get(variable) or "")
        if secret:
            message = message.replace(secret, "[已隐藏]")
    return message[:1200]


def _assert_audio_replacement_allowed(turn: dict) -> None:
    job = turn.get("cloud_job") if isinstance(turn.get("cloud_job"), dict) else {}
    if job.get("status") in ACTIVE_JOB_STATES:
        raise AvatarCloudError(
            f"{turn['turn_id']} 已提交或正在生成云端数字人。为保证嘴型输入可追溯，"
            "请先等待该任务完成；如需换音频，请随后重新生成这一段。"
        )


def _audio_candidates(turn: dict) -> list[dict]:
    raw = turn.get("driving_audio_candidates")
    if not isinstance(raw, list):
        raw = []
        turn["driving_audio_candidates"] = raw
    return raw


def _driving_audio_record(
    project_dir: Path,
    turn: dict,
    source: Path,
    original_filename: str,
    *,
    candidate_id: str,
    state: str,
    source_type: str,
    profile: dict | None = None,
) -> dict:
    """Probe a local take and turn it into one immutable audio-version record."""
    if not source.is_file() or source.stat().st_size <= 0:
        raise AvatarCloudError("驱动音频文件为空")
    if source.stat().st_size > MAX_DRIVING_AUDIO_BYTES:
        raise AvatarCloudError("单段驱动音频不能超过 15MB")
    metadata = _probe_audio(source)
    if metadata["duration_seconds"] > MAX_DRIVING_AUDIO_SECONDS:
        raise AvatarCloudError("单段驱动音频不能超过 20 秒，请先在脚本中拆分该段")
    digest = _file_sha256(source)
    provider_path = _normalise_audio_for_provider(
        project_dir,
        source,
        str(turn["turn_id"]),
        digest,
        exact_clock=_package(project_dir).get("generation_mode") == "runninghub_longcat",
    )
    record: dict[str, Any] = {
        "id": candidate_id,
        "state": state,
        "source_type": source_type,
        "path": _safe_relative(project_dir, source),
        "provider_path": _safe_relative(project_dir, provider_path),
        "original_filename": Path(original_filename).name,
        "sha256": digest,
        "size_bytes": source.stat().st_size,
        "duration_seconds": metadata["duration_seconds"],
        "codec": metadata["codec"],
        "uploaded_at": _now(),
        "created_at": _now(),
    }
    if profile:
        record.update({
            "profile_id": str(profile["id"]),
            "profile_name": str(profile.get("name") or profile["id"]),
            "voice_provider_id": str(profile.get("provider_id") or "voicebox_tts"),
            "voice_provider_name": str(profile.get("provider_name") or "Haike Video 本地配音"),
            "script_text_sha256": _script_text_hash(str(turn["text"])),
        })
        selection_source = str(profile.get("selection_source") or "").strip()
        if selection_source in {"manual", "same_name", "default"}:
            record["voice_selection_source"] = selection_source
        batch_id = str(profile.get("batch_id") or "").strip()
        if batch_id:
            record["batch_id"] = batch_id
    return record


def _invalidate_turn_outputs_for_new_audio(package: dict, turn: dict) -> None:
    """Invalidate only the selected turn's cloud result and sample approval."""
    _assert_audio_replacement_allowed(turn)
    turn.pop("source", None)
    turn.pop("cloud_job", None)
    turn.pop("binding_snapshot", None)
    binding = _binding_for_turn(package, turn)
    if str((binding.get("sample") or {}).get("turn_id") or "") == str(turn["turn_id"]):
        _reset_sample(binding)
    turn["status"] = "audio_ready"
    package["validation"] = {"status": "pending", "issues": [], "summary": {}}
    package["asr"] = {"status": "not_started", "issues": [], "summary": {}}
    package["assembly"] = {"status": "not_started", "issues": [], "summary": {}}


def _adopt_driving_audio(package: dict, turn: dict, record: dict) -> None:
    """Promote one auditioned take to the sole provider-facing audio input."""
    _invalidate_turn_outputs_for_new_audio(package, turn)
    candidates = _audio_candidates(turn)
    for item in candidates:
        if item.get("state") == "current":
            item["state"] = "superseded"
    existing = next((item for item in candidates if item.get("id") == record.get("id")), None)
    promoted = copy.deepcopy(record)
    promoted["state"] = "current"
    promoted["applied_at"] = _now()
    if existing is None:
        candidates.append(promoted)
    else:
        existing.clear()
        existing.update(promoted)
    turn["driving_audio"] = copy.deepcopy(promoted)
    _refresh_readiness(package)


def finalize_driving_audio_upload(project_dir: Path, turn_id: str, temporary: Path, target: Path, original_filename: str) -> dict:
    package = _package(project_dir)
    turn = _find_turn(package, turn_id.upper())
    _assert_audio_replacement_allowed(turn)
    if not temporary.is_file() or temporary.stat().st_size <= 0:
        raise AvatarCloudError("驱动音频文件为空")
    if temporary.stat().st_size > MAX_DRIVING_AUDIO_BYTES:
        raise AvatarCloudError("单段驱动音频不能超过 15MB")
    # Validate before moving anything into the canonical project asset path, so
    # a broken replacement cannot displace the currently adopted take.
    metadata = _probe_audio(temporary)
    if metadata["duration_seconds"] > MAX_DRIVING_AUDIO_SECONDS:
        raise AvatarCloudError("单段驱动音频不能超过 20 秒，请先在脚本中拆分该段")
    digest = _file_sha256(temporary)
    target = target.parent / f"{turn['turn_id']}_{digest[:12]}{temporary.suffix.lower()}"
    target.parent.mkdir(parents=True, exist_ok=True)
    os.replace(temporary, target)
    record = _driving_audio_record(
        project_dir,
        turn,
        target,
        original_filename,
        candidate_id=f"AVAC-{uuid4().hex[:16]}",
        state="current",
        source_type="uploaded",
    )
    _adopt_driving_audio(package, turn, record)
    return _save_package(project_dir, package)


def _normalise_voice_name(value: object) -> str:
    """Compare only strict human-facing Voicebox names, never fuzzy aliases."""
    return unicodedata.normalize("NFKC", str(value or "")).strip().casefold()


def _voicebox_catalog() -> tuple[dict, list[dict], dict[str, dict]]:
    catalog = read_audio_center()
    if str((catalog.get("provider") or {}).get("status") or "") != "available":
        raise AvatarCloudError("Haike Video 本地配音当前不可用，请先完成安装并启动服务")
    profiles = [
        dict(item)
        for item in (catalog.get("profiles") if isinstance(catalog.get("profiles"), list) else [])
        if isinstance(item, dict) and str(item.get("id") or "").strip() and str(item.get("name") or "").strip()
    ]
    by_id = {str(item["id"]): item for item in profiles}
    default = catalog.get("default_voice") if isinstance(catalog.get("default_voice"), dict) else None
    if default and str(default.get("id") or "") and str(default["id"]) not in by_id:
        # Some Voicebox deployments expose the selected default separately from
        # the list.  It is still a valid explicit fallback, but never a name match.
        by_id[str(default["id"])] = dict(default)
    return catalog, profiles, by_id


def _voicebox_configuration(package: dict) -> dict:
    configuration = package.get("voicebox")
    if not isinstance(configuration, dict):
        configuration = {}
        package["voicebox"] = configuration
    if not isinstance(configuration.get("speaker_mappings"), list):
        configuration["speaker_mappings"] = []
    if "batch" not in configuration or not isinstance(configuration.get("batch"), dict):
        configuration["batch"] = None
    return configuration


def _voicebox_mapping_for_speaker(package: dict, speaker_id: str) -> dict | None:
    configuration = _voicebox_configuration(package)
    return next(
        (item for item in configuration["speaker_mappings"] if isinstance(item, dict) and item.get("speaker_id") == speaker_id.lower()),
        None,
    )


def _reconcile_voicebox_speaker_mappings(package: dict, catalog: dict, profiles: list[dict], by_id: dict[str, dict]) -> list[dict]:
    """Refresh automatic routes while preserving a valid user-owned override.

    An exact-name collision is intentionally not resolved by recency, id order,
    or a default.  The user must choose once, then that manual assignment is
    retained for future batches in this project.
    """
    configuration = _voicebox_configuration(package)
    existing = {
        str(item.get("speaker_id") or "").lower(): item
        for item in configuration["speaker_mappings"]
        if isinstance(item, dict) and item.get("speaker_id")
    }
    default = catalog.get("default_voice") if isinstance(catalog.get("default_voice"), dict) else None
    default_profile = by_id.get(str((default or {}).get("id") or ""))
    mappings: list[dict] = []
    for speaker in package.get("speakers", []):
        if not isinstance(speaker, dict):
            continue
        speaker_id = str(speaker.get("speaker_id") or "").lower()
        speaker_name = str(speaker.get("name") or speaker_id)
        previous = existing.get(speaker_id) or {}
        if previous.get("selection_source") == "manual":
            requested = str(previous.get("profile_id") or "")
            selected = by_id.get(requested)
            if selected:
                mapping = {
                    "speaker_id": speaker_id,
                    "speaker_name": speaker_name,
                    "profile_id": str(selected["id"]),
                    "profile_name": str(selected.get("name") or selected["id"]),
                    "selection_source": "manual",
                    "status": "ready",
                    "detail": "已按本项目的手动指定音色配音",
                    "updated_at": _now(),
                }
            else:
                mapping = {
                    "speaker_id": speaker_id,
                    "speaker_name": speaker_name,
                    "profile_id": str(previous.get("profile_id") or "") or None,
                    "profile_name": previous.get("profile_name") or None,
                    "selection_source": "manual",
                    "status": "needs_attention",
                    "detail": "此前手动指定的本地音色已不存在，请重新选择；系统不会静默改用默认音色",
                    "updated_at": _now(),
                }
            mappings.append(mapping)
            continue

        exact_matches = [item for item in profiles if _normalise_voice_name(item.get("name")) == _normalise_voice_name(speaker_name)]
        if len(exact_matches) == 1:
            selected = exact_matches[0]
            mapping = {
                "speaker_id": speaker_id,
                "speaker_name": speaker_name,
                "profile_id": str(selected["id"]),
                "profile_name": str(selected.get("name") or selected["id"]),
                "selection_source": "same_name",
                "status": "ready",
                "detail": "已按脚本说话人与本地音色同名精确匹配",
                "updated_at": _now(),
            }
        elif len(exact_matches) > 1:
            mapping = {
                "speaker_id": speaker_id,
                "speaker_name": speaker_name,
                "profile_id": None,
                "profile_name": None,
                "selection_source": "ambiguous",
                "status": "needs_attention",
                "detail": f"发现 {len(exact_matches)} 个同名本地音色，请手动指定，避免选错克隆音色",
                "updated_at": _now(),
            }
        elif default_profile:
            mapping = {
                "speaker_id": speaker_id,
                "speaker_name": speaker_name,
                "profile_id": str(default_profile["id"]),
                "profile_name": str(default_profile.get("name") or default_profile["id"]),
                "selection_source": "default",
                "status": "ready",
                "detail": "未发现同名本地音色，已按通用默认音色兜底",
                "updated_at": _now(),
            }
        else:
            mapping = {
                "speaker_id": speaker_id,
                "speaker_name": speaker_name,
                "profile_id": None,
                "profile_name": None,
                "selection_source": "unavailable",
                "status": "unavailable",
                "detail": "通用配音中心没有可用音色，请先启动 Haike Video 本地配音或迁移音色",
                "updated_at": _now(),
            }
        mappings.append(mapping)
    configuration["speaker_mappings"] = mappings
    return mappings


def refresh_voicebox_speaker_mappings(project_dir: Path) -> dict:
    package = _package(project_dir)
    catalog, profiles, by_id = _voicebox_catalog()
    _reconcile_voicebox_speaker_mappings(package, catalog, profiles, by_id)
    return _save_package(project_dir, package)


def set_voicebox_speaker_mapping(project_dir: Path, speaker_id: str, payload: dict) -> dict:
    """Persist a deliberate project override, or return a speaker to auto-routing."""
    package = _package(project_dir)
    configuration = _voicebox_configuration(package)
    batch = configuration.get("batch")
    if isinstance(batch, dict) and batch.get("status") in {"queued", "running"}:
        raise AvatarCloudError("批量配音正在执行，暂时不能改变说话人音色映射；请等待当前队列结束")
    speaker = next((item for item in package.get("speakers", []) if str(item.get("speaker_id") or "").lower() == speaker_id.lower()), None)
    if not isinstance(speaker, dict):
        raise AvatarCloudError(f"脚本中不存在说话人：{speaker_id}")
    catalog, profiles, by_id = _voicebox_catalog()
    requested = str(payload.get("profile_id") or "").strip()
    existing = _voicebox_mapping_for_speaker(package, speaker_id)
    if not requested:
        if existing:
            configuration["speaker_mappings"] = [item for item in configuration["speaker_mappings"] if item is not existing]
        _reconcile_voicebox_speaker_mappings(package, catalog, profiles, by_id)
        return _save_package(project_dir, package)
    selected = by_id.get(requested)
    if not selected:
        raise AvatarCloudError("所选本地音色不存在或已不可用，请刷新通用配音中心后重试")
    manual = {
        "speaker_id": str(speaker["speaker_id"]).lower(),
        "speaker_name": str(speaker.get("name") or speaker["speaker_id"]),
        "profile_id": str(selected["id"]),
        "profile_name": str(selected.get("name") or selected["id"]),
        "selection_source": "manual",
        "status": "ready",
        "detail": "已按本项目的手动指定音色配音",
        "updated_at": _now(),
    }
    mappings = [item for item in configuration["speaker_mappings"] if item.get("speaker_id") != manual["speaker_id"]]
    mappings.append(manual)
    configuration["speaker_mappings"] = mappings
    _reconcile_voicebox_speaker_mappings(package, catalog, profiles, by_id)
    return _save_package(project_dir, package)


def _select_voicebox_profile(package: dict, turn: dict, payload: dict) -> tuple[dict, str]:
    catalog, profiles, by_id = _voicebox_catalog()
    _reconcile_voicebox_speaker_mappings(package, catalog, profiles, by_id)
    requested_profile_id = str(payload.get("profile_id") or "").strip()
    if requested_profile_id:
        selected = by_id.get(requested_profile_id)
        if not selected:
            raise AvatarCloudError("所选本地音色不存在或已不可用，请刷新通用配音中心后重试")
        requested_source = str(payload.get("selection_source") or "manual")
        return selected, requested_source if requested_source in {"manual", "same_name", "default"} else "manual"
    mapping = _voicebox_mapping_for_speaker(package, str(turn.get("speaker_id") or ""))
    if not mapping or mapping.get("status") != "ready" or not mapping.get("profile_id"):
        detail = str((mapping or {}).get("detail") or "请先指定一个可用音色")
        raise AvatarCloudError(f"{turn['turn_id']} 无法自动选择本地音色：{detail}")
    selected = by_id.get(str(mapping["profile_id"]))
    if not selected:
        raise AvatarCloudError("当前映射的本地音色已不可用，请重新识别或手动指定")
    return selected, str(mapping.get("selection_source") or "default")


def start_voicebox_driving_audio_candidate(project_dir: Path, turn_id: str, payload: dict | None = None) -> dict:
    """Queue one local Voicebox take without changing the cloud-provider input."""
    package = _package(project_dir)
    turn = _find_turn(package, turn_id.upper())
    request = payload or {}
    active_batch = _voicebox_configuration(package).get("batch")
    requested_batch_id = str(request.get("batch_id") or "").strip()
    if (
        isinstance(active_batch, dict)
        and active_batch.get("status") in {"queued", "running"}
        and requested_batch_id != str(active_batch.get("batch_id") or "")
    ):
        raise AvatarCloudError("批量配音正在执行；请等待当前脚本顺序队列结束后，再生成单段候选音频")
    job = turn.get("driving_audio_job") if isinstance(turn.get("driving_audio_job"), dict) else {}
    if job.get("status") == "generating":
        raise AvatarCloudError(f"{turn['turn_id']} 的本地候选音频正在生成，请等待完成后再试")
    profile, selection_source = _select_voicebox_profile(package, turn, request)
    if profile.get("available") is False:
        raise AvatarCloudError(f"{profile.get('provider_name') or '所选配音服务'}当前不可用，请先完成配置")
    text = str(turn.get("text") or "").strip()
    if not text:
        raise AvatarCloudError(f"{turn['turn_id']} 没有可生成配音的脚本文本，请先修正脚本")
    if len(text) > 5000:
        raise AvatarCloudError(f"{turn['turn_id']} 的台词超过 5000 个字符，请先拆分轮次")
    candidate_id = f"AVAC-{uuid4().hex[:16]}"
    turn["driving_audio_job"] = {
        "job_id": f"AVAJ-{uuid4().hex[:16]}",
        "status": "generating",
        "candidate_id": candidate_id,
        "profile_id": str(profile["id"]),
        "profile_name": str(profile.get("name") or profile["id"]),
        "provider_id": str(profile.get("provider_id") or "voicebox_tts"),
        "provider_name": str(profile.get("provider_name") or "Haike Video 本地配音"),
        "voice_selection_source": selection_source,
        "script_text_sha256": _script_text_hash(text),
        "started_at": _now(),
        "error": "",
    }
    batch_id = str(request.get("batch_id") or "").strip()
    if batch_id:
        turn["driving_audio_job"]["batch_id"] = batch_id
    return _save_package(project_dir, package)


def generate_voicebox_driving_audio_candidate(project_dir: Path, turn_id: str) -> dict:
    """Generate the queued local take and retain it as an auditionable version."""
    with VOICEBOX_BATCH_LOCK:
        package = _package(project_dir)
        turn = _find_turn(package, turn_id.upper())
        job = turn.get("driving_audio_job") if isinstance(turn.get("driving_audio_job"), dict) else {}
        if job.get("status") != "generating":
            raise AvatarCloudError(f"{turn['turn_id']} 当前没有待生成的本地驱动音频")
        candidate_id = str(job.get("candidate_id") or "")
        if not candidate_id:
            raise AvatarCloudError(f"{turn['turn_id']} 的本地候选音频缺少版本编号")
        output = project_dir / VOICEBOX_CANDIDATE_DIRECTORY / str(turn["turn_id"]) / "candidates" / f"{candidate_id}.wav"
        output.parent.mkdir(parents=True, exist_ok=True)
        runtime_profile = get_voice_profile(str(job["profile_id"]))
        if not runtime_profile and str(job.get("provider_id") or "voicebox_tts") == "voicebox_tts":
            # Backward-compatible local jobs only persisted a Voicebox profile
            # id.  The local adapter can resolve that id without private cloud
            # configuration, so old projects remain resumable.
            runtime_profile = {
                "id": str(job["profile_id"]),
                "name": str(job.get("profile_name") or job["profile_id"]),
                "provider_id": "voicebox_tts",
            }
        if not runtime_profile or str(runtime_profile.get("provider_id") or "voicebox_tts") != str(job.get("provider_id") or "voicebox_tts"):
            raise AvatarCloudError(f"{turn['turn_id']} 冻结的配音音色配置已变化，请重新发起候选生成")
        result = generate_voice_audio(
            text=str(turn["text"]),
            profile=runtime_profile,
            output_path=output,
            language="zh",
        )

        package = _package(project_dir)
        turn = _find_turn(package, turn_id.upper())
        current_job = turn.get("driving_audio_job") if isinstance(turn.get("driving_audio_job"), dict) else {}
        if not result.success or not output.is_file():
            current_job.update({"status": "failed", "finished_at": _now(), "error": _safe_voicebox_error(result.error)})
            turn["driving_audio_job"] = current_job
            return _save_package(project_dir, package)
        if str(current_job.get("candidate_id") or "") != candidate_id:
            # A stale worker must never overwrite a newer per-turn audition request.
            return package
        profile = {
            "id": current_job["profile_id"],
            "name": current_job["profile_name"],
            "provider_id": current_job.get("provider_id") or "voicebox_tts",
            "provider_name": current_job.get("provider_name") or "Haike Video 本地配音",
            "selection_source": current_job.get("voice_selection_source"),
            "batch_id": current_job.get("batch_id"),
        }
        record = _driving_audio_record(
            project_dir,
            turn,
            output,
            f"Voicebox_{turn['turn_id']}_{candidate_id}.wav",
            candidate_id=candidate_id,
            state="candidate",
            source_type="cloud_tts_generated" if profile["provider_id"] == "doubao" else "voicebox_generated",
            profile=profile,
        )
        _audio_candidates(turn).append(record)
        current_job.update({"status": "completed", "finished_at": _now(), "error": ""})
        turn["driving_audio_job"] = current_job
        return _save_package(project_dir, package)


def mark_voicebox_driving_audio_candidate_failed(project_dir: Path, turn_id: str, error: object) -> dict:
    package = _package(project_dir)
    turn = _find_turn(package, turn_id.upper())
    job = turn.get("driving_audio_job") if isinstance(turn.get("driving_audio_job"), dict) else {}
    job.update({"status": "failed", "finished_at": _now(), "error": _safe_voicebox_error(error)})
    turn["driving_audio_job"] = job
    return _save_package(project_dir, package)


def apply_voicebox_driving_audio_candidate(project_dir: Path, turn_id: str, candidate_id: str) -> dict:
    """Adopt a listened-to Voicebox take as the one immutable cloud input."""
    package = _package(project_dir)
    turn = _find_turn(package, turn_id.upper())
    candidate = next((item for item in _audio_candidates(turn) if item.get("id") == candidate_id), None)
    if not isinstance(candidate, dict) or candidate.get("state") != "candidate":
        raise AvatarCloudError("请先生成并试听一个有效的本地候选音频")
    if candidate.get("source_type") != "voicebox_generated":
        raise AvatarCloudError("只能采用 Haike Video 本地配音生成的候选音频")
    if candidate.get("script_text_sha256") != _script_text_hash(str(turn.get("text") or "")):
        raise AvatarCloudError("该候选音频对应的台词已变化，请按最新脚本重新生成")
    audio_path = _safe_project_file(project_dir, str(candidate.get("path") or ""))
    if not audio_path.is_file():
        raise AvatarCloudError("该候选音频文件已不存在，请重新生成")
    metadata = _probe_audio(audio_path)
    if metadata["duration_seconds"] > MAX_DRIVING_AUDIO_SECONDS:
        raise AvatarCloudError("该候选音频超过 20 秒，请先拆分台词后重新生成")
    candidate["duration_seconds"] = metadata["duration_seconds"]
    candidate["codec"] = metadata["codec"]
    _adopt_driving_audio(package, turn, candidate)
    return _save_package(project_dir, package)


def _voicebox_batch(package: dict) -> dict | None:
    batch = _voicebox_configuration(package).get("batch")
    return batch if isinstance(batch, dict) else None


def _refresh_voicebox_batch_counts(batch: dict) -> None:
    items = batch.get("items") if isinstance(batch.get("items"), list) else []
    batch["completed_count"] = sum(item.get("status") == "completed" for item in items if isinstance(item, dict))
    batch["failed_count"] = sum(item.get("status") == "failed" for item in items if isinstance(item, dict))
    batch["skipped_count"] = sum(item.get("status") == "skipped" for item in items if isinstance(item, dict))


def _is_cloud_turn_active(turn: dict) -> bool:
    job = turn.get("cloud_job") if isinstance(turn.get("cloud_job"), dict) else {}
    return str(job.get("status") or "") in ACTIVE_JOB_STATES


def start_voicebox_driving_audio_batch(project_dir: Path, payload: dict | None = None) -> dict:
    """Plan a durable script-order batch; actual synthesis is executed later by one worker."""
    package = _package(project_dir)
    request = payload or {}
    mode = str(request.get("mode") or "missing_and_apply")
    if mode not in VOICEBOX_BATCH_MODES:
        raise AvatarCloudError("未知的批量配音模式")
    configuration = _voicebox_configuration(package)
    existing_batch = _voicebox_batch(package)
    if existing_batch and existing_batch.get("status") in {"queued", "running"}:
        # A repeated click is a safe resume request, not a second concurrent queue.
        return package
    catalog, profiles, by_id = _voicebox_catalog()
    mappings = _reconcile_voicebox_speaker_mappings(package, catalog, profiles, by_id)
    mapping_by_speaker = {str(item["speaker_id"]): item for item in mappings}
    blockers = [item["speaker_name"] for item in mappings if item.get("status") != "ready"]
    if blockers:
        raise AvatarCloudError(f"请先为以下说话人指定本地音色：{'、'.join(blockers)}")

    items: list[dict] = []
    for turn in sorted((item for item in package.get("turns", []) if isinstance(item, dict)), key=lambda item: int(item.get("index") or 0)):
        mapping = mapping_by_speaker.get(str(turn.get("speaker_id") or ""))
        if not mapping:
            continue
        status = "queued"
        error = ""
        voice_job = turn.get("driving_audio_job") if isinstance(turn.get("driving_audio_job"), dict) else {}
        if _is_cloud_turn_active(turn):
            status, error = "skipped", "该轮已提交或正在生成云端数字人，不能更换其驱动音频"
        elif voice_job.get("status") == "generating":
            status, error = "skipped", "该轮已有单段本地候选音频正在生成"
        elif mode == "missing_and_apply" and isinstance(turn.get("driving_audio"), dict):
            status, error = "skipped", "已有采用的驱动音频，按安全策略不覆盖"
        elif mode == "failed_only_and_apply" and voice_job.get("status") != "failed":
            status, error = "skipped", "该轮没有失败的本地配音任务"
        items.append({
            "turn_id": str(turn["turn_id"]),
            "speaker_id": str(turn["speaker_id"]),
            "profile_id": str(mapping["profile_id"]),
            "profile_name": str(mapping["profile_name"]),
            "selection_source": str(mapping["selection_source"]),
            "script_text_sha256": _script_text_hash(str(turn.get("text") or "")),
            "status": status,
            "created_at": _now(),
            "error": error,
        })
    if not any(item["status"] == "queued" for item in items):
        raise AvatarCloudError("没有符合当前批量模式的轮次；可改选“全部生成候选”或检查失败任务")
    batch = {
        "batch_id": f"AVAB-{uuid4().hex[:16]}",
        "status": "queued",
        "mode": mode,
        "apply_policy": "candidate_only" if mode == "all_candidates" else "auto_adopt",
        "items": items,
        "created_at": _now(),
        "error": "",
    }
    _refresh_voicebox_batch_counts(batch)
    configuration["batch"] = batch
    return _save_package(project_dir, package)


def _batch_item(batch: dict, turn_id: str) -> dict:
    item = next((item for item in batch.get("items", []) if item.get("turn_id") == turn_id), None)
    if not isinstance(item, dict):
        raise AvatarCloudError(f"批量配音记录中缺少轮次：{turn_id}")
    return item


def run_voicebox_driving_audio_batch(project_dir: Path, batch_id: str) -> dict:
    """Run one persistent batch sequentially, even when multiple projects click at once."""
    with VOICEBOX_BATCH_LOCK:
        while True:
            package = _package(project_dir)
            batch = _voicebox_batch(package)
            if not batch or batch.get("batch_id") != batch_id:
                raise AvatarCloudError("批量配音任务不存在或已被新的批次替换")
            if batch.get("status") in {"completed", "completed_with_failures", "failed"}:
                return package
            if batch.get("status") == "queued":
                batch["status"] = "running"
                batch["started_at"] = batch.get("started_at") or _now()
                _save_package(project_dir, package)

            pending = next((item for item in batch["items"] if item.get("status") == "queued"), None)
            if not pending:
                _refresh_voicebox_batch_counts(batch)
                batch["finished_at"] = _now()
                batch["status"] = "completed_with_failures" if batch["failed_count"] else "completed"
                return _save_package(project_dir, package)

            turn_id = str(pending["turn_id"])
            pending["status"] = "generating"
            pending["started_at"] = _now()
            pending["error"] = ""
            _save_package(project_dir, package)
            try:
                start_voicebox_driving_audio_candidate(project_dir, turn_id, {
                    "profile_id": pending["profile_id"],
                    "selection_source": pending["selection_source"],
                    "batch_id": batch_id,
                })
                generated = generate_voicebox_driving_audio_candidate(project_dir, turn_id)
                generated_turn = _find_turn(generated, turn_id)
                job = generated_turn.get("driving_audio_job") if isinstance(generated_turn.get("driving_audio_job"), dict) else {}
                package = generated
                batch = _voicebox_batch(package)
                item = _batch_item(batch, turn_id)
                if job.get("status") != "completed":
                    item.update({"status": "failed", "finished_at": _now(), "error": str(job.get("error") or "本地配音未返回候选音频")})
                else:
                    candidate_id = str(job.get("candidate_id") or "")
                    if batch.get("apply_policy") == "auto_adopt":
                        package = apply_voicebox_driving_audio_candidate(project_dir, turn_id, candidate_id)
                    batch = _voicebox_batch(package)
                    item = _batch_item(batch, turn_id)
                    item.update({
                        "status": "completed",
                        "candidate_id": candidate_id,
                        "outcome": "adopted" if batch.get("apply_policy") == "auto_adopt" else "candidate",
                        "finished_at": _now(),
                        "error": "",
                    })
            except Exception as exc:
                package = _package(project_dir)
                batch = _voicebox_batch(package)
                item = _batch_item(batch, turn_id)
                job = _find_turn(package, turn_id).get("driving_audio_job")
                if isinstance(job, dict) and job.get("status") == "generating":
                    mark_voicebox_driving_audio_candidate_failed(project_dir, turn_id, exc)
                    package = _package(project_dir)
                    batch = _voicebox_batch(package)
                    item = _batch_item(batch, turn_id)
                item.update({"status": "failed", "finished_at": _now(), "error": _safe_voicebox_error(exc)})
            _refresh_voicebox_batch_counts(batch)
            _save_package(project_dir, package)


def _binding_snapshot(package: dict, turn: dict) -> dict:
    binding = _binding_for_turn(package, turn)
    if not isinstance(binding.get("presenter_shot"), dict):
        raise AvatarCloudError(f"{binding['name']} 尚未上传实际出镜图")
    if not isinstance(turn.get("driving_audio"), dict):
        raise AvatarCloudError(f"{turn['turn_id']} 尚未上传驱动音频")
    snapshot = {
        "speaker_id": str(turn["speaker_id"]),
        "presenter_shot": copy.deepcopy(binding["presenter_shot"]),
        "driving_audio": copy.deepcopy(turn["driving_audio"]),
    }
    fit = binding.get("aspect_fit") if isinstance(binding.get("aspect_fit"), dict) else _aspect_fit_record(package, binding["presenter_shot"])
    if fit.get("status") == "prepared" and isinstance(fit.get("provider_input"), dict):
        snapshot["provider_input"] = copy.deepcopy(fit["provider_input"])
    # The reusable identity record is provenance only.  DashScope receives the
    # actual project-local presenter image and audio, so a role-library entry
    # must never block or alter generation semantics.
    if isinstance(binding.get("role"), dict):
        snapshot["role"] = copy.deepcopy(binding["role"])
    return snapshot


def _binding_hash(snapshot: dict) -> str:
    shot = snapshot.get("provider_input") or snapshot.get("presenter_shot") or {}
    serialised = "|".join([str(snapshot.get("speaker_id") or ""), str(shot.get("sha256") or "")])
    return hashlib.sha256(serialised.encode("utf-8")).hexdigest()


def _input_hash(package: dict, turn: dict, snapshot: dict | None = None) -> str:
    snapshot = snapshot or _binding_snapshot(package, turn)
    cloud = _cloud(package)
    shot = snapshot.get("provider_input") or snapshot.get("presenter_shot") or {}
    audio = snapshot.get("driving_audio") or {}
    serialised = "|".join([
        str(shot.get("sha256") or ""), str(audio.get("sha256") or ""),
        str(cloud.get("model") or ""), str(cloud.get("resolution") or ""), str(cloud.get("aspect_ratio") or ""),
        str(cloud.get("render_spec_revision") or ""), str(turn.get("text") or ""),
    ])
    return hashlib.sha256(serialised.encode("utf-8")).hexdigest()


def _assert_turn_ready(package: dict, turn: dict) -> None:
    binding = _binding_for_turn(package, turn)
    if not binding.get("presenter_shot"):
        raise AvatarCloudError(f"请先为“{binding['name']}”上传实际单人出镜图")
    fit = binding.get("aspect_fit") if isinstance(binding.get("aspect_fit"), dict) else None
    if package.get("generation_mode") == "runninghub_longcat":
        cloud = _cloud(package)
        if cloud.get("model") != RUNNINGHUB_EXACT_CLOCK_MODEL or cloud.get("resolution") != RUNNINGHUB_EXACT_CLOCK_RESOLUTION:
            raise AvatarCloudError("当前素材包仍是旧 RunningHub 输入合同；请先保存“输出画幅与清晰度”，升级为 InfiniteTalk 448×560 精确帧输入")
        provider_input = (fit or {}).get("provider_input") if isinstance((fit or {}).get("provider_input"), dict) else None
        media = (provider_input or {}).get("media") if isinstance((provider_input or {}).get("media"), dict) else {}
        if int(media.get("width") or 0) != 448 or int(media.get("height") or 0) != 560:
            raise AvatarCloudError(f"{binding['name']} 尚未生成 448×560 的供应商输入图；请重新保存输出规格后再提交付费任务")
    if not fit or fit.get("status") == "needs_choice":
        raise AvatarCloudError(f"请先在“输出画幅与清晰度”中处理 {binding['name']} 的出镜图比例，再提交云端试片")
    if fit.get("status") == "prepared":
        provider_input = fit.get("provider_input") if isinstance(fit.get("provider_input"), dict) else None
        if not provider_input:
            raise AvatarCloudError(f"{binding['name']} 的出镜图适配预览不完整，请重新保存输出规格")
    if not turn.get("driving_audio"):
        raise AvatarCloudError(f"{turn['turn_id']} 尚未上传驱动音频")
    if not (turn["driving_audio"] or {}).get("provider_path"):
        raise AvatarCloudError(f"{turn['turn_id']} 的驱动音频尚未准备完成")


def _sync_cloud_sample_summary(package: dict) -> None:
    cloud = _cloud(package)
    bindings = _ensure_speaker_bindings(package)
    sample_turn_ids = [str((binding.get("sample") or {}).get("turn_id")) for binding in bindings if (binding.get("sample") or {}).get("turn_id")]
    cloud["sample_turn_ids"] = sample_turn_ids
    cloud["sample_turn_id"] = sample_turn_ids[0] if sample_turn_ids else None
    cloud["sample_approved"] = bool(bindings) and all(bool((binding.get("sample") or {}).get("approved")) for binding in bindings)


def _refresh_readiness(package: dict) -> None:
    bindings = _ensure_speaker_bindings(package)
    cloud = _cloud(package)
    _sync_cloud_sample_summary(package)
    missing: list[str] = []
    for binding in bindings:
        if not binding.get("presenter_shot"):
            missing.append(f"{binding['name']}缺少出镜图")
    audio_missing = [turn["turn_id"] for turn in package.get("turns", []) if not turn.get("driving_audio")]
    if audio_missing:
        missing.append("缺少驱动音频：" + "、".join(audio_missing))
    if missing:
        _set_cloud_message(package, "not_ready", "还需要：" + "；".join(missing))
        return
    jobs = [turn.get("cloud_job") or {} for turn in package.get("turns", [])]
    if jobs and all(job.get("status") == "succeeded" for job in jobs):
        _set_cloud_message(package, "completed", "全部数字人片段已生成，可检查原片并合成真实时间线。")
    elif any(job.get("status") in ACTIVE_JOB_STATES for job in jobs):
        _set_cloud_message(package, "generating_batch", f"{_provider_label(package)} 正在按脚本顺序生成片段，刷新页面不会中断跟踪。")
    else:
        _set_cloud_message(package, "ready", f"已准备 {len(package.get('turns', []))}/{len(package.get('turns', []))} 段驱动音频；请先为每位角色生成一段试片。")


def _set_job(package: dict, turn_id: str, **changes: Any) -> dict:
    turn = _find_turn(package, turn_id)
    job = turn.get("cloud_job")
    if not isinstance(job, dict):
        raise AvatarCloudError(f"{turn_id} 没有可跟踪的云端任务")
    job.update(changes)
    if job.get("status") in ACTIVE_JOB_STATES:
        job["heartbeat_at"] = _now()
    if job.get("status") in ACTIVE_JOB_STATES:
        turn["status"] = "cloud_generating" if job.get("status") != "queued" else "cloud_queued"
    elif job.get("status") == "succeeded":
        turn["status"] = "cloud_generated"
    elif job.get("status") in {"failed", "cancelled"}:
        turn["status"] = "cloud_failed" if job.get("status") == "failed" else "audio_ready"
    return turn


def _require_provider_configuration(package: dict) -> None:
    if package.get("generation_mode") == "runninghub_longcat":
        status = runninghub_configuration()
        if not status["configured"]:
            raise AvatarCloudError("RunningHub 数字人尚未就绪：" + "；".join(status["issues"]) + "。配置后重启工作台")
        if (
            str(status.get("workflow_id") or "") != INFINITETALK_448X560_EXACT_CLOCK_WORKFLOW_ID
            or str(status.get("workflow_profile") or "") != INFINITETALK_448X560_EXACT_CLOCK_PROFILE
        ):
            raise AvatarCloudError("RunningHub 必须使用已验收的 InfiniteTalk 精确帧工作流，旧逐段工作流不得创建新的付费任务")
        return
    if not str(os.environ.get("DASHSCOPE_API_KEY") or "").strip() or not str(os.environ.get("DASHSCOPE_WORKSPACE_ID") or "").strip():
        raise AvatarCloudError("阿里云数字人尚未配置：请在 .env.secrets.local 中填写 DASHSCOPE_API_KEY 和 DASHSCOPE_WORKSPACE_ID，然后重启工作台")


def _queue_turn_in_package(package: dict, turn: dict, *, purpose: str, force: bool) -> None:
    if purpose not in {"sample", "batch", "retry"}:
        raise AvatarCloudError("数字人任务用途不支持")
    _assert_turn_ready(package, turn)
    old_job = turn.get("cloud_job") if isinstance(turn.get("cloud_job"), dict) else None
    if old_job and old_job.get("status") in ACTIVE_JOB_STATES:
        raise AvatarCloudError(f"{turn['turn_id']} 已有正在执行的云端任务")
    snapshot = _binding_snapshot(package, turn)
    input_hash = _input_hash(package, turn, snapshot)
    if old_job and old_job.get("status") == "succeeded" and old_job.get("input_hash") == input_hash and turn.get("source") and not force:
        raise AvatarCloudError(f"{turn['turn_id']} 的当前输入已经生成完成；如需重新扣费生成，请选择重新生成")
    attempt = int((old_job or {}).get("attempt") or 0) + 1
    turn["binding_snapshot"] = snapshot
    turn["cloud_job"] = {
        "job_id": f"AVJ-{uuid4().hex[:16]}",
        "status": "queued",
        "stage": "等待提交",
        "input_hash": input_hash,
        "binding_hash": _binding_hash(snapshot),
        "purpose": purpose,
        "attempt": attempt,
        "provider_task_id": None,
        "provider_status": None,
        "requested_at": _now(),
    }
    _set_job(package, turn["turn_id"], status="queued", stage="等待提交")
    binding = _binding_for_turn(package, turn)
    if purpose == "sample" or (purpose == "retry" and str((binding.get("sample") or {}).get("turn_id") or "") == str(turn["turn_id"])):
        binding["sample"] = {"status": "queued", "turn_id": turn["turn_id"], "input_hash": input_hash, "approved": False, "requested_at": _now()}


def queue_cloud_turn(project_dir: Path, turn_id: str, *, purpose: str = "sample", force: bool = False) -> dict:
    package = _package(project_dir)
    _require_provider_configuration(package)
    turn = _find_turn(package, turn_id.upper())
    _queue_turn_in_package(package, turn, purpose=purpose, force=force)
    cloud = _cloud(package)
    cloud["batch_started"] = purpose == "batch" or bool(cloud.get("batch_started"))
    _sync_cloud_sample_summary(package)
    _set_cloud_message(package, "generating_sample" if purpose == "sample" else "generating_batch", f"{turn['turn_id']} 已进入云端生成队列。")
    return _save_package(project_dir, package)


def queue_cloud_samples(project_dir: Path, *, force: bool = False) -> tuple[dict, list[str]]:
    """Queue one representative turn for every speaker, in script order."""
    package = _package(project_dir)
    _require_provider_configuration(package)
    turn_ids: list[str] = []
    for binding in _ensure_speaker_bindings(package):
        speaker_turns = [turn for turn in package.get("turns", []) if turn.get("speaker_id") == binding.get("speaker_id")]
        if not speaker_turns:
            continue
        sample = binding.get("sample") or {}
        if sample.get("approved") and not force:
            continue
        if sample.get("status") in {"queued", "generating", "awaiting_approval"} and not force:
            continue
        turn = speaker_turns[0]
        _queue_turn_in_package(package, turn, purpose="sample", force=force)
        turn_ids.append(str(turn["turn_id"]))
    _sync_cloud_sample_summary(package)
    if turn_ids:
        _set_cloud_message(package, "generating_sample", f"已为 {len(turn_ids)} 位说话人排入试片队列，将按脚本顺序生成。")
    else:
        _refresh_readiness(package)
    return _save_package(project_dir, package), turn_ids


def approve_cloud_sample(project_dir: Path, speaker_id: str) -> dict:
    package = _package(project_dir)
    binding = _binding(package, speaker_id)
    sample = binding.get("sample") or {}
    turn_id = str(sample.get("turn_id") or "")
    if not turn_id:
        raise AvatarCloudError(f"“{binding['name']}”尚未生成试片")
    turn = _find_turn(package, turn_id)
    if (turn.get("cloud_job") or {}).get("status") != "succeeded" or not turn.get("source"):
        raise AvatarCloudError(f"“{binding['name']}”的试片尚未生成完成，不能确认")
    binding["sample"] = {
        "status": "approved", "turn_id": turn_id, "input_hash": (turn.get("cloud_job") or {}).get("input_hash"),
        "approved": True, "requested_at": sample.get("requested_at") or _now(), "approved_at": _now(),
    }
    _refresh_binding_statuses(package)
    _sync_cloud_sample_summary(package)
    if _cloud(package).get("sample_approved"):
        _set_cloud_message(package, "ready", "所有角色试片均已确认，可以开始生成其余片段。")
    else:
        waiting = [item["name"] for item in package["speaker_bindings"] if not (item.get("sample") or {}).get("approved")]
        _set_cloud_message(package, "awaiting_sample_approval", "还需要确认试片：" + "、".join(waiting))
    return _save_package(project_dir, package)


def assert_cloud_turn_resumable(project_dir: Path, turn_id: str) -> dict:
    package = _package(project_dir)
    turn = _find_turn(package, turn_id.upper())
    job = turn.get("cloud_job") or {}
    # Older workers treated a local polling timeout as a terminal failure even
    # after DashScope had accepted the task.  Repair that exact legacy state
    # before resuming the *same* provider task id; never use this path for a
    # provider-declared failure, cancellation, or an ambiguous submission.
    timed_out_locally = (
        job.get("status") == "failed"
        and bool(job.get("provider_task_id"))
        and "任务超时；任务编号已保存" in str(job.get("error") or "")
    )
    if timed_out_locally:
        _set_job(
            package,
            turn_id.upper(),
            status="running",
            stage="阿里云仍在生成，正在继续跟踪",
            provider_status=str(job.get("provider_status") or "RUNNING"),
            error="",
        )
        # The artifact contract treats timestamps as optional strings.  Drop
        # the stale terminal timestamp instead of serialising a null value.
        (_find_turn(package, turn_id.upper()).get("cloud_job") or {}).pop("finished_at", None)
        binding = _binding_for_turn(package, turn)
        sample = binding.get("sample") if isinstance(binding.get("sample"), dict) else {}
        if sample.get("turn_id") == turn_id.upper() and not sample.get("approved"):
            binding["sample"] = {
                "status": "queued",
                "turn_id": turn_id.upper(),
                "input_hash": job.get("input_hash"),
                "approved": False,
                "requested_at": sample.get("requested_at") or job.get("requested_at") or _now(),
            }
        _refresh_binding_statuses(package)
        _sync_cloud_sample_summary(package)
        _set_cloud_message(package, "generating_sample", f"{turn_id.upper()} 仍在阿里云生成，已恢复同一任务编号的跟踪。")
        return _save_package(project_dir, package)
    if job.get("status") not in {"submitted", "running", "downloading"} or not job.get("provider_task_id"):
        raise AvatarCloudError(f"{turn_id} 没有可继续跟踪的阿里云任务")
    # A previously restored task may already be active but still carry the
    # terminal timestamp written by the pre-fix worker.  Remove it on this
    # safe resume/read path so the UI cannot show contradictory states.
    if job.get("finished_at"):
        job.pop("finished_at", None)
        return _save_package(project_dir, package)
    return package


def queue_cloud_batch(project_dir: Path) -> tuple[dict, list[str]]:
    package = _package(project_dir)
    _require_provider_configuration(package)
    _sync_cloud_sample_summary(package)
    unapproved = [binding["name"] for binding in package.get("speaker_bindings", []) if not (binding.get("sample") or {}).get("approved")]
    if unapproved:
        raise AvatarCloudError("请先试听并确认以下角色的试片：" + "、".join(unapproved))
    turn_ids: list[str] = []
    for turn in package.get("turns", []):
        _assert_turn_ready(package, turn)
        job = turn.get("cloud_job") or {}
        current_hash = _input_hash(package, turn)
        if job.get("status") == "succeeded" and job.get("input_hash") == current_hash and turn.get("source"):
            continue
        _queue_turn_in_package(package, turn, purpose="batch", force=True)
        turn_ids.append(str(turn["turn_id"]))
    if not turn_ids:
        raise AvatarCloudError("全部片段已经按当前角色、出镜图和音频生成完成")
    cloud = _cloud(package)
    cloud["batch_started"] = True
    _set_cloud_message(package, "generating_batch", f"已排入 {len(turn_ids)} 段，将按脚本顺序生成。")
    return _save_package(project_dir, package), turn_ids


def _mark_failed(project_dir: Path, turn_id: str, error: Exception | str) -> dict:
    package = _package(project_dir)
    message = str(error).strip() or "云端任务失败"
    _set_job(package, turn_id, status="failed", stage="失败", finished_at=_now(), error=message[:1000])
    turn = _find_turn(package, turn_id)
    binding = _binding_for_turn(package, turn)
    sample = binding.get("sample") or {}
    if sample.get("turn_id") == turn_id and not sample.get("approved"):
        binding["sample"] = {"status": "failed", "turn_id": turn_id, "input_hash": sample.get("input_hash"), "approved": False, "error": message[:1000]}
    _refresh_binding_statuses(package)
    _sync_cloud_sample_summary(package)
    _set_cloud_message(package, "failed", f"{turn_id} 生成失败：{message[:200]}")
    return _save_package(project_dir, package)


def _snapshot_paths(project_dir: Path, snapshot: dict) -> tuple[Path, Path]:
    shot = _safe_project_file(project_dir, str((snapshot.get("provider_input") or snapshot.get("presenter_shot") or {}).get("path") or ""))
    audio = _safe_project_file(project_dir, str((snapshot.get("driving_audio") or {}).get("provider_path") or ""))
    if not shot.is_file() or not audio.is_file():
        raise AvatarCloudError("该任务冻结的出镜图或驱动音频已经丢失，请重新生成该片段")
    return shot, audio


def _after_turn_completed(package: dict, turn_id: str) -> None:
    turn = _find_turn(package, turn_id)
    binding = _binding_for_turn(package, turn)
    sample = binding.get("sample") or {}
    if sample.get("turn_id") == turn_id and not sample.get("approved"):
        binding["sample"] = {
            "status": "awaiting_approval", "turn_id": turn_id, "input_hash": (turn.get("cloud_job") or {}).get("input_hash"),
            "approved": False, "requested_at": sample.get("requested_at") or _now(),
        }
    _refresh_binding_statuses(package)
    _sync_cloud_sample_summary(package)
    all_done = all((item.get("cloud_job") or {}).get("status") == "succeeded" for item in package.get("turns", []))
    if all_done:
        _set_cloud_message(package, "completed", "全部数字人片段已生成，可检查原片并合成真实时间线。")
    elif any((item.get("sample") or {}).get("status") == "awaiting_approval" for item in package.get("speaker_bindings", [])):
        waiting = [item["name"] for item in package["speaker_bindings"] if (item.get("sample") or {}).get("status") == "awaiting_approval"]
        _set_cloud_message(package, "awaiting_sample_approval", "试片已生成，请确认：" + "、".join(waiting))
    else:
        _set_cloud_message(package, "generating_batch", f"{turn_id} 已完成，正在继续其余片段。")


def run_cloud_turn(project_dir: Path, turn_id: str, *, poll_interval: float = POLL_INTERVAL_SECONDS, poll_timeout: float = POLL_TIMEOUT_SECONDS) -> dict:
    """Run or resume one queued task. Intended for the server's background worker."""
    key = f"{str(project_dir.resolve()).casefold()}::{turn_id.upper()}"
    with CLOUD_TURN_LOCKS_GUARD:
        turn_lock = CLOUD_TURN_LOCKS.setdefault(key, RLock())
    with turn_lock:
        return _run_cloud_turn_locked(project_dir, turn_id, poll_interval=poll_interval, poll_timeout=poll_timeout)


def _run_cloud_turn_locked(project_dir: Path, turn_id: str, *, poll_interval: float, poll_timeout: float) -> dict:
    """Internal cloud runner; caller owns the per-project/turn execution lock."""
    package = _package(project_dir)
    turn = _find_turn(package, turn_id.upper())
    job = turn.get("cloud_job") or {}
    if job.get("status") not in ACTIVE_JOB_STATES:
        raise AvatarCloudError(f"{turn_id} 没有可执行的云端任务")
    snapshot = turn.get("binding_snapshot") if isinstance(turn.get("binding_snapshot"), dict) else _binding_snapshot(package, turn)
    if job.get("input_hash") != _input_hash(package, turn, snapshot):
        raise AvatarCloudError("任务输入快照不完整或已损坏，请重新生成该片段")
    try:
        runninghub = package.get("generation_mode") == "runninghub_longcat"
        client = RunningHubLongCatClient() if runninghub else DashscopeWanS2VClient()
        provider_label = _provider_label(package)
        task_id = job.get("provider_task_id")
        if not task_id:
            shot_path, audio_path = _snapshot_paths(project_dir, snapshot)
            _set_job(package, turn_id, status="uploading", stage="正在上传该角色的出镜图和驱动音频", started_at=_now())
            _save_package(project_dir, package)
            if runninghub:
                try:
                    clock = inspect_frame_clock_wav(
                        audio_path,
                        fps=AVATAR_VIDEO_FPS,
                        require_aligned=True,
                    )
                except AvatarAudioClockError as exc:
                    raise AvatarCloudError(f"驱动音频不符合精确帧时钟：{exc}") from exc
                image_name = client.upload_file(shot_path, file_type="image")
                audio_name = client.upload_file(audio_path, file_type="audio")
                package = _package(project_dir)
                _set_job(
                    package,
                    turn_id,
                    status="submitted",
                    stage="正在提交 RunningHub 积分任务",
                    sample_rate=int(clock["sample_rate"]),
                    sample_frame_count=int(clock["sample_frame_count"]),
                    samples_per_video_frame=int(clock["samples_per_video_frame"]),
                    video_fps=int(clock["video_fps"]),
                    exact_total_frames=int(clock["video_frame_count"]),
                )
                _save_package(project_dir, package)
                submission = client.submit(
                    presenter_filename=image_name,
                    audio_filename=audio_name,
                    exact_total_frames=int(clock["video_frame_count"]),
                )
            else:
                image = client.upload_file(shot_path)
                audio = client.upload_file(audio_path)
                package = _package(project_dir)
                _set_job(package, turn_id, status="detecting", stage="正在检查出镜图")
                _save_package(project_dir, package)
                client.detect_face(str(image["oss_url"]))
                package = _package(project_dir)
                _set_job(package, turn_id, status="submitted", stage="已提交阿里云生成任务")
                _save_package(project_dir, package)
                submission = client.submit(str(image["oss_url"]), str(audio["oss_url"]), resolution=str(_cloud(package).get("resolution") or "480P"))
            package = _package(project_dir)
            _set_job(package, turn_id, status="running", stage=f"{provider_label} 正在生成", provider_task_id=submission["task_id"], provider_status="PENDING")
            _save_package(project_dir, package)
            task_id = submission["task_id"]
        deadline = time.monotonic() + max(1.0, poll_timeout)
        while time.monotonic() < deadline:
            result = client.poll(str(task_id))
            provider_status = str(result.get("status") or "UNKNOWN")
            package = _package(project_dir)
            _set_job(package, turn_id, status="running", stage=f"{provider_label} 正在生成", provider_status=provider_status)
            _save_package(project_dir, package)
            if provider_status == "SUCCEEDED" and result.get("video_url"):
                provider_slug = "runninghub" if runninghub else "dashscope"
                output_filename = f"{turn_id}_{provider_slug}.mp4"
                temporary, target = prepare_upload(project_dir, output_filename, turn_id=turn_id)
                package = _package(project_dir)
                _set_job(package, turn_id, status="downloading", stage="正在下载并校验成片", provider_status=provider_status)
                _save_package(project_dir, package)
                try:
                    client.download(str(result["video_url"]), temporary)
                    finalized = finalize_upload(project_dir, temporary, target, output_filename, turn_id=turn_id)
                finally:
                    try:
                        temporary.unlink()
                    except OSError:
                        pass
                completed = _find_turn(finalized, turn_id)
                _set_job(finalized, turn_id, status="succeeded", stage="已下载并校验", provider_status=provider_status, finished_at=_now(), result_path=completed["source"]["path"])
                _after_turn_completed(finalized, turn_id)
                return _save_package(project_dir, finalized)
            if provider_status in {"FAILED", "CANCELED", "UNKNOWN"}:
                detail = str(result.get("error") or provider_status)
                raise AvatarCloudError(f"{provider_label} 任务失败：{detail}")
            time.sleep(max(0.2, poll_interval))
        # A saved provider id means the request was already accepted by
        # DashScope.  Treat a local polling-window expiry as resumable rather
        # than a generation failure: re-submitting here could create a second
        # paid job while the original one is still running remotely.
        package = _package(project_dir)
        _set_job(
            package,
            turn_id,
            status="running",
            stage=f"{provider_label} 仍在生成，可稍后继续跟踪",
            provider_task_id=str(task_id),
            provider_status="RUNNING",
            error="",
        )
        _set_cloud_message(package, "generating_sample", f"{turn_id} 仍在 {provider_label} 生成，已保存任务编号，可继续跟踪。")
        return _save_package(project_dir, package)
    except (AvatarCloudError, DashscopeAvatarError, RunningHubAvatarError, AvatarImportError, OSError) as exc:
        return _mark_failed(project_dir, turn_id, exc)


def _cancel_unstarted_turns(project_dir: Path, turn_ids: list[str], reason: str) -> dict:
    package = _package(project_dir)
    for turn_id in turn_ids:
        turn = _find_turn(package, turn_id)
        job = turn.get("cloud_job") if isinstance(turn.get("cloud_job"), dict) else None
        if job and job.get("status") == "queued":
            _set_job(package, turn_id, status="cancelled", stage="前序任务失败，等待人工重试", finished_at=_now(), error=reason[:1000])
    _refresh_readiness(package)
    _set_cloud_message(package, "failed", "前序片段生成失败，后续未提交片段已取消；修正后可逐段重新生成。")
    return _save_package(project_dir, package)


def run_cloud_batch(project_dir: Path, turn_ids: list[str]) -> dict:
    last: dict | None = None
    for index, turn_id in enumerate(turn_ids):
        while True:
            try:
                last = run_cloud_turn(project_dir, turn_id)
            except Exception as exc:
                last = _mark_failed(project_dir, turn_id, exc)
                return _cancel_unstarted_turns(project_dir, turn_ids[index + 1:], str(exc))
            job = _find_turn(last, turn_id).get("cloud_job") or {}
            status = str(job.get("status") or "")
            if status == "succeeded":
                break
            # The provider has already accepted this job, but can take longer
            # than one local polling window.  Continue with the same durable
            # task id rather than asking the user to resume manually or, worse,
            # submitting a duplicate paid request.
            if status in ACTIVE_JOB_STATES and job.get("provider_task_id"):
                continue
            return _cancel_unstarted_turns(project_dir, turn_ids[index + 1:], str(job.get("error") or "前序任务失败"))
    return last or _package(project_dir)


def mark_cloud_turn_failed(project_dir: Path, turn_id: str, error: Exception | str) -> dict:
    package = _package(project_dir)
    current = _find_turn(package, turn_id.upper())
    if (current.get("cloud_job") or {}).get("status") == "succeeded":
        return package
    return _mark_failed(project_dir, turn_id.upper(), error)


def recover_interrupted_avatar_jobs(project_dir: Path) -> dict[str, Any]:
    """Reconcile durable jobs after a Backlot restart without double billing.

    Provider task ids are safe to resume.  Queued jobs have not reached the
    provider and are safe to submit.  Mid-upload/detect jobs without a saved
    provider id are deliberately failed: blindly submitting them could charge
    twice if the process died immediately after the provider accepted them.
    """
    package = read_avatar_package(project_dir)
    if not package or package.get("generation_mode") not in CLOUD_GENERATION_MODES:
        return {"cloud_turn_ids": [], "voicebox_batch_id": None, "changed": False}
    before = copy.deepcopy(package)
    changed = False
    cloud_turn_ids: list[str] = []
    for turn in package.get("turns", []):
        job = turn.get("cloud_job") if isinstance(turn.get("cloud_job"), dict) else None
        if not job or job.get("status") not in ACTIVE_JOB_STATES:
            continue
        if job.get("status") == "queued" or job.get("provider_task_id"):
            cloud_turn_ids.append(str(turn["turn_id"]))
            continue
        job.update({
            "status": "failed",
            "stage": "服务重启后需要人工确认",
            "finished_at": _now(),
            "error": "任务在提交前后中断且没有保存云端任务编号。为避免重复计费，系统没有自动重新提交；请确认后点击重新生成此段。",
        })
        turn["status"] = "cloud_failed"
        changed = True
    batch = _voicebox_batch(package)
    voicebox_batch_id = None
    if batch and batch.get("status") in {"queued", "running"}:
        for item in batch.get("items", []):
            if not isinstance(item, dict) or item.get("status") != "generating":
                continue
            item.update({"status": "queued", "error": "工作台重启后已安全重新排队"})
            item.pop("started_at", None)
            turn = next((candidate for candidate in package.get("turns", []) if candidate.get("turn_id") == item.get("turn_id")), None)
            job = turn.get("driving_audio_job") if isinstance(turn, dict) and isinstance(turn.get("driving_audio_job"), dict) else None
            if job and job.get("status") == "generating":
                job.update({"status": "failed", "finished_at": _now(), "error": "工作台重启，原任务已重新排队"})
            changed = True
        voicebox_batch_id = str(batch.get("batch_id") or "") or None
    _refresh_readiness(package)
    if changed or package != before:
        _save_package(project_dir, package)
        changed = True
    return {"cloud_turn_ids": cloud_turn_ids, "voicebox_batch_id": voicebox_batch_id, "changed": changed}


def reconcile_cloud_avatar_package(project_dir: Path) -> dict | None:
    """Apply backward-compatible readiness rules to an existing package."""
    package = read_avatar_package(project_dir)
    if not package or package.get("generation_mode") not in CLOUD_GENERATION_MODES:
        return package
    # The workbench reads this function on every refresh.  It is an observer,
    # not a worker: while a cloud task is active, a derived-status write here
    # can race the worker's heartbeat/save and make an otherwise valid paid
    # task fail before it reaches the provider.  Active tasks already own the
    # canonical state, so defer non-essential legacy migration until idle.
    if any(_is_cloud_turn_active(turn) for turn in package.get("turns", [])):
        return package
    before = copy.deepcopy(package)
    _ensure_speaker_bindings(package)
    cloud = _cloud(package)
    aspect, _target = _target_aspect(package)
    cloud["aspect_ratio"] = aspect
    for binding in package.get("speaker_bindings", []):
        source = binding.get("presenter_shot") if isinstance(binding.get("presenter_shot"), dict) else None
        if not source:
            continue
        fit = binding.get("aspect_fit") if isinstance(binding.get("aspect_fit"), dict) else None
        if not fit:
            binding["aspect_fit"] = _aspect_fit_record(package, source)
    for turn in package.get("turns", []):
        snapshot = turn.get("binding_snapshot") if isinstance(turn.get("binding_snapshot"), dict) else None
        job = turn.get("cloud_job") if isinstance(turn.get("cloud_job"), dict) else None
        if not snapshot or not job:
            continue
        # Older revisions included optional role-library metadata in these
        # hashes.  Rebase them onto the provider's real inputs so upgrading the
        # workbench never causes a completed paid clip to be regenerated.
        migrated_input_hash = _input_hash(package, turn, snapshot)
        job["input_hash"] = migrated_input_hash
        job["binding_hash"] = _binding_hash(snapshot)
        binding = _binding_for_turn(package, turn)
        sample = binding.get("sample") if isinstance(binding.get("sample"), dict) else None
        if sample and sample.get("turn_id") == turn.get("turn_id"):
            sample["input_hash"] = migrated_input_hash
    _refresh_readiness(package)
    return _save_package(project_dir, package) if package != before else package
