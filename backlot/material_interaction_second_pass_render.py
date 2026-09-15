"""Render hook-led interaction plans with speed-aware audio/video timing."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
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


# v2: video is padded to cover the audio (a frame-based trim loses the sub-frame
# remainder of every segment, which a many-cut plan compounds into a visible
# trailing gap), the renderer can read the review proxy, and captions can be
# burned in.  The version is part of the render signature, so bumping it is what
# stops a stale v1 preview from being reused after a renderer change.
# v3: every segment now carries a 5-15 ms audio fade (in/out) at each seam so the
# 26 hard cuts become click-free, and the whole clip can fade in/out (P0-3/P1-3).
# ``afade`` never changes duration, so the audio master clock is untouched.
VERSION = "interaction-second-pass-render-v3"

# Measured on this project's pinned static-ffmpeg 8.0.1 (win32 build):
#   atempo=0.49 -> out of range [0.5 - 100]; atempo=0.5 and atempo=100.0 -> ok;
#   atempo=100.1 / atempo=101.0 -> out of range.
# The project's segment speed range is only 1.5-4.0, so a **single** ``atempo``
# instance covers it exactly — chaining would be needless complexity.  Anything
# outside [0.5, 100] raises a Chinese, actionable error instead of silently
# clipping or dropping audio.
ATEMPO_MIN_TEMPO = 0.5
ATEMPO_MAX_TEMPO = 100.0


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


# A readable default for a vertical short: libass scales FontSize against a
# 384x288 reference canvas when the subtitle file carries no PlayRes, so 11 is
# roughly 49px on a 1280px-tall frame.  Side margins keep long lines wrapping
# instead of running off the edge.
SUBTITLE_STYLE = "FontName=Microsoft YaHei,FontSize=11,Outline=1,Shadow=1,MarginL=24,MarginR=24,MarginV=48,Alignment=2"


def _srt_timestamp(seconds: float) -> str:
    milliseconds = max(0, int(round(float(seconds) * 1000)))
    hours, remainder = divmod(milliseconds, 3600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def srt_text(cues: list[dict[str, Any]]) -> str:
    """Subtitle file on the **output** timeline, which is what the viewer sees."""
    blocks = []
    for index, cue in enumerate(sorted(cues, key=lambda row: float(row["output_start"])), 1):
        text = str(cue.get("text") or "").strip()
        if not text:
            continue
        blocks.append(f"{index}\n{_srt_timestamp(cue['output_start'])} --> "
                      f"{_srt_timestamp(cue['output_end'])}\n{text}\n")
    return "\n".join(blocks)


def _subtitle_filter(srt_path: Path) -> str:
    """Build the ``subtitles`` filter.

    Two details are load-bearing on Windows:

    * the value must be wrapped in single quotes — unquoted, the parser reads
      the escaped drive letter as a separate option and fails with
      ``Unable to parse option value ... as image size``;
    * the drive colon still needs a backslash because the colon is the filter's
      option separator.
    """
    escaped = srt_path.as_posix().replace(":", "\\:").replace("'", "\\'")
    return f"subtitles='{escaped}':force_style='{SUBTITLE_STYLE}'"


def _shot(media: Path, source_seconds: float, video_filter: str, target: Path, *, ffmpeg: str,
          runner: Callable[..., subprocess.CompletedProcess[str]], timeout: float) -> bool:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.unlink(missing_ok=True)
    command = [
        ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "warning", "-y",
        "-ss", f"{max(0.0, source_seconds):.6f}", "-t", "0.6", "-i", str(media),
        "-an", "-frames:v", "1", "-vf", video_filter,
        "-pix_fmt", "yuvj420p", "-q:v", "3", str(target),
    ]
    try:
        completed = runner(command, capture_output=True, timeout=max(30.0, min(float(timeout), 180.0)),
                           check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0 and target.is_file() and target.stat().st_size > 0


def probe_subtitle_rendering(media: Path, plan: dict[str, Any], srt_path: Path, workdir: Path, *,
                            ffmpeg: str, runner: Callable[..., subprocess.CompletedProcess[str]],
                            timeout: float) -> tuple[bool, str]:
    """Prove that libass really paints the caption before spending a full render.

    Renders half a second twice — with and without the subtitle filter — and
    compares the frames.  Byte-identical output means the filter produced
    nothing (no libass, no usable font, unreadable file), and reporting that is
    the whole point: "subtitles were requested" must never be mistaken for
    "subtitles are on screen".
    """
    mapping = plan.get("timeline_mapping") or []
    cue_rows = [row for row in (plan.get("subtitle_cues") or []) if str(row.get("text") or "").strip()]
    if not cue_rows or not mapping:
        return False, "清单里没有可显示的字幕"
    cue = sorted(cue_rows, key=lambda row: float(row["output_start"]))[0]
    midpoint = (float(cue["output_start"]) + float(cue["output_end"])) / 2
    occurrence = next((row for row in mapping
                       if float(row["output_start"]) <= midpoint <= float(row["output_end"])), None)
    if occurrence is None:
        return False, "字幕时间点不在任何播放片段内"
    source_seconds = float(occurrence["source_start"]) + \
        (midpoint - float(occurrence["output_start"])) * float(occurrence["speed"])
    source_seconds = min(max(source_seconds, float(occurrence["source_start"]) + .05),
                         float(occurrence["source_end"]) - .05)
    workdir.mkdir(parents=True, exist_ok=True)
    probe_srt = workdir / "subtitle-probe.srt"
    probe_srt.write_text("1\n00:00:00,000 --> 00:00:30,000\n" + str(cue["text"]).strip() + "\n",
                         encoding="utf-8")
    plain = workdir / "subtitle-probe-plain.jpg"
    burned = workdir / "subtitle-probe-burned.jpg"
    try:
        if not _shot(media, source_seconds, "null", plain, ffmpeg=ffmpeg, runner=runner, timeout=timeout):
            return False, "无法抽取自检参考帧"
        if not _shot(media, source_seconds, _subtitle_filter(probe_srt), burned,
                     ffmpeg=ffmpeg, runner=runner, timeout=timeout):
            return False, "字幕滤镜无法渲染"
        if plain.read_bytes() == burned.read_bytes():
            return False, "字幕滤镜未在画面上产生任何像素变化"
    except OSError as exc:
        return False, f"字幕自检无法完成：{exc}"
    finally:
        for leftover in (probe_srt, plain, burned):
            leftover.unlink(missing_ok=True)
    return True, ""


def _fade_seconds(milliseconds: float) -> float:
    try:
        value = float(milliseconds)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, value) / 1000.0


def _atempo_chain(speed: float) -> str:
    """Return the ``atempo`` filter for one segment's speed.

    A single instance is exact here: this project's pinned static-ffmpeg 8.0.1
    accepts ``atempo`` in ``[0.5, 100]`` (measured, see module constant), and the
    segment speed range is 1.5-4.0, so no chaining is needed.  The requested
    ratio and the filter's effective ratio agree to 1e-6 by construction.
    """
    value = float(speed)
    if not math.isfinite(value) or value <= 0:
        raise InteractionSecondPassRenderError("片段倍速无效，请重新生成二次剪辑方案")
    if not ATEMPO_MIN_TEMPO <= value <= ATEMPO_MAX_TEMPO:
        raise InteractionSecondPassRenderError(
            f"片段倍速 {value:.3f} 超出本机 FFmpeg 的 atempo 支持范围"
            f"（{ATEMPO_MIN_TEMPO}–{ATEMPO_MAX_TEMPO}），请调整倍速后重新生成。"
        )
    return f"atempo={value:.6f}"


def _segment_audio_filters(start: float, end: float, speed: float, *,
                           audio_fade: bool = True, audio_fade_ms: float = 8.0) -> str:
    """Build one segment's audio chain: trim → resample → atempo → afade.

    The fade-out start is ``(end - start) / speed - fade`` — i.e. it is measured
    on the **speed-adjusted** output timeline, never the source timeline — and
    ``afade`` does not alter duration, so the audio master clock is preserved.
    When ``audio_fade`` is off the returned chain is byte-identical to the V2
    renderer, which is what makes the "off == previous behaviour" check hold.
    """
    chain = (
        f"atrim=start={start:.6f}:end={end:.6f},asetpts=PTS-STARTPTS,"
        "aresample=48000:async=0:first_pts=0,aformat=sample_rates=48000:channel_layouts=stereo,"
        f"{_atempo_chain(speed)}"
    )
    if not audio_fade:
        return chain
    output_duration = (float(end) - float(start)) / float(speed)
    if output_duration <= 0:
        return chain
    # Never let the fade eat a whole short segment: cap it at half the segment.
    fade = min(_fade_seconds(audio_fade_ms), output_duration / 2.0)
    if fade <= 0:
        return chain
    return (
        chain
        + f",afade=t=in:st=0:d={fade:.6f}"
        + f",afade=t=out:st={max(0.0, output_duration - fade):.6f}:d={fade:.6f}"
    )


def _filters(occurrences: list[dict[str, Any]], contract: dict[str, Any], has_audio: bool,
             subtitle_filter: str = "", *, audio_fade: bool = True, audio_fade_ms: float = 8.0,
             edge_fade: bool = False, edge_fade_ms: float = 200.0) -> tuple[str, list[str], float]:
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
                f"[0:a]{_segment_audio_filters(start, end, speed, audio_fade=audio_fade, audio_fade_ms=audio_fade_ms)}"
                f"[a{index}]"
            )
            audio_inputs.append(f"[a{index}]")
    pieces.append("".join(video_inputs) + f"concat=n={len(occurrences)}:v=1:a=0[vcat]")
    # Audio is the master clock: each `atrim` is sample exact, while a frame-based
    # `trim` loses the sub-frame remainder of every segment.  With one or two
    # segments that is a frame or less, but a plan that cuts 20 pauses ends up
    # with the picture several frames short and the check legitimately fails.
    # Cloning the last frame until the trim cuts it makes the picture cover the
    # audio instead of falling behind it.
    pieces.append(f"[vcat]fps={fps},tpad=stop_mode=clone:stop=-1,"
                  f"trim=duration={expected_duration:.6f},setpts=PTS-STARTPTS[vout]")
    if has_audio:
        pieces.append("".join(audio_inputs) + f"concat=n={len(occurrences)}:v=0:a=1[acat]")
        audio_tail = f"[acat]atrim=duration={expected_duration:.6f},asetpts=PTS-STARTPTS"
        if edge_fade and expected_duration > 0:
            edge = min(_fade_seconds(edge_fade_ms), expected_duration / 2.0)
            if edge > 0:
                # Whole-clip head/tail fade on the final, already-concatenated
                # audio; it too leaves the total duration untouched.
                audio_tail += (f",afade=t=in:st=0:d={edge:.6f}"
                               f",afade=t=out:st={max(0.0, expected_duration - edge):.6f}:d={edge:.6f}")
        pieces.append(audio_tail + "[aout]")
    video_out = "[vout]"
    if subtitle_filter:
        # Burned in last, after the concat and the frame-rate normalisation, so
        # the caption sits on the final picture rather than on one segment.
        pieces.append(f"[vout]{subtitle_filter}[vcap]")
        video_out = "[vcap]"
    mappings = ["-map", video_out] + (["-map", "[aout]"] if has_audio else [])
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
    render_source: Path | None = None,
    longest_edge: int = 1280,
    timeout: float = 900,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, Any]:
    """Render one reviewed second-pass plan.

    ``source`` owns the fingerprint and the render contract; ``render_source`` is
    the file actually handed to FFmpeg and defaults to ``source``.  Long
    TS-remuxed recordings carry broken timestamps, so callers pass the review
    proxy here — the same contract the first-pass candidate renderer uses.
    The contract itself must always be derived from the original: the proxy's
    average frame rate is not a regular value and would skew the output.
    """
    validate_second_pass_plan(plan)
    source = source.resolve()
    expected_fingerprint = str((plan.get("source") or {}).get("fingerprint") or "")
    if not source.is_file() or media_content_fingerprint(source) != expected_fingerprint:
        raise InteractionSecondPassRenderError("原片不存在或指纹已变化，已停止二次剪辑")
    occurrences = plan.get("occurrences") or []
    if not occurrences:
        raise InteractionSecondPassRenderError("二次剪辑清单没有可用片段")
    media_input = Path(render_source).resolve() if render_source else source
    if not media_input.is_file():
        raise InteractionSecondPassRenderError("审核代理不存在，已停止二次剪辑")
    degradations: list[str] = []
    if render_source is None:
        # Never silent: reading the original directly is a real downgrade on
        # material with broken timestamps, and the user has to be able to see it.
        degradations.append("proxy_missing:未提供时间戳连续的审核代理，已直接读取原片")
    try:
        source_probe = probe_media(source, ffprobe, runner=runner)
        contract = _render_contract(source_probe, longest_edge)
    except InteractionRenderError as exc:
        raise InteractionSecondPassRenderError(str(exc)) from exc
    has_audio = contract["audio"] is not None
    render_fingerprint = expected_fingerprint if media_input == source else media_content_fingerprint(media_input)
    if media_input != source:
        render_streams = probe_media(media_input, ffprobe, runner=runner).get("streams") or []
        if any(row.get("codec_type") == "audio" for row in render_streams) != has_audio:
            raise InteractionSecondPassRenderError("审核代理的音轨与原片不一致，已停止二次剪辑")
    cue_rows = [row for row in (plan.get("subtitle_cues") or []) if str(row.get("text") or "").strip()]
    plan_options = plan.get("options") if isinstance(plan.get("options"), dict) else {}
    want_subtitles = plan_options.get("burn_subtitles") is not False and bool(cue_rows)
    # Fade settings belong to the render signature: toggling "消爆音" on or off
    # must never return a cached preview rendered with the other setting.
    audio_fade = plan_options.get("audio_fade") is not False
    audio_fade_ms = float(plan_options.get("audio_fade_ms") or 8.0)
    edge_fade = plan_options.get("edge_fade") is True
    edge_fade_ms = float(plan_options.get("edge_fade_ms") or 200.0)
    frozen = {
        "version": VERSION, "plan_id": plan["plan_id"], "plan_revision": plan["revision"],
        "source_fingerprint": expected_fingerprint, "render_source_fingerprint": render_fingerprint,
        "occurrences": occurrences,
        "timeline_mapping": plan.get("timeline_mapping"), "subtitle_cues": plan.get("subtitle_cues"),
        "contract": contract,
        # The caption decision belongs in the signature: toggling subtitles must
        # never return a cached preview that was rendered without them.
        "subtitle_style": SUBTITLE_STYLE if want_subtitles else "",
        "audio_effects": {
            "audio_fade": bool(audio_fade), "audio_fade_ms": round(audio_fade_ms, 6),
            "edge_fade": bool(edge_fade), "edge_fade_ms": round(edge_fade_ms, 6),
        },
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
    # Burn-in is an enhancement that is allowed to fail, but never silently: the
    # plan promises "配上字幕", so a failure has to surface as a degradation and
    # the clip ships without captions instead of the render dying.
    subtitle_filter = ""
    subtitles_info: dict[str, Any] = {
        "requested": want_subtitles, "burned": False, "cue_count": len(cue_rows),
        "style": SUBTITLE_STYLE if want_subtitles else "",
    }
    if want_subtitles:
        srt_path = directory / "subtitles.srt"
        srt_path.write_text(srt_text(cue_rows), encoding="utf-8")
        subtitles_info["file"] = srt_path.name
        rendered, reason = probe_subtitle_rendering(
            media_input, plan, srt_path, directory, ffmpeg=ffmpeg, runner=runner, timeout=timeout,
        )
        if rendered:
            subtitle_filter = _subtitle_filter(srt_path)
            subtitles_info["burned"] = True
        else:
            degradations.append(f"subtitle_burn_failed:{reason}")
            subtitles_info["reason"] = reason
    handle, temporary_name = tempfile.mkstemp(prefix=".preview.", suffix=".mp4", dir=directory)
    os.close(handle)
    temporary = Path(temporary_name)
    filter_graph, mappings, expected_duration = _filters(
        occurrences, contract, has_audio, subtitle_filter=subtitle_filter,
        audio_fade=audio_fade, audio_fade_ms=audio_fade_ms,
        edge_fade=edge_fade, edge_fade_ms=edge_fade_ms,
    )
    command = [
        ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-y", "-i", str(media_input),
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
        "removed_by_pause_seconds": plan.get("removed_by_pause_seconds"),
        "pause_trim_count": len(plan.get("pause_trims") or []),
        "subtitles": subtitles_info,
        "degradations": degradations,
        "qa": {
            "status": "passed", "checks": checks,
            "duration_tolerance_seconds": round(tolerance, 6),
            "audio_video_tail_delta_seconds": round(av_tail_delta, 6),
        },
        "created_at": datetime.now(timezone.utc).isoformat(), "cache_hit": False,
    }
    _atomic_json(manifest_path, manifest)
    return manifest
