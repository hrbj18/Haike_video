"""Persistent, human-directed workbench state for Backlot.

The original Backlot board is intentionally read-only.  This module adds a
small, project-local control plane for the review workbench without taking
over pipeline orchestration or mutating canonical production artifacts.  It
turns the existing script, scene plan and asset manifest into a reviewable
model on first use, then stores human decisions in ``artifacts/workbench.json``.

The core invariant is deliberately conservative: a patch owns one explicit
render segment, frozen neighbouring segments are represented by immutable
snapshots, and a strict-freeze patch can never be marked rendered merely
because a plan was created.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from copy import deepcopy
from datetime import UTC, datetime
from functools import wraps
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable
from uuid import uuid4

from backlot.state import _collect_artifacts, _collect_checkpoints, _read_json
from backlot.audio_center import get_default_voice, get_voice_profile, read_audio_center
from backlot.tts_runtime import generate_voice_audio
from backlot.ai_text import TextAIError, plan_visual_copy, plan_visual_routes, read_text_ai_config
from backlot.ai_vision import describe_shots, test_vision_ai_connection, vision_runtime_identity
from backlot.visual_director import DIRECTOR_VERSION, candidate_asset_id, decide_candidate, prepare_candidates
from backlot.avatar_import import AvatarImportError, initialize_avatar_package, read_avatar_package
from backlot.ppt_cards import CARD_TYPES, normalize_spec as normalize_ppt_card_spec, render_card as render_ppt_card
from backlot.task_center import collect_tasks
from backlot.music_library import MusicLibraryError, list_music_tracks, resolve_music_track
from backlot.media_index import (
    MediaIndexError,
    build_coarse_index,
    build_fine_index,
    build_material_vision_index,
    media_content_fingerprint,
    media_fingerprint,
    recommend_coarse_segments,
    recommend_vision_shots,
)
from backlot.local_material_orchestration import (
    LocalMaterialOrchestrationError,
    build_orchestration_draft,
    find_scene_plan,
    material_indexes_fingerprint,
    script_fingerprint as local_material_script_fingerprint,
)
from backlot.music_preferences import (
    DEFAULT_PLAYBACK_GAIN_DB,
    clamp_playback_gain_db,
    read_music_preferences,
    save_music_preferences,
)
from backlot.narration_preferences import (
    DEFAULT_NARRATION_GAIN_DB,
    clamp_narration_gain_db,
    read_narration_preferences,
    save_narration_preferences,
)
from backlot.subtitle_preferences import read_subtitle_preferences, save_subtitle_preferences
from backlot.script_templates import ScriptTemplateError, build_avatar_script_from_template
from backlot.script_imports import (
    ScriptImportError,
    build_script_from_staged_import,
    consume_staged_import,
    load_staged_import,
)
from lib.tech_brief_style_pack import (
    STYLE_PACK_ID,
    StylePackError,
    build_style_context,
    layout_variant_catalog,
    load_style_pack,
    recommended_subtitle_style,
    resolve_layout_variant,
    style_pack_playbook,
    style_pack_summary,
)
from schemas.artifacts import validate_artifact
from tools.audio.voicebox_tts import VoiceboxTTS
from tools.audio.audio_mixer import AudioMixer
from tools.graphics.openai_image import OpenAIImage
from tools.graphics.pexels_image import PexelsImage
from tools.text.openai_script import OpenAIScript
from tools.video.hyperframes_compose import HyperFramesCompose
from tools.video.pexels_video import PexelsVideo
from tools.video import video_compose as video_compose_runtime
from tools.video.video_compose import VideoCompose
from tools.video.stock_sources.base import SearchFilters
from tools.video.stock_sources.pexels import PexelsSource


WORKBENCH_VERSION = "1.4"
WORKBENCH_FILE = "artifacts/workbench.json"
COMPOSITION_FILE = "artifacts/composition_manifest.json"
PATCH_DIRECTORY = "artifacts/patch_requests"
BOUNDARY_DIRECTORY = "artifacts/boundary_reports"
SEGMENT_DIRECTORY = "renders/segments"
KEYFRAME_REVIEW_DIRECTORY = "artifacts/keyframe_reviews"
AUTOMATION_DIRECTORY = "artifacts/automation"
AUTOMATION_ASSET_MANIFEST = "artifacts/asset_manifest.json"
AUTOMATION_EDIT_DECISIONS = "artifacts/edit_decisions.json"
AUTOMATION_RENDER_REPORT = "artifacts/render_report.json"
AUTOMATION_PREVIEW_RENDER_REPORT = "artifacts/full_preview_render_report.json"
SCRIPT_IMPORT_DIRECTORY = "artifacts/script_imports"
SCRIPT_IMPORT_HISTORY_DIRECTORY = "artifacts/script_import_history"
ASSET_RECYCLE_DIRECTORY = "artifacts/recycle-bin"
MUSIC_SAMPLE_DIRECTORY = "renders/music-samples"
PROJECT_ASSET_UPLOAD_DIRECTORY = Path("assets/uploads")
PROJECT_ASSET_UPLOAD_TYPES = {
    ".mp4": "video", ".mov": "video", ".mkv": "video", ".webm": "video", ".m4v": "video",
    ".png": "image", ".jpg": "image", ".jpeg": "image", ".webp": "image",
}
MAX_PROJECT_ASSET_BYTES = 8 * 1024 * 1024 * 1024

# Batch cleanup deliberately has a very narrow default.  Files supplied by a
# user (and curated project-library material) are never silently selected for
# cleanup, even when the current timeline does not reference them.  They can
# still be reviewed in the asset ledger, but must remain an explicit human
# decision outside of the one-click flow.
ASSET_AUTO_CLEANUP_SOURCE_TYPES = {
    "web_download",
    "ai_generated",
    "local_generated",
}

SOURCE_TYPES = {
    "human_provided",
    "web_download",
    "project_library",
    "ai_generated",
    "local_generated",
    "mixed",
    "avatar_only",
    "undecided",
}
VISUAL_ENGINES = {"openai_image", "hyperframes", "remotion", "ppt_card"}
KEYFRAME_ANCHOR_KINDS = ("first_frame", "climax_frame")
VISUAL_SOURCE_MODES = {
    "human_provided", "web_download", "project_library", "openai_image",
    "hyperframes", "remotion", "local_generated", "ppt_card",
}
LOCAL_MATERIAL_VISUAL_ROLES = {
    "local_full_bleed", "local_focus_card", "stock_full_bleed",
    "hyperframes_full_bleed", "supporting_background",
}
LOCAL_MATERIAL_CUT_POLICIES = {"atomic", "safe_cut", "interruptible"}
VISUAL_COMPOSITION_LAYOUTS = {"full_bleed", "focus_card"}
VISUAL_COMPOSITION_ROLES = {"hero"}
VISUAL_COMPOSITION_FITS = {"contain", "cover"}
VISUAL_COMPOSITION_MAX_OVERLAYS = 8
VISUAL_COMPOSITION_PLACEMENT_PRESETS = {
    "landscape_hero_center", "portrait_hero_center", "source_hero_custom",
}
VISUAL_COMPOSITION_ASPECT_MODES = {"source"}
VISUAL_CONSTRAINTS = {
    "no_presenter", "no_text", "no_ai_baked_text", "reserve_presenter_safe_area", "reserve_caption_safe_area",
}
PRESENTER_TREATMENTS = {"fullscreen", "pip_top_left", "custom", "hidden"}
PRESENTER_LAYOUT_TEMPLATE_LIMIT = 12
SUBTITLE_STYLE_TEMPLATE_LIMIT = 12
SUBTITLE_ANCHORS = {"top-center", "center", "bottom-center"}
VISUAL_BATCH_PROFILES = {
    "image": {"target_seconds": 3.2, "min_seconds": 2.4, "max_seconds": 4.5, "label": "图片节奏"},
    "video": {"target_seconds": 5.0, "min_seconds": 3.5, "max_seconds": 6.5, "label": "视频节奏"},
    "auto": {"target_seconds": 6.2, "min_seconds": 4.2, "max_seconds": 8.8, "label": "智能语义节奏"},
    # Daily short-form news needs a visible cut roughly every three seconds.
    # Keep this scoped profile separate from general-purpose montage planning.
    "daily_news": {"target_seconds": 3.0, "min_seconds": 2.2, "max_seconds": 3.5, "label": "每日快报快切节奏"},
}
VISUAL_BATCH_OPERATIONS = {"fill_missing", "replace_selected"}
VISUAL_BATCH_MIX_STRATEGIES = {"balanced", "video_first", "motion_first", "image_first"}
VISUAL_BATCH_IMAGE_SOURCES = {"web_download", "openai_image"}
VISUAL_BATCH_CONTENT_RULES = {
    "no_frontal_face",
    "no_prominent_person",
    "no_presenter_studio",
    "no_large_text_watermark",
}
VISUAL_BATCH_DEFAULT_RULES = (
    "no_presenter_studio",
    "no_large_text_watermark",
)
VISUAL_BATCH_PERSON_POLICIES = {"relaxed", "balanced", "strict"}
VISUAL_BATCH_CANDIDATE_LIMITS = {4, 6, 8}
VISUAL_BATCH_PLANNING_MODES = {"rule_mix", "ai_director"}
VISUAL_BATCH_ROUTES = {"stock_video", "stock_image", "ai_image", "hyperframes"}
VISUAL_BATCH_AI_DEFAULT_ROUTES = {"stock_video", "hyperframes"}
VISUAL_BATCH_DURATION_BALANCE = {
    "balanced": {"stock_video_target": .65, "stock_video_min": .60, "stock_video_max": .70},
    "video_first": {"stock_video_target": .75, "stock_video_min": .70, "stock_video_max": .80},
    "motion_first": {"stock_video_target": .52, "stock_video_min": .45, "stock_video_max": .60},
    # Backwards compatibility for saved browser drafts created before the
    # primary-image route was removed from automatic AI planning.
    "image_first": {"stock_video_target": .52, "stock_video_min": .45, "stock_video_max": .60},
}
VISUAL_BATCH_ROUTE_LABELS = {
    "stock_video": "网络视频",
    "stock_image": "网络图片",
    "ai_image": "OpenAI 图片",
    "hyperframes": "HyperFrames 动态画面",
}
STOCK_SEARCH_ROLES = ("establishing", "process", "detail", "outcome")
STOCK_SEARCH_ROLE_LABELS = {
    "establishing": "建立镜头",
    "process": "过程镜头",
    "detail": "细节镜头",
    "outcome": "结果镜头",
}
STOCK_TECH_DEFAULT_KEYWORDS = (
    "芯片", "半导体晶圆", "数据中心", "机器人", "机械臂", "自动化生产线",
    "智能手机", "手机界面", "语音助手", "智能设备", "云计算", "数据网络",
)
STOCK_TECH_DEFAULT_CAUTIOUS_TOPICS = (
    "主播", "演播室", "正面人物肖像", "商务会议", "人物采访", "网红自拍",
)
STOCK_KEYWORD_TRANSLATIONS = {
    "芯片": "computer chip", "半导体": "semiconductor", "半导体晶圆": "semiconductor wafer",
    "光刻机": "semiconductor machinery", "数据中心": "data center servers",
    "机器人": "industrial robot", "机械臂": "robotic arm", "机器狗": "quadruped robot",
    "自动化生产线": "automated production line", "生产车间": "automated factory",
    "智能手机": "smartphone", "手机界面": "smartphone interface",
    "语音助手": "voice assistant", "智能设备": "connected devices",
    "云计算": "cloud computing", "数据网络": "digital data network",
    "创作工作台": "creative desk overhead", "手部操作": "hands using technology",
}
SURGICAL_COMPONENT_TYPES = {"text_callout", "info_label", "focus_box"}
SURGICAL_COMPONENT_POSITIONS = {"top_left", "top_right", "center", "lower_third"}
PRESENTER_LAYOUT_DEFAULTS = (
    # Approved on the 2026-08-23 daily-tech project: this framing keeps the
    # shared 4:5 avatar's full head and upper torso visible in the circle.
    {"id": "pip_top_left", "name": "左上角解说员", "geometry": {"x": 0.04, "y": 0.03, "width": 0.29}, "shape": "circle", "face_crop": {"x": .48, "y": .38, "zoom": 1.15}},
    {"id": "pip_top_right", "name": "右上圆形解说员", "geometry": {"x": 0.675, "y": 0.04, "width": 0.29}, "shape": "circle", "face_crop": {"x": .48, "y": .38, "zoom": 1.15}},
    {"id": "pip_lower_left", "name": "左下留字幕", "geometry": {"x": 0.035, "y": 0.43, "width": 0.24}, "shape": "rounded", "face_crop": {"x": .5, "y": 0, "zoom": 1}},
)
STORY_HEADLINE_LAYOUT_DEFAULT = {"x": .04, "y": .055, "width": .56, "height": .125}
PRESENTER_SHAPES = {"rectangle", "rounded", "circle"}
AVATAR_PIPELINE = "avatar-spokesperson"
# Apply a deliberately small lift to the presenter layer only. Technology
# footage is frequently dark, and studio avatars have dark hair or clothing;
# without local compensation the face can disappear against a valid main visual.
# Keep this before the RGBA shape mask so FFmpeg's ``eq`` filter works on colour.
AVATAR_PIP_FACE_LIGHTING_FILTER = "eq=brightness=0.035:contrast=1.060:gamma=1.040:gamma_weight=0.500"
AVATAR_PIP_FACE_LIGHTING_VERSION = "avatar-pip-face-light-v1"
# ``strict_freeze`` is retained for genuinely fixed-duration deliveries.  It
# must never become the default for narration: a natural voice take owns its
# duration, so replacing it needs a ripple edit rather than time stretching.
PATCH_MODES = {"strict_freeze", "seam_transition", "ripple_timeline"}
PATCH_STATUSES = {
    "draft", "planned", "blocked", "ready_to_render", "rendering",
    "rendered", "promoted", "rolled_back",
}
REVIEW_STATUSES = {"pending", "approved", "needs_adjustment", "blocked"}
INTAKE_SCRIPT_STATUSES = {"unknown", "complete", "partial", "idea", "none", "draft_ready", "draft_approved"}
INTAKE_MATERIAL_STATUSES = {"unknown", "available", "partial", "none"}
INTAKE_STYLE_STATUSES = {"unknown", "reference", "direction", "none"}
SCRIPT_DRAFT_MODES = {"organize_script", "expand_idea", "from_scratch"}
TIMELINE_AUTHORITIES = {"narration", "script_estimate", "fixed_duration", "source_media"}
NATURAL_NARRATION_AUTHORITY = "narration"
FIXED_SLOT_AUDIO_TOLERANCE_SECONDS = 0.35

# Windows can briefly deny ``os.replace`` while antivirus, indexing, or a
# concurrent request has the destination open.  Workbench saves are frequent
# during batch generation, so one transient denial must not abort the entire
# job.  The per-path lock also prevents two in-process writers from attempting
# to replace the same project file at the same instant.
_ATOMIC_WRITE_LOCKS: dict[str, threading.RLock] = {}
_ATOMIC_WRITE_LOCKS_GUARD = threading.Lock()
_PROJECT_TRANSACTION_LOCKS: dict[str, threading.RLock] = {}
_PROJECT_TRANSACTION_LOCKS_GUARD = threading.Lock()
_ATOMIC_REPLACE_RETRY_DELAYS = (0.05, 0.1, 0.2, 0.4, 0.8, 1.0, 1.0)
_DEFER_VISUAL_SLOT_STATE_SAVE = "_defer_visual_slot_state_save"


class WorkbenchError(ValueError):
    """A validation failure which should be presented to a workbench user."""


class WorkbenchConflict(WorkbenchError):
    """A compare-and-swap conflict that requires the client to refresh."""


def _project_transaction_lock(project_dir: Path) -> threading.RLock:
    """Return the shared in-process CAS lock for one project's job state."""
    key = os.path.normcase(str(project_dir.resolve()))
    with _PROJECT_TRANSACTION_LOCKS_GUARD:
        return _PROJECT_TRANSACTION_LOCKS.setdefault(key, threading.RLock())


def _project_transactional(function: Callable[..., Any]) -> Callable[..., Any]:
    """Serialize a manual starter with the review-preview parent transaction."""
    @wraps(function)
    def wrapped(project_dir: Path, *args: Any, **kwargs: Any) -> Any:
        with _project_transaction_lock(project_dir):
            return function(project_dir, *args, **kwargs)
    return wrapped


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _json_hash(value: Any) -> str:
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _atomic_write_lock(path: Path) -> threading.RLock:
    key = os.path.normcase(str(path.resolve()))
    with _ATOMIC_WRITE_LOCKS_GUARD:
        return _ATOMIC_WRITE_LOCKS.setdefault(key, threading.RLock())


def _replace_with_retry(temporary: str | Path, destination: Path) -> None:
    """Replace a file, tolerating transient Windows sharing violations."""
    for attempt in range(len(_ATOMIC_REPLACE_RETRY_DELAYS) + 1):
        try:
            os.replace(temporary, destination)
            return
        except OSError as exc:
            retryable = isinstance(exc, PermissionError) or getattr(exc, "winerror", None) in {5, 32, 33}
            if not retryable or attempt >= len(_ATOMIC_REPLACE_RETRY_DELAYS):
                raise
            time.sleep(_ATOMIC_REPLACE_RETRY_DELAYS[attempt])


def _atomic_write(path: Path, data: dict) -> None:
    """Write JSON atomically so the observer UI never reads half a document."""
    with _atomic_write_lock(path):
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            _replace_with_retry(temp_name, path)
        except Exception:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
            raise


def _atomic_text_write(path: Path, text: str) -> None:
    """Persist a template source snapshot without exposing a partial file."""
    with _atomic_write_lock(path):
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            _replace_with_retry(temp_name, path)
        except Exception:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
            raise


def _numbered(prefix: str, values: list[dict], key: str) -> str:
    high = 0
    for item in values:
        raw = str(item.get(key) or "")
        if raw.startswith(prefix):
            try:
                high = max(high, int(raw[len(prefix):]))
            except ValueError:
                continue
    return f"{prefix}{high + 1:03d}"


def _numbered_version(segment: dict) -> str:
    prefix = f"{segment['id']}-V"
    return _numbered(prefix, segment.get("versions") or [], "id")


def _as_number(value: Any, fallback: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def _rounded_seconds(value: Any, fallback: float = 0.0) -> float:
    return round(max(0.0, _as_number(value, fallback)), 3)


def _rounded_signed_seconds(value: Any, fallback: float = 0.0) -> float:
    return round(_as_number(value, fallback), 3)


def _nonnegative_frame(value: Any, fps: int) -> int:
    """Match JavaScript Math.round for the non-negative media-time contract."""
    return max(0, int(math.floor(max(0.0, _as_number(value)) * max(1, int(fps)) + .5)))


def _scene_duration(scene: dict) -> float:
    return max(0.04, _as_number(scene.get("end_seconds")) - _as_number(scene.get("start_seconds")))


def _review_preview_default() -> dict:
    """Return the regenerable local preview contract for one scene."""
    return {
        "status": "idle",
        "output_path": None,
        "input_signature": None,
        "duration_seconds": None,
        "resolution": None,
        "caption_cues": [],
        "generated_at": None,
        "stale_reason": "",
        "error": "",
    }


def _ensure_scene_review_surface(scene: dict) -> None:
    """Migrate the lightweight review data without changing existing decisions."""
    preview = scene.get("review_preview")
    if not isinstance(preview, dict):
        preview = _review_preview_default()
        scene["review_preview"] = preview
    for key, value in _review_preview_default().items():
        preview.setdefault(key, deepcopy(value))
    directives = scene.get("surgical_directives")
    if not isinstance(directives, list):
        scene["surgical_directives"] = []


def _subtitle_style_default() -> dict:
    """Return the portable baseline used by the live editor and final render.

    The values are intentionally expressed in normalized canvas coordinates
    wherever possible.  That keeps a caption's visual intent stable when a
    project changes from 9:16 to 16:9 instead of preserving an accidental CSS
    pixel value from the old canvas.
    """
    return {
        "enabled": True,
        "display_mode": "phrase",
        "font": "Microsoft YaHei",
        "font_size": 42,
        "bold": True,
        "text_color": "#FFFFFF",
        "outline_color": "#07111F",
        "outline_width": 3,
        "background_enabled": False,
        "background_color": "#07111F",
        "background_opacity": 68,
        "position": {"x": 0.5, "y": 0.89, "width": 0.84, "anchor": "bottom-center"},
        "max_lines": 2,
    }


def _subtitle_template_defaults() -> list[dict]:
    defaults = [{
        "id": "subtitle-default",
        "name": "标准中文短句字幕",
        "style": _subtitle_style_default(),
        "builtin": True,
        "revision": 1,
        "updated_at": _now(),
    }]
    # This recommendation is deliberately a *template*, not the default.  A
    # visual-style selection must never silently overwrite a user's caption
    # work; it becomes active only after an explicit opt-in in the visual plan.
    try:
        defaults.append({
            "id": "subtitle-tech-brief-v1",
            "name": "科技快报 V1 字幕",
            "style": recommended_subtitle_style(),
            "builtin": True,
            "revision": 1,
            "updated_at": _now(),
        })
    except StylePackError:
        # A broken optional style pack must not make legacy projects unable to
        # open their existing subtitle editor.  HyperFrames generation itself
        # still fails closed with the exact pack error.
        pass
    return defaults


def _normalised_hex_color(value: Any, fallback: str) -> str:
    candidate = str(value or "").strip().upper()
    return candidate if re.fullmatch(r"#[0-9A-F]{6}", candidate) else fallback


def _normalised_subtitle_style(raw: Any, fallback: dict | None = None) -> dict:
    """Validate one complete subtitle style without trusting browser input."""
    source = raw if isinstance(raw, dict) else {}
    base = deepcopy(fallback) if isinstance(fallback, dict) else _subtitle_style_default()
    defaults = _subtitle_style_default()
    position_source = source.get("position") if isinstance(source.get("position"), dict) else {}
    position_base = base.get("position") if isinstance(base.get("position"), dict) else defaults["position"]
    anchor = str(position_source.get("anchor", position_base.get("anchor") or "bottom-center"))
    if anchor not in SUBTITLE_ANCHORS:
        anchor = "bottom-center"
    width = min(.94, max(.42, _as_number(position_source.get("width"), _as_number(position_base.get("width"), .84))))
    x = min(1.0 - width / 2, max(width / 2, _as_number(position_source.get("x"), _as_number(position_base.get("x"), .5))))
    y = min(.94, max(.07, _as_number(position_source.get("y"), _as_number(position_base.get("y"), .89))))
    font = re.sub(r"[\r\n]", " ", str(source.get("font", base.get("font") or defaults["font"]))).strip()[:80]
    return {
        "enabled": bool(source.get("enabled", base.get("enabled", True))),
        "display_mode": "phrase",
        "font": font or defaults["font"],
        "font_size": int(min(80, max(24, _as_number(source.get("font_size"), _as_number(base.get("font_size"), 42))))),
        "bold": bool(source.get("bold", base.get("bold", True))),
        "text_color": _normalised_hex_color(source.get("text_color", base.get("text_color")), defaults["text_color"]),
        "outline_color": _normalised_hex_color(source.get("outline_color", base.get("outline_color")), defaults["outline_color"]),
        "outline_width": round(min(8, max(0, _as_number(source.get("outline_width"), _as_number(base.get("outline_width"), 3)))), 2),
        "background_enabled": bool(source.get("background_enabled", base.get("background_enabled", False))),
        "background_color": _normalised_hex_color(source.get("background_color", base.get("background_color")), defaults["background_color"]),
        "background_opacity": int(min(100, max(0, _as_number(source.get("background_opacity"), _as_number(base.get("background_opacity"), 68))))),
        "position": {"x": round(x, 4), "y": round(y, 4), "width": round(width, 4), "anchor": anchor},
        "max_lines": int(min(3, max(1, _as_number(source.get("max_lines"), _as_number(base.get("max_lines"), 2))))),
    }


def _software_default_subtitle_style() -> dict:
    """Return the validated workstation default, never trusting local JSON."""
    preferences = read_subtitle_preferences()
    return _normalised_subtitle_style(preferences.get("style"), _subtitle_style_default())


def read_subtitle_preferences_settings() -> dict:
    """Expose the non-sensitive subtitle default used by future projects."""
    preferences = read_subtitle_preferences()
    return {"version": preferences.get("version", 1), "style": _software_default_subtitle_style()}


def update_subtitle_preferences_settings(payload: dict) -> dict:
    """Persist a global default without changing any existing project state."""
    if not isinstance(payload.get("style"), dict):
        raise WorkbenchError("请先提供要保存的字幕样式")
    return save_subtitle_preferences({"style": _normalised_subtitle_style(payload["style"], _subtitle_style_default())})


def _ensure_subtitle_style_state(state: dict) -> dict:
    """Migrate reusable subtitle plans without changing existing captions."""
    stored = state.get("subtitle_styles") if isinstance(state.get("subtitle_styles"), dict) else {}
    existing = {
        str(item.get("id")): item
        for item in (stored.get("templates") or [])
        if isinstance(item, dict) and item.get("id")
    }
    templates: list[dict] = []
    for default in _subtitle_template_defaults():
        item = existing.pop(default["id"], {})
        templates.append({
            "id": default["id"],
            "name": str(item.get("name") or default["name"])[:80],
            "style": _normalised_subtitle_style(item.get("style"), default["style"]),
            "builtin": True,
            "revision": max(1, int(_as_number(item.get("revision"), 1))),
            "updated_at": item.get("updated_at") or _now(),
        })
    for item in existing.values():
        if len(templates) >= SUBTITLE_STYLE_TEMPLATE_LIMIT:
            break
        template_id = re.sub(r"[^a-z0-9_-]", "-", str(item.get("id") or "").lower()).strip("-")
        if not template_id or template_id in {entry["id"] for entry in templates}:
            continue
        templates.append({
            "id": template_id,
            "name": str(item.get("name") or "自定义字幕方案")[:80],
            "style": _normalised_subtitle_style(item.get("style")),
            "builtin": False,
            "revision": max(1, int(_as_number(item.get("revision"), 1))),
            "updated_at": item.get("updated_at") or _now(),
        })
    result = {
        "version": max(1, int(_as_number(stored.get("version"), 1))),
        "default_template_id": str(stored.get("default_template_id") or "subtitle-default"),
        "templates": templates,
    }
    if result["default_template_id"] not in {item["id"] for item in templates}:
        result["default_template_id"] = "subtitle-default"
    state["subtitle_styles"] = result
    return result


def _scene_subtitles(scene: dict) -> dict:
    subtitles = scene.get("subtitles")
    if not isinstance(subtitles, dict):
        subtitles = {}
        scene["subtitles"] = subtitles
    subtitles.setdefault("template_id", "subtitle-default")
    if not isinstance(subtitles.get("style_override"), dict):
        subtitles["style_override"] = {}
    if not isinstance(subtitles.get("cue_overrides"), dict):
        subtitles["cue_overrides"] = {}
    return subtitles


def _resolved_scene_subtitle_style(state: dict, scene: dict) -> dict:
    layouts = _ensure_subtitle_style_state(state)
    templates = {str(item["id"]): item for item in layouts["templates"]}
    subtitles = _scene_subtitles(scene)
    template_id = str(subtitles.get("template_id") or layouts["default_template_id"])
    template = templates.get(template_id) or templates[layouts["default_template_id"]]
    style = _normalised_subtitle_style(subtitles.get("style_override"), template["style"])
    # Return the active template identity for UI inspection without storing a
    # stale resolved snapshot in the project file.
    style["template_id"] = template["id"]
    style["template_name"] = template["name"]
    return style


def _subtitle_cue_id(index: int) -> str:
    return f"cue-{index + 1:03d}"


def _subtitle_cue_text(scene: dict, index: int, fallback: Any) -> str:
    overrides = _scene_subtitles(scene).get("cue_overrides") or {}
    text = str(overrides.get(_subtitle_cue_id(index), fallback) or "").strip()
    return text[:240]


def _subtitle_video_style(style: dict) -> dict:
    """Translate workbench CSS values to the portable ASS renderer contract."""
    value = _normalised_subtitle_style(style)
    position = value["position"]
    alpha = max(0, min(255, round(255 * (1 - value["background_opacity"] / 100))))

    def ass_color(color: str, opacity: int = 0) -> str:
        red, green, blue = color[1:3], color[3:5], color[5:7]
        return f"&H{opacity:02X}{blue}{green}{red}"

    alignment = {"bottom-center": 2, "center": 5, "top-center": 8}[position["anchor"]]
    return {
        "font": value["font"],
        "font_size": value["font_size"],
        "font_size_ratio": round(value["font_size"] / 1080, 5),
        "bold": value["bold"],
        "primary_color": ass_color(value["text_color"]),
        "outline_color": ass_color(value["outline_color"], 20),
        "outline_width": value["outline_width"],
        "back_color": ass_color(value["background_color"], alpha),
        "border_style": 3 if value["background_enabled"] else 1,
        "shadow": 0,
        "alignment": alignment,
        "position_x_ratio": position["x"],
        "position_y_ratio": position["y"],
        "caption_width_ratio": position["width"],
        "responsive": True,
    }


def _visual_source_mode(asset: dict | None) -> str:
    if not asset:
        return "human_provided"
    tool = str((asset.get("provenance") or {}).get("source_tool") or "").lower()
    provider = str((asset.get("provenance") or {}).get("provider") or "").lower()
    if "hyperframes" in tool or "hyperframes" in provider:
        return "hyperframes"
    if "remotion" in tool or "remotion" in provider:
        return "remotion"
    if tool == "ppt_card_provider" or provider == "ppt_master":
        return "ppt_card"
    if tool == "openai_image" or str(asset.get("source_type") or "") == "ai_generated":
        return "openai_image"
    source_type = str(asset.get("source_type") or "human_provided")
    return source_type if source_type in VISUAL_SOURCE_MODES else "human_provided"


def _default_visual_composition() -> dict:
    """Return the backwards-compatible semantic layer contract for one scene."""
    return {
        "version": 1,
        "revision": 1,
        "layout_recipe": "full_bleed",
        "background": {"source": "visual_timeline", "treatment": "normal"},
        "overlays": [],
        "frame_style": {
            "width_ratio": .82,
            "height_ratio": .56,
            "border_radius_ratio": .025,
            "border_color": "#D9F3FF",
            "shadow": "soft",
        },
        "updated_at": _now(),
    }


def _asset_resolution(asset: dict) -> tuple[int, int]:
    """Read the imported media dimensions without probing during composition saves."""
    raw = asset.get("resolution")
    if isinstance(raw, dict):
        width = int(_as_number(raw.get("width"), 0))
        height = int(_as_number(raw.get("height"), 0))
        if width > 0 and height > 0:
            return width, height
    match = re.fullmatch(r"\s*(\d+)\s*[xX×]\s*(\d+)\s*", str(raw or ""))
    if match:
        width, height = int(match.group(1)), int(match.group(2))
        if width > 0 and height > 0:
            return width, height
    return 0, 0


def _recommended_visual_placement(asset: dict, canvas_width: int, canvas_height: int) -> dict:
    """Return a source-aspect recommendation for a bounded 16:9 hero window."""
    source_width, source_height = _asset_resolution(asset)
    source_aspect = source_width / source_height if source_width > 0 and source_height > 0 else canvas_width / canvas_height
    if source_aspect >= 1:
        preset_id = "landscape_hero_center"
        size_ratio = .74
        position_y_ratio = .47
        max_height_ratio = .78
    else:
        preset_id = "portrait_hero_center"
        position_y_ratio = .44
        max_height_ratio = .68
        size_ratio = min(.42, max_height_ratio * (canvas_height / canvas_width) * source_aspect)
    return {
        "preset_id": preset_id,
        "position_x_ratio": .5,
        "position_y_ratio": position_y_ratio,
        "size_ratio": round(size_ratio, 4),
        "aspect_mode": "source",
        "max_height_ratio": max_height_ratio,
    }


def _validated_visual_placement(
    raw: Any,
    asset: dict,
    canvas_width: int,
    canvas_height: int,
    index: int,
) -> dict | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise WorkbenchError(f"第 {index} 个重点素材的位置数据格式无效")
    recommended = _recommended_visual_placement(asset, canvas_width, canvas_height)
    preset_id = str(raw.get("preset_id") or recommended["preset_id"])
    if preset_id not in VISUAL_COMPOSITION_PLACEMENT_PRESETS:
        raise WorkbenchError(f"第 {index} 个重点素材的位置预设无效")
    aspect_mode = str(raw.get("aspect_mode") or "source")
    if aspect_mode not in VISUAL_COMPOSITION_ASPECT_MODES:
        raise WorkbenchError(f"第 {index} 个重点素材必须保持源宽高比")
    x = _as_number(raw.get("position_x_ratio"), recommended["position_x_ratio"])
    y = _as_number(raw.get("position_y_ratio"), recommended["position_y_ratio"])
    size = _as_number(raw.get("size_ratio"), recommended["size_ratio"])
    max_height = _as_number(raw.get("max_height_ratio"), recommended["max_height_ratio"])
    if not .05 <= x <= .95 or not .05 <= y <= .95:
        raise WorkbenchError(f"第 {index} 个重点素材的中心位置超出画布安全范围")
    if not .12 <= size <= .92:
        raise WorkbenchError(f"第 {index} 个重点素材的大小超出安全范围")
    if not .35 <= max_height <= .9:
        raise WorkbenchError(f"第 {index} 个重点素材的最大高度无效")

    source_width, source_height = _asset_resolution(asset)
    source_aspect = source_width / source_height if source_width > 0 and source_height > 0 else canvas_width / canvas_height
    width_ratio = min(size, max_height * (canvas_height / canvas_width) * source_aspect)
    height_ratio = width_ratio * (canvas_width / canvas_height) / source_aspect
    if x - width_ratio / 2 < -1e-6 or x + width_ratio / 2 > 1 + 1e-6:
        raise WorkbenchError(f"第 {index} 个重点素材横向超出画布，请缩小或向中间移动")
    if y - height_ratio / 2 < -1e-6 or y + height_ratio / 2 > 1 + 1e-6:
        raise WorkbenchError(f"第 {index} 个重点素材纵向超出画布，请缩小或向中间移动")
    return {
        "preset_id": preset_id,
        "position_x_ratio": round(x, 4),
        "position_y_ratio": round(y, 4),
        "size_ratio": round(size, 4),
        "aspect_mode": "source",
        "max_height_ratio": round(max_height, 4),
    }


def _validated_candidate_evidence(raw: Any, index: int) -> dict | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise WorkbenchError(f"第 {index} 个重点素材的候选证据格式无效")
    source = str(raw.get("source") or "")
    shot_id = str(raw.get("shot_id") or "")
    query = re.sub(r"\s+", " ", str(raw.get("query") or "")).strip()[:1000]
    fingerprint = str(raw.get("index_fingerprint") or "").lower()
    if source != "vision_v2" or not re.fullmatch(r"SHOT-\d{4}", shot_id):
        raise WorkbenchError(f"第 {index} 个重点素材的视觉候选证据无效")
    if not query:
        raise WorkbenchError(f"第 {index} 个重点素材缺少候选查询文本")
    if not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
        raise WorkbenchError(f"第 {index} 个重点素材的视觉索引指纹无效")
    return {
        "source": "vision_v2",
        "shot_id": shot_id,
        "query": query,
        "index_fingerprint": fingerprint,
    }


def _validated_local_material_planner_evidence(raw: Any, index: int) -> dict | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise WorkbenchError(f"第 {index} 个重点素材的编排证据格式无效")
    shot_id = str(raw.get("shot_id") or "")
    fingerprint = str(raw.get("index_fingerprint") or "").lower()
    sequence_id = str(raw.get("sequence_id") or "")
    cut_policy = str(raw.get("cut_policy") or "")
    if (
        raw.get("source") != "local_material_orchestration_v1"
        or not re.fullmatch(r"SHOT-\d{4}", shot_id)
        or not re.fullmatch(r"[0-9a-f]{64}", fingerprint)
        or not re.fullmatch(r"LMS-\d{3}", sequence_id)
        or cut_policy not in LOCAL_MATERIAL_CUT_POLICIES
    ):
        raise WorkbenchError(f"第 {index} 个重点素材缺少可核验的本地素材编排证据")
    return {
        "source": "local_material_orchestration_v1",
        "shot_id": shot_id,
        "index_fingerprint": fingerprint,
        "sequence_id": sequence_id,
        "cut_policy": cut_policy,
    }


def _ensure_scene_visual_composition(scene: dict) -> dict:
    """Migrate the optional layer contract without enabling it for old projects."""
    raw = scene.get("visual_composition")
    composition = raw if isinstance(raw, dict) else {}
    default = _default_visual_composition()
    composition.setdefault("version", 1)
    composition.setdefault("revision", 1)
    composition.setdefault("layout_recipe", "full_bleed")
    background = composition.get("background") if isinstance(composition.get("background"), dict) else {}
    background.setdefault("source", "visual_timeline")
    background.setdefault(
        "treatment",
        "auto_dim" if composition.get("layout_recipe") == "focus_card" else "normal",
    )
    composition["background"] = background
    composition["overlays"] = composition.get("overlays") if isinstance(composition.get("overlays"), list) else []
    frame_style = composition.get("frame_style") if isinstance(composition.get("frame_style"), dict) else {}
    for key, value in default["frame_style"].items():
        frame_style.setdefault(key, value)
    composition["frame_style"] = frame_style
    composition.setdefault("updated_at", _now())
    scene["visual_composition"] = composition
    return composition


def _visual_composition_render_contract(value: Any) -> dict:
    """Strip review/control metadata that cannot change a rendered frame."""
    composition = value if isinstance(value, dict) else _default_visual_composition()
    return {
        "layout_recipe": composition.get("layout_recipe") or "full_bleed",
        "background": composition.get("background") or {},
        "overlays": [
            {key: item.get(key) for key in (
                "id", "role", "asset_id", "start_seconds", "end_seconds",
                "source_in_seconds", "source_out_seconds", "fit", "muted",
                "playback_rate", "placement", "candidate_evidence", "planner_evidence",
            )}
            for item in (composition.get("overlays") or []) if isinstance(item, dict)
        ],
        "frame_style": composition.get("frame_style") or {},
    }


def _default_visual_plan_prompt(state: dict, scene: dict) -> str:
    intake = _normalize_intake(state.get("project", {}).get("intake"))
    style = intake.get("style_direction") or intake.get("style_reference") or "干净、清晰、适合中文知识类短视频"
    presenter = _scene_presenter(scene)
    safe_area = "左上角已经有数字人讲解员，请把核心信息避开左上角。" if presenter.get("treatment") in {"pip_top_left", "custom"} else ""
    return (
        f"为视频片段《{scene.get('title') or '未命名场景'}》设计主体支持画面。\n"
        f"台词语境：{str(scene.get('description') or '').strip() or '待补充'}\n"
        f"画面意图：{str(scene.get('shot_intent') or '').strip() or '用清晰的对象、环境或信息关系表达台词'}\n"
        f"视觉风格：{style}。{safe_area}\n"
        "这是已有数字人主持人的背景和辅助画面：不要再生成主播、主持人、讲解员、正脸出镜人物或第二数字人。"
        "不要生成文字、字幕、水印、品牌标识或乱码；字幕由工作台单独叠加。"
        "主体明确、层次清晰、保留底部字幕安全区，只呈现与台词有关的对象、事件、数据关系或环境。"
    )[:6000]


def _ensure_scene_visual_state(state: dict, scene: dict) -> None:
    """Add the controllable visual plan and timeline without changing a decision."""
    plan = scene.get("visual_plan") if isinstance(scene.get("visual_plan"), dict) else {}
    plan.setdefault("version", 1)
    plan.setdefault("engine", "openai_image")
    plan.setdefault("prompt", "")
    if not str(plan.get("prompt") or "").strip():
        plan["prompt"] = _default_visual_plan_prompt(state, scene)
    spec = plan.get("structured_spec") if isinstance(plan.get("structured_spec"), dict) else {}
    spec.setdefault("headline", str(scene.get("title") or ""))
    spec.setdefault("center_label", "")
    spec.setdefault("components", [])
    spec.setdefault("motion", "重点元素依次进入，镜头稳定，节奏服务于台词")
    spec.setdefault("palette", "深蓝、青色高光、克制的科技感")
    spec.setdefault("scene_recipe", "relationship_map")
    plan["structured_spec"] = spec
    constraints = [str(item) for item in (plan.get("constraints") or []) if str(item) in VISUAL_CONSTRAINTS]
    if "no_text" in constraints:
        constraints = [item for item in constraints if item != "no_text"]
        constraints.append("no_ai_baked_text")
    for required in ("no_presenter", "no_ai_baked_text", "reserve_caption_safe_area"):
        if required not in constraints:
            constraints.append(required)
    presenter = _scene_presenter(scene)
    if presenter.get("treatment") in {"pip_top_left", "custom"} and "reserve_presenter_safe_area" not in constraints:
        constraints.append("reserve_presenter_safe_area")
    plan["constraints"] = constraints
    plan.setdefault("status", "draft")
    plan.setdefault("revision", 1)
    plan.setdefault("updated_at", _now())
    if plan.get("engine") == "hyperframes":
        style_pack = plan.get("style_pack") if isinstance(plan.get("style_pack"), dict) else {}
        style_pack.setdefault("id", STYLE_PACK_ID)
        style_pack.setdefault("subtitle_mode", "inherit")
        style_pack.setdefault("subtitle_apply_scope", "scene")
        plan["style_pack"] = style_pack
    scene["visual_plan"] = plan

    timeline = scene.get("visual_timeline") if isinstance(scene.get("visual_timeline"), dict) else {}
    blocks = timeline.get("blocks") if isinstance(timeline.get("blocks"), list) else []
    if not blocks:
        selected = _selected_visual_asset(state, str(scene.get("id") or ""))
        if selected:
            blocks = [{
                "id": "VB-001",
                "start_seconds": 0.0,
                "end_seconds": _rounded_seconds(_scene_duration(scene)),
                "source_mode": _visual_source_mode(selected),
                "asset_id": selected.get("id"),
                "label": str(selected.get("name") or selected.get("id") or "主体画面"),
            }]
    timeline.update({
        "version": 1,
        "revision": max(1, int(_as_number(timeline.get("revision"), 1))),
        "blocks": blocks,
        "updated_at": timeline.get("updated_at") or _now(),
    })
    scene["visual_timeline"] = timeline
    _ensure_scene_visual_composition(scene)
    scene.setdefault("ppt_card_generation", None)
    scene.setdefault("ppt_card_candidate", None)
    brief = scene.get("ppt_card_brief")
    if not isinstance(brief, dict) or brief.get("status") != "saved":
        scene["ppt_card_brief"] = _default_ppt_card_brief(scene)


def _cancel_scene_keyframe_generation(scene: dict, reason: str) -> None:
    """Stop an in-flight keyframe job without discarding completed assets.

    A prompt, source, or presenter-layout change invalidates a pending OpenAI
    request.  The worker may still return afterwards, so the caller records a
    terminal state rather than deleting the job.  The worker then detects the
    changed revision and declines to publish stale output.
    """
    job = scene.get("keyframe_generation") if isinstance(scene.get("keyframe_generation"), dict) else None
    if not job or job.get("status") != "generating":
        return
    active_kind = str(job.get("active_anchor_kind") or "")
    anchors = job.get("anchors") if isinstance(job.get("anchors"), dict) else {}
    active = anchors.get(active_kind) if active_kind else None
    if isinstance(active, dict) and active.get("status") == "generating":
        active.update({"status": "queued", "finished_at": _now(), "error": "任务已取消，尚未采用本次结果"})
    job.update({
        "status": "cancelled",
        "active_anchor_kind": None,
        "finished_at": _now(),
        "error": reason[:300],
    })


def _cancel_scene_motion_generation(scene: dict, reason: str) -> None:
    """Cancel a queued local motion render when its inputs are superseded."""
    job = scene.get("motion_generation") if isinstance(scene.get("motion_generation"), dict) else None
    if not job or job.get("status") != "generating":
        return
    job.update({"status": "cancelled", "finished_at": _now(), "error": reason[:300]})


def _invalidate_scene_review_preview(scene: dict, reason: str) -> None:
    _ensure_scene_review_surface(scene)
    preview = scene["review_preview"]
    preview.update({
        "status": "stale" if preview.get("output_path") else "idle",
        "input_signature": None,
        "stale_reason": reason[:300],
        "error": "",
    })


def _mark_render_needs_refresh(state: dict, reason: str) -> None:
    automation = _automation(state)
    # A review candidate and the formal final are different deliverables.  A
    # local edit invalidates both, but must never erase either file: the human
    # still needs the previous version for comparison.
    for key in ("preview_render", "render"):
        job = automation.get(key) or {}
        if job.get("output_path"):
            job.update({"status": "needs_refresh", "stale_reason": reason[:300]})


def _scene_timing(scene: dict) -> dict:
    """Return the backwards-compatible timing contract for one scene.

    Script timestamps are an estimate used to plan a draft.  For narrated
    projects, ``committed_duration_seconds`` becomes the measured voice take
    once a take has been generated and approved.
    """
    start = _rounded_seconds(scene.get("start_seconds"))
    end = max(start, _rounded_seconds(scene.get("end_seconds"), start))
    timing = scene.get("timing") if isinstance(scene.get("timing"), dict) else {}
    planned_start = _rounded_seconds(timing.get("planned_start_seconds"), start)
    planned_end = max(planned_start, _rounded_seconds(timing.get("planned_end_seconds"), end))
    planned_duration = max(0.04, _rounded_seconds(timing.get("planned_duration_seconds"), planned_end - planned_start))
    authority = str(timing.get("authority") or NATURAL_NARRATION_AUTHORITY)
    if authority not in TIMELINE_AUTHORITIES:
        authority = NATURAL_NARRATION_AUTHORITY
    timing.setdefault("authority", authority)
    timing.setdefault("planned_start_seconds", planned_start)
    timing.setdefault("planned_end_seconds", planned_end)
    timing.setdefault("planned_duration_seconds", planned_duration)
    timing.setdefault("voice_duration_seconds", None)
    timing.setdefault("committed_duration_seconds", _rounded_seconds(end - start))
    timing.setdefault("duration_source", "script_estimate")
    timing.setdefault("timeline_revision", 0)
    scene["timing"] = timing
    return timing


def _ensure_timeline_state(state: dict) -> dict:
    """Add the audio-first time contract without mutating existing renders.

    Migration is deliberately additive: legacy scenes retain their current
    absolute timestamps until a new narration run or an accepted ripple edit
    explicitly commits a new timeline.
    """
    timeline = state.get("timeline") if isinstance(state.get("timeline"), dict) else {}
    project = state.setdefault("project", {})
    timeline.setdefault("authority", NATURAL_NARRATION_AUTHORITY)
    if timeline["authority"] not in TIMELINE_AUTHORITIES:
        timeline["authority"] = NATURAL_NARRATION_AUTHORITY
    timeline.setdefault("duration_policy", "natural")
    timeline.setdefault("target_duration_seconds", _rounded_seconds(project.get("duration_seconds")))
    timeline.setdefault("committed_duration_seconds", _rounded_seconds(project.get("duration_seconds")))
    timeline.setdefault("revision", 0)
    timeline.setdefault("last_change", None)
    state["timeline"] = timeline
    for scene in state.get("scenes", []):
        if isinstance(scene, dict):
            _scene_timing(scene)
    return timeline


def _update_scene_anchors_for_timing(scene: dict, old_start: float, old_end: float, new_start: float, new_end: float) -> None:
    """Move review anchors with their scene while preserving semantic position."""
    old_duration = max(0.04, old_end - old_start)
    new_duration = max(0.04, new_end - new_start)
    for anchor in scene.get("anchors", []):
        if not isinstance(anchor, dict):
            continue
        kind = str(anchor.get("kind") or "")
        if kind == "first_frame":
            position = 0.0
        elif kind == "exit_frame":
            position = 1.0
        else:
            position = (_as_number(anchor.get("time_seconds"), old_start) - old_start) / old_duration
            position = min(1.0, max(0.0, position))
        anchor["time_seconds"] = _rounded_seconds(new_start + new_duration * position)


def _sync_segments_from_scene_timeline(state: dict) -> None:
    """Derive frame/sample boundaries from committed scene timings.

    Segment artifacts remain immutable.  Under a ripple edit their *content*
    stays frozen while their absolute placement is recalculated here.
    """
    frame_rate = int(state.get("settings", {}).get("frame_rate") or 30)
    sample_rate = int(state.get("settings", {}).get("sample_rate") or 48000)
    by_scene = {
        str(segment.get("scene_ids", [""])[0]): segment
        for segment in state.get("segments", [])
        if isinstance(segment, dict) and isinstance(segment.get("scene_ids"), list) and len(segment.get("scene_ids")) == 1
    }
    for scene in sorted((item for item in state.get("scenes", []) if isinstance(item, dict)), key=lambda item: int(item.get("order") or 0)):
        segment = by_scene.get(str(scene.get("id")))
        if not segment:
            continue
        old_start = _as_number(segment.get("start_seconds"))
        old_end = _as_number(segment.get("end_seconds"))
        start = _rounded_seconds(scene.get("start_seconds"))
        end = max(start, _rounded_seconds(scene.get("end_seconds"), start))
        segment.update({
            "start_seconds": start,
            "end_seconds": end,
            "start_frame": round(start * frame_rate),
            "end_frame": round(end * frame_rate),
            "audio_start_sample": round(start * sample_rate),
            "audio_end_sample": round(end * sample_rate),
        })
        boundary = segment.setdefault("boundary_contract", {"left": {}, "right": {}})
        boundary.setdefault("left", {})["frame"] = segment["start_frame"]
        boundary.setdefault("right", {})["frame"] = segment["end_frame"]
        if segment.get("freeze") and abs(start - old_start) > 0.0005:
            snapshot = segment["freeze"]
            snapshot["content_locked"] = True
            snapshot["timeline_shift_seconds"] = _rounded_signed_seconds(_as_number(snapshot.get("timeline_shift_seconds")) + (start - old_start))
            boundary["mode"] = "content_freeze"


def _build_timeline_update(state: dict, duration_overrides: dict[str, float], *, reason: str) -> dict:
    """Calculate a ripple edit without mutating the active project state."""
    scenes = sorted((item for item in state.get("scenes", []) if isinstance(item, dict)), key=lambda item: int(item.get("order") or 0))
    if not scenes:
        raise WorkbenchError("没有场景可用于更新项目时间线")
    cursor = _rounded_seconds(scenes[0].get("start_seconds"))
    changes: list[dict] = []
    for scene in scenes:
        scene_id = str(scene.get("id") or "")
        old_start, old_end = _rounded_seconds(scene.get("start_seconds")), _rounded_seconds(scene.get("end_seconds"))
        duration = duration_overrides.get(scene_id, old_end - old_start)
        duration = max(0.04, _rounded_seconds(duration, old_end - old_start))
        new_start = cursor
        new_end = _rounded_seconds(new_start + duration)
        changes.append({
            "scene_id": scene_id,
            "old_start_seconds": old_start,
            "old_end_seconds": old_end,
            "old_duration_seconds": _rounded_seconds(old_end - old_start),
            "new_start_seconds": new_start,
            "new_end_seconds": new_end,
            "new_duration_seconds": _rounded_seconds(duration),
            "shift_seconds": _rounded_signed_seconds(new_start - old_start),
        })
        cursor = new_end
    old_total = _rounded_seconds(state.get("project", {}).get("duration_seconds"), changes[-1]["old_end_seconds"])
    return {
        "reason": reason,
        "authority": NATURAL_NARRATION_AUTHORITY,
        "duration_overrides": {key: _rounded_seconds(value) for key, value in duration_overrides.items()},
        "previous_total_duration_seconds": old_total,
        "new_total_duration_seconds": _rounded_seconds(cursor),
        "delta_seconds": _rounded_signed_seconds(cursor - old_total),
        "changes": changes,
    }


def _apply_timeline_update(state: dict, update: dict) -> dict:
    """Commit an already-reviewed timeline update and refresh derived bounds."""
    scenes = {str(scene.get("id")): scene for scene in state.get("scenes", []) if isinstance(scene, dict)}
    changed_scene_ids: list[str] = []
    for change in update.get("changes", []):
        scene = scenes.get(str(change.get("scene_id")))
        if not scene:
            continue
        old_start, old_end = _rounded_seconds(scene.get("start_seconds")), _rounded_seconds(scene.get("end_seconds"))
        new_start = _rounded_seconds(change.get("new_start_seconds"))
        new_end = max(new_start, _rounded_seconds(change.get("new_end_seconds"), new_start))
        if abs(old_start - new_start) > 0.0005 or abs(old_end - new_end) > 0.0005:
            changed_scene_ids.append(str(scene.get("id")))
        scene["start_seconds"], scene["end_seconds"] = new_start, new_end
        visual_timeline = scene.get("visual_timeline") if isinstance(scene.get("visual_timeline"), dict) else None
        if visual_timeline and isinstance(visual_timeline.get("blocks"), list) and visual_timeline["blocks"]:
            old_duration = max(.04, old_end - old_start)
            new_duration = max(.04, new_end - new_start)
            scale = new_duration / old_duration
            for index, block in enumerate(visual_timeline["blocks"]):
                block["start_seconds"] = 0.0 if index == 0 else _rounded_seconds(_as_number(block.get("start_seconds")) * scale)
                block["end_seconds"] = _rounded_seconds(_as_number(block.get("end_seconds")) * scale)
            visual_timeline["blocks"][-1]["end_seconds"] = _rounded_seconds(new_duration)
            visual_timeline["revision"] = int(_as_number(visual_timeline.get("revision"), 0)) + 1
            visual_timeline["updated_at"] = _now()
        visual_composition = scene.get("visual_composition") if isinstance(scene.get("visual_composition"), dict) else None
        if visual_composition and isinstance(visual_composition.get("overlays"), list):
            old_duration = max(.04, old_end - old_start)
            new_duration = max(.04, new_end - new_start)
            scale = new_duration / old_duration
            changed_overlay_timing = False
            for overlay in visual_composition["overlays"]:
                if not isinstance(overlay, dict):
                    continue
                next_start = min(new_duration, _rounded_seconds(_as_number(overlay.get("start_seconds")) * scale))
                next_end = min(new_duration, _rounded_seconds(_as_number(overlay.get("end_seconds")) * scale))
                next_end = max(next_start + min(.04, new_duration), next_end)
                next_end = min(new_duration, _rounded_seconds(next_end))
                if (
                    abs(_as_number(overlay.get("start_seconds")) - next_start) > .0005
                    or abs(_as_number(overlay.get("end_seconds")) - next_end) > .0005
                ):
                    changed_overlay_timing = True
                overlay["start_seconds"], overlay["end_seconds"] = next_start, next_end
            if changed_overlay_timing:
                visual_composition["revision"] = int(_as_number(visual_composition.get("revision"), 0)) + 1
                visual_composition["updated_at"] = _now()
        timing = _scene_timing(scene)
        timing["committed_duration_seconds"] = _rounded_seconds(new_end - new_start)
        timing["timeline_revision"] = int(_as_number((state.get("timeline") or {}).get("revision"))) + 1
        if str(scene.get("id")) in {str(value) for value in (update.get("duration_overrides") or {})}:
            timing["duration_source"] = "narration"
        _update_scene_anchors_for_timing(scene, old_start, old_end, new_start, new_end)
        review = scene.get("keyframe_review")
        if isinstance(review, dict) and (abs(old_start - new_start) > 0.0005 or abs(old_end - new_end) > 0.0005):
            review["timing_stale"] = True
            review["timeline_revision"] = int(_as_number((state.get("timeline") or {}).get("revision"))) + 1
    _sync_segments_from_scene_timeline(state)
    timeline = _ensure_timeline_state(state)
    timeline["revision"] = int(_as_number(timeline.get("revision"))) + 1
    timeline["committed_duration_seconds"] = _rounded_seconds(update.get("new_total_duration_seconds"))
    timeline["last_change"] = {
        "at": _now(), "reason": str(update.get("reason") or "timeline_update"),
        "delta_seconds": _rounded_signed_seconds(update.get("delta_seconds")), "changed_scene_ids": changed_scene_ids,
    }
    state["project"]["duration_seconds"] = timeline["committed_duration_seconds"]
    return timeline


def _automation_default() -> dict:
    """Return persistent state for source, narration, and video stages."""
    return {
        "status": "idle",
        "audio_mode": "generated_narration",
        "asset_generation": {"status": "idle", "total_scenes": 0, "completed_scenes": 0, "failed_scenes": []},
        "visual_batch": {
            "status": "idle", "job_id": None, "scene_ids": [], "items": [],
            "total_slots": 0, "completed_slots": 0, "failed_slots": 0, "error": "",
        },
        "preview_sync": {
            "status": "idle", "job_id": None, "scene_ids": [], "items": [],
            "total_scenes": 0, "completed_scenes": 0, "failed_scenes": 0,
            "current": None, "started_at": None, "finished_at": None, "error": "",
        },
        "media_index": {
            "status": "idle", "job_id": None, "asset_id": None, "stage": None,
            "started_at": None, "finished_at": None, "result": None, "error": "",
        },
        # A project-level queue deliberately owns batch visual understanding.
        # The underlying media runtime is single-flight, so parallel browser
        # clicks must never turn into concurrent provider calls or overwrite
        # the one durable media_index job record.
        "media_index_batch": {
            "status": "idle", "job_id": None, "stage": "vision",
            "asset_ids": [], "pending_asset_ids": [], "completed_asset_ids": [],
            "failed_assets": [], "skipped_assets": [], "current_asset_id": None,
            "started_at": None, "finished_at": None, "error": "",
        },
        "voice": {"provider": "voicebox_tts", "source": "audio_center", "label": None, "profile_id": None, "profile_name": None},
        "narration_generation": {"status": "idle", "stage": "idle", "completed_scenes": 0, "total_scenes": 0, "audio_path": None, "subtitle_path": None, "error": ""},
        "preview_render": {"status": "idle", "runtime": None, "output_path": None, "version": 0, "error": ""},
        "render": {"status": "idle", "runtime": None, "output_path": None},
        "review_preview_pipeline": {
            "version": "1.0", "job_id": None, "script_hash": None,
            "input_fingerprint": None, "request_fingerprint": None,
            "status": "idle", "stage": "preflight",
            "counts": {"total": 0, "completed": 0, "failed": 0},
            "current": None, "gate": None, "error": None,
            "safe_resume_point": None, "result": None, "frozen_input": None,
            "phases": {}, "worker_token": None,
        },
    }


_REVIEW_PREVIEW_INTERNAL_CAPABILITY = object()


def _require_no_review_preview_conflict(
    automation: dict,
    expected_job_id: object = None,
    expected_worker_token: object = None,
    internal_capability: object = None,
) -> None:
    """Keep manual media jobs mutually exclusive with the parent preview job."""
    parent = automation.get("review_preview_pipeline")
    if not isinstance(parent, dict) or parent.get("status") not in {"queued", "running", "awaiting_human"}:
        return
    if (
        parent.get("status") == "running"
        and internal_capability is _REVIEW_PREVIEW_INTERNAL_CAPABILITY
        and expected_job_id
        and expected_worker_token
        and str(parent.get("job_id") or "") == str(expected_job_id)
        and str(parent.get("worker_token") or "") == str(expected_worker_token)
    ):
        return
    raise WorkbenchError(
        "当前项目的一键审核预览任务正在运行或等待人工处理；"
        "请先完成该父任务，再启动手动旁白、画面、声音样板或全片预览"
    )


def _music_policy_default() -> dict:
    """Project-level BGM contract shared by samples, preview and final render."""
    return {
        "version": 3,
        "enabled": False,
        "category": "news",
        "track_id": None,
        "playback_gain_db": read_music_preferences().get("playback_gain_db", DEFAULT_PLAYBACK_GAIN_DB),
        "source_calibration_db": None,
        "loop": True,
        "source_start_seconds": 0.0,
        "source_end_seconds": None,
        "fade_in_seconds": 0.8,
        "fade_out_seconds": 1.5,
        "sample": {
            "status": "idle", "job_id": None, "scene_id": None, "output_path": None,
            "policy_signature": None, "generated_at": None, "approved_at": None,
            "error": "", "stale_reason": "",
        },
        "updated_at": None,
    }


def _narration_policy_default() -> dict:
    """Project-local speech gain captured from the workstation default."""
    return {
        "version": 1,
        "playback_gain_db": read_narration_preferences().get(
            "playback_gain_db", DEFAULT_NARRATION_GAIN_DB
        ),
        "updated_at": None,
    }


def _ensure_narration_policy(state: dict) -> dict:
    # Workbenches created before this feature had an implicit unity gain.
    # Migrating them to a newly chosen workstation default would silently
    # change an old approved mix, so only newly bootstrapped projects capture
    # the current preference.
    if not isinstance(state.get("narration_policy"), dict):
        state["narration_policy"] = {
            "version": 1,
            "playback_gain_db": 0.0,
            "updated_at": None,
        }
    policy = state["narration_policy"]
    defaults = _narration_policy_default()
    for key, value in defaults.items():
        policy.setdefault(key, deepcopy(value))
    policy["version"] = max(1, int(_as_number(policy.get("version")) or 1))
    policy["playback_gain_db"] = clamp_narration_gain_db(
        policy.get("playback_gain_db"), fallback=DEFAULT_NARRATION_GAIN_DB
    )
    return policy


def _audio_mix_signature_for_music_signature(state: dict, music_signature: str) -> str:
    narration = state.get("narration_policy") if isinstance(state.get("narration_policy"), dict) else {}
    payload = {
        "narration_gain_db": clamp_narration_gain_db(narration.get("playback_gain_db", 0.0)),
        "music_signature": music_signature,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _legacy_music_policy_signature_v2(policy: dict) -> str:
    """Return the pre-source-range fingerprint for safe V2 approval migration."""
    payload = {
        "enabled": bool(policy.get("enabled")),
        "track_id": policy.get("track_id"),
        "playback_gain_db": clamp_playback_gain_db(policy.get("playback_gain_db")),
        "loop": bool(policy.get("loop", True)),
        "fade_in_seconds": round(float(policy.get("fade_in_seconds") or 0.8), 3),
        "fade_out_seconds": round(float(policy.get("fade_out_seconds") or 1.5), 3),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _ensure_music_policy(state: dict) -> dict:
    policy = state.setdefault("music_policy", _music_policy_default())
    previous_version = int(_as_number(policy.get("version")) or 1)
    had_source_range = "source_start_seconds" in policy or "source_end_seconds" in policy
    sample = policy.get("sample") if isinstance(policy.get("sample"), dict) else {}
    migrate_approved_full_track = bool(
        previous_version < 3
        and not had_source_range
        and sample.get("status") == "approved"
        and sample.get("policy_signature")
        == _audio_mix_signature_for_music_signature(
            state,
            _legacy_music_policy_signature_v2(policy),
        )
    )
    defaults = _music_policy_default()
    for key, value in defaults.items():
        policy.setdefault(key, deepcopy(value))
    if not isinstance(policy.get("sample"), dict):
        policy["sample"] = deepcopy(defaults["sample"])
    else:
        for key, value in defaults["sample"].items():
            policy["sample"].setdefault(key, deepcopy(value))
    policy["version"] = max(3, int(_as_number(policy.get("version")) or 1))
    policy["playback_gain_db"] = clamp_playback_gain_db(policy.get("playback_gain_db"), fallback=DEFAULT_PLAYBACK_GAIN_DB)
    if migrate_approved_full_track:
        # V2 had no explicit range and always meant the complete selected
        # track.  Preserve a cryptographically matching approval when V3 adds
        # the equivalent 0 -> end defaults; never migrate a mismatched sample.
        policy["sample"]["policy_signature"] = _audio_mix_signature_for_music_signature(
            state,
            _music_policy_signature(policy),
        )
    return policy


def _automation(state: dict) -> dict:
    automation = state.setdefault("automation", _automation_default())
    defaults = _automation_default()
    added_narration_stage = "narration_generation" not in automation
    for key, value in defaults.items():
        if key not in automation:
            automation[key] = deepcopy(value)
        elif isinstance(value, dict) and isinstance(automation.get(key), dict):
            for nested_key, nested_value in value.items():
                automation[key].setdefault(nested_key, deepcopy(nested_value))
    # A failed legacy "voice + render" job must not poison the newly
    # separated flow.  Keep historical fields for audit, but start the new
    # narration/render stages from the already-completed material step.
    legacy_job = automation.get("final_generation") or {}
    if added_narration_stage and legacy_job.get("status") in {"failed", "generating"}:
        automation["render"] = deepcopy(defaults["render"])
        asset_status = (automation.get("asset_generation") or {}).get("status")
        automation["status"] = "assets_ready" if asset_status == "completed" else "assets_ready_with_warnings" if asset_status == "completed_with_warnings" else "idle"
    return automation


def _scene_narration_default(text: str = "") -> dict:
    """Return the project-local narration state for one reviewable scene.

    The global audio centre owns *voice identities*.  A project owns each
    generated take, its time-aligned asset and the explicit decision to make
    that take the current narration for a scene.  Keeping this record on the
    scene lets the review UI audition a candidate without changing the final
    video or another scene.
    """
    return {
        "status": "idle",
        "text": text,
        "versions": [],
        "current_version_id": None,
        "candidate_version_id": None,
        "job": {"status": "idle", "error": ""},
    }


def _is_avatar_project(state: dict) -> bool:
    return str((state.get("project") or {}).get("pipeline_type") or "") == AVATAR_PIPELINE


def _presenter_default() -> dict:
    """Return the scene-local presenter contract.

    The contract deliberately stores a source *path* as well as an S-xxx id.
    The S-xxx record may advance to a newer master version after a local
    replacement, while an already approved scene must keep pointing at the
    exact avatar master revision that was reviewed.
    """
    return {
        "treatment": "hidden",
        "asset_id": None,
        "asset_version_id": None,
        "source_path": None,
        "source_start_seconds": None,
        "source_end_seconds": None,
        "turn_id": None,
        "audio_mode": "native_avatar_audio",
        "timeline_revision": None,
        # Layout is deliberately separate from the chosen source and time
        # bounds.  Moving a presenter must never change its native audio.
        "layout_template_id": "pip_top_right",
        "layout_override": None,
        "shape": "rounded",
        # A template-local circular-avatar framing. ``None`` means inherit
        # from the selected template, so one approved face crop can be reused
        # across every turn of the same speaker.
        "face_crop": None,
        # Source-local trim for provider videos that include a blurred or
        # padded lower edge. It never changes the avatar audio timeline.
        "crop_bottom": 0.0,
    }


def _normalized_presenter_geometry(raw: Any, fallback: dict | None = None) -> dict:
    """Validate a normalized, no-distortion presenter placement.

    Width controls the presenter size; height is derived from the source video
    at render time.  This avoids the common UI bug where free width/height
    controls accidentally squash a talking-head video.
    """
    source = raw if isinstance(raw, dict) else {}
    base = fallback if isinstance(fallback, dict) else {"x": .035, "y": .04, "width": .29}
    width = min(.70, max(.12, _as_number(source.get("width"), _as_number(base.get("width"), .29))))
    x = min(1.0 - width, max(0.0, _as_number(source.get("x"), _as_number(base.get("x"), .035))))
    # Leave the lower fifth of the canvas available for responsive captions.
    y = min(.72, max(0.0, _as_number(source.get("y"), _as_number(base.get("y"), .04))))
    return {"x": round(x, 4), "y": round(y, 4), "width": round(width, 4)}


def _normalized_presenter_crop_bottom(value: Any) -> float:
    """Keep the source cleanup control conservative and predictable."""
    return round(min(.30, max(0.0, _as_number(value, 0.0))), 4)


def _normalized_presenter_face_crop(raw: Any, fallback: dict | None = None) -> dict:
    """Validate manual circular-avatar framing without ever stretching video.

    ``x``/``y`` are the normalized available crop travel, not source pixels:
    0 is left/top, 1 is right/bottom.  ``zoom`` reduces the square source crop
    before scaling it back to the same avatar box.
    """
    source = raw if isinstance(raw, dict) else {}
    base = fallback if isinstance(fallback, dict) else {"x": .5, "y": 0, "zoom": 1}
    return {
        "x": round(min(1.0, max(0.0, _as_number(source.get("x"), _as_number(base.get("x"), .5)))), 4),
        "y": round(min(1.0, max(0.0, _as_number(source.get("y"), _as_number(base.get("y"), 0)))), 4),
        "zoom": round(min(2.4, max(1.0, _as_number(source.get("zoom"), _as_number(base.get("zoom"), 1)))), 4),
    }


def _normalized_presenter_shape(value: Any, fallback: str = "rounded") -> str:
    shape = str(value or fallback).strip().lower()
    return shape if shape in PRESENTER_SHAPES else fallback


def _normalized_story_headline_layout(raw: Any, fallback: dict | None = None) -> dict:
    """Validate project-level story headline placement in normalized canvas units."""
    source = raw if isinstance(raw, dict) else {}
    base = fallback if isinstance(fallback, dict) else STORY_HEADLINE_LAYOUT_DEFAULT
    width = min(.92, max(.24, _as_number(source.get("width"), _as_number(base.get("width"), .58))))
    height = min(.24, max(.07, _as_number(source.get("height"), _as_number(base.get("height"), .125))))
    x = min(1.0 - width, max(0.0, _as_number(source.get("x"), _as_number(base.get("x"), .36))))
    y = min(1.0 - height, max(0.0, _as_number(source.get("y"), _as_number(base.get("y"), .09))))
    return {"x": round(x, 4), "y": round(y, 4), "width": round(width, 4), "height": round(height, 4)}


def _ensure_story_headline_layout(state: dict) -> dict:
    layout = _normalized_story_headline_layout(state.get("story_headline_layout"))
    state["story_headline_layout"] = layout
    return layout


def _ensure_presenter_layout_state(state: dict) -> dict:
    layouts = state.get("presenter_layouts") if isinstance(state.get("presenter_layouts"), dict) else {}
    existing = {
        str(item.get("id")): item
        for item in (layouts.get("templates") or [])
        if isinstance(item, dict) and item.get("id")
    }
    templates: list[dict] = []
    for default in PRESENTER_LAYOUT_DEFAULTS:
        item = existing.pop(default["id"], {})
        templates.append({
            "id": default["id"],
            "name": str(item.get("name") or default["name"])[:80],
            "geometry": _normalized_presenter_geometry(item.get("geometry"), default["geometry"]),
            "crop_bottom": _normalized_presenter_crop_bottom(item.get("crop_bottom")),
            "shape": _normalized_presenter_shape(item.get("shape"), default.get("shape", "rounded")),
            "face_crop": _normalized_presenter_face_crop(item.get("face_crop"), default.get("face_crop")),
            "builtin": True,
            "revision": max(1, int(_as_number(item.get("revision"), 1))),
            "updated_at": item.get("updated_at") or _now(),
        })
    for item in existing.values():
        if len(templates) >= PRESENTER_LAYOUT_TEMPLATE_LIMIT:
            break
        template_id = re.sub(r"[^a-z0-9_-]", "-", str(item.get("id") or "").lower()).strip("-")
        if not template_id or template_id in {entry["id"] for entry in templates}:
            continue
        templates.append({
            "id": template_id,
            "name": str(item.get("name") or "自定义版式")[:80],
            "geometry": _normalized_presenter_geometry(item.get("geometry")),
            "crop_bottom": _normalized_presenter_crop_bottom(item.get("crop_bottom")),
            "shape": _normalized_presenter_shape(item.get("shape")),
            "face_crop": _normalized_presenter_face_crop(item.get("face_crop")),
            "builtin": False,
            "revision": max(1, int(_as_number(item.get("revision"), 1))),
            "updated_at": item.get("updated_at") or _now(),
        })
    layouts = {
        "version": max(1, int(_as_number(layouts.get("version"), 1))),
        "default_template_id": str(layouts.get("default_template_id") or "pip_top_right"),
        "templates": templates,
    }
    if layouts["default_template_id"] not in {item["id"] for item in templates}:
        layouts["default_template_id"] = "pip_top_right"
    state["presenter_layouts"] = layouts
    return layouts


def _presenter_layout(state: dict, presenter: dict) -> dict:
    layouts = _ensure_presenter_layout_state(state)
    templates = {str(item["id"]): item for item in layouts["templates"]}
    template_id = str(presenter.get("layout_template_id") or layouts["default_template_id"])
    template = templates.get(template_id) or templates[layouts["default_template_id"]]
    return {
        "template_id": template["id"],
        "template_name": template["name"],
        "geometry": _normalized_presenter_geometry(presenter.get("layout_override"), template["geometry"]),
        "crop_bottom": _normalized_presenter_crop_bottom(presenter.get("crop_bottom", template.get("crop_bottom"))),
        "shape": _normalized_presenter_shape(presenter.get("shape"), template.get("shape", "rounded")),
        "face_crop": _normalized_presenter_face_crop(presenter.get("face_crop"), template.get("face_crop")),
        "has_override": isinstance(presenter.get("layout_override"), dict),
    }


def _scene_presenter(scene: dict) -> dict:
    presenter = scene.get("presenter")
    if not isinstance(presenter, dict):
        presenter = _presenter_default()
        scene["presenter"] = presenter
    for key, value in _presenter_default().items():
        presenter.setdefault(key, value)
    if presenter.get("treatment") not in PRESENTER_TREATMENTS:
        presenter["treatment"] = "hidden"
    return presenter


def _narration_version_id(scene: dict) -> str:
    prefix = f"{scene['id']}-NAR-V"
    return _numbered(prefix, (scene.get("narration") or {}).get("versions") or [], "id")


def _scene_text(project_dir: Path, state: dict, scene: dict) -> str:
    narration = scene.get("narration") if isinstance(scene.get("narration"), dict) else {}
    explicit = str(narration.get("text") or "").strip()
    if explicit:
        return explicit
    section = _script_sections(project_dir, state).get(str(scene.get("script_section_id"))) or {}
    return str(section.get("text") or scene.get("description") or "").strip()


def _ensure_scene_narrations(project_dir: Path, state: dict) -> None:
    """Migrate existing project narration into the per-scene version model.

    Earlier workbenches stored only a selected U-xxx narration usage.  The
    migration is deliberately additive: old assets/usages remain untouched,
    while the scene gains a current version that points to that same asset.
    """
    asset_map = {str(asset.get("id")): asset for asset in state.get("assets", []) if isinstance(asset, dict)}
    for scene in state.get("scenes", []):
        if not isinstance(scene, dict):
            continue
        default_text = _scene_text(project_dir, state, scene)
        narration = scene.get("narration")
        if not isinstance(narration, dict):
            narration = _scene_narration_default(default_text)
            scene["narration"] = narration
        for key, value in _scene_narration_default(default_text).items():
            narration.setdefault(key, deepcopy(value))
        if not narration.get("text"):
            narration["text"] = default_text
        if narration.get("versions"):
            continue
        usage = next((item for item in state.get("usages", []) if item.get("scene_id") == scene.get("id") and item.get("role") == "narration" and item.get("selected")), None)
        asset = asset_map.get(str((usage or {}).get("asset_id") or ""))
        if not asset or not asset.get("path"):
            continue
        version_id = _narration_version_id(scene)
        generation = asset.get("generation") if isinstance(asset.get("generation"), dict) else {}
        narration["versions"].append({
            "id": version_id,
            "status": "current",
            "text": narration["text"],
            "asset_id": asset["id"],
            "audio_path": asset["path"],
            "profile_id": generation.get("profile_id"),
            "profile_name": generation.get("profile_name") or generation.get("voice_label"),
            "created_at": asset.get("created_at") or _now(),
            "migrated_from_usage_id": (usage or {}).get("id"),
        })
        narration["current_version_id"] = version_id
        narration["status"] = "ready"


def voice_catalog() -> dict:
    """Expose the reusable Voicebox catalogue without making it project data."""
    return read_audio_center()


def _safe_relpath(project_dir: Path, raw_path: str | None) -> str | None:
    """Return a project-relative path, refusing external asset references."""
    if not raw_path:
        return None
    candidate = Path(raw_path)
    project_root = project_dir.resolve()
    if candidate.is_absolute():
        resolved = candidate.resolve()
    else:
        # Some render helpers construct a cwd-relative path that already
        # includes ``projects/<project-id>``.  Prefixing project_dir again
        # persisted paths such as
        # ``projects/<id>/projects/<id>/renders/...`` even though the output
        # file itself was valid.  Accept that already project-rooted form;
        # otherwise keep the normal project-relative interpretation.
        direct = candidate.resolve()
        try:
            direct.relative_to(project_root)
        except (OSError, ValueError):
            resolved = (project_dir / candidate).resolve()
        else:
            resolved = direct
    try:
        relative = resolved.relative_to(project_root)
    except (OSError, ValueError):
        raise WorkbenchError("素材路径必须位于当前项目目录内")
    return relative.as_posix()


def _source_type(asset: dict) -> str:
    provider = str(asset.get("provider") or "").lower()
    tool = str(asset.get("source_tool") or "").lower()
    if "provided" in tool or provider in {"local", "user"}:
        return "human_provided"
    if any(word in tool for word in ("pexels", "pixabay", "stock", "download")):
        return "web_download"
    if any(word in tool for word in ("generate", "imagen", "kling", "video_gen", "image_gen")):
        return "ai_generated"
    return "undecided"


def _anchor(kind: str, seconds: float, label: str) -> dict:
    return {
        "kind": kind,
        "label": label,
        "time_seconds": round(max(0, seconds), 3),
        "status": "pending",
        "note": "",
        "reviewed_at": None,
    }


def _scene_from_script_section(raw: dict, order: int) -> dict:
    """Convert one approved script section into a reviewable scene record."""
    start = _as_number(raw.get("start_seconds"))
    end = max(start, _as_number(raw.get("end_seconds"), start))
    cues = raw.get("enhancement_cues") if isinstance(raw.get("enhancement_cues"), list) else []
    cue_text = [str(cue.get("description") or "").strip() for cue in cues if isinstance(cue, dict)]
    cue_text = [text for text in cue_text if text]
    cue_times = [
        _as_number(cue.get("timestamp_seconds"), start)
        for cue in cues if isinstance(cue, dict) and cue.get("timestamp_seconds") is not None
    ]
    climax = min(max(max(cue_times, default=start + (end - start) / 2), start), end)
    title = str(raw.get("label") or f"场景 {order}")
    description = str(raw.get("text") or "")
    shot_intent = "；".join(cue_text) or "根据旁白建立画面，高潮帧突出本段核心信息"
    return {
        "id": str(raw.get("id") or f"scene-{order}"),
        "order": order,
        "title": title,
        "description": description,
        "start_seconds": start,
        "end_seconds": end,
        "script_section_id": raw.get("id"),
        "shot_intent": shot_intent,
        "hero_moment": False,
        "source_strategy": "undecided",
        "review_status": "pending",
        "anchors": [
            _anchor("first_frame", start, "首帧"),
            _anchor("climax_frame", climax, "高潮帧"),
            _anchor("exit_frame", end, "出场帧"),
        ],
        "keyframe_review": None,
        "keyframe_generation": None,
        "review_preview": _review_preview_default(),
        "surgical_directives": [],
        "presenter": _presenter_default(),
        "narration": _scene_narration_default(description),
        "notes": [],
    }


def _segments_for_scenes(scenes: list[dict], frame_rate: int, sample_rate: int) -> list[dict]:
    segments: list[dict] = []
    for index, scene in enumerate(scenes, 1):
        start, end = _as_number(scene["start_seconds"]), _as_number(scene["end_seconds"])
        segment_id = f"SEG-{index:03d}"
        version_id = f"{segment_id}-V001"
        segments.append({
            "id": segment_id,
            "order": index,
            "scene_ids": [scene["id"]],
            "start_seconds": start, "end_seconds": end,
            "start_frame": round(start * frame_rate), "end_frame": round(end * frame_rate),
            "audio_start_sample": round(start * sample_rate), "audio_end_sample": round(end * sample_rate),
            "state": "editable", "freeze": None,
            "boundary_contract": {
                "left": {"locked": False, "frame": round(start * frame_rate)},
                "right": {"locked": False, "frame": round(end * frame_rate)},
                "mode": "content_freeze",
                "content_locked": False,
                "timecode_locked": False,
            },
            "versions": [{"id": version_id, "status": "current", "created_at": _now(), "input_hash": None}],
            "current_version_id": version_id,
        })
    return segments


def _normalize_intake(raw: Any) -> dict:
    """Return a stable, forward-compatible production-intake record."""
    source = raw if isinstance(raw, dict) else {}
    intake = {
        "brief": str(source.get("brief") or "").strip()[:3000],
        "video_title": str(source.get("video_title") or "").strip()[:200],
        "source_text": str(source.get("source_text") or "")[:20000],
        "duration_seconds": int(_as_number(source.get("duration_seconds"), 15) or 15),
        "aspect": str(source.get("aspect") or "landscape"),
        "aspect_label": str(source.get("aspect_label") or "横版 16:9"),
        "script_status": str(source.get("script_status") or "unknown"),
        "script_mode": str(source.get("script_mode") or ""),
        "organize_strength": str(source.get("organize_strength") or "faithful"),
        "script_text": str(source.get("script_text") or "")[:20000],
        "idea": str(source.get("idea") or "")[:5000],
        "materials_status": str(source.get("materials_status") or "unknown"),
        "style_status": str(source.get("style_status") or "unknown"),
        "style_reference": str(source.get("style_reference") or "")[:2000],
        "style_direction": str(source.get("style_direction") or "")[:1000],
        "audience": str(source.get("audience") or "")[:500],
        "content_goal": str(source.get("content_goal") or "")[:1000],
        "duration_source": str(source.get("duration_source") or "user_target"),
    }
    if intake["script_status"] not in INTAKE_SCRIPT_STATUSES:
        intake["script_status"] = "unknown"
    if intake["script_mode"] not in SCRIPT_DRAFT_MODES:
        intake["script_mode"] = ""
    if intake["organize_strength"] not in {"faithful", "light_polish"}:
        intake["organize_strength"] = "faithful"
    if intake["materials_status"] not in INTAKE_MATERIAL_STATUSES:
        intake["materials_status"] = "unknown"
    if intake["style_status"] not in INTAKE_STYLE_STATUSES:
        intake["style_status"] = "unknown"
    avatar_source = source.get("avatar") if isinstance(source.get("avatar"), dict) else None
    if avatar_source is not None:
        avatar = {
            "source_status": str(avatar_source.get("source_status") or "planned"),
            "generation_mode": str(avatar_source.get("generation_mode") or "runninghub_longcat"),
            "import_mode": str(avatar_source.get("import_mode") or "per_turn"),
            "default_treatment": str(avatar_source.get("default_treatment") or "fullscreen"),
            "background_mode": str(avatar_source.get("background_mode") or "opaque"),
        }
        if avatar["source_status"] not in {"ready", "planned"}:
            avatar["source_status"] = "planned"
        if avatar["generation_mode"] not in {"runninghub_longcat", "dashscope_wan_s2v", "manual_import"}:
            avatar["generation_mode"] = "runninghub_longcat"
        if avatar["import_mode"] not in {"per_turn", "longform"}:
            avatar["import_mode"] = "per_turn"
        if avatar["default_treatment"] not in PRESENTER_TREATMENTS:
            avatar["default_treatment"] = "fullscreen"
        if avatar["background_mode"] not in {"opaque", "green_screen", "transparent", "unknown"}:
            avatar["background_mode"] = "opaque"
        intake["avatar"] = avatar
    for key in ("updated_at", "created_from"):
        if source.get(key):
            intake[key] = source[key]
    return intake


def _build_default(project_dir: Path) -> dict:
    checkpoints = _collect_checkpoints(project_dir)
    artifacts = _collect_artifacts(project_dir, checkpoints)
    project = _read_json(project_dir / "project.json") or {}
    scene_plan = artifacts.get("scene_plan") or {}
    script = artifacts.get("script") or {}
    manifest = artifacts.get("asset_manifest") or {}
    intake = _normalize_intake(project.get("intake"))
    frame_rate = int((project.get("render_profile") or {}).get("fps") or 30)
    sample_rate = int((project.get("render_profile") or {}).get("audio_sample_rate") or 48000)

    raw_scenes = scene_plan.get("scenes") if isinstance(scene_plan.get("scenes"), list) else []
    scenes: list[dict] = []
    for order, raw in enumerate(raw_scenes, 1):
        if not isinstance(raw, dict):
            continue
        start = _as_number(raw.get("start_seconds"))
        end = max(start, _as_number(raw.get("end_seconds"), start))
        midpoint = start + (end - start) * (0.66 if raw.get("hero_moment") else 0.5)
        scenes.append({
            "id": str(raw.get("id") or f"scene-{order}"),
            "order": order,
            "title": str(raw.get("title") or raw.get("description") or f"场景 {order}"),
            "description": str(raw.get("description") or ""),
            "start_seconds": start,
            "end_seconds": end,
            "script_section_id": raw.get("script_section_id"),
            "shot_intent": raw.get("shot_intent") or raw.get("information_role") or "待定义",
            "hero_moment": bool(raw.get("hero_moment")),
            "source_strategy": "undecided",
            "review_status": "pending",
            "anchors": [
                _anchor("first_frame", start, "首帧"),
                _anchor("climax_frame", midpoint, "高潮帧"),
                _anchor("exit_frame", end, "出场帧"),
            ],
            "keyframe_review": None,
            "keyframe_generation": None,
            "review_preview": _review_preview_default(),
            "surgical_directives": [],
            "presenter": _presenter_default(),
            "narration": _scene_narration_default(str(raw.get("description") or "")),
            "notes": [],
        })

    if not scenes:
        raw_sections = script.get("sections") if isinstance(script.get("sections"), list) else []
        for order, raw in enumerate(raw_sections, 1):
            if not isinstance(raw, dict):
                continue
            scenes.append(_scene_from_script_section(raw, order))

    assets: list[dict] = []
    usages: list[dict] = []
    raw_assets = manifest.get("assets") if isinstance(manifest.get("assets"), list) else []
    for index, raw in enumerate(raw_assets, 1):
        if not isinstance(raw, dict):
            continue
        stable_id = f"S-{index:03d}"
        raw_path = raw.get("path")
        try:
            path = _safe_relpath(project_dir, str(raw_path)) if raw_path else None
        except WorkbenchError:
            path = None
        asset = {
            "id": stable_id,
            "legacy_id": raw.get("id"),
            "name": str(raw.get("id") or raw.get("subtype") or f"素材 {index}"),
            "type": str(raw.get("type") or "unknown"),
            "source_type": _source_type(raw),
            "path": path,
            "duration_seconds": raw.get("duration_seconds"),
            "resolution": raw.get("resolution"),
            "provenance": {
                "provider": raw.get("provider"),
                "source_tool": raw.get("source_tool"),
                "license": raw.get("license") or "待补充",
                "source_url": raw.get("source_url"),
            },
            "versions": [{"id": f"{stable_id}-V001", "created_at": _now(), "path": path, "status": "current"}],
            "created_at": _now(),
        }
        assets.append(asset)
        scene_id = raw.get("scene_id")
        if scene_id and scene_id != "all":
            usages.append({
                "id": _numbered("U-", usages, "id"), "asset_id": stable_id,
                "scene_id": str(scene_id), "role": raw.get("type") or "visual",
                "selected": True, "transform": {"crop": None, "scale": 1, "speed": 1},
                "created_at": _now(),
            })

    duration = max([
        _as_number(s.get("end_seconds")) for s in scenes
    ] or [_as_number(intake.get("duration_seconds"))])
    segments = _segments_for_scenes(scenes, frame_rate, sample_rate)

    avatar_package = read_avatar_package(project_dir)
    state = {
        "schema_version": WORKBENCH_VERSION,
        "persisted": False,
        "project": {
            "id": str(project.get("project_id") or project_dir.name),
            "title": str(project.get("title") or script.get("title") or project_dir.name),
            "locale": "zh-CN",
            "pipeline_type": project.get("pipeline_type"),
            "duration_seconds": duration,
            "intake": intake,
        },
        "settings": {"frame_rate": frame_rate, "sample_rate": sample_rate, "render_profile": "source"},
        "presenter_layouts": {
            "version": 1,
            "default_template_id": "pip_top_right",
            "templates": [
                {"id": item["id"], "name": item["name"], "geometry": item["geometry"], "crop_bottom": 0.0, "shape": item.get("shape", "rounded"), "face_crop": _normalized_presenter_face_crop(item.get("face_crop")), "builtin": True, "revision": 1, "updated_at": _now()}
                for item in PRESENTER_LAYOUT_DEFAULTS
            ],
        },
        "story_headline_layout": deepcopy(STORY_HEADLINE_LAYOUT_DEFAULT),
        "subtitle_styles": {
            "version": 1,
            "default_template_id": "subtitle-default",
            "templates": _subtitle_template_defaults(),
        },
        "scenes": scenes,
        "assets": assets,
        "usages": usages,
        "segments": segments,
        "patches": [],
        "keyframe_reviews": [],
        "automation": _automation_default(),
        "narration_policy": _narration_policy_default(),
        "music_policy": _music_policy_default(),
        "avatar_package": avatar_package,
        "avatar": {
            "status": "not_configured",
            "default_treatment": "fullscreen",
            "master_asset_id": None,
            "package_revision": None,
            "turns": {},
        } if project.get("pipeline_type") == AVATAR_PIPELINE else None,
        "decisions": [],
        "activities": [{
            "at": _now(),
            "kind": "import",
            "message": "已从现有脚本、分镜与素材清单生成工作台草稿" if scenes else "项目已创建，等待导入或生成脚本与分镜",
        }],
        "updated_at": _now(),
    }
    # The workstation default is captured only on first creation.  Existing
    # project contracts must remain stable even after the user changes the
    # default for their next video.
    state["subtitle_styles"]["templates"][0]["style"] = _software_default_subtitle_style()
    if avatar_package:
        state["automation"]["audio_mode"] = "native_avatar_audio"
    _ensure_timeline_state(state)
    _ensure_subtitle_style_state(state)
    for scene in state.get("scenes", []):
        if isinstance(scene, dict):
            _ensure_scene_review_surface(scene)
            _scene_subtitles(scene)
            _ensure_scene_visual_state(state, scene)
    return state


def _workbench_path(project_dir: Path) -> Path:
    return project_dir / WORKBENCH_FILE


def read_workbench(project_dir: Path) -> dict:
    existing = _read_json(_workbench_path(project_dir))
    if existing and isinstance(existing.get("scenes"), list):
        existing.setdefault("persisted", True)
        existing.setdefault("project", {})
        existing["project"]["intake"] = _normalize_intake(existing["project"].get("intake"))
        existing.setdefault("patches", [])
        existing.setdefault("keyframe_reviews", [])
        existing.setdefault("decisions", [])
        existing.setdefault("activities", [])
        if _is_avatar_project(existing):
            existing.setdefault("avatar", {
                "status": "not_configured",
                "default_treatment": "fullscreen",
                "master_asset_id": None,
                "package_revision": None,
                "turns": {},
            })
        _automation(existing)
        _ensure_narration_policy(existing)
        _ensure_music_policy(existing)
        _ensure_timeline_state(existing)
        _ensure_subtitle_style_state(existing)
        existing["avatar_package"] = read_avatar_package(project_dir)
        if (existing["avatar_package"] or {}).get("generation_mode") in {"dashscope_wan_s2v", "runninghub_longcat"}:
            from backlot.avatar_cloud import reconcile_cloud_avatar_package
            existing["avatar_package"] = reconcile_cloud_avatar_package(project_dir)
        if existing["avatar_package"]:
            existing["automation"]["audio_mode"] = "native_avatar_audio"
        for scene in existing.get("scenes", []):
            if isinstance(scene, dict):
                scene.setdefault("keyframe_review", None)
                scene.setdefault("keyframe_generation", None)
                _scene_presenter(scene)
                _ensure_scene_review_surface(scene)
                _scene_subtitles(scene)
                _ensure_scene_visual_state(existing, scene)
        _ensure_presenter_layout_state(existing)
        _ensure_story_headline_layout(existing)
        _ensure_scene_narrations(project_dir, existing)
        # This is intentionally derived on every read rather than persisted:
        # it is a truthful readiness report, not another job state that can
        # drift away from the actual selected visual blocks.
        existing["full_preview"] = _full_preview_summary(existing)
        return existing
    return _build_default(project_dir)


def bootstrap_workbench(project_dir: Path) -> dict:
    state = read_workbench(project_dir)
    if state.get("persisted"):
        return state
    state["persisted"] = True
    state["updated_at"] = _now()
    _atomic_write(_workbench_path(project_dir), state)
    return state


def _load_for_write(project_dir: Path) -> dict:
    return bootstrap_workbench(project_dir)


def _save(project_dir: Path, state: dict) -> dict:
    # A visual slot worker operates on an isolated snapshot while its network
    # or renderer call is in flight.  Nested helpers may still report progress
    # through _save(); defer those writes until the slot-level CAS merges only
    # the proven delta into the latest project state.
    if state.get(_DEFER_VISUAL_SLOT_STATE_SAVE) is True:
        return state
    state["persisted"] = True
    state["updated_at"] = _now()
    _atomic_write(_workbench_path(project_dir), state)
    return state


def read_music_catalog(project_dir: Path) -> dict:
    """Return the audio library plus independent speech/music policies."""
    state = read_workbench(project_dir)
    catalog = list_music_tracks(project_dir)
    policy = deepcopy(_ensure_music_policy(state))
    if not policy.get("track_id") and catalog.get("tracks"):
        policy["track_id"] = catalog["tracks"][0]["id"]
    return {
        **catalog,
        "policy": policy,
        "defaults": read_music_preferences(),
        "narration_policy": deepcopy(_ensure_narration_policy(state)),
        "narration_defaults": read_narration_preferences(),
    }


def _music_policy_signature(policy: dict) -> str:
    """Fingerprint only the settings that affect audible output."""
    payload = {
        "enabled": bool(policy.get("enabled")),
        "track_id": policy.get("track_id"),
        "playback_gain_db": clamp_playback_gain_db(policy.get("playback_gain_db")),
        "loop": bool(policy.get("loop", True)),
        "source_start_seconds": round(float(policy.get("source_start_seconds") or 0.0), 3),
        "source_end_seconds": (
            round(float(policy.get("source_end_seconds")), 3)
            if policy.get("source_end_seconds") is not None else None
        ),
        "fade_in_seconds": round(float(policy.get("fade_in_seconds") or 0.8), 3),
        "fade_out_seconds": round(float(policy.get("fade_out_seconds") or 1.5), 3),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def _audio_mix_signature(state: dict) -> str:
    """Fingerprint every setting that changes the audible first-scene mix."""
    _ensure_narration_policy(state)
    return _audio_mix_signature_for_music_signature(
        state,
        _music_policy_signature(_ensure_music_policy(state)),
    )


def _stale_music_sample(policy: dict, reason: str) -> None:
    sample = policy.setdefault("sample", {})
    had_output = bool(sample.get("output_path"))
    sample.update({
        "status": "stale" if had_output else "idle", "job_id": None,
        "policy_signature": None, "approved_at": None, "error": "",
        "stale_reason": reason[:300],
    })


def read_music_preferences_settings() -> dict:
    """Expose only non-sensitive, software-wide BGM defaults to the UI."""
    return read_music_preferences()


def update_music_preferences_settings(payload: dict) -> dict:
    """Persist the default for *future* projects without touching existing ones."""
    return save_music_preferences(payload)


def read_narration_preferences_settings() -> dict:
    """Expose the non-sensitive default for future projects."""
    return read_narration_preferences()


def update_narration_preferences_settings(payload: dict) -> dict:
    """Persist the future-project speech gain without changing old projects."""
    return save_narration_preferences(payload)


def update_narration_policy(project_dir: Path, payload: dict) -> dict:
    """Persist project speech gain while preserving every immutable source."""
    state = _load_for_write(project_dir)
    previous = deepcopy(_ensure_narration_policy(state))
    gain_db = clamp_narration_gain_db(
        payload.get("playback_gain_db", previous.get("playback_gain_db"))
    )
    policy = _narration_policy_default()
    policy.update({
        "playback_gain_db": gain_db,
        "updated_at": previous.get("updated_at"),
    })
    changed = gain_db != previous.get("playback_gain_db")
    if changed:
        policy["updated_at"] = _now()
        _stale_music_sample(
            _ensure_music_policy(state),
            "人物台词音量已修改，请重新生成第一段声音样板",
        )
        _mark_render_needs_refresh(state, "人物台词音量已修改，请重新生成全片预览")
        _decision(
            state,
            "narration_gain",
            "全片人物台词音量",
            f"{gain_db:+.1f} dB",
            "仅改变可重建混音衍生物，不覆盖本地配音音频或数字人原片",
        )
        _activity(
            state,
            "narration_gain_updated",
            f"人物台词音量已保存为 {gain_db:+.1f} dB；请重新试听第一段声音样板",
        )
    state["narration_policy"] = policy
    return _save(project_dir, state)


def update_music_policy(project_dir: Path, payload: dict) -> dict:
    """Persist validated BGM settings and require a fresh real-mix sample."""
    state = _load_for_write(project_dir)
    previous = deepcopy(_ensure_music_policy(state))
    enabled = payload.get("enabled") is True
    track_id = str(payload.get("track_id") or previous.get("track_id") or "").strip()
    track: dict[str, Any] | None = None
    if track_id:
        try:
            _path, track = resolve_music_track(track_id, project_dir)
        except MusicLibraryError as exc:
            raise WorkbenchError(str(exc)) from exc
    if enabled and not track:
        raise WorkbenchError("请先选择一首可用的新闻背景音乐")

    source_start_seconds = 0.0
    source_end_seconds: float | None = None
    if track:
        duration_seconds = max(0.0, float(track.get("duration_seconds") or 0.0))
        same_track = track_id == str(previous.get("track_id") or "")
        raw_start = payload.get(
            "source_start_seconds",
            previous.get("source_start_seconds") if same_track else 0.0,
        )
        raw_end = payload.get(
            "source_end_seconds",
            previous.get("source_end_seconds") if same_track else duration_seconds,
        )
        try:
            source_start_seconds = round(float(raw_start or 0.0), 3)
            source_end_seconds = round(float(duration_seconds if raw_end is None else raw_end), 3)
        except (TypeError, ValueError) as exc:
            raise WorkbenchError("背景音乐选区起止时间必须是数字") from exc
        if source_start_seconds < 0 or source_end_seconds <= source_start_seconds:
            raise WorkbenchError("背景音乐选区必须满足：起点不小于 0，终点晚于起点")
        if source_end_seconds > duration_seconds + 0.01:
            raise WorkbenchError("背景音乐选区终点不能超过音轨时长")
        if source_end_seconds - source_start_seconds < 1.0:
            raise WorkbenchError("背景音乐选区至少保留 1 秒")

    requested_gain = payload.get("playback_gain_db", previous.get("playback_gain_db"))
    policy = _music_policy_default()
    policy.update({
        "enabled": enabled,
        "track_id": track_id or None,
        "playback_gain_db": clamp_playback_gain_db(requested_gain),
        "source_calibration_db": (track or {}).get("source_calibration_db"),
        "source_start_seconds": source_start_seconds,
        "source_end_seconds": source_end_seconds,
        "sample": deepcopy(previous.get("sample") or policy["sample"]),
    })
    comparison_keys = (
        "enabled", "track_id", "playback_gain_db", "source_calibration_db",
        "loop", "source_start_seconds", "source_end_seconds",
        "fade_in_seconds", "fade_out_seconds",
    )
    changed = any(policy.get(key) != previous.get(key) for key in comparison_keys)
    policy["updated_at"] = _now() if changed else previous.get("updated_at")
    if changed:
        _stale_music_sample(policy, "背景音乐设置已修改，请重新生成第一段试听样板")
    state["music_policy"] = policy
    if changed:
        _mark_render_needs_refresh(state, "背景音乐设置已修改，请重新生成全片预览")
        selected = (track or {}).get("title") if enabled else "不添加背景音乐"
        _decision(state, "background_music", "全片背景音乐", str(selected), f"混音增益 {policy['playback_gain_db']:.0f} dB；确认样板后才会进入全片")
        _activity(state, "background_music_updated", f"背景音乐设置已保存：{selected}（{policy['playback_gain_db']:.0f} dB）；请先试听第一段样板")
    return _save(project_dir, state)


def _require_approved_music_sample(
    state: dict,
    *,
    trusted_default: bool = False,
    upfront_authorized_signature: str = "",
) -> None:
    """Prevent an untested narration/BGM ratio from reaching the whole video."""
    policy = _ensure_music_policy(state)
    narration_policy = _ensure_narration_policy(state)
    narration_gain = clamp_narration_gain_db(narration_policy.get("playback_gain_db"))
    if not policy.get("enabled") and narration_gain == 0.0:
        return
    if upfront_authorized_signature and upfront_authorized_signature == _audio_mix_signature(state):
        return
    automation = _automation(state)
    frozen_voice = (automation.get("voice") or {})
    review_parent = automation.get("review_preview_pipeline") or {}
    frozen_roles = ((review_parent.get("frozen_input") or {}).get("roles") or {})
    trusted_avatar_roles = bool(
        review_parent.get("pipeline_kind") == "avatar_review_preview"
        and set(frozen_roles) == {"yaya", "mengmeng"}
        and all(str((frozen_roles.get(role) or {}).get("profile_id") or "").strip() for role in ("yaya", "mengmeng"))
    )
    if (
        trusted_default
        and not policy.get("enabled")
        and not narration_policy.get("updated_at")
        and (
            str(frozen_voice.get("profile_name") or frozen_voice.get("label") or "").strip() == "雅雅"
            or trusted_avatar_roles
        )
    ):
        return
    sample = policy.get("sample") or {}
    if sample.get("status") != "approved" or sample.get("policy_signature") != _audio_mix_signature(state):
        raise WorkbenchError("声音设置已修改：请先生成并确认第一段音量样板，再生成全片")


@_project_transactional
def start_music_sample(project_dir: Path, payload: dict | None = None) -> dict:
    """Queue a local first-scene speech/BGM sample without altering sources."""
    payload = payload or {}
    state = _load_for_write(project_dir)
    _require_no_review_preview_conflict(
        _automation(state), payload.get("_review_preview_job_id"), payload.get("_review_preview_worker_token"), payload.get("_review_preview_internal_capability")
    )
    policy = _ensure_music_policy(state)
    track_id = str(policy.get("track_id") or "")
    if policy.get("enabled") and not track_id:
        raise WorkbenchError("请先选择一首可用的新闻背景音乐")
    if policy.get("enabled"):
        try:
            resolve_music_track(track_id, project_dir)
        except MusicLibraryError as exc:
            raise WorkbenchError(str(exc)) from exc
    requested_scene_id = str(payload.get("scene_id") or "").strip()
    scene = _find(state.get("scenes") or [], requested_scene_id, "场景") if requested_scene_id else next(iter(state.get("scenes") or []), None)
    if not scene:
        raise WorkbenchError("当前项目没有可用于试听的第一段")
    sample = policy.setdefault("sample", {})
    if sample.get("status") == "generating":
        raise WorkbenchError("声音样板正在生成，请稍候")
    sample.update({
        "status": "generating", "job_id": uuid4().hex, "scene_id": str(scene.get("id")),
        "output_path": None, "policy_signature": _audio_mix_signature(state),
        "parent_job_id": str(payload.get("_review_preview_job_id") or "") or None,
        "request_fingerprint": str(payload.get("_review_preview_request_fingerprint") or "") or None,
        "generated_at": None, "approved_at": None, "error": "", "stale_reason": "",
    })
    narration_gain = _ensure_narration_policy(state).get("playback_gain_db", 0.0)
    _activity(
        state,
        "audio_mix_sample_started",
        f"正在生成 {scene.get('id')} 的声音样板（人声 {narration_gain:+.1f} dB / 音乐 {policy.get('playback_gain_db'):.0f} dB）",
        scene_id=scene.get("id"),
    )
    return _save(project_dir, state)


def generate_music_sample(project_dir: Path) -> dict:
    """Render the queued sample from an isolated scene-preview input copy."""
    state = _load_for_write(project_dir)
    policy = _ensure_music_policy(state)
    sample = policy.get("sample") or {}
    if sample.get("status") != "generating":
        raise WorkbenchError("当前没有待生成的背景音乐样板")
    job_id = str(sample.get("job_id") or "")
    scene_id = str(sample.get("scene_id") or "")
    if not job_id or not scene_id:
        raise WorkbenchError("背景音乐样板任务缺少片段信息")

    # The scene preview is the production-faithful source: it contains the
    # actual avatar/narration audio, scene duration, captions and composition.
    generate_scene_review_preview(project_dir, scene_id)
    state = _load_for_write(project_dir)
    policy = _ensure_music_policy(state)
    sample = policy.get("sample") or {}
    if sample.get("status") != "generating" or str(sample.get("job_id") or "") != job_id:
        return state
    scene = _find(state.get("scenes") or [], scene_id, "场景")
    preview = scene.get("review_preview") or {}
    source_relpath = str(preview.get("output_path") or "")
    source = (project_dir / source_relpath).resolve()
    try:
        source.relative_to(project_dir.resolve())
    except ValueError as exc:
        raise WorkbenchError("试听样板源预览路径不在当前项目目录内") from exc
    if not source.is_file():
        raise WorkbenchError("第一段审核预览尚未生成，请先补齐本段主体画面")

    signature = hashlib.sha256(f"{scene_id}:{preview.get('input_signature')}:{_audio_mix_signature(state)}".encode("utf-8")).hexdigest()
    output = project_dir / MUSIC_SAMPLE_DIRECTORY / f"{scene_id}-{signature[:12]}.mp4"
    _apply_project_audio_mix(project_dir, state, source, output_path=output)

    state = _load_for_write(project_dir)
    policy = _ensure_music_policy(state)
    sample = policy.get("sample") or {}
    if sample.get("status") != "generating" or str(sample.get("job_id") or "") != job_id:
        return state
    sample.update({
        "status": "ready", "output_path": _safe_relpath(project_dir, str(output)),
        "policy_signature": _audio_mix_signature(state), "generated_at": _now(),
        "approved_at": None, "error": "", "stale_reason": "",
    })
    _activity(state, "audio_mix_sample_ready", f"{scene_id} 声音样板已生成，请试听人物与音乐比例后确认", scene_id=scene_id, output_path=sample["output_path"])
    return _save(project_dir, state)


def mark_music_sample_failed(project_dir: Path, error: object) -> dict:
    state = _load_for_write(project_dir)
    policy = _ensure_music_policy(state)
    sample = policy.setdefault("sample", {})
    message = _safe_automation_error(error)
    sample.update({"status": "failed", "error": message, "approved_at": None, "stale_reason": ""})
    _activity(state, "audio_mix_sample_failed", f"声音试听样板生成失败：{message}", scene_id=sample.get("scene_id"))
    return _save(project_dir, state)


def approve_music_sample(project_dir: Path, payload: dict) -> dict:
    """Make the exact sampled mix eligible for full-preview/final rendering."""
    if payload.get("confirmed") is not True:
        raise WorkbenchError("请确认试听结果后再应用背景音乐到全片")
    state = _load_for_write(project_dir)
    policy = _ensure_music_policy(state)
    sample = policy.get("sample") or {}
    output = project_dir / str(sample.get("output_path") or "")
    if sample.get("status") != "ready" or not output.is_file():
        raise WorkbenchError("请先生成可播放的第一段背景音乐样板")
    if sample.get("policy_signature") != _audio_mix_signature(state):
        raise WorkbenchError("人物或背景音乐设置已变化，请重新生成试听样板")
    sample.update({"status": "approved", "approved_at": _now(), "error": "", "stale_reason": ""})
    narration_gain = _ensure_narration_policy(state).get("playback_gain_db", 0.0)
    _decision(state, "audio_mix", "全片声音样板确认", f"人声 {narration_gain:+.1f} dB / 音乐 {policy.get('playback_gain_db'):.0f} dB", f"已试听 {sample.get('scene_id')}，全片将复用相同设置")
    _activity(state, "audio_mix_sample_approved", "声音样板已确认；人物与音乐比例现可用于全片预览和正式成片", scene_id=sample.get("scene_id"))
    return _save(project_dir, state)


def music_track_path(project_dir: Path, track_id: str) -> Path:
    """Resolve an opaque browser id to a whitelisted local audio file."""
    try:
        path, _track = resolve_music_track(track_id, project_dir)
        return path
    except MusicLibraryError as exc:
        raise WorkbenchError(str(exc)) from exc


def _find(items: list[dict], item_id: str, label: str) -> dict:
    for item in items:
        if item.get("id") == item_id:
            return item
    raise WorkbenchError(f"未找到{label}：{item_id}")


def _activity(state: dict, kind: str, message: str, **details: Any) -> None:
    state.setdefault("activities", []).append({"at": _now(), "kind": kind, "message": message, **details})
    state["activities"] = state["activities"][-120:]


def _decision(state: dict, category: str, subject: str, selected: str, note: str = "") -> None:
    decisions = state.setdefault("decisions", [])
    decisions.append({
        "id": _numbered("D-", decisions, "id"), "at": _now(), "category": category,
        "subject": subject, "selected": selected, "note": note,
    })


def update_intake(project_dir: Path, payload: dict) -> dict:
    """Persist the human's pre-production inventory before any generation."""
    state = _load_for_write(project_dir)
    intake = _normalize_intake(state.get("project", {}).get("intake"))
    for key, allowed, label in (
        ("script_status", INTAKE_SCRIPT_STATUSES, "脚本状态"),
        ("materials_status", INTAKE_MATERIAL_STATUSES, "现有素材状态"),
        ("style_status", INTAKE_STYLE_STATUSES, "对标画风状态"),
    ):
        if key in payload:
            value = str(payload.get(key) or "unknown")
            if value not in allowed:
                raise WorkbenchError(f"{label}选项无效")
            intake[key] = value
    limits = {
        "brief": (3000, "项目简报"), "video_title": (200, "视频标题"),
        "source_text": (20000, "脚本或简单想法"), "script_text": (20000, "已有脚本"),
        "idea": (5000, "简单想法"), "style_reference": (2000, "画风参考"),
        "style_direction": (1000, "画风方向"), "audience": (500, "目标观众"),
        "content_goal": (1000, "内容目标"),
    }
    for key, (limit, label) in limits.items():
        if key in payload:
            value = str(payload.get(key) or "").strip()
            if len(value) > limit:
                raise WorkbenchError(f"{label}不能超过 {limit} 个字符")
            intake[key] = value
    intake["updated_at"] = _now()
    state["project"]["intake"] = intake
    _decision(state, "production_intake", "脚本准备状态", intake["script_status"])
    _decision(state, "production_intake", "现有素材状态", intake["materials_status"])
    _decision(state, "production_intake", "对标画风状态", intake["style_status"])
    _activity(state, "production_intake", "已完成制作前盘点，等待选择脚本处理路径")
    return _save(project_dir, state)


def _script_draft_revision(draft: dict) -> int:
    try:
        return max(1, int(draft.get("revision") or 1))
    except (TypeError, ValueError):
        return 1


def _assert_script_draft_revision(draft: dict, payload: dict) -> int:
    current = _script_draft_revision(draft)
    try:
        expected = int(payload.get("expected_revision"))
    except (TypeError, ValueError) as exc:
        raise WorkbenchError("请刷新页面后再保存：草案缺少有效版本号") from exc
    if expected != current:
        raise WorkbenchError(
            f"脚本草案版本已从 {expected} 更新为 {current}；请刷新后重新确认，旧页面不能覆盖新稿"
        )
    return current


def _script_edit_sentences(value: object) -> list[str]:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if not text:
        return []
    return [item.strip() for item in re.findall(r".+?[。！？!?；;]+|.+$", text) if item.strip()]


def _canonical_script_sentence(value: object) -> str:
    sentence = re.sub(r"\s+", " ", str(value or "")).strip()
    if not sentence:
        raise WorkbenchError("脚本句子不能为空")
    if len(sentence) > 2000:
        raise WorkbenchError("单句脚本不能超过 2000 个字符")
    if sentence[-1] not in "。！？!?；;":
        sentence += "。"
    return sentence


def _estimated_script_section_seconds(text: str) -> float:
    spoken = re.sub(r"[\s，。！？!?；;、,:：]", "", text)
    major_pauses = len(re.findall(r"[。！？!?；;]", text))
    minor_pauses = len(re.findall(r"[，、,:：]", text))
    return max(1.2, len(spoken) / 4.6 + major_pauses * 0.24 + minor_pauses * 0.1)


def _retime_script_section(section: dict, *, start: float, end: float) -> dict:
    previous_start = _as_number(section.get("start_seconds"), start)
    previous_end = max(previous_start, _as_number(section.get("end_seconds"), previous_start))
    previous_duration = previous_end - previous_start
    cues = section.get("enhancement_cues") if isinstance(section.get("enhancement_cues"), list) else []
    retimed: list[dict] = []
    for raw_cue in cues:
        if not isinstance(raw_cue, dict):
            continue
        cue = deepcopy(raw_cue)
        if cue.get("timestamp_seconds") is not None:
            old_timestamp = _as_number(cue.get("timestamp_seconds"), previous_start)
            ratio = (old_timestamp - previous_start) / previous_duration if previous_duration > 0 else 0.5
            ratio = max(0.0, min(1.0, ratio))
            cue["timestamp_seconds"] = _rounded_seconds(start + (end - start) * ratio)
        retimed.append(cue)
    section["start_seconds"] = _rounded_seconds(start)
    section["end_seconds"] = _rounded_seconds(end)
    if cues or "enhancement_cues" in section:
        section["enhancement_cues"] = retimed
    return section


def _next_user_section_id(used_ids: set[str]) -> str:
    number = 1
    while True:
        candidate = f"sec-user-{number:03d}"
        if candidate not in used_ids:
            used_ids.add(candidate)
            return candidate
        number += 1


def update_script_draft_content(project_dir: Path, payload: dict) -> dict:
    """Atomically save human sentence/section edits without calling a model."""
    state = _load_for_write(project_dir)
    draft = state.get("project", {}).get("script_draft")
    if not isinstance(draft, dict) or not isinstance(draft.get("script"), dict):
        raise WorkbenchError("当前没有可编辑的脚本草案")
    if draft.get("status") == "approved":
        raise WorkbenchError("脚本已通过；请先使用“重新编辑脚本”安全打开新草案")
    current_revision = _assert_script_draft_revision(draft, payload)
    raw_sections = payload.get("sections")
    if not isinstance(raw_sections, list) or not raw_sections:
        raise WorkbenchError("脚本草案至少需要一个段落")
    if len(raw_sections) > 100:
        raise WorkbenchError("脚本段落不能超过 100 个")

    current_script = deepcopy(draft["script"])
    existing_sections = current_script.get("sections") if isinstance(current_script.get("sections"), list) else []
    existing_by_id = {
        str(item.get("id")): item
        for item in existing_sections
        if isinstance(item, dict) and item.get("id")
    }
    used_ids = set(existing_by_id)
    submitted_ids: set[str] = set()
    prepared: list[dict] = []
    total_characters = 0

    for order, raw in enumerate(raw_sections, 1):
        if not isinstance(raw, dict):
            raise WorkbenchError(f"第 {order} 个脚本段落格式无效")
        raw_id = str(raw.get("id") or "").strip()
        if raw_id:
            if raw_id not in existing_by_id:
                raise WorkbenchError(f"第 {order} 个段落引用了未知 ID；请刷新页面后重试")
            if raw_id in submitted_ids:
                raise WorkbenchError(f"脚本草案包含重复段落 ID：{raw_id}")
            section = deepcopy(existing_by_id[raw_id])
            section_id = raw_id
        else:
            section_id = _next_user_section_id(used_ids)
            section = {"id": section_id, "enhancement_cues": []}
        submitted_ids.add(section_id)

        label = str(raw.get("label") or f"段落 {order}").strip()
        if not label:
            label = f"段落 {order}"
        if len(label) > 120:
            raise WorkbenchError("脚本段落标题不能超过 120 个字符")
        raw_sentences = raw.get("sentences")
        if not isinstance(raw_sentences, list) or not raw_sentences:
            raise WorkbenchError(f"“{label}”至少需要一句台词")
        if len(raw_sentences) > 200:
            raise WorkbenchError(f"“{label}”的句子不能超过 200 条")
        sentences = [_canonical_script_sentence(item) for item in raw_sentences]
        text = "".join(sentences)
        total_characters += len(text)
        if total_characters > 20000:
            raise WorkbenchError("脚本正文不能超过 20000 个字符")
        section.update({"id": section_id, "label": label, "text": text})
        prepared.append(section)

    cursor = 0.0
    sections: list[dict] = []
    for section in prepared:
        duration = _estimated_script_section_seconds(str(section.get("text") or ""))
        end = cursor + duration
        sections.append(_retime_script_section(section, start=cursor, end=end))
        cursor = end

    title = str(payload.get("title") or current_script.get("title") or "").strip()
    if not title:
        raise WorkbenchError("请填写脚本标题")
    if len(title) > 200:
        raise WorkbenchError("脚本标题不能超过 200 个字符")
    updated_script = deepcopy(current_script)
    updated_script.update(
        {
            "version": "1.0",
            "title": title,
            "sections": sections,
            "total_duration_seconds": _rounded_seconds(cursor),
        }
    )

    history = draft.get("history") if isinstance(draft.get("history"), list) else []
    history.append(
        {
            "revision": current_revision,
            "saved_at": _now(),
            "script": current_script,
        }
    )
    draft.update(
        {
            "status": "draft",
            "revision": current_revision + 1,
            "updated_at": _now(),
            "script": updated_script,
            "history": history[-10:],
            "review_note": "",
        }
    )
    draft.setdefault("original_script", deepcopy(current_script))
    draft.pop("approved_at", None)
    draft.pop("approved_revision", None)
    state["project"]["script_draft"] = draft
    intake = _normalize_intake(state["project"].get("intake"))
    intake["script_status"] = "draft_ready"
    intake["updated_at"] = _now()
    state["project"]["intake"] = intake
    _atomic_write(project_dir / "artifacts" / "script_draft.json", updated_script)
    _activity(
        state,
        "script_draft_edit",
        f"已人工保存脚本草案第 {draft['revision']} 版，共 {len(sections)} 段；未调用脚本模型",
    )
    return _save(project_dir, state)


def reopen_script_draft(project_dir: Path, payload: dict) -> dict:
    """Reopen an approved draft only before any downstream media work exists."""
    state = _load_for_write(project_dir)
    draft = state.get("project", {}).get("script_draft")
    if not isinstance(draft, dict) or not isinstance(draft.get("script"), dict):
        raise WorkbenchError("当前没有可重新编辑的脚本草案")
    if draft.get("status") != "approved":
        raise WorkbenchError("当前脚本尚未通过，无需重新打开")
    current_revision = _assert_script_draft_revision(draft, payload)
    if state.get("scenes"):
        raise WorkbenchError("脚本已经建立分镜或进入下游制作，禁止静默改稿；请创建新的脚本版本任务")
    parent = (_automation(state).get("review_preview_pipeline") or {})
    if parent.get("job_id") or parent.get("status") in {"queued", "running", "awaiting_human", "completed"}:
        raise WorkbenchError("脚本已经启动一键预览下游任务，禁止静默改稿；请创建新的脚本版本任务")
    preview = (_automation(state).get("preview_render") or {})
    if preview.get("output_path") or preview.get("status") in {"generating", "completed"}:
        raise WorkbenchError("脚本已经存在下游预览媒体，禁止静默改稿；请创建新的脚本版本任务")

    draft.update(
        {
            "status": "draft",
            "revision": current_revision + 1,
            "updated_at": _now(),
            "review_note": "",
        }
    )
    draft.pop("approved_at", None)
    draft.pop("approved_revision", None)
    state["project"]["script_draft"] = draft
    intake = _normalize_intake(state["project"].get("intake"))
    intake["script_status"] = "draft_ready"
    intake["updated_at"] = _now()
    state["project"]["intake"] = intake
    _activity(state, "script_draft_reopened", "已在下游制作开始前重新打开脚本草案")
    return _save(project_dir, state)


_AVATAR_TURN_LINE_RE = re.compile(
    r"^(?P<turn>T\d{1,6})\s+(?P<speaker>[^：:]{1,32})[：:]\s*(?P<text>.+?)\s*$",
    re.IGNORECASE,
)
_AVATAR_SPEAKER_IDS = {"雅雅": "yaya", "檬檬": "mengmeng", "萌萌": "mengmeng"}


def _parse_avatar_turn_script(source_text: str) -> list[dict[str, str]]:
    """Parse a one- or two-presenter Txxx script into a strict turn ledger."""
    rows = [row.strip() for row in str(source_text or "").replace("\r", "").split("\n") if row.strip()]
    if not rows:
        raise WorkbenchError("数字人脚本为空，请粘贴 T001 雅雅：台词 格式的逐轮脚本")
    turns: list[dict[str, str]] = []
    used: set[str] = set()
    for index, row in enumerate(rows, 1):
        match = _AVATAR_TURN_LINE_RE.fullmatch(row)
        if not match:
            raise WorkbenchError(f"第 {index} 行不是有效数字人轮次；请使用 T001 雅雅：台词 格式")
        turn_id = f"T{int(match.group('turn')[1:]):03d}"
        if turn_id in used:
            raise WorkbenchError(f"数字人轮次编号重复：{turn_id}")
        speaker_name = re.sub(r"\s+", "", match.group("speaker"))
        speaker_id = _AVATAR_SPEAKER_IDS.get(speaker_name)
        if not speaker_id:
            raise WorkbenchError(f"{turn_id} 的主持人“{speaker_name}”无法识别；当前一键路线只支持雅雅和檬檬")
        text = match.group("text").strip()
        if not text:
            raise WorkbenchError(f"{turn_id} 的台词为空")
        used.add(turn_id)
        turns.append({
            "turn_id": turn_id,
            "speaker_id": speaker_id,
            "speaker_name": "檬檬" if speaker_id == "mengmeng" else "雅雅",
            "text": text,
        })
    expected = [f"T{index:03d}" for index in range(1, len(turns) + 1)]
    actual = [turn["turn_id"] for turn in turns]
    if actual != expected:
        raise WorkbenchError(f"数字人轮次必须从 T001 连续排列；当前为 {'、'.join(actual)}")
    if not {turn["speaker_id"] for turn in turns}:
        raise WorkbenchError("数字人脚本至少需要一位已识别的主持人")
    return turns


def _normalize_avatar_turn_script(
    generated_script: dict,
    turns: list[dict[str, str]],
    *,
    title: str,
    organize_strength: str,
) -> dict:
    """Keep the source turn ledger authoritative even when the model merges sections."""
    raw_sections = generated_script.get("sections") if isinstance(generated_script.get("sections"), list) else []
    generated_by_turn: dict[str, dict] = {}
    for section in raw_sections:
        if not isinstance(section, dict):
            continue
        raw_turn = str(section.get("turn_id") or section.get("id") or "").upper()
        if re.fullmatch(r"T\d{1,6}", raw_turn):
            generated_by_turn[f"T{int(raw_turn[1:]):03d}"] = section
    model_contract_complete = set(generated_by_turn) == {turn["turn_id"] for turn in turns}

    cursor = 0.0
    sections: list[dict] = []
    for index, turn in enumerate(turns, 1):
        candidate = generated_by_turn.get(turn["turn_id"], {}) if model_contract_complete else {}
        candidate_speaker = str(candidate.get("speaker_id") or "").strip().lower()
        candidate_valid = bool(
            candidate
            and (not candidate_speaker or candidate_speaker == turn["speaker_id"])
            and str(candidate.get("text") or "").strip()
        )
        # Faithful mode never lets a model silently change user-provided facts.
        text = str(candidate.get("text") or "").strip() if candidate_valid and organize_strength == "light_polish" else turn["text"]
        duration = _estimated_script_section_seconds(text)
        end = round(cursor + duration, 3)
        sections.append({
            "id": f"section-{index:03d}",
            "turn_id": turn["turn_id"],
            "speaker_id": turn["speaker_id"],
            "speaker_name": turn["speaker_name"],
            "expected_asset_filename": f"{turn['turn_id']}_{turn['speaker_id'].upper()}.mp4",
            "label": f"{turn['turn_id']} · {turn['speaker_name']}",
            "text": text,
            "start_seconds": cursor,
            "end_seconds": end,
            "speaker_directions": str(candidate.get("speaker_directions") or "自然口播"),
            "enhancement_cues": deepcopy(candidate.get("enhancement_cues") or []),
            "visual_contract": {
                "visual_intent": f"数字人口播：{turn['speaker_name']} 讲述本段内容",
                "required_assets": [],
                "forbidden_states": ["拉伸或变速驱动音频"],
                "min_visual_coverage": 1,
            },
        })
        cursor = end
    return {
        "version": "1.0",
        "title": str(generated_script.get("title") or title),
        "total_duration_seconds": cursor,
        "voice_performance": deepcopy(generated_script.get("voice_performance") or {
            "performance_intent": "数字人自然播报",
            "pacing_profile": "conversational",
        }),
        "sections": sections,
        "metadata": {
            "source": "organized_avatar_turn_script",
            "turn_contract": "strict_txxx_avatar_presenter_v2",
            "model_turn_contract_complete": model_contract_complete,
            "timing_basis": "script_estimate_pending_native_avatar_audio",
        },
    }


def generate_script_draft(project_dir: Path, payload: dict) -> dict:
    """Generate a reviewable script draft; never silently promotes it to canonical script."""
    if payload.get("confirmed") is not True:
        raise WorkbenchError("生成脚本前需要确认会调用已配置的 AI 接口")
    state = _load_for_write(project_dir)
    intake = _normalize_intake(state.get("project", {}).get("intake"))
    mode = str(payload.get("mode") or "from_scratch")
    if mode not in SCRIPT_DRAFT_MODES:
        raise WorkbenchError("脚本处理路径无效")

    # The manual workbench intentionally exposes one text box. Persist that
    # source before calling the model so a transient API failure never loses
    # the user's input, then map it to the legacy mode-specific fields.
    if "video_title" in payload:
        video_title = str(payload.get("video_title") or "").strip()
        if not video_title:
            raise WorkbenchError("请填写视频标题")
        if len(video_title) > 200:
            raise WorkbenchError("视频标题不能超过 200 个字符")
        intake["video_title"] = video_title
    if "source_text" in payload:
        source_text = str(payload.get("source_text") or "").strip()
        if len(source_text) > 20000:
            raise WorkbenchError("脚本或简单想法不能超过 20000 个字符")
        intake["source_text"] = source_text
        intake["script_text"] = source_text if mode == "organize_script" else ""
        intake["idea"] = source_text if mode in {"expand_idea", "from_scratch"} else ""
    title = intake["video_title"] or str(state["project"].get("title") or project_dir.name).strip()
    if not title:
        raise WorkbenchError("请填写视频标题")

    if mode == "organize_script" and not intake["script_text"]:
        raise WorkbenchError("请在“输入脚本/简单想法”中粘贴已有脚本")
    if mode == "expand_idea" and not intake["idea"]:
        raise WorkbenchError("请在“输入脚本/简单想法”中填写一个想法")
    organize_strength = str(payload.get("organize_strength") or "faithful")
    if organize_strength not in {"faithful", "light_polish"}:
        raise WorkbenchError("脚本整理强度无效")

    avatar_turns: list[dict[str, str]] = []
    if str((state.get("project") or {}).get("pipeline_type") or "") == AVATAR_PIPELINE and mode == "organize_script":
        avatar_turns = _parse_avatar_turn_script(intake["script_text"])

    intake["script_mode"] = mode
    intake["organize_strength"] = organize_strength
    intake["updated_at"] = _now()
    state["project"]["intake"] = intake
    _activity(state, "script_input", "已保存视频标题与脚本输入，准备生成草案", mode=mode)
    state = _save(project_dir, state)

    duration_seconds = None
    if intake.get("duration_source") != "audio_driven":
        duration_seconds = state["project"].get("duration_seconds") or intake["duration_seconds"]
    result = OpenAIScript().execute({
        "mode": mode,
        "organize_strength": organize_strength,
        "title": title,
        "duration_seconds": duration_seconds,
        "audience": intake["audience"],
        "content_goal": intake["content_goal"],
        "brief": intake["brief"],
        "idea": intake["idea"],
        "script_text": intake["script_text"],
        "style_direction": intake["style_direction"],
        "style_reference": intake["style_reference"],
        "avatar_turn_contract": avatar_turns,
        "model": payload.get("model"),
    })
    if not result.success or not result.data.get("script"):
        raw_error = str(result.error or "").lower()
        model = str(result.data.get("model") or os.environ.get("OPENAI_SCRIPT_MODEL") or "gpt-5.6-luna")
        if "model" in raw_error and any(token in raw_error for token in ("not found", "does not exist", "不存在", "invalid")):
            raise WorkbenchError(f"当前脚本模型“{model}”不可用，请把 OPENAI_SCRIPT_MODEL 改为中转站支持的模型后重试")
        if "response_format" in raw_error or "json_object" in raw_error:
            raise WorkbenchError("当前中转接口不支持结构化脚本返回，系统已尝试兼容模式但仍未成功")
        if any(token in raw_error for token in ("401", "403", "unauthorized", "api key", "鉴权")):
            raise WorkbenchError("AI 接口鉴权失败，请检查 API 密钥是否有效")
        if "404" in raw_error or "not found" in raw_error:
            raise WorkbenchError("AI 接口地址或请求路径不可用，请检查 OPENAI_BASE_URL 是否以 /v1 结尾")
        if any(token in raw_error for token in ("timeout", "timed out", "超时")):
            raise WorkbenchError("AI 接口响应超时，请稍后重试")
        raise WorkbenchError("脚本生成服务暂时不可用，请检查 AI 配置或稍后重试")
    generated_script = deepcopy(result.data["script"])
    if avatar_turns:
        generated_script = _normalize_avatar_turn_script(
            generated_script,
            avatar_turns,
            title=title,
            organize_strength=organize_strength,
        )
    draft = {
        "status": "draft",
        "mode": mode,
        "organize_strength": organize_strength,
        "model": result.data.get("model"),
        "created_at": _now(),
        "updated_at": _now(),
        "revision": 1,
        "script": generated_script,
        "original_script": deepcopy(generated_script),
        "history": [],
        "review_note": "",
    }
    state["project"]["script_draft"] = draft
    intake["script_status"] = "draft_ready"
    intake["updated_at"] = _now()
    state["project"]["intake"] = intake
    _activity(state, "script_draft", "已生成脚本草案，等待人工审核")
    return _save(project_dir, state)


def review_script_draft(project_dir: Path, payload: dict) -> dict:
    """Approve or send back a script draft while preserving the review trail."""
    state = _load_for_write(project_dir)
    draft = state["project"].get("script_draft")
    if not isinstance(draft, dict) or not isinstance(draft.get("script"), dict):
        raise WorkbenchError("当前没有可审核的脚本草案")
    current_revision = _script_draft_revision(draft)
    if payload.get("expected_revision") is not None:
        current_revision = _assert_script_draft_revision(draft, payload)
    action = str(payload.get("action") or "")
    intake = _normalize_intake(state["project"].get("intake"))
    if action == "approve":
        draft["status"] = "approved"
        draft["approved_at"] = _now()
        draft["approved_revision"] = current_revision
        intake["script_status"] = "draft_approved"
        _atomic_write(project_dir / "artifacts" / "script.json", draft["script"])
        _atomic_write(project_dir / "artifacts" / "script_draft.json", draft["script"])
        _decision(state, "script_review", "脚本草案审核", "approved")
        _activity(state, "script_review", "已通过脚本草案，并写入正式脚本产物")
    elif action == "request_revision":
        note = str(payload.get("note") or "").strip()
        if not note:
            raise WorkbenchError("请填写需要修改的脚本意见")
        draft["status"] = "revision_requested"
        draft["review_note"] = note[:3000]
        intake["script_status"] = "draft_ready"
        _decision(state, "script_review", "脚本草案审核", "revision_requested", note[:3000])
        _activity(state, "script_review", "已记录脚本修改意见，等待重新生成或人工修改")
    else:
        raise WorkbenchError("脚本审核动作无效")
    state["project"]["script_draft"] = draft
    state["project"]["intake"] = intake
    return _save(project_dir, state)


def _template_import_backup(project_dir: Path) -> str | None:
    """Snapshot replaceable canonical records before a template reset.

    A template import deliberately creates a fresh editable timeline.  It must
    never silently destroy a project that already contains reviewed work, so
    every replaced contract is copied to a timestamped project-local archive.
    Generated media is intentionally left in place but detached from the new
    timeline; the archive tells an operator exactly which canonical records
    belonged to the previous preparation pass.
    """
    candidates = [
        "project.json",
        "artifacts/script.json",
        "artifacts/script_draft.json",
        "artifacts/scene_plan.json",
        "artifacts/workbench.json",
        "artifacts/avatar_source_package.json",
        "artifacts/asset_manifest.json",
        "artifacts/edit_decisions.json",
        "artifacts/render_report.json",
    ]
    existing = [project_dir / item for item in candidates if (project_dir / item).is_file()]
    if not existing:
        return None
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    archive = project_dir / SCRIPT_IMPORT_HISTORY_DIRECTORY / stamp
    for source in existing:
        destination = archive / source.relative_to(project_dir)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    return archive.relative_to(project_dir).as_posix()


def _template_import_requires_confirmation(project_dir: Path, state: dict) -> bool:
    """Return true only when an import would replace meaningful preparation."""
    canonical_files = (
        project_dir / "artifacts" / "script.json",
        project_dir / "artifacts" / "scene_plan.json",
        project_dir / "artifacts" / "avatar_source_package.json",
    )
    if any(path.is_file() for path in canonical_files):
        return True
    return bool(state.get("scenes") or state.get("assets") or state.get("usages") or state.get("patches"))


def _scene_plan_from_imported_script(script: dict, scenes: list[dict]) -> dict:
    return {
        "version": "1.0",
        "title": script.get("title") or "未命名数字人口播",
        "total_duration_seconds": script.get("total_duration_seconds") or 0,
        "scenes": [
            {
                "id": scene["id"],
                "title": scene["title"],
                "description": scene["description"],
                "start_seconds": scene["start_seconds"],
                "end_seconds": scene["end_seconds"],
                "script_section_id": scene["script_section_id"],
                "shot_intent": scene["shot_intent"],
                "hero_moment": scene["hero_moment"],
            }
            for scene in scenes
        ],
    }


def import_avatar_script_template(project_dir: Path, payload: dict) -> dict:
    """Preview-confirmed template import for a multi-speaker avatar project.

    This is a preparation action, not a generation action: it creates the
    canonical script, scene draft and avatar source contract in one coherent
    revision.  It makes no provider request and never spends a cloud quota.
    """
    project = _read_json(project_dir / "project.json") or {}
    if str(project.get("pipeline_type") or "") != AVATAR_PIPELINE:
        raise WorkbenchError("模板脚本一键初始化仅适用于“数字人口播”项目")
    template_id = str(payload.get("template_id") or "").strip()
    if not template_id:
        raise WorkbenchError("请先选择一个数字人口播模板脚本")
    overrides = payload.get("speaker_overrides")
    if overrides is not None and not isinstance(overrides, dict):
        raise WorkbenchError("说话人编号映射格式无效")
    try:
        script, provenance = build_avatar_script_from_template(template_id, speaker_overrides=overrides or {})
        validate_artifact("script", script)
    except ScriptTemplateError as exc:
        raise WorkbenchError(str(exc)) from exc
    except Exception as exc:
        detail = getattr(exc, "message", None) or str(exc)
        raise WorkbenchError(f"模板脚本不符合项目脚本合同：{detail}") from exc

    state = _load_for_write(project_dir)
    replacement = _template_import_requires_confirmation(project_dir, state)
    if replacement and payload.get("replace_confirmed") is not True:
        raise WorkbenchError("当前项目已有脚本或工作台内容；确认覆盖后系统会先创建可恢复备份")

    generation_mode = str(payload.get("generation_mode") or "dashscope_wan_s2v")
    per_turn_cloud_modes = {"dashscope_wan_s2v", "runninghub_longcat"}
    provider_modes = {*per_turn_cloud_modes, "runninghub_longform"}
    if generation_mode not in {"manual_import", *provider_modes}:
        raise WorkbenchError("数字人来源只能选择“阿里云生成”“RunningHub 生成”或“导入已完成数字人视频”")
    requested_import_mode = str(payload.get("import_mode") or "per_turn")
    if requested_import_mode not in {"per_turn", "longform"}:
        raise WorkbenchError("数字人导入方式无效")
    background_mode = str(payload.get("background_mode") or "opaque")
    if background_mode not in {"opaque", "green_screen", "transparent"}:
        raise WorkbenchError("数字人导出背景选项无效")
    treatment = str(payload.get("default_treatment") or "fullscreen")
    if treatment not in PRESENTER_TREATMENTS:
        raise WorkbenchError("默认出镜方式无效")

    sections = script.get("sections") if isinstance(script.get("sections"), list) else []
    scenes = [_scene_from_script_section(section, index) for index, section in enumerate(sections, 1) if isinstance(section, dict)]
    if len(scenes) != len(sections) or not scenes:
        raise WorkbenchError("模板未生成有效轮次，无法初始化项目")
    frame_rate = int(state.get("settings", {}).get("frame_rate") or 30)
    sample_rate = int(state.get("settings", {}).get("sample_rate") or 48000)
    scene_plan = _scene_plan_from_imported_script(script, scenes)
    source_text = str(provenance.pop("source_text", ""))
    source_hash = str(provenance.get("source_sha256") or "")
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    source_snapshot = project_dir / SCRIPT_IMPORT_DIRECTORY / f"{stamp}-{source_hash[:12] or 'template'}.md"

    # Every write below is local and deterministic.  Validation happens before
    # any canonical write; old contracts are copied before the first replace.
    backup_path = _template_import_backup(project_dir) if replacement else None
    _atomic_text_write(source_snapshot, source_text)
    import_record = {
        "version": "1.0",
        "import_id": f"IMP-{stamp}-{source_hash[:8] or 'LOCAL'}",
        "created_at": _now(),
        "template": provenance,
        "source_snapshot": source_snapshot.relative_to(project_dir).as_posix(),
        "script_sha256": _json_hash(script),
        "turn_count": len(sections),
        "speaker_count": len({str(item.get("speaker_id") or "") for item in sections}),
        "replacement": replacement,
        "backup_path": backup_path,
        "note": "仅初始化本地脚本、轮次与数字人素材包；未调用任何云端生成。",
    }
    _atomic_write(project_dir / "artifacts" / "script.json", script)
    _atomic_write(project_dir / "artifacts" / "script_draft.json", script)
    _atomic_write(project_dir / "artifacts" / "scene_plan.json", scene_plan)
    _atomic_write(project_dir / "artifacts" / "script_import.json", import_record)

    # The package initializer reads the just-written script and therefore
    # derives the exact same turn IDs and speaker bindings as the workbench.
    try:
        avatar_package = initialize_avatar_package(project_dir, {
            "replace": True,
            "generation_mode": generation_mode,
            "import_mode": "per_turn" if generation_mode in per_turn_cloud_modes else requested_import_mode,
            "background_mode": background_mode,
            "default_treatment": treatment,
        })
    except AvatarImportError as exc:
        raise WorkbenchError(f"数字人素材包初始化失败：{exc}") from exc

    # A template reset detaches old assets and local patch tasks, but keeps all
    # old files on disk and preserves the decision/activity ledger.  This avoids
    # a stale asset or patch from leaking into the new scripted timeline.
    intake = _normalize_intake(state.get("project", {}).get("intake"))
    intake["script_status"] = "draft_approved"
    intake["script_text"] = "\n".join(
        f"{section.get('speaker_name') or section.get('speaker_id')}：{section.get('text') or ''}" for section in sections
    )[:20000]
    intake["duration_seconds"] = max(1, int(math.ceil(_as_number(script.get("total_duration_seconds"), 1))))
    intake["updated_at"] = _now()
    intake["avatar"] = {
        "source_status": "planned",
        "import_mode": avatar_package.get("import_mode") or "per_turn",
        "default_treatment": treatment,
        "background_mode": background_mode,
    }
    state["project"]["intake"] = intake
    state["project"]["duration_seconds"] = _as_number(script.get("total_duration_seconds"), 0)
    if payload.get("adopt_template_title", True) is not False:
        project["title"] = str(script.get("title") or project.get("title") or project_dir.name)
        _atomic_write(project_dir / "project.json", project)
        state["project"]["title"] = project["title"]
    state["project"]["script_draft"] = {
        "status": "approved",
        "mode": "template_import",
        "created_at": _now(),
        "approved_at": _now(),
        "script": script,
        "review_note": "已在模板预览中人工确认，并完成本地一键初始化。",
        "import_id": import_record["import_id"],
    }
    state["scenes"] = scenes
    state["segments"] = _segments_for_scenes(scenes, frame_rate, sample_rate)
    state["assets"] = []
    state["usages"] = []
    state["patches"] = []
    state["keyframe_reviews"] = []
    state["automation"] = _automation_default()
    state["automation"]["audio_mode"] = "native_avatar_audio"
    state["avatar_package"] = avatar_package
    state["avatar"] = {
        "status": "not_configured",
        "default_treatment": treatment,
        "master_asset_id": None,
        "package_revision": avatar_package.get("revision"),
        "turns": {},
    }
    _ensure_timeline_state(state)
    _decision(state, "script_source", "数字人口播脚本来源", str(provenance.get("title") or template_id), f"模板：{template_id}；共 {len(sections)} 个轮次")
    _decision(state, "script_review", "模板脚本导入确认", "approved", "预览后初始化；未调用云端生成")
    generation_label = (
        "阿里云逐轮次生成" if generation_mode == "dashscope_wan_s2v"
        else "RunningHub 逐轮次生成" if generation_mode == "runninghub_longcat"
        else "RunningHub 双角色长视频生成与本地切割" if generation_mode == "runninghub_longform"
        else "导入已有数字人视频"
    )
    _decision(state, "avatar_generation_mode", "数字人素材准备方式", generation_label)
    if replacement:
        _activity(state, "script_template_import", f"已用模板重置项目准备工作；原有合同已备份至 {backup_path}")
    else:
        _activity(state, "script_template_import", f"已从模板初始化 {len(sections)} 个轮次与 {len(avatar_package.get('speakers') or [])} 位说话人")
    return _save(project_dir, state)


def import_avatar_user_script(project_dir: Path, payload: dict) -> dict:
    """Commit a previewed DOCX or pasted script without AI rewriting.

    The staging token binds the human-reviewed preview to the source bytes.
    This function reuses the same canonical contracts and recoverable backup
    policy as the built-in template importer.
    """
    project = _read_json(project_dir / "project.json") or {}
    if str(project.get("pipeline_type") or "") != AVATAR_PIPELINE:
        raise WorkbenchError("自定义脚本一键初始化仅适用于“数字人口播”项目")
    token = str(payload.get("import_token") or "").strip().upper()
    try:
        staged = load_staged_import(project_dir, token)
        script, provenance, relative_source_path = build_script_from_staged_import(
            staged,
            speaker_overrides=payload.get("speaker_overrides") or {},
        )
        validate_artifact("script", script)
    except ScriptImportError as exc:
        raise WorkbenchError(str(exc)) from exc
    except Exception as exc:
        detail = getattr(exc, "message", None) or str(exc)
        raise WorkbenchError(f"脚本不符合项目生产合同：{detail}") from exc

    state = _load_for_write(project_dir)
    replacement = _template_import_requires_confirmation(project_dir, state)
    if replacement and payload.get("replace_confirmed") is not True:
        raise WorkbenchError("当前项目已有脚本或工作台内容；确认覆盖后系统会先创建可恢复备份")

    generation_mode = str(payload.get("generation_mode") or "manual_import")
    per_turn_cloud_modes = {"dashscope_wan_s2v", "runninghub_longcat"}
    provider_modes = {*per_turn_cloud_modes, "runninghub_longform"}
    if generation_mode not in {"manual_import", *provider_modes}:
        raise WorkbenchError("数字人来源只能选择“阿里云生成”“RunningHub 生成”或“导入已完成数字人视频”")
    requested_import_mode = str(payload.get("import_mode") or "per_turn")
    if requested_import_mode not in {"per_turn", "longform"}:
        raise WorkbenchError("数字人导入方式无效")
    background_mode = str(payload.get("background_mode") or "opaque")
    if background_mode not in {"opaque", "green_screen", "transparent"}:
        raise WorkbenchError("数字人导出背景选项无效")
    treatment = str(payload.get("default_treatment") or "fullscreen")
    if treatment not in PRESENTER_TREATMENTS:
        raise WorkbenchError("默认出镜方式无效")

    sections = script.get("sections") if isinstance(script.get("sections"), list) else []
    scenes = [_scene_from_script_section(section, index) for index, section in enumerate(sections, 1) if isinstance(section, dict)]
    if len(scenes) != len(sections) or not scenes:
        raise WorkbenchError("脚本没有生成有效轮次，无法初始化项目")
    frame_rate = int(state.get("settings", {}).get("frame_rate") or 30)
    sample_rate = int(state.get("settings", {}).get("sample_rate") or 48000)
    scene_plan = _scene_plan_from_imported_script(script, scenes)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    source_hash = str(provenance.get("source_sha256") or "")
    staged_source = (project_dir / relative_source_path).resolve()
    try:
        staged_source.relative_to(project_dir.resolve())
    except ValueError as exc:
        raise WorkbenchError("脚本来源路径越出当前项目") from exc
    suffix = staged_source.suffix.lower() if staged_source.suffix.lower() in {".docx", ".txt"} else ".txt"
    source_snapshot = project_dir / SCRIPT_IMPORT_DIRECTORY / f"{stamp}-{source_hash[:12] or 'user-script'}{suffix}"

    backup_path = _template_import_backup(project_dir) if replacement else None
    source_snapshot.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(staged_source, source_snapshot)
    import_record = {
        "version": "1.0",
        "import_id": f"IMP-{stamp}-{source_hash[:8] or 'LOCAL'}",
        "created_at": _now(),
        "source": provenance,
        "source_snapshot": source_snapshot.relative_to(project_dir).as_posix(),
        "script_sha256": _json_hash(script),
        "turn_count": len(sections),
        "speaker_count": len({str(item.get("speaker_id") or "") for item in sections}),
        "replacement": replacement,
        "backup_path": backup_path,
        "note": "确定性导入：未调用 AI，未改写台词，未消耗生成额度。",
    }
    _atomic_write(project_dir / "artifacts" / "script.json", script)
    _atomic_write(project_dir / "artifacts" / "script_draft.json", script)
    _atomic_write(project_dir / "artifacts" / "scene_plan.json", scene_plan)
    _atomic_write(project_dir / "artifacts" / "script_import.json", import_record)

    try:
        avatar_package = initialize_avatar_package(project_dir, {
            "replace": True,
            "generation_mode": generation_mode,
            "import_mode": "per_turn" if generation_mode in per_turn_cloud_modes else requested_import_mode,
            "background_mode": background_mode,
            "default_treatment": treatment,
        })
    except AvatarImportError as exc:
        raise WorkbenchError(f"数字人素材包初始化失败：{exc}") from exc

    intake = _normalize_intake(state.get("project", {}).get("intake"))
    intake["script_status"] = "draft_approved"
    intake["script_text"] = "\n".join(
        f"{section.get('turn_id')} {section.get('speaker_name') or section.get('speaker_id')}：{section.get('text') or ''}"
        for section in sections
    )[:20000]
    intake["duration_seconds"] = max(1, int(math.ceil(_as_number(script.get("total_duration_seconds"), 1))))
    intake["updated_at"] = _now()
    intake["avatar"] = {
        "source_status": "planned",
        "import_mode": avatar_package.get("import_mode") or "per_turn",
        "default_treatment": treatment,
        "background_mode": background_mode,
    }
    state["project"]["intake"] = intake
    state["project"]["duration_seconds"] = _as_number(script.get("total_duration_seconds"), 0)
    if payload.get("adopt_source_title") is True:
        project["title"] = str(script.get("title") or project.get("title") or project_dir.name)
        _atomic_write(project_dir / "project.json", project)
        state["project"]["title"] = project["title"]
    state["project"]["script_draft"] = {
        "status": "approved",
        "mode": "deterministic_user_import",
        "created_at": _now(),
        "approved_at": _now(),
        "script": script,
        "review_note": "已在前端预览中人工确认；原文导入，未调用 AI。",
        "import_id": import_record["import_id"],
    }
    state["scenes"] = scenes
    state["segments"] = _segments_for_scenes(scenes, frame_rate, sample_rate)
    state["assets"] = []
    state["usages"] = []
    state["patches"] = []
    state["keyframe_reviews"] = []
    state["automation"] = _automation_default()
    state["automation"]["audio_mode"] = "native_avatar_audio"
    state["avatar_package"] = avatar_package
    state["avatar"] = {
        "status": "not_configured",
        "default_treatment": treatment,
        "master_asset_id": None,
        "package_revision": avatar_package.get("revision"),
        "turns": {},
    }
    _ensure_timeline_state(state)
    source_label = str(provenance.get("filename") or provenance.get("source_kind") or "用户脚本")
    _decision(state, "script_source", "数字人口播脚本来源", source_label, f"确定性导入；共 {len(sections)} 个轮次")
    _decision(state, "script_review", "用户脚本导入确认", "approved", "逐轮预览后初始化；未调用云端生成")
    _decision(
        state,
        "avatar_generation_mode",
        "数字人素材准备方式",
        "阿里云逐轮次生成" if generation_mode == "dashscope_wan_s2v"
        else "RunningHub 逐轮次生成" if generation_mode == "runninghub_longcat"
        else "RunningHub 双角色长视频生成与本地切割" if generation_mode == "runninghub_longform"
        else "导入已有数字人视频",
    )
    if replacement:
        _activity(state, "script_user_import", f"已用用户脚本重置项目准备工作；原有合同已备份至 {backup_path}")
    else:
        _activity(state, "script_user_import", f"已从用户脚本初始化 {len(sections)} 个轮次与 {len(avatar_package.get('speakers') or [])} 位说话人")
    saved = _save(project_dir, state)
    consume_staged_import(project_dir, staged)
    return saved


def apply_daily_story_contract(project_dir: Path, script: dict) -> dict:
    """Bind durable news identity and one headline overlay to imported scenes.

    Deterministic script import intentionally knows nothing about editorial
    grouping.  Daily production restores that grouping by line order after the
    import, so every visual slot and renderer can consume one shared story ID.
    """
    state = _load_for_write(project_dir)
    scenes = list(state.get("scenes") or [])
    lines = [item for item in script.get("lines") or [] if isinstance(item, dict)]
    if len(scenes) != len(lines):
        raise WorkbenchError("每日脚本行数与项目场景数不一致，无法绑定新闻小标题")
    stories = {
        str(item.get("story_id") or ""): item
        for item in script.get("stories") or []
        if isinstance(item, dict) and str(item.get("story_id") or "")
    }
    for scene, line in zip(scenes, lines, strict=True):
        story_id = str(line.get("story_id") or "")
        story = stories.get(story_id) or {}
        scene["turn_id"] = str(line.get("turn_id") or scene.get("title") or "")
        scene["story_id"] = story_id
        scene["information_dimension"] = str(line.get("information_dimension") or "")
        scene["information_key"] = str(line.get("information_key") or "")
        scene["headline_overlay"] = deepcopy(story.get("headline_overlay") or {}) if story_id else {}
        scene["official_image_url"] = str(story.get("official_image_url") or "")
        scene["official_image_attribution"] = str(story.get("official_image_attribution") or "")
    state["daily_story_contract"] = {
        "version": "1.0",
        "style_id": "daily_news_headline_v1",
        "stories": [
            {
                "story_id": story_id,
                "headline": str(story.get("headline") or ""),
                "headline_overlay": deepcopy(story.get("headline_overlay") or {}),
                "official_image_url": str(story.get("official_image_url") or ""),
                "official_image_attribution": str(story.get("official_image_attribution") or ""),
            }
            for story_id, story in stories.items()
        ],
        "updated_at": _now(),
    }
    _mark_render_needs_refresh(state, "每日新闻小标题合同已更新")
    _activity(state, "daily_story_contract", f"已为 {len(stories)} 条新闻绑定 story_id 与统一小标题")
    return _save(project_dir, state)


def generate_scene_plan_from_script(project_dir: Path) -> dict:
    """Turn the approved script into a reviewable, project-local scene plan."""
    state = _load_for_write(project_dir)
    draft = state["project"].get("script_draft")
    if not isinstance(draft, dict) or draft.get("status") != "approved" or not isinstance(draft.get("script"), dict):
        raise WorkbenchError("请先通过脚本草案，再生成分镜草案")
    if state.get("scenes"):
        return state

    script = draft["script"]
    sections = script.get("sections") if isinstance(script.get("sections"), list) else []
    scenes = [
        _scene_from_script_section(section, order)
        for order, section in enumerate(sections, 1)
        if isinstance(section, dict)
    ]
    if not scenes:
        raise WorkbenchError("正式脚本中没有可转换的分段，无法生成分镜草案")

    frame_rate = int(state.get("settings", {}).get("frame_rate") or 30)
    sample_rate = int(state.get("settings", {}).get("sample_rate") or 48000)
    state["scenes"] = scenes
    state["segments"] = _segments_for_scenes(scenes, frame_rate, sample_rate)
    state["project"]["duration_seconds"] = max(_as_number(scene["end_seconds"]) for scene in scenes)
    _ensure_timeline_state(state)

    scene_plan = {
        "version": "1.0",
        "script_sha256": _json_hash(script),
        "title": script.get("title") or state["project"].get("title"),
        "total_duration_seconds": script.get("total_duration_seconds") or state["project"].get("duration_seconds"),
        "scenes": [
            {
                "id": scene["id"],
                "title": scene["title"],
                "description": scene["description"],
                "start_seconds": scene["start_seconds"],
                "end_seconds": scene["end_seconds"],
                "script_section_id": scene["script_section_id"],
                "shot_intent": scene["shot_intent"],
                "hero_moment": scene["hero_moment"],
            }
            for scene in scenes
        ],
    }
    state["project"]["scene_plan_script_hash"] = scene_plan["script_sha256"]
    _atomic_write(project_dir / "artifacts" / "script.json", script)
    _atomic_write(project_dir / "artifacts" / "scene_plan.json", scene_plan)
    _decision(state, "scene_plan", "脚本转分镜", f"已生成 {len(scenes)} 个待审核场景")
    _activity(state, "scene_plan", f"已从已通过脚本生成 {len(scenes)} 个分镜草案，等待首帧、高潮帧和素材来源审核")
    return _save(project_dir, state)


def update_scene(project_dir: Path, scene_id: str, payload: dict) -> dict:
    state = _load_for_write(project_dir)
    scene = _find(state["scenes"], scene_id, "场景")
    if "source_strategy" in payload:
        source = str(payload["source_strategy"])
        if source not in SOURCE_TYPES:
            raise WorkbenchError("未知的素材来源策略")
        _cancel_scene_keyframe_generation(scene, "素材来源策略已调整，本次关键帧任务已取消")
        _cancel_scene_motion_generation(scene, "素材来源策略已调整，本次动态画面任务已取消")
        scene["source_strategy"] = source
        _invalidate_scene_review_preview(scene, "素材来源策略已调整，请刷新本段审核预览")
        _decision(state, "source_strategy", f"{scene_id} 素材来源", source)
    if "presenter_treatment" in payload:
        if not _is_avatar_project(state):
            raise WorkbenchError("只有数字人口播项目可以调整数字人版式")
        package = read_avatar_package(project_dir)
        if not package or package.get("assembly", {}).get("status") != "passed":
            raise WorkbenchError("请先完成数字人原片校验、台词核对与原声母版合成")
        treatment = str(payload.get("presenter_treatment") or "")
        if treatment not in PRESENTER_TREATMENTS:
            raise WorkbenchError("数字人版式只能是全屏、预设画中画、自定义画中画或暂时隐藏")
        presenter = _scene_presenter(scene)
        if not presenter.get("source_path"):
            raise WorkbenchError("请先在“数字人素材”中把原声母版应用为真实时间线")
        presenter["treatment"] = treatment
        if treatment == "fullscreen":
            scene["source_strategy"] = "avatar_only"
        elif scene.get("source_strategy") in {"avatar_only", "undecided"}:
            # A picture-in-picture host needs a main picture.  Network download
            # is only a default proposal; the reviewer can still change it to
            # human-provided, project-library, or AI-generated afterwards.
            scene["source_strategy"] = "web_download"
        _decision(state, "presenter_treatment", f"{scene_id} 数字人版式", treatment)
        _invalidate_scene_review_preview(scene, "数字人出镜方式已调整，请刷新本段审核预览")
    if any(key in payload for key in ("presenter_layout", "presenter_layout_template_id", "presenter_crop_bottom", "presenter_shape", "presenter_face_crop")):
        if not _is_avatar_project(state):
            raise WorkbenchError("只有数字人口播项目可以调整数字人画面位置")
        presenter = _scene_presenter(scene)
        if presenter.get("treatment") == "fullscreen":
            raise WorkbenchError("全屏数字人没有可调整的画中画位置；请先切换为画中画")
        layouts = _ensure_presenter_layout_state(state)
        has_template = "presenter_layout_template_id" in payload
        has_geometry = "presenter_layout" in payload
        has_crop = "presenter_crop_bottom" in payload
        has_shape = "presenter_shape" in payload
        has_face_crop = "presenter_face_crop" in payload
        template_id = str(payload.get("presenter_layout_template_id") or presenter.get("layout_template_id") or layouts["default_template_id"])
        template = next((item for item in layouts["templates"] if item["id"] == template_id), None)
        if not template:
            raise WorkbenchError("未找到所选数字人版式模板")
        if has_template:
            presenter["layout_template_id"] = template_id
            if not has_geometry:
                presenter["layout_override"] = None
            if not has_crop:
                presenter["crop_bottom"] = _normalized_presenter_crop_bottom(template.get("crop_bottom"))
            if not has_shape:
                presenter["shape"] = _normalized_presenter_shape(template.get("shape"))
            if not has_face_crop:
                presenter["face_crop"] = None
        if has_geometry:
            presenter["layout_override"] = _normalized_presenter_geometry(payload.get("presenter_layout"), template["geometry"])
        if has_crop:
            presenter["crop_bottom"] = _normalized_presenter_crop_bottom(payload.get("presenter_crop_bottom"))
        if has_shape:
            presenter["shape"] = _normalized_presenter_shape(payload.get("presenter_shape"))
        if has_face_crop:
            presenter["face_crop"] = _normalized_presenter_face_crop(payload.get("presenter_face_crop"), template.get("face_crop"))
        if has_template or has_geometry:
            presenter["treatment"] = "custom" if presenter.get("layout_override") else ("pip_top_left" if template_id == "pip_top_left" else "custom")
        if scene.get("source_strategy") in {"avatar_only", "undecided"}:
            scene["source_strategy"] = "web_download"
        scene["keyframe_review"] = None
        scene["keyframe_generation"] = None
        _invalidate_scene_review_preview(scene, "数字人版式或头像取景已调整，请刷新本段审核预览")
        scene["review_status"] = "needs_adjustment"
        _mark_render_needs_refresh(state, f"{scene_id} 的数字人版式已调整")
        _decision(state, "presenter_layout", f"{scene_id} 数字人画中画位置", template_id, json.dumps(_presenter_layout(state, presenter), ensure_ascii=False))
    if "review_status" in payload:
        status = str(payload["review_status"])
        if status not in REVIEW_STATUSES:
            raise WorkbenchError("未知的审核状态")
        if status == "approved" and scene.get("source_strategy") == "ai_generated":
            review = scene.get("keyframe_review") or {}
            if review.get("status") != "approved":
                raise WorkbenchError("AI 生图场景必须先完成关键帧审核（首帧和高潮帧），再通过场景")
        if status == "approved" and _is_avatar_project(state) and _scene_presenter(scene).get("treatment") != "hidden":
            review = scene.get("keyframe_review") or {}
            if review.get("status") != "approved":
                raise WorkbenchError("数字人口播场景必须先审核合成首帧和高潮帧，再通过场景")
        scene["review_status"] = status
    if "anchor_kind" in payload:
        anchor = next(
            (item for item in (scene.get("anchors") or []) if item.get("kind") == str(payload["anchor_kind"])),
            None,
        )
        if anchor is None:
            raise WorkbenchError(f"未找到审核锚点：{payload['anchor_kind']}")
        status = str(payload.get("anchor_status") or "pending")
        if status not in REVIEW_STATUSES:
            raise WorkbenchError("未知的锚点审核状态")
        anchor["status"] = status
        anchor["note"] = str(payload.get("note") or "").strip()
        anchor["reviewed_at"] = _now()
    if "title" in payload:
        scene["title"] = str(payload["title"]).strip()[:160]
    _activity(state, "scene_review", f"已更新场景 {scene_id}", scene_id=scene_id)
    return _save(project_dir, state)


def update_scene_subtitles(project_dir: Path, scene_id: str, payload: dict) -> dict:
    """Persist one scene's editable captions without regenerating its video.

    Caption styling and wording are an overlay decision.  They intentionally
    do not invalidate the local review MP4: the browser reuses that stable
    media while the live caption layer is updated in place.  Only the derived
    full-preview/final deliverables are marked stale.
    """
    state = _load_for_write(project_dir)
    scene = _find(state["scenes"], scene_id, "场景")
    styles = _ensure_subtitle_style_state(state)
    subtitles = _scene_subtitles(scene)
    if "template_id" in payload:
        template_id = str(payload.get("template_id") or "")
        if template_id not in {str(item["id"]) for item in styles["templates"]}:
            raise WorkbenchError("字幕方案不存在，请重新选择")
        subtitles["template_id"] = template_id
    if "style" in payload:
        if not isinstance(payload.get("style"), dict):
            raise WorkbenchError("字幕样式格式无效")
        subtitles["style_override"] = _normalised_subtitle_style(
            payload.get("style"), _resolved_scene_subtitle_style(state, scene),
        )
    if "cue_overrides" in payload:
        raw_overrides = payload.get("cue_overrides")
        if not isinstance(raw_overrides, dict):
            raise WorkbenchError("字幕文字覆盖格式无效")
        clean_overrides: dict[str, str] = {}
        for raw_id, raw_text in raw_overrides.items():
            cue_id = str(raw_id or "")
            if not re.fullmatch(r"cue-\d{3}", cue_id):
                raise WorkbenchError("字幕条目标识无效")
            text = str(raw_text or "").strip()
            if len(text) > 240:
                raise WorkbenchError("单条字幕不能超过 240 个字符")
            if text:
                clean_overrides[cue_id] = text
        subtitles["cue_overrides"] = clean_overrides
    _mark_render_needs_refresh(state, f"{scene_id} 的字幕已更新")
    _decision(state, "subtitle_edit", f"{scene_id} 字幕", "saved", "仅更新字幕图层，不重新生成片段画面或配音")
    _activity(state, "subtitle_saved", f"已保存 {scene_id} 的字幕文字与样式；左侧视频未重载", scene_id=scene_id)
    return _save(project_dir, state)


def update_subtitle_style_template(project_dir: Path, payload: dict) -> dict:
    """Save a reusable caption plan and optionally apply it across scenes."""
    state = _load_for_write(project_dir)
    # Resolve the source style before retaining a reference to the style
    # registry.  `_resolved_scene_subtitle_style()` performs the migration
    # guard, which may replace `state["subtitle_styles"]`; keeping the old
    # reference here would make a saved template appear to revert on reload.
    source_scene_id = str(payload.get("scene_id") or "")
    source_scene = _find(state["scenes"], source_scene_id, "字幕方案来源片段") if source_scene_id else None
    base_style = _resolved_scene_subtitle_style(state, source_scene) if source_scene else None
    styles = _ensure_subtitle_style_state(state)
    requested_id = re.sub(r"[^a-z0-9_-]", "-", str(payload.get("template_id") or "").lower()).strip("-")
    template_id = requested_id or str(styles["default_template_id"])
    template = next((item for item in styles["templates"] if item["id"] == template_id), None)
    if template is None:
        if len(styles["templates"]) >= SUBTITLE_STYLE_TEMPLATE_LIMIT:
            raise WorkbenchError(f"最多保存 {SUBTITLE_STYLE_TEMPLATE_LIMIT} 个字幕方案")
        template = {"id": template_id, "name": "自定义字幕方案", "builtin": False, "revision": 0, "updated_at": _now()}
        styles["templates"].append(template)
    base_style = base_style or template.get("style")
    raw_style = payload.get("style") if isinstance(payload.get("style"), dict) else base_style
    template["name"] = str(payload.get("name") or template.get("name") or "自定义字幕方案").strip()[:80] or "自定义字幕方案"
    template["style"] = _normalised_subtitle_style(raw_style, base_style)
    template["revision"] = int(_as_number(template.get("revision"))) + 1
    template["updated_at"] = _now()
    if payload.get("set_default"):
        styles["default_template_id"] = template_id
    scope = str(payload.get("apply_scope") or "none")
    targets: list[dict] = []
    if scope == "scene" and source_scene:
        targets = [source_scene]
    elif scope == "all":
        targets = list(state.get("scenes") or [])
    elif scope != "none":
        raise WorkbenchError("字幕方案应用范围只能是当前片段、全部片段或仅保存")
    for scene in targets:
        subtitles = _scene_subtitles(scene)
        subtitles["template_id"] = template_id
        subtitles["style_override"] = {}
    if targets:
        _mark_render_needs_refresh(state, "字幕方案已批量应用")
    _decision(state, "subtitle_style_template", "字幕方案", template_id, f"范围：{scope}；已应用 {len(targets)} 个片段")
    _activity(state, "subtitle_style_saved", f"已保存字幕方案“{template['name']}”，应用到 {len(targets)} 个片段；不重载审核视频", template_id=template_id, scene_ids=[item["id"] for item in targets])
    return _save(project_dir, state)


def update_presenter_layout_template(project_dir: Path, payload: dict) -> dict:
    """Save a reusable project-local layout and optionally apply it in scope."""
    state = _load_for_write(project_dir)
    if not _is_avatar_project(state):
        raise WorkbenchError("只有数字人口播项目可以保存数字人版式模板")
    layouts = _ensure_presenter_layout_state(state)
    requested_id = re.sub(r"[^a-z0-9_-]", "-", str(payload.get("template_id") or "").lower()).strip("-")
    template_id = requested_id or f"custom-{uuid4().hex[:8]}"
    template = next((item for item in layouts["templates"] if item["id"] == template_id), None)
    if template is None:
        if len(layouts["templates"]) >= PRESENTER_LAYOUT_TEMPLATE_LIMIT:
            raise WorkbenchError(f"最多保存 {PRESENTER_LAYOUT_TEMPLATE_LIMIT} 个数字人版式模板")
        template = {"id": template_id, "name": "自定义版式", "builtin": False, "revision": 0, "updated_at": _now()}
        layouts["templates"].append(template)
    template["name"] = str(payload.get("name") or template.get("name") or "自定义版式").strip()[:80] or "自定义版式"
    template["geometry"] = _normalized_presenter_geometry(payload.get("geometry"), template.get("geometry"))
    template["crop_bottom"] = _normalized_presenter_crop_bottom(payload.get("crop_bottom", template.get("crop_bottom")))
    template["shape"] = _normalized_presenter_shape(payload.get("shape"), template.get("shape", "rounded"))
    template["face_crop"] = _normalized_presenter_face_crop(payload.get("face_crop"), template.get("face_crop"))
    template["revision"] = int(_as_number(template.get("revision"))) + 1
    template["updated_at"] = _now()
    if payload.get("set_default"):
        layouts["default_template_id"] = template_id
    scope = str(payload.get("apply_scope") or "none")
    scene_id = str(payload.get("scene_id") or "")
    selected_scene = _find(state["scenes"], scene_id, "场景") if scene_id else None
    selected_speaker = (selected_scene or {}).get("presenter", {}).get("turn_id")
    selected_speaker = str(selected_speaker or "")
    targets: list[dict] = []
    if scope == "scene" and selected_scene:
        targets = [selected_scene]
    elif scope == "speaker" and selected_speaker:
        # Speaker identity lives in the avatar package, while each scene owns
        # a turn.  Use the bound turn->speaker map when available.
        package = read_avatar_package(project_dir) or {}
        speaker_by_turn = {str(turn.get("turn_id")): str(turn.get("speaker_id")) for turn in package.get("turns", []) if isinstance(turn, dict)}
        speaker_id = speaker_by_turn.get(selected_speaker)
        targets = [scene for scene in state["scenes"] if speaker_id and speaker_by_turn.get(str(_scene_presenter(scene).get("turn_id") or "")) == speaker_id]
    elif scope == "all":
        targets = [scene for scene in state["scenes"] if _scene_presenter(scene).get("source_path")]
    elif scope != "none":
        raise WorkbenchError("版式应用范围只能是当前片段、当前角色、全部片段或仅保存")
    for scene in targets:
        presenter = _scene_presenter(scene)
        presenter["layout_template_id"] = template_id
        presenter["layout_override"] = None
        presenter["crop_bottom"] = template["crop_bottom"]
        presenter["shape"] = template["shape"]
        presenter["face_crop"] = None
        if presenter.get("treatment") != "fullscreen":
            presenter["treatment"] = "pip_top_left" if template_id == "pip_top_left" else "custom"
        scene["keyframe_review"] = None
        scene["keyframe_generation"] = None
        _invalidate_scene_review_preview(scene, "数字人版式模板已更新，请刷新本段审核预览")
        scene["review_status"] = "needs_adjustment"
    if targets:
        _mark_render_needs_refresh(state, "数字人版式模板已更新")
    _decision(state, "presenter_layout_template", "数字人版式模板", template_id, f"范围：{scope}；底部裁切 {int(template['crop_bottom'] * 100)}%；头像取景 {int(template['face_crop']['zoom'] * 100)}%；共应用 {len(targets)} 段")
    _activity(state, "presenter_layout_saved", f"已保存数字人版式“{template['name']}”，应用到 {len(targets)} 个片段", template_id=template_id, scene_ids=[scene["id"] for scene in targets])
    return _save(project_dir, state)


def update_story_headline_layout(project_dir: Path, payload: dict) -> dict:
    """Persist one headline placement for preview and final render."""
    state = _load_for_write(project_dir)
    previous = _ensure_story_headline_layout(state)
    layout = _normalized_story_headline_layout(payload, previous)
    state["story_headline_layout"] = layout
    affected: list[str] = []
    for scene in state.get("scenes") or []:
        if not isinstance(scene, dict) or not isinstance(scene.get("headline_overlay"), dict) or not scene.get("headline_overlay"):
            continue
        affected.append(str(scene.get("id") or ""))
        _invalidate_scene_review_preview(scene, "新闻小标题位置已调整，请刷新本段审核预览")
        scene["review_status"] = "needs_adjustment"
    if affected:
        _mark_render_needs_refresh(state, "新闻小标题位置已调整")
    _decision(state, "story_headline_layout", "新闻小标题位置", "project", json.dumps(layout, ensure_ascii=False))
    _activity(state, "story_headline_layout_saved", f"已保存新闻小标题位置，影响 {len(affected)} 个片段", scene_ids=affected)
    return _save(project_dir, state)


def _keyframe_prompt(state: dict, scene: dict, anchor_kind: str) -> str:
    """Build a bounded, review-oriented still-image prompt from project intent."""
    _ensure_scene_visual_state(state, scene)
    plan_prompt = str((scene.get("visual_plan") or {}).get("prompt") or "").strip()
    if plan_prompt:
        moment = (
            "本次生成首帧：构图稳定，让观众一眼看懂主体、空间和本段对象。"
            if anchor_kind == "first_frame"
            else "本次生成高潮帧：信息比首帧更集中、更有记忆点，但保持同一画风和主体连续性。"
        )
        return (plan_prompt + "\n" + moment)[:6000]
    intake = _normalize_intake(state.get("project", {}).get("intake"))
    style = intake.get("style_direction") or intake.get("style_reference") or "干净、清晰、适合中文知识类短视频的画面"
    aspect = intake.get("aspect_label") or "项目设定画幅"
    scene_text = str(scene.get("description") or "").strip()
    shot_intent = str(scene.get("shot_intent") or "").strip()
    if anchor_kind == "first_frame":
        moment = "这是本场景的首帧：先让观众一眼看懂主体、空间和本段要讲的对象，构图稳定，给字幕留出安全区域。"
        label = "首帧"
    else:
        moment = "这是本场景的高潮帧：视觉信息要比首帧更集中、更有记忆点，明确呈现本段最重要的结论或动作，但仍保持主体清楚。"
        label = "高潮帧"
    prompt = (
        "为一条中文视频生成一张可审核的关键帧画面。\n"
        f"场景：{scene.get('title') or '未命名场景'}。\n"
        f"旁白/字幕原文：{scene_text or '待补充'}。\n"
        f"画面意图：{shot_intent or '根据旁白建立明确画面'}。\n"
        f"关键帧类型：{label}。{moment}\n"
        f"画风与参考：{style}。画幅要求：{aspect}。\n"
        "主体优先、层次清晰、留出字幕安全区；不要生成任何文字、字幕、水印、品牌标识或乱码，字幕由工作台单独叠加。"
    )
    return prompt[:6000]


def _keyframe_label(anchor_kind: str) -> str:
    return "首帧" if anchor_kind == "first_frame" else "高潮帧"


def _apply_tech_brief_subtitle_recommendation(
    state: dict,
    source_scene: dict,
    *,
    mode: str,
    scope: str,
) -> int:
    """Apply the optional pack recommendation only after an explicit choice."""
    if mode == "inherit":
        return 0
    if mode != "apply_recommended":
        raise WorkbenchError("字幕处理方式只能选择沿用当前字幕或应用科技快报推荐字幕")
    if scope not in {"scene", "all"}:
        raise WorkbenchError("推荐字幕的应用范围只能是当前片段或全部片段")
    templates = _ensure_subtitle_style_state(state).get("templates") or []
    template_id = "subtitle-tech-brief-v1"
    if template_id not in {str(item.get("id")) for item in templates if isinstance(item, dict)}:
        raise WorkbenchError("科技快报字幕推荐方案不可用，请检查风格包文件")
    targets = [source_scene] if scope == "scene" else list(state.get("scenes") or [])
    for target in targets:
        subtitles = _scene_subtitles(target)
        subtitles["template_id"] = template_id
        subtitles["style_override"] = {}
    if targets:
        _mark_render_needs_refresh(state, "已显式应用科技快报 V1 推荐字幕方案")
    return len(targets)


def update_scene_visual_plan(project_dir: Path, scene_id: str, payload: dict) -> dict:
    """Save the exact visual brief that generation runtimes are allowed to use."""
    state = _load_for_write(project_dir)
    scene = _find(state["scenes"], scene_id, "场景")
    _ensure_scene_visual_state(state, scene)
    engine = str(payload.get("engine") or (scene.get("visual_plan") or {}).get("engine") or "openai_image")
    if engine not in VISUAL_ENGINES:
        raise WorkbenchError("画面生成方式只能选择 OpenAI 静态图、HyperFrames、Remotion 或历史 PPT 信息卡")
    prompt = str(payload.get("prompt") or "").strip()
    if not prompt:
        raise WorkbenchError("画面提示词不能为空；请先说明本段应该出现什么")
    if len(prompt) > 6000:
        raise WorkbenchError("画面提示词不能超过 6000 个字符")
    old = scene["visual_plan"]
    structured = payload.get("structured_spec") if isinstance(payload.get("structured_spec"), dict) else old.get("structured_spec") or {}
    prior_style_pack = old.get("style_pack") if isinstance(old.get("style_pack"), dict) else {}
    payload_style_pack = payload.get("style_pack") if isinstance(payload.get("style_pack"), dict) else {}
    requested_style_pack_id = str(
        payload.get("style_pack_id")
        or payload_style_pack.get("id")
        or prior_style_pack.get("id")
        or STYLE_PACK_ID
    )
    subtitle_mode = str(payload.get("subtitle_mode") or prior_style_pack.get("subtitle_mode") or "inherit")
    subtitle_apply_scope = str(payload.get("subtitle_apply_scope") or prior_style_pack.get("subtitle_apply_scope") or "scene")
    pack_summary: dict | None = None
    scene_recipe = str(structured.get("scene_recipe") or "relationship_map")
    layout_variant: dict | None = None
    if engine == "hyperframes":
        try:
            pack_summary = style_pack_summary(requested_style_pack_id)
            pack = load_style_pack(requested_style_pack_id)
        except StylePackError as exc:
            raise WorkbenchError(f"科技快报风格包不可用：{exc}") from exc
        if scene_recipe not in (pack.get("recipes", {}).get("recipes", {}) or {}):
            raise WorkbenchError("所选科技快报场景结构不存在，请重新选择")
        layout_variant = _resolved_layout_variant(
            scene_recipe,
            structured.get("layout_variant"),
            style_pack_id=requested_style_pack_id,
        )
        scene_recipe = layout_variant["recipe_id"]
        if subtitle_mode not in {"inherit", "apply_recommended"}:
            raise WorkbenchError("字幕处理方式无效")
        if subtitle_apply_scope not in {"scene", "all"}:
            raise WorkbenchError("推荐字幕的应用范围无效")
    constraints = [str(item) for item in (payload.get("constraints") or old.get("constraints") or []) if str(item) in VISUAL_CONSTRAINTS]
    if "no_text" in constraints:
        constraints = [item for item in constraints if item != "no_text"]
        constraints.append("no_ai_baked_text")
    for required in ("no_presenter", "no_ai_baked_text", "reserve_caption_safe_area"):
        if required not in constraints:
            constraints.append(required)
    if _scene_presenter(scene).get("treatment") in {"pip_top_left", "custom"} and "reserve_presenter_safe_area" not in constraints:
        constraints.append("reserve_presenter_safe_area")
    _cancel_scene_keyframe_generation(scene, "画面提示词或生成方式已调整，本次关键帧任务已取消")
    _cancel_scene_motion_generation(scene, "画面提示词或生成方式已调整，本次动态画面任务已取消")
    scene["motion_visual_candidate"] = None
    if (scene.get("motion_generation") or {}).get("status") != "cancelled":
        scene["motion_generation"] = {"status": "idle", "engine": engine, "error": ""}
    scene["visual_plan"] = {
        "version": 1,
        "engine": engine,
        "prompt": prompt,
        "structured_spec": {
            "headline": str(structured.get("headline") or scene.get("title") or "")[:160],
            "center_label": str(structured.get("center_label") or "")[:80],
            "components": [str(item)[:120] for item in (structured.get("components") or []) if str(item).strip()][:12],
            "motion": str(structured.get("motion") or "重点元素依次进入，镜头稳定，节奏服务于台词")[:1000],
            "palette": str(structured.get("palette") or "深蓝、青色高光、克制的科技感")[:500],
            "scene_recipe": scene_recipe if engine == "hyperframes" else str(structured.get("scene_recipe") or "relationship_map")[:80],
            "layout_variant": layout_variant["id"] if layout_variant else str(structured.get("layout_variant") or "")[:80],
            "motion_variant": layout_variant["motion_variant"] if layout_variant else str(structured.get("motion_variant") or "")[:80],
            "copy_plan": deepcopy(structured.get("copy_plan")) if isinstance(structured.get("copy_plan"), dict) else {},
        },
        "constraints": constraints,
        "status": "saved",
        "revision": int(_as_number(old.get("revision"), 0)) + 1,
        "updated_at": _now(),
    }
    if engine == "hyperframes" and pack_summary:
        scene["visual_plan"]["style_pack"] = {
            "id": pack_summary["id"],
            "version": pack_summary["version"],
            "aspect_profile": "auto",
            "subtitle_mode": subtitle_mode,
            "subtitle_apply_scope": subtitle_apply_scope,
            "recipes": deepcopy(pack_summary.get("recipes") or []),
        }
        applied = _apply_tech_brief_subtitle_recommendation(
            state,
            scene,
            mode=subtitle_mode,
            scope=subtitle_apply_scope,
        )
    else:
        applied = 0
    scene["keyframe_generation"] = None
    detail = f"方案版本 {scene['visual_plan']['revision']}"
    if engine == "hyperframes" and pack_summary:
        detail += f"；风格包 {pack_summary['id']}@{pack_summary['version']}；结构 {scene_recipe}；版式 {layout_variant['name'] if layout_variant else ''}"
        if applied:
            detail += f"；已显式应用推荐字幕到 {applied} 段"
    _decision(state, "visual_plan", f"{scene_id} 画面生成方案", engine, detail)
    _activity(state, "visual_plan_saved", f"已保存 {scene_id} 的画面生成方案，生成前可继续修改", scene_id=scene_id, engine=engine, style_pack=pack_summary["id"] if pack_summary else None, scene_recipe=scene_recipe if engine == "hyperframes" else None)
    return _save(project_dir, state)


def refine_scene_visual_copy(project_dir: Path, scene_id: str, payload: dict) -> dict:
    """Distil one scene and its neighbours into editable HyperFrames copy."""
    project_dir = project_dir.resolve()
    state = _load_for_write(project_dir)
    scene = _find(state["scenes"], scene_id, "场景")
    _ensure_scene_visual_state(state, scene)
    plan = scene["visual_plan"]
    if str(plan.get("engine") or "") != "hyperframes":
        raise WorkbenchError("请先把画面生成方式切换为 HyperFrames 并保存")
    if not read_text_ai_config().get("configured"):
        raise WorkbenchError("尚未配置文本 AI，请先打开右上角“AI 配置”")

    scenes = list(state.get("scenes") or [])
    index = next((number for number, item in enumerate(scenes) if item.get("id") == scene_id), -1)
    if index < 0:
        raise WorkbenchError("找不到需要提炼的场景")

    def spoken(item: dict | None) -> str:
        if not item:
            return ""
        narration = item.get("narration") if isinstance(item.get("narration"), dict) else {}
        return str(narration.get("text") or item.get("description") or item.get("shot_intent") or "").strip()[:1200]

    previous = scenes[index - 1] if index > 0 else None
    following = scenes[index + 1] if index + 1 < len(scenes) else None
    context = {
        "project_title": str((state.get("project") or {}).get("title") or "")[:200],
        "scene_id": scene_id,
        "scene_title": str(scene.get("title") or "")[:200],
        "speaker": str((_scene_presenter(scene) or {}).get("speaker_name") or scene.get("speaker") or "")[:80],
        "previous_context": spoken(previous),
        "current_spoken_text": spoken(scene),
        "next_context": spoken(following),
        "visual_intent": str(scene.get("shot_intent") or plan.get("prompt") or "")[:1200],
        "current_recipe": str((plan.get("structured_spec") or {}).get("scene_recipe") or "relationship_map"),
        "current_layout_variant": str((plan.get("structured_spec") or {}).get("layout_variant") or ""),
        "hyperframes_layout_variants": layout_variant_catalog(str((plan.get("style_pack") or {}).get("id") or STYLE_PACK_ID)),
        "aspect": str((state.get("settings") or {}).get("aspect_ratio") or "9:16"),
    }
    if not context["current_spoken_text"]:
        raise WorkbenchError("当前片段没有可用于语境提炼的台词")
    try:
        copy = plan_visual_copy(context)
    except TextAIError as exc:
        raise WorkbenchError(f"AI 画面文案提炼失败：{exc}") from exc

    old_spec = plan.get("structured_spec") if isinstance(plan.get("structured_spec"), dict) else {}
    plan["structured_spec"] = {
        **old_spec,
        "headline": copy["headline"],
        "center_label": copy["center_label"],
        "components": copy["nodes"],
        "scene_recipe": copy["scene_recipe"],
        "layout_variant": _resolved_layout_variant(copy["scene_recipe"], copy.get("layout_variant"))["id"],
        "motion_variant": _resolved_layout_variant(copy["scene_recipe"], copy.get("layout_variant"))["motion_variant"],
        "copy_plan": {
            "status": "ready",
            "scene_goal": copy["scene_goal"],
            "source": "text_ai",
            "model": copy["model"],
            "fingerprint": copy["fingerprint"],
            "generated_at": copy["generated_at"],
        },
    }
    plan["revision"] = int(_as_number(plan.get("revision"), 0)) + 1
    plan["status"] = "saved"
    plan["updated_at"] = _now()
    _cancel_scene_motion_generation(scene, "AI 画面文案已更新，旧的动态素材任务已取消")
    scene["motion_visual_candidate"] = None
    if (scene.get("motion_generation") or {}).get("status") != "cancelled":
        scene["motion_generation"] = {"status": "idle", "engine": "hyperframes", "error": ""}
    _decision(
        state,
        "visual_copy_plan",
        f"{scene_id} HyperFrames 画面文案",
        copy["model"],
        f"标题：{copy['headline']}；结构：{copy['scene_recipe']}；版式：{plan['structured_spec']['layout_variant']}；要点：{'、'.join(copy['nodes'])}",
    )
    _activity(
        state,
        "visual_copy_refined",
        f"已根据前后文提炼 {scene_id} 的 HyperFrames 画面文案，可继续人工修改后生成",
        scene_id=scene_id,
        model=copy["model"],
    )
    return _save(project_dir, state)


def _ppt_card_source_text(scene: dict) -> str:
    """Use narration/scene facts, never the long visual-generation prompt."""
    narration = scene.get("narration") if isinstance(scene.get("narration"), dict) else {}
    return str(narration.get("text") or scene.get("description") or scene.get("shot_intent") or "").strip()


def _ppt_card_clauses(source: str) -> list[str]:
    raw = re.split(r"[。！？；;\n]+|(?<=，)", source)
    result: list[str] = []
    for value in raw:
        cleaned = " ".join(value.strip(" ，、。！？；;\t").split())
        if len(cleaned) < 4:
            continue
        if len(cleaned) > 26:
            cleaned = cleaned[:25].rstrip("，、。！？；; ") + "…"
        if cleaned not in result:
            result.append(cleaned)
    return result[:4]


def _default_ppt_card_brief(scene: dict) -> dict:
    source = _ppt_card_source_text(scene)
    clauses = _ppt_card_clauses(source)
    title = str(scene.get("title") or "").strip()
    if not title or re.fullmatch(r"(?:T|场景|scene)[-_ ]*\d+.*", title, flags=re.IGNORECASE):
        title = clauses[0][:18] if clauses else "本段核心信息"
    takeaway = source[:48].rstrip("，、。！？；; ")
    items = clauses[1:4] if len(clauses) > 1 else clauses[:]
    if len(items) < 2:
        shot_intent = " ".join(str(scene.get("shot_intent") or "").split())[:22]
        if shot_intent and shot_intent not in items:
            items.append(shot_intent)
    while len(items) < 2:
        items.append("请补充本段关键要点")
    return {
        "version": 1,
        "status": "draft",
        "source_text": source[:600],
        "title": title[:28],
        "takeaway": takeaway,
        "items": items[:4],
        "metrics": [],
        "card_type": "headline_metrics",
        "theme": "tech_neon",
        "revision": 0,
        # This draft is a read-time projection until the user explicitly
        # saves it.  A wall-clock value here made two reads of identical
        # persisted state differ and poisoned callers that need a stable
        # snapshot.  Saved briefs still receive a real timestamp in
        # _normalized_ppt_card_brief().
        "updated_at": None,
    }


def _normalized_ppt_card_brief(scene: dict, payload: dict, previous: dict | None = None) -> dict:
    previous = previous if isinstance(previous, dict) else _default_ppt_card_brief(scene)
    source = _ppt_card_source_text(scene)
    title = " ".join(str(payload.get("title", previous.get("title") or "")).split())[:28]
    takeaway = " ".join(str(payload.get("takeaway", previous.get("takeaway") or "")).split())[:48]
    items_raw = payload.get("items", previous.get("items") or [])
    items: list[str] = []
    for value in items_raw if isinstance(items_raw, list) else []:
        cleaned = " ".join(str(value or "").split())[:26]
        if cleaned and cleaned not in items:
            items.append(cleaned)
    if not title:
        raise WorkbenchError("信息卡标题不能为空，请写出本段要传达的核心结论")
    if len(items) < 2:
        raise WorkbenchError("信息卡至少需要两条简短要点，避免生成空白版式")
    if len(items) > 4:
        raise WorkbenchError("信息卡最多保留四条要点，避免文字过密")
    card_type = str(payload.get("card_type", previous.get("card_type") or "headline_metrics"))
    if card_type not in CARD_TYPES:
        raise WorkbenchError("信息卡结构无效")
    theme = str(payload.get("theme", previous.get("theme") or "tech_neon"))
    if theme not in {"tech_neon", "editorial", "signal_amber"}:
        raise WorkbenchError("信息卡主题无效")
    metrics = payload.get("metrics", previous.get("metrics") or [])
    normalized_metrics: list[dict[str, str]] = []
    if isinstance(metrics, list):
        for item in metrics[:3]:
            if not isinstance(item, dict):
                continue
            label = " ".join(str(item.get("label") or "").split())[:18]
            value = " ".join(str(item.get("value") or "").split())[:18]
            if label or value:
                normalized_metrics.append({"label": label or "指标", "value": value or "—"})
    return {
        "version": 1,
        "status": "saved",
        "source_text": source[:600],
        "title": title,
        "takeaway": takeaway,
        "items": items,
        "metrics": normalized_metrics,
        "card_type": card_type,
        "theme": theme,
        "revision": int(_as_number(previous.get("revision"), 0)) + 1,
        "updated_at": _now(),
    }


def update_scene_ppt_card_brief(project_dir: Path, scene_id: str, payload: dict) -> dict:
    """Persist the human-confirmed, short on-card copy before rendering."""
    state = _load_for_write(project_dir)
    scene = _find(state["scenes"], scene_id, "场景")
    _ensure_scene_visual_state(state, scene)
    scene["ppt_card_brief"] = _normalized_ppt_card_brief(scene, payload, scene.get("ppt_card_brief"))
    _activity(state, "ppt_card_brief_saved", f"已保存 {scene_id} 的信息卡内容草案", scene_id=scene_id, revision=scene["ppt_card_brief"]["revision"])
    return _save(project_dir, state)


def read_task_center(project_dir: Path) -> dict:
    """Expose a read-only task projection without changing workbench state."""
    return collect_tasks(read_workbench(project_dir))


def _ppt_card_job(scene: dict) -> dict:
    job = scene.get("ppt_card_generation")
    if not isinstance(job, dict):
        raise WorkbenchError("本场景没有可继续的信息卡任务")
    return job


@_project_transactional
def start_scene_ppt_card_generation(project_dir: Path, scene_id: str, payload: dict) -> dict:
    """Queue a deterministic local PPT information card for one scene."""
    state = _load_for_write(project_dir)
    _require_no_review_preview_conflict(_automation(state))
    scene = _find(state["scenes"], scene_id, "场景")
    _ensure_scene_visual_state(state, scene)
    current = scene.get("ppt_card_generation") if isinstance(scene.get("ppt_card_generation"), dict) else {}
    if current.get("status") in {"queued", "generating", "running"}:
        raise WorkbenchError("本场景的信息卡正在生成，请等待当前任务完成")
    if payload.get("confirmed") is not True:
        raise WorkbenchError("请先确认信息卡内容后再开始生成")
    brief = scene.get("ppt_card_brief") if isinstance(scene.get("ppt_card_brief"), dict) else _default_ppt_card_brief(scene)
    if brief.get("status") != "saved":
        raise WorkbenchError("请先保存并确认信息卡的标题、结论和至少两条要点，再开始生成")
    width, height = _render_dimensions(project_dir, state)
    presenter_treatment = str(_scene_presenter(scene).get("treatment") or "hidden")
    spec = normalize_ppt_card_spec(brief, scene, width, height, presenter_treatment)
    cards = state.setdefault("ppt_cards", [])
    card_id = _numbered("PC-", cards, "id")
    job_id = f"PPTC-{uuid4().hex[:10].upper()}"
    scene["ppt_card_generation"] = {
        "job_id": job_id,
        "card_id": card_id,
        "status": "queued",
        "stage": "已确认信息卡内容，等待本地编译",
        "started_at": _now(),
        "finished_at": None,
        "total_slots": 1,
        "completed_slots": 0,
        "failed_slots": 0,
        "error": "",
        "spec": spec,
        "brief_revision": brief.get("revision"),
        "result": {},
    }
    _decision(state, "ppt_card_plan", f"{scene_id} 信息卡", CARD_TYPES[spec["card_type"]], f"{card_id} · {spec['theme']}")
    _activity(state, "ppt_card_queued", f"已提交 {scene_id} 的信息卡任务 {card_id}", scene_id=scene_id, card_id=card_id, job_id=job_id)
    return _save(project_dir, state)


def generate_scene_ppt_card(project_dir: Path, scene_id: str, job_id: str) -> dict:
    """Render a queued card and register it as an ordinary project asset."""
    state = _load_for_write(project_dir)
    scene = _find(state["scenes"], scene_id, "场景")
    job = _ppt_card_job(scene)
    if str(job.get("job_id") or "") != str(job_id):
        raise WorkbenchError("信息卡任务已被新的请求替代")
    if job.get("status") not in {"queued", "generating"}:
        raise WorkbenchError("信息卡任务不处于可生成状态")
    spec = job.get("spec") if isinstance(job.get("spec"), dict) else None
    if not spec:
        raise WorkbenchError("信息卡任务缺少可恢复的内容方案")
    card_id = str(job.get("card_id") or "")
    if not card_id:
        raise WorkbenchError("信息卡任务缺少素材编号")
    job.update({"status": "generating", "stage": "正在编译可编辑信息卡", "error": ""})
    _save(project_dir, state)

    output_directory = project_dir / "assets" / "images" / "ppt_cards" / card_id
    paths = render_ppt_card(output_directory, card_id, spec)

    state = _load_for_write(project_dir)
    scene = _find(state["scenes"], scene_id, "场景")
    job = _ppt_card_job(scene)
    if str(job.get("job_id") or "") != str(job_id) or job.get("status") not in {"queued", "generating"}:
        raise WorkbenchError("信息卡任务已取消或被新的请求替代")
    png_relative = _safe_relpath(project_dir, str(paths["png"]))
    svg_relative = _safe_relpath(project_dir, str(paths["svg"]))
    editable_spec_relative = _safe_relpath(project_dir, str(paths["spec"]))
    artifact_path = project_dir / "artifacts" / "ppt_cards" / f"{card_id}.json"
    _atomic_write(artifact_path, {
        "card_id": card_id,
        "scene_id": scene_id,
        "job_id": job_id,
        "spec": spec,
        "render_paths": {"png": png_relative, "svg": svg_relative, "editable_spec": editable_spec_relative},
        "created_at": _now(),
    })
    spec_relative = _safe_relpath(project_dir, str(artifact_path))
    asset = _append_asset(project_dir, state, {
        "name": f"{scene.get('title') or scene_id} · PPT 信息卡 {card_id}",
        "type": "image",
        "source_type": "local_generated",
        "path": png_relative,
        "resolution": f"{spec['width']}x{spec['height']}",
        "provider": "PPT Master",
        "source_tool": "ppt_card_provider",
        "license": "项目内本地生成，可编辑信息卡",
        "generation": {
            "kind": "ppt_information_card",
            "card_id": card_id,
            "card_type": spec["card_type"],
            "source_scene_id": scene_id,
            "source_svg_path": svg_relative,
            "spec_path": spec_relative,
            "editable_spec_path": editable_spec_relative,
            "safe_areas": spec.get("safe_areas"),
        },
    })
    record = {
        "id": card_id,
        "scene_id": scene_id,
        "asset_id": asset["id"],
        "spec": spec,
        "png_path": png_relative,
        "svg_path": svg_relative,
        "spec_path": spec_relative,
        "editable_spec_path": editable_spec_relative,
        "created_at": _now(),
    }
    state.setdefault("ppt_cards", []).append(record)
    scene["ppt_card_candidate"] = {
        "card_id": card_id,
        "asset_id": asset["id"],
        "path": png_relative,
        "card_type": spec["card_type"],
        "status": "ready",
    }
    job.update({
        "status": "completed",
        "stage": "信息卡已登记到素材台账，等待人工采用",
        "completed_slots": 1,
        "failed_slots": 0,
        "finished_at": _now(),
        "asset_id": asset["id"],
        "result": {"asset_id": asset["id"], "card_id": card_id, "png_path": png_relative, "svg_path": svg_relative},
    })
    _decision(state, "ppt_card_asset", f"{scene_id} 信息卡素材", asset["id"], f"{card_id} 已入素材台账")
    _activity(state, "ppt_card_completed", f"{scene_id} 的信息卡 {card_id} 已生成，素材编号 {asset['id']}", scene_id=scene_id, card_id=card_id, asset_id=asset["id"])
    return _save(project_dir, state)


def mark_scene_ppt_card_failed(project_dir: Path, scene_id: str, job_id: str, error: object) -> dict:
    state = _load_for_write(project_dir)
    scene = _find(state["scenes"], scene_id, "场景")
    job = _ppt_card_job(scene)
    if str(job.get("job_id") or "") != str(job_id):
        return state
    job.update({
        "status": "failed",
        "stage": "信息卡生成失败，可在任务中心重试",
        "failed_slots": 1,
        "finished_at": _now(),
        "error": str(error)[:1200],
    })
    _activity(state, "ppt_card_failed", f"{scene_id} 的信息卡任务失败", scene_id=scene_id, job_id=job_id, error=str(error)[:500])
    return _save(project_dir, state)


def retry_scene_ppt_card_generation(project_dir: Path, scene_id: str, job_id: str) -> dict:
    state = read_workbench(project_dir)
    scene = _find(state["scenes"], scene_id, "场景")
    job = _ppt_card_job(scene)
    if str(job.get("job_id") or "") != str(job_id):
        raise WorkbenchError("只能重试当前显示的信息卡任务")
    if job.get("status") != "failed":
        raise WorkbenchError("只有失败的信息卡任务可以重试")
    return start_scene_ppt_card_generation(project_dir, scene_id, {"confirmed": True})


def _validated_visual_timeline(state: dict, scene: dict, raw_blocks: Any) -> list[dict]:
    if not isinstance(raw_blocks, list) or not raw_blocks:
        raise WorkbenchError("视觉时间线至少需要一个画面区间")
    duration = _rounded_seconds(_scene_duration(scene))
    minimum_block_duration = min(.4, duration)
    fps = max(1, int(state.get("settings", {}).get("frame_rate") or 30))
    assets = {str(item.get("id")): item for item in state.get("assets", []) if isinstance(item, dict)}
    normalized: list[dict] = []
    used_ids: set[str] = set()
    for index, raw in enumerate(raw_blocks, 1):
        if not isinstance(raw, dict):
            raise WorkbenchError("视觉时间线区间格式无效")
        start = _rounded_seconds(raw.get("start_seconds"))
        end = _rounded_seconds(raw.get("end_seconds"))
        if end - start < minimum_block_duration - .001:
            raise WorkbenchError(f"第 {index} 个画面区间太短；每格至少需要 {minimum_block_duration:.2f} 秒")
        asset_id = str(raw.get("asset_id") or "")
        asset = assets.get(asset_id)
        if not asset or str(asset.get("type") or "").lower() not in {"image", "video"} or not asset.get("path"):
            raise WorkbenchError(f"第 {index} 个画面区间没有选择可用的图片或视频素材")
        mode = str(raw.get("source_mode") or _visual_source_mode(asset))
        if mode not in VISUAL_SOURCE_MODES:
            raise WorkbenchError(f"第 {index} 个画面区间的素材方式无效")
        asset_type = str(asset.get("type") or "").lower()
        has_source_window = "source_in_seconds" in raw or "source_out_seconds" in raw
        source_in = source_out = None
        if has_source_window:
            if asset_type != "video":
                raise WorkbenchError(f"第 {index} 个图片区间不能设置源视频时间范围")
            if "source_in_seconds" not in raw or "source_out_seconds" not in raw:
                raise WorkbenchError(f"第 {index} 个本地视频区间必须同时设置源入点和源出点")
            source_in = _rounded_seconds(raw.get("source_in_seconds"))
            source_out = _rounded_seconds(raw.get("source_out_seconds"))
            if source_out <= source_in:
                raise WorkbenchError(f"第 {index} 个本地视频的源出点必须晚于源入点")
            display_frames = max(1, _nonnegative_frame(end, fps) - _nonnegative_frame(start, fps))
            source_frames = max(1, _nonnegative_frame(source_out, fps) - _nonnegative_frame(source_in, fps))
            if abs(display_frames - source_frames) > 1:
                raise WorkbenchError(f"第 {index} 个本地视频的源区间与显示区间必须按帧一致，不能自动变速或循环")
            available_duration = _as_number(asset.get("duration_seconds"))
            if available_duration > 0 and source_out > available_duration + (1 / fps):
                raise WorkbenchError(f"第 {index} 个本地视频的源出点超过素材实际时长")
        visual_role = str(raw.get("visual_role") or "")
        cut_policy = str(raw.get("cut_policy") or "")
        sequence_id = str(raw.get("sequence_id") or "")
        planner_evidence = raw.get("planner_evidence")
        has_orchestration_metadata = bool(visual_role or cut_policy or sequence_id or planner_evidence is not None)
        if has_orchestration_metadata:
            if visual_role not in LOCAL_MATERIAL_VISUAL_ROLES:
                raise WorkbenchError(f"第 {index} 个画面区间的素材角色无效")
            if cut_policy not in LOCAL_MATERIAL_CUT_POLICIES:
                raise WorkbenchError(f"第 {index} 个画面区间的连续性策略无效")
            if visual_role == "local_full_bleed" and asset_type != "video":
                raise WorkbenchError(f"第 {index} 个本地全屏动作必须使用视频素材")
            if cut_policy == "atomic" and (not sequence_id or source_in is None or source_out is None):
                raise WorkbenchError(f"第 {index} 个完整动作缺少连续序列或真实源时间范围")
            if sequence_id and not re.fullmatch(r"LMS-\d{3}", sequence_id):
                raise WorkbenchError(f"第 {index} 个画面区间的连续序列编号无效")
            if planner_evidence is not None:
                if not isinstance(planner_evidence, dict):
                    raise WorkbenchError(f"第 {index} 个画面区间的编排证据格式无效")
                if (
                    planner_evidence.get("source") != "local_material_orchestration_v1"
                    or not re.fullmatch(r"SHOT-\d{4}", str(planner_evidence.get("shot_id") or ""))
                    or not re.fullmatch(r"[0-9a-f]{64}", str(planner_evidence.get("index_fingerprint") or "").lower())
                ):
                    raise WorkbenchError(f"第 {index} 个画面区间缺少可核验的本地素材证据")
        requested_id = str(raw.get("id") or "")
        block_id = requested_id if re.fullmatch(r"VB-\d{3}", requested_id) and requested_id not in used_ids else f"VB-{index:03d}"
        while block_id in used_ids:
            block_id = f"VB-{index + len(used_ids):03d}"
        used_ids.add(block_id)
        normalized_block = {
            "id": block_id, "start_seconds": start, "end_seconds": end,
            "story_id": str(scene.get("story_id") or raw.get("story_id") or ""),
            "source_mode": mode, "asset_id": asset_id,
            "label": str(raw.get("label") or asset.get("name") or asset_id)[:160],
            "status": "ready",
            "locked": bool(raw.get("locked")),
            "query": str(raw.get("query") or "")[:500],
            "context_text": str(raw.get("context_text") or "")[:800],
            "attempt": max(0, int(_as_number(raw.get("attempt")))),
            "error": "",
        }
        if has_source_window:
            normalized_block.update({"source_in_seconds": source_in, "source_out_seconds": source_out})
        if has_orchestration_metadata:
            normalized_block.update({
                "visual_role": visual_role,
                "cut_policy": cut_policy,
                "sequence_id": sequence_id or None,
                "planner_evidence": deepcopy(planner_evidence) if isinstance(planner_evidence, dict) else None,
            })
        normalized.append(normalized_block)
    normalized.sort(key=lambda item: item["start_seconds"])
    tolerance = .012
    if abs(normalized[0]["start_seconds"]) > tolerance:
        raise WorkbenchError("视觉时间线必须从本段 0 秒开始")
    normalized[0]["start_seconds"] = 0.0
    for previous, current in zip(normalized, normalized[1:]):
        if abs(previous["end_seconds"] - current["start_seconds"]) > tolerance:
            relation = "重叠" if previous["end_seconds"] > current["start_seconds"] else "存在空白"
            raise WorkbenchError(f"视觉时间线区间{relation}；请让相邻区间首尾相接")
        current["start_seconds"] = previous["end_seconds"]
    if abs(normalized[-1]["end_seconds"] - duration) > tolerance:
        raise WorkbenchError(f"视觉时间线必须覆盖本段完整时长 {duration:.3f} 秒")
    normalized[-1]["end_seconds"] = duration
    return normalized


def _commit_visual_timeline(state: dict, scene: dict, blocks: list[dict]) -> None:
    """Replace selected block usages while preserving stable block identity."""
    scene_id = str(scene.get("id") or "")
    for usage in state.get("usages", []):
        if usage.get("scene_id") == scene_id and usage.get("role") == "visual_block":
            usage["selected"] = False
    for block in blocks:
        usage = _append_visual_block_usage(state, scene, block, str(block["asset_id"]))
        block["usage_id"] = usage["id"]
    old = scene.get("visual_timeline") or {}
    scene["visual_timeline"] = {
        "version": 2, "revision": int(_as_number(old.get("revision"), 0)) + 1,
        "blocks": blocks, "updated_at": _now(),
    }


def update_scene_visual_timeline(project_dir: Path, scene_id: str, payload: dict) -> dict:
    """Atomically replace one scene's visual blocks after strict gap checks."""
    state = _load_for_write(project_dir)
    scene = _find(state["scenes"], scene_id, "场景")
    _ensure_scene_visual_state(state, scene)
    blocks = _validated_visual_timeline(state, scene, payload.get("blocks"))
    _commit_visual_timeline(state, scene, blocks)
    _invalidate_scene_review_preview(scene, "片段内视觉时间线已更新，请刷新本段审核预览")
    scene["review_status"] = "needs_adjustment"
    _mark_render_needs_refresh(state, f"{scene_id} 的片段内视觉时间线已更新")
    _decision(state, "visual_timeline", f"{scene_id} 片段内画面", f"{len(blocks)} 个无缝区间", f"时间线版本 {scene['visual_timeline']['revision']}")
    _activity(state, "visual_timeline_saved", f"已保存 {scene_id} 的 {len(blocks)} 个画面区间", scene_id=scene_id, block_count=len(blocks))
    return _save(project_dir, state)


def _validated_visual_composition(project_dir: Path, state: dict, scene: dict, payload: Any) -> dict:
    """Validate the bounded base-plus-hero contract used by preview and final render."""
    if not isinstance(payload, dict):
        raise WorkbenchError("画面布局数据格式无效")
    if payload.get("version") is not None and int(_as_number(payload.get("version"), -1)) != 1:
        raise WorkbenchError("当前工作台只支持第 1 版画面布局合同")
    layout = str(payload.get("layout_recipe") or "full_bleed")
    if layout not in VISUAL_COMPOSITION_LAYOUTS:
        raise WorkbenchError("画面布局只能选择全屏画面或重点素材卡片")
    if layout == "focus_card" and str(_scene_presenter(scene).get("treatment") or "hidden") == "fullscreen":
        raise WorkbenchError("全屏数字人场景不会显示底层画面，请先切换为画中画或隐藏数字人")
    raw_overlays = payload.get("overlays") if isinstance(payload.get("overlays"), list) else []
    if len(raw_overlays) > VISUAL_COMPOSITION_MAX_OVERLAYS:
        raise WorkbenchError(f"单个场景最多只能配置 {VISUAL_COMPOSITION_MAX_OVERLAYS} 个重点素材片段")

    scene_duration = _rounded_seconds(_scene_duration(scene))
    fps = max(1, int(state.get("settings", {}).get("frame_rate") or 30))
    canvas_width, canvas_height = _render_dimensions(project_dir, state)
    assets = {str(item.get("id")): item for item in state.get("assets", []) if isinstance(item, dict)}
    normalized: list[dict] = []
    used_ids: set[str] = set()
    for index, raw in enumerate(raw_overlays, 1):
        if not isinstance(raw, dict):
            raise WorkbenchError(f"第 {index} 个重点素材格式无效")
        role = str(raw.get("role") or "hero")
        if role not in VISUAL_COMPOSITION_ROLES:
            raise WorkbenchError(f"第 {index} 个上层素材角色无效")
        start = _rounded_seconds(raw.get("start_seconds"))
        end = _rounded_seconds(raw.get("end_seconds"))
        if end - start < .4 - .001:
            raise WorkbenchError(f"第 {index} 个重点素材至少需要显示 0.4 秒")
        if start < 0 or end > scene_duration + .012:
            raise WorkbenchError(f"第 {index} 个重点素材超出本段 {scene_duration:.3f} 秒范围")
        end = min(end, scene_duration)
        asset_id = str(raw.get("asset_id") or "")
        asset = assets.get(asset_id)
        asset_type = str((asset or {}).get("type") or "").lower()
        if not asset or asset_type not in {"image", "video"} or not asset.get("path"):
            raise WorkbenchError(f"第 {index} 个重点素材没有选择项目内可用的图片或视频")
        source = (project_dir / str(asset.get("path"))).resolve()
        try:
            source.relative_to(project_dir.resolve())
        except ValueError as exc:
            raise WorkbenchError(f"第 {index} 个重点素材不在当前项目目录内") from exc
        if not source.is_file():
            raise WorkbenchError(f"第 {index} 个重点素材文件不存在")
        fit = str(raw.get("fit") or "contain")
        if fit not in VISUAL_COMPOSITION_FITS:
            raise WorkbenchError(f"第 {index} 个重点素材适配方式无效")
        if "muted" in raw and raw.get("muted") is not True:
            raise WorkbenchError(f"第 {index} 个重点视频必须静音，不能混入素材原声")
        muted = True
        raw_playback_rate = raw.get("playback_rate", 1)
        if isinstance(raw_playback_rate, bool) or not isinstance(raw_playback_rate, (int, float)):
            raise WorkbenchError(f"第 {index} 个重点视频的播放速度格式无效")
        playback_rate = _as_number(raw_playback_rate, 1)
        if abs(playback_rate - 1) > 1e-6:
            raise WorkbenchError(f"第 {index} 个重点视频第一版只允许 1 倍速播放")

        if asset_type == "video" and _as_number(raw.get("source_in_seconds"), 0) < 0:
            raise WorkbenchError(f"第 {index} 个重点视频的源入点不能小于 0 秒")
        source_in = _rounded_seconds(raw.get("source_in_seconds")) if asset_type == "video" else 0.0
        overlay_duration = end - start
        source_out = _rounded_seconds(
            raw.get("source_out_seconds"), source_in + overlay_duration
        ) if asset_type == "video" else _rounded_seconds(overlay_duration)
        if asset_type == "video":
            if source_out <= source_in:
                raise WorkbenchError(f"第 {index} 个重点视频的源出点必须晚于源入点")
            display_frames = max(1, _nonnegative_frame(end, fps) - _nonnegative_frame(start, fps))
            source_frames = max(1, _nonnegative_frame(source_out, fps) - _nonnegative_frame(source_in, fps))
            if abs(source_frames - display_frames) > 1:
                raise WorkbenchError(
                    f"第 {index} 个重点视频的源区间与显示区间必须在 1 帧误差内保持一致，不能自动变速或循环"
                )
            available_duration = _as_number(asset.get("duration_seconds"))
            if available_duration <= 0:
                available_duration = _probe_duration_seconds(source, _ffmpeg_available(), 0)
            if available_duration > 0 and source_out > available_duration + (1 / fps):
                raise WorkbenchError(
                    f"第 {index} 个重点视频的源出点 {source_out:.3f} 秒超过素材时长 {available_duration:.3f} 秒"
                )

        placement = _validated_visual_placement(
            raw.get("placement"), asset, canvas_width, canvas_height, index,
        )
        candidate_evidence = _validated_candidate_evidence(raw.get("candidate_evidence"), index)
        planner_evidence = _validated_local_material_planner_evidence(raw.get("planner_evidence"), index)

        requested_id = str(raw.get("id") or "")
        overlay_id = requested_id if re.fullmatch(r"VL-\d{3}", requested_id) and requested_id not in used_ids else f"VL-{index:03d}"
        while overlay_id in used_ids:
            overlay_id = f"VL-{index + len(used_ids):03d}"
        used_ids.add(overlay_id)
        normalized.append({
            "id": overlay_id,
            "role": role,
            "asset_id": asset_id,
            "start_seconds": start,
            "end_seconds": end,
            "source_in_seconds": source_in,
            "source_out_seconds": source_out,
            "fit": fit,
            "muted": True,
            "playback_rate": 1.0,
            "placement": placement,
            "candidate_evidence": candidate_evidence,
            "planner_evidence": planner_evidence,
            "locked": bool(raw.get("locked")),
        })

    normalized.sort(key=lambda item: (item["start_seconds"], item["end_seconds"], item["id"]))
    for previous, current in zip(normalized, normalized[1:]):
        if previous["end_seconds"] > current["start_seconds"] + .012:
            raise WorkbenchError("重点素材的显示区间不能互相重叠")

    old = _ensure_scene_visual_composition(scene)
    old_locked = {
        str(item.get("id")): item for item in old.get("overlays") or []
        if isinstance(item, dict) and item.get("locked")
    }
    by_id = {item["id"]: item for item in normalized}
    for overlay_id, frozen in old_locked.items():
        replacement = by_id.get(overlay_id)
        if replacement is None:
            raise WorkbenchError(f"{overlay_id} 已锁定，请先解锁后再删除")
        frozen_value = {key: value for key, value in frozen.items() if key != "locked"}
        replacement_value = {key: value for key, value in replacement.items() if key != "locked"}
        for value in (frozen_value, replacement_value):
            value.setdefault("muted", True)
            value.setdefault("playback_rate", 1.0)
            value.setdefault("placement", None)
            value.setdefault("candidate_evidence", None)
            value.setdefault("planner_evidence", None)
        if _json_hash(frozen_value) != _json_hash(replacement_value):
            raise WorkbenchError(f"{overlay_id} 已锁定，请先解锁后再修改")

    raw_style = payload.get("frame_style") if isinstance(payload.get("frame_style"), dict) else {}
    default_style = _default_visual_composition()["frame_style"]
    width_ratio = _as_number(raw_style.get("width_ratio"), default_style["width_ratio"])
    height_ratio = _as_number(raw_style.get("height_ratio"), default_style["height_ratio"])
    radius_ratio = _as_number(raw_style.get("border_radius_ratio"), default_style["border_radius_ratio"])
    if not .35 <= width_ratio <= .96 or not .25 <= height_ratio <= .9:
        raise WorkbenchError("重点素材卡片的宽高比例超出安全范围")
    if not 0 <= radius_ratio <= .1:
        raise WorkbenchError("重点素材卡片圆角比例无效")
    border_color = str(raw_style.get("border_color") or default_style["border_color"]).upper()
    if not re.fullmatch(r"#[0-9A-F]{6}", border_color):
        raise WorkbenchError("重点素材卡片描边颜色必须是 #RRGGBB")
    shadow = str(raw_style.get("shadow") or default_style["shadow"])
    if shadow not in {"soft", "none"}:
        raise WorkbenchError("重点素材卡片阴影样式无效")

    return {
        "version": 1,
        "layout_recipe": layout,
        "background": {
            "source": "visual_timeline",
            "treatment": "auto_dim" if layout == "focus_card" else "normal",
        },
        "overlays": normalized,
        "frame_style": {
            "width_ratio": round(width_ratio, 4),
            "height_ratio": round(height_ratio, 4),
            "border_radius_ratio": round(radius_ratio, 4),
            "border_color": border_color,
            "shadow": shadow,
        },
    }


def update_scene_visual_composition(project_dir: Path, scene_id: str, payload: dict) -> dict:
    """Atomically replace one scene's semantic layer contract."""
    state = _load_for_write(project_dir)
    scene = _find(state["scenes"], scene_id, "场景")
    _ensure_scene_visual_state(state, scene)
    old = _ensure_scene_visual_composition(scene)
    expected_revision = payload.get("expected_revision")
    if expected_revision is None:
        raise WorkbenchError("保存画面布局时必须提供当前版本号")
    if int(_as_number(expected_revision, -1)) != int(_as_number(old.get("revision"), 1)):
        raise WorkbenchConflict("画面布局已在其他操作中更新，请刷新页面后重试")
    validated = _validated_visual_composition(project_dir, state, scene, payload)
    validated.update({
        "revision": int(_as_number(old.get("revision"), 0)) + 1,
        "updated_at": _now(),
    })
    render_changed = _json_hash(_visual_composition_render_contract(old)) != _json_hash(
        _visual_composition_render_contract(validated)
    )
    scene["visual_composition"] = validated
    if render_changed:
        _invalidate_scene_review_preview(scene, "画面布局已更新，请刷新本段审核预览")
        scene["review_status"] = "needs_adjustment"
        _mark_render_needs_refresh(state, f"{scene_id} 的画面布局已更新")
    label = "重点素材卡片" if validated["layout_recipe"] == "focus_card" else "全屏画面"
    _decision(
        state,
        "visual_composition",
        f"{scene_id} 画面布局",
        label,
        f"布局版本 {validated['revision']}，{len(validated['overlays'])} 个重点素材区间",
    )
    _activity(
        state,
        "visual_composition_saved",
        f"已保存 {scene_id} 的{label}",
        scene_id=scene_id,
        overlay_count=len(validated["overlays"]),
    )
    return _save(project_dir, state)


def _visual_profile(profile: str) -> dict:
    key = str(profile or "auto")
    if key not in VISUAL_BATCH_PROFILES:
        raise WorkbenchError("画面节奏只能选择智能混合、图片或视频")
    return {"id": key, **VISUAL_BATCH_PROFILES[key]}


def _clean_search_terms(value: Any, *, limit: int = 24) -> list[str]:
    if isinstance(value, str):
        values = re.split(r"[,，、;；\n]+", value)
    elif isinstance(value, (list, tuple, set)):
        values = list(value)
    else:
        values = []
    cleaned: list[str] = []
    for raw in values:
        term = re.sub(r"\s+", " ", str(raw or "")).strip()[:60]
        if term and term not in cleaned:
            cleaned.append(term)
        if len(cleaned) >= limit:
            break
    return cleaned


def _clean_query_overrides(value: Any) -> dict[str, dict[str, str]]:
    if not isinstance(value, dict):
        return {}
    cleaned: dict[str, dict[str, str]] = {}
    for scene_id, blocks in value.items():
        if not isinstance(blocks, dict):
            continue
        scene_overrides: dict[str, str] = {}
        for block_id, query in blocks.items():
            query_text = re.sub(r"\s+", " ", str(query or "")).strip()[:160]
            if query_text:
                scene_overrides[str(block_id)[:80]] = query_text
        if scene_overrides:
            cleaned[str(scene_id)[:80]] = scene_overrides
    return cleaned


def _visual_batch_policy(payload: dict) -> dict:
    operation = str(payload.get("operation_mode") or "fill_missing")
    if operation not in VISUAL_BATCH_OPERATIONS:
        raise WorkbenchError("批量画面操作只能选择补全缺失画面或替换所选画面")
    mix_strategy = str(payload.get("mix_strategy") or "balanced")
    if mix_strategy not in VISUAL_BATCH_MIX_STRATEGIES:
        raise WorkbenchError("智能混合比例无效")
    image_source = str(payload.get("image_source") or "web_download")
    if image_source not in VISUAL_BATCH_IMAGE_SOURCES:
        raise WorkbenchError("图片来源只能选择网络图片或 OpenAI 生图")
    requested_rules = payload.get("content_rules")
    if requested_rules is None:
        requested_rules = VISUAL_BATCH_DEFAULT_RULES
    rules = list(dict.fromkeys(str(value) for value in requested_rules))
    invalid = [value for value in rules if value not in VISUAL_BATCH_CONTENT_RULES]
    if invalid:
        raise WorkbenchError(f"不支持的画面内容规则：{'、'.join(invalid)}")
    person_policy = str(payload.get("person_policy") or "balanced")
    if person_policy not in VISUAL_BATCH_PERSON_POLICIES:
        raise WorkbenchError("人物出镜策略只能选择宽松、平衡或严格")
    try:
        candidate_limit = int(payload.get("candidate_limit") or 6)
    except (TypeError, ValueError) as exc:
        raise WorkbenchError("候选素材尝试次数无效") from exc
    if candidate_limit not in VISUAL_BATCH_CANDIDATE_LIMITS:
        raise WorkbenchError("候选素材尝试次数只能选择 4、6 或 8")
    planning_mode = str(payload.get("planning_mode") or "rule_mix")
    if planning_mode not in VISUAL_BATCH_PLANNING_MODES:
        raise WorkbenchError("画面规划方式只能选择 AI 智能导演或规则混合")
    detector_available = False
    try:
        import cv2  # type: ignore
        detector_available = bool(getattr(cv2, "data", None))
    except Exception:
        pass
    duration_balance = deepcopy(VISUAL_BATCH_DURATION_BALANCE[mix_strategy])
    return {
        "operation_mode": operation,
        "mix_strategy": mix_strategy,
        "image_source": image_source,
        "content_rules": rules,
        "person_policy": person_policy,
        "candidate_limit": candidate_limit,
        "planning_mode": planning_mode,
        "primary_image_policy": "manual_only" if planning_mode == "ai_director" else "rule_mix_allowed",
        "allowed_ai_routes": sorted(VISUAL_BATCH_AI_DEFAULT_ROUTES),
        "duration_balance": duration_balance,
        "fallback_policy": "pause",
        "screening_mode": "local_detector" if detector_available else "semantic_only",
        "search_theme": re.sub(r"\s+", " ", str(payload.get("search_theme") or "")).strip()[:120],
        "preferred_keywords": _clean_search_terms(payload.get("preferred_keywords")),
        "cautious_topics": _clean_search_terms(payload.get("cautious_topics")),
        "query_overrides": _clean_query_overrides(payload.get("query_overrides")),
    }


def _visual_media_kind(profile: str, index: int, count: int, mix_strategy: str) -> str:
    if profile == "video":
        return "video"
    if profile == "image":
        return "image"
    if count <= 1:
        return "video"
    patterns = {
        "balanced": ("video", "image"),
        "video_first": ("video", "video", "image"),
        "motion_first": ("image", "image", "video"),
        "image_first": ("image", "image", "video"),
    }
    pattern = patterns[mix_strategy]
    kinds = [pattern[position % len(pattern)] for position in range(count)]
    if count > 1 and len(set(kinds)) == 1:
        kinds[-1] = "image" if kinds[0] == "video" else "video"
    return kinds[index % len(kinds)]


def _visual_rule_query(query: str, rules: list[str], person_policy: str = "balanced") -> str:
    # Pexels is a positive-keyword search engine.  Terms such as "no people"
    # or "no presenter" are not reliable exclusions and can actually increase
    # person-led results.  Keep the query concrete and object-led; final local
    # screening remains only a safety net.
    additions: list[str] = []
    if person_policy == "strict":
        additions.append("object detail")
    return " ".join(dict.fromkeys([query.strip(), *additions])).strip()[:240]


def _stock_topic_queries(context: str, *, strict_people: bool = False) -> tuple[str, tuple[str, str, str]]:
    lowered = context.lower()
    if any(marker in lowered for marker in ("科技快报", "四条消息")):
        return "科技快报总览", (
            "artificial intelligence hardware", "industrial robotics factory", "connected technology devices",
        )
    if any(marker in lowered for marker in ("评论区", "告诉我们", "你最愿意")):
        return "互动收尾", (
            "smartphone typing comments", "social media comments interface", "mobile engagement screen",
        )
    if any(marker in lowered for marker in ("苹果", "千问", "模型合作", "ai入口", "ai 入口", "相册", "语音助手", "操作系统")):
        return "手机AI生态", (
            "smartphone AI ecosystem", "connected mobile devices", "cloud AI data network",
        )
    if any(marker in lowered for marker in ("荣耀", "robot phone", "调用应用", "帮你做事", "手机里的 ai")):
        return "AI手机", (
            "smartphone AI assistant", "mobile app automation", "smartphone interface technology",
        )
    topics: tuple[tuple[str, tuple[str, ...], tuple[str, str, str]], ...] = (
        ("芯片与算力", ("芯片", "半导体", "晶圆", "光刻", "算力", "服务器", "数据中心"), (
            "semiconductor wafer manufacturing", "computer chip factory", "data center servers",
        )),
        ("机器人产业", ("宇树", "机器人", "机器狗", "机械臂", "量产", "进工厂", "产业应用", "翻跟头"), (
            "quadruped robot factory", "industrial robot assembly line", "automated manufacturing machinery",
        )),
        ("AI手机", ("手机",), (
            "smartphone AI assistant", "mobile app automation", "smartphone interface technology",
        )),
        ("真人价值与创作", ("创作", "运营", "责任", "风险", "判断", "真实体验", "建立信任", "真人完成"), (
            ("creative tools workspace" if strict_people else "creative desk overhead"),
            "technology planning dashboard", "product design sketch close up",
        )),
        ("自动化工作", ("重复工作", "固定讲解", "自动化", "接管", "替代"), (
            "automated production process", "robotic arm sorting", "workflow automation screen",
        )),
        ("数字人商业化", ("数字人", "虚拟角色", "带货", "粉丝", "宣传", "商业价值", "真人演员", "主播"), (
            "virtual avatar ecommerce interface", "social media shopping analytics", "digital marketing dashboard",
        )),
        ("科技快报总览", ("人工智能", "ai"), (
            "artificial intelligence hardware", "industrial robotics factory", "connected technology devices",
        )),
    )
    for label, markers, queries in topics:
        if any(marker in lowered for marker in markers):
            return label, queries
    return "通用纪实素材", ("technology equipment detail", "modern industry process", "documentary b roll")


def _stock_role_query(query: str, role: str) -> str:
    modifiers = {
        "establishing": "wide shot",
        "process": "working process",
        "detail": "close up",
        "outcome": "finished product",
    }
    words = re.sub(r"[^a-zA-Z0-9\s-]", " ", query).split()
    modifier = modifiers.get(role, "detail").split()
    compact = list(dict.fromkeys([*words, *modifier]))[:8]
    return " ".join(compact).strip()


def _resolved_stock_search_strategy(state: dict, policy: dict) -> dict:
    context = " ".join(
        str(scene.get(key) or "")
        for scene in state.get("scenes", [])
        for key in ("title", "description", "shot_intent")
    ).lower()
    is_tech = any(marker in context for marker in ("科技", "ai", "人工智能", "机器人", "芯片", "手机", "数字人"))
    supplied_theme = str(policy.get("search_theme") or "").strip()
    supplied_keywords = list(policy.get("preferred_keywords") or [])
    supplied_cautious = list(policy.get("cautious_topics") or [])
    cautious_is_default = supplied_cautious == list(STOCK_TECH_DEFAULT_CAUTIOUS_TOPICS)
    return {
        "theme": supplied_theme or ("AI 与高新科技" if is_tech else str(state.get("project", {}).get("title") or "按脚本自动识别")),
        "preferred_keywords": supplied_keywords or (list(STOCK_TECH_DEFAULT_KEYWORDS) if is_tech else []),
        "cautious_topics": supplied_cautious or (list(STOCK_TECH_DEFAULT_CAUTIOUS_TOPICS) if is_tech else []),
        "source": "custom" if supplied_theme or supplied_keywords or (supplied_cautious and not cautious_is_default) else "auto",
        "query_overrides": deepcopy(policy.get("query_overrides") or {}),
    }


def _stock_search_plan_for_block(
    scene: dict,
    *,
    surrounding_context: str,
    slot_index: int,
    block_id: str,
    strategy: dict,
    rules: list[str],
    person_policy: str,
) -> dict:
    text = " ".join(str(scene.get(key) or "") for key in ("title", "description", "shot_intent"))
    context = text.strip()
    role = STOCK_SEARCH_ROLES[(max(1, slot_index) - 1) % len(STOCK_SEARCH_ROLES)]
    topic, base_queries = _stock_topic_queries(context, strict_people=person_policy == "strict")
    if topic == "通用纪实素材" and surrounding_context:
        topic, base_queries = _stock_topic_queries(surrounding_context, strict_people=person_policy == "strict")
    preferred = [STOCK_KEYWORD_TRANSLATIONS.get(term, term) for term in strategy.get("preferred_keywords") or []]
    if topic == "通用纪实素材" and preferred:
        base_queries = tuple((preferred + list(base_queries))[:3])  # type: ignore[assignment]
    scene_rotation = max(0, int(_as_number(scene.get("order"), 1)) - 1)
    rotation = (scene_rotation + max(1, slot_index) - 1) % len(base_queries)
    ordered = [*base_queries[rotation:], *base_queries[:rotation]]
    levels = ("精确检索", "行业检索", "兜底检索")
    ladder = [
        {
            "level": levels[index],
            "query": _visual_rule_query(_stock_role_query(query, role), rules, person_policy),
        }
        for index, query in enumerate(ordered)
    ]
    override = str(((strategy.get("query_overrides") or {}).get(str(scene.get("id"))) or {}).get(block_id) or "").strip()
    if override:
        ladder[0] = {"level": "人工指定", "query": _visual_rule_query(override, rules, person_policy)}
    return {
        "topic": topic,
        "role": role,
        "role_label": STOCK_SEARCH_ROLE_LABELS[role],
        "query": ladder[0]["query"],
        "query_ladder": ladder,
        "context_text": f"{surrounding_context} {text}".strip(),
        "query_source": "manual" if override else "automatic",
    }


def _visual_ai_prompt(context_text: str, rules: list[str], orientation: str, person_policy: str = "balanced") -> str:
    constraints = ["画面中不要出现第二位主持人或主播"]
    if person_policy == "strict":
        constraints.append("画面完全不出现人物、正脸、手部或人群")
    elif person_policy == "balanced":
        constraints.append("允许手部操作、背影、远景人群或产品使用者，但不出现正面大脸和抢占主体的人物")
    else:
        constraints.append("允许普通人物入镜，但禁止对镜讲话的第二主播和占满画面的正面大脸")
    if "no_presenter_studio" in rules:
        constraints.append("不出现演播室、麦克风前主播或新闻播报员")
    if "no_large_text_watermark" in rules:
        constraints.append("不生成文字、标志、水印或字幕")
    constraints.extend(["主体避开左上角数字人区域", "底部保留字幕安全区"])
    return (
        f"为中文科技新闻口播制作一张{orientation}主体画面。内容依据：{context_text.strip()[:1200]}。"
        "使用真实可信、简洁高级的科技视觉，不要把旁白逐字画出来。"
        f"硬性要求：{'；'.join(constraints)}。"
    )[:4000]


def _person_screening_decision(metrics: dict, person_policy: str) -> dict:
    """Turn detector measurements into an explainable, policy-specific decision."""
    face_ratio = float(metrics.get("max_face_ratio") or 0)
    person_ratio = float(metrics.get("max_person_ratio") or 0)
    face_centered = bool(metrics.get("face_centered"))
    person_centered = bool(metrics.get("person_centered"))
    person_frame_hits = int(metrics.get("person_frame_hits") or 0)
    reasons: list[str] = []
    if person_policy == "strict":
        if face_ratio >= .003:
            reasons.append("严格模式检测到正面人脸")
        elif person_ratio >= .04:
            reasons.append("严格模式检测到人物")
    elif person_policy == "balanced":
        if face_centered and face_ratio >= .055:
            reasons.append("检测到居中的正面大脸")
        elif person_ratio >= .55:
            reasons.append("人物占据画面主体")
        elif person_centered and person_ratio >= .38 and person_frame_hits >= 2:
            reasons.append("人物持续居中且占比较大")
    else:
        if face_centered and face_ratio >= .12:
            reasons.append("检测到占满画面的正面大脸")
        elif person_centered and person_ratio >= .55 and person_frame_hits >= 2:
            reasons.append("人物几乎占满主体区域")
    score = 100
    score -= min(45, round(face_ratio * 260))
    score -= min(40, round(person_ratio * 75))
    if face_centered:
        score -= 8
    if person_centered and person_frame_hits >= 2:
        score -= 8
    return {
        "status": "rejected" if reasons else "passed",
        "score": max(0, score),
        "reasons": reasons,
        "metrics": metrics,
    }


def _screen_visual_candidate(path: Path, media_kind: str, rules: list[str], person_policy: str = "balanced") -> dict:
    """Best-effort face/person screening with policy-aware size and position thresholds."""
    try:
        import cv2  # type: ignore
    except Exception:
        return {"status": "needs_review", "mode": "semantic_only", "score": 50, "reasons": ["本机未安装视觉检测器"], "metrics": {}}
    frames: list[Any] = []
    if media_kind == "image":
        frame = cv2.imread(str(path))
        if frame is None:
            try:
                import numpy as np  # type: ignore
                frame = cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_COLOR)
            except Exception:
                frame = None
        if frame is not None:
            frames.append(frame)
    else:
        capture = cv2.VideoCapture(str(path))
        frame_count = max(1, int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 1))
        for ratio in (.15, .5, .85):
            capture.set(cv2.CAP_PROP_POS_FRAMES, int((frame_count - 1) * ratio))
            ok, frame = capture.read()
            if ok and frame is not None:
                frames.append(frame)
        capture.release()
    if not frames:
        return {"status": "needs_review", "mode": "local_detector", "score": 50, "reasons": ["无法抽取检测帧"], "metrics": {}}
    cascade = cv2.CascadeClassifier(str(Path(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml"))
    hog = cv2.HOGDescriptor()
    hog.setSVMDetector(cv2.HOGDescriptor_getDefaultPeopleDetector())
    face_detector_ready = not cascade.empty()
    metrics = {
        "sampled_frames": len(frames), "face_frame_hits": 0, "person_frame_hits": 0,
        "max_face_ratio": 0.0, "max_person_ratio": 0.0,
        "face_centered": False, "person_centered": False,
    }
    for frame in frames:
        height, width = frame.shape[:2]
        scale = min(1.0, 960.0 / max(height, width))
        sample = cv2.resize(frame, None, fx=scale, fy=scale) if scale < 1 else frame
        area = max(1, sample.shape[0] * sample.shape[1])
        gray = cv2.cvtColor(sample, cv2.COLOR_BGR2GRAY)
        faces = cascade.detectMultiScale(gray, scaleFactor=1.12, minNeighbors=5, minSize=(44, 44)) if face_detector_ready else []
        if len(faces):
            metrics["face_frame_hits"] += 1
        for x, y, w, h in faces:
            ratio = (w * h) / area
            metrics["max_face_ratio"] = float(max(metrics["max_face_ratio"], round(float(ratio), 4)))
            cx, cy = (x + w / 2) / sample.shape[1], (y + h / 2) / sample.shape[0]
            metrics["face_centered"] = bool(metrics["face_centered"] or (.25 <= cx <= .75 and .18 <= cy <= .78))
        people, _ = hog.detectMultiScale(sample, winStride=(8, 8), padding=(8, 8), scale=1.05)
        if len(people):
            metrics["person_frame_hits"] += 1
        for x, y, w, h in people:
            ratio = (w * h) / area
            metrics["max_person_ratio"] = float(max(metrics["max_person_ratio"], round(float(ratio), 4)))
            cx, cy = (x + w / 2) / sample.shape[1], (y + h / 2) / sample.shape[0]
            metrics["person_centered"] = bool(metrics["person_centered"] or (.22 <= cx <= .78 and .12 <= cy <= .88))
    decision = _person_screening_decision(metrics, person_policy)
    if not face_detector_ready and decision["status"] == "passed":
        decision = {
            **decision,
            "status": "needs_review",
            "reasons": ["本机缺少人脸检测模型，人物规则需要人工复核"],
        }
    return {"mode": "local_detector", "person_policy": person_policy, **decision}


def _balanced_visual_ranges(start: float, end: float, profile: str = "auto") -> list[tuple[float, float]]:
    """Return seamless, human-paced ranges without a tiny remainder slot."""
    start = _rounded_seconds(start)
    end = max(start + .04, _rounded_seconds(end, start + .04))
    duration = end - start
    rule = _visual_profile(profile)
    target = float(rule["target_seconds"])
    minimum = float(rule["min_seconds"])
    maximum = float(rule["max_seconds"])
    if duration <= maximum:
        count = 1
    else:
        count = max(1, int(math.floor(duration / target + .5)))
        while count > 1 and duration / count < minimum:
            count -= 1
        while duration / count > maximum:
            count += 1
    boundaries = [start]
    for index in range(1, count):
        boundaries.append(_rounded_seconds(start + duration * index / count))
    boundaries.append(end)
    return [(boundaries[index], boundaries[index + 1]) for index in range(count)]


def _scene_has_complete_visual(state: dict, scene: dict) -> bool:
    """Return whether the independent supporting-visual timeline is complete.

    Presenter media deliberately does not participate in this predicate.  A
    full-screen avatar may make a scene renderable, but it is not a news,
    product, environment, or information visual for batch-fill purposes.
    """
    assets = {str(item.get("id")): item for item in state.get("assets", []) if isinstance(item, dict)}
    blocks = sorted(
        (item for item in ((scene.get("visual_timeline") or {}).get("blocks") or []) if isinstance(item, dict)),
        key=lambda item: _as_number(item.get("start_seconds")),
    )
    if not blocks:
        return False
    tolerance = .012
    if abs(_as_number(blocks[0].get("start_seconds"))) > tolerance:
        return False
    if abs(_as_number(blocks[-1].get("end_seconds")) - _scene_duration(scene)) > tolerance:
        return False
    for previous, current in zip(blocks, blocks[1:]):
        if abs(_as_number(previous.get("end_seconds")) - _as_number(current.get("start_seconds"))) > tolerance:
            return False
    return all(
        bool(assets.get(str(block.get("asset_id") or ""), {}).get("path"))
        and str(block.get("status") or "ready") not in {"planned", "generating", "failed"}
        for block in blocks
    )


def _scene_has_presenter_media(state: dict, scene: dict) -> bool:
    return bool(_is_avatar_project(state) and str(_scene_presenter(scene).get("source_path") or "").strip())


def _scene_has_supporting_visual(state: dict, scene: dict) -> bool:
    if _scene_has_complete_visual(state, scene):
        return True
    asset = _selected_visual_asset(state, str(scene.get("id") or ""))
    lifecycle = (asset or {}).get("lifecycle") if isinstance((asset or {}).get("lifecycle"), dict) else {}
    return bool(asset and asset.get("path") and lifecycle.get("status") != "trashed")


def _scene_is_renderable(state: dict, scene: dict) -> bool:
    """Combine the two tracks only at the final renderability boundary."""
    presenter = _scene_presenter(scene)
    treatment = str(presenter.get("treatment") or "hidden")
    has_presenter = _scene_has_presenter_media(state, scene)
    has_supporting = _scene_has_supporting_visual(state, scene)
    if not _is_avatar_project(state):
        return has_supporting
    if treatment == "fullscreen":
        return has_presenter
    if treatment in {"pip_top_left", "custom"}:
        return has_presenter and has_supporting
    return has_supporting


def _visual_batch_scene_ids(state: dict, payload: dict) -> list[str]:
    mode = str(payload.get("selection_mode") or "custom")
    scenes = [item for item in state.get("scenes", []) if isinstance(item, dict)]
    if mode == "missing":
        return [str(scene["id"]) for scene in scenes if not _scene_has_supporting_visual(state, scene)]
    if mode == "all":
        return [str(scene["id"]) for scene in scenes]
    if mode != "custom":
        raise WorkbenchError("片段选择范围无效")
    requested = [str(value) for value in (payload.get("scene_ids") or [])]
    available = {str(scene.get("id")) for scene in scenes}
    invalid = [value for value in requested if value not in available]
    if invalid:
        raise WorkbenchError(f"未找到片段：{'、'.join(invalid)}")
    return list(dict.fromkeys(requested))


def _next_visual_block_id(existing_ids: set[str]) -> str:
    number = 1
    while f"VB-{number:03d}" in existing_ids:
        number += 1
    block_id = f"VB-{number:03d}"
    existing_ids.add(block_id)
    return block_id


def _planned_visual_blocks(
    scene: dict,
    profile: str,
    *,
    preserve_locked: bool = True,
    preserve_existing: bool = False,
    mix_strategy: str = "balanced",
    image_source: str = "web_download",
) -> list[dict]:
    duration = _rounded_seconds(_scene_duration(scene))
    existing = [dict(item) for item in ((scene.get("visual_timeline") or {}).get("blocks") or []) if isinstance(item, dict)]
    preserved = sorted((
        item for item in existing
        if item.get("asset_id") and (
            preserve_existing or (preserve_locked and item.get("locked"))
        )
    ), key=lambda item: _as_number(item.get("start_seconds")))
    occupied_ids = {str(item.get("id")) for item in preserved if re.fullmatch(r"VB-\d{3}", str(item.get("id") or ""))}
    planned: list[dict] = []
    cursor = 0.0
    gaps: list[tuple[float, float]] = []
    for item in preserved:
        start = max(cursor, min(duration, _rounded_seconds(item.get("start_seconds"))))
        end = max(start, min(duration, _rounded_seconds(item.get("end_seconds"))))
        if start > cursor + .012:
            gaps.append((cursor, start))
        item.update({"start_seconds": start, "end_seconds": end, "status": "ready", "locked": bool(item.get("locked")), "error": ""})
        planned.append(item)
        cursor = max(cursor, end)
    if cursor < duration - .012:
        gaps.append((cursor, duration))
    if not preserved:
        gaps = [(0.0, duration)]
    for gap_start, gap_end in gaps:
        ranges = _balanced_visual_ranges(gap_start, gap_end, profile)
        for range_index, (start, end) in enumerate(ranges):
            media_kind = _visual_media_kind(profile, range_index, len(ranges), mix_strategy)
            source_mode = image_source if media_kind == "image" else "web_download"
            planned.append({
                "id": _next_visual_block_id(occupied_ids),
                "story_id": str(scene.get("story_id") or ""),
                "start_seconds": start,
                "end_seconds": end,
                "source_mode": source_mode,
                "media_kind": media_kind,
                "asset_id": None,
                "usage_id": None,
                "label": "等待自动补全",
                "status": "planned",
                "locked": False,
                "query": "",
                "context_text": "",
                "attempt": 0,
                "error": "",
            })
    planned.sort(key=lambda item: _as_number(item.get("start_seconds")))
    return planned


def _visual_route_for_block(block: dict) -> str:
    source_mode = str(block.get("source_mode") or "web_download")
    media_kind = str(block.get("media_kind") or "video")
    if source_mode == "hyperframes":
        return "hyperframes"
    if source_mode == "openai_image":
        return "ai_image"
    return "stock_image" if media_kind == "image" else "stock_video"


def _apply_visual_route(block: dict, route: str) -> None:
    if route not in VISUAL_BATCH_ROUTES:
        raise WorkbenchError(f"不支持的画面生产方式：{route}")
    mapping = {
        "stock_video": ("video", "web_download"),
        "stock_image": ("image", "web_download"),
        "ai_image": ("image", "openai_image"),
        "hyperframes": ("video", "hyperframes"),
    }
    media_kind, source_mode = mapping[route]
    block.update({"route": route, "media_kind": media_kind, "source_mode": source_mode})


def _visual_slot_texts(project_dir: Path, state: dict, scene: dict, blocks: list[dict]) -> dict[str, str]:
    """Map each server-owned visual interval to the narration phrases it covers."""
    text = _scene_text(project_dir, state, scene)
    duration = max(.1, _scene_duration(scene))
    cues = _subtitle_cues(scene, text, duration_seconds=duration, relative_to_scene=True)
    result: dict[str, str] = {}
    for block in blocks:
        block_id = str(block.get("id") or "")
        start = _as_number(block.get("start_seconds"))
        end = max(start, _as_number(block.get("end_seconds")))
        phrases = [
            str(cue.get("text") or "").strip()
            for cue in cues
            if _as_number(cue.get("end_seconds")) > start + .01
            and _as_number(cue.get("start_seconds")) < end - .01
            and str(cue.get("text") or "").strip()
        ]
        result[block_id] = "".join(dict.fromkeys(phrases)).strip() or text
    return result


def _rule_visual_recipe(text: str) -> str:
    value = str(text or "")
    if re.search(r"\d+(?:\.\d+)?\s*(?:%|倍|亿|万|元|项|台|家|年|月|日)", value):
        return "single_metric"
    if any(token in value for token in ("先", "然后", "随后", "最后", "流程", "步骤", "阶段")):
        return "process"
    if any(token in value for token in ("相比", "对比", "从前", "如今", "而不是", "但", "却")):
        return "comparison"
    if value.rstrip().endswith(("？", "?")):
        return "closing_question"
    if any(token in value for token in ("关系", "连接", "账号", "粉丝", "传播", "商业价值", "生态")):
        return "relationship_map"
    if any(token in value for token in ("显示", "证明", "意味着", "重点", "关键", "原因")):
        return "quote_evidence"
    return "headline_statement"


def _rule_graphic_copy(text: str, recipe: str) -> dict:
    """Create an explicitly labelled review draft when the user skips AI."""
    cleaned = re.sub(r"^(?:欢迎收听[^。！？]*[。！？]|今天我们(?:关注|来看)[：:]?)", "", str(text or "").strip())
    clauses = [
        re.sub(r"\s+", "", value).strip("，。！？；：、 ")
        for value in re.split(r"[，。！？；：、]+", cleaned)
        if value.strip()
    ]
    headline = (clauses[0] if clauses else "本格核心判断")[:22]
    nodes = list(dict.fromkeys(value[:12] for value in clauses[1:] if value and value[:12] != headline))[:4]
    for fallback in ("核心对象", "关键变化", "现实影响", "后续价值"):
        if len(nodes) >= 4:
            break
        if fallback not in nodes:
            nodes.append(fallback)
    return {
        "scene_goal": (cleaned or headline)[:48],
        "headline": headline,
        "supporting_statement": (clauses[1] if len(clauses) > 1 else "请在生成前核对本格画面表达")[:44],
        "center_label": headline[:12],
        "nodes": nodes,
    }


def _resolved_layout_variant(recipe: str, requested: object = None, *, style_pack_id: str = STYLE_PACK_ID) -> dict:
    """Resolve batch-facing layout metadata from the frozen style-pack.

    A workbench plan never trusts a browser or model supplied identifier.  The
    fallback is deliberately the former single-layout renderer, so existing
    projects stay visually compatible when their plan lacks this field.
    """
    try:
        return resolve_layout_variant(recipe, requested, style_pack_id=style_pack_id)
    except StylePackError as exc:
        raise WorkbenchError(f"HyperFrames 版式不可用：{exc}") from exc


def _apply_layout_variant(block: dict, recipe: str | None = None, requested: object = None) -> dict:
    resolved = _resolved_layout_variant(
        str(recipe or block.get("scene_recipe") or "relationship_map"),
        block.get("layout_variant") if requested is None else requested,
    )
    block["scene_recipe"] = resolved["recipe_id"]
    block["layout_variant"] = resolved["id"]
    block["motion_variant"] = resolved["motion_variant"]
    return resolved


def _rebalance_hyperframes_layout_variants(items: list[dict]) -> dict:
    """Spread declared HyperFrames layouts without changing semantic meaning.

    This is intentionally a small deterministic scheduler. It only switches
    among alternatives of the already-selected semantic recipe; it never
    turns a comparison into a relationship map merely to produce novelty. It
    first removes immediate repeats, then spreads an over-used variant across
    the full planned sequence. A user-locked selection is never rewritten.
    """
    variants = layout_variant_catalog()
    blocks = _planned_visual_entries(items)
    usage: dict[str, int] = {}
    adjusted = 0
    normalized = 0
    repeated = 0
    previous: tuple[str, str] | None = None
    for block in blocks:
        if str(block.get("route") or _visual_route_for_block(block)) != "hyperframes":
            # A real clip between two motion cards is already a perceptible
            # rhythm break for the first, immediate-repeat pass.
            previous = None
            continue
        recipe = str(block.get("scene_recipe") or "relationship_map")
        prior_variant = str(block.get("layout_variant") or "")
        resolved = _apply_layout_variant(block, recipe, prior_variant)
        if prior_variant != resolved["id"]:
            normalized += 1
        fingerprint = (resolved["recipe_id"], resolved["id"])
        if previous == fingerprint and not bool(block.get("layout_variant_locked")):
            alternatives = [
                item for item in variants.get(resolved["recipe_id"], [])
                if item.get("id") and (resolved["recipe_id"], str(item["id"])) != previous
            ]
            if alternatives:
                alternatives.sort(key=lambda item: (usage.get(f"{resolved['recipe_id']}:{item['id']}", 0), str(item["id"])))
                replacement = alternatives[0]
                resolved = _apply_layout_variant(block, resolved["recipe_id"], replacement["id"])
                fingerprint = (resolved["recipe_id"], resolved["id"])
                block["reason"] = (str(block.get("reason") or "") + "；为避免相邻动态图版式重复，已保留信息结构并切换同配方版式")[:240]
                block["decision_source"] = "layout_diversity_rebalanced"
                adjusted += 1
            else:
                repeated += 1
        usage[f"{fingerprint[0]}:{fingerprint[1]}"] = usage.get(f"{fingerprint[0]}:{fingerprint[1]}", 0) + 1
        previous = fingerprint

    # A plan can still look monotonous when repeated structures are separated
    # by stock clips. Balance each semantic recipe across its declared
    # variants, while respecting manual locks and adjacent-card readability.
    by_recipe: dict[str, list[tuple[int, dict]]] = {}
    for index, block in enumerate(blocks):
        if str(block.get("route") or _visual_route_for_block(block)) != "hyperframes":
            continue
        recipe = str(block.get("scene_recipe") or "relationship_map")
        by_recipe.setdefault(recipe, []).append((index, block))
    for recipe, recipe_blocks in by_recipe.items():
        candidates = [item for item in variants.get(recipe, []) if str(item.get("id") or "")]
        if len(candidates) < 2:
            continue
        per_variant_cap = math.ceil(len(recipe_blocks) / len(candidates))
        recipe_usage = {
            str(item["id"]): sum(
                1 for _, block in recipe_blocks
                if str(block.get("layout_variant") or "") == str(item["id"])
            )
            for item in candidates
        }
        for index, block in recipe_blocks:
            current = str(block.get("layout_variant") or "")
            if bool(block.get("layout_variant_locked")) or recipe_usage.get(current, 0) <= per_variant_cap:
                continue
            adjacent = {
                str(blocks[neighbor].get("layout_variant") or "")
                for neighbor in (index - 1, index + 1)
                if 0 <= neighbor < len(blocks)
                and str(blocks[neighbor].get("route") or _visual_route_for_block(blocks[neighbor])) == "hyperframes"
                and str(blocks[neighbor].get("scene_recipe") or "") == recipe
            }
            alternatives = [
                item for item in candidates
                if str(item["id"]) != current
                and recipe_usage.get(str(item["id"]), 0) < per_variant_cap
                and str(item["id"]) not in adjacent
            ]
            if not alternatives:
                continue
            alternatives.sort(key=lambda item: (recipe_usage.get(str(item["id"]), 0), str(item["id"])))
            replacement = alternatives[0]
            _apply_layout_variant(block, recipe, replacement["id"])
            recipe_usage[current] = max(0, recipe_usage.get(current, 0) - 1)
            recipe_usage[str(replacement["id"])] = recipe_usage.get(str(replacement["id"]), 0) + 1
            block["reason"] = (str(block.get("reason") or "") + "；为避免本期动态图版式过度复用，已保留信息结构并平衡同配方版式")[:240]
            block["decision_source"] = "layout_diversity_rebalanced"
            adjusted += 1
    usage = {}
    for block in blocks:
        if str(block.get("route") or _visual_route_for_block(block)) != "hyperframes":
            continue
        fingerprint = f"{block.get('scene_recipe') or 'relationship_map'}:{block.get('layout_variant') or ''}"
        usage[fingerprint] = usage.get(fingerprint, 0) + 1
    return {
        "layout_adjusted_slots": adjusted,
        "layout_normalized_slots": normalized,
        "layout_repeat_count": repeated,
        "layout_counts": usage,
    }


def _visual_batch_ai_context(project_dir: Path, state: dict, items: list[dict], policy: dict) -> dict:
    scenes: list[dict] = []
    for item in items:
        scene = _find(state.get("scenes", []), str(item["scene_id"]), "场景")
        planned = [block for block in item.get("blocks", []) if block.get("status") == "planned"]
        slot_texts = _visual_slot_texts(project_dir, state, scene, planned)
        slots = [
            {
                "block_id": block["id"],
                "start_seconds": block["start_seconds"],
                "end_seconds": block["end_seconds"],
                "duration_seconds": _rounded_seconds(_as_number(block["end_seconds"]) - _as_number(block["start_seconds"])),
                "shot_role": block.get("role_label") or "素材镜头",
                "slot_text": slot_texts.get(str(block.get("id") or ""), ""),
            }
            for block in planned
        ]
        if slots:
            scenes.append({
                "scene_id": scene["id"],
                "story_id": str(scene.get("story_id") or ""),
                "title": scene.get("title") or "",
                "transcript": scene.get("description") or scene.get("shot_intent") or "",
                "surrounding_context": _scene_surrounding_context(state, scene),
                "slots": slots,
            })
    width, height = _render_dimensions(project_dir, state)
    return {
        "task": "visual_route_planning",
        "aspect": "9:16" if height > width else "16:9",
        "has_presenter_overlay": _is_avatar_project(state),
        "caption_owner": "OpenMontage 独立字幕层",
        "headline_owner": "OpenMontage 按 story_id 统一的小标题叠加层；HyperFrames 不得渲染右上角新闻标题",
        "hyperframes_layout_variants": layout_variant_catalog(),
        "preferences": {
            "mix_strategy": policy["mix_strategy"],
            "image_source": policy["image_source"],
            "person_policy": policy["person_policy"],
            "allowed_routes": list(policy.get("allowed_ai_routes") or sorted(VISUAL_BATCH_AI_DEFAULT_ROUTES)),
            "primary_image_policy": policy.get("primary_image_policy") or "manual_only",
            "duration_balance": deepcopy(policy.get("duration_balance") or {}),
            "fallback_policy": "只建议，不自动执行",
        },
        "scenes": scenes,
    }


def _plan_visual_routes_batched(context: dict, *, max_slots_per_request: int = 6) -> dict:
    """Plan a large episode in small, deterministic AI requests.

    A whole episode can contain more than twenty visual slots. Sending all of
    them through one chat-completions request regularly reaches provider
    timeouts even though the same model passes a connection test. Keep scene
    boundaries intact and process at most six slots per request; this also
    makes one malformed response affect only a small, clearly identified
    batch.
    """
    scenes = [item for item in context.get("scenes", []) if isinstance(item, dict)]
    common = {key: deepcopy(value) for key, value in context.items() if key != "scenes"}
    chunks: list[dict] = []
    current: list[dict] = []
    current_slots = 0
    for scene in scenes:
        scene_slots = max(1, len([slot for slot in scene.get("slots", []) if isinstance(slot, dict)]))
        if current and current_slots + scene_slots > max_slots_per_request:
            chunks.append({**deepcopy(common), "scenes": current})
            current = []
            current_slots = 0
        current.append(scene)
        current_slots += scene_slots
    if current:
        chunks.append({**deepcopy(common), "scenes": current})
    if not chunks:
        raise TextAIError("没有可供 AI 规划的画面槽位")

    plans: list[dict] = []
    repaired_slots = 0
    fallback_slots = 0
    for index, chunk in enumerate(chunks, 1):
        try:
            plan = plan_visual_routes(chunk, allow_missing=True)
        except TextAIError as exc:
            raise TextAIError(f"第 {index}/{len(chunks)} 组片段识别失败：{exc}") from exc
        missing = {
            (str(item.get("scene_id") or ""), str(item.get("block_id") or ""))
            for item in plan.get("missing", []) if isinstance(item, dict)
        }
        if missing:
            repair_scenes: list[dict] = []
            for scene in chunk.get("scenes", []):
                scene_id = str(scene.get("scene_id") or "")
                repair_slots = [
                    deepcopy(slot) for slot in scene.get("slots", [])
                    if (scene_id, str(slot.get("block_id") or "")) in missing
                ]
                if repair_slots:
                    repair_scene = deepcopy(scene)
                    repair_scene["slots"] = repair_slots
                    repair_scenes.append(repair_scene)
            repair_context = {**deepcopy(common), "task": "visual_route_missing_slot_repair", "scenes": repair_scenes}
            try:
                repair = plan_visual_routes(repair_context, allow_missing=True)
            except TextAIError:
                repair = {"blocks": [], "missing": [
                    {"scene_id": scene_id, "block_id": block_id} for scene_id, block_id in sorted(missing)
                ]}
            repair_blocks = [block for block in repair.get("blocks", []) if isinstance(block, dict)]
            repaired_keys = {(str(block.get("scene_id") or ""), str(block.get("block_id") or "")) for block in repair_blocks}
            plan.setdefault("blocks", []).extend(repair_blocks)
            repaired_slots += len(repaired_keys & missing)
            still_missing = missing - repaired_keys
            if still_missing:
                slot_lookup = {
                    (str(scene.get("scene_id") or ""), str(slot.get("block_id") or "")): slot
                    for scene in repair_scenes for slot in scene.get("slots", [])
                }
                for scene_id, block_id in sorted(still_missing):
                    slot = slot_lookup[(scene_id, block_id)]
                    slot_text = str(slot.get("slot_text") or "").strip()
                    recipe = _rule_visual_recipe(slot_text)
                    plan["blocks"].append({
                        "scene_id": scene_id,
                        "block_id": block_id,
                        "route": "hyperframes" if recipe != "headline_statement" else "stock_video",
                        "visual_intent": (slot_text or "根据当前台词补充主体画面")[:80],
                        "reason": "AI 漏项后由确定性规则自动补齐，避免中断整批规划",
                        "confidence": 0.45,
                        "search_query": "",
                    "scene_recipe": recipe,
                    "layout_variant": _resolved_layout_variant(recipe)["id"],
                    "motion_variant": _resolved_layout_variant(recipe)["motion_variant"],
                        "graphic_copy": _rule_graphic_copy(slot_text, recipe),
                        "fallback_route": "stock_video",
                        "start_seconds": slot.get("start_seconds"),
                        "end_seconds": slot.get("end_seconds"),
                    })
                fallback_slots += len(still_missing)
            plan["missing"] = []
        plans.append(plan)
    models = list(dict.fromkeys(str(plan.get("model") or "") for plan in plans if plan.get("model")))
    summaries = [str(plan.get("summary") or "").strip() for plan in plans if str(plan.get("summary") or "").strip()]
    blocks = [deepcopy(block) for plan in plans for block in plan.get("blocks", []) if isinstance(block, dict)]
    fingerprint_source = json.dumps(blocks, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return {
        "summary": "；".join(summaries)[:500] or "已分批完成画面智能规划",
        "blocks": blocks,
        "model": " + ".join(models) or None,
        "generated_at": _now(),
        "fingerprint": f"VAI-{hashlib.sha256(fingerprint_source).hexdigest()[:16]}",
        "batch_count": len(chunks),
        "repaired_slots": repaired_slots,
        "fallback_slots": fallback_slots,
    }


def _visual_batch_plan_digest(items: list[dict]) -> str:
    compact = [
        {
            "scene_id": item.get("scene_id"),
            "blocks": [
                {
                    "id": block.get("id"), "start": block.get("start_seconds"), "end": block.get("end_seconds"),
                    "route": block.get("route"), "query": block.get("query"), "recipe": block.get("scene_recipe"),
                    "graphic_copy": block.get("graphic_copy"),
                }
                for block in item.get("blocks", []) if block.get("status") == "planned"
            ],
        }
        for item in items
    ]
    encoded = json.dumps(compact, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return f"VBP-{hashlib.sha256(encoded).hexdigest()[:16]}"


def _planned_visual_entries(items: list[dict]) -> list[dict]:
    return [
        block
        for item in items
        for block in item.get("blocks", [])
        if isinstance(block, dict) and block.get("status") == "planned"
    ]


def _visual_block_duration(block: dict) -> float:
    return max(0.0, _as_number(block.get("end_seconds")) - _as_number(block.get("start_seconds")))


def _visual_route_flip_cost(block: dict, target_route: str) -> tuple[float, float]:
    """Prefer low-confidence, semantically compatible slots when balancing.

    AI still owns the local editorial decision.  This score only decides which
    low-risk slots may move when the whole episode is outside the selected
    duration envelope.
    """
    recipe = str(block.get("scene_recipe") or "headline_statement")
    abstract = {"relationship_map", "single_metric", "comparison", "process"}
    concrete = {"headline_statement", "quote_evidence", "closing_question"}
    if target_route == "stock_video":
        semantic_cost = 0.0 if recipe in concrete else 1.0 if recipe in abstract else .5
    else:
        semantic_cost = 0.0 if recipe in abstract else 1.0 if recipe in concrete else .5
    return semantic_cost, max(0.0, min(1.0, _as_number(block.get("confidence"), .5)))


def _visual_duration_metrics(items: list[dict], policy: dict) -> dict:
    routes = {route: 0.0 for route in VISUAL_BATCH_ROUTES}
    chronological: list[str] = []
    for block in _planned_visual_entries(items):
        route = str(block.get("route") or _visual_route_for_block(block))
        if route in routes:
            routes[route] += _visual_block_duration(block)
            chronological.append(route)
    routes = {key: _rounded_seconds(value) for key, value in routes.items()}
    total = _rounded_seconds(sum(routes.values()))
    shares = {
        key: round((value / total), 4) if total > 0 else 0.0
        for key, value in routes.items()
    }
    maximum_streak = 0
    current_route = ""
    current_streak = 0
    for route in chronological:
        if route == current_route:
            current_streak += 1
        else:
            current_route = route
            current_streak = 1
        maximum_streak = max(maximum_streak, current_streak)
    envelope = policy.get("duration_balance") or VISUAL_BATCH_DURATION_BALANCE["balanced"]
    stock_share = shares["stock_video"]
    image_share = shares["stock_image"] + shares["ai_image"]
    within = (
        not chronological
        or len(chronological) < 3
        or float(envelope["stock_video_min"]) - .001 <= stock_share <= float(envelope["stock_video_max"]) + .001
    )
    warnings: list[str] = []
    if image_share > .001 and policy.get("planning_mode") == "ai_director":
        warnings.append("AI 默认规划中仍有图片路线，请确认是否为人工覆盖")
    if not within:
        warnings.append("网络视频与动态页面的时长占比超出当前预设区间")
    if maximum_streak > 3:
        warnings.append(f"检测到同类来源连续 {maximum_streak} 格，请审核节奏")
    return {
        "total_planned_duration_seconds": total,
        "route_duration_seconds": routes,
        "duration_shares": shares,
        "stock_video_duration_seconds": routes["stock_video"],
        "hyperframes_duration_seconds": routes["hyperframes"],
        "primary_image_duration_seconds": _rounded_seconds(routes["stock_image"] + routes["ai_image"]),
        "max_route_streak": maximum_streak,
        "balance_status": "warning" if warnings else "balanced",
        "balance_warning": "；".join(warnings),
    }


def _rebalance_ai_visual_routes(items: list[dict], policy: dict) -> dict:
    """Apply an episode-level duration envelope after per-slot AI decisions."""
    if policy.get("planning_mode") != "ai_director":
        return {"adjusted_slots": 0, "normalized_image_slots": 0, **_visual_duration_metrics(items, policy)}
    blocks = _planned_visual_entries(items)
    adjusted = 0
    normalized_images = 0
    for block in blocks:
        route = str(block.get("route") or _visual_route_for_block(block))
        if route not in {"stock_image", "ai_image"}:
            fallback = str(block.get("fallback_route") or "")
            if fallback not in VISUAL_BATCH_AI_DEFAULT_ROUTES or fallback == route:
                block["fallback_route"] = "hyperframes" if route == "stock_video" else "stock_video"
            continue
        recipe = str(block.get("scene_recipe") or "headline_statement")
        replacement = "hyperframes" if recipe in {"relationship_map", "single_metric", "comparison", "process"} else "stock_video"
        _apply_visual_route(block, replacement)
        if replacement == "hyperframes" and (
            not isinstance(block.get("graphic_copy"), dict)
            or not str((block.get("graphic_copy") or {}).get("headline") or "").strip()
        ):
            block["graphic_copy"] = _rule_graphic_copy(str(block.get("slot_text") or ""), recipe)
        block["reason"] = (str(block.get("reason") or "") + "；默认主体画面不使用静态图片，已转为视频或动态页面")[:240]
        block["decision_source"] = "ai_director_rebalanced"
        block["fallback_route"] = "stock_video" if replacement == "hyperframes" else "hyperframes"
        normalized_images += 1
        adjusted += 1

    if len(blocks) >= 3:
        envelope = policy.get("duration_balance") or VISUAL_BATCH_DURATION_BALANCE["balanced"]
        total = sum(_visual_block_duration(block) for block in blocks)

        def stock_share() -> float:
            if total <= 0:
                return 0.0
            return sum(
                _visual_block_duration(block)
                for block in blocks
                if str(block.get("route") or _visual_route_for_block(block)) == "stock_video"
            ) / total

        if stock_share() < float(envelope["stock_video_min"]):
            candidates = sorted(
                (block for block in blocks if str(block.get("route")) == "hyperframes"),
                key=lambda block: _visual_route_flip_cost(block, "stock_video"),
            )
            for block in candidates:
                _apply_visual_route(block, "stock_video")
                block["reason"] = (str(block.get("reason") or "") + "；全片动态图时长过高，按低风险顺序调整为实拍视频")[:240]
                block["decision_source"] = "ai_director_rebalanced"
                block["fallback_route"] = "hyperframes"
                adjusted += 1
                if stock_share() >= float(envelope["stock_video_min"]):
                    break
        elif stock_share() > float(envelope["stock_video_max"]):
            candidates = sorted(
                (block for block in blocks if str(block.get("route")) == "stock_video"),
                key=lambda block: _visual_route_flip_cost(block, "hyperframes"),
            )
            for block in candidates:
                _apply_visual_route(block, "hyperframes")
                if not isinstance(block.get("graphic_copy"), dict) or not block["graphic_copy"].get("headline"):
                    recipe = str(block.get("scene_recipe") or _rule_visual_recipe(block.get("slot_text") or ""))
                    block["scene_recipe"] = recipe
                    block["graphic_copy"] = _rule_graphic_copy(str(block.get("slot_text") or ""), recipe)
                block["reason"] = (str(block.get("reason") or "") + "；全片实拍时长过高，按低风险顺序调整为动态解释")[:240]
                block["decision_source"] = "ai_director_rebalanced"
                block["fallback_route"] = "stock_video"
                adjusted += 1
                if stock_share() <= float(envelope["stock_video_max"]):
                    break

    return {
        "adjusted_slots": adjusted,
        "normalized_image_slots": normalized_images,
        **_visual_duration_metrics(items, policy),
    }


def _visual_batch_counts(items: list[dict]) -> dict:
    routes = {route: 0 for route in VISUAL_BATCH_ROUTES}
    for item in items:
        for block in item.get("blocks", []):
            if block.get("status") != "planned":
                continue
            route = str(block.get("route") or _visual_route_for_block(block))
            if route in routes:
                routes[route] += 1
    return {
        "route_counts": routes,
        "video_slots": routes["stock_video"] + routes["hyperframes"],
        "image_slots": routes["stock_image"] + routes["ai_image"],
        "ai_image_slots": routes["ai_image"],
        "hyperframes_slots": routes["hyperframes"],
    }


def preview_visual_batch_plan(project_dir: Path, payload: dict) -> dict:
    """Build a reviewable production contract before any media generation."""
    state = read_workbench(project_dir)
    profile = str(payload.get("profile") or "auto")
    rule = _visual_profile(profile)
    policy = _visual_batch_policy(payload)
    search_strategy = _resolved_stock_search_strategy(state, policy)
    policy["search_strategy"] = search_strategy
    scene_ids = _visual_batch_scene_ids(state, payload)
    if not scene_ids:
        raise WorkbenchError("当前选择范围没有需要处理的片段")
    items: list[dict] = []
    total_slots = 0
    for scene_id in scene_ids:
        scene = _find(state["scenes"], scene_id, "场景")
        blocks = _planned_visual_blocks(
            scene,
            profile,
            preserve_locked=True,
            preserve_existing=policy["operation_mode"] == "fill_missing",
            mix_strategy=policy["mix_strategy"],
            image_source=policy["image_source"],
        )
        queued = [block for block in blocks if block.get("status") == "planned"]
        context = _scene_surrounding_context(state, scene)
        slot_texts = _visual_slot_texts(project_dir, state, scene, queued)
        for slot_index, block in enumerate(queued, 1):
            search_plan = _stock_search_plan_for_block(
                scene,
                surrounding_context=context,
                slot_index=slot_index,
                block_id=str(block["id"]),
                strategy=search_strategy,
                rules=policy["content_rules"],
                person_policy=policy["person_policy"],
            )
            block.update(search_plan)
            _apply_visual_route(block, _visual_route_for_block(block))
            slot_text = slot_texts.get(str(block.get("id") or ""), "")
            recipe = _rule_visual_recipe(slot_text)
            block.update({
                "slot_text": slot_text,
                "visual_intent": search_plan["topic"],
                "reason": "按既定节奏与媒体偏好生成的规则混合建议",
                "confidence": 1.0,
                "fallback_route": "",
                "scene_recipe": recipe,
                "graphic_copy": _rule_graphic_copy(slot_text, recipe),
                "decision_source": "rule_mix",
            })
        total_slots += len(queued)
        items.append({
            "scene_id": scene_id,
            "order": scene.get("order"),
            "title": scene.get("title"),
            "duration_seconds": _rounded_seconds(_scene_duration(scene)),
            # ``has_visual`` is retained for older clients, but now has the
            # single, documented meaning of independent supporting content.
            "has_visual": _scene_has_supporting_visual(state, scene),
            "has_supporting_visual": _scene_has_supporting_visual(state, scene),
            "has_presenter_media": _scene_has_presenter_media(state, scene),
            "is_renderable": _scene_is_renderable(state, scene),
            "locked_slots": sum(1 for block in blocks if block.get("locked")),
            "preserved_slots": len(blocks) - len(queued),
            "slot_count": len(blocks),
            "queued_slots": len(queued),
            "blocks": blocks,
        })
    ai_plan: dict = {}
    if policy["planning_mode"] == "ai_director":
        if payload.get("ai_planning_confirmed") is not True:
            raise WorkbenchError("AI 智能规划会调用已配置的文本模型，请确认后再生成规划")
        if not read_text_ai_config().get("configured"):
            raise WorkbenchError("尚未配置 AI 文本模型，请先打开右上角“AI 配置”")
        ai_context = _visual_batch_ai_context(project_dir, state, items, policy)
        try:
            ai_plan = _plan_visual_routes_batched(ai_context)
        except TextAIError as exc:
            raise WorkbenchError(f"AI 画面规划失败：{exc}") from exc
        decisions = {(str(row["scene_id"]), str(row["block_id"])): row for row in ai_plan.get("blocks", [])}
        slot_text_map = {
            (str(scene_row.get("scene_id") or ""), str(slot.get("block_id") or "")): str(slot.get("slot_text") or "")
            for scene_row in ai_context.get("scenes", []) if isinstance(scene_row, dict)
            for slot in scene_row.get("slots", []) if isinstance(slot, dict)
        }
        for item in items:
            for block in item.get("blocks", []):
                if block.get("status") != "planned":
                    continue
                decision = decisions.get((str(item["scene_id"]), str(block["id"])))
                if not decision:
                    raise WorkbenchError(f"AI 画面规划缺少 {item['scene_id']}/{block['id']}")
                _apply_visual_route(block, str(decision["route"]))
                block.update({
                    "visual_intent": decision.get("visual_intent") or block.get("visual_intent"),
                    "reason": decision.get("reason") or "AI 根据当前台词与前后文推荐",
                    "confidence": decision.get("confidence"),
                    "fallback_route": decision.get("fallback_route") or "",
                    "scene_recipe": decision.get("scene_recipe") or "relationship_map",
                    "layout_variant": decision.get("layout_variant") or "",
                    "motion_variant": decision.get("motion_variant") or "",
                    "graphic_copy": deepcopy(decision.get("graphic_copy") or {}),
                    "slot_text": slot_text_map.get((str(item["scene_id"]), str(block["id"])), str(block.get("slot_text") or "")),
                    "decision_source": "ai_director",
                })
                if block["route"] == "hyperframes":
                    _apply_layout_variant(block)
                if decision.get("search_query") and block["route"] in {"stock_video", "stock_image"}:
                    block["query"] = decision["search_query"]
                    if block.get("query_ladder"):
                        block["query_ladder"][0] = {"level": "AI 语义检索", "query": decision["search_query"]}
    balance = _rebalance_ai_visual_routes(items, policy)
    layout_balance = _rebalance_hyperframes_layout_variants(items)
    counts = _visual_batch_counts(items)
    result = {
        "status": "planned", "source_mode": "mixed", "profile": rule, "policy": policy,
        "scene_ids": scene_ids, "scene_count": len(scene_ids), "total_slots": total_slots,
        **counts, **balance, **layout_balance,
        "search_strategy": search_strategy, "items": items,
        "planner": {
            "mode": policy["planning_mode"],
            "model": ai_plan.get("model") if ai_plan else None,
            "generated_at": ai_plan.get("generated_at") if ai_plan else _now(),
            "fingerprint": ai_plan.get("fingerprint") if ai_plan else None,
            "summary": ai_plan.get("summary") if ai_plan else "规则混合计划；未调用 AI",
            "batch_count": int(_as_number(ai_plan.get("batch_count"))) if ai_plan else 0,
            "repaired_slots": int(_as_number(ai_plan.get("repaired_slots"))) if ai_plan else 0,
            "fallback_slots": int(_as_number(ai_plan.get("fallback_slots"))) if ai_plan else 0,
            "balance_adjusted_slots": int(balance.get("adjusted_slots") or 0),
            "normalized_image_slots": int(balance.get("normalized_image_slots") or 0),
            "layout_adjusted_slots": int(layout_balance.get("layout_adjusted_slots") or 0),
            "layout_normalized_slots": int(layout_balance.get("layout_normalized_slots") or 0),
            "layout_repeat_count": int(layout_balance.get("layout_repeat_count") or 0),
        },
    }
    result["plan_id"] = _visual_batch_plan_digest(items)
    return result


def _copy_presenter_layout_to_scenes(state: dict, source_scene_id: str, target_scene_ids: list[str]) -> int:
    if not source_scene_id:
        return 0
    source_scene = _find(state.get("scenes", []), source_scene_id, "版式来源片段")
    source = _scene_presenter(source_scene)
    changed = 0
    for scene_id in target_scene_ids:
        if scene_id == source_scene_id:
            continue
        scene = _find(state.get("scenes", []), scene_id, "场景")
        target = _scene_presenter(scene)
        target["treatment"] = source.get("treatment")
        target["layout_template_id"] = source.get("layout_template_id")
        target["layout_override"] = deepcopy(source.get("layout_override"))
        target["crop_bottom"] = _normalized_presenter_crop_bottom(source.get("crop_bottom"))
        target["shape"] = _normalized_presenter_shape(source.get("shape"))
        target["face_crop"] = deepcopy(source.get("face_crop")) if isinstance(source.get("face_crop"), dict) else None
        _invalidate_scene_review_preview(scene, "已批量复用数字人位置大小，请刷新本段审核预览")
        scene["review_status"] = "needs_adjustment"
        changed += 1
    return changed


def apply_presenter_layout_to_selected_scenes(project_dir: Path, payload: dict) -> dict:
    """Copy one approved presenter treatment without starting a media job.

    This deliberately stays separate from visual-batch generation: a reviewer
    can reuse placement and source cleanup across selected scenes without
    replanning, downloading, or replacing any visual timeline blocks.
    """
    state = _load_for_write(project_dir)
    if not _is_avatar_project(state):
        raise WorkbenchError("只有数字人口播项目可以批量同步数字人样式")
    if _automation(state)["preview_sync"].get("status") in {"queued", "generating"}:
        raise WorkbenchError("审核预览正在同步，请等待完成后再调整数字人样式")
    source_scene_id = str(payload.get("source_scene_id") or "")
    source_scene = _find(state.get("scenes", []), source_scene_id, "数字人样式来源片段")
    requested = [str(value) for value in (payload.get("target_scene_ids") or []) if str(value)]
    target_scene_ids = list(dict.fromkeys(scene_id for scene_id in requested if scene_id != source_scene_id))
    if not target_scene_ids:
        raise WorkbenchError("请至少选择一个需要同步的目标片段")
    for scene_id in target_scene_ids:
        _find(state.get("scenes", []), scene_id, "待同步片段")
    changed = _copy_presenter_layout_to_scenes(state, source_scene_id, target_scene_ids)
    if not changed:
        raise WorkbenchError("没有可同步的数字人片段")
    _mark_render_needs_refresh(state, "已批量同步数字人样式")
    source_name = str(source_scene.get("title") or source_scene_id)
    _decision(state, "presenter_layout_batch_copy", "数字人样式批量同步", source_name, f"已复制到 {changed} 个片段；不改动角色、原片、台词、声音或主体画面")
    _activity(state, "presenter_layout_batch_copied", f"已从“{source_name}”同步数字人样式到 {changed} 个片段，正在刷新审核预览", source_scene_id=source_scene_id, scene_ids=target_scene_ids)
    return _save(project_dir, state)


def _validated_reviewed_visual_plan(
    state: dict,
    payload: dict,
    policy: dict,
    profile: str,
    scene_ids: list[str],
) -> tuple[dict[str, list[dict]], dict]:
    """Validate browser-edited routing while keeping timing server-owned."""
    reviewed = payload.get("reviewed_plan")
    reviewed_items = reviewed.get("items") if isinstance(reviewed, dict) else None
    submitted: dict[str, dict[str, dict]] = {}
    if isinstance(reviewed_items, list):
        for item in reviewed_items:
            if not isinstance(item, dict):
                continue
            scene_id = str(item.get("scene_id") or "")
            submitted[scene_id] = {
                str(block.get("id")): block
                for block in item.get("blocks") or []
                if isinstance(block, dict) and block.get("status") == "planned" and block.get("id")
            }
    if policy["planning_mode"] == "ai_director" and not submitted:
        raise WorkbenchError("AI 智能导演计划尚未提交，请先生成并核对画面规划")

    resolved: dict[str, list[dict]] = {}
    for scene_id in scene_ids:
        scene = _find(state.get("scenes", []), scene_id, "场景")
        baseline = _planned_visual_blocks(
            scene, profile, preserve_locked=True,
            preserve_existing=policy["operation_mode"] == "fill_missing",
            mix_strategy=policy["mix_strategy"], image_source=policy["image_source"],
        )
        scene_submitted = submitted.get(scene_id, {})
        for block in baseline:
            if block.get("status") != "planned":
                continue
            candidate = scene_submitted.get(str(block["id"]))
            if not candidate:
                if submitted:
                    raise WorkbenchError(f"已审核计划缺少 {scene_id}/{block['id']}，请重新预览")
                _apply_visual_route(block, _visual_route_for_block(block))
                continue
            if abs(_as_number(candidate.get("start_seconds")) - _as_number(block.get("start_seconds"))) > .02 or abs(_as_number(candidate.get("end_seconds")) - _as_number(block.get("end_seconds"))) > .02:
                raise WorkbenchError(f"{scene_id}/{block['id']} 的时间范围已经变化，请重新预览计划")
            route = str(candidate.get("route") or _visual_route_for_block(candidate))
            _apply_visual_route(block, route)
            block.update({
                "query": re.sub(r"\s+", " ", str(candidate.get("query") or "")).strip()[:240],
                "query_ladder": deepcopy(candidate.get("query_ladder") or []),
                "topic": str(candidate.get("topic") or "")[:120],
                "role": str(candidate.get("role") or "")[:40],
                "role_label": str(candidate.get("role_label") or "")[:40],
                "context_text": str(candidate.get("context_text") or "")[:3000],
                "slot_text": str(candidate.get("slot_text") or "")[:1200],
                "visual_intent": str(candidate.get("visual_intent") or "")[:160],
                "reason": str(candidate.get("reason") or "")[:200],
                "confidence": max(0.0, min(1.0, _as_number(candidate.get("confidence"), .5))),
                "fallback_route": str(candidate.get("fallback_route") or "") if str(candidate.get("fallback_route") or "") in VISUAL_BATCH_ROUTES else "",
                "scene_recipe": str(candidate.get("scene_recipe") or "relationship_map"),
                "layout_variant": str(candidate.get("layout_variant") or ""),
                "motion_variant": str(candidate.get("motion_variant") or ""),
                "layout_variant_locked": bool(candidate.get("layout_variant_locked")),
                "graphic_copy": deepcopy(candidate.get("graphic_copy") or {}),
                "decision_source": str(candidate.get("decision_source") or policy["planning_mode"]),
            })
            if block["route"] == "hyperframes":
                _apply_layout_variant(block)
                graphic = block.get("graphic_copy") if isinstance(block.get("graphic_copy"), dict) else {}
                if not str(graphic.get("headline") or "").strip() or not str(graphic.get("scene_goal") or "").strip():
                    raise WorkbenchError(f"{scene_id}/{block['id']} 缺少可审核的画面标题或表达目标，请先重新执行 AI 识别")
        resolved[scene_id] = baseline
    metadata = {
        "preview_plan_id": str((reviewed or {}).get("plan_id") or "") if isinstance(reviewed, dict) else "",
        "planner": deepcopy((reviewed or {}).get("planner") or {}) if isinstance(reviewed, dict) else {},
    }
    return resolved, metadata


def _write_visual_batch_contract(project_dir: Path, contract: dict) -> str:
    path = project_dir / "artifacts" / "visual_plans" / f"{contract['contract_id']}.json"
    _atomic_write(path, contract)
    return _safe_relpath(project_dir, str(path)) or str(path)


@_project_transactional
def start_visual_batch_generation(project_dir: Path, payload: dict) -> dict:
    """Persist a reviewed mixed-media slot plan and queue a serial worker."""
    if payload.get("confirmed") is not True:
        raise WorkbenchError("请先预览批量画面计划并确认后再开始生成")
    policy = _visual_batch_policy(payload)
    profile = str(payload.get("profile") or "auto")
    state = _load_for_write(project_dir)
    _require_no_review_preview_conflict(
        _automation(state), payload.get("_review_preview_job_id"), payload.get("_review_preview_worker_token"), payload.get("_review_preview_internal_capability")
    )
    search_strategy = _resolved_stock_search_strategy(state, policy)
    batch = _automation(state)["visual_batch"]
    if batch.get("status") in {"queued", "generating"}:
        raise WorkbenchError("已有批量画面任务正在运行，请等待完成后再提交")
    scene_ids = _visual_batch_scene_ids(state, payload)
    if not scene_ids:
        raise WorkbenchError("当前选择范围没有需要处理的片段")
    reviewed_blocks, reviewed_metadata = _validated_reviewed_visual_plan(state, payload, policy, profile, scene_ids)
    planned_routes = [
        str(block.get("route") or _visual_route_for_block(block))
        for blocks in reviewed_blocks.values() for block in blocks if block.get("status") == "planned"
    ]
    if "ai_image" in planned_routes and payload.get("ai_generation_confirmed") is not True:
        raise WorkbenchError("本计划包含 OpenAI 生图，请先确认模型、数量与可能产生的费用")
    if any(route in {"stock_video", "stock_image"} for route in planned_routes) and not os.environ.get("PEXELS_API_KEY"):
        raise WorkbenchError("计划包含网络素材，但 Pexels 尚未配置，请先设置 PEXELS_API_KEY")
    copied = 0
    if payload.get("copy_presenter_layout"):
        copied = _copy_presenter_layout_to_scenes(state, str(payload.get("layout_source_scene_id") or ""), scene_ids)
    items: list[dict] = []
    for scene_id in scene_ids:
        scene = _find(state["scenes"], scene_id, "场景")
        blocks = reviewed_blocks[scene_id]
        ready_blocks = [block for block in blocks if block.get("asset_id") and block.get("status") == "ready"]
        for usage in state.get("usages", []):
            if usage.get("scene_id") == scene_id and usage.get("role") == "visual_block":
                usage["selected"] = False
        for ready_block in ready_blocks:
            _append_visual_block_usage(state, scene, ready_block, str(ready_block["asset_id"]))
        old = scene.get("visual_timeline") if isinstance(scene.get("visual_timeline"), dict) else {}
        scene["visual_timeline"] = {
            "version": 2, "revision": int(_as_number(old.get("revision"), 0)) + 1,
            "blocks": blocks, "updated_at": _now(), "planning_profile": profile,
            "mix_strategy": policy["mix_strategy"], "image_source": policy["image_source"],
            "content_rules": policy["content_rules"], "operation_mode": policy["operation_mode"],
            "person_policy": policy["person_policy"], "candidate_limit": policy["candidate_limit"],
            "planning_mode": policy["planning_mode"],
            "search_strategy": deepcopy(search_strategy),
        }
        scene["source_strategy"] = "mixed" if profile == "auto" else (policy["image_source"] if profile == "image" else "web_download")
        scene["review_status"] = "needs_adjustment"
        _invalidate_scene_review_preview(scene, "批量画面正在补全，完成后请刷新本段审核预览")
        context = _scene_surrounding_context(state, scene)
        queued_blocks = [block for block in blocks if block.get("status") == "planned"]
        for slot_index, block in enumerate(queued_blocks, 1):
            route = str(block.get("route") or _visual_route_for_block(block))
            fallback_route = str(block.get("fallback_route") or "")
            # A stock provider can legitimately return no usable footage.  In
            # autonomous mode that must not create a blank slot or ask the user
            # to choose a replacement: the already-reviewed information-card
            # contract is the deterministic, local safety net.
            if policy["planning_mode"] == "ai_director" and route in {"stock_video", "stock_image"}:
                fallback_route = "hyperframes"
            search_plan = _stock_search_plan_for_block(
                scene,
                surrounding_context=context,
                slot_index=slot_index,
                block_id=str(block["id"]),
                strategy=search_strategy,
                rules=policy["content_rules"],
                person_policy=policy["person_policy"],
            )
            if block.get("query") and block.get("route") in {"stock_video", "stock_image"}:
                search_plan["query"] = str(block["query"])
                if search_plan.get("query_ladder"):
                    search_plan["query_ladder"][0] = {"level": "已审核检索词", "query": str(block["query"])}
            block.update(search_plan)
            items.append({
                "scene_id": scene_id, "block_id": block["id"], "slot_index": slot_index,
                "status": "queued", "query": search_plan["query"],
                "query_ladder": deepcopy(search_plan["query_ladder"]),
                "search_topic": search_plan["topic"], "search_role": search_plan["role"],
                "search_role_label": search_plan["role_label"], "query_source": search_plan["query_source"],
                "attempt": max(1, int(_as_number(block.get("attempt"))) + 1),
                "target_duration_seconds": _rounded_seconds(_as_number(block.get("end_seconds")) - _as_number(block.get("start_seconds"))),
                "media_kind": block.get("media_kind") or "video",
                "source_mode": block.get("source_mode") or "web_download",
                "route": route,
                "visual_intent": block.get("visual_intent") or search_plan["topic"],
                "reason": block.get("reason") or "已审核画面生产合同",
                "confidence": block.get("confidence"),
                "fallback_route": fallback_route,
                "scene_recipe": block.get("scene_recipe") or "relationship_map",
                "layout_variant": block.get("layout_variant") or "",
                "motion_variant": block.get("motion_variant") or "",
                "graphic_copy": deepcopy(block.get("graphic_copy") or {}),
                "slot_text": str(block.get("slot_text") or ""),
                "decision_source": block.get("decision_source") or policy["planning_mode"],
                "planning_mode": policy["planning_mode"],
                "context_text": search_plan["context_text"],
                "content_rules": list(policy["content_rules"]),
                "person_policy": policy["person_policy"], "candidate_limit": policy["candidate_limit"],
                "candidate_attempt": 0, "rejected_candidates": [],
                "director_ledger": {"director_version": DIRECTOR_VERSION, "attempts": [], "status": "pending"},
                "screening": {"status": "pending", "mode": policy["screening_mode"], "reasons": []},
                "error": "", "asset_id": None,
            })
    if not items:
        raise WorkbenchError("所选片段没有可生成的主体画面槽位；请检查主体画面是否已存在，或画面格是否已锁定")
    job_id = f"VBJ-{uuid4().hex[:10]}"
    contract_id = _visual_batch_plan_digest([
        {"scene_id": scene_id, "blocks": reviewed_blocks[scene_id]} for scene_id in scene_ids
    ])
    contract = {
        "version": "1.0", "contract_id": contract_id, "job_id": job_id,
        "status": "approved", "approved_at": _now(), "scene_ids": scene_ids,
        "parent_job_id": str(payload.get("_review_preview_job_id") or "") or None,
        "request_fingerprint": str(payload.get("_review_preview_request_fingerprint") or "") or None,
        "policy": deepcopy(policy), "planner": reviewed_metadata["planner"],
        "preview_plan_id": reviewed_metadata["preview_plan_id"],
        "items": deepcopy(items),
    }
    contract_path = _write_visual_batch_contract(project_dir, contract)
    _automation(state)["visual_batch"] = {
        "status": "queued", "job_id": job_id, "source_mode": "mixed", "profile": profile,
        "operation_mode": policy["operation_mode"], "mix_strategy": policy["mix_strategy"],
        "image_source": policy["image_source"], "content_rules": list(policy["content_rules"]),
        "person_policy": policy["person_policy"], "candidate_limit": policy["candidate_limit"],
        "search_strategy": deepcopy(search_strategy),
        "screening_mode": policy["screening_mode"],
        "planning_mode": policy["planning_mode"], "contract_id": contract_id,
        "contract_path": contract_path, "planner": reviewed_metadata["planner"],
        "parent_job_id": str(payload.get("_review_preview_job_id") or "") or None,
        "request_fingerprint": str(payload.get("_review_preview_request_fingerprint") or "") or None,
        "preview_plan_id": reviewed_metadata["preview_plan_id"],
        "scene_ids": scene_ids, "items": items, "total_slots": len(items),
        "completed_slots": 0, "failed_slots": 0, "current": None,
        "layout_source_scene_id": str(payload.get("layout_source_scene_id") or ""),
        "copied_presenter_layouts": copied, "started_at": _now(), "finished_at": None, "error": "",
    }
    _mark_render_needs_refresh(state, "批量画面时间线已更新")
    action = "替换" if policy["operation_mode"] == "replace_selected" else "补全"
    _activity(state, "visual_batch_started", f"已建立 {len(scene_ids)} 个片段、{len(items)} 个画面槽位的串行{action}任务", scene_ids=scene_ids, job_id=job_id)
    return _save(project_dir, state)


def read_visual_batch_generation(project_dir: Path) -> dict:
    state = read_workbench(project_dir)
    return {"generation": deepcopy(_automation(state)["visual_batch"])}


@_project_transactional
def requeue_failed_visual_batch(
    project_dir: Path,
    *,
    expected_job_id: str,
    expected_parent_job_id: str,
    expected_request_fingerprint: str,
) -> dict:
    """Requeue only failed slots owned by one failed review-preview parent.

    Completed assets/usages stay immutable.  The parent is still terminal
    while this transaction runs; the caller subsequently acquires a new
    parent worker lease through its own CAS-protected resume operation.
    """
    state = _load_for_write(project_dir)
    automation = _automation(state)
    parent = automation.get("review_preview_pipeline") or {}
    batch = automation.get("visual_batch") or {}
    if (
        str(parent.get("job_id") or "") != str(expected_parent_job_id or "")
        or str(parent.get("request_fingerprint") or "")
        != str(expected_request_fingerprint or "")
        or parent.get("status") != "failed"
        or str(parent.get("safe_resume_point") or parent.get("stage") or "")
        != "visual_generation"
    ):
        raise WorkbenchError("一键审核预览父任务身份、状态或安全恢复点已变化，拒绝重排画面")
    if (
        str(batch.get("job_id") or "") != str(expected_job_id or "")
        or str(batch.get("parent_job_id") or "") != str(expected_parent_job_id or "")
        or str(batch.get("request_fingerprint") or "")
        != str(expected_request_fingerprint or "")
    ):
        raise WorkbenchError("画面子任务不属于当前父任务或冻结请求，拒绝恢复")

    # If the child save succeeded but the subsequent parent CAS was
    # interrupted, a repeated explicit resume may finish the parent step
    # without mutating the child a second time.
    if (
        batch.get("status") == "queued"
        and batch.get("resume_parent_job_id") == expected_parent_job_id
        and int(batch.get("retry_slot_count") or 0) > 0
    ):
        return state
    if batch.get("status") not in {"failed", "completed_with_failures"}:
        raise WorkbenchError("当前画面子任务没有可恢复的失败槽")

    failed_items = [
        item for item in batch.get("items") or []
        if isinstance(item, dict) and item.get("status") == "failed"
    ]
    if not failed_items:
        raise WorkbenchError("画面子任务未记录失败槽，不能执行安全恢复")

    resumed_at = _now()
    for item in failed_items:
        scene_id = str(item.get("scene_id") or "")
        block_id = str(item.get("block_id") or "")
        scene = _find(state.get("scenes", []), scene_id, "场景")
        block = _find(
            ((scene.get("visual_timeline") or {}).get("blocks") or []),
            block_id,
            "画面槽位",
        )
        if block.get("status") == "ready" or block.get("asset_id"):
            raise WorkbenchError(
                f"{scene_id}/{block_id} 已存在完成素材，但子任务仍标记失败；请先人工核对，禁止覆盖"
            )
        item.setdefault("failure_history", []).append(
            {
                "failed_at": item.get("finished_at"),
                "stage": str(item.get("stage") or "")[:160],
                "error": _safe_automation_error(item.get("error") or "画面生成失败"),
                "attempt": int(_as_number(item.get("attempt"), 1)),
            }
        )
        item["failure_history"] = item["failure_history"][-5:]
        item.update(
            {
                "status": "queued",
                "stage": "等待仅重试失败画面",
                "attempt": max(1, int(_as_number(item.get("attempt"), 1)) + 1),
                "started_at": None,
                "finished_at": None,
                "error": "",
                "fallback_reason": "",
            }
        )
        item.pop("worker_claim_id", None)
        block.update(
            {
                "status": "planned",
                "attempt": item["attempt"],
                "error": "",
            }
        )

    retry_slot_count = len(failed_items)
    completed_slots = sum(
        1 for item in batch.get("items") or []
        if isinstance(item, dict) and item.get("status") == "completed"
    )
    batch.setdefault("resume_history", []).append(
        {
            "resumed_at": resumed_at,
            "parent_job_id": expected_parent_job_id,
            "retry_slot_count": retry_slot_count,
            "preserved_completed_slots": completed_slots,
        }
    )
    batch["resume_history"] = batch["resume_history"][-10:]
    batch.update(
        {
            "status": "queued",
            "completed_slots": completed_slots,
            "failed_slots": 0,
            "current": None,
            "finished_at": None,
            "error": "",
            "resumed_at": resumed_at,
            "resume_parent_job_id": expected_parent_job_id,
            "retry_slot_count": retry_slot_count,
            "resume_count": int(batch.get("resume_count") or 0) + 1,
        }
    )
    _activity(
        state,
        "visual_batch_failed_slots_requeued",
        f"已保留 {completed_slots} 个成功画面，仅重排 {retry_slot_count} 个失败槽",
        job_id=expected_job_id,
        parent_job_id=expected_parent_job_id,
    )
    return _save(project_dir, state)


def _append_visual_block_usage(state: dict, scene: dict, block: dict, asset_id: str) -> dict:
    for usage in state.get("usages", []):
        if usage.get("scene_id") == scene.get("id") and usage.get("role") == "visual_block" and (usage.get("transform") or {}).get("block_id") == block.get("id"):
            usage["selected"] = False
    usage = {
        "id": _numbered("U-", state["usages"], "id"), "asset_id": asset_id,
        "scene_id": scene["id"], "role": "visual_block", "selected": True,
        "transform": {"crop": None, "scale": 1, "speed": 1, "start_seconds": block["start_seconds"], "end_seconds": block["end_seconds"], "block_id": block["id"]},
        "created_at": _now(),
    }
    state["usages"].append(usage)
    block["usage_id"] = usage["id"]
    return usage


def update_visual_block_lock(project_dir: Path, scene_id: str, block_id: str, payload: dict) -> dict:
    state = _load_for_write(project_dir)
    scene = _find(state["scenes"], scene_id, "场景")
    block = _find((scene.get("visual_timeline") or {}).get("blocks") or [], block_id, "画面槽位")
    locked = bool(payload.get("locked"))
    if locked and not block.get("asset_id"):
        raise WorkbenchError("槽位还没有实际素材，不能锁定")
    block["locked"] = locked
    block["locked_at"] = _now() if locked else None
    _activity(state, "visual_block_lock", f"{scene_id} 的 {block_id} 已{'锁定' if locked else '解锁'}", scene_id=scene_id, block_id=block_id, locked=locked)
    return _save(project_dir, state)


@_project_transactional
def start_visual_block_refresh(project_dir: Path, scene_id: str, block_id: str, payload: dict) -> dict:
    if payload.get("confirmed") is not True:
        raise WorkbenchError("请确认后再重新搜索当前画面槽位")
    state = _load_for_write(project_dir)
    _require_no_review_preview_conflict(_automation(state))
    batch = _automation(state)["visual_batch"]
    if batch.get("status") in {"queued", "generating"}:
        raise WorkbenchError("已有批量画面任务正在运行，请等待完成后再返工单个槽位")
    if not os.environ.get("PEXELS_API_KEY"):
        raise WorkbenchError("Pexels 素材服务尚未配置，请先设置 PEXELS_API_KEY")
    scene = _find(state["scenes"], scene_id, "场景")
    block = _find((scene.get("visual_timeline") or {}).get("blocks") or [], block_id, "画面槽位")
    if block.get("locked"):
        raise WorkbenchError("该画面槽位已锁定，请先解锁再换素材")
    instruction = str(payload.get("instruction") or "").strip()[:600]
    semantic_tolerance = str(payload.get("semantic_tolerance") or "strict").strip()
    if semantic_tolerance not in {"strict", "contextual_broll"}:
        raise WorkbenchError("画面语义容忍模式无效")
    if instruction:
        scene["asset_refresh_instruction"] = instruction
    timeline = scene.get("visual_timeline") if isinstance(scene.get("visual_timeline"), dict) else {}
    planning_mode = str(timeline.get("planning_mode") or "rule_mix")
    if planning_mode not in VISUAL_BATCH_PLANNING_MODES:
        planning_mode = "rule_mix"
    person_policy = str(timeline.get("person_policy") or "balanced")
    content_rules = [str(value) for value in (timeline.get("content_rules") or VISUAL_BATCH_DEFAULT_RULES)]
    strategy = timeline.get("search_strategy") if isinstance(timeline.get("search_strategy"), dict) else _resolved_stock_search_strategy(state, {
        "search_theme": "", "preferred_keywords": [], "cautious_topics": [], "query_overrides": {},
    })
    block_position = next((index for index, entry in enumerate((timeline.get("blocks") or []), 1) if entry.get("id") == block_id), 1)
    search_plan = _stock_search_plan_for_block(
        scene,
        surrounding_context=_scene_surrounding_context(state, scene),
        slot_index=block_position,
        block_id=block_id,
        strategy=strategy,
        rules=content_rules,
        person_policy=person_policy,
    )
    query = search_plan["query"]
    attempt = max(1, int(_as_number(block.get("attempt"))) + 1)
    block.update({**search_plan, "status": "planned", "attempt": attempt, "error": ""})
    job_id = f"VBJ-{uuid4().hex[:10]}"
    _automation(state)["visual_batch"] = {
        "status": "queued", "job_id": job_id, "source_mode": "web_download", "profile": "auto",
        "scene_ids": [scene_id], "items": [{
            "scene_id": scene_id, "block_id": block_id, "slot_index": 1, "status": "queued",
            "query": query, "query_ladder": deepcopy(search_plan["query_ladder"]),
            "search_topic": search_plan["topic"], "search_role": search_plan["role"],
            "search_role_label": search_plan["role_label"], "query_source": search_plan["query_source"],
            "context_text": search_plan["context_text"], "content_rules": content_rules,
            "person_policy": person_policy, "candidate_limit": int(timeline.get("candidate_limit") or 6),
            "planning_mode": planning_mode,
            "semantic_tolerance": semantic_tolerance,
            "candidate_attempt": 0, "rejected_candidates": [], "attempt": attempt,
            "director_ledger": {"director_version": DIRECTOR_VERSION, "attempts": [], "status": "pending"},
            "target_duration_seconds": _rounded_seconds(_as_number(block.get("end_seconds")) - _as_number(block.get("start_seconds"))),
            "error": "", "asset_id": None,
        }],
        "total_slots": 1, "completed_slots": 0, "failed_slots": 0, "current": None,
        "mode": "slot_refresh", "search_strategy": deepcopy(strategy),
        "person_policy": person_policy, "candidate_limit": int(timeline.get("candidate_limit") or 6),
        "planning_mode": planning_mode,
        "started_at": _now(), "finished_at": None, "error": "",
    }
    _invalidate_scene_review_preview(scene, "当前画面槽位正在换素材")
    scene["review_status"] = "needs_adjustment"
    _activity(state, "visual_block_refresh_started", f"已开始仅更换 {scene_id} 的 {block_id}", scene_id=scene_id, block_id=block_id, job_id=job_id)
    return _save(project_dir, state)


@_project_transactional
def start_scene_motion_visual_generation(project_dir: Path, scene_id: str, payload: dict) -> dict:
    """Queue a local HyperFrames or Remotion visual without provider fallback."""
    project_dir = project_dir.resolve()
    state = _load_for_write(project_dir)
    _require_no_review_preview_conflict(_automation(state))
    scene = _find(state["scenes"], scene_id, "场景")
    _ensure_scene_visual_state(state, scene)
    plan = scene["visual_plan"]
    engine = str(plan.get("engine") or "")
    if plan.get("status") != "saved":
        raise WorkbenchError("请先保存画面方案，再生成动态素材")
    if engine not in {"hyperframes", "remotion"}:
        raise WorkbenchError("当前画面方案不是 HyperFrames 或 Remotion 动态画面")
    if scene.get("source_strategy") != "ai_generated":
        raise WorkbenchError("请先在右侧选择“AI 生成素材”，再开始生成动态画面")
    job = scene.get("motion_generation") if isinstance(scene.get("motion_generation"), dict) else {}
    if job.get("status") == "generating":
        raise WorkbenchError("本段动态素材正在生成，请不要重复点击")
    scene["motion_generation"] = {
        "status": "generating", "engine": engine, "started_at": _now(), "error": "",
    }
    _activity(state, "motion_visual_started", f"已开始用 {engine} 生成 {scene_id} 的动态画面", scene_id=scene_id, engine=engine)
    return _save(project_dir, state)


def mark_scene_motion_visual_failed(project_dir: Path, scene_id: str, error: object) -> dict:
    project_dir = project_dir.resolve()
    state = _load_for_write(project_dir)
    scene = _find(state["scenes"], scene_id, "场景")
    message = _safe_automation_error(error)
    scene["motion_generation"] = {**(scene.get("motion_generation") or {}), "status": "failed", "finished_at": _now(), "error": message}
    _activity(state, "motion_visual_failed", f"{scene_id} 的动态画面生成失败：{message}", scene_id=scene_id)
    return _save(project_dir, state)


def _normalize_motion_visual(project_dir: Path, state: dict, scene: dict, source: Path, engine: str) -> Path:
    ffmpeg = _ffmpeg_available()
    if not ffmpeg:
        raise WorkbenchError("本机未发现 FFmpeg，无法规范化动态画面的画幅与时长")
    width, height = _render_dimensions(project_dir, state)
    fps = int(state.get("settings", {}).get("frame_rate") or 30)
    duration = _scene_duration(scene)
    output = project_dir / "assets" / "video" / engine / f"{scene['id']}-{uuid4().hex[:10]}.mp4"
    output.parent.mkdir(parents=True, exist_ok=True)
    ok, detail = _run_media([
        ffmpeg, "-y", "-stream_loop", "-1", "-i", str(source), "-an",
        "-vf", f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},setsar=1,fps={fps}",
        "-t", f"{duration:.6f}", "-c:v", "libx264", "-preset", "medium", "-crf", "20",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(output),
    ])
    if not ok or not output.is_file():
        raise WorkbenchError(f"动态画面规范化失败：{detail}")
    return output


def _hyperframes_style_render_report(result: Any, style_context: dict) -> dict:
    """Keep a compact, user-readable audit trail next to the new asset."""
    data = result.data if isinstance(getattr(result, "data", None), dict) else {}
    steps = data.get("steps") if isinstance(data.get("steps"), dict) else {}
    return {
        "style_pack_id": style_context.get("style_pack_id"),
        "style_pack_version": style_context.get("style_pack_version"),
        "aspect_profile": style_context.get("aspect_profile"),
        "aspect_status": style_context.get("aspect_status"),
        "scene_recipe": style_context.get("scene_recipe"),
        "layout_variant": style_context.get("layout_variant"),
        "motion_variant": style_context.get("motion_variant"),
        "caption_owner": (style_context.get("caption_policy") or {}).get("owner"),
        "caption_baked": bool((style_context.get("caption_policy") or {}).get("baked_into_hyperframes")),
        "headline_owner": (style_context.get("headline_policy") or {}).get("owner"),
        "headline_baked": bool((style_context.get("headline_policy") or {}).get("render_in_hyperframes")),
        "lint_exit_code": (steps.get("lint") or {}).get("exit_code"),
        "validate_exit_code": (steps.get("validate") or {}).get("exit_code"),
        "inspect_exit_code": (steps.get("inspect") or {}).get("exit_code"),
        "generated_at": _now(),
    }


def generate_scene_motion_visual(project_dir: Path, scene_id: str) -> dict:
    """Render one approved structured brief through the selected local runtime."""
    project_dir = project_dir.resolve()
    state = _load_for_write(project_dir)
    scene = _find(state["scenes"], scene_id, "场景")
    _ensure_scene_visual_state(state, scene)
    job = scene.get("motion_generation") if isinstance(scene.get("motion_generation"), dict) else {}
    if job.get("status") != "generating":
        raise WorkbenchError("当前没有待执行的动态画面任务")
    plan = scene["visual_plan"]
    engine = str(plan.get("engine") or "")
    spec = plan.get("structured_spec") or {}
    duration = _scene_duration(scene)
    width, height = _render_dimensions(project_dir, state)
    headline = str(spec.get("headline") or scene.get("title") or "科技信息")
    components = [str(item) for item in (spec.get("components") or []) if str(item).strip()]
    support = " · ".join(components[:4]) or "数据关系 · 产品轮廓 · 信息节点"
    temporary = project_dir / "renders" / "motion-candidates" / f"{scene_id}-{engine}-{uuid4().hex[:8]}.mp4"
    temporary.parent.mkdir(parents=True, exist_ok=True)
    edit_decisions = {
        "version": "1.0", "renderer_family": "explainer-data", "render_runtime": engine, "composition_mode": "templated",
        "metadata": {
            "title": headline, "proposal_render_runtime": engine,
            "compose_target": {"width": width, "height": height, "fit": "cover"},
            "target_duration_seconds": duration,
            "visual_brief": plan.get("prompt"), "motion": spec.get("motion"), "palette": spec.get("palette"),
        },
        "cuts": [
            {"id": f"{scene_id}-hero", "type": "text_card", "text": headline, "in_seconds": 0, "out_seconds": max(.5, duration * .55)},
            {"id": f"{scene_id}-support", "type": "callout", "text": support, "in_seconds": max(.5, duration * .55), "out_seconds": duration},
        ],
    }
    if engine == "remotion":
        result = VideoCompose().execute({
            "operation": "render", "edit_decisions": edit_decisions,
            "asset_manifest": {"version": "1.0", "assets": []}, "output_path": str(temporary),
            "options": {"subtitle_burn": False},
        })
    elif engine == "hyperframes":
        style_pack = plan.get("style_pack") if isinstance(plan.get("style_pack"), dict) else {}
        style_pack_id = str(style_pack.get("id") or STYLE_PACK_ID)
        try:
            style_context = build_style_context(
                scene=scene,
                plan=plan,
                width=width,
                height=height,
                duration_seconds=duration,
                style_pack_id=style_pack_id,
            )
        except StylePackError as exc:
            raise WorkbenchError(f"风格包解析失败：{exc}") from exc
        edit_decisions["metadata"].update({
            "style_pack_id": style_context["style_pack_id"],
            "style_pack_version": style_context["style_pack_version"],
            "style_context": style_context,
            "require_layout_inspect": True,
        })
        workspace = project_dir / "artifacts" / "motion_compositions" / scene_id / f"hyperframes-{uuid4().hex[:8]}"
        profile = "tiktok" if height > width else "youtube_landscape"
        result = HyperFramesCompose().execute({
            "operation": "render", "workspace_path": str(workspace), "profile": profile,
            "fps": int(state.get("settings", {}).get("frame_rate") or 30),
            "edit_decisions": edit_decisions, "asset_manifest": {"version": "1.0", "assets": []},
            "playbook": style_pack_playbook(style_pack_id),
            "output_path": str(temporary), "quality": "draft", "strict": True,
        })
    else:
        raise WorkbenchError("未知的动态画面运行时")
    if not result.success or not temporary.is_file():
        raise WorkbenchError(str(result.error or f"{engine} 没有生成可播放文件"))
    output = _normalize_motion_visual(project_dir, state, scene, temporary, engine)
    try:
        temporary.unlink()
    except OSError:
        pass
    asset = _append_asset(project_dir, state, {
        "name": f"{scene.get('title') or scene_id} · {engine} 动态画面", "type": "video", "source_type": "local_generated",
        "path": str(output), "duration_seconds": duration, "resolution": f"{width}x{height}",
        "provider": engine, "source_tool": f"{engine}_compose", "license": "本地可复现动态合成",
        "generation": {
            "scene_id": scene_id,
            "engine": engine,
            "visual_plan_revision": plan.get("revision"),
            "style_render_report": _hyperframes_style_render_report(result, style_context) if engine == "hyperframes" else None,
            "generated_at": _now(),
        },
    })
    scene["motion_visual_candidate"] = {"asset_id": asset["id"], "engine": engine, "status": "ready", "generated_at": _now()}
    scene["motion_generation"] = {"status": "completed", "engine": engine, "asset_id": asset["id"], "finished_at": _now(), "error": ""}
    _decision(state, "motion_visual_runtime", f"{scene_id} 动态画面运行时", engine, f"生成素材 {asset['id']}")
    _activity(state, "motion_visual_completed", f"{scene_id} 的 {engine} 动态素材已登记为 {asset['id']}，可加入片段内画面轨道", scene_id=scene_id, asset_id=asset["id"])
    return _save(project_dir, state)


def _write_keyframe_review_artifact(project_dir: Path, scene: dict, review: dict) -> str:
    path = project_dir / KEYFRAME_REVIEW_DIRECTORY / f"{scene['id']}.json"
    _atomic_write(path, {
        "version": "1.0",
        "review_id": review.get("id"),
        "scene_id": scene.get("id"),
        "scene_title": scene.get("title"),
        "status": review.get("status"),
        "timeline": review.get("timeline") or [],
        "hyperframes": review.get("hyperframes") or {},
        "generation": review.get("generation") or {},
        "review_note": review.get("review_note") or "",
        "updated_at": _now(),
    })
    return _safe_relpath(project_dir, str(path)) or str(path)


def _build_hyperframes_review(project_dir: Path, state: dict, scene: dict, timeline: list[dict]) -> dict:
    """Materialize a small HyperFrames review composition for this scene.

    The review composition is intentionally local and regenerable. It is not
    the final delivery render; it makes the exact image/caption decisions
    inspectable before the scene is allowed into the usage ledger.
    """
    workspace = project_dir / "artifacts" / "hyperframes_reviews" / str(scene["id"])
    duration = max(0.1, _as_number(scene.get("end_seconds")) - _as_number(scene.get("start_seconds")))
    cuts: list[dict] = []
    manifest_assets: list[dict] = []
    for index, item in enumerate(timeline):
        asset = next((a for a in state.get("assets", []) if a.get("id") == item.get("asset_id")), None)
        if not asset or not asset.get("path"):
            continue
        manifest_assets.append({
            "id": asset["id"],
            "path": str(project_dir / asset["path"]),
        })
        start = _as_number(item.get("relative_start_seconds"))
        end = _as_number(item.get("relative_end_seconds"), duration)
        cuts.append({
            "id": f"{scene['id']}-{item['anchor_kind']}-image",
            "source": asset["id"],
            "type": "image",
            "in_seconds": start,
            "out_seconds": max(start + 0.1, end),
        })
        cuts.append({
            "id": f"{scene['id']}-{item['anchor_kind']}-caption",
            "type": "text_card",
            "text": item.get("caption_text") or scene.get("description") or "",
            "in_seconds": start,
            "out_seconds": max(start + 0.1, end),
        })
    if not cuts:
        return {"status": "blocked", "error": "没有可供 HyperFrames 使用的关键帧图片"}
    aspect = _normalize_intake(state.get("project", {}).get("intake")).get("aspect")
    profile = "tiktok" if aspect in {"portrait", "vertical"} else "youtube_landscape"
    result = HyperFramesCompose().execute({
        "operation": "scaffold_workspace",
        "workspace_path": str(workspace),
        "profile": profile,
        "fps": int(state.get("settings", {}).get("frame_rate") or 30),
        "edit_decisions": {
            "version": "1.0",
            "renderer_family": "director-keyframe-review",
            "render_runtime": "hyperframes",
            "metadata": {"title": f"{scene.get('title') or scene['id']} · 关键帧审核"},
            "cuts": cuts,
        },
        "asset_manifest": {"assets": manifest_assets},
        "playbook": {
            "name": "OpenMontage 中文导演审核台",
            "visual_language": {"color_palette": {"background": "#0B0F1A", "text": "#F5F7FA", "accent": "#F5A623"}},
            "typography": {"heading": {"font": "Arial"}, "body": {"font": "Arial"}},
        },
    })
    if not result.success:
        return {"status": "blocked", "error": str(result.error or "HyperFrames 时间线生成失败")[:1000]}
    index_path = workspace / "index.html"
    return {
        "status": "scaffolded",
        "workspace_path": _safe_relpath(project_dir, str(workspace)),
        "index_path": _safe_relpath(project_dir, str(index_path)),
        "duration_seconds": round(duration, 3),
        "tool": "hyperframes_compose",
        "runtime": "hyperframes",
    }


@_project_transactional
def start_scene_keyframe_generation(project_dir: Path, scene_id: str, payload: dict) -> dict:
    """Create a durable, anchor-level job before the paid worker is launched."""
    if payload.get("confirmed") is not True:
        raise WorkbenchError("生成关键帧前需要确认将调用 OpenAI 生图服务并生成 2 张图片")
    state = _load_for_write(project_dir)
    _require_no_review_preview_conflict(_automation(state))
    scene = _find(state["scenes"], scene_id, "场景")
    _ensure_scene_visual_state(state, scene)
    plan = scene.get("visual_plan") or {}
    if plan.get("status") != "saved":
        raise WorkbenchError("请先检查并保存画面提示词，再提交付费生图任务")
    if plan.get("engine") != "openai_image":
        raise WorkbenchError("当前画面方案不是 OpenAI 静态图；请使用对应的动态素材生成按钮")
    if scene.get("source_strategy") != "ai_generated":
        raise WorkbenchError("请先在右侧选择“AI 生成素材”，再开始生成关键帧")
    previous = scene.get("keyframe_generation") if isinstance(scene.get("keyframe_generation"), dict) else {}
    if previous.get("status") == "generating":
        raise WorkbenchError("本场景正在生成关键帧，请稍候")
    if (scene.get("keyframe_review") or {}).get("status") == "approved":
        raise WorkbenchError("本场景关键帧已经通过；如需重做，请先标记场景需要调整")
    model = str(payload.get("model") or "gpt-image-2")
    quality = str(payload.get("quality") or "low")
    size = str(payload.get("size") or "1536x1024")
    if model != "gpt-image-2":
        raise WorkbenchError("当前工作台仅支持 gpt-image-2；请在中转站开通该模型后再使用")
    if quality not in {"low", "medium", "high", "auto"}:
        raise WorkbenchError("不支持的图片质量档位")
    if size not in {"1024x1024", "1536x1024", "1024x1536", "auto"}:
        raise WorkbenchError("不支持的图片尺寸")

    resume_failed = payload.get("resume_failed") is True
    if resume_failed:
        if previous.get("status") not in {"failed", "completed_with_failures"}:
            raise WorkbenchError("当前没有可继续的失败关键帧")
        if previous.get("visual_plan_revision") != plan.get("revision") or previous.get("source_strategy") != scene.get("source_strategy"):
            raise WorkbenchError("画面方案已变化，不能继续旧任务；请重新生成本段关键帧")
        anchors = previous.get("anchors") if isinstance(previous.get("anchors"), dict) else {}
        failed = [kind for kind in KEYFRAME_ANCHOR_KINDS if isinstance(anchors.get(kind), dict) and anchors[kind].get("status") == "failed"]
        if not failed:
            raise WorkbenchError("没有找到可继续的失败关键帧")
        for kind in failed:
            anchors[kind].update({"status": "queued", "error": "", "started_at": None, "finished_at": None})
        previous.update({
            "status": "generating",
            "started_at": _now(),
            "finished_at": None,
            "active_anchor_kind": None,
            "model": model,
            "quality": quality,
            "size": size,
            "error": "",
        })
        _activity(state, "keyframe_generation_resumed", f"已继续场景 {scene_id} 的失败关键帧；已成功图片不会重新生成", scene_id=scene_id, anchors=failed)
        return _save(project_dir, state)

    anchors: dict[str, dict] = {}
    for kind in KEYFRAME_ANCHOR_KINDS:
        anchor = next((item for item in scene.get("anchors", []) if item.get("kind") == kind), None)
        if anchor is None:
            raise WorkbenchError(f"场景缺少{kind}审核锚点")
        anchors[kind] = {
            "status": "queued",
            "asset_id": None,
            "error": "",
            "prompt": _keyframe_prompt(state, scene, kind),
            "time_seconds": round(_as_number(anchor.get("time_seconds")), 3),
        }
    scene["keyframe_generation"] = {
        "status": "generating",
        "job_id": f"KFG-{uuid4().hex[:12]}",
        "started_at": _now(),
        "finished_at": None,
        "provider": "openai",
        "tool": "openai_image",
        "model": model,
        "quality": quality,
        "size": size,
        "source_strategy": scene.get("source_strategy"),
        "visual_plan_revision": plan.get("revision"),
        "active_anchor_kind": None,
        "anchors": anchors,
        "expected_count": len(KEYFRAME_ANCHOR_KINDS),
        "completed_count": 0,
        "error": "",
    }
    _activity(state, "keyframe_generation_started", f"已开始生成场景 {scene_id} 的首帧与高潮帧", scene_id=scene_id)
    return _save(project_dir, state)


def read_scene_keyframe_generation(project_dir: Path, scene_id: str) -> dict:
    """Return only the small state required by the non-disruptive task card."""
    state = read_workbench(project_dir)
    scene = _find(state.get("scenes", []), scene_id, "场景")
    generation = scene.get("keyframe_generation") if isinstance(scene.get("keyframe_generation"), dict) else None
    return {"scene_id": scene_id, "generation": deepcopy(generation) if generation else None}


def _keyframe_job_is_current(scene: dict, job_id: str, plan_revision: object) -> bool:
    job = scene.get("keyframe_generation") if isinstance(scene.get("keyframe_generation"), dict) else {}
    plan = scene.get("visual_plan") if isinstance(scene.get("visual_plan"), dict) else {}
    return (
        job.get("status") == "generating"
        and job.get("job_id") == job_id
        and job.get("visual_plan_revision") == plan_revision
        and scene.get("source_strategy") == "ai_generated"
        and plan.get("engine") == "openai_image"
    )


def _keyframe_review_timeline(scene: dict, job: dict, review_id: str) -> list[dict]:
    scene_start = _as_number(scene.get("start_seconds"))
    scene_end = max(scene_start, _as_number(scene.get("end_seconds"), scene_start))
    scene_duration = max(0.1, scene_end - scene_start)
    anchors = job.get("anchors") if isinstance(job.get("anchors"), dict) else {}
    timeline: list[dict] = []
    for index, kind in enumerate(KEYFRAME_ANCHOR_KINDS):
        item = anchors.get(kind) if isinstance(anchors.get(kind), dict) else {}
        absolute_time = min(max(_as_number(item.get("time_seconds"), scene_start), scene_start), scene_end)
        relative_time = max(0.0, absolute_time - scene_start)
        if index + 1 < len(KEYFRAME_ANCHOR_KINDS):
            next_item = anchors.get(KEYFRAME_ANCHOR_KINDS[index + 1]) if isinstance(anchors.get(KEYFRAME_ANCHOR_KINDS[index + 1]), dict) else {}
            next_time = max(relative_time + 0.1, _as_number(next_item.get("time_seconds"), scene_end) - scene_start)
        else:
            next_time = scene_duration
        timeline.append({
            "id": f"{review_id}-{index + 1:02d}",
            "anchor_kind": kind,
            "label": "首帧" if kind == "first_frame" else "高潮帧",
            "time_seconds": round(absolute_time, 3),
            "relative_start_seconds": round(relative_time, 3),
            "relative_end_seconds": round(min(scene_duration, next_time), 3),
            "caption_text": str(scene.get("description") or "").strip(),
            "visual_note": str(scene.get("shot_intent") or "").strip(),
            "asset_id": item.get("asset_id"),
            "status": "pending",
            "prompt": str(item.get("prompt") or ""),
        })
    return timeline


def _finalize_scene_keyframe_review(project_dir: Path, state: dict, scene: dict, job: dict) -> None:
    """Create the review surface only after both independently persisted frames exist."""
    anchors = job.get("anchors") if isinstance(job.get("anchors"), dict) else {}
    if any((anchors.get(kind) or {}).get("status") != "completed" for kind in KEYFRAME_ANCHOR_KINDS):
        return
    review_id = _numbered("KFR-", state.setdefault("keyframe_reviews", []), "id")
    timeline = _keyframe_review_timeline(scene, job, review_id)
    review = {
        "id": review_id,
        "status": "generated",
        "timeline": timeline,
        "generation": {
            "provider": "openai", "tool": "openai_image", "model": job.get("model"),
            "quality": job.get("quality"), "size": job.get("size"), "count": len(timeline), "generated_at": _now(),
        },
        "review_note": "",
    }
    review["hyperframes"] = _build_hyperframes_review(project_dir, state, scene, timeline)
    review["artifact_path"] = _write_keyframe_review_artifact(project_dir, scene, review)
    scene["keyframe_review"] = review
    scene["ai_visual_candidate"] = {
        "asset_id": anchors["first_frame"]["asset_id"],
        "review_id": review_id,
        "status": "pending",
        "generated_at": _now(),
    }
    job.update({
        "status": "completed",
        "active_anchor_kind": None,
        "finished_at": _now(),
        "completed_count": len(KEYFRAME_ANCHOR_KINDS),
        "review_id": review_id,
        "error": "",
    })
    state["keyframe_reviews"].append({"id": review_id, "scene_id": scene["id"], "status": review["status"], "artifact_path": review["artifact_path"], "created_at": _now()})
    _decision(state, "keyframe_generation", f"{scene['id']} 首帧与高潮帧", "openai / gpt-image-2", f"已生成 {len(timeline)} 张候选图，等待人工逐帧审核")
    _activity(state, "keyframe_generation", f"场景 {scene['id']} 已生成首帧与高潮帧，等待字幕和画面审核", scene_id=scene["id"], asset_ids=[item["asset_id"] for item in timeline], review_id=review_id)


def mark_scene_keyframe_generation_failed(project_dir: Path, scene_id: str, error: object) -> dict:
    """Mark just the active anchor failed, preserving earlier paid output."""
    state = _load_for_write(project_dir)
    scene = _find(state["scenes"], scene_id, "场景")
    job = scene.get("keyframe_generation") if isinstance(scene.get("keyframe_generation"), dict) else None
    if not job or job.get("status") != "generating":
        return _save(project_dir, state)
    message = _safe_openai_error(error)
    anchors = job.get("anchors") if isinstance(job.get("anchors"), dict) else {}
    active_kind = str(job.get("active_anchor_kind") or "")
    if active_kind not in KEYFRAME_ANCHOR_KINDS or not isinstance(anchors.get(active_kind), dict):
        active_kind = next((kind for kind in KEYFRAME_ANCHOR_KINDS if isinstance(anchors.get(kind), dict) and anchors[kind].get("status") == "generating"), "")
    if active_kind:
        anchors[active_kind].update({"status": "failed", "finished_at": _now(), "error": message})
    completed = sum(1 for kind in KEYFRAME_ANCHOR_KINDS if (anchors.get(kind) or {}).get("status") == "completed")
    job.update({
        "status": "completed_with_failures" if completed else "failed",
        "active_anchor_kind": None,
        "finished_at": _now(),
        "completed_count": completed,
        "error": message,
    })
    _activity(state, "keyframe_generation_failed", f"场景 {scene_id} 的关键帧生成失败：{message}", scene_id=scene_id, anchor_kind=active_kind)
    return _save(project_dir, state)


def generate_scene_keyframes(project_dir: Path, scene_id: str, payload: dict) -> dict:
    """Generate durable keyframes one anchor at a time.

    The function is also kept compatible with the historic synchronous route:
    without ``_single_anchor`` it consumes every remaining queued anchor.
    """
    if payload.get("confirmed") is not True:
        raise WorkbenchError("生成关键帧前需要确认将调用 OpenAI 生图服务并生成 2 张图片")
    state = _load_for_write(project_dir)
    scene = _find(state["scenes"], scene_id, "场景")
    _ensure_scene_visual_state(state, scene)
    if (scene.get("visual_plan") or {}).get("status") != "saved":
        raise WorkbenchError("请先检查并保存画面提示词，再提交付费生图任务")
    if (scene.get("visual_plan") or {}).get("engine") != "openai_image":
        raise WorkbenchError("当前画面方案不是 OpenAI 静态图；请使用对应的动态素材生成按钮")
    if scene.get("source_strategy") != "ai_generated":
        raise WorkbenchError("请先在右侧选择“AI 生成素材”，再开始生成关键帧")
    job = scene.get("keyframe_generation") if isinstance(scene.get("keyframe_generation"), dict) else None
    if not job:
        state = start_scene_keyframe_generation(project_dir, scene_id, payload)
        scene = _find(state["scenes"], scene_id, "场景")
        job = scene["keyframe_generation"]
    if job.get("status") != "generating":
        return state
    job_id = str(job.get("job_id") or "")
    plan_revision = job.get("visual_plan_revision")
    if not job_id or not _keyframe_job_is_current(scene, job_id, plan_revision):
        raise WorkbenchError("关键帧任务已过期；请重新提交本段生成")

    single_anchor = payload.get("_single_anchor") is True
    for kind in KEYFRAME_ANCHOR_KINDS:
        anchors = job.get("anchors") if isinstance(job.get("anchors"), dict) else {}
        anchor = anchors.get(kind) if isinstance(anchors.get(kind), dict) else None
        if not anchor or anchor.get("status") == "completed":
            continue
        if anchor.get("status") not in {"queued", "failed"}:
            continue
        anchor.update({"status": "generating", "started_at": _now(), "finished_at": None, "error": ""})
        job["active_anchor_kind"] = kind
        job["error"] = ""
        _save(project_dir, state)

        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        output_path = project_dir / "assets" / "images" / "openai" / f"keyframe-{scene['id']}-{kind}-{stamp}-{uuid4().hex[:8]}.png"
        result = OpenAIImage().execute({
            "prompt": str(anchor.get("prompt") or _keyframe_prompt(state, scene, kind)),
            "model": job.get("model") or "gpt-image-2",
            "size": job.get("size") or "1536x1024",
            "quality": job.get("quality") or "low",
            "output_format": "png",
            "n": 1,
            "output_path": str(output_path),
        })

        # The prompt/source may have changed while the provider was working.
        # Reload before publishing so stale paid output never replaces the
        # reviewer's current plan.
        state = _load_for_write(project_dir)
        scene = _find(state["scenes"], scene_id, "场景")
        if not _keyframe_job_is_current(scene, job_id, plan_revision):
            return state
        job = scene["keyframe_generation"]
        anchors = job["anchors"]
        anchor = anchors[kind]
        if not result.success:
            message = _safe_openai_error(result.error)
            anchor.update({"status": "failed", "finished_at": _now(), "error": message})
            completed = sum(1 for item in anchors.values() if isinstance(item, dict) and item.get("status") == "completed")
            job.update({"status": "completed_with_failures" if completed else "failed", "active_anchor_kind": None, "finished_at": _now(), "completed_count": completed, "error": message})
            _activity(state, "keyframe_generation_failed", f"场景 {scene_id} 的{_keyframe_label(kind)}生成失败：{message}", scene_id=scene_id, anchor_kind=kind)
            return _save(project_dir, state)
        if not result.artifacts:
            return mark_scene_keyframe_generation_failed(project_dir, scene_id, f"{_keyframe_label(kind)}生图服务没有返回图片文件")
        path = _safe_relpath(project_dir, result.artifacts[0])
        if not path or not (project_dir / path).is_file():
            return mark_scene_keyframe_generation_failed(project_dir, scene_id, "生图服务返回的文件不在当前项目目录内")
        asset = _append_asset(project_dir, state, {
            "name": f"{scene.get('title') or scene_id} · {_keyframe_label(kind)}",
            "type": "image", "source_type": "ai_generated", "path": path,
            "resolution": job.get("size") or "1536x1024", "provider": "OpenAI", "source_tool": "openai_image",
            "license": "AI 生成；请按项目发布规范复核",
            "generation": {
                "provider": "openai", "tool": "openai_image", "model": job.get("model"),
                "prompt": anchor.get("prompt"), "size": job.get("size"), "quality": job.get("quality"), "batch_size": 1,
                "scene_id": scene_id, "anchor_kind": kind, "generated_at": _now(),
            },
        })
        anchor.update({"status": "completed", "asset_id": asset["id"], "finished_at": _now(), "error": ""})
        completed = sum(1 for item in anchors.values() if isinstance(item, dict) and item.get("status") == "completed")
        job.update({"active_anchor_kind": None, "completed_count": completed})
        _activity(state, "keyframe_anchor_completed", f"场景 {scene_id} 的{_keyframe_label(kind)}已生成并登记为 {asset['id']}", scene_id=scene_id, anchor_kind=kind, asset_id=asset["id"])
        if completed == len(KEYFRAME_ANCHOR_KINDS):
            _finalize_scene_keyframe_review(project_dir, state, scene, job)
        state = _save(project_dir, state)
        if single_anchor or job.get("status") != "generating":
            return state
        scene = _find(state["scenes"], scene_id, "场景")
        job = scene["keyframe_generation"]

    if job.get("status") == "generating":
        _finalize_scene_keyframe_review(project_dir, state, scene, job)
    return _save(project_dir, state)


def review_scene_keyframes(project_dir: Path, scene_id: str, payload: dict) -> dict:
    """Update individual keyframe decisions or close the scene keyframe gate."""
    state = _load_for_write(project_dir)
    scene = _find(state["scenes"], scene_id, "场景")
    review = scene.get("keyframe_review")
    if not isinstance(review, dict) or not review.get("timeline"):
        raise WorkbenchError("请先生成本场景的首帧和高潮帧")
    action = str(payload.get("action") or "update")
    if action not in {"update", "approve", "request_revision"}:
        raise WorkbenchError("关键帧审核动作无效")
    updates = payload.get("items") if isinstance(payload.get("items"), list) else []
    for update in updates:
        if not isinstance(update, dict):
            continue
        kind = str(update.get("anchor_kind") or "")
        item = next((entry for entry in review["timeline"] if entry.get("anchor_kind") == kind), None)
        if item is None:
            raise WorkbenchError(f"未找到关键帧：{kind}")
        if "status" in update:
            item_status = str(update.get("status") or "pending")
            if item_status not in {"pending", "approved", "needs_adjustment"}:
                raise WorkbenchError("关键帧状态无效")
            item["status"] = item_status
        if "caption_text" in update:
            caption = str(update.get("caption_text") or "").strip()
            if len(caption) > 2000:
                raise WorkbenchError("字幕不能超过 2000 个字符")
            item["caption_text"] = caption
        if "visual_note" in update:
            item["visual_note"] = str(update.get("visual_note") or "").strip()[:2000]
        item["reviewed_at"] = _now()

    if action == "approve":
        if any(item.get("status") != "approved" for item in review["timeline"]):
            raise WorkbenchError("请先逐张通过首帧和高潮帧，再通过整组关键帧")
        for item in review["timeline"]:
            asset_id = str(item.get("asset_id") or "")
            _find(state["assets"], asset_id, "关键帧素材")
            role = f"visual_{item['anchor_kind']}"
            existing = next((usage for usage in state["usages"] if usage.get("scene_id") == scene_id and usage.get("role") == role and usage.get("asset_id") == asset_id), None)
            if existing:
                existing["selected"] = True
                usage_id = existing["id"]
            else:
                for usage in state["usages"]:
                    if usage.get("scene_id") == scene_id and usage.get("role") == role:
                        usage["selected"] = False
                usage_id = _numbered("U-", state["usages"], "id")
                state["usages"].append({
                    "id": usage_id, "asset_id": asset_id, "scene_id": scene_id, "role": role,
                    "selected": True, "transform": {"crop": None, "scale": 1, "speed": 1}, "created_at": _now(),
                })
            item["usage_id"] = usage_id
            anchor = next((entry for entry in scene.get("anchors", []) if entry.get("kind") == item.get("anchor_kind")), None)
            if anchor:
                anchor["status"] = "approved"
                anchor["reviewed_at"] = _now()
        review["status"] = "approved"
        review["approved_at"] = _now()
        review["review_note"] = str(payload.get("note") or "").strip()[:2000]
        if _is_avatar_project(state) and (state.get("avatar") or {}).get("status") == "timeline_applied":
            subtitle_path = _write_avatar_review_subtitles(project_dir, state)
            _automation(state)["narration_generation"]["subtitle_path"] = _safe_relpath(project_dir, str(subtitle_path))
        _decision(state, "keyframe_review", f"{scene_id} 首帧与高潮帧审核", "approved", review["review_note"])
        _activity(state, "keyframe_review", f"场景 {scene_id} 的关键帧和字幕已通过，候选素材已生成 U-xxx 使用编号", scene_id=scene_id)
    elif action == "request_revision":
        note = str(payload.get("note") or "").strip()
        if not note:
            raise WorkbenchError("请填写关键帧需要调整的意见")
        review["status"] = "needs_adjustment"
        review["review_note"] = note[:2000]
        scene["review_status"] = "needs_adjustment"
        _decision(state, "keyframe_review", f"{scene_id} 首帧与高潮帧审核", "needs_adjustment", review["review_note"])
        _activity(state, "keyframe_review", f"场景 {scene_id} 的关键帧审核需要调整", scene_id=scene_id)
    else:
        _activity(state, "keyframe_review", f"已更新场景 {scene_id} 的关键帧审核意见", scene_id=scene_id)
    review["artifact_path"] = _write_keyframe_review_artifact(project_dir, scene, review)
    return _save(project_dir, state)


def adopt_ai_scene_visual(project_dir: Path, scene_id: str) -> dict:
    """Adopt the reviewed AI keyframe as this scene's actual visual source.

    AI keyframes start as review evidence.  This explicit adoption step turns
    the first-frame image into the selected visual U-xxx record so the local
    review video and final render consume the AI asset rather than an old
    stock-video usage.  The old usage remains in the ledger, deselected, and
    is therefore safely reversible.
    """
    state = _load_for_write(project_dir)
    scene = _find(state["scenes"], scene_id, "场景")
    if scene.get("source_strategy") != "ai_generated":
        raise WorkbenchError("请先将本场景的主体画面来源设为 AI 生成")
    review = scene.get("keyframe_review") if isinstance(scene.get("keyframe_review"), dict) else {}
    timeline = review.get("timeline") if isinstance(review.get("timeline"), list) else []
    first_frame = next((item for item in timeline if item.get("anchor_kind") == "first_frame"), None)
    candidate = scene.get("ai_visual_candidate") if isinstance(scene.get("ai_visual_candidate"), dict) else {}
    asset_id = str(candidate.get("asset_id") or "")
    if not asset_id and first_frame:
        asset_id = str(first_frame.get("asset_id") or "")

    # Backward-compatible recovery for projects generated before the durable
    # candidate record existed.  Prefer the latest AI first frame for exactly
    # this scene; never guess across scenes or use local avatar composites.
    asset = next((item for item in state["assets"] if item.get("id") == asset_id), None)
    if not asset or str(asset.get("source_type") or "") != "ai_generated":
        asset = next((
            item for item in reversed(state["assets"])
            if str(item.get("source_type") or "") == "ai_generated"
            and isinstance(item.get("generation"), dict)
            and item["generation"].get("scene_id") == scene_id
            and item["generation"].get("anchor_kind") == "first_frame"
        ), None)
    if not asset:
        raise WorkbenchError("未找到本场景可采用的 AI 首帧，请先生成 AI 主体画面")
    asset_id = str(asset["id"])
    usage = _append_selected_usage(state, scene_id, asset_id, "visual")
    _set_single_visual_block(state, scene, asset)
    if first_frame and str(first_frame.get("asset_id") or "") == asset_id:
        first_frame["usage_id"] = usage["id"]
    scene["ai_visual_candidate"] = {
        "asset_id": asset_id,
        "review_id": str(candidate.get("review_id") or review.get("id") or ""),
        "status": "adopted",
        "adopted_at": _now(),
        "usage_id": usage["id"],
    }
    scene["visual_fit"] = _visual_fit_plan(project_dir, state, scene, asset)
    _invalidate_scene_review_preview(scene, "已采用 AI 主体画面，正在等待刷新本段审核预览")
    scene["review_status"] = "needs_adjustment"
    _mark_render_needs_refresh(state, f"{scene_id} 已采用 AI 主体画面")
    _decision(state, "asset_usage", f"{scene_id} visual", asset_id, f"AI 主体画面采用；使用编号 {usage['id']}")
    _activity(state, "ai_visual_adopted", f"场景 {scene_id} 已采用 AI 首帧 {asset_id} 作为主体画面（{usage['id']}），旧素材保留在台账中可回退", scene_id=scene_id, asset_id=asset_id, usage_id=usage["id"])
    return _save(project_dir, state)


def _srt_timestamp(seconds: float) -> str:
    milliseconds = max(0, round(float(seconds) * 1000))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def _write_avatar_review_subtitles(project_dir: Path, state: dict) -> Path:
    """Materialise the same phrase cues shown by each scene review preview.

    A keyframe is an image-review anchor, not a subtitle unit.  Older builds
    accidentally treated its full-scene caption as one SRT cue, while the
    scene player displayed the short ``review_preview.caption_cues``.  The
    full preview therefore failed the workbench's WYSIWYG contract.  Prefer
    the persisted review cues when they are current, and deterministically
    rebuild the same phrase cues when a preview has not been materialised yet.
    """
    cues: list[dict] = []
    for scene in sorted(state.get("scenes", []), key=lambda item: _as_number(item.get("start_seconds"))):
        start = _rounded_seconds(scene.get("start_seconds"))
        end = max(start + 0.04, _rounded_seconds(scene.get("end_seconds")))
        duration = end - start
        preview = scene.get("review_preview") if isinstance(scene.get("review_preview"), dict) else {}
        preview_cues = preview.get("caption_cues") if preview.get("status") == "ready" and isinstance(preview.get("caption_cues"), list) else []
        accepted = 0
        for index, item in enumerate(preview_cues):
            caption = _subtitle_cue_text(scene, index, item.get("text"))
            relative_start = _as_number(item.get("start_seconds"))
            relative_end = _as_number(item.get("end_seconds"))
            if not caption or relative_end <= relative_start:
                continue
            cue_start = max(start, min(end - 0.04, start + relative_start))
            cue_end = max(cue_start + 0.04, min(end, start + relative_end))
            cues.append({"start_seconds": cue_start, "end_seconds": cue_end, "text": caption})
            accepted += 1
        if accepted:
            continue

        text = _scene_text(project_dir, state, scene)
        cues.extend(_subtitle_cues(scene, text, duration_seconds=duration))
    output = project_dir / "renders" / "avatar" / "avatar-review-subtitles.srt"
    return _write_srt(output, cues)


def _append_selected_usage(state: dict, scene_id: str, asset_id: str, role: str) -> dict:
    """Create an append-only U-xxx record while de-selecting an old role."""
    for usage in state.get("usages", []):
        if usage.get("scene_id") == scene_id and usage.get("role") == role:
            usage["selected"] = False
    usage = {
        "id": _numbered("U-", state["usages"], "id"),
        "asset_id": asset_id,
        "scene_id": scene_id,
        "role": role,
        "selected": True,
        "transform": {"crop": None, "scale": 1, "speed": 1},
        "created_at": _now(),
    }
    state["usages"].append(usage)
    return usage


def _set_single_visual_block(state: dict, scene: dict, asset: dict) -> None:
    """Make an explicit scene-level choice the sole full-duration visual."""
    old = scene.get("visual_timeline") if isinstance(scene.get("visual_timeline"), dict) else {}
    scene["visual_timeline"] = {
        "version": 1,
        "revision": int(_as_number(old.get("revision"), 0)) + 1,
        "blocks": [{
            "id": "VB-001", "start_seconds": 0.0, "end_seconds": _rounded_seconds(_scene_duration(scene)),
            "source_mode": _visual_source_mode(asset), "asset_id": asset.get("id"),
            "label": str(asset.get("name") or asset.get("id") or "主体画面")[:160],
        }],
        "updated_at": _now(),
    }


def _promote_scene_narration_version(state: dict, scene: dict, version_id: str) -> None:
    """Make a generated take the selected project narration for one scene."""
    narration = scene.get("narration") if isinstance(scene.get("narration"), dict) else _scene_narration_default()
    scene["narration"] = narration
    version = next((item for item in narration.get("versions", []) if item.get("id") == version_id), None)
    if not version:
        raise WorkbenchError("未找到待采用的场景配音版本")
    for item in narration.get("versions", []):
        if item.get("id") != version_id and item.get("status") == "current":
            item["status"] = "superseded"
    version["status"] = "current"
    version["promoted_at"] = _now()
    narration["current_version_id"] = version_id
    narration["candidate_version_id"] = None
    narration["status"] = "ready"
    narration["text"] = str(version.get("text") or narration.get("text") or "")
    if version.get("asset_id"):
        _append_selected_usage(state, scene["id"], str(version["asset_id"]), "narration")


def _current_narration_version(scene: dict) -> dict | None:
    narration = scene.get("narration") if isinstance(scene.get("narration"), dict) else {}
    current_id = narration.get("current_version_id")
    return next((item for item in narration.get("versions", []) if item.get("id") == current_id), None)


def _commit_narration_timeline(state: dict, *, reason: str) -> dict:
    """Commit measured natural take durations as the project's primary clock."""
    durations: dict[str, float] = {}
    for scene in state.get("scenes", []):
        if not isinstance(scene, dict):
            continue
        version = _current_narration_version(scene)
        duration = _as_number((version or {}).get("duration_seconds"))
        if duration > 0:
            durations[str(scene.get("id"))] = _rounded_seconds(duration)
            timing = _scene_timing(scene)
            timing["voice_duration_seconds"] = _rounded_seconds(duration)
            timing["duration_source"] = "narration"
    if not durations:
        raise WorkbenchError("没有可测量的项目旁白，无法建立音频主时间轴")
    update = _build_timeline_update(state, durations, reason=reason)
    _apply_timeline_update(state, update)
    return update


def _rebuild_project_narration_from_scene_versions(project_dir: Path, state: dict) -> tuple[Path | None, Path]:
    """Synchronise promoted per-scene audio with future full renders.

    A local composition is the immediate preview.  Rebuilding this project
    narration manifest makes a later full render start from the same adopted
    takes instead of silently falling back to the old global WAV.
    """
    parts: list[Path] = []
    for scene in state.get("scenes", []):
        narration = scene.get("narration") if isinstance(scene.get("narration"), dict) else {}
        current = next((item for item in narration.get("versions", []) if item.get("id") == narration.get("current_version_id")), None)
        raw_path = (current or {}).get("audio_path")
        if not raw_path:
            return None, _write_subtitles(project_dir, state.get("scenes", []), _script_sections(project_dir, state))
        path = project_dir / str(raw_path)
        if not path.is_file():
            return None, _write_subtitles(project_dir, state.get("scenes", []), _script_sections(project_dir, state))
        parts.append(path)
    subtitle_path = _write_subtitles(project_dir, state.get("scenes", []), _script_sections(project_dir, state))
    return (_concat_audio(project_dir, parts) if parts else None), subtitle_path


def _script_sections(project_dir: Path, state: dict) -> dict[str, dict]:
    script = _read_json(project_dir / "artifacts" / "script.json") or {}
    if not isinstance(script, dict):
        script = {}
    draft = state.get("project", {}).get("script_draft") or {}
    if not script and isinstance(draft, dict):
        script = draft.get("script") or {}
    sections = script.get("sections") if isinstance(script.get("sections"), list) else []
    return {str(section.get("id")): section for section in sections if isinstance(section, dict) and section.get("id")}


def _stock_refresh_query(base_query: str, attempt: int) -> str:
    """Return a deliberately different stock-coverage angle for a scene retry.

    Pexels returns the first matching item for a query.  Repeating the same
    query therefore tends to reproduce the same visual language (and can even
    reproduce the same clip).  A scene-level refresh rotates through adjacent
    coverage angles before moving to the next search page.
    """
    alternatives = {
        "person reading book desk lamp": (
            "hands turning book pages close up",
            "person writing notes beside open book",
            "bookshelf library close up",
            "student studying books desk",
        ),
        "writing notes notebook book": (
            "hands highlighting book pages close up",
            "student organizing study notes desk",
            "library bookshelf detail",
            "person reading book by window",
        ),
        "person reading book close up": (
            "hands writing notes in notebook close up",
            "student studying books desk",
            "library bookshelf browsing books",
            "person reading book by window",
        ),
        "student studying books desk": (
            "hands writing study notes close up",
            "person reading book library aisle",
            "books on desk overhead view",
            "quiet home reading by window",
        ),
        "daily reading routine cozy home": (
            "morning reading book coffee home",
            "hands turning book pages cozy home",
            "bookshelf close up warm light",
            "student writing reading notes desk",
        ),
        "thoughtful person learning at desk": (
            "hands writing notes desk close up",
            "library bookshelf browsing books",
            "student studying books desk",
            "person reading book by window",
        ),
    }
    choices = alternatives.get(base_query)
    if not choices:
        return base_query
    return choices[(max(1, attempt) - 1) % len(choices)]


def _stock_query_for_scene(scene: dict, *, surrounding_context: str = "") -> tuple[str, str]:
    """Produce a compact English Pexels query from a Chinese script beat.

    The automatic path intentionally favours recognisable documentary b-roll
    rather than pretending a generic stock library can reproduce every
    abstract instruction.  The original Chinese context remains in metadata
    for the reviewer and later hot-swap decisions.
    """
    refresh_instruction = str(scene.get("asset_refresh_instruction") or "").strip()
    text = " ".join(
        str(scene.get(key) or "")
        for key in ("title", "description", "shot_intent")
    )
    original_context = f"{surrounding_context} {text} {refresh_instruction}".strip().lower()
    lowered = original_context.lower()
    rules = (
        (("科技", "ai", "人工智能", "机器人", "手机", "芯片"), "technology news smartphone robot artificial intelligence"),
        (("口播", "解说", "观点", "采访"), "modern studio microphone discussion close up"),
        (("台灯", "夜晚", "晚上"), "person reading book desk lamp"),
        (("目录", "重点", "笔记", "总结", "写下", "写"), "writing notes notebook book"),
        (("读书", "阅读", "书", "章节"), "person reading book close up"),
        (("学习", "学生", "知识"), "student studying books desk"),
        (("习惯", "十分钟", "坚持"), "daily reading routine cozy home"),
    )
    for keywords, query in rules:
        if any(str(keyword).lower() in lowered for keyword in keywords):
            base_query = query
            break
    else:
        base_query = "thoughtful person learning at desk"
    refresh_count = max(0, int(_as_number(scene.get("asset_refresh_count"))))
    query = _stock_refresh_query(base_query, refresh_count) if refresh_count else base_query
    return query, original_context[:500]


def _scene_surrounding_context(state: dict, scene: dict) -> str:
    """Give stock search enough continuity without leaking an entire script."""
    ordered = sorted((item for item in state.get("scenes", []) if isinstance(item, dict)), key=lambda item: int(_as_number(item.get("order"))))
    index = next((position for position, item in enumerate(ordered) if item.get("id") == scene.get("id")), -1)
    neighbours = ordered[max(0, index - 1): index + 2] if index >= 0 else [scene]
    return " ".join(" ".join(str(item.get(key) or "") for key in ("title", "description", "shot_intent")) for item in neighbours)


def _render_dimensions(project_dir: Path, state: dict) -> tuple[int, int]:
    project = _read_json(project_dir / "project.json") or {}
    profile = project.get("render_profile") if isinstance(project, dict) else {}
    if isinstance(profile, dict):
        width = int(_as_number(profile.get("width"), 0))
        height = int(_as_number(profile.get("height"), 0))
        if width > 0 and height > 0:
            return width, height
    aspect = _normalize_intake(state.get("project", {}).get("intake")).get("aspect")
    return (1080, 1920) if aspect in {"portrait", "vertical"} else (1920, 1080)


def _avatar_source_for_scene(project_dir: Path, scene: dict) -> tuple[Path, float, float]:
    presenter = _scene_presenter(scene)
    raw_path = str(presenter.get("source_path") or "")
    if not raw_path:
        raise WorkbenchError(f"{scene.get('id')} 尚未绑定数字人母版，请先应用数字人真实时间线")
    try:
        source = (project_dir / raw_path).resolve()
        source.relative_to(project_dir.resolve())
    except (OSError, ValueError) as exc:
        raise WorkbenchError(f"{scene.get('id')} 的数字人素材路径无效") from exc
    if not source.is_file():
        raise WorkbenchError(f"{scene.get('id')} 的数字人母版版本已缺失")
    start = _rounded_seconds(presenter.get("source_start_seconds"))
    end = _rounded_seconds(presenter.get("source_end_seconds"))
    if end <= start:
        raise WorkbenchError(f"{scene.get('id')} 的数字人片段没有有效的原声边界")
    return source, start, end


def _avatar_pip_geometry(project_dir: Path, state: dict, scene: dict, source: Path) -> dict:
    """Resolve a no-distortion presenter box from its template or local edit."""
    width, height = _render_dimensions(project_dir, state)
    info = _probe_video(source, _ffmpeg_available()) or {}
    video = next((item for item in info.get("streams", []) if item.get("codec_type") == "video"), {})
    source_width = max(1, int(video.get("width") or 9))
    source_height = max(1, int(video.get("height") or 16))
    presenter = _scene_presenter(scene)
    layout = _presenter_layout(state, presenter)
    normalized = layout["geometry"]
    shape = _normalized_presenter_shape(layout.get("shape"))
    crop_bottom = _normalized_presenter_crop_bottom(layout.get("crop_bottom"))
    face_crop = _normalized_presenter_face_crop(layout.get("face_crop"))
    cropped_source_height = max(2, int(source_height * (1.0 - crop_bottom)) // 2 * 2)
    crop_width = source_width
    crop_height = cropped_source_height
    crop_x = 0
    crop_y = 0
    if shape == "circle":
        # The default remains the historic upper-centre crop.  A user can
        # instead pan the square source window and zoom it in around a face;
        # all values remain source crops followed by a proportional scale.
        side = max(2, min(source_width, cropped_source_height) // 2 * 2)
        crop_width = max(2, int(side / face_crop["zoom"]) // 2 * 2)
        crop_height = crop_width
        max_crop_x = max(0, source_width - crop_width)
        max_crop_y = max(0, cropped_source_height - crop_height)
        crop_x = max(0, min(max_crop_x, int(round(max_crop_x * face_crop["x"])) // 2 * 2))
        crop_y = max(0, min(max_crop_y, int(round(max_crop_y * face_crop["y"])) // 2 * 2))
    requested_width = max(2, int(width * normalized["width"]) // 2 * 2)
    max_height = int(height * .72)
    scale = min(requested_width / crop_width, max_height / crop_height)
    pip_width = max(2, int(crop_width * scale) // 2 * 2)
    pip_height = max(2, int(crop_height * scale) // 2 * 2)
    margin_x = max(0, min(width - pip_width, round(width * normalized["x"])))
    # Do not place the rectangle in the responsive subtitle safe area.
    margin_y = max(0, min(round(height * .72) - pip_height, round(height * normalized["y"])))
    return {
        "x": margin_x,
        "y": margin_y,
        "width": pip_width,
        "height": pip_height,
        "canvas_width": width,
        "canvas_height": height,
        "layout_template_id": layout["template_id"],
        "layout_template_name": layout["template_name"],
        "normalized_geometry": normalized,
        "crop_bottom": crop_bottom,
        "shape": shape,
        "face_crop": face_crop,
        "source_width": source_width,
        "source_height": source_height,
        "cropped_source_height": cropped_source_height,
        "crop_width": crop_width,
        "crop_height": crop_height,
        "crop_x": crop_x,
        "crop_y": crop_y,
    }


def _avatar_shape_filter(shape: str, width: int, height: int) -> str:
    """Return an alpha mask for direct preview/keyframe compositions."""
    if shape == "circle":
        expression = "if(lte((X-W/2)*(X-W/2)+(Y-H/2)*(Y-H/2),(min(W,H)/2)*(min(W,H)/2)),255,0)"
    elif shape == "rounded":
        radius = max(4, int(min(width, height) * .08))
        expression = (
            f"if(lte(hypot(max(abs(X-W/2)-(W/2-{radius}),0),"
            f"max(abs(Y-H/2)-(H/2-{radius}),0)),{radius}),255,0)"
        )
    else:
        return ""
    return f"format=rgba,geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':a='{expression}'"


def _avatar_pip_filter(geometry: dict, *, fps: int | None = None, duration: float | None = None) -> str:
    """Build one no-distortion PiP filter shared by preview and final render."""
    parts: list[str] = []
    source_width = int(geometry.get("source_width") or 0)
    source_height = int(geometry.get("source_height") or 0)
    crop_width = int(geometry.get("crop_width") or source_width)
    crop_height = int(geometry.get("crop_height") or geometry.get("cropped_source_height") or source_height)
    crop_x = int(geometry.get("crop_x") or 0)
    crop_y = int(geometry.get("crop_y") or 0)
    if source_width and source_height and (crop_width < source_width or crop_height < source_height or crop_x or crop_y):
        width_expression = "iw" if crop_width == source_width and crop_x == 0 else str(crop_width)
        parts.append(f"crop={width_expression}:{crop_height}:{crop_x}:{crop_y}")
    parts.extend([f"scale={int(geometry['width'])}:{int(geometry['height'])}", "setsar=1"])
    parts.append(AVATAR_PIP_FACE_LIGHTING_FILTER)
    shape_filter = _avatar_shape_filter(
        _normalized_presenter_shape(geometry.get("shape"), "rectangle"),
        int(geometry["width"]),
        int(geometry["height"]),
    )
    if shape_filter:
        parts.append(shape_filter)
    if fps:
        parts.append(f"fps={int(fps)}")
    if duration is not None:
        parts.extend([f"trim=duration={duration:.3f}", "setpts=PTS-STARTPTS"])
    return ",".join(parts)


def _review_preview_signature(project_dir: Path, state: dict, scene: dict) -> str:
    """Identify the actual inputs of one local review preview.

    A preview is a cache, never a source of truth.  The signature prevents an
    old full-screen or PiP clip from being presented after the reviewer changes
    material, time boundaries, presenter geometry, or a surgical directive.
    """
    presenter = _scene_presenter(scene)
    visual = _selected_visual_asset(state, str(scene.get("id") or ""))
    _ensure_scene_visual_state(state, scene)
    assets = {str(item.get("id")): item for item in state.get("assets", []) if isinstance(item, dict)}
    visual_timeline = []
    for block in (scene.get("visual_timeline") or {}).get("blocks") or []:
        asset = assets.get(str(block.get("asset_id") or "")) or {}
        visual_timeline.append({
            "block": block,
            "path": asset.get("path"),
            "version": next((item.get("id") for item in (asset.get("versions") or []) if item.get("status") == "current"), None),
        })
    composition = _ensure_scene_visual_composition(scene)
    composition_assets = {}
    for overlay in composition.get("overlays") or []:
        if not isinstance(overlay, dict):
            continue
        asset = assets.get(str(overlay.get("asset_id") or "")) or {}
        composition_assets[str(overlay.get("asset_id") or "")] = {
            "path": asset.get("path"),
            "version": next((item.get("id") for item in (asset.get("versions") or []) if item.get("status") == "current"), None),
        }
    return _json_hash({
        "scene": scene.get("id"),
        "bounds": [scene.get("start_seconds"), scene.get("end_seconds")],
        "render_dimensions": _render_dimensions(project_dir, state),
        "frame_rate": int(state.get("settings", {}).get("frame_rate") or 30),
        "presenter": {
            "treatment": presenter.get("treatment"),
            "source_path": presenter.get("source_path"),
            "source_start_seconds": presenter.get("source_start_seconds"),
            "source_end_seconds": presenter.get("source_end_seconds"),
            "layout": _presenter_layout(state, presenter),
        },
        "visual": {
            "id": (visual or {}).get("id"),
            "path": (visual or {}).get("path"),
            "type": (visual or {}).get("type"),
            "version": next((item.get("id") for item in ((visual or {}).get("versions") or []) if item.get("status") == "current"), None),
        },
        "visual_timeline": visual_timeline,
        "visual_composition": {
            **_visual_composition_render_contract(composition),
            "assets": composition_assets,
        },
        "story_headline": {
            "story_id": str(scene.get("story_id") or ""),
            "content": deepcopy(scene.get("headline_overlay") or {}),
            "layout": deepcopy(_ensure_story_headline_layout(state)),
        },
        "directives": scene.get("surgical_directives") or [],
    })


def _review_directive_time(scene: dict, raw_value: Any) -> float:
    start = _rounded_seconds(scene.get("start_seconds"))
    end = max(start + 0.04, _rounded_seconds(scene.get("end_seconds"), start + 0.04))
    return round(min(end - 0.04, max(start, _as_number(raw_value, start))), 3)


def _review_directive_duration(scene: dict, raw_value: Any) -> float:
    return round(min(max(0.5, _scene_duration(scene)), max(0.5, _as_number(raw_value, 2.5))), 3)


def _escape_drawtext(value: str) -> str:
    return value.replace("\\", r"\\").replace("'", r"\'").replace(":", r"\:").replace("%", r"\%").replace("\n", " ")


def _directive_filter_chain(project_dir: Path, state: dict, directives: list[dict], *, global_time: bool = False) -> str:
    """Build small, legible overlay effects from approved review directives.

    The component library is intentionally bounded: it serves directed review
    without turning the workbench into a hand-built motion graphics editor.
    The same filter is used for a scene preview and for a final render.
    """
    width, height = _render_dimensions(project_dir, state)
    font = Path(os.environ.get("WINDIR", r"C:\\Windows")) / "Fonts" / "msyh.ttc"
    font_value = str(font).replace("\\", "/").replace(":", r"\:") if font.is_file() else ""
    filters: list[str] = []
    for directive in directives:
        kind = str(directive.get("component_type") or "")
        if kind not in SURGICAL_COMPONENT_TYPES:
            continue
        start = _as_number(directive.get("start_seconds"))
        if not global_time:
            start -= _as_number(directive.get("scene_start_seconds"))
        duration = max(0.5, _as_number(directive.get("duration_seconds"), 2.5))
        end = start + duration
        enable = f"between(t\\,{start:.3f}\\,{end:.3f})"
        position = str(directive.get("position") or "lower_third")
        if position == "top_left":
            x, y = int(width * .055), int(height * .11)
        elif position == "top_right":
            x, y = int(width * .60), int(height * .11)
        elif position == "center":
            x, y = int(width * .17), int(height * .42)
        else:
            x, y = int(width * .10), int(height * .60)
        label = _escape_drawtext(str(directive.get("text") or ""))
        if kind == "focus_box":
            box = directive.get("box") if isinstance(directive.get("box"), dict) else {}
            box_x = int(width * min(.88, max(.02, _as_number(box.get("x"), .20))))
            box_y = int(height * min(.78, max(.02, _as_number(box.get("y"), .20))))
            box_w = int(width * min(.90, max(.08, _as_number(box.get("width"), .58))))
            box_h = int(height * min(.62, max(.08, _as_number(box.get("height"), .36))))
            filters.append(f"drawbox=x={box_x}:y={box_y}:w={box_w}:h={box_h}:color=0x55D8FF@0.96:t=5:enable='{enable}'")
            continue
        if kind == "info_label":
            box_w, box_h, font_size = int(width * .30), int(height * .075), max(22, int(height * .026))
        else:
            box_w, box_h, font_size = int(width * .72), int(height * .12), max(24, int(height * .029))
        filters.append(f"drawbox=x={x}:y={y}:w={box_w}:h={box_h}:color=0x07111f@0.84:t=fill:enable='{enable}'")
        if label:
            font_part = f"fontfile='{font_value}':" if font_value else ""
            filters.append(f"drawtext={font_part}text='{label}':x={x + 20}:y={y + max(12, (box_h - font_size) // 2)}:fontsize={font_size}:fontcolor=white:enable='{enable}'")
    return ",".join(filters)


def _scene_directive_filters(project_dir: Path, state: dict, scene: dict) -> str:
    directives = []
    for item in scene.get("surgical_directives") or []:
        if isinstance(item, dict):
            directives.append({**item, "scene_start_seconds": scene.get("start_seconds")})
    return _directive_filter_chain(project_dir, state, directives, global_time=False)


def _project_directive_filters(project_dir: Path, state: dict) -> str:
    directives: list[dict] = []
    for scene in state.get("scenes", []):
        if isinstance(scene, dict):
            directives.extend(item for item in (scene.get("surgical_directives") or []) if isinstance(item, dict))
    return _directive_filter_chain(project_dir, state, directives, global_time=True)


def _apply_surgical_directives_to_video(project_dir: Path, state: dict, source: Path, output: Path, ffmpeg: str | None) -> list[str]:
    """Render saved review components into the final deliverable when needed."""
    directives = [
        item for scene in state.get("scenes", []) if isinstance(scene, dict)
        for item in (scene.get("surgical_directives") or []) if isinstance(item, dict)
    ]
    if not directives:
        return []
    if not ffmpeg:
        raise WorkbenchError("本机未发现 FFmpeg，无法将审核组件写入成片")
    filters = _project_directive_filters(project_dir, state)
    if not filters:
        return []
    temporary = output.with_name(f".{output.stem}-effects-{uuid4().hex[:8]}{output.suffix}")
    ok, detail = _run_media([
        ffmpeg, "-y", "-i", str(source), "-vf", filters,
        "-map", "0:v", "-map", "0:a?", "-c:v", "libx264", "-preset", "medium", "-crf", "20",
        "-c:a", "copy", "-movflags", "+faststart", str(temporary),
    ])
    if not ok or not temporary.is_file():
        try:
            temporary.unlink()
        except OSError:
            pass
        raise WorkbenchError(f"定点组件写入成片失败：{detail}")
    os.replace(temporary, output)
    return [str(item.get("id")) for item in directives]


def _materialize_avatar_overlay_clip(project_dir: Path, state: dict, scene: dict, ffmpeg: str) -> tuple[Path, dict]:
    """Create a silent, exact-duration avatar visual clip for a PiP overlay."""
    source, source_start, source_end = _avatar_source_for_scene(project_dir, scene)
    geometry = _avatar_pip_geometry(project_dir, state, scene, source)
    duration = max(0.04, source_end - source_start)
    revision = int((state.get("timeline") or {}).get("revision") or 0)
    fingerprint = _json_hash({
        "source": str(source), "start": source_start, "end": source_end,
        "geometry": geometry, "revision": revision,
        "face_lighting": AVATAR_PIP_FACE_LIGHTING_VERSION,
    })[:12]
    output = project_dir / "renders" / "avatar" / "scene-overlays" / f"{scene['id']}-{fingerprint}.mp4"
    if output.is_file() and _probe_duration_seconds(output, ffmpeg, 0) >= duration - 0.08:
        return output, geometry
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.stem}-{uuid4().hex[:8]}{output.suffix}")
    filters = _avatar_pip_filter(
        geometry,
        fps=int(state.get("settings", {}).get("frame_rate") or 30),
        duration=duration,
    )
    ok, detail = _run_media([
        ffmpeg, "-y", "-ss", f"{source_start:.6f}", "-t", f"{duration:.6f}", "-i", str(source),
        "-an", "-vf", filters, "-c:v", "libx264", "-preset", "medium", "-crf", "18",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(temporary),
    ])
    if not ok or not temporary.is_file():
        try:
            temporary.unlink()
        except OSError:
            pass
        raise WorkbenchError(f"无法生成 {scene.get('id')} 的数字人画中画片段：{detail}")
    os.replace(temporary, output)
    return output, geometry


_SDR_BT709_COLOR_CONTRACT_VERSION = "sdr-bt709-tone-map-v1"
_HDR_TRANSFER_CHARACTERISTICS = {"arib-std-b67", "smpte2084"}
_HDR_TO_SDR_BT709_FILTER = (
    "zscale=transfer=linear:npl=100,format=gbrpf32le,"
    "tonemap=tonemap=hable:desat=0,"
    "zscale=primaries=bt709:transfer=bt709:matrix=bt709:range=tv,format=yuv420p"
)
_SDR_BT709_TAG_FILTER = (
    "setparams=colorspace=bt709:color_primaries=bt709:color_trc=bt709:range=tv"
)


def _video_color_metadata(path: Path, ffmpeg: str | None = None) -> dict[str, str]:
    """Read only the color fields needed to choose the composition contract."""
    probe = _ffprobe_available(ffmpeg)
    if not probe:
        return {}
    ok, output = _run_media([
        probe, "-v", "error", "-select_streams", "v:0", "-show_entries",
        "stream=color_range,color_space,color_transfer,color_primaries", "-of", "json", str(path),
    ])
    if not ok:
        return {}
    try:
        streams = (json.loads(output).get("streams") or [])
    except (json.JSONDecodeError, TypeError, AttributeError):
        return {}
    stream = streams[0] if streams and isinstance(streams[0], dict) else {}
    return {
        key: str(stream.get(key) or "").strip().lower()
        for key in ("color_range", "color_space", "color_transfer", "color_primaries")
        if stream.get(key)
    }


def _source_to_sdr_bt709_filter(source: Path, ffmpeg: str | None = None) -> str:
    """Return the one color conversion allowed before a source enters composition.

    The delivery contract is SDR BT.709.  HLG/PQ input must be tone-mapped
    before it is concatenated with SDR clips; merely copying its HDR metadata
    makes the whole H.264 preview advertise HLG, which darkens SDR overlays in
    common players.  Existing SDR clips are not tone-mapped or brightened.
    """
    metadata = _video_color_metadata(source, ffmpeg)
    if metadata.get("color_transfer") in _HDR_TRANSFER_CHARACTERISTICS:
        return _HDR_TO_SDR_BT709_FILTER
    return _SDR_BT709_TAG_FILTER


def _review_background_filter(
    asset: dict,
    width: int,
    height: int,
    duration: float,
    *,
    source_color_filter: str = "",
) -> str:
    """Create a cover-fitted, SDR scene-duration background branch for review."""
    filters = [source_color_filter] if source_color_filter else []
    filters.extend([
        f"scale={width}:{height}:force_original_aspect_ratio=increase",
        f"crop={width}:{height}",
        "setsar=1",
        f"trim=duration={duration:.3f}",
        "setpts=PTS-STARTPTS",
    ])
    return ",".join(filters)


def _materialize_scene_visual_timeline(project_dir: Path, state: dict, scene: dict, ffmpeg: str) -> Path:
    """Render the authoritative multi-source visual track as one silent clip."""
    _ensure_scene_visual_state(state, scene)
    blocks = list((scene.get("visual_timeline") or {}).get("blocks") or [])
    if not blocks:
        raise WorkbenchError(f"{scene.get('id')} 没有可合成的视觉时间线")
    assets = {str(item.get("id")): item for item in state.get("assets", []) if isinstance(item, dict)}
    width, height = _render_dimensions(project_dir, state)
    fps = int(state.get("settings", {}).get("frame_rate") or 30)
    signature = _json_hash({
        "scene": scene.get("id"), "blocks": blocks, "dimensions": [width, height], "fps": fps,
        "color_contract": _SDR_BT709_COLOR_CONTRACT_VERSION,
        "assets": {asset_id: {"path": item.get("path"), "versions": item.get("versions")} for asset_id, item in assets.items() if asset_id in {str(block.get('asset_id')) for block in blocks}},
    })
    output = project_dir / "renders" / "visual-timelines" / f"{scene['id']}-{signature[:12]}.mp4"
    duration = _scene_duration(scene)
    if output.is_file() and _probe_duration_seconds(output, ffmpeg, 0) >= duration - .12:
        return output
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.stem}-{uuid4().hex[:8]}{output.suffix}")
    # `-loop 1` / `-stream_loop -1` intentionally make every source long
    # enough for a scene.  Some FFmpeg builds can keep the graph alive after
    # `trim` when the source is a still image, so cap the encoded frame count
    # as a second, deterministic stop condition.
    output_frames = max(1, int(math.ceil(duration * fps)))
    command: list[str] = [ffmpeg, "-y"]
    filters: list[str] = []
    labels: list[str] = []
    for index, block in enumerate(blocks):
        asset = assets.get(str(block.get("asset_id") or ""))
        if not asset or not asset.get("path"):
            raise WorkbenchError(f"{scene.get('id')} 的视觉区间 {block.get('id')} 缺少素材")
        source = (project_dir / str(asset.get("path"))).resolve()
        try:
            source.relative_to(project_dir.resolve())
        except ValueError as exc:
            raise WorkbenchError("视觉时间线素材不在当前项目目录内") from exc
        if not source.is_file():
            raise WorkbenchError(f"视觉区间 {block.get('id')} 的素材文件不存在")
        block_duration = max(.04, _as_number(block.get("end_seconds")) - _as_number(block.get("start_seconds")))
        if str(asset.get("type") or "").lower() == "image":
            # Fail before starting a looped FFmpeg image input.  A damaged or
            # placeholder PNG otherwise may keep a looping decoder busy until
            # the generic media timeout, which is a terrible batch UX.
            try:
                from PIL import Image
                with Image.open(source) as image:
                    image.verify()
            except Exception as exc:
                raise WorkbenchError(f"视觉区间 {block.get('id')} 的图片素材无法读取，请重新选择素材") from exc
            # Bound the *input* as well as the filter/output.  This avoids an
            # FFmpeg edge case where a looped still can keep `fps` alive after
            # a downstream trim has emitted the requested frame range.
            command.extend(["-loop", "1", "-framerate", str(fps), "-t", f"{block_duration:.6f}", "-i", str(source)])
        else:
            # 本地连续动作必须使用已确认的源时间窗。不能循环，否则会把
            # 已确认的动作重复播放，或让完整动作看起来抽搐。
            if "source_in_seconds" in block and "source_out_seconds" in block:
                command.extend([
                    "-ss", f"{_as_number(block.get('source_in_seconds')):.6f}",
                    "-t", f"{block_duration:.6f}", "-i", str(source),
                ])
            else:
                command.extend(["-stream_loop", "-1", "-t", f"{block_duration:.6f}", "-i", str(source)])
        label = f"v{index}"
        color_filter = _source_to_sdr_bt709_filter(source, ffmpeg)
        filters.append(
            f"[{index}:v]{_review_background_filter(asset, width, height, block_duration, source_color_filter=color_filter)},fps={fps}[{label}]"
        )
        labels.append(f"[{label}]")
    if len(labels) == 1:
        filters.append(f"{labels[0]}null[vout]")
    else:
        filters.append(f"{''.join(labels)}concat=n={len(labels)}:v=1:a=0[vout]")
    command.extend([
        "-filter_complex", ";".join(filters), "-map", "[vout]", "-an", "-t", f"{duration:.6f}", "-frames:v", str(output_frames),
        "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p",
        "-movflags", "+faststart", str(temporary),
    ])
    ok, detail = _run_media(command)
    if not ok or not temporary.is_file():
        try:
            temporary.unlink()
        except OSError:
            pass
        raise WorkbenchError(f"{scene.get('id')} 的视觉时间线合成失败：{detail}")
    os.replace(temporary, output)
    return output


def _materialize_focus_video_source(
    project_dir: Path,
    source: Path,
    source_in_frame: int,
    source_out_frame: int,
    fps: int,
    ffmpeg: str,
) -> Path:
    """Cache one silent CFR source window before Remotion frame decoding."""
    frame_count = max(1, source_out_frame - source_in_frame)
    duration = frame_count / fps
    stat = source.stat()
    signature = _json_hash({
        "path": str(source.resolve()),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "source_in_frame": source_in_frame,
        "source_out_frame": source_out_frame,
        "fps": fps,
        # v3 drops auxiliary data streams. Some phone/app MP4s carry a
        # timed-data track which Remotion's frame reader can mistake for a
        # video timeline when the clip is trimmed near its physical end.
        "contract": "silent-cfr-v4-video-only-exact-frame-clock",
    })
    output = project_dir / "renders" / "visual-compositions" / "sources" / f"{signature[:16]}.mp4"
    if output.is_file() and _probe_duration_seconds(output, ffmpeg, 0) >= duration - (1 / fps):
        return output
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.stem}-{uuid4().hex[:8]}{output.suffix}")
    command = [
        ffmpeg, "-y",
        "-ss", f"{source_in_frame / fps:.6f}",
        "-t", f"{duration:.6f}",
        "-i", str(source),
        "-map", "0:v:0", "-dn",
        "-map_metadata", "-1", "-map_chapters", "-1", "-write_tmcd", "0",
        "-an",
        # A 25FPS source can begin slightly after zero and finish one 30FPS
        # tick short of the requested source window. Clone only the final
        # decoded frame, then cap the output at the exact timeline frame
        # count. This is frame-clock normalization, never visual looping.
        "-vf", (
            f"fps={fps},tpad=stop_mode=clone:stop_duration={2 / fps:.6f},"
            "scale=trunc(iw/2)*2:trunc(ih/2)*2"
        ),
        "-frames:v", str(frame_count),
        "-c:v", "libx264", "-preset", "fast", "-crf", "18",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart",
        "-avoid_negative_ts", "make_zero",
        str(temporary),
    ]
    ok, detail = _run_media(command)
    measured = _probe_duration_seconds(temporary, ffmpeg, 0) if temporary.is_file() else 0
    if not ok or measured < duration - (1 / fps):
        try:
            temporary.unlink()
        except OSError:
            pass
        raise WorkbenchError(f"重点视频本地规范化失败：{detail}")
    os.replace(temporary, output)
    return output


def _materialize_scene_visual_composition(project_dir: Path, state: dict, scene: dict, ffmpeg: str) -> Path:
    """Render the one authoritative silent content layer for preview and final."""
    base = _materialize_scene_visual_timeline(project_dir, state, scene, ffmpeg)
    composition = _ensure_scene_visual_composition(scene)
    overlays = [item for item in (composition.get("overlays") or []) if isinstance(item, dict)]
    if composition.get("layout_recipe") != "focus_card" or not overlays:
        return base

    width, height = _render_dimensions(project_dir, state)
    fps = int(state.get("settings", {}).get("frame_rate") or 30)
    duration = _scene_duration(scene)
    assets = {str(item.get("id")): item for item in state.get("assets", []) if isinstance(item, dict)}
    used_assets = {}
    layered_overlays: list[dict] = []
    for overlay in overlays:
        asset_id = str(overlay.get("asset_id") or "")
        asset = assets.get(asset_id)
        if not asset or not asset.get("path"):
            raise WorkbenchError(f"{scene.get('id')} 的重点素材 {overlay.get('id')} 已丢失")
        source = (project_dir / str(asset.get("path"))).resolve()
        try:
            source.relative_to(project_dir.resolve())
        except ValueError as exc:
            raise WorkbenchError("重点素材不在当前项目目录内") from exc
        if not source.is_file():
            raise WorkbenchError(f"重点素材 {overlay.get('id')} 的文件不存在")
        asset_type = str(asset.get("type") or "").lower()
        display_start_frame = _nonnegative_frame(overlay.get("start_seconds"), fps)
        display_end_frame = max(display_start_frame + 1, _nonnegative_frame(overlay.get("end_seconds"), fps))
        source_in_frame = _nonnegative_frame(overlay.get("source_in_seconds"), fps)
        source_out_frame = max(source_in_frame + 1, _nonnegative_frame(overlay.get("source_out_seconds"), fps))
        used_assets[asset_id] = {"path": asset.get("path"), "versions": asset.get("versions")}
        render_source = source
        render_source_in_frame = source_in_frame
        render_source_out_frame = source_out_frame
        materialized_video_source = False
        if asset_type == "video" and "playback_rate" in overlay:
            render_source = _materialize_focus_video_source(
                project_dir, source, source_in_frame, source_out_frame, fps, ffmpeg,
            )
            render_source_in_frame = 0
            render_source_out_frame = source_out_frame - source_in_frame
            materialized_video_source = True
        placement = overlay.get("placement") if isinstance(overlay.get("placement"), dict) else None
        source_width, source_height = _asset_resolution(asset)
        rendered_placement = None
        if placement:
            rendered_placement = {
                "presetId": str(placement.get("preset_id") or "source_hero_custom"),
                "positionXRatio": _as_number(placement.get("position_x_ratio"), .5),
                "positionYRatio": _as_number(placement.get("position_y_ratio"), .47),
                "sizeRatio": _as_number(placement.get("size_ratio"), .74),
                "aspectMode": "source",
                "maxHeightRatio": _as_number(placement.get("max_height_ratio"), .78),
                "sourceAspectRatio": (
                    source_width / source_height
                    if source_width > 0 and source_height > 0
                    else width / height
                ),
            }
        legacy_playback_rate = (
            (source_out_frame - source_in_frame) / (display_end_frame - display_start_frame)
        ) if asset_type == "video" else None
        layered_overlays.append({
            "id": overlay.get("id"),
            "role": "hero",
            "src": str(render_source),
            "mediaType": asset_type,
            "startSeconds": _as_number(overlay.get("start_seconds")),
            "endSeconds": _as_number(overlay.get("end_seconds")),
            "trimBeforeSeconds": render_source_in_frame / fps if asset_type == "video" else None,
            # The visible ``endFrame`` is authoritative. Do not make
            # Remotion seek to the normalized window's exclusive upper bound:
            # a source whose container duration exceeds its decodable frame
            # count can otherwise fail after the usable on-screen range.
            "trimAfterSeconds": (
                None if materialized_video_source else render_source_out_frame / fps
            ) if asset_type == "video" else None,
            "trimBeforeFrame": render_source_in_frame if asset_type == "video" else None,
            "trimAfterFrame": (
                None if materialized_video_source else render_source_out_frame
            ) if asset_type == "video" else None,
            "startFrame": display_start_frame,
            "endFrame": display_end_frame,
            "muted": True,
            "playbackRate": (
                _as_number(overlay.get("playback_rate"), 1.0)
                if "playback_rate" in overlay
                else legacy_playback_rate
            ),
            "fit": str(overlay.get("fit") or "contain"),
            "placement": rendered_placement,
        })

    signature = _json_hash({
        "scene": scene.get("id"),
        "base": str(base),
        "composition": _visual_composition_render_contract(composition),
        "assets": used_assets,
        "dimensions": [width, height],
        "fps": fps,
    })
    output = project_dir / "renders" / "visual-compositions" / f"{scene['id']}-{signature[:12]}.mp4"
    if output.is_file() and _probe_duration_seconds(output, ffmpeg, 0) >= duration - .12:
        return output
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.stem}-{uuid4().hex[:8]}{output.suffix}")
    scene_duration_frames = max(1, int(math.ceil(duration * fps)))
    scene_props = {
        "id": f"layered-{scene.get('id')}",
        "kind": "layered",
        "startSeconds": 0,
        "durationSeconds": duration,
        "startFrame": 0,
        "durationFrames": scene_duration_frames,
        "layoutRecipe": "focus_card",
        "background": {
            "src": str(base.resolve()),
            "mediaType": "video",
            "fit": "cover",
            "trimBeforeSeconds": 0,
            "trimAfterSeconds": duration,
            "trimBeforeFrame": 0,
            "trimAfterFrame": scene_duration_frames,
        },
        "overlays": layered_overlays,
        "frameStyle": {
            "widthRatio": _as_number((composition.get("frame_style") or {}).get("width_ratio"), .82),
            "heightRatio": _as_number((composition.get("frame_style") or {}).get("height_ratio"), .56),
            "borderRadiusRatio": _as_number((composition.get("frame_style") or {}).get("border_radius_ratio"), .025),
            "borderColor": str((composition.get("frame_style") or {}).get("border_color") or "#D9F3FF"),
            "shadow": str((composition.get("frame_style") or {}).get("shadow") or "soft"),
        },
    }
    result = VideoCompose().execute({
        "operation": "remotion_render",
        "composition_data": {
            "renderer_family": "layered-content",
            "scenes": [scene_props],
            "canvasWidth": width,
            "canvasHeight": height,
            "frameRate": fps,
            "durationFrames": scene_duration_frames,
        },
        "output_path": str(temporary),
        "remotion_timeout_ms": max(120000, int(math.ceil(duration * 10000))),
    })
    measured = _probe_duration_seconds(temporary, ffmpeg, 0) if temporary.is_file() else 0
    if not result.success or not temporary.is_file() or measured < duration - .12:
        try:
            temporary.unlink()
        except OSError:
            pass
        detail = result.error or f"输出时长仅 {measured:.3f} 秒"
        raise WorkbenchError(f"{scene.get('id')} 的重点素材卡片渲染失败：{detail}")
    os.replace(temporary, output)
    return output


def generate_scene_review_preview(project_dir: Path, scene_id: str) -> dict:
    """Materialise an exact-duration, actual-aspect scene preview locally.

    This deliberately runs only for the scene the reviewer selected.  It
    combines the native avatar audio with the current composition treatment,
    then applies the same bounded surgical components the final render uses.
    """
    state = _load_for_write(project_dir)
    scene = _find(state["scenes"], scene_id, "场景")
    _ensure_scene_review_surface(scene)
    _ensure_scene_visual_state(state, scene)
    ffmpeg = _ffmpeg_available()
    if not ffmpeg:
        raise WorkbenchError("本机未发现 FFmpeg，无法生成片段审核预览")
    presenter = _scene_presenter(scene)
    treatment = str(presenter.get("treatment") or "hidden")
    duration = _scene_duration(scene)
    width, height = _render_dimensions(project_dir, state)
    fps = int(state.get("settings", {}).get("frame_rate") or 30)
    headline_overlays, _ = _daily_story_headline_overlays(project_dir, state, width, height)
    story_id = str(scene.get("story_id") or "")
    story_headline = next(
        (item for item in headline_overlays if str(item.get("story_id") or "") == story_id),
        None,
    )
    visual = _selected_visual_asset(state, scene_id)
    asset_lookup = {str(item.get("id")): item for item in state.get("assets", []) if isinstance(item, dict)}
    visual_blocks = list((scene.get("visual_timeline") or {}).get("blocks") or [])
    source_path: Path | None = None
    source_start = 0.0
    source_end = duration
    has_avatar = _is_avatar_project(state) and presenter.get("source_path")
    if has_avatar:
        source_path, source_start, source_end = _avatar_source_for_scene(project_dir, scene)
        if abs((source_end - source_start) - duration) > .12:
            raise WorkbenchError("本段数字人原声边界与场景时长不一致，请重新应用真实时间线")
    if treatment == "fullscreen" and not source_path:
        raise WorkbenchError("全屏数字人场景缺少原声视频，请先应用真实时间线")
    needs_background = treatment in {"pip_top_left", "custom", "hidden"} or not has_avatar
    if needs_background and not visual_blocks and (not visual or not visual.get("path")):
        raise WorkbenchError("请先为当前场景选择一条主体素材，再生成审核预览")

    signature = _review_preview_signature(project_dir, state, scene)
    output = project_dir / "renders" / "review-previews" / f"{scene_id}-{signature[:12]}.mp4"
    preview = scene["review_preview"]
    if output.is_file() and _probe_duration_seconds(output, ffmpeg, 0) >= duration - .15:
        preview.update({
            "status": "ready", "output_path": _safe_relpath(project_dir, str(output)),
            "input_signature": signature, "duration_seconds": round(duration, 3),
            "resolution": f"{width}x{height}", "generated_at": preview.get("generated_at") or _now(),
            "stale_reason": "", "error": "",
            "caption_cues": _subtitle_cues(scene, _scene_text(project_dir, state, scene), relative_to_scene=True),
            "story_headline": {
                "story_id": story_id,
                "visible": bool(story_headline),
                "layout": deepcopy(_ensure_story_headline_layout(state)),
            },
        })
        return _save(project_dir, state)

    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.stem}-{uuid4().hex[:8]}{output.suffix}")
    command: list[str] = [ffmpeg, "-y"]
    filter_parts: list[str] = []
    input_count = 0
    background_input_indices: list[int] = []
    if needs_background and visual_blocks:
        # Preview and final share this exact silent content-layer materializer.
        # Keeping a second block compositor here caused historic preview/final
        # drift as soon as a new layout primitive was introduced.
        background = _materialize_scene_visual_composition(project_dir, state, scene, ffmpeg)
        command.extend(["-i", str(background)])
        background_input_indices.append(input_count)
        filter_parts.append(f"[{input_count}:v]{_review_background_filter({}, width, height, duration)},fps={fps}[base]")
        input_count += 1
    elif needs_background:
        background = (project_dir / str(visual.get("path"))).resolve()
        try:
            background.relative_to(project_dir.resolve())
        except ValueError as exc:
            raise WorkbenchError("主体素材不在当前项目目录内") from exc
        if not background.is_file():
            raise WorkbenchError("当前主体素材文件不存在，请重新选择")
        if str(visual.get("type") or "").lower() == "image":
            command.extend(["-loop", "1", "-framerate", str(fps), "-i", str(background)])
        else:
            command.extend(["-stream_loop", "-1", "-i", str(background)])
        filter_parts.append(f"[0:v]{_review_background_filter(visual, width, height, duration)},fps={fps}[base]")
        input_count += 1
    headline_index: int | None = None
    if story_headline:
        headline_path = Path(str(story_headline["asset_path"])).resolve()
        try:
            headline_path.relative_to(project_dir.resolve())
        except ValueError as exc:
            raise WorkbenchError("新闻小标题素材不在当前项目目录内") from exc
        if not headline_path.is_file():
            raise WorkbenchError("新闻小标题素材不存在，请重新保存标题后再预览")
        command.extend(["-loop", "1", "-framerate", str(fps), "-t", f"{duration:.6f}", "-i", str(headline_path)])
        headline_index = input_count
        input_count += 1
    if source_path:
        command.extend(["-ss", f"{source_start:.6f}", "-t", f"{duration:.6f}", "-i", str(source_path)])
        avatar_index = input_count
        if treatment == "fullscreen":
            filter_parts.append(
                f"[{avatar_index}:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
                f"crop={width}:{height},setsar=1,fps={fps},trim=duration={duration:.3f},setpts=PTS-STARTPTS[composed]"
            )
        elif treatment in {"pip_top_left", "custom"}:
            geometry = _avatar_pip_geometry(project_dir, state, scene, source_path)
            filter_parts.append(f"[{avatar_index}:v]{_avatar_pip_filter(geometry, fps=fps, duration=duration)}[presenter]")
            filter_parts.append(f"[base][presenter]overlay={geometry['x']}:{geometry['y']}:eof_action=pass[composed]")
        else:
            filter_parts.append("[base]null[composed]")
        audio_map = f"{avatar_index}:a?"
    else:
        filter_parts.append("[base]null[composed]")
        # A review preview must be a playable representation of the segment,
        # rather than a silent visual proxy.  Stock footage is usually muted,
        # so prefer the scene's adopted narration take; only fall back to the
        # visual source's own audio when no take has been promoted yet.
        narration = _current_narration_version(scene)
        narration_path = project_dir / str((narration or {}).get("audio_path") or "")
        if narration_path.is_file():
            audio_index = input_count
            command.extend(["-i", str(narration_path)])
            audio_map = f"{audio_index}:a?"
        elif len(visual_blocks) == 1 and background_input_indices and str((asset_lookup.get(str(visual_blocks[0].get("asset_id") or "")) or {}).get("type") or "").lower() == "video":
            audio_map = f"{background_input_indices[0]}:a?"
        elif needs_background and str((visual or {}).get("type") or "").lower() == "video":
            audio_map = "0:a?"
        else:
            audio_map = None

    composed_label = "composed"
    if headline_index is not None and story_headline:
        filter_parts.append(
            f"[{headline_index}:v]format=rgba,trim=duration={duration:.3f},setpts=PTS-STARTPTS[storyheadline]"
        )
        filter_parts.append(
            f"[composed][storyheadline]overlay={int(story_headline['x'])}:{int(story_headline['y'])}:"
            "eof_action=pass[headlinecomposed]"
        )
        composed_label = "headlinecomposed"
    effects = _scene_directive_filters(project_dir, state, scene)
    if effects:
        filter_parts.append(f"[{composed_label}]{effects}[vout]")
    else:
        filter_parts.append(f"[{composed_label}]null[vout]")
    command.extend(["-filter_complex", ";".join(filter_parts), "-map", "[vout]"])
    if audio_map:
        command.extend(["-map", audio_map, "-c:a", "aac", "-b:a", "160k"])
    else:
        command.extend(["-an"])
    command.extend([
        "-t", f"{duration:.6f}", "-c:v", "libx264", "-preset", "medium", "-crf", "20",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(temporary),
    ])
    ok, detail = _run_media(command)
    if not ok or not temporary.is_file():
        try:
            temporary.unlink()
        except OSError:
            pass
        preview.update({"status": "failed", "error": detail[:1200]})
        _save(project_dir, state)
        raise WorkbenchError(f"片段审核预览生成失败：{detail}")
    os.replace(temporary, output)
    preview.update({
        "status": "ready", "output_path": _safe_relpath(project_dir, str(output)),
        "input_signature": signature, "duration_seconds": round(duration, 3),
        "resolution": f"{width}x{height}", "generated_at": _now(), "stale_reason": "", "error": "",
        "caption_cues": _subtitle_cues(scene, _scene_text(project_dir, state, scene), relative_to_scene=True),
        "story_headline": {
            "story_id": story_id,
            "visible": bool(story_headline),
            "layout": deepcopy(_ensure_story_headline_layout(state)),
        },
    })
    _activity(state, "scene_review_preview", f"已生成 {scene_id} 的本地审核预览（{width}×{height}）", scene_id=scene_id, output_path=preview["output_path"])
    return _save(project_dir, state)


def add_surgical_directive(project_dir: Path, scene_id: str, payload: dict) -> dict:
    """Record a time-bound component instruction for a single review scene."""
    state = _load_for_write(project_dir)
    scene = _find(state["scenes"], scene_id, "场景")
    _ensure_scene_review_surface(scene)
    kind = str(payload.get("component_type") or "")
    if kind not in SURGICAL_COMPONENT_TYPES:
        raise WorkbenchError("组件类型只能是文字提示卡、信息标签或聚焦框")
    position = str(payload.get("position") or "lower_third")
    if position not in SURGICAL_COMPONENT_POSITIONS:
        raise WorkbenchError("组件位置无效")
    text = str(payload.get("text") or "").strip().replace("\n", " ")
    if kind != "focus_box" and not text:
        raise WorkbenchError("请填写组件要显示的文字")
    if len(text) > 80:
        raise WorkbenchError("组件文字不能超过 80 个字符")
    directive = {
        "id": _numbered("RDX-", scene["surgical_directives"], "id"),
        "component_type": kind,
        "start_seconds": _review_directive_time(scene, payload.get("start_seconds")),
        "duration_seconds": _review_directive_duration(scene, payload.get("duration_seconds")),
        "position": position,
        "text": text,
        "box": payload.get("box") if isinstance(payload.get("box"), dict) else {"x": .20, "y": .20, "width": .58, "height": .36},
        "status": "draft", "created_at": _now(),
    }
    scene["surgical_directives"].append(directive)
    _invalidate_scene_review_preview(scene, f"已添加 {directive['id']}，请刷新本段审核预览")
    scene["review_status"] = "needs_adjustment"
    _mark_render_needs_refresh(state, f"{scene_id} 已添加定点组件，需要重新合成成片")
    _decision(state, "surgical_directive", f"{scene_id} 定点组件", directive["id"], f"{kind} @ {directive['start_seconds']:.2f}s")
    _activity(state, "surgical_directive", f"已在 {scene_id} 的 {directive['start_seconds']:.2f} 秒添加 {directive['id']}", scene_id=scene_id, directive_id=directive["id"])
    return _save(project_dir, state)


def remove_surgical_directive(project_dir: Path, scene_id: str, directive_id: str) -> dict:
    state = _load_for_write(project_dir)
    scene = _find(state["scenes"], scene_id, "场景")
    _ensure_scene_review_surface(scene)
    original = list(scene["surgical_directives"])
    scene["surgical_directives"] = [item for item in original if str(item.get("id")) != directive_id]
    if len(original) == len(scene["surgical_directives"]):
        raise WorkbenchError("未找到要删除的定点组件")
    _invalidate_scene_review_preview(scene, f"已删除 {directive_id}，请刷新本段审核预览")
    _mark_render_needs_refresh(state, f"{scene_id} 已删除定点组件，需要重新合成成片")
    _activity(state, "surgical_directive_removed", f"已删除 {scene_id} 的 {directive_id}", scene_id=scene_id, directive_id=directive_id)
    return _save(project_dir, state)


def _safe_automation_error(error: object) -> str:
    message = str(error or "自动生产任务失败")
    for variable in ("OPENAI_API_KEY", "PEXELS_API_KEY", "VOICEBOX_API_KEY", "DOUBAO_SPEECH_API_KEY"):
        secret = os.environ.get(variable)
        if secret:
            message = message.replace(secret, "[已隐藏]")
    return message[:1200]


def _review_preview_is_current(project_dir: Path, state: dict, scene: dict) -> bool:
    """Return whether a cached review preview matches the scene's live inputs."""
    preview = scene.get("review_preview") if isinstance(scene.get("review_preview"), dict) else {}
    if preview.get("status") != "ready" or not preview.get("output_path"):
        return False
    try:
        relative = _safe_relpath(project_dir, str(preview.get("output_path")))
        return bool(
            relative
            and (project_dir / relative).is_file()
            and str(preview.get("input_signature") or "") == _review_preview_signature(project_dir, state, scene)
        )
    except (OSError, WorkbenchError):
        return False


def _preview_sync_scene_ids(project_dir: Path, state: dict, payload: dict) -> list[str]:
    mode = str(payload.get("selection_mode") or "missing")
    scenes = [item for item in state.get("scenes", []) if isinstance(item, dict)]
    if mode == "custom":
        requested = {str(value) for value in (payload.get("scene_ids") or [])}
        scenes = [scene for scene in scenes if str(scene.get("id")) in requested]
    elif mode not in {"missing", "all"}:
        raise WorkbenchError("审核预览同步范围无效")
    if mode == "missing":
        scenes = [scene for scene in scenes if not _review_preview_is_current(project_dir, state, scene)]
    return [str(scene.get("id")) for scene in scenes if scene.get("id")]


def preview_review_preview_sync(project_dir: Path, payload: dict) -> dict:
    state = read_workbench(project_dir)
    scene_ids = _preview_sync_scene_ids(project_dir, state, payload)
    lookup = {str(scene.get("id")): scene for scene in state.get("scenes", []) if isinstance(scene, dict)}
    return {
        "status": "planned", "selection_mode": str(payload.get("selection_mode") or "missing"),
        "scene_ids": scene_ids, "scene_count": len(scene_ids),
        "items": [{"scene_id": scene_id, "title": lookup[scene_id].get("title"), "order": lookup[scene_id].get("order")} for scene_id in scene_ids],
    }


@_project_transactional
def start_review_preview_sync(project_dir: Path, payload: dict) -> dict:
    """Queue local review-preview renders without rebuilding the active UI."""
    if payload.get("confirmed") is not True:
        raise WorkbenchError("请先确认需要同步的审核预览")
    state = _load_for_write(project_dir)
    _require_no_review_preview_conflict(
        _automation(state), payload.get("_review_preview_job_id"), payload.get("_review_preview_worker_token"), payload.get("_review_preview_internal_capability")
    )
    visual_batch = _automation(state)["visual_batch"]
    if visual_batch.get("status") in {"queued", "generating"}:
        current = visual_batch.get("current") or {}
        current_label = " / ".join(
            str(value) for value in (current.get("scene_id"), current.get("block_id")) if value
        ) or "下一格"
        raise WorkbenchError(
            f"主体画面仍在生成（当前：{current_label}），请等待批量任务完成后再同步审核预览"
        )
    current = _automation(state)["preview_sync"]
    if current.get("status") in {"queued", "generating"}:
        raise WorkbenchError("已有审核预览同步任务正在运行，请等待完成")
    scene_ids = _preview_sync_scene_ids(project_dir, state, payload)
    if not scene_ids:
        raise WorkbenchError("所有片段的审核预览都已同步，无需重复生成")
    job_id = f"PSJ-{uuid4().hex[:10]}"
    _automation(state)["preview_sync"] = {
        "status": "queued", "job_id": job_id, "scene_ids": scene_ids,
        "items": [{"scene_id": scene_id, "status": "queued", "error": ""} for scene_id in scene_ids],
        "total_scenes": len(scene_ids), "completed_scenes": 0, "failed_scenes": 0,
        "current": None, "started_at": _now(), "finished_at": None, "error": "",
    }
    _activity(state, "review_preview_sync_started", f"已建立 {len(scene_ids)} 个片段的审核预览同步任务", scene_ids=scene_ids, job_id=job_id)
    return _save(project_dir, state)


def read_review_preview_sync(project_dir: Path) -> dict:
    state = read_workbench(project_dir)
    sync = deepcopy(_automation(state)["preview_sync"])
    sync["current_scene_ids"] = [
        str(scene.get("id")) for scene in state.get("scenes", [])
        if isinstance(scene, dict) and _review_preview_is_current(project_dir, state, scene)
    ]
    return {"generation": sync}


def generate_review_preview_sync(project_dir: Path, expected_job_id: str | None = None) -> dict:
    state = _load_for_write(project_dir)
    sync = _automation(state)["preview_sync"]
    if expected_job_id and str(sync.get("job_id") or "") != str(expected_job_id):
        return state
    if sync.get("status") not in {"queued", "generating"}:
        return state
    sync["status"] = "generating"
    _save(project_dir, state)
    for scene_id in list(sync.get("scene_ids") or []):
        state = _load_for_write(project_dir)
        sync = _automation(state)["preview_sync"]
        if expected_job_id and str(sync.get("job_id") or "") != str(expected_job_id):
            return state
        item = next((entry for entry in sync.get("items", []) if entry.get("scene_id") == scene_id), None)
        if not item or item.get("status") in {"completed", "failed"}:
            continue
        item["status"] = "generating"
        sync["current"] = {"scene_id": scene_id}
        _save(project_dir, state)
        try:
            generate_scene_review_preview(project_dir, scene_id)
            state = _load_for_write(project_dir)
            sync = _automation(state)["preview_sync"]
            if expected_job_id and str(sync.get("job_id") or "") != str(expected_job_id):
                return state
            item = next(entry for entry in sync.get("items", []) if entry.get("scene_id") == scene_id)
            item.update({"status": "completed", "error": "", "finished_at": _now()})
        except Exception as exc:
            state = _load_for_write(project_dir)
            sync = _automation(state)["preview_sync"]
            if expected_job_id and str(sync.get("job_id") or "") != str(expected_job_id):
                return state
            item = next(entry for entry in sync.get("items", []) if entry.get("scene_id") == scene_id)
            item.update({"status": "failed", "error": _safe_automation_error(exc), "finished_at": _now()})
        sync["completed_scenes"] = sum(1 for entry in sync.get("items", []) if entry.get("status") == "completed")
        sync["failed_scenes"] = sum(1 for entry in sync.get("items", []) if entry.get("status") == "failed")
        _save(project_dir, state)
    state = _load_for_write(project_dir)
    sync = _automation(state)["preview_sync"]
    if expected_job_id and str(sync.get("job_id") or "") != str(expected_job_id):
        return state
    sync["status"] = "completed_with_failures" if sync.get("failed_scenes") else "completed"
    sync["current"] = None
    sync["finished_at"] = _now()
    _activity(state, "review_preview_sync_finished", f"审核预览同步完成：{sync.get('completed_scenes', 0)}/{sync.get('total_scenes', 0)}")
    return _save(project_dir, state)


def mark_review_preview_sync_failed(project_dir: Path, error: object, expected_job_id: str | None = None) -> dict:
    state = _load_for_write(project_dir)
    sync = _automation(state)["preview_sync"]
    if expected_job_id and str(sync.get("job_id") or "") != str(expected_job_id):
        return state
    sync.update({"status": "failed", "current": None, "finished_at": _now(), "error": _safe_automation_error(error)})
    return _save(project_dir, state)


def _stock_review_timeline(
    project_dir: Path,
    state: dict,
    scene: dict,
    visual_asset: dict,
    *,
    source_tool: str,
    query: str,
    review_image_asset: dict | None = None,
) -> dict:
    """Create real first/climax review stills for stock footage or a still fallback."""
    scene_start = _as_number(scene.get("start_seconds"))
    scene_end = max(scene_start, _as_number(scene.get("end_seconds"), scene_start))
    scene_duration = max(0.1, scene_end - scene_start)
    review_id = _numbered("KFR-", state.setdefault("keyframe_reviews", []), "id")
    assets: list[dict] = []

    if review_image_asset is not None:
        assets = [review_image_asset, review_image_asset]
    else:
        ffmpeg = _ffmpeg_available()
        if not ffmpeg:
            raise WorkbenchError("本机未发现 FFmpeg，无法从下载视频提取可审核关键帧")
        raw_duration = max(0.2, _as_number(visual_asset.get("duration_seconds"), scene_duration))
        requested_times = (0.1, min(max(0.1, raw_duration * 0.66), max(0.1, raw_duration - 0.05)))
        for index, (kind, relative) in enumerate(zip(("first_frame", "climax_frame"), requested_times), 1):
            output = project_dir / "assets" / "images" / "stock" / f"keyframe-{scene['id']}-{kind}-{uuid4().hex[:8]}.jpg"
            output.parent.mkdir(parents=True, exist_ok=True)
            ok, detail = _run_media([
                ffmpeg, "-y", "-ss", f"{relative:.3f}", "-i", str(project_dir / visual_asset["path"]),
                "-frames:v", "1", "-q:v", "2", str(output),
            ])
            if not ok or not output.is_file():
                raise WorkbenchError(f"下载视频的关键帧提取失败：{detail}")
            assets.append(_append_asset(project_dir, state, {
                "name": f"{scene.get('title') or scene['id']} · {'首帧' if index == 1 else '高潮帧'}审核图",
                "type": "image", "source_type": "local_generated", "path": str(output),
                "provider": "本地 FFmpeg", "source_tool": "stock_keyframe_extract",
                "license": "由已登记的 Pexels 素材提取，仅用于项目审核",
                "generation": {"derived_from": visual_asset["id"], "scene_id": scene["id"], "anchor_kind": kind, "generated_at": _now()},
            }))

    timeline: list[dict] = []
    for index, (kind, asset) in enumerate(zip(("first_frame", "climax_frame"), assets), 1):
        anchor = next((item for item in scene.get("anchors", []) if item.get("kind") == kind), None)
        absolute_time = _as_number((anchor or {}).get("time_seconds"), scene_start if kind == "first_frame" else scene_start + scene_duration * .66)
        relative_start = max(0.0, min(scene_duration, absolute_time - scene_start))
        next_start = scene_duration if kind == "climax_frame" else max(relative_start + .1, scene_duration * .66)
        timeline.append({
            "id": f"{review_id}-{index:02d}", "anchor_kind": kind,
            "label": "首帧" if kind == "first_frame" else "高潮帧",
            "time_seconds": round(absolute_time, 3), "relative_start_seconds": round(relative_start, 3),
            "relative_end_seconds": round(min(scene_duration, next_start), 3),
            "caption_text": str(scene.get("description") or "").strip(),
            "visual_note": str(scene.get("shot_intent") or "").strip(),
            "asset_id": asset["id"], "status": "pending", "query": query,
        })

    review = {
        "id": review_id, "status": "generated", "timeline": timeline,
        "generation": {"provider": "pexels", "tool": source_tool, "query": query, "count": len(timeline), "generated_at": _now()},
        "review_note": "",
    }
    review["hyperframes"] = _build_hyperframes_review(project_dir, state, scene, timeline)
    review["artifact_path"] = _write_keyframe_review_artifact(project_dir, scene, review)
    scene["keyframe_review"] = review
    scene["keyframe_generation"] = {
        "status": "completed", "finished_at": _now(), "provider": "pexels", "tool": source_tool,
        "expected_count": len(timeline), "completed_count": len(timeline), "review_id": review_id, "error": "",
    }
    state["keyframe_reviews"].append({"id": review_id, "scene_id": scene["id"], "status": review["status"], "artifact_path": review["artifact_path"], "created_at": _now()})
    return review


def _motion_video_from_stock_image(
    project_dir: Path,
    state: dict,
    scene: dict,
    image_asset: dict,
    *,
    duration_seconds: float | None = None,
    output_suffix: str = "",
) -> dict:
    """Turn an approved still image into a timed video asset for FFmpeg."""
    ffmpeg = _ffmpeg_available()
    if not ffmpeg:
        raise WorkbenchError("本机未发现 FFmpeg，无法把 Pexels 图片转为可合成片段")
    duration = max(.1, _as_number(duration_seconds, _scene_duration(scene)))
    width, height = _render_dimensions(project_dir, state)
    suffix = re.sub(r"[^a-zA-Z0-9_-]", "-", str(output_suffix or "")).strip("-")
    output = project_dir / "assets" / "video" / "stock" / f"{scene['id']}{'-' + suffix if suffix else ''}-image-motion.mp4"
    output.parent.mkdir(parents=True, exist_ok=True)
    filters = f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},fps=30,format=yuv420p"
    ok, detail = _run_media([
        ffmpeg, "-y", "-loop", "1", "-i", str(project_dir / image_asset["path"]), "-t", f"{duration:.3f}",
        "-vf", filters, "-an", "-c:v", "libx264", "-preset", "medium", "-crf", "22", "-movflags", "+faststart", str(output),
    ])
    if not ok or not output.is_file():
        raise WorkbenchError(f"图片转视频片段失败：{detail}")
    image_provider = str(image_asset.get("provider") or (image_asset.get("provenance") or {}).get("provider") or "图片素材")
    return _append_asset(project_dir, state, {
        "name": f"{scene.get('title') or scene['id']} · {image_provider} 图片动态片段", "type": "video", "source_type": "local_generated",
        "path": str(output), "duration_seconds": round(duration, 3), "resolution": f"{width}x{height}",
        "provider": "本地 FFmpeg", "source_tool": "stock_image_motion", "source_url": (image_asset.get("provenance") or {}).get("source_url"),
        "license": f"由 {image_asset['id']} 图片生成动态片段；继承原素材许可",
        "generation": {"derived_from": image_asset["id"], "scene_id": scene["id"], "generated_at": _now()},
    })


def _candidate_ledger_summary(candidate: Any) -> dict:
    """Persist only public candidate metadata; never write signed file URLs."""
    extra = candidate.extra if isinstance(getattr(candidate, "extra", None), dict) else {}
    previews = extra.get("preview_frames") if isinstance(extra.get("preview_frames"), list) else []
    return {
        "asset_id": candidate_asset_id(candidate),
        "provider_id": str(candidate.source_id),
        "kind": str(candidate.kind),
        "width": int(candidate.width or 0), "height": int(candidate.height or 0),
        "duration_seconds": round(float(candidate.duration or 0), 3),
        "source_url": str(candidate.source_url or ""),
        "thumbnail_url": str(candidate.thumbnail_url or ""),
        "preview_frame_count": len(previews),
        "source_tags": str(candidate.source_tags or "")[:360],
    }


def _director_slot_context(scene: dict, block: dict, item: dict, used_provider_ids: set[str]) -> dict:
    return {
        "scene_id": str(scene.get("id") or ""), "block_id": str(block.get("id") or ""),
        "start_seconds": _as_number(block.get("start_seconds")),
        "end_seconds": _as_number(block.get("end_seconds")),
        "slot_text": str(item.get("slot_text") or block.get("slot_text") or ""),
        "context_text": str(item.get("context_text") or block.get("context_text") or scene.get("description") or ""),
        "visual_intent": str(item.get("visual_intent") or block.get("visual_intent") or ""),
        "query": str(item.get("query") or block.get("query") or ""),
        "search_role": str(item.get("search_role") or block.get("search_role") or ""),
        "recently_used_asset_ids": [f"pexels:{value}" for value in sorted(used_provider_ids)[:24]],
    }


def _director_search_queries(item: dict, fallback_query: str, *, retry_queries: tuple[str, ...] = ()) -> list[str]:
    values: list[str] = [*retry_queries]
    if not retry_queries:
        values.append(fallback_query)
        values.extend(
            str(entry.get("query") or "")
            for entry in (item.get("query_ladder") or [])
            if isinstance(entry, dict)
        )
    cleaned: list[str] = []
    for value in values:
        query = re.sub(r"\s+", " ", str(value or "")).strip()[:160]
        if query and query not in cleaned:
            cleaned.append(query)
        if len(cleaned) >= 3:
            break
    return cleaned


def _probe_official_image(path: Path) -> dict:
    """Deterministic quality floor for an official press image.

    Rejects favicons, tracking pixels and tiny placeholders so a bad og:image
    never silently becomes a production visual.
    """
    size = path.stat().st_size if path.is_file() else 0
    width = height = 0
    try:
        import cv2

        frame = cv2.imread(str(path))
        if frame is not None:
            height, width = frame.shape[:2]
    except Exception:
        pass
    reasons: list[str] = []
    if size < 8 * 1024:
        reasons.append("文件过小，疑似占位图或图标")
    if width and height and (width < 640 or height < 360):
        reasons.append(f"分辨率不足：{width}x{height}")
    return {"width": width, "height": height, "bytes": size, "reject": bool(reasons), "reasons": reasons}


def _try_official_image_candidate(
    project_dir: Path,
    state: dict,
    item: dict,
    scene: dict,
    block: dict,
    *,
    content_rules: list[str],
    person_policy: str,
    provider_guard: Callable[[], None] | None = None,
) -> tuple[Any, str, dict] | None:
    """Prefer the story's official press image before falling back to Pexels.

    Rules that keep this safe:
    - The image maps to the story's *first* image slot only (the lead visual
      that introduces the concrete object); later slots reuse it never, so a
      story is not painted with one repeated picture.
    - A deterministic quality floor (size + resolution) and the existing
      person screening reject bad images; any rejection falls back to Pexels.
    - Every use is recorded in the asset ledger as provider ``官方媒体`` with
      the source URL and attribution, so the human review can see and reject it.
    """
    image_url = str(scene.get("official_image_url") or "").strip()
    if not image_url:
        return None
    ledger = item.setdefault("director_ledger", {})
    # 同一张官方图只用于该故事的首个图片槽位，避免整段重复。
    if image_url in state.setdefault("used_official_images", []):
        ledger.update({"official_image_skipped": "该故事已有槽位使用官方图"})
        return None
    attribution = str(scene.get("official_image_attribution") or "").strip()
    try:
        import requests

        response = _guarded_visual_provider_io(
            provider_guard,
            lambda: requests.get(
                image_url,
                timeout=30,
                headers={"User-Agent": "Mozilla/5.0 OpenMontage/1.0"},
            ),
        )
        response.raise_for_status()
        content = response.content
    except _ReviewPreviewVisualLeaseError:
        raise
    except Exception as exc:  # noqa: BLE001 - official image is a preference, not a gate.
        ledger.update({"official_image_error": _safe_automation_error(exc)})
        return None
    if not content:
        return None
    output = project_dir / "assets" / "images" / "official_press" / f"{scene['id']}-{block['id']}-official-{uuid4().hex[:6]}.jpg"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(content)
    quality = _probe_official_image(output)
    if quality["reject"]:
        output.unlink(missing_ok=True)
        ledger.update({"official_image_rejected": quality})
        return None
    screening = _screen_visual_candidate(output, "image", content_rules, person_policy)
    if screening.get("status") == "rejected":
        output.unlink(missing_ok=True)
        ledger.update({"official_image_rejected": screening})
        return None
    path = _safe_relpath(project_dir, str(output))
    if not path or not (project_dir / path).is_file():
        return None
    state.setdefault("used_official_images", []).append(image_url)
    item["stage"] = "已锁定官方配图，正在登记素材"
    _save(project_dir, state)
    photo_id = f"official-{hashlib.sha256(image_url.encode('utf-8')).hexdigest()[:16]}"
    data = {
        "video_id": None,
        "photo_id": photo_id,
        "duration_seconds": 0,
        "width": quality["width"],
        "height": quality["height"],
        "license": "官方媒体配图（新闻稿/原站正文），请保留署名来源、不得商用篡改",
        "pexels_url": image_url,
        "attribution": attribution,
        "official_press": True,
        "official_image_quality": quality,
    }
    ledger.update({"status": "official_press", "selected_asset_id": photo_id, "completed_at": _now()})
    return SimpleNamespace(success=True, artifacts=[str(output)], data=data, error=None), path, screening


def _downloaded_visual_content_identity(
    project_dir: Path,
    state: dict,
    candidate_path: Path,
    media_kind: str,
) -> tuple[str, dict | None]:
    """Hash one downloaded stock candidate and find an identical accepted asset.

    Provider ids are useful search hints, but they are not a content identity:
    Pexels can expose the same rendition under multiple ids.  The project
    ledger therefore compares bytes after download and before registration.
    Only existing Pexels assets of the same media kind participate so local
    user media and intentional derived files are not silently rejected.
    """
    candidate = candidate_path.resolve()
    try:
        candidate.relative_to(project_dir.resolve())
    except (OSError, ValueError) as exc:
        raise WorkbenchError("下载候选必须位于当前项目目录内") from exc
    if not candidate.is_file():
        raise WorkbenchError("下载候选文件不存在，无法进行内容去重")

    candidate_sha256 = _sha256_file(candidate)
    for asset in state.get("assets") or []:
        if str(asset.get("type") or "") != media_kind:
            continue
        provider = str(asset.get("provider") or "").strip().lower()
        source_tool = str(asset.get("source_tool") or "").strip().lower()
        if provider != "pexels" and source_tool not in {"pexels_video", "pexels_image"}:
            continue
        try:
            relative = _safe_relpath(project_dir, str(asset.get("path") or ""))
        except WorkbenchError:
            continue
        if not relative:
            continue
        existing = (project_dir / relative).resolve()
        if existing == candidate or not existing.is_file():
            continue
        existing_sha256 = str((asset.get("generation") or {}).get("content_sha256") or "")
        if not existing_sha256:
            existing_sha256 = _sha256_file(existing)
        if existing_sha256 == candidate_sha256:
            generation = asset.get("generation") or {}
            provider_id = generation.get("video_id") if media_kind == "video" else generation.get("photo_id")
            return candidate_sha256, {
                "asset_id": asset.get("id"),
                "path": relative,
                "provider_id": str(provider_id or "") or None,
                "sha256": candidate_sha256,
            }
    return candidate_sha256, None


def _find_autonomous_pexels_candidate(
    project_dir: Path,
    state: dict,
    item: dict,
    scene: dict,
    block: dict,
    *,
    media_kind: str,
    query: str,
    orientation: str,
    target_duration: float,
    content_rules: list[str],
    person_policy: str,
    used_provider_ids: set[str],
    provider_guard: Callable[[], None] | None = None,
) -> tuple[Any, str, dict]:
    """Run metadata retrieval → internal-model decision → one committed download.

    `PexelsSource.search` returns only candidate metadata.  The sole full-file
    transfer happens after `decide_candidate` returns a whitelisted asset.
    """
    if media_kind == "image":
        official = _try_official_image_candidate(
            project_dir, state, item, scene, block,
            content_rules=content_rules, person_policy=person_policy,
            provider_guard=provider_guard,
        )
        if official is not None:
            return official
    source = PexelsSource()
    if not source.is_available():
        raise WorkbenchError("Pexels 素材服务尚未配置，请先设置 PEXELS_API_KEY")
    candidate_limit = int(item.get("candidate_limit") or 6)
    ledger = item.setdefault("director_ledger", {
        "director_version": DIRECTOR_VERSION,
        "attempts": [],
        "status": "pending",
    })
    retry_queries: tuple[str, ...] = ()
    last_reason = ""
    for search_round in range(1, 3):
        queries = _director_search_queries(item, query, retry_queries=retry_queries)
        if not queries:
            break
        item["stage"] = f"自动导演正在预览候选素材（第 {search_round} 轮）"
        item["active_queries"] = queries
        _save(project_dir, state)
        raw_candidates: list[Any] = []
        search_errors: list[str] = []
        for active_query in queries:
            try:
                raw_candidates.extend(
                    _guarded_visual_provider_io(
                        provider_guard,
                        lambda active_query=active_query: source.search(
                            active_query,
                            SearchFilters(
                                kind=media_kind,
                                orientation=orientation,
                                min_duration=max(0.1, target_duration) if media_kind == "video" else None,
                                per_page=min(40, max(12, candidate_limit * 3)),
                                page=1,
                            ),
                        ),
                    )
                )
            except _ReviewPreviewVisualLeaseError:
                raise
            except Exception as exc:
                search_errors.append(_safe_automation_error(exc))
        candidates = prepare_candidates(
            raw_candidates, media_kind=media_kind, orientation=orientation,
            minimum_duration=max(0.1, target_duration) if media_kind == "video" else 0.0,
            used_provider_ids=used_provider_ids,
        )[: max(candidate_limit * 2, 8)]
        slot_context = _director_slot_context(scene, block, item, used_provider_ids)
        decision = decide_candidate(slot_context, candidates)
        attempt = {
            "round": search_round, "queries": queries,
            "candidates": [_candidate_ledger_summary(candidate) for candidate in candidates],
            "decision": deepcopy(decision.ledger),
            "search_errors": search_errors,
        }
        ledger["attempts"].append(attempt)
        last_reason = str(decision.ledger.get("reason") or "")
        if decision.candidate is None:
            contextual_broll = str(item.get("semantic_tolerance") or "strict") == "contextual_broll"
            if contextual_broll and candidates and not (decision.decision == "retry" and search_round == 1):
                context_words = {
                    word.lower() for word in re.findall(
                        r"[A-Za-z][A-Za-z0-9-]{1,}",
                        " ".join((query, str(item.get("visual_intent") or ""), str(item.get("slot_text") or ""))),
                    )
                }
                candidate = max(
                    candidates,
                    key=lambda value: (
                        len(context_words & {
                            word.lower() for word in re.findall(
                                r"[A-Za-z][A-Za-z0-9-]{1,}", str(value.source_tags or "")
                            )
                        }),
                        min(float(value.duration or 0), target_duration * 2),
                        min(int(value.width or 0), int(value.height or 0)),
                        candidate_asset_id(value),
                    ),
                )
                override_ledger = {
                    "director_version": DIRECTOR_VERSION,
                    "decision_source": "contextual_broll_policy",
                    "decision": "accept",
                    "selected_asset_id": candidate_asset_id(candidate),
                    "reason": "严格语义候选不足；配比修复允许使用主题相邻且不改变事实含义的上下文B-roll",
                    "weighted_score": 65.0,
                    "semantic_score": 65.0,
                    "confidence": 0.72,
                    "strict_rejection_reason": last_reason,
                }
                attempt["contextual_broll_override"] = deepcopy(override_ledger)
                decision = SimpleNamespace(
                    candidate=candidate, decision="accept", ledger=override_ledger, retry_queries=()
                )
            else:
                retry_queries = decision.retry_queries
                if decision.decision == "retry" and search_round == 1:
                    item["stage"] = "自动导演置信度不足，正在改写检索词后重试"
                    _save(project_dir, state)
                    continue
                break

        candidate = decision.candidate
        extension = "mp4" if media_kind == "video" else "jpg"
        folder = "video/pexels" if media_kind == "video" else "images/pexels"
        output = project_dir / "assets" / folder / f"{scene['id']}-{block['id']}-selected-{uuid4().hex[:6]}.{extension}"
        item["stage"] = "自动导演已锁定素材，正在下载最终文件"
        _save(project_dir, state)
        try:
            _guarded_visual_provider_io(
                provider_guard,
                lambda: source.download(candidate, output),
            )
        except _ReviewPreviewVisualLeaseError:
            raise
        except Exception as exc:
            ledger["attempts"][-1]["download_error"] = _safe_automation_error(exc)
            used_provider_ids.add(str(candidate.source_id))
            retry_queries = decision.retry_queries or tuple(_director_search_queries(item, query)[1:])
            if search_round == 1:
                continue
            break
        path = _safe_relpath(project_dir, output)
        if not path or not (project_dir / path).is_file():
            raise WorkbenchError("自动导演下载后未获得可登记的项目内素材文件")
        content_sha256, duplicate = _downloaded_visual_content_identity(
            project_dir, state, project_dir / path, media_kind
        )
        if duplicate:
            (project_dir / path).unlink(missing_ok=True)
            used_provider_ids.add(str(candidate.source_id))
            ledger["attempts"][-1]["post_download_duplicate"] = duplicate
            ledger["attempts"][-1]["decision"]["decision"] = "retry"
            item["stage"] = "候选内容与已用素材重复，正在继续搜索"
            retry_queries = decision.retry_queries or tuple(_director_search_queries(item, query)[1:])
            if search_round == 1:
                continue
            break
        item["downloaded_content_sha256"] = content_sha256
        screening = _screen_visual_candidate(project_dir / path, media_kind, content_rules, person_policy)
        if screening.get("status") == "rejected":
            (project_dir / path).unlink(missing_ok=True)
            used_provider_ids.add(str(candidate.source_id))
            ledger["attempts"][-1]["post_download_screening"] = screening
            ledger["attempts"][-1]["decision"]["decision"] = "retry"
            retry_queries = decision.retry_queries or tuple(_director_search_queries(item, query)[1:])
            if search_round == 1:
                continue
            break
        ledger.update({
            "status": "accepted", "selected_asset_id": candidate_asset_id(candidate),
            "selected": _candidate_ledger_summary(candidate), "completed_at": _now(),
        })
        item.update({
            "query": queries[0], "candidate_attempt": 1,
            "accepted_candidate": {
                "provider_id": str(candidate.source_id), "status": "passed",
                "score": decision.ledger.get("weighted_score"), "reasons": [decision.ledger.get("reason")],
                "source_url": candidate.source_url, "query": queries[0], "query_level": "自动导演",
            },
            "screening": screening,
        })
        data = {
            "video_id": str(candidate.source_id) if media_kind == "video" else None,
            "photo_id": str(candidate.source_id) if media_kind == "image" else None,
            "duration_seconds": float(candidate.duration or 0), "width": int(candidate.width or 0),
            "height": int(candidate.height or 0), "license": candidate.license,
            "pexels_url": candidate.source_url,
        }
        return SimpleNamespace(success=True, artifacts=[str(output)], data=data, error=None), path, screening
    ledger.update({"status": "fallback", "completed_at": _now(), "reason": last_reason or "自动导演未找到可提交的候选"})
    item["stage"] = "自动导演未找到合格候选，转入安全降级"
    # Let the existing job-level fallback route handle this slot; never create
    # a fake or blank stock asset merely to make the batch look successful.
    raise WorkbenchError("自动导演未找到合格的 Pexels 素材；已保留原画面并记录降级原因")


def _find_screened_pexels_candidate(
    project_dir: Path,
    state: dict,
    item: dict,
    scene: dict,
    block: dict,
    *,
    media_kind: str,
    query: str,
    orientation: str,
    page: int,
    target_duration: float,
    content_rules: list[str],
    person_policy: str,
    used_provider_ids: set[str],
    tool: Any,
    provider_guard: Callable[[], None] | None = None,
) -> tuple[Any, str, dict]:
    """Search a reviewed query ladder, then lightly inspect downloaded candidates."""
    candidate_limit = int(item.get("candidate_limit") or 6)
    query_ladder = [entry for entry in (item.get("query_ladder") or []) if isinstance(entry, dict) and entry.get("query")]
    if not query_ladder:
        query_ladder = [{"level": "检索词", "query": query}]
    rejected = item.setdefault("rejected_candidates", [])
    last_error = ""
    for candidate_attempt in range(1, candidate_limit + 1):
        ladder_entry = query_ladder[(candidate_attempt - 1) % len(query_ladder)]
        query_round = (candidate_attempt - 1) // len(query_ladder)
        active_query = str(ladder_entry.get("query") or query)
        active_level = str(ladder_entry.get("level") or "检索词")
        active_page = page + query_round
        item["candidate_attempt"] = candidate_attempt
        item["candidate_limit"] = candidate_limit
        item["active_query"] = active_query
        item["active_query_level"] = active_level
        item["stage"] = f"{active_level}：{active_query}"
        extension = "mp4" if media_kind == "video" else "jpg"
        folder = "video/pexels" if media_kind == "video" else "images/pexels"
        output = project_dir / "assets" / folder / f"{scene['id']}-{block['id']}-candidate-{candidate_attempt}-{uuid4().hex[:6]}.{extension}"
        inputs = {
            "query": active_query, "orientation": orientation, "size": "medium" if media_kind == "video" else "large",
            "page": active_page, "per_page": 40, "output_path": str(output),
        }
        if media_kind == "video":
            inputs.update({
                "min_duration": max(1, int(math.ceil(target_duration))), "preferred_quality": "hd",
                "max_duration": max(8, min(30, int(math.ceil(target_duration * 2.5)))),
                "exclude_video_ids": sorted(used_provider_ids),
            })
        else:
            inputs["exclude_photo_ids"] = sorted(used_provider_ids)
        result = _guarded_visual_provider_io(
            provider_guard,
            lambda: tool.execute(inputs),
        )
        if not result.success or not result.artifacts:
            last_error = _safe_automation_error(result.error or "Pexels 未返回更多候选素材")
            rejected.append({
                "attempt": candidate_attempt, "provider_id": None, "status": "not_found", "score": None,
                "reasons": [f"{active_level}没有返回可用结果"], "source_url": None,
                "query": active_query, "query_level": active_level,
            })
            item["stage"] = f"{active_level}没有结果，自动切换下一组检索词"
            _save(project_dir, state)
            continue
        path = _safe_relpath(project_dir, result.artifacts[0])
        if not path or not (project_dir / path).is_file():
            last_error = "Pexels 未返回项目内可检查的候选文件"
            break
        data = result.data or {}
        provider_id = str(data.get("video_id") if media_kind == "video" else data.get("photo_id") or "")
        if provider_id:
            used_provider_ids.add(provider_id)
        content_sha256, duplicate = _downloaded_visual_content_identity(
            project_dir, state, project_dir / path, media_kind
        )
        if duplicate:
            rejected.append({
                "attempt": candidate_attempt, "provider_id": provider_id or None,
                "status": "duplicate_content", "score": None,
                "reasons": [f"文件内容与已用素材 {duplicate.get('asset_id') or duplicate.get('path')} 完全相同"],
                "source_url": data.get("pexels_url"), "query": active_query,
                "query_level": active_level, "content_identity": duplicate,
            })
            item["stage"] = f"第 {candidate_attempt} 个候选内容重复，继续搜索"
            (project_dir / path).unlink(missing_ok=True)
            _save(project_dir, state)
            continue
        item["downloaded_content_sha256"] = content_sha256
        item["stage"] = "检查人物与画面规则"
        screening = _screen_visual_candidate(project_dir / path, media_kind, content_rules, person_policy)
        history = {
            "attempt": candidate_attempt, "provider_id": provider_id or None,
            "status": screening.get("status"), "score": screening.get("score"),
            "reasons": list(screening.get("reasons") or []),
            "source_url": data.get("pexels_url"),
            "query": active_query, "query_level": active_level,
        }
        if screening.get("status") == "rejected":
            rejected.append(history)
            item["screening"] = screening
            item["stage"] = f"第 {candidate_attempt} 个候选未通过，继续搜索"
            (project_dir / path).unlink(missing_ok=True)
            _save(project_dir, state)
            continue
        item["screening"] = screening
        item["accepted_candidate"] = history
        item["query"] = active_query
        item["stage"] = "候选已通过，正在登记"
        return result, path, screening
    rejected_count = len(rejected)
    reason_counts: dict[str, int] = {}
    for entry in rejected:
        for reason in entry.get("reasons") or ["未通过规则"]:
            reason_counts[str(reason)] = reason_counts.get(str(reason), 0) + 1
    summary = "、".join(f"{reason} {count} 次" for reason, count in reason_counts.items())
    if rejected_count:
        raise WorkbenchError(f"已连续筛选 {rejected_count} 个候选，仍未找到符合人物策略的素材{f'（{summary}）' if summary else ''}")
    raise WorkbenchError(last_error or "Pexels 未返回可用候选素材")


def _generate_hyperframes_visual_block(
    project_dir: Path,
    state: dict,
    scene: dict,
    block: dict,
    item: dict,
    duration_seconds: float,
) -> dict:
    """Render one reviewed slot through HyperFrames without touching neighbours."""
    width, height = _render_dimensions(project_dir, state)
    fps = int(state.get("settings", {}).get("frame_rate") or 30)
    duration = max(.2, float(duration_seconds))
    intent = str(item.get("visual_intent") or scene.get("title") or "科技信息")[:160]
    context = str(item.get("slot_text") or item.get("context_text") or scene.get("description") or intent)[:1200]
    recipe = str(item.get("scene_recipe") or "relationship_map")
    layout = _resolved_layout_variant(recipe, item.get("layout_variant"))
    recipe = layout["recipe_id"]
    graphic = deepcopy(item.get("graphic_copy") or {}) if isinstance(item.get("graphic_copy"), dict) else {}
    if not str(graphic.get("headline") or "").strip():
        raise WorkbenchError(f"{scene['id']}/{block['id']} 缺少已审核的 HyperFrames 画面标题")
    existing_plan = scene.get("visual_plan") if isinstance(scene.get("visual_plan"), dict) else {}
    style_pack = existing_plan.get("style_pack") if isinstance(existing_plan.get("style_pack"), dict) else {"id": STYLE_PACK_ID}
    plan = {
        "engine": "hyperframes",
        "prompt": f"{intent}。依据：{context}",
        "structured_spec": {
            "scene_goal": str(graphic.get("scene_goal") or intent),
            "headline": str(graphic.get("headline") or intent),
            "supporting_statement": str(graphic.get("supporting_statement") or ""),
            "center_label": str(graphic.get("center_label") or ""),
            "components": [str(value) for value in (graphic.get("nodes") or []) if str(value).strip()][:4],
            "motion": "信息节点分阶段出现，保持数字人与字幕安全区",
            "palette": "科技快报风格包 V1",
            "scene_recipe": recipe,
            "layout_variant": layout["id"],
            "motion_variant": layout["motion_variant"],
            "external_headline": bool(scene.get("story_id")),
        },
        "scene_recipe": recipe,
        "style_pack": style_pack,
    }
    synthetic_scene = deepcopy(scene)
    synthetic_scene.update({"id": f"{scene['id']}-{block['id']}", "start_seconds": 0.0, "end_seconds": duration})
    style_pack_id = str(style_pack.get("id") or STYLE_PACK_ID)
    try:
        style_context = build_style_context(
            scene=synthetic_scene, plan=plan, width=width, height=height,
            duration_seconds=duration, style_pack_id=style_pack_id,
        )
    except StylePackError as exc:
        raise WorkbenchError(f"HyperFrames 风格包解析失败：{exc}") from exc
    temporary = project_dir / "renders" / "motion-candidates" / f"{scene['id']}-{block['id']}-hyperframes-{uuid4().hex[:8]}.mp4"
    temporary.parent.mkdir(parents=True, exist_ok=True)
    edit_decisions = {
        "version": "1.0", "renderer_family": "explainer-data", "render_runtime": "hyperframes",
        "composition_mode": "templated",
        "metadata": {
            "title": intent, "proposal_render_runtime": "hyperframes",
            "compose_target": {"width": width, "height": height, "fit": "cover"},
            "target_duration_seconds": duration, "visual_brief": context,
            "style_pack_id": style_context["style_pack_id"],
            "style_pack_version": style_context["style_pack_version"],
            "style_context": style_context, "require_layout_inspect": True,
            "caption_owner": "openmontage", "subtitle_burn": False,
            "headline_owner": "openmontage-story-overlay" if scene.get("story_id") else "hyperframes",
        },
        "cuts": [
            {"id": f"{scene['id']}-{block['id']}-hero", "type": "text_card", "text": intent, "in_seconds": 0, "out_seconds": duration},
        ],
    }
    workspace = project_dir / "artifacts" / "motion_compositions" / str(scene["id"]) / f"{block['id']}-hyperframes-{uuid4().hex[:8]}"
    profile = "tiktok" if height > width else "youtube_landscape"
    result = HyperFramesCompose().execute({
        "operation": "render", "workspace_path": str(workspace), "profile": profile,
        "fps": fps, "edit_decisions": edit_decisions,
        "asset_manifest": {"version": "1.0", "assets": []},
        "playbook": style_pack_playbook(style_pack_id), "output_path": str(temporary),
        "quality": "draft", "strict": True,
    })
    if not result.success or not temporary.is_file():
        raise WorkbenchError(str(result.error or "HyperFrames 没有生成可播放文件"))
    output = _normalize_motion_visual(
        project_dir, state, synthetic_scene, temporary, "hyperframes",
    )
    temporary.unlink(missing_ok=True)
    return _append_asset(project_dir, state, {
        "name": f"{scene.get('title') or scene['id']} · {block['id']} · HyperFrames 动态画面",
        "type": "video", "source_type": "local_generated", "path": str(output),
        "duration_seconds": duration, "resolution": f"{width}x{height}",
        "provider": "HyperFrames", "source_tool": "hyperframes_compose",
        "license": "本地可复现动态合成",
        "generation": {
            "scene_id": scene["id"], "block_id": block["id"], "engine": "hyperframes",
            "contract_id": (_automation(state).get("visual_batch") or {}).get("contract_id"),
            "visual_intent": intent, "scene_recipe": recipe,
            "layout_variant": layout["id"], "motion_variant": layout["motion_variant"],
            "graphic_copy": graphic,
            "style_render_report": _hyperframes_style_render_report(result, style_context),
            "generated_at": _now(),
        },
    })


class _ReviewPreviewVisualLeaseError(WorkbenchError):
    """Stop a parent-owned visual worker without recording a slot failure."""


def _assert_review_preview_visual_lease(
    project_dir: Path,
    state: dict,
    batch: dict,
    *,
    expected_parent_job_id: str | None,
    expected_worker_token: str | None,
    expected_request_fingerprint: str | None,
    expected_contract_versions: dict[str, str] | None,
) -> None:
    if not expected_parent_job_id:
        return
    automation = _automation(state)
    current_batch = automation.get("visual_batch") or {}
    parent = automation.get("review_preview_pipeline") or {}
    raw_project = _read_json(project_dir / "project.json") or {}
    frozen = parent.get("frozen_input") if isinstance(parent.get("frozen_input"), dict) else {}
    if (
        str(current_batch.get("job_id") or "") != str(batch.get("job_id") or "")
        or str(current_batch.get("parent_job_id") or "") != expected_parent_job_id
        or str(current_batch.get("request_fingerprint") or "") != str(expected_request_fingerprint or "")
        or str(parent.get("job_id") or "") != expected_parent_job_id
        or parent.get("status") != "running"
        or str(parent.get("worker_token") or "") != str(expected_worker_token or "")
        or str(parent.get("request_fingerprint") or "") != str(expected_request_fingerprint or "")
        or str(raw_project.get("pipeline_type") or "") not in {"animated-explainer", "avatar-spokesperson"}
        or str(frozen.get("project_type") or "") != str(raw_project.get("pipeline_type") or "")
        or frozen.get("versions") != expected_contract_versions
    ):
        raise _ReviewPreviewVisualLeaseError(
            "一键审核预览父任务租约、项目类型或冻结版本已变化，停止后续画面外部调用"
        )


def _assert_visual_provider_io_lease(
    project_dir: Path,
    candidate_batch: dict,
    *,
    expected_job_id: str,
    scene_id: str,
    block_id: str,
    slot_claim_id: str,
    expected_parent_job_id: str | None,
    expected_worker_token: str | None,
    expected_request_fingerprint: str | None,
    expected_contract_versions: dict[str, str] | None,
) -> None:
    """Fresh-read the exact child/parent lease around every provider I/O."""
    with _project_transaction_lock(project_dir):
        latest = _load_for_write(project_dir)
        latest_batch = _automation(latest)["visual_batch"]
        if str(latest_batch.get("job_id") or "") != expected_job_id:
            raise _ReviewPreviewVisualLeaseError("画面 provider 调用期间 child 已被替换，停止旧 worker")
        _assert_review_preview_visual_lease(
            project_dir,
            latest,
            candidate_batch,
            expected_parent_job_id=expected_parent_job_id,
            expected_worker_token=expected_worker_token,
            expected_request_fingerprint=expected_request_fingerprint,
            expected_contract_versions=expected_contract_versions,
        )
        latest_item = next(
            (
                entry
                for entry in latest_batch.get("items", [])
                if str(entry.get("scene_id") or "") == scene_id
                and str(entry.get("block_id") or "") == block_id
            ),
            None,
        )
        if not latest_item or str(latest_item.get("worker_claim_id") or "") != slot_claim_id:
            raise _ReviewPreviewVisualLeaseError("画面 provider 调用期间格级 claim 已失效，停止旧 worker")


def _guarded_visual_provider_io(
    provider_guard: Callable[[], None] | None,
    operation: Callable[[], Any],
) -> Any:
    if provider_guard is not None:
        provider_guard()
    try:
        return operation()
    finally:
        # A post-call guard runs even for empty/rejected/error responses.  It
        # must win over provider retry logic so an obsolete worker can never
        # submit the next query, execute, or download.
        if provider_guard is not None:
            provider_guard()


def _visual_slot_state_signature(state: dict, scene_id: str, block_id: str) -> str:
    scene = next(
        (entry for entry in state.get("scenes", []) if str(entry.get("id") or "") == scene_id),
        None,
    )
    block = next(
        (
            entry
            for entry in (((scene or {}).get("visual_timeline") or {}).get("blocks") or [])
            if str(entry.get("id") or "") == block_id
        ),
        None,
    )
    usages = sorted(
        (
            deepcopy(entry)
            for entry in state.get("usages", [])
            if str(entry.get("scene_id") or "") == scene_id
        ),
        key=lambda entry: str(entry.get("id") or ""),
    )
    return _json_hash(
        {
            "block": block,
            "review_status": (scene or {}).get("review_status"),
            "review_preview": (scene or {}).get("review_preview"),
            "usages": usages,
        }
    )


def _commit_visual_batch_slot(
    project_dir: Path,
    base_state: dict,
    candidate_state: dict,
    *,
    expected_job_id: str,
    scene_id: str,
    block_id: str,
    slot_claim_id: str,
    expected_parent_job_id: str | None,
    expected_worker_token: str | None,
    expected_request_fingerprint: str | None,
    expected_contract_versions: dict[str, str] | None,
) -> dict:
    """CAS one completed/failed slot into the latest project state."""
    base_batch = _automation(base_state)["visual_batch"]
    candidate_batch = _automation(candidate_state)["visual_batch"]
    base_item = next(
        (
            entry
            for entry in base_batch.get("items", [])
            if str(entry.get("scene_id") or "") == scene_id
            and str(entry.get("block_id") or "") == block_id
        ),
        None,
    )
    candidate_item = next(
        (
            entry
            for entry in candidate_batch.get("items", [])
            if str(entry.get("scene_id") or "") == scene_id
            and str(entry.get("block_id") or "") == block_id
        ),
        None,
    )
    candidate_scene = next(
        (entry for entry in candidate_state.get("scenes", []) if str(entry.get("id") or "") == scene_id),
        None,
    )
    candidate_block = next(
        (
            entry
            for entry in ((candidate_scene or {}).get("visual_timeline") or {}).get("blocks", [])
            if str(entry.get("id") or "") == block_id
        ),
        None,
    )
    if not base_item or not candidate_item or not candidate_scene or not candidate_block:
        raise _ReviewPreviewVisualLeaseError("画面格提交证据不完整，拒绝覆盖最新项目状态")

    with _project_transaction_lock(project_dir):
        latest = _load_for_write(project_dir)
        latest_batch = _automation(latest)["visual_batch"]
        if str(latest_batch.get("job_id") or "") != expected_job_id:
            raise _ReviewPreviewVisualLeaseError("画面子任务已被替换，旧 worker 结果仅保留为隔离证据")
        _assert_review_preview_visual_lease(
            project_dir,
            latest,
            candidate_batch,
            expected_parent_job_id=expected_parent_job_id,
            expected_worker_token=expected_worker_token,
            expected_request_fingerprint=expected_request_fingerprint,
            expected_contract_versions=expected_contract_versions,
        )
        latest_item = next(
            (
                entry
                for entry in latest_batch.get("items", [])
                if str(entry.get("scene_id") or "") == scene_id
                and str(entry.get("block_id") or "") == block_id
            ),
            None,
        )
        if (
            not latest_item
            or str(latest_item.get("worker_claim_id") or "") != slot_claim_id
            or _json_hash(latest_item) != _json_hash(base_item)
            or _visual_slot_state_signature(latest, scene_id, block_id)
            != _visual_slot_state_signature(base_state, scene_id, block_id)
        ):
            raise _ReviewPreviewVisualLeaseError(
                "画面格或场景在外部调用期间已变化，旧 worker 禁止提交"
            )

        latest_scene = _find(latest.get("scenes", []), scene_id, "场景")
        latest_blocks = (latest_scene.get("visual_timeline") or {}).get("blocks") or []
        latest_block_index = next(
            (index for index, entry in enumerate(latest_blocks) if str(entry.get("id") or "") == block_id),
            None,
        )
        if latest_block_index is None:
            raise _ReviewPreviewVisualLeaseError("画面格在外部调用期间已被删除，旧 worker 禁止提交")

        base_asset_ids = {str(entry.get("id") or "") for entry in base_state.get("assets", [])}
        latest_assets_by_id = {str(entry.get("id") or ""): entry for entry in latest.get("assets", [])}
        for asset in candidate_state.get("assets", []):
            asset_id = str(asset.get("id") or "")
            if not asset_id or asset_id in base_asset_ids:
                continue
            existing = latest_assets_by_id.get(asset_id)
            if existing is not None and _json_hash(existing) != _json_hash(asset):
                raise _ReviewPreviewVisualLeaseError("隔离画面资产 ID 与最新状态冲突，拒绝提升")
            if existing is None:
                copied = deepcopy(asset)
                latest.setdefault("assets", []).append(copied)
                latest_assets_by_id[asset_id] = copied

        base_usages_by_id = {
            str(entry.get("id") or ""): entry for entry in base_state.get("usages", [])
        }
        candidate_usages_by_id = {
            str(entry.get("id") or ""): entry for entry in candidate_state.get("usages", [])
        }
        latest_usages_by_id = {
            str(entry.get("id") or ""): entry for entry in latest.get("usages", [])
        }
        for usage_id, candidate_usage in candidate_usages_by_id.items():
            if str(candidate_usage.get("scene_id") or "") != scene_id:
                continue
            base_usage = base_usages_by_id.get(usage_id)
            latest_usage = latest_usages_by_id.get(usage_id)
            if base_usage is None:
                if latest_usage is not None and _json_hash(latest_usage) != _json_hash(candidate_usage):
                    raise _ReviewPreviewVisualLeaseError("画面 usage ID 与最新状态冲突，拒绝提升")
                if latest_usage is None:
                    copied = deepcopy(candidate_usage)
                    latest.setdefault("usages", []).append(copied)
                    latest_usages_by_id[usage_id] = copied
            elif _json_hash(candidate_usage) != _json_hash(base_usage):
                if latest_usage is None or _json_hash(latest_usage) != _json_hash(base_usage):
                    raise _ReviewPreviewVisualLeaseError("场景 usage 在外部调用期间已变化，拒绝覆盖")
                latest_usage.clear()
                latest_usage.update(deepcopy(candidate_usage))

        latest_blocks[latest_block_index] = deepcopy(candidate_block)
        for field in ("review_status", "review_preview"):
            if field in candidate_scene:
                latest_scene[field] = deepcopy(candidate_scene[field])
        committed_item = deepcopy(candidate_item)
        committed_item.pop("worker_claim_id", None)
        latest_item.clear()
        latest_item.update(committed_item)
        latest_batch["completed_slots"] = sum(
            1 for entry in latest_batch.get("items", []) if entry.get("status") == "completed"
        )
        latest_batch["failed_slots"] = sum(
            1 for entry in latest_batch.get("items", []) if entry.get("status") == "failed"
        )
        candidate_activities = candidate_state.get("activities") or []
        base_activity_count = len(base_state.get("activities") or [])
        for activity in candidate_activities[base_activity_count:]:
            latest.setdefault("activities", []).append(deepcopy(activity))
        latest["activities"] = latest.get("activities", [])[-120:]
        return _save(project_dir, latest)


def generate_visual_batch(
    project_dir: Path,
    expected_job_id: str | None = None,
    *,
    expected_parent_job_id: str | None = None,
    expected_worker_token: str | None = None,
    expected_request_fingerprint: str | None = None,
    expected_contract_versions: dict[str, str] | None = None,
) -> dict:
    """Run one reviewed production contract serially and persist each result."""
    with _project_transaction_lock(project_dir):
        state = _load_for_write(project_dir)
        batch = _automation(state)["visual_batch"]
        if expected_job_id and str(batch.get("job_id") or "") != expected_job_id:
            # Preserve the legacy pre-dispatch stale-worker no-op contract.
            return state
        stable_job_id = str(expected_job_id or batch.get("job_id") or "")
        if not stable_job_id:
            raise WorkbenchError("批量画面任务缺少稳定 job_id")
        if batch.get("status") not in {"queued", "generating"}:
            raise WorkbenchError("当前没有待执行的批量画面任务")
        _assert_review_preview_visual_lease(
            project_dir,
            state,
            batch,
            expected_parent_job_id=expected_parent_job_id,
            expected_worker_token=expected_worker_token,
            expected_request_fingerprint=expected_request_fingerprint,
            expected_contract_versions=expected_contract_versions,
        )
        batch["status"] = "generating"
        _save(project_dir, state)
    video_tool, image_tool = PexelsVideo(), PexelsImage()
    while True:
        with _project_transaction_lock(project_dir):
            state = _load_for_write(project_dir)
            batch = _automation(state)["visual_batch"]
            if str(batch.get("job_id") or "") != stable_job_id:
                raise _ReviewPreviewVisualLeaseError("画面子任务在格级调用前已被替换")
            _assert_review_preview_visual_lease(
                project_dir,
                state,
                batch,
                expected_parent_job_id=expected_parent_job_id,
                expected_worker_token=expected_worker_token,
                expected_request_fingerprint=expected_request_fingerprint,
                expected_contract_versions=expected_contract_versions,
            )
            item = next(
                (entry for entry in batch.get("items", []) if entry.get("status") in {"queued", "generating"}),
                None,
            )
            if item is None:
                break
            slot_claim_id = uuid4().hex
            item["status"] = "generating"
            item["worker_claim_id"] = slot_claim_id
            item["started_at"] = item.get("started_at") or _now()
            batch["status"] = "generating"
            batch["current"] = {"scene_id": item["scene_id"], "block_id": item["block_id"]}
            scene = _find(state["scenes"], str(item["scene_id"]), "场景")
            block = _find((scene.get("visual_timeline") or {}).get("blocks") or [], str(item["block_id"]), "画面槽位")
            block["status"] = "generating"
            _save(project_dir, state)
            base_state = deepcopy(state)
            state[_DEFER_VISUAL_SLOT_STATE_SAVE] = True
        provider_guard = lambda: _assert_visual_provider_io_lease(
            project_dir,
            batch,
            expected_job_id=stable_job_id,
            scene_id=str(item["scene_id"]),
            block_id=str(item["block_id"]),
            slot_claim_id=slot_claim_id,
            expected_parent_job_id=expected_parent_job_id,
            expected_worker_token=expected_worker_token,
            expected_request_fingerprint=expected_request_fingerprint,
            expected_contract_versions=expected_contract_versions,
        )
        query = str(item.get("query") or "")
        attempt = max(1, int(_as_number(item.get("attempt"), 1)))
        target_duration = max(.1, _as_number(item.get("target_duration_seconds"), _scene_duration(scene)))
        page = max(1, attempt + int(_as_number(item.get("slot_index"))) - 1)
        visual_asset: dict | None = None
        image_asset: dict | None = None
        media_kind = str(item.get("media_kind") or "video")
        source_mode = str(item.get("source_mode") or "web_download")
        content_rules = [str(value) for value in (item.get("content_rules") or [])]
        person_policy = str(item.get("person_policy") or batch.get("person_policy") or "balanced")
        route = str(item.get("route") or _visual_route_for_block(item))
        chosen_tool = "hyperframes_compose" if route == "hyperframes" else ("pexels_video" if media_kind == "video" else ("openai_image" if source_mode == "openai_image" else "pexels_image"))
        try:
            orientation = "portrait" if _render_dimensions(project_dir, state)[1] > _render_dimensions(project_dir, state)[0] else "landscape"
            used_video_ids = {
                str((asset.get("generation") or {}).get("video_id"))
                for asset in state.get("assets", [])
                if (asset.get("generation") or {}).get("video_id") is not None
            }
            used_photo_ids = {
                str((asset.get("generation") or {}).get("photo_id"))
                for asset in state.get("assets", [])
                if (asset.get("generation") or {}).get("photo_id") is not None
            }
            if route == "hyperframes":
                item["stage"] = "HyperFrames 正在按已审核画面合同渲染"
                _save(project_dir, state)
                try:
                    _assert_review_preview_visual_lease(
                        project_dir, _load_for_write(project_dir), batch,
                        expected_parent_job_id=expected_parent_job_id,
                        expected_worker_token=expected_worker_token,
                        expected_request_fingerprint=expected_request_fingerprint,
                        expected_contract_versions=expected_contract_versions,
                    )
                    visual_asset = _guarded_visual_provider_io(
                        provider_guard,
                        lambda: _generate_hyperframes_visual_block(
                            project_dir, state, scene, block, item, target_duration
                        ),
                    )
                except _ReviewPreviewVisualLeaseError:
                    raise
                except WorkbenchError as hyperframes_error:
                    if str(item.get("fallback_route") or "") != "stock_video":
                        raise
                    item["stage"] = "HyperFrames 版式未通过校验，正在回退实拍素材"
                    item["fallback_reason"] = _safe_automation_error(hyperframes_error)
                    _assert_review_preview_visual_lease(
                        project_dir, _load_for_write(project_dir), batch,
                        expected_parent_job_id=expected_parent_job_id,
                        expected_worker_token=expected_worker_token,
                        expected_request_fingerprint=expected_request_fingerprint,
                        expected_contract_versions=expected_contract_versions,
                    )
                    if str(item.get("planning_mode") or batch.get("planning_mode") or "rule_mix") == "ai_director":
                        result, path, screening = _find_autonomous_pexels_candidate(
                            project_dir, state, item, scene, block,
                            media_kind="video", query=query, orientation=orientation,
                            target_duration=target_duration, content_rules=content_rules,
                            person_policy=person_policy, used_provider_ids=used_video_ids,
                            provider_guard=provider_guard,
                        )
                    else:
                        result, path, screening = _find_screened_pexels_candidate(
                            project_dir, state, item, scene, block,
                            media_kind="video", query=query, orientation=orientation, page=page,
                            target_duration=target_duration, content_rules=content_rules,
                            person_policy=person_policy, used_provider_ids=used_video_ids, tool=video_tool,
                            provider_guard=provider_guard,
                        )
                    query = str(item.get("query") or query)
                    data = result.data or {}
                    measured_duration = _probe_duration_seconds(
                        project_dir / path, _ffmpeg_available(), _as_number(data.get("duration_seconds"))
                    )
                    visual_asset = _append_asset(project_dir, state, {
                        "name": f"{scene.get('title') or scene['id']} · {block['id']} · Pexels 视频",
                        "type": "video", "source_type": "web_download", "path": path,
                        "duration_seconds": measured_duration,
                        "resolution": f"{data.get('width') or '?'}x{data.get('height') or '?'}",
                        "provider": "Pexels", "source_tool": "pexels_video",
                        "license": data.get("license") or "Pexels License (free, no attribution required)",
                        "source_url": data.get("pexels_url"),
                        "generation": {
                            "query": query, "scene_id": scene["id"], "block_id": block["id"],
                            "video_id": data.get("video_id"), "result_page": page, "downloaded_at": _now(),
                            "content_sha256": item.get("downloaded_content_sha256"),
                            "person_policy": person_policy, "screening": screening,
                            "director_ledger": deepcopy(item.get("director_ledger") or {}),
                        },
                    })
                    item.setdefault("director_ledger", {}).update({
                        "status": "fallback_rendered", "fallback_route": "stock_video"
                    })
                    item.update({"route": "stock_video", "source_mode": "web_download", "media_kind": "video"})
                    route, source_mode, media_kind, chosen_tool = "stock_video", "web_download", "video", "pexels_video"
            elif media_kind == "video":
                _assert_review_preview_visual_lease(
                    project_dir, _load_for_write(project_dir), batch,
                    expected_parent_job_id=expected_parent_job_id,
                    expected_worker_token=expected_worker_token,
                    expected_request_fingerprint=expected_request_fingerprint,
                    expected_contract_versions=expected_contract_versions,
                )
                if str(item.get("planning_mode") or batch.get("planning_mode") or "rule_mix") == "ai_director":
                    try:
                        result, path, screening = _find_autonomous_pexels_candidate(
                            project_dir, state, item, scene, block,
                            media_kind="video", query=query, orientation=orientation,
                            target_duration=target_duration, content_rules=content_rules,
                            person_policy=person_policy, used_provider_ids=used_video_ids,
                            provider_guard=provider_guard,
                        )
                    except _ReviewPreviewVisualLeaseError:
                        raise
                    except WorkbenchError as director_error:
                        if str(item.get("fallback_route") or "") != "hyperframes":
                            raise
                        item["stage"] = "自动导演未找到合格素材，正在使用信息图安全降级"
                        item["fallback_reason"] = _safe_automation_error(director_error)
                        if not str((item.get("graphic_copy") or {}).get("headline") or "").strip():
                            item["graphic_copy"] = _rule_graphic_copy(
                                str(item.get("slot_text") or item.get("context_text") or query),
                                str(item.get("scene_recipe") or "relationship_map"),
                            )
                        _assert_review_preview_visual_lease(
                            project_dir, _load_for_write(project_dir), batch,
                            expected_parent_job_id=expected_parent_job_id,
                            expected_worker_token=expected_worker_token,
                            expected_request_fingerprint=expected_request_fingerprint,
                            expected_contract_versions=expected_contract_versions,
                        )
                        visual_asset = _guarded_visual_provider_io(
                            provider_guard,
                            lambda: _generate_hyperframes_visual_block(
                                project_dir, state, scene, block, item, target_duration
                            ),
                        )
                        item.setdefault("director_ledger", {}).update({"status": "fallback_rendered", "fallback_route": "hyperframes"})
                else:
                    result, path, screening = _find_screened_pexels_candidate(
                        project_dir, state, item, scene, block,
                        media_kind="video", query=query, orientation=orientation, page=page,
                        target_duration=target_duration, content_rules=content_rules,
                        person_policy=person_policy, used_provider_ids=used_video_ids, tool=video_tool,
                        provider_guard=provider_guard,
                    )
                if visual_asset is None:
                    query = str(item.get("query") or query)
                    data = result.data or {}
                    measured_duration = _probe_duration_seconds(project_dir / path, _ffmpeg_available(), _as_number(data.get("duration_seconds")))
                    visual_asset = _append_asset(project_dir, state, {
                        "name": f"{scene.get('title') or scene['id']} · {block['id']} · Pexels 视频",
                        "type": "video", "source_type": "web_download", "path": path,
                        "duration_seconds": measured_duration, "resolution": f"{data.get('width') or '?'}x{data.get('height') or '?'}",
                        "provider": "Pexels", "source_tool": "pexels_video",
                        "license": data.get("license") or "Pexels License (free, no attribution required)",
                        "source_url": data.get("pexels_url"),
                        "generation": {"query": query, "scene_id": scene["id"], "block_id": block["id"], "video_id": data.get("video_id"), "result_page": page, "downloaded_at": _now(), "content_sha256": item.get("downloaded_content_sha256"), "person_policy": person_policy, "screening": screening, "director_ledger": deepcopy(item.get("director_ledger") or {})},
                    })
            else:
                image_output = project_dir / "assets" / "images" / ("openai" if source_mode == "openai_image" else "pexels") / f"{scene['id']}-{block['id']}-{uuid4().hex[:8]}.png"
                if source_mode == "openai_image":
                    _assert_review_preview_visual_lease(
                        project_dir, _load_for_write(project_dir), batch,
                        expected_parent_job_id=expected_parent_job_id,
                        expected_worker_token=expected_worker_token,
                        expected_request_fingerprint=expected_request_fingerprint,
                        expected_contract_versions=expected_contract_versions,
                    )
                    prompt = _visual_ai_prompt(str(item.get("context_text") or block.get("context_text") or query), content_rules, "竖屏" if orientation == "portrait" else "横屏", person_policy)
                    size = "1024x1536" if orientation == "portrait" else "1536x1024"
                    image_result = _guarded_visual_provider_io(
                        provider_guard,
                        lambda: OpenAIImage().execute({
                            "prompt": prompt, "model": "gpt-image-2", "size": size,
                            "quality": "low", "output_format": "png", "n": 1,
                            "output_path": str(image_output),
                        }),
                    )
                else:
                    _assert_review_preview_visual_lease(
                        project_dir, _load_for_write(project_dir), batch,
                        expected_parent_job_id=expected_parent_job_id,
                        expected_worker_token=expected_worker_token,
                        expected_request_fingerprint=expected_request_fingerprint,
                        expected_contract_versions=expected_contract_versions,
                    )
                    if str(item.get("planning_mode") or batch.get("planning_mode") or "rule_mix") == "ai_director":
                        try:
                            image_result, path, screening = _find_autonomous_pexels_candidate(
                                project_dir, state, item, scene, block,
                                media_kind="image", query=query, orientation=orientation,
                                target_duration=target_duration, content_rules=content_rules,
                                person_policy=person_policy, used_provider_ids=used_photo_ids,
                                provider_guard=provider_guard,
                            )
                        except _ReviewPreviewVisualLeaseError:
                            raise
                        except WorkbenchError as director_error:
                            if str(item.get("fallback_route") or "") != "hyperframes":
                                raise
                            item["stage"] = "自动导演未找到合格素材，正在使用信息图安全降级"
                            item["fallback_reason"] = _safe_automation_error(director_error)
                            if not str((item.get("graphic_copy") or {}).get("headline") or "").strip():
                                item["graphic_copy"] = _rule_graphic_copy(
                                    str(item.get("slot_text") or item.get("context_text") or query),
                                    str(item.get("scene_recipe") or "relationship_map"),
                                )
                            _assert_review_preview_visual_lease(
                                project_dir, _load_for_write(project_dir), batch,
                                expected_parent_job_id=expected_parent_job_id,
                                expected_worker_token=expected_worker_token,
                                expected_request_fingerprint=expected_request_fingerprint,
                                expected_contract_versions=expected_contract_versions,
                            )
                            visual_asset = _guarded_visual_provider_io(
                                provider_guard,
                                lambda: _generate_hyperframes_visual_block(
                                    project_dir, state, scene, block, item, target_duration
                                ),
                            )
                            item.setdefault("director_ledger", {}).update({"status": "fallback_rendered", "fallback_route": "hyperframes"})
                    else:
                        image_result, path, screening = _find_screened_pexels_candidate(
                            project_dir, state, item, scene, block,
                            media_kind="image", query=query, orientation=orientation, page=page,
                            target_duration=target_duration, content_rules=content_rules,
                            person_policy=person_policy, used_provider_ids=used_photo_ids, tool=image_tool,
                            provider_guard=provider_guard,
                        )
                    query = str(item.get("query") or query)
                if visual_asset is None:
                    if not image_result.success or not image_result.artifacts:
                        raise WorkbenchError(_safe_automation_error(image_result.error or "图片服务未返回匹配素材"))
                    if source_mode == "openai_image":
                        path = _safe_relpath(project_dir, image_result.artifacts[0])
                        if not path or not (project_dir / path).is_file():
                            raise WorkbenchError("图片服务未返回项目内可登记的图片文件")
                        screening = _screen_visual_candidate(project_dir / path, "image", content_rules, person_policy)
                        item["screening"] = screening
                        if screening["status"] == "rejected":
                            (project_dir / path).unlink(missing_ok=True)
                            raise WorkbenchError(f"AI 图片未通过人物策略：{'、'.join(screening['reasons'])}")
                    data = image_result.data or {}
                    official_press = bool(data.get("official_press"))
                    image_asset = _append_asset(project_dir, state, {
                        "name": f"{scene.get('title') or scene['id']} · {block['id']} · {'OpenAI 图片' if source_mode == 'openai_image' else ('官方配图' if official_press else 'Pexels 图片')}",
                        "type": "image", "source_type": "ai_generated" if source_mode == "openai_image" else "web_download", "path": path,
                        "resolution": f"{data.get('width') or '?'}x{data.get('height') or '?'}" if source_mode != "openai_image" else size,
                        "provider": "OpenAI" if source_mode == "openai_image" else ("官方媒体" if official_press else "Pexels"), "source_tool": chosen_tool,
                        "license": "AI 生成；请按项目发布规范复核" if source_mode == "openai_image" else (data.get("license") or "Pexels License (free, no attribution required)"),
                        "source_url": data.get("pexels_url"),
                        "attribution": data.get("attribution") or "",
                        "generation": {"query": query, "prompt": prompt if source_mode == "openai_image" else None, "model": "gpt-image-2" if source_mode == "openai_image" else None, "scene_id": scene["id"], "block_id": block["id"], "photo_id": data.get("photo_id"), "result_page": page, "downloaded_at": _now(), "content_sha256": item.get("downloaded_content_sha256") if source_mode != "openai_image" else None, "content_rules": content_rules, "person_policy": person_policy, "screening": screening, "director_ledger": deepcopy(item.get("director_ledger") or {})},
                    })
                    visual_asset = _motion_video_from_stock_image(
                        project_dir, state, scene, image_asset,
                        duration_seconds=target_duration, output_suffix=f"{block['id']}-{uuid4().hex[:6]}",
                    )
            if visual_asset is None:
                raise WorkbenchError("Pexels 槽位任务未产生可用素材")
            usage = _append_visual_block_usage(state, scene, block, visual_asset["id"])
            block.update({
                "asset_id": visual_asset["id"], "usage_id": usage["id"],
                "source_mode": source_mode, "media_kind": media_kind, "label": str(visual_asset.get("name") or visual_asset["id"]),
                "route": route,
                "status": "ready", "attempt": attempt, "query": query, "error": "",
            })
            item.update({"status": "completed", "stage": "已完成", "asset_id": visual_asset["id"], "usage_id": usage["id"], "tool": chosen_tool, "finished_at": _now(), "error": ""})
            _invalidate_scene_review_preview(scene, f"{block['id']} 已更新，请刷新本段审核预览")
            scene["review_status"] = "needs_adjustment"
            ready_blocks = [entry for entry in (scene.get("visual_timeline") or {}).get("blocks", []) if entry.get("asset_id") and entry.get("status") == "ready"]
            if ready_blocks:
                lead = ready_blocks[0]
                for usage_record in state.get("usages", []):
                    if usage_record.get("scene_id") == scene["id"] and usage_record.get("role") == "visual":
                        usage_record["selected"] = False
                _append_selected_usage(state, scene["id"], str(lead["asset_id"]), "visual")
            _activity(state, "visual_block_generated", f"{scene['id']} 的 {block['id']} 已采用 {visual_asset['id']}（{usage['id']}）", scene_id=scene["id"], block_id=block["id"], asset_id=visual_asset["id"])
        except _ReviewPreviewVisualLeaseError:
            raise
        except Exception as exc:
            detail = _safe_automation_error(exc)
            block.update({"status": "failed", "error": detail, "attempt": attempt})
            item.update({"status": "failed", "stage": "筛选失败", "finished_at": _now(), "error": detail})
            _activity(state, "visual_block_generation_failed", f"{scene['id']} 的 {block['id']} 生成失败：{detail}", scene_id=scene["id"], block_id=block["id"])
        batch["completed_slots"] = sum(1 for entry in batch.get("items", []) if entry.get("status") == "completed")
        batch["failed_slots"] = sum(1 for entry in batch.get("items", []) if entry.get("status") == "failed")
        _commit_visual_batch_slot(
            project_dir,
            base_state,
            state,
            expected_job_id=stable_job_id,
            scene_id=str(item["scene_id"]),
            block_id=str(item["block_id"]),
            slot_claim_id=slot_claim_id,
            expected_parent_job_id=expected_parent_job_id,
            expected_worker_token=expected_worker_token,
            expected_request_fingerprint=expected_request_fingerprint,
            expected_contract_versions=expected_contract_versions,
        )
    with _project_transaction_lock(project_dir):
        state = _load_for_write(project_dir)
        batch = _automation(state)["visual_batch"]
        if str(batch.get("job_id") or "") != stable_job_id:
            raise _ReviewPreviewVisualLeaseError("画面子任务在最终提交前已被替换")
        _assert_review_preview_visual_lease(
            project_dir,
            state,
            batch,
            expected_parent_job_id=expected_parent_job_id,
            expected_worker_token=expected_worker_token,
            expected_request_fingerprint=expected_request_fingerprint,
            expected_contract_versions=expected_contract_versions,
        )
        batch["current"] = None
        batch["finished_at"] = _now()
        batch["completed_slots"] = sum(1 for entry in batch.get("items", []) if entry.get("status") == "completed")
        batch["failed_slots"] = sum(1 for entry in batch.get("items", []) if entry.get("status") == "failed")
        batch["status"] = "completed_with_failures" if batch["failed_slots"] else "completed"
        _mark_render_needs_refresh(state, "批量画面素材已更新")
        _activity(state, "visual_batch_finished", f"批量画面完成：{batch['completed_slots']}/{batch['total_slots']} 个槽位成功", job_id=batch.get("job_id"))
        return _save(project_dir, state)


def mark_visual_batch_failed(project_dir: Path, error: object, expected_job_id: str | None = None) -> dict:
    state = _load_for_write(project_dir)
    batch = _automation(state)["visual_batch"]
    if expected_job_id and str(batch.get("job_id") or "") != expected_job_id:
        return state
    batch.update({"status": "failed", "finished_at": _now(), "error": _safe_automation_error(error), "current": None})
    _activity(state, "visual_batch_failed", f"批量画面任务失败：{batch['error']}", job_id=batch.get("job_id"))
    return _save(project_dir, state)


@_project_transactional
def start_network_asset_generation(project_dir: Path, payload: dict) -> dict:
    """Persist a duration-aware Pexels job after natural narration is ready."""
    if payload.get("confirmed") is not True:
        raise WorkbenchError("请确认后再调用 Pexels 自动搜集网络素材")
    state = _load_for_write(project_dir)
    _require_no_review_preview_conflict(_automation(state))
    automation = _automation(state)
    asset_job = automation["asset_generation"]
    if asset_job.get("status") == "generating":
        raise WorkbenchError("网络素材正在自动搜集，请不要重复点击")
    if not os.environ.get("PEXELS_API_KEY"):
        raise WorkbenchError("Pexels 素材服务尚未配置，请先在服务器环境中设置 PEXELS_API_KEY")
    narration_job = automation["narration_generation"]
    if automation.get("audio_mode") == "generated_narration" and narration_job.get("status") != "completed":
        raise WorkbenchError("请先生成并试听项目旁白。系统需要根据真实配音时长搜索、裁切和组合画面素材。")

    target_ids: list[str] = []
    for scene in state.get("scenes", []):
        if scene.get("source_strategy") == "web_download":
            target_ids.append(scene["id"])
        elif scene.get("source_strategy") == "undecided" and payload.get("fill_undecided", True):
            scene["source_strategy"] = "web_download"
            target_ids.append(scene["id"])
            _decision(state, "source_strategy", f"{scene['id']} 素材来源", "web_download", "自动生产模式：沿用项目的 Pexels 网络素材策略")
    if not target_ids:
        raise WorkbenchError("请至少为一个场景选择“网络下载”后再开始生成")

    automation["status"] = "generating_assets"
    automation["asset_generation"] = {
        "status": "generating", "started_at": _now(), "scene_ids": target_ids,
        "total_scenes": len(target_ids), "completed_scenes": 0, "failed_scenes": [],
        "provider": "pexels", "mode": "batch", "search_page": 1,
        "strategy": "以真实配音时长筛选视频；无可用视频时使用 Pexels 图片生成同长本地动态片段", "error": "",
    }
    _decision(state, "network_asset_policy", "自动网络素材策略", "按旁白时长匹配 Pexels 视频", "视频先按真实配音时长筛选；无匹配视频时使用 Pexels 图片生成同长本地动态片段；每项素材保留 S-xxx、来源链接和许可")
    _activity(state, "network_asset_generation_started", f"已开始按真实旁白时长通过 Pexels 为 {len(target_ids)} 个场景自动搜集素材", scene_ids=target_ids)
    return _save(project_dir, state)


@_project_transactional
def start_scene_network_asset_refresh(project_dir: Path, scene_id: str, payload: dict) -> dict:
    """Queue a Pexels replacement for one scene without touching its neighbours.

    The old S-/U- records remain append-only for audit.  Only the selected
    visual U-record for ``scene_id`` is replaced after a new candidate has
    actually been downloaded and registered.
    """
    state = _load_for_write(project_dir)
    _require_no_review_preview_conflict(_automation(state))
    if payload.get("confirmed") is not True:
        raise WorkbenchError("请确认后再重新搜索当前场景的 Pexels 素材")
    automation = _automation(state)
    if automation["asset_generation"].get("status") == "generating":
        raise WorkbenchError("已有网络素材任务正在运行，请等待它完成后再更换当前场景素材")
    if not os.environ.get("PEXELS_API_KEY"):
        raise WorkbenchError("Pexels 素材服务尚未配置，请先在服务器环境中设置 PEXELS_API_KEY")

    scene = _find(state["scenes"], scene_id, "场景")
    if scene.get("source_strategy") != "web_download":
        raise WorkbenchError("请先将当前场景的素材来源切换为“网络下载”，再一键换素材")
    previous_usage = next((
        usage for usage in state.get("usages", [])
        if usage.get("scene_id") == scene_id and usage.get("role") == "visual" and usage.get("selected")
    ), None)
    previous_asset_id = previous_usage.get("asset_id") if previous_usage else None
    instruction = str(payload.get("instruction") or "").strip()[:600]
    refresh_count = max(0, int(_as_number(scene.get("asset_refresh_count")))) + 1
    scene["asset_refresh_count"] = refresh_count
    scene["asset_refresh_instruction"] = instruction
    scene["review_status"] = "needs_adjustment"
    _invalidate_scene_review_preview(scene, "已要求重新搜索主体素材，请在素材更新后刷新审核预览")
    previous_review = scene.get("keyframe_review") or {}
    if previous_review.get("id"):
        previous_review["status"] = "superseded"
        previous_review["review_note"] = "已一键更换当前场景素材，原关键帧仅保留作追溯。"
        for record in state.get("keyframe_reviews", []):
            if record.get("id") == previous_review.get("id"):
                record["status"] = "superseded"
    for anchor in scene.get("anchors", []):
        anchor["status"] = "pending"
        anchor["note"] = "已更换素材，等待重新审核。"
        anchor.pop("reviewed_at", None)
    _mark_render_needs_refresh(state, f"{scene_id} 的已用素材已更换，需要重新合成视频。")

    variant_count = 4
    search_page = ((refresh_count - 1) // variant_count) + 1
    automation["status"] = "generating_assets"
    automation["asset_generation"] = {
        "status": "generating", "started_at": _now(), "scene_ids": [scene_id],
        "total_scenes": 1, "completed_scenes": 0, "failed_scenes": [],
        "provider": "pexels", "mode": "scene_refresh", "search_page": search_page,
        "refresh": {
            "scene_id": scene_id,
            "previous_asset_id": previous_asset_id,
            "attempt": refresh_count,
            "instruction": instruction,
        },
        "strategy": "仅替换当前场景；优先使用不同的 Pexels 检索角度，旧素材和使用记录保留在台账中",
        "error": "",
    }
    _decision(
        state,
        "asset_refresh",
        f"{scene_id} 素材返工",
        "Pexels 当前场景一键换素材",
        instruction or "未填写额外指令；系统将自动切换检索角度并保留旧素材。",
    )
    _activity(
        state,
        "network_asset_refresh_started",
        f"已开始仅为 {scene_id} 重新搜索 Pexels 素材；其他场景保持不变。",
        scene_id=scene_id,
        previous_asset_id=previous_asset_id,
        refresh_attempt=refresh_count,
    )
    return _save(project_dir, state)


def mark_network_asset_generation_failed(project_dir: Path, error: object) -> dict:
    state = _load_for_write(project_dir)
    automation = _automation(state)
    automation["status"] = "failed"
    automation["asset_generation"].update({"status": "failed", "finished_at": _now(), "error": _safe_automation_error(error)})
    _activity(state, "network_asset_generation_failed", f"Pexels 自动素材搜集失败：{automation['asset_generation']['error']}")
    return _save(project_dir, state)


def generate_network_assets(project_dir: Path) -> dict:
    """Download and register one auditable Pexels visual per selected scene."""
    state = _load_for_write(project_dir)
    automation = _automation(state)
    job = automation["asset_generation"]
    if job.get("status") != "generating":
        raise WorkbenchError("当前没有待执行的 Pexels 自动素材任务")

    scene_ids = [str(value) for value in job.get("scene_ids", [])]
    video_tool, image_tool = PexelsVideo(), PexelsImage()
    for scene_id in scene_ids:
        state = _load_for_write(project_dir)
        automation = _automation(state)
        job = automation["asset_generation"]
        if scene_id in {item.get("scene_id") for item in job.get("completed", []) if isinstance(item, dict)}:
            continue
        scene = _find(state["scenes"], scene_id, "场景")
        query, original_context = _stock_query_for_scene(scene, surrounding_context=_scene_surrounding_context(state, scene))
        search_page = max(1, int(_as_number(job.get("search_page"), 1)))
        target_duration = max(.1, _as_number(scene.get("end_seconds")) - _as_number(scene.get("start_seconds")))
        visual_asset: dict | None = None
        review_image_asset: dict | None = None
        chosen_tool = "pexels_video"
        try:
            video_output = project_dir / "assets" / "video" / "pexels" / f"{scene_id}-{uuid4().hex[:8]}.mp4"
            result = video_tool.execute({
                "query": query, "orientation": "portrait" if _render_dimensions(project_dir, state)[1] > _render_dimensions(project_dir, state)[0] else "landscape",
                "size": "medium", "min_duration": max(1, int(math.ceil(target_duration))), "preferred_quality": "hd", "page": search_page, "output_path": str(video_output),
            })
            if result.success and result.artifacts:
                path = _safe_relpath(project_dir, result.artifacts[0])
                if not path or not (project_dir / path).is_file():
                    raise WorkbenchError("Pexels 未返回项目内可登记的视频文件")
                data = result.data or {}
                measured_duration = _probe_duration_seconds(project_dir / path, _ffmpeg_available(), _as_number(data.get("duration_seconds")))
                visual_asset = _append_asset(project_dir, state, {
                    "name": f"{scene.get('title') or scene_id} · Pexels 视频", "type": "video", "source_type": "web_download", "path": path,
                    "duration_seconds": measured_duration, "resolution": f"{data.get('width') or '?'}x{data.get('height') or '?'}",
                    "provider": "Pexels", "source_tool": "pexels_video", "license": data.get("license") or "Pexels License (free, no attribution required)",
                    "source_url": data.get("pexels_url"), "generation": {"query": query, "original_context": original_context, "video_id": data.get("video_id"), "result_page": search_page, "downloaded_at": _now()},
                })
            else:
                chosen_tool = "pexels_image"
                image_output = project_dir / "assets" / "images" / "pexels" / f"{scene_id}-{uuid4().hex[:8]}.jpg"
                image_result = image_tool.execute({"query": query, "orientation": "portrait" if _render_dimensions(project_dir, state)[1] > _render_dimensions(project_dir, state)[0] else "landscape", "size": "large", "page": search_page, "output_path": str(image_output)})
                if not image_result.success or not image_result.artifacts:
                    raise WorkbenchError(_safe_automation_error(image_result.error or result.error or "Pexels 未返回匹配素材"))
                path = _safe_relpath(project_dir, image_result.artifacts[0])
                if not path or not (project_dir / path).is_file():
                    raise WorkbenchError("Pexels 未返回项目内可登记的图片文件")
                data = image_result.data or {}
                review_image_asset = _append_asset(project_dir, state, {
                    "name": f"{scene.get('title') or scene_id} · Pexels 图片", "type": "image", "source_type": "web_download", "path": path,
                    "resolution": f"{data.get('width') or '?'}x{data.get('height') or '?'}", "provider": "Pexels", "source_tool": "pexels_image",
                    "license": data.get("license") or "Pexels License (free, no attribution required)", "source_url": data.get("pexels_url"),
                    "generation": {"query": query, "original_context": original_context, "photo_id": data.get("photo_id"), "result_page": search_page, "downloaded_at": _now()},
                })
                visual_asset = _motion_video_from_stock_image(project_dir, state, scene, review_image_asset)

            if visual_asset is None:
                raise WorkbenchError("Pexels 素材任务未产生可用视频")
            _append_selected_usage(state, scene_id, visual_asset["id"], "visual")
            scene["visual_fit"] = _visual_fit_plan(project_dir, state, scene, visual_asset)
            _stock_review_timeline(project_dir, state, scene, visual_asset, source_tool=chosen_tool, query=query, review_image_asset=review_image_asset)
            job.setdefault("completed", []).append({"scene_id": scene_id, "asset_id": visual_asset["id"], "query": query, "page": search_page, "tool": chosen_tool})
            job["completed_scenes"] = len(job["completed"])
            action = "已一键换用" if job.get("mode") == "scene_refresh" else "已通过 Pexels 生成"
            _activity(state, "network_asset_generation", f"{scene_id} {action}素材 {visual_asset['id']}，并创建首帧与高潮帧审核图", scene_id=scene_id, asset_id=visual_asset["id"])
        except Exception as exc:  # keep other scenes resumable when one query misses.
            detail = _safe_automation_error(exc)
            job.setdefault("failed_scenes", []).append({"scene_id": scene_id, "error": detail, "at": _now()})
            _activity(state, "network_asset_generation_failed_scene", f"{scene_id} 的 Pexels 素材搜集失败：{detail}", scene_id=scene_id)
        _save(project_dir, state)

    state = _load_for_write(project_dir)
    automation = _automation(state)
    job = automation["asset_generation"]
    job["finished_at"] = _now()
    job["status"] = "completed" if not job.get("failed_scenes") else "completed_with_warnings"
    automation["status"] = "assets_ready" if job["status"] == "completed" else "assets_ready_with_warnings"
    _activity(state, "network_asset_generation_finished", f"Pexels 自动素材搜集完成：{job.get('completed_scenes', 0)}/{job.get('total_scenes', 0)} 个场景可进入配音与合成")
    return _save(project_dir, state)


def _srt_time(seconds: float) -> str:
    milliseconds = max(0, int(round(seconds * 1000)))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    whole_seconds, ms = divmod(remainder, 1_000)
    return f"{hours:02d}:{minutes:02d}:{whole_seconds:02d},{ms:03d}"


def _split_subtitle_phrases(text: str, max_chars: int = 18) -> list[str]:
    """Split narration into short, natural Chinese subtitle phrases.

    A scene is a production unit, not a subtitle unit.  The previous
    implementation wrote the whole scene paragraph as one SRT cue, which
    caused a long multi-line block to cover most of a portrait frame.  Keep
    sentence punctuation with the preceding phrase, then use commas (and
    finally a hard character limit) only when a sentence is still too long.
    """
    normalized = re.sub(r"\s+", " ", str(text or "")).strip()
    normalized = re.sub(
        r"(?<=[\u4e00-\u9fff，。！？、；：])\s+|\s+(?=[\u4e00-\u9fff，。！？、；：])",
        "",
        normalized,
    )
    if not normalized:
        return []

    sentence_parts = re.findall(r".+?[。！？!?；;]+|.+$", normalized)
    phrases: list[str] = []
    break_chars = set("，、,：:")

    for sentence in sentence_parts:
        remainder = sentence
        while len(remainder) > max_chars:
            window = remainder[: max_chars + 1]
            comma_breaks = [index + 1 for index, char in enumerate(window) if char in break_chars]
            cut = max(comma_breaks) if comma_breaks else max_chars
            if not comma_breaks:
                latin_group = next((
                    match for match in re.finditer(
                        r"[A-Za-z0-9][A-Za-z0-9._+/-]*(?: [A-Za-z0-9][A-Za-z0-9._+/-]*)+",
                        remainder,
                    )
                    if match.start() < cut < match.end()
                ), None)
                if latin_group is not None:
                    cut = latin_group.start() if latin_group.start() > 0 else latin_group.end()
            piece = remainder[:cut].strip()
            if piece:
                phrases.append(piece)
            remainder = remainder[cut:].lstrip("，、,：:")
        if remainder:
            phrases.append(remainder)

    return phrases


def _subtitle_cues(
    scene: dict,
    text: str,
    *,
    duration_seconds: float | None = None,
    relative_to_scene: bool = False,
) -> list[dict]:
    """Create short phrase cues derived from the committed voice duration.

    Voicebox does not currently return word timestamps.  Character-weighted
    phrasing is the deterministic fallback, but its window is always the
    natural take duration, never a stale script estimate.
    """
    scene_start = 0.0 if relative_to_scene else _as_number(scene.get("start_seconds"))
    scene_duration = max(0.1, _as_number(duration_seconds, _scene_duration(scene)))
    scene_end = scene_start + scene_duration
    phrases = _split_subtitle_phrases(text)
    if not phrases:
        return []
    weights = [max(1, len(re.sub(r"[^\w\u4e00-\u9fff]", "", phrase))) for phrase in phrases]
    total_weight = sum(weights)
    cursor = scene_start
    cues: list[dict] = []
    for phrase_index, (phrase, weight) in enumerate(zip(phrases, weights)):
        cue_end = scene_end if phrase_index == len(phrases) - 1 else cursor + scene_duration * weight / total_weight
        cue_id = _subtitle_cue_id(phrase_index)
        cues.append({
            "id": cue_id,
            "start_seconds": round(cursor, 3),
            "end_seconds": round(cue_end, 3),
            "text": _subtitle_cue_text(scene, phrase_index, phrase),
        })
        cursor = cue_end
    return cues


def _write_srt(path: Path, cues: list[dict]) -> Path:
    lines: list[str] = []
    for cue_index, cue in enumerate(cues, 1):
        lines.extend([
            str(cue_index),
            f"{_srt_time(_as_number(cue.get('start_seconds')))} --> {_srt_time(_as_number(cue.get('end_seconds')))}",
            str(cue.get("text") or ""),
            "",
        ])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _write_scene_subtitles(
    project_dir: Path,
    scene: dict,
    text: str,
    version_id: str,
    *,
    duration_seconds: float | None = None,
) -> tuple[Path, list[dict]]:
    # A candidate is burned into an isolated B segment, so its SRT begins at
    # zero.  The project SRT is rebuilt separately with absolute offsets.
    cues = _subtitle_cues(scene, text, duration_seconds=duration_seconds, relative_to_scene=True)
    path = project_dir / "assets" / "subtitles" / scene["id"] / f"{version_id}.srt"
    return _write_srt(path, cues), cues


def _write_subtitles(
    project_dir: Path,
    scenes: list[dict],
    sections: dict[str, dict],
    *,
    output_path: Path | None = None,
) -> Path:
    path = output_path or (project_dir / "assets" / "subtitles.srt")
    lines: list[str] = []
    cue_index = 1
    for scene in scenes:
        narration = scene.get("narration") if isinstance(scene.get("narration"), dict) else {}
        current_version = next((
            item for item in narration.get("versions", [])
            if item.get("id") == narration.get("current_version_id")
        ), None)
        line_cues = (
            current_version.get("subtitle_cues")
            if isinstance(current_version, dict) and isinstance(current_version.get("subtitle_cues"), list)
            else None
        )
        if line_cues:
            scene_start = _as_number(scene.get("start_seconds"))
            for cue in line_cues:
                cursor = scene_start + _as_number(cue.get("start_seconds"))
                cue_end = scene_start + _as_number(cue.get("end_seconds"))
                lines.extend([
                    str(cue_index),
                    f"{_srt_time(cursor)} --> {_srt_time(cue_end)}",
                    str(cue.get("text") or ""),
                    "",
                ])
                cue_index += 1
            continue
        section = sections.get(str(scene.get("script_section_id"))) or {}
        text = str(narration.get("text") or section.get("text") or scene.get("description") or "").strip()
        if not text:
            continue
        for cue in _subtitle_cues(scene, text):
            cursor = _as_number(cue.get("start_seconds"))
            cue_end = _as_number(cue.get("end_seconds"))
            lines.extend([
                str(cue_index),
                f"{_srt_time(cursor)} --> {_srt_time(cue_end)}",
                str(cue.get("text") or ""),
                "",
            ])
            cue_index += 1
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _validate_fixed_slot_audio(source: Path, scene: dict, ffmpeg: str | None = None) -> float:
    """Permit fixed-duration audio only when it already naturally fits.

    The old implementation used ``atempo`` to stretch every take to the
    script slot.  That makes cloned voices visibly and audibly unnatural.  A
    strict slot now rejects a material mismatch instead of silently changing
    speaking cadence.
    """
    target = _scene_duration(scene)
    duration = _probe_duration_seconds(source, ffmpeg, target)
    if abs(duration - target) > FIXED_SLOT_AUDIO_TOLERANCE_SECONDS:
        raise WorkbenchError(
            f"候选配音自然时长为 {duration:.2f} 秒，而固定时间槽为 {target:.2f} 秒；"
            "差异过大。请采用“自然语速并调整时间线”，或修改脚本后重新配音。"
        )
    return duration


def _concat_audio(
    project_dir: Path,
    parts: list[Path],
    *,
    output_path: Path | None = None,
) -> Path:
    from backlot import narration_lines as _narration_lines

    ffmpeg = _ffmpeg_available()
    if not ffmpeg:
        raise WorkbenchError("本机未发现 FFmpeg，无法拼接本地配音")
    if not parts:
        raise WorkbenchError("本地配音拼接没有可用的逐句音频")
    inspected_parts = [_narration_lines.inspect_pcm_wav(Path(part)) for part in parts]
    expected_duration = sum(float(item["duration_seconds"]) for item in inspected_parts)
    output = output_path or project_dir / "assets" / "audio" / "voicebox" / "project-narration.wav"
    listing = output.with_name(f".{output.stem}-{uuid4().hex[:8]}.txt")
    temporary = output.with_name(f".{output.stem}-{uuid4().hex[:12]}.wav")
    output.parent.mkdir(parents=True, exist_ok=True)
    listing.write_text("".join(f"file '{str(part.resolve()).replace('\\\\', '/')}'\n" for part in parts), encoding="utf-8")
    try:
        ok, detail = _run_media([ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", str(listing), "-c:a", "pcm_s16le", str(temporary)])
        if not ok or not temporary.is_file():
            raise WorkbenchError(f"本地配音拼接失败：{detail}")
        media = _narration_lines.inspect_pcm_wav(temporary)
        tolerance = max(0.05, len(parts) * 0.01)
        if abs(float(media["duration_seconds"]) - expected_duration) > tolerance:
            raise WorkbenchError(
                f"本地配音拼接时长校验失败：预期 {expected_duration:.3f} 秒，实测 {media['duration_seconds']:.3f} 秒"
            )
        os.replace(temporary, output)
    finally:
        try:
            listing.unlink()
        except OSError:
            pass
        try:
            temporary.unlink()
        except OSError:
            pass
    _narration_lines.inspect_pcm_wav(output)
    return output


def _automation_asset_manifest(project_dir: Path, state: dict) -> dict:
    assets: list[dict] = []
    for asset in state.get("assets", []):
        path = asset.get("path")
        if not path or not (project_dir / path).is_file():
            continue
        assets.append({
            "id": asset["id"], "type": asset.get("type") or "unknown", "path": str((project_dir / path).resolve()),
            "source_tool": (asset.get("provenance") or {}).get("source_tool"), "provider": (asset.get("provenance") or {}).get("provider"),
            "license": (asset.get("provenance") or {}).get("license"), "duration_seconds": asset.get("duration_seconds"),
        })
    return {"version": "1.0", "assets": assets, "total_cost_usd": 0.0, "generation_summary": {"automated": True, "asset_count": len(assets)}}


def _scene_needs_main_visual(scene: dict) -> bool:
    presenter = _scene_presenter(scene)
    return presenter.get("treatment") != "fullscreen"


def _scene_has_renderable_visual(state: dict, scene: dict) -> bool:
    """Return final technical readiness from independent presenter/content tracks."""
    return _scene_is_renderable(state, scene)


def _full_preview_summary(state: dict) -> dict:
    """Expose technical readiness separately from human approval.

    The UI uses this contract for the two-stage flow: a technically complete
    project can be rendered into a review candidate even while every scene is
    still marked ``needs_adjustment``.  Formal publication remains gated by
    the human approvals below.
    """
    automation = _automation(state)
    scenes = [scene for scene in state.get("scenes", []) if isinstance(scene, dict)]
    missing = [str(scene.get("id") or "") for scene in scenes if not _scene_has_renderable_visual(state, scene)]
    approved = [str(scene.get("id") or "") for scene in scenes if scene.get("review_status") == "approved"]
    preview = automation.get("preview_render") or {}
    return {
        "technical_ready": bool(scenes) and not missing,
        "missing_scene_ids": missing,
        "total_scenes": len(scenes),
        "approved_scene_ids": approved,
        "approved_count": len(approved),
        "all_scenes_approved": bool(scenes) and len(approved) == len(scenes),
        "preview": deepcopy(preview),
    }


def _require_ready_network_assets(state: dict, automation: dict) -> None:
    """Require a main visual only where the selected layout needs one.

    A full-screen avatar scene is already a complete visual.  Treating it as
    a missing Pexels scene would make an all-avatar video impossible to render
    and would wrongly send the user back into a stock-material flow.
    """
    required_scenes = [
        scene for scene in state.get("scenes", [])
        if isinstance(scene, dict) and _scene_needs_main_visual(scene)
    ]
    missing = [
        str(scene.get("id") or "") for scene in required_scenes
        if not _scene_has_renderable_visual(state, scene)
    ]
    if not missing:
        return
    raise WorkbenchError("仍有需要主体画面的场景没有可合成素材；请先补齐下列场景的已选素材或完整画面时间线：" + "、".join(missing))


@_project_transactional
def start_project_narration(project_dir: Path, payload: dict) -> dict:
    """Queue project narration using the software-wide default Voicebox profile."""
    if payload.get("confirmed") is not True:
        raise WorkbenchError("请确认后再按通用默认音色生成项目旁白")
    state = _load_for_write(project_dir)
    automation = _automation(state)
    _require_no_review_preview_conflict(
        automation, payload.get("_review_preview_job_id"), payload.get("_review_preview_worker_token"), payload.get("_review_preview_internal_capability")
    )
    avatar_package = read_avatar_package(project_dir)
    if avatar_package and avatar_package.get("audio_mode") == "native_avatar_audio":
        automation["audio_mode"] = "native_avatar_audio"
        _save(project_dir, state)
        raise WorkbenchError("当前项目使用数字人原生音频；请在“数字人导入”中完成台词核验和母版合成，系统不会重复生成 TTS")
    narration_job = automation["narration_generation"]
    if narration_job.get("status") == "generating":
        raise WorkbenchError("项目旁白正在生成，请不要重复点击")
    voice = get_default_voice()
    if not voice or not voice.get("id") or voice.get("available") is False:
        raise WorkbenchError("尚未在通用配音中心设置可用默认音色，请先选择并试听")
    provider_id = str(voice.get("provider_id") or "voicebox_tts")
    provider_name = str(voice.get("provider_name") or "Haike Video 本地配音")
    automation["status"] = "generating_narration"
    automation["voice"] = {
        "provider": provider_id, "provider_name": provider_name, "source": "audio_center", "label": voice["name"],
        "profile_id": voice["id"], "profile_name": voice["name"],
        "default_engine": voice.get("default_engine"),
    }
    automation["narration_generation"] = {
        "status": "generating", "stage": "voice", "started_at": _now(),
        "completed_scenes": 0, "total_scenes": len(state.get("scenes", [])),
        "audio_path": None, "subtitle_path": None, "error": "",
    }
    _mark_render_needs_refresh(state, "项目旁白将重新生成，原全片预览与正式成片会在新时间线下过期")
    automation["render"] = {"status": "awaiting_narration", "runtime": "ffmpeg", "output_path": None, "error": ""}
    _decision(state, "voice_selection", "旁白配音", f"{provider_name} / {voice['name']}", "引用通用配音中心的当前默认音色；任务已冻结供应商和音色，后续修改通用默认不会影响本次生成。")
    _activity(state, "narration_generation_started", f"开始用通用音色“{voice['name']}”生成项目旁白；完成后将以真实时长建立时间线")
    return _save(project_dir, state)


def mark_project_narration_failed(project_dir: Path, error: object) -> dict:
    state = _load_for_write(project_dir)
    automation = _automation(state)
    message = _safe_automation_error(error)
    automation["status"] = "narration_failed"
    automation["narration_generation"].update({"status": "failed", "finished_at": _now(), "error": message})
    automation["render"].update({"status": "awaiting_narration", "error": ""})
    _activity(state, "narration_generation_failed", f"项目旁白生成失败：{message}")
    return _save(project_dir, state)


def generate_project_narration(project_dir: Path) -> dict:
    """Generate natural narration first, then commit it as the master clock."""
    state = _load_for_write(project_dir)
    automation = _automation(state)
    narration_job = automation["narration_generation"]
    if narration_job.get("status") != "generating":
        raise WorkbenchError("当前没有待执行的项目旁白任务")
    sections = _script_sections(project_dir, state)
    scenes = list(state.get("scenes", []))
    if not scenes:
        raise WorkbenchError("没有场景可用于生成项目旁白")
    profile_id = automation["voice"].get("profile_id")
    if not profile_id:
        raise WorkbenchError("项目未记录有效的通用音色引用，请重新开始生成旁白")
    profile = get_voice_profile(str(profile_id))
    if not profile and str(automation["voice"].get("provider") or "voicebox_tts") == "voicebox_tts":
        profile = {
            "id": str(profile_id),
            "name": str(automation["voice"].get("profile_name") or profile_id),
            "provider_id": "voicebox_tts",
            "provider_name": "Haike Video 本地配音",
        }
    if not profile:
        raise WorkbenchError("项目冻结的配音音色已不存在，请回到配音中心重新选择")
    frozen_provider = str(automation["voice"].get("provider") or "voicebox_tts")
    if str(profile.get("provider_id") or "voicebox_tts") != frozen_provider:
        raise WorkbenchError("项目冻结的配音供应商与当前音色配置不一致，请重新开始旁白任务")
    provider_name = str(profile.get("provider_name") or automation["voice"].get("provider_name") or "Haike Video 本地配音")
    natural_audio: list[Path] = []
    ffmpeg = _ffmpeg_available()
    if not ffmpeg:
        raise WorkbenchError("本机未发现 FFmpeg，无法测量旁白时长并建立音频主时间线")

    for scene in scenes:
        section = sections.get(str(scene.get("script_section_id"))) or {}
        text = str(section.get("text") or scene.get("description") or "").strip()
        if not text:
            raise WorkbenchError(f"{scene.get('id')} 缺少可配音的脚本内容")
        source_audio = project_dir / "assets" / "audio" / "voicebox" / f"{scene['id']}.wav"
        result = generate_voice_audio(text=text, profile=profile, output_path=source_audio, language="zh")
        if not result.success or not source_audio.is_file():
            raise WorkbenchError(_safe_automation_error(result.error or f"{scene['id']} 的配音未生成"))
        duration_seconds = _probe_duration_seconds(source_audio, ffmpeg, 0)
        if duration_seconds <= 0:
            raise WorkbenchError(f"无法读取 {scene['id']} 的本地配音时长")
        natural_audio.append(source_audio)
        audio_asset = _append_asset(project_dir, state, {
            "name": f"{scene.get('title') or scene['id']} · {automation['voice']['label']}旁白", "type": "audio", "source_type": "local_generated",
            "path": str(source_audio), "duration_seconds": duration_seconds,
            "provider": provider_name, "source_tool": frozen_provider, "license": "由所选配音供应商生成；请按项目发布规范复核",
            "generation": {"provider_id": frozen_provider, "profile_id": profile_id, "profile_name": automation["voice"]["profile_name"], "voice_label": automation["voice"]["label"], "scene_id": scene["id"], "generated_at": _now(), "timing_mode": "natural", "metadata_path": (result.data or {}).get("metadata_path")},
        })
        scene_narration = scene.get("narration") if isinstance(scene.get("narration"), dict) else _scene_narration_default(text)
        scene["narration"] = scene_narration
        version_id = _narration_version_id(scene)
        scene_narration.setdefault("versions", []).append({
            "id": version_id,
            "status": "candidate",
            "text": text,
            "asset_id": audio_asset["id"],
            "audio_path": audio_asset["path"],
            "profile_id": profile_id,
            "profile_name": automation["voice"].get("profile_name"),
            "duration_seconds": duration_seconds,
            "raw_duration_seconds": duration_seconds,
            "timing_mode": "natural",
            "created_at": _now(),
            "source": "project_narration",
        })
        _promote_scene_narration_version(state, scene, version_id)
        narration_job["completed_scenes"] = int(narration_job.get("completed_scenes") or 0) + 1
        _activity(state, "voice_generation", f"{scene['id']} 的 {automation['voice']['label']} 自然旁白已生成（{duration_seconds:.2f} 秒）", scene_id=scene["id"], asset_id=audio_asset["id"], duration_seconds=duration_seconds)
        _save(project_dir, state)

    state = _load_for_write(project_dir)
    automation = _automation(state)
    narration_job = automation["narration_generation"]
    timeline_update = _commit_narration_timeline(state, reason="project_narration_generated")
    visual_timing = _refresh_visual_timing_status(project_dir, state)
    narration = _concat_audio(project_dir, natural_audio)
    scenes = list(state.get("scenes", []))
    subtitle_path = _write_subtitles(project_dir, scenes, sections)
    manifest = _automation_asset_manifest(project_dir, state)
    _atomic_write(project_dir / AUTOMATION_ASSET_MANIFEST, manifest)
    narration_job.update({
        "status": "completed", "stage": "ready_to_render", "finished_at": _now(),
        "audio_path": _safe_relpath(project_dir, str(narration)),
        "subtitle_path": _safe_relpath(project_dir, str(subtitle_path)),
        "timeline_update": timeline_update, "error": "",
    })
    automation["render"] = {"status": "awaiting_assets", "runtime": "ffmpeg", "output_path": None, "error": ""}
    automation["status"] = "narration_ready"
    if visual_timing["replacement_scene_ids"]:
        asset_job = automation["asset_generation"]
        asset_job.update({
            "status": "needs_duration_refresh",
            "timing_issues": visual_timing["replacement_scene_ids"],
            "timing_checked_at": visual_timing["checked_at"],
        })
        _activity(
            state,
            "visual_timing_refresh_required",
            "旁白时长已更新；已选画面无法覆盖新的真实时长，请仅替换这些场景的素材："
            + "、".join(visual_timing["replacement_scene_ids"]),
            scene_ids=visual_timing["replacement_scene_ids"],
        )
    _decision(state, "timeline_authority", "旁白类项目时间轴", "自然配音主时间轴", "脚本时长仅作预估；已按每段实测本地配音时长重排场景、字幕和后续画面需求。")
    _activity(state, "narration_generation_finished", f"项目旁白与字幕已按自然语速生成；正式时长为 {timeline_update['new_total_duration_seconds']:.2f} 秒，请先试听，再按此时长搜集画面素材")
    return _save(project_dir, state)


@_project_transactional
def start_scene_narration_candidate(project_dir: Path, scene_id: str, payload: dict) -> dict:
    """Queue one isolated Voicebox take for the selected scene.

    This never changes the current narration, project WAV or final video.  It
    only creates a candidate that the reviewer can listen to and compare.
    """
    state = _load_for_write(project_dir)
    _require_no_review_preview_conflict(
        _automation(state), payload.get("_review_preview_job_id"), payload.get("_review_preview_worker_token"), payload.get("_review_preview_internal_capability")
    )
    if _is_avatar_project(state):
        raise WorkbenchError("数字人口播的声音来自已导入数字人原片；请替换对应 Txxx 原片后重新合成母版，不能在这里用 TTS 覆盖嘴型")
    scene = _find(state["scenes"], scene_id, "场景")
    narration = scene.get("narration") if isinstance(scene.get("narration"), dict) else _scene_narration_default(_scene_text(project_dir, state, scene))
    scene["narration"] = narration
    job = narration.get("job") if isinstance(narration.get("job"), dict) else {"status": "idle", "error": ""}
    narration["job"] = job
    if job.get("status") == "generating":
        raise WorkbenchError("这个片段的候选配音正在生成，请等待完成后再试")
    catalog = voice_catalog()
    profiles = catalog.get("profiles") if isinstance(catalog.get("profiles"), list) else []
    requested_profile = str(payload.get("profile_id") or "").strip()
    selected = next((profile for profile in profiles if profile.get("id") == requested_profile), None)
    if selected is None:
        selected = catalog.get("default_voice") if not requested_profile else None
    if not selected or not selected.get("id"):
        raise WorkbenchError("请先在通用配音中心确认可用音色，或在这里选择一个音色")
    if selected.get("available") is False:
        raise WorkbenchError(f"{selected.get('provider_name') or '所选配音服务'}当前不可用，请先完成配置")

    text = str(payload.get("text") or narration.get("text") or _scene_text(project_dir, state, scene)).strip()
    if not text:
        raise WorkbenchError("该片段没有可配音的脚本文本，请先补充脚本")
    if len(text) > 5000:
        raise WorkbenchError("单个片段的配音文本不能超过 5000 个字符")

    version_id = _narration_version_id(scene)
    narration["status"] = "generating_candidate"
    narration["job"] = {
        "id": f"NJOB-{uuid4().hex[:10]}", "status": "generating", "version_id": version_id,
        "text": text, "profile_id": selected["id"], "profile_name": selected.get("name") or selected["id"],
        "provider_id": selected.get("provider_id") or "voicebox_tts", "provider_name": selected.get("provider_name") or "Haike Video 本地配音",
        "started_at": _now(), "error": "",
    }
    _activity(state, "scene_narration_started", f"开始生成 {scene_id} 的候选配音 {version_id}", scene_id=scene_id, narration_version_id=version_id)
    return _save(project_dir, state)


def generate_scene_narration_candidate(project_dir: Path, scene_id: str) -> dict:
    """Generate a natural candidate take and preview its ripple impact."""
    state = _load_for_write(project_dir)
    if _is_avatar_project(state):
        raise WorkbenchError("数字人口播不支持生成独立 TTS 候选，请回到数字人素材包更换原片")
    scene = _find(state["scenes"], scene_id, "场景")
    narration = scene.get("narration") if isinstance(scene.get("narration"), dict) else {}
    job = narration.get("job") if isinstance(narration.get("job"), dict) else {}
    if job.get("status") != "generating":
        raise WorkbenchError("当前没有待生成的片段候选配音")
    version_id = str(job.get("version_id") or "")
    if not version_id:
        raise WorkbenchError("片段候选配音缺少版本编号")
    raw_audio = project_dir / "assets" / "audio" / "voicebox" / scene_id / f"{version_id}-raw.wav"
    profile = get_voice_profile(str(job["profile_id"]))
    if not profile and str(job.get("provider_id") or "voicebox_tts") == "voicebox_tts":
        profile = {
            "id": str(job["profile_id"]),
            "name": str(job.get("profile_name") or job["profile_id"]),
            "provider_id": "voicebox_tts",
            "provider_name": "Haike Video 本地配音",
        }
    if not profile or str(profile.get("provider_id") or "voicebox_tts") != str(job.get("provider_id") or "voicebox_tts"):
        raise WorkbenchError("候选配音冻结的音色配置已变化，请重新发起候选生成")
    result = generate_voice_audio(text=str(job["text"]), profile=profile, output_path=raw_audio, language="zh")
    state = _load_for_write(project_dir)
    scene = _find(state["scenes"], scene_id, "场景")
    narration = scene.get("narration") if isinstance(scene.get("narration"), dict) else {}
    job = narration.get("job") if isinstance(narration.get("job"), dict) else {}
    if not result.success or not raw_audio.is_file():
        message = _safe_automation_error(result.error or "配音服务没有返回可试听的音频文件")
        narration["status"] = "candidate_failed"
        narration["job"] = {**job, "status": "failed", "finished_at": _now(), "error": message}
        _activity(state, "scene_narration_failed", f"{scene_id} 候选配音生成失败：{message}", scene_id=scene_id)
        return _save(project_dir, state)

    ffmpeg = _ffmpeg_available()
    if not ffmpeg:
        raise WorkbenchError("本机未发现 FFmpeg，无法测量候选配音时长")
    duration_seconds = _probe_duration_seconds(raw_audio, ffmpeg, 0)
    if duration_seconds <= 0:
        raise WorkbenchError("无法读取候选配音时长，请重新生成")
    subtitle_path, cues = _write_scene_subtitles(
        project_dir, scene, str(job["text"]), version_id, duration_seconds=duration_seconds,
    )
    timeline_impact = _build_timeline_update(
        state, {scene_id: duration_seconds}, reason="scene_narration_candidate_preview",
    )
    audio_asset = _append_asset(project_dir, state, {
        "name": f"{scene.get('title') or scene_id} · {job.get('profile_name') or '本地音色'} 候选配音",
        "type": "audio", "source_type": "local_generated", "path": str(raw_audio), "duration_seconds": duration_seconds,
        "provider": job.get("provider_name") or "Haike Video 本地配音", "source_tool": job.get("provider_id") or "voicebox_tts", "license": "由所选配音供应商生成；请按项目发布规范复核",
        "generation": {
            "provider_id": job.get("provider_id") or "voicebox_tts", "profile_id": job["profile_id"], "profile_name": job.get("profile_name"), "scene_id": scene_id,
            "narration_version_id": version_id, "generated_at": _now(), "timing_mode": "natural",
            "metadata_path": (result.data or {}).get("metadata_path"),
        },
    })
    version = {
        "id": version_id, "status": "candidate", "text": str(job["text"]),
        "asset_id": audio_asset["id"], "audio_path": audio_asset["path"], "raw_audio_path": _safe_relpath(project_dir, str(raw_audio)),
        "subtitle_path": _safe_relpath(project_dir, str(subtitle_path)), "subtitle_cues": cues,
        "profile_id": job["profile_id"], "profile_name": job.get("profile_name"),
        "duration_seconds": duration_seconds, "raw_duration_seconds": duration_seconds,
        "timing_mode": "natural", "timeline_impact": timeline_impact,
        "created_at": _now(),
    }
    narration.setdefault("versions", []).append(version)
    narration["candidate_version_id"] = version_id
    narration["status"] = "candidate_ready"
    narration["job"] = {**job, "status": "completed", "finished_at": _now(), "asset_id": audio_asset["id"], "error": ""}
    _activity(state, "scene_narration_ready", f"{scene_id} 的候选配音 {version_id} 已生成（{duration_seconds:.2f} 秒）；采用后会按自然时长更新后续时间线", scene_id=scene_id, narration_version_id=version_id, asset_id=audio_asset["id"], duration_seconds=duration_seconds)
    return _save(project_dir, state)


def mark_scene_narration_candidate_failed(project_dir: Path, scene_id: str, error: object) -> dict:
    state = _load_for_write(project_dir)
    scene = _find(state["scenes"], scene_id, "场景")
    narration = scene.get("narration") if isinstance(scene.get("narration"), dict) else _scene_narration_default()
    scene["narration"] = narration
    message = _safe_automation_error(error)
    narration["status"] = "candidate_failed"
    narration["job"] = {**(narration.get("job") or {}), "status": "failed", "finished_at": _now(), "error": message}
    _activity(state, "scene_narration_failed", f"{scene_id} 候选配音生成失败：{message}", scene_id=scene_id)
    return _save(project_dir, state)


@_project_transactional
def start_scene_narration_apply(project_dir: Path, scene_id: str, version_id: str) -> dict:
    """Queue an audio-only B-segment composition for an auditioned take."""
    state = _load_for_write(project_dir)
    _require_no_review_preview_conflict(_automation(state))
    if _is_avatar_project(state):
        raise WorkbenchError("数字人口播不支持将 TTS 局部替换并入成片；应替换数字人原片并重新应用原声时间线")
    scene = _find(state["scenes"], scene_id, "场景")
    narration = scene.get("narration") if isinstance(scene.get("narration"), dict) else {}
    candidate = next((item for item in narration.get("versions", []) if item.get("id") == version_id), None)
    if not candidate or candidate.get("status") not in {"candidate", "current"}:
        raise WorkbenchError("请先生成并试听一个有效的候选配音")
    audio_asset = _find(state.get("assets", []), str(candidate.get("asset_id") or ""), "候选配音素材")
    if audio_asset.get("type") != "audio" or not audio_asset.get("path") or not (project_dir / str(audio_asset["path"])).is_file():
        raise WorkbenchError("候选配音文件不可用，请重新生成后再试")
    # Older projects can have an auditionable candidate created before the
    # audio-first contract existed.  Measure it at the moment it is adopted
    # instead of treating the old scene slot as its duration.
    ffmpeg = _ffmpeg_available()
    if not ffmpeg:
        raise WorkbenchError("本机未发现 FFmpeg，无法测量候选配音的真实时长")
    measured_duration = _probe_duration_seconds(project_dir / str(audio_asset["path"]), ffmpeg, 0)
    if measured_duration <= 0:
        raise WorkbenchError("无法读取候选配音的真实时长，请重新生成后再试")
    stored_duration = _as_number(candidate.get("duration_seconds"))
    if abs(stored_duration - measured_duration) > 0.05:
        candidate["duration_seconds"] = measured_duration
        candidate["raw_duration_seconds"] = measured_duration
        candidate["timing_mode"] = "natural"
        subtitle_path, cues = _write_scene_subtitles(
            project_dir, scene, str(candidate.get("text") or narration.get("text") or ""), version_id,
            duration_seconds=measured_duration,
        )
        candidate["subtitle_path"] = _safe_relpath(project_dir, str(subtitle_path))
        candidate["subtitle_cues"] = cues
        candidate["timeline_impact"] = _build_timeline_update(
            state, {scene_id: measured_duration}, reason="legacy_scene_narration_candidate_measured",
        )
        audio_asset["duration_seconds"] = measured_duration
    segment = next((item for item in state.get("segments", []) if scene_id in item.get("scene_ids", [])), None)
    if not segment:
        raise WorkbenchError("该场景尚未拥有可局部合成的渲染片段")
    if not _current_artifact(project_dir, segment):
        raise WorkbenchError("请先生成一次完整成片以建立片段基线；基线完成后只会重新合成当前片段")

    # A newer choice explicitly supersedes an unmerged local preview of this
    # same scene.  It never alters the promoted final or a different segment.
    for patch in state.get("patches", []):
        if patch.get("segment_id") == segment["id"] and patch.get("source") == "scene_narration" and patch.get("status") in {"planned", "ready_to_render", "blocked", "rendered"}:
            patch["status"] = "rolled_back"
            patch["rolled_back_at"] = _now()
            patch["rollback_reason"] = "a newer narration candidate was selected"
            _persist_patch_artifact(project_dir, patch)
    _save(project_dir, state)
    state = prepare_patch(project_dir, {
        "segment_id": segment["id"], "candidate_audio_asset_id": audio_asset["id"], "change_scope": "audio",
        "narration_version_id": version_id, "target_duration_seconds": measured_duration,
        "instruction": f"采用 {version_id} 作为本片段自然旁白；只重新合成该片段，前后片段内容保持冻结并随时间线顺延",
        "mode": "ripple_timeline", "source": "scene_narration",
    })
    patch = state["patches"][-1]
    patch["status"] = "rendering"
    patch["source"] = "scene_narration"
    patch["narration_version_id"] = version_id
    narration = _find(state["scenes"], scene_id, "场景")["narration"]
    narration["status"] = "applying_candidate"
    narration["job"] = {"status": "rendering", "version_id": version_id, "patch_id": patch["id"], "started_at": _now(), "error": ""}
    _persist_patch_artifact(project_dir, patch)
    _activity(state, "scene_narration_apply", f"{scene_id} 正在局部合成候选配音 {version_id}", scene_id=scene_id, patch_id=patch["id"], narration_version_id=version_id)
    return _save(project_dir, state)


def _require_renderable_project(project_dir: Path, state: dict, automation: dict) -> None:
    narration_job = automation["narration_generation"]
    audio_path = narration_job.get("audio_path")
    if narration_job.get("status") != "completed" or not audio_path or not (project_dir / audio_path).is_file():
        if _is_avatar_project(state):
            raise WorkbenchError("请先在“数字人素材”中完成原声母版合成，并点击“应用为真实时间线”")
        raise WorkbenchError("请先完成项目旁白生成，并确认旁白文件可播放")
    _require_ready_network_assets(state, automation)


def _assert_ffmpeg_render_available(state: dict, *, subject: str) -> None:
    # Preview and formal workbench renders are explicitly FFmpeg jobs.  Do
    # not call VideoCompose.get_info() here: that broad capability report also
    # probes optional Remotion/HyperFrames runtimes and may invoke networked
    # package discovery even though neither runtime can be selected on this
    # path.  A local FFmpeg check is the complete gate for this operation.
    if not _ffmpeg_available():
        raise WorkbenchError("本机 FFmpeg 合成器不可用，无法合成视频")
    _decision(state, "render_runtime_selection", subject, "ffmpeg", "当前工作台预览/成片路径固定使用本机 FFmpeg。")


@_project_transactional
def start_full_preview_render(project_dir: Path, payload: dict) -> dict:
    """Queue a fast, review-only whole-video candidate without approving scenes."""
    if payload.get("confirmed") is not True:
        raise WorkbenchError("请确认后再生成全片预览")
    state = _load_for_write(project_dir)
    automation = _automation(state)
    _require_no_review_preview_conflict(
        automation, payload.get("_review_preview_job_id"), payload.get("_review_preview_worker_token"), payload.get("_review_preview_internal_capability")
    )
    _require_renderable_project(project_dir, state, automation)
    trusted_default_audio = bool(
        payload.get("_review_preview_internal_capability") is _REVIEW_PREVIEW_INTERNAL_CAPABILITY
        and payload.get("_review_preview_trusted_default_audio") is True
    )
    upfront_audio_signature = ""
    if payload.get("_review_preview_internal_capability") is _REVIEW_PREVIEW_INTERNAL_CAPABILITY:
        candidate = str(payload.get("_review_preview_upfront_audio_signature") or "")
        parent = automation.get("review_preview_pipeline") or {}
        frozen_signature = str((((parent.get("frozen_input") or {}).get("audio") or {}).get("audio_mix_signature") or ""))
        if candidate and candidate == frozen_signature:
            upfront_audio_signature = candidate
    _require_approved_music_sample(
        state,
        trusted_default=trusted_default_audio,
        upfront_authorized_signature=upfront_audio_signature,
    )
    job = automation["preview_render"]
    if job.get("status") == "generating":
        raise WorkbenchError("全片预览正在合成，请不要重复点击")
    _assert_ffmpeg_render_available(state, subject="全片预览合成运行时")
    version = max(0, int(_as_number(job.get("version")))) + 1
    preview_job_id = f"PRJ-{uuid4().hex[:10]}"
    automation["status"] = "rendering_preview"
    automation["preview_render"] = {
        "status": "generating", "runtime": "ffmpeg", "output_path": None,
        "version": version, "job_id": preview_job_id, "started_at": _now(), "error": "",
        "parent_job_id": str(payload.get("_review_preview_job_id") or "") or None,
        "input_fingerprint": str(payload.get("_review_preview_input_fingerprint") or "") or None,
    }
    _activity(state, "full_preview_started", f"开始合成全片预览 v{version}；不会改变任何场景审核状态，也不会建立正式成片基线")
    return _save(project_dir, state)


def mark_full_preview_render_failed(project_dir: Path, error: object) -> dict:
    state = _load_for_write(project_dir)
    automation = _automation(state)
    message = _safe_automation_error(error)
    automation["status"] = "preview_render_failed"
    automation["preview_render"].update({"status": "failed", "finished_at": _now(), "error": message})
    _activity(state, "full_preview_failed", f"全片预览合成失败：{message}")
    return _save(project_dir, state)


def approve_full_preview_scenes(project_dir: Path, payload: dict) -> dict:
    """Mark technically ready scenes approved after the human watched a candidate."""
    if payload.get("confirmed") is not True:
        raise WorkbenchError("请确认已看完全片预览后再批量通过")
    state = _load_for_write(project_dir)
    summary = _full_preview_summary(state)
    preview = summary["preview"]
    if preview.get("status") != "completed" or not preview.get("output_path") or not (project_dir / str(preview["output_path"])).is_file():
        raise WorkbenchError("请先生成并查看当前版全片预览，再一键确认场景")
    if not summary["technical_ready"]:
        raise WorkbenchError("仍有片段缺少完整画面，无法批量通过：" + "、".join(summary["missing_scene_ids"]))
    requested = payload.get("scene_ids")
    targets = {str(value) for value in requested} if isinstance(requested, list) and requested else {str(scene.get("id")) for scene in state.get("scenes", [])}
    approved: list[str] = []
    for scene in state.get("scenes", []):
        if not isinstance(scene, dict) or str(scene.get("id")) not in targets:
            continue
        if _scene_has_renderable_visual(state, scene):
            scene["review_status"] = "approved"
            approved.append(str(scene["id"]))
    if not approved:
        raise WorkbenchError("没有可批量通过的片段")
    _decision(state, "full_preview_review", "全片预览批量确认", f"通过 {len(approved)} 段", f"基于全片预览 v{preview.get('version')}")
    _activity(state, "full_preview_batch_approved", f"已基于全片预览批量通过 {len(approved)} 个场景；现可以发布正式成片", scene_ids=approved)
    return _save(project_dir, state)


@_project_transactional
def start_project_video_render(project_dir: Path, payload: dict) -> dict:
    """Queue the formal FFmpeg deliverable after a human has approved every scene."""
    if payload.get("confirmed") is not True:
        raise WorkbenchError("请确认后再使用已生成的旁白合成视频")
    state = _load_for_write(project_dir)
    automation = _automation(state)
    _require_no_review_preview_conflict(automation)
    _require_renderable_project(project_dir, state, automation)
    _require_approved_music_sample(state)
    review = _full_preview_summary(state)
    if not review["all_scenes_approved"]:
        raise WorkbenchError(f"正式成片需要先完成人工确认：已通过 {review['approved_count']}/{review['total_scenes']} 段。请先生成全片预览，查看后使用“一键确认全部可发布片段”。")
    if automation["render"].get("status") == "generating":
        raise WorkbenchError("视频正在合成，请不要重复点击")
    _assert_ffmpeg_render_available(state, subject="正式成片合成运行时")
    automation["status"] = "rendering_video"
    automation["render"] = {"status": "generating", "runtime": "ffmpeg", "output_path": None, "started_at": _now(), "error": ""}
    _activity(state, "video_render_started", "已确认旁白，开始通过本地 FFmpeg 合成视频")
    return _save(project_dir, state)


def mark_project_video_render_failed(project_dir: Path, error: object) -> dict:
    state = _load_for_write(project_dir)
    automation = _automation(state)
    message = _safe_automation_error(error)
    automation["status"] = "render_failed"
    automation["render"].update({"status": "failed", "finished_at": _now(), "error": message})
    _activity(state, "video_render_failed", f"视频合成失败：{message}")
    return _save(project_dir, state)


def _apply_project_narration_gain(
    project_dir: Path,
    state: dict,
    video_path: Path,
    *,
    output_path: Path | None = None,
) -> dict:
    """Apply project speech gain to a derivative without touching its source."""
    gain_db = clamp_narration_gain_db(
        _ensure_narration_policy(state).get("playback_gain_db")
    )
    target = output_path or video_path
    target.parent.mkdir(parents=True, exist_ok=True)
    same_target = target.resolve() == video_path.resolve()
    if gain_db == 0.0:
        if not same_target:
            shutil.copy2(video_path, target)
        return {
            "enabled": False,
            "playback_gain_db": gain_db,
            "linear_gain": 1.0,
            "output_path": _safe_relpath(project_dir, str(target)),
        }

    ffmpeg = _ffmpeg_available()
    if not ffmpeg:
        raise WorkbenchError("本机未发现 FFmpeg，无法应用人物台词音量")
    temporary = target.with_name(
        f".{target.stem}-narration-{uuid4().hex[:8]}{target.suffix}"
    )
    ok, detail = _run_media([
        ffmpeg,
        "-y",
        "-i",
        str(video_path),
        "-map",
        "0:v:0",
        "-map",
        "0:a:0",
        "-c:v",
        "copy",
        "-af",
        f"volume={gain_db:.1f}dB",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-ar",
        "48000",
        "-movflags",
        "+faststart",
        str(temporary),
    ])
    if not ok or not temporary.is_file():
        temporary.unlink(missing_ok=True)
        raise WorkbenchError(f"人物台词音量处理失败：{detail}")
    os.replace(temporary, target)
    return {
        "enabled": True,
        "playback_gain_db": gain_db,
        "linear_gain": round(10 ** (gain_db / 20.0), 6),
        "output_path": _safe_relpath(project_dir, str(target)),
    }


def _apply_project_audio_mix(
    project_dir: Path,
    state: dict,
    video_path: Path,
    *,
    output_path: Path | None = None,
) -> dict:
    """Apply speech gain first and BGM second using one production contract."""
    target = output_path or video_path
    music_enabled = bool(_ensure_music_policy(state).get("enabled"))
    if not music_enabled:
        narration = _apply_project_narration_gain(
            project_dir, state, video_path, output_path=target
        )
        return {
            "narration": narration,
            "background_music": {"enabled": False},
            "output_path": _safe_relpath(project_dir, str(target)),
        }

    narration_derivative = target.with_name(
        f".{target.stem}-speech-{uuid4().hex[:8]}{target.suffix}"
    )
    try:
        narration = _apply_project_narration_gain(
            project_dir, state, video_path, output_path=narration_derivative
        )
        music = _apply_project_background_music(
            project_dir, state, narration_derivative, output_path=target
        )
    finally:
        narration_derivative.unlink(missing_ok=True)
    return {
        "narration": narration,
        "background_music": music,
        "output_path": _safe_relpath(project_dir, str(target)),
    }


def _apply_project_background_music(project_dir: Path, state: dict, video_path: Path, *, output_path: Path | None = None) -> dict:
    """Mix saved BGM into a completed video, optionally as a separate derivative.

    ``output_path`` is used by the first-scene audition flow.  It guarantees
    the review preview stays voice-only while the sample receives the exact
    same FFmpeg mix operation that whole-video output will receive later.
    """
    policy = deepcopy(_ensure_music_policy(state))
    if not policy.get("enabled"):
        return {"enabled": False}
    try:
        music_path, track = resolve_music_track(str(policy.get("track_id") or ""), project_dir)
    except MusicLibraryError as exc:
        raise WorkbenchError(str(exc)) from exc

    gain_db = clamp_playback_gain_db(policy.get("playback_gain_db"))
    music_volume = 10 ** (gain_db / 20.0)
    target = output_path or video_path
    target.parent.mkdir(parents=True, exist_ok=True)
    mixed_path = target.with_name(f".{target.stem}-music-{uuid4().hex[:8]}{target.suffix}")
    result = AudioMixer().execute({
        "operation": "segmented_music",
        "video_path": str(video_path),
        "music_path": str(music_path),
        "music_volume": music_volume,
        "segments": [{"start": 0.0, "end": 86400.0}],
        "source_start_seconds": float(policy.get("source_start_seconds") or 0.0),
        "source_end_seconds": (
            float(policy.get("source_end_seconds"))
            if policy.get("source_end_seconds") is not None else None
        ),
        "fade_in_seconds": float(policy.get("fade_in_seconds") or 0.8),
        "fade_out_seconds": float(policy.get("fade_out_seconds") or 1.5),
        "output_path": str(mixed_path),
    })
    if not result.success or not mixed_path.is_file():
        try:
            mixed_path.unlink()
        except OSError:
            pass
        raise WorkbenchError(_safe_automation_error(result.error or "背景音乐混音未生成输出文件"))
    os.replace(mixed_path, target)
    return {
        "enabled": True,
        "track_id": track["id"],
        "title": track["title"],
        "filename": track["filename"],
        "source_calibration_db": track.get("source_calibration_db"),
        "playback_gain_db": gain_db,
        "loop": True,
        "source_start_seconds": float(policy.get("source_start_seconds") or 0.0),
        "source_end_seconds": (
            float(policy.get("source_end_seconds"))
            if policy.get("source_end_seconds") is not None else None
        ),
        "fade_in_seconds": float(policy.get("fade_in_seconds") or 0.8),
        "fade_out_seconds": float(policy.get("fade_out_seconds") or 1.5),
        "mix_result": result.data or {},
        "output_path": _safe_relpath(project_dir, str(target)),
    }


def _daily_headline_font(size: int):
    from PIL import ImageFont

    candidates = (
        Path(r"C:\Windows\Fonts\msyhbd.ttc"),
        Path(r"C:\Windows\Fonts\msyh.ttc"),
        Path(r"C:\Windows\Fonts\simhei.ttf"),
    )
    for candidate in candidates:
        if candidate.is_file():
            return ImageFont.truetype(str(candidate), size=size)
    return ImageFont.load_default()


def _fit_daily_headline_font(draw: Any, text: str, maximum_width: int, *, initial: int = 64, minimum: int = 34):
    for size in range(initial, minimum - 1, -2):
        font = _daily_headline_font(size)
        bounds = draw.textbbox((0, 0), text, font=font, stroke_width=5)
        if bounds[2] - bounds[0] <= maximum_width:
            return font
    return _daily_headline_font(minimum)


def _daily_headline_display_units(text: str) -> float:
    """Approximate visible width across mixed Chinese and Latin headlines."""
    return sum(1.0 if "\u3400" <= char <= "\u9fff" else 0.35 if char.isspace() else 0.55 for char in text)


def _compact_daily_headline_text(value: Any) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return re.sub(r"(?<=[\u3400-\u9fff])\s+|\s+(?=[\u3400-\u9fff])", "", text)


def _daily_story_headline_overlays(project_dir: Path, state: dict, width: int, height: int) -> tuple[list[dict], dict]:
    """Render one reusable transparent title asset per contiguous story."""
    from PIL import Image, ImageDraw

    scenes = [scene for scene in state.get("scenes") or [] if isinstance(scene, dict)]
    groups: list[dict[str, Any]] = []
    for scene in scenes:
        story_id = str(scene.get("story_id") or "")
        headline = scene.get("headline_overlay") if isinstance(scene.get("headline_overlay"), dict) else {}
        if not story_id or not headline:
            continue
        start = _rounded_seconds(scene.get("start_seconds"))
        end = _rounded_seconds(scene.get("end_seconds"))
        if groups and groups[-1]["story_id"] == story_id:
            groups[-1]["end_seconds"] = end
            continue
        groups.append({"story_id": story_id, "start_seconds": start, "end_seconds": end, "headline": deepcopy(headline)})

    layout = _ensure_story_headline_layout(state)
    canvas_w = max(240, int(width * layout["width"]))
    canvas_h = max(100, int(height * layout["height"]))
    x = int(width * layout["x"])
    y = int(height * layout["y"])
    output_dir = project_dir / "renders" / "overlays" / "story-headlines"
    output_dir.mkdir(parents=True, exist_ok=True)
    overlays: list[dict] = []
    assets: list[dict] = []
    for group in groups:
        headline = group["headline"]
        mode = str(headline.get("mode") or "one_line")
        line_1 = _compact_daily_headline_text(headline.get("line_1"))
        line_2 = _compact_daily_headline_text(headline.get("line_2"))
        if not line_1 or mode not in {"one_line", "two_line"}:
            raise WorkbenchError(f"{group['story_id']} 新闻小标题合同不完整")
        if mode == "two_line" and (not line_2 or _daily_headline_display_units(line_2) <= _daily_headline_display_units(line_1)):
            raise WorkbenchError(f"{group['story_id']} 双行小标题必须下黄行长于上白行")
        fingerprint = hashlib.sha256(f"{mode}|{line_1}|{line_2}|{canvas_w}|{canvas_h}".encode("utf-8")).hexdigest()[:12]
        path = output_dir / f"{group['story_id']}-{fingerprint}.png"
        if not path.is_file():
            image = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
            draw = ImageDraw.Draw(image)
            maximum = canvas_w - 24
            if mode == "one_line":
                font = _fit_daily_headline_font(draw, line_1, maximum, initial=68, minimum=38)
                bbox = draw.textbbox((0, 0), line_1, font=font, stroke_width=5)
                top = max(10, (canvas_h - (bbox[3] - bbox[1])) // 2 - bbox[1])
                draw.text((8, top + 5), line_1, font=font, fill="#FFD400", stroke_width=7, stroke_fill="#111111")
            else:
                font_1 = _fit_daily_headline_font(draw, line_1, maximum, initial=57, minimum=34)
                font_2 = _fit_daily_headline_font(draw, line_2, maximum, initial=64, minimum=36)
                draw.text((8, 18), line_1, font=font_1, fill="#FFFFFF", stroke_width=7, stroke_fill="#111111")
                draw.text((8, canvas_h // 2), line_2, font=font_2, fill="#FFD400", stroke_width=7, stroke_fill="#111111")
            image.save(path)
        overlays.append({
            "asset_path": str(path),
            "start_seconds": group["start_seconds"],
            "end_seconds": group["end_seconds"],
            "x": x,
            "y": y,
            "width": canvas_w,
            "height": canvas_h,
            "shape": "rectangle",
            "story_id": group["story_id"],
        })
        assets.append({"story_id": group["story_id"], "path": _safe_relpath(project_dir, str(path)), "mode": mode, "line_1": line_1, "line_2": line_2})
    return overlays, {"style_id": "daily_news_headline_v1", "placement": {"x": x, "y": y, "width": canvas_w, "height": canvas_h}, "assets": assets}


def _analyze_loudnorm(
    path: Path,
    ffmpeg: str,
    *,
    target_lufs: float,
    true_peak_dbtp: float,
) -> dict[str, float]:
    process = subprocess.run(
        [
            ffmpeg, "-hide_banner", "-i", str(path), "-af",
            (
                f"loudnorm=I={float(target_lufs):.1f}:LRA=11:"
                f"TP={float(true_peak_dbtp):.1f}:print_format=json"
            ),
            "-f", "null", "-",
        ],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180, check=False,
    )
    matches = re.findall(r"\{\s*\"input_i\".*?\}", process.stderr, re.DOTALL)
    if not matches:
        raise WorkbenchError("FFmpeg 未返回可解析的响度报告")
    payload = json.loads(matches[-1])
    return {
        "input_i": float(payload.get("input_i")),
        "input_tp": float(payload.get("input_tp")),
        "input_lra": float(payload.get("input_lra")),
        "input_thresh": float(payload.get("input_thresh")),
        "target_offset": float(payload.get("target_offset")),
    }


def _measure_integrated_loudness(path: Path, ffmpeg: str) -> dict[str, Any]:
    measured = _analyze_loudnorm(
        path,
        ffmpeg,
        target_lufs=-14.0,
        true_peak_dbtp=-1.5,
    )
    return {
        "integrated_lufs": measured["input_i"],
        "true_peak_dbtp": measured["input_tp"],
        "loudness_range_lu": measured["input_lra"],
        "threshold_lufs": measured["input_thresh"],
    }


def _normalize_video_loudness(
    project_dir: Path,
    video_path: Path,
    *,
    target_lufs: float,
    enforce_acceptance: bool = True,
) -> dict[str, Any]:
    ffmpeg = _ffmpeg_available()
    if not ffmpeg:
        raise WorkbenchError("本机未发现 FFmpeg，无法执行成片响度归一化")
    # AAC encoding can add roughly 0.5 dB of inter-sample peak overshoot.  A
    # -2.0 dBTP normalization target leaves reliable headroom while the final
    # acceptance gate remains at -1.0 dBTP.
    normalization_true_peak_dbtp = -2.0
    first_pass = _analyze_loudnorm(
        video_path,
        ffmpeg,
        target_lufs=target_lufs,
        true_peak_dbtp=normalization_true_peak_dbtp,
    )
    loudnorm_filter = (
        f"loudnorm=I={float(target_lufs):.1f}:LRA=11:TP={normalization_true_peak_dbtp:.1f}:"
        f"measured_I={first_pass['input_i']:.6f}:"
        f"measured_LRA={first_pass['input_lra']:.6f}:"
        f"measured_TP={first_pass['input_tp']:.6f}:"
        f"measured_thresh={first_pass['input_thresh']:.6f}:"
        f"offset={first_pass['target_offset']:.6f}:linear=true:print_format=summary"
    )
    temporary = video_path.with_name(f".{video_path.stem}-loudnorm-{uuid4().hex[:8]}{video_path.suffix}")
    ok, detail = _run_media([
        ffmpeg, "-y", "-i", str(video_path), "-map", "0:v:0", "-map", "0:a:0",
        "-c:v", "copy", "-af", loudnorm_filter,
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-movflags", "+faststart", str(temporary),
    ])
    if not ok or not temporary.is_file():
        temporary.unlink(missing_ok=True)
        raise WorkbenchError(f"成片响度归一化失败：{detail}")
    os.replace(temporary, video_path)
    measured = _measure_integrated_loudness(video_path, ffmpeg)
    peak_safety_attenuation_db = 0.0
    # AAC can still overshoot the requested loudnorm peak by a fraction of a
    # decibel.  When loudness is already acceptable, apply one small,
    # deterministic safety trim and measure again instead of rejecting an
    # otherwise valid render at the very last step.
    if (
        abs(measured["integrated_lufs"] - float(target_lufs)) <= 1.5
        and measured["true_peak_dbtp"] > -1.0
    ):
        peak_safety_attenuation_db = min(-0.1, -1.2 - measured["true_peak_dbtp"])
        safety_output = video_path.with_name(
            f".{video_path.stem}-peak-safety-{uuid4().hex[:8]}{video_path.suffix}"
        )
        ok, detail = _run_media([
            ffmpeg, "-y", "-i", str(video_path), "-map", "0:v:0", "-map", "0:a:0",
            "-c:v", "copy", "-af", f"volume={peak_safety_attenuation_db:.3f}dB",
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-movflags", "+faststart",
            str(safety_output),
        ])
        if not ok or not safety_output.is_file():
            safety_output.unlink(missing_ok=True)
            raise WorkbenchError(f"成片峰值安全衰减失败：{detail}")
        os.replace(safety_output, video_path)
        measured = _measure_integrated_loudness(video_path, ffmpeg)
    meets_acceptance = (
        abs(measured["integrated_lufs"] - float(target_lufs)) <= 1.5
        and measured["true_peak_dbtp"] <= -1.0
    )
    report = {
        "target_lufs": float(target_lufs),
        "normalization_true_peak_target_dbtp": normalization_true_peak_dbtp,
        "acceptance_true_peak_limit_dbtp": -1.0,
        "peak_safety_attenuation_db": peak_safety_attenuation_db,
        "acceptance_enforced": bool(enforce_acceptance),
        "acceptance_status": "passed" if meets_acceptance else "warning",
        **measured,
        "output_path": _safe_relpath(project_dir, str(video_path)),
    }
    # An audit preview is a human review artifact, not a released deliverable.
    # Keep its measured loudness in the report, but do not discard an otherwise
    # playable preview because of a marginal (or even material) loudness miss.
    # Formal renders continue to fail closed below.
    if enforce_acceptance and not meets_acceptance:
        raise WorkbenchError(
            f"成片响度未达到发布容差：{measured['integrated_lufs']:.1f} LUFS / {measured['true_peak_dbtp']:.1f} dBTP"
        )
    return report


def _generate_project_video_render(project_dir: Path, *, preview: bool) -> dict:
    """Compose either a disposable full-preview candidate or the formal deliverable."""
    state = _load_for_write(project_dir)
    automation = _automation(state)
    job_key = "preview_render" if preview else "render"
    job = automation[job_key]
    if job.get("status") != "generating":
        raise WorkbenchError("当前没有待执行的视频合成任务")
    preview_job_id = str(job.get("job_id") or "") if preview else ""
    preview_version = int(_as_number(job.get("version"))) if preview else 0
    preview_parent_job_id = str(job.get("parent_job_id") or "") if preview else ""
    preview_input_fingerprint = str(job.get("input_fingerprint") or "") if preview else ""
    expected_parent_worker_token: str | None = None
    if preview:
        if not preview_job_id:
            raise WorkbenchError("全片预览子任务缺少稳定 job_id，拒绝执行")
        if preview_parent_job_id:
            parent = automation.get("review_preview_pipeline") or {}
            if (
                parent.get("job_id") != preview_parent_job_id
                or parent.get("status") != "running"
                or not parent.get("worker_token")
                or parent.get("input_fingerprint") != preview_input_fingerprint
            ):
                raise WorkbenchError("全片预览父任务租约或冻结输入不一致，拒绝执行")
            expected_parent_worker_token = str(parent.get("worker_token"))
    narration_job = automation["narration_generation"]
    narration = project_dir / str(narration_job.get("audio_path") or "")
    scenes = list(state.get("scenes", []))
    sections = _script_sections(project_dir, state)
    if not scenes:
        raise WorkbenchError("没有场景可用于合成视频")
    if _is_avatar_project(state) and automation.get("audio_mode") == "native_avatar_audio":
        # This is a render derivative, not an immutable source artifact.
        # Rebuild it on every render so an older scene-paragraph SRT can never
        # outlive the phrase cues currently shown in the scene workbench.
        subtitle_path = _write_avatar_review_subtitles(project_dir, state)
        narration_job["subtitle_path"] = _safe_relpath(project_dir, str(subtitle_path))
    else:
        # Rebuild the project SRT from the editable phrase model at render
        # time.  A caption wording change must never be hidden behind an old
        # narration-generation artifact.
        subtitle_path = _write_subtitles(project_dir, scenes, sections)
        narration_job["subtitle_path"] = _safe_relpath(project_dir, str(subtitle_path))
    if not narration.is_file() or not subtitle_path.is_file():
        raise WorkbenchError("项目旁白或字幕文件不存在，请重新生成旁白")
    manifest = _automation_asset_manifest(project_dir, state)
    _atomic_write(project_dir / AUTOMATION_ASSET_MANIFEST, manifest)
    cuts: list[dict] = []
    presenter_overlays: list[dict] = []
    is_avatar_render = _is_avatar_project(state) and automation.get("audio_mode") == "native_avatar_audio"
    ffmpeg = _ffmpeg_available() if is_avatar_render else None
    for scene in scenes:
        duration = max(.1, _as_number(scene.get("end_seconds")) - _as_number(scene.get("start_seconds")))
        presenter = _scene_presenter(scene)
        treatment = presenter.get("treatment") if is_avatar_render else "hidden"
        if treatment == "fullscreen":
            source, source_start, source_end = _avatar_source_for_scene(project_dir, scene)
            if abs((source_end - source_start) - duration) > 0.08:
                raise WorkbenchError(
                    f"{scene.get('id')} 的数字人原声边界与真实场景时长不一致；"
                    "请重新应用数字人真实时间线后再合成"
                )
            cuts.append({
                "id": f"cut-{scene['id']}", "source": str(source),
                "in_seconds": source_start, "out_seconds": source_end, "speed": 1.0,
                "transition_in": "fade", "transition_out": "dissolve",
            })
            continue

        _ensure_scene_visual_state(state, scene)
        visual_blocks = list((scene.get("visual_timeline") or {}).get("blocks") or [])
        timeline_asset_id = None
        if visual_blocks:
            ffmpeg = ffmpeg or _ffmpeg_available()
            if not ffmpeg:
                raise WorkbenchError("本机未发现 FFmpeg，无法合成片段内视觉时间线")
            timeline_path = _materialize_scene_visual_composition(project_dir, state, scene, ffmpeg)
            timeline_asset_id = f"VT-{scene['id']}"
            manifest["assets"].append({
                "id": timeline_asset_id, "type": "video", "path": str(timeline_path.resolve()),
                "source_tool": "workbench_visual_composition", "provider": "local_remotion_or_ffmpeg",
                "license": "由项目内已登记素材合成", "duration_seconds": duration,
            })
        usage = next((item for item in state["usages"] if item.get("scene_id") == scene.get("id") and item.get("role") == "visual" and item.get("selected")), None)
        if not usage and not timeline_asset_id:
            raise WorkbenchError(f"{scene.get('id')} 没有可合成的主体画面")
        cuts.append({"id": f"cut-{scene['id']}", "source": timeline_asset_id or usage["asset_id"], "in_seconds": 0, "out_seconds": duration, "speed": 1.0, "transition_in": "fade", "transition_out": "dissolve"})
        if treatment in {"pip_top_left", "custom"}:
            if not ffmpeg:
                raise WorkbenchError("本机未发现 FFmpeg，无法合成左上角数字人画中画")
            overlay_path, geometry = _materialize_avatar_overlay_clip(project_dir, state, scene, ffmpeg)
            presenter_overlays.append({
                "asset_path": str(overlay_path),
                "start_seconds": _rounded_seconds(scene.get("start_seconds")),
                "end_seconds": _rounded_seconds(scene.get("end_seconds")),
                "x": geometry["x"], "y": geometry["y"],
                "width": geometry["width"], "height": geometry["height"],
                "shape": geometry.get("shape", "rectangle"),
                "scene_id": scene["id"],
            })
    _atomic_write(project_dir / AUTOMATION_ASSET_MANIFEST, manifest)
    width, height = _render_dimensions(project_dir, state)
    headline_overlays, headline_report = _daily_story_headline_overlays(project_dir, state, width, height)
    subtitle_style_state = _ensure_subtitle_style_state(state)
    default_subtitle_style = _subtitle_video_style(
        next(
            (item.get("style") for item in subtitle_style_state["templates"] if item.get("id") == subtitle_style_state["default_template_id"]),
            _subtitle_style_default(),
        )
    )
    scene_subtitle_styles = [{
        "start_seconds": _rounded_seconds(scene.get("start_seconds")),
        "end_seconds": _rounded_seconds(scene.get("end_seconds")),
        "style": _subtitle_video_style(_resolved_scene_subtitle_style(state, scene)),
    } for scene in scenes]
    edit_decisions = {
        "version": "1.0", "renderer_family": "stock-video-narration", "render_runtime": "ffmpeg", "composition_mode": "templated",
        "metadata": {"proposal_render_runtime": "ffmpeg", "compose_target": {"width": width, "height": height, "fit": "cover"}, "automation": "separate_narration_then_render"},
        "cuts": cuts,
        "subtitles": {"enabled": True, "source": str(subtitle_path), "mode": "phrase", "style": {**default_subtitle_style, "scene_styles": scene_subtitle_styles}},
    }
    _atomic_write(project_dir / AUTOMATION_EDIT_DECISIONS, edit_decisions)
    version = max(1, int(_as_number(job.get("version")))) if preview else 1
    final_output = (
        project_dir / "renders" / "previews" / f"full-preview-v{version:03d}.mp4"
        if preview else project_dir / "renders" / "final.mp4"
    )
    if preview:
        safe_preview_job_id = re.sub(r"[^A-Za-z0-9_-]+", "", preview_job_id)[:24]
        output = final_output.with_name(
            f".{final_output.stem}-{safe_preview_job_id}.staged{final_output.suffix}"
        )
    else:
        output = final_output
    output.parent.mkdir(parents=True, exist_ok=True)
    all_overlays = [*presenter_overlays, *headline_overlays]
    base_output = output.with_name(f".{output.stem}-base-{uuid4().hex[:8]}{output.suffix}") if all_overlays else output
    result = VideoCompose().execute({
        "operation": "render", "edit_decisions": edit_decisions, "asset_manifest": manifest, "audio_path": str(narration),
        "subtitle_path": str(subtitle_path), "output_path": str(base_output), "script_text": "\n".join(str((sections.get(str(scene.get('script_section_id'))) or {}).get("text") or scene.get("description") or "") for scene in scenes),
        "options": {"subtitle_burn": True}, "codec": "libx264",
        "crf": 24 if preview else 22, "preset": "fast" if preview else "medium",
    })
    if not result.success or not base_output.is_file():
        raise WorkbenchError(_safe_automation_error(result.error or "FFmpeg 未生成可播放成片"))
    overlay_data: dict[str, Any] = {}
    if all_overlays:
        overlay_result = VideoCompose().execute({
            "operation": "overlay", "input_path": str(base_output), "overlays": all_overlays,
            "output_path": str(output), "codec": "libx264", "crf": 23 if preview else 20,
        })
        try:
            base_output.unlink()
        except OSError:
            pass
        if not overlay_result.success or not output.is_file():
            raise WorkbenchError(_safe_automation_error(overlay_result.error or "数字人画中画合成失败"))
        overlay_data = overlay_result.data or {}
    component_ids = _apply_surgical_directives_to_video(project_dir, state, output, output, ffmpeg or _ffmpeg_available())
    # The formal baseline cache must remain voice-only.  Otherwise an audio
    # hot-swap cannot replace the narration without also inheriting the old
    # baked music.  Preview can be mixed immediately; formal BGM is applied
    # only after the clean reusable segments have been created below.
    audio_mix_result = (
        _apply_project_audio_mix(project_dir, state, output)
        if preview else {
            "narration": {
                "enabled": False,
                "pending_after_baseline": clamp_narration_gain_db(
                    _ensure_narration_policy(state).get("playback_gain_db")
                ) != 0.0,
            },
            "background_music": {
                "enabled": False,
                "pending_after_baseline": bool(_ensure_music_policy(state).get("enabled")),
            },
        }
    )
    loudness_result = _normalize_video_loudness(
        project_dir,
        output,
        target_lufs=-14.0 if preview else -16.0,
        enforce_acceptance=not preview,
    )
    report_path = AUTOMATION_PREVIEW_RENDER_REPORT if preview else AUTOMATION_RENDER_REPORT
    render_report = {
        "version": "1.0", "status": "completed", "kind": "full_preview" if preview else "formal_final",
        "output_path": _safe_relpath(project_dir, str(final_output)),
        "runtime": "ffmpeg", "data": result.data or {}, "generated_at": _now(),
        "avatar": {
            "audio_mode": "native_avatar_audio",
            "timeline_mode": "scene_local_pts_shifted_to_project_clock",
            "fullscreen_scene_ids": [scene["id"] for scene in scenes if _scene_presenter(scene).get("treatment") == "fullscreen"] if is_avatar_render else [],
            "pip_scene_ids": [item["scene_id"] for item in presenter_overlays],
            "overlay": overlay_data,
        } if is_avatar_render else None,
        "subtitles": {
            "mode": "scene_review_phrase_cues" if is_avatar_render else "project_narration_cues",
            "source_path": _safe_relpath(project_dir, str(subtitle_path)),
        },
        "story_headlines": headline_report,
        "surgical_directives": {"applied_ids": component_ids, "count": len(component_ids)},
        "narration_gain": audio_mix_result["narration"],
        "background_music": audio_mix_result["background_music"],
        "audio_mix_signature": _audio_mix_signature(state),
        "loudness": loudness_result,
    }
    if preview:
        final_report_path = project_dir / report_path
        staged_report_path = final_report_path.with_name(
            f".{final_report_path.stem}-{safe_preview_job_id}.staged{final_report_path.suffix}"
        )
        _atomic_write(staged_report_path, render_report)
        with _project_transaction_lock(project_dir):
            latest = _load_for_write(project_dir)
            latest_automation = _automation(latest)
            latest_job = latest_automation.get("preview_render") or {}
            if (
                latest_job.get("status") != "generating"
                or str(latest_job.get("job_id") or "") != preview_job_id
                or int(_as_number(latest_job.get("version"))) != preview_version
                or str(latest_job.get("parent_job_id") or "") != preview_parent_job_id
                or str(latest_job.get("input_fingerprint") or "") != preview_input_fingerprint
            ):
                raise WorkbenchError(
                    f"全片预览子任务已被替换；隔离输出保留在 {output.name}，旧 worker 禁止提交"
                )
            if preview_parent_job_id:
                latest_parent = latest_automation.get("review_preview_pipeline") or {}
                if (
                    latest_parent.get("job_id") != preview_parent_job_id
                    or latest_parent.get("status") != "running"
                    or str(latest_parent.get("worker_token") or "") != expected_parent_worker_token
                    or latest_parent.get("input_fingerprint") != preview_input_fingerprint
                ):
                    raise WorkbenchError(
                        f"全片预览父任务租约已变化；隔离输出保留在 {output.name}，旧 worker 禁止提交"
                    )
            os.replace(output, final_output)
            os.replace(staged_report_path, final_report_path)
            latest_automation["preview_render"] = {
                "status": "completed",
                "runtime": "ffmpeg",
                "output_path": render_report["output_path"],
                "report_path": report_path,
                "version": preview_version,
                "job_id": preview_job_id,
                "parent_job_id": preview_parent_job_id or None,
                "input_fingerprint": preview_input_fingerprint or None,
                "finished_at": _now(),
                "error": "",
            }
            latest_automation["status"] = "preview_ready"
            latest["automation"] = latest_automation
            _activity(
                latest,
                "full_preview_finished",
                f"全片预览 v{version} 已生成；请查看后再批量确认或回到指定片段修改",
                output_path=render_report["output_path"],
            )
            return _save(project_dir, latest)
    _atomic_write(project_dir / report_path, render_report)
    automation[job_key] = {
        "status": "completed", "runtime": "ffmpeg", "output_path": render_report["output_path"],
        "report_path": report_path, "version": job.get("version", 1),
        "parent_job_id": job.get("parent_job_id"), "input_fingerprint": job.get("input_fingerprint"),
        "finished_at": _now(), "error": "",
    }
    automation["status"] = "review_ready"
    _activity(state, "video_render_finished", "正式成片主体已生成，正在建立无背景音乐的可热插拔片段基线", output_path=render_report["output_path"])
    _save(project_dir, state)
    state = build_baseline_cache(project_dir)
    audio_mix_result = _apply_project_audio_mix(project_dir, state, output)
    render_report["narration_gain"] = audio_mix_result["narration"]
    render_report["background_music"] = audio_mix_result["background_music"]
    render_report["audio_mix_signature"] = _audio_mix_signature(state)
    render_report["loudness"] = _normalize_video_loudness(project_dir, output, target_lufs=-14.0)
    _atomic_write(project_dir / report_path, render_report)
    _activity(state, "video_render_finished", "成片与片段基线已就绪，可以进入逐片段审核或局部热插拔")
    return _save(project_dir, state)


def generate_full_preview_render(project_dir: Path) -> dict:
    """Generate a fast, web-optimised review candidate without baseline cache."""
    return _generate_project_video_render(project_dir, preview=True)


def review_preview_preflight(project_dir: Path, payload: dict | None = None) -> dict:
    """Lazy bridge used by server integration without creating an import cycle."""
    from backlot.review_preview_pipeline import review_preview_preflight as inspect
    return inspect(project_dir, payload)


def start_review_preview_job(project_dir: Path, payload: dict) -> dict:
    from backlot.review_preview_pipeline import start_review_preview_job as start
    return start(project_dir, payload)


def read_review_preview_job(project_dir: Path) -> dict:
    from backlot.review_preview_pipeline import read_review_preview_job as read
    return read(project_dir)


def resume_review_preview_job(project_dir: Path, job_id: str, payload: dict | None = None) -> dict:
    from backlot.review_preview_pipeline import resume_review_preview_job as resume
    return resume(project_dir, job_id, payload)


def run_review_preview_job(project_dir: Path, expected_job_id: str | None = None) -> dict:
    from backlot.review_preview_pipeline import run_review_preview_job as run
    return run(project_dir, expected_job_id)


def recover_review_preview_job(project_dir: Path) -> dict:
    from backlot.review_preview_pipeline import recover_review_preview_job as recover
    return recover(project_dir)


def generate_project_video_render(project_dir: Path) -> dict:
    """Generate the approved formal final and its hot-swap baseline cache."""
    return _generate_project_video_render(project_dir, preview=False)


# Compatibility names for callers saved by the older two-step UI.  They now
# stop after project narration so video composition remains a separate action.
def start_auto_final_generation(project_dir: Path, payload: dict) -> dict:
    return start_project_narration(project_dir, payload)


def mark_auto_final_generation_failed(project_dir: Path, error: object) -> dict:
    return mark_project_narration_failed(project_dir, error)


def generate_auto_final_video(project_dir: Path) -> dict:
    return generate_project_narration(project_dir)


def add_annotation(project_dir: Path, payload: dict) -> dict:
    state = _load_for_write(project_dir)
    scene_id = str(payload.get("scene_id") or "")
    scene = _find(state["scenes"], scene_id, "场景")
    text = str(payload.get("text") or "").strip()
    if not text:
        raise WorkbenchError("批注内容不能为空")
    annotation = {
        "id": _numbered("N-", scene.get("notes") or [], "id"), "at": _now(),
        "anchor_kind": payload.get("anchor_kind"), "text": text[:2000],
        "author": str(payload.get("author") or "人工审核"),
    }
    scene.setdefault("notes", []).append(annotation)
    _activity(state, "annotation", f"已记录 {scene_id} 的审核批注", scene_id=scene_id)
    return _save(project_dir, state)


def add_asset(project_dir: Path, payload: dict) -> dict:
    state = _load_for_write(project_dir)
    _append_asset(project_dir, state, payload)
    return _save(project_dir, state)


def _resolve_project_relative_path(project_dir: Path, raw_path: object) -> tuple[Path, str] | None:
    """Resolve one persisted project path without ever escaping its project.

    Older workbench files can contain a mixture of POSIX relative paths and
    Windows absolute paths.  The latter remain valid only when they still
    resolve inside the project; asset governance must treat every other value
    as invalid instead of following it during a cleanup operation.
    """
    text = str(raw_path or "").strip()
    if not text:
        return None
    candidate = Path(text)
    resolved = (candidate if candidate.is_absolute() else project_dir / candidate).resolve()
    try:
        relative = resolved.relative_to(project_dir.resolve()).as_posix()
    except (OSError, ValueError) as exc:
        raise WorkbenchError("素材记录包含项目目录外的路径，不能执行清理") from exc
    return resolved, relative


def _asset_registered_file_paths(project_dir: Path, asset: dict) -> tuple[list[dict], list[str]]:
    """Return the live files of an asset and any unsafe/stale path warnings."""
    seen: set[str] = set()
    files: list[dict] = []
    warnings: list[str] = []
    raw_paths: list[object] = [asset.get("path")]
    for version in asset.get("versions") or []:
        if isinstance(version, dict):
            raw_paths.append(version.get("path"))
    for raw_path in raw_paths:
        if not raw_path:
            continue
        try:
            resolved_item = _resolve_project_relative_path(project_dir, raw_path)
        except WorkbenchError:
            warnings.append(str(raw_path))
            continue
        if not resolved_item:
            continue
        path, relative = resolved_item
        key = os.path.normcase(str(path))
        if key in seen:
            continue
        seen.add(key)
        files.append({
            "path": path,
            "relative_path": relative,
            "exists": path.is_file(),
            "size_bytes": path.stat().st_size if path.is_file() else 0,
            "inside_assets": relative == "assets" or relative.startswith("assets/"),
        })
    return files, warnings


def _asset_reference_index(state: dict) -> dict[str, list[dict]]:
    """Build the active-delivery reference graph for registered material.

    The workbench keeps an append-only history of U-xxx usages.  Counting all
    of it as live would make old rejected candidates impossible to clean up.
    This index therefore follows only *current* timeline, narration, presenter
    and in-review decisions.  It is intentionally explicit rather than a
    blind recursive scan of historical activity logs.
    """
    references: dict[str, list[dict]] = {}

    def add(asset_id: object, kind: str, label: str, *, scene_id: object = None) -> None:
        asset_key = str(asset_id or "").strip()
        if not asset_key:
            return
        item = {"kind": kind, "label": label}
        if scene_id:
            item["scene_id"] = str(scene_id)
        references.setdefault(asset_key, []).append(item)

    for usage in state.get("usages") or []:
        if not isinstance(usage, dict) or not usage.get("selected"):
            continue
        scene_id = usage.get("scene_id")
        role = str(usage.get("role") or "素材")
        add(usage.get("asset_id"), "selected_usage", f"当前 {role} 使用", scene_id=scene_id)

    for scene in state.get("scenes") or []:
        if not isinstance(scene, dict):
            continue
        scene_id = scene.get("id")
        title = str(scene.get("title") or scene_id or "场景")
        timeline = scene.get("visual_timeline") if isinstance(scene.get("visual_timeline"), dict) else {}
        for block in timeline.get("blocks") or []:
            if isinstance(block, dict) and str(block.get("status") or "ready") != "failed":
                add(block.get("asset_id"), "visual_timeline", f"{title} · 画面区间 {block.get('id') or ''}".strip(), scene_id=scene_id)
        composition = scene.get("visual_composition") if isinstance(scene.get("visual_composition"), dict) else {}
        for overlay in composition.get("overlays") or []:
            if isinstance(overlay, dict):
                add(
                    overlay.get("asset_id"),
                    "visual_composition",
                    f"{title} · 重点素材 {overlay.get('id') or ''}".strip(),
                    scene_id=scene_id,
                )
        presenter = scene.get("presenter") if isinstance(scene.get("presenter"), dict) else {}
        if presenter.get("treatment") != "hidden":
            add(presenter.get("asset_id"), "presenter", f"{title} · 数字人出镜", scene_id=scene_id)
        narration = scene.get("narration") if isinstance(scene.get("narration"), dict) else {}
        current_id = narration.get("current_version_id")
        for version in narration.get("versions") or []:
            if isinstance(version, dict) and version.get("id") == current_id:
                add(version.get("asset_id"), "narration", f"{title} · 当前配音", scene_id=scene_id)
        for field, label in (
            ("ai_visual_candidate", "AI 主体候选"),
            ("motion_visual_candidate", "动态画面候选"),
            ("ppt_card_candidate", "静态信息图候选"),
        ):
            candidate = scene.get(field)
            if isinstance(candidate, dict) and str(candidate.get("status") or "ready") not in {"failed", "discarded"}:
                add(candidate.get("asset_id"), "review_candidate", f"{title} · {label}", scene_id=scene_id)
        review = scene.get("keyframe_review") if isinstance(scene.get("keyframe_review"), dict) else {}
        if review and str(review.get("status") or "pending") not in {"failed", "discarded"}:
            for item in review.get("timeline") or []:
                if isinstance(item, dict):
                    add(item.get("asset_id"), "keyframe_review", f"{title} · 关键帧审核", scene_id=scene_id)

    avatar = state.get("avatar") if isinstance(state.get("avatar"), dict) else {}
    add(avatar.get("master_asset_id"), "avatar_master", "数字人原声母版")
    for turn in avatar.get("turns") or []:
        if not isinstance(turn, dict):
            continue
        add(turn.get("asset_id"), "avatar_turn", f"数字人轮次 {turn.get('turn_id') or ''}".strip())
        add(turn.get("video_asset_id"), "avatar_turn", f"数字人轮次 {turn.get('turn_id') or ''}".strip())

    active_patch_statuses = {"draft", "planned", "blocked", "ready_to_render", "rendering", "rendered"}
    for patch in state.get("patches") or []:
        if not isinstance(patch, dict) or str(patch.get("status") or "") not in active_patch_statuses:
            continue
        segment = str(patch.get("segment_id") or "局部片段")
        add(patch.get("candidate_asset_id"), "patch", f"{segment} · 局部画面任务")
        add(patch.get("candidate_audio_asset_id"), "patch", f"{segment} · 局部配音任务")
    return references


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def audit_asset_library(project_dir: Path) -> dict:
    """Inspect the project asset ledger without changing a single file.

    The returned audit is intentionally derived rather than persisted.  A
    subsequent cleanup recomputes it under the writer path, preventing a stale
    browser scan from deleting material that became active in the meantime.
    """
    state = read_workbench(project_dir)
    references = _asset_reference_index(state)
    all_usage_counts: dict[str, int] = {}
    for usage in state.get("usages") or []:
        if isinstance(usage, dict) and usage.get("asset_id"):
            key = str(usage["asset_id"])
            all_usage_counts[key] = all_usage_counts.get(key, 0) + 1

    rows: list[dict] = []
    hashes: dict[tuple[int, str], list[str]] = {}
    for asset in state.get("assets") or []:
        if not isinstance(asset, dict) or not asset.get("id"):
            continue
        asset_id = str(asset["id"])
        lifecycle = asset.get("lifecycle") if isinstance(asset.get("lifecycle"), dict) else {}
        lifecycle_status = str(lifecycle.get("status") or "active")
        file_entries, invalid_paths = _asset_registered_file_paths(project_dir, asset)
        existing = [item for item in file_entries if item["exists"]]
        recycled_entries: list[dict] = []
        if lifecycle_status == "trashed":
            raw_recycled = lifecycle.get("trashed_files") if isinstance(lifecycle.get("trashed_files"), list) else []
            if not raw_recycled and lifecycle.get("trash_path"):
                raw_recycled = [{"trash_path": lifecycle.get("trash_path")}]
            for entry in raw_recycled:
                if not isinstance(entry, dict):
                    continue
                try:
                    resolved_item = _resolve_project_relative_path(project_dir, entry.get("trash_path"))
                except WorkbenchError:
                    continue
                if not resolved_item:
                    continue
                recycled_path, recycled_relative = resolved_item
                if recycled_path.is_file():
                    recycled_entries.append({
                        "path": recycled_path,
                        "relative_path": recycled_relative,
                        "exists": True,
                        "size_bytes": recycled_path.stat().st_size,
                        "inside_assets": False,
                    })
        refs = references.get(asset_id, [])
        if lifecycle_status == "trashed":
            status = "trashed"
        elif refs:
            status = "active"
        elif not file_entries:
            status = "record_only"
        elif not existing:
            status = "missing"
        else:
            status = "unused"
        cleanup_eligible = (
            status == "unused"
            and str(asset.get("source_type") or "") in ASSET_AUTO_CLEANUP_SOURCE_TYPES
            and bool(existing)
            and not invalid_paths
            and all(item["inside_assets"] for item in file_entries)
        )
        if status != "unused":
            cleanup_reason = "当前被交付内容引用" if status == "active" else ("已在项目回收站" if status == "trashed" else "没有可清理的本地文件")
        elif str(asset.get("source_type") or "") not in ASSET_AUTO_CLEANUP_SOURCE_TYPES:
            cleanup_reason = "人工或项目库素材受保护，不纳入一键清理"
        elif invalid_paths or not all(item["inside_assets"] for item in file_entries):
            cleanup_reason = "文件不完全位于 assets/，为安全起见不自动清理"
        else:
            cleanup_reason = "未被当前时间线、配音、关键帧审核或局部任务引用"
        display_files = recycled_entries if status == "trashed" else existing
        row = {
            "id": asset_id,
            "name": str(asset.get("name") or asset_id),
            "type": str(asset.get("type") or "unknown"),
            "source_type": str(asset.get("source_type") or "undecided"),
            "path": asset.get("path"),
            "status": status,
            "references": refs,
            "history_usage_count": all_usage_counts.get(asset_id, 0),
            "file_count": len(display_files),
            "size_bytes": sum(int(item["size_bytes"]) for item in display_files),
            "file_paths": [item["relative_path"] for item in display_files],
            "missing_paths": [] if status == "trashed" else [item["relative_path"] for item in file_entries if not item["exists"]],
            "invalid_paths": invalid_paths,
            "cleanup_eligible": cleanup_eligible,
            "cleanup_reason": cleanup_reason,
            "lifecycle": lifecycle_status,
            "trash_path": lifecycle.get("trash_path"),
            "duplicate_group": None,
            "duplicate_count": 0,
        }
        rows.append(row)
        if status != "trashed":
            for item in existing:
                # Hashes are only calculated during an explicit audit request,
                # never on normal workbench render or SSE refresh.
                fingerprint = _sha256_file(item["path"])
                hashes.setdefault((int(item["size_bytes"]), fingerprint), []).append(asset_id)

    duplicate_groups: list[dict] = []
    for (size_bytes, fingerprint), ids in hashes.items():
        unique_ids = sorted(set(ids))
        if len(unique_ids) < 2:
            continue
        group_id = f"dup-{fingerprint[:12]}"
        duplicate_groups.append({
            "id": group_id,
            "sha256": fingerprint,
            "size_bytes": size_bytes,
            "asset_ids": unique_ids,
        })
        for row in rows:
            if row["id"] in unique_ids:
                row["duplicate_group"] = group_id
                row["duplicate_count"] = len(unique_ids)

    summary = {
        "total_assets": len(rows),
        "active_count": sum(item["status"] == "active" for item in rows),
        "unused_count": sum(item["status"] == "unused" for item in rows),
        "missing_count": sum(item["status"] == "missing" for item in rows),
        "record_only_count": sum(item["status"] == "record_only" for item in rows),
        "trashed_count": sum(item["status"] == "trashed" for item in rows),
        "duplicate_group_count": len(duplicate_groups),
        "reclaimable_file_count": sum(item["file_count"] for item in rows if item["cleanup_eligible"]),
        "reclaimable_bytes": sum(item["size_bytes"] for item in rows if item["cleanup_eligible"]),
    }
    return {"generated_at": _now(), "summary": summary, "assets": rows, "duplicate_groups": duplicate_groups}


def cleanup_unused_assets(project_dir: Path, payload: dict) -> dict:
    """Move explicitly selected safe leftovers into the project recycle bin.

    This is deliberately a move, not a delete.  The ledger record and its
    history survive, the original path is retained, and the UI can restore it
    with one operation.  ``audit_asset_library`` is recomputed here to close
    the race between a scan and a user pressing the cleanup button.
    """
    if payload.get("confirmed") is not True:
        raise WorkbenchError("请确认后再将未使用素材移入项目回收站")
    requested_ids = [str(item).strip() for item in (payload.get("asset_ids") or []) if str(item).strip()]
    requested_ids = list(dict.fromkeys(requested_ids))
    if not requested_ids:
        raise WorkbenchError("请至少选择一个可清理素材")
    state = _load_for_write(project_dir)
    audit = audit_asset_library(project_dir)
    by_row = {item["id"]: item for item in audit["assets"]}
    assets = {str(item.get("id")): item for item in state.get("assets", []) if isinstance(item, dict)}
    batch_id = datetime.now(UTC).strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:6]
    recycle_root = project_dir / ASSET_RECYCLE_DIRECTORY / batch_id
    moved_assets: list[str] = []
    skipped: list[str] = []
    for asset_id in requested_ids:
        row = by_row.get(asset_id)
        asset = assets.get(asset_id)
        if not row or not asset or not row.get("cleanup_eligible"):
            skipped.append(asset_id)
            continue
        file_entries, invalid_paths = _asset_registered_file_paths(project_dir, asset)
        live_files = [item for item in file_entries if item["exists"]]
        if invalid_paths or not live_files or not all(item["inside_assets"] for item in file_entries):
            skipped.append(asset_id)
            continue
        moved: list[tuple[Path, Path, str]] = []
        try:
            for item in live_files:
                source = item["path"]
                relative_under_assets = Path(item["relative_path"]).relative_to("assets")
                target = recycle_root / asset_id / relative_under_assets
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(source), str(target))
                moved.append((source, target, item["relative_path"]))
        except OSError:
            for source, target, _ in reversed(moved):
                try:
                    source.parent.mkdir(parents=True, exist_ok=True)
                    if target.exists() and not source.exists():
                        shutil.move(str(target), str(source))
                except OSError:
                    pass
            skipped.append(asset_id)
            continue
        asset["lifecycle"] = {
            "status": "trashed",
            "trashed_at": _now(),
            "reason": "unused_cleanup",
            "batch_id": batch_id,
            "original_path": asset.get("path"),
            "trash_path": _safe_relpath(project_dir, str(moved[0][1])) if moved else None,
            "trashed_files": [
                {"original_path": original, "trash_path": _safe_relpath(project_dir, str(target))}
                for _, target, original in moved
            ],
        }
        moved_assets.append(asset_id)
    if not moved_assets:
        raise WorkbenchError("没有可安全移入回收站的素材；它们可能已被使用、受保护或文件已不存在")
    _activity(state, "asset_cleanup", f"已将 {len(moved_assets)} 个未使用素材移入项目回收站", asset_ids=moved_assets, skipped_asset_ids=skipped, batch_id=batch_id)
    return _save(project_dir, state)


def restore_trashed_asset(project_dir: Path, asset_id: str) -> dict:
    """Restore one recycle-bin asset to exactly its registered project path."""
    state = _load_for_write(project_dir)
    asset = _find(state.get("assets") or [], str(asset_id), "素材")
    lifecycle = asset.get("lifecycle") if isinstance(asset.get("lifecycle"), dict) else {}
    if lifecycle.get("status") != "trashed":
        raise WorkbenchError("该素材当前不在项目回收站")
    entries = lifecycle.get("trashed_files") if isinstance(lifecycle.get("trashed_files"), list) else []
    if not entries and lifecycle.get("trash_path") and lifecycle.get("original_path"):
        entries = [{"trash_path": lifecycle.get("trash_path"), "original_path": lifecycle.get("original_path")}]
    if not entries:
        raise WorkbenchError("回收站记录不完整，无法安全恢复该素材")
    restored: list[tuple[Path, Path]] = []
    try:
        for entry in entries:
            source_item = _resolve_project_relative_path(project_dir, entry.get("trash_path"))
            target_item = _resolve_project_relative_path(project_dir, entry.get("original_path"))
            if not source_item or not target_item:
                raise WorkbenchError("回收站路径无效")
            source, _ = source_item
            target, _ = target_item
            try:
                target.relative_to((project_dir / "assets").resolve())
            except ValueError as exc:
                raise WorkbenchError("回收站素材只能恢复到项目 assets 目录") from exc
            if not source.is_file():
                raise WorkbenchError("回收站中的源文件不存在，无法恢复")
            if target.exists():
                raise WorkbenchError("原始位置已有同名文件；为避免覆盖，未执行恢复")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(target))
            restored.append((source, target))
    except (OSError, WorkbenchError):
        for source, target in reversed(restored):
            try:
                source.parent.mkdir(parents=True, exist_ok=True)
                if target.exists() and not source.exists():
                    shutil.move(str(target), str(source))
            except OSError:
                pass
        raise
    asset["lifecycle"] = {"status": "active", "restored_at": _now(), "restored_from_batch": lifecycle.get("batch_id")}
    _activity(state, "asset_restored", f"已从项目回收站恢复素材 {asset['id']}", asset_id=asset["id"])
    return _save(project_dir, state)


def _safe_project_asset_upload_name(original_filename: str) -> tuple[str, str]:
    leaf = Path(str(original_filename or "").replace("\\", "/")).name.strip()
    suffix = Path(leaf).suffix.lower()
    media_type = PROJECT_ASSET_UPLOAD_TYPES.get(suffix)
    if not leaf or not media_type:
        raise WorkbenchError("只支持上传常见的视频或图片素材")
    stem = re.sub(r"[^\w\- .()（）\u4e00-\u9fff]+", "_", Path(leaf).stem, flags=re.UNICODE).strip(" ._")
    return f"{(stem or 'asset')[:96]}{suffix}", media_type


def prepare_project_asset_upload(project_dir: Path, original_filename: str) -> Path:
    """Create one project-contained temporary file for a streamed local upload."""
    safe_name, _ = _safe_project_asset_upload_name(original_filename)
    project = project_dir.resolve(strict=True)
    root = (project / PROJECT_ASSET_UPLOAD_DIRECTORY).resolve()
    try:
        root.relative_to(project)
    except ValueError as exc:
        raise WorkbenchError("项目素材上传目录越过了当前项目边界") from exc
    root.mkdir(parents=True, exist_ok=True)
    temporary = (root / f".incoming-{uuid4().hex}-{safe_name}").resolve()
    try:
        temporary.relative_to(root)
    except ValueError as exc:
        raise WorkbenchError("素材上传临时路径无效") from exc
    temporary.touch(exist_ok=False)
    return temporary


def complete_project_asset_upload(
    project_dir: Path,
    temporary_path: Path,
    original_filename: str,
    *,
    display_name: str = "",
    license_notice: str = "",
    content_sha256: str = "",
    max_bytes: int = MAX_PROJECT_ASSET_BYTES,
) -> dict:
    """Validate, atomically adopt and register one browser-uploaded visual asset."""
    safe_name, media_type = _safe_project_asset_upload_name(original_filename)
    project = project_dir.resolve(strict=True)
    root = (project / PROJECT_ASSET_UPLOAD_DIRECTORY).resolve()
    temporary = temporary_path.resolve(strict=True)
    try:
        temporary.relative_to(root)
    except ValueError as exc:
        raise WorkbenchError("上传文件不在当前项目的安全临时目录中") from exc
    if not temporary.is_file() or not temporary.name.startswith(".incoming-"):
        raise WorkbenchError("素材上传临时文件无效")
    size = temporary.stat().st_size
    if size <= 0:
        raise WorkbenchError("上传的素材文件为空")
    if size > int(max_bytes):
        raise WorkbenchError(f"单个素材不能超过 {int(max_bytes) // (1024 ** 3)}GB")
    digest = str(content_sha256 or "").lower()
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        digest = _sha256_file(temporary)
    final = (root / f"asset-{digest}{Path(safe_name).suffix.lower()}").resolve()
    try:
        final.relative_to(root)
    except ValueError as exc:
        raise WorkbenchError("素材上传目标路径无效") from exc
    created_final = False
    if final.exists():
        if not final.is_file() or _sha256_file(final) != digest:
            raise WorkbenchError("素材内容标识发生冲突，未覆盖现有文件")
        temporary.unlink(missing_ok=True)
    else:
        os.replace(temporary, final)
        created_final = True

    try:
        duration: float | None = None
        resolution: dict | None = None
        if media_type == "video":
            probe = _probe_video(final, _ffmpeg_available())
            streams = probe.get("streams") if isinstance(probe, dict) else []
            video = next((item for item in streams or [] if isinstance(item, dict) and item.get("codec_type") == "video"), None)
            duration = _as_number(((probe or {}).get("format") or {}).get("duration")) if isinstance(probe, dict) else 0
            if not video or duration <= 0:
                raise WorkbenchError("上传文件不是可读取的有效视频，请检查格式或 FFmpeg 配置")
            resolution = {"width": int(video.get("width") or 0), "height": int(video.get("height") or 0)}
        else:
            try:
                from PIL import Image
                with Image.open(final) as image:
                    image.verify()
                with Image.open(final) as image:
                    resolution = {"width": int(image.width), "height": int(image.height)}
            except Exception as exc:
                raise WorkbenchError("上传文件不是可读取的有效图片") from exc

        state = _load_for_write(project)
        relative = _safe_relpath(project, str(final))
        existing = next((item for item in state.get("assets") or [] if str(item.get("path") or "") == str(relative)), None)
        if existing:
            _activity(state, "asset_upload_reused", f"已复用相同内容的素材 {existing['id']}", asset_id=existing["id"])
            return _save(project, state)
        _append_asset(project, state, {
            "name": str(display_name or Path(safe_name).stem)[:160],
            "type": media_type,
            "source_type": "human_provided",
            "path": relative,
            "duration_seconds": round(duration, 3) if duration else None,
            "resolution": resolution,
            "provider": "本地上传",
            "source_tool": "workbench_asset_upload",
            "license": str(license_notice or "用户上传；发布前请确认使用权")[:500],
        })
        return _save(project, state)
    except Exception:
        if created_final:
            try:
                final.unlink()
            except OSError:
                pass
        raise


def _append_asset(project_dir: Path, state: dict, payload: dict) -> dict:
    """Append a traceable material record without assigning it to a scene."""
    source_type = str(payload.get("source_type") or "undecided")
    if source_type not in SOURCE_TYPES:
        raise WorkbenchError("未知的素材来源类型")
    path = _safe_relpath(project_dir, payload.get("path"))
    if path and not (project_dir / path).is_file():
        raise WorkbenchError("素材路径不存在；请先将文件放入当前项目目录")
    asset_id = _numbered("S-", state["assets"], "id")
    asset = {
        "id": asset_id, "legacy_id": None,
        "name": str(payload.get("name") or asset_id).strip()[:160],
        "type": str(payload.get("type") or "video"), "source_type": source_type,
        "path": path, "duration_seconds": payload.get("duration_seconds"),
        "resolution": payload.get("resolution"),
        "provenance": {
            "provider": str(payload.get("provider") or "人工登记"),
            "source_tool": str(payload.get("source_tool") or "workbench"),
            "license": str(payload.get("license") or "待补充"),
            "source_url": payload.get("source_url"),
        },
        "versions": [{"id": f"{asset_id}-V001", "created_at": _now(), "path": path, "status": "current"}],
        "created_at": _now(),
    }
    generation = payload.get("generation")
    if isinstance(generation, dict):
        asset["generation"] = generation
    state["assets"].append(asset)
    _activity(state, "asset", f"已登记素材 {asset_id}", asset_id=asset_id)
    return asset


def _media_index_source(project_dir: Path, state: dict, asset_id: str) -> tuple[dict, Path]:
    asset = _find(state.get("assets") or [], asset_id, "素材")
    if str(asset.get("type") or "").lower() != "video" or not asset.get("path"):
        raise WorkbenchError("只有已登记到项目内的视频素材可以建立片段索引")
    source = (project_dir / str(asset.get("path"))).resolve()
    try:
        source.relative_to(project_dir.resolve())
    except ValueError as exc:
        raise WorkbenchError("待分析素材不在当前项目目录内") from exc
    if not source.is_file():
        raise WorkbenchError("待分析素材文件不存在")
    return asset, source


def _media_transcript_provider(model_path: str | None = None):
    """Create one lazy, local-only Whisper provider for a media-index job."""
    cache: dict[str, Any] = {}

    def transcribe(path: Path) -> tuple[str, list[dict], dict]:
        from backlot.avatar_import import _load_whisper, _transcribe_file

        if "model" not in cache:
            model, selected = _load_whisper(model_path)
            cache.update({"model": model, "selected": selected})
        text, segments = _transcribe_file(cache["model"], path, word_timestamps=True, beam_size=5)
        return text, segments, {
            "provider": "faster-whisper-local",
            "model": Path(str(cache["selected"])).parent.parent.name.replace("models--Systran--", ""),
            "local_only": True,
        }

    return transcribe


def start_asset_media_index(project_dir: Path, asset_id: str, payload: dict) -> dict:
    """Queue one local evidence or confirmed visual job on the shared media runtime."""
    state = _load_for_write(project_dir)
    asset, _ = _media_index_source(project_dir, state, asset_id)
    automation = _automation(state)
    batch = automation["media_index_batch"]
    supplied_batch_id = str(payload.get("batch_job_id") or "")
    if batch.get("status") in {"queued", "generating"} and supplied_batch_id != str(batch.get("job_id") or ""):
        raise WorkbenchError("本项目正在批量理解本地视频；请等待批量任务完成后再单独操作")
    current = automation["media_index"]
    if current.get("status") in {"queued", "generating"}:
        raise WorkbenchError("已有本地视频素材分析任务正在运行，请等待完成")
    stage = str(payload.get("stage") or "coarse")
    if stage not in {"coarse", "fine", "vision"}:
        raise WorkbenchError("素材分析阶段只能是粗筛、精筛或视觉理解")
    if stage == "fine":
        coarse_path = str((asset.get("media_index") or {}).get("coarse_index_path") or "")
        if not coarse_path or not (project_dir / coarse_path).is_file():
            raise WorkbenchError("请先完成该素材的粗筛索引，再开始精筛")
        start = _rounded_seconds(payload.get("start_seconds"))
        end = _rounded_seconds(payload.get("end_seconds"))
        if end - start < .4:
            raise WorkbenchError("精筛窗口至少需要 0.4 秒")
    if stage == "vision" and payload.get("remote_vision_confirmed") is not True:
        raise WorkbenchError("视觉理解会把去重后的关键帧发送给已配置的 AI 服务，请先明确确认")
    job_id = f"MIJ-{uuid4().hex[:10]}"
    job = {
        "status": "queued", "job_id": job_id, "asset_id": asset_id, "stage": stage,
        "started_at": _now(), "finished_at": None, "result": None, "error": "",
        "request": {
            "transcribe": bool(payload.get("transcribe")),
            "model_path": str(payload.get("model_path") or "") or None,
            "query": str(payload.get("query") or "")[:1000],
            "start_seconds": _rounded_seconds(payload.get("start_seconds")),
            "end_seconds": _rounded_seconds(payload.get("end_seconds")),
            "remote_vision_confirmed": payload.get("remote_vision_confirmed") is True,
        },
        "progress": {"stage": "queued", "message": "等待共享媒体分析资源"},
    }
    automation["media_index"] = job
    asset["media_index"] = {**(asset.get("media_index") or {}), "status": "queued", "job_id": job_id, "stage": stage}
    label = {"coarse": "本地粗筛", "fine": "本地精筛", "vision": "画面理解"}[stage]
    _activity(state, "media_index_started", f"已开始 {asset_id} 的{label}", asset_id=asset_id, job_id=job_id)
    return _save(project_dir, state)


_LOCAL_MATERIAL_BATCH_SOURCES = {"human_provided", "local_generated", "project_library"}


def _is_batch_local_video(asset: object) -> bool:
    return (
        isinstance(asset, dict)
        and str(asset.get("type") or "").lower() == "video"
        and str(asset.get("source_type") or "") in _LOCAL_MATERIAL_BATCH_SOURCES
    )


def start_asset_media_index_batch(project_dir: Path, payload: dict) -> dict:
    """Create a durable, serial V2 visual-understanding queue for local videos.

    This is intentionally a single confirmed operation rather than a browser
    loop.  A worker can resume it after a server restart without re-submitting
    already completed assets, and individual failures do not discard the rest
    of the user's material preparation.
    """
    if not isinstance(payload, dict) or payload.get("remote_vision_confirmed") is not True:
        raise WorkbenchError("批量画面理解会把去重后的关键帧发送给已配置的 AI 服务，请先明确确认")
    state = _load_for_write(project_dir)
    automation = _automation(state)
    batch = automation["media_index_batch"]
    if batch.get("status") in {"queued", "generating"}:
        raise WorkbenchError("已有本地视频批量理解任务正在运行，请等待完成")
    if automation["media_index"].get("status") in {"queued", "generating"}:
        raise WorkbenchError("已有单个本地视频分析任务正在运行，请等待完成后再批量理解")

    requested = payload.get("asset_ids")
    requested_ids = {str(item) for item in requested if str(item).strip()} if isinstance(requested, list) else None
    candidate_ids: list[str] = []
    skipped: list[dict[str, str]] = []
    for asset in state.get("assets") or []:
        if not _is_batch_local_video(asset):
            continue
        asset_id = str(asset.get("id") or "")
        if requested_ids is not None and asset_id not in requested_ids:
            continue
        media_state = asset.get("media_index") if isinstance(asset.get("media_index"), dict) else {}
        if media_state.get("vision_index_path"):
            skipped.append({"asset_id": asset_id, "reason": "已有视觉理解索引"})
            continue
        try:
            _media_index_source(project_dir, state, asset_id)
        except WorkbenchError as exc:
            skipped.append({"asset_id": asset_id, "reason": str(exc)[:300]})
            continue
        candidate_ids.append(asset_id)
    if not candidate_ids:
        if skipped:
            raise WorkbenchError("没有需要理解的本地视频；已有索引可在素材库中查看或单独重建")
        raise WorkbenchError("请先上传至少一个项目内本地视频，再开始批量理解")

    job_id = f"MIB-{uuid4().hex[:10]}"
    automation["media_index_batch"] = {
        "status": "queued", "job_id": job_id, "stage": "vision",
        "asset_ids": candidate_ids, "pending_asset_ids": list(candidate_ids),
        "completed_asset_ids": [], "failed_assets": [], "skipped_assets": skipped,
        "current_asset_id": None, "started_at": _now(), "finished_at": None,
        "error": "", "request": {"remote_vision_confirmed": True},
    }
    _activity(state, "media_index_batch_started", f"已开始批量理解 {len(candidate_ids)} 个本地视频", job_id=job_id)
    return _save(project_dir, state)


def _finish_media_index_batch(state: dict, batch: dict, *, status: str) -> dict:
    batch.update({"status": status, "current_asset_id": None, "finished_at": _now()})
    message = (
        f"本地视频批量理解完成：{len(batch.get('completed_asset_ids') or [])} 个成功"
        f"，{len(batch.get('failed_assets') or [])} 个失败"
    )
    _activity(state, "media_index_batch_completed", message, job_id=batch.get("job_id"))
    return state


def generate_asset_media_index_batch(project_dir: Path, expected_job_id: str) -> dict:
    """Run or resume a batch queue without ever overlapping the media runtime."""
    while True:
        state = _load_for_write(project_dir)
        automation = _automation(state)
        batch = automation["media_index_batch"]
        if str(batch.get("job_id") or "") != str(expected_job_id) or batch.get("status") not in {"queued", "generating"}:
            return state
        pending = [str(item) for item in (batch.get("pending_asset_ids") or []) if str(item)]
        if not pending:
            final_status = "completed_with_warnings" if batch.get("failed_assets") else "completed"
            return _save(project_dir, _finish_media_index_batch(state, batch, status=final_status))

        asset_id = pending[0]
        current = automation["media_index"]
        batch.update({
            "status": "generating", "current_asset_id": asset_id,
            "progress": {"completed": len(batch.get("completed_asset_ids") or []), "total": len(batch.get("asset_ids") or []), "message": f"正在理解 {asset_id}"},
        })
        _save(project_dir, state)
        error_message = ""
        try:
            current_is_item = str(current.get("asset_id") or "") == asset_id
            current_matches = current_is_item and current.get("status") in {"queued", "generating"}
            # A process can stop after the child index wrote its completed
            # record but before this queue removed the asset from pending.
            # In that recovery window, consume the durable child result below
            # instead of submitting the same visual request a second time.
            child_already_terminal = current_is_item and current.get("status") in {"completed", "failed"}
            if not current_matches and not child_already_terminal:
                started = start_asset_media_index(project_dir, asset_id, {
                    "stage": "vision", "remote_vision_confirmed": True, "batch_job_id": expected_job_id,
                })
                current = _automation(started)["media_index"]
            if not child_already_terminal:
                generate_asset_media_index(project_dir, str(current.get("job_id") or ""))
        except Exception as exc:
            error_message = str(exc)[:1200] or "素材视觉理解失败"
            try:
                latest = _load_for_write(project_dir)
                current_job = _automation(latest)["media_index"]
                if str(current_job.get("asset_id") or "") == asset_id and current_job.get("status") in {"queued", "generating"}:
                    mark_asset_media_index_failed(project_dir, str(current_job.get("job_id") or ""), exc)
            except Exception:
                pass

        state = _load_for_write(project_dir)
        automation = _automation(state)
        batch = automation["media_index_batch"]
        if str(batch.get("job_id") or "") != str(expected_job_id) or batch.get("status") not in {"queued", "generating"}:
            return state
        batch["pending_asset_ids"] = [item for item in (batch.get("pending_asset_ids") or []) if str(item) != asset_id]
        asset = next((item for item in state.get("assets") or [] if str(item.get("id") or "") == asset_id), {})
        media_state = asset.get("media_index") if isinstance(asset.get("media_index"), dict) else {}
        succeeded = not error_message and bool(media_state.get("vision_index_path")) and media_state.get("status") == "completed"
        if succeeded:
            batch["completed_asset_ids"] = list(dict.fromkeys([*(batch.get("completed_asset_ids") or []), asset_id]))
        else:
            batch["failed_assets"] = [item for item in (batch.get("failed_assets") or []) if str(item.get("asset_id") or "") != asset_id]
            batch["failed_assets"].append({"asset_id": asset_id, "error": error_message or str(media_state.get("error") or "未生成视觉理解索引")[:1200]})
        batch["current_asset_id"] = None
        _save(project_dir, state)


def mark_asset_media_index_batch_failed(project_dir: Path, expected_job_id: str, error: object) -> dict:
    state = _load_for_write(project_dir)
    batch = _automation(state)["media_index_batch"]
    if str(batch.get("job_id") or "") != str(expected_job_id) or batch.get("status") not in {"queued", "generating"}:
        return state
    message = str(error)[:1200] or "批量素材理解任务异常中止"
    batch.update({"status": "failed", "finished_at": _now(), "current_asset_id": None, "error": message})
    _activity(state, "media_index_batch_failed", f"本地视频批量理解异常中止：{message[:160]}", job_id=expected_job_id)
    return _save(project_dir, state)


def generate_asset_media_index(project_dir: Path, expected_job_id: str) -> dict:
    state = _load_for_write(project_dir)
    job = _automation(state)["media_index"]
    if str(job.get("job_id") or "") != str(expected_job_id) or job.get("status") not in {"queued", "generating"}:
        return state
    asset_id = str(job.get("asset_id") or "")
    asset, source = _media_index_source(project_dir, state, asset_id)
    job["status"] = "generating"
    job["progress"] = {"stage": "local_analysis", "message": "正在读取媒体并建立证据"}
    asset["media_index"] = {**(asset.get("media_index") or {}), "status": "generating", "job_id": expected_job_id, "stage": job.get("stage")}
    _save(project_dir, state)

    ffmpeg = _ffmpeg_available()
    ffprobe = _ffprobe_available(ffmpeg)
    if not ffmpeg or not ffprobe:
        raise WorkbenchError("本机缺少 FFmpeg/ffprobe，无法分析视频素材")
    request = job.get("request") if isinstance(job.get("request"), dict) else {}
    provider = _media_transcript_provider(request.get("model_path")) if request.get("transcribe") else None
    output_dir = project_dir / "artifacts" / "media-index" / re.sub(r"[^A-Za-z0-9_-]", "-", asset_id)
    if job.get("stage") == "coarse":
        result = build_coarse_index(
            source,
            output_dir,
            ffmpeg=ffmpeg,
            ffprobe=ffprobe,
            transcript_provider=provider,
        )
        index_relative = _safe_relpath(project_dir, str(result["index_path"]))
        recommendations = recommend_coarse_segments(result, str(request.get("query") or ""), limit=6) if request.get("query") else []
        result_summary = {
            "stage": "coarse", "index_path": index_relative,
            "duration_seconds": (result.get("probe") or {}).get("duration_seconds"),
            "segment_count": len(result.get("segments") or []),
            "representative_frame_count": len(result.get("representative_frames") or []),
            "transcript_status": result.get("transcript_status"),
            "recommendations": recommendations,
            "cache_hit": bool(result.get("cache_hit")),
        }
    elif job.get("stage") == "fine":
        coarse_relative = str((asset.get("media_index") or {}).get("coarse_index_path") or "")
        coarse_path = project_dir / coarse_relative
        coarse = json.loads(coarse_path.read_text(encoding="utf-8"))
        coarse_source = (coarse.get("source") or {}) if isinstance(coarse.get("source"), dict) else {}
        try:
            recorded_source = Path(str(coarse_source.get("path") or "")).resolve()
        except OSError as exc:
            raise WorkbenchError("粗筛索引记录的素材路径无效，请重新粗筛") from exc
        if recorded_source != source.resolve():
            raise WorkbenchError("粗筛索引与当前素材不一致，请重新粗筛")
        if str(coarse_source.get("fingerprint") or "") != media_fingerprint(source):
            raise WorkbenchError("视频素材在粗筛后已经变化，请重新建立粗筛索引")
        result = build_fine_index(
            coarse,
            _as_number(request.get("start_seconds")),
            _as_number(request.get("end_seconds")),
            ffmpeg=ffmpeg,
            transcript_provider=provider,
        )
        index_relative = _safe_relpath(project_dir, str(result["index_path"]))
        result_summary = {
            "stage": "fine", "index_path": index_relative,
            "start_seconds": result.get("start_seconds"), "end_seconds": result.get("end_seconds"),
            "frame_count": len(result.get("frames") or []),
            "transcript_status": result.get("transcript_status"),
            "transcript": result.get("transcript"),
            "cache_hit": bool(result.get("cache_hit")),
        }
    else:
        if request.get("remote_vision_confirmed") is not True:
            raise WorkbenchError("视觉理解缺少关键帧外发确认")
        identity = vision_runtime_identity()
        preflight_holder: dict[str, Any] = {}

        def run_confirmed_vision(shots: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
            if not preflight_holder:
                progress_state = _load_for_write(project_dir)
                progress_job = _automation(progress_state)["media_index"]
                if str(progress_job.get("job_id") or "") != str(expected_job_id):
                    raise WorkbenchError("素材视觉任务已经被更新的任务替代")
                progress_job["progress"] = {"stage": "vision_preflight", "message": "正在验证图片输入和多图顺序"}
                _save(project_dir, progress_state)
                preflight = test_vision_ai_connection()
                preflight_holder.update(preflight)
            progress_state = _load_for_write(project_dir)
            progress_job = _automation(progress_state)["media_index"]
            progress_job["progress"] = {"stage": "vision_describe", "message": "正在按镜头理解去重后的关键帧"}
            _save(project_dir, progress_state)
            return describe_shots(shots)

        result = build_material_vision_index(
            source,
            output_dir,
            ffmpeg=ffmpeg,
            ffprobe=ffprobe,
            vision_describer=run_confirmed_vision,
            vision_identity=identity,
        )
        index_relative = _safe_relpath(project_dir, str(result["index_path"]))
        selected_frame_count = sum(
            1 for shot in result.get("shots") or [] for frame in shot.get("frames") or []
            if frame.get("selected_for_vision")
        )
        result_summary = {
            "stage": "vision", "index_path": index_relative,
            "duration_seconds": (result.get("probe") or {}).get("duration_seconds"),
            "shot_count": len(result.get("shots") or []),
            "selected_frame_count": selected_frame_count,
            "vision": result.get("vision"),
            "preflight": preflight_holder or {
                "ok": True,
                "status": "cache_reused",
                "message": "沿用相同素材、模型和策略的已验证视觉索引",
                **identity,
            },
            "cache_hit": bool(result.get("cache_hit")),
        }

    state = _load_for_write(project_dir)
    job = _automation(state)["media_index"]
    if str(job.get("job_id") or "") != str(expected_job_id):
        return state
    asset = _find(state.get("assets") or [], asset_id, "素材")
    media_state = asset.get("media_index") if isinstance(asset.get("media_index"), dict) else {}
    if result_summary["stage"] == "coarse":
        media_state.update({
            "coarse_index_path": result_summary["index_path"],
            "duration_seconds": result_summary["duration_seconds"],
            "segment_count": result_summary["segment_count"],
            "transcript_status": result_summary["transcript_status"],
        })
    elif result_summary["stage"] == "fine":
        fine = media_state.get("fine_indices") if isinstance(media_state.get("fine_indices"), list) else []
        fine.append({
            "path": result_summary["index_path"],
            "start_seconds": result_summary["start_seconds"],
            "end_seconds": result_summary["end_seconds"],
        })
        media_state["fine_indices"] = fine[-20:]
    else:
        media_state.update({
            "vision_index_path": result_summary["index_path"],
            "duration_seconds": result_summary["duration_seconds"],
            "vision_shot_count": result_summary["shot_count"],
            "vision_frame_count": result_summary["selected_frame_count"],
            "vision": result_summary["vision"],
            "vision_preflight": result_summary["preflight"],
        })
    media_state.update({"status": "completed", "job_id": expected_job_id, "stage": result_summary["stage"], "updated_at": _now()})
    asset["media_index"] = media_state
    job.update({"status": "completed", "finished_at": _now(), "result": result_summary, "error": "", "progress": {"stage": "completed", "message": "素材分析完成"}})
    label = {"coarse": "粗筛", "fine": "精筛", "vision": "画面理解"}[result_summary["stage"]]
    _activity(state, "media_index_completed", f"{asset_id} 的素材{label}已完成", asset_id=asset_id, job_id=expected_job_id)
    return _save(project_dir, state)


def mark_asset_media_index_failed(project_dir: Path, expected_job_id: str, error: object) -> dict:
    state = _load_for_write(project_dir)
    job = _automation(state)["media_index"]
    if str(job.get("job_id") or "") != str(expected_job_id):
        return state
    message = str(error)[:1200]
    job.update({"status": "failed", "finished_at": _now(), "error": message})
    asset = next((item for item in state.get("assets", []) if str(item.get("id")) == str(job.get("asset_id"))), None)
    if asset:
        asset["media_index"] = {**(asset.get("media_index") or {}), "status": "failed", "job_id": expected_job_id, "error": message}
    _activity(state, "media_index_failed", f"本地素材分析失败：{message[:160]}", asset_id=job.get("asset_id"), job_id=expected_job_id)
    return _save(project_dir, state)


def read_asset_media_index_job(project_dir: Path) -> dict:
    state = read_workbench(project_dir)
    return deepcopy(_automation(state)["media_index"])


def read_asset_media_index_batch(project_dir: Path) -> dict:
    state = read_workbench(project_dir)
    return deepcopy(_automation(state)["media_index_batch"])


def recommend_asset_media_segments(project_dir: Path, asset_id: str, query: str, limit: int = 6) -> dict:
    state = read_workbench(project_dir)
    asset, source = _media_index_source(project_dir, state, asset_id)
    media_state = asset.get("media_index") if isinstance(asset.get("media_index"), dict) else {}
    vision_relative = str(media_state.get("vision_index_path") or "")
    vision_path = (project_dir / vision_relative).resolve()
    if vision_relative:
        try:
            vision_path.relative_to(project_dir.resolve())
        except ValueError as exc:
            raise WorkbenchError("画面理解索引路径无效，请重新分析") from exc
    if vision_relative and vision_path.is_file():
        vision_index = json.loads(vision_path.read_text(encoding="utf-8"))
        if str((vision_index.get("source") or {}).get("fingerprint") or "") != media_content_fingerprint(source):
            raise WorkbenchError("视频素材在画面理解后已经变化，请重新分析")
        candidates = recommend_vision_shots(vision_index, query, limit=limit)
        for candidate in candidates:
            frame = candidate.get("representative_frame") if isinstance(candidate.get("representative_frame"), dict) else None
            if frame and frame.get("path"):
                try:
                    frame["path"] = _safe_relpath(project_dir, str(frame.get("path")))
                except WorkbenchError:
                    candidate["representative_frame"] = None
        if any(int(item.get("score") or 0) > 0 for item in candidates):
            return {
                "asset_id": asset_id,
                "query": str(query or "")[:1000],
                "candidates": candidates,
                "evidence_source": "vision_v2",
                "index_fingerprint": str(vision_index.get("signature") or ""),
                "vision": vision_index.get("vision"),
            }
    relative = str(media_state.get("coarse_index_path") or "")
    path = project_dir / relative
    if not relative or not path.is_file():
        raise WorkbenchError("该素材还没有完成粗筛索引")
    index = json.loads(path.read_text(encoding="utf-8"))
    return {
        "asset_id": asset_id,
        "query": str(query or "")[:1000],
        "candidates": recommend_coarse_segments(index, query, limit=limit),
        "transcript_status": index.get("transcript_status"),
    }


def adopt_asset_media_candidate(
    project_dir: Path,
    asset_id: str,
    shot_id: str,
    payload: dict,
) -> dict:
    """Adopt one verified V2 shot into one scene's unlocked hero draft."""
    if not isinstance(payload, dict):
        raise WorkbenchError("采用镜头候选的数据格式无效")
    scene_id = str(payload.get("scene_id") or "").strip()
    query = re.sub(r"\s+", " ", str(payload.get("query") or "")).strip()[:1000]
    if not scene_id:
        raise WorkbenchError("采用镜头候选时必须指定当前片段")
    if not query:
        raise WorkbenchError("采用镜头候选时必须保留原始查询文本")
    if not re.fullmatch(r"SHOT-\d{4}", str(shot_id or "")):
        raise WorkbenchError("要采用的视觉镜头编号无效")

    state = read_workbench(project_dir)
    scene = _find(state.get("scenes") or [], scene_id, "场景")
    asset = _find(state.get("assets") or [], asset_id, "素材")
    composition = _ensure_scene_visual_composition(scene)
    expected_revision = payload.get("expected_revision")
    if expected_revision is None:
        raise WorkbenchError("采用镜头候选时必须提供当前画面布局版本号")
    if int(_as_number(expected_revision, -1)) != int(_as_number(composition.get("revision"), 1)):
        raise WorkbenchConflict("画面布局已在其他操作中更新，请刷新页面后重新采用")

    recommendation = recommend_asset_media_segments(project_dir, asset_id, query, limit=20)
    if recommendation.get("evidence_source") != "vision_v2":
        raise WorkbenchError("当前候选不是视觉分析 2.0 的可核验证据，不能一键采用")
    candidate = next(
        (
            item for item in recommendation.get("candidates") or []
            if str(item.get("segment_id") or "") == shot_id
            and str(item.get("evidence_kind") or "") == "vision"
            and int(item.get("score") or 0) > 0
        ),
        None,
    )
    if not candidate:
        raise WorkbenchError("该镜头没有命中当前台词，系统不会伪造采用结果")

    source_start = _rounded_seconds(candidate.get("start_seconds"))
    source_end = _rounded_seconds(candidate.get("end_seconds"))
    source_duration = source_end - source_start
    if source_duration < .4 - .001:
        raise WorkbenchError("该镜头不足 0.4 秒，不能作为重点视频采用")
    scene_duration = _rounded_seconds(_scene_duration(scene))
    overlays = [deepcopy(item) for item in composition.get("overlays") or [] if isinstance(item, dict)]
    target = next((item for item in overlays if item.get("role", "hero") == "hero" and not item.get("locked")), None)
    if target is not None:
        display_start = _rounded_seconds(target.get("start_seconds"))
        current_duration = max(.4, _rounded_seconds(target.get("end_seconds")) - display_start)
        display_duration = min(source_duration, current_duration, scene_duration - display_start)
        if display_duration < .4 - .001:
            raise WorkbenchError("当前重点素材草案没有足够的显示时长，请先调整时间区间")
    else:
        occupied = sorted(
            (
                (_rounded_seconds(item.get("start_seconds")), _rounded_seconds(item.get("end_seconds")))
                for item in overlays
            ),
            key=lambda item: item[0],
        )
        gaps: list[tuple[float, float]] = []
        cursor = 0.0
        for start, end in occupied:
            if start - cursor >= .4 - .001:
                gaps.append((cursor, start))
            cursor = max(cursor, end)
        if scene_duration - cursor >= .4 - .001:
            gaps.append((cursor, scene_duration))
        if not gaps:
            raise WorkbenchError("当前片段没有可用的重点素材空档，请先解锁或调整已有草案")
        display_start, gap_end = max(gaps, key=lambda item: item[1] - item[0])
        display_duration = min(source_duration, gap_end - display_start)
        target = {
            "id": "", "role": "hero", "asset_id": asset_id,
            "locked": False,
        }
        overlays.append(target)

    canvas_width, canvas_height = _render_dimensions(project_dir, state)
    target.update({
        "asset_id": asset_id,
        "start_seconds": _rounded_seconds(display_start),
        "end_seconds": _rounded_seconds(display_start + display_duration),
        "source_in_seconds": source_start,
        "source_out_seconds": _rounded_seconds(source_start + display_duration),
        "fit": "contain",
        "muted": True,
        "playback_rate": 1.0,
        "placement": _recommended_visual_placement(asset, canvas_width, canvas_height),
        "candidate_evidence": {
            "source": "vision_v2",
            "shot_id": shot_id,
            "query": query,
            "index_fingerprint": str(recommendation.get("index_fingerprint") or "").lower(),
        },
        "locked": False,
    })
    return update_scene_visual_composition(project_dir, scene_id, {
        "version": 1,
        "expected_revision": expected_revision,
        "layout_recipe": "focus_card",
        "overlays": overlays,
        "frame_style": deepcopy(composition.get("frame_style") or {}),
    })


def _local_material_vision_indexes(project_dir: Path, state: dict) -> tuple[dict[str, dict], list[str]]:
    """Load only project-local, completed V2 indexes for the draft planner.

    This deliberately does *not* start visual analysis or talk to a model.  A
    stale/missing index is a visible preparation warning, never a reason to
    infer a semantic description from an uploaded filename.
    """
    indexes: dict[str, dict] = {}
    warnings: list[str] = []
    root = project_dir.resolve()
    for asset in state.get("assets") or []:
        if not isinstance(asset, dict) or str(asset.get("type") or "").lower() != "video":
            continue
        source_type = str(asset.get("source_type") or "")
        if source_type not in {"human_provided", "local_generated", "project_library"}:
            continue
        asset_id = str(asset.get("id") or "")
        media_state = asset.get("media_index") if isinstance(asset.get("media_index"), dict) else {}
        relative = str(media_state.get("vision_index_path") or "")
        if not relative:
            warnings.append(f"素材 {asset_id} 尚未完成视觉理解 2.0")
            continue
        try:
            path = (project_dir / relative).resolve()
            path.relative_to(root)
            source = (project_dir / str(asset.get("path") or "")).resolve()
            source.relative_to(root)
        except (OSError, ValueError):
            warnings.append(f"素材 {asset_id} 的视觉理解路径无效")
            continue
        if not path.is_file() or not source.is_file():
            warnings.append(f"素材 {asset_id} 的视觉理解文件或原视频缺失")
            continue
        try:
            index = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            warnings.append(f"素材 {asset_id} 的视觉理解索引损坏，请重新分析")
            continue
        expected = str((index.get("source") or {}).get("fingerprint") or "")
        try:
            actual = media_content_fingerprint(source)
        except MediaIndexError:
            warnings.append(f"素材 {asset_id} 无法核对视觉理解版本")
            continue
        if not expected or expected != actual:
            warnings.append(f"素材 {asset_id} 在视觉理解后已经变化，请重新分析")
            continue
        indexes[asset_id] = index
    return indexes, list(dict.fromkeys(warnings))


def _local_material_orchestration_fingerprint(value: dict) -> str:
    return _json_hash({key: item for key, item in value.items() if key not in {"fingerprint", "created_at", "updated_at", "revision"}})


def create_local_material_orchestration(project_dir: Path, payload: dict) -> dict:
    """Persist an auditable local-material draft without changing scenes."""
    if not isinstance(payload, dict):
        raise WorkbenchError("素材驱动编排请求格式无效")
    state = _load_for_write(project_dir)
    indexes, index_warnings = _local_material_vision_indexes(project_dir, state)
    try:
        draft = build_orchestration_draft(state, indexes, payload)
    except LocalMaterialOrchestrationError as exc:
        raise WorkbenchError(str(exc)) from exc
    previous = state.get("local_material_orchestration") if isinstance(state.get("local_material_orchestration"), dict) else {}
    draft["revision"] = max(1, int(_as_number(previous.get("revision"), 0)) + 1)
    draft["warnings"] = list(dict.fromkeys([*(draft.get("warnings") or []), *index_warnings]))
    draft["created_at"] = _now()
    draft["updated_at"] = draft["created_at"]
    draft["fingerprint"] = _local_material_orchestration_fingerprint(draft)
    state["local_material_orchestration"] = draft
    _decision(
        state,
        "local_material_orchestration",
        "本地素材驱动编排草案",
        f"{len(draft.get('sequences') or [])} 个可采用序列",
        "仅生成草案；不会下载、生成、覆盖或批准画面",
    )
    _activity(
        state,
        "local_material_orchestration_drafted",
        f"已生成本地素材编排草案：{len(draft.get('material_capability_map') or [])} 个能力片段，{len(draft.get('sequences') or [])} 个可采用序列",
        revision=draft["revision"],
    )
    return _save(project_dir, state)


def _planner_evidence_for_sequence(sequence: dict) -> dict:
    evidence = sequence.get("evidence") if isinstance(sequence.get("evidence"), dict) else {}
    fingerprint = str(evidence.get("index_fingerprint") or "").lower()
    shot_id = str(evidence.get("shot_id") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", fingerprint) or not re.fullmatch(r"SHOT-\d{4}", shot_id):
        raise WorkbenchError("本地素材草案缺少可核验的视觉理解证据")
    return {
        "source": "local_material_orchestration_v1",
        "shot_id": shot_id,
        "index_fingerprint": fingerprint,
        "sequence_id": str(sequence.get("sequence_id") or ""),
        "cut_policy": str(sequence.get("cut_policy") or "safe_cut"),
    }


def _adopt_local_material_focus_card(project_dir: Path, state: dict, scene: dict, sequence: dict, payload: dict) -> None:
    composition = _ensure_scene_visual_composition(scene)
    expected = payload.get("expected_composition_revision")
    if expected is None:
        raise WorkbenchError("采用本地主角窗时必须提供当前画面布局版本号")
    if int(_as_number(expected, -1)) != int(_as_number(composition.get("revision"), 1)):
        raise WorkbenchConflict("画面布局已在其他操作中更新，请刷新后重新采用本地素材草案")
    overlays = [deepcopy(item) for item in composition.get("overlays") or [] if isinstance(item, dict)]
    if any(item.get("locked") for item in overlays):
        raise WorkbenchError("当前片段存在锁定的重点素材，请先解锁或保留现有画面")
    overlays = [item for item in overlays if item.get("role", "hero") != "hero"]
    overlays.append({
        "id": "",
        "role": "hero",
        "asset_id": str(sequence["asset_id"]),
        "start_seconds": _rounded_seconds(sequence["display_start_seconds"]),
        "end_seconds": _rounded_seconds(sequence["display_end_seconds"]),
        "source_in_seconds": _rounded_seconds(sequence["source_in_seconds"]),
        "source_out_seconds": _rounded_seconds(sequence["source_out_seconds"]),
        "fit": "contain",
        "muted": True,
        "playback_rate": 1.0,
        "placement": _recommended_visual_placement(
            _find(state.get("assets") or [], str(sequence["asset_id"]), "素材"),
            *_render_dimensions(project_dir, state),
        ),
        "candidate_evidence": {
            "source": "vision_v2",
            "shot_id": _planner_evidence_for_sequence(sequence)["shot_id"],
            "query": "素材驱动编排草案",
            "index_fingerprint": _planner_evidence_for_sequence(sequence)["index_fingerprint"],
        },
        "planner_evidence": _planner_evidence_for_sequence(sequence),
        "locked": False,
    })
    validated = _validated_visual_composition(project_dir, state, scene, {
        "version": 1,
        "layout_recipe": "focus_card",
        "overlays": overlays,
        "frame_style": deepcopy(composition.get("frame_style") or {}),
    })
    validated.update({"revision": int(_as_number(composition.get("revision"), 0)) + 1, "updated_at": _now()})
    scene["visual_composition"] = validated


def _background_block_for_local_sequence(scene: dict, display_end: float) -> dict | None:
    """Use only an existing unlocked background block; never trigger a download."""
    blocks = [item for item in ((scene.get("visual_timeline") or {}).get("blocks") or []) if isinstance(item, dict)]
    if not blocks or display_end >= _scene_duration(scene) - .001:
        return None
    candidate = next((item for item in blocks if item.get("asset_id") and not item.get("locked")), None)
    if not candidate:
        return None
    return {
        "id": "VB-002",
        "start_seconds": _rounded_seconds(display_end),
        "end_seconds": _rounded_seconds(_scene_duration(scene)),
        "source_mode": str(candidate.get("source_mode") or "web_download"),
        "asset_id": str(candidate.get("asset_id")),
        "label": f"连续动作后的既有背景：{candidate.get('label') or candidate.get('asset_id')}",
        "visual_role": "supporting_background",
        "cut_policy": "interruptible",
        "sequence_id": None,
        "planner_evidence": None,
        "locked": False,
    }


def _adopt_local_material_full_bleed(project_dir: Path, state: dict, scene: dict, sequence: dict, payload: dict) -> None:
    timeline = scene.get("visual_timeline") if isinstance(scene.get("visual_timeline"), dict) else {}
    expected = payload.get("expected_timeline_revision")
    if expected is None:
        raise WorkbenchError("采用本地全屏动作时必须提供当前视觉时间线版本号")
    if int(_as_number(expected, -1)) != int(_as_number(timeline.get("revision"), 1)):
        raise WorkbenchConflict("视觉时间线已在其他操作中更新，请刷新后重新采用本地素材草案")
    old_blocks = [item for item in timeline.get("blocks") or [] if isinstance(item, dict)]
    if any(item.get("locked") for item in old_blocks):
        raise WorkbenchError("当前片段存在锁定的背景区间，不能自动改为本地全屏动作")
    display_end = _rounded_seconds(sequence["display_end_seconds"])
    local_block = {
        "id": "VB-001",
        "start_seconds": 0.0,
        "end_seconds": display_end,
        "source_mode": "human_provided",
        "asset_id": str(sequence["asset_id"]),
        "label": f"本地连续动作：{sequence.get('sequence_id')}",
        "visual_role": "local_full_bleed",
        "cut_policy": str(sequence["cut_policy"]),
        "sequence_id": str(sequence["sequence_id"]),
        "source_in_seconds": _rounded_seconds(sequence["source_in_seconds"]),
        "source_out_seconds": _rounded_seconds(sequence["source_out_seconds"]),
        "planner_evidence": _planner_evidence_for_sequence(sequence),
        "locked": False,
    }
    blocks = [local_block]
    background = _background_block_for_local_sequence(scene, display_end)
    if background:
        blocks.append(background)
    elif display_end < _scene_duration(scene) - .001:
        raise WorkbenchError("本地动作未覆盖完整片段，且没有可复用的未锁定背景；请先准备背景或改用主角窗")
    blocks = _validated_visual_timeline(state, scene, blocks)
    _commit_visual_timeline(state, scene, blocks)
    composition = _ensure_scene_visual_composition(scene)
    expected_composition = payload.get("expected_composition_revision")
    if expected_composition is None:
        raise WorkbenchError("采用本地全屏动作时必须提供当前画面布局版本号")
    if int(_as_number(expected_composition, -1)) != int(_as_number(composition.get("revision"), 1)):
        raise WorkbenchConflict("画面布局已在其他操作中更新，请刷新后重新采用本地素材草案")
    if any(item.get("locked") for item in composition.get("overlays") or []):
        raise WorkbenchError("当前片段存在锁定的重点素材，不能自动切换为本地全屏动作")
    composition.update({
        "layout_recipe": "full_bleed",
        "background": {"source": "visual_timeline", "treatment": "normal"},
        "overlays": [],
        "revision": int(_as_number(composition.get("revision"), 0)) + 1,
        "updated_at": _now(),
    })


def adopt_local_material_orchestration_scene(project_dir: Path, scene_id: str, payload: dict) -> dict:
    """Apply one accepted local-material scene plan atomically and locally."""
    if not isinstance(payload, dict):
        raise WorkbenchError("采用素材驱动编排草案的数据格式无效")
    state = _load_for_write(project_dir)
    draft = state.get("local_material_orchestration") if isinstance(state.get("local_material_orchestration"), dict) else None
    if not draft:
        raise WorkbenchError("请先生成素材驱动编排草案")
    expected_revision = payload.get("expected_orchestration_revision")
    if expected_revision is None or int(_as_number(expected_revision, -1)) != int(_as_number(draft.get("revision"), -1)):
        raise WorkbenchConflict("素材驱动编排草案已更新，请刷新后再采用")
    indexes, _warnings = _local_material_vision_indexes(project_dir, state)
    if draft.get("script_fingerprint") != local_material_script_fingerprint(state):
        raise WorkbenchConflict("脚本或场景内容已变化，请重新生成素材驱动编排草案")
    if draft.get("asset_index_fingerprint") != material_indexes_fingerprint(indexes):
        raise WorkbenchConflict("本地素材或视觉理解索引已变化，请重新生成素材驱动编排草案")
    if draft.get("fingerprint") != _local_material_orchestration_fingerprint(draft):
        raise WorkbenchConflict("素材驱动编排草案已损坏或被旧页面覆盖，请重新生成")
    try:
        _plan, sequence = find_scene_plan(draft, scene_id)
    except LocalMaterialOrchestrationError as exc:
        raise WorkbenchError(str(exc)) from exc
    scene = _find(state.get("scenes") or [], scene_id, "场景")
    _ensure_scene_visual_state(state, scene)
    role = str(sequence.get("visual_role") or "")
    if role == "local_focus_card":
        _adopt_local_material_focus_card(project_dir, state, scene, sequence, payload)
    elif role == "local_full_bleed":
        _adopt_local_material_full_bleed(project_dir, state, scene, sequence, payload)
    else:
        raise WorkbenchError("当前草案不是可采用的本地素材主画面")
    _invalidate_scene_review_preview(scene, "已采用素材驱动编排草案，请刷新本段审核预览")
    scene["review_status"] = "needs_adjustment"
    _mark_render_needs_refresh(state, f"{scene_id} 已采用素材驱动编排草案")
    for scene_plan in draft.get("scene_plans") or []:
        if str(scene_plan.get("scene_id") or "") == str(scene_id):
            scene_plan.update({"status": "adopted", "adopted_at": _now()})
    draft["status"] = "partially_adopted"
    draft["updated_at"] = _now()
    draft["fingerprint"] = _local_material_orchestration_fingerprint(draft)
    _decision(state, "local_material_orchestration", f"{scene_id} 本地素材采用", str(sequence.get("sequence_id") or ""), f"{role} / {sequence.get('cut_policy')}")
    _activity(state, "local_material_orchestration_adopted", f"已采用 {scene_id} 的本地素材草案；仅该片段需要刷新审核预览", scene_id=scene_id, sequence_id=sequence.get("sequence_id"))
    return _save(project_dir, state)

def read_asset_material_vision(project_dir: Path, asset_id: str, *, limit: int = 80) -> dict:
    state = read_workbench(project_dir)
    asset, source = _media_index_source(project_dir, state, asset_id)
    relative = str((asset.get("media_index") or {}).get("vision_index_path") or "")
    path = (project_dir / relative).resolve()
    if relative:
        try:
            path.relative_to(project_dir.resolve())
        except ValueError as exc:
            raise WorkbenchError("画面理解索引路径无效，请重新分析") from exc
    if not relative or not path.is_file():
        raise WorkbenchError("该素材还没有完成画面理解")
    try:
        index = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkbenchError("画面理解索引损坏，请保留现场后重新分析") from exc
    if str((index.get("source") or {}).get("fingerprint") or "") != media_content_fingerprint(source):
        raise WorkbenchError("视频素材在画面理解后已经变化，请重新分析")
    rows = []
    for shot in (index.get("shots") or [])[:max(1, min(200, int(limit)))]:
        frames = []
        for frame in shot.get("frames") or []:
            if not frame.get("selected_for_vision"):
                continue
            frames.append({
                "frame_id": frame.get("frame_id"),
                "time_seconds": frame.get("time_seconds"),
                "sampling_reason": frame.get("sampling_reason"),
                "path": _safe_relpath(project_dir, str(frame.get("path") or "")),
            })
        rows.append({
            "shot_id": shot.get("shot_id"),
            "start_seconds": shot.get("start_seconds"),
            "end_seconds": shot.get("end_seconds"),
            "frames": frames,
            "description": shot.get("description"),
        })
    return {
        "asset_id": asset_id,
        "status": index.get("status"),
        "signature": index.get("signature"),
        "vision": index.get("vision"),
        "shot_count": len(index.get("shots") or []),
        "shots": rows,
    }


def _read_avatar_timeline(project_dir: Path, package: dict) -> dict:
    assembly = package.get("assembly") if isinstance(package.get("assembly"), dict) else {}
    raw_path = str(assembly.get("timeline_path") or "")
    if not raw_path:
        raise WorkbenchError("数字人母版缺少真实时间线文件，请重新合成母版")
    try:
        path = (project_dir / raw_path).resolve()
        path.relative_to(project_dir.resolve())
    except (OSError, ValueError) as exc:
        raise WorkbenchError("数字人时间线路径无效") from exc
    if not path.is_file():
        raise WorkbenchError("数字人真实时间线文件不存在，请重新合成母版")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkbenchError("数字人真实时间线不是有效 JSON") from exc
    turns = value.get("turns") if isinstance(value, dict) else None
    if value.get("audio_mode") != "native_avatar_audio" or not isinstance(turns, list) or not turns:
        raise WorkbenchError("数字人母版不满足“原声音频主时间轴”合同")
    return value


def _copy_avatar_master_revision(project_dir: Path, state: dict, package: dict) -> tuple[dict, str]:
    """Copy an assembled master into an immutable, project-local asset revision.

    ``renders/avatar/avatar-dialogue-master.mp4`` is a convenient latest
    pointer and is intentionally overwritten by a new assembly.  A reviewed
    scene may not reference that mutable pointer: doing so would silently
    alter an old scene when only another avatar turn was re-imported.  Each
    application therefore gets a separate asset revision under ``assets``.
    """
    assembly = package.get("assembly") if isinstance(package.get("assembly"), dict) else {}
    raw_path = str(assembly.get("output_path") or "")
    try:
        source = (project_dir / raw_path).resolve()
        source.relative_to(project_dir.resolve())
    except (OSError, ValueError) as exc:
        raise WorkbenchError("数字人母版文件路径无效") from exc
    if not source.is_file():
        raise WorkbenchError("数字人母版文件不存在，请重新合成")

    current = next((
        asset for asset in state.get("assets", [])
        if isinstance(asset, dict)
        and (asset.get("generation") or {}).get("kind") == "avatar_dialogue_master"
    ), None)
    version_number = len((current or {}).get("versions") or []) + 1
    output = project_dir / "assets" / "video" / "avatar" / "masters" / f"avatar-master-v{version_number:03d}.mp4"
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.stem}-{uuid4().hex[:8]}{output.suffix}")
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, output)
    except OSError as exc:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise WorkbenchError(f"无法保存数字人母版版本：{exc}") from exc
    relative_path = _safe_relpath(project_dir, str(output))
    if not relative_path:
        raise WorkbenchError("无法登记数字人母版版本")
    duration = _as_number((assembly.get("summary") or {}).get("duration_seconds"))
    package_revision = int(_as_number(package.get("revision"), 0))

    if current is None:
        current = _append_asset(project_dir, state, {
            "name": "数字人原声母版",
            "type": "video",
            "source_type": "human_provided",
            "path": relative_path,
            "duration_seconds": duration or None,
            "provider": (package.get("provider") or {}).get("name") or "本地导入",
            "source_tool": "avatar_dialogue_master",
            "license": "用户导入；请确认数字人形象、声音与背景素材的使用授权",
            "generation": {
                "kind": "avatar_dialogue_master",
                "audio_mode": "native_avatar_audio",
                "package_revision": package_revision,
                "assembled_from": raw_path,
                "applied_at": _now(),
            },
        })
        return current, current["versions"][0]["id"]

    for version in current.get("versions", []):
        if version.get("status") == "current":
            version["status"] = "superseded"
    version_id = f"{current['id']}-V{version_number:03d}"
    current["versions"].append({
        "id": version_id,
        "created_at": _now(),
        "path": relative_path,
        "status": "current",
        "package_revision": package_revision,
        "sha256": _json_hash({"package_revision": package_revision, "source": raw_path}),
    })
    current["path"] = relative_path
    current["duration_seconds"] = duration or current.get("duration_seconds")
    current["generation"] = {
        **(current.get("generation") or {}),
        "kind": "avatar_dialogue_master",
        "audio_mode": "native_avatar_audio",
        "package_revision": package_revision,
        "assembled_from": raw_path,
        "applied_at": _now(),
    }
    return current, version_id


def apply_avatar_package_to_timeline(project_dir: Path, payload: dict | None = None) -> dict:
    """Apply an approved avatar master to scene timing and presentation.

    This is the handoff point between imported avatar media and the normal
    review workbench.  The measured avatar audio owns every scene duration;
    the function never time-stretches audio, lips, or a scene video.
    """
    payload = payload or {}
    state = _load_for_write(project_dir)
    if not _is_avatar_project(state):
        raise WorkbenchError("当前项目不是“数字人口播”工作流，不能应用数字人原声时间线")
    package = read_avatar_package(project_dir)
    if not package or package.get("assembly", {}).get("status") != "passed":
        raise WorkbenchError("请先完成数字人原片检查、台词核对与原声母版合成")
    timeline = _read_avatar_timeline(project_dir, package)
    turns = timeline["turns"]
    scenes = sorted(
        (scene for scene in state.get("scenes", []) if isinstance(scene, dict)),
        key=lambda scene: int(_as_number(scene.get("order"))),
    )
    if not scenes:
        raise WorkbenchError("请先完成脚本审核并生成分镜草案，再应用数字人时间线")
    sections = _script_sections(project_dir, state)
    turn_by_id = {str(turn.get("turn_id") or ""): turn for turn in turns if turn.get("turn_id")}
    scene_turn_pairs: list[tuple[dict, dict]] = []
    missing_turn_links: list[str] = []
    used_turn_ids: set[str] = set()
    for scene in scenes:
        section = sections.get(str(scene.get("script_section_id") or "")) or {}
        turn_id = str(section.get("turn_id") or "").upper()
        if not turn_id or turn_id not in turn_by_id:
            missing_turn_links.append(str(scene.get("id") or "未命名场景"))
            continue
        if turn_id in used_turn_ids:
            raise WorkbenchError(f"数字人轮次 {turn_id} 被多个场景引用；请回到脚本修正轮次编号后再应用")
        used_turn_ids.add(turn_id)
        scene_turn_pairs.append((scene, turn_by_id[turn_id]))
    if missing_turn_links or len(used_turn_ids) != len(turn_by_id):
        missing_scenes = "、".join(missing_turn_links) or "无"
        unused_turns = "、".join(sorted(set(turn_by_id) - used_turn_ids)) or "无"
        raise WorkbenchError(
            "场景与数字人轮次无法一一对应："
            f"未关联轮次的场景为 {missing_scenes}；未被场景使用的轮次为 {unused_turns}。"
            "请重新从已确认脚本生成分镜后再应用。"
        )
    treatment = str(payload.get("default_treatment") or (package.get("presentation") or {}).get("default_treatment") or "fullscreen")
    if treatment not in PRESENTER_TREATMENTS:
        raise WorkbenchError("默认数字人版式只能是全屏、左上角画中画或暂时隐藏")
    master_asset, master_version_id = _copy_avatar_master_revision(project_dir, state, package)
    duration_overrides: dict[str, float] = {}
    for scene, turn in scene_turn_pairs:
        start = _rounded_seconds(turn.get("start_seconds"))
        end = _rounded_seconds(turn.get("end_seconds"))
        if end <= start:
            raise WorkbenchError(f"数字人时间线中的 {turn.get('turn_id') or '未知片段'} 没有有效时长")
        duration_overrides[str(scene["id"])] = end - start
    timeline_update = _build_timeline_update(state, duration_overrides, reason="avatar_native_audio_applied")
    _apply_timeline_update(state, timeline_update)
    timeline_revision = int((state.get("timeline") or {}).get("revision") or 0)

    for scene, turn in scene_turn_pairs:
        presenter = _scene_presenter(scene)
        layouts = _ensure_presenter_layout_state(state)
        default_layout = layouts["default_template_id"]
        default_template = next((item for item in layouts["templates"] if item["id"] == default_layout), {})
        presenter.update({
            "treatment": treatment,
            "asset_id": master_asset["id"],
            "asset_version_id": master_version_id,
            "source_path": master_asset["path"],
            "source_start_seconds": _rounded_seconds(turn.get("start_seconds")),
            "source_end_seconds": _rounded_seconds(turn.get("end_seconds")),
            "turn_id": str(turn.get("turn_id") or ""),
            "audio_mode": "native_avatar_audio",
            "timeline_revision": timeline_revision,
            "layout_template_id": default_layout,
            "layout_override": None,
            "crop_bottom": _normalized_presenter_crop_bottom(default_template.get("crop_bottom")),
            "shape": _normalized_presenter_shape(default_template.get("shape"), "rounded"),
            "face_crop": None,
        })
        if treatment == "fullscreen":
            scene["source_strategy"] = "avatar_only"
        elif scene.get("source_strategy") in {"undecided", "avatar_only"}:
            scene["source_strategy"] = "web_download"
        scene["keyframe_review"] = None
        scene["keyframe_generation"] = None
        _invalidate_scene_review_preview(scene, "数字人原声时间线已更新，请刷新本段审核预览")
        _append_selected_usage(state, scene["id"], master_asset["id"], "presenter")
        narration = scene.get("narration") if isinstance(scene.get("narration"), dict) else _scene_narration_default()
        narration["status"] = "native_avatar_audio"
        narration["text"] = str(turn.get("text") or scene.get("description") or "")
        narration["audio_mode"] = "native_avatar_audio"
        scene["narration"] = narration

    automation = _automation(state)
    assembly = package["assembly"]
    automation["audio_mode"] = "native_avatar_audio"
    subtitle_override = _write_avatar_review_subtitles(project_dir, state)
    automation["narration_generation"] = {
        "status": "completed",
        "stage": "native_avatar_ready",
        "completed_scenes": len(scene_turn_pairs),
        "total_scenes": len(scene_turn_pairs),
        "audio_path": master_asset["path"],
        "subtitle_path": _safe_relpath(project_dir, str(subtitle_override)),
        "timeline_update": timeline_update,
        "error": "",
        "timing_basis": "native_avatar_audio",
    }
    automation["render"] = {"status": "awaiting_assets", "runtime": "ffmpeg", "output_path": None, "error": ""}
    automation["status"] = "avatar_timeline_ready"
    state["avatar"] = {
        "status": "timeline_applied",
        "default_treatment": treatment,
        "master_asset_id": master_asset["id"],
        "master_asset_version_id": master_version_id,
        "package_revision": int(_as_number(package.get("revision"))),
        "audio_mode": "native_avatar_audio",
        "applied_at": _now(),
        "turns": {
            str(turn.get("turn_id") or scene["id"]): {
                "scene_id": scene["id"],
                "source_start_seconds": _rounded_seconds(turn.get("start_seconds")),
                "source_end_seconds": _rounded_seconds(turn.get("end_seconds")),
                "duration_seconds": _rounded_seconds(_as_number(turn.get("end_seconds")) - _as_number(turn.get("start_seconds"))),
            }
            for scene, turn in scene_turn_pairs
        },
    }
    _decision(state, "timeline_authority", "数字人口播项目时间轴", "数字人原声音频", "母版实际时长已写入场景、字幕与片段边界；系统未拉伸语音或嘴型。")
    _decision(state, "presenter_treatment", "项目默认数字人版式", treatment)
    _activity(state, "avatar_timeline_applied", "数字人原声母版已应用为真实时间线；现在可逐场选择全屏或左上角解说，并审核合成关键帧", asset_id=master_asset["id"])
    return _save(project_dir, state)


def generate_avatar_scene_keyframes(project_dir: Path, scene_id: str) -> dict:
    """Build actual, local first/climax review frames for an avatar layout.

    Unlike generic stock-frame extraction, a PiP review frame must show the
    resolved layout: the selected main visual and the avatar in its final
    left-top placement.  This is intentionally a local FFmpeg task, with no
    provider cost and no mutable edit of the final video.
    """
    state = _load_for_write(project_dir)
    if not _is_avatar_project(state):
        raise WorkbenchError("只有数字人口播项目可以生成数字人合成审核帧")
    avatar_state = state.get("avatar") if isinstance(state.get("avatar"), dict) else {}
    if avatar_state.get("status") != "timeline_applied":
        raise WorkbenchError("请先在“数字人素材”中应用原声母版为真实时间线")
    scene = _find(state["scenes"], scene_id, "场景")
    presenter = _scene_presenter(scene)
    if presenter.get("treatment") == "hidden":
        raise WorkbenchError("当前场景已隐藏数字人；请改为全屏或左上角画中画后再生成合成审核帧")
    ffmpeg = _ffmpeg_available()
    if not ffmpeg:
        raise WorkbenchError("本机未发现 FFmpeg，无法生成数字人合成审核帧")
    avatar_source, avatar_start, avatar_end = _avatar_source_for_scene(project_dir, scene)
    scene_start = _rounded_seconds(scene.get("start_seconds"))
    scene_duration = _scene_duration(scene)
    width, height = _render_dimensions(project_dir, state)
    anchors = [
        (kind, next((item for item in scene.get("anchors", []) if item.get("kind") == kind), None))
        for kind in ("first_frame", "climax_frame")
    ]
    if any(anchor is None for _, anchor in anchors):
        raise WorkbenchError("当前场景缺少首帧或高潮帧审核锚点")
    needs_background = presenter.get("treatment") in {"pip_top_left", "custom"}
    background = _selected_visual_asset(state, scene_id) if needs_background else None
    if needs_background and (not background or not background.get("path")):
        raise WorkbenchError("左上角数字人模式需要先为当前场景选择主体画面")
    review_id = _numbered("KFR-", state.setdefault("keyframe_reviews", []), "id")
    timeline: list[dict] = []
    generated_assets: list[str] = []
    geometry = _avatar_pip_geometry(project_dir, state, scene, avatar_source) if background else None
    for index, (kind, anchor) in enumerate(anchors, 1):
        absolute_time = min(max(_as_number((anchor or {}).get("time_seconds"), scene_start), scene_start), scene_start + scene_duration)
        relative_time = min(max(0.0, absolute_time - scene_start), max(0.0, scene_duration - 0.04))
        avatar_time = min(max(avatar_start, avatar_start + relative_time), max(avatar_start, avatar_end - 0.04))
        output = project_dir / "assets" / "images" / "avatar-review" / f"{scene_id}-{kind}-{uuid4().hex[:8]}.jpg"
        output.parent.mkdir(parents=True, exist_ok=True)
        if background:
            background_path = project_dir / str(background["path"])
            if not background_path.is_file():
                raise WorkbenchError("当前场景的主体画面文件不存在")
            background_is_image = str(background.get("type") or "").lower() == "image"
            input_args = [ffmpeg, "-y"]
            if background_is_image:
                input_args.extend(["-loop", "1", "-i", str(background_path)])
            else:
                input_args.extend(["-ss", f"{relative_time:.6f}", "-i", str(background_path)])
            input_args.extend(["-ss", f"{avatar_time:.6f}", "-i", str(avatar_source)])
            base_filters = f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},setsar=1"
            overlay_filters = _avatar_pip_filter(geometry)
            filter_graph = f"[0:v]{base_filters}[bg];[1:v]{overlay_filters}[presenter];[bg][presenter]overlay={geometry['x']}:{geometry['y']}"
            command = [*input_args, "-filter_complex", filter_graph, "-frames:v", "1", "-q:v", "2", str(output)]
        else:
            filters = f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},setsar=1"
            command = [
                ffmpeg, "-y", "-ss", f"{avatar_time:.6f}", "-i", str(avatar_source),
                "-vf", filters, "-frames:v", "1", "-q:v", "2", str(output),
            ]
        ok, detail = _run_media(command)
        if not ok or not output.is_file():
            raise WorkbenchError(f"提取 {kind} 数字人合成审核帧失败：{detail}")
        asset = _append_asset(project_dir, state, {
            "name": f"{scene.get('title') or scene_id} · 数字人{'首帧' if kind == 'first_frame' else '高潮帧'}审核图",
            "type": "image", "source_type": "local_generated", "path": str(output),
            "resolution": f"{width}x{height}", "provider": "本地 FFmpeg", "source_tool": "avatar_scene_keyframe",
            "license": "由当前已登记数字人母版和主体画面生成，仅用于项目审核",
            "generation": {
                "kind": "avatar_scene_keyframe", "scene_id": scene_id, "anchor_kind": kind,
                "presenter_treatment": presenter.get("treatment"), "avatar_source_path": presenter.get("source_path"),
                "background_asset_id": (background or {}).get("id"), "generated_at": _now(),
            },
        })
        generated_assets.append(asset["id"])
        next_relative = scene_duration if index == len(anchors) else max(relative_time + .1, scene_duration * .66)
        timeline.append({
            "id": f"{review_id}-{index:02d}", "anchor_kind": kind,
            "label": "首帧" if kind == "first_frame" else "高潮帧",
            "time_seconds": round(absolute_time, 3), "relative_start_seconds": round(relative_time, 3),
            "relative_end_seconds": round(min(scene_duration, next_relative), 3),
            "caption_text": str((scene.get("narration") or {}).get("text") or scene.get("description") or "").strip(),
            "visual_note": f"数字人版式：{'全屏主体' if presenter.get('treatment') == 'fullscreen' else '左上角解说员'}",
            "asset_id": asset["id"], "status": "pending", "source": "avatar_scene_composite",
        })
        # Store the resolved template name instead of a hard-coded “top-left”
        # label so the review artifact remains meaningful after a custom edit.
        timeline[-1]["visual_note"] = "数字人版式：" + (
            "全屏主体" if presenter.get("treatment") == "fullscreen" else _presenter_layout(state, presenter)["template_name"]
        )
    review = {
        "id": review_id, "status": "generated", "timeline": timeline,
        "generation": {
            "provider": "local", "tool": "ffmpeg", "kind": "avatar_scene_composite",
            "treatment": presenter.get("treatment"), "layout": _presenter_layout(state, presenter),
            "count": len(timeline), "generated_at": _now(),
        },
        "review_note": "",
    }
    review["hyperframes"] = _build_hyperframes_review(project_dir, state, scene, timeline)
    review["artifact_path"] = _write_keyframe_review_artifact(project_dir, scene, review)
    scene["keyframe_review"] = review
    scene["keyframe_generation"] = {
        "status": "completed", "finished_at": _now(), "provider": "local", "tool": "ffmpeg",
        "expected_count": len(timeline), "completed_count": len(timeline), "review_id": review_id, "error": "",
    }
    state["keyframe_reviews"].append({"id": review_id, "scene_id": scene_id, "status": review["status"], "artifact_path": review["artifact_path"], "created_at": _now()})
    _decision(state, "avatar_keyframe_review", f"{scene_id} 数字人合成首帧与高潮帧", presenter.get("treatment") or "fullscreen")
    _activity(state, "avatar_keyframe_review", f"{scene_id} 的数字人合成审核帧已生成；请逐张确认画面和字幕", scene_id=scene_id, asset_ids=generated_assets)
    return _save(project_dir, state)


def _safe_openai_error(error: object) -> str:
    """Never let a configured credential be reflected into a browser error."""
    message = str(error or "OpenAI 生图失败")
    key = os.environ.get("OPENAI_API_KEY")
    if key:
        message = message.replace(key, "[已隐藏]")
    return message[:1200]


def generate_openai_images(project_dir: Path, payload: dict) -> dict:
    """Generate project-local stills, then register each as a stable S-xxx asset.

    The browser must set ``confirmed`` after showing the provider/model/cost
    notice.  Generated material is intentionally not auto-assigned: a human
    still chooses where it becomes a U-xxx usage record.
    """
    if payload.get("confirmed") is not True:
        raise WorkbenchError("请先确认本次 AI 生图的模型、数量与可能产生的费用")
    prompt = str(payload.get("prompt") or "").strip()
    if not prompt:
        raise WorkbenchError("请填写用于生成图片的导演提示词")
    if len(prompt) > 6000:
        raise WorkbenchError("导演提示词不能超过 6000 个字符")

    model = str(payload.get("model") or "gpt-image-2")
    if model != "gpt-image-2":
        raise WorkbenchError("当前工作台仅支持 gpt-image-2；请在中转站开通该模型后再使用")
    size = str(payload.get("size") or "1536x1024")
    if size not in {"1024x1024", "1536x1024", "1024x1536", "auto"}:
        raise WorkbenchError("不支持的图片尺寸")
    quality = str(payload.get("quality") or "medium")
    if quality not in {"low", "medium", "high", "auto"}:
        raise WorkbenchError("不支持的图片质量档位")
    try:
        quantity = int(payload.get("n") or 1)
    except (TypeError, ValueError) as exc:
        raise WorkbenchError("生成数量必须是 1 到 4") from exc
    if quantity not in {1, 2, 3, 4}:
        raise WorkbenchError("生成数量必须是 1 到 4")

    output_dir = project_dir / "assets" / "images" / "openai"
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    output_path = output_dir / f"gpt-image-{stamp}-{uuid4().hex[:8]}.png"
    tool = OpenAIImage()
    inputs = {
        "prompt": prompt,
        "model": model,
        "size": size,
        "quality": quality,
        "output_format": "png",
        "n": quantity,
        "output_path": str(output_path),
    }
    result = tool.execute(inputs)
    if not result.success:
        raise WorkbenchError(_safe_openai_error(result.error))

    state = _load_for_write(project_dir)
    created_assets: list[str] = []
    estimated_cost = tool.estimate_cost(inputs)
    for index, output in enumerate(result.artifacts, 1):
        path = _safe_relpath(project_dir, output)
        if not path or not (project_dir / path).is_file():
            raise WorkbenchError("生图服务未返回可登记的项目内图片文件")
        asset = _append_asset(project_dir, state, {
            "name": str(payload.get("name") or f"AI 图片 {stamp}").strip()[:140] + (f" #{index}" if quantity > 1 else ""),
            "type": "image",
            "source_type": "ai_generated",
            "path": path,
            "resolution": size,
            "provider": "OpenAI",
            "source_tool": "openai_image",
            "license": "AI 生成；请按项目发布规范复核",
            "generation": {
                "provider": "openai",
                "tool": "openai_image",
                "model": model,
                "prompt": prompt,
                "size": size,
                "quality": quality,
                "batch_size": quantity,
                "estimated_cost_usd": round(estimated_cost, 4),
                "generated_at": _now(),
            },
        })
        created_assets.append(asset["id"])
    _decision(
        state,
        "image_provider_selection",
        "场景图片生成服务",
        "openai / gpt-image-2",
        f"已生成 {len(created_assets)} 张图片；质量 {quality}，尺寸 {size}。",
    )
    _activity(state, "image_generation", f"OpenAI 已生成并登记 {', '.join(created_assets)}", asset_ids=created_assets)
    return _save(project_dir, state)


def assign_usage(project_dir: Path, payload: dict) -> dict:
    state = _load_for_write(project_dir)
    asset_id, scene_id = str(payload.get("asset_id") or ""), str(payload.get("scene_id") or "")
    _find(state["assets"], asset_id, "素材")
    scene = _find(state["scenes"], scene_id, "场景")
    selected_asset = next((item for item in state["assets"] if item.get("id") == asset_id), None)
    for usage in state["usages"]:
        if usage.get("scene_id") == scene_id and usage.get("role") == (payload.get("role") or "visual"):
            usage["selected"] = False
    usage = {
        "id": _numbered("U-", state["usages"], "id"), "asset_id": asset_id, "scene_id": scene_id,
        "role": str(payload.get("role") or "visual"), "selected": True,
        "transform": payload.get("transform") if isinstance(payload.get("transform"), dict) else {"crop": None, "scale": 1, "speed": 1},
        "created_at": _now(),
    }
    state["usages"].append(usage)
    if usage["role"] == "visual":
        if selected_asset:
            _set_single_visual_block(state, scene, selected_asset)
        _invalidate_scene_review_preview(scene, "主体素材已更换，请刷新本段审核预览")
        _mark_render_needs_refresh(state, f"{scene_id} 的主体素材已更换")
    _decision(state, "asset_usage", f"{scene_id} {usage['role']}", asset_id, f"使用编号 {usage['id']}")
    _activity(state, "usage", f"场景 {scene_id} 已选用 {asset_id}（{usage['id']}）", scene_id=scene_id, asset_id=asset_id)
    return _save(project_dir, state)


def freeze_segment(project_dir: Path, segment_id: str, frozen: bool = True) -> dict:
    state = _load_for_write(project_dir)
    segment = _find(state["segments"], segment_id, "渲染片段")
    if frozen:
        snapshot = {
            "at": _now(), "current_version_id": segment.get("current_version_id"),
            "content_hash": _json_hash({
                "segment_id": segment.get("id"), "current_version_id": segment.get("current_version_id"),
                "usages": [u for u in state["usages"] if u.get("scene_id") in segment.get("scene_ids", []) and u.get("selected")],
            }),
            "input_hash": _json_hash({
                "segment": {key: segment.get(key) for key in ("id", "scene_ids", "start_frame", "end_frame", "audio_start_sample", "audio_end_sample")},
                "usages": [u for u in state["usages"] if u.get("scene_id") in segment.get("scene_ids", []) and u.get("selected")],
            }),
            "timeline_snapshot": {key: segment.get(key) for key in ("start_seconds", "end_seconds", "start_frame", "end_frame")},
            "content_locked": True,
            "timeline_shift_seconds": 0.0,
        }
        segment["freeze"] = snapshot
        segment["state"] = "frozen"
        segment["boundary_contract"]["left"]["locked"] = True
        segment["boundary_contract"]["right"]["locked"] = True
        segment["boundary_contract"]["content_locked"] = True
        segment["boundary_contract"]["timecode_locked"] = False
        segment["boundary_contract"]["mode"] = "content_freeze"
        _activity(state, "freeze", f"已冻结 {segment_id} 的内容与当前版本；波纹时间线可移动其绝对位置", segment_id=segment_id)
    else:
        blocking = [p for p in state.get("patches", []) if p.get("segment_id") == segment_id and p.get("status") in {"planned", "ready_to_render", "rendering", "rendered"}]
        if blocking:
            raise WorkbenchError("该片段有未收口的热插拔任务，不能解除冻结")
        segment["freeze"] = None
        segment["state"] = "editable"
        segment["boundary_contract"]["left"]["locked"] = False
        segment["boundary_contract"]["right"]["locked"] = False
        segment["boundary_contract"]["content_locked"] = False
        segment["boundary_contract"]["timecode_locked"] = False
        _activity(state, "unfreeze", f"已解除 {segment_id} 的冻结", segment_id=segment_id)
    return _save(project_dir, state)


def _patch_overlap(segment: dict, existing: dict) -> bool:
    return not (
        segment["end_frame"] <= existing.get("start_frame", -1)
        or segment["start_frame"] >= existing.get("end_frame", 10**18)
    )


def prepare_patch(project_dir: Path, payload: dict) -> dict:
    state = _load_for_write(project_dir)
    segment_id = str(payload.get("segment_id") or "")
    segment = _find(state["segments"], segment_id, "渲染片段")
    instruction = str(payload.get("instruction") or "").strip()
    if not instruction:
        raise WorkbenchError("请写明局部调整指令")
    mode = str(payload.get("mode") or "strict_freeze")
    if mode not in PATCH_MODES:
        raise WorkbenchError("未知的热插拔模式")
    candidate_asset_id = payload.get("candidate_asset_id") or None
    candidate_audio_asset_id = payload.get("candidate_audio_asset_id") or None
    change_scope = str(payload.get("change_scope") or ("audio" if candidate_audio_asset_id else "visual"))
    if change_scope not in {"visual", "audio", "mixed"}:
        raise WorkbenchError("局部调整范围必须是画面、配音或混合")
    if mode == "ripple_timeline" and change_scope != "audio":
        raise WorkbenchError("波纹时间线当前仅用于自然配音替换；画面替换应保持原片段时长")
    if candidate_asset_id:
        _find(state["assets"], str(candidate_asset_id), "候选素材")
    if candidate_audio_asset_id:
        audio_asset = _find(state["assets"], str(candidate_audio_asset_id), "候选配音素材")
        if audio_asset.get("type") != "audio":
            raise WorkbenchError("配音局部调整必须指定音频素材")
    if change_scope == "visual" and not candidate_asset_id:
        has_candidate = False
    elif change_scope == "audio" and not candidate_audio_asset_id:
        has_candidate = False
    else:
        has_candidate = bool(candidate_asset_id or candidate_audio_asset_id)
    for old in state.get("patches", []):
        if old.get("status") in {"draft", "planned", "blocked", "ready_to_render", "rendering", "rendered"} and _patch_overlap(segment, old):
            raise WorkbenchError(f"与未收口热插拔 {old['id']} 的帧范围重叠；请先合并、回滚或取消它")
    if not segment.get("freeze"):
        # The target must be snapshotted before a patch can assert that A/C did
        # not move.  This is intentionally automatic only for the selected B.
        freeze_segment(project_dir, segment_id, True)
        state = _load_for_write(project_dir)
        segment = _find(state["segments"], segment_id, "渲染片段")

    patch_id = _numbered("P-", state["patches"], "id")
    start_frame, end_frame = int(segment["start_frame"]), int(segment["end_frame"])
    dependencies = {
        "frozen_before": [s["id"] for s in state["segments"] if s["order"] < segment["order"] and s.get("state") == "frozen"],
        "frozen_after": [s["id"] for s in state["segments"] if s["order"] > segment["order"] and s.get("state") == "frozen"],
        "segment_snapshot": deepcopy(segment["freeze"]),
    }
    scene_id = str(segment["scene_ids"][0]) if segment.get("scene_ids") else ""
    target_duration = _as_number(payload.get("target_duration_seconds"))
    if change_scope == "audio" and target_duration <= 0 and candidate_audio_asset_id:
        target_duration = _as_number(_find(state["assets"], str(candidate_audio_asset_id), "候选配音素材").get("duration_seconds"))
    if target_duration <= 0:
        target_duration = _scene_duration(_find(state["scenes"], scene_id, "场景"))
    timeline_impact = (
        _build_timeline_update(state, {scene_id: target_duration}, reason="scene_narration_ripple_preview")
        if mode == "ripple_timeline" else None
    )
    cache_key = _json_hash({
        "segment": segment_id, "boundaries": [start_frame, end_frame, segment["audio_start_sample"], segment["audio_end_sample"]],
        "instruction": instruction, "candidate_asset_id": candidate_asset_id, "candidate_audio_asset_id": candidate_audio_asset_id,
        "change_scope": change_scope, "mode": mode, "target_duration_seconds": _rounded_seconds(target_duration),
        "snapshot": dependencies["segment_snapshot"],
    })
    patch = {
        "id": patch_id, "segment_id": segment_id, "scene_ids": list(segment["scene_ids"]),
        "start_frame": start_frame, "end_frame": end_frame,
        "audio_start_sample": segment["audio_start_sample"], "audio_end_sample": segment["audio_end_sample"],
        "mode": mode, "instruction": instruction, "candidate_asset_id": candidate_asset_id,
        "candidate_audio_asset_id": candidate_audio_asset_id, "change_scope": change_scope,
        "narration_version_id": payload.get("narration_version_id") or None, "source": str(payload.get("source") or "manual"),
        "target_duration_seconds": _rounded_seconds(target_duration), "timeline_impact": timeline_impact,
        "status": "ready_to_render" if has_candidate else "planned",
        "dependencies": dependencies, "cache_key": cache_key, "created_at": _now(),
        "candidate_version_id": None, "render_plan": {
            "A": "前序冻结片段：不参与渲染", "B": f"{segment_id}：仅此片段参与渲染", "C": "后序冻结片段：不参与渲染",
            "requires": "候选素材尚未指定" if not has_candidate else ("候选配音已指定" if change_scope == "audio" else "候选素材已指定"),
            "strict_guarantee": (
                "A/C 内容保持不重编码；B 按自然配音时长重新合成，后续片段仅改变全局开始时间。"
                if mode == "ripple_timeline" else
                "仅当复用段通过编码/关键帧预检时允许无重编码拼接" if mode == "strict_freeze" else "允许为转场重编码相邻边界，必须在报告中标明"
            ),
        },
    }
    state["patches"].append(patch)
    _activity(state, "patch_plan", f"已创建 {patch_id}：仅调整 {segment_id}", patch_id=patch_id, segment_id=segment_id)
    _decision(state, "hot_swap", f"{segment_id} 局部调整", mode, instruction)
    _persist_patch_artifact(project_dir, patch)
    return _save(project_dir, state)


def _persist_patch_artifact(project_dir: Path, patch: dict) -> None:
    path = project_dir / PATCH_DIRECTORY / f"{patch['id']}.json"
    _atomic_write(path, patch)


def _candidate_asset(state: dict, patch: dict) -> dict | None:
    asset_id = patch.get("candidate_asset_id")
    return next((a for a in state["assets"] if a.get("id") == asset_id), None)


def _candidate_audio_asset(state: dict, patch: dict) -> dict | None:
    asset_id = patch.get("candidate_audio_asset_id")
    return next((a for a in state["assets"] if a.get("id") == asset_id), None)


def _ffmpeg_pair() -> tuple[str, str] | None:
    """Use VideoCompose's single read-only runtime-pair resolver."""
    resolver = getattr(video_compose_runtime, "_discover_ffmpeg_pair", None)
    if not callable(resolver):
        # Transitional compatibility for this isolated worktree.  The target
        # integration branch exposes _discover_ffmpeg_pair; until it is
        # merged, reuse VideoCompose's existing resolver and still require its
        # sibling probe instead of reviving a second discovery implementation.
        legacy_resolver = getattr(video_compose_runtime, "_ensure_ffmpeg_on_path", None)
        ffmpeg = legacy_resolver() if callable(legacy_resolver) else None
        if ffmpeg:
            binary = Path(ffmpeg)
            ffprobe = binary.with_name("ffprobe.exe" if binary.suffix.lower() == ".exe" else "ffprobe")
            if binary.is_file() and ffprobe.is_file():
                return str(binary.resolve()), str(ffprobe.resolve())
        # This compatibility branch exists only because the isolated A
        # worktree predates VideoCompose._discover_ffmpeg_pair.  It is not
        # reached after integration.  Keep it pair-only and find_spec-only so
        # it cannot mix versions, import static_ffmpeg, or trigger downloads.
        try:
            spec = importlib.util.find_spec("static_ffmpeg")
        except (ImportError, ModuleNotFoundError, ValueError):
            spec = None
        platform_dir = "win32" if os.name == "nt" else ("darwin" if sys.platform == "darwin" else "linux")
        if spec is not None:
            for location in spec.submodule_search_locations or ():
                package_root = Path(location)
                for directory in (package_root / "bin" / platform_dir, package_root / "bin"):
                    suffix = ".exe" if os.name == "nt" else ""
                    binary = directory / f"ffmpeg{suffix}"
                    ffprobe = directory / f"ffprobe{suffix}"
                    if binary.is_file() and ffprobe.is_file():
                        return str(binary.resolve()), str(ffprobe.resolve())
        return None
    pair = resolver()
    if not pair or len(pair) != 2:
        return None
    ffmpeg, ffprobe = Path(pair[0]), Path(pair[1])
    if not ffmpeg.is_file() or not ffprobe.is_file() or ffmpeg.parent.resolve() != ffprobe.parent.resolve():
        return None
    return str(ffmpeg.resolve()), str(ffprobe.resolve())


def _ffmpeg_available() -> str | None:
    pair = _ffmpeg_pair()
    return pair[0] if pair else None


def _ffprobe_available(ffmpeg: str | None = None) -> str | None:
    if ffmpeg:
        binary = Path(ffmpeg)
        sibling = binary.with_name("ffprobe.exe" if binary.suffix.lower() == ".exe" else "ffprobe")
        if sibling.is_file():
            return str(sibling.resolve())
        return None
    pair = _ffmpeg_pair()
    return pair[1] if pair else None


def _run_media(command: list[str]) -> tuple[bool, str]:
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20 * 60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    if completed.returncode == 0:
        return True, completed.stdout[-2000:]
    return False, (completed.stderr or completed.stdout or "媒体命令失败")[-3000:]


def _probe_video(path: Path, ffmpeg: str | None = None) -> dict | None:
    probe = _ffprobe_available(ffmpeg)
    if not probe:
        return None
    ok, output = _run_media([
        probe, "-v", "error", "-show_entries",
        "format=duration:stream=codec_type,codec_name,width,height,pix_fmt,r_frame_rate,sample_rate,channels",
        "-of", "json", str(path),
    ])
    if not ok:
        return None
    try:
        return json.loads(output)
    except json.JSONDecodeError:
        return None


def _probe_duration_seconds(path: Path, ffmpeg: str | None = None, fallback: float = 0.0) -> float:
    """Read a real media duration instead of trusting a planned scene slot."""
    probe = _probe_video(path, ffmpeg) or {}
    value = _as_number((probe.get("format") or {}).get("duration"), fallback)
    return _rounded_seconds(value, fallback)


def _selected_visual_asset(state: dict, scene_id: str) -> dict | None:
    candidates = [
        item for item in state.get("usages", [])
        if item.get("scene_id") == scene_id
        and item.get("role") in {"visual", "image", "video"}
        and item.get("selected")
    ]
    usage = next((item for item in reversed(candidates) if item.get("role") == "visual"), None)
    usage = usage or (candidates[-1] if candidates else None)
    if not usage:
        return None
    return next((asset for asset in state.get("assets", []) if asset.get("id") == usage.get("asset_id")), None)


def _visual_fit_plan(project_dir: Path, state: dict, scene: dict, asset: dict | None) -> dict:
    """Make the visual-duration decision explicit and auditable.

    We trim surplus stock footage.  A material shortage is reported rather
    than hidden behind a long cloned last frame; the caller can trigger the
    existing scene-level Pexels refresh for a better clip.
    """
    target = _scene_duration(scene)
    source_duration = _as_number((asset or {}).get("duration_seconds"))
    source_path = (project_dir / str((asset or {}).get("path") or "")) if asset and asset.get("path") else None
    if source_duration <= 0 and source_path and source_path.is_file():
        source_duration = _probe_duration_seconds(source_path, _ffmpeg_available(), 0)
    source_duration = _rounded_seconds(source_duration)
    if asset and asset.get("type") == "image":
        strategy = "generated_motion"
    elif source_duration >= target:
        strategy = "trim"
    elif source_duration >= max(0.0, target - 0.35):
        strategy = "brief_hold"
    else:
        strategy = "needs_replacement"
    return {
        "target_duration_seconds": _rounded_seconds(target),
        "source_asset_id": (asset or {}).get("id"),
        "source_duration_seconds": source_duration or None,
        "strategy": strategy,
        "shortfall_seconds": _rounded_seconds(max(0.0, target - source_duration)) if source_duration else _rounded_seconds(target),
        "updated_at": _now(),
    }


def _refresh_visual_timing_status(project_dir: Path, state: dict) -> dict:
    """Re-evaluate selected visuals after a natural-narration timing change.

    This does not replace a reviewer-approved picture automatically.  It only
    records whether that exact selected source can still cover its now-real
    scene duration, allowing the UI to direct the user to the one-click asset
    refresh before a final render is attempted.
    """
    checked_scene_ids: list[str] = []
    replacement_scene_ids: list[str] = []
    for scene in state.get("scenes", []):
        if not isinstance(scene, dict):
            continue
        asset = _selected_visual_asset(state, str(scene.get("id") or ""))
        if not asset:
            continue
        checked_scene_ids.append(str(scene.get("id") or ""))
        fit = _visual_fit_plan(project_dir, state, scene, asset)
        scene["visual_fit"] = fit
        if fit["strategy"] == "needs_replacement":
            replacement_scene_ids.append(str(scene.get("id") or ""))
    return {
        "checked_scene_ids": checked_scene_ids,
        "replacement_scene_ids": replacement_scene_ids,
        "checked_at": _now(),
    }


def _render_source(project_dir: Path) -> Path | None:
    renders = project_dir / "renders"
    if not renders.is_dir():
        return None
    preferred = renders / "final.mp4"
    if preferred.is_file():
        return preferred
    candidates = [path for path in renders.glob("*.mp4") if path.is_file()]
    if not candidates:
        return None
    final_named = [path for path in candidates if "final" in path.name.lower()]
    return max(final_named or candidates, key=lambda path: path.stat().st_size)


def _relative(project_dir: Path, path: Path) -> str:
    return path.resolve().relative_to(project_dir.resolve()).as_posix()


def _current_artifact(project_dir: Path, segment: dict) -> Path | None:
    version_id = segment.get("current_version_id")
    version = next((item for item in segment.get("versions", []) if item.get("id") == version_id), None)
    raw_path = (version or {}).get("artifact_path")
    if not raw_path:
        return None
    try:
        path = (project_dir / raw_path).resolve()
        path.relative_to(project_dir.resolve())
    except (OSError, ValueError):
        return None
    return path if path.is_file() else None


def build_baseline_cache(project_dir: Path) -> dict:
    """Split an existing final render into normalised, independently reusable segments.

    This is an explicit one-time baseline build.  It is the prerequisite that
    makes later strict hot-swaps honest: A and C are then pre-rendered files
    and can be stream-copied byte-for-byte while only B is newly encoded.
    """
    state = _load_for_write(project_dir)
    ffmpeg = _ffmpeg_available()
    source = _render_source(project_dir)
    if not ffmpeg:
        raise WorkbenchError("未发现 ffmpeg，无法建立片段缓存")
    if not source:
        raise WorkbenchError("未找到可作为基线的成片 MP4；请先完成一次全片渲染")
    info = _probe_video(source, ffmpeg)
    video_stream = next((stream for stream in (info or {}).get("streams", []) if stream.get("codec_type") == "video"), None)
    audio_stream = next((stream for stream in (info or {}).get("streams", []) if stream.get("codec_type") == "audio"), None)
    if not video_stream or not audio_stream:
        raise WorkbenchError("基线成片必须同时包含视频与音频，才能建立可缝合片段缓存")
    width, height = int(video_stream.get("width") or 0), int(video_stream.get("height") or 0)
    if width <= 0 or height <= 0:
        raise WorkbenchError("无法读取基线成片的画面规格")
    fps = state["settings"].get("frame_rate") or 30
    sample_rate = state["settings"].get("sample_rate") or 48000
    for segment in state["segments"]:
        current = _current_artifact(project_dir, segment)
        if current:
            continue
        version_id = segment["current_version_id"]
        output = project_dir / SEGMENT_DIRECTORY / segment["id"] / version_id / "segment.mp4"
        output.parent.mkdir(parents=True, exist_ok=True)
        duration = max(0.04, _as_number(segment["end_seconds"]) - _as_number(segment["start_seconds"]))
        ok, detail = _run_media([
            ffmpeg, "-y", "-ss", str(segment["start_seconds"]), "-i", str(source), "-t", str(duration),
            "-map", "0:v:0", "-map", "0:a:0", "-vf", f"fps={fps},scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1",
            "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-ar", str(sample_rate), "-movflags", "+faststart", str(output),
        ])
        if not ok:
            raise WorkbenchError(f"建立 {segment['id']} 片段缓存失败：{detail}")
        version = next(item for item in segment["versions"] if item["id"] == version_id)
        version["artifact_path"] = _relative(project_dir, output)
        version["baseline_source"] = _relative(project_dir, source)
        version["baseline_created_at"] = _now()
    state["settings"]["render_profile"] = {"width": width, "height": height, "frame_rate": fps, "sample_rate": sample_rate}
    _activity(state, "baseline_cache", "已建立可复用片段缓存；后续严格热插拔仅重编码目标 B", source=_relative(project_dir, source))
    return _save(project_dir, state)


def _normalise_candidate_segment(project_dir: Path, state: dict, patch: dict, candidate: dict, segment: dict, ffmpeg: str) -> tuple[Path | None, str]:
    baseline = _current_artifact(project_dir, segment)
    candidate_path = project_dir / str(candidate.get("path") or "")
    profile = state.get("settings", {}).get("render_profile") or {}
    width, height = int(profile.get("width") or 0), int(profile.get("height") or 0)
    fps = int(profile.get("frame_rate") or state["settings"].get("frame_rate") or 30)
    sample_rate = int(profile.get("sample_rate") or state["settings"].get("sample_rate") or 48000)
    if not baseline or not candidate_path.is_file() or not width or not height:
        return None, "缺少经过验证的片段缓存、候选视频或输出规格"
    duration = max(0.04, _as_number(segment["end_seconds"]) - _as_number(segment["start_seconds"]))
    version_id = _numbered_version(segment)
    output = project_dir / SEGMENT_DIRECTORY / segment["id"] / version_id / "segment.mp4"
    output.parent.mkdir(parents=True, exist_ok=True)
    filter_graph = (
        f"[0:v]fps={fps},scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,"
        f"tpad=stop_mode=clone:stop_duration={duration},trim=duration={duration},setpts=PTS-STARTPTS[v];"
        f"[1:a]atrim=duration={duration},asetpts=PTS-STARTPTS[a]"
    )
    ok, detail = _run_media([
        ffmpeg, "-y", "-i", str(candidate_path), "-i", str(baseline), "-filter_complex", filter_graph,
        "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-preset", "medium", "-crf", "18",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-ar", str(sample_rate), "-movflags", "+faststart", str(output),
    ])
    return (output, detail) if ok else (None, detail)


def _normalise_audio_candidate_segment(project_dir: Path, state: dict, patch: dict, candidate: dict, segment: dict, ffmpeg: str) -> tuple[Path | None, str]:
    """Fixed-slot audio replacement without changing natural speaking speed."""
    baseline = _current_artifact(project_dir, segment)
    candidate_path = project_dir / str(candidate.get("path") or "")
    profile = state.get("settings", {}).get("render_profile") or {}
    sample_rate = int(profile.get("sample_rate") or state["settings"].get("sample_rate") or 48000)
    if not baseline or not candidate_path.is_file():
        return None, "缺少经过验证的片段缓存或候选配音文件"
    duration = max(0.04, _as_number(segment["end_seconds"]) - _as_number(segment["start_seconds"]))
    try:
        natural_duration = _validate_fixed_slot_audio(candidate_path, _find(state["scenes"], str(segment["scene_ids"][0]), "场景"), ffmpeg)
    except WorkbenchError as exc:
        return None, str(exc)
    if natural_duration > duration + 0.03:
        return None, "候选配音略长于固定时间槽；为避免截断台词，请采用自然语速并调整时间线。"
    version_id = _numbered_version(segment)
    output = project_dir / SEGMENT_DIRECTORY / segment["id"] / version_id / "segment.mp4"
    output.parent.mkdir(parents=True, exist_ok=True)
    ok, detail = _run_media([
        ffmpeg, "-y", "-i", str(baseline), "-i", str(candidate_path),
        "-filter_complex", f"[1:a]apad=pad_dur={duration:.3f},atrim=duration={duration:.3f},asetpts=PTS-STARTPTS[a]",
        "-map", "0:v:0", "-map", "[a]", "-c:v", "copy", "-c:a", "aac", "-ar", str(sample_rate),
        "-movflags", "+faststart", str(output),
    ])
    return (output, detail) if ok and output.is_file() else (None, detail)


def _scene_narration_version(scene: dict, version_id: str | None) -> dict | None:
    narration = scene.get("narration") if isinstance(scene.get("narration"), dict) else {}
    return next((item for item in narration.get("versions", []) if item.get("id") == version_id), None)


def _render_ripple_audio_segment(
    project_dir: Path,
    state: dict,
    patch: dict,
    candidate: dict,
    segment: dict,
    ffmpeg: str,
) -> tuple[Path | None, str]:
    """Rebuild B with natural audio and local captions; A/C stay byte-identical."""
    scene_id = str(segment.get("scene_ids", [""])[0])
    scene = _find(state["scenes"], scene_id, "场景")
    visual = _selected_visual_asset(state, scene_id)
    if not visual or visual.get("type") != "video" or not visual.get("path"):
        return None, "当前场景没有可重用的视频素材；请先为该场景选择视频，或点击“一键换素材”。"
    visual_path = project_dir / str(visual["path"])
    audio_path = project_dir / str(candidate.get("path") or "")
    if not visual_path.is_file() or not audio_path.is_file():
        return None, "当前场景视频或候选配音文件不可用"
    duration = max(0.04, _as_number(patch.get("target_duration_seconds")))
    natural_duration = _probe_duration_seconds(audio_path, ffmpeg, duration)
    if abs(natural_duration - duration) > 0.05:
        duration = natural_duration
        patch["target_duration_seconds"] = duration
        patch["timeline_impact"] = _build_timeline_update(state, {scene_id: duration}, reason="scene_narration_ripple_render")
    fit = _visual_fit_plan(project_dir, state, {**scene, "end_seconds": _as_number(scene.get("start_seconds")) + duration}, visual)
    scene["visual_fit"] = fit
    if fit["strategy"] == "needs_replacement":
        return None, (
            f"当前素材仅 {fit.get('source_duration_seconds') or 0:.2f} 秒，无法自然覆盖新的 {duration:.2f} 秒配音。"
            "请点击“一键换素材”，系统会按新配音时长重新搜索。"
        )
    profile = state.get("settings", {}).get("render_profile") or {}
    width, height = int(profile.get("width") or 0), int(profile.get("height") or 0)
    if not width or not height:
        width, height = _render_dimensions(project_dir, state)
    fps = int(profile.get("frame_rate") or state.get("settings", {}).get("frame_rate") or 30)
    sample_rate = int(profile.get("sample_rate") or state.get("settings", {}).get("sample_rate") or 48000)
    version = _scene_narration_version(scene, patch.get("narration_version_id"))
    subtitle_path = project_dir / str((version or {}).get("subtitle_path") or "")
    video_filters = [
        f"fps={fps}",
        f"scale={width}:{height}:force_original_aspect_ratio=increase",
        f"crop={width}:{height}",
        "setsar=1",
    ]
    if fit["strategy"] == "brief_hold":
        video_filters.append(f"tpad=stop_mode=clone:stop_duration={fit['shortfall_seconds']:.3f}")
    video_filters.extend([f"trim=duration={duration:.3f}", "setpts=PTS-STARTPTS"])
    if subtitle_path.is_file():
        video_filters.append(VideoCompose._build_subtitles_filter(
            subtitle_path,
            {"font": "Microsoft YaHei", "responsive": True, "bold": True, "primary_color": "&H00FFFFFF", "outline_color": "&HAA000000", "outline_width": 2, "margin_v": 30, "alignment": 2},
            original_size=f"{width}x{height}",
        ))
    version_id = _numbered_version(segment)
    output = project_dir / SEGMENT_DIRECTORY / segment["id"] / version_id / "segment.mp4"
    output.parent.mkdir(parents=True, exist_ok=True)
    filter_graph = (
        f"[0:v]{','.join(video_filters)}[v];"
        f"[1:a]atrim=duration={duration:.3f},asetpts=PTS-STARTPTS[a]"
    )
    ok, detail = _run_media([
        ffmpeg, "-y", "-i", str(visual_path), "-i", str(audio_path), "-filter_complex", filter_graph,
        "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-preset", "medium", "-crf", "18",
        "-pix_fmt", "yuv420p", "-r", str(fps), "-c:a", "aac", "-ar", str(sample_rate),
        "-movflags", "+faststart", str(output),
    ])
    if not ok or not output.is_file():
        return None, detail
    return output, f"自然配音 {duration:.2f} 秒；画面策略：{fit['strategy']}"


def _concat_segments(project_dir: Path, paths: list[Path], output: Path, ffmpeg: str) -> tuple[bool, str]:
    output.parent.mkdir(parents=True, exist_ok=True)
    list_path = output.with_suffix(".concat.txt")
    # All names are generated project paths.  concat demuxer accepts forward
    # slashes on Windows and quoting protects a space in the project name.
    lines = ["file '" + str(path.resolve()).replace("\\", "/").replace("'", "'\\''") + "'" for path in paths]
    list_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        return _run_media([ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", str(list_path), "-c", "copy", "-movflags", "+faststart", str(output)])
    finally:
        try:
            list_path.unlink()
        except OSError:
            pass


def render_patch(project_dir: Path, patch_id: str) -> dict:
    """Render B against the cached segment contract and stream-copy A/C."""
    state = _load_for_write(project_dir)
    patch = _find(state["patches"], patch_id, "热插拔任务")
    if patch.get("status") not in {"planned", "ready_to_render", "rendering", "blocked"}:
        raise WorkbenchError("该热插拔任务当前不能渲染")
    visual_asset = _candidate_asset(state, patch)
    audio_asset = _candidate_audio_asset(state, patch)
    change_scope = str(patch.get("change_scope") or "visual")
    report = {
        "patch_id": patch_id, "at": _now(), "mode": patch["mode"], "change_scope": change_scope,
        "status": "blocked", "checks": [], "output": None,
    }
    segment = _find(state["segments"], patch["segment_id"], "渲染片段")
    ffmpeg = _ffmpeg_available()
    visual_ok = bool(visual_asset and visual_asset.get("type") == "video" and visual_asset.get("path") and (project_dir / visual_asset["path"]).is_file())
    audio_ok = bool(audio_asset and audio_asset.get("type") == "audio" and audio_asset.get("path") and (project_dir / audio_asset["path"]).is_file())
    candidate_ok = audio_ok if change_scope == "audio" else visual_ok if change_scope == "visual" else visual_ok and audio_ok
    candidate_detail = (
        audio_asset.get("path") if change_scope == "audio" and audio_ok
        else visual_asset.get("path") if change_scope == "visual" and visual_ok
        else "音频和视频候选均已指定" if change_scope == "mixed" and candidate_ok
        else "请指定项目内可用的候选配音或视频素材"
    )
    report["checks"].append({"name": "候选素材", "ok": candidate_ok, "detail": candidate_detail})
    report["checks"].append({"name": "冻结快照", "ok": bool(patch.get("dependencies", {}).get("segment_snapshot")), "detail": "A/C 边界已快照"})
    report["checks"].append({"name": "渲染器", "ok": bool(ffmpeg), "detail": "ffmpeg" if ffmpeg else "未发现 ffmpeg"})
    cached = [_current_artifact(project_dir, item) for item in state["segments"]]
    cache_ok = all(cached)
    report["checks"].append({
        "name": "片段缓存合同", "ok": cache_ok,
        "detail": "所有 A/B/C 均有标准化独立片段，可对 A/C 流复制" if cache_ok else "请先建立一次片段缓存；这一步会从现有成片生成可冻结基线",
    })
    if patch.get("mode") == "ripple_timeline":
        impact = patch.get("timeline_impact") or {}
        report["checks"].append({
            "name": "自然配音时间线", "ok": bool(impact),
            "detail": (
                f"目标片段采用 {patch.get('target_duration_seconds') or 0:.2f} 秒自然配音；"
                f"完整成片将从 {impact.get('previous_total_duration_seconds') or 0:.2f} 秒变为 {impact.get('new_total_duration_seconds') or 0:.2f} 秒。"
                if impact else "缺少波纹时间线预览"
            ),
        })

    if candidate_ok and ffmpeg and cache_ok and report["checks"][1]["ok"]:
        if change_scope == "audio":
            candidate_file, detail = (
                _render_ripple_audio_segment(project_dir, state, patch, audio_asset, segment, ffmpeg)
                if patch.get("mode") == "ripple_timeline"
                else _normalise_audio_candidate_segment(project_dir, state, patch, audio_asset, segment, ffmpeg)
            )
        elif change_scope == "visual":
            candidate_file, detail = _normalise_candidate_segment(project_dir, state, patch, visual_asset, segment, ffmpeg)
        else:
            candidate_file, detail = None, "混合替换尚未开放；请先分别完成画面或配音候选审核"
        if candidate_file is None:
            report["checks"].append({"name": "目标 B 标准化", "ok": False, "detail": detail})
        else:
            report["checks"].append({"name": "目标 B 标准化", "ok": True, "detail": _relative(project_dir, candidate_file)})
            ordered: list[Path] = []
            for item in state["segments"]:
                ordered.append(candidate_file if item["id"] == segment["id"] else _current_artifact(project_dir, item))
            composition = project_dir / "renders" / "compositions" / f"composition-{patch_id}-{_numbered_version(segment)}.mp4"
            joined, join_detail = _concat_segments(project_dir, ordered, composition, ffmpeg)
            report["checks"].append({
                "name": "A/B/C 合成", "ok": joined,
                "detail": (
                    "A/C 内容已以流复制保留；B 已按自然配音时长重新合成，后续片段仅改变时间位置。"
                    if joined and patch.get("mode") == "ripple_timeline"
                    else "A/C 已以流复制拼接；只有 B 进行了标准化编码" if joined else join_detail
                ),
            })
            if joined:
                try:
                    patch_mix = _apply_project_audio_mix(project_dir, state, composition)
                    patch_music = patch_mix["background_music"]
                    report["checks"].append({
                        "name": "全片声音合同", "ok": True,
                        "detail": (
                            f"人物 {patch_mix['narration'].get('playback_gain_db', 0):+.1f} dB；"
                            + (
                                f"已重新混入 {patch_music.get('title')}，避免局部换音后出现音乐断层"
                                if patch_music.get("enabled") else "项目未启用背景音乐"
                            )
                        ),
                    })
                    patch_loudness = _normalize_video_loudness(
                        project_dir, composition, target_lufs=-14.0
                    )
                    report["audio_mix"] = {
                        **patch_mix,
                        "signature": _audio_mix_signature(state),
                        "loudness": patch_loudness,
                    }
                except WorkbenchError as exc:
                    joined = False
                    report["checks"].append({"name": "全片声音合同", "ok": False, "detail": str(exc)})
            if joined:
                version_id = _numbered_version(segment)
                patch["candidate_version_id"] = version_id
                patch["candidate_artifact_path"] = _relative(project_dir, candidate_file)
                patch["composition_candidate_path"] = _relative(project_dir, composition)
                report["status"] = "rendered"
                report["output"] = patch["composition_candidate_path"]
                patch["status"] = "rendered"
    if patch.get("status") != "rendered":
        patch["status"] = "blocked"
        if patch["mode"] == "strict_freeze":
            report["checks"].append({"name": "严格冻结承诺", "ok": False, "detail": "未满足缓存/编码合同，系统没有重编码 A/C，也没有生成伪完成成片。"})
        elif patch["mode"] == "ripple_timeline":
            report["checks"].append({"name": "波纹时间线承诺", "ok": False, "detail": "系统没有拉伸候选配音；请补齐可覆盖真实时长的画面素材后重试。"})
        else:
            report["checks"].append({"name": "缝合转场声明", "ok": False, "detail": "尚未满足可生成候选版本的前置条件。"})
    if patch.get("source") == "scene_narration":
        scene = next((item for item in state.get("scenes", []) if item.get("id") in patch.get("scene_ids", [])), None)
        if scene and isinstance(scene.get("narration"), dict):
            narration = scene["narration"]
            narration["status"] = "candidate_rendered" if patch.get("status") == "rendered" else "candidate_failed"
            narration["job"] = {
                **(narration.get("job") or {}), "status": "rendered" if patch.get("status") == "rendered" else "failed",
                "finished_at": _now(), "patch_id": patch_id,
                "error": "" if patch.get("status") == "rendered" else "; ".join(str(check.get("detail") or "") for check in report["checks"] if not check.get("ok"))[-1200:],
            }
    patch["render_report"] = report
    _activity(state, "patch_render", f"{patch_id} 已完成局部渲染：{patch['status']}", patch_id=patch_id)
    report_path = project_dir / BOUNDARY_DIRECTORY / f"{patch_id}.json"
    _atomic_write(report_path, report)
    _persist_patch_artifact(project_dir, patch)
    return _save(project_dir, state)


def mark_patch_render_failed(project_dir: Path, patch_id: str, error: object) -> dict:
    """Persist an unexpected local-composition failure for the reviewer."""
    state = _load_for_write(project_dir)
    patch = _find(state["patches"], patch_id, "热插拔任务")
    message = _safe_automation_error(error)
    patch["status"] = "blocked"
    patch["render_report"] = {
        "patch_id": patch_id, "at": _now(), "mode": patch.get("mode"), "status": "blocked", "output": None,
        "checks": [{"name": "局部合成任务", "ok": False, "detail": message}],
    }
    if patch.get("source") == "scene_narration":
        scene = next((item for item in state.get("scenes", []) if item.get("id") in patch.get("scene_ids", [])), None)
        if scene and isinstance(scene.get("narration"), dict):
            scene["narration"]["status"] = "candidate_failed"
            scene["narration"]["job"] = {**(scene["narration"].get("job") or {}), "status": "failed", "finished_at": _now(), "error": message}
    _persist_patch_artifact(project_dir, patch)
    _atomic_write(project_dir / BOUNDARY_DIRECTORY / f"{patch_id}.json", patch["render_report"])
    _activity(state, "patch_render_failed", f"{patch_id} 局部合成失败：{message}", patch_id=patch_id)
    return _save(project_dir, state)


def _promote_composition_to_final(project_dir: Path, composition_path: str | None) -> str:
    """Atomically publish the reviewed composition as the project's final MP4."""
    if not composition_path:
        raise WorkbenchError("候选合成文件缺失，无法并入最终成片")
    try:
        candidate = (project_dir / composition_path).resolve()
        candidate.relative_to(project_dir.resolve())
    except (OSError, ValueError) as exc:
        raise WorkbenchError("候选合成文件路径无效") from exc
    if not candidate.is_file():
        raise WorkbenchError("候选合成文件不存在，请重新执行片段合成")
    final_path = project_dir / "renders" / "final.mp4"
    final_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = final_path.with_name(f".{final_path.stem}-promote-{uuid4().hex[:8]}.mp4")
    try:
        shutil.copy2(candidate, temporary)
        os.replace(temporary, final_path)
    except OSError as exc:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise WorkbenchError(f"无法发布局部合成后的成片：{exc}") from exc
    return _relative(project_dir, final_path)


def promote_patch(project_dir: Path, patch_id: str) -> dict:
    state = _load_for_write(project_dir)
    patch = _find(state["patches"], patch_id, "热插拔任务")
    if patch.get("status") != "rendered":
        raise WorkbenchError("只有通过渲染与人工缝合审核的候选版本才能并入成片")
    segment = _find(state["segments"], patch["segment_id"], "渲染片段")
    old_current = segment.get("current_version_id")
    version_id = patch.get("candidate_version_id") or _numbered_version(segment)
    timeline_update = patch.get("timeline_impact") if patch.get("mode") == "ripple_timeline" else None
    if timeline_update:
        _apply_timeline_update(state, timeline_update)
        segment = _find(state["segments"], patch["segment_id"], "渲染片段")
    for version in segment.get("versions", []):
        if version.get("id") == old_current:
            version["status"] = "superseded"
    segment.setdefault("versions", []).append({
        "id": version_id, "status": "current", "created_at": _now(), "from_patch": patch_id,
        "artifact_path": patch.get("candidate_artifact_path"),
        "composition_candidate_path": patch.get("composition_candidate_path"),
    })
    segment["current_version_id"] = version_id
    final_path = _promote_composition_to_final(project_dir, patch.get("composition_candidate_path"))
    patch["status"] = "promoted"
    patch["promoted_at"] = _now()
    patch["published_final_path"] = final_path
    if patch.get("source") == "scene_narration":
        scene = next((item for item in state.get("scenes", []) if item.get("id") in patch.get("scene_ids", [])), None)
        if scene and isinstance(scene.get("narration"), dict) and patch.get("narration_version_id"):
            _promote_scene_narration_version(state, scene, str(patch["narration_version_id"]))
            narration_path, subtitle_path = _rebuild_project_narration_from_scene_versions(project_dir, state)
            automation = _automation(state)
            if narration_path:
                automation["narration_generation"].update({
                    "status": "completed", "audio_path": _relative(project_dir, narration_path),
                    "subtitle_path": _relative(project_dir, subtitle_path), "error": "",
                })
            automation["render"].update({"status": "completed", "runtime": "ffmpeg", "output_path": final_path, "error": ""})
            automation["status"] = "review_ready"
            note = "已试听并通过局部合成；仅替换该片段音频"
            if timeline_update:
                note = (
                    f"已采用自然配音并波纹更新：片段时长变化 {timeline_update.get('delta_seconds') or 0:.2f} 秒；"
                    "前序内容不变，后续内容只调整全局位置。"
                )
            _decision(state, "scene_narration", scene["id"], str(patch["narration_version_id"]), note)
    composition = {
        "schema_version": WORKBENCH_VERSION, "updated_at": _now(),
        "segments": [{"id": s["id"], "version_id": s.get("current_version_id"), "freeze": s.get("freeze"), "start_seconds": s.get("start_seconds"), "end_seconds": s.get("end_seconds")} for s in state["segments"]],
        "last_patch": patch_id,
        "composition_path": patch.get("composition_candidate_path"), "published_final_path": final_path,
        "timeline_revision": (state.get("timeline") or {}).get("revision"),
    }
    _atomic_write(project_dir / COMPOSITION_FILE, composition)
    _persist_patch_artifact(project_dir, patch)
    _activity(state, "patch_promote", f"{patch_id} 已原子并入合成清单", patch_id=patch_id)
    return _save(project_dir, state)


def rollback_patch(project_dir: Path, patch_id: str) -> dict:
    state = _load_for_write(project_dir)
    patch = _find(state["patches"], patch_id, "热插拔任务")
    if patch.get("status") == "promoted":
        raise WorkbenchError("已并入成片的任务请通过版本回滚处理，不能删除其审计记录")
    if patch.get("status") == "rolled_back":
        return state
    patch["status"] = "rolled_back"
    patch["rolled_back_at"] = _now()
    _persist_patch_artifact(project_dir, patch)
    _activity(state, "patch_rollback", f"已回滚 {patch_id} 的候选方案，冻结版本保持不变", patch_id=patch_id)
    return _save(project_dir, state)
