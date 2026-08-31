from __future__ import annotations

import json
from pathlib import Path

import pytest

from backlot import daily_text_resilience as resilience
from backlot.daily_script_v2 import DailyScriptV2ValidationError


def _story(event_id: str, family: str, form: str, *, heat: str = "H1") -> dict:
    return {
        "selection_id": "",
        "event_id": event_id,
        "canonical_title": f"事件{event_id}",
        "heat_level": heat,
        "external_heat_matches": [{"rank": 10}] if heat == "H3" else [],
        "topic_family": family,
        "event_form": form,
        "allocated_planning_units": 2,
        "marginal_contribution": 10,
        "replacement_priority": 1,
        "coverage_plan": [{"claim_id": f"{event_id}-event_core", "dim": "event_core", "claim": "可靠事实"}],
    }


def _selection() -> dict:
    combo_a_stories = [
        _story("ROBOT", "robotics", "visual_record", heat="H3"),
        _story("GAME-A", "gaming", "trailer_announcement"),
        _story("GAME-B", "gaming", "trailer_announcement"),
    ]
    combo_b_stories = [
        _story("ROBOT", "robotics", "visual_record", heat="H3"),
        _story("GAME-A", "gaming", "trailer_announcement"),
    ]
    for stories in (combo_a_stories, combo_b_stories):
        for index, story in enumerate(stories, 1):
            story["selection_id"] = f"S{index:02d}"
    return {
        "version": "2.1",
        "prompt_version": "test",
        "target_date": "2026-08-26",
        "selected_stories": combo_a_stories,
        "selection_summary": {"episode_score": 70, "duration_profile": "full_episode"},
        "episode_combinations": [
            {
                "combination_id": "EC-A",
                "rank": 1,
                "episode_score": 70,
                "story_count": 3,
                "duration_profile": "full_episode",
                "event_ids": ["ROBOT", "GAME-A", "GAME-B"],
                "selected_stories": combo_a_stories,
            },
            {
                "combination_id": "EC-B",
                "rank": 2,
                "episode_score": 68,
                "story_count": 2,
                "duration_profile": "compact_high_value",
                "event_ids": ["ROBOT", "GAME-A"],
                "selected_stories": combo_b_stories,
            },
        ],
    }


def _script(score: int) -> dict:
    return {
        "episode_title": "每日科技快讯",
        "stories": [{"story_id": "S01", "headline": "机器人百米比赛"}],
        "lines": [{"turn_id": "T001", "kind": "story", "text": "每日科技快讯来了，机器人跑出新成绩。"}],
        "validation": {"line_count": 1, "estimated_duration_seconds": 70},
        "editorial_review": {"total": score, "quality_band": "premium", "passed": score >= 85},
        "generation_audit": {"writer_model": "writer", "reviewer_model": "reviewer"},
    }


def _write_editorial_checkpoint(run_dir, selection, score, issue):
    checkpoint = {
        "status": "editorial_rejected",
        "script": _script(score),
        "editorial_review": {
            "total": score,
            "issues": [issue["message"]],
            "structured_issues": [issue],
        },
        "story_order": [story["selection_id"] for story in selection["selected_stories"]],
    }
    (run_dir / "daily_script_v2_last_rejected.json").write_text(
        json.dumps(checkpoint, ensure_ascii=False), encoding="utf-8"
    )


def test_systemic_failure_switches_combination_and_second_review_can_pass(tmp_path):
    selection = _selection()
    calls = []

    def generator(current, _research, **_kwargs):
        calls.append([story["event_id"] for story in current["selected_stories"]])
        if len(calls) == 1:
            _write_editorial_checkpoint(tmp_path, current, 83, {
                "code": "episode_topic_homogeneity", "scope": "episode", "message": "后半段两条游戏预告题材重复。",
                "suggested_action": "reselect", "hard_fact_boundary": False,
            })
            raise DailyScriptV2ValidationError(["后半段两条游戏预告题材重复"])
        return _script(87)

    result = resilience.generate_resilient_script_v2(
        selection, {"target_date": "2026-08-26", "candidates": []}, run_dir=tmp_path, generator=generator
    )

    assert result["status"] == "passed"
    assert calls == [["ROBOT", "GAME-A", "GAME-B"], ["ROBOT", "GAME-A"]]
    assert result["ledger"]["editorial_reviews_used"] == 2
    assert result["ledger"]["attempts"][0]["recovery"]["action"] == "reselect"
    assert all(attempt["media_cost_cny"] == 0 for attempt in result["ledger"]["attempts"])


def test_second_rejection_preserves_best_valid_draft_for_human(tmp_path):
    selection = _selection()
    scores = iter((83, 84))

    def generator(current, _research, **_kwargs):
        score = next(scores)
        _write_editorial_checkpoint(tmp_path, current, score, {
            "code": "dialogue_flow", "scope": "turn", "turn_ids": ["T003"],
            "message": "T003承接偏书面。", "suggested_action": "repair_lines", "hard_fact_boundary": False,
        })
        raise DailyScriptV2ValidationError(["T003承接偏书面"])

    result = resilience.generate_resilient_script_v2(
        selection, {"target_date": "2026-08-26", "candidates": []}, run_dir=tmp_path, generator=generator
    )

    assert result["status"] == "awaiting_human"
    assert result["ledger"]["best_candidate"]["editorial_score"] == 84
    assert result["script"]["editorial_review"]["total"] == 84
    assert result["ledger"]["safe_resume_point"] == "human_review_best_candidate"
    assert result["ledger"]["next_action"] == "human_review"


def test_passed_ledger_is_idempotently_reused_without_model_call(tmp_path):
    selection = _selection()
    calls = 0

    def generator(_selection, _research, **_kwargs):
        nonlocal calls
        calls += 1
        return _script(88)

    first = resilience.generate_resilient_script_v2(
        selection, {"target_date": "2026-08-26", "candidates": []}, run_dir=tmp_path, generator=generator
    )
    second = resilience.generate_resilient_script_v2(
        selection, {"target_date": "2026-08-26", "candidates": []}, run_dir=tmp_path, generator=generator
    )

    assert first["status"] == second["status"] == "passed"
    assert second["reused"] is True
    assert calls == 1


def test_unrecoverable_failure_without_valid_script_still_fails(tmp_path):
    def generator(_selection, _research, **_kwargs):
        raise DailyScriptV2ValidationError(["结构无法恢复"])

    with pytest.raises(DailyScriptV2ValidationError, match="未得到结构有效脚本"):
        resilience.generate_resilient_script_v2(
            _selection(), {"target_date": "2026-08-26", "candidates": []}, run_dir=tmp_path,
            policy={"max_text_attempts": 1}, generator=generator,
        )


def test_frozen_august_26_fixture_records_the_original_same_form_failure():
    fixture = json.loads(
        (Path(__file__).parents[1] / "fixtures" / "daily_text_resilience_2026-08-26.json").read_text(encoding="utf-8")
    )
    games = [
        event for event in fixture["events"]
        if event["topic_family"] == "gaming" and event["event_form"] == "trailer_announcement"
    ]

    assert fixture["baseline"]["editorial_score"] == 83
    assert fixture["baseline"]["media_cost_cny"] == 0
    assert len(games) == 2
    assert all(event["heat_level"] == "H1" and event["platform_hot"] is False for event in games)


def test_identical_structural_checkpoint_is_not_counted_three_times(tmp_path):
    selection = _selection()
    selection["episode_combinations"] = [selection["episode_combinations"][0]]
    calls = []

    def generator(current, _research, **kwargs):
        calls.append(kwargs.get("reuse_structural_checkpoint"))
        checkpoint = {
            "status": "structural_rejected",
            "story_order": [story["selection_id"] for story in current["selected_stories"]],
            "issues": ["S02 event_identity必须逐字来自冻结标题或claim"],
            "issue_codes": ["event_identity_not_grounded"],
            "repair_status": "no_progress",
            "repair_rule_version": "event-identity-v2",
            "raw_script": {
                "story_identities": [{"story_id": "S02", "event_identity": "8月25日深夜"}],
                "lines": [{"turn_id": "T001", "text": "每日科技快讯来了，同一份拒稿。"}],
            },
        }
        (tmp_path / "daily_script_v2_last_rejected.json").write_text(
            json.dumps(checkpoint, ensure_ascii=False), encoding="utf-8"
        )
        raise DailyScriptV2ValidationError(checkpoint["issues"])

    for _ in range(2):
        with pytest.raises(DailyScriptV2ValidationError):
            resilience.generate_resilient_script_v2(
                selection,
                {"target_date": "2026-08-26", "candidates": []},
                run_dir=tmp_path,
                policy={"max_text_attempts": 3},
                generator=generator,
            )

    ledger = json.loads((tmp_path / "daily_text_attempts.json").read_text(encoding="utf-8"))
    assert calls == [True, False, False]
    assert ledger["unique_attempt_count"] == 2
    assert len(ledger["attempts"]) == 2
    assert ledger["duplicate_attempts_suppressed"] == 1
    assert any(event["type"] == "no_progress_detected" for event in ledger["events"])
