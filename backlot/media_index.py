"""Local, evidence-first indexing for long project video assets.

The index deliberately separates cheap coarse discovery from focused analysis.
It never invents semantic labels from pixels: a candidate is semantic only when
the project has transcript or filename evidence to support that claim.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import subprocess
from pathlib import Path
from typing import Any, Callable


class MediaIndexError(RuntimeError):
    pass


TranscriptProvider = Callable[[Path], tuple[str, list[dict[str, Any]], dict[str, Any]]]
VisionDescriber = Callable[[list[dict[str, Any]]], tuple[list[dict[str, Any]], dict[str, Any]]]

MATERIAL_VISION_INDEX_VERSION = 2
SHOT_POLICY_VERSION = "adaptive-shots-v1"
FRAME_POLICY_VERSION = "adaptive-evidence-frames-v1"
DEDUPE_POLICY_VERSION = "perceptual-dedupe-v1"


def _run(command: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(30, timeout),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MediaIndexError(str(exc)) from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "本地媒体分析失败")[-3000:]
        raise MediaIndexError(detail)
    return completed


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def media_fingerprint(path: Path) -> str:
    """Hash stable file evidence without reading an hour-long asset in full."""
    source = path.resolve()
    if not source.is_file():
        raise MediaIndexError("素材文件不存在")
    stat = source.stat()
    digest = hashlib.sha256()
    digest.update(str(source).encode("utf-8"))
    digest.update(f"|{stat.st_size}|{stat.st_mtime_ns}".encode("ascii"))
    sample_size = min(1024 * 1024, stat.st_size)
    with source.open("rb") as handle:
        digest.update(handle.read(sample_size))
        if stat.st_size > sample_size:
            handle.seek(max(0, stat.st_size - sample_size))
            digest.update(handle.read(sample_size))
    return digest.hexdigest()


def media_content_fingerprint(path: Path) -> str:
    """Create a rename-stable bounded content signature for V2 asset versions."""
    source = path.resolve()
    if not source.is_file():
        raise MediaIndexError("素材文件不存在")
    size = source.stat().st_size
    digest = hashlib.sha256()
    digest.update(f"material-content-v1|{size}".encode("ascii"))
    sample_size = min(1024 * 1024, size)
    offsets = sorted({0, max(0, (size - sample_size) // 2), max(0, size - sample_size)})
    with source.open("rb") as handle:
        for offset in offsets:
            handle.seek(offset)
            digest.update(f"|{offset}|".encode("ascii"))
            digest.update(handle.read(sample_size))
    return digest.hexdigest()


def probe_media(path: Path, ffprobe: str) -> dict[str, Any]:
    completed = _run([
        ffprobe,
        "-v", "error",
        "-show_entries", "format=duration,size,format_name:stream=index,codec_type,codec_name,width,height,r_frame_rate,sample_rate,channels",
        "-of", "json",
        str(path),
    ], timeout=120)
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise MediaIndexError("ffprobe 没有返回可解析的媒体信息") from exc
    duration = float((payload.get("format") or {}).get("duration") or 0)
    if duration <= 0:
        raise MediaIndexError("素材时长无效，无法建立索引")
    return {"duration_seconds": round(duration, 3), **payload}


def _scene_change_times(source: Path, ffmpeg: str, duration: float, threshold: float) -> list[float]:
    completed = _run([
        ffmpeg,
        "-hide_banner",
        "-i", str(source),
        "-an", "-sn", "-dn",
        # Coarse indexing does not need every full-resolution source frame.
        # Downsample before scene scoring so a one-hour upload stays bounded.
        "-vf", f"fps=2,scale=480:-2,select='gt(scene,{threshold:.3f})',showinfo",
        "-vsync", "vfr",
        "-f", "null",
        "-",
    ], timeout=max(180, duration * 1.5))
    values = []
    for match in re.finditer(r"pts_time:([0-9]+(?:\.[0-9]+)?)", completed.stderr or ""):
        value = round(float(match.group(1)), 3)
        if .1 < value < duration - .1 and (not values or value - values[-1] >= .35):
            values.append(value)
    return values


def _representative_frames(
    source: Path,
    ffmpeg: str,
    directory: Path,
    duration: float,
    interval_seconds: float,
) -> list[dict[str, Any]]:
    directory.mkdir(parents=True, exist_ok=True)
    pattern = directory / "frame-%05d.jpg"
    _run([
        ffmpeg,
        "-hide_banner",
        "-y",
        "-i", str(source),
        "-an",
        "-vf", f"fps=fps=1/{interval_seconds:.3f}:start_time=0,scale=480:-2,format=yuvj420p",
        "-q:v", "3",
        str(pattern),
    ], timeout=max(180, duration * .8))
    fallback_time: float | None = None
    if not any(directory.glob("frame-*.jpg")):
        # Some FFmpeg builds legitimately emit zero frames when a clip is
        # shorter than the requested sampling interval.  A midpoint still is
        # valid visual evidence and keeps short user uploads indexable.
        fallback_time = round(duration / 2, 3)
        _run([
            ffmpeg, "-hide_banner", "-y", "-ss", f"{fallback_time:.3f}",
            "-i", str(source), "-an", "-frames:v", "1",
            "-vf", "scale=480:-2,format=yuvj420p", "-q:v", "3",
            str(directory / "frame-00001.jpg"),
        ], timeout=120)
    frames = []
    for index, path in enumerate(sorted(directory.glob("frame-*.jpg"))):
        frames.append({
            "index": index,
            "time_seconds": fallback_time if fallback_time is not None else round(min(duration, index * interval_seconds), 3),
            "path": str(path.resolve()),
        })
    if not frames:
        raise MediaIndexError("没有提取到可用的代表帧")
    return frames


def _coarse_boundaries(duration: float, scene_changes: list[float], window_seconds: float) -> list[float]:
    boundaries = [0.0]
    target = window_seconds
    while target < duration - .2:
        nearby = [value for value in scene_changes if abs(value - target) <= min(5.0, window_seconds * .25)]
        point = min(nearby, key=lambda value: abs(value - target)) if nearby else target
        if point - boundaries[-1] >= 2:
            boundaries.append(round(point, 3))
        target += window_seconds
    boundaries.append(round(duration, 3))
    return boundaries


def _transcript_for_range(segments: list[dict[str, Any]], start: float, end: float) -> str:
    return "".join(
        str(item.get("text") or "").strip()
        for item in segments
        if float(item.get("end") or 0) > start and float(item.get("start") or 0) < end
    ).strip()


def build_coarse_index(
    source: Path,
    output_dir: Path,
    *,
    ffmpeg: str,
    ffprobe: str,
    interval_seconds: float = 12,
    window_seconds: float = 30,
    scene_threshold: float = .32,
    transcript_provider: TranscriptProvider | None = None,
) -> dict[str, Any]:
    source = source.resolve()
    fingerprint = media_fingerprint(source)
    config = {
        # Production callers keep the conservative 12s/30s defaults.  Smaller
        # explicit values are useful for short clips and deterministic local
        # verification without changing the long-video cost profile.
        "interval_seconds": round(max(.5, interval_seconds), 3),
        "window_seconds": round(max(2, window_seconds), 3),
        "scene_threshold": round(min(.9, max(.05, scene_threshold)), 3),
        "transcript_requested": transcript_provider is not None,
    }
    signature = hashlib.sha256(json.dumps({"fingerprint": fingerprint, "config": config}, sort_keys=True).encode("utf-8")).hexdigest()
    run_dir = output_dir / signature[:16]
    index_path = run_dir / "coarse-index.json"
    if index_path.is_file():
        cached = json.loads(index_path.read_text(encoding="utf-8"))
        cached["cache_hit"] = True
        return cached

    probe = probe_media(source, ffprobe)
    duration = float(probe["duration_seconds"])
    frames = _representative_frames(source, ffmpeg, run_dir / "coarse-frames", duration, config["interval_seconds"])
    scene_changes = _scene_change_times(source, ffmpeg, duration, config["scene_threshold"])

    transcript_text = ""
    transcript_segments: list[dict[str, Any]] = []
    transcript_status: dict[str, Any] = {"status": "transcript_unavailable", "reason": "未请求本地语音识别"}
    if transcript_provider is not None:
        try:
            transcript_text, transcript_segments, transcript_status = transcript_provider(source)
            transcript_status = {"status": "available", **(transcript_status or {})}
        except Exception as exc:  # ASR is optional; visual evidence remains useful.
            transcript_status = {"status": "transcript_unavailable", "reason": str(exc)[:500]}

    boundaries = _coarse_boundaries(duration, scene_changes, config["window_seconds"])
    segments = []
    for index, (start, end) in enumerate(zip(boundaries, boundaries[1:]), 1):
        midpoint = (start + end) / 2
        representative = min(frames, key=lambda item: abs(float(item["time_seconds"]) - midpoint))
        text = _transcript_for_range(transcript_segments, start, end)
        segments.append({
            "id": f"COARSE-{index:04d}",
            "start_seconds": round(start, 3),
            "end_seconds": round(end, 3),
            "representative_frame": representative,
            "scene_changes": [value for value in scene_changes if start <= value < end],
            "transcript": text,
            "context_before_id": f"COARSE-{index - 1:04d}" if index > 1 else None,
            "context_after_id": f"COARSE-{index + 1:04d}" if index < len(boundaries) - 1 else None,
        })

    payload = {
        "version": 1,
        "status": "completed",
        "stage": "coarse",
        "cache_hit": False,
        "source": {"path": str(source), "fingerprint": fingerprint, "name": source.name},
        "signature": signature,
        "config": config,
        "probe": probe,
        "transcript_status": transcript_status,
        "transcript": transcript_text,
        "scene_changes": scene_changes,
        "representative_frames": frames,
        "segments": segments,
        "index_path": str(index_path.resolve()),
    }
    _write_json(index_path, payload)
    return payload


def build_fine_index(
    coarse_index: dict[str, Any],
    start_seconds: float,
    end_seconds: float,
    *,
    ffmpeg: str,
    transcript_provider: TranscriptProvider | None = None,
    fps: float = 2,
) -> dict[str, Any]:
    source = Path(str((coarse_index.get("source") or {}).get("path") or "")).resolve()
    duration = float((coarse_index.get("probe") or {}).get("duration_seconds") or 0)
    start = max(0.0, round(float(start_seconds), 3))
    end = min(duration, round(float(end_seconds), 3))
    if end - start < .4:
        raise MediaIndexError("精筛窗口至少需要 0.4 秒")
    index_path = Path(str(coarse_index.get("index_path") or ""))
    if not index_path.is_file():
        raise MediaIndexError("粗筛索引文件不存在，无法开始精筛")
    window_key = hashlib.sha256(f"{coarse_index.get('signature')}|{start}|{end}|{fps}".encode("utf-8")).hexdigest()[:16]
    directory = index_path.parent / "fine" / window_key
    result_path = directory / "fine-index.json"
    if result_path.is_file():
        cached = json.loads(result_path.read_text(encoding="utf-8"))
        cached["cache_hit"] = True
        return cached
    directory.mkdir(parents=True, exist_ok=True)
    frame_pattern = directory / "frame-%05d.jpg"
    _run([
        ffmpeg, "-hide_banner", "-y", "-ss", f"{start:.3f}", "-i", str(source),
        "-t", f"{end - start:.3f}", "-an", "-vf", f"fps={max(.5, min(5, fps)):.3f},scale=720:-2,format=yuvj420p",
        "-q:v", "2", str(frame_pattern),
    ], timeout=max(90, (end - start) * 2))
    frames = [{
        "index": index,
        "time_seconds": round(start + index / max(.5, min(5, fps)), 3),
        "path": str(path.resolve()),
    } for index, path in enumerate(sorted(directory.glob("frame-*.jpg")))]
    if not frames:
        raise MediaIndexError("精筛窗口没有提取到关键帧")

    transcript_text = ""
    transcript_segments: list[dict[str, Any]] = []
    transcript_status: dict[str, Any] = {"status": "transcript_unavailable", "reason": "未请求本地语音识别"}
    if transcript_provider is not None:
        audio = directory / "audio.wav"
        try:
            _run([
                ffmpeg, "-hide_banner", "-y", "-ss", f"{start:.3f}", "-i", str(source),
                "-t", f"{end - start:.3f}", "-vn", "-ac", "1", "-ar", "16000", str(audio),
            ], timeout=max(60, end - start))
            transcript_text, transcript_segments, transcript_status = transcript_provider(audio)
            transcript_status = {"status": "available", **(transcript_status or {})}
        except Exception as exc:
            transcript_status = {"status": "transcript_unavailable", "reason": str(exc)[:500]}

    payload = {
        "version": 1,
        "status": "completed",
        "stage": "fine",
        "cache_hit": False,
        "coarse_signature": coarse_index.get("signature"),
        "source": coarse_index.get("source"),
        "start_seconds": start,
        "end_seconds": end,
        "frames": frames,
        "transcript": transcript_text,
        "transcript_segments": transcript_segments,
        "transcript_status": transcript_status,
        "index_path": str(result_path.resolve()),
    }
    _write_json(result_path, payload)
    return payload


def _adaptive_shot_spans(
    duration: float,
    scene_changes: list[float],
    *,
    minimum_seconds: float = .8,
    maximum_seconds: float = 12.0,
) -> list[dict[str, Any]]:
    """Turn scene evidence into bounded shots without losing the full timeline."""
    points = [0.0] + [value for value in sorted(set(scene_changes)) if .1 < value < duration - .1] + [duration]
    merged: list[tuple[float, float]] = []
    start = points[0]
    for index, end in enumerate(points[1:], 1):
        is_last = index == len(points) - 1
        if end - start < minimum_seconds and not is_last:
            continue
        if is_last and end - start < minimum_seconds and merged:
            previous_start, _ = merged.pop()
            start = previous_start
        merged.append((start, end))
        start = end

    spans: list[dict[str, Any]] = []
    for start, end in merged:
        length = end - start
        parts = max(1, math.ceil(length / max(minimum_seconds, maximum_seconds)))
        step = length / parts
        for part in range(parts):
            part_start = start + part * step
            part_end = end if part == parts - 1 else start + (part + 1) * step
            spans.append({
                "start_seconds": round(part_start, 3),
                "end_seconds": round(part_end, 3),
                "boundary_reason": "scene_change" if parts == 1 else "long_shot_split",
            })
    return spans or [{"start_seconds": 0.0, "end_seconds": round(duration, 3), "boundary_reason": "single_shot"}]


def _base_sample_plan(start: float, end: float) -> list[tuple[float, str]]:
    duration = end - start
    if duration <= 1.5:
        offsets = [(.5, "anchor_middle")]
    elif duration <= 5:
        offsets = [(.25, "anchor_start"), (.75, "anchor_end")]
    elif duration <= 12:
        offsets = [(.15, "anchor_start"), (.5, "anchor_middle"), (.85, "anchor_end")]
    else:
        offsets = [(.1, "anchor_start"), (.35, "anchor_early"), (.65, "anchor_late"), (.9, "anchor_end")]
    return [(round(start + duration * ratio, 3), reason) for ratio, reason in offsets]


def _read_frame(capture: Any, cv2: Any, timestamp: float) -> Any:
    capture.set(cv2.CAP_PROP_POS_MSEC, max(0.0, timestamp) * 1000)
    ok, frame = capture.read()
    if not ok or frame is None:
        raise MediaIndexError(f"无法读取 {timestamp:.3f} 秒的视频帧")
    return frame


def _motion_peak_time(capture: Any, cv2: Any, start: float, end: float) -> float | None:
    duration = end - start
    if duration < 5:
        return None
    times = [start + duration * ratio for ratio in (.18, .34, .5, .66, .82)]
    previews = []
    try:
        for timestamp in times:
            frame = _read_frame(capture, cv2, timestamp)
            gray = cv2.cvtColor(cv2.resize(frame, (160, 90)), cv2.COLOR_BGR2GRAY)
            previews.append(gray)
    except MediaIndexError:
        return None
    scores = [float(cv2.absdiff(previews[index - 1], previews[index]).mean()) for index in range(1, len(previews))]
    if not scores or max(scores) < 2.0:
        return None
    peak_index = scores.index(max(scores)) + 1
    return round(times[peak_index], 3)


def _dhash(frame: Any, cv2: Any) -> str:
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    resized = cv2.resize(gray, (9, 8), interpolation=cv2.INTER_AREA)
    bits = resized[:, 1:] > resized[:, :-1]
    value = 0
    for bit in bits.flatten():
        value = (value << 1) | int(bool(bit))
    return f"{value:016x}"


def _hamming(left: str, right: str) -> int:
    return (int(left, 16) ^ int(right, 16)).bit_count()


def _histogram(frame: Any, cv2: Any) -> Any:
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    hist = cv2.calcHist([hsv], [0, 1], None, [24, 16], [0, 180, 0, 256])
    return cv2.normalize(hist, hist).flatten()


def _deduplicate_shot_frames(frames: list[dict[str, Any]], cv2: Any) -> None:
    groups: list[list[dict[str, Any]]] = []
    for frame in frames:
        target = None
        for group in groups:
            reference = group[0]
            exact = frame["content_sha256"] == reference["content_sha256"]
            perceptual = _hamming(frame["dhash"], reference["dhash"]) <= 5
            correlation = float(cv2.compareHist(frame["_histogram"], reference["_histogram"], cv2.HISTCMP_CORREL))
            if exact or (perceptual and correlation >= .985):
                target = group
                break
        if target is None:
            groups.append([frame])
        else:
            target.append(frame)

    for group_index, group in enumerate(groups, 1):
        representative = max(group, key=lambda item: (float(item["sharpness"]), -float(item["time_seconds"])))
        group_id = f"{frames[0]['shot_id']}-DG-{group_index:02d}"
        for frame in group:
            frame["duplicate_group_id"] = group_id
            frame["duplicate_of_frame_id"] = None if frame is representative else representative["frame_id"]
            frame["selected_for_vision"] = frame is representative
            frame.pop("_histogram", None)


def _extract_adaptive_shot_frames(
    source: Path,
    shots: list[dict[str, Any]],
    directory: Path,
) -> list[dict[str, Any]]:
    try:
        import cv2
    except ImportError as exc:
        raise MediaIndexError("本机缺少 OpenCV，无法执行自适应抽帧") from exc
    directory.mkdir(parents=True, exist_ok=True)
    capture = cv2.VideoCapture(str(source))
    if not capture.isOpened():
        raise MediaIndexError("OpenCV 无法打开待分析视频")
    frame_counter = 0
    try:
        for shot_index, shot in enumerate(shots, 1):
            shot_id = f"SHOT-{shot_index:04d}"
            shot["shot_id"] = shot_id
            plan = _base_sample_plan(float(shot["start_seconds"]), float(shot["end_seconds"]))
            peak = _motion_peak_time(capture, cv2, float(shot["start_seconds"]), float(shot["end_seconds"]))
            if peak is not None and all(abs(peak - timestamp) > .08 for timestamp, _ in plan):
                plan.append((peak, "motion_peak"))
            plan = sorted(plan, key=lambda item: item[0])[:5]
            frames: list[dict[str, Any]] = []
            for timestamp, reason in plan:
                frame = _read_frame(capture, cv2, timestamp)
                height, width = frame.shape[:2]
                if max(width, height) > 960:
                    scale = 960 / max(width, height)
                    frame = cv2.resize(
                        frame,
                        (max(2, round(width * scale)), max(2, round(height * scale))),
                        interpolation=cv2.INTER_AREA,
                    )
                frame_counter += 1
                frame_id = f"FRAME-{frame_counter:05d}"
                path = directory / f"{frame_id}.jpg"
                ok, encoded = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 88])
                if not ok:
                    raise MediaIndexError(f"无法编码证据帧 {frame_id}")
                path.write_bytes(encoded.tobytes())
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                frames.append({
                    "frame_id": frame_id,
                    "shot_id": shot_id,
                    "time_seconds": round(timestamp, 3),
                    "path": str(path.resolve()),
                    "sampling_reason": reason,
                    "content_sha256": hashlib.sha256(encoded.tobytes()).hexdigest(),
                    "dhash": _dhash(frame, cv2),
                    "sharpness": round(float(cv2.Laplacian(gray, cv2.CV_64F).var()), 3),
                    "_histogram": _histogram(frame, cv2),
                    "selected_for_vision": True,
                })
            _deduplicate_shot_frames(frames, cv2)
            shot["frames"] = frames
    finally:
        capture.release()
    return shots


def _enforce_vision_frame_budget(shots: list[dict[str, Any]], maximum: int) -> None:
    selected = [frame for shot in shots for frame in shot.get("frames") or [] if frame.get("selected_for_vision")]
    if len(shots) > maximum:
        raise MediaIndexError(f"素材包含 {len(shots)} 个镜头，超过单次视觉理解上限；请先分段分析")
    if len(selected) <= maximum:
        return
    keep: set[str] = set()
    for shot in shots:
        candidates = [frame for frame in shot.get("frames") or [] if frame.get("selected_for_vision")]
        if candidates:
            keep.add(max(candidates, key=lambda item: float(item.get("sharpness") or 0))["frame_id"])
    extras = sorted(
        (frame for frame in selected if frame["frame_id"] not in keep),
        key=lambda item: (item.get("sampling_reason") != "motion_peak", -float(item.get("sharpness") or 0)),
    )
    keep.update(frame["frame_id"] for frame in extras[:max(0, maximum - len(keep))])
    for frame in selected:
        if frame["frame_id"] not in keep:
            frame["selected_for_vision"] = False
            frame["budget_excluded"] = True


def _vision_batches(shots: list[dict[str, Any]], *, maximum_shots: int = 4, maximum_frames: int = 12) -> list[list[dict[str, Any]]]:
    batches: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_frames = 0
    for shot in shots:
        frame_count = sum(1 for frame in shot.get("frames") or [] if frame.get("selected_for_vision"))
        if frame_count < 1:
            raise MediaIndexError(f"{shot.get('shot_id') or '镜头'} 没有可供视觉理解的证据帧")
        if frame_count > maximum_frames:
            raise MediaIndexError(f"{shot.get('shot_id') or '镜头'} 的证据帧超过单请求上限")
        if current and (len(current) >= maximum_shots or current_frames + frame_count > maximum_frames):
            batches.append(current)
            current = []
            current_frames = 0
        current.append(shot)
        current_frames += frame_count
    if current:
        batches.append(current)
    return batches


def build_material_vision_index(
    source: Path,
    output_dir: Path,
    *,
    ffmpeg: str,
    ffprobe: str,
    scene_threshold: float = .32,
    minimum_shot_seconds: float = .8,
    maximum_shot_seconds: float = 12.0,
    maximum_vision_frames: int = 600,
    vision_describer: VisionDescriber | None = None,
    vision_identity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build or resume a V2 visual index without overwriting V1 artifacts."""
    source = source.resolve()
    fingerprint = media_content_fingerprint(source)
    config = {
        "scene_threshold": round(min(.9, max(.05, scene_threshold)), 3),
        "minimum_shot_seconds": round(max(.4, minimum_shot_seconds), 3),
        "maximum_shot_seconds": round(max(2.0, maximum_shot_seconds), 3),
        "maximum_vision_frames": max(1, min(2000, int(maximum_vision_frames))),
        "shot_policy_version": SHOT_POLICY_VERSION,
        "frame_policy_version": FRAME_POLICY_VERSION,
        "dedupe_policy_version": DEDUPE_POLICY_VERSION,
        "vision_enabled": vision_describer is not None,
        "vision_identity": vision_identity or {},
    }
    signature = hashlib.sha256(json.dumps({"fingerprint": fingerprint, "config": config}, sort_keys=True).encode("utf-8")).hexdigest()
    run_dir = output_dir / "vision-v2" / signature[:16]
    index_path = run_dir / "material-vision-index.json"
    payload: dict[str, Any] | None = None
    if index_path.is_file():
        try:
            cached = json.loads(index_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise MediaIndexError("已有 V2 视觉索引损坏，请保留现场后重新分析") from exc
        if cached.get("status") == "completed" or vision_describer is None:
            cached["cache_hit"] = True
            return cached
        payload = cached

    if payload is None:
        probe = probe_media(source, ffprobe)
        duration = float(probe["duration_seconds"])
        scene_changes = _scene_change_times(source, ffmpeg, duration, config["scene_threshold"])
        shots = _adaptive_shot_spans(
            duration,
            scene_changes,
            minimum_seconds=config["minimum_shot_seconds"],
            maximum_seconds=config["maximum_shot_seconds"],
        )
        shots = _extract_adaptive_shot_frames(source, shots, run_dir / "evidence-frames")
        _enforce_vision_frame_budget(shots, config["maximum_vision_frames"])
        payload = {
            "version": MATERIAL_VISION_INDEX_VERSION,
            "status": "frames_ready",
            "stage": "vision",
            "cache_hit": False,
            "source": {"path": str(source), "fingerprint": fingerprint, "name": source.name},
            "signature": signature,
            "config": config,
            "probe": probe,
            "scene_changes": scene_changes,
            "shots": shots,
            "vision": {"status": "not_requested"},
            "index_path": str(index_path.resolve()),
        }
        from backlot.material_vision_eval import validate_material_vision_index

        validate_material_vision_index(payload)
        _write_json(index_path, payload)

    if vision_describer is not None:
        remaining = [shot for shot in payload["shots"] if not isinstance(shot.get("description"), dict)]
        accumulated = payload.get("vision") if isinstance(payload.get("vision"), dict) else {}
        request_count = int(accumulated.get("request_count") or 0)
        image_count = int(accumulated.get("image_count") or 0)
        metadata: dict[str, Any] = {key: value for key, value in accumulated.items() if key not in {"status", "request_count", "image_count"}}
        for batch in _vision_batches(remaining):
            descriptions, batch_metadata = vision_describer(batch)
            supplied = {str(item.get("shot_id")): item for item in descriptions if isinstance(item, dict)}
            expected = {str(shot["shot_id"]) for shot in batch}
            if set(supplied) != expected:
                raise MediaIndexError("视觉模型没有为每个输入镜头恰好返回一项结构化描述")
            for shot in batch:
                shot["description"] = supplied[shot["shot_id"]]
            request_count += int(batch_metadata.get("request_count") or 0)
            image_count += int(batch_metadata.get("image_count") or 0)
            metadata.update({key: value for key, value in batch_metadata.items() if key not in {"request_count", "image_count"}})
            payload["vision"] = {
                "status": "generating",
                **metadata,
                "request_count": request_count,
                "image_count": image_count,
                "completed_shots": sum(isinstance(shot.get("description"), dict) for shot in payload["shots"]),
                "total_shots": len(payload["shots"]),
            }
            _write_json(index_path, payload)
        expected = {str(shot["shot_id"]) for shot in payload["shots"]}
        completed = {str(shot["shot_id"]) for shot in payload["shots"] if isinstance(shot.get("description"), dict)}
        if completed != expected:
            raise MediaIndexError("视觉模型没有完成全部镜头描述")
        payload["vision"] = {
            "status": "completed",
            **metadata,
            "request_count": request_count,
            "image_count": image_count,
            "completed_shots": len(completed),
            "total_shots": len(expected),
        }
        payload["status"] = "completed"
        from backlot.material_vision_eval import validate_material_vision_index

        validate_material_vision_index(payload)
        _write_json(index_path, payload)
    return payload


def recommend_vision_shots(index: dict[str, Any], query: str, *, limit: int = 6) -> list[dict[str, Any]]:
    query_terms = _terms(query)
    candidates: list[dict[str, Any]] = []
    for shot in index.get("shots") or []:
        description = shot.get("description") if isinstance(shot.get("description"), dict) else {}
        searchable = " ".join([
            str(description.get("summary") or ""),
            str(description.get("environment") or ""),
            " ".join(str(item.get("name") or "") for item in description.get("entities") or [] if isinstance(item, dict)),
            " ".join(str(item.get("name") or "") for item in description.get("actions") or [] if isinstance(item, dict)),
            " ".join(str(value) for value in description.get("screen_text") or []),
        ])
        hits = sorted(query_terms & _terms(searchable))
        entities = [
            str(item.get("name") or "") for item in description.get("entities") or []
            if isinstance(item, dict) and str(item.get("name") or "").strip()
        ]
        actions = [
            str(item.get("name") or "") for item in description.get("actions") or []
            if isinstance(item, dict) and str(item.get("name") or "").strip()
        ]
        unknowns = [str(item) for item in description.get("unknowns") or [] if str(item).strip()]
        candidates.append({
            "segment_id": shot.get("shot_id"),
            "start_seconds": shot.get("start_seconds"),
            "end_seconds": shot.get("end_seconds"),
            "score": len(hits) * 5,
            "evidence_kind": "vision" if hits else "visual_unmatched",
            "matched_terms": hits,
            "reason": f"画面语义命中：{'、'.join(hits)}" if hits else "已有画面描述，但没有命中当前查询",
            "representative_frame": next((frame for frame in shot.get("frames") or [] if frame.get("selected_for_vision")), None),
            "vision_summary": description.get("summary") or "",
            "entities": entities,
            "actions": actions,
            "unknowns": unknowns,
        })
    candidates.sort(key=lambda item: (-int(item["score"]), float(item["start_seconds"] or 0)))
    return candidates[:max(1, min(20, int(limit)))]


def _terms(value: str) -> set[str]:
    compact = re.sub(r"\s+", "", value.lower())
    latin = set(re.findall(r"[a-z0-9][a-z0-9_-]{1,}", value.lower()))
    chinese = "".join(re.findall(r"[\u4e00-\u9fff]", compact))
    return latin | {chinese[index:index + 2] for index in range(max(0, len(chinese) - 1))}


def recommend_coarse_segments(index: dict[str, Any], query: str, *, limit: int = 6) -> list[dict[str, Any]]:
    query_terms = _terms(query)
    filename_terms = _terms(str((index.get("source") or {}).get("name") or ""))
    candidates = []
    for segment in index.get("segments") or []:
        transcript_terms = _terms(str(segment.get("transcript") or ""))
        transcript_hits = sorted(query_terms & transcript_terms)
        filename_hits = sorted(query_terms & filename_terms)
        score = len(transcript_hits) * 4 + len(filename_hits)
        evidence_kind = "transcript" if transcript_hits else "filename" if filename_hits else "visual_only"
        candidates.append({
            "segment_id": segment.get("id"),
            "start_seconds": segment.get("start_seconds"),
            "end_seconds": segment.get("end_seconds"),
            "score": score,
            "evidence_kind": evidence_kind,
            "matched_terms": transcript_hits or filename_hits,
            "reason": (
                f"台词命中：{'、'.join(transcript_hits)}" if transcript_hits
                else f"文件名命中：{'、'.join(filename_hits)}" if filename_hits
                else "只有镜头与代表帧证据，尚不能宣称语义匹配"
            ),
            "representative_frame": segment.get("representative_frame"),
            "transcript": segment.get("transcript") or "",
        })
    candidates.sort(key=lambda item: (-int(item["score"]), float(item["start_seconds"] or 0)))
    return candidates[:max(1, min(20, int(limit)))]
