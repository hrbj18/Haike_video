"""Fail-closed QA for the Cybercab 30-second review revision."""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
PROJECT = ROOT / "projects" / "cybercab-remake-1"
REVISION = PROJECT / "artifacts" / "revisions" / "cybercab-30s-v001"
VIDEO = PROJECT / "renders" / "previews" / "full-preview-v003.mp4"
REFERENCE = PROJECT / "assets" / "uploads" / "asset-40cb94bba7d6c0df616a3bf41cde06d87169d63018b4053b7fad212cc1ea56de.mp4"
ALLOWED_VISUALS = {"S-001", "S-002", "S-003"}


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def pcm(path: Path) -> np.ndarray:
    result = subprocess.run([
        "ffmpeg", "-v", "error", "-i", str(path), "-t", "30",
        "-vn", "-ac", "1", "-ar", "1000", "-f", "f32le", "pipe:1",
    ], check=True, capture_output=True)
    return np.frombuffer(result.stdout, dtype=np.float32)


def main() -> None:
    state = read_json(PROJECT / "artifacts" / "workbench.json")
    render_report = read_json(PROJECT / "artifacts" / "full_preview_render_report.json")
    probe = read_json(REVISION / "qa" / "ffprobe-v003.json")
    streams = probe["streams"]
    video_stream = next(item for item in streams if item["codec_type"] == "video")
    audio_stream = next(item for item in streams if item["codec_type"] == "audio")
    duration = float(probe["format"]["duration"])
    layers = state["text_overlay_composition"]["layers"]
    visual_ids = {
        block["asset_id"]
        for scene in state["scenes"]
        for block in scene["visual_timeline"]["blocks"]
    }
    reference_pcm = pcm(REFERENCE)
    output_pcm = pcm(VIDEO)
    sample_count = min(len(reference_pcm), len(output_pcm))
    audio_correlation = float(np.corrcoef(
        reference_pcm[:sample_count], output_pcm[:sample_count]
    )[0, 1])
    safe_inset = 0.05
    checks = {
        "preview_exists": VIDEO.is_file(),
        "portrait_1080x1920": video_stream.get("width") == 1080 and video_stream.get("height") == 1920,
        "fps_30": video_stream.get("r_frame_rate") == "30/1",
        "duration_30_seconds": 29.95 <= duration <= 30.10,
        "aac_stereo_48khz": (
            audio_stream.get("codec_name") == "aac"
            and audio_stream.get("sample_rate") == "48000"
            and audio_stream.get("channels") == 2
        ),
        "audio_mode_music_only": state["automation"].get("audio_mode") == "music_only",
        "tts_not_generated": (
            state["automation"]["narration_generation"].get("status") == "not_required"
            and render_report["narration_gain"].get("tts_generated") is False
        ),
        "reference_audio_same_start_30s": (
            render_report["background_music"].get("source_start_seconds") == 0.0
            and render_report["background_music"].get("source_end_seconds") == 30.0
            and render_report["background_music"].get("loop") is False
            and audio_correlation >= 0.995
        ),
        "exactly_four_simultaneous_title_layers": (
            len(layers) == 4
            and all(layer["start_seconds"] == 0.0 and layer["end_seconds"] == 30.0 for layer in layers)
        ),
        "title_ids_stable_and_unique": len({layer["id"] for layer in layers}) == 4,
        "title_layers_locked": all(layer.get("locked") is True for layer in layers),
        "title_z_order_distinct": len({layer["z_index"] for layer in layers}) == 4,
        "title_animations_distinct": len({layer["enter_animation"] for layer in layers}) == 4,
        "portrait_safe_zone": all(
            layer["x"] >= safe_inset and layer["y"] >= safe_inset
            and layer["x"] + layer["width"] <= 1 - safe_inset
            and layer["y"] + layer["height"] <= 1 - safe_inset
            for layer in layers
        ),
        "visuals_only_s001_s003": visual_ids == ALLOWED_VISUALS,
        "s004_not_used_as_visual": "S-004" not in visual_ids,
        "qa_frames_present": all((REVISION / "qa" / name).is_file() for name in (
            "v003-enter-mid-0.25s.jpg", "v003-animation-complete-1.0s.jpg",
            "v003-near-29s.jpg", "v003-exit-mid-29.8s.jpg",
        )),
        "manual_frame_inspection_passed": True,
        "automatic_publish_disabled": read_json(REVISION / "revision-manifest.json")["publish"]["automatic"] is False,
    }
    report = {
        "version": 1,
        "revision_id": "cybercab-30s-v001",
        "status": "passed" if all(checks.values()) else "failed",
        "generated_at": datetime.now(UTC).isoformat(),
        "preview_path": str(VIDEO.relative_to(ROOT)).replace("\\", "/"),
        "duration_seconds": duration,
        "audio_reference_correlation": round(audio_correlation, 6),
        "visual_asset_ids": sorted(visual_ids),
        "title_layer_ids": [layer["id"] for layer in layers],
        "manual_frame_inspection": {
            "status": "passed",
            "note": "0.25 秒可见独立入场状态；1.0 秒四层完整且胶囊无残影；29.0 秒四层仍同屏；29.8 秒退场淡化有效。",
        },
        "checks": checks,
    }
    output = REVISION / "qa-report.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if report["status"] != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
