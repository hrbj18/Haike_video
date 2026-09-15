"""Software-wide 腾讯云 (Tencent Cloud) credential + service management.

Mirrors the RunningHub configuration contract: credentials live only in the
Git-ignored ``.env.secrets.local`` file, are never returned to the browser in
clear text, and every save/test path surfaces a Chinese, actionable error.
"""

from __future__ import annotations

import os
import re
from typing import Any

from backlot.ai_text import (
    _atomic_write_text,
    _effective_value,
    _mask_secret,
    _quote_env_value,
    _read_env_file,
    _secrets_path,
)
from lib.tencent_cloud import (
    ASR_HOST,
    ASR_SERVICE,
    ASR_VERSION,
    DEFAULT_REGION,
    SUPPORTED_REGIONS,
    TTS_HOST,
    TTS_SERVICE,
    TTS_VERSION,
    TencentCloudError,
    mask_secret,
    redact_secret,
    tencent_credentials,
    tencent_region,
)


class TencentConfigError(RuntimeError):
    """A user-correctable Tencent Cloud configuration error."""


CONFIG_KEYS = (
    "TENCENT_SECRET_ID",
    "TENCENT_SECRET_KEY",
    "TENCENT_TTS_REGION",
    "TENCENT_ASR_REGION",
)

CREDENTIAL_KEYS = ("TENCENT_SECRET_ID", "TENCENT_SECRET_KEY")

TENCENT_CONSOLE_TTS = "https://console.cloud.tencent.com/tts"
TENCENT_CONSOLE_ASR = "https://console.cloud.tencent.com/asr"

_ENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")

# The six curated presets imported into the 配音中心 for this account.
# Three female + three male, chosen to cover narration, news and chat scenes.
TENCENT_PRESET_VOICES: tuple[dict[str, Any], ...] = (
    {
        "profile_id": "tencent:voice:502001",
        "voice_id": "502001",
        "name": "腾讯·智小柔",
        "gender": "female",
        "scene": "聊天女声",
        "engine": "超自然大模型",
        "description": "超自然大模型温柔女声，语气松弛自然，适合口播、vlog 与生活类旁白。",
    },
    {
        "profile_id": "tencent:voice:501001",
        "voice_id": "501001",
        "name": "腾讯·智兰",
        "gender": "female",
        "scene": "资讯女声",
        "engine": "大模型",
        "description": "大模型资讯女声，吐字清晰、节奏稳定，适合新闻播报与知识讲解。",
    },
    {
        "profile_id": "tencent:voice:601009",
        "voice_id": "601009",
        "name": "腾讯·爱小芊",
        "gender": "female",
        "scene": "聊天女声",
        "engine": "大模型",
        "description": "大模型年轻女声，明亮活泼，适合种草、测评与快节奏短视频。",
    },
    {
        "profile_id": "tencent:voice:502005",
        "voice_id": "502005",
        "name": "腾讯·智小解",
        "gender": "male",
        "scene": "解说男声",
        "engine": "超自然大模型",
        "description": "超自然大模型解说男声，沉稳有磁性，适合影视解说与纪录片旁白。",
    },
    {
        "profile_id": "tencent:voice:501003",
        "voice_id": "501003",
        "name": "腾讯·智宇",
        "gender": "male",
        "scene": "阅读男声",
        "engine": "大模型",
        "description": "大模型阅读男声，语流平稳，适合长文朗读与课程讲解。",
    },
    {
        "profile_id": "tencent:voice:501005",
        "voice_id": "501005",
        "name": "腾讯·飞镜",
        "gender": "male",
        "scene": "聊天男声",
        "engine": "大模型",
        "description": "大模型聊天男声，亲和自然，适合对话类与口播类内容。",
    },
)

PRESET_PROFILE_IDS = tuple(item["profile_id"] for item in TENCENT_PRESET_VOICES)


def _read_env() -> tuple[list[str], dict[str, str]]:
    return _read_env_file(_secrets_path())


def _service_status() -> dict[str, Any]:
    credentials = tencent_credentials()
    return {
        "configured": credentials.configured,
        "secret_id_configured": bool(credentials.secret_id),
        "secret_key_configured": bool(credentials.secret_key),
        "secret_id_masked": mask_secret(credentials.secret_id) or _mask_secret(credentials.secret_id),
        "tts_region": tencent_region(TTS_SERVICE),
        "asr_region": tencent_region(ASR_SERVICE),
    }


def read_tencent_config() -> dict[str, Any]:
    """Return Tencent Cloud configuration health without leaking the SecretKey."""
    _, values = _read_env()
    status = _service_status()
    return {
        **status,
        "storage": ".env.secrets.local",
        "regions": list(SUPPORTED_REGIONS),
        "tts": {
            "id": "tencent_tts",
            "name": "腾讯云语音合成 (TTS)",
            "configured": status["configured"],
            "region": status["tts_region"],
            "endpoint": TTS_HOST,
            "version": TTS_VERSION,
            "console_url": TENCENT_CONSOLE_TTS,
            "voice_count": len(TENCENT_PRESET_VOICES),
        },
        "asr": {
            "id": "tencent_asr",
            "name": "腾讯云语音识别 (ASR)",
            "configured": status["configured"],
            "region": status["asr_region"],
            "endpoint": ASR_HOST,
            "version": ASR_VERSION,
            "console_url": TENCENT_CONSOLE_ASR,
        },
        "preset_voices": [dict(item) for item in TENCENT_PRESET_VOICES],
        "secret_id_env": "TENCENT_SECRET_ID",
        "secret_key_env": "TENCENT_SECRET_KEY",
        "secret_id_source_present": bool(_effective_value("TENCENT_SECRET_ID", values)),
    }


def _apply_updates(path, lines: list[str], updates: dict[str, str]) -> None:
    """Rewrite only the managed keys, preserving the rest of the file."""
    output: list[str] = []
    seen: set[str] = set()
    for line in lines:
        match = _ENV_LINE.match(line)
        key = match.group(1) if match else ""
        if key in updates:
            if key not in seen:
                output.append(f"{key}={_quote_env_value(updates[key])}")
                seen.add(key)
            continue
        output.append(line)
    if output and output[-1].strip():
        output.append("")
    for key in CONFIG_KEYS:
        if key in updates and key not in seen:
            output.append(f"{key}={_quote_env_value(updates[key])}")
    _atomic_write_text(path, "\n".join(output).rstrip() + "\n")


def save_tencent_config(payload: dict[str, Any]) -> dict[str, Any]:
    """Persist SecretId / SecretKey / regions; blank secrets keep the old value."""
    path = _secrets_path()
    lines, current = _read_env_file(path)

    secret_id = str(payload.get("secret_id") or "").strip()
    secret_key = str(payload.get("secret_key") or "").strip()
    for label, value in (("SecretId", secret_id), ("SecretKey", secret_key)):
        if value and (len(value) > 200 or re.search(r"[\r\n\s]", value)):
            raise TencentConfigError(f"腾讯云 {label} 格式无效：不应包含空格或换行")

    tts_region = str(payload.get("tts_region") or current.get("TENCENT_TTS_REGION") or DEFAULT_REGION).strip()
    asr_region = str(payload.get("asr_region") or current.get("TENCENT_ASR_REGION") or DEFAULT_REGION).strip()
    for label, region in (("语音合成", tts_region), ("语音识别", asr_region)):
        if region not in SUPPORTED_REGIONS:
            raise TencentConfigError(f"{label}地域仅支持：{'、'.join(SUPPORTED_REGIONS)}")

    updates = {
        "TENCENT_SECRET_ID": secret_id or current.get("TENCENT_SECRET_ID", ""),
        "TENCENT_SECRET_KEY": secret_key or current.get("TENCENT_SECRET_KEY", ""),
        "TENCENT_TTS_REGION": tts_region,
        "TENCENT_ASR_REGION": asr_region,
    }
    if not updates["TENCENT_SECRET_ID"] or not updates["TENCENT_SECRET_KEY"]:
        raise TencentConfigError("请同时填写腾讯云 SecretId 与 SecretKey")

    _apply_updates(path, lines, updates)
    for key, value in updates.items():
        os.environ[key] = value
    return read_tencent_config()


def clear_tencent_config() -> dict[str, Any]:
    """Remove Tencent Cloud credentials from the local secrets file."""
    path = _secrets_path()
    lines, _ = _read_env_file(path)
    output: list[str] = []
    for line in lines:
        match = _ENV_LINE.match(line)
        if match and match.group(1) in CREDENTIAL_KEYS:
            continue
        output.append(line)
    while output and not output[-1].strip():
        output.pop()
    _atomic_write_text(path, ("\n".join(output).rstrip() + "\n") if output else "")
    for key in CREDENTIAL_KEYS:
        os.environ.pop(key, None)
    return read_tencent_config()


def test_tencent_tts_connection() -> dict[str, Any]:
    """Synthesise one short sentence through Tencent Cloud TTS (billable)."""
    from tools.audio.tencent_tts import TencentTTS

    if not tencent_credentials().configured:
        raise TencentConfigError("尚未配置腾讯云 SecretId / SecretKey")
    tool = TencentTTS()
    output = _secrets_path().parent / ".backlot" / "audio" / "tencent-tts-selftest.mp3"
    result = tool.execute({
        "text": "腾讯云语音合成连通测试，一二三四五。",
        "voice_id": "101001",
        "format": "mp3",
        "sample_rate": 16000,
        "playback_rate": 1.0,
        "enable_timestamp": False,
        "output_path": str(output),
        "timeout_seconds": 60,
    })
    if not result.success:
        raise TencentConfigError(str(result.error or "腾讯云语音合成测试失败"))
    data = result.data or {}
    return {
        "ok": True,
        "service": "tts",
        "region": tencent_region(TTS_SERVICE),
        "voice_id": data.get("voice_id"),
        "chunk_count": data.get("chunk_count"),
        "audio_duration_seconds": data.get("audio_duration_seconds"),
        "scope": "腾讯云语音合成连通测试（按字计费）",
    }


def test_tencent_asr_connection() -> dict[str, Any]:
    """Run the self-contained TTS→ASR round-trip connectivity test."""
    from backlot.tencent_asr import TencentASRError
    from backlot.tencent_asr import test_tencent_asr_connection as run

    try:
        return run()
    except TencentASRError as exc:
        raise TencentConfigError(str(exc)) from exc
    except TencentCloudError as exc:
        raise TencentConfigError(redact_secret(exc)) from exc


__all__ = [
    "TencentConfigError",
    "CONFIG_KEYS",
    "TENCENT_PRESET_VOICES",
    "PRESET_PROFILE_IDS",
    "TENCENT_CONSOLE_TTS",
    "TENCENT_CONSOLE_ASR",
    "read_tencent_config",
    "save_tencent_config",
    "clear_tencent_config",
    "test_tencent_tts_connection",
    "test_tencent_asr_connection",
]
