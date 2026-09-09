from __future__ import annotations

from copy import deepcopy

import pytest

from backlot.material_interaction_second_pass import (
    InteractionSecondPassError,
    apply_second_pass_action,
    attach_render_result,
    build_second_pass_plan,
    output_time_to_source,
    read_second_pass_plan,
    source_time_to_outputs,
    write_second_pass_plan,
)
from backlot.material_interaction_story import analyze_story


def _inputs():
    index = {"audio": {"status": "available", "utterances": [
        {"id": "U1", "start": .5, "end": 1.2, "text": "大家好。"},
        {"id": "U2", "start": 2.0, "end": 3.0, "text": "可以拍照吗？"},
        {"id": "U3", "start": 3.2, "end": 4.2, "text": "当然可以。"},
        {"id": "U4", "start": 5.0, "end": 7.6, "text": "三二一，茄子！"},
        {"id": "U5", "start": 8.2, "end": 9.0, "text": "拜拜。"},
    ]}}
    parent = {
        "plan_id": "IEP-parent", "version": "interaction-edit-plan-v2", "revision": 3,
        "preview": {"signature": "preview-parent"}, "source": {"fingerprint": "source-hash"},
        "index_signature": "index-hash", "review_revision": 2, "event_id": "R1", "group_id": "people-a",
        "keep_ranges": [{"start": .4, "end": 9.2}],
    }
    raw = {"summary": "精选", "groups": [
        {"id": "G1", "type": "greeting", "utterance_ids": ["U1"], "decision": "drop", "reason": "寒暄", "depends_on": [], "hook_eligible": False, "hook_score": 0},
        {"id": "G2", "type": "question_answer", "utterance_ids": ["U2", "U3"], "decision": "keep", "reason": "问答", "depends_on": [], "hook_eligible": True, "hook_score": .8},
        {"id": "G3", "type": "action", "utterance_ids": ["U4"], "decision": "keep", "reason": "合照", "depends_on": ["G2"], "hook_eligible": True, "hook_score": 1},
        {"id": "G4", "type": "farewell", "utterance_ids": ["U5"], "decision": "drop", "reason": "告别", "depends_on": [], "hook_eligible": False, "hook_score": 0},
    ], "hook_candidates": [{"id": "H1", "group_ids": ["G3"], "reason": "结果明确"}]}
    options = {"speed": 1.25, "target_min_seconds": 3, "target_max_seconds": 20}
    # Public options enforce production bounds; keep the fixture in that range.
    options = {"speed": 1.25, "target_min_seconds": 15, "target_max_seconds": 20}
    story, identity = analyze_story(index, parent, options, analyze=lambda context: (raw, "model-x"))
    plan = build_second_pass_plan(
        parent_plan=parent, story=story, utterances=index["audio"]["utterances"],
        options=options, story_identity=identity,
    )
    return index, parent, plan


def test_default_hook_moves_source_out_of_body_and_keeps_mapping_unique():
    _, _, plan = _inputs()
    assert plan["selected_hook_id"] == "H1"
    assert plan["options"]["hook_mode"] == "move"
    assert [row["role"] for row in plan["occurrences"]] == ["hook", "body"]
    matches = source_time_to_outputs(plan, 6.0)
    assert [row["role"] for row in matches] == ["hook"]
    inverse = output_time_to_source(plan, matches[0]["output_seconds"])
    assert inverse["occurrence_id"] == matches[0]["occurrence_id"]
    assert inverse["source_seconds"] == pytest.approx(6.0)
    assert len([row for row in plan["subtitle_cues"] if row["utterance_id"] == "U4"]) == 1
    assert plan["repeated_source_seconds"] == 0


def test_explicit_preview_repeat_keeps_two_occurrences_and_reports_overlap():
    index, parent, plan = _inputs()
    repeated = build_second_pass_plan(
        parent_plan=parent, story=plan["story"], utterances=index["audio"]["utterances"],
        options={**plan["options"], "hook_mode": "repeat"}, story_identity=plan["story_identity"],
    )
    assert [row["role"] for row in repeated["occurrences"]] == ["hook", "body", "body"]
    assert [row["role"] for row in source_time_to_outputs(repeated, 6.0)] == ["hook", "body"]
    assert repeated["repeated_source_seconds"] > 0
    assert repeated["content_qa"]["status"] == "passed"


def test_legacy_v1_repeat_plan_remains_readable(tmp_path):
    index, parent, plan = _inputs()
    legacy = build_second_pass_plan(
        parent_plan=parent, story=plan["story"], utterances=index["audio"]["utterances"],
        options={**plan["options"], "hook_mode": "repeat"}, story_identity=plan["story_identity"],
    )
    legacy["version"] = "interaction-second-pass-plan-v1"
    legacy["options"].pop("hook_mode", None)
    legacy.pop("repeated_source_seconds", None)
    legacy.pop("content_qa", None)
    path = tmp_path / legacy["plan_id"] / "interaction-second-pass-plan.json"
    write_second_pass_plan(path, legacy)
    restored = read_second_pass_plan(path)
    assert restored["version"] == "interaction-second-pass-plan-v1"
    assert [row["role"] for row in restored["occurrences"]] == ["hook", "body", "body"]


def test_dependency_break_and_locked_group_changes_are_rejected():
    _, _, plan = _inputs()
    with pytest.raises(InteractionSecondPassError, match="依赖前文"):
        apply_second_pass_action(plan, action="save_edits", expected_revision=plan["revision"], group_states={"G2": False})
    plan = apply_second_pass_action(plan, action="save_edits", expected_revision=plan["revision"], locked_states={"G2": True})
    with pytest.raises(InteractionSecondPassError, match="已锁定"):
        apply_second_pass_action(plan, action="save_edits", expected_revision=plan["revision"], group_states={"G2": False})


def test_save_edits_changes_speed_once_and_marks_old_preview_stale():
    _, _, plan = _inputs()
    plan["qa"] = {"status": "passed"}
    plan["preview"] = {"path": "old.mp4", "signature": "old", "stale": False}
    updated = apply_second_pass_action(
        plan, action="save_edits", expected_revision=plan["revision"], speed=1.1, hook_candidate_id="__none__",
    )
    assert updated["options"]["speed"] == 1.1
    assert updated["selected_hook_id"] is None
    assert updated["qa"]["status"] == "stale"
    assert updated["preview"]["stale"] is True
    assert updated["revision"] == plan["revision"] + 1


def test_plan_persistence_and_approval_gate(tmp_path):
    _, _, plan = _inputs()
    path = tmp_path / plan["plan_id"] / "interaction-second-pass-plan.json"
    write_second_pass_plan(path, plan)
    assert read_second_pass_plan(path)["plan_id"] == plan["plan_id"]
    with pytest.raises(InteractionSecondPassError, match="QA"):
        apply_second_pass_action(plan, action="approve", expected_revision=plan["revision"])
    manifest = {"plan_id": plan["plan_id"], "plan_revision": plan["revision"], "signature": "render", "qa": {"status": "passed"}, "output_duration": plan["output_duration"]}
    rendered = attach_render_result(plan, manifest, expected_revision=plan["revision"], preview_path="artifacts/preview.mp4")
    approved = apply_second_pass_action(rendered, action="approve", expected_revision=rendered["revision"])
    assert approved["status"] == "approved"


def test_over_maximum_plan_keeps_preview_but_blocks_approval():
    parent = {
        "plan_id": "IEP-long", "version": "interaction-edit-plan-v2", "revision": 1,
        "preview": {"signature": "parent"}, "source": {"fingerprint": "source"},
        "index_signature": "index", "review_revision": 1, "event_id": "R-long", "group_id": "people",
        "keep_ranges": [{"start": 0.0, "end": 22.0}],
    }
    utterances = [{"id": "U-long", "start": .2, "end": 20.2, "text": "完整长对话"}]
    story = {
        "groups": [{"id": "G-long", "sequence": 1, "type": "question_answer",
                    "utterance_ids": ["U-long"], "summary": "完整长对话", "decision": "keep",
                    "selected": True, "locked": False, "reason": "必须完整保留", "depends_on": [],
                    "hook_eligible": False, "hook_score": 0.0, "evidence_ids": [],
                    "source_ranges": [{"start": .05, "end": 20.35}],
                    "source_range": {"start": .05, "end": 20.35}}],
        "hook_candidates": [], "recommended_hook_id": None, "warnings": [], "summary": "长对话",
    }
    plan = build_second_pass_plan(
        parent_plan=parent, story=story, utterances=utterances,
        options={"speed": 1.0, "hook_enabled": False, "target_min_seconds": 15, "target_max_seconds": 15},
        story_identity={"model": "fixture"},
    )
    assert plan["content_qa"]["status"] == "needs_adjustment"
    rendered = attach_render_result(
        plan, {"plan_id": plan["plan_id"], "plan_revision": 0, "signature": "render",
               "qa": {"status": "passed"}, "output_duration": plan["output_duration"]},
        expected_revision=0, preview_path="preview.mp4",
    )
    assert rendered["preview"]["path"] == "preview.mp4"
    with pytest.raises(InteractionSecondPassError, match="超出目标"):
        apply_second_pass_action(rendered, action="approve", expected_revision=rendered["revision"])


def test_parent_source_ranges_cannot_be_silently_bypassed():
    _, _, plan = _inputs()
    broken = deepcopy(plan)
    broken["story"]["groups"][1]["source_range"]["start"] = .1
    broken["story"]["groups"][1]["source_ranges"][0]["start"] = .1
    with pytest.raises(InteractionSecondPassError, match="越出"):
        apply_second_pass_action(broken, action="save_edits", expected_revision=broken["revision"], speed=1.1)


def test_first_pass_removed_pause_is_not_resurrected_inside_one_semantic_group():
    index, parent, _ = _inputs()
    parent["keep_ranges"] = [{"start": .4, "end": 3.05}, {"start": 3.15, "end": 9.2}]
    raw = {
        "groups": [
            {"id": "G1", "type": "greeting", "utterance_ids": ["U1"], "decision": "drop", "reason": "寒暄", "depends_on": [], "hook_eligible": False, "hook_score": 0},
            {"id": "G2", "type": "question_answer", "utterance_ids": ["U2", "U3"], "decision": "keep", "reason": "问答", "depends_on": [], "hook_eligible": False, "hook_score": .7},
            {"id": "G3", "type": "action", "utterance_ids": ["U4"], "decision": "keep", "reason": "跨父候选安全删段", "depends_on": [], "hook_eligible": False, "hook_score": .8},
            {"id": "G4", "type": "farewell", "utterance_ids": ["U5"], "decision": "drop", "reason": "告别", "depends_on": [], "hook_eligible": False, "hook_score": 0},
        ],
        "hook_candidates": [],
    }
    options = {"speed": 1.0, "hook_enabled": False, "target_min_seconds": 15, "target_max_seconds": 20}
    story, identity = analyze_story(index, parent, options, analyze=lambda context: (raw, "model-x"))
    plan = build_second_pass_plan(
        parent_plan=parent, story=story, utterances=index["audio"]["utterances"],
        options=options, story_identity=identity,
    )
    answer_occurrences = [
        row for row in plan["occurrences"] if "G2" in row["group_ids"]
    ]
    assert [(row["source_start"], row["source_end"]) for row in answer_occurrences] == [
        (1.85, 3.05), (3.15, 4.35),
    ]


def test_non_numeric_speed_is_reported_as_a_domain_error():
    _, parent, plan = _inputs()
    with pytest.raises(InteractionSecondPassError, match="播放速度格式无效"):
        build_second_pass_plan(
            parent_plan=parent, story=plan["story"], utterances=plan["utterances"],
            options={"speed": "fast"}, story_identity=plan["story_identity"],
        )
