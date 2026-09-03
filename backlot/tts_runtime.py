"""Provider-neutral TTS runtime used by the Backlot workflows.

The audio centre owns the user-facing voice catalogue.  This module only
executes one frozen voice profile and normalises cloud output for existing
WAV-based timelines.  It never falls back to another provider implicitly:
changing a voice after a task starts would be both audible and potentially
billable.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from tools.audio.doubao_tts import DoubaoTTS
from tools.audio.voicebox_tts import VoiceboxTTS
from tools.base_tool import ToolResult, ToolStatus


LOCAL_PROVIDER_ID = "voicebox_tts"
CLOUD_PROVIDER_ID = "doubao"


def _doubao_speech_rate(profile: dict[str, Any]) -> tuple[float, int]:
    """Translate the audio-centre multiplier to the Speech 2.0 API scale."""
    try:
        playback_rate = round(float(profile.get("speech_rate", 1.0)), 2)
    except (TypeError, ValueError):
        return 1.0, 0
    if not 0.50 <= playback_rate <= 2.00:
        return 1.0, 0
    return playback_rate, int(round((playback_rate - 1.0) * 100))


def provider_status(provider_id: str) -> ToolStatus:
    if provider_id == LOCAL_PROVIDER_ID:
        return VoiceboxTTS().get_status()
    if provider_id == CLOUD_PROVIDER_ID:
        return DoubaoTTS().get_status()
    return ToolStatus.UNAVAILABLE


def generate_voice_audio(
    *,
    text: str,
    profile: dict[str, Any],
    output_path: str | Path,
    language: str = "zh",
    sample_mode: bool = False,
) -> ToolResult:
    """Generate one take with the exact provider frozen in ``profile``."""
    provider_id = str(profile.get("provider_id") or LOCAL_PROVIDER_ID)
    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)

    if provider_id == LOCAL_PROVIDER_ID:
        return VoiceboxTTS().execute({
            "text": text,
            "profile_id": str(profile["id"]),
            "language": language,
            "sample_mode": sample_mode,
            "output_path": str(target),
        })

    if provider_id != CLOUD_PROVIDER_ID:
        return ToolResult(success=False, error=f"不支持的配音供应商：{provider_id}")

    voice_id = str(profile.get("provider_voice_id") or "").strip()
    if not voice_id:
        return ToolResult(success=False, error="豆包音色缺少 provider_voice_id 配置")

    cloud_output = target
    needs_wav = target.suffix.lower() == ".wav"
    if needs_wav:
        cloud_output = target.with_name(f".{target.stem}.doubao.mp3")
    metadata_path = target.with_suffix(target.suffix + ".doubao.json")
    playback_rate, provider_speech_rate = _doubao_speech_rate(profile)
    result = DoubaoTTS().execute({
        "text": text,
        "voice_id": voice_id,
        "resource_id": str(profile.get("resource_id") or DoubaoTTS.DEFAULT_RESOURCE_ID),
        "format": "mp3",
        "sample_rate": 24000,
        "speech_rate": provider_speech_rate,
        "enable_timestamp": True,
        "sample_mode": sample_mode,
        "output_path": str(cloud_output),
        "metadata_path": str(metadata_path),
    })
    if not result.success or not cloud_output.is_file():
        return result

    if needs_wav:
        conversion = _convert_to_wav(cloud_output, target)
        if conversion:
            return ToolResult(
                success=False,
                error=conversion,
                artifacts=list(result.artifacts),
                cost_usd=result.cost_usd,
                duration_seconds=result.duration_seconds,
                model=result.model,
            )
        try:
            cloud_output.unlink()
        except OSError:
            pass
        result.artifacts = [str(target) if item == str(cloud_output) else item for item in result.artifacts]
        result.data["output"] = str(target)
        result.data["normalised_format"] = "wav_pcm_s16le_mono_24000"

    result.data.update({
        "profile_id": str(profile["id"]),
        "profile_name": str(profile.get("name") or profile["id"]),
        "provider_id": provider_id,
        "playback_rate": playback_rate,
        "provider_speech_rate": provider_speech_rate,
    })
    return result


def _convert_to_wav(source: Path, target: Path) -> str | None:
    ffmpeg = shutil.which(os.environ.get("FFMPEG_BINARY", "ffmpeg"))
    if not ffmpeg:
        return "本机未发现 FFmpeg，无法把豆包音频规范化为项目 WAV"
    completed = subprocess.run(
        [
            ffmpeg,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(source),
            "-ac",
            "1",
            "-ar",
            "24000",
            "-c:a",
            "pcm_s16le",
            str(target),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
    )
    if completed.returncode != 0 or not target.is_file() or target.stat().st_size <= 0:
        detail = (completed.stderr or completed.stdout or "未知错误").strip()
        return f"豆包音频转换 WAV 失败：{detail[:600]}"
    return None
