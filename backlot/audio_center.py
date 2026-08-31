"""Software-wide OpenMontage local TTS configuration and previews.

Project narration remains an auditable project asset.  Voice identity and
short listening tests live here instead, so a creator configures a voice once
and every project can deliberately reference that shared choice.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from backlot.state import REPO_ROOT
from tools.audio.voicebox_tts import VoiceboxTTS


AUDIO_CENTER_DIR = REPO_ROOT / ".backlot" / "audio"
AUDIO_CENTER_FILE = AUDIO_CENTER_DIR / "audio_center.json"
PREVIEW_DIRECTORY = AUDIO_CENTER_DIR / "previews"
MAX_PREVIEWS = 16


class AudioCenterError(ValueError):
    """A user-facing validation error for the global audio centre."""


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _write(data: dict[str, Any]) -> None:
    AUDIO_CENTER_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".audio-center-", suffix=".tmp", dir=AUDIO_CENTER_FILE.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        Path(temp_name).replace(AUDIO_CENTER_FILE)
    except Exception:
        try:
            Path(temp_name).unlink()
        except OSError:
            pass
        raise


def _load() -> dict[str, Any]:
    if not AUDIO_CENTER_FILE.is_file():
        return {"version": "1.0", "default_profile_id": None, "previews": [], "preview_job": {"status": "idle"}}
    try:
        data = json.loads(AUDIO_CENTER_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    data.setdefault("version", "1.0")
    data.setdefault("default_profile_id", None)
    data.setdefault("previews", [])
    data.setdefault("preview_job", {"status": "idle"})
    return data


def _profiles() -> list[dict[str, Any]]:
    tool = VoiceboxTTS()
    if tool.get_status().value != "available":
        return []
    return tool.list_profiles()


def _select_default(profiles: list[dict[str, Any]], stored_id: str | None) -> dict[str, Any] | None:
    if stored_id:
        selected = next((item for item in profiles if item["id"] == stored_id), None)
        if selected:
            return selected
    # Prefer the actual cloned "雅雅" profile installed by the user.  The
    # Serena fallback preserves a useful default on a clean installation.
    for preferred_name in ("雅雅", "qwen serena"):
        selected = next((item for item in profiles if item["name"].lower() == preferred_name), None)
        if selected:
            return selected
    return next((item for item in profiles if item.get("language", "").lower().startswith("zh")), None) or (profiles[0] if profiles else None)


def _profile_payload(profile: dict[str, Any] | None) -> dict[str, Any] | None:
    if not profile:
        return None
    configured_id = (
        os.environ.get("OPENMONTAGE_TTS_PROFILE_ID", "").strip()
        or os.environ.get("VOICEBOX_PROFILE_ID", "").strip()
    )
    configured_name = (
        os.environ.get("OPENMONTAGE_TTS_PROFILE_DISPLAY_NAME", "").strip()
        or os.environ.get("OPENMONTAGE_TTS_PROFILE_NAME", "").strip()
        or os.environ.get("VOICEBOX_PROFILE_DISPLAY_NAME", "").strip()
        or os.environ.get("VOICEBOX_PROFILE_NAME", "").strip()
    )
    display_name = profile["name"]
    if configured_id and configured_name and profile["id"] == configured_id:
        display_name = configured_name
    return {
        "id": profile["id"],
        "name": display_name,
        "description": profile.get("description") or "",
        "language": profile.get("language") or "zh",
        "voice_type": profile.get("voice_type") or "preset",
        "default_engine": profile.get("default_engine") or "qwen_custom_voice",
    }


def _safe_error(error: object) -> str:
    message = str(error or "配音任务失败")
    for variable in ("OPENAI_API_KEY",):
        secret = os.environ.get(variable)
        if secret:
            message = message.replace(secret, "[已隐藏]")
    return message[:1200]


def read_audio_center() -> dict[str, Any]:
    """Read global voice configuration plus the embedded service state."""
    persisted = _load()
    profiles = _profiles()
    selected = _select_default(profiles, persisted.get("default_profile_id"))
    if selected and persisted.get("default_profile_id") != selected["id"]:
        persisted["default_profile_id"] = selected["id"]
        _write(persisted)
    tool_status = VoiceboxTTS().get_status().value
    previews = list(persisted.get("previews") or [])[-MAX_PREVIEWS:]
    previews.reverse()
    return {
        "provider": {
            "id": "voicebox_tts",
            "name": "OpenMontage 本地配音",
            "status": tool_status,
            "detail": "音色、试听与默认选择为软件通用设置；项目只引用已确认的默认音色。",
        },
        "default_voice": _profile_payload(selected),
        "profiles": [_profile_payload(profile) for profile in profiles],
        "previews": previews,
        "preview_job": persisted.get("preview_job") or {"status": "idle"},
    }


def get_default_voice() -> dict[str, Any] | None:
    """Return the current global default for project narration generation."""
    return read_audio_center().get("default_voice")


def set_default_voice(payload: dict[str, Any]) -> dict[str, Any]:
    profile_id = str(payload.get("profile_id") or "").strip()
    if not profile_id:
        raise AudioCenterError("请选择一个音色后再设为通用默认")
    profiles = _profiles()
    if not profiles:
        raise AudioCenterError("OpenMontage 本地配音当前不可用，请先完成安装并启动服务")
    if not any(profile["id"] == profile_id for profile in profiles):
        raise AudioCenterError("所选音色已不存在，请刷新音色列表后重试")
    persisted = _load()
    persisted["default_profile_id"] = profile_id
    persisted["default_updated_at"] = _now()
    _write(persisted)
    return read_audio_center()


def start_preview(payload: dict[str, Any]) -> dict[str, Any]:
    text = str(payload.get("text") or "").strip()
    if not text:
        raise AudioCenterError("请先输入一段试听文案")
    if len(text) > 500:
        raise AudioCenterError("试听文案请控制在 500 个字符以内")
    profiles = _profiles()
    if not profiles:
        raise AudioCenterError("OpenMontage 本地配音当前不可用，请先完成安装并启动服务")
    persisted = _load()
    if (persisted.get("preview_job") or {}).get("status") == "generating":
        raise AudioCenterError("已有试听正在生成，请等待完成后再试")
    selected = _select_default(profiles, str(payload.get("profile_id") or persisted.get("default_profile_id") or ""))
    if not selected:
        raise AudioCenterError("没有可用的本地音色；请检查内置预设或迁移克隆音色")
    preview_id = f"VP-{uuid4().hex[:10]}"
    persisted["preview_job"] = {
        "id": preview_id,
        "status": "generating",
        "text": text,
        "profile_id": selected["id"],
        "profile_name": (_profile_payload(selected) or {}).get("name") or selected["name"],
        "started_at": _now(),
        "error": "",
    }
    _write(persisted)
    return read_audio_center()


def generate_preview() -> dict[str, Any]:
    """Run the queued short preview without attaching it to a video project."""
    persisted = _load()
    job = persisted.get("preview_job") or {}
    if job.get("status") != "generating":
        raise AudioCenterError("当前没有待生成的配音试听")
    preview_id = str(job["id"])
    output = PREVIEW_DIRECTORY / f"{preview_id}.wav"
    result = VoiceboxTTS().execute({
        "text": job["text"],
        "profile_id": job["profile_id"],
        "language": "zh",
        "output_path": str(output),
    })
    persisted = _load()
    current = persisted.get("preview_job") or {}
    if not result.success or not output.is_file():
        current.update({"status": "failed", "finished_at": _now(), "error": _safe_error(result.error)})
        persisted["preview_job"] = current
        _write(persisted)
        return read_audio_center()
    preview = {
        "id": preview_id,
        "text": job["text"],
        "profile_id": job["profile_id"],
        "profile_name": job["profile_name"],
        "file_name": output.name,
        "created_at": _now(),
        "duration_seconds": (result.data or {}).get("duration"),
        "provider": "OpenMontage 本地配音",
    }
    previews = list(persisted.get("previews") or [])
    previews.append(preview)
    persisted["previews"] = previews[-MAX_PREVIEWS:]
    current.update({"status": "completed", "finished_at": _now(), "preview_id": preview_id, "error": ""})
    persisted["preview_job"] = current
    _write(persisted)
    return read_audio_center()


def mark_preview_failed(error: object) -> dict[str, Any]:
    persisted = _load()
    job = persisted.get("preview_job") or {}
    job.update({"status": "failed", "finished_at": _now(), "error": _safe_error(error)})
    persisted["preview_job"] = job
    _write(persisted)
    return read_audio_center()


def preview_audio_path(preview_id: str) -> Path:
    preview = next((item for item in _load().get("previews") or [] if item.get("id") == preview_id), None)
    if not preview:
        raise AudioCenterError("未找到该试听音频")
    file_name = Path(str(preview.get("file_name") or "")).name
    path = (PREVIEW_DIRECTORY / file_name).resolve()
    try:
        path.relative_to(PREVIEW_DIRECTORY.resolve())
    except ValueError as exc:
        raise AudioCenterError("试听音频路径无效") from exc
    if not path.is_file():
        raise AudioCenterError("试听音频文件已不存在")
    return path
