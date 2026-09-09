"""Optional, reusable audio evidence for local-material analysis.

This module is deliberately provider-agnostic.  The caller owns paid-request
journaling; this layer makes the opt-in contract explicit and supplies local
silence evidence without treating silence as proof that content is irrelevant.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import re
import subprocess
from typing import Any, Callable


VERSION = "material-audio-evidence-v1"
POLICIES = {"disabled", "doubao_transcript"}


class MaterialAudioEvidenceError(ValueError):
    pass


def _finite(value: Any, *, minimum: float, maximum: float, label: str) -> float:
    if isinstance(value, bool):
        raise MaterialAudioEvidenceError(f"{label}格式无效")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise MaterialAudioEvidenceError(f"{label}格式无效") from exc
    if not math.isfinite(number) or not minimum <= number <= maximum:
        raise MaterialAudioEvidenceError(f"{label}超出范围")
    return round(number, 3)


def audio_policy(recognize_audio: bool) -> str:
    if not isinstance(recognize_audio, bool):
        raise MaterialAudioEvidenceError("识别音频必须明确为开启或关闭")
    return "doubao_transcript" if recognize_audio else "disabled"


def cache_identity(source_fingerprint: str, *, recognize_audio: bool, asr_identity: str | None) -> str:
    policy = audio_policy(recognize_audio)
    if policy == "doubao_transcript" and not str(asr_identity or "").strip():
        raise MaterialAudioEvidenceError("开启音频识别后必须冻结语音服务身份")
    payload = {
        "version": VERSION,
        "source_fingerprint": str(source_fingerprint),
        "policy": policy,
        "asr_identity": str(asr_identity) if recognize_audio else None,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def normalize_utterances(rows: Any, duration: float) -> list[dict[str, Any]]:
    duration = _finite(duration, minimum=.001, maximum=24 * 3600, label="素材时长")
    if not isinstance(rows, list):
        raise MaterialAudioEvidenceError("语音识别结果不是分句列表")
    result: list[dict[str, Any]] = []
    previous_start = -1.0
    for row in rows:
        if not isinstance(row, dict):
            raise MaterialAudioEvidenceError("语音分句结构无效")
        start = _finite(row.get("start"), minimum=0, maximum=duration, label="分句开始时间")
        end = _finite(row.get("end"), minimum=0, maximum=duration, label="分句结束时间")
        text = str(row.get("text") or "").strip()
        if end <= start or start < previous_start:
            raise MaterialAudioEvidenceError("语音分句时间倒序或为空")
        if not text:
            continue
        result.append({"id": f"U{len(result)+1:05d}", "start": start, "end": end, "text": text[:1500]})
        previous_start = start
    return result


def resolve_audio_evidence(
    source: Path,
    *,
    duration: float,
    has_audio: bool,
    recognize_audio: bool,
    asr_identity: str | None = None,
    transcript_provider: Callable[[Path], tuple[str, list[dict[str, Any]], dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Resolve one in-memory evidence result.

    The provider is never constructed or called when recognition is disabled or
    when the source has no audio.  Durable remote-call journaling remains the
    caller's responsibility so an unknown acceptance state cannot be retried.
    """
    policy = audio_policy(recognize_audio)
    if not has_audio:
        return {"version": VERSION, "policy": policy, "status": "no_audio", "provider": None,
                "utterances": [], "metadata": {"utterance_count": 0}}
    if not recognize_audio:
        return {"version": VERSION, "policy": policy, "status": "skipped", "provider": None,
                "utterances": [], "metadata": {"utterance_count": 0}}
    if transcript_provider is None:
        raise MaterialAudioEvidenceError("开启音频识别后必须提供已确认的语音服务")
    _, rows, metadata = transcript_provider(source)
    utterances = normalize_utterances(rows, duration)
    safe_metadata = metadata if isinstance(metadata, dict) else {}
    return {
        "version": VERSION,
        "policy": policy,
        "status": "available" if utterances else "no_speech",
        "provider": str(asr_identity or ""),
        "utterances": utterances,
        "metadata": {"utterance_count": len(utterances),
                     "timestamp_unit": str(safe_metadata.get("timestamp_unit") or "seconds")},
    }


_SILENCE_START = re.compile(r"silence_start:\s*(-?\d+(?:\.\d+)?)")
_SILENCE_END = re.compile(r"silence_end:\s*(-?\d+(?:\.\d+)?)")


def parse_silencedetect(stderr: str, *, range_start: float, range_end: float) -> list[dict[str, float]]:
    range_start = _finite(range_start, minimum=0, maximum=24 * 3600, label="检测开始时间")
    range_end = _finite(range_end, minimum=range_start, maximum=24 * 3600, label="检测结束时间")
    if range_end <= range_start:
        return []
    open_start: float | None = None
    intervals: list[dict[str, float]] = []
    for line in str(stderr).splitlines():
        start_match = _SILENCE_START.search(line)
        if start_match:
            open_start = max(range_start, range_start + float(start_match.group(1)))
        end_match = _SILENCE_END.search(line)
        if end_match and open_start is not None:
            end = min(range_end, range_start + float(end_match.group(1)))
            if end > open_start:
                intervals.append({"start": round(open_start, 3), "end": round(end, 3)})
            open_start = None
    if open_start is not None and range_end > open_start:
        intervals.append({"start": round(open_start, 3), "end": range_end})
    return intervals


def detect_silence(
    source: Path,
    *,
    ffmpeg: str,
    start: float,
    end: float,
    noise_db: float = -38.0,
    minimum_duration: float = .12,
    timeout: float = 180.0,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> list[dict[str, float]]:
    """Collect local audio silence evidence for one bounded source interval."""
    start = _finite(start, minimum=0, maximum=24 * 3600, label="检测开始时间")
    end = _finite(end, minimum=start, maximum=24 * 3600, label="检测结束时间")
    minimum_duration = _finite(minimum_duration, minimum=.04, maximum=5, label="静音最小时长")
    noise_db = _finite(noise_db, minimum=-90, maximum=-10, label="静音阈值")
    if end <= start:
        return []
    command = [
        ffmpeg, "-hide_banner", "-nostdin", "-ss", f"{start:.3f}", "-to", f"{end:.3f}",
        "-i", str(source), "-vn", "-af", f"silencedetect=noise={noise_db:g}dB:d={minimum_duration:g}",
        "-f", "null", "-",
    ]
    try:
        completed = runner(command, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=max(30.0, timeout), check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MaterialAudioEvidenceError("本地声音边界检测失败，请检查 FFmpeg") from exc
    if completed.returncode != 0:
        raise MaterialAudioEvidenceError("本地声音边界检测失败，未修改素材")
    return parse_silencedetect(completed.stderr, range_start=start, range_end=end)
