"""Tencent Cloud 语音合成 (TTS) provider tool.

The basic ``TextToVoice`` action caps a single request at ~150 Chinese
characters, so long narration is split on sentence boundaries, synthesised
chunk by chunk, then concatenated into one file.  Character-level subtitle
timestamps are offset-merged so existing subtitle builders keep working.
"""

from __future__ import annotations

import base64
import json
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

from lib.tencent_cloud import (
    TTS_HOST,
    TTS_SERVICE,
    TTS_VERSION,
    TencentCloudError,
    friendly_error,
    redact_secret,
    response_error,
    response_request_id,
    tc3_request,
    tencent_credentials,
)
from lib.ffmpeg_locator import resolve_ffmpeg
from tools.base_tool import (
    BaseTool,
    Determinism,
    ExecutionMode,
    ResourceProfile,
    RetryPolicy,
    ToolResult,
    ToolRuntime,
    ToolStability,
    ToolStatus,
    ToolTier,
)


# The documented anchor points of the ``Speed`` parameter:
#   -2 -> 0.6x, -1 -> 0.8x, 0 -> 1.0x, 1 -> 1.2x, 2 -> 1.5x, 4 -> 2.0x, 6 -> 2.5x
TENCENT_SPEED_ANCHORS: tuple[tuple[int, float], ...] = (
    (-2, 0.60),
    (-1, 0.80),
    (0, 1.00),
    (1, 1.20),
    (2, 1.50),
    (3, 1.70),
    (4, 2.00),
    (5, 2.25),
    (6, 2.50),
)

# Per-request text budget.  The API documents 150 Chinese characters where a
# full-width punctuation mark also counts as one; English allows 500 letters.
MAX_TEXT_UNITS = 140
ASCII_UNITS_PER_UNIT = 150 / 500  # one ASCII letter is worth ~0.3 汉字
MAX_CHUNKS = 60


def playback_rate_to_tencent_speed(playback_rate: float) -> int:
    """Snap a UI multiplier (0.50x-2.00x) onto Tencent's coarse Speed scale."""
    try:
        rate = float(playback_rate)
    except (TypeError, ValueError):
        return 0
    if rate <= 0:
        return 0
    return min(
        TENCENT_SPEED_ANCHORS,
        key=lambda anchor: (abs(anchor[1] - rate), abs(anchor[0])),
    )[0]


def _text_units(text: str) -> float:
    """Approximate the API's own character accounting for one string."""
    ascii_letters = sum(1 for char in text if ord(char) < 128 and not char.isspace())
    wide = sum(1 for char in text if ord(char) >= 128)
    whitespace = sum(1 for char in text if char.isspace())
    return wide + ascii_letters * ASCII_UNITS_PER_UNIT + whitespace * 0.5


def split_text_for_tencent(text: str, *, max_units: float = MAX_TEXT_UNITS) -> list[str]:
    """Split text into API-sized chunks, preferring sentence boundaries."""
    normalized = str(text or "").strip()
    if not normalized:
        return []
    if _text_units(normalized) <= max_units:
        return [normalized]

    # 1) break on Chinese/English sentence enders, keeping the delimiter
    sentences: list[str] = []
    buffer = ""
    for char in normalized:
        buffer += char
        if char in "。！？!?；;\n":
            sentences.append(buffer)
            buffer = ""
    if buffer:
        sentences.append(buffer)

    # 2) greedily pack sentences; hard-split any oversized sentence
    chunks: list[str] = []
    current = ""
    for sentence in sentences:
        if _text_units(sentence) > max_units:
            if current:
                chunks.append(current)
                current = ""
            chunks.extend(_hard_split(sentence, max_units))
            continue
        if _text_units(current + sentence) > max_units:
            chunks.append(current)
            current = sentence
        else:
            current += sentence
    if current:
        chunks.append(current)
    return [chunk for chunk in chunks if chunk.strip()]


def _hard_split(text: str, max_units: float) -> list[str]:
    """Break one oversized sentence on word/character boundaries."""
    pieces: list[str] = []
    current = ""
    for token in text.replace(",", "，").split("，"):
        candidate = f"{token}，"
        if _text_units(candidate) > max_units and not current:
            # a single unbreakable run: fall back to character slicing
            step = max(1, int(max_units))
            for index in range(0, len(token), step):
                pieces.append(token[index:index + step])
            continue
        if _text_units(current + candidate) > max_units:
            pieces.append(current)
            current = candidate
        else:
            current += candidate
    if current:
        pieces.append(current)
    return pieces


class TencentTTS(BaseTool):
    name = "tencent_tts"
    version = "0.1.0"
    tier = ToolTier.VOICE
    capability = "tts"
    provider = "tencent"
    stability = ToolStability.EXPERIMENTAL
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.STOCHASTIC
    runtime = ToolRuntime.API

    dependencies = []
    install_instructions = (
        "Set TENCENT_SECRET_ID and TENCENT_SECRET_KEY to a Tencent Cloud API key pair.\n"
        "Open 语音合成 (TTS) in the Tencent Cloud console for the same account.\n"
        "Optional: set TENCENT_TTS_REGION (default ap-guangzhou)."
    )
    fallback = "doubao"
    fallback_tools = ["doubao", "google_tts", "elevenlabs_tts"]
    agent_skills = ["tencent-tts", "text-to-speech"]

    capabilities = [
        "text_to_speech",
        "voice_selection",
        "multilingual",
        "timestamp_alignment",
        "long_text_chunking",
    ]
    supports = {
        "voice_cloning": False,
        "multilingual": True,
        "offline": False,
        "native_audio": True,
        "timestamps": True,
        "long_text_async": False,
    }
    best_for = [
        "Mandarin narration with a very large preset voice catalogue",
        "Chinese explainer voiceovers with character-level timestamps",
        "Accounts that already hold a Tencent Cloud free TTS package",
    ]
    not_good_for = [
        "fully offline production",
        "voice clone matching via the basic endpoint",
        "single-call long-form synthesis without local concatenation",
    ]

    input_schema = {
        "type": "object",
        "required": ["text"],
        "properties": {
            "text": {"type": "string", "description": "Text to convert to speech"},
            "voice_id": {
                "type": "string",
                "description": "Tencent Cloud VoiceType integer, e.g. 502001. Defaults to TENCENT_TTS_VOICE_TYPE.",
            },
            "format": {"type": "string", "default": "mp3", "enum": ["mp3", "wav", "pcm"]},
            "sample_rate": {"type": "integer", "default": 16000, "enum": [8000, 16000, 24000]},
            "playback_rate": {
                "type": "number",
                "default": 1.0,
                "minimum": 0.5,
                "maximum": 2.0,
                "description": "UI multiplier; snapped onto Tencent's Speed scale.",
            },
            "speed": {
                "type": "number",
                "minimum": -2,
                "maximum": 6,
                "description": "Explicit Tencent Speed value; overrides playback_rate.",
            },
            "volume": {"type": "number", "default": 0.0, "minimum": -10, "maximum": 10},
            "enable_timestamp": {"type": "boolean", "default": True},
            "primary_language": {"type": "integer", "default": 1, "enum": [1, 2]},
            "emotion_category": {"type": "string", "default": ""},
            "emotion_intensity": {"type": "integer", "default": 100, "minimum": 50, "maximum": 200},
            "output_path": {"type": "string"},
            "metadata_path": {"type": "string"},
            "timeout_seconds": {"type": "integer", "default": 120, "minimum": 10},
        },
    }

    output_schema = {
        "type": "object",
        "properties": {
            "output": {"type": "string"},
            "metadata_path": {"type": "string"},
            "audio_duration_seconds": {"type": ["number", "null"]},
            "subtitles": {"type": "array"},
        },
    }
    artifact_schema = {"type": "array", "items": {"type": "string"}}

    resource_profile = ResourceProfile(
        cpu_cores=1, ram_mb=256, vram_mb=0, disk_mb=50, network_required=True
    )
    retry_policy = RetryPolicy(
        max_retries=2,
        backoff_seconds=1.5,
        retryable_errors=["RequestLimitExceeded", "InternalError", "网络请求失败"],
    )
    idempotency_key_fields = ["text", "voice_id", "format", "sample_rate", "speed"]
    side_effects = [
        "writes audio file to output_path",
        "writes Tencent Cloud request metadata JSON next to output_path",
        "calls the Tencent Cloud tts.tencentcloudapi.com API",
    ]
    user_visible_verification = [
        "Listen to generated audio for Mandarin naturalness and pacing",
        "Confirm long narration was concatenated without audible seams",
    ]
    quality_score = 0.90
    latency_p50_seconds = 3.0

    DEFAULT_VOICE_ENV = "TENCENT_TTS_VOICE_TYPE"

    def get_status(self) -> ToolStatus:
        if tencent_credentials().configured:
            return ToolStatus.AVAILABLE
        return ToolStatus.UNAVAILABLE

    def estimate_cost(self, inputs: dict[str, Any]) -> float:
        # Tencent bills per 万字符; the free package covers the first tier.
        return round(len(inputs.get("text", "")) * 0.000002, 4)

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        import os

        voice_id = str(
            inputs.get("voice_id") or os.environ.get(self.DEFAULT_VOICE_ENV) or ""
        ).strip()
        if not voice_id:
            return ToolResult(
                success=False,
                error=(
                    "未选择腾讯云音色。请在「配音中心」选择一个腾讯云预设音色，"
                    f"或设置 {self.DEFAULT_VOICE_ENV}。"
                ),
            )
        if not str(inputs.get("text") or "").strip():
            return ToolResult(success=False, error="腾讯云语音合成需要非空文本")

        start = time.time()
        try:
            result = self._generate(inputs, voice_id=voice_id)
        except TencentCloudError as exc:
            return ToolResult(success=False, error=str(exc))
        except Exception as exc:  # noqa: BLE001
            return ToolResult(success=False, error=f"腾讯云语音合成失败：{redact_secret(exc)}")
        result.duration_seconds = round(time.time() - start, 2)
        if not result.cost_usd:
            result.cost_usd = self.estimate_cost(inputs)
        return result

    def _generate(self, inputs: dict[str, Any], *, voice_id: str) -> ToolResult:
        fmt = str(inputs.get("format", "mp3")).lower()
        if fmt not in {"mp3", "wav", "pcm"}:
            fmt = "mp3"
        sample_rate = int(inputs.get("sample_rate", 16000))
        if sample_rate not in {8000, 16000, 24000}:
            sample_rate = 16000
        if "speed" in inputs and inputs.get("speed") is not None:
            speed = max(-2.0, min(6.0, float(inputs["speed"])))
        else:
            speed = float(playback_rate_to_tencent_speed(inputs.get("playback_rate", 1.0)))
        volume = max(-10.0, min(10.0, float(inputs.get("volume", 0.0) or 0.0)))
        primary_language = int(inputs.get("primary_language", 1) or 1)
        enable_timestamp = bool(inputs.get("enable_timestamp", True))
        timeout = int(inputs.get("timeout_seconds", 120) or 120)

        output_path = Path(inputs.get("output_path") or f"tencent_tts.{fmt}")
        metadata_path = Path(
            inputs.get("metadata_path") or output_path.with_suffix(output_path.suffix + ".json")
        )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        metadata_path.parent.mkdir(parents=True, exist_ok=True)

        chunks = split_text_for_tencent(str(inputs["text"]))
        if not chunks:
            return ToolResult(success=False, error="腾讯云语音合成需要有效文本")
        if len(chunks) > MAX_CHUNKS:
            return ToolResult(
                success=False,
                error=f"文本过长（{len(chunks)} 段，上限 {MAX_CHUNKS} 段）；请缩短旁白后重试",
            )

        work_dir = output_path.parent / f".tencent-tts-{uuid.uuid4().hex[:8]}"
        work_dir.mkdir(parents=True, exist_ok=True)
        records: list[dict[str, Any]] = []
        subtitles: list[dict[str, Any]] = []
        requests_log: list[dict[str, Any]] = []
        effective_sample_rate = sample_rate
        try:
            # 24000 Hz 仅部分音色支持；被拒时自动回落到 16000 Hz 重试。
            candidate_rates = [sample_rate] if sample_rate == 16000 else [sample_rate, 16000]
            for index, chunk in enumerate(chunks):
                piece = work_dir / f"chunk-{index:03d}.{fmt}"
                payload = {
                    "Text": chunk,
                    "SessionId": f"haike-{uuid.uuid4().hex[:24]}",
                    "VoiceType": int(voice_id),
                    "Codec": fmt,
                    "SampleRate": sample_rate,
                    "Speed": speed,
                    "Volume": volume,
                    "PrimaryLanguage": primary_language,
                    "EnableSubtitle": enable_timestamp,
                }
                emotion = str(inputs.get("emotion_category") or "").strip()
                if emotion:
                    payload["EmotionCategory"] = emotion
                    payload["EmotionIntensity"] = int(inputs.get("emotion_intensity", 100) or 100)

                response: dict[str, Any] | None = None
                for attempt, attempt_rate in enumerate(candidate_rates):
                    payload["SampleRate"] = attempt_rate
                    response = tc3_request(
                        TTS_SERVICE,
                        "TextToVoice",
                        payload,
                        version=TTS_VERSION,
                        host=TTS_HOST,
                        timeout=timeout,
                    )
                    error = response_error(response)
                    if not error:
                        effective_sample_rate = attempt_rate
                        break
                    code = str(error.get("Code") or "")
                    if code == "InvalidParameterValue.SampleRate" and attempt + 1 < len(candidate_rates):
                        continue
                    return ToolResult(
                        success=False,
                        error=friendly_error(code, str(error.get("Message") or ""), product="语音合成 TTS"),
                    )
                if response is None:
                    return ToolResult(success=False, error="腾讯云语音合成未返回响应")
                body = response.get("Response") or {}
                audio_b64 = body.get("Audio")
                if not audio_b64:
                    return ToolResult(
                        success=False,
                        error=f"腾讯云语音合成第 {index + 1} 段未返回音频数据",
                    )
                raw = base64.b64decode(audio_b64)
                if not raw:
                    return ToolResult(
                        success=False,
                        error=f"腾讯云语音合成第 {index + 1} 段返回了空音频",
                    )
                piece.write_bytes(raw)
                records.append({"index": index, "path": piece, "text": chunk})
                chunk_subs = body.get("Subtitles") or []
                requests_log.append({
                    "index": index,
                    "request_id": response_request_id(response),
                    "characters": len(chunk),
                    "subtitle_count": len(chunk_subs) if isinstance(chunk_subs, list) else 0,
                })
                if enable_timestamp and isinstance(chunk_subs, list):
                    subtitles.extend({"chunk": index, **item} for item in chunk_subs if isinstance(item, dict))

            if len(records) == 1 and records[0]["path"] == output_path:
                assembled = output_path
            elif len(records) == 1:
                shutil.move(str(records[0]["path"]), str(output_path))
                assembled = output_path
            else:
                assembled = self._concat(records, output_path, fmt)

            audio_duration = self._audio_duration(assembled)
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

        metadata_path.write_text(
            json.dumps(
                {
                    "provider": "tencent",
                    "product": "tts",
                    "voice_type": voice_id,
                    "format": fmt,
                    "sample_rate": effective_sample_rate,
                    "speed": speed,
                    "volume": volume,
                    "primary_language": primary_language,
                    "chunk_count": len(chunks),
                    "requests": requests_log,
                    "subtitles": subtitles,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

        return ToolResult(
            success=True,
            data={
                "provider": self.provider,
                "model": f"tencent-tts:{voice_id}",
                "voice_id": voice_id,
                "format": fmt,
                "sample_rate": sample_rate,
                "speed": speed,
                "text_length": len(str(inputs["text"])),
                "chunk_count": len(chunks),
                "audio_duration_seconds": round(audio_duration, 2) if audio_duration else None,
                "output": str(assembled),
                "metadata_path": str(metadata_path),
                "subtitles": subtitles,
            },
            artifacts=[str(assembled), str(metadata_path)],
            cost_usd=self.estimate_cost(inputs),
            model=f"tencent-tts:{voice_id}",
        )

    def _concat(self, records: list[dict[str, Any]], target: Path, fmt: str) -> Path:
        """Join chunk files, re-encoding only when the container needs it."""
        ffmpeg = resolve_ffmpeg()
        if not ffmpeg:
            raise TencentCloudError(
                "本机未发现 FFmpeg，无法拼接腾讯云分段音频；请先完成项目依赖安装"
            )
        listing = target.parent / f".tencent-tts-list-{uuid.uuid4().hex[:8]}.txt"
        listing.write_text(
            "\n".join(f"file '{Path(item['path']).resolve().as_posix()}'" for item in records) + "\n",
            encoding="utf-8",
        )
        codec = ["-c:a", "pcm_s16le"] if fmt == "wav" else ["-c:a", "libmp3lame", "-q:a", "3"]
        if fmt == "pcm":
            codec = ["-c:a", "pcm_s16le"]
        try:
            completed = subprocess.run(
                [
                    ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                    "-f", "concat", "-safe", "0", "-i", str(listing),
                    *codec, str(target),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=300,
                check=False,
            )
            if completed.returncode != 0 or not target.is_file() or target.stat().st_size <= 0:
                detail = (completed.stderr or completed.stdout or "未知错误").strip()
                raise TencentCloudError(f"腾讯云分段音频拼接失败：{detail[:400]}")
        finally:
            listing.unlink(missing_ok=True)
        return target

    @staticmethod
    def _audio_duration(path: Path) -> float | None:
        try:
            from tools.analysis.audio_probe import probe_duration

            return probe_duration(path)
        except Exception:
            return None
