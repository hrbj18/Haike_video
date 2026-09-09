"""Produce and inspect the real 1080x1920 text-overlay acceptance render."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageChops, ImageStat

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backlot.text_overlay_composition import build_text_overlay_assets, normalize_text_overlay_composition
from backlot.workbench import _ffmpeg_available
from tools.video.video_compose import VideoCompose


OUTPUT_DIR = ROOT / "artifacts" / "acceptance" / "text-overlay-composition-v1"
WIDTH = 1080
HEIGHT = 1920
FPS = 30
DURATION = 30


def run(command: list[str], timeout: int = 300) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout, check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr[-4000:])
    return result


def layer(layer_id: str, text: str, **values) -> dict:
    result = {
        "id": layer_id, "text": text, "start_seconds": 0, "end_seconds": 30,
        "x": .06, "y": .06, "width": .88, "height": .09,
        "font_family": "Microsoft YaHei", "font_size": 68, "font_weight": 700,
        "color": "#FFFFFF", "stroke_color": "#111111", "stroke_width": 2,
        "shadow_color": "#000000A0", "shadow_blur": 5, "shadow_offset_x": 3, "shadow_offset_y": 5,
        "line_height": 1.12, "text_align": "center",
        "background_color": "#D81E06", "background_opacity": .88,
        "background_radius": 42, "padding_x": 24, "padding_y": 12,
        "enter_animation": "fade", "enter_duration_seconds": .6,
        "exit_animation": "fade", "exit_duration_seconds": .5,
        "z_index": 1, "locked": False,
    }
    result.update(values)
    return result


def changed_ratio(frame: Image.Image, box: tuple[int, int, int, int], background: tuple[int, int, int]) -> float:
    crop = frame.convert("RGB").crop(box)
    changed = 0
    total = crop.width * crop.height
    for red, green, blue in crop.getdata():
        if abs(red - background[0]) + abs(green - background[1]) + abs(blue - background[2]) > 35:
            changed += 1
    return changed / max(1, total)


def main() -> None:
    ffmpeg = _ffmpeg_available()
    if not ffmpeg:
        raise RuntimeError("FFmpeg unavailable")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    base = OUTPUT_DIR / "base-1080x1920-30s.mp4"
    output = OUTPUT_DIR / "text-overlay-four-layers-1080x1920-30s.mp4"
    run([
        ffmpeg, "-y", "-f", "lavfi", "-i", f"color=c=0x152033:s={WIDTH}x{HEIGHT}:r={FPS}:d={DURATION}",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28", "-pix_fmt", "yuv420p", str(base),
    ])

    contract = normalize_text_overlay_composition({
        "version": 1, "revision": 7, "layers": [
            layer("ACCEPT-T1", "未来已来", color="#FFFFFF", background_color="#D81E06", z_index=1),
            layer("ACCEPT-T2", "四层文字同时出现", x=.08, y=.20, width=.62, height=.08,
                  font_size=54, color="#101010", background_color="#FFD400", background_opacity=.94,
                  enter_animation="slide_left", enter_duration_seconds=.8, z_index=2, locked=True),
            layer("ACCEPT-T3", "位置 · 字号 · 颜色 · 背景", x=.20, y=.34, width=.62, height=.12,
                  font_size=48, color="#FFFFFF", background_color="#008060", background_opacity=.9,
                  background_radius=20, enter_animation="scale", enter_duration_seconds=.6, z_index=3),
            layer("ACCEPT-T4", "最高层级\n胶囊标题", start_seconds=.8, end_seconds=29.7,
                  x=.28, y=.38, width=.62, height=.12, font_size=42, color="#FFE8FF",
                  background_color="#7B2CBF", background_opacity=.93, background_radius=70,
                  enter_animation="slide_up", enter_duration_seconds=.5,
                  exit_animation="slide_right", exit_duration_seconds=.5, z_index=9),
        ],
    })
    overlays, render_contract = build_text_overlay_assets(OUTPUT_DIR, contract, WIDTH, HEIGHT)
    result = VideoCompose().execute({
        "operation": "overlay", "input_path": str(base), "overlays": overlays,
        "output_path": str(output), "codec": "libx264", "crf": 22, "preset": "veryfast",
    })
    if not result.success or not output.is_file():
        raise RuntimeError(result.error or "overlay output missing")

    probe = json.loads(run([
        ffmpeg.replace("ffmpeg.EXE", "ffprobe.EXE").replace("ffmpeg.exe", "ffprobe.exe"),
        "-v", "error", "-show_streams", "-show_format", "-of", "json", str(output),
    ]).stdout)
    video_stream = next(item for item in probe["streams"] if item.get("codec_type") == "video")
    frame_specs = {"enter-mid": .3, "animation-complete": 1.5, "near-29s": 29.0, "after-t4": 29.9}
    frame_paths: dict[str, Path] = {}
    for name, seconds in frame_specs.items():
        path = OUTPUT_DIR / f"frame-{name}-{seconds:.1f}s.png"
        run([ffmpeg, "-y", "-ss", f"{seconds:.3f}", "-i", str(output), "-frames:v", "1", str(path)], timeout=120)
        frame_paths[name] = path

    frames = {name: Image.open(path).convert("RGB") for name, path in frame_paths.items()}
    background = (21, 32, 51)
    regions = {
        "ACCEPT-T1": (70, 125, 1010, 265),
        "ACCEPT-T2": (95, 395, 740, 525),
        "ACCEPT-T3": (225, 660, 875, 780),
        "ACCEPT-T4": (905, 745, 965, 930),
    }
    near_ratios = {layer_id: round(changed_ratio(frames["near-29s"], box, background), 4) for layer_id, box in regions.items()}
    after_t4_ratio = round(changed_ratio(frames["after-t4"], regions["ACCEPT-T4"], background), 4)
    animation_difference = ImageStat.Stat(ImageChops.difference(frames["enter-mid"], frames["animation-complete"])).mean
    duration = float(probe["format"]["duration"])
    checks = {
        "dimensions_1080x1920": int(video_stream["width"]) == WIDTH and int(video_stream["height"]) == HEIGHT,
        "frame_rate_30": video_stream.get("avg_frame_rate") == "30/1",
        "duration_30_seconds": 29.95 <= duration <= 30.05,
        "four_layers_visible_near_29s": all(value > .2 for value in near_ratios.values()),
        "animation_mid_differs_from_complete": sum(animation_difference) > 3,
        "t4_time_range_ends_before_29_9": after_t4_ratio < .08,
        "safe_zone": not render_contract["safe_zone"]["warnings"],
        "z_order_overlap_declared": [item["text_layer_id"] for item in overlays] == ["ACCEPT-T1", "ACCEPT-T2", "ACCEPT-T3", "ACCEPT-T4"],
        "locked_layer_rendered": next(item for item in contract["layers"] if item["id"] == "ACCEPT-T2")["locked"] is True,
    }
    report = {
        "status": "passed" if all(checks.values()) else "failed",
        "output": str(output),
        "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
        "probe": {"width": video_stream["width"], "height": video_stream["height"], "avg_frame_rate": video_stream["avg_frame_rate"], "duration": duration},
        "sample_frames": {name: str(path) for name, path in frame_paths.items()},
        "near_29s_changed_ratios": near_ratios,
        "after_t4_changed_ratio": after_t4_ratio,
        "animation_difference_mean_rgb": [round(value, 4) for value in animation_difference],
        "checks": checks,
        "render_contract": render_contract,
    }
    (OUTPUT_DIR / "acceptance-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"status": report["status"], "output": str(output), "checks": checks}, ensure_ascii=False, indent=2))
    if report["status"] != "passed":
        raise RuntimeError("acceptance checks failed")


if __name__ == "__main__":
    main()
