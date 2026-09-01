"""Compatibility adapter for the Haike Video embedded local TTS service.

The historical class and tool names remain stable so existing project state
does not need migration.  Runtime generation is owned by Haike Video and no
longer starts or calls the standalone Voicebox application.
"""

from __future__ import annotations

import hashlib
import http.client
import os
import time
import urllib.error
import urllib.request
import json
import wave
from pathlib import Path
from typing import Any
from uuid import uuid4

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


DEFAULT_BASE_URL = "http://127.0.0.1:17494"
DEFAULT_PROFILE_NAME = "Qwen Serena"
DEFAULT_ENGINE = "qwen_custom_voice"
DEFAULT_MODEL_SIZE = "1.7B"
DEFAULT_LANGUAGE = "zh"
TERMINAL_STATUSES = {"completed", "failed", "cancelled"}
JOB_STATUSES = {"queued", "generating", *TERMINAL_STATUSES}


class TTSServiceUnavailable(RuntimeError):
    """A localhost transport failure; the same request ID may be retried."""

    def __init__(self, message: str, *, ambiguous_after_submit: bool = False) -> None:
        super().__init__(message)
        self.ambiguous_after_submit = ambiguous_after_submit
        self.same_request_id_safe = True


class TTSRequestConflict(RuntimeError):
    """The service rejected an idempotency or frozen-profile conflict."""


class TTSProtocolError(RuntimeError):
    """The local service returned a response that cannot be recovered safely."""


class TTSJobNotReady(RuntimeError):
    """Audio was requested before its durable task completed."""


class VoiceboxTTS(BaseTool):
    """Generate narration through the Haike Video local TTS server."""

    _resolved_base_url: str | None = None
    _base_url_candidates = (
        "http://127.0.0.1:17494",
    )

    name = "voicebox_tts"
    version = "0.1.0"
    tier = ToolTier.VOICE
    capability = "tts"
    provider = "haike_video_local_tts"
    stability = ToolStability.BETA
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.DETERMINISTIC
    runtime = ToolRuntime.LOCAL

    dependencies = ["http:HAIKE_VIDEO_TTS_BASE_URL"]
    install_instructions = (
        "Run scripts/setup_local_tts.ps1 once, then scripts/start_local_tts.ps1."
    )
    agent_skills: list[str] = []

    capabilities = ["text_to_speech", "offline_generation", "voice_selection"]
    supports = {
        "offline": True,
        "multilingual": True,
        "voice_cloning": True,
        "native_audio": True,
    }
    best_for = [
        "local Chinese narration through Haike Video's embedded Qwen3-TTS runtime",
        "Qwen CustomVoice presets and private cloned profiles migrated into Haike Video",
        "private, API-free voiceover generation",
    ]
    not_good_for = [
        "machines where the Haike Video local TTS runtime has not been installed",
        "high-concurrency CPU generation with the Qwen 1.7B model",
    ]

    input_schema = {
        "type": "object",
        "required": ["text"],
        "properties": {
            "text": {"type": "string"},
            "profile_id": {"type": "string"},
            "profile_name": {"type": "string"},
            "voice_id": {
                "type": "string",
                "description": "Alias for Voicebox profile_id or preset voice id.",
            },
            "language": {"type": "string", "default": DEFAULT_LANGUAGE},
            "engine": {"type": "string", "default": DEFAULT_ENGINE},
            "model_size": {"type": "string", "default": DEFAULT_MODEL_SIZE},
            "instruct": {"type": "string"},
            "normalize": {"type": "boolean", "default": True},
            "personality": {"type": "boolean", "default": False},
            "poll_seconds": {"type": "number", "default": 2.0},
            "timeout_seconds": {"type": "integer", "default": 1800},
            "output_path": {"type": "string"},
            "request_id": {
                "type": "string",
                "description": "Stable per-attempt idempotency key for recoverable submit/query/download flows.",
            },
            "voice_signature": {
                "type": "string",
                "description": "Opaque frozen profile signature returned by /profiles.",
            },
        },
    }

    resource_profile = ResourceProfile(
        cpu_cores=4, ram_mb=4096, vram_mb=0, disk_mb=200, network_required=False
    )
    retry_policy = RetryPolicy(max_retries=0)
    idempotency_key_fields = ["text", "profile_id", "engine", "model_size", "instruct"]
    side_effects = ["writes WAV audio to output_path", "calls the Haike Video localhost TTS service"]
    user_visible_verification = ["Listen to the generated WAV for intelligibility and voice consistency"]

    @classmethod
    def _base_url(cls) -> str:
        configured = os.environ.get("HAIKE_VIDEO_TTS_BASE_URL", "").strip()
        if not configured:
            # Temporary compatibility for existing private .env files.  New
            # installations only document HAIKE_VIDEO_TTS_BASE_URL.
            configured = os.environ.get("VOICEBOX_BASE_URL", "auto").strip()
        if configured and configured.lower() not in {"auto", "detect"}:
            return configured.rstrip("/")

        if cls._resolved_base_url:
            return cls._resolved_base_url

        for candidate in cls._base_url_candidates:
            try:
                request = urllib.request.Request(f"{candidate}/health", method="GET")
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                with opener.open(request, timeout=2) as response:
                    if response.status < 400:
                        cls._resolved_base_url = candidate
                        return candidate
            except (OSError, urllib.error.URLError):
                continue

        # Preserve the usual default in the eventual connection error.
        return DEFAULT_BASE_URL

    @classmethod
    def _request(
        cls,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        timeout: float = 30,
    ) -> Any:
        data = None
        headers = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
        req = urllib.request.Request(cls._base_url() + path, data=data, headers=headers, method=method)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(req, timeout=timeout) as response:
                raw = response.read()
                if "audio" in response.headers.get("Content-Type", ""):
                    return raw
                try:
                    text = raw.decode("utf-8")
                    # Some legacy status routes return one SSE event. Support
                    # both JSON and SSE without weakening response validation.
                    events = [
                        line.removeprefix("data:").strip()
                        for line in text.splitlines()
                        if line.startswith("data:") and line.removeprefix("data:").strip()
                    ]
                    return json.loads(events[-1] if events else text)
                except (json.JSONDecodeError, UnicodeError) as exc:
                    raise TTSProtocolError(f"Haike Video 本地配音返回了无效 JSON/SSE：{exc}") from exc
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            if exc.code == 409:
                raise TTSRequestConflict(f"Haike Video TTS 请求冲突：{detail}") from exc
            if exc.code >= 500:
                raise TTSServiceUnavailable(
                    f"Haike Video TTS 服务暂不可用：HTTP {exc.code}",
                    ambiguous_after_submit=method.upper() == "POST",
                ) from exc
            raise RuntimeError(f"Haike Video TTS {method} {path} failed: HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise TTSServiceUnavailable(
                f"无法连接 Haike Video 本地配音服务 {cls._base_url()}：{exc.reason}",
                ambiguous_after_submit=method.upper() == "POST",
            ) from exc
        except (TimeoutError, ConnectionError, OSError, http.client.IncompleteRead) as exc:
            ambiguous = method.upper() == "POST"
            hint = "；提交结果可能未知，请仅使用相同 request_id 安全重试" if ambiguous else ""
            raise TTSServiceUnavailable(
                f"Haike Video 本地配音传输中断：{exc}{hint}",
                ambiguous_after_submit=ambiguous,
            ) from exc

    @classmethod
    def get_status(cls) -> ToolStatus:
        try:
            health = cls._request("GET", "/health", timeout=2)
            if isinstance(health, dict) and health.get("status") in {"healthy", "ok"}:
                return ToolStatus.AVAILABLE
        except Exception:  # noqa: BLE001 - status probing must never break discovery.
            pass
        return ToolStatus.UNAVAILABLE

    @classmethod
    def _resolve_profile_id(cls, inputs: dict[str, Any]) -> str:
        return str(cls._resolve_profile(inputs).get("id") or "")

    @classmethod
    def list_profiles(cls) -> list[dict[str, Any]]:
        """Return local profiles in a UI-ready, stable shape."""
        profiles = cls._request("GET", "/profiles", timeout=10)
        if isinstance(profiles, dict):
            profiles = profiles.get("value", profiles.get("profiles", []))
        if not isinstance(profiles, list):
            return []
        normalized: list[dict[str, Any]] = []
        for raw in profiles:
            if not isinstance(raw, dict) or not raw.get("id"):
                continue
            normalized.append({
                "id": str(raw["id"]),
                "name": str(raw.get("name") or raw["id"]),
                "description": str(raw.get("description") or ""),
                "language": str(raw.get("language") or DEFAULT_LANGUAGE),
                "voice_type": str(raw.get("voice_type") or "preset"),
                "default_engine": str(raw.get("default_engine") or DEFAULT_ENGINE),
                "preset_voice_id": raw.get("preset_voice_id"),
                "available": raw.get("available") is not False,
                "voice_signature": raw.get("voice_signature"),
            })
        return normalized

    @classmethod
    def _resolve_profile(cls, inputs: dict[str, Any]) -> dict[str, Any]:
        explicit = (
            inputs.get("profile_id")
            or inputs.get("voice_id")
            or os.environ.get("HAIKE_VIDEO_TTS_PROFILE_ID")
            or os.environ.get("VOICEBOX_PROFILE_ID")
        )
        profiles = cls.list_profiles()
        if explicit:
            profile_id = str(explicit)
            return next((profile for profile in profiles if profile["id"] == profile_id), {"id": profile_id})

        profile_name = str(
            inputs.get("profile_name")
            or os.environ.get("HAIKE_VIDEO_TTS_PROFILE_NAME")
            or os.environ.get("VOICEBOX_PROFILE_NAME")
            or DEFAULT_PROFILE_NAME
        ).lower()
        for profile in profiles:
            if profile["name"].lower() == profile_name:
                return profile
        raise RuntimeError(f"找不到 Haike Video 本地音色：{profile_name}")

    @classmethod
    def submit(cls, inputs: dict[str, Any], *, request_id: str) -> dict[str, Any]:
        """Submit one recoverable local job without polling or downloading."""

        text = str(inputs.get("text") or "").strip()
        if not text:
            raise ValueError("Haike Video 本地配音文本不能为空")
        if not str(request_id or "").strip():
            raise TTSProtocolError("可恢复配音提交必须提供稳定 request_id")
        profile = cls._resolve_profile(inputs)
        voice_signature = str(profile.get("voice_signature") or "")
        if profile.get("available") is False or len(voice_signature) != 64:
            raise TTSProtocolError("所选本地音色不可用或缺少可冻结的音色签名")
        requested_voice_signature = str(inputs.get("voice_signature") or "").strip()
        if requested_voice_signature and requested_voice_signature != voice_signature:
            raise TTSRequestConflict("父任务冻结音色签名与当前本地音色不一致")
        engine = str(
            inputs.get("engine")
            or profile.get("default_engine")
            or os.environ.get("HAIKE_VIDEO_TTS_ENGINE")
            or os.environ.get("VOICEBOX_ENGINE", DEFAULT_ENGINE)
        )
        language = str(
            inputs.get("language")
            or os.environ.get("HAIKE_VIDEO_TTS_LANGUAGE")
            or os.environ.get("VOICEBOX_LANGUAGE", DEFAULT_LANGUAGE)
        )
        payload: dict[str, Any] = {
            "text": text,
            "profile": str(profile["id"]),
            "language": language,
            "engine": engine,
            "model_size": str(inputs.get("model_size") or DEFAULT_MODEL_SIZE),
            "instruct": str(inputs.get("instruct") or ""),
            "personality": bool(inputs.get("personality", False)),
            "normalize": bool(inputs.get("normalize", True)),
            "request_id": str(request_id),
            "voice_signature": voice_signature,
        }
        response = cls._request("POST", "/speak", payload, timeout=60)
        if not isinstance(response, dict):
            raise TTSProtocolError("Haike Video 本地配音提交返回了非对象响应")
        generation_id = str(response.get("id") or response.get("generation_id") or "")
        status = str(response.get("status") or "")
        if not generation_id or status not in JOB_STATUSES:
            raise TTSProtocolError("Haike Video 本地配音提交缺少有效任务 ID 或状态")
        if response.get("request_id") != request_id:
            raise TTSProtocolError("Haike Video 本地配音未回显相同 request_id，禁止进入不可恢复轮询")
        if response.get("voice_signature") != voice_signature:
            raise TTSProtocolError("Haike Video 本地配音未回显冻结音色签名")
        return {
            "generation_id": generation_id,
            "request_id": str(request_id),
            "status": status,
            "api_mode": "speak",
            "profile_id": str(profile["id"]),
            "profile_name": profile.get("name"),
            "engine": engine,
            "language": language,
            "voice_signature": voice_signature,
        }

    @classmethod
    def query(cls, generation_id: str, *, api_mode: str = "speak") -> dict[str, Any]:
        """Read one durable status snapshot without sleeping."""

        path = f"/generate/{generation_id}/status" if api_mode == "speak" else f"/history/{generation_id}"
        info = cls._request("GET", path, timeout=30)
        if not isinstance(info, dict):
            raise TTSProtocolError("Haike Video 本地配音返回了无效任务状态")
        returned_id = str(info.get("id") or info.get("generation_id") or "")
        status = str(info.get("status") or "")
        if returned_id != str(generation_id) or status not in JOB_STATUSES:
            raise TTSProtocolError("Haike Video 本地配音任务 ID 或状态不符合协议")
        return {
            "generation_id": returned_id,
            "request_id": info.get("request_id"),
            "status": status,
            "duration": info.get("duration"),
            "sample_rate": info.get("sample_rate"),
            "voice_signature": info.get("voice_signature"),
            "error": info.get("error"),
        }

    @classmethod
    def download(
        cls,
        generation_id: str,
        output_path: str | Path,
        *,
        api_mode: str = "speak",
    ) -> dict[str, Any]:
        """Atomically promote one completed, validated PCM16 WAV."""

        status = cls.query(generation_id, api_mode=api_mode)
        if status["status"] != "completed":
            raise TTSJobNotReady(f"配音任务 {generation_id} 尚未完成：{status['status']}")
        data = cls._request("GET", f"/audio/{generation_id}", timeout=180)
        if not isinstance(data, bytes) or not data:
            raise TTSProtocolError("Haike Video 本地配音没有返回音频")
        destination = Path(output_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
        try:
            temporary.write_bytes(data)
            with wave.open(str(temporary), "rb") as wav:
                channels = wav.getnchannels()
                sample_width = wav.getsampwidth()
                sample_rate = wav.getframerate()
                frames = wav.getnframes()
                compression = wav.getcomptype()
                raw_frames = wav.readframes(frames)
            expected_bytes = frames * channels * sample_width
            if (
                compression != "NONE"
                or channels <= 0
                or sample_width != 2
                or sample_rate <= 0
                or frames <= 0
                or len(raw_frames) != expected_bytes
            ):
                raise TTSProtocolError("Haike Video 本地配音返回的不是完整 PCM16 WAV")
            digest = hashlib.sha256(data).hexdigest()
            os.replace(temporary, destination)
        except (OSError, wave.Error, EOFError) as exc:
            raise TTSProtocolError(f"Haike Video 本地配音 WAV 校验失败：{exc}") from exc
        finally:
            if temporary.exists():
                temporary.unlink()
        return {
            "generation_id": str(generation_id),
            "output": str(destination),
            "sha256": digest,
            "sample_rate": sample_rate,
            "channels": channels,
            "sample_width": sample_width,
            "duration_seconds": round(frames / sample_rate, 6),
        }

    @classmethod
    def _download_audio(cls, generation_id: str, output_path: Path, api_mode: str = "speak") -> None:
        cls.download(generation_id, output_path, api_mode=api_mode)

    @classmethod
    def _wait_for_generation(
        cls,
        generation_id: str,
        poll_seconds: float,
        timeout_seconds: int,
        api_mode: str,
    ) -> dict[str, Any]:
        started = time.monotonic()
        while True:
            info = cls.query(generation_id, api_mode=api_mode)
            status = str(info.get("status"))
            if status in TERMINAL_STATUSES:
                return info
            if time.monotonic() - started > timeout_seconds:
                raise RuntimeError(f"Haike Video 本地配音超过 {timeout_seconds} 秒仍未完成")
            time.sleep(max(0.2, poll_seconds))

    @classmethod
    def estimate_cost(cls, inputs: dict[str, Any]) -> float:
        return 0.0

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        if self.get_status() != ToolStatus.AVAILABLE:
            return ToolResult(success=False, error="Haike Video 本地配音服务不可用。" + self.install_instructions)

        text = str(inputs.get("text", "")).strip()
        if not text:
            return ToolResult(success=False, error="Haike Video 本地配音文本不能为空。")

        started = time.time()
        output_path = Path(inputs.get("output_path", "voicebox_tts.wav"))
        try:
            profile = self._resolve_profile(inputs)
            profile_id = str(profile["id"])
            engine = str(
                inputs.get("engine")
                or profile.get("default_engine")
                or os.environ.get("HAIKE_VIDEO_TTS_ENGINE")
                or os.environ.get("VOICEBOX_ENGINE", DEFAULT_ENGINE)
            )
            modern_payload: dict[str, Any] = {
                "text": text,
                "profile": profile_id,
                "language": (
                    inputs.get("language")
                    or os.environ.get("HAIKE_VIDEO_TTS_LANGUAGE")
                    or os.environ.get("VOICEBOX_LANGUAGE", DEFAULT_LANGUAGE)
                ),
                "engine": engine,
                "personality": bool(inputs.get("personality", False)),
            }
            try:
                response = self._request("POST", "/speak", modern_payload, timeout=60)
                api_mode = "speak"
            except RuntimeError as modern_error:
                # Older Voicebox builds do not expose /speak.  Only fall back
                # for a missing route: other errors are useful user feedback
                # and must not be hidden behind a second request.
                if "HTTP 404" not in str(modern_error):
                    raise
                legacy_payload: dict[str, Any] = {
                    "profile_id": profile_id,
                    "text": text,
                    "language": modern_payload["language"],
                    "engine": engine,
                    "model_size": inputs.get("model_size") or os.environ.get("VOICEBOX_MODEL_SIZE", DEFAULT_MODEL_SIZE),
                    "personality": modern_payload["personality"],
                    "normalize": bool(inputs.get("normalize", True)),
                }
                instruct = (
                    inputs.get("instruct")
                    or os.environ.get("HAIKE_VIDEO_TTS_INSTRUCT")
                    or os.environ.get("VOICEBOX_INSTRUCT")
                )
                if instruct:
                    legacy_payload["instruct"] = instruct
                response = self._request("POST", "/generate", legacy_payload, timeout=60)
                api_mode = "legacy"
            generation_id = response.get("id") or response.get("generation_id")
            if not generation_id:
                raise RuntimeError(f"Haike Video 本地配音没有返回任务 ID：{response}")

            info = self._wait_for_generation(
                str(generation_id),
                float(inputs.get("poll_seconds", 2.0)),
                int(inputs.get("timeout_seconds", 1800)),
                api_mode,
            )
            if str(info.get("status")) != "completed":
                return ToolResult(
                    success=False,
                    error=f"Haike Video 本地配音任务 {generation_id} 结束状态为 {info.get('status')}：{info.get('error', '')}",
                    data={"generation_id": generation_id, "status": info.get("status")},
                )

            self._download_audio(str(generation_id), output_path, api_mode)
            return ToolResult(
                success=True,
                data={
                    "provider": self.provider,
                    "generation_id": generation_id,
                    "profile_id": profile_id,
                    "profile_name": profile.get("name"),
                    "engine": engine,
                    "model_size": inputs.get("model_size") or DEFAULT_MODEL_SIZE,
                    "language": modern_payload["language"],
                    "api_mode": api_mode,
                    "duration": info.get("duration"),
                    "output": str(output_path),
                    "format": "wav",
                },
                artifacts=[str(output_path)],
                model=str(inputs.get("model_size") or DEFAULT_MODEL_SIZE),
                duration_seconds=round(time.time() - started, 2),
            )
        except Exception as exc:  # noqa: BLE001 - surface provider errors as ToolResult.
            return ToolResult(success=False, error=f"Haike Video 本地配音失败：{exc}", duration_seconds=round(time.time() - started, 2))
