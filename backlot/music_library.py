"""Built-in and project-scoped background-music library.

V1 deliberately scans only the repository ``song`` directory.  A browser
receives opaque track ids and media URLs; absolute local paths never leave the
server.  Project-specific choices live in ``artifacts/workbench.json`` and are
validated again immediately before a render.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Any

from backlot.state import REPO_ROOT


MUSIC_ROOT = (REPO_ROOT / "song").resolve()
SUPPORTED_EXTENSIONS = {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".aiff", ".aif"}
PROJECT_UPLOAD_DIRECTORY = Path("assets/audio/music/uploads")
MAX_PROJECT_MUSIC_BYTES = 100 * 1024 * 1024
MIN_PROJECT_MUSIC_DURATION_SECONDS = 1.0

# This source was exported by the user after applying -13 in their editor.
# The value remains provenance metadata only.  The final relative loudness is
# deliberately chosen per project in the BGM sample workflow, where the user
# hears it against the actual narration/host audio before applying it globally.
PRECALIBRATED_TRACKS = {
    "新闻传播序曲.wav": {
        "id": "news-opening-01",
        "title": "新闻传播序曲",
        "category": "news",
        "source_calibration_db": -13.0,
        "playback_gain_db": -8.0,
        "calibration_note": "源文件已按 -13 dB 制作；请以第一段实际混音样板确认相对人声音量",
    },
}


class MusicLibraryError(ValueError):
    """A user-facing music library or policy error."""


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def _project_upload_root(project_dir: Path, *, create: bool = False) -> Path:
    project = Path(project_dir).resolve(strict=True)
    if not project.is_dir():
        raise MusicLibraryError("当前项目目录不存在")
    root = (project / PROJECT_UPLOAD_DIRECTORY).resolve()
    if not _inside(root, project):
        raise MusicLibraryError("项目音乐目录越过了当前项目边界")
    if create:
        root.mkdir(parents=True, exist_ok=True)
    return root


def _safe_display_name(original_filename: str) -> str:
    leaf = Path(str(original_filename or "").replace("\\", "/")).name.strip()
    if not leaf:
        raise MusicLibraryError("背景音乐文件名不能为空")
    suffix = Path(leaf).suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        raise MusicLibraryError("不支持此背景音乐格式")
    stem = re.sub(r"[^\w\- .()（）\u4e00-\u9fff]+", "_", Path(leaf).stem, flags=re.UNICODE).strip(" ._")
    return f"{(stem or 'music')[:96]}{suffix}"


def prepare_project_music_upload(project_dir: Path, original_filename: str) -> Path:
    """Create a project-contained temporary destination for a streamed upload."""
    safe_name = _safe_display_name(original_filename)
    root = _project_upload_root(project_dir, create=True)
    temporary = (root / f".incoming-{uuid.uuid4().hex}-{safe_name}").resolve()
    if not _inside(temporary, root):
        raise MusicLibraryError("上传临时路径越过了当前项目边界")
    temporary.touch(exist_ok=False)
    return temporary


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _project_track_metadata(path: Path, display_name: str, digest: str) -> dict[str, Any]:
    probe = _probe(path)
    duration = float(probe.get("duration_seconds") or 0.0)
    if duration < MIN_PROJECT_MUSIC_DURATION_SECONDS:
        raise MusicLibraryError("背景音乐时长必须至少为 1 秒")
    return {
        "id": f"project-music-{digest}", "title": Path(display_name).stem,
        "filename": path.name, "display_name": display_name, "category": "project_upload",
        "scope": "project", "content_sha256": digest,
        "media_url": f"music/project-tracks/project-music-{digest}",
        "source_calibration_db": None, "playback_gain_db": 0.0,
        "calibration_note": "项目上传音乐；请先试听第一段真实混音样板",
        "license_notice": "用户上传的音乐；发布前请确认拥有相应使用权", **probe,
    }


def complete_project_music_upload(
    project_dir: Path, temporary_path: Path, original_filename: str, *,
    max_bytes: int = MAX_PROJECT_MUSIC_BYTES,
) -> tuple[Path, dict[str, Any]]:
    """Validate and atomically adopt an upload, reusing identical content."""
    display_name = _safe_display_name(original_filename)
    root = _project_upload_root(project_dir, create=True)
    temporary = Path(temporary_path).resolve(strict=True)
    if not temporary.is_file() or not _inside(temporary, root) or not temporary.name.startswith(".incoming-"):
        raise MusicLibraryError("上传文件不在当前项目的安全临时目录中")
    size = temporary.stat().st_size
    if size <= 0:
        raise MusicLibraryError("背景音乐文件为空")
    if size > int(max_bytes):
        raise MusicLibraryError(f"背景音乐文件超过 {int(max_bytes) // (1024 * 1024)} MB 限制")
    digest = _sha256(temporary)
    track_id = f"project-music-{digest}"
    existing_meta = root / f"{track_id}.json"
    if existing_meta.is_file():
        try:
            metadata = json.loads(existing_meta.read_text(encoding="utf-8"))
            existing = (root / str(metadata["filename"])).resolve(strict=True)
            if _inside(existing, root) and _sha256(existing) == digest:
                temporary.unlink(missing_ok=True)
                return existing, metadata
        except (OSError, KeyError, json.JSONDecodeError, MusicLibraryError):
            pass
    probe = _probe(temporary)
    if float(probe.get("duration_seconds") or 0.0) < MIN_PROJECT_MUSIC_DURATION_SECONDS:
        raise MusicLibraryError("背景音乐时长必须至少为 1 秒")
    final_path = (root / f"{track_id}{Path(display_name).suffix.lower()}").resolve()
    if not _inside(final_path, root):
        raise MusicLibraryError("背景音乐目标路径越过了当前项目边界")
    if final_path.exists() and _sha256(final_path) != digest:
        raise MusicLibraryError("项目音乐内容标识发生冲突")
    if not final_path.exists():
        os.replace(temporary, final_path)
    else:
        temporary.unlink(missing_ok=True)
    metadata = _project_track_metadata(final_path, display_name, digest)
    metadata_path = root / f"{track_id}.json"
    # Keep the transient name short for Windows' classic MAX_PATH limit.  The
    # final sidecar still carries the full stable SHA-256 id.
    metadata_tmp = root / f".metadata-{uuid.uuid4().hex[:12]}.tmp"
    metadata_tmp.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(metadata_tmp, metadata_path)
    return final_path, metadata


def _probe(path: Path) -> dict[str, Any]:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise MusicLibraryError("本机未安装 FFprobe，无法验证背景音乐")
    completed = subprocess.run(
        [
            ffprobe, "-v", "error", "-show_entries",
            "format=duration:stream=codec_type,codec_name,sample_rate,channels",
            "-of", "json", str(path),
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
    )
    if completed.returncode != 0:
        raise MusicLibraryError(f"背景音乐无法解码：{path.name}")
    try:
        payload = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise MusicLibraryError(f"背景音乐信息读取失败：{path.name}") from exc
    stream = next((item for item in payload.get("streams", []) if item.get("codec_type") == "audio"), None)
    if not stream:
        raise MusicLibraryError(f"文件不包含可用音轨：{path.name}")
    try:
        duration = max(0.0, float((payload.get("format") or {}).get("duration") or 0))
    except (TypeError, ValueError):
        duration = 0.0
    return {
        "duration_seconds": round(duration, 3),
        "codec": str(stream.get("codec_name") or "unknown"),
        "sample_rate": int(stream.get("sample_rate") or 0),
        "channels": int(stream.get("channels") or 0),
    }


def _fallback_metadata(path: Path) -> dict[str, Any]:
    digest = hashlib.sha256(path.name.encode("utf-8")).hexdigest()[:12]
    return {
        "id": f"news-{digest}",
        "title": path.stem,
        "category": "news",
        "source_calibration_db": None,
        "playback_gain_db": 0.0,
        "calibration_note": "未登记源文件校准值；合成时保持当前文件响度",
    }


def _list_project_tracks(project_dir: Path) -> tuple[list[dict[str, Any]], list[str]]:
    tracks: list[dict[str, Any]] = []
    errors: list[str] = []
    try:
        root = _project_upload_root(project_dir)
    except (OSError, MusicLibraryError) as exc:
        return tracks, [str(exc)]
    if not root.is_dir():
        return tracks, errors
    for metadata_path in sorted(root.glob("project-music-*.json")):
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            filename = str(metadata.get("filename") or "")
            path = (root / filename).resolve(strict=True)
            digest = str(metadata.get("content_sha256") or "")
            if not _inside(path, root) or path.suffix.lower() not in SUPPORTED_EXTENSIONS:
                raise MusicLibraryError("项目音乐索引包含越界路径")
            if metadata.get("id") != f"project-music-{digest}" or _sha256(path) != digest:
                raise MusicLibraryError("项目音乐索引与文件内容不一致")
            current = dict(metadata)
            current.update(_probe(path))
            tracks.append(current)
        except (OSError, json.JSONDecodeError, MusicLibraryError) as exc:
            errors.append(str(exc))
    return tracks, errors


def list_music_tracks(project_dir: Path | None = None) -> dict[str, Any]:
    """Return validated news tracks without exposing local absolute paths."""
    tracks: list[dict[str, Any]] = []
    errors: list[str] = []
    if not MUSIC_ROOT.is_dir():
        errors.append("新闻背景音乐目录不存在")
    else:
        for path in sorted(MUSIC_ROOT.iterdir(), key=lambda item: item.name.casefold()):
            if not path.is_file() or path.suffix.lower() not in SUPPORTED_EXTENSIONS:
                continue
            try:
                resolved = path.resolve(strict=True)
                if not _inside(resolved, MUSIC_ROOT):
                    continue
                metadata = dict(PRECALIBRATED_TRACKS.get(path.name) or _fallback_metadata(path))
                metadata.update(_probe(resolved))
                metadata.update({
                    "filename": path.name,
                    "media_url": f"music/tracks/{metadata['id']}",
                    "license_notice": "用户提供的音乐；发布前请确认拥有相应使用权",
                })
                tracks.append(metadata)
            except (OSError, MusicLibraryError) as exc:
                errors.append(str(exc))
    if project_dir is not None:
        project_tracks, project_errors = _list_project_tracks(Path(project_dir))
        tracks.extend(project_tracks)
        errors.extend(project_errors)
    return {"version": 2, "category": "news", "tracks": tracks, "errors": errors}


def resolve_music_track(track_id: str, project_dir: Path | None = None) -> tuple[Path, dict[str, Any]]:
    wanted = str(track_id or "").strip()
    if wanted.startswith("project-music-") and project_dir is None:
        raise MusicLibraryError("项目上传音乐只能在所属项目中解析")
    catalog = list_music_tracks(project_dir)
    track = next((item for item in catalog["tracks"] if item.get("id") == wanted), None)
    if not track:
        raise MusicLibraryError("所选背景音乐不存在或当前无法解码")
    root = _project_upload_root(Path(project_dir)) if track.get("scope") == "project" else MUSIC_ROOT
    path = (root / str(track["filename"])).resolve(strict=True)
    if not _inside(path, root) or path.suffix.lower() not in SUPPORTED_EXTENSIONS:
        raise MusicLibraryError("背景音乐路径不在允许的曲库中")
    return path, track
