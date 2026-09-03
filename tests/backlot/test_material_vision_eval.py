from __future__ import annotations

import json
from pathlib import Path

from backlot.material_vision_eval import evaluate_material_vision, load_acceptance_manifest, validate_material_vision_index


def _index(tmp_path: Path) -> dict:
    frame = tmp_path / "frame.jpg"
    frame.write_bytes(b"fixture")
    return {
        "version": 2,
        "status": "completed",
        "source": {"path": str(tmp_path / "duck.mp4"), "fingerprint": "12345678abcdef", "name": "duck.mp4"},
        "signature": "a" * 64,
        "config": {},
        "probe": {"duration_seconds": 4},
        "shots": [{
            "shot_id": "SHOT-0001", "start_seconds": 1.0, "end_seconds": 3.0,
            "frames": [{
                "frame_id": "FRAME-00001", "time_seconds": 2.0, "path": str(frame),
                "content_sha256": "b" * 64, "dhash": "0" * 16, "sharpness": 10,
                "selected_for_vision": True,
            }],
            "description": {
                "summary": "黄色机器鸭向前行走",
                "entities": [{"name": "机器鸭", "confidence": .9, "evidence_frame_ids": ["FRAME-00001"]}],
                "actions": [{"name": "向前行走", "confidence": .8, "evidence_frame_ids": ["FRAME-00001"]}],
            },
        }],
        "vision": {"status": "completed"},
        "index_path": str(tmp_path / "index.json"),
    }


def test_acceptance_manifest_and_metrics_are_schema_backed(tmp_path: Path) -> None:
    manifest_path = tmp_path / "eval.json"
    manifest_path.write_text(json.dumps({
        "version": 1,
        "corpus_id": "duck-v1",
        "sources": [{"asset_id": "S-001", "fingerprint": "12345678abcdef"}],
        "cases": [{
            "case_id": "walking", "asset_id": "S-001", "mode": "vision_only",
            "expected_intervals": [[1, 3]], "forbidden_intervals": [[8, 9]],
            "required_entities": ["机器鸭"], "required_actions": ["向前行走"],
            "forbidden_claims": ["人物骑行"],
        }],
    }, ensure_ascii=False), encoding="utf-8")
    manifest = load_acceptance_manifest(manifest_path)
    index = _index(tmp_path)
    validate_material_vision_index(index)

    result = evaluate_material_vision(manifest, {"S-001": index})

    assert result["temporal_coverage"] == 1
    assert result["required_label_recall"] == 1
    assert result["unsupported_claim_rate"] == 0
