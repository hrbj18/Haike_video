"""Local silence evidence for second-pass pause compression.

This module answers exactly one question: **where is the audio actually quiet**.
It never decides what to delete.  Deciding belongs to the plan layer, which
combines this evidence with the VAD speech ranges and the parent candidate's
allowed ranges, and then validates every resulting cut.

Why a second, independent signal instead of trusting VAD alone:

* VAD (Silero) answers "is somebody speaking".  Laughter, a door slam or a
  dropped object are not speech to VAD, but they are absolutely not dead air —
  cutting them would damage the take.
* ``silencedetect`` answers "is this passage quiet".  It cannot tell whether a
  quiet passage still contains a whispered word.

Only the intersection of both is treated as removable, which matches the
repository's conservative-first rule for anything that touches real dialogue.

Two interchangeable backends
----------------------------
``backend="envelope"`` decodes the whole file **once** and answers every
threshold from an in-memory RMS envelope (:mod:`backlot.material_audio_envelope`);
``backend="ffmpeg"`` keeps the historical per-range ``silencedetect`` spawn.
``auto`` (the default) prefers the envelope and falls back, recording the reason
in ``degradations``.  Falling back is a documented normal branch, not an
incident.

The envelope is **not** the same measurement as ``silencedetect``: at the same
nominal dB it reports ~2.5x more quiet time, so the envelope path uses its own
calibrated operating point (``ENVELOPE_THRESHOLD_DB``).  See the envelope
module docstring for the measurements.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

from backlot.media_index import media_content_fingerprint
from backlot.material_audio_envelope import (
    MaterialAudioEnvelopeError,
    MaterialAudioEnvelopeUnavailable,
    decode_envelope,
    envelope_identity,
    intervals_within,
    load_envelope,
    save_envelope,
    silence_intervals,
)


VERSION = "material-pause-evidence-v2"

# -30 dBFS is roughly "3% of full scale": on a street recording that is the
# ambient floor, which a viewer experiences as waiting even though it is not
# digital silence.  Measured on the 88.9-minute sample, -30 finds 180s of dead
# air versus 154s at -32 and 134s at -34; tightening the probe further only
# removes material an editor would keep.
DEFAULT_NOISE_DB = -30.0
# Tuning the probe finer than this is wasted work: what actually limits the
# result is the plan layer's guard / breathing-room / minimum-pause rules, so
# intervals shorter than ~0.45s can never survive them anyway.
DEFAULT_MIN_SILENCE = 0.45
MIN_NOISE_DB = -60.0
MAX_NOISE_DB = -10.0
MIN_SILENCE_FLOOR = 0.12
MIN_SILENCE_CEILING = 3.0
# --- bridging short loud excursions ------------------------------------------
# A street recording's multi-second dialogue gap usually contains one or two loud
# frames (a car, a passer-by, the subject's own machine).  ``silence_intervals``
# demands *contiguous* quiet frames, so a single such frame truncates the run and
# the whole gap is offered to nobody — measured on the acceptance material, only
# 23.4 s of the 50.8 s of real VAD gaps were visible at the calibrated −40 dB,
# which is why "压缩对话间停顿" looked like it did nothing.  Bridging merges quiet
# runs separated by less than this into one interval.
DEFAULT_BRIDGE_SECONDS = 0.0
BRIDGE_BOUNDS = (0.0, 0.5)

# Calibrated replacement for ``silencedetect -30dB`` on **this** envelope
# metric.  Measured on the pilot asset: plan-layer removal 81.5s / 136 cuts
# versus 81.0s / 143 cuts (+0.6% / -4.9%), mean interval IoU 0.822.
ENVELOPE_THRESHOLD_DB = -40.0

AUDIO_BACKEND_ENV = "MATERIAL_EVIDENCE_AUDIO_BACKEND"
AUDIO_BACKENDS = ("auto", "envelope", "ffmpeg")
DETECTOR_FFMPEG = "ffmpeg/silencedetect"
DETECTOR_ENVELOPE = "envelope/rms"

# ``silence_start: 12.345`` / ``silence_end: 13.579 | silence_duration: 1.234``
_VALUE = r"(-?\d+(?:\.\d+)?)"
_SILENCE_START = re.compile(r"silence_start:\s*" + _VALUE)
_SILENCE_END = re.compile(r"silence_end:\s*" + _VALUE)


class InteractionPauseEvidenceError(ValueError):
    pass


class InteractionPauseEvidenceUnavailable(InteractionPauseEvidenceError):
    """The probe could not run at all (missing binary, unsupported filter)."""


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _number(value: Any, *, minimum: float, maximum: float, label: str) -> float:
    if isinstance(value, bool):
        raise InteractionPauseEvidenceError(f"{label}格式无效")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise InteractionPauseEvidenceError(f"{label}格式无效") from exc
    if not math.isfinite(result) or not minimum <= result <= maximum:
        raise InteractionPauseEvidenceError(f"{label}超出范围")
    return result


def requested_backend(backend: str | None = None) -> str:
    """Resolve the effective backend from the argument, then the environment."""
    raw = str(backend if backend is not None else (os.environ.get(AUDIO_BACKEND_ENV) or "auto")).strip().lower()
    if raw not in AUDIO_BACKENDS:
        raise InteractionPauseEvidenceError("音频证据后端取值无效（应为 auto/envelope/ffmpeg）")
    return raw


def runtime_identity(noise_db: float = DEFAULT_NOISE_DB,
                     min_silence: float = DEFAULT_MIN_SILENCE,
                     *, backend: str = "ffmpeg",
                     envelope_threshold_db: float | None = None,
                     bridge_seconds: float = DEFAULT_BRIDGE_SECONDS) -> dict[str, Any]:
    noise_db = _number(noise_db, minimum=MIN_NOISE_DB, maximum=MAX_NOISE_DB, label="静音阈值")
    min_silence = _number(min_silence, minimum=MIN_SILENCE_FLOOR, maximum=MIN_SILENCE_CEILING,
                          label="最短静音时长")
    bridge = _number(bridge_seconds, minimum=BRIDGE_BOUNDS[0], maximum=BRIDGE_BOUNDS[1],
                     label="静音桥接时长")
    backend = requested_backend(backend) if backend != "auto" else "envelope"
    identity: dict[str, Any] = {
        "version": VERSION, "noise_db": round(noise_db, 3),
        "min_silence_seconds": round(min_silence, 3), "backend": backend,
        # Bridging changes which intervals exist, so it belongs in the identity:
        # two probes that disagree about the gaps must not share a cache entry.
        "bridge_seconds": round(bridge, 3),
    }
    if backend == "envelope":
        threshold = _number(ENVELOPE_THRESHOLD_DB if envelope_threshold_db is None
                            else envelope_threshold_db,
                            minimum=-80.0, maximum=MAX_NOISE_DB, label="包络静音阈值")
        identity.update({
            "engine": "numpy/rms-envelope", "detector": DETECTOR_ENVELOPE,
            "envelope_threshold_db": round(threshold, 3),
        })
        decoder = envelope_identity()
        identity.update({key: decoder[key] for key in ("window_ms", "hop_ms", "metric", "sample_rate")})
    else:
        identity.update({"engine": DETECTOR_FFMPEG, "detector": DETECTOR_FFMPEG})
    identity["signature"] = _digest(identity)
    return identity


def normalize_ranges(ranges: Any) -> list[dict[str, float]]:
    """Validate and merge the probe ranges so the cache key stays stable."""
    rows: list[dict[str, float]] = []
    for raw in ranges if isinstance(ranges, (list, tuple)) else []:
        if not isinstance(raw, dict):
            raise InteractionPauseEvidenceError("停顿探测范围格式无效")
        start = _number(raw.get("start"), minimum=0, maximum=24 * 3600, label="探测范围开始")
        end = _number(raw.get("end"), minimum=0, maximum=24 * 3600, label="探测范围结束")
        if end <= start:
            raise InteractionPauseEvidenceError("停顿探测范围为空或倒序")
        rows.append({"start": round(start, 6), "end": round(end, 6)})
    if not rows:
        raise InteractionPauseEvidenceError("停顿探测范围为空")
    rows.sort(key=lambda row: (row["start"], row["end"]))
    merged: list[dict[str, float]] = []
    for row in rows:
        if merged and row["start"] <= merged[-1]["end"] + 1e-6:
            merged[-1]["end"] = round(max(merged[-1]["end"], row["end"]), 6)
            continue
        merged.append(dict(row))
    return merged


def parse_silencedetect(stderr: str, *, offset: float, span: float,
                        min_silence: float) -> tuple[list[dict[str, float]], int]:
    """Turn ``silencedetect`` stderr into absolute source-time intervals.

    ``-ss`` before ``-i`` makes the filter see timestamps relative to the seek
    point, so ``offset`` is added back.  Values that cannot possibly belong to
    this span are counted as anomalies instead of being silently trusted.
    """
    starts = [float(match) for match in _SILENCE_START.findall(stderr or "")]
    ends = [float(match) for match in _SILENCE_END.findall(stderr or "")]
    rows: list[dict[str, float]] = []
    anomalies = 0
    slack = 1.0
    for index, start in enumerate(starts):
        relative_end = ends[index] if index < len(ends) else span
        if not (-slack <= start <= span + slack) or not (-slack <= relative_end <= span + slack):
            anomalies += 1
            continue
        local_start = min(max(start, 0.0), span)
        local_end = min(max(relative_end, 0.0), span)
        if local_end - local_start < min_silence - 1e-6:
            continue
        rows.append({"start": round(offset + local_start, 6), "end": round(offset + local_end, 6)})
    if len(ends) > len(starts):
        anomalies += len(ends) - len(starts)
    return rows, anomalies


def _merge(rows: list[dict[str, float]]) -> list[dict[str, float]]:
    merged: list[dict[str, float]] = []
    for row in sorted(rows, key=lambda item: (item["start"], item["end"])):
        if merged and row["start"] <= merged[-1]["end"] + 1e-6:
            merged[-1]["end"] = round(max(merged[-1]["end"], row["end"]), 6)
            continue
        merged.append(dict(row))
    return merged


def probe_range(media: Path, start: float, end: float, *, ffmpeg: str, noise_db: float,
                min_silence: float, timeout: float,
                runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
                ) -> tuple[list[dict[str, float]], int]:
    span = round(end - start, 6)
    command = [
        ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "info",
        "-ss", f"{start:.6f}", "-t", f"{span:.6f}", "-i", str(media),
        "-vn", "-af", f"silencedetect=noise={noise_db:g}dB:d={min_silence:g}",
        "-f", "null", "-",
    ]
    try:
        completed = runner(command, capture_output=True, timeout=max(30.0, min(float(timeout), 600.0)),
                           check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise InteractionPauseEvidenceUnavailable(f"停顿探测无法执行：{exc}") from exc
    raw = completed.stderr
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    if completed.returncode != 0:
        detail = (raw or "").strip().splitlines()
        raise InteractionPauseEvidenceUnavailable(
            f"停顿探测失败：{detail[-1][:200] if detail else '未知原因'}")
    return parse_silencedetect(raw or "", offset=start, span=span, min_silence=min_silence)


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    handle, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
        for attempt in range(12):
            try:
                os.replace(temporary, path)
                return
            except PermissionError:
                if attempt == 11:
                    raise
                time.sleep(.01 * (attempt + 1))
    finally:
        temporary.unlink(missing_ok=True)


def _envelope_silences(envelope: dict[str, Any], rows: list[dict[str, float]], *,
                       threshold_db: float, min_silence: float,
                       ) -> tuple[list[dict[str, float]], str | None]:
    """Project the whole-file silence view onto the requested ranges."""
    if int(envelope.get("frame_count") or 0) <= 0:
        # An empty decode cannot answer anything; treat it exactly like a
        # missing envelope so the ffmpeg path takes over instead of returning
        # a confident "no silence found".
        return [], "audio_envelope_empty:一次解码没有产生可用的音频样本"
    whole = silence_intervals(envelope, threshold_db=threshold_db, min_silence_seconds=min_silence)
    return intervals_within(whole, rows), None


def _envelope_cache_path(output_root: Path | None, fingerprint: str) -> Path | None:
    if output_root is None:
        return None
    key = _digest({"fingerprint": fingerprint, "identity": envelope_identity()["signature"]})
    return Path(output_root).resolve() / "audio-envelope" / (key[:20] + ".npz")


def _identity_backends(requested: str) -> tuple[str, ...]:
    """Backends whose cache keys are worth probing for a given request."""
    if requested == "ffmpeg":
        return ("ffmpeg",)
    if requested == "envelope":
        return ("envelope",)
    return ("envelope", "ffmpeg")   # auto prefers the cheaper cached answer


def bridge_intervals(rows: list[dict[str, float]], seconds: float) -> list[dict[str, float]]:
    """Merge quiet runs separated by a short loud excursion (see ``DEFAULT_BRIDGE_SECONDS``)."""
    if seconds <= 0 or not rows:
        return [dict(row) for row in rows]
    ordered = sorted(rows, key=lambda row: (row["start"], row["end"]))
    merged = [dict(ordered[0])]
    for row in ordered[1:]:
        if row["start"] - merged[-1]["end"] <= seconds:
            merged[-1]["end"] = max(merged[-1]["end"], row["end"])
            continue
        merged.append(dict(row))
    return merged


def detect_pause_evidence(
    media: Path,
    ranges: Any,
    *,
    ffmpeg: str,
    output_root: Path | None = None,
    noise_db: float = DEFAULT_NOISE_DB,
    min_silence: float = DEFAULT_MIN_SILENCE,
    timeout: float = 300.0,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    backend: str | None = None,
    envelope: dict[str, Any] | None = None,
    envelope_provider: Callable[[], dict[str, Any] | None] | None = None,
    envelope_threshold_db: float | None = None,
    bridge_seconds: float = DEFAULT_BRIDGE_SECONDS,
) -> dict[str, Any]:
    """Measure quiet passages inside ``ranges`` on ``media``.

    ``media`` must be the exact file the renderer will read: keeping probe and
    render on one timeline removes the need to assume that a proxy's second *T*
    equals the original's second *T*.

    A single unreadable range degrades to ``partial`` rather than failing the
    whole operation — the plan layer translates that into a visible degradation.
    """
    media = Path(media).resolve()
    if not media.is_file():
        raise InteractionPauseEvidenceUnavailable("停顿探测的媒体文件不存在")
    requested = requested_backend(backend)
    rows = normalize_ranges(ranges)
    bridge = _number(bridge_seconds, minimum=BRIDGE_BOUNDS[0], maximum=BRIDGE_BOUNDS[1],
                     label="静音桥接时长")
    fingerprint = media_content_fingerprint(media)
    degradations: list[str] = []
    silences: list[dict[str, float]] = []
    failures: list[dict[str, Any]] = []
    anomalies = 0
    spawns = 0
    decode_seconds = 0.0
    envelope_cache_hit = False
    envelope_used = False
    threshold = _number(ENVELOPE_THRESHOLD_DB if envelope_threshold_db is None else envelope_threshold_db,
                        minimum=-80.0, maximum=MAX_NOISE_DB, label="包络静音阈值")

    # Cache lookup happens *before* any decode or spawn: the candidate identity
    # is known without probing (the backend is either forced or guessed).
    if output_root is not None:
        for candidate_backend in _identity_backends(requested):
            identity_candidate = runtime_identity(noise_db, min_silence, backend=candidate_backend,
                                                  envelope_threshold_db=threshold,
                                                  bridge_seconds=bridge)
            key = _digest({"fingerprint": fingerprint, "identity": identity_candidate["signature"],
                           "ranges": rows})
            cache_path = Path(output_root).resolve() / "pause-evidence" / (key[:20] + ".json")
            if not cache_path.is_file():
                continue
            try:
                cached = json.loads(cache_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(cached, dict) and cached.get("cache_key") == key:
                return {**cached, "cache_hit": True}

    if requested in {"auto", "envelope"}:
        shared = envelope
        if shared is None and envelope_provider is not None:
            try:
                shared = envelope_provider()
            except Exception as exc:  # a broken provider must not break the probe
                shared = None
                degradations.append(f"audio_envelope_unavailable:证据文档读取失败（{str(exc)[:120]}）")
            if shared is not None:
                envelope_cache_hit = True
        cached_path = _envelope_cache_path(output_root, fingerprint) if shared is None else None
        if cached_path is not None and cached_path.is_file():
            try:
                shared = load_envelope(cached_path)
            except MaterialAudioEnvelopeError as exc:
                degradations.append(f"audio_envelope_unavailable:{exc}")
                shared = None
            if shared is not None:
                envelope_cache_hit = True
        if shared is None:
            try:
                shared = decode_envelope(
                    media, ffmpeg=ffmpeg, runner=runner,
                    timeout=max(30.0, min(float(timeout), 1800.0)),
                )
                spawns += 1
                decode_seconds = float((shared.get("metadata") or {}).get("decode_seconds") or 0.0)
                if cached_path is not None:
                    try:
                        save_envelope(cached_path, shared)
                    except (MaterialAudioEnvelopeError, OSError) as exc:
                        degradations.append(f"audio_envelope_cache_write_failed:{str(exc)[:120]}")
            except (MaterialAudioEnvelopeUnavailable, MaterialAudioEnvelopeError) as exc:
                degradations.append(f"audio_envelope_unavailable:{exc}")
                shared = None
        if shared is not None:
            found, problem = _envelope_silences(shared, rows, threshold_db=threshold,
                                                min_silence=min_silence)
            if problem:
                degradations.append(problem)
            else:
                envelope_used = True
                silences = found

    if not envelope_used:
        # Historical per-range path.  Kept verbatim: it is the fallback switch's
        # physical carrier, and the A2 calibration compares against it.
        for row in rows:
            try:
                found, odd = probe_range(media, row["start"], row["end"], ffmpeg=ffmpeg,
                                         noise_db=noise_db, min_silence=min_silence,
                                         timeout=timeout, runner=runner)
            except InteractionPauseEvidenceUnavailable as exc:
                failures.append({"range": dict(row), "error": str(exc)[:300]})
                continue
            spawns += 1
            anomalies += odd
            silences.extend(found)

    merged = bridge_intervals(_merge(silences), bridge)
    for row in merged:
        row["length"] = round(row["end"] - row["start"], 6)
    # An unreadable range is a recorded degradation, not a fatal error: the
    # caller decides whether to keep the remaining evidence or disable the
    # feature.  Never report "available" when part of the audio was not probed.
    status = "unavailable" if len(failures) == len(rows) else ("partial" if failures else "available")
    used_backend = "envelope" if envelope_used else "ffmpeg"
    identity = runtime_identity(noise_db, min_silence, backend=used_backend,
                                envelope_threshold_db=threshold, bridge_seconds=bridge)
    detector = DETECTOR_ENVELOPE if envelope_used else DETECTOR_FFMPEG
    cache_path = None
    if output_root is not None:
        key = _digest({"fingerprint": fingerprint, "identity": identity["signature"], "ranges": rows})
        cache_path = Path(output_root).resolve() / "pause-evidence" / (key[:20] + ".json")
    payload = {
        "version": VERSION, "status": status, "identity": identity, "detector": detector,
        "backend": used_backend, "requested_backend": requested,
        "source_fingerprint": fingerprint, "media_path": str(media),
        "ranges": rows, "silences": merged,
        "metadata": {
            "range_count": len(rows), "failed_ranges": len(failures),
            "silence_count": len(merged), "anomalies": anomalies,
            "silence_seconds": round(sum(row["length"] for row in merged), 3),
            "detector": detector, "spawns": spawns,
            "decode_seconds": round(decode_seconds, 3),
            "envelope_cache_hit": envelope_cache_hit,
        },
        "failures": failures, "degradations": degradations, "cache_hit": False,
    }
    if cache_path is not None:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        payload["cache_key"] = _digest({"fingerprint": fingerprint, "identity": identity["signature"],
                                        "ranges": rows})
        _atomic_json(cache_path, payload)
    return payload


__all__ = [
    "AUDIO_BACKENDS", "AUDIO_BACKEND_ENV", "BRIDGE_BOUNDS", "DEFAULT_BRIDGE_SECONDS",
    "DEFAULT_MIN_SILENCE", "DEFAULT_NOISE_DB",
    "DETECTOR_ENVELOPE", "DETECTOR_FFMPEG", "ENVELOPE_THRESHOLD_DB",
    "InteractionPauseEvidenceError", "InteractionPauseEvidenceUnavailable",
    "MAX_NOISE_DB", "MIN_NOISE_DB", "VERSION",
    "bridge_intervals", "detect_pause_evidence", "normalize_ranges", "parse_silencedetect",
    "probe_range", "requested_backend", "runtime_identity",
]
