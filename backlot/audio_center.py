"""Software-wide Haike Video TTS provider configuration and previews.

Project narration remains an auditable project asset.  Voice identity and
short listening tests live here instead, so a creator configures a voice once
and every project can deliberately reference that shared choice.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from hashlib import sha256
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from backlot.state import REPO_ROOT
from backlot.avatar_roles import find_avatar_role_by_voice_profile
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
MAX_CUSTOM_CLOUD_PROFILES = 24
MAX_CLOUD_VOICE_NAME_LENGTH = 48
MAX_CLOUD_VOICE_ID_LENGTH = 256
SUPPORTED_DOUBAO_RESOURCE_IDS = {"seed-tts-2.0", "seed-icl-2.0"}
VISIBLE_LOCAL_PROFILE_NAMES = (
    "雅雅",
    "檬檬",
    "雅雅（强情感版）",
    "檬檬（强情感版）",
)
DEFAULT_CLOUD_PLAYBACK_RATE = 1.25
MIN_PLAYBACK_RATE = 0.50
MAX_PLAYBACK_RATE = 2.00
DOUBAO_ROLE_SOURCES = {
    "yaya": {
        "name": "豆包雅雅",
        "sources": (
            ("DOUBAO_SPEECH_YAYA_VOICE_TYPE", "DOUBAO_SPEECH_YAYA_RESOURCE_ID", "doubao:yaya"),
            # Preserve the existing public-female profile id when it is the
            # active source. Existing avatar role bindings then remain valid.
            ("DOUBAO_SPEECH_PUBLIC_VOICE_TYPE", "DOUBAO_SPEECH_PUBLIC_RESOURCE_ID", "doubao:public_female"),
        ),
    },
    "mengmeng": {
        "name": "豆包檬檬",
        "sources": (
            ("DOUBAO_SPEECH_MENGMENG_VOICE_TYPE", "DOUBAO_SPEECH_MENGMENG_RESOURCE_ID", "doubao:mengmeng"),
            ("DOUBAO_SPEECH_PUBLIC_MALE_VOICE_TYPE", "DOUBAO_SPEECH_PUBLIC_MALE_RESOURCE_ID", "doubao:public_male"),
        ),
    },
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


def _playback_rate(value: object, *, fallback: float = DEFAULT_CLOUD_PLAYBACK_RATE) -> float:
    """Return a UI rate multiplier, never exposing the provider's raw scale."""
    try:
        rate = float(value)
    except (TypeError, ValueError):
        return fallback
    if not MIN_PLAYBACK_RATE <= rate <= MAX_PLAYBACK_RATE:
        return fallback
    return round(rate, 2)


def _required_playback_rate(value: object) -> float:
    rate = _playback_rate(value, fallback=-1.0)
    if rate < 0:
        raise AudioCenterError("语速请设置在 0.50× 到 2.00× 之间")
    return rate


def _doubao_rate_value(playback_rate: float) -> int:
    """Map 1.25x into Doubao Speech 2.0's 25-point rate scale."""
    return int(round((playback_rate - 1.0) * 100))


def _load() -> dict[str, Any]:
    if not AUDIO_CENTER_FILE.is_file():
        return {
            "version": "1.2",
            "default_profile_id": None,
            "cloud_playback_rate": DEFAULT_CLOUD_PLAYBACK_RATE,
            "cloud_voice_rates": {},
            "custom_cloud_profiles": [],
            "previews": [],
            "preview_job": {"status": "idle"},
        }
    try:
        data = json.loads(AUDIO_CENTER_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    data.setdefault("version", "1.2")
    data.setdefault("default_profile_id", None)
    data["cloud_playback_rate"] = _playback_rate(data.get("cloud_playback_rate"))
    if not isinstance(data.get("cloud_voice_rates"), dict):
        data["cloud_voice_rates"] = {}
    if not isinstance(data.get("custom_cloud_profiles"), list):
        data["custom_cloud_profiles"] = []
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


def _cloud_voice_playback_rate(persisted: dict[str, Any], profile_id: str) -> float:
    rates = persisted.get("cloud_voice_rates")
    value = rates.get(profile_id) if isinstance(rates, dict) else None
    return _playback_rate(value, fallback=_playback_rate(persisted.get("cloud_playback_rate")))


def _make_doubao_profile(
    *,
    profile_id: str,
    name: str,
    voice_id: str,
    resource_id: str,
    playback_rate: float,
    provider_available: bool,
    role: str | None = None,
    enabled: bool = True,
    custom: bool = False,
) -> dict[str, Any]:
    is_clone = voice_id.startswith("S_")
    voice_signature = "doubao:" + sha256(
        json.dumps(
            {
                "provider": CLOUD_PROVIDER_ID,
                "voice_id": voice_id,
                "resource_id": resource_id,
                "engine": "doubao_icl_2_0" if is_clone else "doubao_speech_2_0",
                "playback_rate": playback_rate,
            },
            sort_keys=True,
            ensure_ascii=True,
        ).encode("utf-8")
    ).hexdigest()
    return {
        "id": profile_id,
        "name": name,
        "description": (
            "用户添加的豆包声音复刻音色；需要账号开通匹配的 ICL 资源。"
            if custom and is_clone
            else "用户添加的豆包 Speech 2.0 云端音色；可先生成短试听确认。"
            if custom
            else "豆包声音复刻云端音色；需要账号开通匹配的 ICL 资源。"
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
        "voice_signature": voice_signature,
        "speech_rate": playback_rate,
        "provider_speech_rate": _doubao_rate_value(playback_rate),
        "is_custom_cloud_voice": custom,
        "available": provider_available and enabled,
    }


def _custom_cloud_profile_records(persisted: dict[str, Any]) -> list[dict[str, str]]:
    """Read only valid local custom records; malformed legacy state is ignored."""
    records: list[dict[str, str]] = []
    seen: set[str] = set()
    raw_items = persisted.get("custom_cloud_profiles")
    if not isinstance(raw_items, list):
        return records
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        profile_id = str(raw.get("id") or "").strip()
        name = str(raw.get("name") or "").strip()
        voice_id = str(raw.get("voice_id") or "").strip()
        resource_id = str(raw.get("resource_id") or "").strip()
        if (
            not profile_id.startswith("doubao:custom:")
            or profile_id in seen
            or not name
            or not voice_id
            or resource_id not in SUPPORTED_DOUBAO_RESOURCE_IDS
        ):
            continue
        seen.add(profile_id)
        records.append({
            "id": profile_id,
            "name": name,
            "voice_id": voice_id,
            "resource_id": resource_id,
            "created_at": str(raw.get("created_at") or ""),
        })
    return records


def _doubao_profiles(persisted: dict[str, Any]) -> list[dict[str, Any]]:
    provider_available = provider_status(CLOUD_PROVIDER_ID).value == "available"
    profiles: list[dict[str, Any]] = []
    for role, definition in DOUBAO_ROLE_SOURCES.items():
        configured_sources = [item for item in definition["sources"] if os.environ.get(item[0], "").strip()]
        source = next(
            (
                item for item in configured_sources
                if os.environ.get(item[0].removesuffix("_VOICE_TYPE") + "_ENABLED", "1").strip().lower()
                not in {"0", "false", "no", "off"}
            ),
            configured_sources[0] if configured_sources else None,
        )
        if source is None:
            continue
        variable, resource_variable, profile_id = source
        voice_id = os.environ.get(variable, "").strip()
        if not voice_id:
            continue
        resource_id = os.environ.get(resource_variable, "").strip()
        if not resource_id:
            resource_id = "seed-icl-2.0" if voice_id.startswith("S_") else "seed-tts-2.0"
        enabled = os.environ.get(variable.removesuffix("_VOICE_TYPE") + "_ENABLED", "1").strip().lower() not in {
            "0", "false", "no", "off",
        }
        profiles.append(_make_doubao_profile(
            profile_id=profile_id,
            name=str(definition["name"]),
            voice_id=voice_id,
            resource_id=resource_id,
            playback_rate=_cloud_voice_playback_rate(persisted, profile_id),
            provider_available=provider_available,
            role=role,
            enabled=enabled,
        ))
    for record in _custom_cloud_profile_records(persisted):
        profiles.append(_make_doubao_profile(
            profile_id=record["id"],
            name=record["name"],
            voice_id=record["voice_id"],
            resource_id=record["resource_id"],
            playback_rate=_cloud_voice_playback_rate(persisted, record["id"]),
            provider_available=provider_available,
            custom=True,
        ))
    return profiles


def _runtime_profiles(persisted: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    state = persisted or _load()
    return [*_local_profiles(), *_doubao_profiles(state)]


def _catalog_profiles(persisted: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Expose only the six deliberate choices without deleting legacy voices."""
    by_name = {str(profile.get("name") or ""): profile for profile in _local_profiles()}
    selected_local = [by_name[name] for name in VISIBLE_LOCAL_PROFILE_NAMES if name in by_name]
    state = persisted or _load()
    return [*selected_local, *_doubao_profiles(state)]


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
    # The visible catalogue deliberately contains only the approved six
    # identities. Never revive a historical Qwen preset as a silent default.
    for preferred_name in ("雅雅", "豆包雅雅"):
        selected = next((item for item in available_profiles if item["name"].lower() == preferred_name), None)
        if selected:
            return selected
    return next((item for item in available_profiles if item.get("language", "").lower().startswith("zh")), None) or (available_profiles[0] if available_profiles else None)


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
    payload = {
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
        "voice_signature": profile.get("voice_signature"),
        "available": profile.get("available") is not False,
        "is_custom_cloud_voice": profile.get("is_custom_cloud_voice") is True,
    }
    if payload["provider_id"] == CLOUD_PROVIDER_ID:
        playback_rate = _playback_rate(profile.get("speech_rate"))
        payload.update({
            "speech_rate": playback_rate,
            "provider_speech_rate": _doubao_rate_value(playback_rate),
        })
    return payload


def _safe_error(error: object) -> str:
    message = str(error or "配音任务失败")
    for variable in ("OPENAI_API_KEY", "DOUBAO_SPEECH_API_KEY"):
        secret = os.environ.get(variable)
        if secret:
            message = message.replace(secret, "[已隐藏]")
    return message[:1200]


def _freeze_preview_profile(profile: dict[str, Any], playback_rate: float) -> dict[str, Any]:
    """Keep the provider-facing identity fixed after a user starts a preview."""
    frozen = {
        key: profile.get(key)
        for key in (
            "id",
            "name",
            "description",
            "language",
            "voice_type",
            "default_engine",
            "provider_id",
            "provider_name",
            "provider_voice_id",
            "resource_id",
            "role",
            "voice_signature",
        )
    }
    if frozen.get("provider_id") == CLOUD_PROVIDER_ID:
        frozen["speech_rate"] = playback_rate
        frozen["provider_speech_rate"] = _doubao_rate_value(playback_rate)
    return frozen


def _public_preview_job(job: dict[str, Any]) -> dict[str, Any]:
    """Do not send provider-facing voice identifiers back to the browser."""
    return {
        key: value
        for key, value in job.items()
        if key != "profile_snapshot"
    }


def read_audio_center() -> dict[str, Any]:
    """Read global voice configuration plus the embedded service state."""
    persisted = _load()
    profiles = _catalog_profiles(persisted)
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
        "cloud_playback_rate": _playback_rate(persisted.get("cloud_playback_rate")),
        "previews": previews,
        "preview_job": _public_preview_job(persisted.get("preview_job") or {"status": "idle"}),
    }


def get_default_voice() -> dict[str, Any] | None:
    """Return the current global default for project narration generation."""
    return read_audio_center().get("default_voice")


def get_voice_profile(profile_id: str) -> dict[str, Any] | None:
    """Resolve one private runtime profile without exposing provider voice ids to the UI."""
    requested = str(profile_id or "").strip()
    return next((dict(profile) for profile in _runtime_profiles() if profile.get("id") == requested), None)


def set_default_voice(payload: dict[str, Any]) -> dict[str, Any]:
    profile_id = str(payload.get("profile_id") or "").strip()
    if not profile_id:
        raise AudioCenterError("请选择一个音色后再设为通用默认")
    profiles = _catalog_profiles(_load())
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


def set_cloud_playback_rate(payload: dict[str, Any]) -> dict[str, Any]:
    """Set the default rate used by new Doubao tasks; active jobs stay frozen."""
    rate = _required_playback_rate(payload.get("playback_rate"))
    persisted = _load()
    persisted["cloud_playback_rate"] = rate
    persisted["cloud_playback_rate_updated_at"] = _now()
    _write(persisted)
    return read_audio_center()


def set_cloud_voice_playback_rate(profile_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Update one cloud voice without altering the other cloud voice settings."""
    requested = str(profile_id or "").strip()
    persisted = _load()
    profile = next((item for item in _catalog_profiles(persisted) if item.get("id") == requested), None)
    if not profile or profile.get("provider_id") != CLOUD_PROVIDER_ID:
        raise AudioCenterError("未找到可配置语速的豆包音色，请刷新后重试")
    rate = _required_playback_rate(payload.get("playback_rate"))
    rates = dict(persisted.get("cloud_voice_rates") or {})
    rates[requested] = rate
    persisted["cloud_voice_rates"] = rates
    persisted["cloud_voice_rates_updated_at"] = _now()
    _write(persisted)
    return read_audio_center()


def add_custom_cloud_voice(payload: dict[str, Any]) -> dict[str, Any]:
    """Store one user-supplied Doubao voice locally without exposing it to the UI."""
    name = str(payload.get("name") or "").strip()
    voice_id = str(payload.get("voice_id") or "").strip()
    if not name:
        raise AudioCenterError("请为新增豆包音色填写一个显示名称")
    if len(name) > MAX_CLOUD_VOICE_NAME_LENGTH:
        raise AudioCenterError(f"音色名称请控制在 {MAX_CLOUD_VOICE_NAME_LENGTH} 个字符以内")
    if not voice_id or len(voice_id) > MAX_CLOUD_VOICE_ID_LENGTH or any(char.isspace() for char in voice_id):
        raise AudioCenterError("豆包音色 ID 格式无效，请粘贴完整且不含空格的 ID")
    resource_id = str(payload.get("resource_id") or "").strip() or (
        "seed-icl-2.0" if voice_id.startswith("S_") else "seed-tts-2.0"
    )
    if resource_id not in SUPPORTED_DOUBAO_RESOURCE_IDS:
        raise AudioCenterError("豆包资源仅支持 seed-tts-2.0 或 seed-icl-2.0")
    persisted = _load()
    records = _custom_cloud_profile_records(persisted)
    if len(records) >= MAX_CUSTOM_CLOUD_PROFILES:
        raise AudioCenterError(f"最多可保存 {MAX_CUSTOM_CLOUD_PROFILES} 个自定义豆包音色，请先移除不再使用的音色")
    existing = _doubao_profiles(persisted)
    if any(item.get("provider_voice_id") == voice_id and item.get("resource_id") == resource_id for item in existing):
        raise AudioCenterError("该豆包音色已存在，无需重复添加")
    if any(record["name"] == name for record in records):
        raise AudioCenterError("已有同名自定义豆包音色，请换一个显示名称")
    profile_id = f"doubao:custom:{uuid4().hex}"
    rate = _required_playback_rate(payload.get("playback_rate", DEFAULT_CLOUD_PLAYBACK_RATE))
    records.append({
        "id": profile_id,
        "name": name,
        "voice_id": voice_id,
        "resource_id": resource_id,
        "created_at": _now(),
    })
    rates = dict(persisted.get("cloud_voice_rates") or {})
    rates[profile_id] = rate
    persisted["custom_cloud_profiles"] = records
    persisted["cloud_voice_rates"] = rates
    persisted["custom_cloud_profiles_updated_at"] = _now()
    _write(persisted)
    return read_audio_center()


def remove_custom_cloud_voice(profile_id: str) -> dict[str, Any]:
    """Remove a user-added local configuration, never a built-in role voice."""
    requested = str(profile_id or "").strip()
    persisted = _load()
    records = _custom_cloud_profile_records(persisted)
    remaining = [record for record in records if record["id"] != requested]
    if len(remaining) == len(records):
        if requested.startswith("doubao:"):
            raise AudioCenterError("内置豆包音色不能移除；只有你新增的音色可删除")
        raise AudioCenterError("未找到要移除的自定义豆包音色")
    bound_role = find_avatar_role_by_voice_profile(requested)
    if bound_role:
        role_name = str(bound_role.get("name") or bound_role.get("role_id") or "该数字人角色")
        raise AudioCenterError(f"该音色仍关联数字人角色“{role_name}”，请先解除关联再移除")
    persisted["custom_cloud_profiles"] = remaining
    rates = dict(persisted.get("cloud_voice_rates") or {})
    rates.pop(requested, None)
    persisted["cloud_voice_rates"] = rates
    if persisted.get("default_profile_id") == requested:
        persisted["default_profile_id"] = None
    persisted["custom_cloud_profiles_updated_at"] = _now()
    _write(persisted)
    return read_audio_center()


def start_preview(payload: dict[str, Any]) -> dict[str, Any]:
    text = str(payload.get("text") or "").strip()
    if not text:
        raise AudioCenterError("请先输入一段试听文案")
    if len(text) > 500:
        raise AudioCenterError("试听文案请控制在 500 个字符以内")
    persisted = _load()
    profiles = _catalog_profiles(persisted)
    if not profiles:
        raise AudioCenterError("当前没有已配置的配音音色，请先完成本地或云端配音配置")
    if (persisted.get("preview_job") or {}).get("status") == "generating":
        raise AudioCenterError("已有试听正在生成，请等待完成后再试")
    requested_profile_id = str(payload.get("profile_id") or "").strip()
    if requested_profile_id:
        selected = next((profile for profile in profiles if profile["id"] == requested_profile_id), None)
        if not selected:
            raise AudioCenterError("所选音色已不存在，请刷新音色列表后重新选择")
    else:
        selected = _select_default(profiles, str(persisted.get("default_profile_id") or ""))
    if not selected:
        raise AudioCenterError("没有可用音色；请检查本地服务或云端密钥与音色配置")
    if selected.get("available") is False:
        raise AudioCenterError(f"{selected.get('provider_name') or '所选服务'}当前不可用，请先完成配置")
    if selected.get("provider_id") == CLOUD_PROVIDER_ID and len(text) > 180:
        raise AudioCenterError("云端试听请控制在 180 个字符以内；确认音色后再生成完整旁白")
    playback_rate = _required_playback_rate(
        payload.get("playback_rate", selected.get("speech_rate", 1.0) if selected.get("provider_id") == CLOUD_PROVIDER_ID else 1.0)
    )
    preview_id = f"VP-{uuid4().hex[:10]}"
    persisted["preview_job"] = {
        "id": preview_id,
        "status": "generating",
        "text": text,
        "profile_id": selected["id"],
        "profile_name": (_profile_payload(selected) or {}).get("name") or selected["name"],
        "provider_id": selected.get("provider_id") or LOCAL_PROVIDER_ID,
        "provider_name": selected.get("provider_name") or "Haike Video 本地配音",
        "playback_rate": playback_rate,
        "provider_speech_rate": _doubao_rate_value(playback_rate) if selected.get("provider_id") == CLOUD_PROVIDER_ID else None,
        "profile_snapshot": _freeze_preview_profile(selected, playback_rate),
        "started_at": _now(),
        "error": "",
    }
    _write(persisted)
    return read_audio_center()


def _apply_local_preview_tempo(path: Path, playback_rate: float) -> str | None:
    """Use tempo adjustment only for a standalone local audition, never a project track."""
    if abs(playback_rate - 1.0) < 0.001:
        return None
    ffmpeg = shutil.which(os.environ.get("FFMPEG_BINARY", "ffmpeg"))
    if not ffmpeg:
        return "本机未发现 FFmpeg，无法生成指定语速的本地试听"
    temporary = path.with_name(f".{path.stem}.tempo{path.suffix}")
    codec = ["-c:a", "pcm_s16le"] if path.suffix.lower() == ".wav" else ["-c:a", "libmp3lame", "-q:a", "2"]
    try:
        completed = subprocess.run(
            [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(path), "-filter:a", f"atempo={playback_rate:.4f}", *codec, str(temporary)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
            check=False,
        )
        if completed.returncode != 0 or not temporary.is_file() or temporary.stat().st_size <= 0:
            detail = (completed.stderr or completed.stdout or "未知错误").strip()
            return f"本地试听语速处理失败：{detail[:600]}"
        temporary.replace(path)
        return None
    except (OSError, subprocess.SubprocessError) as exc:
        return f"本地试听语速处理失败：{exc}"
    finally:
        temporary.unlink(missing_ok=True)


def generate_preview() -> dict[str, Any]:
    """Run the queued short preview without attaching it to a video project."""
    persisted = _load()
    job = persisted.get("preview_job") or {}
    if job.get("status") != "generating":
        raise AudioCenterError("当前没有待生成的配音试听")
    preview_id = str(job["id"])
    snapshot = job.get("profile_snapshot")
    profile = dict(snapshot) if isinstance(snapshot, dict) else None
    output_suffix = ".mp3" if profile and profile.get("provider_id") == CLOUD_PROVIDER_ID else ".wav"
    output = PREVIEW_DIRECTORY / f"{preview_id}{output_suffix}"
    if not profile:
        result = None
        error = "这条旧版试听任务没有冻结音色配置，请重新选择后再试"
    else:
        playback_rate = _required_playback_rate(job.get("playback_rate", 1.0))
        if profile.get("provider_id") == CLOUD_PROVIDER_ID:
            # Queue rate, rather than a mutable global setting, is the sample
            # contract once the user has clicked generate.
            profile["speech_rate"] = playback_rate
            profile["provider_speech_rate"] = _doubao_rate_value(playback_rate)
        result = generate_voice_audio(
            text=str(job["text"]),
            profile=profile,
            output_path=output,
            language="zh",
            sample_mode=True,
        )
        error = result.error
        if result.success and output.is_file() and profile.get("provider_id") == LOCAL_PROVIDER_ID:
            error = _apply_local_preview_tempo(output, playback_rate)
            if error:
                result = None
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
        "playback_rate": job.get("playback_rate", 1.0),
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
