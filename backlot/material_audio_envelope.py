"""One-decode, in-memory audio envelope for local material understanding.

Why this module exists
----------------------
The pause-compression evidence layer used to spawn ``ffmpeg silencedetect``
once per candidate range.  That made the *threshold* part of the cache key, so
every threshold sweep re-decoded the same audio (25 candidates -> 27 ranges ->
27 process spawns per sweep, measured 2.19s).

This module splits the expensive part (decode) from the cheap part (decision):

* :func:`decode_envelope` performs **exactly one** ``ffmpeg`` spawn for the
  whole media and returns a frozen, quantised RMS/peak envelope.
* :func:`silence_intervals` is a **pure function of that envelope** plus a
  threshold, so re-tuning costs milliseconds and zero I/O.

Calibration warning (measured, not theoretical)
-----------------------------------------------
A mono RMS envelope is **not** the same measurement as ``silencedetect``.  On
the 88.9-minute sample, the naive "same dB number" swap (``-30 dB`` on both
sides) reports **2.49x** more quiet time and removes **1.76x** more audio once
the real plan layer runs.  The factory preset therefore uses ``-40.0 dB``,
which was measured to reproduce the ``silencedetect -30 dB`` operating point
(plan-layer removal 81.5s / 136 cuts vs 81.0s / 143 cuts, mean IoU 0.822).
Changing any preset value invalidates that calibration.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Iterator

VERSION = "material-audio-envelope-v1"

# The calibration value -40.0 is *not* a free parameter: see the module
# docstring.  window/hop were swept (40..400ms) and made the divergence worse,
# so they stay at 20/10ms.
PRESET: dict[str, Any] = {
    "sample_rate": 16000,
    "channels": 1,
    "sample_format": "s16le",
    "window_ms": 20,
    "hop_ms": 10,
    "metric": "rms",
    "threshold_db": -40.0,
    "min_silence_seconds": 0.45,
}

MIN_THRESHOLD_DB = -80.0
MAX_THRESHOLD_DB = -10.0
MIN_SILENCE_FLOOR = 0.08
MIN_SILENCE_CEILING = 10.0
DEFAULT_CHUNK_BYTES = 8 * 1024 * 1024
DB_SCALE = 100            # quantise dB to int16 at 0.01 dB resolution
DB_FLOOR = -120.0         # anything quieter is stored as the floor
_ENV_FIELDS = ("window_ms", "hop_ms", "metric", "sample_rate", "channels", "sample_format")


class MaterialAudioEnvelopeError(ValueError):
    pass


class MaterialAudioEnvelopeUnavailable(MaterialAudioEnvelopeError):
    """The envelope could not be built at all (missing binary / missing numpy)."""


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _number(value: Any, *, minimum: float, maximum: float, label: str) -> float:
    if isinstance(value, bool):
        raise MaterialAudioEnvelopeError(f"{label}格式无效")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise MaterialAudioEnvelopeError(f"{label}格式无效") from exc
    if not math.isfinite(result) or not minimum <= result <= maximum:
        raise MaterialAudioEnvelopeError(f"{label}超出范围")
    return result


def _normalized_preset(preset: dict[str, Any] | None) -> dict[str, Any]:
    raw = dict(PRESET)
    raw.update(preset or {})
    sample_rate = int(_number(raw.get("sample_rate"), minimum=8000, maximum=192000, label="包络采样率"))
    channels = int(_number(raw.get("channels"), minimum=1, maximum=2, label="包络声道数"))
    window_ms = _number(raw.get("window_ms"), minimum=5.0, maximum=400.0, label="包络窗长")
    hop_ms = _number(raw.get("hop_ms"), minimum=1.0, maximum=400.0, label="包络跳长")
    if hop_ms > window_ms:
        raise MaterialAudioEnvelopeError("包络跳长不得大于窗长")
    metric = str(raw.get("metric") or "rms")
    if metric not in {"rms"}:
        raise MaterialAudioEnvelopeError("包络度量无效")
    threshold_db = _number(raw.get("threshold_db"), minimum=MIN_THRESHOLD_DB,
                           maximum=MAX_THRESHOLD_DB, label="静音阈值")
    min_silence = _number(raw.get("min_silence_seconds"), minimum=MIN_SILENCE_FLOOR,
                          maximum=MIN_SILENCE_CEILING, label="最短静音时长")
    window = max(1, int(round(sample_rate * window_ms / 1000.0)))
    hop = max(1, int(round(sample_rate * hop_ms / 1000.0)))
    return {
        "sample_rate": sample_rate,
        "channels": channels,
        "sample_format": str(raw.get("sample_format") or "s16le"),
        "window_ms": round(window_ms, 4),
        "hop_ms": round(hop_ms, 4),
        "metric": metric,
        "threshold_db": round(threshold_db, 3),
        "min_silence_seconds": round(min_silence, 3),
        "window_samples": window,
        "hop_samples": hop,
    }


def envelope_identity(preset: dict[str, Any] | None = None) -> dict[str, Any]:
    """Identity of the *decoder*, not of a threshold choice.

    ``threshold_db`` / ``min_silence_seconds`` deliberately stay out of the
    signature: they only参数化 the pure silence view, so re-tuning must not
    invalidate anything that was already decoded.
    """
    normalized = _normalized_preset(preset)
    identity = {
        "version": VERSION,
        "engine": "ffmpeg/decode-once",
        "sample_rate": normalized["sample_rate"],
        "channels": normalized["channels"],
        "sample_format": normalized["sample_format"],
        "window_ms": normalized["window_ms"],
        "hop_ms": normalized["hop_ms"],
        "metric": normalized["metric"],
    }
    identity["signature"] = _digest(identity)
    return identity


def default_thresholds(preset: dict[str, Any] | None = None) -> dict[str, float]:
    normalized = _normalized_preset(preset)
    return {"threshold_db": normalized["threshold_db"],
            "min_silence_seconds": normalized["min_silence_seconds"]}


# --- decoding ---------------------------------------------------------------

def _import_numpy() -> Any:
    try:
        import numpy as np
    except ImportError as exc:  # pragma: no cover - exercised via injection in tests
        raise MaterialAudioEnvelopeUnavailable("本机 numpy 不可用，音频包络整体降级") from exc
    return np


def _decode_command(media: Path, ffmpeg: str, normalized: dict[str, Any]) -> list[str]:
    return [
        ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error",
        "-i", str(media), "-vn",
        "-ac", str(normalized["channels"]), "-ar", str(normalized["sample_rate"]),
        "-f", normalized["sample_format"], "pipe:1",
    ]


def _iter_stdout_chunks(command: list[str], *, runner: Callable[..., Any], timeout: float,
                        chunk_bytes: int) -> Iterator[bytes]:
    """Yield raw PCM in bounded chunks from exactly one process.

    ``subprocess.run`` (the production default) is replaced by a streaming
    ``Popen`` read here so an 88.9-minute decode never materialises twice.  A
    test-injected runner is honoured as-is and its captured stdout is then
    sliced, which keeps the "exactly one spawn" contract observable.
    """
    if runner is subprocess.run:
        process = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        try:
            while True:
                chunk = process.stdout.read(chunk_bytes) if process.stdout else b""
                if not chunk:
                    break
                yield chunk
        finally:
            stderr = b""
            if process.stderr is not None:
                stderr = process.stderr.read() or b""
            process.wait(timeout=max(30.0, timeout))
            if process.returncode != 0:
                detail = stderr.decode("utf-8", errors="replace").strip().splitlines()
                raise MaterialAudioEnvelopeUnavailable(
                    f"音频包络解码失败：{detail[-1][:200] if detail else '未知原因'}")
        return
    try:
        completed = runner(command, capture_output=True, timeout=max(30.0, float(timeout)), check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MaterialAudioEnvelopeUnavailable(f"音频包络解码无法执行：{exc}") from exc
    if completed.returncode != 0:
        raw = completed.stderr or b""
        if isinstance(raw, str):
            raw = raw.encode("utf-8", errors="replace")
        detail = raw.decode("utf-8", errors="replace").strip().splitlines()
        raise MaterialAudioEnvelopeUnavailable(
            f"音频包络解码失败：{detail[-1][:200] if detail else '未知原因'}")
    payload = bytes(completed.stdout or b"")
    for offset in range(0, len(payload), chunk_bytes):
        yield payload[offset:offset + chunk_bytes]


def _accumulate(chunks: Iterator[bytes], *, np: Any, sample_width: int,
                 duration: float | None, sample_rate: int) -> tuple[Any, int]:
    """Copy chunks into one pre-allocated int16 buffer, growing only if needed."""
    estimate = int(math.ceil(float(duration) * sample_rate)) + sample_rate if duration else 0
    buffer = np.empty(max(estimate, 1), dtype="<i2")
    filled = 0
    carry = b""
    for chunk in chunks:
        payload = carry + bytes(chunk)
        usable = len(payload) - (len(payload) % sample_width)
        carry = payload[usable:]
        if not usable:
            continue
        samples = np.frombuffer(payload[:usable], dtype="<i2")
        needed = filled + int(samples.size)
        if needed > buffer.size:
            grown = np.empty(max(needed, buffer.size * 2), dtype="<i2")
            grown[:filled] = buffer[:filled]
            buffer = grown
        buffer[filled:needed] = samples
        filled = needed
    if carry:
        # A trailing half sample means the stream was truncated.  Silently
        # dropping it would shift every later timestamp, so refuse instead.
        raise MaterialAudioEnvelopeError(
            f"音频包络解码字节数不是样本宽度的整数倍（尾部 {len(carry)} 字节），拒绝静默截断")
    return buffer, filled


def build_envelope(audio: Any, *, sample_rate: int, window: int, hop: int) -> tuple[Any, Any]:
    """Window RMS/peak in dBFS, computed in bounded blocks.

    A single ``as_strided`` window matrix over the whole file would allocate
    ``frames x window`` floats (tens of GB on a 90-minute source), so the
    windowing runs in blocks; the strided view itself stays zero-copy.
    """
    np = _import_numpy()
    total = int(audio.size)
    frame_count = 0 if total < window else (total - window) // hop + 1
    rms_db = np.zeros(frame_count, dtype="<i2")
    peak_db = np.zeros(frame_count, dtype="<i2")
    if frame_count == 0:
        return rms_db, peak_db
    block_frames = max(1, int(2_000_000 // max(1, window)))
    scale = 1.0 / 32768.0
    for start_index in range(0, frame_count, block_frames):
        count = min(block_frames, frame_count - start_index)
        first_sample = start_index * hop
        last_sample = (start_index + count - 1) * hop + window
        segment = audio[first_sample:last_sample].astype(np.float32)
        segment *= scale
        frames = np.lib.stride_tricks.as_strided(
            segment, shape=(count, window), strides=(segment.strides[0] * hop, segment.strides[0]),
        )
        rms = np.sqrt(np.mean(np.square(frames), axis=1))
        peak = np.max(np.abs(frames), axis=1)
        db = np.clip(20.0 * np.log10(np.maximum(rms, 1e-9)), DB_FLOOR, 0.0)
        pdb = np.clip(20.0 * np.log10(np.maximum(peak, 1e-9)), DB_FLOOR, 0.0)
        rms_db[start_index:start_index + count] = np.rint(db * DB_SCALE).astype("<i2")
        peak_db[start_index:start_index + count] = np.rint(pdb * DB_SCALE).astype("<i2")
    return rms_db, peak_db


def decode_envelope(
    media: Path,
    *,
    ffmpeg: str,
    preset: dict[str, Any] | None = None,
    duration: float | None = None,
    timeout: float = 900.0,
    runner: Callable[..., Any] = subprocess.run,
    chunk_bytes: int = DEFAULT_CHUNK_BYTES,
) -> dict[str, Any]:
    """Decode ``media`` once and return the frozen envelope.

    Raises :class:`MaterialAudioEnvelopeUnavailable` when the decode cannot run
    at all; callers translate that into a recorded degradation, never into a
    hard failure of the surrounding pipeline.
    """
    np = _import_numpy()
    media = Path(media).resolve()
    if not media.is_file():
        raise MaterialAudioEnvelopeUnavailable("音频包络的媒体文件不存在")
    normalized = _normalized_preset(preset)
    identity = envelope_identity(preset)
    command = _decode_command(media, ffmpeg, normalized)
    started = time.perf_counter()
    buffer, filled = _accumulate(
        _iter_stdout_chunks(command, runner=runner, timeout=timeout, chunk_bytes=max(1024, int(chunk_bytes))),
        np=np, sample_width=2, duration=duration, sample_rate=normalized["sample_rate"],
    )
    decode_seconds = time.perf_counter() - started
    audio = buffer[:filled]
    started = time.perf_counter()
    rms_db, peak_db = build_envelope(
        audio, sample_rate=normalized["sample_rate"],
        window=normalized["window_samples"], hop=normalized["hop_samples"],
    )
    envelope_seconds = time.perf_counter() - started
    sample_count = int(filled)
    return {
        "version": VERSION,
        "identity": identity,
        "sample_rate": normalized["sample_rate"],
        "window_ms": normalized["window_ms"],
        "hop_ms": normalized["hop_ms"],
        "metric": normalized["metric"],
        "window_samples": normalized["window_samples"],
        "hop_samples": normalized["hop_samples"],
        "sample_count": sample_count,
        "frame_count": int(rms_db.size),
        "audio_seconds": round(sample_count / float(normalized["sample_rate"]), 6),
        "rms_db": rms_db,
        "peak_db": peak_db,
        "spawns": 1,
        "metadata": {
            "decode_seconds": round(decode_seconds, 3),
            "envelope_seconds": round(envelope_seconds, 3),
            "bytes_per_frame": 4,
        },
    }


# --- pure views over the envelope -------------------------------------------

def _require_envelope(envelope: Any) -> dict[str, Any]:
    if not isinstance(envelope, dict) or "rms_db" not in envelope:
        raise MaterialAudioEnvelopeError("音频包络载荷无效")
    return envelope


def frame_time(envelope: dict[str, Any], index: int) -> float:
    return index * float(envelope["hop_samples"]) / float(envelope["sample_rate"])


def _runs(mask: Any, envelope: dict[str, Any], *, minimum: float, np: Any) -> list[dict[str, float]]:
    """Contiguous ``True`` runs of a per-frame mask, as source-time intervals."""
    if mask.size == 0:
        return []
    window, hop = int(envelope["window_samples"]), int(envelope["hop_samples"])
    sample_rate = float(envelope["sample_rate"])
    padded = np.concatenate((np.zeros(1, dtype=bool), mask, np.zeros(1, dtype=bool)))
    edges = np.flatnonzero(padded[1:] != padded[:-1])
    rows: list[dict[str, float]] = []
    for run_start, run_end in zip(edges[::2], edges[1::2]):
        start_seconds = int(run_start) * hop / sample_rate
        end_seconds = (int(run_end) - 1) * hop / sample_rate + window / sample_rate
        length = end_seconds - start_seconds
        if length + 1e-9 < minimum:
            continue
        rows.append({"start": round(start_seconds, 6), "end": round(end_seconds, 6),
                     "length": round(length, 6)})
    return rows


def silence_intervals(envelope: dict[str, Any], *, threshold_db: float | None = None,
                      min_silence_seconds: float | None = None) -> list[dict[str, float]]:
    """Runs of frames below ``threshold_db`` that last at least the minimum.

    Pure and I/O-free: calling it with seven different thresholds costs
    microseconds and zero process spawns.  The interval end uses the window's
    own right edge (``j*hop + window``), matching ``silencedetect``'s habit of
    reporting the end of the quiet run.
    """
    np = _import_numpy()
    data = _require_envelope(envelope)
    threshold = _number(threshold_db if threshold_db is not None else -40.0,
                        minimum=MIN_THRESHOLD_DB, maximum=MAX_THRESHOLD_DB, label="静音阈值")
    minimum = _number(min_silence_seconds if min_silence_seconds is not None else 0.45,
                      minimum=MIN_SILENCE_FLOOR, maximum=MIN_SILENCE_CEILING, label="最短静音时长")
    values = np.asarray(data["rms_db"], dtype=np.float32) / DB_SCALE
    return _runs(values < threshold, data, minimum=minimum, np=np)


def peak_guard_intervals(envelope: dict[str, Any], *, ceiling_db: float = -1.0,
                         min_seconds: float = 0.2) -> list[dict[str, float]]:
    """Passages already at or above the ceiling, measured on the *peak* array.

    A near-full-scale passage is never dead air even when its RMS dips; the
    guard keeps the plan layer from treating a clipped recording as a pause.
    """
    np = _import_numpy()
    data = _require_envelope(envelope)
    ceiling = _number(ceiling_db, minimum=-40.0, maximum=0.0, label="峰值上限")
    minimum = _number(min_seconds, minimum=0.02, maximum=MIN_SILENCE_CEILING, label="最短峰值时长")
    values = np.asarray(data["peak_db"], dtype=np.float32) / DB_SCALE
    return _runs(values >= ceiling, data, minimum=minimum, np=np)


def intervals_within(intervals: list[dict[str, Any]], ranges: list[dict[str, Any]]) -> list[dict[str, float]]:
    """Project whole-file intervals onto disjoint ``ranges`` (clipped, merged)."""
    rows: list[dict[str, float]] = []
    for raw in intervals or []:
        if not isinstance(raw, dict):
            continue
        try:
            start, end = float(raw["start"]), float(raw["end"])
        except (KeyError, TypeError, ValueError):
            continue
        for window in ranges or []:
            try:
                low, high = float(window["start"]), float(window["end"])
            except (KeyError, TypeError, ValueError):
                continue
            clipped_start, clipped_end = max(start, low), min(end, high)
            if clipped_end - clipped_start > 1e-9:
                rows.append({"start": round(clipped_start, 6), "end": round(clipped_end, 6)})
    rows.sort(key=lambda row: (row["start"], row["end"]))
    merged: list[dict[str, float]] = []
    for row in rows:
        if merged and row["start"] <= merged[-1]["end"] + 1e-6:
            merged[-1]["end"] = round(max(merged[-1]["end"], row["end"]), 6)
            merged[-1]["length"] = round(merged[-1]["end"] - merged[-1]["start"], 6)
            continue
        merged.append({**row, "length": round(row["end"] - row["start"], 6)})
    return merged


# --- comparison helpers (A2 calibration) -------------------------------------

def interval_iou(reference: list[Any], candidate: list[Any]) -> float:
    def span(rows: list[Any]) -> list[tuple[float, float]]:
        result = []
        for row in rows or []:
            try:
                start, end = float(row["start"]), float(row["end"])
            except (KeyError, IndexError, TypeError, ValueError):
                continue
            if end > start:
                result.append((start, end))
        return result

    left, right = span(reference), span(candidate)
    if not left and not right:
        return 1.0
    intersection = 0.0
    for start_a, end_a in left:
        for start_b, end_b in right:
            intersection += max(0.0, min(end_a, end_b) - max(start_a, start_b))
    union = sum(end - start for start, end in left) + sum(end - start for start, end in right) - intersection
    return intersection / union if union > 0 else 1.0


def interval_coverage(reference: list[Any], candidate: list[Any]) -> float:
    """Share of the reference's quiet seconds that the candidate also calls quiet."""
    total = 0.0
    covered = 0.0
    for row in reference or []:
        try:
            start, end = float(row["start"]), float(row["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if end <= start:
            continue
        total += end - start
        hit = 0.0
        for other in candidate or []:
            try:
                low, high = float(other["start"]), float(other["end"])
            except (KeyError, TypeError, ValueError):
                continue
            hit += max(0.0, min(end, high) - max(start, low))
        covered += min(hit, end - start)
    return covered / total if total > 0 else 1.0


def calibrated_against_ffmpeg(pairs: list[dict[str, Any]], *,
                              reference: str = "silencedetect", candidate: str = "envelope") -> dict[str, Any]:
    """Aggregate IoU/coverage over per-range detector pairs.

    ``pairs`` is ``[{"range": {...}, "<reference>": [...], "<candidate>": [...]}, ...]``
    where each interval list is already expressed in **source time**.
    """
    scores: list[float] = []
    coverages: list[float] = []
    reference_seconds = 0.0
    candidate_seconds = 0.0
    for row in pairs or []:
        left = row.get(reference) or []
        right = row.get(candidate) or []
        scores.append(interval_iou(left, right))
        coverages.append(interval_coverage(left, right))
        reference_seconds += sum(max(0.0, float(item["end"]) - float(item["start"])) for item in left)
        candidate_seconds += sum(max(0.0, float(item["end"]) - float(item["start"])) for item in right)
    mean_iou = sum(scores) / len(scores) if scores else 0.0
    return {
        "range_count": len(pairs or []),
        "mean_iou": round(mean_iou, 4),
        "min_iou": round(min(scores), 4) if scores else 0.0,
        "mean_coverage": round(sum(coverages) / len(coverages), 4) if coverages else 0.0,
        "min_coverage": round(min(coverages), 4) if coverages else 0.0,
        "reference_seconds": round(reference_seconds, 3),
        "candidate_seconds": round(candidate_seconds, 3),
        "ratio": round(candidate_seconds / reference_seconds, 3) if reference_seconds else 0.0,
    }


# --- persistence -------------------------------------------------------------

def save_envelope(path: Path, envelope: dict[str, Any]) -> Path:
    """Write the envelope as a compressed npz (atomic replace)."""
    np = _import_numpy()
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    header = {
        "version": envelope.get("version"), "identity": envelope.get("identity"),
        "sample_rate": envelope.get("sample_rate"), "window_ms": envelope.get("window_ms"),
        "hop_ms": envelope.get("hop_ms"), "metric": envelope.get("metric"),
        "window_samples": envelope.get("window_samples"), "hop_samples": envelope.get("hop_samples"),
        "sample_count": envelope.get("sample_count"), "frame_count": envelope.get("frame_count"),
        "audio_seconds": envelope.get("audio_seconds"),
    }
    temporary = path.with_suffix(path.suffix + ".tmp")
    # Passing a file object (not a name) stops numpy from appending ".npz".
    with open(temporary, "wb") as handle:
        np.savez_compressed(
            handle,
            header=np.frombuffer(json.dumps(header, ensure_ascii=False).encode("utf-8"), dtype=np.uint8),
            rms_db=np.asarray(envelope["rms_db"], dtype="<i2"),
            peak_db=np.asarray(envelope["peak_db"], dtype="<i2"),
        )
    for attempt in range(12):
        try:
            os.replace(temporary, path)
            break
        except PermissionError:
            if attempt == 11:
                temporary.unlink(missing_ok=True)
                raise
            time.sleep(.01 * (attempt + 1))
    return path


def load_envelope(path: Path) -> dict[str, Any] | None:
    """Read a stored envelope; ``None`` when the file is absent or unreadable."""
    np = _import_numpy()
    path = Path(path)
    if not path.is_file():
        return None
    try:
        with np.load(str(path)) as stored:
            header = json.loads(bytes(stored["header"]).decode("utf-8"))
            envelope = {
                **header,
                "rms_db": np.asarray(stored["rms_db"], dtype="<i2"),
                "peak_db": np.asarray(stored["peak_db"], dtype="<i2"),
                "spawns": 0,
                "metadata": {},
            }
    except (OSError, ValueError, KeyError) as exc:
        raise MaterialAudioEnvelopeError(f"音频包络读取失败：{exc}") from exc
    return envelope


__all__ = [
    "DB_SCALE", "DEFAULT_CHUNK_BYTES", "PRESET", "VERSION",
    "MaterialAudioEnvelopeError", "MaterialAudioEnvelopeUnavailable",
    "build_envelope", "calibrated_against_ffmpeg", "decode_envelope", "default_thresholds",
    "envelope_identity", "frame_time", "interval_coverage", "interval_iou",
    "intervals_within", "load_envelope", "peak_guard_intervals", "save_envelope",
    "silence_intervals",
]
