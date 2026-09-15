"""Provider-neutral TTS runtime used by the Backlot workflows.

The audio centre owns the user-facing voice catalogue.  This module only
executes one frozen voice profile and normalises cloud output for existing
WAV-based timelines.  It never falls back to another provider implicitly:
changing a voice after a task starts would be both audible and potentially
billable.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from lib.ffmpeg_locator import resolve_ffmpeg
from tools.audio.doubao_tts import DoubaoTTS
from tools.audio.tencent_tts import TencentTTS, playback_rate_to_tencent_speed
from tools.audio.voicebox_tts import VoiceboxTTS
from tools.base_tool import ToolResult, ToolStatus


LOCAL_PROVIDER_ID = "voicebox_tts"
CLOUD_PROVIDER_ID = "doubao"
TENCENT_PROVIDER_ID = "tencent"
CLOUD_PROVIDER_IDS = (CLOUD_PROVIDER_ID, TENCENT_PROVIDER_ID)

# Doubao's Speech 2.0 endpoint only offers mp3 / ogg_opus / pcm, so it keeps the
# lossy path.  Tencent additionally offers lossless WAV and we always take it.
TENCENT_CLOUD_FORMAT = "wav"

MIN_PLAYBACK_RATE = 0.50
MAX_PLAYBACK_RATE = 2.00


def _profile_playback_rate(profile: dict[str, Any]) -> float:
    """UI multiplier frozen into the profile, clamped to the supported window."""
    try:
        rate = round(float(profile.get("speech_rate", 1.0)), 2)
    except (TypeError, ValueError):
        return 1.0
    if not MIN_PLAYBACK_RATE <= rate <= MAX_PLAYBACK_RATE:
        return 1.0
    return rate


def _doubao_speech_rate(profile: dict[str, Any]) -> tuple[float, int]:
    """Translate the audio-centre multiplier to the Speech 2.0 API scale."""
    playback_rate = _profile_playback_rate(profile)
    return playback_rate, int(round((playback_rate - 1.0) * 100))


def provider_status(provider_id: str) -> ToolStatus:
    if provider_id == LOCAL_PROVIDER_ID:
        return VoiceboxTTS().get_status()
    if provider_id == CLOUD_PROVIDER_ID:
        return DoubaoTTS().get_status()
    if provider_id == TENCENT_PROVIDER_ID:
        return TencentTTS().get_status()
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

    if provider_id == TENCENT_PROVIDER_ID:
        return _generate_tencent_voice(text=text, profile=profile, target=target)

    if provider_id != CLOUD_PROVIDER_ID:
        return ToolResult(success=False, error=f"不支持的配音供应商：{provider_id}")

    voice_id = str(profile.get("provider_voice_id") or "").strip()
    if not voice_id:
        return ToolResult(success=False, error="豆包音色缺少 provider_voice_id 配置")

    cloud_output, needs_wav, cleanup = _cloud_staging_path(target, CLOUD_PROVIDER_ID)
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

    normalised = _finalise_cloud_take(
        result=result,
        cloud_output=cloud_output,
        target=target,
        needs_wav=needs_wav,
        cleanup=cleanup,
    )
    if isinstance(normalised, ToolResult):
        return normalised
    result.data.update({
        "profile_id": str(profile["id"]),
        "profile_name": str(profile.get("name") or profile["id"]),
        "provider_id": provider_id,
        "playback_rate": playback_rate,
        "provider_speech_rate": provider_speech_rate,
    })
    return result


def _generate_tencent_voice(
    *, text: str, profile: dict[str, Any], target: Path
) -> ToolResult:
    """Run one take through Tencent Cloud 语音合成."""
    voice_id = str(profile.get("provider_voice_id") or "").strip()
    if not voice_id:
        return ToolResult(success=False, error="腾讯云音色缺少 provider_voice_id 配置")

    cloud_output, needs_wav, cleanup = _cloud_staging_path(
        target, TENCENT_PROVIDER_ID, suffix=TENCENT_CLOUD_FORMAT
    )
    metadata_path = target.with_suffix(target.suffix + ".tencent.json")
    playback_rate = _profile_playback_rate(profile)
    provider_speed = playback_rate_to_tencent_speed(playback_rate)
    # Ask for WAV, never MP3: at 24 kHz Tencent's MP3 encoder is MPEG-2 Layer III
    # capped at 32 kb/s, which band-limits the take to roughly 8 kHz and makes a
    # preset audition sound duller than the console.  WAV is lossless PCM and is
    # what the console itself uses by default.
    result = TencentTTS().execute({
        "text": text,
        "voice_id": voice_id,
        "format": TENCENT_CLOUD_FORMAT,
        "sample_rate": 24000,
        "playback_rate": playback_rate,
        "enable_timestamp": True,
        "output_path": str(cloud_output),
        "metadata_path": str(metadata_path),
        "timeout_seconds": 180,
    })
    if not result.success or not cloud_output.is_file():
        return result

    normalised = _finalise_cloud_take(
        result=result,
        cloud_output=cloud_output,
        target=target,
        needs_wav=needs_wav,
        cleanup=cleanup,
    )
    if isinstance(normalised, ToolResult):
        return normalised
    result.data.update({
        "profile_id": str(profile["id"]),
        "profile_name": str(profile.get("name") or profile["id"]),
        "provider_id": TENCENT_PROVIDER_ID,
        "playback_rate": playback_rate,
        "provider_speech_rate": provider_speed,
    })
    return result


def _cloud_staging_path(
    target: Path, provider_id: str, *, suffix: str = "mp3"
) -> tuple[Path, bool, bool]:
    """Stage the provider take next to the target before WAV normalisation."""
    needs_wav = target.suffix.lower() == ".wav"
    if not needs_wav:
        return target, False, False
    return target.with_name(f".{target.stem}.{provider_id}.{suffix}"), True, True


def _finalise_cloud_take(
    *,
    result: ToolResult,
    cloud_output: Path,
    target: Path,
    needs_wav: bool,
    cleanup: bool,
) -> ToolResult | None:
    """Normalise a staged cloud take into the project WAV, or return an error."""
    if not needs_wav:
        return None
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
    if cleanup:
        try:
            cloud_output.unlink()
        except OSError:
            pass
    result.artifacts = [
        str(target) if item == str(cloud_output) else item for item in result.artifacts
    ]
    result.data["output"] = str(target)
    result.data["normalised_format"] = "wav_pcm_s16le_mono_24000"
    return None


def _convert_to_wav(source: Path, target: Path) -> str | None:
    """Normalise a cloud take into the project's 24 kHz mono PCM WAV."""
    ffmpeg = resolve_ffmpeg() or None
    if not ffmpeg:
        return "本机未发现 FFmpeg，无法把云端音频规范化为项目 WAV"
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
        return f"云端配音音频转换 WAV 失败：{detail[:600]}"
    return None
