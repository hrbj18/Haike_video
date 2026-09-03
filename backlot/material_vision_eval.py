"""Schema-backed evaluation for private material-vision acceptance sets."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator


REPO_ROOT = Path(__file__).resolve().parents[1]
EVAL_SCHEMA_PATH = REPO_ROOT / "config" / "schemas" / "material_vision_eval.schema.json"
INDEX_SCHEMA_PATH = REPO_ROOT / "config" / "schemas" / "material_vision_index.schema.json"


class MaterialVisionEvalError(RuntimeError):
    pass


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MaterialVisionEvalError(f"无法读取视觉验收 JSON：{path}") from exc
    if not isinstance(payload, dict):
        raise MaterialVisionEvalError("视觉验收文件必须是 JSON 对象")
    return payload


def _validate(payload: dict[str, Any], schema_path: Path, label: str) -> None:
    schema = _load_json(schema_path)
    errors = sorted(Draft202012Validator(schema).iter_errors(payload), key=lambda item: list(item.path))
    if errors:
        first = errors[0]
        location = ".".join(str(part) for part in first.path) or "root"
        raise MaterialVisionEvalError(f"{label}不符合 Schema：{location}: {first.message}")


def load_acceptance_manifest(path: Path) -> dict[str, Any]:
    payload = _load_json(path)
    _validate(payload, EVAL_SCHEMA_PATH, "视觉验收集")
    for case in payload["cases"]:
        for start, end in case.get("expected_intervals", []) + case.get("forbidden_intervals", []):
            if end <= start:
                raise MaterialVisionEvalError(f"{case['case_id']} 的区间结束时间必须晚于开始时间")
    return payload


def validate_material_vision_index(index: dict[str, Any]) -> None:
    _validate(index, INDEX_SCHEMA_PATH, "素材视觉索引")
    frame_ids: set[str] = set()
    for shot in index.get("shots") or []:
        if float(shot["end_seconds"]) <= float(shot["start_seconds"]):
            raise MaterialVisionEvalError(f"{shot['shot_id']} 的结束时间必须晚于开始时间")
        shot_frame_ids = {str(frame["frame_id"]) for frame in shot.get("frames") or []}
        if frame_ids & shot_frame_ids:
            raise MaterialVisionEvalError("素材视觉索引包含重复 frame_id")
        frame_ids.update(shot_frame_ids)
        description = shot.get("description")
        if isinstance(description, dict):
            referenced: set[str] = set()
            for entity in description.get("entities") or []:
                referenced.update(str(value) for value in entity.get("evidence_frame_ids") or [])
            for action in description.get("actions") or []:
                referenced.update(str(value) for value in action.get("evidence_frame_ids") or [])
            if not referenced.issubset(shot_frame_ids):
                raise MaterialVisionEvalError(f"{shot['shot_id']} 的描述引用了未输入的证据帧")


def _overlap(left: tuple[float, float], right: tuple[float, float]) -> float:
    return max(0.0, min(left[1], right[1]) - max(left[0], right[0]))


def evaluate_material_vision(manifest: dict[str, Any], indices: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Evaluate temporal coverage and literal label support without semantic guessing."""
    _validate(manifest, EVAL_SCHEMA_PATH, "视觉验收集")
    rows: list[dict[str, Any]] = []
    covered_count = 0
    forbidden_hit_count = 0
    required_total = 0
    required_hit_count = 0
    forbidden_claim_total = 0
    forbidden_claim_hit_count = 0

    for case in manifest.get("cases") or []:
        index = indices.get(str(case["asset_id"]))
        if not index:
            rows.append({"case_id": case["case_id"], "status": "missing_index"})
            continue
        validate_material_vision_index(index)
        matching_shots = []
        for shot in index.get("shots") or []:
            span = (float(shot["start_seconds"]), float(shot["end_seconds"]))
            if any(_overlap(span, (float(start), float(end))) > 0 for start, end in case.get("expected_intervals") or []):
                matching_shots.append(shot)
        covered = bool(matching_shots)
        covered_count += int(covered)
        forbidden_hit = any(
            _overlap((float(shot["start_seconds"]), float(shot["end_seconds"])), (float(start), float(end))) > 0
            for shot in matching_shots
            for start, end in case.get("forbidden_intervals") or []
        )
        forbidden_hit_count += int(forbidden_hit)

        searchable = " ".join(
            json.dumps(shot.get("description") or {}, ensure_ascii=False)
            for shot in matching_shots
        ).lower()
        required = [str(value).lower() for value in (case.get("required_entities") or []) + (case.get("required_actions") or [])]
        required_total += len(required)
        hits = [value for value in required if value in searchable]
        required_hit_count += len(hits)
        forbidden_claims = [str(value).lower() for value in case.get("forbidden_claims") or []]
        forbidden_claim_total += len(forbidden_claims)
        claim_hits = [value for value in forbidden_claims if value in searchable]
        forbidden_claim_hit_count += len(claim_hits)
        rows.append({
            "case_id": case["case_id"],
            "status": "evaluated",
            "covered": covered,
            "forbidden_interval_hit": forbidden_hit,
            "required_hits": hits,
            "required_total": len(required),
            "forbidden_claim_hits": claim_hits,
        })

    case_count = len(manifest.get("cases") or [])
    return {
        "corpus_id": manifest.get("corpus_id"),
        "case_count": case_count,
        "temporal_coverage": round(covered_count / case_count, 4) if case_count else 0.0,
        "forbidden_interval_hit_rate": round(forbidden_hit_count / case_count, 4) if case_count else 0.0,
        "required_label_recall": round(required_hit_count / required_total, 4) if required_total else 1.0,
        "unsupported_claim_rate": round(forbidden_claim_hit_count / forbidden_claim_total, 4) if forbidden_claim_total else 0.0,
        "rows": rows,
    }
