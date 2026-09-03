"""Render bounded local evidence for the 16:9 source-video hero-window contract.

This QA harness is intentionally offline. It reads user-provided media, renders
short review clips into an ignored scratch directory, probes the outputs, and
extracts representative frames. It never writes to projects/ or .backlot/.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.video.video_compose import VideoCompose  # noqa: E402


SAMPLES = (
    ("balance", "microduck-balance-recovery.mp4", 0.0),
    ("chorale", "microduck-chorale.mp4", 0.0),
    ("roller", "microduck-roller-skating.mp4", 2.5),
    ("portrait", "microduck-grab-and-carry.mp4", 0.0),
    ("still", "microduck-kickabout.jpg", 0.0),
)


def run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)


def probe(ffprobe: Path, source: Path) -> dict:
    result = run([
        str(ffprobe), "-v", "error", "-show_streams", "-show_format",
        "-of", "json", str(source),
    ])
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe failed for {source.name}: {result.stderr[-800:]}")
    return json.loads(result.stdout)


def video_stream(data: dict) -> dict:
    return next((item for item in data.get("streams") or [] if item.get("codec_type") == "video"), {})


def duration_seconds(data: dict) -> float:
    stream = video_stream(data)
    return float(stream.get("duration") or (data.get("format") or {}).get("duration") or 0)


def audio_max_volume_db(ffmpeg: Path, source: Path) -> float | None:
    result = run([
        str(ffmpeg), "-v", "info", "-i", str(source),
        "-map", "0:a:0?", "-af", "volumedetect", "-f", "null", "NUL",
    ])
    match = re.search(r"max_volume:\s*(-?inf|-?[0-9.]+)\s*dB", result.stderr, re.IGNORECASE)
    if not match:
        return None
    return float("-inf") if match.group(1).lower() == "-inf" else float(match.group(1))


def source_dimensions(path: Path, data: dict) -> tuple[int, int]:
    if path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}:
        from PIL import Image

        with Image.open(path) as image:
            return int(image.width), int(image.height)
    stream = video_stream(data)
    return int(stream.get("width") or 0), int(stream.get("height") or 0)


def placement(width: int, height: int) -> dict:
    aspect = width / height
    portrait = aspect < 1
    max_height = .68 if portrait else .78
    size = min(.42, max_height * (1080 / 1920) * aspect) if portrait else .74
    return {
        "presetId": "portrait_hero_center" if portrait else "landscape_hero_center",
        "positionXRatio": .5,
        "positionYRatio": .44 if portrait else .47,
        "sizeRatio": round(size, 4),
        "aspectMode": "source",
        "maxHeightRatio": max_height,
        "sourceAspectRatio": aspect,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--ffmpeg", required=True, type=Path)
    parser.add_argument("--ffprobe", required=True, type=Path)
    args = parser.parse_args()
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    background = output_dir / "dynamic-background.mp4"
    background_result = run([
        str(args.ffmpeg), "-y", "-f", "lavfi", "-i",
        "testsrc2=size=1920x1080:rate=30:duration=2",
        "-vf", "boxblur=20:2,eq=brightness=-0.25:saturation=0.35",
        "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(background),
    ])
    if background_result.returncode != 0:
        raise RuntimeError(f"background generation failed: {background_result.stderr[-1200:]}")

    reports: list[dict] = []
    for sample_id, filename, requested_start in SAMPLES:
        source = (args.source_root / filename).resolve()
        if not source.is_file():
            raise FileNotFoundError(source)
        source_probe = probe(args.ffprobe, source)
        width, height = source_dimensions(source, source_probe)
        is_image = source.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}
        available = duration_seconds(source_probe) if not is_image else 2.0
        source_start = 0.0 if is_image else min(requested_start, max(0.0, available - .4))
        clip_duration = 2.0 if is_image else min(2.0, available - source_start)
        if clip_duration < .4:
            raise RuntimeError(f"{filename} has less than 0.4 seconds available")
        render_source = source
        render_source_start = source_start
        if not is_image:
            render_source = output_dir / f"{sample_id}-source-normalized.mp4"
            normalized = run([
                str(args.ffmpeg), "-y", "-ss", f"{source_start:.3f}",
                "-t", f"{clip_duration:.3f}", "-i", str(source), "-an",
                "-vf", "fps=30,scale=trunc(iw/2)*2:trunc(ih/2)*2",
                "-c:v", "libx264", "-preset", "fast", "-crf", "18",
                "-pix_fmt", "yuv420p", "-avoid_negative_ts", "make_zero",
                str(render_source),
            ])
            if normalized.returncode != 0:
                raise RuntimeError(f"source normalization failed for {filename}: {normalized.stderr[-1200:]}")
            render_source_start = 0.0
        output = output_dir / f"{sample_id}-1920x1080.mp4"
        hero = {
            "id": f"hero-{sample_id}", "role": "hero", "src": str(render_source),
            "mediaType": "image" if is_image else "video", "fit": "contain",
            "muted": True, "playbackRate": 1,
            "startSeconds": 0, "endSeconds": clip_duration,
            "startFrame": 0, "endFrame": round(clip_duration * 30),
            "placement": placement(width, height),
        }
        if not is_image:
            hero.update({
                "trimBeforeSeconds": render_source_start,
                "trimAfterSeconds": render_source_start + clip_duration,
                "trimBeforeFrame": round(render_source_start * 30),
                "trimAfterFrame": round((render_source_start + clip_duration) * 30),
            })
        result = VideoCompose().execute({
            "operation": "remotion_render",
            "composition_data": {
                "renderer_family": "layered-content",
                "canvasWidth": 1920, "canvasHeight": 1080, "frameRate": 30,
                "durationFrames": round(clip_duration * 30),
                "captions": {
                    "words": [
                        {"word": "机械鸭", "startMs": 100, "endMs": 850},
                        {"word": "主角窗验收", "startMs": 850, "endMs": 1850},
                    ],
                    "wordsPerPage": 2,
                    "fontSize": 46,
                },
                "scenes": [{
                    "id": f"scene-{sample_id}", "kind": "layered",
                    "startSeconds": 0, "durationSeconds": clip_duration,
                    "startFrame": 0, "durationFrames": round(clip_duration * 30),
                    "layoutRecipe": "focus_card",
                    "background": {
                        "src": str(background), "mediaType": "video", "fit": "cover",
                        "muted": True, "playbackRate": 1,
                        "trimBeforeSeconds": 0, "trimAfterSeconds": clip_duration,
                        "trimBeforeFrame": 0, "trimAfterFrame": round(clip_duration * 30),
                    },
                    "overlays": [hero],
                    "frameStyle": {
                        "borderRadiusRatio": .025, "borderColor": "#D9F3FF", "shadow": "soft",
                    },
                }],
            },
            "output_path": str(output),
        })
        if not result.success:
            raise RuntimeError(f"render failed for {filename}: {result.error}")
        output_probe = probe(args.ffprobe, output)
        output_video = video_stream(output_probe)
        output_duration = duration_seconds(output_probe)
        audio_streams = [item for item in output_probe.get("streams") or [] if item.get("codec_type") == "audio"]
        maximum_volume = audio_max_volume_db(args.ffmpeg, output) if audio_streams else None
        if (int(output_video.get("width") or 0), int(output_video.get("height") or 0)) != (1920, 1080):
            raise RuntimeError(f"{output.name} is not 1920x1080")
        if abs(output_duration - clip_duration) > .12:
            raise RuntimeError(f"{output.name} duration drifted: {output_duration:.3f} vs {clip_duration:.3f}")
        if audio_streams and (maximum_volume is None or maximum_volume > -80):
            raise RuntimeError(f"{output.name} contains non-silent source audio: {maximum_volume}")
        frames = []
        for label, timestamp in (("enter", .12), ("middle", clip_duration / 2), ("exit", max(.12, clip_duration - .12))):
            frame_path = output_dir / f"{sample_id}-{label}.png"
            extracted = run([
                str(args.ffmpeg), "-y", "-ss", f"{timestamp:.3f}", "-i", str(output),
                "-frames:v", "1", str(frame_path),
            ])
            if extracted.returncode != 0 or not frame_path.is_file():
                raise RuntimeError(f"frame extraction failed for {output.name}: {extracted.stderr[-800:]}")
            frames.append(str(frame_path))
        reports.append({
            "sample_id": sample_id,
            "source_name": filename,
            "source_dimensions": [width, height],
            "source_start_seconds": round(source_start, 3),
            "duration_seconds": round(output_duration, 3),
            "output_dimensions": [1920, 1080],
            "audio_stream_count": len(audio_streams),
            "audio_max_volume_db": "-inf" if maximum_volume == float("-inf") else maximum_volume,
            "placement": hero["placement"],
            "output": str(output),
            "frames": frames,
        })

    report = {
        "status": "technical_pass",
        "network_calls": 0,
        "paid_calls": 0,
        "samples": reports,
    }
    report_path = output_dir / "qa-report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(report_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
