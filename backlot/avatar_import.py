"""Provider-neutral avatar source import, validation, alignment, and assembly.

The workbench must not care whether an avatar clip came from a manual company
website workflow or a future server-side API adapter.  Both paths produce the
same ``avatar_source_package`` artifact.  Native avatar audio is the timing
authority; script estimates are never used as final cut points.
"""

from __future__ import annotations

import copy
import difflib
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import unicodedata
from datetime import UTC, datetime
from fractions import Fraction
from importlib import metadata as importlib_metadata
from pathlib import Path
from threading import RLock
from typing import Any
from uuid import uuid4

from schemas.artifacts import validate_artifact
from tools.video import video_compose as video_compose_runtime


PACKAGE_FILE = Path("artifacts/avatar_source_package.json")
PLAN_HISTORY_FILE = Path("artifacts/avatar_source_plans.json")
PLAN_SNAPSHOT_DIRECTORY = Path("artifacts/avatar_source_plans")
INCOMING_DIRECTORY = Path("assets/incoming/avatar")
OUTPUT_DIRECTORY = Path("renders/avatar")
SUPPORTED_MEDIA_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}
SPEAKER_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{1,31}$")
TURN_ID_RE = re.compile(r"^T[0-9]{3,}$")
MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024
_PACKAGE_WRITE_LOCKS: dict[str, RLock] = {}
_PACKAGE_WRITE_LOCKS_GUARD = RLock()


class AvatarImportError(ValueError):
    """A user-correctable avatar-package validation or processing failure."""


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _atomic_write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as file:
            json.dump(value, file, ensure_ascii=False, indent=2)
            file.write("\n")
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def _safe_relative(project_dir: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(project_dir.resolve()).as_posix()
    except (OSError, ValueError) as exc:
        raise AvatarImportError("数字人素材必须位于当前项目目录内") from exc


def _safe_project_file(project_dir: Path, raw_path: str) -> Path:
    path = (project_dir / raw_path).resolve()
    try:
        path.relative_to(project_dir.resolve())
    except (OSError, ValueError) as exc:
        raise AvatarImportError("数字人素材路径越过了当前项目边界") from exc
    return path


def _save_package(project_dir: Path, package: dict) -> dict:
    """Validate and atomically save one optimistic project revision.

    Background Voicebox and cloud-avatar workers can finish while a user is
    changing another input.  A plain last-writer-wins JSON save can silently
    erase the newer mutation.  The per-project lock serialises writers in this
    process, while the revision comparison also rejects a stale object that
    was read before another writer committed.
    """
    key = str(project_dir.resolve()).casefold()
    with _PACKAGE_WRITE_LOCKS_GUARD:
        lock = _PACKAGE_WRITE_LOCKS.setdefault(key, RLock())
    with lock:
        path = project_dir / PACKAGE_FILE
        expected_revision = int(package.get("revision") or 0)
        if path.is_file():
            try:
                current = json.loads(path.read_text(encoding="utf-8"))
                current_revision = int(current.get("revision") or 0) if isinstance(current, dict) else 0
            except (OSError, json.JSONDecodeError, TypeError, ValueError):
                current_revision = expected_revision
            if current_revision != expected_revision:
                raise AvatarImportError("项目状态刚刚发生变化，请刷新页面后重试；系统已阻止旧状态覆盖新结果")
        package["updated_at"] = _now()
        package["revision"] = expected_revision + 1
        try:
            validate_artifact("avatar_source_package", package)
        except Exception as exc:
            package["revision"] = expected_revision
            detail = getattr(exc, "message", None) or str(exc)
            raise AvatarImportError(f"数字人素材包不符合数据合同：{detail}") from exc
        _atomic_write(path, package)
        return package


def read_avatar_package(project_dir: Path) -> dict | None:
    path = project_dir / PACKAGE_FILE
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def _plan_kind(generation_mode: str, import_mode: str) -> str:
    if generation_mode == "dashscope_wan_s2v":
        return "dashscope_cloud"
    if generation_mode == "runninghub_longcat":
        return "runninghub_cloud"
    if generation_mode == "runninghub_longform":
        return "runninghub_longform"
    return "manual_longform" if import_mode == "longform" else "manual_per_turn"


def _new_plan_metadata(generation_mode: str, import_mode: str, *, replaces_plan_id: str | None = None, archived_count: int = 0) -> dict:
    kind = _plan_kind(generation_mode, import_mode)
    labels = {
        "manual_longform": "本地整段口播切割",
        "manual_per_turn": "本地逐段口播导入",
        "dashscope_cloud": "阿里云逐段生成",
        "runninghub_cloud": "RunningHub InfiniteTalk 精确帧逐段生成",
        "runninghub_longform": "RunningHub 双角色长视频生成与本地切割",
    }
    value = {
        "plan_id": f"AVP-{uuid4().hex[:16]}",
        "kind": kind,
        "label": labels[kind],
        "created_at": _now(),
        "archived_plan_count": archived_count,
    }
    if replaces_plan_id:
        value["replaces_plan_id"] = replaces_plan_id
    return value


def _read_plan_history(project_dir: Path) -> dict:
    path = project_dir / PLAN_HISTORY_FILE
    if not path.is_file():
        return {"version": "1.0", "plans": []}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"version": "1.0", "plans": []}
    return value if isinstance(value, dict) and isinstance(value.get("plans"), list) else {"version": "1.0", "plans": []}


def list_avatar_source_plans(project_dir: Path) -> dict:
    history = _read_plan_history(project_dir)
    current = read_avatar_package(project_dir)
    active = (current or {}).get("plan") if isinstance((current or {}).get("plan"), dict) else None
    return {"active": active, "archived": history["plans"]}


def _cloud_jobs_active(package: dict) -> bool:
    active = {"queued", "uploading", "detecting", "submitted", "running", "downloading"}
    return any(str((turn.get("cloud_job") or {}).get("status") or "") in active for turn in package.get("turns", []))


def switch_to_local_longform_plan(project_dir: Path, payload: dict | None = None) -> dict:
    """Archive the active plan and create an isolated local long-form plan.

    The archive is a JSON snapshot, so choosing local source material cannot
    erase the user's cloud configuration or completed provider outputs.  A
    still-running paid task is deliberately a hard stop: its worker may still
    write to the active package and must be settled/cancelled first.
    """
    payload = payload or {}
    existing = read_avatar_package(project_dir)
    if existing and existing.get("generation_mode") == "manual_import" and existing.get("import_mode") == "longform":
        return existing
    if existing and _cloud_jobs_active(existing):
        raise AvatarImportError("当前仍有阿里云数字人任务在运行。请先等待任务结束或在云端方案中取消后，再切换到本地整段口播方案；这样可避免后台任务写入错误方案。")
    history = _read_plan_history(project_dir)
    replaced_plan_id = None
    if existing:
        old_plan = existing.get("plan") if isinstance(existing.get("plan"), dict) else {}
        replaced_plan_id = str(old_plan.get("plan_id") or "") or None
        snapshot_id = replaced_plan_id or f"AVP-legacy-{uuid4().hex[:12]}"
        snapshot_path = project_dir / PLAN_SNAPSHOT_DIRECTORY / f"{snapshot_id}.json"
        _atomic_write(snapshot_path, existing)
        history["plans"].append({
            "plan_id": snapshot_id,
            "kind": str(old_plan.get("kind") or _plan_kind(str(existing.get("generation_mode") or "manual_import"), str(existing.get("import_mode") or "per_turn"))),
            "label": str(old_plan.get("label") or "历史数字人方案"),
            "archived_at": _now(),
            "snapshot_path": _safe_relative(project_dir, snapshot_path),
        })
        _atomic_write(project_dir / PLAN_HISTORY_FILE, history)
    package = initialize_avatar_package(project_dir, {
        "replace": True,
        "generation_mode": "manual_import",
        "import_mode": "longform",
        "background_mode": str(payload.get("background_mode") or "opaque"),
        "default_treatment": str(payload.get("default_treatment") or "fullscreen"),
        "frame_fit_mode": str(payload.get("frame_fit_mode") or "blur_background"),
    })
    package["plan"] = _new_plan_metadata(
        "manual_import", "longform", replaces_plan_id=replaced_plan_id, archived_count=len(history["plans"]),
    )
    return _save_package(project_dir, package)


def _project_id(project_dir: Path) -> str:
    marker = project_dir / "project.json"
    if marker.is_file():
        try:
            data = json.loads(marker.read_text(encoding="utf-8"))
            if data.get("project_id"):
                return str(data["project_id"])
        except (OSError, json.JSONDecodeError):
            pass
    return project_dir.name


def _project_render_profile(project_dir: Path) -> dict[str, int | str]:
    """Read the project's delivery canvas without making the avatar package guess.

    The avatar package is eventually assembled into this same project.  Keeping
    the dimensions here in sync from its first write prevents a portrait avatar
    package from silently being attached to a landscape project later on.
    """
    marker = project_dir / "project.json"
    try:
        project = json.loads(marker.read_text(encoding="utf-8")) if marker.is_file() else {}
    except (OSError, json.JSONDecodeError):
        project = {}
    profile = project.get("render_profile") if isinstance(project, dict) else {}
    if not isinstance(profile, dict):
        profile = {}
    width = int(profile.get("width") or 0)
    height = int(profile.get("height") or 0)
    if width < 16 or height < 16:
        width, height = 1080, 1920
    return {
        "width": width,
        "height": height,
        "fps": int(profile.get("fps") or 25),
        "audio_sample_rate": int(profile.get("audio_sample_rate") or 48000),
        "aspect_ratio": str(profile.get("aspect_ratio") or ("portrait" if height > width else "landscape")),
    }


def _load_script_sections(project_dir: Path) -> list[dict]:
    path = project_dir / "artifacts" / "script.json"
    if not path.is_file():
        raise AvatarImportError("项目缺少 artifacts/script.json，无法建立数字人轮次")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AvatarImportError("项目脚本不是有效 JSON") from exc
    sections = data.get("sections")
    if not isinstance(sections, list) or not sections:
        raise AvatarImportError("项目脚本没有可导入的 sections")
    return [item for item in sections if isinstance(item, dict)]


def _normalise_turns(raw_turns: list[dict]) -> tuple[list[dict], list[dict]]:
    turns: list[dict] = []
    speakers: dict[str, dict] = {}
    seen_turns: set[str] = set()
    for index, raw in enumerate(raw_turns, 1):
        turn_id = str(raw.get("turn_id") or f"T{index:03d}").upper()
        speaker_id = str(raw.get("speaker_id") or "").strip().lower()
        speaker_name = str(raw.get("speaker_name") or speaker_id).strip()
        text = str(raw.get("text") or "").strip()
        if not TURN_ID_RE.fullmatch(turn_id):
            raise AvatarImportError(f"轮次编号不合法：{turn_id}")
        if turn_id in seen_turns:
            raise AvatarImportError(f"轮次编号重复：{turn_id}")
        if not SPEAKER_ID_RE.fullmatch(speaker_id):
            raise AvatarImportError(f"说话人编号不合法：{speaker_id or '空值'}")
        if not text:
            raise AvatarImportError(f"{turn_id} 的台词为空")
        seen_turns.add(turn_id)
        speakers.setdefault(speaker_id, {"speaker_id": speaker_id, "name": speaker_name or speaker_id})
        visual_contract = raw.get("visual_contract") if isinstance(raw.get("visual_contract"), dict) else None
        turn = {
            "turn_id": turn_id,
            "index": index,
            "speaker_id": speaker_id,
            "text": text,
            "expected_filename": str(raw.get("expected_asset_filename") or f"{turn_id}_{speaker_id.upper()}.mp4"),
            "status": "missing",
        }
        if visual_contract:
            turn["visual_contract"] = visual_contract
        turns.append(turn)
    return turns, list(speakers.values())


def initialize_avatar_package(project_dir: Path, payload: dict | None = None) -> dict:
    """Create an idempotent package contract from an approved multi-speaker script."""
    payload = payload or {}
    existing = read_avatar_package(project_dir)
    if existing and payload.get("replace") is not True:
        return existing
    raw_turns = payload.get("turns")
    if not isinstance(raw_turns, list) or not raw_turns:
        raw_turns = _load_script_sections(project_dir)
    turns, derived_speakers = _normalise_turns(raw_turns)
    supplied_speakers = payload.get("speakers")
    speakers = supplied_speakers if isinstance(supplied_speakers, list) and supplied_speakers else derived_speakers
    speaker_ids = {str(item.get("speaker_id") or "").lower() for item in speakers if isinstance(item, dict)}
    if speaker_ids != {turn["speaker_id"] for turn in turns}:
        raise AvatarImportError("说话人清单与脚本轮次不一致")
    generation_mode = str(payload.get("generation_mode") or "manual_import")
    per_turn_cloud_modes = {"dashscope_wan_s2v", "runninghub_longcat"}
    provider_modes = {*per_turn_cloud_modes, "runninghub_longform"}
    if generation_mode not in {"manual_import", *provider_modes}:
        raise AvatarImportError("数字人生成方式只能是本地导入、阿里云或 RunningHub 口播生成")
    import_mode = str(payload.get("import_mode") or "per_turn")
    if generation_mode in per_turn_cloud_modes:
        # The cloud API takes exactly one driving audio file for each clip.
        # A long-form upload would lose the retry and timing boundary contract.
        import_mode = "per_turn"
    elif generation_mode == "runninghub_longform":
        import_mode = "longform"
    if import_mode not in {"per_turn", "longform"}:
        raise AvatarImportError("数字人导入模式只能是 per_turn 或 longform")
    render_profile = _project_render_profile(project_dir)
    settings = {
        "max_duration_seconds": float(payload.get("max_duration_seconds") or 120),
        "fps": int(payload.get("fps") or render_profile["fps"]),
        "audio_sample_rate": int(payload.get("audio_sample_rate") or render_profile["audio_sample_rate"]),
        "width": int(payload.get("width") or render_profile["width"]),
        "height": int(payload.get("height") or render_profile["height"]),
        "require_asr": bool(payload.get("require_asr", generation_mode not in per_turn_cloud_modes)),
        "minimum_turn_coverage": float(payload.get("minimum_turn_coverage") or 0.80),
        "minimum_turn_similarity": float(payload.get("minimum_turn_similarity") or 0.86),
        "minimum_average_similarity": float(payload.get("minimum_average_similarity") or 0.92),
        "speaker_change_gap_seconds": float(payload.get("speaker_change_gap_seconds") or 0.16),
        "same_speaker_gap_seconds": float(payload.get("same_speaker_gap_seconds") or 0.0),
    }
    replacement_revision = int(existing.get("revision") or 0) if existing and payload.get("replace") is True else 0
    package = {
        "version": "1.0",
        "project_id": _project_id(project_dir),
        "audio_mode": "native_avatar_audio",
        "import_mode": import_mode,
        "generation_mode": generation_mode,
        "plan": _new_plan_metadata(generation_mode, import_mode),
        "provider": {
            "type": "company_api" if generation_mode in provider_modes else "manual_import",
            "name": (
                "DashScopeWanS2VProvider" if generation_mode == "dashscope_wan_s2v"
                else "RunningHubInfiniteTalkProvider" if generation_mode == "runninghub_longcat"
                else "RunningHubLongformProvider" if generation_mode == "runninghub_longform"
                else "ManualAvatarImportProvider"
            ),
            "base_url": "https://www.runninghub.cn" if generation_mode in {"runninghub_longcat", "runninghub_longform"} else None,
            "api_version": (
                "wan2.2-s2v" if generation_mode == "dashscope_wan_s2v"
                else "InfiniteTalk-exact-clock-workflow-v2" if generation_mode in {"runninghub_longcat", "runninghub_longform"}
                else None
            ),
        },
        "presentation": {
            "background_mode": str(payload.get("background_mode") or "opaque"),
            "alpha_mode": bool(payload.get("alpha_mode", False)),
            "expected_audio": str(payload.get("expected_audio") or "embedded_native"),
            "default_treatment": str(payload.get("default_treatment") or "fullscreen"),
            "frame_fit_mode": str(payload.get("frame_fit_mode") or ("blur_background" if import_mode == "longform" else "contain_black")),
        },
        "speakers": [{"speaker_id": str(item["speaker_id"]).lower(), "name": str(item.get("name") or item["speaker_id"])} for item in speakers],
        "turns": turns,
        "settings": settings,
        "validation": {"status": "not_started", "issues": [], "summary": {}},
        "asr": {"status": "not_started", "issues": [], "summary": {}},
        "cut_plan": {"status": "not_started", "items": [], "summary": {}},
        "assembly": {"status": "not_started", "issues": [], "summary": {}},
        "created_at": _now(),
        "updated_at": _now(),
        "revision": replacement_revision,
    }
    if generation_mode in per_turn_cloud_modes:
        package["speaker_bindings"] = [
            {
                "speaker_id": speaker["speaker_id"],
                "name": speaker["name"],
                "status": "not_ready",
                "sample": {"status": "not_started", "turn_id": None, "input_hash": None, "approved": False},
                "updated_at": _now(),
            }
            for speaker in package["speakers"]
        ]
        package["cloud"] = {
            "provider": generation_mode,
            "model": "wan2.2-s2v" if generation_mode == "dashscope_wan_s2v" else "InfiniteTalk-exact-clock-v2",
            "resolution": str(payload.get("resolution") or ("480P" if generation_mode == "dashscope_wan_s2v" else "448x560")),
            "aspect_ratio": "portrait" if generation_mode == "runninghub_longcat" else str(render_profile["aspect_ratio"]),
            "input_fit_mode": "cover_crop",
            "render_spec_revision": 1,
            "status": "not_ready",
            "sample_turn_id": None,
            "sample_turn_ids": [],
            "sample_approved": False,
            "batch_started": False,
            "updated_at": _now(),
            "message": "请为每位说话人上传实际出镜图，并为每段台词准备驱动音频；通用角色档案为可选留档。",
        }
    return _save_package(project_dir, package)


def _find_turn(package: dict, turn_id: str) -> dict:
    for turn in package.get("turns", []):
        if turn.get("turn_id") == turn_id:
            return turn
    raise AvatarImportError(f"素材包中不存在轮次 {turn_id}")


def _find_speaker(package: dict, speaker_id: str) -> dict:
    for speaker in package.get("speakers", []):
        if speaker.get("speaker_id") == speaker_id:
            return speaker
    raise AvatarImportError(f"素材包中不存在说话人 {speaker_id}")


def prepare_upload(
    project_dir: Path,
    original_filename: str,
    *,
    turn_id: str | None = None,
    speaker_id: str | None = None,
) -> tuple[Path, Path]:
    """Return a project-local temporary file and canonical final upload path."""
    package = read_avatar_package(project_dir)
    if not package:
        raise AvatarImportError("请先初始化数字人素材包")
    filename = Path(original_filename).name
    extension = Path(filename).suffix.lower()
    if extension not in SUPPORTED_MEDIA_EXTENSIONS:
        raise AvatarImportError("仅支持 MP4、MOV、MKV、WEBM 或 M4V 数字人视频")
    if turn_id:
        turn = _find_turn(package, turn_id.upper())
        target_dir = project_dir / INCOMING_DIRECTORY / turn["speaker_id"]
        target = target_dir / f"{turn['turn_id']}_{turn['speaker_id'].upper()}{extension}"
    elif speaker_id:
        speaker = _find_speaker(package, speaker_id.lower())
        target_dir = project_dir / INCOMING_DIRECTORY / "longform"
        target = target_dir / f"{speaker['speaker_id']}{extension}"
    else:
        raise AvatarImportError("上传数字人素材时必须指定 turn_id 或 speaker_id")
    target_dir.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(prefix=".avatar-upload-", suffix=extension, dir=target_dir)
    os.close(handle)
    return Path(temp_name), target


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _find_binary(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    resolver = getattr(video_compose_runtime, "_discover_ffmpeg_pair", None)
    pair = resolver() if callable(resolver) else None
    if pair and len(pair) == 2:
        index = 0 if name.lower() == "ffmpeg" else 1 if name.lower() == "ffprobe" else None
        if index is not None and Path(pair[index]).is_file():
            return str(Path(pair[index]).resolve())
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        package_root = Path(local_app_data) / "Microsoft" / "WinGet" / "Packages"
        executable = f"{name}.exe" if os.name == "nt" else name
        try:
            matches = sorted(package_root.glob(f"Gyan.FFmpeg.Essentials_*/*/bin/{executable}"))
        except OSError:
            matches = []
        if matches:
            return str(matches[-1])
    return None


def _run(command: list[str], *, timeout: int = 20 * 60) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AvatarImportError(str(exc)) from exc


def _fps(value: str | None) -> float:
    if not value or value in {"0/0", "N/A"}:
        return 0.0
    try:
        return float(Fraction(value))
    except (ValueError, ZeroDivisionError):
        return 0.0


def probe_media(path: Path) -> dict:
    ffprobe = _find_binary("ffprobe")
    if not ffprobe:
        raise AvatarImportError("未发现 ffprobe，无法检查数字人视频")
    result = _run([
        ffprobe,
        "-v", "error",
        "-count_frames",
        "-show_entries", "format=duration,size:stream=index,codec_type,codec_name,width,height,pix_fmt,r_frame_rate,avg_frame_rate,nb_frames,nb_read_frames,duration,sample_rate,channels",
        "-of", "json",
        str(path),
    ])
    if result.returncode != 0:
        raise AvatarImportError((result.stderr or "ffprobe 无法读取视频")[-2000:])
    try:
        raw = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise AvatarImportError("ffprobe 返回了无效 JSON") from exc
    video = next((item for item in raw.get("streams", []) if item.get("codec_type") == "video"), None)
    audio = next((item for item in raw.get("streams", []) if item.get("codec_type") == "audio"), None)
    duration = float((raw.get("format") or {}).get("duration") or 0)
    try:
        video_frame_count = int((video or {}).get("nb_read_frames") or (video or {}).get("nb_frames") or 0)
    except (TypeError, ValueError):
        video_frame_count = 0
    video_duration = float((video or {}).get("duration") or duration or 0)
    audio_duration = float((audio or {}).get("duration") or duration or 0)
    return {
        "duration_seconds": round(duration, 6),
        "size_bytes": int((raw.get("format") or {}).get("size") or path.stat().st_size),
        "video": {
            "present": bool(video),
            "codec": (video or {}).get("codec_name"),
            "width": int((video or {}).get("width") or 0),
            "height": int((video or {}).get("height") or 0),
            "fps": round(_fps((video or {}).get("r_frame_rate")), 3),
            "average_fps": round(_fps((video or {}).get("avg_frame_rate")), 3),
            "frame_count": video_frame_count,
            "duration_seconds": round(video_duration, 6),
            "pixel_format": (video or {}).get("pix_fmt"),
        },
        "audio": {
            "present": bool(audio),
            "codec": (audio or {}).get("codec_name"),
            "sample_rate": int((audio or {}).get("sample_rate") or 0),
            "channels": int((audio or {}).get("channels") or 0),
            "duration_seconds": round(audio_duration, 6),
        },
    }


def finalize_upload(
    project_dir: Path,
    temp_path: Path,
    target_path: Path,
    original_filename: str,
    *,
    turn_id: str | None = None,
    speaker_id: str | None = None,
) -> dict:
    """Probe first, then atomically promote an upload and update the package.

    A malformed replacement must never destroy a previously valid source file.
    """
    if not temp_path.is_file() or temp_path.stat().st_size <= 0:
        raise AvatarImportError("上传文件为空")
    if temp_path.stat().st_size > MAX_UPLOAD_BYTES:
        raise AvatarImportError("单个数字人视频不能超过 2GB")
    package = read_avatar_package(project_dir)
    if not package:
        raise AvatarImportError("数字人素材包在上传过程中丢失")
    media = probe_media(temp_path)
    if not media["video"]["present"] or not media["audio"]["present"] or media["duration_seconds"] <= 0:
        raise AvatarImportError("数字人视频必须同时包含可读取的画面、原生音频和有效时长")
    digest = _file_sha256(temp_path)
    size_bytes = temp_path.stat().st_size
    target_path.parent.mkdir(parents=True, exist_ok=True)
    os.replace(temp_path, target_path)
    source = {
        "path": _safe_relative(project_dir, target_path),
        "original_filename": Path(original_filename).name,
        "sha256": digest,
        "size_bytes": size_bytes,
        "uploaded_at": _now(),
        "media": media,
    }
    if turn_id:
        turn = _find_turn(package, turn_id.upper())
        turn["source"] = source
        turn["status"] = "media_valid"
    elif speaker_id:
        speaker = _find_speaker(package, speaker_id.lower())
        speaker["source"] = source
    package["validation"] = {"status": "pending", "issues": [], "summary": {}}
    package["asr"] = {"status": "not_started", "issues": [], "summary": {}}
    package["cut_plan"] = {"status": "not_started", "items": [], "summary": {}}
    package["assembly"] = {"status": "not_started", "issues": [], "summary": {}}
    return _save_package(project_dir, package)


def _issue(code: str, message: str, *, severity: str = "error", turn_id: str | None = None) -> dict:
    value = {"code": code, "severity": severity, "message": message}
    if turn_id:
        value["turn_id"] = turn_id
    return value


def validate_avatar_package(project_dir: Path) -> dict:
    package = read_avatar_package(project_dir)
    if not package:
        raise AvatarImportError("请先初始化数字人素材包")
    issues: list[dict] = []
    durations: list[float] = []
    hashes: dict[str, list[str]] = {}
    turn_ids = [str(turn.get("turn_id") or "") for turn in package.get("turns", [])]
    duplicates = sorted({turn_id for turn_id in turn_ids if turn_ids.count(turn_id) > 1})
    if duplicates:
        issues.append(_issue("duplicate_turn_id", f"轮次编号重复：{', '.join(duplicates)}"))
    expected_indexes = list(range(1, len(package.get("turns", [])) + 1))
    actual_indexes = [turn.get("index") for turn in package.get("turns", [])]
    if actual_indexes != expected_indexes:
        issues.append(_issue("turn_order_invalid", "轮次 index 必须从 1 开始连续递增，且顺序不可交换"))
    speaker_ids = {speaker.get("speaker_id") for speaker in package.get("speakers", [])}
    unknown_speakers = sorted({str(turn.get("speaker_id")) for turn in package.get("turns", []) if turn.get("speaker_id") not in speaker_ids})
    if unknown_speakers:
        issues.append(_issue("unknown_turn_speaker", f"轮次引用了未登记的人物：{', '.join(unknown_speakers)}"))
    if package["import_mode"] == "per_turn":
        for turn in package["turns"]:
            source = turn.get("source")
            if not source:
                issues.append(_issue("missing_turn_file", f"{turn['turn_id']} 尚未上传", turn_id=turn["turn_id"]))
                turn["status"] = "missing"
                continue
            path = _safe_project_file(project_dir, source["path"])
            if not path.is_file():
                issues.append(_issue("missing_source_path", f"{turn['turn_id']} 的素材文件不存在", turn_id=turn["turn_id"]))
                turn["status"] = "missing"
                continue
            try:
                media = probe_media(path)
                source["media"] = media
            except AvatarImportError as exc:
                issues.append(_issue("unreadable_media", str(exc), turn_id=turn["turn_id"]))
                turn["status"] = "media_invalid"
                continue
            if not media["video"]["present"] or not media["audio"]["present"]:
                issues.append(_issue("missing_stream", f"{turn['turn_id']} 必须同时包含视频和原生音频", turn_id=turn["turn_id"]))
                turn["status"] = "media_invalid"
                continue
            turn["status"] = "media_valid"
            durations.append(float(media["duration_seconds"]))
            hashes.setdefault(source["sha256"], []).append(turn["turn_id"])
    else:
        for speaker in package["speakers"]:
            source = speaker.get("source")
            if not source:
                issues.append(_issue("missing_longform_source", f"{speaker['name']} 尚未上传长视频"))
                continue
            path = _safe_project_file(project_dir, source["path"])
            if not path.is_file():
                issues.append(_issue("missing_longform_path", f"{speaker['name']} 的长视频不存在"))
                continue
            try:
                media = probe_media(path)
                source["media"] = media
            except AvatarImportError as exc:
                issues.append(_issue("unreadable_longform_media", str(exc)))
                continue
            if not media["video"]["present"] or not media["audio"]["present"]:
                issues.append(_issue("missing_longform_stream", f"{speaker['name']} 的长视频必须包含视频和原生音频"))
            durations.append(float(media["duration_seconds"]))
    for digest, turn_ids in hashes.items():
        if len(turn_ids) > 1:
            issues.append(_issue("duplicate_file_content", f"{', '.join(turn_ids)} 使用了完全相同的视频文件", severity="warning"))
    settings = package["settings"]
    gap_total = 0.0
    if package["import_mode"] == "per_turn":
        for current, following in zip(package["turns"], package["turns"][1:]):
            gap_total += settings["speaker_change_gap_seconds"] if current["speaker_id"] != following["speaker_id"] else settings["same_speaker_gap_seconds"]
        estimated_duration = sum(durations) + gap_total
        if len(durations) == len(package["turns"]) and estimated_duration > settings["max_duration_seconds"]:
            issues.append(_issue("duration_exceeded", f"预计母版 {estimated_duration:.2f} 秒，超过上限 {settings['max_duration_seconds']:.2f} 秒"))
    else:
        estimated_duration = None
    errors = [item for item in issues if item["severity"] == "error"]
    warnings = [item for item in issues if item["severity"] == "warning"]
    status = "failed" if errors else "passed_with_warnings" if warnings else "passed"
    package["validation"] = {
        "status": status,
        "finished_at": _now(),
        "issues": issues,
        "summary": {
            "expected_turns": len(package["turns"]),
            "valid_turn_files": len(durations) if package["import_mode"] == "per_turn" else None,
            "speaker_sources": len(durations) if package["import_mode"] == "longform" else None,
            "missing_turns": [
                turn["turn_id"]
                for turn in package["turns"]
                if turn.get("status") == "missing"
            ],
            "estimated_duration_seconds": round(estimated_duration, 3) if estimated_duration is not None else None,
            "error_count": len(errors),
            "warning_count": len(warnings),
        },
    }
    return _save_package(project_dir, package)


def normalize_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).lower()
    return "".join(char for char in value if not char.isspace() and not unicodedata.category(char).startswith("P"))


_TRADITIONAL_TO_SIMPLIFIED = None


def _simplify_chinese(value: str) -> str:
    """Convert Traditional Chinese to Simplified before character alignment.

    Whisper may transcribe the same Mandarin audio in Traditional Chinese even
    when the approved script is Simplified Chinese.  Text is normalised only
    for matching; the stored ASR transcript remains untouched for review.
    """
    global _TRADITIONAL_TO_SIMPLIFIED
    if _TRADITIONAL_TO_SIMPLIFIED is None:
        try:
            from opencc import OpenCC
        except ModuleNotFoundError as exc:
            raise AvatarImportError("缺少中文繁简体转换组件，请安装 opencc-python-reimplemented 后重试") from exc
        _TRADITIONAL_TO_SIMPLIFIED = OpenCC("t2s")
    return _TRADITIONAL_TO_SIMPLIFIED.convert(value)


def text_metrics(expected: str, actual: str) -> tuple[float, float]:
    expected_norm = normalize_text(_simplify_chinese(expected))
    actual_norm = normalize_text(_simplify_chinese(actual))
    if not expected_norm:
        return 0.0, 0.0
    matcher = difflib.SequenceMatcher(None, expected_norm, actual_norm, autojunk=False)
    matched = sum(block.size for block in matcher.get_matching_blocks())
    return matcher.ratio(), matched / len(expected_norm)


def _cached_whisper_model() -> str | None:
    root = Path.home() / ".cache" / "huggingface" / "hub" / "models--Systran--faster-whisper-small" / "snapshots"
    try:
        snapshots = [path for path in root.glob("*") if path.is_dir()]
    except OSError:
        snapshots = []
    return str(max(snapshots, key=lambda path: path.stat().st_mtime)) if snapshots else None


def list_local_whisper_models() -> list[dict]:
    """List local snapshots only; re-analysis never downloads a model."""
    root = Path.home() / ".cache" / "huggingface" / "hub"
    options: list[dict] = []
    try:
        repositories = sorted(path for path in root.glob("models--Systran--faster-whisper-*") if path.is_dir())
    except OSError:
        repositories = []
    for repository in repositories:
        try:
            snapshots = [path for path in (repository / "snapshots").glob("*") if path.is_dir()]
        except OSError:
            snapshots = []
        if not snapshots:
            continue
        selected = max(snapshots, key=lambda path: path.stat().st_mtime)
        options.append({
            "id": str(selected),
            "label": repository.name.removeprefix("models--Systran--"),
            "path": str(selected),
        })
    return options


def _load_whisper(model: str | None = None):
    try:
        from faster_whisper import WhisperModel
    except ModuleNotFoundError as exc:
        raise AvatarImportError("当前 Python 环境未安装 faster-whisper，无法执行台词核验") from exc
    selected = model or _cached_whisper_model()
    if not selected:
        raise AvatarImportError("未找到本地 faster-whisper-small 模型；为避免后台静默下载，本次任务已停止")
    if model and not Path(selected).is_dir():
        raise AvatarImportError("只能选择已经安装在本机的 ASR 模型；系统不会在后台下载模型")
    return WhisperModel(selected, device="cpu", compute_type="int8"), selected


def preflight_local_whisper(model: str | None = None, *, load_test: bool = True) -> dict[str, Any]:
    """Verify the exact local-only ASR runtime before any paid avatar submit.

    The returned evidence is deliberately path-free so it is safe to persist in
    a project job or expose in the workbench.  The snapshot directory remains a
    machine-local implementation detail and is resolved again on resume.
    """

    options = list_local_whisper_models()
    if not options:
        raise AvatarImportError("本机没有已安装的 faster-whisper 模型；付费数字人任务未启动")
    selected = next((item for item in options if model and item.get("id") == model), None)
    if selected is None and model:
        raise AvatarImportError("指定的 faster-whisper 模型不在本机白名单中；系统不会后台下载")
    selected = selected or next(
        (item for item in options if "small" in str(item.get("label") or "").lower()),
        options[0],
    )
    root = Path(str(selected["id"]))
    model_file = root / "model.bin"
    if not model_file.is_file() or model_file.stat().st_size < 1024 * 1024:
        raise AvatarImportError("本地 faster-whisper 模型核心文件缺失或异常；付费数字人任务未启动")
    inventory = []
    for path in sorted(item for item in root.iterdir() if item.is_file()):
        inventory.append({"name": path.name, "size": path.stat().st_size})
    fingerprint = hashlib.sha256(
        json.dumps(inventory, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    try:
        faster_whisper_version = importlib_metadata.version("faster-whisper")
        ctranslate2_version = importlib_metadata.version("ctranslate2")
    except importlib_metadata.PackageNotFoundError as exc:
        raise AvatarImportError("当前项目虚拟环境缺少 faster-whisper/ctranslate2；付费数字人任务未启动") from exc
    if load_test:
        _load_whisper(str(root))
    return {
        "status": "passed",
        "model_id": str(selected.get("label") or "faster-whisper-local"),
        "snapshot_revision": root.name,
        "fingerprint": fingerprint,
        "model_size_bytes": model_file.stat().st_size,
        "faster_whisper_version": faster_whisper_version,
        "ctranslate2_version": ctranslate2_version,
        "device": "cpu",
        "compute_type": "int8",
        "language": "zh",
        "local_only": True,
        "load_tested": bool(load_test),
    }


def _transcribe_file(
    model: Any,
    path: Path,
    *,
    word_timestamps: bool = False,
    beam_size: int = 5,
    vad_filter: bool = True,
) -> tuple[str, list[dict]]:
    segments, _info = model.transcribe(
        str(path), language="zh", vad_filter=vad_filter, word_timestamps=word_timestamps, beam_size=beam_size,
    )
    text_parts: list[str] = []
    serialised: list[dict] = []
    for segment in segments:
        text_parts.append(segment.text)
        item = {"start": float(segment.start), "end": float(segment.end), "text": segment.text}
        if word_timestamps:
            item["words"] = [
                {"start": float(word.start), "end": float(word.end), "word": word.word}
                for word in (segment.words or []) if word.start is not None and word.end is not None
            ]
        serialised.append(item)
    return "".join(text_parts).strip(), serialised


def start_avatar_asr(project_dir: Path, payload: dict | None = None) -> dict:
    package = validate_avatar_package(project_dir)
    if package["validation"]["status"] not in {"passed", "passed_with_warnings"}:
        raise AvatarImportError("数字人素材媒体检查未通过，不能开始 ASR")
    if package["asr"]["status"] == "running":
        raise AvatarImportError("数字人台词核验正在运行")
    timing_manifest = copy.deepcopy((package.get("asr") or {}).get("summary", {}).get("timing_manifest"))
    package["asr"] = {"status": "running", "started_at": _now(), "issues": [], "summary": {"completed": 0, "total": len(package["turns"])} }
    if timing_manifest:
        package["asr"]["summary"]["timing_manifest"] = timing_manifest
    if package["import_mode"] == "longform":
        package["cut_plan"] = {"status": "not_started", "items": [], "summary": {}}
    package["assembly"] = {"status": "not_started", "issues": [], "summary": {}}
    return _save_package(project_dir, package)


def _character_tokens(segments: list[dict]) -> tuple[list[str], list[tuple[float, float]]]:
    chars: list[str] = []
    times: list[tuple[float, float]] = []
    for segment in segments:
        for word in segment.get("words", []):
            normalized = normalize_text(_simplify_chinese(str(word.get("word") or "")))
            if not normalized:
                continue
            start, end = float(word["start"]), float(word["end"])
            span = max(0.001, end - start)
            for index, char in enumerate(normalized):
                chars.append(char)
                times.append((start + span * index / len(normalized), start + span * (index + 1) / len(normalized)))
    return chars, times


def _read_pcm(path: Path, sample_rate: int = 16000):
    try:
        import numpy as np
    except ModuleNotFoundError:
        return None
    ffmpeg = _find_binary("ffmpeg")
    if not ffmpeg:
        return None
    result = subprocess.run(
        [ffmpeg, "-v", "error", "-i", str(path), "-map", "0:a:0", "-ac", "1", "-ar", str(sample_rate), "-f", "f32le", "pipe:1"],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    return np.frombuffer(result.stdout, dtype="<f4")


def _quiet_boundary(audio: Any, left: float, right: float, sample_rate: int = 16000) -> float:
    midpoint = (left + right) / 2.0
    if audio is None or right <= left + 0.04:
        return midpoint
    import numpy as np
    window = max(1, round(sample_rate * 0.08))
    step = max(1, round(sample_rate * 0.01))
    lo = max(0, round(left * sample_rate))
    hi = min(len(audio), round(right * sample_rate))
    candidates: list[tuple[float, float]] = []
    for start in range(lo, max(lo + 1, hi - window + 1), step):
        chunk = audio[start:start + window]
        if len(chunk) < window // 2:
            continue
        rms = float(np.sqrt(np.mean(chunk * chunk)))
        center = (start + len(chunk) / 2) / sample_rate
        candidates.append((rms, center))
    if not candidates:
        return midpoint
    minimum = min(item[0] for item in candidates)
    quiet = [item for item in candidates if item[0] <= minimum * 1.08 + 1e-7]
    return min(quiet, key=lambda item: abs(item[1] - midpoint))[1]


def _align_longform_turns_legacy(project_dir: Path, package: dict, transcripts: dict[str, dict]) -> list[dict]:
    issues: list[dict] = []
    for speaker in package["speakers"]:
        speaker_id = speaker["speaker_id"]
        speaker_turns = [turn for turn in package["turns"] if turn["speaker_id"] == speaker_id]
        source_record = transcripts[speaker_id]
        asr_chars, asr_times = _character_tokens(source_record["segments"])
        script_chars: list[str] = []
        ranges: dict[str, tuple[int, int]] = {}
        for turn in speaker_turns:
            start = len(script_chars)
            script_chars.extend(normalize_text(_simplify_chinese(turn["text"])))
            ranges[turn["turn_id"]] = (start, len(script_chars))
        matcher = difflib.SequenceMatcher(None, script_chars, asr_chars, autojunk=False)
        mapping: dict[int, int] = {}
        for script_start, asr_start, size in matcher.get_matching_blocks():
            for offset in range(size):
                mapping[script_start + offset] = asr_start + offset
        voice_ranges: list[tuple[dict, float, float, float]] = []
        for turn in speaker_turns:
            start, end = ranges[turn["turn_id"]]
            hits = [mapping[index] for index in range(start, end) if index in mapping]
            coverage = len(hits) / max(1, end - start)
            if not hits or coverage < package["settings"]["minimum_turn_coverage"]:
                issues.append(_issue("longform_alignment_failed", f"{turn['turn_id']} 与 {speaker['name']} 长视频覆盖率仅 {coverage:.3f}", turn_id=turn["turn_id"]))
                continue
            actual = "".join(asr_chars[min(hits):max(hits) + 1])
            similarity, _ = text_metrics(turn["text"], actual)
            turn["transcript"] = actual
            turn["asr_similarity"] = round(similarity, 4)
            turn["asr_coverage"] = round(coverage, 4)
            if similarity < package["settings"].get("minimum_turn_similarity", 0.86):
                issues.append(_issue("longform_turn_similarity_failed", f"{turn['turn_id']} 台词相似度仅 {similarity:.3f}", turn_id=turn["turn_id"]))
                continue
            voice_ranges.append((turn, min(asr_times[index][0] for index in hits), max(asr_times[index][1] for index in hits), coverage))
        if len(voice_ranges) != len(speaker_turns):
            continue
        source_path = _safe_project_file(project_dir, speaker["source"]["path"])
        duration = float(speaker["source"]["media"]["duration_seconds"])
        audio = _read_pcm(source_path)
        boundaries = [max(0.0, voice_ranges[0][1] - 0.12)]
        for previous, current in zip(voice_ranges, voice_ranges[1:]):
            boundaries.append(_quiet_boundary(audio, previous[2], current[1]))
        boundaries.append(min(duration, voice_ranges[-1][2] + 0.18))
        for index, (turn, _start, _end, _coverage) in enumerate(voice_ranges):
            turn["source_start_seconds"] = round(boundaries[index], 4)
            turn["source_end_seconds"] = round(boundaries[index + 1], 4)
            turn["status"] = "asr_passed"
    return issues


def _cut_confidence(similarity: float, coverage: float) -> str:
    if similarity >= 0.95 and coverage >= 0.94:
        return "high"
    if similarity >= 0.90 and coverage >= 0.88:
        return "medium"
    return "low"


def _strict_manifest_int(value: Any, label: str) -> int:
    """Accept only JSON integers for the exact-clock contract.

    ``int(30.5)`` silently becoming ``30`` would make a malformed external
    manifest look frame-exact.  The v2 contract is deliberately stricter than
    the legacy manifest, so reject booleans, numeric strings and fractional
    values rather than coercing them.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise AvatarImportError(f"{label} 必须是整数")
    return value


def apply_longform_timing_manifest(project_dir: Path, manifest: dict) -> dict:
    """Persist deterministic turn boundaries without claiming an ASR result.

    The full generation ledger lives in the parent job and its JSON artifact;
    the package keeps the review input under the extensible ASR summary until
    Whisper verifies it.  Existing packages with no manifest remain on the
    legacy ASR alignment route.
    """
    package = read_avatar_package(project_dir)
    if not package or package.get("import_mode") != "longform":
        raise AvatarImportError("确定性时间清单只能应用于长视频数字人素材包")
    raw_turns = manifest.get("turns") if isinstance(manifest.get("turns"), list) else []
    by_id = {str(item.get("turn_id") or "").upper(): item for item in raw_turns if isinstance(item, dict)}
    if len(raw_turns) != len(by_id):
        raise AvatarImportError("逐轮时间清单包含重复或无效的轮次编号")
    if set(by_id) != {turn["turn_id"] for turn in package["turns"]}:
        raise AvatarImportError("逐轮时间清单与当前脚本轮次不一致")
    version = str(manifest.get("version") or "")
    exact_clock = version == "avatar-turn-timing-v2"
    role_clocks = manifest.get("roles") if isinstance(manifest.get("roles"), dict) else {}
    video_fps = 0
    if exact_clock:
        contract = manifest.get("contract") if isinstance(manifest.get("contract"), dict) else {}
        video_fps = int(contract.get("video_fps") or 0)
        if video_fps != 25 or str(contract.get("frame_alignment") or "") != "final_role_track_once":
            raise AvatarImportError("精确帧时间清单必须使用 25FPS 整条音频一次对齐合同")
        expected_roles = {turn["speaker_id"] for turn in package["turns"]}
        if set(role_clocks) != expected_roles:
            raise AvatarImportError("精确帧时间清单的角色音频账本不完整")
        for speaker_id, role_clock in role_clocks.items():
            if not isinstance(role_clock, dict):
                raise AvatarImportError(f"{speaker_id} 的精确帧音频账本无效")
            try:
                sample_rate = _strict_manifest_int(role_clock["sample_rate"], f"{speaker_id} sample_rate")
                channels = _strict_manifest_int(role_clock["channels"], f"{speaker_id} channels")
                sample_width = _strict_manifest_int(role_clock["sample_width"], f"{speaker_id} sample_width")
                role_video_fps = _strict_manifest_int(role_clock["video_fps"], f"{speaker_id} video_fps")
                samples_per_frame = _strict_manifest_int(role_clock["samples_per_video_frame"], f"{speaker_id} samples_per_video_frame")
                video_frames = _strict_manifest_int(role_clock["video_frame_count"], f"{speaker_id} video_frame_count")
                sample_frames = _strict_manifest_int(role_clock["sample_frame_count"], f"{speaker_id} sample_frame_count")
                content_frames = _strict_manifest_int(role_clock["content_sample_frames"], f"{speaker_id} content_sample_frames")
                padding_frames = _strict_manifest_int(role_clock["final_padding_sample_frames"], f"{speaker_id} final_padding_sample_frames")
            except KeyError as exc:
                raise AvatarImportError(f"{speaker_id} 的精确帧音频账本缺少整数采样证据") from exc
            if (
                sample_rate <= 0
                or channels != 1
                or sample_width != 2
                or role_video_fps != video_fps
                or sample_rate != samples_per_frame * video_fps
                or video_frames <= 0
                or sample_frames != video_frames * samples_per_frame
                or content_frames + padding_frames != sample_frames
                or not 0 <= padding_frames < samples_per_frame
            ):
                raise AvatarImportError(f"{speaker_id} 的音频采样数与视频总帧数不一致")
            if abs(float(role_clock.get("duration_seconds") or 0) - video_frames / video_fps) > 1e-6:
                raise AvatarImportError(f"{speaker_id} 的精确帧音频时长账本不一致")
    previous_end: dict[str, float] = {}
    previous_end_frame: dict[str, int] = {}
    role_turns_seen: dict[str, int] = {}
    for turn in package["turns"]:
        item = by_id[turn["turn_id"]]
        if str(item.get("speaker_id") or "").lower() != turn["speaker_id"]:
            raise AvatarImportError(f"{turn['turn_id']} 的时间清单说话人与脚本不一致")
        start = float(item.get("source_start_seconds"))
        speech_start = float(item.get("speech_start_seconds"))
        speech_end = float(item.get("speech_end_seconds"))
        end = float(item.get("source_end_seconds"))
        if not (0 <= start <= speech_start < speech_end <= end):
            raise AvatarImportError(f"{turn['turn_id']} 的确定性时间范围无效")
        if start < previous_end.get(turn["speaker_id"], 0.0) - 0.001:
            raise AvatarImportError(f"{turn['turn_id']} 的确定性时间范围与同角色上一轮重叠")
        if str(item.get("text_sha256") or "") != hashlib.sha256(turn["text"].encode("utf-8")).hexdigest():
            raise AvatarImportError(f"{turn['turn_id']} 的时间清单文本签名已漂移")
        if exact_clock:
            speaker_id = turn["speaker_id"]
            role_clock = role_clocks[speaker_id]
            samples_per_frame = int(role_clock["samples_per_video_frame"])
            role_total_frames = int(role_clock["video_frame_count"])
            try:
                turn_sample_rate = _strict_manifest_int(item["sample_rate"], f"{turn['turn_id']} sample_rate")
                start_frame = _strict_manifest_int(item["source_start_frame"], f"{turn['turn_id']} source_start_frame")
                end_frame = _strict_manifest_int(item["source_end_frame_exclusive"], f"{turn['turn_id']} source_end_frame_exclusive")
                start_sample = _strict_manifest_int(item["source_start_sample"], f"{turn['turn_id']} source_start_sample")
                end_sample = _strict_manifest_int(item["source_end_sample"], f"{turn['turn_id']} source_end_sample")
                speech_start_sample = _strict_manifest_int(item["speech_start_sample"], f"{turn['turn_id']} speech_start_sample")
                speech_end_sample = _strict_manifest_int(item["speech_end_sample"], f"{turn['turn_id']} speech_end_sample")
            except KeyError as exc:
                raise AvatarImportError(f"{turn['turn_id']} 缺少精确的采样或帧边界") from exc
            if (
                turn_sample_rate != int(role_clock["sample_rate"])
                or not 0 <= start_frame < end_frame <= role_total_frames
                or start_sample != start_frame * samples_per_frame
                or end_sample != end_frame * samples_per_frame
                or not start_sample <= speech_start_sample < speech_end_sample <= end_sample
                or abs(start - start_frame / video_fps) > 1e-6
                or abs(end - end_frame / video_fps) > 1e-6
                or abs(speech_start - speech_start_sample / turn_sample_rate) > 1e-6
                or abs(speech_end - speech_end_sample / turn_sample_rate) > 1e-6
            ):
                raise AvatarImportError(f"{turn['turn_id']} 的精确采样边界与 25FPS 帧边界不一致")
            seen = role_turns_seen.get(speaker_id, 0)
            if seen == 0 and start_frame != 0:
                raise AvatarImportError(f"{turn['turn_id']} 所在角色的首个切点必须从第 0 帧开始")
            if seen and start_frame != previous_end_frame[speaker_id]:
                raise AvatarImportError(f"{turn['turn_id']} 与同角色上一轮没有共享同一个帧边界")
            previous_end_frame[speaker_id] = end_frame
            role_turns_seen[speaker_id] = seen + 1
        previous_end[turn["speaker_id"]] = end
    if exact_clock:
        for speaker_id, role_clock in role_clocks.items():
            if previous_end_frame.get(speaker_id) != int(role_clock["video_frame_count"]):
                raise AvatarImportError(f"{speaker_id} 的最后一轮没有覆盖到角色音频末帧")
    package.setdefault("asr", {}).setdefault("summary", {})["timing_manifest"] = {
        "version": version,
        "path": str(manifest.get("path") or ""),
        "sha256": str(manifest.get("sha256") or ""),
        "input_signature": str(manifest.get("input_signature") or ""),
        "contract": copy.deepcopy(manifest.get("contract") or {}),
        "roles": copy.deepcopy(role_clocks),
        "turns": copy.deepcopy(raw_turns),
    }
    package["cut_plan"] = {"status": "not_started", "items": [], "summary": {"source": "deterministic_timing_manifest"}}
    package["assembly"] = {"status": "not_started", "issues": [], "summary": {}}
    return _save_package(project_dir, package)


def ensure_exact_clock_assembly_duration_limit(
    project_dir: Path,
    *,
    maximum_seconds: float,
    tolerance_seconds: float = 1.0,
) -> dict:
    """Reserve enough local master duration for a validated v2 timing manifest.

    The one-click RunningHub route creates one source video per presenter and
    then interleaves their frame-exact turns locally. Its default 120-second
    import limit predates that route and must not turn a valid, already-paid
    121--180 second master into a false failure. This function is deliberately
    limited to the validated v2 contract: it never estimates, trims, stretches,
    or changes legacy/ASR-derived cuts.
    """
    package = read_avatar_package(project_dir)
    if not package:
        raise AvatarImportError("数字人素材包不存在")
    manifest = (((package.get("asr") or {}).get("summary") or {}).get("timing_manifest") or {})
    if str(manifest.get("version") or "") != "avatar-turn-timing-v2":
        return package
    if package.get("import_mode") != "longform":
        raise AvatarImportError("精确帧时长预算只能用于长视频数字人素材包")
    if maximum_seconds <= 0 or tolerance_seconds < 0:
        raise AvatarImportError("精确帧时长预算参数无效")

    turns = package.get("turns") or []
    if not turns:
        raise AvatarImportError("数字人素材包没有可合成的台词")
    planned_duration = 0.0
    previous_speaker_id: str | None = None
    settings = package.get("settings") or {}
    for turn in turns:
        try:
            start = float(turn["source_start_seconds"])
            end = float(turn["source_end_seconds"])
        except (KeyError, TypeError, ValueError) as exc:
            raise AvatarImportError(f"{turn.get('turn_id') or '未知轮次'} 缺少精确帧切割边界") from exc
        if not 0 <= start < end:
            raise AvatarImportError(f"{turn.get('turn_id') or '未知轮次'} 的精确帧切割边界无效")
        speaker_id = str(turn.get("speaker_id") or "")
        if previous_speaker_id is not None:
            gap_key = "same_speaker_gap_seconds" if speaker_id == previous_speaker_id else "speaker_change_gap_seconds"
            try:
                gap = float(settings.get(gap_key) or 0)
            except (TypeError, ValueError) as exc:
                raise AvatarImportError("数字人切换静音合同无效") from exc
            if gap < 0:
                raise AvatarImportError("数字人切换静音合同不能为负数")
            planned_duration += gap
        planned_duration += end - start
        previous_speaker_id = speaker_id

    if planned_duration > maximum_seconds + 1e-6:
        raise AvatarImportError(
            f"精确帧清单预计母版 {planned_duration:.2f} 秒，超过一键数字人 {maximum_seconds:.2f} 秒安全上限"
        )
    required_limit = math.ceil((planned_duration + tolerance_seconds) * 1000) / 1000
    current_limit = float(settings.get("max_duration_seconds") or 0)
    if current_limit + 1e-6 >= required_limit:
        return package
    package["settings"]["max_duration_seconds"] = required_limit
    return _save_package(project_dir, package)


def _review_deterministic_longform_turns(package: dict, transcripts: dict[str, dict], manifest: dict) -> list[dict]:
    """Review deterministic boundaries without letting ASR rewrite v2 cuts.

    The v1 manifest predates the sample/frame ledger and therefore still uses
    Whisper as a human-review gate.  A validated v2 manifest already proves
    every cut against one final PCM role track and the exact video frame clock;
    Whisper is diagnostic evidence only for that route.
    """
    issues: list[dict] = []
    items: list[dict] = []
    exact_clock = str(manifest.get("version") or "") == "avatar-turn-timing-v2"
    by_id = {str(item["turn_id"]).upper(): item for item in manifest.get("turns") or []}
    for speaker in package["speakers"]:
        speaker_id = speaker["speaker_id"]
        speaker_turns = [turn for turn in package["turns"] if turn["speaker_id"] == speaker_id]
        transcript_record = transcripts.get(speaker_id) if isinstance(transcripts, dict) else None
        segments = (transcript_record or {}).get("segments") if isinstance(transcript_record, dict) else []
        asr_chars, asr_times = _character_tokens(segments or [])
        script_chars: list[str] = []
        ranges: dict[str, tuple[int, int]] = {}
        for turn in speaker_turns:
            start = len(script_chars)
            script_chars.extend(normalize_text(_simplify_chinese(turn["text"])))
            ranges[turn["turn_id"]] = (start, len(script_chars))
        matcher = difflib.SequenceMatcher(None, script_chars, asr_chars, autojunk=False)
        mapping = {left + offset: right + offset for left, right, size in matcher.get_matching_blocks() for offset in range(size)}
        for turn in speaker_turns:
            timing = by_id[turn["turn_id"]]
            start, end = ranges[turn["turn_id"]]
            hits = [mapping[index] for index in range(start, end) if index in mapping and mapping[index] < len(asr_times)]
            coverage = len(hits) / max(1, end - start)
            actual = "".join(asr_chars[min(hits):max(hits) + 1]) if hits else ""
            similarity, _ = text_metrics(turn["text"], actual)
            observed_start = min((asr_times[index][0] for index in hits), default=None)
            observed_end = max((asr_times[index][1] for index in hits), default=None)
            drift = max(
                abs(observed_start - float(timing["speech_start_seconds"])) if observed_start is not None else float("inf"),
                abs(observed_end - float(timing["speech_end_seconds"])) if observed_end is not None else float("inf"),
            )
            diagnostic_passed = coverage >= package["settings"]["minimum_turn_coverage"] and similarity >= package["settings"].get("minimum_turn_similarity", 0.86) and drift <= 0.35
            approved = exact_clock or diagnostic_passed
            if exact_clock:
                fps = int(((manifest.get("contract") or {}).get("video_fps") or 25))
                suggested_start = int(timing["source_start_frame"]) / fps
                suggested_end = int(timing["source_end_frame_exclusive"]) / fps
            else:
                suggested_start = float(timing["source_start_seconds"])
                suggested_end = float(timing["source_end_seconds"])
            item = {
                "turn_id": turn["turn_id"], "speaker_id": speaker_id,
                "status": "approved" if approved else "needs_manual",
                "confidence": "exact_clock" if exact_clock else "high" if diagnostic_passed else "low",
                "source_type": "deterministic_timing_manifest" if exact_clock else "manual",
                "suggested_start_seconds": suggested_start,
                "suggested_end_seconds": suggested_end,
                "start_seconds": suggested_start,
                "end_seconds": suggested_end,
                "transcript": actual, "asr_similarity": round(similarity, 4), "asr_coverage": round(coverage, 4),
                "review_note": (
                    f"精确帧清单为切割主合同；Whisper 诊断边界最大偏差 {drift:.3f} 秒"
                    if exact_clock and drift != float("inf")
                    else "精确帧清单为切割主合同；Whisper 未定位到台词边界，仅记录提醒"
                    if exact_clock
                    else f"确定性时间清单；Whisper 边界最大偏差 {drift:.3f} 秒"
                    if drift != float("inf")
                    else "确定性时间清单；Whisper 未定位到台词边界"
                ),
                "updated_at": _now(),
            }
            if approved:
                item["approved_at"] = _now()
                turn["source_start_seconds"] = suggested_start
                turn["source_end_seconds"] = suggested_end
                turn["status"] = "cut_approved"
            else:
                turn["status"] = "cut_pending_review"
                issues.append(_issue("deterministic_timing_review_needs_manual", f"{turn['turn_id']} 的 Whisper 复核未通过（覆盖率 {coverage:.3f}、相似度 {similarity:.3f}、边界偏差 {drift:.3f} 秒）", severity="warning", turn_id=turn["turn_id"]))
            if exact_clock and not diagnostic_passed:
                drift_text = f"{drift:.3f} 秒" if drift != float("inf") else "未定位"
                issues.append(_issue(
                    "exact_clock_asr_diagnostic_warning",
                    f"{turn['turn_id']} 的 Whisper 诊断未达门槛（覆盖率 {coverage:.3f}、相似度 {similarity:.3f}、边界偏差 {drift_text}）；仍按已校验的精确帧清单切割",
                    severity="warning",
                    turn_id=turn["turn_id"],
                ))
            turn["transcript"] = actual
            turn["asr_similarity"] = round(similarity, 4)
            turn["asr_coverage"] = round(coverage, 4)
            items.append(item)
    order = {turn["turn_id"]: turn["index"] for turn in package["turns"]}
    items.sort(key=lambda item: order[item["turn_id"]])
    approved = sum(item["status"] == "approved" for item in items)
    manual = len(items) - approved
    diagnostic_warnings = sum(item.get("code") == "exact_clock_asr_diagnostic_warning" for item in issues)
    package["cut_plan"] = {
        "status": "approved" if not manual else "needs_attention", "generated_at": _now(), "items": items,
        "summary": {
            "total": len(items), "approved": approved, "pending_review": 0,
            "needs_manual": manual, "diagnostic_warnings": diagnostic_warnings,
            "source": "deterministic_timing_manifest", "manifest_version": manifest.get("version"),
            "cut_authority": "exact_frame_manifest" if exact_clock else "whisper_review",
        },
    }
    return issues


def approve_exact_clock_manifest_cuts(
    project_dir: Path,
    *,
    diagnostic_error: object | None = None,
    model_name: str | None = None,
) -> dict:
    """Approve validated v2 frame cuts when Whisper is unavailable.

    This is intentionally unavailable to legacy manifests.  It does not infer
    any boundary: it merely materialises the already validated integer frame
    ranges, while retaining the ASR failure as non-blocking diagnostics.
    """
    package = read_avatar_package(project_dir)
    if not package or package.get("import_mode") != "longform":
        raise AvatarImportError("当前项目没有可应用的长视频精确帧清单")
    asr_before = package.get("asr") if isinstance(package.get("asr"), dict) else {}
    manifest = copy.deepcopy((asr_before.get("summary") or {}).get("timing_manifest"))
    if not isinstance(manifest, dict) or str(manifest.get("version") or "") != "avatar-turn-timing-v2":
        raise AvatarImportError("只有通过校验的 v2 精确帧清单可以绕过 Whisper 切点门")
    empty_transcripts = {
        speaker["speaker_id"]: {"text": "", "segments": []}
        for speaker in package.get("speakers") or []
    }
    issues = _review_deterministic_longform_turns(package, empty_transcripts, manifest)
    if diagnostic_error is not None:
        message = str(diagnostic_error).strip() or diagnostic_error.__class__.__name__
        issues.insert(0, _issue(
            "exact_clock_asr_unavailable",
            f"Whisper 诊断未完成：{message[:500]}；切割继续使用已校验的精确帧清单",
            severity="warning",
        ))
    package["asr"] = {
        "status": "passed",
        "started_at": asr_before.get("started_at", _now()),
        "finished_at": _now(),
        "issues": issues,
        "summary": {
            "completed": 0,
            "total": len(package.get("turns") or []),
            "model": model_name or str((asr_before.get("summary") or {}).get("model") or "unavailable"),
            "minimum_coverage": 0,
            "average_similarity": 0,
            "diagnostic_only": True,
            "diagnostic_status": "unavailable" if diagnostic_error is not None else "not_run",
            "alignment_source": "deterministic_timing_manifest",
            "timing_manifest": manifest,
        },
    }
    package["assembly"] = {"status": "not_started", "issues": [], "summary": {}}
    return _save_package(project_dir, package)


def _align_longform_turns(project_dir: Path, package: dict, transcripts: dict[str, dict]) -> list[dict]:
    """Build an editable cut plan instead of silently accepting ASR boundaries."""
    issues: list[dict] = []
    candidates_by_speaker: dict[str, list[dict]] = {}
    items: list[dict] = []
    for speaker in package["speakers"]:
        speaker_id = speaker["speaker_id"]
        speaker_turns = [turn for turn in package["turns"] if turn["speaker_id"] == speaker_id]
        asr_chars, asr_times = _character_tokens(transcripts[speaker_id]["segments"])
        script_chars: list[str] = []
        ranges: dict[str, tuple[int, int]] = {}
        for turn in speaker_turns:
            start = len(script_chars)
            script_chars.extend(normalize_text(_simplify_chinese(turn["text"])))
            ranges[turn["turn_id"]] = (start, len(script_chars))
        matcher = difflib.SequenceMatcher(None, script_chars, asr_chars, autojunk=False)
        mapping = {script_start + offset: asr_start + offset for script_start, asr_start, size in matcher.get_matching_blocks() for offset in range(size)}
        candidates: list[dict] = []
        for turn in speaker_turns:
            start, end = ranges[turn["turn_id"]]
            hits = [mapping[index] for index in range(start, end) if index in mapping]
            coverage = len(hits) / max(1, end - start)
            item = {
                "turn_id": turn["turn_id"], "speaker_id": speaker_id, "status": "needs_manual",
                "confidence": "low", "source_type": "asr_alignment", "suggested_start_seconds": None,
                "suggested_end_seconds": None, "start_seconds": None, "end_seconds": None,
                "asr_coverage": round(coverage, 4), "review_note": "", "updated_at": _now(),
            }
            if not hits or not asr_times or coverage < package["settings"]["minimum_turn_coverage"]:
                turn["status"] = "asr_failed"
                turn.pop("source_start_seconds", None)
                turn.pop("source_end_seconds", None)
                item["review_note"] = "自动识别未覆盖足够台词，请手动填写该段起止时间后审核。"
                issues.append(_issue("longform_alignment_needs_manual", f"{turn['turn_id']} 在 {speaker['name']} 长视频覆盖率仅 {coverage:.3f}", severity="warning", turn_id=turn["turn_id"]))
            else:
                actual = "".join(asr_chars[min(hits):max(hits) + 1])
                similarity, _ = text_metrics(turn["text"], actual)
                turn["transcript"] = actual
                turn["asr_similarity"] = round(similarity, 4)
                turn["asr_coverage"] = round(coverage, 4)
                item.update({"transcript": actual, "asr_similarity": round(similarity, 4)})
                if similarity < package["settings"].get("minimum_turn_similarity", 0.86):
                    turn["status"] = "asr_failed"
                    item["review_note"] = "识别文字与脚本差异较大，请手动填写起止时间并核对原片。"
                    issues.append(_issue("longform_turn_similarity_needs_manual", f"{turn['turn_id']} 台词相似度仅 {similarity:.3f}", severity="warning", turn_id=turn["turn_id"]))
                else:
                    candidates.append({"turn": turn, "item": item, "start": min(asr_times[index][0] for index in hits), "end": max(asr_times[index][1] for index in hits), "similarity": similarity, "coverage": coverage})
            items.append(item)
        candidates_by_speaker[speaker_id] = candidates
        if not candidates:
            continue
        source = _safe_project_file(project_dir, speaker["source"]["path"])
        duration = float(speaker["source"]["media"]["duration_seconds"])
        audio = _read_pcm(source)
        boundaries = [max(0.0, candidates[0]["start"] - 0.12)]
        boundaries.extend(_quiet_boundary(audio, previous["end"], current["start"]) for previous, current in zip(candidates, candidates[1:]))
        boundaries.append(min(duration, candidates[-1]["end"] + 0.18))
        for index, candidate in enumerate(candidates):
            turn, item = candidate["turn"], candidate["item"]
            start_seconds, end_seconds = round(boundaries[index], 4), round(boundaries[index + 1], 4)
            item.update({
                "status": "pending_review", "confidence": _cut_confidence(candidate["similarity"], candidate["coverage"]),
                "suggested_start_seconds": start_seconds, "suggested_end_seconds": end_seconds,
                "start_seconds": start_seconds, "end_seconds": end_seconds,
            })
            turn["status"] = "cut_pending_review"
            turn.pop("source_start_seconds", None)
            turn.pop("source_end_seconds", None)
    turn_order = {turn["turn_id"]: turn["index"] for turn in package["turns"]}
    items.sort(key=lambda item: turn_order[item["turn_id"]])
    pending = sum(item["status"] == "pending_review" for item in items)
    manual = sum(item["status"] == "needs_manual" for item in items)
    package["cut_plan"] = {
        "status": "needs_attention" if manual else "awaiting_review",
        "generated_at": _now(), "items": items,
        "summary": {"total": len(items), "approved": 0, "pending_review": pending, "needs_manual": manual},
    }
    return issues


def _speaker_candidate_from_items(
    package: dict,
    speaker_id: str,
    transcript_record: dict,
    items: list[dict],
    issues: list[dict],
    *,
    candidate_id: str,
    model_name: str,
    kind: str,
    transcription_options: dict,
) -> dict:
    """Create an immutable, review-only ASR candidate for one speaker."""
    speaker = _find_speaker(package, speaker_id)
    turns = [turn for turn in package["turns"] if turn["speaker_id"] == speaker_id]
    item_by_turn = {item["turn_id"]: item for item in items}
    script_text = "".join(str(turn["text"]) for turn in turns)
    actual_text = str(transcript_record.get("text") or "")
    similarity, coverage = text_metrics(script_text, actual_text)
    diagnostic_turns: list[dict] = []
    for turn in turns:
        item = copy.deepcopy(item_by_turn.get(turn["turn_id"], {}))
        requires_manual = item.get("status") == "needs_manual"
        diagnostic_turns.append({
            "turn_id": turn["turn_id"],
            "script_text": turn["text"],
            "transcript": item.get("transcript", ""),
            "asr_similarity": item.get("asr_similarity"),
            "asr_coverage": item.get("asr_coverage", 0),
            "status": item.get("status", "needs_manual"),
            "reason": item.get("review_note") or ("未找到可用切点，需要人工定位" if requires_manual else "已生成候选切点，等待审核"),
            "suggested_start_seconds": item.get("suggested_start_seconds"),
            "suggested_end_seconds": item.get("suggested_end_seconds"),
        })
    ready = sum(item.get("status") == "pending_review" for item in items)
    manual = sum(item.get("status") == "needs_manual" for item in items)
    return {
        "candidate_id": candidate_id,
        "kind": kind,
        "status": "ready",
        "created_at": _now(),
        "model": model_name,
        "transcription_options": transcription_options,
        "source_sha256": (speaker.get("source") or {}).get("sha256"),
        "source_path": (speaker.get("source") or {}).get("path"),
        "full_transcript": actual_text,
        "segments": copy.deepcopy(transcript_record.get("segments") or []),
        "overall_metrics": {"similarity": round(similarity, 4), "coverage": round(coverage, 4)},
        "turns": diagnostic_turns,
        "cut_items": copy.deepcopy(items),
        "issues": copy.deepcopy(issues),
        "summary": {"total": len(items), "pending_review": ready, "needs_manual": manual},
    }


def _build_longform_speaker_candidate(
    project_dir: Path,
    package: dict,
    speaker_id: str,
    transcript_record: dict,
    *,
    candidate_id: str,
    model_name: str,
    kind: str,
    transcription_options: dict,
) -> dict:
    """Align a speaker in an isolated copy so active cuts cannot be changed."""
    scratch = copy.deepcopy(package)
    scratch["speakers"] = [copy.deepcopy(_find_speaker(package, speaker_id))]
    scratch["turns"] = [copy.deepcopy(turn) for turn in package["turns"] if turn["speaker_id"] == speaker_id]
    scratch["cut_plan"] = {"status": "not_started", "items": [], "summary": {}}
    issues = _align_longform_turns(project_dir, scratch, {speaker_id: transcript_record})
    return _speaker_candidate_from_items(
        package,
        speaker_id,
        transcript_record,
        scratch["cut_plan"]["items"],
        issues,
        candidate_id=candidate_id,
        model_name=model_name,
        kind=kind,
        transcription_options=transcription_options,
    )


def _initial_longform_speaker_diagnostics(package: dict, transcripts: dict[str, dict], model_name: str, issues: list[dict]) -> dict:
    diagnostics: dict[str, dict] = {}
    plan_items = (package.get("cut_plan") or {}).get("items") or []
    for speaker in package["speakers"]:
        speaker_id = speaker["speaker_id"]
        speaker_turn_ids = {turn["turn_id"] for turn in package["turns"] if turn["speaker_id"] == speaker_id}
        candidate_id = f"ASRC-{uuid4().hex[:12]}"
        candidate = _speaker_candidate_from_items(
            package,
            speaker_id,
            transcripts[speaker_id],
            [item for item in plan_items if item.get("speaker_id") == speaker_id],
            [item for item in issues if item.get("turn_id") in speaker_turn_ids],
            candidate_id=candidate_id,
            model_name=model_name,
            kind="initial",
            transcription_options={"beam_size": 5, "vad_filter": True},
        )
        diagnostics[speaker_id] = {
            "speaker_id": speaker_id,
            "speaker_name": speaker["name"],
            "status": "completed",
            "active_candidate_id": candidate_id,
            "latest_candidate_id": candidate_id,
            "candidates": [candidate],
            "job": {"status": "completed", "finished_at": _now()},
        }
    return diagnostics


def _require_longform_speaker(project_dir: Path, speaker_id: str) -> tuple[dict, dict]:
    package = read_avatar_package(project_dir)
    if not package or package.get("import_mode") != "longform":
        raise AvatarImportError("当前项目不是本地整段口播切割方案")
    speaker = _find_speaker(package, speaker_id.lower())
    if not speaker.get("source"):
        raise AvatarImportError(f"请先上传 {speaker['name']} 的完整口播原片")
    if (package.get("validation") or {}).get("status") not in {"passed", "passed_with_warnings"}:
        raise AvatarImportError("请先完成原片检查，再进行台词诊断")
    return package, speaker


def start_longform_speaker_diagnosis(project_dir: Path, speaker_id: str, payload: dict | None = None) -> dict:
    package, speaker = _require_longform_speaker(project_dir, speaker_id)
    if package.get("asr", {}).get("status") == "running":
        raise AvatarImportError("全量 ASR 正在运行，请等待完成后再分析单个说话人")
    payload = payload or {}
    requested_model = str(payload.get("model") or "").strip() or None
    if requested_model and requested_model not in {option["id"] for option in list_local_whisper_models()}:
        raise AvatarImportError("只能选择本机已安装的 ASR 模型")
    diagnostics = package.setdefault("asr", {}).setdefault("speaker_diagnostics", {})
    record = diagnostics.setdefault(speaker["speaker_id"], {"speaker_id": speaker["speaker_id"], "speaker_name": speaker["name"], "candidates": []})
    if (record.get("job") or {}).get("status") == "running":
        raise AvatarImportError(f"{speaker['name']} 的诊断任务正在运行")
    candidate_id = f"ASRC-{uuid4().hex[:12]}"
    record["status"] = "running"
    record["job"] = {
        "status": "running", "candidate_id": candidate_id, "started_at": _now(),
        "model": requested_model,
    }
    return _save_package(project_dir, package)


def run_longform_speaker_diagnosis(project_dir: Path, speaker_id: str, payload: dict | None = None) -> dict:
    package, speaker = _require_longform_speaker(project_dir, speaker_id)
    payload = payload or {}
    record = ((package.get("asr") or {}).get("speaker_diagnostics") or {}).get(speaker["speaker_id"])
    job = (record or {}).get("job") or {}
    if job.get("status") != "running" or not job.get("candidate_id"):
        raise AvatarImportError("当前没有待执行的单说话人诊断任务")
    model, model_name = _load_whisper(job.get("model") or None)
    source_path = _safe_project_file(project_dir, speaker["source"]["path"])
    actual, segments = _transcribe_file(
        model, source_path, word_timestamps=True, beam_size=10, vad_filter=False,
    )
    candidate = _build_longform_speaker_candidate(
        project_dir,
        package,
        speaker["speaker_id"],
        {"text": actual, "segments": segments},
        candidate_id=str(job["candidate_id"]),
        model_name=model_name,
        kind="enhanced_diagnosis",
        transcription_options={"beam_size": 10, "vad_filter": False},
    )
    candidates = [item for item in (record.get("candidates") or []) if item.get("candidate_id") != candidate["candidate_id"]]
    # Keep the active baseline plus recent comparison candidates; do not lose a decision record.
    active_id = record.get("active_candidate_id")
    candidates.append(candidate)
    if len(candidates) > 6:
        protected = [item for item in candidates if item.get("candidate_id") == active_id]
        candidates = protected + [item for item in candidates if item.get("candidate_id") != active_id][-5:]
    record.update({
        "status": "completed", "latest_candidate_id": candidate["candidate_id"], "candidates": candidates,
        "job": {"status": "completed", "candidate_id": candidate["candidate_id"], "started_at": job.get("started_at"), "finished_at": _now(), "model": model_name},
    })
    return _save_package(project_dir, package)


def mark_longform_speaker_diagnosis_failed(project_dir: Path, speaker_id: str, error: object) -> dict:
    package = read_avatar_package(project_dir)
    if not package:
        raise AvatarImportError("数字人素材包不存在")
    speaker = _find_speaker(package, speaker_id.lower())
    diagnostics = package.setdefault("asr", {}).setdefault("speaker_diagnostics", {})
    record = diagnostics.setdefault(speaker["speaker_id"], {"speaker_id": speaker["speaker_id"], "speaker_name": speaker["name"], "candidates": []})
    message = str(error).strip() or error.__class__.__name__
    record.update({"status": "failed", "job": {"status": "failed", "finished_at": _now(), "error": message[:3000]}})
    return _save_package(project_dir, package)


def start_longform_speaker_realign(project_dir: Path, speaker_id: str, candidate_id: str) -> dict:
    """Queue a zero-ASR re-alignment of a stored transcript as a new candidate."""
    package, speaker = _require_longform_speaker(project_dir, speaker_id)
    diagnostics = package.setdefault("asr", {}).setdefault("speaker_diagnostics", {})
    record = diagnostics.get(speaker["speaker_id"]) or {}
    baseline = next((item for item in record.get("candidates", []) if item.get("candidate_id") == candidate_id), None)
    if not baseline or not baseline.get("full_transcript"):
        raise AvatarImportError("未找到可复用的 ASR 识别结果，请先执行一次重新分析")
    if baseline.get("source_sha256") != (speaker.get("source") or {}).get("sha256"):
        raise AvatarImportError("原片已更换；历史 ASR 结果已过期，请重新分析")
    if (record.get("job") or {}).get("status") == "running":
        raise AvatarImportError(f"{speaker['name']} 的诊断任务正在运行")
    new_candidate_id = f"ASRC-{uuid4().hex[:12]}"
    record.update({
        "status": "running",
        "job": {
            "status": "running", "candidate_id": new_candidate_id, "started_at": _now(),
            "mode": "realign_existing_transcript", "source_candidate_id": candidate_id,
        },
    })
    return _save_package(project_dir, package)


def run_longform_speaker_realign(project_dir: Path, speaker_id: str) -> dict:
    """Rebuild candidate cuts from saved words without invoking Whisper again."""
    package, speaker = _require_longform_speaker(project_dir, speaker_id)
    record = ((package.get("asr") or {}).get("speaker_diagnostics") or {}).get(speaker["speaker_id"])
    job = (record or {}).get("job") or {}
    if job.get("status") != "running" or job.get("mode") != "realign_existing_transcript":
        raise AvatarImportError("当前没有待执行的既有识别结果重新对齐任务")
    baseline = next((item for item in record.get("candidates", []) if item.get("candidate_id") == job.get("source_candidate_id")), None)
    if not baseline:
        raise AvatarImportError("原始识别候选已不存在，无法重新对齐")
    candidate = _build_longform_speaker_candidate(
        project_dir,
        package,
        speaker["speaker_id"],
        {"text": baseline.get("full_transcript", ""), "segments": baseline.get("segments") or []},
        candidate_id=str(job["candidate_id"]),
        model_name=str(baseline.get("model") or "本机 ASR"),
        kind="normalized_realign",
        transcription_options={
            **(baseline.get("transcription_options") or {}),
            "text_normalization": "traditional_to_simplified_for_alignment",
            "reused_existing_transcript": True,
        },
    )
    candidates = list(record.get("candidates") or []) + [candidate]
    if len(candidates) > 6:
        active_id = record.get("active_candidate_id")
        protected = [item for item in candidates if item.get("candidate_id") == active_id]
        candidates = protected + [item for item in candidates if item.get("candidate_id") != active_id][-5:]
    record.update({
        "status": "completed", "latest_candidate_id": candidate["candidate_id"], "candidates": candidates,
        "job": {
            "status": "completed", "candidate_id": candidate["candidate_id"], "started_at": job.get("started_at"),
            "finished_at": _now(), "mode": "realign_existing_transcript", "source_candidate_id": baseline["candidate_id"],
        },
    })
    return _save_package(project_dir, package)


def apply_longform_speaker_candidate(project_dir: Path, speaker_id: str, candidate_id: str) -> dict:
    """Replace one speaker's *unapproved* plan only; every other speaker stays frozen."""
    package, speaker = _require_longform_speaker(project_dir, speaker_id)
    record = (((package.get("asr") or {}).get("speaker_diagnostics") or {}).get(speaker["speaker_id"]) or {})
    candidate = next((item for item in record.get("candidates", []) if item.get("candidate_id") == candidate_id), None)
    if not candidate or candidate.get("status") != "ready":
        raise AvatarImportError("未找到可采用的诊断候选方案")
    if candidate.get("source_sha256") != (speaker.get("source") or {}).get("sha256"):
        raise AvatarImportError("原片已更换；该诊断候选已过期，请重新分析")
    candidate_items = copy.deepcopy(candidate.get("cut_items") or [])
    expected_ids = {turn["turn_id"] for turn in package["turns"] if turn["speaker_id"] == speaker["speaker_id"]}
    if {item.get("turn_id") for item in candidate_items} != expected_ids:
        raise AvatarImportError("诊断候选不完整，不能替换当前切割方案")

    turn_by_id = {turn["turn_id"]: turn for turn in package["turns"]}
    for item in candidate_items:
        item.pop("approved_at", None)
        item["updated_at"] = _now()
        turn = turn_by_id[item["turn_id"]]
        for key in ("source_start_seconds", "source_end_seconds"):
            turn.pop(key, None)
        for key in ("transcript", "asr_similarity", "asr_coverage"):
            if key in item:
                turn[key] = item[key]
            else:
                turn.pop(key, None)
        turn["status"] = "cut_pending_review" if item.get("status") == "pending_review" else "asr_failed"

    existing_items = (package.get("cut_plan") or {}).get("items") or []
    merged_items = [item for item in existing_items if item.get("speaker_id") != speaker["speaker_id"]] + candidate_items
    turn_order = {turn["turn_id"]: turn["index"] for turn in package["turns"]}
    merged_items.sort(key=lambda item: turn_order[item["turn_id"]])
    package["cut_plan"]["items"] = merged_items
    package["cut_plan"]["generated_at"] = _now()
    _update_cut_plan_summary(package)

    candidate_turn_ids = {item["turn_id"] for item in candidate_items}
    asr = package.setdefault("asr", {})
    # Aggregate warnings are derived from the *current* set of cuts.  Keeping
    # an old average-similarity warning after one speaker is repaired makes a
    # fully valid package appear permanently broken in the UI.
    asr["issues"] = [
        issue for issue in asr.get("issues", [])
        if issue.get("turn_id") not in candidate_turn_ids and issue.get("code") != "average_similarity_failed"
    ] + copy.deepcopy(candidate.get("issues") or [])
    values = [float(turn.get("asr_similarity") or 0) for turn in package["turns"]]
    average_similarity = sum(values) / max(1, len(values))
    asr.setdefault("summary", {})["average_similarity"] = round(average_similarity, 4)
    if average_similarity < package["settings"]["minimum_average_similarity"]:
        asr["issues"].append(_issue(
            "average_similarity_failed",
            f"平均台词相似度 {average_similarity:.3f}，低于门槛 {package['settings']['minimum_average_similarity']:.3f}",
            severity="warning",
        ))
    asr["status"] = "passed"
    record["active_candidate_id"] = candidate_id
    record["applied_at"] = _now()
    package["assembly"] = {"status": "not_started", "issues": [], "summary": {}}
    return _save_package(project_dir, package)


def run_avatar_asr(project_dir: Path, payload: dict | None = None) -> dict:
    package = read_avatar_package(project_dir)
    if not package or package["asr"]["status"] != "running":
        raise AvatarImportError("当前没有待执行的数字人 ASR 任务")
    payload = payload or {}
    model, model_name = _load_whisper(str(payload.get("model")) if payload.get("model") else None)
    issues: list[dict] = []
    similarities: list[float] = []
    coverages: list[float] = []
    speaker_diagnostics: dict | None = None
    if package["import_mode"] == "per_turn":
        for position, turn in enumerate(package["turns"], 1):
            source_path = _safe_project_file(project_dir, turn["source"]["path"])
            actual, _segments = _transcribe_file(model, source_path)
            similarity, coverage = text_metrics(turn["text"], actual)
            turn["transcript"] = actual
            turn["asr_similarity"] = round(similarity, 4)
            turn["asr_coverage"] = round(coverage, 4)
            similarities.append(similarity)
            coverages.append(coverage)
            if coverage < package["settings"]["minimum_turn_coverage"] or similarity < package["settings"].get("minimum_turn_similarity", 0.86):
                turn["status"] = "asr_failed"
                issues.append(_issue("turn_asr_failed", f"{turn['turn_id']} 台词覆盖率 {coverage:.3f}、相似度 {similarity:.3f}", turn_id=turn["turn_id"]))
            else:
                turn["status"] = "asr_passed"
            package["asr"]["summary"] = {"completed": position, "total": len(package["turns"]), "model": model_name}
            _save_package(project_dir, package)
    else:
        timing_manifest = copy.deepcopy((package.get("asr") or {}).get("summary", {}).get("timing_manifest"))
        transcripts: dict[str, dict] = {}
        for position, speaker in enumerate(package["speakers"], 1):
            source_path = _safe_project_file(project_dir, speaker["source"]["path"])
            actual, segments = _transcribe_file(model, source_path, word_timestamps=True)
            transcripts[speaker["speaker_id"]] = {"text": actual, "segments": segments}
            progress_summary = {"completed": position, "total": len(package["speakers"]), "model": model_name}
            if timing_manifest:
                progress_summary["timing_manifest"] = timing_manifest
                progress_summary["alignment_source"] = "deterministic_timing_manifest"
            package["asr"]["summary"] = progress_summary
            _save_package(project_dir, package)
        if timing_manifest:
            issues.extend(_review_deterministic_longform_turns(package, transcripts, timing_manifest))
        else:
            issues.extend(_align_longform_turns(project_dir, package, transcripts))
            speaker_diagnostics = _initial_longform_speaker_diagnostics(package, transcripts, model_name, issues)
        similarities = [float(turn.get("asr_similarity") or 0) for turn in package["turns"]]
        coverages = [float(turn.get("asr_coverage") or 0) for turn in package["turns"]]
    average_similarity = sum(similarities) / max(1, len(similarities))
    if average_similarity < package["settings"]["minimum_average_similarity"]:
        issues.append(_issue(
            "average_similarity_failed",
            f"平均台词相似度 {average_similarity:.3f}，低于门槛 {package['settings']['minimum_average_similarity']:.3f}",
            severity="warning" if package["import_mode"] == "longform" else "error",
        ))
    errors = [item for item in issues if item["severity"] == "error"]
    package["asr"] = {
        "status": "failed" if errors else "passed",
        "started_at": package["asr"].get("started_at", _now()),
        "finished_at": _now(),
        "issues": issues,
        "summary": {
            "completed": len(package["turns"]),
            "total": len(package["turns"]),
            "model": model_name,
            "minimum_coverage": round(min(coverages, default=0), 4),
            "average_similarity": round(average_similarity, 4),
        },
    }
    if package.get("import_mode") == "longform" and timing_manifest:
        package["asr"]["summary"]["timing_manifest"] = timing_manifest
        package["asr"]["summary"]["alignment_source"] = "deterministic_timing_manifest"
    if speaker_diagnostics is not None:
        package["asr"]["speaker_diagnostics"] = speaker_diagnostics
    return _save_package(project_dir, package)


def _cut_plan_item(package: dict, turn_id: str) -> dict:
    for item in (package.get("cut_plan") or {}).get("items") or []:
        if item.get("turn_id") == turn_id:
            return item
    raise AvatarImportError(f"未找到 {turn_id} 的长视频切割方案，请先完成 ASR 台词对齐。")


def _update_cut_plan_summary(package: dict) -> None:
    plan = package.get("cut_plan") or {}
    items = list(plan.get("items") or [])
    total = len(items)
    approved = sum(item.get("status") == "approved" for item in items)
    manual = sum(item.get("status") == "needs_manual" for item in items)
    pending = sum(item.get("status") == "pending_review" for item in items)
    plan["summary"] = {"total": total, "approved": approved, "pending_review": pending, "needs_manual": manual}
    if total and approved == total:
        plan["status"] = "approved"
        plan["approved_at"] = _now()
    elif manual:
        plan["status"] = "needs_attention"
        plan.pop("approved_at", None)
    else:
        plan["status"] = "awaiting_review"
        plan.pop("approved_at", None)
    package["cut_plan"] = plan


def update_longform_cut(project_dir: Path, turn_id: str, payload: dict) -> dict:
    package = read_avatar_package(project_dir)
    if not package or package.get("import_mode") != "longform":
        raise AvatarImportError("当前项目不是本地整段口播切割方案。")
    turn = _find_turn(package, turn_id.upper())
    item = _cut_plan_item(package, turn["turn_id"])
    speaker = _find_speaker(package, turn["speaker_id"])
    source = speaker.get("source") or {}
    duration = float(((source.get("media") or {}).get("duration_seconds")) or 0)
    try:
        start = round(float(payload.get("start_seconds")), 4)
        end = round(float(payload.get("end_seconds")), 4)
    except (TypeError, ValueError) as exc:
        raise AvatarImportError("请填写有效的起止秒数。") from exc
    if start < 0 or end <= start or end - start < 0.12 or (duration and end > duration + 0.001):
        raise AvatarImportError(f"{turn['turn_id']} 的切点无效：起止时间必须在原片 0–{duration:.3f} 秒内，且片段至少 0.12 秒。")
    for other in (package.get("cut_plan") or {}).get("items") or []:
        if other.get("turn_id") == turn["turn_id"] or other.get("speaker_id") != turn["speaker_id"]:
            continue
        other_start, other_end = other.get("start_seconds"), other.get("end_seconds")
        if isinstance(other_start, (int, float)) and isinstance(other_end, (int, float)) and start < other_end and other_start < end:
            raise AvatarImportError(
                f"{turn['turn_id']} 的切点与同一角色的 {other['turn_id']} 重叠。请调整起止时间，避免原声在母版中重复播放。"
            )
    item.update({
        "start_seconds": start, "end_seconds": end, "status": "pending_review", "confidence": "manual",
        "source_type": "manual", "review_note": str(payload.get("review_note") or "").strip()[:500], "updated_at": _now(),
    })
    item.pop("approved_at", None)
    turn.pop("source_start_seconds", None)
    turn.pop("source_end_seconds", None)
    turn["status"] = "cut_pending_review"
    package["assembly"] = {"status": "not_started", "issues": [], "summary": {}}
    _update_cut_plan_summary(package)
    return _save_package(project_dir, package)


def approve_longform_cut(project_dir: Path, turn_id: str) -> dict:
    package = read_avatar_package(project_dir)
    if not package or package.get("import_mode") != "longform":
        raise AvatarImportError("当前项目不是本地整段口播切割方案。")
    turn = _find_turn(package, turn_id.upper())
    item = _cut_plan_item(package, turn["turn_id"])
    start, end = item.get("start_seconds"), item.get("end_seconds")
    if not isinstance(start, (int, float)) or not isinstance(end, (int, float)) or end <= start:
        raise AvatarImportError(f"{turn['turn_id']} 尚未获得有效切点，请先填写起止时间。")
    item["status"] = "approved"
    item["approved_at"] = _now()
    item["updated_at"] = _now()
    turn["source_start_seconds"] = round(float(start), 4)
    turn["source_end_seconds"] = round(float(end), 4)
    turn["status"] = "cut_approved"
    package["assembly"] = {"status": "not_started", "issues": [], "summary": {}}
    _update_cut_plan_summary(package)
    return _save_package(project_dir, package)


def approve_high_confidence_longform_cuts(project_dir: Path) -> dict:
    package = read_avatar_package(project_dir)
    if not package or package.get("import_mode") != "longform":
        raise AvatarImportError("当前项目不是本地整段口播切割方案。")
    for item in (package.get("cut_plan") or {}).get("items") or []:
        if item.get("status") != "pending_review" or item.get("confidence") != "high":
            continue
        turn = _find_turn(package, item["turn_id"])
        start, end = item.get("start_seconds"), item.get("end_seconds")
        if isinstance(start, (int, float)) and isinstance(end, (int, float)) and end > start:
            item["status"] = "approved"
            item["approved_at"] = _now()
            item["updated_at"] = _now()
            turn["source_start_seconds"] = round(float(start), 4)
            turn["source_end_seconds"] = round(float(end), 4)
            turn["status"] = "cut_approved"
    package["assembly"] = {"status": "not_started", "issues": [], "summary": {}}
    _update_cut_plan_summary(package)
    return _save_package(project_dir, package)


def update_longform_presentation(project_dir: Path, payload: dict) -> dict:
    package = read_avatar_package(project_dir)
    if not package or package.get("import_mode") != "longform":
        raise AvatarImportError("当前项目不是本地整段口播切割方案。")
    mode = str(payload.get("frame_fit_mode") or "")
    if mode not in {"blur_background", "contain_black", "cover_crop"}:
        raise AvatarImportError("画幅处理方式只能是模糊背景、黑边完整显示或裁切铺满。")
    package.setdefault("presentation", {})["frame_fit_mode"] = mode
    package["assembly"] = {"status": "not_started", "issues": [], "summary": {}}
    return _save_package(project_dir, package)


def mark_avatar_job_failed(project_dir: Path, stage: str, error: object) -> dict:
    package = read_avatar_package(project_dir)
    if not package:
        raise AvatarImportError("数字人素材包不存在")
    if stage not in {"asr", "assembly"}:
        raise AvatarImportError("未知的数字人任务阶段")
    message = str(error).strip() or error.__class__.__name__
    previous = package.get(stage) if isinstance(package.get(stage), dict) else {}
    summary = copy.deepcopy(previous.get("summary") or {})
    if stage == "assembly":
        summary.update({
            "phase": "failed",
            "failed_at": _now(),
            "resumable": package.get("import_mode") == "longform",
        })
    package[stage] = {
        "status": "failed",
        "started_at": previous.get("started_at", _now()),
        "finished_at": _now(),
        "issues": [_issue(f"{stage}_exception", message)],
        "summary": summary,
        "error": message[:3000],
    }
    if stage == "assembly" and previous.get("run_id"):
        package[stage]["run_id"] = previous["run_id"]
    return _save_package(project_dir, package)


def start_avatar_assembly(project_dir: Path, payload: dict | None = None) -> dict:
    package = read_avatar_package(project_dir)
    if not package:
        raise AvatarImportError("数字人素材包不存在")
    if package["validation"]["status"] not in {"passed", "passed_with_warnings"}:
        raise AvatarImportError("媒体检查未通过，不能合成数字人母版")
    if package["settings"]["require_asr"] and package["asr"]["status"] != "passed":
        raise AvatarImportError("台词 ASR 核验未通过，不能合成数字人母版")
    if package.get("import_mode") == "longform" and (package.get("cut_plan") or {}).get("status") != "approved":
        raise AvatarImportError("整段口播的切割方案还未全部审核通过，请先在“切割审核”中确认每句台词的起止点。")
    package["assembly"] = {
        "status": "running",
        "run_id": f"AVA-{uuid4().hex[:16]}",
        "started_at": _now(),
        "issues": [],
        "summary": {
            "phase": "preparing",
            "completed": 0,
            "total": len(package["turns"]),
            "reused": 0,
            "current_turn_id": None,
            "resumable": package.get("import_mode") == "longform",
        },
    }
    return _save_package(project_dir, package)


def _turn_source(project_dir: Path, package: dict, turn: dict) -> tuple[Path, float, float]:
    if package["import_mode"] == "per_turn":
        path = _safe_project_file(project_dir, turn["source"]["path"])
        duration = float(turn["source"]["media"]["duration_seconds"])
        return path, 0.0, duration
    speaker = _find_speaker(package, turn["speaker_id"])
    path = _safe_project_file(project_dir, speaker["source"]["path"])
    start = float(turn.get("source_start_seconds") or 0)
    end = float(turn.get("source_end_seconds") or 0)
    if end <= start:
        raise AvatarImportError(f"{turn['turn_id']} 没有有效的长视频切割边界")
    return path, start, end


def _escape_filter_path(path: Path) -> str:
    return str(path).replace("\\", "/").replace("'", "'\\''")


def _build_filter_graph(project_dir: Path, package: dict) -> tuple[list[Path], str, list[dict]]:
    settings = package["settings"]
    inputs: list[Path] = []
    input_index: dict[str, int] = {}
    filters: list[str] = []
    timeline: list[dict] = []
    cursor = 0.0
    turns = package["turns"]
    for index, turn in enumerate(turns):
        source, source_start, source_end = _turn_source(project_dir, package, turn)
        key = str(source.resolve())
        if key not in input_index:
            input_index[key] = len(inputs)
            inputs.append(source)
        source_index = input_index[key]
        duration = source_end - source_start
        next_turn = turns[index + 1] if index + 1 < len(turns) else None
        if not next_turn:
            gap = 0.0
        elif next_turn["speaker_id"] == turn["speaker_id"]:
            gap = float(settings["same_speaker_gap_seconds"])
        else:
            gap = float(settings["speaker_change_gap_seconds"])
        total = duration + gap
        fit_mode = str((package.get("presentation") or {}).get("frame_fit_mode") or "contain_black")
        if fit_mode == "blur_background":
            source_label = f"src{index}"
            filters.append(
                f"[{source_index}:v:0]trim=start={source_start:.6f}:end={source_end:.6f},setpts=PTS-STARTPTS,"
                f"fps={settings['fps']},split=2[{source_label}bg][{source_label}fg]"
            )
            background_label = f"bg{index}"
            foreground_label = f"fg{index}"
            filters.append(
                f"[{source_label}bg]scale={settings['width']}:{settings['height']}:force_original_aspect_ratio=increase,"
                f"crop={settings['width']}:{settings['height']},boxblur=18:2[{background_label}]"
            )
            filters.append(
                f"[{source_label}fg]scale={settings['width']}:{settings['height']}:force_original_aspect_ratio=decrease[{foreground_label}]"
            )
            video = f"[{background_label}][{foreground_label}]overlay=(W-w)/2:(H-h)/2,setsar=1"
        elif fit_mode == "cover_crop":
            video = (
                f"[{source_index}:v:0]trim=start={source_start:.6f}:end={source_end:.6f},setpts=PTS-STARTPTS,fps={settings['fps']},"
                f"scale={settings['width']}:{settings['height']}:force_original_aspect_ratio=increase,"
                f"crop={settings['width']}:{settings['height']},setsar=1"
            )
        else:
            video = (
                f"[{source_index}:v:0]trim=start={source_start:.6f}:end={source_end:.6f},setpts=PTS-STARTPTS,fps={settings['fps']},"
                f"scale={settings['width']}:{settings['height']}:force_original_aspect_ratio=decrease,"
                f"pad={settings['width']}:{settings['height']}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1"
            )
        audio = (
            f"[{source_index}:a:0]atrim=start={source_start:.6f}:end={source_end:.6f},asetpts=PTS-STARTPTS,"
            f"aresample={settings['audio_sample_rate']},aformat=sample_fmts=fltp:channel_layouts=stereo"
        )
        if gap > 0:
            video += f",tpad=stop_mode=clone:stop_duration={gap:.6f},trim=duration={total:.6f}"
            audio += f",apad=pad_dur={gap:.6f},atrim=duration={total:.6f}"
        filters.append(video + f"[v{index}]")
        filters.append(audio + f"[a{index}]")
        timeline.append({
            "turn_id": turn["turn_id"],
            "index": turn["index"],
            "speaker_id": turn["speaker_id"],
            "text": turn["text"],
            "source_path": _safe_relative(project_dir, source),
            "source_start_seconds": round(source_start, 4),
            "source_end_seconds": round(source_end, 4),
            "start_seconds": round(cursor, 4),
            "speech_end_seconds": round(cursor + duration, 4),
            "end_seconds": round(cursor + total, 4),
            "gap_after_seconds": round(gap, 4),
            "frame_fit_mode": fit_mode,
            "visual_contract": turn.get("visual_contract", {}),
        })
        cursor += total
    concat_inputs = "".join(f"[v{index}][a{index}]" for index in range(len(turns)))
    filters.append(f"{concat_inputs}concat=n={len(turns)}:v=1:a=1[vout][aout]")
    return inputs, ";\n".join(filters), timeline


def _srt_time(seconds: float) -> str:
    milliseconds = max(0, round(seconds * 1000))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def _write_srt(path: Path, timeline: list[dict]) -> None:
    blocks: list[str] = []
    for index, turn in enumerate(timeline, 1):
        blocks.append(
            f"{index}\n{_srt_time(turn['start_seconds'])} --> {_srt_time(turn['speech_end_seconds'])}\n{turn['text']}\n"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(blocks), encoding="utf-8")


def _verify_decode(path: Path) -> tuple[bool, str]:
    ffmpeg = _find_binary("ffmpeg")
    if not ffmpeg:
        return False, "未发现 ffmpeg"
    result = _run([ffmpeg, "-v", "error", "-i", str(path), "-map", "0:v:0", "-map", "0:a:0", "-f", "null", "-"])
    return result.returncode == 0, (result.stderr or result.stdout or "")[-2000:]


def _longform_part_signature(project_dir: Path, package: dict, turn: dict, *, gap: float) -> str:
    """Fingerprint one normalized long-form turn without trusting a filename."""
    source, source_start, source_end = _turn_source(project_dir, package, turn)
    speaker = _find_speaker(package, turn["speaker_id"])
    source_record = speaker.get("source") or {}
    payload = {
        "version": 1,
        "turn_id": turn["turn_id"],
        "speaker_id": turn["speaker_id"],
        "source_path": _safe_relative(project_dir, source),
        "source_sha256": source_record.get("sha256"),
        "source_size": source_record.get("size_bytes"),
        "source_start_seconds": round(source_start, 6),
        "source_end_seconds": round(source_end, 6),
        "gap_seconds": round(gap, 6),
        "settings": {
            "width": package["settings"]["width"],
            "height": package["settings"]["height"],
            "fps": package["settings"]["fps"],
            "audio_sample_rate": package["settings"]["audio_sample_rate"],
            "fit_mode": (package.get("presentation") or {}).get("frame_fit_mode") or "contain_black",
        },
    }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _longform_part_output(project_dir: Path, turn: dict, signature: str) -> Path:
    return project_dir / OUTPUT_DIRECTORY / "segments" / "longform-native" / f"{int(turn['index']):03d}-{turn['turn_id']}-{signature[:16]}.mp4"


def _valid_normalized_part(path: Path, settings: dict, expected_duration: float) -> bool:
    if not path.is_file() or path.stat().st_size <= 1024:
        return False
    try:
        media = probe_media(path)
    except AvatarImportError:
        return False
    video = media.get("video") or {}
    audio = media.get("audio") or {}
    return (
        video.get("codec") == "h264"
        and audio.get("codec") == "aac"
        and int(video.get("width") or 0) == int(settings["width"])
        and int(video.get("height") or 0) == int(settings["height"])
        and int(audio.get("sample_rate") or 0) == int(settings["audio_sample_rate"])
        and abs(float(media.get("duration_seconds") or 0) - expected_duration) <= 0.25
    )


def _longform_turn_gap(package: dict, index: int) -> float:
    turns = package["turns"]
    next_turn = turns[index + 1] if index + 1 < len(turns) else None
    if not next_turn:
        return 0.0
    if next_turn["speaker_id"] == turns[index]["speaker_id"]:
        return float(package["settings"]["same_speaker_gap_seconds"])
    return float(package["settings"]["speaker_change_gap_seconds"])


def _render_longform_part(project_dir: Path, package: dict, turn: dict, *, gap: float, output: Path, ffmpeg: str) -> None:
    """Render exactly one turn, keeping decoder and filter memory bounded.

    Long-form phone/avatar sources are frequently HEVC.  On a busy desktop,
    x264's look-ahead and B-frame reference buffers can be the difference
    between a successful handoff and a process-wide allocation failure.  The
    intermediate is deliberately encoded with a low-latency profile: its job
    is to be a reliable, reusable editing proxy, not the final delivery.
    """
    source, source_start, source_end = _turn_source(project_dir, package, turn)
    settings = package["settings"]
    width, height, fps = int(settings["width"]), int(settings["height"]), int(settings["fps"])
    fit_mode = str((package.get("presentation") or {}).get("frame_fit_mode") or "contain_black")
    duration = source_end - source_start
    if duration <= 0:
        raise AvatarImportError(f"{turn['turn_id']} 没有有效的切割时长")
    if fit_mode == "blur_background":
        video_filters = [
            f"[0:v:0]trim=start={source_start:.6f}:end={source_end:.6f},setpts=PTS-STARTPTS,fps={fps},split=2[srcbg][srcfg]",
            f"[srcbg]scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},boxblur=18:2[bg]",
            f"[srcfg]scale={width}:{height}:force_original_aspect_ratio=decrease[fg]",
            "[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1[vbase]",
        ]
    elif fit_mode == "cover_crop":
        video_filters = [
            f"[0:v:0]trim=start={source_start:.6f}:end={source_end:.6f},setpts=PTS-STARTPTS,fps={fps},"
            f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},setsar=1[vbase]",
        ]
    else:
        video_filters = [
            f"[0:v:0]trim=start={source_start:.6f}:end={source_end:.6f},setpts=PTS-STARTPTS,fps={fps},"
            f"scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1[vbase]",
        ]
    audio_filters = [
        f"[0:a:0]atrim=start={source_start:.6f}:end={source_end:.6f},asetpts=PTS-STARTPTS,"
        f"aresample={int(settings['audio_sample_rate'])},aformat=sample_fmts=fltp:channel_layouts=stereo[abase]",
    ]
    total_duration = duration + gap
    if gap > 0:
        video_filters.append(f"[vbase]tpad=stop_mode=clone:stop_duration={gap:.6f},trim=duration={total_duration:.6f}[vout]")
        audio_filters.append(f"[abase]apad=pad_dur={gap:.6f},atrim=duration={total_duration:.6f}[aout]")
    else:
        video_filters.append("[vbase]null[vout]")
        audio_filters.append("[abase]anull[aout]")
    output.parent.mkdir(parents=True, exist_ok=True)
    candidate = output.with_name(f".{output.stem}-{uuid4().hex}.tmp{output.suffix}")
    command = [
        ffmpeg, "-y", "-hide_banner", "-nostdin",
        # Input options: explicitly limit HEVC frame workers before opening it.
        "-threads", "1", "-thread_type", "slice", "-i", str(source),
        "-filter_threads", "1", "-filter_complex_threads", "1", "-filter_complex", ";".join(video_filters + audio_filters),
        "-map", "[vout]", "-map", "[aout]",
        # Output options: remove x264 look-ahead/reference-frame buffering.
        "-threads", "1", "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency", "-crf", "20",
        "-x264-params", "bframes=0:ref=1:rc-lookahead=0:sync-lookahead=0:lookahead-threads=1:scenecut=0",
        "-profile:v", "high", "-pix_fmt", "yuv420p", "-r", str(fps), "-fps_mode", "cfr",
        "-c:a", "aac", "-b:a", "192k", "-ar", str(settings["audio_sample_rate"]), "-ac", "2",
        "-movflags", "+faststart", str(candidate),
    ]
    result = _run(command, timeout=60 * 60)
    if result.returncode != 0 or not candidate.is_file():
        candidate.unlink(missing_ok=True)
        raise AvatarImportError((result.stderr or "FFmpeg 未生成规范化数字人片段")[-3000:])
    os.replace(candidate, output)


def _write_concat_listing(path: Path, parts: list[Path]) -> None:
    lines = []
    for part in parts:
        escaped = str(part.resolve()).replace("\\", "/").replace("'", "'\\''")
        lines.append(f"file '{escaped}'")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _concat_normalized_parts(parts: list[Path], candidate_output: Path, package: dict, ffmpeg: str) -> None:
    if not parts:
        raise AvatarImportError("没有可用于拼接的数字人片段")
    listing = candidate_output.with_suffix(".concat.txt")
    _write_concat_listing(listing, parts)
    settings = package["settings"]
    command = [
        ffmpeg, "-y", "-hide_banner", "-nostdin", "-threads", "1", "-f", "concat", "-safe", "0", "-i", str(listing), "-map", "0:v:0", "-map", "0:a:0",
        "-threads", "1", "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency", "-crf", "18",
        "-x264-params", "bframes=0:ref=1:rc-lookahead=0:sync-lookahead=0:lookahead-threads=1:scenecut=0", "-profile:v", "high", "-pix_fmt", "yuv420p",
        "-r", str(settings["fps"]), "-fps_mode", "cfr", "-c:a", "aac", "-b:a", "192k", "-ar", str(settings["audio_sample_rate"]), "-ac", "2",
        "-movflags", "+faststart", str(candidate_output)]
    result = _run(command, timeout=60 * 60)
    listing.unlink(missing_ok=True)
    if result.returncode != 0 or not candidate_output.is_file():
        candidate_output.unlink(missing_ok=True)
        raise AvatarImportError((result.stderr or "FFmpeg 未能拼接数字人片段")[-3000:])


def _update_longform_assembly_progress(project_dir: Path, run_id: str, changes: dict) -> dict:
    """Persist observable progress without overwriting a new user configuration."""
    current = read_avatar_package(project_dir)
    if not current:
        raise AvatarImportError("数字人素材包在合成期间丢失")
    assembly = current.get("assembly") if isinstance(current.get("assembly"), dict) else {}
    if assembly.get("status") != "running" or assembly.get("run_id") != run_id:
        raise AvatarImportError("合成设置已被更新，已停止旧任务以避免覆盖新配置")
    summary = copy.deepcopy(assembly.get("summary") or {})
    summary.update(changes)
    assembly["summary"] = summary
    current["assembly"] = assembly
    return _save_package(project_dir, current)


def _assemble_longform_package_serially(project_dir: Path, payload: dict | None, package: dict, ffmpeg: str) -> tuple[dict, Path, list[dict]]:
    """Normalize one approved long-form cut at a time, then concatenate it."""
    run_id = str((package.get("assembly") or {}).get("run_id") or "")
    if not run_id:
        raise AvatarImportError("本次合成缺少任务编号，请重新开始合成")
    parts: list[Path] = []
    timeline: list[dict] = []
    cursor = 0.0
    reused = 0
    for index, turn in enumerate(package["turns"]):
        source, source_start, source_end = _turn_source(project_dir, package, turn)
        gap = _longform_turn_gap(package, index)
        duration = source_end - source_start
        total = duration + gap
        signature = _longform_part_signature(project_dir, package, turn, gap=gap)
        part = _longform_part_output(project_dir, turn, signature)
        _update_longform_assembly_progress(project_dir, run_id, {
            "phase": "normalizing", "current_turn_id": turn["turn_id"], "current_index": index + 1,
            "completed": len(parts), "total": len(package["turns"]), "reused": reused,
        })
        was_reused = _valid_normalized_part(part, package["settings"], total)
        if not was_reused:
            _render_longform_part(project_dir, package, turn, gap=gap, output=part, ffmpeg=ffmpeg)
        else:
            reused += 1
        parts.append(part)
        timeline.append({
            "turn_id": turn["turn_id"], "index": turn["index"], "speaker_id": turn["speaker_id"], "text": turn["text"],
            "source_path": _safe_relative(project_dir, source), "source_start_seconds": round(source_start, 4),
            "source_end_seconds": round(source_end, 4), "start_seconds": round(cursor, 4),
            "speech_end_seconds": round(cursor + duration, 4), "end_seconds": round(cursor + total, 4),
            "gap_after_seconds": round(gap, 4),
            "frame_fit_mode": str((package.get("presentation") or {}).get("frame_fit_mode") or "contain_black"),
            "visual_contract": turn.get("visual_contract", {}), "part_path": _safe_relative(project_dir, part), "part_reused": was_reused,
        })
        cursor += total
        _update_longform_assembly_progress(project_dir, run_id, {
            "phase": "normalizing", "current_turn_id": turn["turn_id"], "current_index": index + 1,
            "completed": len(parts), "total": len(package["turns"]), "reused": reused,
        })
    output_dir = project_dir / OUTPUT_DIRECTORY
    output_dir.mkdir(parents=True, exist_ok=True)
    candidate = output_dir / f".avatar-dialogue-master-{uuid4().hex}.mp4"
    _update_longform_assembly_progress(project_dir, run_id, {
        "phase": "concatenating", "current_turn_id": None, "current_index": len(parts),
        "completed": len(parts), "total": len(package["turns"]), "reused": reused,
    })
    _concat_normalized_parts(parts, candidate, package, ffmpeg)
    current = read_avatar_package(project_dir)
    if not current or (current.get("assembly") or {}).get("run_id") != run_id or (current.get("assembly") or {}).get("status") != "running":
        candidate.unlink(missing_ok=True)
        raise AvatarImportError("合成设置已被更新，未应用旧任务结果")
    return current, candidate, timeline


def _complete_avatar_assembly(project_dir: Path, package: dict, candidate_output: Path, timeline: list[dict], payload: dict | None = None) -> dict:
    """Run the common master QA and commit a completed or reviewable assembly."""
    output_dir = project_dir / OUTPUT_DIRECTORY
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / "avatar-dialogue-master.mp4"
    timeline_path = output_dir / "avatar-dialogue-timeline.json"
    subtitle_path = output_dir / "avatar-dialogue-subtitles.srt"
    qa_path = output_dir / "avatar-dialogue-qa.json"
    media = probe_media(candidate_output)
    decode_ok, decode_detail = _verify_decode(candidate_output)
    duration = float(media["duration_seconds"])
    issues: list[dict] = []
    if duration > package["settings"]["max_duration_seconds"] + 0.05:
        issues.append(_issue("master_duration_exceeded", f"实际母版 {duration:.2f} 秒，超过上限 {package['settings']['max_duration_seconds']:.2f} 秒"))
    if not decode_ok:
        issues.append(_issue("master_decode_failed", decode_detail or "最终母版无法完整解码"))
    if media["video"]["codec"] != "h264":
        issues.append(_issue("unexpected_video_codec", f"输出视频编码为 {media['video']['codec']}，预期 h264"))
    if media["audio"]["codec"] != "aac":
        issues.append(_issue("unexpected_audio_codec", f"输出音频编码为 {media['audio']['codec']}，预期 aac"))
    master_asr: dict[str, Any] = {"required": package["settings"]["require_asr"]}
    if package["settings"]["require_asr"]:
        model, model_name = _load_whisper(str((payload or {}).get("model")) if (payload or {}).get("model") else None)
        actual, _segments = _transcribe_file(model, candidate_output)
        expected = "".join(turn["text"] for turn in package["turns"])
        similarity, coverage = text_metrics(expected, actual)
        master_asr.update({"model": model_name, "transcript": actual, "similarity": round(similarity, 4), "coverage": round(coverage, 4)})
        if coverage < package["settings"]["minimum_turn_coverage"] or similarity < package["settings"]["minimum_average_similarity"]:
            issues.append(_issue("master_asr_failed", f"最终母版台词覆盖率 {coverage:.3f}、相似度 {similarity:.3f}，未达到门槛"))
    timeline_payload = {
        "version": "1.0",
        "audio_mode": "native_avatar_audio",
        "timing_basis": "assembled native avatar audio",
        "duration_seconds": duration,
        "turns": timeline,
    }
    _atomic_write(timeline_path, timeline_payload)
    _write_srt(subtitle_path, timeline)
    qa_payload = {
        "version": "1.0",
        "status": "failed" if issues else "passed",
        "checks": {
            "turn_count": len(timeline) == len(package["turns"]),
            "decode": decode_ok,
            "duration_within_limit": duration <= package["settings"]["max_duration_seconds"] + 0.05,
            "h264_aac": media["video"]["codec"] == "h264" and media["audio"]["codec"] == "aac",
            "native_audio_mode": True,
        },
        "media": media,
        "master_asr": master_asr,
        "issues": issues,
        "sha256": _file_sha256(candidate_output),
    }
    delivered_output = output_dir / f"avatar-dialogue-master.failed-{uuid4().hex[:8]}.mp4" if issues else output
    os.replace(candidate_output, delivered_output)
    qa_payload["output_path"] = _safe_relative(project_dir, delivered_output)
    _atomic_write(qa_path, qa_payload)
    if not issues:
        for turn in package["turns"]:
            turn["status"] = "assembled"
    previous = package.get("assembly") if isinstance(package.get("assembly"), dict) else {}
    summary = copy.deepcopy(previous.get("summary") or {})
    summary.update({
        "phase": "completed" if not issues else "qa_failed",
        "turns": len(timeline),
        "duration_seconds": duration,
        "timing_basis": "native_avatar_audio",
        "video_codec": media["video"]["codec"],
        "audio_codec": media["audio"]["codec"],
        "fps": media["video"]["fps"],
        "audio_sample_rate": media["audio"]["sample_rate"],
    })
    package["assembly"] = {
        "status": "failed" if issues else "passed",
        "started_at": previous.get("started_at", _now()),
        "finished_at": _now(),
        "issues": issues,
        "summary": summary,
        "output_path": _safe_relative(project_dir, delivered_output),
        "timeline_path": _safe_relative(project_dir, timeline_path),
        "subtitle_path": _safe_relative(project_dir, subtitle_path),
        "qa_path": _safe_relative(project_dir, qa_path),
    }
    if previous.get("run_id"):
        package["assembly"]["run_id"] = previous["run_id"]
    return _save_package(project_dir, package)


def _assemble_avatar_package_parallel(project_dir: Path, payload: dict | None = None) -> dict:
    package = read_avatar_package(project_dir)
    if not package or package["assembly"]["status"] != "running":
        raise AvatarImportError("当前没有待执行的数字人合成任务")
    ffmpeg = _find_binary("ffmpeg")
    if not ffmpeg:
        raise AvatarImportError("未发现 ffmpeg，无法合成数字人母版")
    inputs, graph, timeline = _build_filter_graph(project_dir, package)
    output_dir = project_dir / OUTPUT_DIRECTORY
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / "avatar-dialogue-master.mp4"
    candidate_output = output_dir / f".avatar-dialogue-master-{uuid4().hex}.mp4"
    with tempfile.TemporaryDirectory(prefix="openmontage-avatar-") as temp_dir:
        graph_path = Path(temp_dir) / "filter.txt"
        graph_path.write_text(graph, encoding="utf-8")
        command = [ffmpeg, "-y"]
        for source in inputs:
            command.extend(["-i", str(source)])
        command.extend([
            "-filter_complex_script", str(graph_path),
            "-map", "[vout]", "-map", "[aout]",
            "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-profile:v", "high",
            "-pix_fmt", "yuv420p", "-r", str(package["settings"]["fps"]), "-fps_mode", "cfr",
            "-c:a", "aac", "-b:a", "192k", "-ar", str(package["settings"]["audio_sample_rate"]), "-ac", "2",
            "-movflags", "+faststart",
            "-metadata", "title=OpenMontage Avatar Dialogue Master",
            "-metadata", "comment=Native avatar audio is the master timeline",
            str(candidate_output),
        ])
        result = _run(command, timeout=60 * 60)
    if result.returncode != 0 or not candidate_output.is_file():
        candidate_output.unlink(missing_ok=True)
        raise AvatarImportError((result.stderr or "FFmpeg 未生成数字人母版")[-3000:])
    return _complete_avatar_assembly(project_dir, package, candidate_output, timeline, payload)


def assemble_avatar_package(project_dir: Path, payload: dict | None = None) -> dict:
    package = read_avatar_package(project_dir)
    if not package or package["assembly"]["status"] != "running":
        raise AvatarImportError("当前没有待执行的数字人合成任务")
    if package.get("import_mode") != "longform":
        return _assemble_avatar_package_parallel(project_dir, payload)
    ffmpeg = _find_binary("ffmpeg")
    if not ffmpeg:
        raise AvatarImportError("未发现 ffmpeg，无法合成数字人母版")
    current, candidate_output, timeline = _assemble_longform_package_serially(project_dir, payload, package, ffmpeg)
    return _complete_avatar_assembly(project_dir, current, candidate_output, timeline, payload)
