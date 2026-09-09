"""Local speech-activity evidence for interaction fine cuts.

The adapter deliberately uses the Silero model already bundled by
``faster-whisper``.  It never downloads a model, never performs transcription,
and never modifies the source media.  Returned timestamps are always expressed
on the source timeline.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import importlib.metadata
import json
import math
from pathlib import Path
import subprocess
from typing import Any, Callable


VERSION = "material-speech-activity-v1"
SAMPLE_RATE = 16_000


class SpeechActivityError(RuntimeError):
    pass


class SpeechActivityUnavailable(SpeechActivityError):
    pass


@dataclass(frozen=True)
class SpeechActivityOptions:
    threshold: float = .5
    min_speech_duration_ms: int = 0
    min_silence_duration_ms: int = 100
    speech_pad_ms: int = 0
    chunk_seconds: float = 60.0
    chunk_overlap_seconds: float = .5

    def validate(self) -> None:
        if not .01 <= float(self.threshold) <= .99:
            raise SpeechActivityError("VAD 语音阈值超出范围")
        if not 0 <= int(self.min_speech_duration_ms) <= 5_000:
            raise SpeechActivityError("VAD 最短语音时长超出范围")
        if not 20 <= int(self.min_silence_duration_ms) <= 5_000:
            raise SpeechActivityError("VAD 最短静音时长超出范围")
        if not 0 <= int(self.speech_pad_ms) <= 2_000:
            raise SpeechActivityError("VAD 内部保护时长超出范围")
        if not 5 <= float(self.chunk_seconds) <= 600:
            raise SpeechActivityError("VAD 分块时长超出范围")
        if not 0 <= float(self.chunk_overlap_seconds) < min(5, float(self.chunk_seconds) / 2):
            raise SpeechActivityError("VAD 分块重叠时长超出范围")


def _finite(value: Any, *, minimum: float, maximum: float, label: str) -> float:
    if isinstance(value, bool):
        raise SpeechActivityError(f"{label}格式无效")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise SpeechActivityError(f"{label}格式无效") from exc
    if not math.isfinite(number) or not minimum <= number <= maximum:
        raise SpeechActivityError(f"{label}超出范围")
    return number


def _model_path() -> Path:
    try:
        from faster_whisper.utils import get_assets_path
    except (ImportError, OSError) as exc:
        raise SpeechActivityUnavailable("本机未安装可选的 faster-whisper VAD 依赖") from exc
    return Path(get_assets_path()) / "silero_vad_v6.onnx"


def runtime_identity(options: SpeechActivityOptions | None = None) -> dict[str, Any]:
    options = options or SpeechActivityOptions()
    options.validate()
    model = _model_path()
    if not model.is_file():
        raise SpeechActivityUnavailable("本地 Silero VAD 模型不存在；不会自动联网下载")
    try:
        package_version = importlib.metadata.version("faster-whisper")
    except importlib.metadata.PackageNotFoundError as exc:
        raise SpeechActivityUnavailable("本机未安装可选的 faster-whisper VAD 依赖") from exc
    model_hash = hashlib.sha256(model.read_bytes()).hexdigest()
    payload = {
        "version": VERSION,
        "engine": "faster-whisper/silero-vad-v6",
        "package_version": package_version,
        "model_sha256": model_hash,
        "sample_rate": SAMPLE_RATE,
        "options": asdict(options),
    }
    payload["signature"] = hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
    return payload


def capability(options: SpeechActivityOptions | None = None) -> dict[str, Any]:
    try:
        return {"status": "available", "identity": runtime_identity(options), "error": None}
    except SpeechActivityUnavailable as exc:
        return {"status": "unavailable", "identity": None, "error": str(exc)}


def _decode_audio(
    source: Path,
    *,
    ffmpeg: str,
    start: float,
    end: float,
    timeout: float,
    runner: Callable[..., subprocess.CompletedProcess[bytes]],
) -> bytes:
    command = [
        ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error",
        "-ss", f"{start:.6f}", "-to", f"{end:.6f}", "-i", str(source),
        "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "f32le", "pipe:1",
    ]
    try:
        completed = runner(command, capture_output=True, timeout=max(30.0, timeout), check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SpeechActivityError("本地 VAD 音频解码失败，请检查 FFmpeg") from exc
    if completed.returncode != 0:
        raise SpeechActivityError("本地 VAD 音频解码失败，未修改素材")
    return bytes(completed.stdout or b"")


def _merge_ranges(rows: list[dict[str, float]], *, join_gap: float = .12) -> list[dict[str, float]]:
    merged: list[dict[str, float]] = []
    for row in sorted(rows, key=lambda item: (item["start"], item["end"])):
        if not merged or row["start"] > merged[-1]["end"] + join_gap:
            merged.append({"start": row["start"], "end": row["end"]})
        else:
            merged[-1]["end"] = max(merged[-1]["end"], row["end"])
    return [{"start": round(row["start"], 3), "end": round(row["end"], 3)} for row in merged]


def non_speech_ranges(start: float, end: float, speech: list[dict[str, Any]]) -> list[dict[str, float]]:
    start = _finite(start, minimum=0, maximum=24 * 3600, label="VAD 开始时间")
    end = _finite(end, minimum=start, maximum=24 * 3600, label="VAD 结束时间")
    cursor = start
    result: list[dict[str, float]] = []
    for row in _merge_ranges([
        {"start": max(start, float(item["start"])), "end": min(end, float(item["end"]))}
        for item in speech
        if isinstance(item, dict) and float(item.get("end", 0)) > start and float(item.get("start", end)) < end
    ]):
        if row["start"] > cursor:
            result.append({"start": round(cursor, 3), "end": round(row["start"], 3)})
        cursor = max(cursor, row["end"])
    if cursor < end:
        result.append({"start": round(cursor, 3), "end": round(end, 3)})
    return [row for row in result if row["end"] - row["start"] >= .04]


def detect_speech_activity(
    source: Path,
    *,
    ffmpeg: str,
    start: float,
    end: float,
    options: SpeechActivityOptions | None = None,
    timeout: float = 300.0,
    runner: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
    detector: Callable[..., list[dict[str, int]]] | None = None,
) -> dict[str, Any]:
    """Detect bounded speech and return source-timeline evidence.

    ``detector`` exists for deterministic tests; production uses the local
    faster-whisper API and never downloads a model.
    """
    source = Path(source).resolve()
    if not source.is_file():
        raise SpeechActivityError("VAD 源素材不存在")
    start = _finite(start, minimum=0, maximum=24 * 3600, label="VAD 开始时间")
    end = _finite(end, minimum=start, maximum=24 * 3600, label="VAD 结束时间")
    if end <= start:
        raise SpeechActivityError("VAD 检测范围为空")
    options = options or SpeechActivityOptions()
    options.validate()
    identity = runtime_identity(options)
    if detector is None:
        try:
            import numpy as np
            from faster_whisper.vad import VadOptions, get_speech_timestamps
        except (ImportError, OSError) as exc:
            raise SpeechActivityUnavailable("本机 VAD 运行依赖不可用") from exc

        vad_options = VadOptions(
            threshold=options.threshold,
            min_speech_duration_ms=options.min_speech_duration_ms,
            min_silence_duration_ms=options.min_silence_duration_ms,
            speech_pad_ms=options.speech_pad_ms,
        )

        def detector(audio: Any, **_kwargs: Any) -> list[dict[str, int]]:
            return get_speech_timestamps(audio, vad_options=vad_options, sampling_rate=SAMPLE_RATE)
    else:
        import numpy as np

    ranges: list[dict[str, float]] = []
    chunk_start = start
    step = options.chunk_seconds - options.chunk_overlap_seconds
    chunk_count = 0
    decoded_samples = 0
    while chunk_start < end - 1e-6:
        chunk_end = min(end, chunk_start + options.chunk_seconds)
        raw = _decode_audio(
            source, ffmpeg=ffmpeg, start=chunk_start, end=chunk_end,
            timeout=min(timeout, max(30.0, chunk_end - chunk_start + 30.0)), runner=runner,
        )
        if len(raw) % 4:
            raise SpeechActivityError("VAD 解码样本长度无效")
        audio = np.frombuffer(raw, dtype=np.float32)
        decoded_samples += int(audio.size)
        chunk_count += 1
        if audio.size:
            for row in detector(audio, sampling_rate=SAMPLE_RATE):
                try:
                    local_start = int(row["start"]) / SAMPLE_RATE
                    local_end = int(row["end"]) / SAMPLE_RATE
                except (KeyError, TypeError, ValueError) as exc:
                    raise SpeechActivityError("VAD 返回了无效时间戳") from exc
                absolute_start = max(start, chunk_start + local_start)
                absolute_end = min(end, chunk_start + local_end)
                if absolute_end > absolute_start:
                    ranges.append({"start": absolute_start, "end": absolute_end})
        if chunk_end >= end:
            break
        chunk_start += step
    speech = _merge_ranges(ranges)
    return {
        "version": VERSION,
        "status": "available" if decoded_samples else "no_audio_samples",
        "identity": identity,
        "range": {"start": round(start, 3), "end": round(end, 3)},
        "speech_ranges": speech,
        "non_speech_ranges": non_speech_ranges(start, end, speech),
        "metadata": {"chunk_count": chunk_count, "decoded_samples": decoded_samples},
    }
