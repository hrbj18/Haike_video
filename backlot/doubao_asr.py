"""Durable, secret-safe adapter for Volcengine Doubao recording-file ASR.

The default 1.0 flash route receives only a Base64-encoded, extracted audio
track.  The older 2.0 route remains available as an explicit signed-URL
fallback.  Neither route silently falls back to another ASR provider.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

from backlot.state import REPO_ROOT
from lib.env_loader import load_env


load_env(REPO_ROOT)

FLASH_URL = "https://openspeech.bytedance.com/api/v3/auc/bigmodel/recognize/flash"
FLASH_RESOURCE_ID = "volc.bigasr.auc_turbo"
SUBMIT_URL = "https://openspeech.bytedance.com/api/v3/auc/bigmodel/submit"
QUERY_URL = "https://openspeech.bytedance.com/api/v3/auc/bigmodel/query"
STANDARD_RESOURCE_ID = "volc.seedasr.auc"
# Backward-compatible name used by the signed URL tests and integrations.
RESOURCE_ID = STANDARD_RESOURCE_ID
ASR_MODE_FLASH = "flash"
ASR_MODE_STANDARD_URL = "standard_url"
VALID_ASR_MODES = {ASR_MODE_FLASH, ASR_MODE_STANDARD_URL}
MAX_FLASH_AUDIO_BYTES = 100 * 1024 * 1024
RECOMMENDED_FLASH_AUDIO_BYTES = 20 * 1024 * 1024
# This is the public audio used in Volcengine's own quick-start example.  It is
# only used by the explicit connection test, never as a project-media fallback.
PUBLIC_SAMPLE_AUDIO_URL = (
    "https://lf3-static.bytednsdoc.com/obj/eden-cn/lm_hz_ihsph/"
    "ljhwZthlaukjlkulzlp/console/bigtts/zh_female_cancan_mars_bigtts.mp3"
)
MAX_MEDIA_URL_TTL_SECONDS = 24 * 60 * 60


class DoubaoASRError(RuntimeError):
    """A known, user-remediable ASR failure."""

    # Cloud ASR was explicitly selected and may be billable.  Media indexing
    # must not silently downgrade its failure to an optional transcript miss.
    required = True


class DoubaoASRAmbiguous(DoubaoASRError):
    """The submit request may have been accepted; never auto-submit again."""

    status = "ambiguous"

    def __init__(self, message: str, *, request_id: str | None = None):
        super().__init__(message)
        self.request_id = request_id


@dataclass(frozen=True)
class DoubaoASRSettings:
    api_key: str
    mode: str
    flash_resource_id: str
    standard_resource_id: str
    public_base_url: str
    media_signing_secret: str

    @property
    def resource_id(self) -> str:
        return self.flash_resource_id if self.mode == ASR_MODE_FLASH else self.standard_resource_id

    @property
    def provider(self) -> str:
        return "doubao-asr-1.0-flash" if self.mode == ASR_MODE_FLASH else "doubao-asr-2.0"


def _settings() -> DoubaoASRSettings:
    # Keep an independent setting available for future credential rotation, but
    # allow the already configured new-console Speech key to serve both TTS and
    # ASR for the same account when Volcengine grants the product entitlement.
    api_key = str(os.environ.get("DOUBAO_ASR_API_KEY") or os.environ.get("DOUBAO_SPEECH_API_KEY") or "").strip()
    mode = str(os.environ.get("DOUBAO_ASR_MODE") or ASR_MODE_FLASH).strip().lower()
    flash_resource_id = str(os.environ.get("DOUBAO_ASR_FLASH_RESOURCE_ID") or FLASH_RESOURCE_ID).strip()
    standard_resource_id = str(
        os.environ.get("DOUBAO_ASR_STANDARD_RESOURCE_ID")
        or os.environ.get("DOUBAO_ASR_RESOURCE_ID")
        or STANDARD_RESOURCE_ID
    ).strip()
    public_base_url = str(os.environ.get("BACKLOT_PUBLIC_BASE_URL") or "").strip().rstrip("/")
    media_signing_secret = str(os.environ.get("DOUBAO_ASR_MEDIA_SIGNING_SECRET") or "").strip()
    return DoubaoASRSettings(api_key, mode, flash_resource_id, standard_resource_id, public_base_url, media_signing_secret)


def _masked(value: str) -> str:
    if not value:
        return ""
    return "已保存" if len(value) <= 8 else f"{value[:3]}••••••{value[-4:]}"


def read_doubao_asr_config() -> dict[str, Any]:
    """Return configuration health without leaking credentials or public URLs."""
    settings = _settings()
    mode_valid = settings.mode in VALID_ASR_MODES
    resource_valid = (
        settings.flash_resource_id == FLASH_RESOURCE_ID
        if settings.mode == ASR_MODE_FLASH
        else settings.standard_resource_id == STANDARD_RESOURCE_ID
    )
    configured = bool(settings.api_key and mode_valid and resource_valid)
    project_media_ready = configured and (
        settings.mode == ASR_MODE_FLASH
        or bool(settings.public_base_url.startswith("https://") and settings.media_signing_secret)
    )
    return {
        "provider": settings.provider if mode_valid else "doubao-asr-invalid",
        "mode": settings.mode,
        "resource_id": settings.resource_id,
        "configured": configured,
        "api_key_masked": _masked(settings.api_key),
        "project_media_ready": project_media_ready,
        "direct_base64_upload": settings.mode == ASR_MODE_FLASH,
        "public_base_url_configured": bool(settings.public_base_url.startswith("https://")),
        "media_signing_secret_configured": bool(settings.media_signing_secret),
        "storage": ".env.secrets.local",
    }


def assert_doubao_asr_media_ready() -> None:
    settings = _settings()
    if not settings.api_key:
        raise DoubaoASRError("未配置豆包 ASR 密钥；请在 .env.secrets.local 设置 DOUBAO_ASR_API_KEY，或复用已配置的 DOUBAO_SPEECH_API_KEY")
    if settings.mode not in VALID_ASR_MODES:
        raise DoubaoASRError("DOUBAO_ASR_MODE 只能是 flash 或 standard_url")
    if settings.mode == ASR_MODE_FLASH:
        if settings.flash_resource_id != FLASH_RESOURCE_ID:
            raise DoubaoASRError("豆包录音文件识别 1.0 极速版资源 ID 必须是 volc.bigasr.auc_turbo")
        return
    if settings.standard_resource_id != STANDARD_RESOURCE_ID:
        raise DoubaoASRError("豆包录音文件识别 2.0 的资源 ID 必须是 volc.seedasr.auc")
    if not settings.public_base_url.startswith("https://"):
        raise DoubaoASRError("豆包 ASR 需要可被互联网访问的 HTTPS 音频地址；请设置 BACKLOT_PUBLIC_BASE_URL 后重试")
    if not settings.media_signing_secret:
        raise DoubaoASRError("豆包 ASR 需要限时音频签名密钥；请设置 DOUBAO_ASR_MEDIA_SIGNING_SECRET 后重试")


def _safe_message(value: object) -> str:
    message = str(value or "豆包语音识别服务返回了未知错误")
    key = _settings().api_key
    if key:
        message = message.replace(key, "[密钥已隐藏]")
    return message[:900]


def _status_code(response: Any) -> str:
    return str(response.headers.get("X-Api-Status-Code") or "").strip()


def _status_message(response: Any) -> str:
    return str(response.headers.get("X-Api-Message") or "").strip()


def _json_or_empty(response: Any) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _headers(
    settings: DoubaoASRSettings,
    request_id: str,
    *,
    include_sequence: bool,
    resource_id: str | None = None,
) -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "X-Api-Key": settings.api_key,
        "X-Api-Resource-Id": resource_id or settings.resource_id,
        "X-Api-Request-Id": request_id,
    }
    if include_sequence:
        headers["X-Api-Sequence"] = "-1"
    return headers


def _validate_remote_audio_url(audio_url: str) -> str:
    normalized = str(audio_url or "").strip()
    if not normalized.startswith("https://"):
        raise DoubaoASRError("豆包 ASR 只能提交可访问的 HTTPS 音频地址")
    return normalized


def _provider_failure(status: str, response: Any, *, resource_id: str) -> DoubaoASRError:
    """Map known Volcengine states to an actionable Chinese remediation."""
    detail = _status_message(response) or _safe_message(_json_or_empty(response).get("message"))
    lowered = detail.lower()
    if status == "45000030" or "requested resource not granted" in lowered:
        service = (
            "录音文件识别 1.0 极速版"
            if resource_id == FLASH_RESOURCE_ID
            else "录音文件识别模型 2.0"
        )
        return DoubaoASRError(
            f"当前 API Key 尚未获得豆包{service}（{resource_id}）的权限。请确认开通实例与 API Key 属于同一新版控制台账号。"
        )
    if status == "55000031":
        if resource_id == FLASH_RESOURCE_ID:
            return DoubaoASRError("豆包 ASR 极速版当前服务繁忙，本次请求已明确失败；请稍后由用户重新发起")
        return DoubaoASRError("豆包 ASR 当前服务繁忙；请稍后从同一任务安全继续查询，不要重复提交")
    if status == "45000132":
        return DoubaoASRError("豆包 ASR 音频超过大小限制；请缩短素材或降低音频体积后重试")
    if status == "45000002":
        return DoubaoASRError("豆包 ASR 收到空音频；请检查该视频是否包含可用人声轨")
    if status == "45000151":
        return DoubaoASRError("豆包 ASR 无法识别该音频格式；请重新提取 MP3 后重试")
    return DoubaoASRError(f"豆包 ASR 请求失败（{status or getattr(response, 'status_code', 'unknown')}）：{_safe_message(detail)}")


def _result_to_transcript(
    payload: dict[str, Any],
    *,
    request_id: str,
    provider: str,
    resource_id: str,
) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
    result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    utterances = result.get("utterances") if isinstance(result.get("utterances"), list) else []
    segments: list[dict[str, Any]] = []
    for item in utterances:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        start_ms = item.get("start_time")
        end_ms = item.get("end_time")
        try:
            start = max(0.0, float(start_ms) / 1000)
            end = max(start, float(end_ms) / 1000)
        except (TypeError, ValueError):
            continue
        if text:
            segments.append({"start": round(start, 3), "end": round(end, 3), "text": text})
    text = str(result.get("text") or "").strip()
    if not text and segments:
        text = "".join(item["text"] for item in segments)
    return text, segments, {
        "provider": provider,
        "resource_id": resource_id,
        "request_id": request_id,
        "remote": True,
        "utterance_count": len(segments),
        "timestamp_unit": "seconds",
    }


def transcribe_url(
    audio_url: str,
    *,
    audio_format: str,
    request_id: str | None = None,
    language: str = "zh-CN",
    timeout_seconds: int = 300,
    poll_interval_seconds: float = 1.5,
    on_accepted: Callable[[str], None] | None = None,
    requests_module: Any | None = None,
) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
    """Submit or resume one known ASR task and return normalized timestamps.

    A submit-network exception is intentionally ambiguous: it may already have
    created a paid task, so the caller must not retry automatically.
    """
    settings = _settings()
    if not settings.api_key:
        raise DoubaoASRError("未配置豆包 ASR 密钥")
    if settings.standard_resource_id != STANDARD_RESOURCE_ID:
        raise DoubaoASRError("豆包录音文件识别 2.0 资源 ID 配置无效")
    normalized_url = _validate_remote_audio_url(audio_url)
    fmt = str(audio_format or "mp3").lower().strip()
    if fmt not in {"mp3", "wav", "ogg", "raw"}:
        raise DoubaoASRError("豆包 ASR 当前仅允许 mp3、wav、ogg 或 raw 音频")
    if requests_module is None:
        import requests as requests_module  # type: ignore[no-redef]

    task_id = str(request_id or uuid.uuid4())
    if not request_id:
        body = {
            "user": {"uid": "openmontage"},
            "audio": {"url": normalized_url, "format": fmt, "language": language},
            "request": {
                "model_name": "bigmodel",
                "enable_itn": True,
                "enable_punc": True,
                "show_utterances": True,
                "enable_speaker_info": False,
                "vad_segment": False,
                "sensitive_words_filter": "",
            },
        }
        try:
            response = requests_module.post(
                SUBMIT_URL,
                headers=_headers(
                    settings,
                    task_id,
                    include_sequence=True,
                    resource_id=settings.standard_resource_id,
                ),
                json=body,
                timeout=(10, 90),
            )
        except Exception as exc:
            raise DoubaoASRAmbiguous("豆包 ASR 提交连接中断，无法确认任务是否已受理；系统没有自动重提") from exc
        status = _status_code(response)
        if status != "20000000":
            raise _provider_failure(status, response, resource_id=settings.standard_resource_id)
        if on_accepted:
            on_accepted(task_id)

    deadline = time.monotonic() + max(20, int(timeout_seconds))
    last_transport_error = ""
    while time.monotonic() < deadline:
        try:
            response = requests_module.post(
                QUERY_URL,
                headers=_headers(
                    settings,
                    task_id,
                    include_sequence=False,
                    resource_id=settings.standard_resource_id,
                ),
                json={},
                timeout=(10, 60),
            )
        except Exception as exc:
            last_transport_error = _safe_message(exc)
            time.sleep(max(.25, poll_interval_seconds))
            continue
        status = _status_code(response)
        if status in {"20000001", "20000002", ""} and int(getattr(response, "status_code", 0) or 0) < 400:
            time.sleep(max(.25, poll_interval_seconds))
            continue
        if status == "20000000":
            return _result_to_transcript(
                _json_or_empty(response),
                request_id=task_id,
                provider="doubao-asr-2.0",
                resource_id=settings.standard_resource_id,
            )
        if status == "20000003":
            return "", [], {
                "provider": "doubao-asr-2.0",
                "resource_id": settings.standard_resource_id,
                "request_id": task_id,
                "remote": True,
                "utterance_count": 0,
                "status": "silent_audio",
            }
        raise _provider_failure(status, response, resource_id=settings.standard_resource_id)
    hint = f"；最后一次查询网络错误：{last_transport_error}" if last_transport_error else ""
    raise DoubaoASRError(f"豆包 ASR 在 {int(timeout_seconds)} 秒内未完成；可安全继续查询同一任务{hint}")


def _flash_request_body(audio_bytes: bytes) -> dict[str, Any]:
    return {
        "user": {"uid": "openmontage"},
        "audio": {"data": base64.b64encode(audio_bytes).decode("ascii")},
        "request": {
            "model_name": "bigmodel",
            "enable_itn": True,
            "enable_punc": True,
            "show_utterances": True,
            "enable_speaker_info": False,
            "vad_segment": False,
            "sensitive_words_filter": "",
        },
    }


def transcribe_audio_bytes_flash(
    audio_bytes: bytes,
    *,
    request_id: str | None = None,
    on_submitting: Callable[[str], None] | None = None,
    requests_module: Any | None = None,
) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
    """Synchronously transcribe one audio payload through ASR 1.0 flash.

    The UUID is checkpointed before the HTTP call.  A transport exception is
    therefore ambiguous and must never be retried automatically.
    """
    settings = _settings()
    if not settings.api_key:
        raise DoubaoASRError("未配置豆包 ASR 密钥")
    if settings.flash_resource_id != FLASH_RESOURCE_ID:
        raise DoubaoASRError("豆包录音文件识别 1.0 极速版资源 ID 配置无效")
    data = bytes(audio_bytes or b"")
    if not data:
        raise DoubaoASRError("豆包 ASR 收到空音频；请检查该视频是否包含可用人声轨")
    if len(data) > MAX_FLASH_AUDIO_BYTES:
        raise DoubaoASRError("豆包 ASR 极速版单条音频不能超过 100 MB；请切分素材或改用 2.0 标准 URL 模式")
    if requests_module is None:
        import requests as requests_module  # type: ignore[no-redef]

    task_id = str(request_id or uuid.uuid4())
    if on_submitting:
        on_submitting(task_id)
    try:
        response = requests_module.post(
            FLASH_URL,
            headers=_headers(
                settings,
                task_id,
                include_sequence=True,
                resource_id=settings.flash_resource_id,
            ),
            json=_flash_request_body(data),
            timeout=(10, 300),
        )
    except Exception as exc:
        raise DoubaoASRAmbiguous(
            "豆包 ASR 极速版提交连接中断，无法确认是否已经计费并完成；系统没有自动重提",
            request_id=task_id,
        ) from exc
    status = _status_code(response)
    if status == "20000000":
        text, segments, metadata = _result_to_transcript(
            _json_or_empty(response),
            request_id=task_id,
            provider="doubao-asr-1.0-flash",
            resource_id=settings.flash_resource_id,
        )
        metadata.update({
            "upload_bytes": len(data),
            "large_direct_upload": len(data) > RECOMMENDED_FLASH_AUDIO_BYTES,
        })
        return text, segments, metadata
    if status == "20000003":
        return "", [], {
            "provider": "doubao-asr-1.0-flash",
            "resource_id": settings.flash_resource_id,
            "request_id": task_id,
            "remote": True,
            "utterance_count": 0,
            "status": "silent_audio",
            "upload_bytes": len(data),
        }
    raise _provider_failure(status, response, resource_id=settings.flash_resource_id)


def transcribe_file_flash(
    audio_path: Path,
    *,
    request_id: str | None = None,
    on_submitting: Callable[[str], None] | None = None,
    requests_module: Any | None = None,
) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
    path = Path(audio_path)
    if not path.is_file():
        raise DoubaoASRError("豆包 ASR 待上传音频不存在")
    if path.suffix.lower() not in {".mp3", ".wav", ".ogg", ".opus"}:
        raise DoubaoASRError("豆包 ASR 极速版只允许 MP3、WAV、OGG 或 OPUS 音频")
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise DoubaoASRError("无法读取豆包 ASR 待上传音频") from exc
    return transcribe_audio_bytes_flash(
        data,
        request_id=request_id,
        on_submitting=on_submitting,
        requests_module=requests_module,
    )


def test_doubao_asr_connection(*, requests_module: Any | None = None) -> dict[str, Any]:
    """Run one user-authorized, minimal real test against Volcengine's sample."""
    settings = _settings()
    if requests_module is None:
        import requests as requests_module  # type: ignore[no-redef]
    if settings.mode == ASR_MODE_FLASH:
        try:
            sample = requests_module.get(PUBLIC_SAMPLE_AUDIO_URL, timeout=(10, 60))
        except Exception as exc:
            raise DoubaoASRError("无法下载火山引擎官方 ASR 测试音频") from exc
        if int(getattr(sample, "status_code", 0) or 0) != 200 or not bytes(getattr(sample, "content", b"")):
            raise DoubaoASRError("火山引擎官方 ASR 测试音频下载失败")
        text, segments, metadata = transcribe_audio_bytes_flash(
            bytes(sample.content),
            requests_module=requests_module,
        )
    else:
        text, segments, metadata = transcribe_url(
            PUBLIC_SAMPLE_AUDIO_URL,
            audio_format="mp3",
            timeout_seconds=90,
            poll_interval_seconds=1.0,
            requests_module=requests_module,
        )
    if not text or not segments:
        raise DoubaoASRError("豆包 ASR 已返回但没有得到可用的文本时间戳")
    return {
        "ok": True,
        "provider": metadata["provider"],
        "resource_id": metadata["resource_id"],
        "utterance_count": len(segments),
        "text_length": len(text),
        "timestamp_coverage": [segments[0]["start"], segments[-1]["end"]],
        "scope": "火山引擎公开示例音频连通测试",
    }


def _audio_fingerprint(path: Path) -> str:
    stat = path.stat()
    raw = f"{path.resolve()}|{stat.st_size}|{stat.st_mtime_ns}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:24]


def _to_cloud_audio(source: Path, output_dir: Path, ffmpeg: str) -> Path:
    source = source.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / f"{_audio_fingerprint(source)}.mp3"
    if output.is_file() and output.stat().st_size > 256:
        return output
    command = [
        ffmpeg, "-hide_banner", "-y", "-i", str(source), "-vn", "-map", "0:a:0",
        "-ac", "1", "-ar", "16000", "-c:a", "libmp3lame", "-b:a", "64k", str(output),
    ]
    try:
        completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DoubaoASRError("无法从素材提取可上传的音轨") from exc
    if completed.returncode != 0 or not output.is_file() or output.stat().st_size <= 256:
        try:
            output.unlink(missing_ok=True)
        except OSError:
            pass
        raise DoubaoASRError("素材没有可用音轨，无法使用豆包语音识别")
    return output


def _signature_payload(project_id: str, relative_path: str, expires_at: int) -> bytes:
    return f"doubao-asr-media-v1|{project_id}|{relative_path}|{expires_at}".encode("utf-8")


def _sign(project_id: str, relative_path: str, expires_at: int, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), _signature_payload(project_id, relative_path, expires_at), hashlib.sha256).hexdigest()


def build_signed_project_audio_url(project_id: str, project_dir: Path, audio_path: Path, *, ttl_seconds: int = 3600) -> str:
    """Make a short-lived server URL for a generated audio artifact only."""
    settings = _settings()
    if not settings.api_key:
        raise DoubaoASRError("未配置豆包 ASR 密钥")
    if not settings.public_base_url.startswith("https://"):
        raise DoubaoASRError("豆包 ASR 2.0 备用模式需要可被互联网访问的 HTTPS 音频地址")
    if not settings.media_signing_secret:
        raise DoubaoASRError("豆包 ASR 2.0 备用模式需要限时音频签名密钥")
    root = project_dir.resolve()
    target = audio_path.resolve()
    try:
        relative = target.relative_to(root).as_posix()
    except ValueError as exc:
        raise DoubaoASRError("豆包 ASR 音频不在当前项目目录内") from exc
    if not relative.startswith("artifacts/asr/") or target.suffix.lower() != ".mp3":
        raise DoubaoASRError("豆包 ASR 只能公开项目内生成的临时 MP3 音轨")
    expires_at = int(time.time()) + max(60, min(int(ttl_seconds), MAX_MEDIA_URL_TTL_SECONDS))
    signature = _sign(str(project_id), relative, expires_at, settings.media_signing_secret)
    return (
        f"{settings.public_base_url}/api/project/{quote(str(project_id), safe='')}/workbench/asr-audio"
        f"?path={quote(relative, safe='/')}&expires_at={expires_at}&signature={signature}"
    )


def resolve_signed_project_audio(project_id: str, project_dir: Path, relative_path: str, expires_at: int, signature: str) -> Path:
    """Validate the server route's project-scoped, expiring audio token."""
    settings = _settings()
    relative = str(relative_path or "").replace("\\", "/").lstrip("/")
    try:
        expiry = int(expires_at)
    except (TypeError, ValueError) as exc:
        raise DoubaoASRError("豆包 ASR 临时音频链接无效") from exc
    if expiry < int(time.time()) or expiry > int(time.time()) + MAX_MEDIA_URL_TTL_SECONDS:
        raise DoubaoASRError("豆包 ASR 临时音频链接已过期")
    if not settings.media_signing_secret:
        raise DoubaoASRError("豆包 ASR 音频签名服务尚未配置")
    expected = _sign(str(project_id), relative, expiry, settings.media_signing_secret)
    if not hmac.compare_digest(expected, str(signature or "")):
        raise DoubaoASRError("豆包 ASR 临时音频链接签名无效")
    root = project_dir.resolve()
    target = (root / relative).resolve()
    try:
        target.relative_to(root / "artifacts" / "asr")
    except ValueError as exc:
        raise DoubaoASRError("豆包 ASR 临时音频链接越出允许目录") from exc
    if target.suffix.lower() != ".mp3" or not target.is_file():
        raise DoubaoASRError("豆包 ASR 临时音频不存在")
    return target


def create_project_transcript_provider(
    *,
    project_id: str,
    project_dir: Path,
    asset_id: str,
    ffmpeg: str,
    resume_request_id: str | None = None,
    on_accepted: Callable[[str], None] | None = None,
    on_submitting: Callable[[str], None] | None = None,
) -> Callable[[Path], tuple[str, list[dict[str, Any]], dict[str, Any]]]:
    """Create the existing ``TranscriptProvider`` contract for one media job."""
    assert_doubao_asr_media_ready()
    artifact_dir = project_dir / "artifacts" / "asr" / str(asset_id)
    settings = _settings()

    def transcribe(source: Path) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
        audio = _to_cloud_audio(source, artifact_dir, ffmpeg)
        # A durable 2.0 task predates a possible switch to the default flash
        # mode. Continue querying that exact UUID instead of submitting audio
        # again through the newly selected route.
        if resume_request_id:
            return transcribe_url(
                "https://resume-existing-task.invalid/audio.mp3",
                audio_format="mp3",
                request_id=resume_request_id,
                on_accepted=on_accepted,
            )
        if settings.mode == ASR_MODE_FLASH:
            return transcribe_file_flash(audio, on_submitting=on_submitting)
        url = build_signed_project_audio_url(project_id, project_dir, audio)
        return transcribe_url(
            url,
            audio_format="mp3",
            request_id=resume_request_id,
            on_accepted=on_accepted,
        )

    return transcribe


def doubao_asr_runtime_identity() -> str:
    """Stable cache identity so flash and standard transcripts never mix."""
    settings = _settings()
    return f"{settings.provider}:{settings.resource_id}:audio-16k-mono-mp3-v1"
