from copy import deepcopy

import pytest

from backlot import material_interaction_edit as edit


def fixtures(audio_status="available"):
    index = {
        "status": "completed", "signature": "IDX-1", "duration": 30,
        "source": {"fingerprint": "source-fingerprint"},
        "audio": {"policy": "doubao_transcript" if audio_status == "available" else "disabled",
                  "status": audio_status, "provider": "doubao-test", "utterances": [
                      {"id": "U00001", "start": 2, "end": 3, "text": "机械狗打招呼"},
                      {"id": "U00002", "start": 5, "end": 6, "text": "阿姨回答"},
                      {"id": "U00003", "start": 8, "end": 9, "text": "继续交流"},
                  ]},
    }
    review = {
        "index_signature": "IDX-1", "revision": 4,
        "events": [{"review_event_id": "R0001", "source_event_ids": ["E1"], "group_id": "G1",
                    "status": "kept", "start": 1, "end": 10,
                    "utterance_ids": ["U00001", "U00002", "U00003"]}],
    }
    return index, review


def waiting_refinement(*ranges):
    return {
        "signature": "visual-wait-test", "safe_for_internal_edit": True,
        "original_range": {"start": 1, "end": 10},
        "protected_range": {"start": 1, "end": 10},
        "protected_ranges": [], "warnings": [],
        "annotations": [
            {"id": f"PV{number:03d}", "start": start, "end": end,
             "stage": "waiting", "confidence": .95, "safe_to_shorten": True}
            for number, (start, end) in enumerate(ranges, 1)
        ],
    }


def test_build_plan_compresses_only_silence_confirmed_long_gap():
    index, review = fixtures()
    plan = edit.build_edit_plan(index, review, "R0001", silence_intervals=[
        {"start": 3.05, "end": 4.95},
        # Second transcript gap has no local silence proof and must remain.
    ], refinement=waiting_refinement((3.05, 4.95)))
    assert len(plan["removed_ranges"]) == 1
    removed = plan["removed_ranges"][0]
    assert removed["reason_code"] == "confirmed_silent_wait"
    assert removed["output_gap_seconds"] == .3
    assert removed["start"] >= 3.15 and removed["end"] <= 4.85
    assert plan["source_duration"] == 9
    assert plan["output_duration"] > 7 and plan["output_duration"] < 9
    edit.validate_edit_plan(plan)


@pytest.mark.parametrize("candidate", [
    {"start": 6.4, "end": 7.1, "confidence": .89, "no_related_speech": True,
     "no_key_action": True, "context_preserved": True, "evidence_frame_ids": ["F1", "F2"]},
    {"start": 6.4, "end": 7.1, "confidence": .99, "no_related_speech": True,
     "no_key_action": False, "context_preserved": True, "evidence_frame_ids": ["F1", "F2"]},
    {"start": 5.5, "end": 6.5, "confidence": .99, "no_related_speech": True,
     "no_key_action": True, "context_preserved": True, "evidence_frame_ids": ["F1", "F2"]},
    {"start": 6.4, "end": 7.1, "confidence": .99, "no_related_speech": True,
     "no_key_action": True, "context_preserved": True, "evidence_frame_ids": ["F1"]},
])
def test_visual_drop_requires_every_gate_and_transcript_veto(candidate):
    index, review = fixtures()
    plan = edit.build_edit_plan(index, review, "R0001", irrelevant_candidates=[candidate])
    assert plan["removed_ranges"] == []


def test_high_confidence_irrelevant_visual_can_be_removed_with_two_frames():
    index, review = fixtures()
    plan = edit.build_edit_plan(index, review, "R0001", irrelevant_candidates=[{
        "start": 6.4, "end": 7.1, "confidence": .96, "no_related_speech": True,
        "no_key_action": True, "context_preserved": True,
        "evidence_frame_ids": ["F001", "F002"], "reason": "镜头离题且对话已停止",
    }])
    assert plan["removed_ranges"][0]["reason_code"] == "high_confidence_irrelevant_visual"
    assert plan["keep_ranges"] == [{"start": 1.0, "end": 6.4}, {"start": 7.1, "end": 10.0}]


def test_audio_disabled_never_compresses_dialogue_or_visual_segments():
    index, review = fixtures(audio_status="skipped")
    plan = edit.build_edit_plan(index, review, "R0001", silence_intervals=[{"start": 3, "end": 5}],
                                irrelevant_candidates=[{"start": 6.4, "end": 7.1, "confidence": 1,
                                    "no_related_speech": True, "no_key_action": True, "context_preserved": True,
                                    "evidence_frame_ids": ["F1", "F2"]}])
    assert plan["removed_ranges"] == [] and plan["keep_ranges"] == [{"start": 1.0, "end": 10.0}]


def test_restore_recalculates_timeline_and_revision_then_requires_fresh_qa():
    index, review = fixtures()
    plan = edit.build_edit_plan(
        index, review, "R0001", silence_intervals=[{"start": 3.05, "end": 4.95}],
        refinement=waiting_refinement((3.05, 4.95)),
    )
    shortened = plan["output_duration"]
    restored = edit.apply_plan_action(plan, action="restore_removal", removal_id="D001", expected_revision=0)
    assert restored["revision"] == 1 and restored["output_duration"] == 9
    assert restored["output_duration"] > shortened and restored["qa"]["status"] == "stale"
    with pytest.raises(edit.InteractionEditConflict):
        edit.apply_plan_action(restored, action="reject", expected_revision=0)
    with pytest.raises(edit.InteractionEditError, match="QA"):
        edit.apply_plan_action(restored, action="approve", expected_revision=1)
    undone = edit.apply_plan_action(restored, action="undo", expected_revision=1)
    assert undone["revision"] == 2 and undone["output_duration"] == shortened


def test_render_result_must_match_plan_revision_before_approval():
    index, review = fixtures()
    plan = edit.build_edit_plan(index, review, "R0001")
    manifest = {"plan_id": plan["plan_id"], "plan_revision": 0, "signature": "render-sig",
                "output_duration": 9, "qa": {"status": "passed", "checks": {"duration": True}}}
    rendered = edit.attach_render_result(plan, manifest, expected_revision=0, preview_path="artifacts/candidate.mp4")
    assert rendered["revision"] == 1 and rendered["preview"]["path"].endswith("candidate.mp4")
    approved = edit.apply_plan_action(rendered, action="approve", expected_revision=1)
    assert approved["status"] == "approved"
    with pytest.raises(edit.InteractionEditError, match="不可继续改写"):
        edit.apply_plan_action(approved, action="undo", expected_revision=2)
    with pytest.raises(edit.InteractionEditError, match="不可继续改写"):
        edit.apply_plan_action(approved, action="reject", expected_revision=2)
    with pytest.raises(edit.InteractionEditConflict):
        edit.attach_render_result(rendered, manifest, expected_revision=0, preview_path="stale.mp4")


def test_plan_roundtrip_is_atomic_and_validated(tmp_path):
    index, review = fixtures()
    plan = edit.build_edit_plan(index, review, "R0001")
    path = tmp_path / "interaction-edit-plan.json"
    edit.write_edit_plan(path, plan)
    assert edit.read_edit_plan(path) == plan
    assert not path.with_suffix(".json.tmp").exists()


def test_invalid_overlap_and_changed_index_are_rejected():
    index, review = fixtures()
    review["index_signature"] = "changed"
    with pytest.raises(edit.InteractionEditError, match="已经变化"):
        edit.build_edit_plan(index, review, "R0001")
    index, review = fixtures()
    plan = edit.build_edit_plan(index, review, "R0001")
    broken = deepcopy(plan)
    broken["removed_ranges"] = [
        {"id": "D1", "start": 2, "end": 4, "evidence_ids": ["F1"], "restored": False},
        {"id": "D2", "start": 3, "end": 5, "evidence_ids": ["F2"], "restored": False},
    ]
    with pytest.raises(edit.InteractionEditError, match="重叠"):
        edit.validate_edit_plan(broken)


def test_v2_refinement_expands_boundary_protects_speech_and_maps_output():
    index, review = fixtures()
    index["audio"]["utterances"][0].update({"start": .8, "end": 2.4})
    review["events"][0]["start"] = 2
    refinement = {
        "signature": "refined-v2", "safe_for_internal_edit": True,
        "original_range": {"start": 2, "end": 10},
        "protected_range": {"start": .8, "end": 10},
        "protected_ranges": [
            {"start": .65, "end": 2.55, "evidence_ids": ["U00001"]},
            {"start": 4.85, "end": 6.15, "evidence_ids": ["U00002"]},
        ],
        "annotations": [{"id": "PV001", "start": 2.55, "end": 4.85,
                         "stage": "waiting", "confidence": .95, "safe_to_shorten": True}],
        "warnings": [],
    }
    plan = edit.build_edit_plan(
        index, review, "R0001", refinement=refinement,
        speech_activity={"status": "available", "identity": {"signature": "vad"},
                         "non_speech_ranges": [{"start": 2.55, "end": 4.85}]},
        silence_intervals=[{"start": 2.55, "end": 4.85}],
    )
    assert plan["event_start"] == .8
    assert all(not edit._intersects(row["start"], row["end"], protected["start"], protected["end"])
               for row in plan["removed_ranges"] for protected in plan["protected_ranges"])
    assert plan["timeline_mapping"] == edit.timeline_mapping(plan["keep_ranges"])


def test_batch_save_changes_multiple_ranges_in_one_revision():
    index, review = fixtures()
    plan = edit.build_edit_plan(index, review, "R0001", silence_intervals=[
        {"start": 3.05, "end": 4.95}, {"start": 6.05, "end": 7.95},
    ], refinement=waiting_refinement((3.05, 4.95), (6.05, 7.95)))
    assert len(plan["removed_ranges"]) == 2
    updated = edit.apply_plan_action(
        plan, action="save_edits", expected_revision=0,
        removal_states={"D001": True, "D002": True},
    )
    assert updated["revision"] == 1
    assert all(row["restored"] for row in updated["removed_ranges"])
    assert updated["keep_ranges"] == [{"start": 1.0, "end": 10.0}]
    assert updated["qa"]["status"] == "stale"
