from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from backlot.material_interaction_second_pass import (
    InteractionSecondPassError,
    apply_second_pass_action,
    attach_render_result,
    build_second_pass_plan,
    output_time_to_source,
    read_second_pass_plan,
    source_time_to_outputs,
    subtitle_cues,
    write_second_pass_plan,
)
from backlot.material_interaction_story import analyze_story


def _inputs():
    # The fixture text deliberately avoids opening/closing words so the
    # deterministic edge anchor stays quiet here; it has its own tests in
    # ``test_material_interaction_story.py`` and the hook/occurrence arithmetic
    # below must not silently depend on where the anchor lands.
    index = {"audio": {"status": "available", "utterances": [
        {"id": "U1", "start": .5, "end": 1.2, "text": "今天人挺多。"},
        {"id": "U2", "start": 2.0, "end": 3.0, "text": "可以拍照吗？"},
        {"id": "U3", "start": 3.2, "end": 4.2, "text": "当然可以。"},
        {"id": "U4", "start": 5.0, "end": 7.6, "text": "三二一，茄子！"},
        {"id": "U5", "start": 8.2, "end": 9.0, "text": "那就先这样吧。"},
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
    legacy = _downgrade_to_v1(legacy)
    path = tmp_path / legacy["plan_id"] / "interaction-second-pass-plan.json"
    write_second_pass_plan(path, legacy)
    restored = read_second_pass_plan(path)
    assert restored["version"] == "interaction-second-pass-plan-v1"
    assert [row["role"] for row in restored["occurrences"]] == ["hook", "body", "body"]


# --- v1/v2/v3 must stay readable (N7) ---------------------------------------

V4_ONLY_OPTION_KEYS = (
    "pause_handling", "pause_speed", "audio_fade", "audio_fade_ms", "edge_fade", "edge_fade_ms",
    "pause_margin_head_seconds", "pause_margin_tail_seconds", "window_context_policy",
    "interaction_concurrency", "asr_concurrency",
)
# v5 introduced per-phrase captions; the options that tune them must never appear
# on a plan written by an older build.
V5_ONLY_OPTION_KEYS = ("subtitle_max_chars", "subtitle_max_seconds", "subtitle_min_seconds")
# v6 introduced the target-duration policy and the pause strength preset.
V6_ONLY_OPTION_KEYS = ("duration_policy", "pause_preset")


def _downgrade_to_version(plan, version):
    """Turn a freshly built plan into a faithful older-version plan.

    The newer-only inputs (options, occurrence actions, ``speed_segments``) are
    removed and the derived fields are then recomputed by the same ``_rebuild``
    the reader uses, so the result is exactly what a real historical plan written
    by an older build would look like.
    """
    from backlot.material_interaction_second_pass import _rebuild

    row = deepcopy(plan)
    row["version"] = version
    if version == "interaction-second-pass-plan-v1":
        row["options"].pop("hook_mode", None)
    for key in V4_ONLY_OPTION_KEYS + V5_ONLY_OPTION_KEYS + V6_ONLY_OPTION_KEYS:
        row["options"].pop(key, None)
    row.pop("spoken_units", None)
    row.pop("unit_signature", None)
    row.pop("unit_source", None)
    for occurrence in row["occurrences"]:
        occurrence.pop("actions", None)
    row.pop("speed_segments", None)
    return _rebuild(row)


def _legacy_subtitle_cues(plan):
    """The pre-v5 derivation: one cue per utterance per occurrence, whole text.

    Reproduced verbatim so a fabricated older plan carries exactly the captions a
    real older build wrote -- the "198 characters, four times" shape.
    """
    by_group = {row["id"]: row for row in (plan.get("story") or {}).get("groups") or []}
    by_utterance = {row["id"]: row for row in plan.get("utterances") or []}
    result = []
    for occurrence in plan.get("timeline_mapping") or []:
        utterance_ids = []
        for group_id in occurrence.get("group_ids") or []:
            utterance_ids.extend((by_group.get(group_id) or {}).get("utterance_ids") or [])
        for utterance_id in dict.fromkeys(utterance_ids):
            utterance = by_utterance.get(utterance_id)
            if not utterance:
                continue
            source_start = max(float(utterance["start"]), float(occurrence["source_start"]))
            source_end = min(float(utterance["end"]), float(occurrence["source_end"]))
            if source_end <= source_start:
                continue
            output_start = float(occurrence["output_start"]) + \
                (source_start - float(occurrence["source_start"])) / float(occurrence["speed"])
            output_end = float(occurrence["output_start"]) + \
                (source_end - float(occurrence["source_start"])) / float(occurrence["speed"])
            result.append({
                "cue_id": f"{occurrence['occurrence_id']}:{utterance_id}",
                "occurrence_id": occurrence["occurrence_id"], "role": occurrence["role"],
                "utterance_id": utterance_id, "text": str(utterance["text"]),
                "source_start": round(source_start, 6), "source_end": round(source_end, 6),
                "output_start": round(output_start, 6), "output_end": round(output_end, 6),
            })
    return result


def _downgrade_to_v1(plan):
    return _downgrade_to_version(plan, "interaction-second-pass-plan-v1")


def test_v1_v2_v3_history_plans_still_read_and_export(tmp_path):
    """历史计划读取即通过：新字段绝不能被套到旧版本上（否则 1334 项回归被污染）。"""
    _, _, plan = _inputs()
    for version in ("interaction-second-pass-plan-v1",
                    "interaction-second-pass-plan-v2",
                    "interaction-second-pass-plan-v3"):
        downgraded = _downgrade_to_version(plan, version)
        path = tmp_path / version / downgraded["plan_id"] / "interaction-second-pass-plan.json"
        write_second_pass_plan(path, downgraded)
        restored = read_second_pass_plan(path)
        assert restored["version"] == version
        assert "speed_segments" not in restored
        assert all("actions" not in row for row in restored["occurrences"])


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


# --- pause compression -------------------------------------------------------

# Geometry-sensitive cases pin the preset explicitly so they keep testing the
# arithmetic instead of silently tracking whatever preset ships.  They also pin
# the ``quiet`` gap policy, because the shipped default (``any``) also cuts the
# occurrence's head and tail and would swamp the three-permission arithmetic they
# exist to check.
TEST_PRESET = {"pause_guard_seconds": 0.15, "pause_target_gap_seconds": 0.3,
               "pause_min_seconds": 0.6, "gap_policy": "quiet"}


def _pause_inputs(*, silences=((2.5, 6.0),), speech=((0.0, 2.0), (6.5, 20.0)), target_min=15,
                  target_max=60, pause_options=None, body_end=19.95):
    parent = {
        "plan_id": "IEP-pause", "version": "interaction-edit-plan-v2", "revision": 1,
        "preview": {"signature": "parent"}, "source": {"fingerprint": "source"},
        "index_signature": "index", "review_revision": 1, "event_id": "R-pause", "group_id": "people",
        "keep_ranges": [{"start": 0.0, "end": 20.0}],
    }
    utterances = [
        {"id": "U1", "start": .2, "end": 2.0, "text": "你好。"},
        {"id": "U2", "start": 6.5, "end": 19.8, "text": "聊很久。"},
    ]
    story = {
        "groups": [{"id": "G1", "sequence": 1, "type": "question_answer", "utterance_ids": ["U1", "U2"],
                    "summary": "完整对话", "decision": "keep", "selected": True, "locked": False,
                    "reason": "保留完整对话", "depends_on": [], "hook_eligible": False, "hook_score": 0.0,
                    "evidence_ids": [],
                    "source_ranges": [{"start": 0.05, "end": body_end}],
                    "source_range": {"start": 0.05, "end": body_end}}],
        "hook_candidates": [], "recommended_hook_id": None, "warnings": [], "summary": "对话",
    }
    options = {"speed": 1.0, "hook_enabled": False, "target_min_seconds": target_min,
               "target_max_seconds": target_max, **TEST_PRESET}
    options.update(pause_options or {})
    evidence = {
        "version": "material-pause-evidence-v1", "status": "available",
        "identity": {"signature": "pause-identity"}, "source_fingerprint": "source",
        "silences": [{"start": start, "end": end} for start, end in silences],
    }
    return parent, utterances, story, options, evidence, [{"start": a, "end": b} for a, b in speech]


def _pause_plan(**kwargs):
    parent, utterances, story, options, evidence, speech = _pause_inputs(**kwargs)
    return build_second_pass_plan(
        parent_plan=parent, story=story, utterances=utterances, options=options,
        story_identity={"model": "fixture"}, pause_evidence=evidence, speech_ranges=speech,
    )


def test_shipped_pause_preset_is_explicit_and_well_ordered():
    from backlot import material_interaction_second_pass as module

    normalized = module.normalize_options({})
    assert normalized["compress_pauses"] is True
    assert normalized["burn_subtitles"] is True
    assert normalized["pause_scope"] == "body"
    # The shipped default is the tight preset, and every one of its numbers comes
    # from the preset table rather than from the module-level fallbacks.
    assert normalized["pause_preset"] == module.PAUSE_PRESET_DEFAULT == "tight"
    preset = module.PAUSE_PRESETS[module.PAUSE_PRESET_DEFAULT]
    assert normalized["pause_guard_seconds"] == preset["pause_guard_seconds"]
    assert normalized["pause_target_gap_seconds"] == preset["pause_target_gap_seconds"]
    assert normalized["pause_min_seconds"] == preset["pause_min_seconds"]
    # v7: `pause_target_gap_seconds` is the **surviving** gap between two adjacent
    # lines, and the guard is only a floor — it can no longer add itself on top of
    # the target (that is why a "0.3 s gap" used to be unreachable).
    assert module.pause_survivor_seconds(normalized) == pytest.approx(0.30)
    assert module.pause_survivor_seconds({"pause_target_gap_seconds": 0.06,
                                          "pause_guard_seconds": 0.10}) == pytest.approx(0.20)
    for preset_name, survivor in (("conservative", 0.50), ("standard", 0.40), ("tight", 0.30)):
        assert module.pause_survivor_seconds(
            module.normalize_options({"pause_preset": preset_name})) == pytest.approx(survivor)
    # The presets also carry the probe floor and the bridge width: a plan layer
    # that promises 0.30 s while the prober cannot see the gaps is exactly the
    # "implemented but does nothing" failure this preset table exists to prevent.
    assert module.pause_probe_min_silence(normalized) == preset["probe_min_silence_seconds"]
    assert module.pause_probe_bridge_seconds(normalized) == preset["probe_bridge_seconds"]
    with pytest.raises(InteractionSecondPassError, match="停顿压缩范围无效"):
        module.normalize_options({"pause_scope": "everything"})
    with pytest.raises(InteractionSecondPassError, match="停顿压缩强度无效"):
        module.normalize_options({"pause_preset": "extreme"})



def test_an_explicit_number_beats_the_pause_preset():
    from backlot import material_interaction_second_pass as module

    normalized = module.normalize_options({"pause_preset": "tight", "pause_guard_seconds": 0.2,
                                           "pause_target_gap_seconds": 0.4, "pause_min_seconds": 1.0})
    assert normalized["pause_preset"] == "tight"
    assert normalized["pause_guard_seconds"] == 0.2
    assert normalized["pause_target_gap_seconds"] == 0.4
    assert normalized["pause_min_seconds"] == 1.0


def test_dead_air_between_two_lines_is_removed_and_the_pieces_are_reported():
    plan = _pause_plan()
    assert plan["version"] == "interaction-second-pass-plan-v7"
    assert plan["speed_segments"] == []
    assert all(row["actions"][0]["reason"] == "preset" for row in plan["occurrences"])
    assert [row["trim_id"] for row in plan["pause_trims"]] == ["PT001"]
    trim = plan["pause_trims"][0]
    # Silence 2.5-6.0 with a 0.30 s target: 0.15 s of untouched audio on each side.
    assert (trim["source_start"], trim["source_end"]) == (2.65, 5.85)
    assert trim["removed_seconds"] == pytest.approx(3.2)
    assert trim["kept_pause_seconds"] == pytest.approx(0.30)
    assert [(row["source_start"], row["source_end"]) for row in plan["occurrences"]] == [
        (0.05, 2.65), (5.85, 19.95),
    ]
    assert [row["occurrence_id"] for row in plan["occurrences"]] == [
        "O-BODY-001-P01", "O-BODY-001-P02",
    ]
    assert plan["removed_by_pause_seconds"] == pytest.approx(3.2)
    # The union of kept source is unchanged by a cut: only the played media shrinks.
    assert plan["body_source_duration"] == pytest.approx(19.9)
    assert plan["played_source_seconds"] == pytest.approx(19.9 - 3.2)
    assert plan["output_duration"] == pytest.approx(16.7)
    assert plan["degradations"] == []
    assert plan["compression"]["trim_count"] == 1
    assert plan["compression"]["notes"]


def test_output_duration_always_equals_untrimmed_minus_removed():
    for speed in (1.0, 1.1, 1.25):
        plan = _pause_plan(pause_options={"speed": speed})
        expected = plan["compression"]["untrimmed_output_seconds"] - plan["removed_by_pause_seconds"] / speed
        assert plan["output_duration"] == pytest.approx(expected, abs=1e-3)


def test_a_pause_containing_vad_speech_is_cut_around_it_not_discarded():
    # silencedetect calls 2.5-6.0 quiet, but VAD hears somebody talking from
    # 4.0s onwards.  An editor removes the dead air before the speech and keeps
    # the speech; discarding the whole passage would silently surrender the cut.
    plan = _pause_plan(speech=((0.0, 2.0), (4.0, 20.0)))
    assert plan["compression"]["trim_count"] == 1
    trim = plan["pause_trims"][0]
    assert (trim["source_start"], trim["source_end"]) == (2.65, 3.85)
    assert trim["removed_seconds"] == pytest.approx(1.2)
    # Never overlaps the speech, and the speech is still played in full.
    assert trim["source_end"] <= 4.0
    played = [(row["source_start"], row["source_end"]) for row in plan["occurrences"]]
    assert any(start <= 4.0 and end >= 6.0 for start, end in played)


def test_a_pause_entirely_inside_speech_is_never_cut():
    # A short break in the level meter that VAD hears as continuous speech is
    # left completely alone.
    plan = _pause_plan(speech=((0.0, 20.0),))
    assert plan["pause_trims"] == []
    assert plan["removed_by_pause_seconds"] == 0
    assert [row["occurrence_id"] for row in plan["occurrences"]] == ["O-BODY-001"]


def test_short_pauses_are_below_the_minimum_and_survive():
    tight = {"pause_target_gap_seconds": 0.1, "pause_guard_seconds": 0.05}
    kept = _pause_plan(silences=((2.5, 3.11),), speech=((0.0, 2.0), (3.5, 20.0)), pause_options=tight)
    assert kept["compression"]["trim_count"] == 1
    dropped = _pause_plan(silences=((2.5, 3.09),), speech=((0.0, 2.0), (3.5, 20.0)), pause_options=tight)
    assert dropped["compression"]["trim_count"] == 0
    assert dropped["degradations"] == []


def test_the_guard_and_the_target_both_bound_a_cut():
    # v7 geometry: keep = max(guard, gap/2) per side.  With the shipped preset
    # (guard 0.15 in TEST_PRESET, target 0.3) each side keeps 0.15, so a 0.70 s
    # silence is cut down to 0.30.
    plan = _pause_plan(silences=((2.5, 3.20),), speech=((0.0, 2.0), (3.5, 20.0)))
    assert plan["compression"]["trim_count"] == 1
    assert plan["pause_trims"][0]["removed_seconds"] == pytest.approx(0.4, abs=1e-3)
    assert plan["pause_trims"][0]["kept_pause_seconds"] == pytest.approx(0.3, abs=1e-3)
    # And the minimum-pause floor still protects short natural breaths.
    breath = _pause_plan(silences=((2.5, 2.81),), speech=((0.0, 2.0), (3.5, 20.0)))
    assert breath["compression"]["trim_count"] == 0
    # A pause only a few milliseconds longer than the target is not worth a cut.
    marginal = _pause_plan(silences=((2.5, 2.84),), speech=((0.0, 2.0), (3.5, 20.0)),
                           pause_options={"pause_min_seconds": 0.2})
    assert marginal["compression"]["trim_count"] == 0


def test_pause_scope_body_never_touches_the_hook():
    parent, utterances, story, options, evidence, speech = _pause_inputs()
    story["hook_candidates"] = [{"id": "H1", "group_ids": ["G1"], "reason": "亮点",
                                 "source_range": {"start": 0.05, "end": 19.95},
                                 "source_ranges": [{"start": 0.05, "end": 19.95}],
                                 "duration": 19.9, "score": 0.9}]
    story["recommended_hook_id"] = "H1"
    options = {**options, "hook_enabled": True, "hook_mode": "repeat", "pause_scope": "body"}
    plan = build_second_pass_plan(
        parent_plan=parent, story=story, utterances=utterances, options=options,
        story_identity={"model": "fixture"}, pause_evidence=evidence, speech_ranges=speech,
    )
    roles = {row["role"] for row in plan["pause_trims"]}
    assert roles == {"body"}
    hooks = [row for row in plan["occurrences"] if row["role"] == "hook"]
    assert len(hooks) == 1 and hooks[0]["source_start"] == 0.05 and hooks[0]["source_end"] == 19.95

    widened = build_second_pass_plan(
        parent_plan=parent, story=story, utterances=utterances,
        options={**options, "pause_scope": "all"},
        story_identity={"model": "fixture"}, pause_evidence=evidence, speech_ranges=speech,
    )
    assert {row["role"] for row in widened["pause_trims"]} == {"hook", "body"}


def test_missing_evidence_disables_compression_and_says_so():
    parent, utterances, story, options, _, speech = _pause_inputs()
    plan = build_second_pass_plan(
        parent_plan=parent, story=story, utterances=utterances, options=options,
        story_identity={"model": "fixture"}, speech_ranges=speech,
    )
    assert plan["pause_trims"] == []
    assert plan["degradations"] == ["pause_compression_disabled:停顿证据不可用（缺失）"]
    assert plan["compression"]["enabled"] is True and plan["compression"]["trim_count"] == 0
    assert plan["played_source_seconds"] == pytest.approx(19.9)


def test_turning_compression_off_is_honoured_and_recorded():
    plan = _pause_plan(pause_options={"compress_pauses": False})
    assert plan["pause_trims"] == []
    assert plan["compression"]["enabled"] is False
    assert plan["degradations"] == []


def test_compression_stops_at_the_target_minimum_duration():
    # 19.9s of content, 6.4s of removable silence, but the clip must not drop
    # below the 15s the user asked for.
    plan = _pause_plan(target_min=18, silences=((2.5, 6.0), (10.0, 15.0)))
    assert plan["removed_by_pause_seconds"] == pytest.approx(1.9, abs=1e-3)
    assert plan["output_duration"] == pytest.approx(18.0, abs=1e-3)
    assert plan["compression"]["budget_seconds"] == pytest.approx(1.9, abs=1e-3)


def test_a_short_selection_is_tightened_instead_of_left_alone():
    plan = _pause_plan(target_min=45, silences=((2.5, 6.0),))
    assert plan["compression"]["budget_seconds"] is None
    assert plan["removed_by_pause_seconds"] == pytest.approx(3.2)
    assert plan["compression"]["notes"]


def test_subtitles_follow_the_compressed_timeline():
    plan = _pause_plan()
    assert plan["subtitle_cues"]
    starts = [row["output_start"] for row in plan["subtitle_cues"]]
    assert starts == sorted(starts)
    for cue in plan["subtitle_cues"]:
        assert 0 <= cue["output_start"] < cue["output_end"] <= plan["output_duration"] + 1e-6
    # The second line starts at 6.5 s in the source; the first piece now plays
    # 0.05—2.65 (2.60 s of output) and the cut leaves 0.30 s of the pause, so the
    # line lands 0.65 s into the second occurrence.
    second = [row for row in plan["subtitle_cues"] if row["utterance_id"] == "U2"][0]
    assert second["output_start"] == pytest.approx(2.60 + (6.5 - 5.85))


def test_a_tampered_cut_list_is_rejected_because_it_cannot_be_reproduced(tmp_path):
    # Every cut must be reproducible from the frozen evidence alone.  Editing the
    # persisted plan therefore fails loudly instead of rendering something the
    # user never reviewed.
    plan = _pause_plan()
    broken = deepcopy(plan)
    broken["pause_trims"][0]["source_start"] = -1.0
    with pytest.raises(InteractionSecondPassError, match="pause_trims与语义选择不一致"):
        write_second_pass_plan(tmp_path / broken["plan_id"] / "plan.json", broken)


def test_a_tampered_speech_evidence_list_is_rejected(tmp_path):
    plan = _pause_plan()
    broken = deepcopy(plan)
    broken["speech_ranges"] = [{"start": 3.0, "end": 4.0}]
    with pytest.raises(InteractionSecondPassError, match="不一致"):
        write_second_pass_plan(tmp_path / broken["plan_id"] / "plan.json", broken)


def test_the_trim_validator_rejects_a_cut_that_touches_speech():
    from backlot.material_interaction_second_pass import _validate_pause_trims

    allowed = [{"start": 0.0, "end": 20.0}]
    cut = {"source_start": 2.8, "source_end": 5.7, "removed_seconds": 2.9, "role": "body"}
    with pytest.raises(InteractionSecondPassError, match="与语音帧相交"):
        _validate_pause_trims([cut], allowed, [{"start": 3.0, "end": 4.0}])
    # The same cut is accepted once the conflicting speech is gone.
    _validate_pause_trims([cut], allowed, [{"start": 0.0, "end": 2.0}])
    with pytest.raises(InteractionSecondPassError, match="越出"):
        _validate_pause_trims([{**cut, "source_end": 25.0, "removed_seconds": 22.2}], allowed, [])
    with pytest.raises(InteractionSecondPassError, match="删除时长过短"):
        _validate_pause_trims([{**cut, "source_end": 2.81, "removed_seconds": 0.01}], allowed, [])


def test_save_edits_reproduces_the_same_cuts_from_the_frozen_evidence():
    plan = _pause_plan()
    plan["qa"] = {"status": "passed"}
    updated = apply_second_pass_action(plan, action="save_edits", expected_revision=plan["revision"],
                                       speed=1.1)
    # The cuts are derived from the frozen evidence, not from a fresh probe: the
    # same silences produce the same cut *positions*.  The removal budget does
    # depend on the speed (a faster clip has less room before it would fall under
    # the target minimum), so a cut may legitimately be shortened — but its start
    # may not move, and the total may not exceed the budget.
    assert [row["silence_start"] for row in updated["pause_trims"]] == \
        [row["silence_start"] for row in plan["pause_trims"]]
    assert [row["source_start"] for row in updated["pause_trims"]] == \
        [row["source_start"] for row in plan["pause_trims"]]
    budget = updated["compression"]["budget_seconds"]
    assert budget is not None
    assert updated["removed_by_pause_seconds"] <= budget + 1e-6
    assert updated["output_duration"] == pytest.approx(
        updated["compression"]["untrimmed_output_seconds"]
        - updated["removed_by_pause_seconds"] / 1.1, abs=1e-3)
    # The budget exists so compression never pushes the clip under the minimum
    # the user asked for (15 s in this fixture).
    assert updated["output_duration"] >= 15.0 - 1e-3


def test_undo_restores_the_uncompressed_cut_list():
    plan = _pause_plan()
    plan["qa"] = {"status": "passed"}
    updated = apply_second_pass_action(plan, action="save_edits", expected_revision=plan["revision"],
                                       group_states={"G1": True})
    assert updated["revision"] == plan["revision"] + 1
    restored = apply_second_pass_action(updated, action="undo", expected_revision=updated["revision"])
    assert restored["pause_trims"] == plan["pause_trims"]
    assert restored["occurrences"] == plan["occurrences"]


# --- v4: audio fades, waiting-passage handling, concurrency knobs -----------

def test_v4_option_defaults_are_explicit():
    from backlot import material_interaction_second_pass as module

    normalized = module.normalize_options({})
    assert normalized["pause_handling"] == "remove"
    assert normalized["pause_speed"] == 2.0
    assert normalized["audio_fade"] is True
    assert normalized["audio_fade_ms"] == 8.0
    assert normalized["edge_fade"] is False
    assert normalized["edge_fade_ms"] == module.EDGE_FADE_MS_DEFAULT
    assert normalized["pause_margin_head_seconds"] is None
    assert normalized["pause_margin_tail_seconds"] is None
    assert normalized["window_context_policy"] == "serial_equivalent"
    assert normalized["interaction_concurrency"] == 1
    assert normalized["asr_concurrency"] == 3


def test_audio_fade_bounds_are_enforced_only_while_enabled():
    from backlot import material_interaction_second_pass as module

    with pytest.raises(InteractionSecondPassError, match="5–15 毫秒"):
        module.normalize_options({"audio_fade_ms": 40.0})
    # Off means "not enabled": an out-of-range value is ignored, not fatal.
    assert module.normalize_options({"audio_fade": False, "audio_fade_ms": 40.0})["audio_fade_ms"] == 8.0
    with pytest.raises(InteractionSecondPassError, match="150–300 毫秒"):
        module.normalize_options({"edge_fade": True, "edge_fade_ms": 10.0})


def test_pause_handling_and_compression_are_mutually_exclusive():
    from backlot import material_interaction_second_pass as module

    with pytest.raises(InteractionSecondPassError, match="互斥"):
        module.normalize_options({"pause_handling": "speed_up", "compress_pauses": True})
    # speed_up silently disables compression when it was not explicitly forced.
    normalized = module.normalize_options({"pause_handling": "speed_up"})
    assert normalized["compress_pauses"] is False
    assert normalized["pause_handling"] == "speed_up"
    with pytest.raises(InteractionSecondPassError, match="等待段处理方式无效"):
        module.normalize_options({"pause_handling": "blur"})


def test_speed_up_fast_forwards_the_passage_without_cutting_or_repeating():
    plan = _pause_plan(pause_options={"pause_handling": "speed_up", "pause_speed": 2.0})
    assert plan["pause_trims"] == []
    assert len(plan["speed_segments"]) == 1
    segment = plan["speed_segments"][0]
    assert (segment["source_start"], segment["source_end"]) == (2.65, 5.85)
    assert segment["speed"] == 2.0 and segment["unit"] == "ratio"
    # The waiting passage is kept and played fast, so the picture never jumps.
    ids = [row["occurrence_id"] for row in plan["occurrences"]]
    assert ids == ["O-BODY-001-S01", "O-BODY-001-S02", "O-BODY-001-S03"]
    sped = plan["occurrences"][1]
    assert sped["speed"] == 2.0
    assert sped["actions"] == [{"kind": "speed", "value": 2.0, "unit": "ratio",
                                "reason": "waiting_segment"}]
    assert plan["compression"]["pause_handling"] == "speed_up"
    assert plan["compression"]["speed_segment_count"] == 1
    assert plan["compression"]["enabled"] is False
    # Audio is the master clock: the output duration is exactly Σ(src)/speed.
    expected = sum((row["source_end"] - row["source_start"]) / row["speed"] for row in plan["occurrences"])
    assert plan["output_duration"] == pytest.approx(expected, abs=1e-3)
    # No source repetition and no jump cut (source ranges still tile the body).
    assert plan["repeated_source_seconds"] == 0
    starts = [row["output_start"] for row in plan["subtitle_cues"]]
    assert starts == sorted(starts)
    for cue in plan["subtitle_cues"]:
        assert 0 <= cue["output_start"] < cue["output_end"] <= plan["output_duration"] + 1e-6


def test_speed_up_is_deterministic_and_survives_save_edits():
    plan = _pause_plan(pause_options={"pause_handling": "speed_up", "pause_speed": 3.0})
    plan["qa"] = {"status": "passed"}
    updated = apply_second_pass_action(plan, action="save_edits", expected_revision=plan["revision"],
                                       speed=1.1)
    assert updated["speed_segments"] == plan["speed_segments"]
    # A segment-level speed is *absolute*: the preset ratio moves the normal
    # pieces, but the waiting piece stays at its own 3.0× (never 3.3×).
    assert [row["speed"] for row in updated["occurrences"]] == [1.1, 3.0, 1.1]
    waiting = [row for row in updated["occurrences"] if row["actions"][0]["reason"] == "waiting_segment"]
    assert len(waiting) == 1 and waiting[0]["speed"] == 3.0


def test_pause_handling_off_leaves_the_body_untouched():
    plan = _pause_plan(pause_options={"pause_handling": "off"})
    assert plan["pause_trims"] == []
    assert plan["speed_segments"] == []
    assert [row["occurrence_id"] for row in plan["occurrences"]] == ["O-BODY-001"]
    assert plan["degradations"] == []


def test_validate_speed_segments_rejects_speech_overlap_and_wrong_speed():
    from backlot.material_interaction_second_pass import _validate_speed_segments

    allowed = [{"start": 0.0, "end": 20.0}]
    options = {"pause_speed": 2.0}
    good = [{"segment_id": "SS001", "source_start": 2.65, "source_end": 5.85,
             "speed": 2.0, "unit": "ratio"}]
    _validate_speed_segments(good, allowed, [{"start": 0.0, "end": 2.0}], options)
    with pytest.raises(InteractionSecondPassError, match="与语音帧相交"):
        _validate_speed_segments(good, allowed, [{"start": 3.0, "end": 4.0}], options)
    with pytest.raises(InteractionSecondPassError, match="倍速与所选倍速不一致"):
        _validate_speed_segments([{**good[0], "speed": 3.0}], allowed, [], options)
    with pytest.raises(InteractionSecondPassError, match="越出"):
        _validate_speed_segments([{**good[0], "source_end": 25.0}], allowed, [], options)


def test_asymmetric_margins_are_accepted_and_default_to_symmetric():
    from backlot import material_interaction_second_pass as module

    symmetric = _pause_plan()
    asymmetric = _pause_plan(pause_options={"pause_margin_head_seconds": 0.3,
                                            "pause_margin_tail_seconds": 0.0})
    assert symmetric["options"]["pause_margin_head_seconds"] is None
    assert asymmetric["options"]["pause_margin_head_seconds"] == 0.3
    with pytest.raises(InteractionSecondPassError, match="0–0.5 秒"):
        module.normalize_options({"pause_margin_head_seconds": 0.9})


# --- v5: one cue per spoken phrase, not one cue per whole utterance ----------

_LONG_UTTERANCE_TEXT = (
    "嗯 然后我还知道衡阳有一个 嗯 南阳南阳南华大学 对不对呀 嗯 对 南华大学 嗯 "
    "姐姐你穿的这个白色裙子很好看 谢谢 嗯 就是天气有点热 要注意防暑 嗯 "
    "那姐姐我给你比一个爱心小心心送给你 嘻嘻嘻嘻 好可爱 旁边这位是你的女儿吗 是的 你怎么知道 嗯 "
    "她跟你长得很像呀 是吧 嗯 女儿看起来和妈妈长得很像 都很漂亮 看起来你就是这位姐姐 "
    "也是走久了 感觉很累了 你要好好休息 嗯 那我先走了 拜拜"
)


def _long_utterance_inputs():
    """The real cut-v2-accept shape: one 60 s utterance cut into four pieces.

    The utterance is what the viewer complained about -- 198 characters of text
    (165 spoken), spoken over 60 s and split by pause removal into four
    occurrences, so the old build stamped the whole block four times.
    """
    from backlot.material_interaction_second_pass import timeline_mapping

    occurrences = [
        {"occurrence_id": "O-BODY-001-P01", "role": "body", "group_ids": ["G1"],
         "source_start": 87.93, "source_end": 95.865, "speed": 1.1},
        {"occurrence_id": "O-BODY-001-P02", "role": "body", "group_ids": ["G1"],
         "source_start": 96.2, "source_end": 99.27, "speed": 1.1},
        {"occurrence_id": "O-BODY-001-P03", "role": "body", "group_ids": ["G1"],
         "source_start": 99.81, "source_end": 114.937, "speed": 1.1},
        {"occurrence_id": "O-BODY-001-P04", "role": "body", "group_ids": ["G1"],
         "source_start": 115.74, "source_end": 148.56, "speed": 1.1},
    ]
    plan = {
        "version": "interaction-second-pass-plan-v5",
        "options": {},
        "story": {"groups": [{"id": "G1", "utterance_ids": ["U00003"]}]},
        "utterances": [{"id": "U00003", "start": 88.08, "end": 148.56,
                        "text": _LONG_UTTERANCE_TEXT}],
    }
    return plan, timeline_mapping(occurrences)


def test_a_sixty_second_utterance_is_split_into_many_distinct_one_line_cues():
    from backlot.material_interaction_second_pass import subtitle_cues

    plan, mapping = _long_utterance_inputs()
    cues = subtitle_cues(plan, mapping)
    texts = [cue["text"] for cue in cues]
    # The defect: four identical 198-character blocks.
    assert len(cues) >= 12
    assert len(set(texts)) == len(texts)
    assert all(len(text) <= 16 for text in texts)
    assert not any(len(text) == 198 for text in texts)
    # Phrases run in order and the text is essentially preserved, not reinvented.
    assert "嗯 然后我还知道衡阳有一个" in texts[0]
    assert texts[-1].endswith("拜拜")
    spoken = _LONG_UTTERANCE_TEXT.replace(" ", "")
    rendered = "".join(texts).replace(" ", "")
    assert 0 <= len(spoken) - len(rendered) <= 3


def test_cue_windows_stay_monotonic_inside_the_film_and_within_the_limits():
    from backlot.material_interaction_second_pass import subtitle_cues

    plan, mapping = _long_utterance_inputs()
    cues = subtitle_cues(plan, mapping)
    output_duration = mapping[-1]["output_end"]
    starts = [cue["output_start"] for cue in cues]
    ends = [cue["output_end"] for cue in cues]
    assert starts == sorted(starts)
    assert ends == sorted(ends)
    for earlier, later in zip(cues, cues[1:]):
        assert earlier["output_end"] <= later["output_start"] + 1e-6
    for cue in cues:
        assert cue["text"].strip()
        assert len(cue["text"]) <= 16
        assert 0 <= cue["output_start"] < cue["output_end"] <= output_duration + 1e-6
        assert cue["output_end"] - cue["output_start"] >= 0.8 - 1e-6


def test_a_repeated_hook_keeps_its_caption_on_both_plays():
    index, parent, plan = _inputs()
    repeated = build_second_pass_plan(
        parent_plan=parent, story=plan["story"], utterances=index["audio"]["utterances"],
        options={**plan["options"], "hook_mode": "repeat"}, story_identity=plan["story_identity"],
    )
    # An explicit repeat really plays the line twice, so both plays keep a cue --
    # unlike a pause cut, which fragments one utterance and must not echo it.
    u4_cues = [row for row in repeated["subtitle_cues"] if row["utterance_id"] == "U4"]
    assert len(u4_cues) == 2
    assert {row["text"] for row in u4_cues} == {"三二一，茄子！"}


def test_a_short_utterance_inside_a_single_occurrence_keeps_its_one_cue():
    from backlot.material_interaction_second_pass import subtitle_cues

    plan = {
        "version": "interaction-second-pass-plan-v5",
        "options": {},
        "story": {"groups": [{"id": "G1", "utterance_ids": ["U1"]}]},
        "utterances": [{"id": "U1", "start": 0.2, "end": 2.0, "text": "你好。"}],
    }
    mapping = [{"occurrence_id": "O-BODY-001", "role": "body", "group_ids": ["G1"],
                "source_start": 0.05, "source_end": 2.8, "speed": 1.0,
                "output_start": 0.0, "output_end": 2.75}]
    cues = subtitle_cues(plan, mapping)
    # Behaviour is unchanged for a caption that already fits on one line.
    assert len(cues) == 1
    assert cues[0]["text"] == "你好。"
    assert cues[0]["output_start"] == pytest.approx(0.15)
    assert cues[0]["output_end"] == pytest.approx(1.95)


def test_the_caption_char_limit_is_worded_as_a_target_not_a_hard_ceiling():
    """看门狗：这个字数是**目标**，不是硬上限。

    极端配置下它会为「最短显示时长」让位（例如 max_chars=4 + min_s=0.8 可能拿到
    8 字），所以界面与报错都不能写成硬承诺——否则「用户设 4 却拿到 8」就是界面在
    说谎。文案一旦被改回「上限」，这条立刻变红。
    """
    from backlot import material_interaction_second_pass as module

    with pytest.raises(InteractionSecondPassError) as failure:
        module.normalize_options({"subtitle_max_chars": 2})
    message = str(failure.value)
    assert any("\u4e00" <= char <= "\u9fff" for char in message)  # 中文文案
    assert "目标" in message
    assert "上限" not in message

    script = (Path(__file__).parents[2] / "backlot" / "ui" / "workbench.js").read_text(encoding="utf-8")
    assert "字幕单条目标字数" in script
    assert "字幕单条字数上限" not in script
    assert "个别长句可能略微超出" in script


def test_the_caption_options_have_defaults_and_chinese_validation():
    from backlot import material_interaction_second_pass as module

    normalized = module.normalize_options({})
    assert normalized["subtitle_max_chars"] == module.SUBTITLE_MAX_CHARS_DEFAULT == 16
    assert normalized["subtitle_max_seconds"] == module.SUBTITLE_MAX_SECONDS_DEFAULT == 4.0
    assert normalized["subtitle_min_seconds"] == module.SUBTITLE_MIN_SECONDS_DEFAULT == 0.8
    with pytest.raises(InteractionSecondPassError, match="字幕单条目标字数"):
        module.normalize_options({"subtitle_max_chars": 2})
    with pytest.raises(InteractionSecondPassError, match="字幕单条目标字数"):
        module.normalize_options({"subtitle_max_chars": "很多"})
    with pytest.raises(InteractionSecondPassError, match="字幕单条最长时长"):
        module.normalize_options({"subtitle_max_seconds": 30})
    with pytest.raises(InteractionSecondPassError, match="字幕单条最短时长"):
        module.normalize_options({"subtitle_min_seconds": 0.0})
    with pytest.raises(InteractionSecondPassError, match="不能大于最长时长"):
        module.normalize_options({"subtitle_min_seconds": 3.0, "subtitle_max_seconds": 2.0})


def _long_caption_plan():
    """A real v5 plan whose single utterance is cut into four occurrences."""
    parent = {
        "plan_id": "IEP-caption", "version": "interaction-edit-plan-v2", "revision": 1,
        "preview": {"signature": "parent"}, "source": {"fingerprint": "source"},
        "index_signature": "index", "review_revision": 1, "event_id": "R-caption", "group_id": "people",
        "keep_ranges": [{"start": 0.0, "end": 60.0}],
    }
    utterances = [{"id": "U1", "start": 0.5, "end": 59.5, "text": _LONG_UTTERANCE_TEXT}]
    story = {
        "groups": [{"id": "G1", "sequence": 1, "type": "question_answer", "utterance_ids": ["U1"],
                    "summary": "长独白", "decision": "keep", "selected": True, "locked": False,
                    "reason": "保留完整独白", "depends_on": [], "hook_eligible": False, "hook_score": 0.0,
                    "evidence_ids": [], "source_ranges": [{"start": 0.05, "end": 59.95}],
                    "source_range": {"start": 0.05, "end": 59.95}}],
        "hook_candidates": [], "recommended_hook_id": None, "warnings": [], "summary": "独白",
    }
    options = {"speed": 1.0, "hook_enabled": False, "target_min_seconds": 15,
               "target_max_seconds": 60, **TEST_PRESET}
    evidence = {
        "version": "material-pause-evidence-v1", "status": "available",
        "identity": {"signature": "caption-identity"}, "source_fingerprint": "source",
        "silences": [{"start": 15.0, "end": 16.6}, {"start": 30.0, "end": 31.6},
                     {"start": 45.0, "end": 46.6}],
    }
    speech = [{"start": 0.0, "end": 14.5}, {"start": 17.0, "end": 29.5},
              {"start": 32.0, "end": 44.5}, {"start": 47.0, "end": 60.0}]
    return build_second_pass_plan(
        parent_plan=parent, story=story, utterances=utterances, options=options,
        story_identity={"model": "fixture"}, pause_evidence=evidence, speech_ranges=speech,
    )


@pytest.mark.parametrize("version", (
    "interaction-second-pass-plan-v1",
    "interaction-second-pass-plan-v2",
    "interaction-second-pass-plan-v3",
    "interaction-second-pass-plan-v4",
))
def test_v1_to_v4_plans_keep_their_legacy_captions_and_still_read(tmp_path, version):
    from backlot.material_interaction_second_pass import subtitle_cues

    plan = _long_caption_plan()
    assert len(plan["occurrences"]) == 4
    legacy = _downgrade_to_version(plan, version)
    legacy["subtitle_cues"] = _legacy_subtitle_cues(legacy)
    # The gate is load-bearing: the stored captions are the old whole-utterance
    # blocks, so an ungated compare would reject every historical plan (N7).
    assert legacy["subtitle_cues"] != subtitle_cues(legacy, legacy["timeline_mapping"])
    path = tmp_path / version / legacy["plan_id"] / "interaction-second-pass-plan.json"
    write_second_pass_plan(path, legacy)
    restored = read_second_pass_plan(path)
    assert restored["version"] == version
    assert restored["subtitle_cues"] == legacy["subtitle_cues"]  # read back verbatim
    assert all(len(row["text"]) == len(_LONG_UTTERANCE_TEXT) for row in restored["subtitle_cues"])


def test_a_tampered_v5_caption_list_is_rejected_because_it_cannot_be_reproduced(tmp_path):
    plan = _long_caption_plan()
    broken = deepcopy(plan)
    broken["subtitle_cues"][0]["text"] = "改过的字幕"
    with pytest.raises(InteractionSecondPassError, match="subtitle_cues与语义选择不一致"):
        write_second_pass_plan(tmp_path / broken["plan_id"] / "plan.json", broken)


_REAL_PLAN_PATHS = (
    "projects/cut-v2-accept/artifacts/media-index/S-001/interaction-second-pass/"
    "ISP-6106ac64394cce84/interaction-second-pass-plan.json",
    "projects/local-material-understanding-test/artifacts/media-index/S-001/interaction-second-pass/"
    "ISP-0326f483c31b0953/interaction-second-pass-plan.json",
)


@pytest.mark.parametrize("relative", _REAL_PLAN_PATHS)
def test_real_historical_plans_on_disk_are_still_readable(relative):
    """真实历史计划（磁盘上真实存在的 v3/v4）读取即通过，字幕原样返回。"""
    path = Path(__file__).resolve().parents[2] / relative
    if not path.is_file():
        pytest.skip(f"真实历史计划不在磁盘上：{relative}")
    restored = read_second_pass_plan(path)
    assert restored["version"] in {"interaction-second-pass-plan-v3",
                                   "interaction-second-pass-plan-v4"}
    assert restored["subtitle_cues"]
    # An older plan must come back exactly as written, not re-derived into v5.
    assert all(row["cue_id"].count(":") == 1 for row in restored["subtitle_cues"])




def test_v7_captions_are_anchored_on_the_spoken_unit_not_the_whole_asr_block():
    """Captions used to lag: the phrase was placed inside a 60-second ASR block.

    On long material an ASR "sentence" can be 60 seconds wide, so a phrase really
    spoken at 1050 s could be estimated at 1056 s — the viewer heard the line
    finish before its caption appeared.  v7 builds the caption from the VAD-aligned
    spoken unit (2—6 s), so the estimate inherits the unit's own bounds.
    """
    parent = {"plan_id": "IEP-caption", "version": "interaction-edit-plan-v2", "revision": 1,
              "preview": {"signature": "p"}, "source": {"fingerprint": "src"},
              "index_signature": "idx", "review_revision": 1, "event_id": "R1", "group_id": "G",
              "keep_ranges": [{"start": 100.0, "end": 160.0}]}
    utterances = [{"id": "U1", "start": 100.0, "end": 160.0,
                   "text": "你好 你好吗 我很好 谢谢你 再见"}]
    units = [{"id": "P0001", "start": 130.0, "end": 132.0, "text": "你好 你好吗",
              "utterance_id": "U1", "duration_seconds": 2.0},
             {"id": "P0002", "start": 132.4, "end": 134.0, "text": "我很好 谢谢你 再见",
              "utterance_id": "U1", "duration_seconds": 1.6}]
    story = {"groups": [
        {"id": "G1", "sequence": 1, "type": "other", "utterance_ids": ["P0001"], "summary": "a",
         "decision": "keep", "selected": True, "locked": False, "reason": "a", "depends_on": [],
         "hook_eligible": False, "hook_score": 0.0, "evidence_ids": [],
         "source_ranges": [{"start": 129.85, "end": 132.15}],
         "source_range": {"start": 129.85, "end": 132.15}},
        {"id": "G2", "sequence": 2, "type": "other", "utterance_ids": ["P0002"], "summary": "b",
         "decision": "keep", "selected": True, "locked": False, "reason": "b", "depends_on": [],
         "hook_eligible": False, "hook_score": 0.0, "evidence_ids": [],
         "source_ranges": [{"start": 132.25, "end": 134.15}],
         "source_range": {"start": 132.25, "end": 134.15}},
    ], "hook_candidates": [], "recommended_hook_id": None, "warnings": [], "summary": "fixture"}
    plan = build_second_pass_plan(
        parent_plan=parent, story=story, utterances=utterances,
        options={"speed": 1.0, "hook_enabled": False, "target_min_seconds": 15,
                 "target_max_seconds": 60},
        story_identity={"model": "fixture"}, units=units)
    assert plan["version"] == "interaction-second-pass-plan-v7"
    cues = plan["subtitle_cues"]
    assert [(row["output_start"], row["output_end"], row["text"]) for row in cues] == [
        (0.15, 2.15, "你好 你好吗"), (2.45, 4.05, "我很好 谢谢你 再见"),
    ]
    # The v6 derivation is still readable and still does what it always did: it
    # spreads the same phrases over the 60-second block, so the first line lands
    # 30 seconds early and is clipped out of the occurrence entirely — the caption
    # that the viewer needed simply never appears.
    legacy = deepcopy(plan)
    legacy["version"] = "interaction-second-pass-plan-v6"
    legacy_cues = subtitle_cues(legacy, legacy["timeline_mapping"])
    assert [row["text"] for row in legacy_cues] == ["我很好"]


def test_the_any_gap_policy_compresses_the_whole_gap_between_two_lines():
    """The shipped default: "上一句结束到下一句衔接 0.3 秒", ambience included.

    The three-permission policy can only tighten passages the probe called
    silence; on the acceptance material that left 10 of 65 surviving gaps 0.58—1.59 s
    long because they sit at −18…−29 dB (the machine's motor, traffic, crowd) — the
    user's "间隙压缩还是几乎看不出来".  ``any`` cuts every gap between two VAD speech
    runs, so the surviving gap is exactly the target.
    """
    from backlot import material_interaction_second_pass as module

    assert module.GAP_POLICY_DEFAULT == "any"
    assert module.normalize_options({})["gap_policy"] == "any"
    plan = _pause_plan(pause_options={"gap_policy": "any"})
    trim = plan["pause_trims"][0]
    assert (trim["source_start"], trim["source_end"]) == (2.15, 6.35)
    assert trim["removed_seconds"] == pytest.approx(4.2)
    assert trim["kept_pause_seconds"] == pytest.approx(0.30)
    assert "连环境音一起压" in trim["reason"]
    # It needs no silence evidence at all: the VAD gaps are the candidate set.
    parent, utterances, story, options, _, speech = _pause_inputs()
    without_evidence = build_second_pass_plan(
        parent_plan=parent, story=story, utterances=utterances,
        options={**options, "gap_policy": "any"}, story_identity={"model": "fixture"},
        speech_ranges=speech,
    )
    assert without_evidence["pause_trims"]
    assert without_evidence["degradations"] == []
    # The three-permission policy still exists and still needs the probe.
    quiet = build_second_pass_plan(
        parent_plan=parent, story=story, utterances=utterances,
        options={**options, "gap_policy": "quiet"}, story_identity={"model": "fixture"},
        speech_ranges=speech,
    )
    assert quiet["pause_trims"] == []
    assert quiet["degradations"] == ["pause_compression_disabled:停顿证据不可用（缺失）"]


def test_an_unknown_gap_policy_is_rejected():
    from backlot import material_interaction_second_pass as module

    with pytest.raises(InteractionSecondPassError, match="间隙压缩范围无效"):
        module.normalize_options({"gap_policy": "everything"})
