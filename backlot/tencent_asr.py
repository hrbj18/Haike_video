"""Tencent Cloud 语音识别 (ASR) adapter.

Two routes are supported, mirroring how the account is actually billed:

* ``SentenceRecognition`` —— 一句话识别 for short clips (<= 60 s, <= 5 MB).
* ``CreateRecTask`` —— 录音文件识别 for longer media, submitting Base64 audio
  directly (no inbound public URL is required for a local-only workbench).

Both routes normalise into ``(text, segments, metadata)`` exactly like the
Doubao adapter so the rest of the pipeline stays provider-neutral.  The module
never silently falls back to a different provider.
"""

from __future__ import annotations

import base64
import json
import math
import os
import tempfile
import time
import uuid
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Callable

from lib.env_loader import load_env
from lib.ffmpeg_locator import resolve_ffmpeg
from backlot.state import REPO_ROOT
from backlot.interaction_concurrency import (
    ConcurrencyStats,
    InteractionConcurrencyError,
    KIND_ASR,
    run_bounded,
    resolve_limit,
)
from lib.tencent_cloud import (
    ASR_HOST,
    ASR_SERVICE,
    ASR_VERSION,
    DEFAULT_REGION,
    TencentCloudError,
    friendly_error,
    redact_secret,
    response_error,
    response_request_id,
    tc3_request,
    tencent_credentials,
    tencent_region,
)


load_env(REPO_ROOT)

SENTENCE_MAX_BYTES = 5 * 1024 * 1024
SENTENCE_MAX_SECONDS = 60
REC_TASK_MAX_BYTES = 5 * 1024 * 1024
MAX_TASK_SECONDS = 900
# 单次 Base64 提交上限 5 MB 是硬约束，长素材必须本地分片后再逐片识别。
# 注意上限卡的是 Base64 之后的体积（约为原始字节的 4/3），所以分片按原始字节取
# 600 秒（10 分钟）：48 kbps 单声道下约 3.44 MB，Base64 后约 4.58 MB，留足余量。
# 分片边界按固定时长切，可能与句尾错位一两个字。
CHUNK_SECONDS = 600.0
# 分片限流退避：腾讯把限流报成 RequestLimitExceeded / FailedOperation.ServiceBusy
# （见 lib/tencent_cloud.friendly_error）。被拒的请求不计费，因此可以安全退避重试；
# 但超时/连接中断属于「受理状态不明确」，绝不能自动重发（可能重复计费），故不在此重试。
CHUNK_RETRY_ATTEMPTS = 3
CHUNK_RETRY_BASE_SECONDS = 1.5
_RATE_LIMIT_MARKERS = (
    "RequestLimitExceeded", "ServiceBusy", "限流", "请求过于频繁", "频率", "QPS", "429",
)
# 分片写前日志的三态。``submitting`` = 提交已发出、受理状态不明确（禁止自动重提）；
# ``done`` = 已完成、可复用；无记录 = 可提交。
CHUNK_STATE_SUBMITTING = "submitting"
CHUNK_STATE_DONE = "done"
# 分片写前日志必须原子落盘：临时文件写完后 ``os.replace`` 替换，Windows 下读者短暂
# 持句柄会让 replace 抛 PermissionError，故做少量退避重试（同 material_evidence）。
_ATOMIC_REPLACE_ATTEMPTS = 12
_ATOMIC_REPLACE_BASE_SECONDS = 0.01
# 读一份「存在却用不了」的分片记录时的重试预算（躲开写者一次 os.replace）。
_CHUNK_READ_ATTEMPTS = 3
_CHUNK_READ_BACKOFF_SECONDS = 0.01
# 「受理不明」标记：提交已经发出，却没有拿到供应商的确定应答（连接中断 / 超时 / 无响应）。
# 这类失败可能已在供应商侧受理并计费，绝不自动重提；反之「语音识别服务不可用」这类拿到
# 明确拒绝的失败可以安全重提。判定方式与 ``_is_rate_limited`` 一致（文本标记）。
_AMBIGUOUS_SUBMIT_MARKERS = (
    "中断", "超时", "连接", "无响应", "尚不明确", "无法确认", "未收到",
    "timeout", "connection", "reset", "aborted", "refused", "brokepipe", "disconnected",
)
DEFAULT_ENGINE = "16k_zh"
SUPPORTED_ENGINES = ("16k_zh", "16k_zh_large", "16k_yue", "8k_zh", "16k_en", "16k_zh_dialect")
SUPPORTED_FORMATS = ("mp3", "wav", "m4a", "aac", "ogg", "pcm", "amr", "silk", "speex")

# A short, deterministic Mandarin sentence used by the self-contained test:
# Tencent TTS synthesises it, then Tencent ASR must read it back.
TEST_PHRASE = "海客视频语音识别连通测试，一二三四五。"
# 同音字与中/阿拉伯数字是 ASR 的正常输出差异（实测会回读成「海科…12345」），
# 因此自检打分前先做归一化，并允许少量字级误差。
MATCH_SIMILARITY_THRESHOLD = 0.7
_CN_DIGIT_FOLD = str.maketrans("一二三四五六七八九零〇", "12345678900")


def _normalise_for_match(value: str) -> str:
    """Fold punctuation, whitespace, case and Chinese numerals for comparison."""

    folded = (value or "").translate(_CN_DIGIT_FOLD)
    return "".join(ch for ch in folded.lower() if ch.isalnum())


class TencentASRError(RuntimeError):
    """A known, user-remediable ASR failure."""

    required = True


class TencentASRAmbiguous(TencentASRError):
    """提交已发出但受理状态不明确：可能已计费，绝不允许自动重提。

    与 :class:`backlot.doubao_asr.DoubaoASRAmbiguous` 对齐：``status = "ambiguous"``
    让上层（``workbench.mark_asset_media_index_failed``）把任务冻结成「人工核对」，
    而不是可重试的 ``failed``；``retryable = False`` 明确禁止自动重发。
    """

    status = "ambiguous"
    retryable = False


@dataclass(frozen=True)
class TencentASRSettings:
    secret_id: str
    secret_key: str
    region: str
    engine: str
    language: str

    @property
    def configured(self) -> bool:
        return bool(self.secret_id and self.secret_key)

    @property
    def provider(self) -> str:
        return "tencent-asr"


def _settings() -> TencentASRSettings:
    import os

    credentials = tencent_credentials()
    engine = str(os.environ.get("TENCENT_ASR_ENGINE") or DEFAULT_ENGINE).strip()
    language = "en-US" if engine.endswith("_en") else "zh-CN"
    return TencentASRSettings(
        secret_id=credentials.secret_id,
        secret_key=credentials.secret_key,
        region=tencent_region(ASR_SERVICE),
        engine=engine if engine in SUPPORTED_ENGINES else DEFAULT_ENGINE,
        language=language,
    )


def _mask(value: str) -> str:
    if not value:
        return ""
    return "已保存" if len(value) <= 10 else f"{value[:4]}••••••{value[-4:]}"


def read_tencent_asr_config() -> dict[str, Any]:
    """Return configuration health without leaking credentials."""
    settings = _settings()
    return {
        "provider": settings.provider,
        "configured": settings.configured,
        "secret_id_masked": _mask(settings.secret_id),
        "region": settings.region,
        "engine": settings.engine,
        "language": settings.language,
        "sentence_max_seconds": SENTENCE_MAX_SECONDS,
        "sentence_max_bytes": SENTENCE_MAX_BYTES,
        "long_form_supported": True,
        "long_form_transport": "base64_inline",
        "long_form_chunk_seconds": CHUNK_SECONDS,
        "storage": ".env.secrets.local",
    }


def assert_tencent_asr_ready() -> None:
    settings = _settings()
    if not settings.configured:
        raise TencentASRError(
            "尚未配置腾讯云 SecretId / SecretKey；请在「配音中心 → API 管理」中填写并保存。"
        )


def _safe(message: object) -> str:
    return redact_secret(message)[:900]


def _raise_for_error(payload: dict[str, Any], *, product: str) -> None:
    error = response_error(payload)
    if not error:
        return
    raise TencentASRError(
        friendly_error(str(error.get("Code") or ""), str(error.get("Message") or ""), product=product)
    )


def _segments_from_sentence(words: Any, text: str, duration_ms: Any) -> list[dict[str, Any]]:
    """SentenceRecognition returns a word list; fold it into readable segments."""
    if isinstance(words, list) and words:
        groups: list[dict[str, Any]] = []
        current_text = ""
        start_ms: float | None = None
        end_ms = 0.0
        for word in words:
            if not isinstance(word, dict):
                continue
            value = str(word.get("Word") or "")
            begin = word.get("StartTime")
            finish = word.get("EndTime")
            try:
                begin = float(begin)
                finish = float(finish)
            except (TypeError, ValueError):
                continue
            if start_ms is None:
                start_ms = begin
            current_text += value
            end_ms = finish
            if value and value[-1] in "。！？!?，,；;、":
                groups.append({
                    "start": round((start_ms or 0) / 1000, 3),
                    "end": round(end_ms / 1000, 3),
                    "text": current_text.strip(),
                })
                current_text = ""
                start_ms = None
        if current_text.strip():
            groups.append({
                "start": round((start_ms or 0) / 1000, 3),
                "end": round(end_ms / 1000, 3),
                "text": current_text.strip(),
            })
        if groups:
            return groups
    try:
        total = max(0.0, float(duration_ms) / 1000)
    except (TypeError, ValueError):
        total = 0.0
    stripped = str(text or "").strip()
    return [{"start": 0.0, "end": round(total, 3), "text": stripped}] if stripped else []


def transcribe_audio_bytes(
    audio_bytes: bytes,
    *,
    audio_format: str = "mp3",
    duration_seconds: float | None = None,
    request_id: str | None = None,
    timeout_seconds: int = 300,
    poll_interval_seconds: float = 2.0,
    requests_module: Any | None = None,
) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
    """Transcribe one Base64 audio payload, choosing the right ASR route."""
    settings = _settings()
    if not settings.configured:
        raise TencentASRError("尚未配置腾讯云 SecretId / SecretKey")
    data = bytes(audio_bytes or b"")
    if not data:
        raise TencentASRError("收到空音频；请检查该素材是否包含可用人声轨")
    fmt = str(audio_format or "mp3").lower().lstrip(".")
    if fmt not in SUPPORTED_FORMATS:
        raise TencentASRError(f"腾讯云语音识别不支持 {fmt or '未知'} 格式")
    if len(data) > REC_TASK_MAX_BYTES:
        raise TencentASRError(
            "腾讯云语音识别单次 Base64 提交上限约 5 MB；请缩短素材或降低码率后重试"
        )

    is_short = len(data) <= SENTENCE_MAX_BYTES and (
        duration_seconds is None or float(duration_seconds) <= SENTENCE_MAX_SECONDS
    )
    if is_short:
        text, segments, metadata = _transcribe_sentence(data, fmt, requests_module=requests_module)
    else:
        task_id = str(request_id or uuid.uuid4())
        text, segments, metadata = _transcribe_rec_task(
            data,
            fmt,
            task_id=task_id,
            engine=settings.engine,
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
            requests_module=requests_module,
        )
    return text, _clamp_segments(segments, duration_seconds), metadata


def _clamp_segments(segments: list[dict[str, Any]], duration_seconds: float | None) -> list[dict[str, Any]]:
    """Pin segment timestamps inside the audio that was actually submitted.

    腾讯返回的末段 ``EndMs`` 会因解码填充略微越过音频末尾（实测约 33 ms），
    下游互动索引对越界时间戳是硬拒绝的，所以要在这里收回区间内。
    """
    if not segments or duration_seconds is None:
        return segments
    try:
        limit = float(duration_seconds)
    except (TypeError, ValueError):
        return segments
    if not math.isfinite(limit) or limit <= 0:
        return segments
    clamped: list[dict[str, Any]] = []
    for row in segments:
        if not isinstance(row, dict):
            continue
        try:
            start = min(max(0.0, float(row.get("start") or 0.0)), limit)
            end = min(max(start, float(row.get("end") or start)), limit)
        except (TypeError, ValueError):
            clamped.append(row)
            continue
        clamped.append({**row, "start": round(start, 3), "end": round(end, 3)})
    return clamped


def _transcribe_sentence(
    data: bytes, fmt: str, *, requests_module: Any | None = None
) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
    settings = _settings()
    payload = {
        "ProjectId": 0,
        "SubServiceType": 2,
        "EngSerViceType": settings.engine,
        "SourceType": 1,
        "VoiceFormat": fmt,
        "Data": base64.b64encode(data).decode("ascii"),
        "DataLen": len(data),
        "WordInfo": 2,
        "FilterDirty": 1,
        "FilterModal": 1,
        "FilterPunc": 2,
        "ConvertNumMode": 1,
    }
    response = tc3_request(
        ASR_SERVICE,
        "SentenceRecognition",
        payload,
        version=ASR_VERSION,
        host=ASR_HOST,
        timeout=90,
        requests_module=requests_module,
    )
    _raise_for_error(response, product="语音识别（一句话识别）")
    body = response.get("Response") or {}
    text = str(body.get("Result") or "").strip()
    segments = _segments_from_sentence(body.get("WordList"), text, body.get("AudioDuration"))
    return text, segments, {
        "provider": "tencent-asr-sentence",
        "request_id": response_request_id(response),
        "language": settings.language,
        "engine": settings.engine,
        "remote": True,
        "utterance_count": len(segments),
        "timestamp_unit": "seconds",
        "audio_duration": body.get("AudioDuration"),
        "upload_bytes": len(data),
    }


def _transcribe_rec_task(
    data: bytes,
    fmt: str,
    *,
    task_id: str,
    engine: str,
    timeout_seconds: int,
    poll_interval_seconds: float,
    requests_module: Any | None = None,
) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
    settings = _settings()
    submit = tc3_request(
        ASR_SERVICE,
        "CreateRecTask",
        {
            "EngineModelType": engine,
            "ChannelNum": 1,
            "ResTextFormat": 1,
            "SourceType": 1,
            "Data": base64.b64encode(data).decode("ascii"),
            "DataLen": len(data),
            "FilterDirty": 1,
            "FilterModal": 1,
            "FilterPunc": 2,
            "ConvertNumMode": 1,
            "SpeakerDiarization": 0,
        },
        version=ASR_VERSION,
        host=ASR_HOST,
        timeout=120,
        requests_module=requests_module,
    )
    _raise_for_error(submit, product="语音识别（录音文件识别）")
    body = submit.get("Response") or {}
    data_block = body.get("Data") if isinstance(body.get("Data"), dict) else {}
    # 腾讯把任务号嵌在 Response.Data.TaskId 下（顶层没有 TaskId）。
    # 这条录音文件识别路径此前只读顶层，导致长音轨提交后一律报「未返回任务号」。
    provider_task_id = data_block.get("TaskId") if data_block else body.get("TaskId")
    if provider_task_id in (None, ""):
        raise TencentASRError("腾讯云未返回录音文件识别任务号，无法继续查询")

    deadline = time.monotonic() + max(30, int(timeout_seconds))
    while time.monotonic() < deadline:
        time.sleep(max(0.5, poll_interval_seconds))
        query = tc3_request(
            ASR_SERVICE,
            "DescribeTaskStatus",
            {"TaskId": int(provider_task_id)},
            version=ASR_VERSION,
            host=ASR_HOST,
            timeout=60,
            requests_module=requests_module,
        )
        _raise_for_error(query, product="语音识别（录音文件识别）")
        data_block = query.get("Response", {}).get("Data") or {}
        status = str(data_block.get("Status") or "").strip()
        if status == "3":
            message = str(data_block.get("ErrorMsg") or "识别失败")
            raise TencentASRError(f"腾讯云录音文件识别失败：{_safe(message)}")
        if status != "2":
            continue
        text = str(data_block.get("Result") or "").strip()
        segments = _segments_from_detail(data_block.get("ResultDetail"), text)
        return text, segments, {
            "provider": "tencent-asr-rec-task",
            "request_id": response_request_id(query),
            "task_id": str(provider_task_id),
            "language": settings.language,
            "engine": engine,
            "remote": True,
            "utterance_count": len(segments),
            "timestamp_unit": "seconds",
            "upload_bytes": len(data),
            "resumed_task": bool(task_id),
        }
    raise TencentASRError(
        f"腾讯云录音文件识别在 {int(timeout_seconds)} 秒内未完成；可稍后用同一任务号继续查询"
    )


def _segments_from_detail(detail: Any, text: str) -> list[dict[str, Any]]:
    segments: list[dict[str, Any]] = []
    if isinstance(detail, list):
        for item in detail:
            if not isinstance(item, dict):
                continue
            sentence = str(item.get("FinalSentence") or "").strip()
            start_ms = item.get("StartMs")
            end_ms = item.get("EndMs")
            try:
                start = max(0.0, float(start_ms) / 1000)
                end = max(start, float(end_ms) / 1000)
            except (TypeError, ValueError):
                continue
            if sentence:
                segments.append({"start": round(start, 3), "end": round(end, 3), "text": sentence})
    if segments:
        return segments
    stripped = str(text or "").strip()
    return [{"start": 0.0, "end": 0.0, "text": stripped}] if stripped else []


def transcribe_file(
    audio_path: Path,
    *,
    timeout_seconds: int = 300,
    requests_module: Any | None = None,
    ffmpeg: str | None = None,
    on_progress: Callable[[str], None] | None = None,
    on_accepted: Callable[[str], None] | None = None,
    resume_request_id: str | None = None,
    concurrency: int | None = None,
) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
    path = Path(audio_path)
    if not path.is_file():
        raise TencentASRError("待识别的音频文件不存在")
    fmt = path.suffix.lower().lstrip(".")
    if fmt not in SUPPORTED_FORMATS:
        raise TencentASRError(f"腾讯云语音识别不支持 .{fmt or '未知'} 格式")
    data = path.read_bytes()
    ffmpeg_binary, ffprobe_binary = _ffmpeg_tools(ffmpeg)
    # 时长必须一并交给 transcribe_audio_bytes：腾讯的「一句话识别」硬拒 60 秒以上的音频，
    # 而 60–873 秒的音轨体积仍可能小于 5 MB，只按体积分流会把它错送进一句话识别。
    duration = probe_audio_seconds(path, ffprobe_binary) if ffprobe_binary else 0.0
    if len(data) <= REC_TASK_MAX_BYTES:
        return transcribe_audio_bytes(
            data,
            audio_format=fmt,
            duration_seconds=duration or None,
            timeout_seconds=timeout_seconds,
            requests_module=requests_module,
        )
    # 超过单次 Base64 上限：本地分片后有界并发逐片识别，再按片偏移合并回绝对时间戳。
    return _transcribe_long_file(
        path,
        fallback_ffmpeg=ffmpeg_binary or ffmpeg,
        timeout_seconds=timeout_seconds,
        requests_module=requests_module,
        on_progress=on_progress,
        on_accepted=on_accepted,
        resume_request_id=resume_request_id,
        concurrency=concurrency,
    )


def _ffmpeg_tools(fallback_ffmpeg: str | None) -> tuple[str | None, str | None]:
    """Resolve ffmpeg/ffprobe; chunked transcription needs both binaries.

    The caller's ffmpeg wins so a long job keeps using the same binary as the
    rest of its pipeline; ffprobe is taken from the shared locator because the
    transcript contract never carries one.
    """
    from lib.ffmpeg_locator import resolve_ffmpeg_pair

    pair = resolve_ffmpeg_pair()
    if pair:
        return (fallback_ffmpeg or pair[0]), pair[1]
    return fallback_ffmpeg, None


def probe_audio_seconds(path: Path, ffprobe: str) -> float:
    """Read one media file's duration in seconds; 0.0 when undeterminable."""
    import subprocess

    try:
        completed = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(path)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=60, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return 0.0
    try:
        return max(0.0, float(str(completed.stdout or "").strip()))
    except ValueError:
        return 0.0


def _cut_audio_chunk(ffmpeg: str, source: Path, target: Path, start: float, span: float) -> None:
    """Cut one 48 kbps mono chunk so its bytes stay inside the inline submit cap."""
    import subprocess

    command = [
        ffmpeg, "-hide_banner", "-y", "-ss", f"{start:.3f}", "-t", f"{span:.3f}",
        "-i", str(source), "-vn", "-ac", "1", "-ar", "16000",
        "-c:a", "libmp3lame", "-b:a", "48k", str(target),
    ]
    try:
        completed = subprocess.run(
            command, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=600, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise TencentASRError(f"音轨分片失败：{_safe(exc)}") from exc
    if completed.returncode != 0 or not target.is_file() or target.stat().st_size <= 256:
        target.unlink(missing_ok=True)
        raise TencentASRError("音轨分片失败，无法为长素材建立识别片段")


def _chunk_record_is_unreadable(manifest: Path) -> bool:
    """文件在盘上、却不是一条可用记录（崩溃残片 / 非字典 / 缺 ``offset``）。

    刻意与 :func:`_read_chunk_manifest` 分开：后者按既有契约把损坏记录当作「无记录」
    （``None``），而预筛阶段需要进一步区分「根本没有文件」（可提交）与「文件存在却读不出」
    （只可能来自旧版原地写崩在中途，或外部破坏）。后者无法判断该分片是否已被受理计费，
    因此必须按受理不明冻结——否则升级前的残片会让重跑**重复付费**。

    ``PermissionError`` 视为瞬时争用并短退避重试，避免把写者正在替换的完整 ``done``
    误判成残片而误冻结。
    """
    if not manifest.is_file():
        return False
    raw: str | None = None
    for attempt in range(_CHUNK_READ_ATTEMPTS):
        try:
            raw = manifest.read_text(encoding="utf-8")
            break
        except PermissionError:
            if attempt == _CHUNK_READ_ATTEMPTS - 1:
                return True
            time.sleep(_CHUNK_READ_BACKOFF_SECONDS * (attempt + 1))
        except OSError:
            return True
    if raw is None:
        return True
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return True
    if not isinstance(value, dict):
        return True
    try:
        float(value.get("offset"))
    except (TypeError, ValueError):
        return True
    return False


def _read_chunk_manifest(manifest: Path, start: float) -> dict[str, Any] | None:
    """Read one chunk journal for this exact offset, distinguishing three states.

    偏移 ``0.0`` 是合法值，不能用 ``or`` 兜底（0.0 为假值会让第 0 片永远不命中缓存）。

    返回值本身就是三态载体，调用方用 :func:`_chunk_state` 判定：

    * ``{"state": "submitting", ...}`` —— 提交已发出、受理状态不明确，**禁止自动重提**；
    * ``{"state": "done", ...}`` 或历史缓存（无 ``state``）—— 已完成，可复用；
    * ``None`` —— 没有记录，可以提交。

    偏移不匹配或损坏的记录同样按「无记录」处理（返回 ``None``）。「损坏」不再是一个
    可达状态：写前日志与 ``done`` 记录都走 :func:`_write_chunk_marker` 的原子替换，
    磁盘上要么是完整的旧内容、要么是完整的新内容，因此崩溃不会留下截断记录（见该函数）。
    """
    if not manifest.is_file():
        return None
    try:
        cached = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(cached, dict):
        return None
    try:
        cached_offset = float(cached.get("offset"))
    except (TypeError, ValueError):
        return None
    if abs(cached_offset - start) < 1e-3:
        return cached
    return None


def _chunk_state(cached: dict[str, Any] | None) -> str:
    """把一条分片记录归一成三态：``submitting`` / ``done`` / 空串（无记录）。"""
    if not isinstance(cached, dict):
        return ""
    if str(cached.get("state") or "").strip().lower() == CHUNK_STATE_SUBMITTING:
        return CHUNK_STATE_SUBMITTING
    return CHUNK_STATE_DONE


def _is_rate_limited(exc: BaseException) -> bool:
    text = str(exc)
    return any(marker in text for marker in _RATE_LIMIT_MARKERS)


def _is_ambiguous_submit_error(exc: BaseException) -> bool:
    """True when a chunk submission left the provider's acceptance state unknown."""
    text = str(exc).lower()
    return any(marker in text for marker in _AMBIGUOUS_SUBMIT_MARKERS)


def _utc_now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _write_chunk_marker(manifest: Path, payload: dict[str, Any]) -> None:
    """落一条分片写前日志，**原子替换**（临时文件 → ``os.replace``）。

    不能用 ``write_text`` 原地截断写：若恰在「已截断、未写完」的瞬间崩溃，重跑会把
    已经付过费的分片当成「无记录」而重提，从而**重复付费**。临时文件 + ``os.replace``
    保证文件内容在任何时刻要么是**完整旧内容**、要么是**完整新内容**，永不出现截断。

    Windows 上 ``os.replace`` 可能因读者短暂持有句柄而抛 ``PermissionError``（仓库里
    ``material_evidence._atomic_json`` 已有同款处理），这里做少量退避重试。
    """
    manifest.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(
        prefix=f".{manifest.name}.", suffix=".tmp", dir=manifest.parent
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False)
            stream.flush()
        for attempt in range(_ATOMIC_REPLACE_ATTEMPTS):
            try:
                os.replace(temp_name, manifest)
                return
            except PermissionError:
                if attempt == _ATOMIC_REPLACE_ATTEMPTS - 1:
                    raise
                time.sleep(_ATOMIC_REPLACE_BASE_SECONDS * (attempt + 1))
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def _clear_chunk_marker(manifest: Path) -> None:
    """Drop a pre-submit marker once the failure is proven not to have been accepted."""
    try:
        manifest.unlink()
    except FileNotFoundError:
        pass


def _submit_chunk(
    ffmpeg: str,
    source: Path,
    cache_dir: Path,
    descriptor: dict[str, Any],
    *,
    timeout_seconds: int,
    requests_module: Any | None,
    on_progress: Callable[[str], None] | None,
    total: int,
    stats: ConcurrencyStats,
) -> dict[str, Any]:
    """切一片、识别一片、落一片写前日志。失败就地抛出，由调用方按分片序汇总。

    写前日志的顺序是防止重复付费的关键：**在付费提交之前**先落一条 ``submitting`` 标记，
    只有拿到「确定未受理」的失败才清除它。这样进程崩溃 / 连接中断后重跑，能从标记里
    看出「该分片可能已被受理计费」，从而绝不盲目重提。
    """
    index = int(descriptor["index"])
    start = float(descriptor["start"])
    span = float(descriptor["span"])
    manifest = descriptor["manifest"]
    if on_progress:
        on_progress(f"长音轨分片识别 {index + 1}/{total}")
    piece = cache_dir / f"chunk-{index:05d}.mp3"
    _cut_audio_chunk(ffmpeg, source, piece, start, span)
    data = piece.read_bytes()
    if len(data) > REC_TASK_MAX_BYTES:
        raise TencentASRError(
            f"音轨第 {index + 1} 片为 {len(data) / 1024 / 1024:.1f} MB，超过单次 5 MB 提交上限"
        )
    # 付费提交之前落写前日志：即使这次调用中途崩溃，重跑也能看到「可能已受理」。
    _write_chunk_marker(manifest, {
        "offset": start, "span": span, "state": CHUNK_STATE_SUBMITTING, "index": index,
        "submitted_at": _utc_now_iso(), "request_hint": uuid.uuid4().hex,
    })
    attempt = 0
    while True:
        try:
            text, rows, metadata = transcribe_audio_bytes(
                data,
                audio_format="mp3",
                duration_seconds=span,
                timeout_seconds=timeout_seconds,
                requests_module=requests_module,
            )
            break
        except TencentASRError as exc:
            attempt += 1
            rate_limited = _is_rate_limited(exc)
            if rate_limited and attempt < CHUNK_RETRY_ATTEMPTS:
                # 限流是被拒的、不计费，可安全退避重试；标记留到重试结束再决定去留。
                stats.record_retry()
                time.sleep(CHUNK_RETRY_BASE_SECONDS * (2 ** (attempt - 1)))
                continue
            if not rate_limited and _is_ambiguous_submit_error(exc):
                # 受理状态不明确：**保留** submitting 标记并冻结该分片，绝不自动重提。
                raise TencentASRAmbiguous(
                    f"腾讯云语音识别第 {index + 1} 片的提交在返回前中断，可能已受理并计费；"
                    f"系统不会自动重提。请先到腾讯云识别记录人工核对，确认未受理后再续跑"
                    f"（已保留分片标记 {manifest.name}）。"
                ) from exc
            # 确定失败（供应商明确拒绝，或限流已耗尽重试次数）：清除标记，允许日后重提。
            _clear_chunk_marker(manifest)
            raise
    # 短片会走「一句话识别」（只返回 request_id），长片走「录音文件识别」（返回 task_id）。
    # 两者都要落盘：崩溃后要靠它核实本次提交是否已被受理，避免重复付费。
    reference = str(metadata.get("task_id") or metadata.get("request_id") or "")
    payload = {"offset": start, "span": span, "state": CHUNK_STATE_DONE, "text": text,
               "segments": rows, "reference": reference,
               "task_id": str(metadata.get("task_id") or "")}
    # 原子替换，不能原地截断写：崩在「已截断、未写完」的瞬间会让已付费分片被当成
    # 「无记录」而重提（重复付费）。原子写后，崩溃窗口里留下的仍是完整的
    # ``submitting`` 记录，重跑只会冻结、不会重提。
    _write_chunk_marker(manifest, payload)
    return payload


def _transcribe_long_file(
    path: Path,
    *,
    fallback_ffmpeg: str | None,
    timeout_seconds: int,
    requests_module: Any | None,
    on_progress: Callable[[str], None] | None = None,
    on_accepted: Callable[[str], None] | None = None,
    resume_request_id: str | None = None,
    concurrency: int | None = None,
) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
    """识别超出单次 5 MB 上限的长音轨：本地分片 → 有界并发逐片识别 → 按偏移合并。

    每片结果与任务号单独落盘在 ``<音轨名>.chunks/`` 下，中途失败后重跑只会
    重新提交未成功的分片，已付费的分片不会重复计费。合并后的 ``start``/``end``
    是相对原音轨的绝对秒数，因此线上游的窗口匹配与字幕线索都无需改动。

    并发只改变「提交的墙钟」，不改变结果：先做缓存预筛（已成功的分片直接复用），
    再把未命中的分片交给有界并发池，最后**严格按分片序**拼装文本 / 任务号 / 时间戳，
    因此乱序完成也与串行逐字节一致。

    已知代价：分片边界按固定 600 秒切，可能与句尾错位一两个字。
    """
    ffmpeg_binary, ffprobe_binary = _ffmpeg_tools(fallback_ffmpeg)
    if not ffmpeg_binary or not ffprobe_binary:
        raise TencentASRError("本机缺少 FFmpeg/ffprobe，无法为长素材建立分片识别")
    duration = probe_audio_seconds(path, ffprobe_binary)
    if duration <= 0:
        raise TencentASRError("无法读取音轨时长，长素材分片识别无法继续")
    total = max(1, math.ceil(duration / CHUNK_SECONDS))
    cache_dir = path.parent / f"{path.stem}.chunks"
    cache_dir.mkdir(parents=True, exist_ok=True)

    descriptors: list[dict[str, Any]] = []
    for index in range(total):
        start = round(index * CHUNK_SECONDS, 3)
        span = round(min(CHUNK_SECONDS, duration - start), 3)
        if span <= 0:
            break
        descriptors.append({"index": index, "start": start, "span": span,
                            "manifest": cache_dir / f"chunk-{index:05d}.json"})

    # 缓存预筛：已成功落盘的分片直接复用；受理不明的分片触发冻结，绝不重复提交（N1）。
    payloads: dict[int, dict[str, Any]] = {}
    pending: list[dict[str, Any]] = []
    ambiguous: list[dict[str, Any]] = []
    for descriptor in descriptors:
        manifest = descriptor["manifest"]
        cached = _read_chunk_manifest(manifest, float(descriptor["start"]))
        if cached is None:
            # 进一步区分「没有文件」（可提交）与「文件存在却读不出」（崩溃残片 → 冻结）。
            if _chunk_record_is_unreadable(manifest):
                ambiguous.append(descriptor)
            else:
                pending.append(descriptor)
        elif _chunk_state(cached) == CHUNK_STATE_SUBMITTING:
            ambiguous.append(descriptor)
        else:
            payloads[int(descriptor["index"])] = cached

    if ambiguous:
        # 上一次运行有分片「提交已发出、受理状态不明」：冻结整条任务，绝不自动重提（可能重复计费）。
        labels = "、".join(f"第 {int(row['index']) + 1} 片" for row in ambiguous)
        # 受理不明的分片**没有可查询的服务端 id**，因此调用方给的 hint 只能当人工核对
        # 线索展示；绝不能写成「已对账」，否则用户会以为系统已经替他对过账。（保留
        # 「续查标识」字样是因为既有契约按此字面量断言，另加「系统未做服务端对账」澄清。）
        guidance = ""
        if resume_request_id:
            guidance = (
                f"（上次提交线索：续查标识「{resume_request_id}」；仅供人工核对参考，"
                f"系统未做服务端对账）"
            )
        raise TencentASRAmbiguous(
            f"{labels}的上一次提交在返回前中断，可能已受理并计费；系统不会自动重提。"
            f"请先到腾讯云识别记录人工核对，确认未受理后再续跑。{guidance}"
        )

    if pending:
        try:
            limit = resolve_limit(KIND_ASR, concurrency)
        except InteractionConcurrencyError as exc:
            raise TencentASRError(str(exc)) from exc
        stats = ConcurrencyStats()

        def work(descriptor: dict[str, Any], _position: int) -> dict[str, Any]:
            return _submit_chunk(ffmpeg_binary, path, cache_dir, descriptor,
                                 timeout_seconds=timeout_seconds, requests_module=requests_module,
                                 on_progress=on_progress, total=total, stats=stats)

        outcomes = run_bounded(pending, work, limit=limit, stats=stats)
        failure: BaseException | None = None
        ambiguous_failure: BaseException | None = None
        for descriptor, outcome in zip(pending, outcomes):
            if outcome.ok:
                payloads[int(descriptor["index"])] = outcome.value
                continue
            # 「受理不明」优先于同一批里的确定失败：只要有一个分片刻在 submitting，
            # 整条任务就必须以 ambiguous 收口（冻结、不可自动重试），否则用户看到
            # failed 会直接点重试，而那条分片可能已经计费。
            if isinstance(outcome.error, TencentASRAmbiguous) or (
                getattr(outcome.error, "status", "") == "ambiguous"
            ):
                if ambiguous_failure is None:
                    ambiguous_failure = outcome.error
            elif failure is None:
                failure = outcome.error
        if ambiguous_failure is not None:
            raise ambiguous_failure
        if failure is not None:
            # 已成功的分片已单独落盘；抛出后由上层保留日志，续跑只会重试未成功的分片。
            raise failure

    texts: list[str] = []
    segments: list[dict[str, Any]] = []
    task_ids: list[str] = []
    for descriptor in descriptors:
        start = float(descriptor["start"])
        payload = payloads[int(descriptor["index"])]
        reference = str(payload.get("reference") or payload.get("task_id") or "")
        if reference:
            task_ids.append(reference)
            if on_accepted:
                on_accepted(reference)
        if payload.get("text"):
            texts.append(str(payload["text"]))
        for row in payload.get("segments") or []:
            if not isinstance(row, dict):
                continue
            try:
                row_start = float(row["start"]) + start
                row_end = float(row["end"]) + start
            except (KeyError, TypeError, ValueError):
                continue
            segments.append({
                "start": round(max(0.0, row_start), 3),
                "end": round(max(row_start, row_end), 3),
                "text": str(row.get("text") or ""),
            })
    segments.sort(key=lambda row: (row["start"], row["end"]))
    settings = _settings()
    return "\n".join(texts), segments, {
        "provider": "tencent-asr-chunked",
        "request_id": "",
        "task_id": task_ids[-1] if task_ids else "",
        "task_ids": task_ids,
        "language": settings.language,
        "engine": settings.engine,
        "remote": True,
        "utterance_count": len(segments),
        "timestamp_unit": "seconds",
        "chunk_count": total,
        "chunk_seconds": CHUNK_SECONDS,
        "audio_seconds": round(duration, 3),
    }


def test_tencent_asr_connection(*, requests_module: Any | None = None) -> dict[str, Any]:
    """Self-contained connectivity test: TTS a known phrase, then ASR it back.

    This deliberately avoids any external sample URL so the test works on a
    local-only workbench and never depends on a third-party CDN.
    """
    from tools.audio.tencent_tts import TencentTTS

    assert_tencent_asr_ready()
    settings = _settings()
    tts = TencentTTS()
    if tts.get_status().value != "available":
        raise TencentASRError("腾讯云语音合成不可用，无法生成自检音频")
    output = REPO_ROOT / ".backlot" / "audio" / "tencent-asr-selftest.mp3"
    output.parent.mkdir(parents=True, exist_ok=True)
    result = tts.execute({
        "text": TEST_PHRASE,
        "voice_id": "101001",
        "format": "mp3",
        "sample_rate": 16000,
        "playback_rate": 1.0,
        "enable_timestamp": False,
        "output_path": str(output),
        "timeout_seconds": 90,
    })
    if not result.success or not output.is_file():
        raise TencentASRError(f"自检音频生成失败：{_safe(result.error or '未知错误')}")

    text, segments, metadata = transcribe_audio_bytes(
        output.read_bytes(),
        audio_format="mp3",
        requests_module=requests_module,
    )
    if not text:
        raise TencentASRError("腾讯云语音识别已连通，但没有返回可用文本")
    expected_normalised = _normalise_for_match(TEST_PHRASE)
    actual_normalised = _normalise_for_match(text)
    similarity = SequenceMatcher(None, expected_normalised, actual_normalised).ratio()
    return {
        "ok": True,
        "provider": metadata.get("provider"),
        "engine": settings.engine,
        "recognized_text": text,
        "expected_text": TEST_PHRASE,
        "similarity": round(similarity, 3),
        "match": similarity >= MATCH_SIMILARITY_THRESHOLD,
        "segment_count": len(segments),
        "scope": "本地自检：先用腾讯云 TTS 合成，再用腾讯云 ASR 识别",
    }


def create_project_transcript_provider(
    *,
    project_id: str,
    project_dir: Path,
    asset_id: str,
    ffmpeg: str | None = None,
    resume_request_id: str | None = None,
    on_accepted: Callable[[str], None] | None = None,
    on_submitting: Callable[[str], None] | None = None,
    on_progress: Callable[[str], None] | None = None,
    asr_concurrency: int | None = None,
) -> Callable[[Path], tuple[str, list[dict[str, Any]], dict[str, Any]]]:
    """Build the provider-neutral ``TranscriptProvider`` contract for one job."""
    assert_tencent_asr_ready()
    artifact_dir = project_dir / "artifacts" / "asr" / str(asset_id)

    def transcribe(source: Path) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
        audio = _to_cloud_audio(source, artifact_dir, ffmpeg)
        if on_submitting:
            on_submitting(str(uuid.uuid4()))
        # 长音轨的分片与合并由 transcribe_file 内部完成；分片级 on_accepted 由它转交，
        # 因此这里只在单次提交（没有分片任务号列表）时补记一次任务号。
        text, segments, metadata = transcribe_file(
            audio,
            ffmpeg=ffmpeg,
            on_progress=on_progress,
            on_accepted=on_accepted,
            resume_request_id=resume_request_id,
            concurrency=asr_concurrency,
        )
        # 单次提交成功后必须把任务级「提交中」改写成「已受理」：短音轨只返回 request_id，
        # 漏记会让一次成功的腾讯云运行被后续的「受理不明」守卫误判（假冻结）。
        if on_accepted and not metadata.get("task_ids"):
            reference = str(metadata.get("task_id") or metadata.get("request_id") or "")
            if reference:
                on_accepted(reference)
        return text, segments, metadata

    return transcribe


def _to_cloud_audio(source: Path, output_dir: Path, ffmpeg: str | None) -> Path:
    """Extract a mono 16 kHz MP3 track small enough for inline Base64 submit."""
    from hashlib import sha256

    binary = ffmpeg or resolve_ffmpeg()
    if not binary:
        raise TencentASRError("本机未发现 FFmpeg，无法从素材提取可上传的音轨")
    source = source.resolve()
    stat = source.stat()
    digest = sha256(f"{source}|{stat.st_size}|{stat.st_mtime_ns}".encode("utf-8")).hexdigest()[:24]
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / f"{digest}.mp3"
    if output.is_file() and output.stat().st_size > 256:
        return output
    import subprocess

    command = [
        binary, "-hide_banner", "-y", "-i", str(source), "-vn", "-map", "0:a:0",
        "-ac", "1", "-ar", "16000", "-c:a", "libmp3lame", "-b:a", "48k", str(output),
    ]
    try:
        completed = subprocess.run(
            command, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=300, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise TencentASRError(f"无法从素材提取可上传的音轨：{_safe(exc)}") from exc
    if completed.returncode != 0 or not output.is_file() or output.stat().st_size <= 256:
        output.unlink(missing_ok=True)
        raise TencentASRError("素材没有可用音轨，无法使用腾讯云语音识别")
    return output


def tencent_asr_runtime_identity() -> str:
    """Stable cache identity so transcripts never mix between engines."""
    settings = _settings()
    return f"{settings.provider}:{settings.region}:{settings.engine}:audio-16k-mono-mp3-v1"


__all__ = [
    "TencentASRAmbiguous",
    "TencentASRError",
    "TencentASRSettings",
    "DEFAULT_REGION",
    "read_tencent_asr_config",
    "assert_tencent_asr_ready",
    "transcribe_audio_bytes",
    "transcribe_file",
    "test_tencent_asr_connection",
    "create_project_transcript_provider",
    "tencent_asr_runtime_identity",
    "TencentCloudError",
]
