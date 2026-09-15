from __future__ import annotations

import pytest

from backlot.material_interaction_story import (
    InteractionStoryError,
    analyze_story,
    build_story_context,
    normalize_story_analysis,
)


def _index():
    # Deliberately free of opening/closing words so the deterministic edge anchor
    # stays quiet: grouping, dependency and range tests must pin one behaviour at
    # a time.  The anchor has its own tests further down.
    return {
        "audio": {"status": "available", "utterances": [
            {"id": "U1", "start": .5, "end": 1.2, "text": "今天人不算多。"},
            {"id": "U2", "start": 1.8, "end": 2.6, "text": "你叫什么名字？"},
            {"id": "U3", "start": 2.8, "end": 3.6, "text": "我叫月仔。"},
            {"id": "U4", "start": 4.0, "end": 5.2, "text": "我们拍张照吧。"},
            {"id": "U5", "start": 5.4, "end": 6.8, "text": "三二一，茄子！"},
            {"id": "U6", "start": 8.0, "end": 9.0, "text": "那就先这样。"},
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
    assert context["unit_source"] == "asr_utterance"
    result = normalize_story_analysis(_raw(), context)
    assert [row["id"] for row in result["groups"] if row["selected"]] == ["G2", "G3"]
    assert result["hook_candidates"][0]["group_ids"] == ["G3"]
    assert result["hook_candidates"][0]["source_range"] == {"start": 3.85, "end": 6.95}
    assert result["recommended_hook_id"] == "H1"


def test_edge_anchor_uses_the_opening_and_closing_words_as_the_clip_edges():
    # Anything before the first opening word and after the last closing word is
    # dropped, and the two groups holding those words are kept even though the
    # model called them redundant chatter — they define the clip's boundary.
    index = {"audio": {"status": "available", "utterances": [
        {"id": "U1", "start": .2, "end": .8, "text": "哇 好多人排队。"},
        {"id": "U2", "start": 1.0, "end": 1.6, "text": "你好呀。"},
        {"id": "U3", "start": 2.0, "end": 2.8, "text": "可以合影吗？"},
        {"id": "U4", "start": 3.0, "end": 3.8, "text": "拜拜。"},
        {"id": "U5", "start": 4.2, "end": 5.0, "text": "然后大家就去吃饭了。"},
    ]}, "events": []}
    parent = {"group_id": "g", "keep_ranges": [{"start": .1, "end": 5.2}]}
    raw = {"summary": "s", "groups": [
        {"id": "G1", "type": "other", "utterance_ids": ["U1"], "decision": "keep", "reason": "开场铺垫",
         "depends_on": [], "hook_eligible": False, "hook_score": 0},
        {"id": "G2", "type": "greeting", "utterance_ids": ["U2"], "decision": "drop", "reason": "寒暄",
         "depends_on": [], "hook_eligible": False, "hook_score": 0},
        {"id": "G3", "type": "question_answer", "utterance_ids": ["U3"], "decision": "keep", "reason": "问答",
         "depends_on": ["G1"], "hook_eligible": False, "hook_score": .7},
        {"id": "G4", "type": "farewell", "utterance_ids": ["U4"], "decision": "drop", "reason": "告别",
         "depends_on": [], "hook_eligible": False, "hook_score": 0},
        {"id": "G5", "type": "other", "utterance_ids": ["U5"], "decision": "keep", "reason": "尾随",
         "depends_on": [], "hook_eligible": False, "hook_score": 0},
    ], "hook_candidates": []}
    context = build_story_context(index, parent, {})
    result = normalize_story_analysis(raw, context)
    assert [row["id"] for row in result["groups"] if row["selected"]] == ["G2", "G3", "G4"]
    by_id = {row["id"]: row for row in result["groups"]}
    assert "片头/片尾锚定词" in by_id["G2"]["reason"]
    assert "之前" in by_id["G1"]["reason"] and "之后" in by_id["G5"]["reason"]
    # The head anchor declares that the preceding context is no longer required,
    # so a dependency pointing at a dropped group must not resurrect it.
    assert by_id["G3"]["depends_on"] == []
    assert any("裁掉首尾" in note for note in result["warnings"])


def test_edge_anchor_says_so_when_no_opening_or_closing_word_exists():
    context = build_story_context(_index(), _parent(), {})
    result = normalize_story_analysis(_raw(), context)
    assert any("未找到明确的开场或告别词" in note for note in result["warnings"])
    assert [row["id"] for row in result["groups"] if row["selected"]] == ["G2", "G3"]


def test_trim_never_force_drops_a_group_the_model_argued_to_keep():
    # The old behaviour deleted a whole 13-second leading greeting group even
    # though the model had argued to keep it — the exact "掐头去尾太狠" the user
    # rejected.  Without a lexical hit the edge is left to the model.
    raw = _raw()
    for group in raw["groups"]:
        group["decision"] = "keep"
    context = build_story_context(_index(), _parent(), {})
    result = normalize_story_analysis(raw, context)
    assert [row["id"] for row in result["groups"] if row["selected"]] == ["G1", "G2", "G3", "G4"]
    first = next(row for row in result["groups"] if row["id"] == "G1")
    # Nothing overwrote the model's own reason, because nothing overrode it.
    assert first["reason"] == "独立寒暄"


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


def test_evidence_shown_under_an_event_is_valid_even_when_frames_is_capped():
    """The exposure cap must not turn a real frame id into an "invented" one.

    ``visual_evidence.frames`` is capped at 12 ids, while an event's highlights
    carry their own frame ids.  The acceptance run cited exactly such an id (the
    cap had dropped it from ``frames``), was judged to have invented evidence and
    threw away a paid analysis — the guard must police fabrication, not truncation.
    """
    index = _index()
    frames = [f"F{i:03d}" for i in range(1, 21)]
    index["events"][0]["evidence_frame_ids"] = frames
    index["events"][0]["highlights"] = [{"label": "合影", "frame_id": frames[-1], "time": 5.0}]
    index["frames"] = [{"id": value, "pts": float(position)} for position, value in enumerate(frames)]
    context = build_story_context(index, _parent(), {})
    exposed = context["visual_evidence"]
    assert len(exposed["frames"]) <= 12
    late = frames[-1]
    assert late not in [row["id"] for row in exposed["frames"]]
    assert late == exposed["events"][0]["highlights"][0]["frame_id"]
    raw = _raw()
    raw["groups"][2]["evidence_ids"] = [late]
    result = normalize_story_analysis(raw, context)
    assert next(row for row in result["groups"] if row["id"] == "G3")["evidence_ids"] == [late]
    # Fabrication is still rejected.
    raw = _raw()
    raw["groups"][2]["evidence_ids"] = ["FAKE-FRAME"]
    with pytest.raises(InteractionStoryError, match="画面证据"):
        normalize_story_analysis(raw, context)


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
    assert story["version"] == "interaction-story-v4"
    assert identity["model"] == "configured-model"
    assert identity["context_signature"]
    assert identity["unit_source"] == "asr_utterance"


def test_unit_atoms_replace_utterances_when_they_are_supplied():
    units = [
        {"id": "P0001", "start": 1.0, "end": 2.0, "text": "你好呀", "utterance_id": "U1"},
        {"id": "P0002", "start": 3.0, "end": 4.0, "text": "拜拜", "utterance_id": "U2"},
    ]
    context = build_story_context(_index(), _parent(), {}, units=units)
    assert context["unit_source"] == "vad_aligned"
    assert context["unit_count"] == 2
    assert [row["id"] for row in context["utterances"]] == ["P0001", "P0002"]
