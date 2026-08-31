"""Deterministic sentence-level narration planning and durable WAV ledger.

This module deliberately does not know about scenes, visual generation, or
video rendering.  It turns approved script sections into stable line units,
validates each locally generated PCM WAV, and persists enough evidence for a
parent job to resume without regenerating completed lines.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
import time
import wave
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Iterable


LEDGER_VERSION = "1.0"
PLANNER_VERSION = "narration-lines-v1"
DEFAULT_MAX_LINE_CHARS = 72
LEDGER_PATH = Path("artifacts/narration_lines.json")
LINE_AUDIO_DIRECTORY = Path("assets/audio/narration-lines")


class NarrationLineError(ValueError):
    """A safe, user-facing sentence narration failure."""


class NarrationOutputValidationError(NarrationLineError):
    """A newly synthesized output is invalid and may be regenerated safely."""


class NarrationEvidenceDriftError(NarrationLineError):
    """Persisted completed evidence changed and must not be blindly replaced."""


class NarrationTTSSubmitUncertainError(NarrationLineError):
    """The idempotent POST may have succeeded; retry only with the same request ID."""


class NarrationTTSTerminalError(NarrationLineError):
    """The persisted TTS task reached an explicit terminal failure."""


class NarrationVoiceDriftError(NarrationEvidenceDriftError):
    """The service voice signature differs from the frozen parent voice."""


class StaleNarrationWorker(NarrationLineError):
    """Raised when an obsolete worker attempts to promote its output."""


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _json_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tool_payload(result: Any, operation: str) -> dict[str, Any]:
    success = getattr(result, "success", None)
    if success is False:
        raise NarrationLineError(str(getattr(result, "error", None) or f"TTS {operation} 失败"))
    data = getattr(result, "data", None)
    if isinstance(data, dict):
        return deepcopy(data)
    if isinstance(result, dict):
        if result.get("success") is False:
            raise NarrationLineError(str(result.get("error") or f"TTS {operation} 失败"))
        nested = result.get("data")
        return deepcopy(nested if isinstance(nested, dict) else result)
    raise NarrationLineError(f"TTS {operation} 返回格式无效")


def deterministic_tts_request_id(
    project_dir: Path,
    parent_job_id: str,
    line_id: str,
    input_fingerprint: str,
    attempt: int,
) -> str:
    """Build a path-free idempotency key from durable project/job identity."""
    project_payload: dict[str, Any] = {}
    try:
        raw = json.loads((project_dir / "project.json").read_text(encoding="utf-8"))
        project_payload = raw if isinstance(raw, dict) else {}
    except (OSError, json.JSONDecodeError):
        project_payload = {}
    project_identity = str(
        project_payload.get("project_id")
        or project_payload.get("id")
        or project_payload.get("slug")
        or project_dir.name
    )
    digest = _json_hash(
        {
            "project_identity": project_identity,
            "parent_job_id": parent_job_id,
            "line_id": line_id,
            "input_fingerprint": input_fingerprint,
            "attempt": int(attempt),
        }
    )
    return f"rpp-tts-{digest[:40]}"


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def normalize_line_text(value: object) -> str:
    """Return the canonical text used by line IDs and TTS fingerprints."""
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return re.sub(
        r"(?<=[\u4e00-\u9fff，。！？、；：])\s+|\s+(?=[\u4e00-\u9fff，。！？、；：])",
        "",
        text,
    )


def _safe_ascii_break(text: str, cut: int) -> int:
    group = next(
        (
            match
            for match in re.finditer(
                r"[A-Za-z0-9][A-Za-z0-9._+/-]*(?: [A-Za-z0-9][A-Za-z0-9._+/-]*)+",
                text,
            )
            if match.start() < cut < match.end()
        ),
        None,
    )
    if group is None:
        return cut
    # Never exceed the caller's hard limit.  If a long ASCII token starts at
    # the beginning there is no safe word boundary, so a deterministic hard
    # split is the only contract-preserving choice.
    return group.start() if 0 < group.start() <= cut else cut


def split_section_text(text: object, *, max_chars: int = DEFAULT_MAX_LINE_CHARS) -> list[str]:
    """Split one section at stable sentence punctuation, then minor clauses.

    Major punctuation remains attached to the preceding unit.  A sentence
    longer than ``max_chars`` first uses the latest comma-like boundary in the
    window and falls back to a deterministic hard split.
    """
    if max_chars < 12:
        raise ValueError("max_chars must be at least 12")
    normalized = normalize_line_text(text)
    if not normalized:
        return []
    sentences = re.findall(r".+?[。！？!?；;]+|.+$", normalized)
    lines: list[str] = []
    minor = set("，、,：:")
    for sentence in sentences:
        remainder = sentence
        while len(remainder) > max_chars:
            window = remainder[:max_chars]
            boundaries = [index + 1 for index, char in enumerate(window) if char in minor]
            useful = [index for index in boundaries if index >= max(8, max_chars // 3)]
            cut = min(max_chars, max(useful) if useful else _safe_ascii_break(remainder, max_chars))
            piece = remainder[:cut]
            if piece:
                lines.append(piece)
            remainder = remainder[cut:]
        if remainder:
            lines.append(remainder)
    return [line for line in lines if line]


def stable_line_id(section_id: object, ordinal: int, text: object) -> str:
    normalized = normalize_line_text(text)
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]
    return f"{str(section_id)}:L{int(ordinal):03d}:{digest}"


def voice_fingerprint(voice: dict[str, Any]) -> str:
    return _json_hash(
        {
            "profile_id": voice.get("profile_id") or voice.get("id"),
            "profile_name": voice.get("profile_name") or voice.get("name"),
            "engine": voice.get("engine") or voice.get("default_engine"),
            "voice_signature": voice.get("voice_signature"),
        }
    )


def build_line_plan(
    sections: Iterable[dict[str, Any]],
    voice: dict[str, Any],
    *,
    max_chars: int = DEFAULT_MAX_LINE_CHARS,
) -> dict[str, Any]:
    """Build a stable, ordered line plan from canonical script sections."""
    frozen_voice_fingerprint = voice_fingerprint(voice)
    lines: list[dict[str, Any]] = []
    section_rows: list[dict[str, Any]] = []
    project_ordinal = 0
    seen_section_ids: set[str] = set()
    for section_order, section in enumerate(sections, 1):
        if not isinstance(section, dict):
            raise NarrationLineError(
                f"正式脚本第 {section_order} 个 section 不是对象，禁止静默丢弃内容"
            )
        section_id = str(section.get("id") or f"section-{section_order:03d}")
        if section_id in seen_section_ids:
            raise NarrationLineError(f"正式脚本包含重复的 section_id：{section_id}，无法建立唯一逐句账本")
        seen_section_ids.add(section_id)
        pieces = split_section_text(section.get("text"), max_chars=max_chars)
        if not pieces:
            continue
        section_line_ids: list[str] = []
        for line_ordinal, text in enumerate(pieces, 1):
            project_ordinal += 1
            line_id = stable_line_id(section_id, line_ordinal, text)
            fingerprint = _json_hash(
                {
                    "planner_version": PLANNER_VERSION,
                    "line_id": line_id,
                    "normalized_text": normalize_line_text(text),
                    "voice_fingerprint": frozen_voice_fingerprint,
                    "language": "zh",
                    "format": "wav_pcm_s16le",
                }
            )
            section_line_ids.append(line_id)
            lines.append(
                {
                    "line_id": line_id,
                    "section_id": section_id,
                    "section_order": section_order,
                    "line_ordinal": line_ordinal,
                    "project_ordinal": project_ordinal,
                    "text": text,
                    "normalized_text": normalize_line_text(text),
                    "text_sha256": hashlib.sha256(normalize_line_text(text).encode("utf-8")).hexdigest(),
                    "voice_fingerprint": frozen_voice_fingerprint,
                    "input_fingerprint": fingerprint,
                    "status": "planned",
                    "error": "",
                }
            )
        section_rows.append(
            {
                "section_id": section_id,
                "section_order": section_order,
                "line_ids": section_line_ids,
            }
        )
    return {
        "version": LEDGER_VERSION,
        "planner_version": PLANNER_VERSION,
        "voice": deepcopy(voice),
        "voice_fingerprint": frozen_voice_fingerprint,
        "line_count": len(lines),
        "sections": section_rows,
        "lines": lines,
        "plan_fingerprint": _json_hash(
            {
                "planner_version": PLANNER_VERSION,
                "voice_fingerprint": frozen_voice_fingerprint,
                "lines": [line["input_fingerprint"] for line in lines],
            }
        ),
    }


def inspect_pcm_wav(path: Path) -> dict[str, Any]:
    """Return measured PCM16 WAV evidence, raising on an unusable output."""
    if not path.is_file() or path.stat().st_size <= 44:
        raise NarrationLineError("逐句配音文件不存在或为空")
    try:
        with wave.open(str(path), "rb") as handle:
            channels = int(handle.getnchannels())
            sample_width = int(handle.getsampwidth())
            sample_rate = int(handle.getframerate())
            frame_count = int(handle.getnframes())
            compression = handle.getcomptype()
            expected_bytes = frame_count * channels * sample_width
            audio_bytes = handle.readframes(frame_count)
            trailing = handle.readframes(1)
    except (OSError, EOFError, wave.Error) as exc:
        raise NarrationLineError("逐句配音不是有效的 WAV 文件") from exc
    if compression != "NONE" or sample_width != 2:
        raise NarrationLineError("逐句配音必须是 PCM16 WAV")
    if channels <= 0 or sample_rate <= 0 or frame_count <= 0:
        raise NarrationLineError("逐句配音缺少有效的声道、采样率或音频帧")
    if len(audio_bytes) != expected_bytes or trailing:
        raise NarrationLineError("逐句配音 WAV 数据被截断或声明帧数不一致")
    duration = frame_count / sample_rate
    if duration <= 0:
        raise NarrationLineError("逐句配音实测时长无效")
    return {
        "format": "wav",
        "codec": "pcm_s16le",
        "sample_rate": sample_rate,
        "channels": channels,
        "sample_width_bytes": sample_width,
        "frame_count": frame_count,
        "duration_seconds": round(duration, 6),
        "sha256": _sha256(path),
        "size_bytes": path.stat().st_size,
    }


def load_ledger(project_dir: Path) -> dict[str, Any]:
    path = project_dir / LEDGER_PATH
    if not path.is_file():
        return {
            "version": LEDGER_VERSION,
            "planner_version": PLANNER_VERSION,
            "plan_fingerprint": None,
            "voice": None,
            "lines": [],
            "history": [],
        }
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise NarrationLineError("逐句配音账本损坏，请从安全点重新生成") from exc
    if not isinstance(payload, dict):
        raise NarrationLineError("逐句配音账本格式无效")
    payload.setdefault("lines", [])
    payload.setdefault("history", [])
    return payload


def _safe_line_root(project_dir: Path) -> Path:
    project_root = project_dir.resolve()
    raw_root = project_dir / LINE_AUDIO_DIRECTORY
    if raw_root.exists() or raw_root.is_symlink():
        attributes = int(getattr(os.lstat(raw_root), "st_file_attributes", 0) or 0)
        if raw_root.is_symlink() or attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
            raise NarrationLineError("逐句音频目录不得是 symlink、junction 或其他 reparse-point")
    resolved_root = raw_root.resolve()
    try:
        resolved_root.relative_to(project_root)
    except ValueError as exc:
        raise NarrationLineError("逐句音频目录解析后逃逸了当前项目") from exc
    return resolved_root


def _safe_line_output(project_dir: Path, raw_path: object) -> tuple[Path, str]:
    relative = str(raw_path or "").replace("\\", "/").strip()
    candidate = Path(relative)
    if not relative or candidate.is_absolute() or ".." in candidate.parts:
        raise NarrationLineError("逐句音频 output_path 必须是项目内的安全相对路径")
    allowed_root = _safe_line_root(project_dir)
    resolved = (project_dir / candidate).resolve()
    try:
        canonical = resolved.relative_to(allowed_root).as_posix()
    except ValueError as exc:
        raise NarrationLineError("逐句音频 output_path 逃逸了 assets/audio/narration-lines") from exc
    return resolved, (LINE_AUDIO_DIRECTORY / canonical).as_posix()


def _record_is_reusable(
    project_dir: Path,
    record: dict[str, Any],
    fingerprint: str,
) -> dict[str, Any] | None:
    if record.get("status") != "completed" or record.get("input_fingerprint") != fingerprint:
        return None
    try:
        path, relative = _safe_line_output(project_dir, record.get("output_path"))
        media = inspect_pcm_wav(path)
    except NarrationLineError as exc:
        raise NarrationEvidenceDriftError(
            f"既有 completed 逐句音频或安全路径证据已漂移：{exc}；请启动新任务核验"
        ) from exc
    if media["sha256"] != record.get("sha256"):
        raise NarrationEvidenceDriftError("既有 completed 逐句音频哈希已漂移；请启动新任务核验")
    return {"output_path": relative, **media}


def materialize_line_audio(
    project_dir: Path,
    plan: dict[str, Any],
    synthesize: Callable[[dict[str, Any], Path, dict[str, Any]], dict[str, Any] | None] | None = None,
    *,
    tts_client: Any | None = None,
    is_current: Callable[[], bool] | None = None,
    commit: Callable[[Callable[[], None]], None] | None = None,
    parent_job_id: str | None = None,
    worker_token: str | None = None,
    allow_terminal_retry: bool = False,
    poll_interval_seconds: float = 0.5,
    poll_timeout_seconds: float = 1800.0,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Generate only missing/invalid lines and atomically promote each WAV."""
    parent_scoped = parent_job_id is not None or worker_token is not None
    if parent_scoped and (not parent_job_id or not worker_token):
        raise NarrationLineError("父任务逐句生产必须同时提供 parent_job_id 和 worker_token")
    if parent_scoped and commit is None:
        raise NarrationLineError("父任务逐句生产必须提供项目锁内的 commit/CAS 租约校验回调")
    if synthesize is None and tts_client is None:
        raise NarrationLineError("逐句配音必须提供可注入 synthesize 或 submit/query/download TTS dependency")
    if tts_client is not None and not all(callable(getattr(tts_client, name, None)) for name in ("submit", "query", "download")):
        raise NarrationLineError("TTS dependency 缺少 submit/query/download 可恢复合同")
    output_root = _safe_line_root(project_dir)

    def commit_action(action: Callable[[], None]) -> None:
        if commit is not None:
            commit(action)
        else:
            if is_current is not None and not is_current():
                raise StaleNarrationWorker("旧逐句配音 worker 已失效，拒绝提交账本")
            action()

    def validate_lease() -> None:
        if commit is not None:
            commit(lambda: None)
        elif is_current is not None and not is_current():
            raise StaleNarrationWorker("旧逐句配音 worker 已失效，拒绝继续 TTS 操作")

    ledger = load_ledger(project_dir)
    prior_records = {
        (str(item.get("line_id")), str(item.get("input_fingerprint"))): item
        for item in [*(ledger.get("history") or []), *(ledger.get("lines") or [])]
        if isinstance(item, dict) and item.get("line_id") and item.get("input_fingerprint")
    }
    current_keys = {
        (str(item.get("line_id")), str(item.get("input_fingerprint")))
        for item in plan.get("lines") or []
    }
    history = [
        deepcopy(item)
        for item in [*(ledger.get("history") or []), *(ledger.get("lines") or [])]
        if isinstance(item, dict)
        and (str(item.get("line_id")), str(item.get("input_fingerprint"))) not in current_keys
    ]
    deduped_history = {
        (str(item.get("line_id")), str(item.get("input_fingerprint"))): item
        for item in history
        if item.get("line_id") and item.get("input_fingerprint")
    }
    records: list[dict[str, Any]] = []
    for planned in plan.get("lines") or []:
        fingerprint = str(planned.get("input_fingerprint") or "")
        previous = prior_records.get((str(planned.get("line_id") or ""), fingerprint)) or {}
        reusable_media = _record_is_reusable(project_dir, previous, fingerprint)
        if reusable_media:
            restored = deepcopy(previous)
            restored.update({key: deepcopy(value) for key, value in planned.items()})
            restored.update(reusable_media)
            restored.update({"status": "completed", "reused": True, "error": ""})
            records.append(restored)
        elif previous and previous.get("status") in {"queued", "generating", "failed"}:
            restored = deepcopy(previous)
            restored.update({key: deepcopy(value) for key, value in planned.items()})
            restored["error"] = str(previous.get("error") or "")
            records.append(restored)
        else:
            records.append(deepcopy(planned))
    ledger = {
        "version": LEDGER_VERSION,
        "planner_version": PLANNER_VERSION,
        "plan_fingerprint": plan.get("plan_fingerprint"),
        "voice": deepcopy(plan.get("voice") or {}),
        "voice_fingerprint": plan.get("voice_fingerprint"),
        "sections": deepcopy(plan.get("sections") or []),
        "lines": records,
        "history": list(deduped_history.values()),
        "parent_job_id": parent_job_id,
        "worker_token": worker_token,
        "status": "planned",
        "completed_count": sum(1 for item in records if item.get("status") == "completed"),
        "failed_count": 0,
        "updated_at": _now(),
    }
    ledger_path = project_dir / LEDGER_PATH
    # Persist the complete line plan before any blocking synthesis begins, so
    # a crash leaves every unstarted line visibly planned.
    commit_action(lambda: _atomic_write(ledger_path, ledger))

    for index, planned in enumerate(plan.get("lines") or []):
        if is_current is not None and not is_current():
            raise StaleNarrationWorker("旧逐句配音 worker 已失效，未提升本次输出")
        line_id = str(planned["line_id"])
        fingerprint = str(planned["input_fingerprint"])
        previous = prior_records.get((line_id, fingerprint)) or {}
        reusable_media = _record_is_reusable(project_dir, previous, fingerprint)
        if reusable_media:
            record = deepcopy(previous)
            record.update({key: deepcopy(value) for key, value in planned.items()})
            record.update(reusable_media)
            record.update({"status": "completed", "reused": True, "error": ""})
            records[index] = record
            ledger["completed_count"] = sum(1 for item in records if item.get("status") == "completed")
            ledger["updated_at"] = _now()
            commit_action(lambda: _atomic_write(ledger_path, ledger))
            if on_progress:
                on_progress(deepcopy(record))
            continue

        record = deepcopy(records[index] if isinstance(records[index], dict) else planned)
        if tts_client is not None:
            terminal_statuses = {"failed", "cancelled", "canceled", "error"}
            previous_tts_status = str(previous.get("tts_status") or "").lower()
            previous_attempt = max(1, int(previous.get("attempts") or 1))
            if previous_tts_status in terminal_statuses:
                if not allow_terminal_retry:
                    raise NarrationTTSTerminalError(
                        "逐句 TTS 已明确终态失败；必须经父任务人工安全恢复后才能建立新 attempt"
                    )
                attempt = previous_attempt + 1
                request_id = deterministic_tts_request_id(
                    project_dir, str(parent_job_id or "standalone"), line_id, fingerprint, attempt
                )
                task_id = None
                api_mode = "speak"
            else:
                attempt = previous_attempt
                request_id = str(previous.get("tts_request_id") or "") or deterministic_tts_request_id(
                    project_dir, str(parent_job_id or "standalone"), line_id, fingerprint, attempt
                )
                task_id = str(previous.get("tts_task_id") or "") or None
                api_mode = str(previous.get("api_mode") or "speak")
            record.update(
                {
                    "status": "generating" if task_id else "queued",
                    "attempts": attempt,
                    "tts_request_id": request_id,
                    "tts_task_id": task_id,
                    "api_mode": api_mode,
                    "tts_status": str(previous.get("tts_status") or ("submitted" if task_id else "queued")),
                    "voice_signature": previous.get("voice_signature"),
                    "started_at": previous.get("started_at") or _now(),
                    "finished_at": None,
                    "output_path": None,
                    "sha256": None,
                    "error": "",
                    "reused": False,
                }
            )
        else:
            record.update(
                {
                    "status": "generating",
                    "attempts": int(previous.get("attempts") or 0) + 1,
                    "started_at": _now(),
                    "finished_at": None,
                    "output_path": None,
                    "sha256": None,
                    "error": "",
                    "reused": False,
                }
            )
        records[index] = record
        ledger["updated_at"] = _now()
        # For the recoverable API this is the mandatory pre-POST checkpoint:
        # a crash after this replace can only replay the exact same request ID.
        commit_action(lambda: _atomic_write(ledger_path, ledger))
        output_dir = output_root
        output_dir.mkdir(parents=True, exist_ok=True)
        safe_stem = re.sub(r"[^A-Za-z0-9._-]+", "-", line_id).strip("-")[:96]
        output_stem = f"{safe_stem}-{fingerprint[:12]}"
        fd, temporary_name = tempfile.mkstemp(prefix=f".{output_stem}.", suffix=".wav", dir=output_dir)
        os.close(fd)
        temporary = Path(temporary_name)
        try:
            temporary.unlink(missing_ok=True)
            if tts_client is not None:
                voice = deepcopy(plan.get("voice") or {})
                expected_voice_signature = str(voice.get("voice_signature") or "")
                if not expected_voice_signature:
                    raise NarrationVoiceDriftError("父任务冻结音色缺少 voice_signature，禁止提交 TTS")

                def checkpoint_tts(**changes: Any) -> None:
                    def apply() -> None:
                        record.update(changes)
                        ledger["updated_at"] = _now()
                        _atomic_write(ledger_path, ledger)
                    commit_action(apply)
                    if on_progress:
                        on_progress(deepcopy(record))

                def validate_voice(payload: dict[str, Any], *, required: bool = False) -> None:
                    actual = str(payload.get("voice_signature") or "")
                    if required and not actual:
                        raise NarrationVoiceDriftError("TTS submit 未返回 voice_signature，无法核对冻结音色")
                    if actual and actual != expected_voice_signature:
                        raise NarrationVoiceDriftError("TTS 返回的 voice_signature 与父任务冻结音色不一致")

                task_id = str(record.get("tts_task_id") or "")
                api_mode = str(record.get("api_mode") or "speak")
                resumed_existing_task = bool(task_id)
                if not task_id:
                    validate_lease()
                    inputs = {
                        "text": planned["text"],
                        "profile_id": voice.get("profile_id"),
                        "language": "zh",
                        "engine": voice.get("engine"),
                        "voice_signature": expected_voice_signature,
                    }
                    try:
                        submitted = _tool_payload(
                            tts_client.submit(inputs, request_id=str(record["tts_request_id"])),
                            "submit",
                        )
                        validate_lease()
                    except StaleNarrationWorker:
                        raise
                    except Exception as exc:
                        if type(exc).__name__ == "TTSRequestConflict":
                            raise NarrationVoiceDriftError(
                                "当前本地音色签名与父任务冻结 voice_signature 不一致"
                            ) from exc
                        checkpoint_tts(
                            status="generating",
                            tts_status="submit_unknown",
                            error=str(exc or "TTS submit 响应丢失")[:1200],
                        )
                        raise NarrationTTSSubmitUncertainError(
                            "TTS submit 响应不明；已保留相同 request_id，恢复时仅以同键重放"
                        ) from exc
                    task_id = str(submitted.get("generation_id") or submitted.get("id") or "")
                    if not task_id:
                        checkpoint_tts(
                            status="generating",
                            tts_status="submit_unknown",
                            error="TTS submit 未返回 generation_id",
                        )
                        raise NarrationTTSSubmitUncertainError(
                            "TTS submit 未返回 generation_id；已保留 request_id 供幂等恢复"
                        )
                    if str(submitted.get("request_id") or "") != str(record["tts_request_id"]):
                        raise NarrationEvidenceDriftError("TTS submit 未回显冻结的 request_id，拒绝继续")
                    api_mode = str(submitted.get("api_mode") or "speak")
                    checkpoint_tts(
                        status="generating",
                        tts_task_id=task_id,
                        api_mode=api_mode,
                        tts_status=str(submitted.get("status") or "submitted"),
                        voice_signature=submitted.get("voice_signature"),
                        error="",
                    )
                    validate_voice(submitted, required=True)

                started_polling = time.monotonic()
                status = str(record.get("tts_status") or "submitted").lower()
                terminal_failed = {"failed", "cancelled", "canceled", "error"}
                must_query_once = resumed_existing_task
                while status != "completed" or must_query_once:
                    if status in terminal_failed:
                        checkpoint_tts(status="failed", tts_status=status, finished_at=_now())
                        raise NarrationTTSTerminalError(f"逐句 TTS 任务已终态失败：{status}")
                    if time.monotonic() - started_polling > max(0.1, poll_timeout_seconds):
                        raise NarrationLineError("逐句 TTS 等待超时；已保留任务 ID，恢复时只查询")
                    validate_lease()
                    queried = _tool_payload(tts_client.query(task_id, api_mode=api_mode), "query")
                    validate_lease()
                    must_query_once = False
                    returned_task_id = str(queried.get("generation_id") or queried.get("id") or task_id)
                    if returned_task_id != task_id:
                        raise NarrationEvidenceDriftError("TTS query 返回了不同 generation_id，拒绝继续")
                    returned_request_id = str(queried.get("request_id") or "")
                    if returned_request_id and returned_request_id != str(record.get("tts_request_id") or ""):
                        raise NarrationEvidenceDriftError("TTS query 返回了不同 request_id，拒绝继续")
                    validate_voice(queried)
                    next_status = str(queried.get("status") or status or "running").lower()
                    if next_status != status or queried.get("voice_signature"):
                        checkpoint_tts(
                            status="generating" if next_status not in terminal_failed else "failed",
                            tts_status=next_status,
                            voice_signature=queried.get("voice_signature") or record.get("voice_signature"),
                            error=str(queried.get("error") or "")[:1200],
                        )
                    status = next_status
                    if status != "completed" and poll_interval_seconds > 0:
                        time.sleep(poll_interval_seconds)

                validate_lease()
                downloaded = _tool_payload(
                    tts_client.download(task_id, temporary, api_mode=api_mode),
                    "download",
                )
                validate_lease()
                validate_voice(downloaded)
                result = {
                    **downloaded,
                    "generation_id": task_id,
                    "api_mode": api_mode,
                    "request_id": record.get("tts_request_id"),
                }
            else:
                validate_lease()
                result = synthesize(deepcopy(planned), temporary, deepcopy(plan.get("voice") or {})) or {}
                validate_lease()
            try:
                media = inspect_pcm_wav(temporary)
            except NarrationLineError as exc:
                raise NarrationOutputValidationError(
                    f"本次新生成的逐句配音校验失败，可从当前句安全重试：{exc}"
                ) from exc
            returned_sha256 = str(result.get("sha256") or "")
            if returned_sha256 and returned_sha256 != media["sha256"]:
                raise NarrationOutputValidationError("TTS download 返回哈希与重新探测的 WAV 不一致")
            # The content hash makes promoted files immutable.  Even if an old
            # process returns during a narrow liveness-check race, it can only
            # create a different versioned path; it cannot overwrite the file
            # selected by a newer parent ledger.
            output = output_dir / f"{output_stem}-{media['sha256'][:12]}.wav"
            _safe_line_output(project_dir, output.relative_to(project_dir).as_posix())

            def promote_and_persist() -> None:
                if output.is_file():
                    existing = inspect_pcm_wav(output)
                    if existing["sha256"] != media["sha256"]:
                        raise NarrationEvidenceDriftError("逐句音频内容寻址路径发生哈希冲突")
                    temporary.unlink(missing_ok=True)
                else:
                    os.replace(temporary, output)
                record.update(
                    {
                        "status": "completed",
                        "finished_at": _now(),
                        "output_path": output.relative_to(project_dir).as_posix(),
                        "tts_task_id": result.get("task_id") or result.get("generation_id") or result.get("id"),
                        "api_mode": result.get("api_mode") or record.get("api_mode"),
                        "tts_status": "completed" if tts_client is not None else record.get("tts_status"),
                        "voice_signature": (
                            result.get("voice_signature")
                            or record.get("voice_signature")
                            or (plan.get("voice") or {}).get("voice_signature")
                        ),
                        **media,
                        "error": "",
                    }
                )
                ledger["completed_count"] = sum(1 for item in records if item.get("status") == "completed")
                ledger["failed_count"] = sum(1 for item in records if item.get("status") == "failed")
                ledger["updated_at"] = _now()
                _atomic_write(ledger_path, ledger)

            commit_action(promote_and_persist)
            if on_progress:
                on_progress(deepcopy(record))
        except StaleNarrationWorker:
            temporary.unlink(missing_ok=True)
            raise
        except Exception as exc:
            temporary.unlink(missing_ok=True)
            def fail_and_persist() -> None:
                uncertain_submit = isinstance(exc, NarrationTTSSubmitUncertainError)
                record.update(
                    {
                        "status": "generating" if uncertain_submit else "failed",
                        "finished_at": None if uncertain_submit else _now(),
                        "error": str(exc or "逐句配音失败")[:1200],
                    }
                )
                ledger["completed_count"] = sum(1 for item in records if item.get("status") == "completed")
                ledger["failed_count"] = sum(1 for item in records if item.get("status") == "failed")
                ledger["status"] = "failed"
                ledger["updated_at"] = _now()
                _atomic_write(ledger_path, ledger)

            commit_action(fail_and_persist)
            if on_progress:
                on_progress(deepcopy(record))
            if isinstance(exc, NarrationLineError):
                raise
            raise NarrationLineError(record["error"]) from exc

    ledger["completed_count"] = sum(1 for item in records if item.get("status") == "completed")
    ledger["failed_count"] = sum(1 for item in records if item.get("status") == "failed")
    ledger["status"] = "completed" if records and ledger["completed_count"] == len(records) else "failed"
    ledger["updated_at"] = _now()
    commit_action(lambda: _atomic_write(ledger_path, ledger))
    return ledger
