"""OpenMontage-owned Qwen3-TTS engine, profile store, and serial job queue.

Heavy model imports are intentionally lazy.  The main OpenMontage environment
can discover this module without installing PyTorch; the dedicated TTS runtime
created by ``scripts/setup_local_tts.ps1`` owns those dependencies.
"""

from __future__ import annotations

import importlib.util
import hashlib
import json
import logging
import os
import queue
import re
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4, uuid5


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATA_DIR = REPO_ROOT / ".backlot" / "tts"
DEFAULT_PROFILE_FILE = REPO_ROOT / "config" / "tts" / "profiles.default.json"
DEFAULT_BASE_MODEL = "Qwen/Qwen3-TTS-12Hz-1.7B-Base"
DEFAULT_CUSTOM_MODEL = "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice"
DEFAULT_CPU_DTYPE = "bfloat16"
TERMINAL_STATUSES = {"completed", "failed", "cancelled"}
REQUEST_NAMESPACE = UUID("2a31bf1c-418f-55c0-90e8-768a70cb8a47")
REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
LOGGER = logging.getLogger(__name__)
LANGUAGE_NAMES = {
    "zh": "Chinese",
    "zh-cn": "Chinese",
    "chinese": "Chinese",
    "en": "English",
    "english": "English",
    "ja": "Japanese",
    "japanese": "Japanese",
    "ko": "Korean",
    "korean": "Korean",
    "auto": "Auto",
}


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def data_dir_from_env() -> Path:
    value = os.environ.get("OPENMONTAGE_TTS_DATA_DIR", "").strip()
    return Path(value).expanduser().resolve() if value else DEFAULT_DATA_DIR


def model_cache_from_env(data_dir: Path | None = None) -> Path:
    value = os.environ.get("OPENMONTAGE_TTS_MODEL_CACHE", "").strip()
    return Path(value).expanduser().resolve() if value else (data_dir or data_dir_from_env()) / "models"


def runtime_dependencies() -> dict[str, bool]:
    return {
        name: importlib.util.find_spec(name) is not None
        for name in ("qwen_tts", "torch", "soundfile", "numpy")
    }


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return default


def _write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        for attempt in range(7):
            try:
                os.replace(temporary, path)
                return
            except PermissionError as exc:
                retryable = getattr(exc, "winerror", None) in {5, 32} or getattr(exc, "errno", None) in {13}
                if not retryable or attempt == 6:
                    raise
                time.sleep(0.02 * (2 ** attempt))
    finally:
        temporary.unlink(missing_ok=True)


class ProfileError(ValueError):
    """Raised for an invalid or unavailable local TTS profile."""


class ProfileSignatureMismatch(ProfileError):
    """Raised when a queued voice no longer matches its frozen signature."""


class IdempotencyConflict(ValueError):
    """Raised when one request ID is reused with different immutable inputs."""


class JobLedgerError(RuntimeError):
    """Raised when existing durable job evidence cannot be read safely."""


class ProfileStore:
    """Merge checked-in preset profiles with ignored, machine-local profiles."""

    def __init__(self, data_dir: Path | None = None, default_file: Path | None = None) -> None:
        self.data_dir = (data_dir or data_dir_from_env()).resolve()
        self.default_file = default_file or DEFAULT_PROFILE_FILE
        self.local_file = self.data_dir / "profiles.json"

    def list_profiles(self) -> list[dict[str, Any]]:
        defaults = _read_json(self.default_file, {}).get("profiles", [])
        local = _read_json(self.local_file, {}).get("profiles", [])
        merged: dict[str, dict[str, Any]] = {}
        for raw in [*defaults, *local]:
            if not isinstance(raw, dict) or not str(raw.get("id") or "").strip():
                continue
            profile = dict(raw)
            profile["id"] = str(profile["id"])
            profile.setdefault("name", profile["id"])
            profile.setdefault("description", "")
            profile.setdefault("language", "zh")
            profile.setdefault("voice_type", "preset")
            profile.setdefault("default_engine", "qwen_custom_voice" if profile["voice_type"] == "preset" else "qwen")
            profile.setdefault("samples", [])
            merged[profile["id"]] = profile
        return list(merged.values())

    def public_profiles(self) -> list[dict[str, Any]]:
        profiles: list[dict[str, Any]] = []
        for profile in self.list_profiles():
            try:
                signature = self.voice_signature(profile["id"])
                available = True
            except ProfileError:
                signature = None
                available = False
            profiles.append({
                "id": profile["id"],
                "name": profile["name"],
                "description": profile.get("description") or "",
                "language": profile.get("language") or "zh",
                "voice_type": profile.get("voice_type") or "preset",
                "default_engine": profile.get("default_engine") or "qwen",
                "preset_voice_id": profile.get("preset_voice_id"),
                "available": available,
                "voice_signature": signature,
            })
        return profiles

    @staticmethod
    def _sha256_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def voice_signature(self, profile_or_id: dict[str, Any] | str) -> str:
        """Return an opaque signature without exposing samples, paths or text."""

        profile_id = str(profile_or_id.get("id") if isinstance(profile_or_id, dict) else profile_or_id)
        profile = self.get(profile_id)
        voice_type = str(profile.get("voice_type") or "preset")
        model_id = (
            os.environ.get("OPENMONTAGE_TTS_BASE_MODEL", DEFAULT_BASE_MODEL)
            if voice_type == "cloned"
            else os.environ.get("OPENMONTAGE_TTS_CUSTOM_MODEL", DEFAULT_CUSTOM_MODEL)
        )
        payload: dict[str, Any] = {
            "signature_version": 1,
            "profile_id": profile_id,
            "voice_type": voice_type,
            "language": str(profile.get("language") or "zh"),
            "default_engine": str(profile.get("default_engine") or "qwen"),
            "model_id": model_id,
            "seed": int(os.environ.get("OPENMONTAGE_TTS_SEED", "42")),
            "preset_voice_id": profile.get("preset_voice_id"),
        }
        if voice_type == "cloned":
            sample = self.preferred_sample(profile)
            payload.update({
                "preferred_sample_id": str(sample.get("id") or ""),
                "sample_sha256": self._sha256_file(Path(sample["resolved_audio_path"])),
                "reference_text_sha256": hashlib.sha256(
                    str(sample["reference_text"]).encode("utf-8")
                ).hexdigest(),
            })
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def get(self, profile_id: str) -> dict[str, Any]:
        profile = next((item for item in self.list_profiles() if item["id"] == profile_id), None)
        if not profile:
            raise ProfileError(f"找不到本地配音音色：{profile_id}")
        if profile.get("voice_type") == "cloned":
            self.preferred_sample(profile)
        elif not profile.get("preset_voice_id"):
            raise ProfileError(f"预设音色缺少 preset_voice_id：{profile_id}")
        return profile

    def preferred_sample(self, profile: dict[str, Any]) -> dict[str, Any]:
        samples = [item for item in profile.get("samples", []) if isinstance(item, dict)]
        preferred_id = str(profile.get("preferred_sample_id") or "")
        if preferred_id:
            samples.sort(key=lambda item: str(item.get("id")) != preferred_id)
        for sample in samples:
            raw = str(sample.get("audio_path") or "").strip()
            if not raw:
                continue
            path = Path(raw)
            if not path.is_absolute():
                path = (self.data_dir / path).resolve()
                try:
                    path.relative_to(self.data_dir)
                except ValueError as exc:
                    raise ProfileError(f"克隆音色引用越过本地数据目录：{profile['id']}") from exc
            if path.is_file() and str(sample.get("reference_text") or "").strip():
                return {**sample, "resolved_audio_path": str(path)}
        raise ProfileError(f"克隆音色没有可用的参考音频和原文：{profile['id']}")


def split_text(text: str, max_chars: int) -> list[str]:
    """Split long narration at Chinese/English sentence boundaries."""
    normalized = re.sub(r"\s+", " ", text).strip()
    if not normalized:
        return []
    pieces = [item.strip() for item in re.split(r"(?<=[。！？!?；;])", normalized) if item.strip()]
    chunks: list[str] = []
    current = ""
    for piece in pieces or [normalized]:
        if len(piece) > max_chars:
            if current:
                chunks.append(current)
                current = ""
            chunks.extend(piece[index:index + max_chars] for index in range(0, len(piece), max_chars))
        elif not current or len(current) + len(piece) <= max_chars:
            current += piece
        else:
            chunks.append(current)
            current = piece
    if current:
        chunks.append(current)
    return chunks


class QwenTTSRuntime:
    """Lazy, process-local Qwen model and clone-prompt cache."""

    def __init__(self, data_dir: Path | None = None) -> None:
        self.data_dir = (data_dir or data_dir_from_env()).resolve()
        self.model_cache = model_cache_from_env(self.data_dir)
        self.store = ProfileStore(self.data_dir)
        self._models: dict[tuple[str, str, str, str], Any] = {}
        self._clone_prompts: dict[tuple[str, str], Any] = {}
        self._lock = threading.RLock()

    @property
    def loaded_models(self) -> list[str]:
        return sorted({key[1] for key in self._models})

    def _load_model(self, kind: str) -> Any:
        with self._lock:
            missing = [name for name, available in runtime_dependencies().items() if not available]
            if missing:
                raise RuntimeError("本地配音依赖未安装：" + ", ".join(missing))
            import torch
            from huggingface_hub import snapshot_download
            from qwen_tts import Qwen3TTSModel

            configured = os.environ.get("OPENMONTAGE_TTS_DEVICE", "auto").strip().lower()
            if configured in {"", "auto"}:
                device = "cuda:0" if torch.cuda.is_available() else "cpu"
            else:
                device = configured
            if device.startswith("cuda"):
                dtype = torch.bfloat16
            else:
                cpu_dtype = os.environ.get("OPENMONTAGE_TTS_CPU_DTYPE", DEFAULT_CPU_DTYPE).strip().lower()
                if cpu_dtype in {"bf16", "bfloat16"}:
                    dtype = torch.bfloat16
                elif cpu_dtype in {"fp32", "float32"}:
                    dtype = torch.float32
                else:
                    raise RuntimeError(
                        "OPENMONTAGE_TTS_CPU_DTYPE 仅支持 bfloat16 或 float32；"
                        "默认 bfloat16 以降低 1.7B 本地克隆模型的载入峰值"
                    )
            model_id = (
                os.environ.get("OPENMONTAGE_TTS_BASE_MODEL", DEFAULT_BASE_MODEL)
                if kind == "base"
                else os.environ.get("OPENMONTAGE_TTS_CUSTOM_MODEL", DEFAULT_CUSTOM_MODEL)
            )
            model_key = (kind, model_id, device, str(dtype))
            if model_key in self._models:
                return self._models[model_key]
            max_loaded = max(1, int(os.environ.get("OPENMONTAGE_TTS_MAX_LOADED_MODELS", "1")))
            if len(self._models) >= max_loaded:
                self._models.clear()
                self._clone_prompts.clear()
                import gc

                gc.collect()
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
            self.model_cache.mkdir(parents=True, exist_ok=True)
            offline = os.environ.get("OPENMONTAGE_TTS_OFFLINE", "").strip().lower() in {"1", "true", "yes"}
            try:
                model_path = snapshot_download(
                    repo_id=model_id,
                    cache_dir=str(self.model_cache),
                    local_files_only=True,
                )
            except Exception:
                if offline:
                    raise
                model_path = snapshot_download(repo_id=model_id, cache_dir=str(self.model_cache))
            kwargs: dict[str, Any] = {
                "device_map": device,
                "dtype": dtype,
            }
            if offline:
                kwargs["local_files_only"] = True
            # qwen-tts 0.1.1 does not forward cache_dir to its nested speech
            # tokenizer loader. Passing the resolved snapshot directory keeps
            # every nested component inside OpenMontage's private cache.
            model = Qwen3TTSModel.from_pretrained(model_path, **kwargs)
            self._models[model_key] = model
            return model

    @staticmethod
    def _language(value: str | None) -> str:
        raw = str(value or "zh").strip()
        return LANGUAGE_NAMES.get(raw.lower(), raw)

    def _clone_prompt(self, model: Any, profile: dict[str, Any], voice_signature: str) -> Any:
        sample = self.store.preferred_sample(profile)
        sample_path = Path(sample["resolved_audio_path"])
        # voice_signature already binds model id, sample bytes and reference
        # text, avoiding stale prompt reuse when size/mtime happen to match.
        key = (profile["id"], voice_signature)
        if key not in self._clone_prompts:
            self._clone_prompts[key] = model.create_voice_clone_prompt(
                ref_audio=str(sample_path),
                ref_text=str(sample["reference_text"]),
                x_vector_only_mode=False,
            )
        return self._clone_prompts[key]

    def generate(
        self,
        *,
        text: str,
        profile_id: str,
        output_path: Path,
        language: str = "zh",
        instruct: str = "",
        expected_voice_signature: str | None = None,
    ) -> dict[str, Any]:
        max_chars = max(80, int(os.environ.get("OPENMONTAGE_TTS_MAX_CHARS", "500")))
        chunks = split_text(text, max_chars)
        if not chunks:
            raise ValueError("配音文本不能为空")

        with self._lock:
            profile = self.store.get(profile_id)
            current_voice_signature = self.store.voice_signature(profile_id)
            if expected_voice_signature and current_voice_signature != expected_voice_signature:
                raise ProfileSignatureMismatch("冻结音色签名已变化；请重新选择音色并启动新的配音任务")
            import numpy as np
            import soundfile as sf
            import torch

            seed = int(os.environ.get("OPENMONTAGE_TTS_SEED", "42"))
            torch.manual_seed(seed)
            language_name = self._language(language or profile.get("language"))
            voice_type = profile.get("voice_type")
            if voice_type == "cloned":
                model = self._load_model("base")
                prompt = self._clone_prompt(model, profile, current_voice_signature)
            else:
                model = self._load_model("custom")
                prompt = None

            audio_parts: list[Any] = []
            sample_rate: int | None = None
            for chunk in chunks:
                if voice_type == "cloned":
                    wavs, current_rate = model.generate_voice_clone(
                        text=chunk,
                        language=language_name,
                        voice_clone_prompt=prompt,
                    )
                else:
                    kwargs: dict[str, Any] = {
                        "text": chunk,
                        "language": language_name,
                        "speaker": profile["preset_voice_id"],
                    }
                    if instruct.strip():
                        kwargs["instruct"] = instruct.strip()
                    wavs, current_rate = model.generate_custom_voice(**kwargs)
                if not wavs:
                    raise RuntimeError("Qwen3-TTS 未返回音频")
                if sample_rate is not None and int(current_rate) != sample_rate:
                    raise RuntimeError("分段生成返回了不一致的采样率")
                sample_rate = int(current_rate)
                audio_parts.append(np.asarray(wavs[0], dtype=np.float32).reshape(-1))

            assert sample_rate is not None
            silence_ms = max(0, int(os.environ.get("OPENMONTAGE_TTS_CHUNK_SILENCE_MS", "80")))
            silence = np.zeros(int(sample_rate * silence_ms / 1000), dtype=np.float32)
            combined: list[Any] = []
            for index, part in enumerate(audio_parts):
                if index and silence.size:
                    combined.append(silence)
                combined.append(part)
            waveform = np.concatenate(combined)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            sf.write(str(output_path), waveform, sample_rate, subtype="PCM_16")
            # Profiles and private reference WAVs are machine-local files that
            # can change outside this process. Recheck after synthesis and
            # discard the candidate if any voice input drifted while running.
            final_voice_signature = self.store.voice_signature(profile_id)
            if final_voice_signature != current_voice_signature:
                output_path.unlink(missing_ok=True)
                raise ProfileSignatureMismatch("配音生成期间音色签名发生变化，已丢弃未冻结的输出")
            return {
                "duration": round(float(len(waveform)) / sample_rate, 3),
                "sample_rate": sample_rate,
                "chunks": len(chunks),
                "profile_id": profile_id,
                "profile_name": profile.get("name"),
                "voice_type": voice_type,
                "model_kind": "base" if voice_type == "cloned" else "custom",
                "voice_signature": final_voice_signature,
            }


class SerialTTSJobs:
    """Persistent, process-wide serial queue for local TTS jobs."""

    def __init__(self, data_dir: Path | None = None, runtime: QwenTTSRuntime | None = None) -> None:
        self.data_dir = (data_dir or data_dir_from_env()).resolve()
        self.jobs_dir = self.data_dir / "generations"
        self._process_lock_file: Any | None = None
        self._acquire_process_lock()
        self.runtime = runtime or QwenTTSRuntime(self.data_dir)
        self._queue: queue.Queue[str] = queue.Queue()
        self._lock = threading.RLock()
        self._enqueued: set[str] = set()
        self._thread: threading.Thread | None = None
        self._recover_interrupted()

    def _acquire_process_lock(self) -> None:
        """Exclusively own one TTS data directory across processes."""

        self.data_dir.mkdir(parents=True, exist_ok=True)
        lock_path = self.data_dir / ".tts-service.lock"
        handle = lock_path.open("a+b")
        try:
            if lock_path.stat().st_size == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError) as exc:
            handle.close()
            raise RuntimeError("同一本地配音数据目录已被另一个服务占用；禁止并发启动 TTS") from exc
        self._process_lock_file = handle

    def close(self) -> None:
        handle = self._process_lock_file
        if handle is None:
            return
        self._process_lock_file = None
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def __del__(self) -> None:  # pragma: no cover - process exit remains the hard release.
        try:
            self.close()
        except Exception:
            pass

    def _job_file(self, job_id: str) -> Path:
        return self.jobs_dir / job_id / "job.json"

    def output_file(self, job_id: str) -> Path:
        return self.jobs_dir / job_id / "output.wav"

    def _load(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            path = self._job_file(job_id)
            job = _read_json(path, None)
            if not isinstance(job, dict):
                if path.is_file():
                    raise JobLedgerError("既有本地配音任务账本损坏或暂不可读，禁止覆盖并重复生成")
                raise KeyError(job_id)
            return job

    def _save(self, job: dict[str, Any]) -> None:
        with self._lock:
            _write_json_atomic(self._job_file(str(job["id"])), job)

    def _recover_interrupted(self) -> None:
        if not self.jobs_dir.is_dir():
            return
        for path in self.jobs_dir.glob("*/job.json"):
            job = _read_json(path, None)
            if isinstance(job, dict) and job.get("status") in {"queued", "generating"}:
                job.update({
                    "status": "failed",
                    "finished_at": utc_now(),
                    "error": "本地配音服务重启，未完成任务已安全中止；可重新提交。",
                })
                _write_json_atomic(path, job)

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._worker, name="openmontage-tts", daemon=True)
            self._thread.start()

    def submit(self, payload: dict[str, Any]) -> dict[str, Any]:
        text = str(payload.get("text") or "").strip()
        profile_id = str(payload.get("profile") or payload.get("profile_id") or "").strip()
        if not text:
            raise ValueError("配音文本不能为空")
        profile = self.runtime.store.get(profile_id)
        current_voice_signature = self.runtime.store.voice_signature(profile_id)
        requested_voice_signature = str(payload.get("voice_signature") or "").strip()
        if requested_voice_signature and requested_voice_signature != current_voice_signature:
            raise ProfileSignatureMismatch("提交的冻结音色签名与当前本地音色不一致")
        request_id = str(payload.get("request_id") or "").strip() or None
        if request_id and not REQUEST_ID_PATTERN.fullmatch(request_id):
            raise ValueError("request_id 只能使用安全 ASCII 字符且长度不超过 200")
        immutable = {
            "text": text,
            "profile_id": profile_id,
            "language": str(payload.get("language") or "zh"),
            "instruct": str(payload.get("instruct") or ""),
            "engine": str(payload.get("engine") or profile.get("default_engine") or "qwen"),
            "model_size": str(payload.get("model_size") or ""),
            "personality": bool(payload.get("personality", False)),
            "normalize": bool(payload.get("normalize", True)),
            "voice_signature": current_voice_signature,
        }
        request_fingerprint = hashlib.sha256(
            json.dumps(immutable, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        job_id = str(uuid5(REQUEST_NAMESPACE, request_id)) if request_id else str(uuid4())
        with self._lock:
            if request_id:
                try:
                    existing = self._load(job_id)
                except KeyError:
                    existing = None
                if existing is not None:
                    if existing.get("request_fingerprint") != request_fingerprint:
                        raise IdempotencyConflict("同一 request_id 已绑定不同的配音输入，禁止覆盖")
                    return dict(existing)
            job = {
                "id": job_id,
                "status": "queued",
                "created_at": utc_now(),
                "request_id": request_id,
                "request_fingerprint": request_fingerprint,
                "voice_signature": current_voice_signature,
                "text": text,
                "profile_id": profile_id,
                "language": immutable["language"],
                "instruct": immutable["instruct"],
                "engine": immutable["engine"],
                "model_size": immutable["model_size"],
                "personality": immutable["personality"],
                "normalize": immutable["normalize"],
            }
            self._save(job)
            self.start()
            if job_id not in self._enqueued:
                self._enqueued.add(job_id)
                self._queue.put(job_id)
            return dict(job)

    def status(self, job_id: str) -> dict[str, Any]:
        return self._load(job_id)

    def _worker(self) -> None:
        while True:
            job_id = self._queue.get()
            try:
                job = self._load(job_id)
                job.update({"status": "generating", "started_at": utc_now()})
                self._save(job)
                result = self.runtime.generate(
                    text=job["text"],
                    profile_id=job["profile_id"],
                    output_path=self.output_file(job_id),
                    language=job["language"],
                    instruct=job["instruct"],
                    expected_voice_signature=job.get("voice_signature"),
                )
                job.update(result)
                job.update({"status": "completed", "finished_at": utc_now()})
            except Exception as exc:  # noqa: BLE001 - persisted user-visible job error.
                try:
                    job = self._load(job_id)
                except KeyError:
                    job = {"id": job_id}
                job.update({"status": "failed", "finished_at": utc_now(), "error": str(exc)[:2000]})
            finally:
                try:
                    self._save(job)
                except Exception:  # noqa: BLE001 - keep the sole queue worker alive.
                    LOGGER.exception("无法持久化本地配音任务终态：%s", job_id)
                finally:
                    with self._lock:
                        self._enqueued.discard(job_id)
                    self._queue.task_done()
