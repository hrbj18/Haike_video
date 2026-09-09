from __future__ import annotations

import pytest

from backlot.material_interaction_story import (
    InteractionStoryError,
    analyze_story,
    build_story_context,
    normalize_story_analysis,
)


def _index():
    return {
        "audio": {"status": "available", "utterances": [
            {"id": "U1", "start": .5, "end": 1.2, "text": "姐姐们好。"},
            {"id": "U2", "start": 1.8, "end": 2.6, "text": "你叫什么名字？"},
            {"id": "U3", "start": 2.8, "end": 3.6, "text": "我叫月仔。"},
            {"id": "U4", "start": 4.0, "end": 5.2, "text": "我们拍张照吧。"},
            {"id": "U5", "start": 5.4, "end": 6.8, "text": "三二一，茄子！"},
            {"id": "U6", "start": 8.0, "end": 9.0, "text": "拜拜。"},
        ]},
        "events": [{
            "event_id": "E1", "group_id": "people-a", "participants": "四位阿姨",
            "summary": "提出合影并完成跳起动作", "recommend_reason": "动作完整",
            "evidence_frame_ids": ["F1"],
            "highlights": [{"label": "合影", "frame_id": "F1", "time": 5.0}],
        }],
        "frames": [{"id": "F1", "pts": 5.0, "path": "must-not-leak.jpg"}],
    }


def _parent():
    return {"group_id": "people-a", "keep_ranges": [{"start": .4, "end": 9.2}]}


def _raw():
    return {
        "summary": "保留问答和合照，删除寒暄告别",
        "groups": [
            {"id": "G1", "type": "greeting", "utterance_ids": ["U1"], "decision": "drop", "reason": "独立寒暄", "depends_on": [], "hook_eligible": False, "hook_score": 0},
            {"id": "G2", "type": "question_answer", "utterance_ids": ["U2", "U3"], "decision": "keep", "reason": "完整问答", "depends_on": [], "hook_eligible": True, "hook_score": .7},
            {"id": "G3", "type": "action", "utterance_ids": ["U4", "U5"], "decision": "keep", "reason": "合照动作", "depends_on": ["G2"], "hook_eligible": True, "hook_score": .95},
            {"id": "G4", "type": "farewell", "utterance_ids": ["U6"], "decision": "drop", "reason": "独立告别", "depends_on": [], "hook_eligible": False, "hook_score": 0},
        ],
        "hook_candidates": [{"id": "H1", "group_ids": ["G3"], "reason": "动作明确"}],
    }


def test_normalize_story_uses_only_frozen_utterance_ranges():
    context = build_story_context(_index(), _parent(), {"extract_highlights": True})
    assert context["visual_evidence"]["frames"] == [{"id": "F1", "pts": 5.0}]
    assert "must-not-leak" not in str(context)
    result = normalize_story_analysis(_raw(), context)
    assert [row["id"] for row in result["groups"] if row["selected"]] == ["G2", "G3"]
    assert result["hook_candidates"][0]["group_ids"] == ["G3"]
    assert result["hook_candidates"][0]["source_range"] == {"start": 3.85, "end": 6.95}
    assert result["recommended_hook_id"] == "H1"


def test_missing_model_utterance_is_conservatively_retained():
    raw = _raw()
    raw["groups"] = raw["groups"][:-1]
    context = build_story_context(_index(), _parent(), {})
    result = normalize_story_analysis(raw, context)
    automatic = next(row for row in result["groups"] if row["id"].startswith("G-AUTO"))
    assert automatic["utterance_ids"] == ["U6"]
    assert automatic["selected"] is True
    assert "遗漏 1 条" in result["warnings"][0]


def test_unknown_or_non_contiguous_evidence_is_rejected():
    context = build_story_context(_index(), _parent(), {})
    raw = _raw()
    raw["groups"][0]["utterance_ids"] = ["FAKE"]
    with pytest.raises(InteractionStoryError, match="不存在"):
        normalize_story_analysis(raw, context)
    raw = _raw()
    raw["groups"][0]["utterance_ids"] = ["U1", "U3"]
    with pytest.raises(InteractionStoryError, match="连续"):
        normalize_story_analysis(raw, context)
    raw = _raw()
    raw["groups"][2]["evidence_ids"] = ["FAKE-FRAME"]
    with pytest.raises(InteractionStoryError, match="画面证据"):
        normalize_story_analysis(raw, context)


def test_known_visual_evidence_is_preserved_on_group():
    context = build_story_context(_index(), _parent(), {})
    raw = _raw()
    raw["groups"][2]["evidence_ids"] = ["F1"]
    result = normalize_story_analysis(raw, context)
    assert next(row for row in result["groups"] if row["id"] == "G3")["evidence_ids"] == ["F1"]


def test_semantic_group_preserves_first_pass_removed_pause_as_multiple_ranges():
    parent = {"group_id": "people-a", "keep_ranges": [
        {"start": .4, "end": 5.2},
        {"start": 5.35, "end": 9.2},
    ]}
    context = build_story_context(_index(), parent, {})
    result = normalize_story_analysis(_raw(), context)
    action = next(row for row in result["groups"] if row["id"] == "G3")
    assert action["source_ranges"] == [
        {"start": 3.85, "end": 5.2},
        {"start": 5.35, "end": 6.95},
    ]
    assert action["source_range"] == {"start": 3.85, "end": 6.95}


def test_dependency_is_restored_when_model_keeps_a_dependent_group():
    raw = _raw()
    raw["groups"][1]["decision"] = "drop"
    context = build_story_context(_index(), _parent(), {})
    result = normalize_story_analysis(raw, context)
    assert next(row for row in result["groups"] if row["id"] == "G2")["selected"] is True


def test_analyze_story_records_model_identity():
    story, identity = analyze_story(
        _index(), _parent(), {}, analyze=lambda context: (_raw(), "configured-model")
    )
    assert story["version"] == "interaction-story-v2"
    assert identity["model"] == "configured-model"
    assert identity["context_signature"]
