"""Render hook-led interaction plans with speed-aware audio/video timing."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
from typing import Any, Callable

from backlot.material_interaction_render import (
    InteractionRenderError,
    _faststart,
    _render_contract,
    _run,
    probe_media,
)
from backlot.material_interaction_second_pass import validate_second_pass_plan
from backlot.media_index import media_content_fingerprint


VERSION = "interaction-second-pass-render-v1"


class InteractionSecondPassRenderError(RuntimeError):
    pass


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
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


def _filters(occurrences: list[dict[str, Any]], contract: dict[str, Any], has_audio: bool) -> tuple[str, list[str], float]:
    pieces = []
    video_inputs = []
    audio_inputs = []
    expected_duration = 0.0
    fps = f"{contract['fps']:.6f}".rstrip("0").rstrip(".")
    for index, row in enumerate(occurrences):
        start, end, speed = float(row["source_start"]), float(row["source_end"]), float(row["speed"])
        expected_duration += (end - start) / speed
        pieces.append(
            f"[0:v]trim=start={start:.6f}:end={end:.6f},setpts=(PTS-STARTPTS)/{speed:.6f},"
            f"scale={contract['width']}:{contract['height']}:flags=lanczos,setsar=1[v{index}]"
        )
        video_inputs.append(f"[v{index}]")
        if has_audio:
            pieces.append(
                f"[0:a]atrim=start={start:.6f}:end={end:.6f},asetpts=PTS-STARTPTS,"
                "aresample=48000:async=0:first_pts=0,aformat=sample_rates=48000:channel_layouts=stereo,"
                f"atempo={speed:.6f}[a{index}]"
            )
            audio_inputs.append(f"[a{index}]")
    pieces.append("".join(video_inputs) + f"concat=n={len(occurrences)}:v=1:a=0[vcat]")
    pieces.append(f"[vcat]fps={fps},trim=duration={expected_duration:.6f},setpts=PTS-STARTPTS[vout]")
    if has_audio:
        pieces.append("".join(audio_inputs) + f"concat=n={len(occurrences)}:v=0:a=1[acat]")
        pieces.append(f"[acat]atrim=duration={expected_duration:.6f},asetpts=PTS-STARTPTS[aout]")
    mappings = ["-map", "[vout]"] + (["-map", "[aout]"] if has_audio else [])
    return ";".join(pieces), mappings, expected_duration


def _stream_duration(stream: dict[str, Any] | None) -> float | None:
    if not isinstance(stream, dict):
        return None
    try:
        value = float(stream.get("duration"))
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def render_second_pass_candidate(
    source: Path,
    plan: dict[str, Any],
    output_root: Path,
    *,
    ffmpeg: str,
    ffprobe: str,
    longest_edge: int = 1280,
    timeout: float = 900,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, Any]:
    validate_second_pass_plan(plan)
    source = source.resolve()
    expected_fingerprint = str((plan.get("source") or {}).get("fingerprint") or "")
    if not source.is_file() or media_content_fingerprint(source) != expected_fingerprint:
        raise InteractionSecondPassRenderError("原片不存在或指纹已变化，已停止二次剪辑")
    occurrences = plan.get("occurrences") or []
    if not occurrences:
        raise InteractionSecondPassRenderError("二次剪辑清单没有可用片段")
    try:
        source_probe = probe_media(source, ffprobe, runner=runner)
        contract = _render_contract(source_probe, longest_edge)
    except InteractionRenderError as exc:
        raise InteractionSecondPassRenderError(str(exc)) from exc
    has_audio = contract["audio"] is not None
    frozen = {
        "version": VERSION, "plan_id": plan["plan_id"], "plan_revision": plan["revision"],
        "source_fingerprint": expected_fingerprint, "occurrences": occurrences,
        "timeline_mapping": plan.get("timeline_mapping"), "subtitle_cues": plan.get("subtitle_cues"),
        "contract": contract,
    }
    signature = _digest(frozen)
    directory = output_root.resolve() / str(plan["plan_id"]) / signature[:20]
    output_path, manifest_path = directory / "preview.mp4", directory / "manifest.json"
    if output_path.is_file() and manifest_path.is_file():
        try:
            previous = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            previous = {}
        if previous.get("signature") == signature and (previous.get("qa") or {}).get("status") == "passed":
            return {**previous, "path": str(output_path), "manifest_path": str(manifest_path), "cache_hit": True}
    directory.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(prefix=".preview.", suffix=".mp4", dir=directory)
    os.close(handle)
    temporary = Path(temporary_name)
    filter_graph, mappings, expected_duration = _filters(occurrences, contract, has_audio)
    command = [
        ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-y", "-i", str(source),
        "-filter_complex", filter_graph, *mappings,
        "-c:v", "libx264", "-preset", "fast", "-crf", "25", "-pix_fmt", "yuv420p", "-threads", "4",
    ]
    if has_audio:
        command.extend(["-c:a", "aac", "-b:a", "128k", "-ar", "48000"])
    else:
        command.append("-an")
    command.extend(["-movflags", "+faststart", str(temporary)])
    try:
        try:
            _run(command, timeout=timeout, runner=runner)
            output_probe = probe_media(temporary, ffprobe, runner=runner)
        except InteractionRenderError as exc:
            raise InteractionSecondPassRenderError("二次剪辑生成失败，原片、语义清单和旧预览均已保留") from exc
        video = next((row for row in output_probe["streams"] if row.get("codec_type") == "video"), {})
        audio = next((row for row in output_probe["streams"] if row.get("codec_type") == "audio"), None)
        actual = float((output_probe.get("format") or {}).get("duration") or 0)
        tolerance = max(2.0 / float(contract["fps"]), .05)
        video_duration = _stream_duration(video)
        audio_duration = _stream_duration(audio)
        av_tail_delta = abs(video_duration - audio_duration) if video_duration is not None and audio_duration is not None else 0.0
        checks = {
            "video_codec": video.get("codec_name") == "h264",
            "pixel_format": video.get("pix_fmt") == "yuv420p",
            "dimensions": int(video.get("width") or 0) == contract["width"] and int(video.get("height") or 0) == contract["height"],
            "audio_contract": (audio is not None and audio.get("codec_name") == "aac") if has_audio else audio is None,
            "duration": abs(actual - expected_duration) <= tolerance,
            "audio_video_tail": av_tail_delta <= tolerance,
            "faststart": _faststart(temporary),
        }
        if not all(checks.values()):
            failed = "、".join(key for key, passed in checks.items() if not passed)
            raise InteractionSecondPassRenderError(
                f"二次剪辑 QA 未通过（{failed}; actual={actual:.6f}, expected={expected_duration:.6f}, av_tail={av_tail_delta:.6f}）"
            )
        for attempt in range(12):
            try:
                os.replace(temporary, output_path)
                break
            except PermissionError:
                if attempt == 11:
                    raise
                time.sleep(.01 * (attempt + 1))
    finally:
        temporary.unlink(missing_ok=True)
    manifest = {
        **frozen, "signature": signature, "status": "pending_review",
        "path": str(output_path), "manifest_path": str(manifest_path),
        "source_duration": round(float(plan["source_duration"]), 3),
        "body_source_duration": plan.get("body_source_duration"),
        "hook_source_duration": plan.get("hook_source_duration"),
        "output_duration": round(actual, 3),
        "removed_source_seconds": plan.get("removed_source_seconds"),
        "qa": {
            "status": "passed", "checks": checks,
            "duration_tolerance_seconds": round(tolerance, 6),
            "audio_video_tail_delta_seconds": round(av_tail_delta, 6),
        },
        "created_at": datetime.now(timezone.utc).isoformat(), "cache_hit": False,
    }
    _atomic_json(manifest_path, manifest)
    return manifest
