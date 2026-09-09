"""Human review overlay for an immutable outdoor-interaction model index.

The source index is evidence, not an editable project plan.  This module keeps
manual range/status decisions in a separate CAS-protected document and can
create a browser-friendly media proxy without touching the source file.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
from typing import Any

from backlot.media_index import media_content_fingerprint


VERSION = "interaction-review-v1"
PROXY_VERSION = "interaction-browser-proxy-v1"
MIN_EVENT_SECONDS = 0.4
MAX_MERGE_GAP_SECONDS = 30.0
MAX_HISTORY = 50
STATUSES = {"pending", "kept", "discarded"}


class InteractionReviewError(ValueError):
    pass


class InteractionReviewConflict(InteractionReviewError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _atomic_write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _number(value: Any, *, lower: float, upper: float, label: str) -> float:
    if isinstance(value, bool):
        raise InteractionReviewError(f"{label}必须是有效数字")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise InteractionReviewError(f"{label}必须是有效数字") from exc
    if not math.isfinite(result) or result < lower or result > upper:
        raise InteractionReviewError(f"{label}超出原片范围")
    return round(result, 3)


def _source_contract(index: dict[str, Any]) -> dict[str, Any]:
    try:
        duration = _number(index["duration"], lower=MIN_EVENT_SECONDS, upper=3600 * 24, label="原片时长")
        fingerprint = str(index["source"]["fingerprint"])
        signature = str(index["signature"])
    except (KeyError, TypeError) as exc:
        raise InteractionReviewError("互动索引缺少源素材身份") from exc
    if not fingerprint or not signature:
        raise InteractionReviewError("互动索引缺少源素材身份")
    return {"fingerprint": fingerprint, "index_signature": signature, "duration": duration}


def _event_from_index(event: dict[str, Any], sequence: int, duration: float) -> dict[str, Any]:
    start = _number(event.get("start"), lower=0, upper=duration, label="事件开始时间")
    end = _number(event.get("end"), lower=0, upper=duration, label="事件结束时间")
    if end - start < MIN_EVENT_SECONDS:
        raise InteractionReviewError("互动候选不足 0.4 秒，无法进入人工审核")
    event_id = str(event.get("event_id") or "").strip()
    group_id = str(event.get("group_id") or "").strip()
    if not event_id or not group_id:
        raise InteractionReviewError("互动候选缺少事件或群组编号")
    return {
        "review_event_id": f"R{sequence:04d}",
        "source_event_ids": [event_id],
        "group_id": group_id,
        "participants": str(event.get("participants") or "未命名互动对象")[:240],
        "summary": str(event.get("summary") or "待人工补充")[:500],
        "start": start,
        "end": end,
        "status": "pending",
        "confidence": event.get("confidence"),
        "score": event.get("score"),
        "completeness": event.get("completeness"),
        "requires_review": bool(event.get("requires_review", True)),
        "recommend_reason": str(event.get("recommend_reason") or "")[:500],
        "boundary_reason": str(event.get("boundary_reason") or "")[:500],
        "evidence_frame_ids": list(dict.fromkeys(str(item) for item in event.get("evidence_frame_ids") or [])),
        "utterance_ids": list(dict.fromkeys(str(item) for item in event.get("utterance_ids") or [])),
        "highlights": deepcopy(event.get("highlights") or []),
        "unknowns": [str(item)[:240] for item in event.get("unknowns") or []],
    }


def initialize_review(index: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(index, dict) or index.get("status") != "completed":
        raise InteractionReviewError("只有已完成的互动索引可以进入人工审核")
    source = _source_contract(index)
    events = [_event_from_index(event, i, source["duration"]) for i, event in enumerate(index.get("events") or [], 1)]
    return {
        "version": VERSION,
        "revision": 0,
        "source": source,
        "events": events,
        "next_event_sequence": len(events) + 1,
        "history": [],
        "action_log": [],
        "created_at": _now(),
        "updated_at": _now(),
    }


def validate_review(review: dict[str, Any], index: dict[str, Any]) -> None:
    if not isinstance(review, dict) or review.get("version") != VERSION:
        raise InteractionReviewError("互动审核文件版本无效")
    if review.get("source") != _source_contract(index):
        raise InteractionReviewError("原片或互动索引已经变化，请重新建立人工审核目录")
    duration = review["source"]["duration"]
    identifiers: set[str] = set()
    for event in review.get("events") or []:
        identifier = str(event.get("review_event_id") or "")
        if not identifier or identifier in identifiers:
            raise InteractionReviewError("人工审核事件编号无效或重复")
        identifiers.add(identifier)
        if event.get("status") not in STATUSES:
            raise InteractionReviewError("人工审核状态无效")
        start = _number(event.get("start"), lower=0, upper=duration, label="事件开始时间")
        end = _number(event.get("end"), lower=0, upper=duration, label="事件结束时间")
        if end - start < MIN_EVENT_SECONDS:
            raise InteractionReviewError("人工审核事件不足 0.4 秒")
        if not str(event.get("group_id") or "") or not event.get("source_event_ids"):
            raise InteractionReviewError("人工审核事件缺少来源身份")
    try:
        revision = int(review.get("revision"))
        next_sequence = int(review.get("next_event_sequence"))
    except (TypeError, ValueError) as exc:
        raise InteractionReviewError("互动审核版本号无效") from exc
    if revision < 0 or next_sequence < 1:
        raise InteractionReviewError("互动审核版本号无效")
    if not isinstance(review.get("history"), list) or not isinstance(review.get("action_log"), list):
        raise InteractionReviewError("互动审核历史格式无效")
    for snapshot in review["history"]:
        if not isinstance(snapshot, dict) or not isinstance(snapshot.get("events"), list):
            raise InteractionReviewError("互动审核撤销快照已损坏")
        try:
            if int(snapshot.get("next_event_sequence")) < 1:
                raise ValueError
        except (TypeError, ValueError) as exc:
            raise InteractionReviewError("互动审核撤销快照已损坏") from exc


def write_review(path: Path, review: dict[str, Any], index: dict[str, Any]) -> dict[str, Any]:
    validate_review(review, index)
    _atomic_write(path, review)
    return review


def read_review(path: Path, index: dict[str, Any]) -> dict[str, Any]:
    try:
        review = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InteractionReviewError("互动审核文件不存在或已损坏") from exc
    validate_review(review, index)
    return review


def _find_event(review: dict[str, Any], event_id: str) -> dict[str, Any]:
    event = next((item for item in review["events"] if item.get("review_event_id") == event_id), None)
    if event is None:
        raise InteractionReviewError("要修改的互动事件不存在，请刷新页面")
    return event


def _allocate_id(review: dict[str, Any]) -> str:
    sequence = int(review["next_event_sequence"])
    review["next_event_sequence"] = sequence + 1
    return f"R{sequence:04d}"


def _snapshot(review: dict[str, Any]) -> dict[str, Any]:
    return {"events": deepcopy(review["events"]), "next_event_sequence": review["next_event_sequence"]}


def _log_payload(action: str, payload: dict[str, Any]) -> dict[str, Any]:
    if action in {"set_status", "set_range", "split"}:
        result = {"event_id": str(payload.get("event_id") or "")}
    elif action == "merge":
        result = {"event_ids": [str(item) for item in payload.get("event_ids") or []]}
    else:
        result = {}
    if action == "set_status":
        result["status"] = str(payload.get("status") or "")
    return result


def apply_action(review: dict[str, Any], index: dict[str, Any], action: str,
                 payload: dict[str, Any], expected_revision: Any) -> dict[str, Any]:
    validate_review(review, index)
    try:
        expected = int(expected_revision)
    except (TypeError, ValueError) as exc:
        raise InteractionReviewError("保存审核结果时必须提供当前版本号") from exc
    if expected != int(review["revision"]):
        raise InteractionReviewConflict("互动目录已在其他页面更新，请刷新后继续")
    if not isinstance(payload, dict):
        raise InteractionReviewError("互动审核操作格式无效")
    action = str(action or "")
    if action == "undo":
        if not review["history"]:
            raise InteractionReviewError("没有可以撤销的人工操作")
        previous = review["history"].pop()
        review["events"] = previous["events"]
        review["next_event_sequence"] = previous["next_event_sequence"]
    else:
        if action not in {"set_range", "set_status", "merge", "split"}:
            raise InteractionReviewError("不支持的互动审核操作")
        before = _snapshot(review)
        duration = review["source"]["duration"]
        if action == "set_range":
            event = _find_event(review, str(payload.get("event_id") or ""))
            start = _number(payload.get("start"), lower=0, upper=duration, label="事件开始时间")
            end = _number(payload.get("end"), lower=0, upper=duration, label="事件结束时间")
            if end - start < MIN_EVENT_SECONDS:
                raise InteractionReviewError("人工片段至少需要 0.4 秒")
            event.update({"start": start, "end": end, "requires_review": True})
        elif action == "set_status":
            event = _find_event(review, str(payload.get("event_id") or ""))
            status = str(payload.get("status") or "")
            if status not in STATUSES:
                raise InteractionReviewError("互动审核状态只能是待审核、保留或弃用")
            event["status"] = status
        elif action == "merge":
            event_ids = list(dict.fromkeys(str(item) for item in payload.get("event_ids") or []))
            if len(event_ids) < 2:
                raise InteractionReviewError("至少选择两个事件才能合并")
            selected = sorted((_find_event(review, item) for item in event_ids), key=lambda item: item["start"])
            groups = {item["group_id"] for item in selected}
            if len(groups) != 1:
                raise InteractionReviewError("只有同一群组的相邻事件可以合并")
            ordered_ids = [item["review_event_id"] for item in sorted(review["events"], key=lambda item: item["start"])]
            positions = sorted(ordered_ids.index(item) for item in event_ids)
            if positions != list(range(positions[0], positions[-1] + 1)):
                raise InteractionReviewError("只能合并目录中相邻的同群组事件")
            for left, right in zip(selected, selected[1:]):
                gap = right["start"] - left["end"]
                if gap < 0:
                    raise InteractionReviewError("所选事件时间重叠，需先修正边界")
                if gap > MAX_MERGE_GAP_SECONDS:
                    raise InteractionReviewError("所选事件间隔超过 30 秒，不能视作同一段连续互动")
            merged = deepcopy(selected[0])
            merged.update({
                "review_event_id": _allocate_id(review),
                "source_event_ids": list(dict.fromkeys(item for row in selected for item in row["source_event_ids"])),
                "start": selected[0]["start"],
                "end": selected[-1]["end"],
                "status": selected[0]["status"] if len({item["status"] for item in selected}) == 1 else "pending",
                "summary": "；".join(dict.fromkeys(item["summary"] for item in selected))[:500],
                "evidence_frame_ids": list(dict.fromkeys(item for row in selected for item in row.get("evidence_frame_ids") or [])),
                "utterance_ids": list(dict.fromkeys(item for row in selected for item in row.get("utterance_ids") or [])),
                "highlights": [deepcopy(item) for row in selected for item in row.get("highlights") or []],
                "unknowns": list(dict.fromkeys(item for row in selected for item in row.get("unknowns") or [])),
                "requires_review": True,
            })
            selected_ids = set(event_ids)
            review["events"] = [item for item in review["events"] if item["review_event_id"] not in selected_ids] + [merged]
        else:
            event = _find_event(review, str(payload.get("event_id") or ""))
            split = _number(payload.get("split_seconds"), lower=0, upper=duration, label="拆分时间")
            if split - event["start"] < MIN_EVENT_SECONDS or event["end"] - split < MIN_EVENT_SECONDS:
                raise InteractionReviewError("拆分后两侧都必须至少保留 0.4 秒")
            left, right = deepcopy(event), deepcopy(event)
            left.update({"review_event_id": _allocate_id(review), "end": split, "requires_review": True})
            right.update({"review_event_id": _allocate_id(review), "start": split, "requires_review": True})
            position = review["events"].index(event)
            review["events"][position:position + 1] = [left, right]
        review["history"].append(before)
        review["history"] = review["history"][-MAX_HISTORY:]
    review["events"] = sorted(review["events"], key=lambda item: (item["start"], item["end"], item["review_event_id"]))
    review["revision"] = int(review["revision"]) + 1
    review["updated_at"] = _now()
    review["action_log"].append({"action": action, "at": review["updated_at"], **_log_payload(action, payload)})
    review["action_log"] = review["action_log"][-200:]
    validate_review(review, index)
    return review


def confirmed_catalog(review: dict[str, Any], index: dict[str, Any]) -> dict[str, Any]:
    validate_review(review, index)
    events = []
    for item in sorted(review["events"], key=lambda row: row["start"]):
        if item["status"] != "kept":
            continue
        events.append({key: deepcopy(item[key]) for key in (
            "review_event_id", "source_event_ids", "group_id", "participants", "summary",
            "start", "end", "evidence_frame_ids", "utterance_ids",
        )})
    return {
        "version": VERSION,
        "review_revision": review["revision"],
        "source": deepcopy(review["source"]),
        "events": events,
        "notice": "人工确认目录尚未裁切媒体，也尚未采用到项目片段或成片。",
    }


def _run(command: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                                   errors="replace", timeout=max(30.0, timeout), check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise InteractionReviewError("浏览器审核代理生成失败，请检查 FFmpeg 是否可用") from exc
    if completed.returncode != 0:
        raise InteractionReviewError("浏览器审核代理生成失败，请检查原片编码和本机 FFmpeg")
    return completed


def _probe(path: Path, ffprobe: str) -> dict[str, Any]:
    completed = _run([
        ffprobe, "-v", "error", "-show_entries",
        "format=duration,format_name:stream=codec_type,codec_name,pix_fmt,width,height",
        "-of", "json", str(path),
    ], timeout=120)
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise InteractionReviewError("ffprobe 无法验证浏览器审核代理") from exc


def _faststart(path: Path) -> bool:
    with path.open("rb") as handle:
        head = handle.read(min(path.stat().st_size, 8 * 1024 * 1024))
    moov, mdat = head.find(b"moov"), head.find(b"mdat")
    return moov >= 0 and (mdat < 0 or moov < mdat)


def build_browser_proxy(source: Path, output_dir: Path, *, ffmpeg: str, ffprobe: str,
                        source_fingerprint: str) -> dict[str, Any]:
    """Create or reuse a web-review MP4; never edits or replaces ``source``."""
    source = source.resolve()
    source_bytes = source.stat().st_size
    if media_content_fingerprint(source) != source_fingerprint:
        raise InteractionReviewError("原片指纹已变化，已停止生成审核代理")
    source_probe = _probe(source, ffprobe)
    source_video = next((row for row in source_probe.get("streams") or [] if row.get("codec_type") == "video"), None)
    source_audio = next((row for row in source_probe.get("streams") or [] if row.get("codec_type") == "audio"), None)
    source_format = str((source_probe.get("format") or {}).get("format_name") or "")
    if (
        source_video
        and source_video.get("codec_name") == "h264"
        and source_video.get("pix_fmt") == "yuv420p"
        and (not source_audio or source_audio.get("codec_name") == "aac")
        and any(name in source_format.split(",") for name in ("mov", "mp4", "m4a", "3gp", "3g2", "mj2"))
    ):
        return {
            "status": "source_compatible", "path": str(source), "manifest_path": None,
            "duration": round(float((source_probe.get("format") or {}).get("duration") or 0), 3),
            "width": int(source_video.get("width") or 0), "height": int(source_video.get("height") or 0),
            "video_codec": "h264", "pixel_format": "yuv420p",
            "audio_codec": "aac" if source_audio else None, "faststart": None, "cache_hit": True,
        }
    contract = {"version": PROXY_VERSION, "source_fingerprint": source_fingerprint,
                "longest_edge": 1280, "video": "h264/yuv420p/crf25", "audio": "aac/96k"}
    directory = output_dir.resolve() / "browser-proxy" / _digest(contract)[:20]
    path, manifest_path = directory / "review.mp4", directory / "manifest.json"
    if manifest_path.is_file() and path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            manifest = {}
        if manifest.get("contract") == contract and manifest.get("status") == "completed":
            return {**manifest, "path": str(path), "manifest_path": str(manifest_path), "cache_hit": True}
    directory.mkdir(parents=True, exist_ok=True)
    temporary = directory / ".review.tmp.mp4"
    if temporary.exists():
        temporary.unlink()
    try:
        source_duration = float((source_probe.get("format") or {}).get("duration") or 0)
    except (TypeError, ValueError) as exc:
        raise InteractionReviewError("原片时长无效，无法生成审核代理") from exc
    has_audio = any(row.get("codec_type") == "audio" for row in source_probe.get("streams") or [])
    # libx264 requires both dimensions to be even.  The fixed axis is rounded
    # down explicitly; FFmpeg's -2 keeps the proportional axis even as well.
    scale = "scale='if(gte(iw,ih),trunc(min(1280,iw)/2)*2,-2)':'if(gte(iw,ih),-2,trunc(min(1280,ih)/2)*2)'"
    command = [ffmpeg, "-hide_banner", "-y", "-i", str(source), "-map", "0:v:0", "-map", "0:a?",
               "-vf", scale, "-c:v", "libx264", "-preset", "veryfast", "-crf", "25",
               "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", str(temporary)]
    try:
        _run(command, timeout=max(180, source_duration * 4))
        probe = _probe(temporary, ffprobe)
        video = next((row for row in probe.get("streams") or [] if row.get("codec_type") == "video"), None)
        audio = next((row for row in probe.get("streams") or [] if row.get("codec_type") == "audio"), None)
        duration = float((probe.get("format") or {}).get("duration") or 0)
        if not video or video.get("codec_name") != "h264" or video.get("pix_fmt") != "yuv420p":
            raise InteractionReviewError("审核代理编码不是浏览器兼容的 H.264/yuv420p")
        width, height = int(video.get("width") or 0), int(video.get("height") or 0)
        if width <= 0 or height <= 0 or width % 2 or height % 2 or max(width, height) > 1280:
            raise InteractionReviewError("审核代理尺寸不符合浏览器播放合同")
        if has_audio and (not audio or audio.get("codec_name") != "aac"):
            raise InteractionReviewError("审核代理没有保留为 AAC 音轨")
        if abs(duration - source_duration) > 0.25:
            raise InteractionReviewError("审核代理与原片时长漂移超过 0.25 秒")
        if not _faststart(temporary):
            raise InteractionReviewError("审核代理未启用 faststart，无法稳定渐进播放")
        temporary.replace(path)
        if source.stat().st_size != source_bytes or media_content_fingerprint(source) != source_fingerprint:
            raise InteractionReviewError("代理生成期间原片发生变化，已停止登记")
        manifest = {"status": "completed", "contract": contract, "duration": round(duration, 3),
                    "width": width, "height": height, "video_codec": "h264", "pixel_format": "yuv420p",
                    "audio_codec": "aac" if audio else None, "faststart": True, "created_at": _now()}
        _atomic_write(manifest_path, manifest)
        return {**manifest, "path": str(path), "manifest_path": str(manifest_path), "cache_hit": False}
    finally:
        if temporary.exists():
            temporary.unlink()
