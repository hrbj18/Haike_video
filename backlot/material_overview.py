"""Low-cost, evidence-first visual overviews for local video material.

Overview V1 deliberately creates a navigable local evidence map before any
remote vision call.  It is separate from the existing per-shot V2 index: the
two products have different frame budgets, evidence granularity and cache
contracts, and neither is allowed to rewrite the other.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import subprocess
from pathlib import Path
from typing import Any, Iterable

from PIL import Image, ImageDraw, ImageFilter, ImageOps, ImageStat

from backlot.media_index import MediaIndexError, media_content_fingerprint, probe_media


MATERIAL_OVERVIEW_VERSION = 1
OVERVIEW_POLICY_VERSION = "duration-activity-overview-v1"
CONTACT_SHEET_POLICY_VERSION = "contact-sheet-v1"
DETAIL_FRAME_POLICY_VERSION = "detail-frame-v1"
MAX_DETAIL_FRAMES = 12


class MaterialOverviewError(RuntimeError):
    pass


def _run(command: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(30, float(timeout)),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MaterialOverviewError(str(exc)) from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "本地概览抽帧失败")[-3000:]
        raise MaterialOverviewError(detail)
    return completed


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _stable_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _round_time(value: float) -> float:
    return round(max(0.0, float(value)), 3)


def _chapter_id(index: int) -> str:
    return f"CHAPTER-{index:02d}"


def _even_times(start: float, end: float, count: int) -> list[float]:
    """Return deterministic inclusive time anchors, preserving start and end."""
    if count <= 0:
        return []
    if count == 1:
        return [_round_time((start + end) / 2)]
    interval = max(0.0, end - start)
    return [_round_time(start + interval * index / (count - 1)) for index in range(count)]


def duration_policy(duration_seconds: float) -> dict[str, Any]:
    """Return the frozen V1 budget contract without inspecting media pixels."""
    duration = float(duration_seconds)
    if duration <= 0:
        raise MaterialOverviewError("素材时长无效，无法建立快速概览")
    if duration <= 45:
        budget = max(18, min(81, math.ceil(duration * 2)))
        chapter_count, grid = 1, (3, 3)
        kind = "continuous_short"
    elif duration <= 10 * 60:
        budget = 81
        chapter_count, grid = 1, (3, 3)
        kind = "stratified_medium"
    elif duration <= 60 * 60:
        budget = 96
        chapter_count, grid = 6, (4, 4)
        kind = "six_chapter_long"
    else:
        # Over one hour remains local-only until the user chooses chapters.
        # Ten-minute chapters are an index, not a claim of whole-video AI
        # understanding.  There is intentionally no remote budget here.
        chapter_count = max(1, math.ceil(duration / (10 * 60)))
        budget = chapter_count * 16
        grid = (4, 4)
        kind = "local_chapter_index"
    return {
        "policy_version": OVERVIEW_POLICY_VERSION,
        "duration_seconds": _round_time(duration),
        "budget_max": int(budget),
        "chapter_count": int(chapter_count),
        "grid_rows": grid[0],
        "grid_columns": grid[1],
        "kind": kind,
        "remote_whole_video_allowed": duration <= 60 * 60,
    }


def sampling_plan(duration_seconds: float, activity_candidates: Iterable[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Build a budget-bounded plan with protected coverage and activity slots.

    ``activity_candidates`` is intentionally only a local ranking signal.  The
    caller may supply scene, motion or keyframe observations; none of them can
    remove uniform anchors or increase the duration budget.
    """
    policy = duration_policy(duration_seconds)
    duration = float(policy["duration_seconds"])
    chapter_count = int(policy["chapter_count"])
    raw_candidates: list[dict[str, Any]] = []
    for raw in activity_candidates or []:
        if not isinstance(raw, dict):
            continue
        try:
            timestamp = float(raw.get("timestamp_seconds"))
            score = float(raw.get("activity_score", 0.0))
        except (TypeError, ValueError):
            continue
        if 0 < timestamp < duration:
            raw_candidates.append({
                "timestamp_seconds": _round_time(timestamp),
                "activity_score": max(0.0, score),
                "reason": str(raw.get("reason") or "activity_candidate")[:80],
            })
    raw_candidates.sort(key=lambda item: (-float(item["activity_score"]), float(item["timestamp_seconds"]), str(item["reason"])))

    chapters: list[dict[str, Any]] = []
    selected: list[dict[str, Any]] = []
    global_budget = int(policy["budget_max"])
    for chapter_index in range(chapter_count):
        start = duration * chapter_index / chapter_count
        end = duration * (chapter_index + 1) / chapter_count
        # Seek a little before EOF: an exact duration timestamp has no frame.
        # A fast input seek close to EOF can decode no frame, especially for
        # a short-GOP MP4 on Windows.  Keep a conservative decode margin
        # instead of requesting the container's exact declared duration.
        safe_end = max(start, end - min(0.5, max(0.25, (end - start) / 64)))
        if chapter_count == 1:
            chapter_budget = global_budget
        else:
            base, remainder = divmod(global_budget, chapter_count)
            chapter_budget = base + (1 if chapter_index < remainder else 0)
        anchor_budget = max(1, math.ceil(chapter_budget / 2))
        anchors = _even_times(start, safe_end, anchor_budget)
        chapter_id = _chapter_id(chapter_index + 1)
        chapter_rows: list[dict[str, Any]] = []
        for timestamp in anchors:
            row = {
                "timestamp_seconds": timestamp,
                "sampling_reason": "uniform_anchor",
                "protected_anchor": True,
                "chapter_id": chapter_id,
                "activity_score": 0.0,
            }
            selected.append(row)
            chapter_rows.append(row)

        information_budget = max(0, chapter_budget - len(anchors))
        span = max(0.01, end - start)
        minimum_gap = max(0.08, span / max(12, chapter_budget * 4))
        for candidate in raw_candidates:
            if len([item for item in chapter_rows if not item["protected_anchor"]]) >= information_budget:
                break
            timestamp = float(candidate["timestamp_seconds"])
            if not (start < timestamp < end):
                continue
            if any(abs(timestamp - float(row["timestamp_seconds"])) < minimum_gap for row in chapter_rows):
                continue
            row = {
                "timestamp_seconds": timestamp,
                "sampling_reason": str(candidate["reason"]),
                "protected_anchor": False,
                "chapter_id": chapter_id,
                "activity_score": round(float(candidate["activity_score"]), 4),
            }
            selected.append(row)
            chapter_rows.append(row)

        # In a static clip there can be no useful activity candidate.  The
        # positions remain explicit fallbacks and may later be deduplicated;
        # they are never silently turned into claims about an action.
        remaining = information_budget - len([item for item in chapter_rows if not item["protected_anchor"]])
        if remaining > 0:
            for timestamp in _even_times(start, safe_end, max(remaining + 2, 2))[1:-1]:
                if remaining <= 0:
                    break
                if any(abs(timestamp - float(row["timestamp_seconds"])) < minimum_gap for row in chapter_rows):
                    continue
                row = {
                    "timestamp_seconds": timestamp,
                    "sampling_reason": "uniform_fallback",
                    "protected_anchor": False,
                    "chapter_id": chapter_id,
                    "activity_score": 0.0,
                }
                selected.append(row)
                chapter_rows.append(row)
                remaining -= 1
        chapters.append({
            "chapter_id": chapter_id,
            "start_seconds": _round_time(start),
            "end_seconds": _round_time(end),
            "budget_max": chapter_budget,
            "anchor_budget": anchor_budget,
            "selected_requested_count": len(chapter_rows),
        })

    selected.sort(key=lambda item: (float(item["timestamp_seconds"]), not bool(item["protected_anchor"]), str(item["sampling_reason"])))
    for index, row in enumerate(selected, 1):
        row["request_id"] = f"REQUEST-{index:05d}"
    return {**policy, "chapters": chapters, "requested_frames": selected}


def _scene_activity_times(source: Path, ffmpeg: str, duration: float) -> list[dict[str, Any]]:
    completed = _run([
        ffmpeg, "-hide_banner", "-i", str(source), "-an", "-sn", "-dn",
        "-vf", "fps=2,scale=480:-2,select='gt(scene,0.30)',showinfo",
        "-vsync", "vfr", "-f", "null", "-",
    ], timeout=max(180, duration * 1.5))
    rows: list[dict[str, Any]] = []
    for match in re.finditer(r"pts_time:([0-9]+(?:\.[0-9]+)?)", completed.stderr or ""):
        value = _round_time(float(match.group(1)))
        if .04 < value < duration - .04 and (not rows or value - float(rows[-1]["timestamp_seconds"]) >= .25):
            rows.append({"timestamp_seconds": value, "activity_score": 4.0, "reason": "scene_change"})
    return rows


def _keyframe_activity_times(source: Path, ffprobe: str, duration: float) -> list[dict[str, Any]]:
    completed = _run([
        ffprobe, "-v", "error", "-skip_frame", "nokey", "-select_streams", "v:0",
        "-show_entries", "frame=best_effort_timestamp_time,pkt_dts_time", "-of", "json", str(source),
    ], timeout=max(180, duration * .4))
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise MaterialOverviewError("ffprobe 没有返回可解析的关键帧时间") from exc
    rows: list[dict[str, Any]] = []
    for raw in payload.get("frames") or []:
        if not isinstance(raw, dict):
            continue
        value = raw.get("best_effort_timestamp_time", raw.get("pkt_dts_time"))
        try:
            timestamp = _round_time(float(value))
        except (TypeError, ValueError):
            continue
        if .04 < timestamp < duration - .04 and (not rows or timestamp - float(rows[-1]["timestamp_seconds"]) >= .2):
            rows.append({"timestamp_seconds": timestamp, "activity_score": 1.0, "reason": "keyframe"})
    return rows


def _extract_frame(source: Path, ffmpeg: str, timestamp: float, target: Path) -> tuple[float, str]:
    """Fast-seek one JPEG and record FFmpeg's decoded PTS rather than guessing."""
    target.parent.mkdir(parents=True, exist_ok=True)
    completed = _run([
        ffmpeg, "-hide_banner", "-y", "-ss", f"{timestamp:.6f}", "-copyts", "-i", str(source),
        "-an", "-frames:v", "1", "-vf", "showinfo", "-pix_fmt", "yuvj420p", "-q:v", "3", str(target),
    ], timeout=120)
    values = [float(match.group(1)) for match in re.finditer(r"pts_time:([+-]?[0-9]+(?:\.[0-9]+)?)", completed.stderr or "")]
    if not target.is_file() or target.stat().st_size <= 0:
        raise MaterialOverviewError(f"无法提取 {timestamp:.3f} 秒的视频帧")
    if not values:
        raise MaterialOverviewError("FFmpeg 未返回抽取帧的实际 PTS，已停止以避免写入伪时间证据")
    return _round_time(values[-1]), _sha256(target)


def _dhash(path: Path) -> str:
    with Image.open(path) as image:
        gray = ImageOps.grayscale(image).resize((9, 8), Image.Resampling.LANCZOS)
        values = list(gray.get_flattened_data()) if hasattr(gray, "get_flattened_data") else list(gray.getdata())
    digest = 0
    for row in range(8):
        offset = row * 9
        for column in range(8):
            digest = (digest << 1) | int(values[offset + column + 1] > values[offset + column])
    return f"{digest:016x}"


def _hamming(left: str, right: str) -> int:
    return (int(left, 16) ^ int(right, 16)).bit_count()


def _image_metrics(path: Path) -> tuple[float, tuple[float, float, float]]:
    with Image.open(path) as image:
        image = image.convert("RGB")
        thumb = image.copy()
        thumb.thumbnail((160, 90), Image.Resampling.LANCZOS)
        color = ImageStat.Stat(thumb).mean
        edges = ImageOps.grayscale(thumb).filter(ImageFilter.FIND_EDGES)
        sharpness = ImageStat.Stat(edges).var[0]
    return round(float(sharpness), 4), tuple(round(float(value), 3) for value in color)


def _near_same(left: dict[str, Any], right: dict[str, Any]) -> bool:
    if str(left["source_frame_sha256"]) == str(right["source_frame_sha256"]):
        return True
    if _hamming(str(left["dhash"]), str(right["dhash"])) > 5:
        return False
    color_left = tuple(left.get("mean_rgb") or ())
    color_right = tuple(right.get("mean_rgb") or ())
    return len(color_left) == 3 and len(color_right) == 3 and sum(abs(float(a) - float(b)) for a, b in zip(color_left, color_right)) <= 18


def _deduplicate(frames: list[dict[str, Any]]) -> None:
    """Fail open: only information-layer frames can be excluded as duplicates."""
    selected: list[dict[str, Any]] = []
    for frame in frames:
        frame["selected_for_overview"] = True
        frame["duplicate_of_frame_id"] = None
        if frame.get("protected_anchor"):
            selected.append(frame)
            continue
        duplicate = next((previous for previous in reversed(selected) if _near_same(frame, previous)), None)
        if duplicate is not None:
            frame["selected_for_overview"] = False
            frame["duplicate_of_frame_id"] = duplicate["frame_id"]
        else:
            selected.append(frame)


def _label(draw: ImageDraw.ImageDraw, text: str, x: int, y: int, width: int) -> None:
    draw.rectangle((x, y, x + width, y + 25), fill=(15, 25, 35))
    draw.text((x + 5, y + 5), text, fill=(245, 245, 245))


def _contact_sheets(run_dir: Path, frames: list[dict[str, Any]], plan: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows, columns = int(plan["grid_rows"]), int(plan["grid_columns"])
    cell_capacity = rows * columns
    edge = 1536 if (rows, columns) == (3, 3) else 2048
    cell_width, cell_height = edge // columns, edge // rows
    selected = [frame for frame in frames if frame.get("selected_for_overview")]
    sheets: list[dict[str, Any]] = []
    cells: list[dict[str, Any]] = []
    sheet_number = 0
    by_chapter: dict[str, list[dict[str, Any]]] = {}
    for frame in selected:
        by_chapter.setdefault(str(frame["chapter_id"]), []).append(frame)
    for chapter in plan["chapters"]:
        chapter_id = str(chapter["chapter_id"])
        chapter_frames = by_chapter.get(chapter_id, [])
        for start in range(0, len(chapter_frames) or 1, cell_capacity):
            sheet_number += 1
            sheet_id = f"SHEET-{sheet_number:04d}"
            page = chapter_frames[start:start + cell_capacity]
            image = Image.new("RGB", (edge, edge), (245, 240, 227))
            draw = ImageDraw.Draw(image)
            page_cells: list[str] = []
            for index in range(cell_capacity):
                row, column = divmod(index, columns)
                x, y = column * cell_width, row * cell_height
                draw.rectangle((x, y, x + cell_width - 1, y + cell_height - 1), outline=(161, 145, 112), width=2)
                cell_id = f"{sheet_id}:R{row + 1}C{column + 1}"
                frame = page[index] if index < len(page) else None
                if frame is None:
                    draw.text((x + 12, y + 12), "EMPTY", fill=(126, 116, 94))
                    continue
                with Image.open(Path(str(frame["path"]))) as source:
                    fitted = ImageOps.contain(source.convert("RGB"), (cell_width - 14, cell_height - 40), Image.Resampling.LANCZOS)
                image.paste(fitted, (x + (cell_width - fitted.width) // 2, y + 28 + (cell_height - 40 - fitted.height) // 2))
                _label(draw, f"{sheet_id} / R{row + 1}C{column + 1} / {float(frame['actual_pts_seconds']):09.3f}", x, y, cell_width)
                frame["cell_id"] = cell_id
                cells.append({
                    "cell_id": cell_id,
                    "sheet_id": sheet_id,
                    "chapter_id": chapter_id,
                    "frame_id": frame["frame_id"],
                    "actual_pts_seconds": frame["actual_pts_seconds"],
                    "source_frame_sha256": frame["source_frame_sha256"],
                })
                page_cells.append(cell_id)
            target = run_dir / "contact-sheets" / f"{sheet_id}.jpg"
            target.parent.mkdir(parents=True, exist_ok=True)
            image.save(target, format="JPEG", quality=90, optimize=True)
            sheets.append({
                "sheet_id": sheet_id,
                "chapter_id": chapter_id,
                "path": str(target.resolve()),
                "sha256": _sha256(target),
                "rows": rows,
                "columns": columns,
                "cell_ids": page_cells,
            })
    return sheets, cells


def validate_material_overview_index(payload: dict[str, Any]) -> None:
    if not isinstance(payload, dict) or payload.get("version") != MATERIAL_OVERVIEW_VERSION:
        raise MaterialOverviewError("快速概览索引版本无效")
    if payload.get("status") not in {"sheets_ready", "overview_completed", "completed", "overview_completed_detail_failed"}:
        raise MaterialOverviewError("快速概览索引状态无效")
    source = payload.get("source") if isinstance(payload.get("source"), dict) else {}
    if not source.get("fingerprint") or not source.get("path"):
        raise MaterialOverviewError("快速概览缺少素材身份")
    frames = payload.get("frames") if isinstance(payload.get("frames"), list) else []
    frame_ids = {str(frame.get("frame_id")) for frame in frames if isinstance(frame, dict)}
    if len(frame_ids) != len(frames) or not frame_ids:
        raise MaterialOverviewError("快速概览帧编号无效")
    cells = payload.get("cells") if isinstance(payload.get("cells"), list) else []
    seen_cells: set[str] = set()
    for cell in cells:
        if not isinstance(cell, dict) or not str(cell.get("cell_id") or "") or str(cell.get("frame_id") or "") not in frame_ids:
            raise MaterialOverviewError("快速概览单元格映射无效")
        if str(cell["cell_id"]) in seen_cells:
            raise MaterialOverviewError("快速概览单元格编号重复")
        seen_cells.add(str(cell["cell_id"]))
    for frame in frames:
        if frame.get("selected_for_overview") and str(frame.get("frame_id")) not in {str(cell.get("frame_id")) for cell in cells}:
            raise MaterialOverviewError("入选概览帧缺少联系表单元格")


def build_material_overview_index(
    source: Path,
    output_dir: Path,
    *,
    ffmpeg: str,
    ffprobe: str,
) -> dict[str, Any]:
    """Build or reuse the local-only overview evidence before any AI call."""
    source = source.resolve()
    if not source.is_file():
        raise MaterialOverviewError("待建立快速概览的素材不存在")
    try:
        probe = probe_media(source, ffprobe)
        fingerprint = media_content_fingerprint(source)
    except MediaIndexError as exc:
        raise MaterialOverviewError(str(exc)) from exc
    duration = float(probe["duration_seconds"])
    policy = duration_policy(duration)
    config = {
        "overview_policy_version": OVERVIEW_POLICY_VERSION,
        "contact_sheet_policy_version": CONTACT_SHEET_POLICY_VERSION,
        "detail_frame_policy_version": DETAIL_FRAME_POLICY_VERSION,
        "duration_policy": policy,
    }
    signature = _stable_hash({"source_fingerprint": fingerprint, "config": config})
    run_dir = output_dir / "overview-v1" / signature[:16]
    index_path = run_dir / "material-overview-index.json"
    if index_path.is_file():
        try:
            cached = json.loads(index_path.read_text(encoding="utf-8"))
            if (
                str((cached.get("source") or {}).get("fingerprint") or "") == fingerprint
                and str(cached.get("signature") or "") == signature
            ):
                validate_material_overview_index(cached)
                cached["cache_hit"] = True
                return cached
        except (OSError, json.JSONDecodeError, MaterialOverviewError):
            # A damaged cache must not be trusted; rebuild only this new V1 run.
            pass

    activity = _scene_activity_times(source, ffmpeg, duration) if duration <= 10 * 60 else _keyframe_activity_times(source, ffprobe, duration)
    plan = sampling_plan(duration, activity)
    frames_dir = run_dir / "overview-frames"
    frames: list[dict[str, Any]] = []
    for number, request in enumerate(plan["requested_frames"], 1):
        frame_id = f"FRAME-{number:05d}"
        target = frames_dir / f"{frame_id}.jpg"
        actual_pts, source_sha = _extract_frame(source, ffmpeg, float(request["timestamp_seconds"]), target)
        sharpness, mean_rgb = _image_metrics(target)
        frames.append({
            "frame_id": frame_id,
            "request_id": request["request_id"],
            "chapter_id": request["chapter_id"],
            "requested_timestamp_seconds": request["timestamp_seconds"],
            "actual_pts_seconds": actual_pts,
            "sampling_reason": request["sampling_reason"],
            "protected_anchor": bool(request["protected_anchor"]),
            "activity_score": request["activity_score"],
            "path": str(target.resolve()),
            "source_frame_sha256": source_sha,
            "dhash": _dhash(target),
            "sharpness": sharpness,
            "mean_rgb": mean_rgb,
        })
    _deduplicate(frames)
    sheets, cells = _contact_sheets(run_dir, frames, plan)
    payload: dict[str, Any] = {
        "version": MATERIAL_OVERVIEW_VERSION,
        "status": "sheets_ready",
        "source": {"path": str(source), "name": source.name, "fingerprint": fingerprint},
        "signature": signature,
        "config": config,
        "probe": probe,
        "sampling": {key: value for key, value in plan.items() if key != "requested_frames"},
        "activity_candidates": activity,
        "frames": frames,
        "sheets": sheets,
        "cells": cells,
        "overview": {"status": "not_requested", "chapters": [], "vision": {}},
        "detail": {"status": "not_requested", "candidates": [], "frames": [], "vision": {}},
        "index_path": str(index_path.resolve()),
        "cache_hit": False,
    }
    validate_material_overview_index(payload)
    _write_json(index_path, payload)
    return payload


def write_material_overview_index(index: dict[str, Any]) -> dict[str, Any]:
    validate_material_overview_index(index)
    path = Path(str(index.get("index_path") or ""))
    if not path.is_absolute():
        raise MaterialOverviewError("快速概览索引路径无效")
    _write_json(path, index)
    return index


def cell_map(index: dict[str, Any]) -> dict[str, dict[str, Any]]:
    frames = {str(frame.get("frame_id")): frame for frame in index.get("frames") or [] if isinstance(frame, dict)}
    output: dict[str, dict[str, Any]] = {}
    for cell in index.get("cells") or []:
        if not isinstance(cell, dict):
            continue
        frame = frames.get(str(cell.get("frame_id") or ""))
        if frame is not None:
            output[str(cell.get("cell_id") or "")] = {**cell, "frame": frame}
    return output


def prepare_detail_frames(index: dict[str, Any], *, ffmpeg: str, maximum: int = MAX_DETAIL_FRAMES) -> dict[str, Any]:
    """Extract at most the frozen detail budget, retaining the overview cache."""
    maximum = max(1, min(MAX_DETAIL_FRAMES, int(maximum)))
    overview = index.get("overview") if isinstance(index.get("overview"), dict) else {}
    candidates = overview.get("detail_candidates") if isinstance(overview.get("detail_candidates"), list) else []
    mapping = cell_map(index)
    selected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        cell_id = str(candidate.get("cell_id") or "")
        if cell_id in seen or cell_id not in mapping:
            continue
        seen.add(cell_id)
        selected.append({"cell_id": cell_id, "reason": str(candidate.get("reason") or "need_detail")[:120]})
        if len(selected) >= maximum:
            break
    detail = index.setdefault("detail", {})
    detail["candidates"] = selected
    if not selected:
        detail.update({"status": "completed", "frames": [], "vision": {"request_count": 0, "image_count": 0}})
        write_material_overview_index(index)
        return index
    source = Path(str((index.get("source") or {}).get("path") or ""))
    if not source.is_file():
        raise MaterialOverviewError("原始素材已缺失，无法执行精细化复核")
    run_dir = Path(str(index["index_path"])).parent
    frames: list[dict[str, Any]] = []
    for number, candidate in enumerate(selected, 1):
        cell = mapping[candidate["cell_id"]]
        frame = cell["frame"]
        timestamp = float(frame["actual_pts_seconds"])
        detail_id = f"DETAIL-{number:03d}"
        target = run_dir / "detail-frames" / f"{detail_id}.jpg"
        actual_pts, digest = _extract_frame(source, ffmpeg, timestamp, target)
        frames.append({
            "detail_id": detail_id,
            "cell_id": candidate["cell_id"],
            "frame_id": frame["frame_id"],
            "reason": candidate["reason"],
            "requested_timestamp_seconds": timestamp,
            "actual_pts_seconds": actual_pts,
            "path": str(target.resolve()),
            "source_frame_sha256": digest,
        })
    detail.update({"status": "frames_ready", "frames": frames, "vision": {}})
    write_material_overview_index(index)
    return index
