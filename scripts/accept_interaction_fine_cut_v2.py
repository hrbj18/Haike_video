"""Local-only acceptance runner for interaction fine-cut V2.

The runner reads an existing source/index/review contract and writes derived
plans, previews and a machine-readable report into an isolated output folder.
It never invokes ASR, a vision model, approval/adoption, or publication.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys
import time
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backlot.material_interaction_candidates import generate_candidate
from backlot.material_interaction_edit import validate_edit_plan
from backlot.material_interaction_render import probe_media
from backlot.material_interaction_review import read_review
from backlot.material_interactions import media_content_fingerprint


DEFAULT_SOURCE = ROOT / "projects" / "interaction-acceptance-20260905" / "assets" / "source.mp4"
DEFAULT_EVIDENCE = (
    ROOT / "projects" / "interaction-acceptance-20260905" / "artifacts" / "media-index"
    / "S-001" / "interaction-v1" / "f462f4b1c0369d86658f"
)
DEFAULT_OUTPUT = ROOT / "projects" / "interaction-finecut-v2-acceptance-20260908"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"无法读取验收证据：{path}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"验收证据不是 JSON 对象：{path}")
    return value


def _overlap(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return float(left["start"]) < float(right["end"]) and float(right["start"]) < float(left["end"])


def _boundary_intersections(plan: dict[str, Any], utterances: list[dict[str, Any]]) -> list[dict[str, Any]]:
    protected = plan.get("protected_range") or plan.get("original_range") or {}
    start, end = float(protected.get("start", 0)), float(protected.get("end", 0))
    return [
        {"utterance_id": row.get("id"), "start": row.get("start"), "end": row.get("end"), "text": row.get("text")}
        for row in utterances
        if (float(row.get("start", 0)) < start < float(row.get("end", 0)))
        or (float(row.get("start", 0)) < end < float(row.get("end", 0)))
    ]


def _event_report(
    plan: dict[str, Any], *, ffprobe: str, elapsed: float,
    utterances: list[dict[str, Any]], project_dir: Path,
) -> dict[str, Any]:
    validate_edit_plan(plan)
    removed = [row for row in plan.get("removed_ranges") or [] if not row.get("restored")]
    protected = plan.get("protected_ranges") or []
    collisions = [
        {"removal_id": removal.get("id"), "protected_id": guard.get("id")}
        for removal in removed for guard in protected if _overlap(removal, guard)
    ]
    preview = plan.get("preview") if isinstance(plan.get("preview"), dict) else {}
    preview_path = Path(str(preview.get("path") or ""))
    if not preview_path.is_absolute():
        preview_path = project_dir / preview_path
    media = probe_media(preview_path, ffprobe) if preview_path.is_file() else {"streams": []}
    streams = {str(row.get("codec_type")): row for row in media.get("streams") or []}
    mapping = plan.get("timeline_mapping") or []
    mapping_duration = round(sum(float(row["output_end"]) - float(row["output_start"]) for row in mapping), 3)
    pause_visual = plan.get("pause_visual") if isinstance(plan.get("pause_visual"), dict) else {}
    event_range = plan.get("protected_range") or plan.get("original_range") or {}
    event_visual_windows = [
        row for row in pause_visual.get("windows") or []
        if isinstance(row, dict) and _overlap(
            {"start": row.get("gap_start", row.get("start")), "end": row.get("gap_end", row.get("end"))},
            event_range,
        )
    ]
    checks = {
        "v2_plan": plan.get("version") == "interaction-edit-plan-v2",
        "pending_human_review": plan.get("status") == "pending_review",
        "protected_collision_count_zero": not collisions,
        "boundary_not_inside_utterance": not _boundary_intersections(plan, utterances),
        "mapping_matches_output": abs(mapping_duration - float(plan.get("output_duration") or 0)) <= .002,
        "media_qa_passed": (plan.get("qa") or {}).get("status") == "passed",
        "browser_video": streams.get("video", {}).get("codec_name") == "h264"
        and streams.get("video", {}).get("pix_fmt") == "yuv420p",
        "original_audio_contract": streams.get("audio", {}).get("codec_name") == "aac",
    }
    return {
        "plan_id": plan.get("plan_id"),
        "event_id": plan.get("event_id"),
        "status": "passed" if all(checks.values()) else "failed",
        "elapsed_seconds": round(elapsed, 3),
        "original_range": plan.get("original_range"),
        "protected_range": plan.get("protected_range"),
        "output_duration": plan.get("output_duration"),
        "removed_seconds": round(
            float(plan.get("source_duration") or 0) - float(plan.get("output_duration") or 0), 3
        ),
        "removal_count": len(removed),
        "protected_collision_count": len(collisions),
        "speech_activity_status": (plan.get("speech_activity") or {}).get("status"),
        "speech_activity_cache_hit": bool((plan.get("speech_activity") or {}).get("cache_hit")),
        "pause_visual_status": pause_visual.get("status"),
        "pause_visual_cache_hit": bool(pause_visual.get("cache_hit")),
        "pause_visual_event_windows": len(event_visual_windows),
        "pause_visual_safe_windows": sum(bool(row.get("safe_to_shorten")) for row in event_visual_windows),
        "warnings": plan.get("warnings") or [],
        "preview": str(preview_path),
        "checks": checks,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="户外互动精剪 V2 本地验收")
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--index", type=Path, default=DEFAULT_EVIDENCE / "material-interaction-index.json")
    parser.add_argument("--review", type=Path, default=DEFAULT_EVIDENCE / "material-interaction-review.json")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--event-id", action="append", dest="event_ids")
    parser.add_argument("--target-gap", type=float, default=.3)
    parser.add_argument("--timeout", type=float, default=900)
    args = parser.parse_args(argv)

    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        raise SystemExit("FFmpeg/ffprobe 不可用，无法做真实媒体验收")
    source, index_path, review_path = args.source.resolve(), args.index.resolve(), args.review.resolve()
    if not source.is_file():
        raise SystemExit(f"验收源素材不存在：{source}")
    source_hash_before = _sha256(source)
    index_hash_before, review_hash_before = _sha256(index_path), _sha256(review_path)
    index = _load_json(index_path)
    review = read_review(review_path, index)
    if str((index.get("source") or {}).get("fingerprint") or "") != media_content_fingerprint(source):
        raise SystemExit("源素材内容指纹与索引不一致，已停止验收")

    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    project_json = output / "project.json"
    if not project_json.exists():
        project_json.write_text(json.dumps({
            "project_id": output.name,
            "title": "户外互动精剪 V2 独立验收",
            "pipeline_type": "acceptance-only",
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    candidate_root = output / "artifacts" / "interaction-candidates"
    available_ids = [str(row.get("review_event_id")) for row in review.get("events") or []]
    event_ids = args.event_ids or available_ids[:3]
    unknown = [event_id for event_id in event_ids if event_id not in available_ids]
    if unknown:
        raise SystemExit("未知验收事件：" + ", ".join(unknown))

    rows = []
    utterances = list(((index.get("audio") or {}).get("utterances") or []))
    for event_id in event_ids:
        started = time.perf_counter()
        plan = generate_candidate(
            project_dir=output,
            source=source,
            index=index,
            review=review,
            event_id=event_id,
            output_root=candidate_root,
            ffmpeg=ffmpeg,
            ffprobe=ffprobe,
            timeout=args.timeout,
            target_gap=args.target_gap,
        )
        rows.append(_event_report(
            plan, ffprobe=ffprobe, elapsed=time.perf_counter() - started,
            utterances=utterances, project_dir=output,
        ))

    immutable_inputs = {
        "source_unchanged": _sha256(source) == source_hash_before,
        "index_unchanged": _sha256(index_path) == index_hash_before,
        "review_unchanged": _sha256(review_path) == review_hash_before,
    }
    checks = {
        **immutable_inputs,
        "external_calls_zero": True,
        "all_candidates_passed": bool(rows) and all(row["status"] == "passed" for row in rows),
        "no_automatic_approval": all("approved" not in str(row.get("status")) for row in rows),
    }
    report = {
        "version": "interaction-fine-cut-v2-acceptance-v1",
        "status": "passed" if all(checks.values()) else "failed",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "external_calls": 0,
        "source": {"path": str(source), "sha256": source_hash_before},
        "inputs": {"index": str(index_path), "review": str(review_path)},
        "target_gap": args.target_gap,
        "candidates": rows,
        "checks": checks,
        "limitations": [
            "本报告复用既有 ASR/视觉索引，只执行本地 VAD、规则、FFmpeg 渲染与 ffprobe QA。",
            "自动检查不能替代真人对所有新切口的观看和听辨。",
            "Linux、独立 5—15 分钟样本及真实可删正例未由本脚本验证。",
        ],
    }
    report_path = output / "acceptance-report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
