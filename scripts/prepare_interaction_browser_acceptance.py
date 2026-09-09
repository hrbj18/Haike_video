"""Prepare an isolated, provider-free browser fixture for interaction review."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backlot import material_interaction_candidates as candidates
from backlot import material_interaction_review as review_mod
from backlot import material_interactions
from backlot import workbench
from backlot.media_index import media_content_fingerprint


def run(command: list[str]) -> None:
    completed = subprocess.run(
        command, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=120, check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr[-1200:] or "FFmpeg fixture creation failed")


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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--projects-dir", type=Path,
        default=ROOT / ".backlot" / "acceptance" / "interaction-browser" / "projects",
    )
    parser.add_argument("--port", type=int, default=4765)
    args = parser.parse_args()
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        raise SystemExit("FFmpeg/ffprobe unavailable")
    projects_dir = args.projects_dir.resolve()
    project_dir = projects_dir / "interaction-browser-acceptance-v1"
    projects_dir.mkdir(parents=True, exist_ok=True)
    if project_dir.exists():
        project_dir.resolve().relative_to((ROOT / ".backlot" / "acceptance").resolve())
        shutil.rmtree(project_dir)
    (project_dir / "assets").mkdir(parents=True)
    (project_dir / "project.json").write_text(json.dumps({
        "project_id": project_dir.name, "title": "户外互动浏览器验收", "pipeline_type": "cinematic",
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    source = project_dir / "assets" / "synthetic-dialogue-source.mp4"
    create_source(source, ffmpeg)
    state = workbench.bootstrap_workbench(project_dir)
    asset = workbench._append_asset(project_dir, state, {
        "name": "合成对话验收原片", "type": "video", "source_type": "human_provided",
        "path": "assets/synthetic-dialogue-source.mp4", "duration_seconds": 10,
        "license": "本地合成测试夹具",
    })
    utterances = [
        {"id": "U00001", "start": 1, "end": 2, "text": "机械狗打招呼"},
        {"id": "U00002", "start": 4, "end": 5, "text": "阿姨回答"},
        {"id": "U00003", "start": 7, "end": 8, "text": "继续交流"},
    ]
    index = {
        "version": material_interactions.VERSION, "signature": "browser-fixture-index-v1",
        "status": "completed", "duration": 10, "profile": "efficient",
        "source": {"fingerprint": media_content_fingerprint(source)},
        "identity": {"model": "offline-fixture"},
        "audio": {"policy": "doubao_transcript", "status": "available",
                  "provider": "offline-fixture", "utterances": utterances},
        "events": [{
            "event_id": "E01", "group_id": "G01", "participants": "机械狗与同一组阿姨",
            "summary": "从打招呼到结束的连续互动", "start": 0, "end": 9,
            "confidence": .95, "score": .93, "completeness": "complete",
            "requires_review": False, "recommend_reason": "合成验收事件",
            "evidence_frame_ids": ["F001", "F002"],
            "utterance_ids": [row["id"] for row in utterances],
            "highlights": [{"time": 4, "label": "阿姨回答"}], "unknowns": [],
        }],
        "ranked_event_ids": ["E01"], "usage": {"model_calls": 0, "contact_sheets": 0,
                                                   "detail_frames": 0, "elapsed_seconds": 0},
        "notice": "离线浏览器验收夹具；未调用外部模型。",
    }
    output = project_dir / "artifacts" / "media-index" / asset["id"] / "interaction-v1" / "fixture"
    output.mkdir(parents=True)
    index_path = output / "material-interaction-index.json"
    index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    review = review_mod.initialize_review(index)
    review = review_mod.apply_action(
        review, index, "set_status", {"event_id": "R0001", "status": "kept"}, review["revision"],
    )
    review_path = output / "material-interaction-review.json"
    review_mod.write_review(review_path, review, index)
    candidate = candidates.generate_candidate(
        project_dir=project_dir, source=source, index=index, review=review, event_id="R0001",
        output_root=project_dir / "artifacts" / "media-index" / asset["id"] / "interaction-candidates",
        ffmpeg=ffmpeg, ffprobe=ffprobe,
    )
    asset["media_index"] = {
        "status": "completed", "stage": "interaction",
        "interaction_index_path": index_path.relative_to(project_dir).as_posix(),
        "interaction_review_path": review_path.relative_to(project_dir).as_posix(),
        "interaction_candidate_root": f"artifacts/media-index/{asset['id']}/interaction-candidates",
        "interaction_candidates": [{
            "plan_id": candidate["plan_id"],
            "status": candidate["status"],
            "output_duration": candidate["output_duration"],
        }],
        "interaction_proxy": {"status": "source_compatible", "path": asset["path"]},
        "interaction_recognize_audio": True, "interaction_event_count": 1,
    }
    workbench._save(project_dir, state)
    print(json.dumps({
        "projects_dir": str(projects_dir), "project_id": project_dir.name,
        "asset_id": asset["id"], "candidate_id": candidate["plan_id"],
        "url": f"http://127.0.0.1:{args.port}/p/{project_dir.name}/workbench?view=assets",
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
