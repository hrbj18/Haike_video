"""Materialize reviewable interaction edits without touching the source media."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import subprocess
from typing import Any, Callable

from backlot.material_interaction_edit import validate_edit_plan
from backlot.media_index import media_content_fingerprint


VERSION = "interaction-candidate-render-v1"


class InteractionRenderError(RuntimeError):
    pass


def _run(command: list[str], *, timeout: float,
         runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run) -> subprocess.CompletedProcess[str]:
    try:
        completed = runner(command, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=max(30.0, timeout), check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise InteractionRenderError("互动候选生成失败，请检查 FFmpeg 是否可用") from exc
    if completed.returncode != 0:
        raise InteractionRenderError("互动候选生成失败，已保留原片和精剪清单")
    return completed


def probe_media(path: Path, ffprobe: str, *, runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run) -> dict[str, Any]:
    result = _run([
        ffprobe, "-v", "error", "-show_entries",
        "format=duration,format_name:stream=index,codec_type,codec_name,pix_fmt,width,height,duration,avg_frame_rate,r_frame_rate,sample_rate,channels:stream_tags=rotate:stream_side_data=rotation",
        "-of", "json", str(path),
    ], timeout=120, runner=runner)
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise InteractionRenderError("ffprobe 无法验证互动候选") from exc
    if not isinstance(payload.get("streams"), list):
        raise InteractionRenderError("互动候选缺少媒体流信息")
    return payload


def _fraction(value: Any) -> float:
    raw = str(value or "0/0")
    try:
        left, right = raw.split("/", 1)
        result = float(left) / float(right)
    except (ValueError, ZeroDivisionError):
        return 0.0
    return result if math.isfinite(result) else 0.0


def _rotation(stream: dict[str, Any]) -> int:
    candidates = [(stream.get("tags") or {}).get("rotate")]
    candidates.extend(row.get("rotation") for row in stream.get("side_data_list") or [] if isinstance(row, dict))
    for value in candidates:
        try:
            return int(round(float(value))) % 360
        except (TypeError, ValueError):
            continue
    return 0


def _render_contract(source_probe: dict[str, Any], longest_edge: int) -> dict[str, Any]:
    video = next((row for row in source_probe["streams"] if row.get("codec_type") == "video"), None)
    audio = next((row for row in source_probe["streams"] if row.get("codec_type") == "audio"), None)
    if not video:
        raise InteractionRenderError("原片没有视频流")
    width, height = int(video.get("width") or 0), int(video.get("height") or 0)
    if _rotation(video) in {90, 270}:
        width, height = height, width
    if width <= 0 or height <= 0:
        raise InteractionRenderError("原片画面尺寸无效")
    scale = min(1.0, float(longest_edge) / max(width, height))
    target_width = max(2, int(width * scale) // 2 * 2)
    target_height = max(2, int(height * scale) // 2 * 2)
    fps = _fraction(video.get("avg_frame_rate")) or _fraction(video.get("r_frame_rate")) or 30.0
    if not 1 <= fps <= 120:
        fps = 30.0
    # Freeze VFR and awkward rates to a stable CFR while retaining the common source cadence.
    common = min((23.976, 24.0, 25.0, 29.97, 30.0, 50.0, 59.94, 60.0), key=lambda value: abs(value - fps))
    fps = common if abs(common - fps) <= .08 else min(60.0, max(12.0, round(fps, 3)))
    return {
        "version": VERSION, "longest_edge": int(longest_edge), "width": target_width,
        "height": target_height, "fps": fps, "video": "h264/yuv420p/crf25",
        "audio": "aac/48kHz/128k" if audio else None, "faststart": True,
    }


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _faststart(path: Path) -> bool:
    with path.open("rb") as handle:
        head = handle.read(min(path.stat().st_size, 8 * 1024 * 1024))
    moov, mdat = head.find(b"moov"), head.find(b"mdat")
    return moov >= 0 and (mdat < 0 or moov < mdat)


def _filters(ranges: list[dict[str, float]], contract: dict[str, Any],
             has_audio: bool) -> tuple[str, list[str]]:
    """Build the filter graph and the output mappings for a single input.

    取值一律走**绝对时间戳** trim，输入只有一个。这要求送进来的媒体时间戳连续，
    因此调用方必须传审核代理（`render_source`）而不是原片：

    本项目的长直播回放是 TS 转封装录制的 HEVC，时间戳在部分区间断裂。直接对原片
    `trim` 会少取数据——实测 46.042 秒的区间只取到 21.8 秒视频 / 25.0 秒音频，候选比
    保留清单短几十秒，被 QA 判为不合格（QA 只容忍 1 帧 + 一次 AAC padding）。改成
    「每段一次输入 seek」虽然能取对（46.083 秒，误差 41 毫秒），但每段一次 seek 在
    1.7 GB 的 HEVC 上代价极高：三段事件跑满 900 秒超时。代理是完整重编码的标准
    H.264、时间戳连续，且分辨率与渲染契约完全一致，所以输出规格不变。
    """
    pieces = []
    video_inputs = []
    audio_inputs = []
    fps = f"{contract['fps']:.6f}".rstrip("0").rstrip(".")
    expected_duration = sum(float(row["end"]) - float(row["start"]) for row in ranges)
    for index, row in enumerate(ranges):
        start, end = float(row["start"]), float(row["end"])
        pieces.append(
            f"[0:v]trim=start={start:.6f}:end={end:.6f},setpts=PTS-STARTPTS,"
            f"scale={contract['width']}:{contract['height']}:flags=lanczos,setsar=1[v{index}]"
        )
        video_inputs.append(f"[v{index}]")
        if has_audio:
            pieces.append(
                f"[0:a]atrim=start={start:.6f}:end={end:.6f},asetpts=PTS-STARTPTS,"
                "aresample=48000:async=0:first_pts=0,aformat=sample_rates=48000:channel_layouts=stereo"
                f"[a{index}]"
            )
            audio_inputs.append(f"[a{index}]")
    # Frame-rate conversion happens once after concatenation.  Applying fps to
    # every kept piece rounds every cut independently and lets duration error
    # accumulate with the number of removals.
    pieces.append("".join(video_inputs) + f"concat=n={len(ranges)}:v=1:a=0[vcat]")
    # `trim` selects complete source frames.  Cap the concatenated result once
    # at the logical plan duration so two boundary frames cannot accumulate at
    # the final tail; the protected original audio is not shortened here.
    pieces.append(
        f"[vcat]fps={fps},trim=duration={expected_duration:.6f},setpts=PTS-STARTPTS[vout]"
    )
    if has_audio:
        # Audio is concatenated independently.  A combined A/V concat pads each
        # piece to its longer stream and accumulates frame rounding at every
        # edit, producing visible timeline drift on plans with many removals.
        pieces.append("".join(audio_inputs) + f"concat=n={len(ranges)}:v=0:a=1[aout]")
    return ";".join(pieces), ["-map", "[vout]"] + (["-map", "[aout]"] if has_audio else [])


def render_interaction_candidate(
    source: Path,
    plan: dict[str, Any],
    output_dir: Path,
    *,
    ffmpeg: str,
    ffprobe: str,
    render_source: Path | None = None,
    longest_edge: int = 1280,
    timeout: float = 900,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, Any]:
    """Render one exact edit plan and return its auditable browser-preview manifest.

    ``source`` 是原片，负责指纹校验和渲染契约（尺寸/帧率/音轨以它为准）；
    ``render_source`` 是真正送进 FFmpeg 的文件，默认同原片。长直播回放的原片是
    TS 转封装录制的 HEVC、时间戳在部分区间断裂，直接 trim 会少取数据（见 ``_filters``），
    所以调用方应传时间戳连续的审核代理——它由原片完整重编码而来，分辨率与契约一致，
    输出规格不受影响。
    """
    validate_edit_plan(plan)
    source = source.resolve()
    expected_fingerprint = str((plan.get("source") or {}).get("fingerprint") or "")
    if not source.is_file() or media_content_fingerprint(source) != expected_fingerprint:
        raise InteractionRenderError("原片不存在或指纹已变化，已停止生成候选")
    ranges = plan.get("keep_ranges") or []
    if not ranges:
        raise InteractionRenderError("精剪清单没有可保留画面")
    media_input = Path(render_source).resolve() if render_source else source
    if not media_input.is_file():
        raise InteractionRenderError("审核代理不存在，已停止生成候选")
    render_fingerprint = expected_fingerprint if media_input == source else media_content_fingerprint(media_input)
    source_probe = probe_media(source, ffprobe, runner=runner)
    contract = _render_contract(source_probe, longest_edge)
    has_audio = contract["audio"] is not None
    if media_input != source:
        render_streams = probe_media(media_input, ffprobe, runner=runner).get("streams") or []
        if any(row.get("codec_type") == "audio" for row in render_streams) != has_audio:
            raise InteractionRenderError("审核代理的音轨与原片不一致，已停止生成候选")
    frozen = {
        "version": VERSION, "plan_id": plan["plan_id"], "plan_revision": plan["revision"],
        "source_fingerprint": expected_fingerprint, "keep_ranges": ranges,
        "timeline_mapping": plan.get("timeline_mapping"), "contract": contract,
        "render_source_fingerprint": render_fingerprint,
    }
    signature = _digest(frozen)
    directory = output_dir.resolve() / str(plan["plan_id"]) / signature[:20]
    path, manifest_path = directory / "preview.mp4", directory / "manifest.json"
    if path.is_file() and manifest_path.is_file():
        try:
            previous = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            previous = {}
        if previous.get("signature") == signature and (previous.get("qa") or {}).get("status") == "passed":
            return {**previous, "path": str(path), "manifest_path": str(manifest_path), "cache_hit": True}
    directory.mkdir(parents=True, exist_ok=True)
    temporary = directory / ".preview.tmp.mp4"
    if temporary.exists():
        temporary.unlink()
    filter_graph, mappings = _filters(ranges, contract, has_audio)
    expected_duration = sum(float(row["end"]) - float(row["start"]) for row in ranges)
    command = [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-y", "-i", str(media_input),
               "-filter_complex", filter_graph, *mappings,
               # 输出级 -t 把成品卡在各保留段之和上，避免尾部边界帧累积。
               "-t", f"{expected_duration:.6f}",
               "-c:v", "libx264", "-preset", "fast", "-crf", "25",
               "-pix_fmt", "yuv420p", "-threads", "4"]
    if has_audio:
        command.extend(["-c:a", "aac", "-b:a", "128k", "-ar", "48000"])
    else:
        command.append("-an")
    command.extend(["-movflags", "+faststart", str(temporary)])
    try:
        _run(command, timeout=timeout, runner=runner)
        output_probe = probe_media(temporary, ffprobe, runner=runner)
        video = next((row for row in output_probe["streams"] if row.get("codec_type") == "video"), {})
        audio = next((row for row in output_probe["streams"] if row.get("codec_type") == "audio"), None)
        actual = float((output_probe.get("format") or {}).get("duration") or 0)
        expected = expected_duration
        tolerance = 1.0 / float(contract["fps"]) + (1024.0 / 48000 if has_audio else 0) + .01
        checks = {
            "video_codec": video.get("codec_name") == "h264",
            "pixel_format": video.get("pix_fmt") == "yuv420p",
            "dimensions": int(video.get("width") or 0) == contract["width"] and int(video.get("height") or 0) == contract["height"],
            "audio_contract": (audio is not None and audio.get("codec_name") == "aac") if has_audio else audio is None,
            "duration": abs(actual - expected) <= tolerance,
            "faststart": _faststart(temporary),
        }
        if not all(checks.values()):
            failed = "、".join(key for key, passed in checks.items() if not passed)
            detail = f"actual={actual:.6f}, expected={expected:.6f}, tolerance={tolerance:.6f}"
            raise InteractionRenderError(f"互动候选 QA 未通过（{failed}; {detail}），未登记不合格预览")
        temporary.replace(path)
    except Exception:
        if temporary.exists():
            temporary.unlink()
        raise
    manifest = {
        **frozen, "signature": signature, "status": "pending_review", "path": str(path),
        "manifest_path": str(manifest_path), "source_duration": round(float(plan["source_duration"]), 3),
        "output_duration": round(actual, 3), "removed_seconds": round(float(plan["source_duration"]) - actual, 3),
        "qa": {"status": "passed", "checks": checks, "duration_tolerance_seconds": round(tolerance, 6)},
        "created_at": datetime.now(timezone.utc).isoformat(), "cache_hit": False,
    }
    _atomic_json(manifest_path, manifest)
    return manifest
