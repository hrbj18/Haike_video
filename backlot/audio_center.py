"""Software-wide Haike Video TTS provider configuration and previews.

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
from backlot.tts_runtime import (
    CLOUD_PROVIDER_ID,
    LOCAL_PROVIDER_ID,
    generate_voice_audio,
    provider_status,
)
from tools.audio.voicebox_tts import VoiceboxTTS


AUDIO_CENTER_DIR = REPO_ROOT / ".backlot" / "audio"
AUDIO_CENTER_FILE = AUDIO_CENTER_DIR / "audio_center.json"
PREVIEW_DIRECTORY = AUDIO_CENTER_DIR / "previews"
MAX_PREVIEWS = 16
DOUBAO_ROLE_VOICE_ENVS = {
    "yaya": ("DOUBAO_SPEECH_YAYA_VOICE_TYPE", "雅雅"),
    "mengmeng": ("DOUBAO_SPEECH_MENGMENG_VOICE_TYPE", "檬檬"),
    "public_female": ("DOUBAO_SPEECH_PUBLIC_VOICE_TYPE", "豆包公版女声"),
}


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


def _local_profiles() -> list[dict[str, Any]]:
    tool = VoiceboxTTS()
    if tool.get_status().value != "available":
        return []
    profiles: list[dict[str, Any]] = []
    for raw in tool.list_profiles():
        profile = dict(raw)
        profile.update({
            "provider_id": LOCAL_PROVIDER_ID,
            "provider_name": "Haike Video 本地配音",
            "available": profile.get("available") is not False,
        })
        profiles.append(profile)
    return profiles


def _doubao_profiles() -> list[dict[str, Any]]:
    provider_available = provider_status(CLOUD_PROVIDER_ID).value == "available"
    profiles: list[dict[str, Any]] = []
    for role, (variable, display_name) in DOUBAO_ROLE_VOICE_ENVS.items():
        voice_id = os.environ.get(variable, "").strip()
        if not voice_id:
            continue
        prefix = variable.removesuffix("_VOICE_TYPE")
        resource_id = os.environ.get(f"{prefix}_RESOURCE_ID", "").strip()
        if not resource_id:
            resource_id = "seed-icl-2.0" if voice_id.startswith("S_") else "seed-tts-2.0"
        enabled = os.environ.get(f"{prefix}_ENABLED", "1").strip().lower() not in {
            "0", "false", "no", "off",
        }
        is_clone = voice_id.startswith("S_")
        profiles.append({
            "id": f"doubao:{role}",
            "name": display_name,
            "description": (
                "豆包声音复刻云端音色；需要账号开通匹配的 ICL 资源。"
                if is_clone
                else "豆包 Speech 2.0 公版云端音色；适合快速中文视频配音。"
            ),
            "language": "zh",
            "voice_type": "cloud_clone" if is_clone else "cloud",
            "default_engine": "doubao_icl_2_0" if is_clone else "doubao_speech_2_0",
            "provider_id": CLOUD_PROVIDER_ID,
            "provider_name": "豆包云端配音",
            "provider_voice_id": voice_id,
            "resource_id": resource_id,
            "role": role,
            "available": provider_available and enabled,
        })
    return profiles


def _profiles() -> list[dict[str, Any]]:
    return [*_local_profiles(), *_doubao_profiles()]


def _select_default(profiles: list[dict[str, Any]], stored_id: str | None) -> dict[str, Any] | None:
    if stored_id:
        selected = next((item for item in profiles if item["id"] == stored_id), None)
        # Preserve a deliberate default across temporary provider outages.
        # Generation will block with a clear unavailable state; silently
        # replacing the user's voice would change identity and may alter cost.
        if selected:
            return selected
    available_profiles = [profile for profile in profiles if profile.get("available") is not False]
    preferred_provider = os.environ.get("HAIKE_VIDEO_TTS_PROVIDER", "auto").strip().lower()
    if preferred_provider in {LOCAL_PROVIDER_ID, CLOUD_PROVIDER_ID}:
        selected = next(
            (item for item in available_profiles if item.get("provider_id") == preferred_provider and item.get("role") == "yaya"),
            None,
        ) or next((item for item in available_profiles if item.get("provider_id") == preferred_provider), None)
        if selected:
            return selected
    # Prefer the actual cloned "雅雅" profile installed by the user.  The
    # Serena fallback preserves a useful default on a clean installation.
    for preferred_name in ("雅雅", "qwen serena"):
        selected = next((item for item in available_profiles if item["name"].lower() == preferred_name), None)
        if selected:
            return selected
    return next((item for item in available_profiles if item.get("language", "").lower().startswith("zh")), None) or (available_profiles[0] if available_profiles else None)


def _profile_payload(profile: dict[str, Any] | None) -> dict[str, Any] | None:
    if not profile:
        return None
    configured_id = (
        os.environ.get("HAIKE_VIDEO_TTS_PROFILE_ID", "").strip()
        or os.environ.get("VOICEBOX_PROFILE_ID", "").strip()
    )
    configured_name = (
        os.environ.get("HAIKE_VIDEO_TTS_PROFILE_DISPLAY_NAME", "").strip()
        or os.environ.get("HAIKE_VIDEO_TTS_PROFILE_NAME", "").strip()
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
        "provider_id": profile.get("provider_id") or LOCAL_PROVIDER_ID,
        "provider_name": profile.get("provider_name") or "Haike Video 本地配音",
        "resource_id": profile.get("resource_id"),
        "role": profile.get("role"),
        "available": profile.get("available") is not False,
    }


def _safe_error(error: object) -> str:
    message = str(error or "配音任务失败")
    for variable in ("OPENAI_API_KEY", "DOUBAO_SPEECH_API_KEY"):
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
    local_status = provider_status(LOCAL_PROVIDER_ID).value
    cloud_status = provider_status(CLOUD_PROVIDER_ID).value
    providers = [
        {
            "id": LOCAL_PROVIDER_ID,
            "name": "Haike Video 本地配音",
            "status": local_status,
            "detail": "使用本机 Qwen3-TTS；无需按次付费，但速度取决于本机资源。",
        },
        {
            "id": CLOUD_PROVIDER_ID,
            "name": "豆包云端配音",
            "status": cloud_status,
            "detail": "使用豆包 Speech 2.0；速度较快且按云端服务实际用量计费。",
        },
    ]
    aggregate_status = "available" if any(item["status"] == "available" for item in providers) else "unavailable"
    previews = list(persisted.get("previews") or [])[-MAX_PREVIEWS:]
    previews.reverse()
    return {
        "provider": {
            "id": "tts_runtime",
            "name": "Haike Video 配音服务",
            "status": aggregate_status,
            "detail": "本地 Qwen3-TTS 与豆包云端配音可独立切换；任务启动后会冻结所选音色。",
        },
        "providers": providers,
        "default_voice": _profile_payload(selected),
        "profiles": [_profile_payload(profile) for profile in profiles],
        "previews": previews,
        "preview_job": persisted.get("preview_job") or {"status": "idle"},
    }


def get_default_voice() -> dict[str, Any] | None:
    """Return the current global default for project narration generation."""
    return read_audio_center().get("default_voice")


def get_voice_profile(profile_id: str) -> dict[str, Any] | None:
    """Resolve one private runtime profile without exposing provider voice ids to the UI."""
    requested = str(profile_id or "").strip()
    return next((dict(profile) for profile in _profiles() if profile.get("id") == requested), None)


def set_default_voice(payload: dict[str, Any]) -> dict[str, Any]:
    profile_id = str(payload.get("profile_id") or "").strip()
    if not profile_id:
        raise AudioCenterError("请选择一个音色后再设为通用默认")
    profiles = _profiles()
    selected = next((profile for profile in profiles if profile["id"] == profile_id), None)
    if not selected:
        raise AudioCenterError("所选音色已不存在，请刷新音色列表后重试")
    if selected.get("available") is False:
        raise AudioCenterError(f"{selected.get('provider_name') or '所选服务'}当前不可用，请先完成配置")
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
        raise AudioCenterError("当前没有已配置的配音音色，请先完成本地或云端配音配置")
    persisted = _load()
    if (persisted.get("preview_job") or {}).get("status") == "generating":
        raise AudioCenterError("已有试听正在生成，请等待完成后再试")
    selected = _select_default(profiles, str(payload.get("profile_id") or persisted.get("default_profile_id") or ""))
    if not selected:
        raise AudioCenterError("没有可用音色；请检查本地服务或云端密钥与音色配置")
    if selected.get("available") is False:
        raise AudioCenterError(f"{selected.get('provider_name') or '所选服务'}当前不可用，请先完成配置")
    if selected.get("provider_id") == CLOUD_PROVIDER_ID and len(text) > 180:
        raise AudioCenterError("云端试听请控制在 180 个字符以内；确认音色后再生成完整旁白")
    preview_id = f"VP-{uuid4().hex[:10]}"
    persisted["preview_job"] = {
        "id": preview_id,
        "status": "generating",
        "text": text,
        "profile_id": selected["id"],
        "profile_name": (_profile_payload(selected) or {}).get("name") or selected["name"],
        "provider_id": selected.get("provider_id") or LOCAL_PROVIDER_ID,
        "provider_name": selected.get("provider_name") or "Haike Video 本地配音",
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
    profile = next((item for item in _profiles() if item.get("id") == job.get("profile_id")), None)
    output_suffix = ".mp3" if profile and profile.get("provider_id") == CLOUD_PROVIDER_ID else ".wav"
    output = PREVIEW_DIRECTORY / f"{preview_id}{output_suffix}"
    if not profile:
        result = None
        error = "试听音色配置已变化，请重新选择后再试"
    else:
        result = generate_voice_audio(
            text=str(job["text"]),
            profile=profile,
            output_path=output,
            language="zh",
            sample_mode=True,
        )
        error = result.error
    persisted = _load()
    current = persisted.get("preview_job") or {}
    if result is None or not result.success or not output.is_file():
        current.update({"status": "failed", "finished_at": _now(), "error": _safe_error(error)})
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
        "duration_seconds": (result.data or {}).get("audio_duration_seconds") or (result.data or {}).get("duration"),
        "provider_id": job.get("provider_id") or LOCAL_PROVIDER_ID,
        "provider": job.get("provider_name") or "Haike Video 本地配音",
        "metadata_path": (result.data or {}).get("metadata_path"),
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
