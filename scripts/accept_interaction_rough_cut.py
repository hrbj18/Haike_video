"""Offline acceptance for interaction rough-cut timing and media materialization."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backlot.material_audio_evidence import detect_silence
from backlot.material_interaction_edit import attach_render_result, build_edit_plan, write_edit_plan
from backlot.material_interaction_render import probe_media, render_interaction_candidate
from backlot.media_index import media_content_fingerprint


def run(command: list[str], *, timeout: float = 120) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace",
                               timeout=timeout, check=False)
    if completed.returncode != 0:
        raise RuntimeError("本地验收命令失败；未调用外部服务\n" + completed.stderr[-1200:])
    return completed


def create_source(path: Path, ffmpeg: str) -> None:
    expression = (
        "aevalsrc=if(between(t\\,1\\,2)+between(t\\,4\\,5)+between(t\\,7\\,8)\\,"
        "0.22*sin(2*PI*620*t)\\,0):s=48000:d=10"
    )
    run([
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=25:duration=10",
        "-f", "lavfi", "-i", expression,
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart", str(path),
    ])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=ROOT / ".backlot" / "acceptance" / "interaction-rough-cut-v1")
    args = parser.parse_args(argv)
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        raise SystemExit("FFmpeg/ffprobe 不可用，无法做真实媒体验收")
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    source = output / "synthetic-dialogue-source.mp4"
    create_source(source, ffmpeg)
    original_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    fingerprint = media_content_fingerprint(source)
    utterances = [
        {"id": "U00001", "start": 1, "end": 2, "text": "第一句"},
        {"id": "U00002", "start": 4, "end": 5, "text": "第二句"},
        {"id": "U00003", "start": 7, "end": 8, "text": "第三句"},
    ]
    index = {
        "status": "completed", "signature": "offline-interaction-index-v1", "duration": 10,
        "source": {"fingerprint": fingerprint},
        "audio": {"policy": "doubao_transcript", "status": "available", "provider": "offline-fixture",
                  "utterances": utterances},
    }
    review = {
        "index_signature": index["signature"], "revision": 1,
        "events": [{"review_event_id": "R0001", "group_id": "G01", "status": "kept",
                    "start": 0, "end": 9, "utterance_ids": [row["id"] for row in utterances]}],
    }
    silences = detect_silence(source, ffmpeg=ffmpeg, start=0, end=9, noise_db=-38, minimum_duration=.12)
    plan = build_edit_plan(index, review, "R0001", silence_intervals=silences)
    manifest = render_interaction_candidate(source, plan, output / "candidates", ffmpeg=ffmpeg, ffprobe=ffprobe)
    relative_preview = Path(manifest["path"]).relative_to(output).as_posix()
    plan = attach_render_result(plan, manifest, expected_revision=0, preview_path=relative_preview)
    write_edit_plan(output / "interaction-edit-plan.json", plan)
    cached = render_interaction_candidate(source, {**plan, "revision": 0, "qa": {"status": "not_rendered"},
                                                    "preview": None, "history": []},
                                                  output / "candidates", ffmpeg=ffmpeg, ffprobe=ffprobe)
    media = probe_media(Path(manifest["path"]), ffprobe)
    streams = {row["codec_type"]: row for row in media["streams"]}
    gaps_after = []
    for left, right in zip(utterances, utterances[1:]):
        removed = sum(max(0, min(right["start"], row["end"]) - max(left["end"], row["start"]))
                      for row in plan["removed_ranges"] if not row.get("restored"))
        gaps_after.append(round(right["start"] - left["end"] - removed, 3))
    checks = {
        "two_long_gaps_detected": len(plan["removed_ranges"]) == 2,
        "gaps_are_target_or_safer": all(.29 <= value <= .36 for value in gaps_after),
        "source_unchanged": hashlib.sha256(source.read_bytes()).hexdigest() == original_hash,
        "browser_video": streams.get("video", {}).get("codec_name") == "h264" and streams.get("video", {}).get("pix_fmt") == "yuv420p",
        "original_audio_preserved": streams.get("audio", {}).get("codec_name") == "aac",
        "media_qa_passed": manifest["qa"]["status"] == "passed" and all(manifest["qa"]["checks"].values()),
        "cache_reused": cached.get("cache_hit") is True,
        "pending_human_review": plan["status"] == "pending_review",
    }
    report = {
        "version": "interaction-rough-cut-offline-acceptance-v1", "status": "passed" if all(checks.values()) else "failed",
        "external_calls": 0, "source": str(source), "preview": manifest["path"],
        "silence_intervals": silences, "gaps_after_seconds": gaps_after,
        "source_duration": plan["source_duration"], "output_duration": plan["output_duration"],
        "checks": checks,
        "limitations": ["合成夹具验证媒体与规则，不替代真实豆包、视觉模型和直播语义验收"],
    }
    report_path = output / "acceptance-report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
