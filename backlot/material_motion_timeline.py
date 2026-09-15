"""Whole-file motion timeline (96x54 gray @2fps + numpy diff), zero API cost.

Replaces the "12 windows for the whole source" budget in
:mod:`backlot.material_interaction_refinement`, which measured **3/28**
event coverage: whether a pause could be shortened depended on which two
seconds happened to be sampled, not on the content.

The metric is deliberately the *same* one the old window sampler used
(``fps=2, scale=96:54:flags=area, format=gray`` then ``mean(|diff|)/255``) so
the historical threshold ``0.035`` keeps its meaning and the two
implementations can be asserted equal.

Storage note
------------
Scores are stored as **float32**, not the ``uint16`` the design sketch
suggested: the acceptance criterion requires the timeline to reproduce the old
window ``motion.maximum`` to ``1e-6``, and a 16-bit quantisation has a step of
~1.5e-5.  float32 keeps the value bit-exact and still costs only ~43KB for a
90-minute source (budget is 5MB).
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

VERSION = "material-motion-timeline-v1"

PRESET: dict[str, Any] = {
    "fps": 2.0,
    "width": 96,
    "height": 54,
    "pix_fmt": "gray",
    "metric": "mean_abs_diff_over_255",
    "low_motion_threshold": 0.035,
}

MIN_FPS = 0.5
MAX_FPS = 10.0
MIN_SIDE = 16
MAX_SIDE = 320
FRAME_BYTES_CHUNK = 64 * 1024 * 1024


class MaterialMotionTimelineError(ValueError):
    pass


class MaterialMotionTimelineUnavailable(MaterialMotionTimelineError):
    pass


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _number(value: Any, *, minimum: float, maximum: float, label: str) -> float:
    if isinstance(value, bool):
        raise MaterialMotionTimelineError(f"{label}格式无效")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise MaterialMotionTimelineError(f"{label}格式无效") from exc
    if not math.isfinite(result) or not minimum <= result <= maximum:
        raise MaterialMotionTimelineError(f"{label}超出范围")
    return result


def _normalized_preset(preset: dict[str, Any] | None) -> dict[str, Any]:
    raw = dict(PRESET)
    raw.update(preset or {})
    fps = _number(raw.get("fps"), minimum=MIN_FPS, maximum=MAX_FPS, label="运动采样帧率")
    width = int(_number(raw.get("width"), minimum=MIN_SIDE, maximum=MAX_SIDE, label="运动采样宽度"))
    height = int(_number(raw.get("height"), minimum=MIN_SIDE, maximum=MAX_SIDE, label="运动采样高度"))
    pix_fmt = str(raw.get("pix_fmt") or "gray")
    if pix_fmt not in {"gray"}:
        raise MaterialMotionTimelineError("运动采样像素格式无效")
    metric = str(raw.get("metric") or "mean_abs_diff_over_255")
    if metric != "mean_abs_diff_over_255":
        raise MaterialMotionTimelineError("运动度量无效")
    threshold = _number(raw.get("low_motion_threshold"), minimum=0.0, maximum=1.0, label="低运动阈值")
    return {"fps": round(fps, 4), "width": width, "height": height, "pix_fmt": pix_fmt,
            "metric": metric, "low_motion_threshold": round(threshold, 6)}


def motion_identity(preset: dict[str, Any] | None = None) -> dict[str, Any]:
    normalized = _normalized_preset(preset)
    identity = {"version": VERSION, "engine": "ffmpeg/rawvideo-gray", **normalized}
    identity["signature"] = _digest(identity)
    return identity


def _import_numpy() -> Any:
    try:
        import numpy as np
    except ImportError as exc:  # pragma: no cover - injected in tests
        raise MaterialMotionTimelineUnavailable("本机 numpy 不可用，运动时间线整体降级") from exc
    return np


def _filter_expression(normalized: dict[str, Any]) -> str:
    return (f"fps={normalized['fps']:g},"
            f"scale={normalized['width']}:{normalized['height']}:flags=area,"
            f"format={normalized['pix_fmt']}")


def _decode_command(media: Path, ffmpeg: str, normalized: dict[str, Any]) -> list[str]:
    return [
        ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error",
        "-i", str(media), "-an", "-vf", _filter_expression(normalized),
        "-f", "rawvideo", "pipe:1",
    ]


def _read_frames(command: list[str], *, runner: Callable[..., Any], timeout: float) -> bytes:
    if runner is subprocess.run:
        process = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        chunks: list[bytes] = []
        try:
            while True:
                chunk = process.stdout.read(FRAME_BYTES_CHUNK) if process.stdout else b""
                if not chunk:
                    break
                chunks.append(chunk)
        finally:
            stderr = process.stderr.read() if process.stderr else b""
            process.wait(timeout=max(30.0, float(timeout)))
            if process.returncode != 0:
                detail = (stderr or b"").decode("utf-8", errors="replace").strip().splitlines()
                raise MaterialMotionTimelineUnavailable(
                    f"运动时间线解码失败：{detail[-1][:200] if detail else '未知原因'}")
        return b"".join(chunks)
    try:
        completed = runner(command, capture_output=True, timeout=max(30.0, float(timeout)), check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MaterialMotionTimelineUnavailable(f"运动时间线解码无法执行：{exc}") from exc
    if completed.returncode != 0:
        raw = completed.stderr or b""
        if isinstance(raw, str):
            raw = raw.encode("utf-8", errors="replace")
        detail = raw.decode("utf-8", errors="replace").strip().splitlines()
        raise MaterialMotionTimelineUnavailable(
            f"运动时间线解码失败：{detail[-1][:200] if detail else '未知原因'}")
    return bytes(completed.stdout or b"")


def build_motion_timeline(
    media: Path,
    *,
    ffmpeg: str,
    preset: dict[str, Any] | None = None,
    duration: float | None = None,
    timeout: float = 1800.0,
    runner: Callable[..., Any] = subprocess.run,
) -> dict[str, Any]:
    """One spawn: decode the whole file to 96x54 gray @2fps and diff in numpy."""
    np = _import_numpy()
    media = Path(media).resolve()
    if not media.is_file():
        raise MaterialMotionTimelineUnavailable("运动时间线的媒体文件不存在")
    normalized = _normalized_preset(preset)
    frame_size = normalized["width"] * normalized["height"]
    command = _decode_command(media, ffmpeg, normalized)
    started = time.perf_counter()
    raw = _read_frames(command, runner=runner, timeout=timeout)
    decode_seconds = time.perf_counter() - started
    if len(raw) % frame_size:
        raise MaterialMotionTimelineError(
            f"运动时间线帧字节数不是帧大小的整数倍（{len(raw)} 字节），拒绝静默截断")
    started = time.perf_counter()
    frames = np.frombuffer(raw, dtype=np.uint8).reshape((-1, frame_size)).astype(np.float32)
    if len(frames) == 0:
        # An empty decode cannot answer anything.  Reporting "available, 0
        # samples" would look like "no motion anywhere", which is a different
        # and much more dangerous claim.
        raise MaterialMotionTimelineUnavailable("运动时间线解码没有产生任何帧")
    if len(frames) < 2:
        scores = np.zeros(0, dtype=np.float32)
    else:
        # Same expression as `analyze_pause_visual_activity`, so the historical
        # 0.035 threshold and every frozen `pause_visual` window stay comparable.
        scores = np.mean(np.abs(np.diff(frames, axis=0)), axis=1) / 255.0
        scores = scores.astype(np.float32)
    diff_seconds = time.perf_counter() - started
    frame_count = int(len(frames))
    media_seconds = float(duration) if duration else frame_count / float(normalized["fps"])
    return {
        "version": VERSION,
        "identity": motion_identity(preset),
        "fps": normalized["fps"], "width": normalized["width"], "height": normalized["height"],
        "pix_fmt": normalized["pix_fmt"], "metric": normalized["metric"],
        "low_motion_threshold": normalized["low_motion_threshold"],
        "frame_count": frame_count,
        "sample_count": int(scores.size),
        "duration_seconds": round(media_seconds, 6),
        "scores": scores,
        "spawns": 1,
        "metadata": {"decode_seconds": round(decode_seconds, 3),
                     "diff_seconds": round(diff_seconds, 3),
                     "bytes": len(raw)},
    }


# --- views -------------------------------------------------------------------

def _require_timeline(timeline: Any) -> dict[str, Any]:
    if not isinstance(timeline, dict) or "scores" not in timeline:
        raise MaterialMotionTimelineError("运动时间线载荷无效")
    return timeline


def score_time(timeline: dict[str, Any], index: int) -> float:
    """Start time of the frame pair that produced ``scores[index]``."""
    return index / float(timeline["fps"])


def max_motion_in(timeline: dict[str, Any], start: float, end: float) -> float:
    """Peak motion inside ``[start, end]`` — the only entry point the plan uses.

    A score counts when **both** of its frames fall inside the span, which is
    exactly which frames a `-ss start -to end` decode would have produced.
    """
    np = _import_numpy()
    data = _require_timeline(timeline)
    scores = np.asarray(data["scores"], dtype=np.float32)
    if scores.size == 0:
        return 0.0
    fps = float(data["fps"])
    low = _number(start, minimum=0.0, maximum=24 * 3600, label="运动窗口开始")
    high = _number(end, minimum=0.0, maximum=24 * 3600, label="运动窗口结束")
    if high <= low:
        raise MaterialMotionTimelineError("运动窗口为空或倒序")
    first = int(math.ceil(low * fps - 1e-9))
    last = int(math.floor(high * fps - 1e-9)) - 1
    first = max(0, first)
    last = min(int(scores.size) - 1, last)
    if last < first:
        # A span narrower than one frame interval still has a nearest sample;
        # reporting 0.0 would look like "no motion" instead of "no coverage".
        nearest = min(int(scores.size) - 1, max(0, int(round(low * fps)) ))
        return float(scores[nearest])
    return float(np.max(scores[first:last + 1]))


def low_motion_intervals(timeline: dict[str, Any], *, threshold: float | None = None,
                         min_seconds: float = 1.0) -> list[dict[str, float]]:
    """Continuous passages at or below the motion threshold."""
    np = _import_numpy()
    data = _require_timeline(timeline)
    limit = _number(threshold if threshold is not None else data.get("low_motion_threshold", 0.035),
                    minimum=0.0, maximum=1.0, label="低运动阈值")
    minimum = _number(min_seconds, minimum=0.0, maximum=3600.0, label="最短低运动时长")
    scores = np.asarray(data["scores"], dtype=np.float32)
    if scores.size == 0:
        return []
    fps = float(data["fps"])
    quiet = scores <= limit
    padded = np.concatenate((np.zeros(1, dtype=bool), quiet, np.zeros(1, dtype=bool)))
    edges = np.flatnonzero(padded[1:] != padded[:-1])
    rows = []
    for run_start, run_end in zip(edges[::2], edges[1::2]):
        start_seconds = int(run_start) / fps
        end_seconds = int(run_end) / fps
        length = end_seconds - start_seconds
        if length + 1e-9 < minimum:
            continue
        rows.append({"start": round(start_seconds, 6), "end": round(end_seconds, 6),
                     "length": round(length, 6)})
    return rows


def low_motion_seconds(timeline: dict[str, Any], *, threshold: float | None = None,
                       min_seconds: float = 1.0) -> float:
    return round(sum(row["length"] for row in
                     low_motion_intervals(timeline, threshold=threshold, min_seconds=min_seconds)), 3)


def timeline_summary(timeline: dict[str, Any]) -> dict[str, Any]:
    data = _require_timeline(timeline)
    return {
        "version": data.get("version"), "fps": data.get("fps"),
        "width": data.get("width"), "height": data.get("height"),
        "sample_count": data.get("sample_count"), "frame_count": data.get("frame_count"),
        "low_motion_threshold": data.get("low_motion_threshold"),
        "duration_seconds": data.get("duration_seconds"),
    }


# --- persistence -------------------------------------------------------------

def save_motion_timeline(path: Path, timeline: dict[str, Any]) -> Path:
    np = _import_numpy()
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    header = {
        "version": timeline.get("version"), "identity": timeline.get("identity"),
        "fps": timeline.get("fps"), "width": timeline.get("width"), "height": timeline.get("height"),
        "pix_fmt": timeline.get("pix_fmt"), "metric": timeline.get("metric"),
        "low_motion_threshold": timeline.get("low_motion_threshold"),
        "frame_count": timeline.get("frame_count"), "sample_count": timeline.get("sample_count"),
        "duration_seconds": timeline.get("duration_seconds"),
    }
    temporary = path.with_suffix(path.suffix + ".tmp")
    with open(temporary, "wb") as handle:
        # float32 keeps the score bit-exact; see the module docstring.
        np.savez_compressed(
            handle,
            header=np.frombuffer(json.dumps(header, ensure_ascii=False).encode("utf-8"), dtype=np.uint8),
            scores=np.asarray(timeline["scores"], dtype=np.float32),
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


def load_motion_timeline(path: Path) -> dict[str, Any] | None:
    np = _import_numpy()
    path = Path(path)
    if not path.is_file():
        return None
    try:
        with np.load(str(path)) as stored:
            header = json.loads(bytes(stored["header"]).decode("utf-8"))
            timeline = {**header, "scores": np.asarray(stored["scores"], dtype=np.float32),
                        "spawns": 0, "metadata": {}}
    except (OSError, ValueError, KeyError) as exc:
        raise MaterialMotionTimelineError(f"运动时间线读取失败：{exc}") from exc
    return timeline


__all__ = [
    "PRESET", "VERSION", "MaterialMotionTimelineError", "MaterialMotionTimelineUnavailable",
    "build_motion_timeline", "load_motion_timeline", "low_motion_intervals",
    "low_motion_seconds", "max_motion_in", "motion_identity", "save_motion_timeline",
    "score_time",
]
