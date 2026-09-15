"""Reusable "what does this material look like" document (bypass artifact).

Four modules used to measure the same media independently, each with its own
cache policy: pause evidence (keyed by detector identity), VAD (keyed by
candidate range), the 12-window motion sample (keyed by index signature) and
the overview activity sampler.  There was no single authoritative answer.

This module composes them into one versioned, cacheable artifact:

    <output_root>/material-evidence/<signature[:20]>/
      evidence.json     version, per-section status, signature, degradations
      envelope.npz      audio envelope  (~1MB)
      motion.npz        motion timeline (~0.04MB)

Two hard rules
--------------
1. **It is a bypass artifact.**  It never touches
   ``material-interaction-index.json``; that file's content hash must be the
   same before and after.  The index signature is the key of the *paid* vision
   cache, so changing it would re-bill 34 windows.
2. **Sections are independent.**  A missing VAD runtime or a missing transcript
   never voids the document; it becomes ``status="partial"`` with a Chinese
   reason in ``degradations``.

The signature is derived from **identities only** — never from content or from
thresholds.  Re-tuning a silence threshold is a pure function of the stored
envelope and must not invalidate the document.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Iterable

from backlot.material_audio_envelope import (
    MaterialAudioEnvelopeError,
    MaterialAudioEnvelopeUnavailable,
    decode_envelope,
    envelope_identity,
    load_envelope,
    save_envelope,
    silence_intervals,
)
from backlot.material_motion_timeline import (
    MaterialMotionTimelineError,
    MaterialMotionTimelineUnavailable,
    build_motion_timeline,
    load_motion_timeline,
    low_motion_seconds,
    motion_identity,
    save_motion_timeline,
)
from backlot.media_index import media_content_fingerprint

VERSION = "material-evidence-v1"
DIRECTORY = "material-evidence"
SECTIONS = ("audio_envelope", "speech_activity", "motion", "transcript")
STATUSES = ("available", "partial", "unavailable")
MAX_BYTES = 5 * 1024 * 1024


class MaterialEvidenceError(ValueError):
    pass


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _identity_signature(value: Any) -> Any:
    """Accept a full identity dict, a bare signature string, or ``None``."""
    if isinstance(value, dict):
        return value.get("signature") or _digest(value)
    if isinstance(value, str):
        return value
    return None


def evidence_signature(source_fingerprint: str, *, envelope_identity: Any = None,
                       speech_identity: Any = None, motion_identity: Any = None,
                       transcript_identity: Any = None) -> str:
    """Identity-only signature: thresholds and content never enter it."""
    payload = {
        "version": VERSION,
        "source_fingerprint": str(source_fingerprint or ""),
        "envelope_identity": _identity_signature(envelope_identity),
        "speech_identity": _identity_signature(speech_identity),
        "motion_identity": _identity_signature(motion_identity),
        "transcript_identity": _identity_signature(transcript_identity),
    }
    return _digest(payload)


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with open(temporary, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2)
    for attempt in range(12):
        try:
            os.replace(temporary, path)
            break
        except PermissionError:
            if attempt == 11:
                temporary.unlink(missing_ok=True)
                raise
            time.sleep(.01 * (attempt + 1))


def _not_available(reason: str) -> dict[str, Any]:
    return {"status": "unavailable", "reason": reason}


def _strictly_increasing(rows: Iterable[dict[str, Any]]) -> list[dict[str, float]]:
    result: list[dict[str, float]] = []
    for row in rows or []:
        try:
            start, end = float(row["start"]), float(row["end"])
        except (KeyError, IndexError, TypeError, ValueError):
            continue
        if end > start and (not result or start >= result[-1]["end"] - 1e-6):
            result.append({"start": round(start, 6), "end": round(end, 6)})
    return result


def _reusable_envelope(directory: Path, identity: str, refresh: bool) -> dict[str, Any] | None:
    """Reuse a stored envelope when its decoder identity still matches.

    The npz lives inside the signature directory, so a rebuild that only adds a
    section (transcript, speech) does not pay the decode again — which is what
    makes "build once, reuse everywhere" true in practice.
    """
    if refresh:
        return None
    try:
        stored = load_envelope(directory / "envelope.npz")
    except MaterialAudioEnvelopeError:
        return None
    if stored is None or str((stored.get("identity") or {}).get("signature") or "") != identity:
        return None
    return stored


def _reusable_motion(directory: Path, identity: str, refresh: bool) -> dict[str, Any] | None:
    if refresh:
        return None
    try:
        stored = load_motion_timeline(directory / "motion.npz")
    except MaterialMotionTimelineError:
        return None
    if stored is None or str((stored.get("identity") or {}).get("signature") or "") != identity:
        return None
    return stored


def build_material_evidence(
    source: Any,
    *,
    output_root: Any,
    ffmpeg: str,
    duration: float,
    sections: tuple[str, ...] = SECTIONS,
    speech_ranges_by_range: list[dict[str, Any]] | None = None,
    transcript: dict[str, Any] | None = None,
    preset_overrides: dict[str, Any] | None = None,
    runner: Callable[..., Any] | None = None,
    refresh: bool = False,
) -> dict[str, Any]:
    """Build (or reuse) the evidence document for ``source``.

    Only called from entry points that already require ffmpeg + numpy; a read
    path must use :func:`read_material_evidence` instead.
    """
    source = Path(source).resolve()
    if not source.is_file():
        raise MaterialEvidenceError("素材证据的媒体文件不存在")
    for name in sections:
        if name not in SECTIONS:
            raise MaterialEvidenceError(f"未知的证据分区：{name}")
    overrides = dict(preset_overrides or {})
    envelope_preset = overrides.get("envelope")
    motion_preset = overrides.get("motion")
    fingerprint = media_content_fingerprint(source)
    identities = {
        "envelope": envelope_identity(envelope_preset),
        "motion": motion_identity(motion_preset),
        "speech": _identity_signature((speech_ranges_by_range or {}).get("identity")
                                      if isinstance(speech_ranges_by_range, dict) else None),
        "transcript": _identity_signature((transcript or {}).get("identity")),
    }
    signature = evidence_signature(
        fingerprint, envelope_identity=identities["envelope"],
        speech_identity=identities["speech"], motion_identity=identities["motion"],
        transcript_identity=identities["transcript"],
    )
    directory = Path(output_root).resolve() / DIRECTORY / signature[:20]
    document_path = directory / "evidence.json"
    if not refresh and document_path.is_file():
        existing = read_material_evidence(source, output_root=output_root, with_arrays=True)
        covered = isinstance(existing, dict) and existing.get("signature") == signature
        if covered:
            # Reuse only when *every requested* section is already available:
            # a document that is missing a section must be completed, not
            # silently served as a cache hit.
            for name in sections:
                section = ((existing or {}).get("sections") or {}).get(name) or {}
                if str(section.get("status")) != "available":
                    covered = False
                    break
        if covered:
            arrays_ok = (("audio_envelope" not in sections or existing.get("envelope") is not None)
                         and ("motion" not in sections or existing.get("motion") is not None))
            if arrays_ok:
                return {**existing, "cache_hit": True, "spawns": 0}

    started = time.perf_counter()
    spawns = 0
    degradations: list[str] = []
    section_payloads: dict[str, Any] = {}
    envelope = None
    motion = None

    if "audio_envelope" in sections:
        try:
            envelope = _reusable_envelope(directory, identities["envelope"]["signature"], refresh)
            if envelope is None:
                envelope = decode_envelope(
                    source, ffmpeg=ffmpeg, preset=envelope_preset, duration=duration,
                    runner=runner if runner is not None else subprocess.run)
                spawns += 1
            save_envelope(directory / "envelope.npz", envelope)
            quiet = silence_intervals(envelope)
            section_payloads["audio_envelope"] = {
                "status": "available", "identity": identities["envelope"],
                "identity_signature": identities["envelope"]["signature"],
                "sample_rate": envelope["sample_rate"], "window_ms": envelope["window_ms"],
                "hop_ms": envelope["hop_ms"], "frame_count": envelope["frame_count"],
                "file": "envelope.npz",
                "silence_view": {
                    "preset": {"threshold_db": -40.0, "min_silence_seconds": 0.45},
                    "intervals": quiet,
                    "total_seconds": round(sum(row["length"] for row in quiet), 3),
                },
            }
        except (MaterialAudioEnvelopeUnavailable, MaterialAudioEnvelopeError) as exc:
            degradations.append(f"audio_envelope_unavailable:{exc}")
            section_payloads["audio_envelope"] = _not_available(str(exc))
    else:
        section_payloads["audio_envelope"] = _not_available("本次未请求音频包络分区")

    if "motion" in sections:
        try:
            motion = _reusable_motion(directory, identities["motion"]["signature"], refresh)
            if motion is None:
                motion = build_motion_timeline(
                    source, ffmpeg=ffmpeg, preset=motion_preset, duration=duration,
                    runner=runner if runner is not None else subprocess.run)
                spawns += 1
            save_motion_timeline(directory / "motion.npz", motion)
            section_payloads["motion"] = {
                "status": "available", "identity": identities["motion"],
                "fps": motion["fps"], "width": motion["width"], "height": motion["height"],
                "sample_count": motion["sample_count"], "frame_count": motion["frame_count"],
                "file": "motion.npz",
                "low_motion_threshold": motion["low_motion_threshold"],
                "low_motion_seconds": low_motion_seconds(motion),
            }
        except (MaterialMotionTimelineUnavailable, MaterialMotionTimelineError) as exc:
            degradations.append(f"motion_timeline_unavailable:{exc}")
            section_payloads["motion"] = _not_available(str(exc))
    else:
        section_payloads["motion"] = _not_available("本次未请求运动时间线分区")

    if "speech_activity" in sections:
        rows = speech_ranges_by_range
        if isinstance(rows, dict):
            rows = rows.get("ranges") or []
        ranges = []
        total = 0
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            speech = row.get("speech_ranges") or []
            if not isinstance(speech, list):
                continue
            total += len(speech)
            ranges.append({"start": row.get("start"), "end": row.get("end"),
                           "speech_ranges": len(speech)})
        if rows:
            section_payloads["speech_activity"] = {
                "status": "available", "identity": identities["speech"],
                "range_count": len(ranges), "speech_ranges": total, "ranges": ranges,
            }
        else:
            section_payloads["speech_activity"] = _not_available("没有提供候选范围的 VAD 证据")
    else:
        section_payloads["speech_activity"] = _not_available("本次未请求语音活动分区")

    if "transcript" in sections:
        payload = transcript if isinstance(transcript, dict) else {}
        utterances = [row for row in payload.get("utterances") or [] if isinstance(row, dict)]
        if utterances:
            timeline = []
            for index, row in enumerate(utterances, 1):
                try:
                    start, end = float(row["start"]), float(row["end"])
                except (KeyError, TypeError, ValueError):
                    continue
                if end <= start:
                    continue
                timeline.append({"id": str(row.get("id") or f"U{index:05d}"),
                                 "start": round(start, 3), "end": round(end, 3)})
            section_payloads["transcript"] = {
                "status": "available", "policy": str(payload.get("policy") or "unknown"),
                "identity": identities["transcript"], "utterance_count": len(timeline),
                "timeline": timeline,
            }
        else:
            section_payloads["transcript"] = _not_available("没有可用的转写时间轴")
    else:
        section_payloads["transcript"] = _not_available("本次未请求转写分区")

    statuses = [str(row.get("status") or "unavailable") for row in section_payloads.values()]
    if all(value == "available" for value in statuses):
        status = "available"
    elif any(value == "available" for value in statuses):
        status = "partial"
    else:
        status = "unavailable"

    total_bytes = 0
    for name in ("envelope.npz", "motion.npz"):
        candidate = directory / name
        if candidate.is_file():
            total_bytes += candidate.stat().st_size
    if total_bytes > MAX_BYTES:
        degradations.append(f"material_evidence_too_large:证据文档 {total_bytes / 1048576:.2f}MB 超过 5MB 预算")

    payload = {
        "version": VERSION, "status": status, "signature": signature,
        "directory": str(directory), "cache_hit": False, "spawns": spawns,
        "source": {"fingerprint": fingerprint, "duration": round(float(duration), 3),
                   "media_path": str(source)},
        "sections": section_payloads,
        "degradations": degradations,
        "metadata": {"built_seconds": round(time.perf_counter() - started, 3), "spawns": spawns,
                     "cache_hit": False, "bytes": total_bytes},
    }
    if status != "unavailable":
        _atomic_json(document_path, payload)
    return {**payload, "envelope": envelope, "motion": motion}


def _candidate_documents(output_root: Any) -> list[Path]:
    root = Path(output_root).resolve() / DIRECTORY
    if not root.is_dir():
        return []
    return sorted(root.glob("*/evidence.json"), key=lambda path: path.stat().st_mtime, reverse=True)


def read_material_evidence(media: Any, *, output_root: Any, with_arrays: bool = False,
                           identity: str | None = None) -> dict[str, Any] | None:
    """Read the document for ``media``.  Pure file read + pure numpy.

    Returns ``None`` when nothing was ever built.  When numpy is missing it
    returns a ``status="unavailable"`` payload and **never raises** — a
    diagnostic must not add a dependency to a path that never had one.
    """
    root = Path(output_root).resolve() / DIRECTORY
    if not root.is_dir():
        return None
    try:
        fingerprint = media_content_fingerprint(Path(media))
    except Exception:
        return None
    found = None
    for path in _candidate_documents(output_root):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        if identity is not None and payload.get("signature") != identity:
            continue
        if str((payload.get("source") or {}).get("fingerprint") or "") != fingerprint:
            continue
        found = payload
        break
    if found is None:
        return None
    result: dict[str, Any] = {
        "version": found.get("version"), "status": found.get("status"),
        "signature": found.get("signature"), "directory": found.get("directory") or str(found and root),
        "source": found.get("source") or {}, "sections": found.get("sections") or {},
        "degradations": list(found.get("degradations") or []),
        "metadata": {**(found.get("metadata") or {}), "cache_hit": True, "spawns": 0},
        "cache_hit": True, "spawns": 0, "envelope": None, "motion": None,
    }
    if not with_arrays:
        return result
    directory = Path(str(found.get("directory") or ""))
    if not directory.is_dir():
        directory = Path(str(root))
    result["degradations"] = list(result["degradations"])
    for name, loader, key in (("envelope.npz", load_envelope, "envelope"),
                              ("motion.npz", load_motion_timeline, "motion")):
        section = (result["sections"] or {}).get(
            "audio_envelope" if key == "envelope" else "motion") or {}
        if section.get("status") != "available":
            continue
        try:
            result[key] = loader(directory / name)
        except (MaterialAudioEnvelopeUnavailable, MaterialMotionTimelineUnavailable) as exc:
            # numpy absent: degrade the whole document, do not raise.
            result["status"] = "unavailable"
            result["degradations"].append(f"numpy_unavailable:{exc}")
            result["envelope"] = None
            result["motion"] = None
            return result
        except (MaterialAudioEnvelopeError, MaterialMotionTimelineError) as exc:
            result["degradations"].append(f"{key}_unavailable:{exc}")
    return result


def summarize_material_evidence(payload: dict[str, Any] | None) -> dict[str, Any]:
    """One-line-per-section summary for the UI and for logs."""
    if not isinstance(payload, dict):
        return {"status": "absent", "sections": {}, "degradations": [],
                "built_seconds": None, "megabytes": None}
    sections = payload.get("sections") or {}
    metadata = payload.get("metadata") or {}
    return {
        "status": str(payload.get("status") or "unavailable"),
        "sections": {name: str((row or {}).get("status") or "unavailable")
                     for name, row in sections.items() if isinstance(row, dict)},
        "degradations": list(payload.get("degradations") or []),
        "built_seconds": metadata.get("built_seconds"),
        "megabytes": round(int(metadata.get("bytes") or 0) / 1048576, 3),
        "spawns": metadata.get("spawns"),
    }


__all__ = [
    "DIRECTORY", "MAX_BYTES", "SECTIONS", "VERSION", "MaterialEvidenceError",
    "build_material_evidence", "evidence_signature", "read_material_evidence",
    "summarize_material_evidence",
]
