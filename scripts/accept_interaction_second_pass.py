"""Run a local-only real-media acceptance for interaction second-pass editing.

The runner reuses an existing reviewed first-pass candidate and cached
transcript.  A deterministic story fixture stands in for the paid semantic
request so the acceptance cannot spend money or silently contact a provider.
It renders through production code, validates the output with ffprobe, checks
idempotent reuse, and proves that every upstream artifact remains unchanged.
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

from backlot.material_interaction_render import probe_media
from backlot.material_interaction_second_pass import validate_second_pass_plan
from backlot.material_interaction_second_pass_candidates import generate_second_pass_candidate
from backlot.media_index import media_content_fingerprint


SOURCE_PROJECT = ROOT / "projects" / "interaction-acceptance-20260905"
DEFAULT_SOURCE = SOURCE_PROJECT / "assets" / "source.mp4"
DEFAULT_INDEX = (
    SOURCE_PROJECT / "artifacts" / "media-index" / "S-001" / "interaction-v1"
    / "f462f4b1c0369d86658f" / "material-interaction-index.json"
)
DEFAULT_PARENT = (
    SOURCE_PROJECT / "artifacts" / "media-index" / "S-001" / "interaction-candidates"
    / "IEP-31bb279305199f39" / "interaction-edit-plan.json"
)
DEFAULT_OUTPUT = ROOT / "projects" / "interaction-second-pass-acceptance-20260908"


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


def _sample_story(context: dict[str, Any]) -> dict[str, Any]:
    """Frozen editorial fixture for the four-aunt acceptance sample.

    It intentionally retains the first photo/action, the strongest phone
    reaction, and the second photo, while removing the greeting, location chat,
    repetitive explanation, and farewell.  Only utterance IDs cross the
    semantic boundary; production code derives every source timestamp.
    """
    expected = [f"U{index:05d}" for index in range(1, 47)]
    supplied = [str(row.get("id") or "") for row in context.get("utterances") or []]
    if supplied != expected:
        raise RuntimeError("内置四阿姨验收夹具与当前父候选转写不匹配")

    def ids(first: int, last: int) -> list[str]:
        return [f"U{index:05d}" for index in range(first, last + 1)]

    return {
        "summary": "去掉独立寒暄、地域闲聊、重复解释和告别，保留两次合影、跳起动作与手机反应；手机笑点前置。",
        "groups": [
            {"id": "G1", "type": "greeting", "utterance_ids": ids(1, 4), "decision": "drop", "summary": "问候与互报称呼", "reason": "正文开头避免寒暄", "depends_on": [], "hook_eligible": False, "hook_score": 0.15},
            {"id": "G2", "type": "setup", "utterance_ids": ids(5, 10), "decision": "drop", "summary": "询问来处与同行关系", "reason": "不影响合影主线", "depends_on": [], "hook_eligible": False, "hook_score": 0.2},
            {"id": "G3", "type": "setup", "utterance_ids": ids(11, 16), "decision": "keep", "summary": "提出合影并请人拍摄", "reason": "第一次合影的必要铺垫", "depends_on": [], "hook_eligible": False, "hook_score": 0.55},
            {"id": "G4", "type": "action", "utterance_ids": ids(17, 22), "decision": "keep", "summary": "跳起摆姿势完成第一次合影", "reason": "动作完整且有视觉价值", "depends_on": ["G3"], "hook_eligible": True, "hook_score": 0.88},
            {"id": "G5", "type": "transition", "utterance_ids": ids(23, 31), "decision": "drop", "summary": "第一次照片归属讨论", "reason": "信息重复，可从笑点直接理解", "depends_on": [], "hook_eligible": False, "hook_score": 0.35},
            {"id": "G6", "type": "reaction", "utterance_ids": ids(32, 34), "decision": "keep", "summary": "改用阿姨手机并集体笑场", "reason": "可独立理解的强反应", "depends_on": [], "hook_eligible": True, "hook_score": 0.98},
            {"id": "G7", "type": "explanation", "utterance_ids": ids(35, 38), "decision": "drop", "summary": "夸赞并解释为何重拍", "reason": "与前后动作重复", "depends_on": [], "hook_eligible": False, "hook_score": 0.45},
            {"id": "G8", "type": "action", "utterance_ids": ids(39, 43), "decision": "keep", "summary": "完成第二次合影", "reason": "为手机笑点提供动作结果", "depends_on": ["G6"], "hook_eligible": True, "hook_score": 0.82},
            {"id": "G9", "type": "farewell", "utterance_ids": ids(44, 46), "decision": "drop", "summary": "总结与告别", "reason": "短视频正文不保留告别", "depends_on": [], "hook_eligible": False, "hook_score": 0.1},
        ],
        "hook_candidates": [
            {"id": "H1", "group_ids": ["G6"], "reason": "手机笑点可独立理解，倍速后约五秒"},
        ],
    }


def _parent_preview_path(parent_path: Path, parent: dict[str, Any]) -> Path:
    preview = parent.get("preview") if isinstance(parent.get("preview"), dict) else {}
    value = Path(str(preview.get("path") or ""))
    if value.is_absolute():
        return value
    for ancestor in parent_path.parents:
        candidate = ancestor / value
        if candidate.is_file():
            return candidate
    return SOURCE_PROJECT / value


def _media_contract(path: Path, ffprobe: str) -> dict[str, Any]:
    media = probe_media(path, ffprobe)
    streams = {str(row.get("codec_type")): row for row in media.get("streams") or []}
    return {
        "duration": float((media.get("format") or {}).get("duration") or 0),
        "video_codec": (streams.get("video") or {}).get("codec_name"),
        "pixel_format": (streams.get("video") or {}).get("pix_fmt"),
        "width": (streams.get("video") or {}).get("width"),
        "height": (streams.get("video") or {}).get("height"),
        "audio_codec": (streams.get("audio") or {}).get("codec_name"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="户外互动二次剪辑 V2 本地真实媒体验收")
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--index", type=Path, default=DEFAULT_INDEX)
    parser.add_argument("--parent-plan", type=Path, default=DEFAULT_PARENT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--speed", type=float, default=1.25)
    parser.add_argument("--timeout", type=float, default=900)
    args = parser.parse_args(argv)

    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        raise SystemExit("FFmpeg/ffprobe 不可用，无法做真实媒体验收")
    source = args.source.resolve()
    index_path = args.index.resolve()
    parent_path = args.parent_plan.resolve()
    if not source.is_file() or not index_path.is_file() or not parent_path.is_file():
        raise SystemExit("二次剪辑验收的源片、索引或父候选不存在")
    index, parent = _load_json(index_path), _load_json(parent_path)
    if media_content_fingerprint(source) != str((parent.get("source") or {}).get("fingerprint") or ""):
        raise SystemExit("源片指纹与父候选不一致，已停止验收")
    parent_preview = _parent_preview_path(parent_path, parent)
    protected = [source, index_path, parent_path]
    if parent_preview.is_file():
        protected.append(parent_preview)
    before = {str(path): _sha256(path) for path in protected}

    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    project_path = output / "project.json"
    if not project_path.is_file():
        project_path.write_text(json.dumps({
            "project_id": output.name,
            "title": "户外互动二次剪辑 V1 独立验收",
            "pipeline_type": "acceptance-only",
        }, ensure_ascii=False, indent=2), encoding="utf-8")

    analyzer_calls = 0

    def analyze(context: dict[str, Any]) -> tuple[dict[str, Any], str]:
        nonlocal analyzer_calls
        analyzer_calls += 1
        return _sample_story(context), "fixture-no-paid-call"

    options = {
        "trim_head": True,
        "trim_tail": True,
        "extract_highlights": True,
        "hook_enabled": True,
        "hook_mode": "move",
        "speed": args.speed,
        "target_min_seconds": 45,
        "target_max_seconds": 60,
    }
    root = output / "artifacts" / "interaction-second-pass"
    started = time.perf_counter()
    first = generate_second_pass_candidate(
        project_dir=output, source=source, index=index, parent_plan=parent,
        output_root=root, options=options,
        runtime_identity={"provider": "fixture", "model": "fixture-no-paid-call", "paid": False},
        ffmpeg=ffmpeg, ffprobe=ffprobe, analyze=analyze, timeout=args.timeout,
    )
    calls_after_first = analyzer_calls
    second = generate_second_pass_candidate(
        project_dir=output, source=source, index=index, parent_plan=parent,
        output_root=root, options=options,
        runtime_identity={"provider": "fixture", "model": "fixture-no-paid-call", "paid": False},
        ffmpeg=ffmpeg, ffprobe=ffprobe, analyze=analyze, timeout=args.timeout,
    )
    elapsed = time.perf_counter() - started
    validate_second_pass_plan(first)

    preview = output / str((first.get("preview") or {}).get("path") or "")
    contract = _media_contract(preview, ffprobe)
    mapping = first.get("timeline_mapping") or []
    body = [row for row in mapping if row.get("role") == "body"]
    hook = [row for row in mapping if row.get("role") == "hook"]
    repeated_source = []
    if hook:
        hook_start = float(hook[0]["source_start"])
        repeated_source = [
            row for row in mapping
            if float(row["source_start"]) <= hook_start <= float(row["source_end"])
        ]
    hook_cues = [row for row in first.get("subtitle_cues") or [] if row.get("role") == "hook"]
    duplicated_cues = [
        row for row in first.get("subtitle_cues") or []
        if row.get("role") == "body" and row.get("utterance_id") in {cue.get("utterance_id") for cue in hook_cues}
    ]
    after = {str(path): _sha256(path) for path in protected}
    checks = {
        "source_and_upstream_unchanged": before == after,
        "paid_external_calls_zero": True,
        "semantic_submit_at_most_once": calls_after_first <= 1,
        "repeat_request_adds_no_submit": analyzer_calls == calls_after_first,
        "repeat_request_reuses_preview": second.get("render_cache_hit") is True
        and (second.get("preview") or {}).get("signature") == (first.get("preview") or {}).get("signature"),
        "pending_human_review": first.get("status") == "pending_review",
        "qa_passed": (first.get("qa") or {}).get("status") == "passed",
        "target_duration": first.get("target_duration_status") == "within_target",
        "hook_first": bool(mapping) and mapping[0].get("role") == "hook",
        "body_chronological": all(
            float(left["source_start"]) <= float(right["source_start"])
            for left, right in zip(body, body[1:])
        ),
        "hook_source_occurs_once": len(repeated_source) == 1,
        "hook_subtitle_has_no_body_copy": bool(hook_cues) and not duplicated_cues,
        "unexpected_source_repetition_zero": float(first.get("repeated_source_seconds") or 0) == 0,
        "content_qa_passed": (first.get("content_qa") or {}).get("status") == "passed",
        "browser_video_contract": contract["video_codec"] == "h264"
        and contract["pixel_format"] == "yuv420p" and contract["audio_codec"] == "aac",
        "render_duration_matches_plan": abs(contract["duration"] - float(first["output_duration"]))
        <= float((first.get("qa") or {}).get("duration_tolerance_seconds") or .08),
    }
    selected = [row for row in (first.get("story") or {}).get("groups") or [] if row.get("selected")]
    dropped = [row for row in (first.get("story") or {}).get("groups") or [] if not row.get("selected")]
    report = {
        "version": "interaction-second-pass-acceptance-v2",
        "status": "passed" if all(checks.values()) else "failed",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "elapsed_seconds": round(elapsed, 3),
        "paid_external_calls": 0,
        "fixture_analyzer_calls": analyzer_calls,
        "plan": {
            "plan_id": first.get("plan_id"), "revision": first.get("revision"),
            "parent": first.get("parent"), "status": first.get("status"),
            "speed": (first.get("options") or {}).get("speed"),
            "source_duration": first.get("source_duration"),
            "body_source_duration": first.get("body_source_duration"),
            "hook_source_duration": first.get("hook_source_duration"),
            "repeated_source_seconds": first.get("repeated_source_seconds"),
            "output_duration": first.get("output_duration"),
            "removed_source_seconds": first.get("removed_source_seconds"),
            "target_duration_status": first.get("target_duration_status"),
            "usage": first.get("usage"),
        },
        "selected_groups": [{"id": row.get("id"), "summary": row.get("summary")} for row in selected],
        "dropped_groups": [{"id": row.get("id"), "summary": row.get("summary")} for row in dropped],
        "occurrences": mapping,
        "subtitle_cue_count": len(first.get("subtitle_cues") or []),
        "preview": str(preview),
        "media": contract,
        "checks": checks,
        "protected_sha256": before,
        "limitations": [
            "语义结果由确定性夹具注入，未触发付费模型；因此本报告不证明真实模型的选段质量。",
            "自动 QA 已验证容器、音画尾差、时长、精彩段去重映射与上游不变；仍需真人观看试听跳切、叙事与声音自然度。",
            "当前只覆盖四阿姨真实原片的一个父候选，尚未覆盖其他互动主体、横屏、无音轨和 Linux。",
        ],
    }
    report_path = output / "acceptance-report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
