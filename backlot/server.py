"""Backlot server — FastAPI app: board state API, SSE change feed, media.

The watcher observes ``projects/`` with watchfiles; on any change it bumps a
per-project version and wakes SSE subscribers, who tell the browser to
refetch state. Library intake and director-workbench mutations write only
inside the selected project directory.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import time
from contextlib import asynccontextmanager, suppress
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from backlot.audio_center import (
    AudioCenterError,
    generate_preview,
    mark_preview_failed,
    preview_audio_path,
    read_audio_center,
    set_default_voice,
    start_preview,
)
from backlot.ai_text import (
    TextAIError,
    read_doubao_text_ai_config,
    read_text_ai_config,
    read_text_provider_status,
    save_doubao_text_ai_config,
    save_text_ai_config,
    test_doubao_text_ai_connection,
    test_text_ai_connection,
)
from backlot.runninghub_config import (
    RunningHubConfigError,
    read_runninghub_config,
    save_runninghub_config,
)
from backlot.daily_automation import (
    approve_fallback_script,
    apply_config_with_scheduler,
    DailyAutomationError,
    previous_target_date,
    read_status as read_daily_automation_status,
    release_run_lock as release_daily_run_lock,
    request_text_story_replacement,
    scheduler_effective_state,
    scheduler_runtime_status,
    try_acquire_run_lock as try_acquire_daily_run_lock,
)
from backlot.daily_pipeline import run_daily_pipeline
from backlot.news_selection_v2 import read_news_selection_v2, read_news_selection_v2_run, run_news_selection_v2
from backlot.avatar_import import (
    AvatarImportError,
    MAX_UPLOAD_BYTES,
    assemble_avatar_package,
    finalize_upload,
    initialize_avatar_package,
    list_local_whisper_models,
    list_avatar_source_plans,
    apply_longform_speaker_candidate,
    mark_avatar_job_failed,
    mark_longform_speaker_diagnosis_failed,
    approve_high_confidence_longform_cuts,
    approve_longform_cut,
    prepare_upload,
    read_avatar_package,
    run_avatar_asr,
    run_longform_speaker_diagnosis,
    run_longform_speaker_realign,
    start_avatar_assembly,
    start_avatar_asr,
    start_longform_speaker_diagnosis,
    start_longform_speaker_realign,
    switch_to_local_longform_plan,
    update_longform_cut,
    update_longform_presentation,
    validate_avatar_package,
)
from backlot.avatar_cloud import (
    AvatarCloudError,
    MAX_DRIVING_AUDIO_BYTES,
    MAX_PRESENTER_IMAGE_BYTES,
    approve_cloud_sample,
    configure_cloud_render_spec,
    apply_voicebox_driving_audio_candidate,
    assert_cloud_turn_resumable,
    finalize_driving_audio_upload,
    finalize_presenter_upload,
    prepare_driving_audio_upload,
    prepare_presenter_upload,
    queue_cloud_batch,
    queue_cloud_samples,
    queue_cloud_turn,
    mark_cloud_turn_failed,
    mark_voicebox_driving_audio_candidate_failed,
    refresh_voicebox_speaker_mappings,
    recover_interrupted_avatar_jobs,
    run_voicebox_driving_audio_batch,
    run_cloud_batch,
    run_cloud_turn,
    generate_voicebox_driving_audio_candidate,
    select_cloud_avatar_role,
    set_voicebox_speaker_mapping,
    start_voicebox_driving_audio_batch,
    start_voicebox_driving_audio_candidate,
)
from backlot.avatar_roles import (
    AvatarRoleError,
    avatar_role_asset_file,
    create_avatar_role,
    finalize_role_reference_upload,
    list_avatar_roles,
    prepare_role_reference_upload,
)
from backlot.music_library import (
    MAX_PROJECT_MUSIC_BYTES,
    MusicLibraryError,
    complete_project_music_upload,
    prepare_project_music_upload,
)
from backlot.state import PROJECTS_DIR, REPO_ROOT, list_projects, load_board_state, summarize_project
from backlot.script_templates import ScriptTemplateError, list_avatar_script_templates, preview_avatar_script_template
from backlot.script_imports import (
    MAX_SCRIPT_IMPORT_BYTES,
    ScriptImportError,
    stage_docx_preview,
    stage_text_preview,
)
from lib.checkpoint import init_project
from backlot.review_preview_pipeline import (
    ReviewPreviewConflict,
    ReviewPreviewError,
    StaleReviewPreviewWorker,
)
from backlot.avatar_review_preview_pipeline import (
    AmbiguousAvatarOperation,
    AvatarReviewPreviewConflict,
    AvatarReviewPreviewError,
    StaleAvatarReviewPreviewWorker,
    avatar_review_preview_preflight,
    read_avatar_review_preview_job,
    recover_avatar_review_preview_job,
    resume_avatar_review_preview_job,
    run_avatar_review_preview_job,
    start_avatar_review_preview_job,
)
from backlot.workbench import (
    WorkbenchError,
    add_annotation,
    add_asset,
    audit_asset_library,
    adopt_ai_scene_visual,
    apply_avatar_package_to_timeline,
    assign_usage,
    add_surgical_directive,
    build_baseline_cache,
    cleanup_unused_assets,
    bootstrap_workbench,
    freeze_segment,
    generate_avatar_scene_keyframes,
    generate_scene_review_preview,
    generate_scene_motion_visual,
    generate_scene_ppt_card,
    generate_openai_images,
    generate_auto_final_video,
    generate_project_narration,
    generate_scene_narration_candidate,
    generate_project_video_render,
    generate_full_preview_render,
    generate_network_assets,
    generate_review_preview_sync,
    generate_visual_batch,
    generate_scene_keyframes,
    generate_scene_plan_from_script,
    generate_script_draft,
    import_avatar_user_script,
    import_avatar_script_template,
    mark_scene_keyframe_generation_failed,
    mark_scene_motion_visual_failed,
    mark_scene_ppt_card_failed,
    mark_auto_final_generation_failed,
    mark_network_asset_generation_failed,
    mark_review_preview_sync_failed,
    mark_visual_batch_failed,
    mark_music_sample_failed,
    music_track_path,
    mark_project_narration_failed,
    mark_project_video_render_failed,
    mark_full_preview_render_failed,
    mark_patch_render_failed,
    mark_scene_narration_candidate_failed,
    prepare_patch,
    promote_patch,
    read_workbench,
    read_music_catalog,
    read_task_center,
    read_scene_keyframe_generation,
    read_review_preview_sync,
    read_review_preview_job,
    read_visual_batch_generation,
    refine_scene_visual_copy,
    render_patch,
    rollback_patch,
    restore_trashed_asset,
    remove_surgical_directive,
    review_scene_keyframes,
    review_script_draft,
    reopen_script_draft,
    start_scene_keyframe_generation,
    start_scene_motion_visual_generation,
    start_scene_ppt_card_generation,
    start_scene_network_asset_refresh,
    start_auto_final_generation,
    start_network_asset_generation,
    start_review_preview_sync,
    review_preview_preflight,
    start_review_preview_job,
    resume_review_preview_job,
    run_review_preview_job,
    recover_review_preview_job,
    start_visual_batch_generation,
    start_visual_block_refresh,
    start_project_narration,
    start_scene_narration_apply,
    start_scene_narration_candidate,
    start_project_video_render,
    start_full_preview_render,
    retry_scene_ppt_card_generation,
    approve_full_preview_scenes,
    approve_music_sample,
    update_scene,
    update_scene_subtitles,
    update_scene_ppt_card_brief,
    update_scene_visual_plan,
    update_scene_visual_timeline,
    update_visual_block_lock,
    preview_visual_batch_plan,
    apply_presenter_layout_to_selected_scenes,
    update_presenter_layout_template,
    update_subtitle_style_template,
    update_subtitle_preferences_settings,
    update_intake,
    update_script_draft_content,
    update_music_policy,
    update_music_preferences_settings,
    update_narration_policy,
    update_narration_preferences_settings,
    read_music_preferences_settings,
    read_narration_preferences_settings,
    read_subtitle_preferences_settings,
    start_music_sample,
    generate_music_sample,
    voice_catalog,
)

UI_DIR = Path(__file__).resolve().parent / "ui"
THUMB_CACHE_DIR = REPO_ROOT / ".backlot" / "thumbs"
THUMB_WIDTHS = (320, 640, 960)

# Paths inside a project whose changes are pure noise for the board.
_IGNORE_PARTS = {"node_modules", ".git", "__pycache__", ".cache"}

SSE_HEARTBEAT_SECONDS = 15
PROJECT_ID_RE = re.compile(r"^[a-z][a-z0-9-]{2,63}$")
PROJECT_ASPECTS = {
    "landscape": {"label": "横版 16:9", "width": 1920, "height": 1080},
    "portrait": {"label": "竖版 9:16", "width": 1080, "height": 1920},
    "square": {"label": "方形 1:1", "width": 1080, "height": 1080},
}
LIBRARY_CREATE_PIPELINES = {"animated-explainer", "avatar-spokesperson"}
ACTIVE_PROJECT_TASK_STATES = {"queued", "uploading", "detecting", "submitted", "running", "generating", "downloading"}


def _atomic_json_write(path: Path, value: dict) -> None:
    """Write a project marker atomically so library readers never see partial JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as file:
            json.dump(value, file, ensure_ascii=False, indent=2)
            file.write("\n")
        Path(temp_name).replace(path)
    except Exception:
        with suppress(OSError):
            Path(temp_name).unlink()
        raise


def _create_project_from_library(payload: dict) -> dict:
    """Create a safe project workspace from the library's short intake form."""
    project_id = str(payload.get("project_id") or "").strip().lower()
    title = str(payload.get("title") or "").strip()
    pipeline_type = str(payload.get("pipeline_type") or "animated-explainer").strip()
    style_playbook = str(payload.get("style_playbook") or "clean-professional").strip()
    brief = str(payload.get("brief") or "").strip()
    aspect = str(payload.get("aspect") or "portrait")
    if not PROJECT_ID_RE.fullmatch(project_id):
        raise ValueError("项目代号须为 3–64 位小写英文、数字或连字符，且以英文开头")
    if not title or len(title) > 120:
        raise ValueError("请填写不超过 120 个字符的项目名称")
    if len(brief) > 3000:
        raise ValueError("项目说明不能超过 3000 个字符")
    if pipeline_type not in LIBRARY_CREATE_PIPELINES:
        raise ValueError("创建项目时只能选择“无数字人口播”或“有数字人口播”")
    if aspect not in PROJECT_ASPECTS:
        raise ValueError("所选画幅不可用")
    duration_seconds: int | None = None
    if payload.get("duration_seconds") not in (None, ""):
        try:
            duration_seconds = int(payload["duration_seconds"])
        except (TypeError, ValueError) as exc:
            raise ValueError("目标时长必须是有效数字") from exc
        if not 5 <= duration_seconds <= 300:
            raise ValueError("目标时长需在 5 到 300 秒之间")
    avatar_intake: dict[str, str] | None = None
    if pipeline_type == "avatar-spokesperson":
        avatar_source_status = str(payload.get("avatar_source_status") or "planned")
        avatar_generation_mode = str(payload.get("avatar_generation_mode") or "runninghub_longcat")
        avatar_import_mode = str(payload.get("avatar_import_mode") or "per_turn")
        avatar_default_treatment = str(payload.get("avatar_default_treatment") or "fullscreen")
        avatar_background_mode = str(payload.get("avatar_background_mode") or "opaque")
        if avatar_source_status not in {"ready", "planned"}:
            raise ValueError("数字人素材准备状态无效")
        if avatar_generation_mode not in {"runninghub_longcat", "dashscope_wan_s2v", "manual_import"}:
            raise ValueError("数字人生成来源无效")
        if avatar_import_mode not in {"per_turn", "longform"}:
            raise ValueError("数字人导入方式无效")
        if avatar_default_treatment not in {"fullscreen", "pip_top_left", "custom", "hidden"}:
            raise ValueError("数字人默认出镜方式无效")
        if avatar_background_mode not in {"opaque", "green_screen", "transparent", "unknown"}:
            raise ValueError("数字人视频背景类型无效")
        avatar_intake = {
            "source_status": avatar_source_status,
            "generation_mode": avatar_generation_mode,
            "import_mode": avatar_import_mode,
            "default_treatment": avatar_default_treatment,
            "background_mode": avatar_background_mode,
        }

    project_dir = PROJECTS_DIR / project_id
    if project_dir.exists():
        raise FileExistsError("该项目代号已存在；请换一个代号")
    try:
        project_dir = init_project(
            project_id,
            title=title,
            pipeline_type=pipeline_type,
            pipeline_dir=PROJECTS_DIR,
            style_playbook=style_playbook,
        )
    except Exception as exc:
        raise ValueError(f"无法创建项目：{exc}") from exc

    aspect_profile = PROJECT_ASPECTS[aspect]
    marker_path = project_dir / "project.json"
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    marker["render_profile"] = {
        "aspect_ratio": aspect,
        "width": aspect_profile["width"],
        "height": aspect_profile["height"],
        "fps": 30,
        "audio_sample_rate": 48000,
    }
    marker["intake"] = {
        "aspect": aspect,
        "aspect_label": aspect_profile["label"],
        "created_from": "backlot_library",
        "duration_source": "audio_driven" if duration_seconds is None else "user_target",
    }
    if brief:
        marker["intake"]["brief"] = brief
    if duration_seconds is not None:
        marker["intake"]["duration_seconds"] = duration_seconds
    if avatar_intake is not None:
        marker["intake"]["avatar"] = avatar_intake
    _atomic_json_write(marker_path, marker)
    return summarize_project(project_dir)


def _project_storage_summary(project_dir: Path) -> dict:
    """Return a bounded, symlink-safe inventory for the delete confirmation."""
    file_count = 0
    directory_count = 0
    total_bytes = 0
    categories = {"assets": 0, "artifacts": 0, "renders": 0, "other": 0}
    for root, directories, files in os.walk(project_dir, followlinks=False):
        root_path = Path(root)
        directory_count += len(directories)
        for filename in files:
            path = root_path / filename
            try:
                size = path.lstat().st_size
            except OSError:
                continue
            file_count += 1
            total_bytes += size
            try:
                top_level = path.relative_to(project_dir).parts[0]
            except (ValueError, IndexError):
                top_level = "other"
            category = top_level if top_level in {"assets", "artifacts", "renders"} else "other"
            categories[category] += size
    return {
        "file_count": file_count,
        "directory_count": directory_count,
        "total_bytes": total_bytes,
        "categories": categories,
    }


def _project_active_tasks(project_dir: Path) -> list[dict]:
    """Return persisted active work that makes immediate deletion unsafe."""
    active: list[dict] = []
    try:
        center = read_task_center(project_dir)
    except Exception:
        center = {}
    for task in center.get("tasks") or []:
        status = str(task.get("status") or "").lower()
        if status in ACTIVE_PROJECT_TASK_STATES:
            active.append({
                "task_id": str(task.get("task_id") or task.get("id") or ""),
                "label": str(task.get("label") or task.get("title") or "后台任务"),
                "status": status,
            })
    try:
        summary = summarize_project(project_dir)
    except Exception:
        summary = {}
    running_stages = [
        stage for stage in summary.get("stage_states") or []
        if str(stage.get("status") or "").lower() == "in_progress"
    ]
    if running_stages and not active:
        stage_name = str(running_stages[0].get("name") or summary.get("active_stage") or "项目")
        active.append({
            "task_id": stage_name,
            "label": f"{stage_name}仍在执行",
            "status": "running",
        })
    return active


def _project_deletion_preview(project_dir: Path) -> dict:
    marker = json.loads((project_dir / "project.json").read_text(encoding="utf-8")) if (project_dir / "project.json").is_file() else {}
    active_tasks = _project_active_tasks(project_dir)
    return {
        "project_id": project_dir.name,
        "title": str(marker.get("title") or project_dir.name),
        "storage": _project_storage_summary(project_dir),
        "active_tasks": active_tasks,
        "can_delete": not active_tasks,
        "scope": ["项目配置与审核记录", "项目脚本、分镜和任务记录", "项目内图片、视频、音频与字幕", "项目渲染成片与历史版本"],
        "preserved": ["通用角色库", "通用配音中心", "公共风格包与共享素材"],
    }


def _delete_project_from_library(project_id: str, payload: dict) -> dict:
    """Permanently remove one project after strict identity and path checks."""
    project_dir = _safe_project_dir(project_id)
    root = PROJECTS_DIR.resolve()
    if project_dir.is_symlink():
        raise ValueError("该项目目录是符号链接，为避免误删外部数据，不能从项目库删除")
    resolved = project_dir.resolve(strict=True)
    if resolved.parent != root or resolved.name != project_id:
        raise ValueError("项目目录不在允许的 projects 目录内，已拒绝删除")

    preview = _project_deletion_preview(project_dir)
    if str(payload.get("confirm_project_id") or "") != project_id:
        raise ValueError("项目编号确认不一致，请重新打开删除窗口")
    if str(payload.get("confirmation") or "") != "DELETE_PROJECT":
        raise ValueError("请在删除确认窗口中完成二次确认")
    if payload.get("permanent") is not True:
        raise ValueError("请明确确认永久删除项目及其数据")
    if preview["active_tasks"]:
        raise RuntimeError("项目仍有正在运行的任务，请等待任务结束后再删除")

    tombstone = root / f"_deleting-{project_id}-{int(time.time() * 1000)}"
    resolved.replace(tombstone)

    def make_writable_and_retry(function, path, _exc_info):
        os.chmod(path, stat.S_IWRITE)
        function(path)

    try:
        shutil.rmtree(tombstone, onerror=make_writable_and_retry)
    except Exception:
        if tombstone.exists() and not resolved.exists():
            tombstone.replace(resolved)
        raise
    return {
        "deleted": True,
        "project_id": project_id,
        "title": preview["title"],
        "deleted_storage": preview["storage"],
    }


def _ui_html(name: str, assets: tuple[str, ...]) -> HTMLResponse:
    html = (UI_DIR / name).read_text(encoding="utf-8")
    for asset in assets:
        path = UI_DIR / asset
        if path.is_file():
            # apply_patch and some sync tools can preserve mtimes.  A content
            # fingerprint prevents a long-lived browser from receiving 304 for
            # stale JavaScript after an interaction fix.
            version = hashlib.sha1(path.read_bytes()).hexdigest()[:12]
            html = html.replace(f"/ui/{asset}", f"/ui/{asset}?v={version}")
    return HTMLResponse(html)


class ChangeHub:
    """Fan-out of project-change notifications to SSE subscribers.

    Subscriptions are filtered: a board subscribed to one project only ever
    receives that project's ids, so unrelated-project bursts can't flood its
    queue and starve out the one notification it actually needs.
    """

    def __init__(self) -> None:
        self._subscribers: dict[asyncio.Queue, Optional[str]] = {}

    def subscribe(self, project_id: Optional[str] = None) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=64)
        self._subscribers[q] = project_id
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.pop(q, None)

    def publish(self, project_id: str) -> None:
        for q, only in list(self._subscribers.items()):
            if only is not None and only != project_id:
                continue
            try:
                q.put_nowait(project_id)
            except asyncio.QueueFull:
                # Queue holds only THIS subscriber's relevant ids, so a full
                # queue already guarantees a pending wake-up → safe to drop.
                pass


hub = ChangeHub()

# Library summaries are expensive to derive (full state parse per project);
# cache per project and invalidate from the watcher.
_summary_cache: dict[str, dict] = {}


def _invalidate_summary(project_id: str) -> None:
    _summary_cache.pop(project_id, None)


_REVIEW_PREVIEW_PRIVATE_KEYS = {
    "worker_token",
    "tts_terminal_retry_authorized",
    "capabilities",
    "dependencies",
    "launch_required",
}


def _validate_review_preview_request(payload: dict, *, allowed: set[str]) -> None:
    """Reject client attempts to inject parent-worker or dependency state."""

    unknown = sorted(str(key) for key in payload if key not in allowed)
    if unknown:
        raise ReviewPreviewError("请求包含不支持的字段：" + "、".join(unknown))

    def inspect(value: object) -> None:
        if isinstance(value, dict):
            for raw_key, child in value.items():
                key = str(raw_key)
                if key in _REVIEW_PREVIEW_PRIVATE_KEYS or key.startswith("_review_preview_"):
                    raise ReviewPreviewError("请求不得包含内部任务字段")
                inspect(child)
        elif isinstance(value, list):
            for child in value:
                inspect(child)

    inspect(payload)


def _public_review_preview_payload(value):
    """Defence in depth: never serialize a parent worker lease to the browser."""

    if isinstance(value, dict):
        return {
            key: _public_review_preview_payload(child)
            for key, child in value.items()
            if key not in {"worker_token", "tts_terminal_retry_authorized"}
            and not str(key).startswith("_review_preview_")
        }
    if isinstance(value, list):
        return [_public_review_preview_payload(child) for child in value]
    return value


def _track_background_task(app: FastAPI, coroutine) -> asyncio.Task:
    """Keep background work owned by the app until completion or shutdown."""

    tasks = getattr(app.state, "recovery_tasks", None)
    if tasks is None:
        tasks = set()
        app.state.recovery_tasks = tasks
    task = asyncio.create_task(coroutine)
    tasks.add(task)

    def finished(completed: asyncio.Task) -> None:
        tasks.discard(completed)
        if completed.cancelled():
            return
        with suppress(Exception):
            completed.exception()

    task.add_done_callback(finished)
    return task


def _review_preview_job_id(state: dict) -> str:
    return str(state.get("job_id") or "")


def _record_review_preview_recovery_error(app: FastAPI, project_id: str, message: str) -> None:
    errors = getattr(app.state, "review_preview_recovery_errors", None)
    if errors is None:
        errors = {}
        app.state.review_preview_recovery_errors = errors
    errors[project_id] = message
    _invalidate_summary(project_id)
    hub.publish(project_id)


def _launch_review_preview_worker(app: FastAPI, project_dir: Path, job_id: str) -> asyncio.Task:
    """Dispatch exactly one lease-protected parent worker and publish its result."""

    async def run_parent() -> None:
        try:
            await asyncio.to_thread(run_review_preview_job, project_dir, job_id)
        except Exception:
            # The durable parent records ordinary failures itself.  Conflicts
            # mean another/current worker owns the job and need no second write.
            pass
        finally:
            _invalidate_summary(project_dir.name)
            hub.publish(project_dir.name)

    return _track_background_task(app, run_parent())


def _launch_avatar_review_preview_worker(app: FastAPI, project_dir: Path, job_id: str) -> asyncio.Task:
    """Dispatch one lease-protected paid-avatar parent worker."""

    async def run_parent() -> None:
        try:
            await asyncio.to_thread(run_avatar_review_preview_job, project_dir, job_id)
        except Exception:
            # The durable parent records ordinary failures and ambiguous paid
            # submits.  A stale lease is intentionally a no-op here.
            pass
        finally:
            _invalidate_summary(project_dir.name)
            hub.publish(project_dir.name)

    return _track_background_task(app, run_parent())


def _cached_summaries() -> list[dict]:
    if not PROJECTS_DIR.is_dir():
        return []
    summaries = []
    for entry in sorted(PROJECTS_DIR.iterdir()):
        if not entry.is_dir() or entry.name.startswith(("_", ".")):
            continue
        cached = _summary_cache.get(entry.name)
        if cached is None:
            try:
                cached = summarize_project(entry)
            except Exception:
                cached = {
                    "project_id": entry.name, "title": entry.name,
                    "pipeline_type": "unknown", "has_pipeline_state": False,
                    "poster": None, "live": False, "last_activity": 0,
                    "active_stage": None, "awaiting_human": False,
                    "stage_states": [], "completed_count": 0,
                    "render_count": 0, "scene_count": 0, "error": "unreadable",
                }
            _summary_cache[entry.name] = cached
        summaries.append(cached)
    summaries.sort(key=lambda s: (not s["live"], -(s["last_activity"] or 0)))
    return summaries


# Watch-loop hot path: pure string comparison, no per-path filesystem calls
# (change batches can be thousands of paths during a render).
import os as _os

_PROJECTS_ROOT_STR = _os.path.normcase(str(PROJECTS_DIR.resolve()))


def _project_of_change(path_str: str) -> Optional[str]:
    """Map a changed filesystem path to a project id (None = irrelevant)."""
    norm = _os.path.normcase(_os.path.normpath(path_str))
    if not norm.startswith(_PROJECTS_ROOT_STR):
        return None
    rel = norm[len(_PROJECTS_ROOT_STR):].lstrip("\\/")
    if not rel:
        return None
    parts = rel.replace("\\", "/").split("/")
    if _IGNORE_PARTS.intersection(parts):
        return None
    return parts[0]


async def _watch_projects() -> None:
    """Background task: watch projects/ and publish debounced changes."""
    try:
        from watchfiles import awatch
    except ImportError:
        return  # watcher unavailable → board still works via manual refresh
    if not PROJECTS_DIR.is_dir():
        return
    async for changes in awatch(PROJECTS_DIR, recursive=True, step=400):
        touched: set[str] = set()
        for _change, path_str in changes:
            pid = _project_of_change(path_str)
            if pid:
                touched.add(pid)
        for pid in touched:
            _invalidate_summary(pid)
            hub.publish(pid)


async def _recover_avatar_background_jobs(app: FastAPI) -> None:
    """Resume durable local/provider tasks after the web process restarts."""
    if not PROJECTS_DIR.is_dir():
        return
    for project_dir in sorted(PROJECTS_DIR.iterdir()):
        if not project_dir.is_dir() or project_dir.name.startswith(("_", ".")):
            continue
        try:
            parent = ((await asyncio.to_thread(read_workbench, project_dir)).get("automation") or {}).get("review_preview_pipeline") or {}
            if (
                parent.get("pipeline_kind") == "avatar_review_preview"
                and parent.get("status") not in {None, "idle", "completed", "cancelled"}
            ):
                continue
        except Exception:
            # The parent recovery pass below is the conservative owner when
            # workbench state cannot be inspected here.
            continue
        try:
            recovery = await asyncio.to_thread(recover_interrupted_avatar_jobs, project_dir)
        except Exception:
            continue
        turn_ids = list(recovery.get("cloud_turn_ids") or [])
        batch_id = recovery.get("voicebox_batch_id")

        async def run_cloud_recovery(path: Path = project_dir, ids: list[str] = turn_ids) -> None:
            try:
                await asyncio.to_thread(run_cloud_batch, path, ids)
            finally:
                _invalidate_summary(path.name)
                hub.publish(path.name)

        async def run_voicebox_recovery(path: Path = project_dir, identifier: str = str(batch_id or "")) -> None:
            try:
                await asyncio.to_thread(run_voicebox_driving_audio_batch, path, identifier)
            finally:
                _invalidate_summary(path.name)
                hub.publish(path.name)

        if turn_ids:
            app.state.recovery_tasks.add(asyncio.create_task(run_cloud_recovery()))
        if batch_id:
            app.state.recovery_tasks.add(asyncio.create_task(run_voicebox_recovery()))


async def _recover_workbench_background_jobs(app: FastAPI) -> None:
    """Resume the parent pipeline, or legacy child jobs when no parent owns them."""
    if not PROJECTS_DIR.is_dir():
        return
    for project_dir in sorted(PROJECTS_DIR.iterdir()):
        if not project_dir.is_dir() or project_dir.name.startswith(("_", ".")):
            continue

        # The parent owns visual/audio/preview children while it is active or
        # waiting for a user decision.  Recover it first and conservatively
        # suppress legacy child launch if parent inspection itself fails.
        try:
            project_meta = json.loads((project_dir / "project.json").read_text(encoding="utf-8"))
            is_avatar_project = str(project_meta.get("pipeline_type") or "") == "avatar-spokesperson"
            recovery_function = recover_avatar_review_preview_job if is_avatar_project else recover_review_preview_job
            parent = await asyncio.to_thread(recovery_function, project_dir)
        except Exception:
            _record_review_preview_recovery_error(
                app,
                project_dir.name,
                "父任务恢复检查失败；已保守阻止子任务自动恢复",
            )
            continue
        if not isinstance(parent, dict):
            _record_review_preview_recovery_error(
                app,
                project_dir.name,
                "父任务恢复返回了无效状态；已保守阻止子任务自动恢复",
            )
            continue
        raw_parent_status = parent.get("status")
        parent_status = str(raw_parent_status) if isinstance(raw_parent_status, str) else ""
        parent_job_id = _review_preview_job_id(parent)
        if parent.get("launch_required") is True:
            if parent_job_id:
                if is_avatar_project:
                    _launch_avatar_review_preview_worker(app, project_dir, parent_job_id)
                else:
                    _launch_review_preview_worker(app, project_dir, parent_job_id)
            else:
                _record_review_preview_recovery_error(
                    app,
                    project_dir.name,
                    "父任务要求恢复但缺少任务编号；已阻止子任务并等待人工检查",
                )
            continue
        if not parent_status:
            _record_review_preview_recovery_error(
                app,
                project_dir.name,
                "父任务恢复缺少有效状态；已保守阻止子任务自动恢复",
            )
            continue
        if parent_status not in {"idle", "completed", "cancelled"}:
            continue

        try:
            visual = await asyncio.to_thread(read_visual_batch_generation, project_dir)
            visual_job = dict(visual.get("generation") or {})
            visual_job_id = str(visual_job.get("job_id") or "")
            should_resume_visual = (
                bool(visual_job_id)
                and visual_job.get("status") in {"queued", "generating"}
                and any(item.get("status") in {"queued", "generating"} for item in (visual_job.get("items") or []))
            )
        except Exception:
            should_resume_visual = False
            visual_job_id = ""

        if should_resume_visual:
            async def run_visual_recovery(path: Path = project_dir, identifier: str = visual_job_id) -> None:
                try:
                    await asyncio.to_thread(generate_visual_batch, path, identifier)
                except Exception as exc:
                    try:
                        await asyncio.to_thread(mark_visual_batch_failed, path, exc, identifier)
                    except Exception:
                        pass
                finally:
                    _invalidate_summary(path.name)
                    hub.publish(path.name)

            _track_background_task(app, run_visual_recovery())

        try:
            sync = await asyncio.to_thread(read_review_preview_sync, project_dir)
            sync_job = dict(sync.get("generation") or {})
            sync_job_id = str(sync_job.get("job_id") or "")
            should_resume_sync = (
                bool(sync_job_id)
                and sync_job.get("status") in {"queued", "generating"}
                and any(item.get("status") in {"queued", "generating"} for item in (sync_job.get("items") or []))
            )
        except Exception:
            should_resume_sync = False
            sync_job_id = ""

        if should_resume_sync:
            async def run_sync_recovery(path: Path = project_dir, identifier: str = sync_job_id) -> None:
                try:
                    await asyncio.to_thread(generate_review_preview_sync, path, identifier)
                except Exception as exc:
                    try:
                        await asyncio.to_thread(mark_review_preview_sync_failed, path, exc, identifier)
                    except Exception:
                        pass
                finally:
                    _invalidate_summary(path.name)
                    hub.publish(path.name)

            _track_background_task(app, run_sync_recovery())


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Own and cleanly stop the project watcher with FastAPI's lifespan API."""

    task = asyncio.create_task(_watch_projects())
    app.state.watch_task = task
    app.state.recovery_tasks = set()
    app.state.review_preview_recovery_errors = {}
    await _recover_avatar_background_jobs(app)
    await _recover_workbench_background_jobs(app)
    # Do not resume a daily production run merely because the web server was
    # restarted.  A browser/server restart is not user approval to continue
    # paid Voicebox or RunningHub stages.  The durable daily CLI task owns the
    # scheduled 03:00 execution; an interrupted manual run stays visible for
    # an explicit user retry instead of silently advancing in the background.
    try:
        yield
    finally:
        task.cancel()
        for recovery_task in app.state.recovery_tasks:
            recovery_task.cancel()
        if app.state.recovery_tasks:
            await asyncio.gather(*app.state.recovery_tasks, return_exceptions=True)
        with suppress(asyncio.CancelledError):
            await task


def create_app() -> FastAPI:
    app = FastAPI(title="Backlot", docs_url=None, redoc_url=None, lifespan=_lifespan)

    # ---- API ----------------------------------------------------------

    @app.get("/api/health")
    async def health() -> dict:
        return {"ok": True, "app": "backlot"}

    # ---- Daily technology brief automation -----------------------------

    async def _daily_automation_payload() -> dict:
        state = await asyncio.to_thread(read_daily_automation_status)
        try:
            scheduler = await asyncio.to_thread(scheduler_runtime_status)
        except DailyAutomationError as exc:
            scheduler = {
                "installed": False,
                "runtime_enabled": False,
                "command_matches": False,
                "detail": str(exc),
            }
        state["scheduler"] = scheduler
        state["effective_state"] = scheduler_effective_state(state.get("config") or {}, scheduler)
        return state

    @app.get("/api/daily-automation/status")
    async def daily_automation_status() -> dict:
        return await _daily_automation_payload()

    @app.put("/api/daily-automation/config")
    async def put_daily_automation_config(payload: dict = Body(...)) -> dict:
        try:
            await asyncio.to_thread(apply_config_with_scheduler, payload)
            return await _daily_automation_payload()
        except DailyAutomationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/daily-automation/runs", status_code=202)
    async def start_daily_automation_run(payload: dict = Body(default={})) -> dict:
        raw_target = str(payload.get("target_date") or "").strip()
        try:
            target = date.fromisoformat(raw_target) if raw_target else previous_target_date()
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="目标日期必须使用 YYYY-MM-DD") from exc
        if target >= datetime.now().astimezone().date():
            raise HTTPException(status_code=422, detail="只能生成已经结束的自然日新闻")
        if not try_acquire_daily_run_lock(target, trigger="frontend"):
            raise HTTPException(status_code=409, detail="已有每日科技快报任务正在运行，请在状态卡查看进度")

        async def run_daily() -> None:
            try:
                await asyncio.to_thread(run_daily_pipeline, target, trigger="frontend")
            except Exception:
                # The durable run manifest already contains the user-safe
                # stage error.  Never leak provider responses into server logs.
                pass
            finally:
                release_daily_run_lock()

        task = asyncio.create_task(run_daily())
        app.state.recovery_tasks.add(task)
        task.add_done_callback(lambda completed: app.state.recovery_tasks.discard(completed))
        return {"accepted": True, "target_date": target.isoformat(), "message": "一条龙任务已开始：检索、脚本、配音、Standard 24GB数字人、画面与全片预览将依次完成"}

    @app.post("/api/daily-automation/runs/{target_date}/approve-fallback-script")
    async def approve_daily_fallback_script(target_date: str) -> dict:
        try:
            target = date.fromisoformat(target_date)
            run = await asyncio.to_thread(approve_fallback_script, target)
            return {"approved": True, "run": run, "message": "保底脚本已人工确认；再次启动任务后才会进入配音阶段"}
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="目标日期必须使用 YYYY-MM-DD") from exc
        except DailyAutomationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/daily-automation/runs/{target_date}/replace-weak-story")
    async def replace_daily_weak_story(target_date: str) -> dict:
        try:
            target = date.fromisoformat(target_date)
            run = await asyncio.to_thread(request_text_story_replacement, target)
            return {
                "accepted": True,
                "run": run,
                "message": "已保留当前头条并切换差异化候选；请重新启动任务进行文本双门。",
            }
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="目标日期必须使用 YYYY-MM-DD") from exc
        except DailyAutomationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/api/daily-automation/news-selection-v2")
    async def get_daily_news_selection_v2(target_date: str = "") -> dict:
        try:
            target = date.fromisoformat(target_date) if target_date else previous_target_date()
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="目标日期必须使用 YYYY-MM-DD") from exc
        return {
            "target_date": target.isoformat(),
            "run": await asyncio.to_thread(read_news_selection_v2_run, target),
            "selection": await asyncio.to_thread(read_news_selection_v2, target),
        }

    @app.post("/api/daily-automation/news-selection-v2", status_code=202)
    async def start_daily_news_selection_v2(payload: dict = Body(default={})) -> dict:
        raw_target = str(payload.get("target_date") or "").strip()
        try:
            target = date.fromisoformat(raw_target) if raw_target else previous_target_date()
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="目标日期必须使用 YYYY-MM-DD") from exc
        if target >= datetime.now().astimezone().date():
            raise HTTPException(status_code=422, detail="只能选择已经结束自然日的新闻")
        if not try_acquire_daily_run_lock(target, trigger="news-selection-v2"):
            raise HTTPException(status_code=409, detail="已有每日科技快报任务正在运行")

        async def run_selection() -> None:
            try:
                await asyncio.to_thread(run_news_selection_v2, target, trigger="frontend")
            except Exception:
                pass
            finally:
                release_daily_run_lock()

        task = asyncio.create_task(run_selection())
        app.state.recovery_tasks.add(task)
        task.add_done_callback(lambda completed: app.state.recovery_tasks.discard(completed))
        return {"accepted": True, "target_date": target.isoformat(), "message": "新闻素材选择V2已开始；不会生成脚本或覆盖旧选题"}

    # ---- Software-wide text intelligence configuration -----------------

    @app.get("/api/ai-text/config")
    async def get_ai_text_config() -> dict:
        return await asyncio.to_thread(read_text_ai_config)

    @app.put("/api/ai-text/config")
    async def put_ai_text_config(payload: dict = Body(...)) -> dict:
        try:
            return await asyncio.to_thread(save_text_ai_config, payload)
        except TextAIError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/ai-text/test")
    async def post_ai_text_test() -> dict:
        try:
            return await asyncio.to_thread(test_text_ai_connection)
        except TextAIError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/api/ai-text/providers")
    async def get_ai_text_providers() -> dict:
        return await asyncio.to_thread(read_text_provider_status)

    @app.get("/api/ai-text/doubao/config")
    async def get_doubao_text_config() -> dict:
        return await asyncio.to_thread(read_doubao_text_ai_config)

    @app.put("/api/ai-text/doubao/config")
    async def put_doubao_text_config(payload: dict = Body(...)) -> dict:
        try:
            return await asyncio.to_thread(save_doubao_text_ai_config, payload)
        except TextAIError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/ai-text/doubao/test")
    async def post_doubao_text_test() -> dict:
        try:
            return await asyncio.to_thread(test_doubao_text_ai_connection)
        except TextAIError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/api/runninghub/config")
    async def get_runninghub_config() -> dict:
        return await asyncio.to_thread(read_runninghub_config)

    @app.put("/api/runninghub/config")
    async def put_runninghub_config(payload: dict = Body(...)) -> dict:
        try:
            return await asyncio.to_thread(save_runninghub_config, payload)
        except RunningHubConfigError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    # ---- Global avatar identity library ---------------------------------

    @app.get("/api/avatar-roles")
    async def avatar_roles() -> dict:
        return await asyncio.to_thread(list_avatar_roles)

    @app.post("/api/avatar-roles", status_code=201)
    async def post_avatar_role(payload: dict = Body(...)) -> dict:
        try:
            return await asyncio.to_thread(create_avatar_role, payload)
        except AvatarRoleError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.put("/api/avatar-roles/{role_id}/references/{slot}/file")
    async def put_avatar_role_reference(role_id: str, slot: str, request: Request, filename: str) -> dict:
        try:
            temporary, target = await asyncio.to_thread(prepare_role_reference_upload, role_id, slot, filename)
        except AvatarRoleError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        received = 0
        try:
            with temporary.open("wb") as output:
                async for chunk in request.stream():
                    received += len(chunk)
                    if received > 25 * 1024 * 1024:
                        raise HTTPException(status_code=413, detail="角色参考图不能超过 25MB")
                    output.write(chunk)
            return await asyncio.to_thread(finalize_role_reference_upload, role_id, slot, temporary, target, filename)
        except HTTPException:
            raise
        except AvatarRoleError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        finally:
            try:
                temporary.unlink()
            except OSError:
                pass

    @app.get("/api/avatar-roles/{role_id}/media/{reference_path:path}")
    async def avatar_role_media(role_id: str, reference_path: str):
        try:
            path = await asyncio.to_thread(avatar_role_asset_file, role_id, reference_path)
        except AvatarRoleError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return FileResponse(path)

    # ---- Global audio centre (software-wide; not tied to a project) -----

    @app.get("/api/audio-center")
    async def audio_center() -> dict:
        return await asyncio.to_thread(read_audio_center)

    @app.put("/api/audio-center/default-voice")
    async def put_default_voice(payload: dict = Body(...)) -> dict:
        try:
            return await asyncio.to_thread(set_default_voice, payload)
        except AudioCenterError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/audio-center/previews/jobs")
    async def start_audio_preview(payload: dict = Body(...)) -> dict:
        try:
            state = await asyncio.to_thread(start_preview, payload)
        except AudioCenterError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        async def run_preview() -> None:
            try:
                await asyncio.to_thread(generate_preview)
            except Exception as exc:
                try:
                    await asyncio.to_thread(mark_preview_failed, exc)
                except Exception:
                    pass

        asyncio.create_task(run_preview())
        return state

    @app.get("/api/projects")
    async def projects() -> list:
        return await asyncio.to_thread(_cached_summaries)

    @app.post("/api/projects", status_code=201)
    async def create_project(payload: dict = Body(...)) -> dict:
        """Create a project from the library intake without accepting filesystem paths."""
        try:
            project = await asyncio.to_thread(_create_project_from_library, payload)
        except FileExistsError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        _invalidate_summary(project["project_id"])
        hub.publish(project["project_id"])
        return project

    @app.get("/api/projects/{project_id}/deletion-preview")
    async def project_deletion_preview(project_id: str) -> dict:
        project_dir = _safe_project_dir(project_id)
        try:
            return await asyncio.to_thread(_project_deletion_preview, project_dir)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=422, detail=f"无法检查项目删除范围：{exc}") from exc

    @app.delete("/api/projects/{project_id}")
    async def delete_project(project_id: str, payload: dict = Body(...)) -> dict:
        try:
            result = await asyncio.to_thread(_delete_project_from_library, project_id, payload)
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"项目数据删除失败，原项目已尽量恢复：{exc}") from exc
        _invalidate_summary(project_id)
        hub.publish(project_id)
        return result

    @app.get("/api/script-templates/avatar")
    async def avatar_script_templates() -> dict:
        """Discover local, parseable multi-speaker Markdown templates."""
        return await asyncio.to_thread(list_avatar_script_templates)

    @app.get("/api/script-templates/avatar/preview")
    async def avatar_script_template_preview(template_id: str) -> dict:
        try:
            # The endpoint intentionally omits the complete Markdown body.
            # The frontend only needs the parsed, reviewable dialogue contract;
            # the immutable source copy is stored project-locally on import.
            return await asyncio.to_thread(preview_avatar_script_template, template_id)
        except ScriptTemplateError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/api/project/{project_id}/state")
    async def project_state(project_id: str) -> dict:
        project_dir = _safe_project_dir(project_id)
        return await asyncio.to_thread(load_board_state, project_dir)

    # ---- Director workbench (human decisions, project-local persistence) --

    async def workbench_call(fn, project_id: str, *args, **kwargs) -> dict:
        project_dir = _safe_project_dir(project_id)
        try:
            result = await asyncio.to_thread(fn, project_dir, *args, **kwargs)
        except WorkbenchError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        _invalidate_summary(project_id)
        hub.publish(project_id)
        return result

    async def receive_project_music_upload(
        project_id: str,
        request: Request,
        filename: str,
    ) -> dict:
        """Stream one local BGM into the selected project with a hard byte cap."""
        project_dir = _safe_project_dir(project_id)
        try:
            temporary = await asyncio.to_thread(
                prepare_project_music_upload, project_dir, filename
            )
        except MusicLibraryError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        received = 0
        try:
            with temporary.open("wb") as output:
                async for chunk in request.stream():
                    received += len(chunk)
                    if received > MAX_PROJECT_MUSIC_BYTES:
                        raise HTTPException(status_code=413, detail="背景音乐文件不能超过 100MB")
                    output.write(chunk)
            _path, track = await asyncio.to_thread(
                complete_project_music_upload,
                project_dir,
                temporary,
                filename,
            )
            catalog = await asyncio.to_thread(read_music_catalog, project_dir)
        except HTTPException:
            raise
        except MusicLibraryError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        finally:
            try:
                temporary.unlink()
            except OSError:
                pass
        _invalidate_summary(project_id)
        hub.publish(project_id)
        return {"track": track, "catalog": catalog}

    async def avatar_call(fn, project_id: str, *args, **kwargs) -> dict:
        project_dir = _safe_project_dir(project_id)
        try:
            result = await asyncio.to_thread(fn, project_dir, *args, **kwargs)
        except AvatarImportError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        _invalidate_summary(project_id)
        hub.publish(project_id)
        return result

    async def require_paid_avatar_confirmation(project_dir: Path, payload: dict) -> None:
        """RunningHub spends account credits, so API callers must opt in explicitly."""
        package = await asyncio.to_thread(read_avatar_package, project_dir)
        if (package or {}).get("generation_mode") == "runninghub_longcat" and payload.get("confirm_paid") is not True:
            raise HTTPException(status_code=422, detail="RunningHub 会消耗积分；请在确认弹窗中明确同意本次付费生成")

    async def receive_avatar_upload(
        project_id: str,
        request: Request,
        filename: str,
        *,
        turn_id: str | None = None,
        speaker_id: str | None = None,
    ) -> dict:
        project_dir = _safe_project_dir(project_id)
        try:
            temp_path, target_path = await asyncio.to_thread(
                prepare_upload,
                project_dir,
                filename,
                turn_id=turn_id,
                speaker_id=speaker_id,
            )
        except AvatarImportError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        received = 0
        try:
            with temp_path.open("wb") as output:
                async for chunk in request.stream():
                    received += len(chunk)
                    if received > MAX_UPLOAD_BYTES:
                        raise HTTPException(status_code=413, detail="单个数字人视频不能超过 2GB")
                    output.write(chunk)
            result = await asyncio.to_thread(
                finalize_upload,
                project_dir,
                temp_path,
                target_path,
                filename,
                turn_id=turn_id,
                speaker_id=speaker_id,
            )
        except HTTPException:
            raise
        except AvatarImportError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        finally:
            try:
                temp_path.unlink()
            except OSError:
                pass
        _invalidate_summary(project_id)
        hub.publish(project_id)
        return result

    async def receive_cloud_upload(
        project_id: str,
        request: Request,
        filename: str,
        *,
        kind: str,
        turn_id: str | None = None,
        speaker_id: str | None = None,
    ) -> dict:
        project_dir = _safe_project_dir(project_id)
        try:
            if kind == "presenter" and speaker_id:
                temporary, target = await asyncio.to_thread(prepare_presenter_upload, project_dir, speaker_id.lower(), filename)
                finalize = lambda: finalize_presenter_upload(project_dir, speaker_id.lower(), temporary, target, filename)
                limit = MAX_PRESENTER_IMAGE_BYTES
                limit_message = "项目出镜图不能超过 25MB"
            elif kind == "audio" and turn_id:
                temporary, target = await asyncio.to_thread(prepare_driving_audio_upload, project_dir, turn_id, filename)
                finalize = lambda: finalize_driving_audio_upload(project_dir, turn_id, temporary, target, filename)
                limit = MAX_DRIVING_AUDIO_BYTES
                limit_message = "单段驱动音频不能超过 15MB"
            else:
                raise AvatarCloudError("云端数字人上传类型不支持")
        except AvatarImportError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        received = 0
        try:
            with temporary.open("wb") as output:
                async for chunk in request.stream():
                    received += len(chunk)
                    if received > limit:
                        raise HTTPException(status_code=413, detail=limit_message)
                    output.write(chunk)
            result = await asyncio.to_thread(finalize)
        except HTTPException:
            raise
        except AvatarImportError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        finally:
            try:
                temporary.unlink()
            except OSError:
                pass
        _invalidate_summary(project_id)
        hub.publish(project_id)
        return result

    @app.get("/api/project/{project_id}/workbench")
    async def project_workbench(project_id: str) -> dict:
        project_dir = _safe_project_dir(project_id)
        return await asyncio.to_thread(read_workbench, project_dir)

    @app.get("/api/project/{project_id}/workbench/music")
    async def project_music_catalog(project_id: str) -> dict:
        project_dir = _safe_project_dir(project_id)
        try:
            return await asyncio.to_thread(read_music_catalog, project_dir)
        except WorkbenchError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/project/{project_id}/workbench/music/uploads")
    async def upload_project_music(
        project_id: str,
        request: Request,
        filename: str,
    ) -> dict:
        return await receive_project_music_upload(project_id, request, filename)

    @app.put("/api/project/{project_id}/workbench/music-policy")
    async def put_project_music_policy(project_id: str, payload: dict = Body(...)) -> dict:
        return await workbench_call(update_music_policy, project_id, payload)

    @app.put("/api/project/{project_id}/workbench/narration-policy")
    async def put_project_narration_policy(project_id: str, payload: dict = Body(...)) -> dict:
        return await workbench_call(update_narration_policy, project_id, payload)

    @app.get("/api/workbench/music-defaults")
    async def get_music_defaults() -> dict:
        return await asyncio.to_thread(read_music_preferences_settings)

    @app.put("/api/workbench/music-defaults")
    async def put_music_defaults(payload: dict = Body(...)) -> dict:
        return await asyncio.to_thread(update_music_preferences_settings, payload)

    @app.get("/api/workbench/narration-defaults")
    async def get_narration_defaults() -> dict:
        return await asyncio.to_thread(read_narration_preferences_settings)

    @app.put("/api/workbench/narration-defaults")
    async def put_narration_defaults(payload: dict = Body(...)) -> dict:
        return await asyncio.to_thread(update_narration_preferences_settings, payload)

    @app.get("/api/workbench/subtitle-defaults")
    async def get_subtitle_defaults() -> dict:
        return await asyncio.to_thread(read_subtitle_preferences_settings)

    @app.put("/api/workbench/subtitle-defaults")
    async def put_subtitle_defaults(payload: dict = Body(...)) -> dict:
        try:
            return await asyncio.to_thread(update_subtitle_preferences_settings, payload)
        except WorkbenchError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/project/{project_id}/workbench/music-sample/jobs")
    async def start_workbench_music_sample(project_id: str, payload: dict = Body(default={})) -> dict:
        """Queue the first-scene BGM audition without blocking the UI."""
        project_dir = _safe_project_dir(project_id)
        try:
            state = await asyncio.to_thread(start_music_sample, project_dir, payload)
        except WorkbenchError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        _invalidate_summary(project_id)
        hub.publish(project_id)

        async def run_generation() -> None:
            try:
                await asyncio.to_thread(generate_music_sample, project_dir)
            except Exception as exc:
                try:
                    await asyncio.to_thread(mark_music_sample_failed, project_dir, exc)
                except Exception:
                    pass
            _invalidate_summary(project_id)
            hub.publish(project_id)

        asyncio.create_task(run_generation())
        return state

    @app.post("/api/project/{project_id}/workbench/music-sample/approve")
    async def approve_workbench_music_sample(project_id: str, payload: dict = Body(...)) -> dict:
        return await workbench_call(approve_music_sample, project_id, payload)

    @app.get("/api/project/{project_id}/workbench/music/tracks/{track_id}")
    @app.get("/api/project/{project_id}/workbench/music/project-tracks/{track_id}")
    async def project_music_track(project_id: str, track_id: str) -> FileResponse:
        # Resolve the project first so an arbitrary id cannot turn this into a
        # repository-global media endpoint.
        project_dir = _safe_project_dir(project_id)
        try:
            path = await asyncio.to_thread(music_track_path, project_dir, track_id)
        except WorkbenchError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return FileResponse(path, filename=path.name)

    @app.post("/api/project/{project_id}/workbench/bootstrap")
    async def bootstrap_project_workbench(project_id: str) -> dict:
        return await workbench_call(bootstrap_workbench, project_id)

    @app.post("/api/project/{project_id}/workbench/avatar-script/template")
    async def import_workbench_avatar_script_template(project_id: str, payload: dict = Body(...)) -> dict:
        """Commit a preview-confirmed local template and prepare its avatar turns."""
        return await workbench_call(import_avatar_script_template, project_id, payload)

    @app.put("/api/project/{project_id}/workbench/avatar-script/imports/preview")
    async def preview_workbench_avatar_docx(project_id: str, request: Request, filename: str) -> dict:
        """Parse a user DOCX into a project-bound preview without changing the formal script."""
        project_dir = _safe_project_dir(project_id)
        received = bytearray()
        async for chunk in request.stream():
            received.extend(chunk)
            if len(received) > MAX_SCRIPT_IMPORT_BYTES:
                raise HTTPException(status_code=413, detail="Word 脚本不能超过 10 MB")
        try:
            return await asyncio.to_thread(stage_docx_preview, project_dir, bytes(received), filename=filename)
        except ScriptImportError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/project/{project_id}/workbench/avatar-script/imports/preview")
    async def preview_workbench_avatar_text(project_id: str, payload: dict = Body(...)) -> dict:
        """Parse pasted dialogue into the same deterministic preview contract."""
        project_dir = _safe_project_dir(project_id)
        try:
            return await asyncio.to_thread(
                stage_text_preview,
                project_dir,
                str(payload.get("text") or ""),
                title=str(payload.get("title") or ""),
            )
        except ScriptImportError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/project/{project_id}/workbench/avatar-script/imports/commit")
    async def commit_workbench_avatar_user_script(project_id: str, payload: dict = Body(...)) -> dict:
        """Commit exactly the previewed user script and initialize avatar preparation."""
        return await workbench_call(import_avatar_user_script, project_id, payload)

    @app.post("/api/project/{project_id}/workbench/avatar-package/initialize")
    async def initialize_workbench_avatar_package(project_id: str, payload: dict = Body(default={})) -> dict:
        await avatar_call(initialize_avatar_package, project_id, payload)
        project_dir = _safe_project_dir(project_id)
        return await asyncio.to_thread(read_workbench, project_dir)

    @app.get("/api/project/{project_id}/workbench/avatar-package/plans")
    async def list_workbench_avatar_plans(project_id: str) -> dict:
        project_dir = _safe_project_dir(project_id)
        return await asyncio.to_thread(list_avatar_source_plans, project_dir)

    @app.post("/api/project/{project_id}/workbench/avatar-package/plans/local-longform")
    async def switch_workbench_avatar_to_local_longform(project_id: str, payload: dict = Body(default={})) -> dict:
        await avatar_call(switch_to_local_longform_plan, project_id, payload)
        project_dir = _safe_project_dir(project_id)
        return await asyncio.to_thread(read_workbench, project_dir)

    @app.put("/api/project/{project_id}/workbench/avatar-package/turns/{turn_id}/file")
    async def upload_workbench_avatar_turn(project_id: str, turn_id: str, request: Request, filename: str) -> dict:
        await receive_avatar_upload(project_id, request, filename, turn_id=turn_id.upper())
        project_dir = _safe_project_dir(project_id)
        return await asyncio.to_thread(read_workbench, project_dir)

    @app.put("/api/project/{project_id}/workbench/avatar-package/speakers/{speaker_id}/file")
    async def upload_workbench_avatar_longform(project_id: str, speaker_id: str, request: Request, filename: str) -> dict:
        await receive_avatar_upload(project_id, request, filename, speaker_id=speaker_id.lower())
        project_dir = _safe_project_dir(project_id)
        return await asyncio.to_thread(read_workbench, project_dir)

    @app.put("/api/project/{project_id}/workbench/avatar-package/cloud/speakers/{speaker_id}/presenter/file")
    async def upload_workbench_avatar_presenter(project_id: str, speaker_id: str, request: Request, filename: str) -> dict:
        await receive_cloud_upload(project_id, request, filename, kind="presenter", speaker_id=speaker_id)
        project_dir = _safe_project_dir(project_id)
        return await asyncio.to_thread(read_workbench, project_dir)

    @app.put("/api/project/{project_id}/workbench/avatar-package/presenter/file", include_in_schema=False)
    async def upload_legacy_workbench_avatar_presenter(project_id: str, request: Request, filename: str) -> dict:
        """Keep one-speaker projects usable while refusing unsafe dialog guesses."""
        project_dir = _safe_project_dir(project_id)
        package = await asyncio.to_thread(read_avatar_package, project_dir)
        speakers = (package or {}).get("speakers") or []
        if len(speakers) != 1:
            raise HTTPException(status_code=422, detail="此项目包含多位说话人，请在对应角色卡片中分别上传实际出镜图")
        speaker_id = str(speakers[0].get("speaker_id") or "").lower()
        await receive_cloud_upload(project_id, request, filename, kind="presenter", speaker_id=speaker_id)
        return await asyncio.to_thread(read_workbench, project_dir)

    @app.put("/api/project/{project_id}/workbench/avatar-package/turns/{turn_id}/driving-audio/file")
    async def upload_workbench_avatar_driving_audio(project_id: str, turn_id: str, request: Request, filename: str) -> dict:
        await receive_cloud_upload(project_id, request, filename, kind="audio", turn_id=turn_id.upper())
        project_dir = _safe_project_dir(project_id)
        return await asyncio.to_thread(read_workbench, project_dir)

    @app.post("/api/project/{project_id}/workbench/avatar-package/turns/{turn_id}/driving-audio/voicebox/candidates/jobs")
    async def start_workbench_avatar_voicebox_driving_audio(project_id: str, turn_id: str, payload: dict = Body(default={})) -> dict:
        """Queue a local Voicebox take; it remains a candidate until adopted."""
        project_dir = _safe_project_dir(project_id)
        await avatar_call(start_voicebox_driving_audio_candidate, project_id, turn_id.upper(), payload)

        async def run_generation() -> None:
            try:
                await asyncio.to_thread(generate_voicebox_driving_audio_candidate, project_dir, turn_id.upper())
            except Exception as exc:
                try:
                    await asyncio.to_thread(mark_voicebox_driving_audio_candidate_failed, project_dir, turn_id.upper(), exc)
                except Exception:
                    pass
            _invalidate_summary(project_id)
            hub.publish(project_id)

        asyncio.create_task(run_generation())
        return await asyncio.to_thread(read_workbench, project_dir)

    @app.post("/api/project/{project_id}/workbench/avatar-package/turns/{turn_id}/driving-audio/voicebox/candidates/{candidate_id}/apply")
    async def apply_workbench_avatar_voicebox_driving_audio(project_id: str, turn_id: str, candidate_id: str) -> dict:
        await avatar_call(apply_voicebox_driving_audio_candidate, project_id, turn_id.upper(), candidate_id)
        project_dir = _safe_project_dir(project_id)
        return await asyncio.to_thread(read_workbench, project_dir)

    @app.post("/api/project/{project_id}/workbench/avatar-package/voicebox/mappings/refresh")
    async def refresh_workbench_avatar_voicebox_mappings(project_id: str) -> dict:
        """Re-run strict same-name routing against the shared Voicebox library."""
        await avatar_call(refresh_voicebox_speaker_mappings, project_id)
        project_dir = _safe_project_dir(project_id)
        return await asyncio.to_thread(read_workbench, project_dir)

    @app.post("/api/project/{project_id}/workbench/avatar-package/voicebox/speakers/{speaker_id}/mapping")
    async def set_workbench_avatar_voicebox_mapping(project_id: str, speaker_id: str, payload: dict = Body(default={})) -> dict:
        await avatar_call(set_voicebox_speaker_mapping, project_id, speaker_id.lower(), payload)
        project_dir = _safe_project_dir(project_id)
        return await asyncio.to_thread(read_workbench, project_dir)

    @app.post("/api/project/{project_id}/workbench/avatar-package/voicebox/batch/jobs")
    async def start_workbench_avatar_voicebox_batch(project_id: str, payload: dict = Body(default={})) -> dict:
        """Persist then asynchronously run a globally serial local Voicebox batch."""
        project_dir = _safe_project_dir(project_id)
        package = await avatar_call(start_voicebox_driving_audio_batch, project_id, payload)
        batch = ((package.get("voicebox") or {}).get("batch") or {}) if isinstance(package, dict) else {}
        batch_id = str(batch.get("batch_id") or "")
        if not batch_id:
            raise HTTPException(status_code=500, detail="批量配音任务创建后缺少任务编号")

        async def run_generation() -> None:
            try:
                await asyncio.to_thread(run_voicebox_driving_audio_batch, project_dir, batch_id)
            except Exception:
                # The runner writes each item outcome before continuing.  A
                # later retry remains safe even if the process itself stops.
                pass
            _invalidate_summary(project_id)
            hub.publish(project_id)

        asyncio.create_task(run_generation())
        return await asyncio.to_thread(read_workbench, project_dir)

    @app.post("/api/project/{project_id}/workbench/avatar-package/cloud/speakers/{speaker_id}/role")
    async def select_workbench_avatar_cloud_role(project_id: str, speaker_id: str, payload: dict = Body(...)) -> dict:
        role_id = str(payload.get("role_id") or "")
        await avatar_call(select_cloud_avatar_role, project_id, speaker_id.lower(), role_id)
        project_dir = _safe_project_dir(project_id)
        return await asyncio.to_thread(read_workbench, project_dir)

    @app.post("/api/project/{project_id}/workbench/avatar-package/cloud/render-spec")
    async def configure_workbench_avatar_cloud_render_spec(project_id: str, payload: dict = Body(...)) -> dict:
        """Save and locally preflight the actual picture submitted to DashScope.

        This endpoint never invokes the provider.  It may invalidate unsent
        samples because the aspect ratio/resolution is part of their immutable
        paid-task input contract.
        """
        await avatar_call(configure_cloud_render_spec, project_id, payload)
        project_dir = _safe_project_dir(project_id)
        return await asyncio.to_thread(read_workbench, project_dir)

    @app.post("/api/project/{project_id}/workbench/avatar-package/cloud/role", include_in_schema=False)
    async def select_legacy_workbench_avatar_cloud_role(project_id: str, payload: dict = Body(...)) -> dict:
        project_dir = _safe_project_dir(project_id)
        package = await asyncio.to_thread(read_avatar_package, project_dir)
        speakers = (package or {}).get("speakers") or []
        if len(speakers) != 1:
            raise HTTPException(status_code=422, detail="此项目包含多位说话人，请在对应角色卡片中分别选择角色")
        role_id = str(payload.get("role_id") or "")
        speaker_id = str(speakers[0].get("speaker_id") or "").lower()
        await avatar_call(select_cloud_avatar_role, project_id, speaker_id, role_id)
        return await asyncio.to_thread(read_workbench, project_dir)

    @app.post("/api/project/{project_id}/workbench/avatar-package/cloud/sample/jobs")
    async def start_workbench_avatar_cloud_sample(project_id: str, payload: dict = Body(default={})) -> dict:
        project_dir = _safe_project_dir(project_id)
        await require_paid_avatar_confirmation(project_dir, payload)
        try:
            requested_turn = str(payload.get("turn_id") or "").upper()
            if requested_turn:
                await asyncio.to_thread(queue_cloud_turn, project_dir, requested_turn, purpose="sample", force=bool(payload.get("force")))
                turn_ids = [requested_turn]
            else:
                _package, turn_ids = await asyncio.to_thread(queue_cloud_samples, project_dir, force=bool(payload.get("force")))
        except AvatarImportError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        _invalidate_summary(project_id)
        hub.publish(project_id)

        async def run_job() -> None:
            try:
                await asyncio.to_thread(run_cloud_batch, project_dir, turn_ids)
            except Exception as exc:
                if turn_ids:
                    await asyncio.to_thread(mark_cloud_turn_failed, project_dir, turn_ids[0], exc)
            _invalidate_summary(project_id)
            hub.publish(project_id)

        asyncio.create_task(run_job())
        return await asyncio.to_thread(read_workbench, project_dir)

    @app.post("/api/project/{project_id}/workbench/avatar-package/cloud/sample/approve")
    async def approve_workbench_avatar_cloud_sample(project_id: str, payload: dict = Body(default={})) -> dict:
        project_dir = _safe_project_dir(project_id)
        speaker_id = str(payload.get("speaker_id") or "").lower()
        if not speaker_id:
            package = await asyncio.to_thread(read_avatar_package, project_dir)
            speakers = (package or {}).get("speakers") or []
            if len(speakers) != 1:
                raise HTTPException(status_code=422, detail="请指定要确认试片的说话人")
            speaker_id = str(speakers[0].get("speaker_id") or "").lower()
        await avatar_call(approve_cloud_sample, project_id, speaker_id)
        return await asyncio.to_thread(read_workbench, project_dir)

    @app.post("/api/project/{project_id}/workbench/avatar-package/cloud/batch/jobs")
    async def start_workbench_avatar_cloud_batch(project_id: str, payload: dict = Body(default={})) -> dict:
        project_dir = _safe_project_dir(project_id)
        await require_paid_avatar_confirmation(project_dir, payload)
        try:
            _package, turn_ids = await asyncio.to_thread(queue_cloud_batch, project_dir)
        except AvatarImportError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        _invalidate_summary(project_id)
        hub.publish(project_id)

        async def run_job() -> None:
            try:
                await asyncio.to_thread(run_cloud_batch, project_dir, turn_ids)
            except Exception as exc:
                # ``run_cloud_batch`` normally persists the exact turn that
                # failed. This is only a final guard for an unexpected crash.
                await asyncio.to_thread(mark_cloud_turn_failed, project_dir, turn_ids[0], exc)
            _invalidate_summary(project_id)
            hub.publish(project_id)

        asyncio.create_task(run_job())
        return await asyncio.to_thread(read_workbench, project_dir)

    @app.post("/api/project/{project_id}/workbench/avatar-package/turns/{turn_id}/cloud/retry/jobs")
    async def retry_workbench_avatar_cloud_turn(project_id: str, turn_id: str, payload: dict = Body(default={})) -> dict:
        project_dir = _safe_project_dir(project_id)
        await require_paid_avatar_confirmation(project_dir, payload)
        try:
            await asyncio.to_thread(queue_cloud_turn, project_dir, turn_id.upper(), purpose="retry", force=True)
        except AvatarImportError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        _invalidate_summary(project_id)
        hub.publish(project_id)

        async def run_job() -> None:
            try:
                await asyncio.to_thread(run_cloud_turn, project_dir, turn_id.upper())
            except Exception as exc:
                await asyncio.to_thread(mark_cloud_turn_failed, project_dir, turn_id.upper(), exc)
            _invalidate_summary(project_id)
            hub.publish(project_id)

        asyncio.create_task(run_job())
        return await asyncio.to_thread(read_workbench, project_dir)

    @app.post("/api/project/{project_id}/workbench/avatar-package/turns/{turn_id}/cloud/resume/jobs")
    async def resume_workbench_avatar_cloud_turn(project_id: str, turn_id: str) -> dict:
        project_dir = _safe_project_dir(project_id)
        try:
            await asyncio.to_thread(assert_cloud_turn_resumable, project_dir, turn_id.upper())
        except AvatarImportError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        async def run_job() -> None:
            try:
                await asyncio.to_thread(run_cloud_turn, project_dir, turn_id.upper())
            except Exception as exc:
                await asyncio.to_thread(mark_cloud_turn_failed, project_dir, turn_id.upper(), exc)
            _invalidate_summary(project_id)
            hub.publish(project_id)

        asyncio.create_task(run_job())
        return await asyncio.to_thread(read_workbench, project_dir)

    @app.post("/api/project/{project_id}/workbench/avatar-package/validate")
    async def validate_workbench_avatar_package(project_id: str) -> dict:
        await avatar_call(validate_avatar_package, project_id)
        project_dir = _safe_project_dir(project_id)
        return await asyncio.to_thread(read_workbench, project_dir)

    @app.post("/api/project/{project_id}/workbench/avatar-package/asr/jobs")
    async def start_workbench_avatar_asr(project_id: str, payload: dict = Body(default={})) -> dict:
        project_dir = _safe_project_dir(project_id)
        try:
            await asyncio.to_thread(start_avatar_asr, project_dir, payload)
        except AvatarImportError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        _invalidate_summary(project_id)
        hub.publish(project_id)

        async def run_job() -> None:
            try:
                await asyncio.to_thread(run_avatar_asr, project_dir, payload)
            except Exception as exc:
                try:
                    await asyncio.to_thread(mark_avatar_job_failed, project_dir, "asr", exc)
                except Exception:
                    pass
            _invalidate_summary(project_id)
            hub.publish(project_id)

        asyncio.create_task(run_job())
        return await asyncio.to_thread(read_workbench, project_dir)

    @app.get("/api/project/{project_id}/workbench/avatar-package/asr/local-models")
    async def list_workbench_avatar_local_asr_models(project_id: str) -> dict:
        _safe_project_dir(project_id)
        return {"models": await asyncio.to_thread(list_local_whisper_models)}

    @app.post("/api/project/{project_id}/workbench/avatar-package/asr/speakers/{speaker_id}/candidates/jobs")
    async def start_workbench_avatar_speaker_diagnosis(project_id: str, speaker_id: str, payload: dict = Body(default={})) -> dict:
        project_dir = _safe_project_dir(project_id)
        try:
            await asyncio.to_thread(start_longform_speaker_diagnosis, project_dir, speaker_id.lower(), payload)
        except AvatarImportError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        _invalidate_summary(project_id)
        hub.publish(project_id)

        async def run_job() -> None:
            try:
                await asyncio.to_thread(run_longform_speaker_diagnosis, project_dir, speaker_id.lower(), payload)
            except Exception as exc:
                try:
                    await asyncio.to_thread(mark_longform_speaker_diagnosis_failed, project_dir, speaker_id.lower(), exc)
                except Exception:
                    pass
            _invalidate_summary(project_id)
            hub.publish(project_id)

        asyncio.create_task(run_job())
        return await asyncio.to_thread(read_workbench, project_dir)

    @app.post("/api/project/{project_id}/workbench/avatar-package/asr/speakers/{speaker_id}/candidates/{candidate_id}/apply")
    async def apply_workbench_avatar_speaker_diagnosis_candidate(project_id: str, speaker_id: str, candidate_id: str) -> dict:
        await avatar_call(apply_longform_speaker_candidate, project_id, speaker_id.lower(), candidate_id)
        project_dir = _safe_project_dir(project_id)
        return await asyncio.to_thread(read_workbench, project_dir)

    @app.post("/api/project/{project_id}/workbench/avatar-package/asr/speakers/{speaker_id}/candidates/{candidate_id}/realign/jobs")
    async def start_workbench_avatar_speaker_realign(project_id: str, speaker_id: str, candidate_id: str) -> dict:
        project_dir = _safe_project_dir(project_id)
        try:
            await asyncio.to_thread(start_longform_speaker_realign, project_dir, speaker_id.lower(), candidate_id)
        except AvatarImportError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        _invalidate_summary(project_id)
        hub.publish(project_id)

        async def run_job() -> None:
            try:
                await asyncio.to_thread(run_longform_speaker_realign, project_dir, speaker_id.lower())
            except Exception as exc:
                try:
                    await asyncio.to_thread(mark_longform_speaker_diagnosis_failed, project_dir, speaker_id.lower(), exc)
                except Exception:
                    pass
            _invalidate_summary(project_id)
            hub.publish(project_id)

        asyncio.create_task(run_job())
        return await asyncio.to_thread(read_workbench, project_dir)

    @app.patch("/api/project/{project_id}/workbench/avatar-package/cut-plan/items/{turn_id}")
    async def patch_workbench_avatar_longform_cut(project_id: str, turn_id: str, payload: dict = Body(...)) -> dict:
        await avatar_call(update_longform_cut, project_id, turn_id.upper(), payload)
        project_dir = _safe_project_dir(project_id)
        return await asyncio.to_thread(read_workbench, project_dir)

    @app.post("/api/project/{project_id}/workbench/avatar-package/cut-plan/items/{turn_id}/approve")
    async def approve_workbench_avatar_longform_cut(project_id: str, turn_id: str) -> dict:
        await avatar_call(approve_longform_cut, project_id, turn_id.upper())
        project_dir = _safe_project_dir(project_id)
        return await asyncio.to_thread(read_workbench, project_dir)

    @app.post("/api/project/{project_id}/workbench/avatar-package/cut-plan/approve-high-confidence")
    async def approve_workbench_avatar_high_confidence_cuts(project_id: str) -> dict:
        await avatar_call(approve_high_confidence_longform_cuts, project_id)
        project_dir = _safe_project_dir(project_id)
        return await asyncio.to_thread(read_workbench, project_dir)

    @app.post("/api/project/{project_id}/workbench/avatar-package/presentation")
    async def save_workbench_avatar_longform_presentation(project_id: str, payload: dict = Body(...)) -> dict:
        await avatar_call(update_longform_presentation, project_id, payload)
        project_dir = _safe_project_dir(project_id)
        return await asyncio.to_thread(read_workbench, project_dir)

    @app.post("/api/project/{project_id}/workbench/avatar-package/assembly/jobs")
    async def start_workbench_avatar_assembly(project_id: str, payload: dict = Body(default={})) -> dict:
        project_dir = _safe_project_dir(project_id)
        try:
            await asyncio.to_thread(start_avatar_assembly, project_dir, payload)
        except AvatarImportError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        _invalidate_summary(project_id)
        hub.publish(project_id)

        async def run_job() -> None:
            try:
                await asyncio.to_thread(assemble_avatar_package, project_dir, payload)
            except Exception as exc:
                try:
                    await asyncio.to_thread(mark_avatar_job_failed, project_dir, "assembly", exc)
                except Exception:
                    pass
            _invalidate_summary(project_id)
            hub.publish(project_id)

        asyncio.create_task(run_job())
        return await asyncio.to_thread(read_workbench, project_dir)

    @app.post("/api/project/{project_id}/workbench/avatar-package/apply")
    async def apply_workbench_avatar_package(project_id: str, payload: dict = Body(default={})) -> dict:
        """Commit an approved avatar master as the project's real timeline."""
        return await workbench_call(apply_avatar_package_to_timeline, project_id, payload)

    @app.post("/api/project/{project_id}/workbench/avatar-package/handoff/jobs")
    async def start_workbench_avatar_handoff(project_id: str, payload: dict = Body(default={})) -> dict:
        """Assemble a checked avatar package and apply its native timeline in one job."""
        project_dir = _safe_project_dir(project_id)
        try:
            await asyncio.to_thread(start_avatar_assembly, project_dir, payload)
        except AvatarImportError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        _invalidate_summary(project_id)
        hub.publish(project_id)

        async def run_job() -> None:
            try:
                await asyncio.to_thread(assemble_avatar_package, project_dir, payload)
                await asyncio.to_thread(apply_avatar_package_to_timeline, project_dir, payload)
            except Exception as exc:
                try:
                    await asyncio.to_thread(mark_avatar_job_failed, project_dir, "assembly", exc)
                except Exception:
                    pass
            _invalidate_summary(project_id)
            hub.publish(project_id)

        asyncio.create_task(run_job())
        return await asyncio.to_thread(read_workbench, project_dir)

    @app.post("/api/project/{project_id}/workbench/scenes/{scene_id}/avatar-keyframes")
    async def generate_workbench_avatar_scene_keyframes(project_id: str, scene_id: str) -> dict:
        return await workbench_call(generate_avatar_scene_keyframes, project_id, scene_id)

    @app.post("/api/project/{project_id}/workbench/scenes/{scene_id}/review-preview")
    async def generate_workbench_scene_review_preview(project_id: str, scene_id: str) -> dict:
        return await workbench_call(generate_scene_review_preview, project_id, scene_id)

    @app.post("/api/project/{project_id}/workbench/scenes/{scene_id}/surgical-directives")
    async def post_workbench_surgical_directive(project_id: str, scene_id: str, payload: dict = Body(...)) -> dict:
        return await workbench_call(add_surgical_directive, project_id, scene_id, payload)

    @app.delete("/api/project/{project_id}/workbench/scenes/{scene_id}/surgical-directives/{directive_id}")
    async def delete_workbench_surgical_directive(project_id: str, scene_id: str, directive_id: str) -> dict:
        return await workbench_call(remove_surgical_directive, project_id, scene_id, directive_id)

    @app.patch("/api/project/{project_id}/workbench/scenes/{scene_id}")
    async def patch_workbench_scene(project_id: str, scene_id: str, payload: dict = Body(...)) -> dict:
        return await workbench_call(update_scene, project_id, scene_id, payload)

    @app.put("/api/project/{project_id}/workbench/scenes/{scene_id}/subtitles")
    async def put_workbench_scene_subtitles(project_id: str, scene_id: str, payload: dict = Body(...)) -> dict:
        return await workbench_call(update_scene_subtitles, project_id, scene_id, payload)

    @app.put("/api/project/{project_id}/workbench/scenes/{scene_id}/visual-plan")
    async def put_workbench_scene_visual_plan(project_id: str, scene_id: str, payload: dict = Body(...)) -> dict:
        return await workbench_call(update_scene_visual_plan, project_id, scene_id, payload)

    @app.post("/api/project/{project_id}/workbench/scenes/{scene_id}/visual-copy/refine")
    async def post_workbench_scene_visual_copy_refine(project_id: str, scene_id: str, payload: dict = Body(default={})) -> dict:
        return await workbench_call(refine_scene_visual_copy, project_id, scene_id, payload)

    @app.put("/api/project/{project_id}/workbench/scenes/{scene_id}/ppt-card-brief")
    async def put_workbench_scene_ppt_card_brief(project_id: str, scene_id: str, payload: dict = Body(...)) -> dict:
        return await workbench_call(update_scene_ppt_card_brief, project_id, scene_id, payload)

    @app.get("/api/project/{project_id}/workbench/tasks")
    async def get_workbench_task_center(project_id: str) -> dict:
        """A small polling surface that never reloads the review player."""
        project_dir = _safe_project_dir(project_id)
        try:
            return await asyncio.to_thread(read_task_center, project_dir)
        except WorkbenchError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/project/{project_id}/workbench/scenes/{scene_id}/ppt-cards/jobs")
    async def start_workbench_scene_ppt_card(project_id: str, scene_id: str, payload: dict = Body(...)) -> dict:
        project_dir = _safe_project_dir(project_id)
        try:
            state = await asyncio.to_thread(start_scene_ppt_card_generation, project_dir, scene_id, payload)
        except WorkbenchError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        _invalidate_summary(project_id)
        hub.publish(project_id)
        scene = next((item for item in state.get("scenes", []) if item.get("id") == scene_id), {})
        job_id = str((scene.get("ppt_card_generation") or {}).get("job_id") or "")

        async def run_generation() -> None:
            try:
                await asyncio.to_thread(generate_scene_ppt_card, project_dir, scene_id, job_id)
            except Exception as exc:
                try:
                    await asyncio.to_thread(mark_scene_ppt_card_failed, project_dir, scene_id, job_id, exc)
                except Exception:
                    pass
            _invalidate_summary(project_id)
            hub.publish(project_id)

        asyncio.create_task(run_generation())
        return state

    @app.post("/api/project/{project_id}/workbench/scenes/{scene_id}/ppt-cards/jobs/current/retry")
    async def retry_workbench_scene_ppt_card(project_id: str, scene_id: str, payload: dict = Body(...)) -> dict:
        project_dir = _safe_project_dir(project_id)
        try:
            state = await asyncio.to_thread(retry_scene_ppt_card_generation, project_dir, scene_id, str(payload.get("job_id") or ""))
        except WorkbenchError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        _invalidate_summary(project_id)
        hub.publish(project_id)
        scene = next((item for item in state.get("scenes", []) if item.get("id") == scene_id), {})
        next_job_id = str((scene.get("ppt_card_generation") or {}).get("job_id") or "")

        async def run_generation() -> None:
            try:
                await asyncio.to_thread(generate_scene_ppt_card, project_dir, scene_id, next_job_id)
            except Exception as exc:
                try:
                    await asyncio.to_thread(mark_scene_ppt_card_failed, project_dir, scene_id, next_job_id, exc)
                except Exception:
                    pass
            _invalidate_summary(project_id)
            hub.publish(project_id)

        asyncio.create_task(run_generation())
        return state

    @app.put("/api/project/{project_id}/workbench/scenes/{scene_id}/visual-timeline")
    async def put_workbench_scene_visual_timeline(project_id: str, scene_id: str, payload: dict = Body(...)) -> dict:
        return await workbench_call(update_scene_visual_timeline, project_id, scene_id, payload)

    @app.patch("/api/project/{project_id}/workbench/scenes/{scene_id}/visual-blocks/{block_id}")
    async def patch_workbench_visual_block(project_id: str, scene_id: str, block_id: str, payload: dict = Body(...)) -> dict:
        return await workbench_call(update_visual_block_lock, project_id, scene_id, block_id, payload)

    @app.get("/api/project/{project_id}/workbench/automation/review-preview/preflight")
    async def get_workbench_review_preview_preflight(
        project_id: str,
        planning_mode: str = "ai_director",
    ) -> dict:
        project_dir = _safe_project_dir(project_id)
        try:
            if planning_mode not in {"ai_director", "rule_mix"}:
                raise ReviewPreviewError("画面规划方式只能是 AI 智能导演或规则混合")
            result = await asyncio.to_thread(
                review_preview_preflight,
                project_dir,
                {"visual": {"planning_mode": planning_mode}},
            )
        except (ReviewPreviewConflict, StaleReviewPreviewWorker) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ReviewPreviewError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return _public_review_preview_payload(result)

    @app.post("/api/project/{project_id}/workbench/automation/review-preview/jobs")
    async def start_workbench_review_preview_job(project_id: str, payload: dict = Body(...)) -> dict:
        project_dir = _safe_project_dir(project_id)
        try:
            _validate_review_preview_request(
                payload,
                allowed={"confirmed", "network_confirmed", "text_ai_confirmed", "visual"},
            )
            state = await asyncio.to_thread(start_review_preview_job, project_dir, payload)
        except (ReviewPreviewConflict, StaleReviewPreviewWorker) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ReviewPreviewError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        _invalidate_summary(project_id)
        hub.publish(project_id)
        job_id = _review_preview_job_id(state)
        if state.get("launch_required") is True:
            if not job_id:
                raise HTTPException(status_code=500, detail="一键审核预览任务已建立，但缺少任务编号")
            _launch_review_preview_worker(app, project_dir, job_id)
        return _public_review_preview_payload(state)

    @app.get("/api/project/{project_id}/workbench/automation/review-preview/jobs/current")
    async def get_workbench_review_preview_job(project_id: str) -> dict:
        project_dir = _safe_project_dir(project_id)
        try:
            state = await asyncio.to_thread(read_review_preview_job, project_dir)
        except (ReviewPreviewConflict, StaleReviewPreviewWorker) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ReviewPreviewError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return _public_review_preview_payload(state)

    @app.post("/api/project/{project_id}/workbench/automation/review-preview/jobs/{job_id}/resume")
    async def resume_workbench_review_preview_job(
        project_id: str,
        job_id: str,
        payload: dict = Body(default={}),
    ) -> dict:
        project_dir = _safe_project_dir(project_id)
        try:
            _validate_review_preview_request(
                payload,
                allowed={"job_id", "confirmed", "external_state_confirmed", "safe_to_retry"},
            )
            body_job_id = str(payload.get("job_id") or "")
            if body_job_id and body_job_id != job_id:
                raise ReviewPreviewConflict("请求中的任务编号与地址不一致，拒绝恢复")
            resume_payload = {key: value for key, value in payload.items() if key != "job_id"}
            state = await asyncio.to_thread(resume_review_preview_job, project_dir, job_id, resume_payload)
        except (ReviewPreviewConflict, StaleReviewPreviewWorker) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ReviewPreviewError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        _invalidate_summary(project_id)
        hub.publish(project_id)
        next_job_id = _review_preview_job_id(state)
        if state.get("launch_required") is True:
            if not next_job_id:
                raise HTTPException(status_code=500, detail="一键审核预览任务恢复后缺少任务编号")
            _launch_review_preview_worker(app, project_dir, next_job_id)
        return _public_review_preview_payload(state)

    @app.get("/api/project/{project_id}/workbench/automation/avatar-review-preview/preflight")
    async def get_workbench_avatar_review_preview_preflight(
        project_id: str,
        planning_mode: str = "ai_director",
        budget_limit_cny: float = 5.0,
        allow_plus_on_oom: bool = False,
    ) -> dict:
        project_dir = _safe_project_dir(project_id)
        try:
            if planning_mode not in {"ai_director", "rule_mix"}:
                raise AvatarReviewPreviewError("画面规划方式只能是 AI 智能导演或规则混合")
            result = await asyncio.to_thread(
                avatar_review_preview_preflight,
                project_dir,
                {
                    "visual": {"planning_mode": planning_mode},
                    "budget_limit_cny": budget_limit_cny,
                    "allow_plus_on_oom": allow_plus_on_oom,
                },
            )
        except (AvatarReviewPreviewConflict, StaleAvatarReviewPreviewWorker) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except AvatarReviewPreviewError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return _public_review_preview_payload(result)

    @app.post("/api/project/{project_id}/workbench/automation/avatar-review-preview/jobs")
    async def start_workbench_avatar_review_preview_job(project_id: str, payload: dict = Body(...)) -> dict:
        project_dir = _safe_project_dir(project_id)
        try:
            _validate_review_preview_request(
                payload,
                allowed={"confirmed", "budget_limit_cny", "allow_plus_on_oom", "visual"},
            )
            state = await asyncio.to_thread(start_avatar_review_preview_job, project_dir, payload)
        except (AvatarReviewPreviewConflict, StaleAvatarReviewPreviewWorker) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (AvatarReviewPreviewError, ReviewPreviewError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        _invalidate_summary(project_id)
        hub.publish(project_id)
        job_id = _review_preview_job_id(state)
        if state.get("launch_required") is True:
            if not job_id:
                raise HTTPException(status_code=500, detail="有数字人一键审核预览任务已建立，但缺少任务编号")
            _launch_avatar_review_preview_worker(app, project_dir, job_id)
        return _public_review_preview_payload(state)

    @app.get("/api/project/{project_id}/workbench/automation/avatar-review-preview/jobs/current")
    async def get_workbench_avatar_review_preview_job(project_id: str) -> dict:
        project_dir = _safe_project_dir(project_id)
        try:
            state = await asyncio.to_thread(read_avatar_review_preview_job, project_dir)
        except (AvatarReviewPreviewConflict, StaleAvatarReviewPreviewWorker) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except AvatarReviewPreviewError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return _public_review_preview_payload(state)

    @app.post("/api/project/{project_id}/workbench/automation/avatar-review-preview/jobs/{job_id}/resume")
    async def resume_workbench_avatar_review_preview_job(
        project_id: str,
        job_id: str,
        payload: dict = Body(default={}),
    ) -> dict:
        project_dir = _safe_project_dir(project_id)
        try:
            _validate_review_preview_request(payload, allowed={"job_id", "confirmed"})
            body_job_id = str(payload.get("job_id") or "")
            if body_job_id and body_job_id != job_id:
                raise AvatarReviewPreviewConflict("请求中的任务编号与地址不一致，拒绝恢复")
            resume_payload = {key: value for key, value in payload.items() if key != "job_id"}
            state = await asyncio.to_thread(resume_avatar_review_preview_job, project_dir, job_id, resume_payload)
        except (AvatarReviewPreviewConflict, StaleAvatarReviewPreviewWorker, AmbiguousAvatarOperation) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (AvatarReviewPreviewError, ReviewPreviewError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        _invalidate_summary(project_id)
        hub.publish(project_id)
        next_job_id = _review_preview_job_id(state)
        if state.get("launch_required") is True:
            if not next_job_id:
                raise HTTPException(status_code=500, detail="有数字人一键任务恢复后缺少任务编号")
            _launch_avatar_review_preview_worker(app, project_dir, next_job_id)
        return _public_review_preview_payload(state)

    @app.post("/api/project/{project_id}/workbench/review-previews/jobs")
    async def start_workbench_review_preview_sync(project_id: str, payload: dict = Body(...)) -> dict:
        project_dir = _safe_project_dir(project_id)
        try:
            state = await asyncio.to_thread(start_review_preview_sync, project_dir, payload)
        except WorkbenchError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        _invalidate_summary(project_id)
        hub.publish(project_id)
        job_id = str(((state.get("automation") or {}).get("preview_sync") or {}).get("job_id") or "")

        async def run_generation() -> None:
            try:
                await asyncio.to_thread(generate_review_preview_sync, project_dir, job_id)
            except Exception as exc:
                try:
                    await asyncio.to_thread(mark_review_preview_sync_failed, project_dir, exc, job_id)
                except Exception:
                    pass
            _invalidate_summary(project_id)
            hub.publish(project_id)

        asyncio.create_task(run_generation())
        return state

    @app.get("/api/project/{project_id}/workbench/review-previews/jobs/current")
    async def get_workbench_review_preview_sync(project_id: str) -> dict:
        project_dir = _safe_project_dir(project_id)
        try:
            return await asyncio.to_thread(read_review_preview_sync, project_dir)
        except WorkbenchError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/project/{project_id}/workbench/visual-batch/preview")
    async def preview_workbench_visual_batch(project_id: str, payload: dict = Body(...)) -> dict:
        project_dir = _safe_project_dir(project_id)
        try:
            return await asyncio.to_thread(preview_visual_batch_plan, project_dir, payload)
        except WorkbenchError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/project/{project_id}/workbench/visual-batch/jobs")
    async def start_workbench_visual_batch(project_id: str, payload: dict = Body(...)) -> dict:
        project_dir = _safe_project_dir(project_id)
        try:
            state = await asyncio.to_thread(start_visual_batch_generation, project_dir, payload)
        except WorkbenchError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        _invalidate_summary(project_id)
        hub.publish(project_id)
        job_id = str(((state.get("automation") or {}).get("visual_batch") or {}).get("job_id") or "")

        async def run_generation() -> None:
            try:
                await asyncio.to_thread(generate_visual_batch, project_dir, job_id)
            except Exception as exc:
                try:
                    await asyncio.to_thread(mark_visual_batch_failed, project_dir, exc, job_id)
                except Exception:
                    pass
            _invalidate_summary(project_id)
            hub.publish(project_id)

        asyncio.create_task(run_generation())
        return state

    @app.get("/api/project/{project_id}/workbench/visual-batch/jobs/current")
    async def get_workbench_visual_batch(project_id: str) -> dict:
        project_dir = _safe_project_dir(project_id)
        try:
            return await asyncio.to_thread(read_visual_batch_generation, project_dir)
        except WorkbenchError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/project/{project_id}/workbench/scenes/{scene_id}/visual-blocks/{block_id}/refresh/jobs")
    async def refresh_workbench_visual_block(project_id: str, scene_id: str, block_id: str, payload: dict = Body(...)) -> dict:
        project_dir = _safe_project_dir(project_id)
        try:
            state = await asyncio.to_thread(start_visual_block_refresh, project_dir, scene_id, block_id, payload)
        except WorkbenchError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        _invalidate_summary(project_id)
        hub.publish(project_id)
        job_id = str(((state.get("automation") or {}).get("visual_batch") or {}).get("job_id") or "")

        async def run_generation() -> None:
            try:
                await asyncio.to_thread(generate_visual_batch, project_dir, job_id)
            except Exception as exc:
                try:
                    await asyncio.to_thread(mark_visual_batch_failed, project_dir, exc, job_id)
                except Exception:
                    pass
            _invalidate_summary(project_id)
            hub.publish(project_id)

        asyncio.create_task(run_generation())
        return state

    @app.post("/api/project/{project_id}/workbench/scenes/{scene_id}/motion-visual/jobs")
    async def start_workbench_scene_motion_visual(project_id: str, scene_id: str, payload: dict = Body(default={})) -> dict:
        project_dir = _safe_project_dir(project_id)
        try:
            state = await asyncio.to_thread(start_scene_motion_visual_generation, project_dir, scene_id, payload)
        except WorkbenchError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        _invalidate_summary(project_id)
        hub.publish(project_id)

        async def run_generation() -> None:
            try:
                await asyncio.to_thread(generate_scene_motion_visual, project_dir, scene_id)
            except Exception as exc:
                try:
                    await asyncio.to_thread(mark_scene_motion_visual_failed, project_dir, scene_id, exc)
                except Exception:
                    pass
            _invalidate_summary(project_id)
            hub.publish(project_id)

        asyncio.create_task(run_generation())
        return state

    @app.post("/api/project/{project_id}/workbench/presenter-layouts")
    async def save_workbench_presenter_layout(project_id: str, payload: dict = Body(...)) -> dict:
        return await workbench_call(update_presenter_layout_template, project_id, payload)

    @app.post("/api/project/{project_id}/workbench/subtitle-styles")
    async def save_workbench_subtitle_style(project_id: str, payload: dict = Body(...)) -> dict:
        return await workbench_call(update_subtitle_style_template, project_id, payload)

    @app.post("/api/project/{project_id}/workbench/presenter-layouts/apply-selected")
    async def apply_workbench_presenter_layout_to_selected(project_id: str, payload: dict = Body(...)) -> dict:
        """Apply a presenter layout and immediately queue only local preview refreshes."""
        project_dir = _safe_project_dir(project_id)
        try:
            await asyncio.to_thread(apply_presenter_layout_to_selected_scenes, project_dir, payload)
            source_scene_id = str(payload.get("source_scene_id") or "")
            scene_ids = [str(value) for value in (payload.get("target_scene_ids") or []) if str(value) and str(value) != source_scene_id]
            state = await asyncio.to_thread(start_review_preview_sync, project_dir, {
                "confirmed": True,
                "selection_mode": "custom",
                "scene_ids": scene_ids,
            })
        except WorkbenchError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        _invalidate_summary(project_id)
        hub.publish(project_id)
        job_id = str(((state.get("automation") or {}).get("preview_sync") or {}).get("job_id") or "")

        async def run_generation() -> None:
            try:
                await asyncio.to_thread(generate_review_preview_sync, project_dir, job_id)
            except Exception as exc:
                try:
                    await asyncio.to_thread(mark_review_preview_sync_failed, project_dir, exc, job_id)
                except Exception:
                    pass
            _invalidate_summary(project_id)
            hub.publish(project_id)

        asyncio.create_task(run_generation())
        return state

    @app.post("/api/project/{project_id}/workbench/annotations")
    async def post_workbench_annotation(project_id: str, payload: dict = Body(...)) -> dict:
        return await workbench_call(add_annotation, project_id, payload)

    @app.post("/api/project/{project_id}/workbench/assets")
    async def post_workbench_asset(project_id: str, payload: dict = Body(...)) -> dict:
        return await workbench_call(add_asset, project_id, payload)

    @app.get("/api/project/{project_id}/workbench/asset-library/audit")
    async def get_workbench_asset_library_audit(project_id: str) -> dict:
        project_dir = _safe_project_dir(project_id)
        try:
            return await asyncio.to_thread(audit_asset_library, project_dir)
        except WorkbenchError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/project/{project_id}/workbench/asset-library/cleanup")
    async def post_workbench_asset_library_cleanup(project_id: str, payload: dict = Body(...)) -> dict:
        return await workbench_call(cleanup_unused_assets, project_id, payload)

    @app.post("/api/project/{project_id}/workbench/asset-library/assets/{asset_id}/restore")
    async def post_workbench_asset_library_restore(project_id: str, asset_id: str) -> dict:
        return await workbench_call(restore_trashed_asset, project_id, asset_id)

    @app.post("/api/project/{project_id}/workbench/openai-images")
    async def post_workbench_openai_images(project_id: str, payload: dict = Body(...)) -> dict:
        return await workbench_call(generate_openai_images, project_id, payload)

    @app.post("/api/project/{project_id}/workbench/scenes/{scene_id}/keyframes")
    async def post_workbench_scene_keyframes(project_id: str, scene_id: str, payload: dict = Body(...)) -> dict:
        return await workbench_call(generate_scene_keyframes, project_id, scene_id, payload)

    @app.post("/api/project/{project_id}/workbench/scenes/{scene_id}/keyframes/jobs")
    async def start_workbench_scene_keyframes(project_id: str, scene_id: str, payload: dict = Body(...)) -> dict:
        project_dir = _safe_project_dir(project_id)
        try:
            state = await asyncio.to_thread(start_scene_keyframe_generation, project_dir, scene_id, payload)
        except WorkbenchError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        _invalidate_summary(project_id)
        hub.publish(project_id)

        async def run_generation() -> None:
            # One worker call per anchor means a completed first frame is
            # committed before the second provider request begins.  Publish a
            # semantic wake-up after each unit; the browser's job card decides
            # whether it needs to load the new full review state.
            while True:
                try:
                    await asyncio.to_thread(generate_scene_keyframes, project_dir, scene_id, {**payload, "_single_anchor": True})
                except Exception as exc:
                    try:
                        await asyncio.to_thread(mark_scene_keyframe_generation_failed, project_dir, scene_id, exc)
                    except Exception:
                        # The original error remains visible in the server log;
                        # a best-effort state write must not crash the event loop.
                        pass
                _invalidate_summary(project_id)
                hub.publish(project_id)
                try:
                    task = await asyncio.to_thread(read_scene_keyframe_generation, project_dir, scene_id)
                except Exception:
                    return
                generation = task.get("generation") if isinstance(task, dict) else None
                if not isinstance(generation, dict) or generation.get("status") != "generating":
                    return
                anchors = generation.get("anchors") if isinstance(generation.get("anchors"), dict) else {}
                if not any(isinstance(item, dict) and item.get("status") in {"queued", "generating"} for item in anchors.values()):
                    return

        asyncio.create_task(run_generation())
        return state

    @app.post("/api/project/{project_id}/workbench/scenes/{scene_id}/keyframes/jobs/current/retry")
    async def retry_workbench_scene_keyframes(project_id: str, scene_id: str, payload: dict = Body(...)) -> dict:
        project_dir = _safe_project_dir(project_id)
        retry_payload = {**payload, "confirmed": True, "resume_failed": True}
        try:
            state = await asyncio.to_thread(start_scene_keyframe_generation, project_dir, scene_id, retry_payload)
        except WorkbenchError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        _invalidate_summary(project_id)
        hub.publish(project_id)

        async def run_retry() -> None:
            while True:
                try:
                    await asyncio.to_thread(generate_scene_keyframes, project_dir, scene_id, {**retry_payload, "_single_anchor": True})
                except Exception as exc:
                    try:
                        await asyncio.to_thread(mark_scene_keyframe_generation_failed, project_dir, scene_id, exc)
                    except Exception:
                        pass
                _invalidate_summary(project_id)
                hub.publish(project_id)
                task = await asyncio.to_thread(read_scene_keyframe_generation, project_dir, scene_id)
                generation = task.get("generation") if isinstance(task, dict) else None
                anchors = generation.get("anchors") if isinstance((generation or {}).get("anchors"), dict) else {}
                if not isinstance(generation, dict) or generation.get("status") != "generating" or not any(isinstance(item, dict) and item.get("status") in {"queued", "generating"} for item in anchors.values()):
                    return

        asyncio.create_task(run_retry())
        return state

    @app.get("/api/project/{project_id}/workbench/scenes/{scene_id}/keyframes/jobs/current")
    async def get_workbench_scene_keyframe_job(project_id: str, scene_id: str) -> dict:
        """Small polling surface for the task card; never forces a board redraw."""
        project_dir = _safe_project_dir(project_id)
        try:
            return await asyncio.to_thread(read_scene_keyframe_generation, project_dir, scene_id)
        except WorkbenchError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/project/{project_id}/workbench/scenes/{scene_id}/keyframes/review")
    async def review_workbench_scene_keyframes(project_id: str, scene_id: str, payload: dict = Body(...)) -> dict:
        return await workbench_call(review_scene_keyframes, project_id, scene_id, payload)

    @app.post("/api/project/{project_id}/workbench/scenes/{scene_id}/ai-visual/adopt")
    async def adopt_workbench_ai_scene_visual(project_id: str, scene_id: str) -> dict:
        return await workbench_call(adopt_ai_scene_visual, project_id, scene_id)

    @app.post("/api/project/{project_id}/workbench/automation/network-assets/jobs")
    async def start_workbench_network_assets(project_id: str, payload: dict = Body(...)) -> dict:
        """Start the free Pexels material pass and immediately return progress state."""
        project_dir = _safe_project_dir(project_id)
        try:
            state = await asyncio.to_thread(start_network_asset_generation, project_dir, payload)
        except WorkbenchError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        _invalidate_summary(project_id)
        hub.publish(project_id)

        async def run_generation() -> None:
            try:
                await asyncio.to_thread(generate_network_assets, project_dir)
            except Exception as exc:
                try:
                    await asyncio.to_thread(mark_network_asset_generation_failed, project_dir, exc)
                except Exception:
                    pass
            _invalidate_summary(project_id)
            hub.publish(project_id)

        asyncio.create_task(run_generation())
        return state

    @app.post("/api/project/{project_id}/workbench/scenes/{scene_id}/network-assets/jobs")
    async def refresh_workbench_scene_network_assets(project_id: str, scene_id: str, payload: dict = Body(...)) -> dict:
        """Replace only one scene's Pexels candidate, retaining all old ledger records."""
        project_dir = _safe_project_dir(project_id)
        try:
            state = await asyncio.to_thread(start_scene_network_asset_refresh, project_dir, scene_id, payload)
        except WorkbenchError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        _invalidate_summary(project_id)
        hub.publish(project_id)

        async def run_generation() -> None:
            try:
                await asyncio.to_thread(generate_network_assets, project_dir)
            except Exception as exc:
                try:
                    await asyncio.to_thread(mark_network_asset_generation_failed, project_dir, exc)
                except Exception:
                    pass
            _invalidate_summary(project_id)
            hub.publish(project_id)

        asyncio.create_task(run_generation())
        return state

    @app.post("/api/project/{project_id}/workbench/automation/finalize/jobs")
    async def start_workbench_final_generation(project_id: str, payload: dict = Body(...)) -> dict:
        """Compatibility route: create project narration, then wait for render consent."""
        project_dir = _safe_project_dir(project_id)
        try:
            state = await asyncio.to_thread(start_auto_final_generation, project_dir, payload)
        except WorkbenchError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        _invalidate_summary(project_id)
        hub.publish(project_id)

        async def run_generation() -> None:
            try:
                await asyncio.to_thread(generate_auto_final_video, project_dir)
            except Exception as exc:
                try:
                    await asyncio.to_thread(mark_auto_final_generation_failed, project_dir, exc)
                except Exception:
                    pass
            _invalidate_summary(project_id)
            hub.publish(project_id)

        asyncio.create_task(run_generation())
        return state

    @app.post("/api/project/{project_id}/workbench/automation/narration/jobs")
    async def start_workbench_narration(project_id: str, payload: dict = Body(...)) -> dict:
        """Generate project-local narration from the software-wide default voice."""
        project_dir = _safe_project_dir(project_id)
        try:
            state = await asyncio.to_thread(start_project_narration, project_dir, payload)
        except WorkbenchError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        _invalidate_summary(project_id)
        hub.publish(project_id)

        async def run_generation() -> None:
            try:
                await asyncio.to_thread(generate_project_narration, project_dir)
            except Exception as exc:
                try:
                    await asyncio.to_thread(mark_project_narration_failed, project_dir, exc)
                except Exception:
                    pass
            _invalidate_summary(project_id)
            hub.publish(project_id)

        asyncio.create_task(run_generation())
        return state

    @app.post("/api/project/{project_id}/workbench/automation/video-render/jobs")
    async def start_workbench_video_render(project_id: str, payload: dict = Body(...)) -> dict:
        """Render video only after the separately generated project narration is ready."""
        project_dir = _safe_project_dir(project_id)
        try:
            state = await asyncio.to_thread(start_project_video_render, project_dir, payload)
        except WorkbenchError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        _invalidate_summary(project_id)
        hub.publish(project_id)

        async def run_generation() -> None:
            try:
                await asyncio.to_thread(generate_project_video_render, project_dir)
            except Exception as exc:
                try:
                    await asyncio.to_thread(mark_project_video_render_failed, project_dir, exc)
                except Exception:
                    pass
            _invalidate_summary(project_id)
            hub.publish(project_id)

        asyncio.create_task(run_generation())
        return state

    @app.post("/api/project/{project_id}/workbench/automation/full-preview/jobs")
    async def start_workbench_full_preview(project_id: str, payload: dict = Body(...)) -> dict:
        """Render a review candidate without mutating scene approval state."""
        project_dir = _safe_project_dir(project_id)
        try:
            state = await asyncio.to_thread(start_full_preview_render, project_dir, payload)
        except WorkbenchError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        _invalidate_summary(project_id)
        hub.publish(project_id)

        async def run_generation() -> None:
            try:
                await asyncio.to_thread(generate_full_preview_render, project_dir)
            except Exception as exc:
                try:
                    await asyncio.to_thread(mark_full_preview_render_failed, project_dir, exc)
                except Exception:
                    pass
            _invalidate_summary(project_id)
            hub.publish(project_id)

        asyncio.create_task(run_generation())
        return state

    @app.post("/api/project/{project_id}/workbench/review/full-preview/approve")
    async def approve_workbench_full_preview(project_id: str, payload: dict = Body(...)) -> dict:
        return await workbench_call(approve_full_preview_scenes, project_id, payload)

    @app.patch("/api/project/{project_id}/workbench/intake")
    async def patch_workbench_intake(project_id: str, payload: dict = Body(...)) -> dict:
        return await workbench_call(update_intake, project_id, payload)

    @app.post("/api/project/{project_id}/workbench/script-draft")
    async def post_workbench_script_draft(project_id: str, payload: dict = Body(...)) -> dict:
        return await workbench_call(generate_script_draft, project_id, payload)

    @app.patch("/api/project/{project_id}/workbench/script-draft/content")
    async def patch_workbench_script_draft_content(project_id: str, payload: dict = Body(...)) -> dict:
        return await workbench_call(update_script_draft_content, project_id, payload)

    @app.post("/api/project/{project_id}/workbench/script-draft/review")
    async def review_workbench_script_draft(project_id: str, payload: dict = Body(...)) -> dict:
        return await workbench_call(review_script_draft, project_id, payload)

    @app.post("/api/project/{project_id}/workbench/script-draft/reopen")
    async def reopen_workbench_script_draft(project_id: str, payload: dict = Body(...)) -> dict:
        return await workbench_call(reopen_script_draft, project_id, payload)

    @app.post("/api/project/{project_id}/workbench/scene-plan")
    async def post_workbench_scene_plan(project_id: str) -> dict:
        return await workbench_call(generate_scene_plan_from_script, project_id)

    @app.post("/api/project/{project_id}/workbench/usages")
    async def post_workbench_usage(project_id: str, payload: dict = Body(...)) -> dict:
        return await workbench_call(assign_usage, project_id, payload)

    @app.get("/api/project/{project_id}/workbench/voices")
    async def get_workbench_voices(project_id: str) -> dict:
        """Read shared Voicebox profiles without attaching a voice to this project."""
        _safe_project_dir(project_id)
        return await asyncio.to_thread(voice_catalog)

    @app.post("/api/project/{project_id}/workbench/scenes/{scene_id}/narration/candidates/jobs")
    async def start_scene_narration_job(project_id: str, scene_id: str, payload: dict = Body(...)) -> dict:
        project_dir = _safe_project_dir(project_id)
        try:
            state = await asyncio.to_thread(start_scene_narration_candidate, project_dir, scene_id, payload)
        except WorkbenchError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        _invalidate_summary(project_id)
        hub.publish(project_id)

        async def run_generation() -> None:
            try:
                await asyncio.to_thread(generate_scene_narration_candidate, project_dir, scene_id)
            except Exception as exc:
                try:
                    await asyncio.to_thread(mark_scene_narration_candidate_failed, project_dir, scene_id, exc)
                except Exception:
                    pass
            _invalidate_summary(project_id)
            hub.publish(project_id)

        asyncio.create_task(run_generation())
        return state

    @app.post("/api/project/{project_id}/workbench/scenes/{scene_id}/narration/candidates/{version_id}/apply/jobs")
    async def apply_scene_narration_job(project_id: str, scene_id: str, version_id: str) -> dict:
        project_dir = _safe_project_dir(project_id)
        try:
            state = await asyncio.to_thread(start_scene_narration_apply, project_dir, scene_id, version_id)
        except WorkbenchError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        # The selected scene carries the authoritative job reference.  Do not
        # infer it from a list position because other scenes may have history.
        selected = next((item for item in state.get("scenes", []) if item.get("id") == scene_id), {})
        patch_id = str(((selected.get("narration") or {}).get("job") or {}).get("patch_id") or "")
        if not patch_id:
            raise HTTPException(status_code=500, detail="未能建立该片段的局部合成任务")
        _invalidate_summary(project_id)
        hub.publish(project_id)

        async def run_compose() -> None:
            try:
                await asyncio.to_thread(render_patch, project_dir, patch_id)
            except Exception as exc:
                # render_patch persists a blocked report for normal validation
                # failures.  This guard only protects unexpected worker errors.
                try:
                    await asyncio.to_thread(mark_patch_render_failed, project_dir, patch_id, exc)
                except Exception:
                    pass
            _invalidate_summary(project_id)
            hub.publish(project_id)

        asyncio.create_task(run_compose())
        return state

    @app.post("/api/project/{project_id}/workbench/segments/{segment_id}/freeze")
    async def post_workbench_freeze(project_id: str, segment_id: str, payload: dict = Body(default={})) -> dict:
        return await workbench_call(freeze_segment, project_id, segment_id, bool(payload.get("frozen", True)))

    @app.post("/api/project/{project_id}/workbench/baseline-cache")
    async def build_workbench_baseline_cache(project_id: str) -> dict:
        return await workbench_call(build_baseline_cache, project_id)

    @app.post("/api/project/{project_id}/workbench/patches")
    async def post_workbench_patch(project_id: str, payload: dict = Body(...)) -> dict:
        return await workbench_call(prepare_patch, project_id, payload)

    @app.post("/api/project/{project_id}/workbench/patches/{patch_id}/render")
    async def render_workbench_patch(project_id: str, patch_id: str) -> dict:
        return await workbench_call(render_patch, project_id, patch_id)

    @app.post("/api/project/{project_id}/workbench/patches/{patch_id}/promote")
    async def promote_workbench_patch(project_id: str, patch_id: str) -> dict:
        return await workbench_call(promote_patch, project_id, patch_id)

    @app.post("/api/project/{project_id}/workbench/patches/{patch_id}/rollback")
    async def rollback_workbench_patch(project_id: str, patch_id: str) -> dict:
        return await workbench_call(rollback_patch, project_id, patch_id)

    @app.get("/api/project/{project_id}/events")
    async def project_events(project_id: str, request: Request) -> StreamingResponse:
        _safe_project_dir(project_id)  # 404 early for unknown projects

        async def stream():
            q = hub.subscribe(project_id)
            try:
                yield _sse({"type": "hello", "project_id": project_id})
                while True:
                    if await request.is_disconnected():
                        return
                    try:
                        await asyncio.wait_for(q.get(), timeout=SSE_HEARTBEAT_SECONDS)
                    except asyncio.TimeoutError:
                        yield _sse({"type": "heartbeat", "ts": time.time()})
                        continue
                    # Coalesce bursts: drain anything else queued.
                    while not q.empty():
                        try:
                            q.get_nowait()
                        except asyncio.QueueEmpty:
                            break
                    yield _sse({"type": "change", "project_id": project_id})
            finally:
                hub.unsubscribe(q)

        return StreamingResponse(stream(), media_type="text/event-stream", headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        })

    @app.get("/api/library/events")
    async def library_events(request: Request) -> StreamingResponse:
        async def stream():
            q = hub.subscribe()
            try:
                yield _sse({"type": "hello"})
                while True:
                    if await request.is_disconnected():
                        return
                    try:
                        changed = await asyncio.wait_for(q.get(), timeout=SSE_HEARTBEAT_SECONDS)
                    except asyncio.TimeoutError:
                        yield _sse({"type": "heartbeat", "ts": time.time()})
                        continue
                    while not q.empty():
                        try:
                            q.get_nowait()
                        except asyncio.QueueEmpty:
                            break
                    yield _sse({"type": "change", "project_id": changed})
            finally:
                hub.unsubscribe(q)

        return StreamingResponse(stream(), media_type="text/event-stream", headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        })

    # ---- Thumbnails (downscaled, cached on disk) ------------------------

    @app.get("/thumb/{project_id}/{file_path:path}")
    async def thumb(project_id: str, file_path: str, w: int = 640) -> FileResponse:
        project_dir = _safe_project_dir(project_id)
        target = (project_dir / file_path).resolve()
        try:
            target.relative_to(project_dir.resolve())
        except ValueError:
            raise HTTPException(status_code=403, detail="path escapes project")
        if not target.is_file():
            raise HTTPException(status_code=404, detail="media not found")
        width = min(THUMB_WIDTHS, key=lambda x: abs(x - w))
        cached = await asyncio.to_thread(_thumbnail_for, target, width)
        if cached is None:
            # Never fall back to raw video bytes for an <img> consumer (F-03);
            # non-thumbable images are safe to serve as-is.
            if target.suffix.lower() in {".mp4", ".webm", ".mov"}:
                raise HTTPException(status_code=404, detail="no poster frame available")
            return FileResponse(target)
        return FileResponse(cached, media_type="image/jpeg")

    # ---- Media (range requests handled by FileResponse) ---------------

    @app.get("/audio-preview/{preview_id}")
    async def audio_preview(preview_id: str) -> FileResponse:
        try:
            target = await asyncio.to_thread(preview_audio_path, preview_id)
        except AudioCenterError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return FileResponse(target, media_type="audio/wav")

    @app.get("/media/{project_id}/{file_path:path}")
    async def media(project_id: str, file_path: str) -> FileResponse:
        project_dir = _safe_project_dir(project_id)
        target = (project_dir / file_path).resolve()
        try:
            target.relative_to(project_dir.resolve())
        except ValueError:
            raise HTTPException(status_code=403, detail="path escapes project")
        if not target.is_file():
            raise HTTPException(status_code=404, detail="media not found")
        return FileResponse(target)

    # ---- UI ------------------------------------------------------------

    @app.get("/audio")
    async def audio_center_page() -> HTMLResponse:
        return _ui_html("audio_center.html", ("audio_center.css", "audio_center.js"))

    @app.get("/p/{project_id}/board")
    async def legacy_board_page(project_id: str) -> HTMLResponse:
        return _ui_html("board.html", ("board.css", "board.js"))

    @app.get("/p/{project_id}")
    async def workbench_page(project_id: str, request: Request) -> HTMLResponse:
        # Static screenshot fixtures exercise the established observer board.
        # Interactive project visits are routed to the new director workbench.
        if request.query_params.get("static"):
            return _ui_html("board.html", ("board.css", "board.js"))
        return _ui_html("workbench.html", ("workbench.css", "workbench.js"))

    @app.get("/p/{project_path:path}")
    async def board_page_path(project_path: str, request: Request) -> HTMLResponse:
        if request.query_params.get("static"):
            return _ui_html("board.html", ("board.css", "board.js"))
        return _ui_html("workbench.html", ("workbench.css", "workbench.js"))

    @app.get("/")
    async def library_page() -> HTMLResponse:
        return _ui_html("index.html", ("board.css", "library.js"))

    @app.get("/automation")
    async def daily_automation_page() -> HTMLResponse:
        return _ui_html("automation.html", ("board.css", "automation.css", "automation.js"))

    if UI_DIR.is_dir():
        app.mount("/ui", StaticFiles(directory=UI_DIR), name="ui")

    # The board is a long-lived SPA: a tab keeps running whatever board.js it
    # loaded, and browsers heuristically cache /ui assets. no-cache forces a
    # conditional revalidation (cheap 304 via ETag) on every load so UI fixes
    # show up on a plain refresh. Media/thumb responses keep normal caching.
    @app.middleware("http")
    async def ui_no_cache(request, call_next):
        response = await call_next(request)
        path = request.url.path
        if path == "/" or path.startswith("/ui") or path.startswith("/p/"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    return app


def _safe_project_dir(project_id: str) -> Path:
    # ':' rejects Windows drive-relative ids like "C:" (PROJECTS_DIR / "C:"
    # collapses back to PROJECTS_DIR itself).
    if any(c in project_id for c in "/\\:") or project_id in (".", ".."):
        raise HTTPException(status_code=400, detail="invalid project id")
    project_dir = PROJECTS_DIR / project_id
    if not project_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"unknown project: {project_id}")
    return project_dir


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


def _thumbnail_for(source: Path, width: int) -> Optional[Path]:
    """Downscale an image (or extract a video poster frame) to a cached JPEG."""
    suffix = source.suffix.lower()
    is_image = suffix in {".png", ".jpg", ".jpeg", ".webp", ".gif"}
    is_video = suffix in {".mp4", ".webm", ".mov"}
    if not (is_image or is_video):
        return None
    try:
        import hashlib
        stat = source.stat()
        key = hashlib.sha1(
            f"{source}|{stat.st_mtime_ns}|{stat.st_size}|{width}".encode()
        ).hexdigest()[:20]
        cached = THUMB_CACHE_DIR / f"{key}.jpg"
        if cached.is_file():
            return cached
        THUMB_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        # Unique temp per request — concurrent misses for the same source
        # must not write (and replace from) the same temp file.
        import uuid
        tmp = THUMB_CACHE_DIR / f"{key}.{uuid.uuid4().hex[:8]}.tmp.jpg"
        if is_video:
            import subprocess
            result = subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error", "-ss", "1.5",
                 "-i", str(source), "-frames:v", "1",
                 "-vf", f"scale={width}:-2", str(tmp)],
                capture_output=True, timeout=30,
            )
            if result.returncode != 0 or not tmp.is_file():
                return None
        else:
            from PIL import Image
            with Image.open(source) as img:
                img = img.convert("RGB")
                img.thumbnail((width, width * 3))
                img.save(tmp, "JPEG", quality=82)
        tmp.replace(cached)
        return cached
    except Exception:
        return None


app = create_app()
