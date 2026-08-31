from __future__ import annotations

import json
from pathlib import Path

import pytest

from backlot import daily_script_v2


def _selection():
    stories = []
    for story_index in range(1, 4):
        story_id = f"S{story_index:02d}"
        event_id = f"E-{story_index}"
        plan = [
            {"claim_id": f"{event_id}-c{claim_index}", "dim": "event_core", "claim": "冻结事实", "source_candidate_ids": [f"N-{story_index}"]}
            for claim_index in range(1, 4)
        ]
        stories.append(
            {
                "selection_id": story_id,
                "event_id": event_id,
                "allocated_planning_units": 3,
                "coverage_plan": plan,
                "canonical_title": f"测试公司发布第{story_index}项重要科技产品更新",
                "story_type": "product",
                "heat_level": "H2",
                "evidence_candidate_ids": [f"N-{story_index}"],
            }
        )
    return {"version": "2.0", "target_date": "2026-08-23", "selected_stories": stories}


def _raw_script(selection):
    rows = []
    slots = daily_script_v2._script_slots(selection)
    for slot in slots:
        is_first = slot["turn_id"] == "T001"
        is_outro = slot["kind"] == "outro"
        story = next((item for item in selection["selected_stories"] if item["selection_id"] == slot["story_id"]), None)
        is_story_opening = bool(story) and not any(row["story_id"] == slot["story_id"] for row in rows)
        text = "这三条变化里，你最希望哪一项更快走进普通人的生活？" if is_outro else "这条信息来自冻结正文，保留具体事实和必要边界，也不增加任何额外推断。"
        if is_story_opening:
            text = f"{story['canonical_title']}，这次变化已经完成正文核验。"
        if is_first:
            text = f"每日科技快讯来了，{story['canonical_title']}。"
        rows.append(
            {
                "turn_id": slot["turn_id"],
                "speaker_id": slot["speaker_id"],
                "kind": slot["kind"],
                "story_id": slot["story_id"],
                "function": "closing" if is_outro else ("hook" if is_first else "fact"),
                "information_dimension": slot["required_information_dimension"],
                "information_key": "" if is_outro else f"{slot['story_id']}-{slot['turn_id']}",
                "claim_ids": slot["required_claim_ids"],
                "text": text,
            }
        )
    while sum(len(row["text"]) for row in rows) < 365:
        changed = False
        for row in rows[:-1]:
            if len(row["text"]) + 2 <= 42:
                row["text"] += "明确"
                changed = True
            if sum(len(item["text"]) for item in rows) >= 365:
                break
        if not changed:
            break
    return {
        "episode_title": "测试快报",
        "episode_summary": "结构测试。",
        "story_identities": [
            {"story_id": story["selection_id"], "event_identity": story["canonical_title"]}
            for story in selection["selected_stories"]
        ],
        "story_headlines": [
            {"story_id": story["selection_id"], "headline": "测试公司科技产品完成重要更新"}
            for story in selection["selected_stories"]
        ],
        "lines": rows,
    }


def test_script_v2_validates_fixed_slots_and_builds_pure_scripts():
    selection = _selection()
    result = daily_script_v2.validate_script_v2(_raw_script(selection), selection)

    assert result["validation"]["valid"] is True
    assert result["validation"]["line_count"] == 10
    assert 365 <= result["validation"]["spoken_character_count"] <= 420
    assert "每日科技快讯来了" in result["pure_scripts"]["yaya"]
    assert "你最希望" in result["pure_scripts"]["mengmeng"]
    assert result["stories"][0]["headline_overlay"]["style_id"] == "daily_news_headline_v1"
    assert result["stories"][0]["event_identity"] == selection["selected_stories"][0]["canonical_title"]
    overlay = result["stories"][0]["headline_overlay"]
    assert overlay["mode"] == "two_line"
    assert len(overlay["line_2"]) > len(overlay["line_1"])


def test_script_v2_rejects_claim_reuse_or_wrong_slot_binding():
    selection = _selection()
    raw = _raw_script(selection)
    raw["lines"][1]["claim_ids"] = raw["lines"][0]["claim_ids"]

    with pytest.raises(daily_script_v2.DailyScriptV2ValidationError, match="固定claim"):
        daily_script_v2.validate_script_v2(raw, selection)


def test_script_v2_rejects_overlong_episode():
    selection = _selection()
    raw = _raw_script(selection)
    for row in raw["lines"][1:-1]:
        row["text"] = "这条信息来自冻结正文并保留全部必要边界。" * 6

    with pytest.raises(daily_script_v2.DailyScriptV2ValidationError, match="字数|总口播"):
        daily_script_v2.validate_script_v2(raw, selection)


def test_script_v2_accepts_platform_hot_expressive_wording_from_canonical_title():
    selection = _selection()
    selection["selected_stories"][0]["wording_policy"] = "hot_topic_expressive"
    selection["selected_stories"][0]["canonical_title"] = "测试机器人百米快过人类世界纪录"
    raw = _raw_script(selection)

    result = daily_script_v2.validate_script_v2(raw, selection)

    assert result["validation"]["valid"] is True


def test_script_v2_rejects_comparison_missing_from_assigned_claim():
    selection = _selection()
    raw = _raw_script(selection)
    second_story_opening = next(
        line for line in raw["lines"] if line["story_id"] == "S02"
    )
    second_story_opening["text"] += "，门槛比之前高"

    with pytest.raises(daily_script_v2.DailyScriptV2ValidationError, match="冻结claim之外的比较结论"):
        daily_script_v2.validate_script_v2(raw, selection)


def test_script_v2_rejects_unfrozen_remote_and_release_status_claims():
    selection = _selection()
    raw = _raw_script(selection)
    raw["lines"][1]["text"] += "，全程没有人工遥控"
    raw["lines"][-1]["text"] = "面对已经开售的这款产品，你会优先看价格还是性能？"

    with pytest.raises(daily_script_v2.DailyScriptV2ValidationError) as captured:
        daily_script_v2.validate_script_v2(raw, selection)

    assert "无遥控结论" in str(captured.value)
    assert "开售状态" in str(captured.value)


def test_script_v2_rejects_unfrozen_episode_title_or_summary_claims():
    selection = _selection()
    raw = _raw_script(selection)
    raw["episode_title"] = "测试产品涨价2500元"
    raw["episode_summary"] = "这是该系列第一次面向AI升级。"

    with pytest.raises(daily_script_v2.DailyScriptV2ValidationError) as captured:
        daily_script_v2.validate_script_v2(raw, selection)

    assert "标题或摘要包含冻结claim之外的数字" in str(captured.value)
    assert "标题或摘要包含冻结claim之外的强结论" in str(captured.value)


def test_script_v2_rejects_unfrozen_no_remote_wording_without_ren_gong_prefix():
    selection = _selection()
    raw = _raw_script(selection)
    raw["lines"][1]["text"] += "，不靠遥控"

    with pytest.raises(daily_script_v2.DailyScriptV2ValidationError, match="无遥控结论"):
        daily_script_v2.validate_script_v2(raw, selection)


def test_script_v2_rejects_unfrozen_number_in_broadcast_headline():
    selection = _selection()
    raw = _raw_script(selection)
    raw["story_headlines"][0]["headline"] = "测试产品价格上涨2500元"

    with pytest.raises(daily_script_v2.DailyScriptV2ValidationError, match="小标题包含冻结claim之外的数字"):
        daily_script_v2.validate_script_v2(raw, selection)


def test_script_v2_accepts_hot_topic_record_wording_in_broadcast_headline():
    selection = _selection()
    selection["selected_stories"][0]["wording_policy"] = "hot_topic_expressive"
    selection["selected_stories"][0]["canonical_title"] = "测试机器人百米再破人类纪录"
    raw = _raw_script(selection)
    raw["story_headlines"][0]["headline"] = "测试机器人百米再破人类纪录"

    result = daily_script_v2.validate_script_v2(raw, selection)

    assert result["stories"][0]["headline"] == "测试机器人百米再破人类纪录"


def test_script_v2_tolerates_slight_line_overrun_but_keeps_hard_cap():
    selection = _selection()
    raw = _raw_script(selection)
    row = raw["lines"][1]
    row["text"] += "确" * (44 - len(row["text"]))

    result = daily_script_v2.validate_script_v2(raw, selection)
    assert len(result["lines"][1]["text"]) == 44

    raw["lines"][1]["text"] += "已超过"
    raw["lines"][1]["text"] += "继续超出" * 7
    with pytest.raises(daily_script_v2.DailyScriptV2ValidationError, match="70字异常上限"):
        daily_script_v2.validate_script_v2(raw, selection)


def test_script_v2_tolerates_small_total_overrun_but_keeps_hard_cap():
    selection = _selection()
    raw = _raw_script(selection)
    total = sum(len(row["text"]) for row in raw["lines"])
    for row in raw["lines"][:-1]:
        room = 50 - len(row["text"])
        add = min(room, max(0, 465 - total))
        row["text"] += "确" * add
        total += add
    assert total == 465
    assert daily_script_v2.validate_script_v2(raw, selection)["validation"]["spoken_character_count"] == 465

    for row in raw["lines"]:
        room = 65 - len(row["text"])
        add = min(room, max(0, 601 - total))
        row["text"] += "明" * add
        total += add
    assert total == 601
    with pytest.raises(daily_script_v2.DailyScriptV2ValidationError, match="280—600"):
        daily_script_v2.validate_script_v2(raw, selection)


def test_small_total_overrun_removes_only_zero_information_padding_without_model_call(monkeypatch):
    raw = {
        "lines": [
            {"turn_id": "T001", "text": "每日科技快讯来了，" + "甲" * 45},
            {"turn_id": "T002", "text": "对应机型成绩亮眼，产品跑出8.64秒，关键数字保持不变。" + "乙" * 495},
            {"turn_id": "T003", "text": "这项技术门槛可不低：其余冻结事实保持完整。" + "丙" * 10},
        ]
    }
    total = sum(len(item["text"]) for item in raw["lines"])
    assert 600 < total <= 640
    monkeypatch.setattr(
        daily_script_v2,
        "_chat_json_with_transient_retry",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("不应调用模型压缩")),
    )

    repaired = daily_script_v2._repair_total_length(raw, [f"总口播{total}字，不在280—600字保护区间"])

    assert sum(len(item["text"]) for item in repaired["lines"]) <= 600
    assert "8.64秒" in repaired["lines"][1]["text"]
    assert "对应机型成绩亮眼" not in repaired["lines"][1]["text"]


def test_script_v2_rejects_identity_not_grounded_or_not_spoken_in_opening():
    selection = _selection()
    raw = _raw_script(selection)
    raw["story_identities"][1]["event_identity"] = "并不存在的超级模型"

    with pytest.raises(daily_script_v2.DailyScriptV2ValidationError, match="event_identity"):
        daily_script_v2.validate_script_v2(raw, selection)


def test_event_identity_may_omit_a_source_date_without_changing_the_fact():
    selection = _selection()
    selection["selected_stories"][0]["canonical_title"] = "测试公司于8月24日发布第1项重要科技产品更新"
    raw = _raw_script(selection)
    raw["story_identities"][0]["event_identity"] = "测试公司发布第1项重要科技产品更新"
    raw["lines"][0]["text"] = "每日科技快讯来了，测试公司发布第1项重要科技产品更新。"
    raw["lines"][1]["text"] += "边界明确"
    raw["lines"][2]["text"] += "信息清楚可核验"

    result = daily_script_v2.validate_script_v2(raw, selection)

    assert result["stories"][0]["event_identity"] == "测试公司发布第1项重要科技产品更新"


def test_story_opening_may_omit_identity_category_suffix():
    selection = _selection()
    raw = _raw_script(selection)
    raw["story_identities"][1]["event_identity"] += "手机"
    selection["selected_stories"][1]["canonical_title"] += "手机"

    result = daily_script_v2.validate_script_v2(raw, selection)

    assert result["stories"][1]["event_identity"].endswith("手机")


def test_story_opening_may_omit_brand_when_unique_model_is_spoken():
    selection = _selection()
    raw = _raw_script(selection)
    selection["selected_stories"][1]["canonical_title"] = "苹果 AirPods 4 耳机发布"
    raw["story_identities"][1]["event_identity"] = "苹果 AirPods 4"
    raw["lines"][3]["text"] = "AirPods 4耳机已经正式发布，具体变化完成核验。"
    raw["lines"][4]["text"] += "事实边界清楚可核验"

    result = daily_script_v2.validate_script_v2(raw, selection)

    assert result["stories"][1]["event_identity"] == "苹果 AirPods 4"


def test_story_opening_may_omit_nonessential_day_modifier():
    selection = _selection()
    raw = _raw_script(selection)
    raw["story_identities"][0]["event_identity"] = "测试公司当日发布第1项重要科技产品更新"
    selection["selected_stories"][0]["canonical_title"] = "测试公司当日发布第1项重要科技产品更新"

    result = daily_script_v2.validate_script_v2(raw, selection)

    assert result["stories"][0]["event_identity"].startswith("测试公司当日")


def test_story_opening_may_split_subject_and_event_keyword_naturally():
    selection = _selection()
    selection["selected_stories"][0]["canonical_title"] = "荣耀公布人形机器人运动会百米项目成绩"
    raw = _raw_script(selection)
    raw["story_identities"][0]["event_identity"] = "荣耀公布人形机器人运动会百米项目成绩"
    raw["lines"][0]["text"] = "每日科技快讯来了，人形机器人跑百米进入九秒区间，荣耀这次把具体成绩摆出来了。"

    result = daily_script_v2.validate_script_v2(raw, selection)

    assert result["stories"][0]["event_identity"] == "荣耀公布人形机器人运动会百米项目成绩"


def test_identity_grounding_may_skip_a_source_action_word_and_fact_kind_is_normalized():
    selection = _selection()
    raw = _raw_script(selection)
    selection["selected_stories"][0]["canonical_title"] = "小米发布玄戒O3芯片"
    raw["story_identities"][0]["event_identity"] = "小米玄戒O3"
    raw["lines"][0]["text"] = "每日科技快讯来了，小米玄戒O3芯片已经正式亮相。"
    raw["lines"][1]["kind"] = "fact"

    result = daily_script_v2.validate_script_v2(raw, selection)

    assert result["stories"][0]["event_identity"] == "小米玄戒O3"
    assert result["lines"][1]["kind"] == "story"


def test_event_identity_comes_from_frozen_event_core_not_model_metadata():
    story = _selection()["selected_stories"][0]
    story["coverage_plan"][0]["claim"] = "荣耀公布人形机器人运动会百米项目成绩：元气仔跑出12.02秒。"
    story["canonical_title"] = "荣耀机器人百米赛成绩公布"

    identity = daily_script_v2._grounded_event_identity(
        story,
        "每日科技快讯来了，荣耀公布了人形机器人百米项目成绩。",
        "模型随意写的错误身份",
    )

    assert identity == "荣耀公布人形机器人运动会百米项目成绩"


def test_event_identity_keeps_a_grounded_spoken_proposal_before_database_prefix():
    story = _selection()["selected_stories"][0]
    story["coverage_plan"][0]["claim"] = "荣耀公布人形机器人运动会百米项目成绩：元气仔跑出12.02秒。"
    story["canonical_title"] = "荣耀机器人百米赛成绩公布"

    identity = daily_script_v2._grounded_event_identity(
        story,
        "每日科技快讯来了，荣耀的人形机器人运动会百米项目成绩出炉。",
        "人形机器人运动会百米项目",
    )

    assert identity == "人形机器人运动会百米项目"


def test_event_identity_uses_spoken_frozen_chinese_project_title_over_unfrozen_publisher():
    story = _selection()["selected_stories"][0]
    story["canonical_title"] = "《昭和米国物语》游戏新实机预告片公开"
    story["coverage_plan"][0]["claim"] = "2026科隆游戏展公开《昭和米国物语》全新实机预告。"

    identity = daily_script_v2._grounded_event_identity(
        story,
        "最后看游戏，铃空游戏公开《昭和米国物语》全新实机预告。",
        "铃空游戏《昭和米国物语》新实机预告",
    )

    assert identity == "《昭和米国物语》"


def test_august_27_metadata_repair_preserves_dialogue_and_repairs_bad_identities():
    fixture = json.loads(
        (Path(__file__).parents[1] / "fixtures" / "daily_structural_resilience_2026-08-27.json").read_text(
            encoding="utf-8"
        )
    )
    selection = {"selected_stories": fixture["stories"]}
    repaired, audit = daily_script_v2.repair_structural_metadata(fixture["raw"], selection)
    identities = {item["story_id"]: item["event_identity"] for item in repaired["story_identities"]}

    assert identities == fixture["expected_identities"]
    assert audit["dialogue_preserved"] is True
    assert audit["dialogue_sha256_before"] == audit["dialogue_sha256_after"]
    assert [item["text"] for item in repaired["lines"]] == [item["text"] for item in fixture["raw"]["lines"]]


def test_event_identity_rejects_time_only_and_generic_trend_labels():
    fixture = json.loads(
        (Path(__file__).parents[1] / "fixtures" / "daily_structural_resilience_2026-08-27.json").read_text(
            encoding="utf-8"
        )
    )
    s02 = fixture["stories"][1]
    s04 = fixture["stories"][3]

    time_check = daily_script_v2.validate_event_identity_candidate(
        "8月25日深夜", story=s02, opening_text=fixture["raw"]["lines"][2]["text"]
    )
    trend_check = daily_script_v2.validate_event_identity_candidate(
        "2026年8月国内人形机器人行业集中升温",
        story=s04,
        opening_text=fixture["raw"]["lines"][6]["text"],
    )

    assert time_check["reason_code"] == "time_only"
    assert trend_check["reason_code"] in {"length", "generic", "not_spoken"}


def test_technical_details_with_a_clear_user_payoff_are_not_rejected_as_broadcast_copy():
    script = {
        "lines": [
            {
                "turn_id": "T002",
                "story_id": "S01",
                "required_dialogue_move": "decision_boundary",
                "text": "这次是天美原班人马做的，iOS、安卓、鸿蒙和桌面模拟器都能玩，而且多端数据互通，换设备不用重新肝号。",
            },
            {
                "turn_id": "T004",
                "story_id": "S02",
                "required_dialogue_move": "decision_boundary",
                "text": "他原话挺实在，说不焦虑不可能，但熬得久比起得早更重要，还说AI马拉松才跑第一公里。",
            },
        ]
    }

    assert daily_script_v2._dialogue_move_issues(script) == []


def test_entertainment_game_launch_is_a_valid_public_hook_without_forced_wallet_language():
    script = {
        "lines": [
            {
                "turn_id": "T001",
                "story_id": "S01",
                "required_dialogue_move": "story_open_with_consequence",
                "text": "每日科技快讯来了，《王者万象棋》正式定档9月10日全平台上线，摸鱼党又要集体开黑了。",
            },
            {
                "turn_id": "T002",
                "story_id": "S01",
                "required_dialogue_move": "decision_boundary",
                "text": "iOS、安卓、鸿蒙和桌面模拟器都能玩，多端数据互通，换设备不用重新肝号。",
            },
        ]
    }

    assert daily_script_v2._dialogue_move_issues(script) == []


def test_editorial_review_does_not_treat_compliance_as_propagation_quality():
    review = daily_script_v2.validate_editorial_review(
        {
            "scores": {"hook": 15, "dialogue": 15, "information_density": 19, "public_value": 18, "interaction": 14},
            "total": 99,
            "issues": ["T001只有栏目身份，没有具体后果"],
            "verdict": "pass",
        }
    )

    assert review["total"] == 81
    assert review["verdict"] == "revise"
    assert review["passed"] is False


def test_editorial_review_cannot_pass_with_a_reported_factual_boundary_issue():
    review = daily_script_v2.validate_editorial_review(
        {
            "scores": {"hook": 18, "dialogue": 18, "information_density": 22, "public_value": 18, "interaction": 13},
            "issues": ["T008的核心价格数字未在coverage_plan中明确支撑，需删除。"],
            "verdict": "pass",
        }
    )

    assert review["total"] == 89
    assert review["passed"] is False
    assert review["verdict"] == "revise"


def test_editorial_review_keeps_non_blocking_style_notes_when_scores_pass():
    review = daily_script_v2.validate_editorial_review(
        {
            "scores": {"hook": 18, "dialogue": 17, "information_density": 22, "public_value": 17, "interaction": 12},
            "issues": ["T005转场略硬，可以继续润色。"],
            "verdict": "revise",
        }
    )

    assert review["total"] == 86
    assert review["passed"] is True
    assert review["verdict"] == "pass"
    assert review["issues"] == ["T005转场略硬，可以继续润色。"]


def test_editorial_review_normalizes_global_duration_issue_for_recovery():
    review = daily_script_v2.validate_editorial_review(
        {
            "scores": {"hook": 17, "dialogue": 17, "information_density": 21, "public_value": 17, "interaction": 11},
            "issues": ["全稿预计119.5秒，超过110秒，需要压缩。"],
            "verdict": "revise",
        }
    )

    assert review["structured_issues"][0]["code"] == "episode_duration_over_target"
    assert review["structured_issues"][0]["scope"] == "episode"
    assert review["structured_issues"][0]["suggested_action"] == "compress"


@pytest.mark.parametrize("breakout_level", ["H3", "H4"])
def test_editorial_policy_keeps_premium_gate_for_high_heat_and_uses_reliable_floor_without_it(breakout_level):
    selection = _selection()
    selection["selected_stories"][0]["heat_level"] = breakout_level
    assert daily_script_v2._editorial_policy(selection)["required_total"] == 85

    for story in selection["selected_stories"]:
        story["heat_level"] = "H2"
    policy = daily_script_v2._editorial_policy(selection)
    review = daily_script_v2.validate_editorial_review(
        {
            "scores": {"hook": 14, "dialogue": 16, "information_density": 21, "public_value": 16, "interaction": 11},
            "issues": [],
            "verdict": "pass",
        },
        policy,
    )

    assert policy["required_total"] == 78
    assert review["passed"] is True
    assert review["quality_band"] == "fallback_publishable"


def test_script_lead_prioritizes_direct_public_value_without_changing_story_ids():
    selection = _selection()
    selection["selected_stories"][0].update(
        {"audience_fit_score": 72, "editorial_potential_score": 78, "observed_heat_score": 49}
    )
    selection["selected_stories"][1].update(
        {
            "canonical_title": "耳机参与国补",
            "audience_fit_score": 78,
            "editorial_potential_score": 66,
            "observed_heat_score": 41,
        }
    )
    selection["selected_stories"][1]["coverage_plan"][0]["claim"] = "部分地区收货地址可享15%国补"

    prioritized = daily_script_v2._prioritize_script_lead(selection)

    assert prioritized["selected_stories"][0]["selection_id"] == "S02"
    assert selection["selected_stories"][0]["selection_id"] == "S01"


def test_script_lead_prefers_top_douyin_story_when_base_scores_are_close():
    selection = _selection()
    first, second = selection["selected_stories"][:2]
    first.update({
        "audience_fit_score": 92,
        "editorial_potential_score": 88,
        "observed_heat_score": 96,
        "external_heat_matches": [{
            "source_id": "copy_skill-douyin-hotspot-v2",
            "source_name": "抖音科技热点",
            "rank": 1,
        }],
    })
    second.update({
        "audience_fit_score": 94,
        "editorial_potential_score": 90,
        "observed_heat_score": 90,
        "external_heat_matches": [{"source_id": "baidu-realtime", "rank": 7}],
    })

    prioritized = daily_script_v2._prioritize_script_lead(selection)

    assert prioritized["selected_stories"][0]["selection_id"] == first["selection_id"]


def test_event_identity_recovers_changxin_storage_from_spoken_opening():
    story = {
        "canonical_title": "长鑫存储开通微博首个关注小米手机",
        "coverage_plan": [{"claim": "长鑫存储开通官方微博，其首个关注账号为小米手机。"}],
    }

    identity = daily_script_v2._grounded_event_identity(
        story,
        "长鑫存储开通官微，首个关注给了小米。",
        "小米手机",
    )

    assert identity == "长鑫存储"


def test_metadata_repair_shortens_overlong_headline_at_complete_clause():
    selection = _selection()
    raw = _raw_script(selection)
    raw["story_headlines"][0]["headline"] = "腾讯开源混元Hy4 preview，上下文突破100万Token"

    repaired, audit = daily_script_v2.repair_structural_metadata(raw, selection)

    assert repaired["story_headlines"][0]["headline"] == "腾讯开源混元Hy4 preview"
    assert audit["dialogue_preserved"] is True


def test_dialogue_move_check_requires_hook_and_transition_without_forcing_connectors():
    selection = _selection()
    result = daily_script_v2.validate_script_v2(_raw_script(selection), selection)
    issues = daily_script_v2._dialogue_move_issues(result)

    assert any("T001" in issue for issue in issues)
    assert any("切换新闻" in issue for issue in issues)
    assert not any("对话动作" in issue for issue in issues)


def test_dialogue_move_accepts_a_concrete_visual_spectacle_as_public_hook():
    selection = _selection()
    selection["selected_stories"][0]["canonical_title"] = "荣耀公布人形机器人运动会百米项目成绩"
    raw = _raw_script(selection)
    raw["story_identities"][0]["event_identity"] = "荣耀公布人形机器人运动会百米项目成绩"
    raw["lines"][0]["text"] = "每日科技快讯来了，人形机器人百米跑进九秒区间，赛场冲刺画面很直观。"
    raw["lines"][1]["text"] += "荣耀公布了成绩"
    result = daily_script_v2.validate_script_v2(raw, selection)

    issues = daily_script_v2._dialogue_move_issues(result)

    assert not any("T001-T002" in issue for issue in issues)


def test_dialogue_connector_normalizer_only_adds_story_transitions_without_changing_slots():
    selection = _selection()
    raw = _raw_script(selection)
    original_metadata = [
        {key: value for key, value in row.items() if key != "text"}
        for row in raw["lines"]
    ]

    repaired = daily_script_v2._normalize_dialogue_connectors(raw, selection)

    assert [
        {key: value for key, value in row.items() if key != "text"}
        for row in repaired["lines"]
    ] == original_metadata
    assert repaired["lines"][1]["text"] == raw["lines"][1]["text"]
    second_story_start = next(index for index, row in enumerate(repaired["lines"]) if row["story_id"] == "S02")
    assert repaired["lines"][second_story_start]["text"].startswith("接着，")


def test_dialogue_move_validator_rejects_repeated_story_transition_phrases():
    script = {
        "lines": [
            {"turn_id": "T001", "story_id": "S01", "text": "每日科技快讯来了，机器人百米冲线。", "required_dialogue_move": "story_open_with_consequence"},
            {"turn_id": "T002", "story_id": "S01", "text": "它这次跑出了具体成绩。", "required_dialogue_move": "plain_language_payoff"},
            {"turn_id": "T003", "story_id": "S02", "text": "再看，手机智能体公布评测结果。", "required_dialogue_move": "story_open_with_consequence"},
            {"turn_id": "T004", "story_id": "S03", "text": "再看，智慧屏发布显示技术。", "required_dialogue_move": "story_open_with_consequence"},
        ]
    }

    issues = daily_script_v2._dialogue_move_issues(script)

    assert any("主持短语“再看”重复" in issue for issue in issues)


def test_editorial_review_treats_non_hard_commentary_boundary_as_non_blocking():
    review = daily_script_v2.validate_editorial_review({
        "scores": {"hook": 18, "dialogue": 18, "information_density": 22, "public_value": 18, "interaction": 13},
        "issues": ["T004的‘终于不是走秀’属于coverage_plan之外的主持评论。"],
        "verdict": "pass",
    })

    assert review["passed"] is True


def test_dialogue_move_check_rejects_repeated_template_leads():
    selection = _selection()
    raw = _raw_script(selection)
    raw["lines"][1]["text"] = "具体看，" + raw["lines"][1]["text"]
    raw["lines"][2]["text"] = "具体看，" + raw["lines"][2]["text"]
    result = daily_script_v2.validate_script_v2(raw, selection)

    assert any("主持短语“具体看”重复" in issue for issue in daily_script_v2._dialogue_move_issues(result))


def test_dialogue_move_check_rejects_broadcast_style_technical_list():
    script = {
        "lines": [{
            "turn_id": "T003",
            "story_id": "S01",
            "text": "它升级了关节电机、构型、散热、轻量化，配百米加速算法与控制系统。",
            "required_dialogue_move": "followup_translation",
        }]
    }

    issues = daily_script_v2._dialogue_move_issues(script)

    assert any("连续罗列技术名词" in issue for issue in issues)


def test_slot_metadata_normalizer_restores_model_dropped_structure():
    selection = _selection()
    raw = _raw_script(selection)
    raw["lines"][3].update({"kind": "constraint", "story_id": "", "function": ""})

    repaired = daily_script_v2._normalize_slot_metadata(raw, selection)
    result = daily_script_v2.validate_script_v2(repaired, selection)

    assert result["lines"][3]["kind"] == "story"
    assert result["lines"][3]["story_id"] == "S02"
    assert result["lines"][3]["function"] == "fact"
    assert result["lines"][3]["claim_ids"] == result["lines"][3]["required_claim_ids"]
    assert result["lines"][3]["information_key"]


def test_slot_metadata_normalizer_repairs_event_identity_without_model_revision():
    selection = _selection()
    selection["selected_stories"][0]["canonical_title"] = "OpenAI与博通共同开发Jalapeño系统"
    selection["selected_stories"][0]["coverage_plan"][0]["claim"] = "OpenAI与博通共同开发了Jalapeño系统。"
    raw = _raw_script(selection)
    raw["story_identities"][0]["event_identity"] = "OpenAI与博通共研Jalapeño芯片"
    raw["lines"][0]["text"] = "每日科技快讯来了，OpenAI与博通共同开发的Jalapeño系统瞄准AI推理。"

    repaired = daily_script_v2._normalize_slot_metadata(raw, selection)

    assert repaired["story_identities"][0] == {
        "story_id": "S01",
        "event_identity": "Jalapeño系统",
    }


def test_event_identity_prefers_spoken_chinese_brand_product_over_award_prefix():
    story = {
        "canonical_title": "中国电信AITMark奖出炉：华为小艺拿下AI手机智能体综合评分第一",
        "coverage_plan": [{"claim": "2026中国电信研究院AITMark奖项公布，华为小艺获综合评分第一。"}],
    }

    identity = daily_script_v2._grounded_event_identity(
        story,
        "接着，手机AI也在卷，华为小艺获综合评分第一。",
        "2026中国电信研究院AITMark奖项公布",
    )

    assert identity == "华为小艺"


@pytest.mark.parametrize(
    ("title", "expected_top", "expected_bottom"),
    [
        ("GPT-5.6 Sol限时降价超过20%", "GPT-5.6 Sol", "限时降价超过20%"),
        ("阿里配售新股募资800亿港元投入AI", "阿里配售新股", "募资800亿港元投入AI"),
        ("中国牵头的首项固态电池国际标准立项", "中国牵头的首项", "固态电池国际标准立项"),
        ("启元两款消费级机器人开放预订", "启元两款", "消费级机器人开放预订"),
    ],
)
def test_headline_overlay_preserves_product_names_and_semantic_phrases(title, expected_top, expected_bottom):
    overlay = daily_script_v2._headline_overlay(title)

    assert overlay["mode"] == "two_line"
    assert overlay["line_1"] == expected_top
    assert overlay["line_2"] == expected_bottom


def test_headline_overlay_never_splits_a_number_from_its_unit():
    overlay = daily_script_v2._headline_overlay("新款Mac mini 6999元起售 M6版AI性能升级")

    assert not (overlay["line_1"].endswith("6999") and overlay["line_2"].startswith("元"))
    assert "6999元" in overlay["line_1"] or "6999元" in overlay["line_2"]


def test_script_text_call_retries_truncated_stream_once(monkeypatch):
    attempts = 0

    def flaky_call(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts < 2:
            raise daily_script_v2.TextAIError("AI 流式连接中断，响应内容不完整，可安全重试")
        return {"ok": True}, "text-model"

    monkeypatch.setattr(daily_script_v2, "_chat_json", flaky_call)

    result, model = daily_script_v2._chat_json_with_transient_retry("prompt", {}, temperature=0.0)

    assert result == {"ok": True}
    assert model == "text-model"
    assert attempts == 2


def test_script_text_call_retries_truncated_structured_response(monkeypatch):
    attempts = 0

    def truncated_then_valid(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise daily_script_v2.TextAIError("AI 返回内容不是有效的结构化文案")
        return {"ok": True}, "text-model"

    monkeypatch.setattr(daily_script_v2, "_chat_json", truncated_then_valid)

    result, model = daily_script_v2._chat_json_with_transient_retry("prompt", {}, temperature=0.0)

    assert result == {"ok": True}
    assert model == "text-model"
    assert attempts == 2


def test_editorial_issue_expands_turn_ranges_for_local_repair():
    valid = {f"T{index:03d}" for index in range(1, 11)}

    assert daily_script_v2._issue_turn_ids("T005-T007需要压缩，T010修正结尾", valid) == [
        "T005", "T006", "T007", "T010"
    ]


def test_reviewer_contract_excludes_raw_headlines_and_unused_claims():
    selection = _selection()
    selection["selected_stories"][0]["canonical_title"] = "媒体原题不得进入复验"
    selection["selected_stories"][0]["available_coverage_plan"] = [{"claim": "未分配日期"}]

    contract = daily_script_v2._review_selection_contract(selection)
    serialized = json.dumps(contract, ensure_ascii=False)

    assert "媒体原题不得进入复验" not in serialized
    assert "未分配日期" not in serialized
    assert "coverage_plan" in serialized


def test_writer_payload_uses_frozen_claims_not_full_article_excerpts():
    selection = _selection()
    research = {
        "candidates": [
            {
                "candidate_id": "N-1",
                "evidence_excerpt": "不应进入写稿请求" * 2000,
            }
        ]
    }

    payload = daily_script_v2._payload(selection, research)

    assert "coverage_plan" in payload["stories"][0]
    assert "evidence" not in payload["stories"][0]
    assert "不应进入写稿请求" not in json.dumps(payload, ensure_ascii=False)


def test_overlong_line_repair_only_patches_requested_turns(monkeypatch):
    selection = _selection()
    raw = _raw_script(selection)
    original_second = raw["lines"][1]["text"]
    raw["lines"][0]["text"] = "每日科技快讯来了，测试公司发布第1项重要科技产品更新，具体变化已经完成正文核验并保留全部事实边界。"

    monkeypatch.setattr(daily_script_v2, "_chat_json_with_transient_retry", lambda *args, **kwargs: ({
        "replacements": [{"turn_id": "T001", "text": "每日科技快讯来了，测试公司发布第1项重要科技产品更新。"}],
    }, "text-model"))

    repaired = daily_script_v2._repair_overlong_lines(
        raw, ["T001 字数超过70字异常上限"], selection,
    )

    assert repaired["lines"][0]["text"] == "每日科技快讯来了，测试公司发布第1项重要科技产品更新。"
    assert repaired["lines"][1]["text"] == original_second
    assert raw["lines"][1]["text"] == original_second


def test_overlong_line_repair_accepts_result_inside_70_character_hard_guard(monkeypatch):
    selection = _selection()
    raw = _raw_script(selection)
    raw["lines"][0]["text"] = "每日科技快讯来了，" + "超长事实" * 20
    replacement = "每日科技快讯来了，" + "保留冻结事实和必要边界" * 5
    assert 60 < len(replacement) <= daily_script_v2.HARD_LINE_MAX
    monkeypatch.setattr(
        daily_script_v2,
        "_chat_json_with_transient_retry",
        lambda *_args, **_kwargs: ({
            "replacements": [{"turn_id": "T001", "text": replacement}],
        }, "text-model"),
    )

    repaired = daily_script_v2._repair_overlong_lines(
        raw,
        ["T001 字数超过70字异常上限"],
        selection,
    )

    assert repaired["lines"][0]["text"] == replacement


def test_overlong_line_repair_trims_complete_suffix_and_may_drop_secondary_numbers(monkeypatch):
    selection = _selection()
    raw = _raw_script(selection)
    raw["lines"][0]["text"] = (
        "每日科技快讯来了，机器人跑出8.64秒核心成绩，"
        "随后在2026年专项测试继续验证稳定性并补充大量背景说明。"
    )
    replacement = (
        "每日科技快讯来了，机器人跑出8.64秒，核心成绩已经完成核验。"
        "至于2026年的次要对照数据和更多背景说明可以留到评论区继续展开，"
        "这里不需要一次性把所有参数全部念完。"
    )
    assert len(replacement) > daily_script_v2.HARD_LINE_MAX
    monkeypatch.setattr(
        daily_script_v2,
        "_chat_json_with_transient_retry",
        lambda *_args, **_kwargs: ({
            "replacements": [{"turn_id": "T001", "text": replacement}],
        }, "text-model"),
    )

    repaired = daily_script_v2._repair_overlong_lines(
        raw,
        ["T001 字数超过70字异常上限"],
        selection,
    )

    text = repaired["lines"][0]["text"]
    assert len(text) <= daily_script_v2.HARD_LINE_MAX
    assert "8.64" in text
    assert "2026" not in text


def test_overlong_line_repair_accepts_equivalent_decimal_spelling(monkeypatch):
    selection = _selection()
    raw = _raw_script(selection)
    raw["lines"][0]["text"] = (
        "每日科技快讯来了，模型盲测得分4.0分，"
        "这项核心结果已经核验并补充了很多需要被精简的背景说明。"
    )
    monkeypatch.setattr(
        daily_script_v2,
        "_chat_json_with_transient_retry",
        lambda *_args, **_kwargs: ({
            "replacements": [{
                "turn_id": "T001",
                "text": "每日科技快讯来了，模型盲测得分4分，核心结果已经完成核验。",
            }],
        }, "text-model"),
    )

    repaired = daily_script_v2._repair_overlong_lines(
        raw,
        ["T001 字数超过70字异常上限"],
        selection,
    )

    assert "4分" in repaired["lines"][0]["text"]
    assert daily_script_v2._normalized_number_tokens("4.0分") == {"4"}
    assert daily_script_v2._normalized_number_tokens("4分") == {"4"}


def test_overlong_line_repair_falls_back_to_punctuation_trim_when_model_changes_numbers(monkeypatch):
    selection = _selection()
    raw = _raw_script(selection)
    raw["lines"][0]["text"] = (
        "每日科技快讯来了，模型完成163名专家和203项工程任务盲测，"
        "最终拿到2.99分，后面还有很长的对照说明需要被安全删除。" + "补充背景" * 12
    )
    monkeypatch.setattr(
        daily_script_v2,
        "_chat_json_with_transient_retry",
        lambda *_args, **_kwargs: ({
            "replacements": [{
                "turn_id": "T001",
                "text": "每日科技快讯来了，模型完成163名专家盲测，最终拿到3.01分。",
            }],
        }, "text-model"),
    )

    repaired = daily_script_v2._repair_overlong_lines(
        raw,
        ["T001 字数超过70字异常上限"],
        selection,
    )

    text = repaired["lines"][0]["text"]
    assert len(text) <= daily_script_v2.HARD_LINE_MAX
    assert "2.99" in text
    assert "3.01" not in text


def test_editorial_line_repair_receives_user_approved_style_examples(monkeypatch):
    selection = _selection()
    script = daily_script_v2.validate_script_v2(_raw_script(selection), selection)
    captured = {}
    monkeypatch.setattr(
        daily_script_v2,
        "_load_golden_examples",
        lambda limit=2: [{
            "id": "approved-style",
            "lines": [{"speaker_name": "雅雅", "text": "[对象]这也太提气了！"}],
        }],
    )

    def fake_chat(system, payload, **_kwargs):
        captured["system"] = system
        captured["payload"] = payload
        return ({
            "replacements": [{
                "turn_id": "T001",
                "text": "每日科技快讯来了，第一条核心结果直接前置，确实很有看点。",
            }],
        }, "text-model")

    monkeypatch.setattr(daily_script_v2, "_chat_json_with_transient_retry", fake_chat)
    daily_script_v2._repair_editorial_lines(
        script,
        ["T001 钩子缺少具体结果"],
        selection,
    )

    assert captured["payload"]["approved_style_examples"][0]["id"] == "approved-style"
    assert "先抛本期最强结果" in captured["system"]


def test_total_length_repair_only_replaces_text(monkeypatch):
    selection = _selection()
    raw = _raw_script(selection)
    for row in raw["lines"][:-1]:
        row["text"] += "明确边界"
    original_slots = [
        {key: value for key, value in row.items() if key != "text"}
        for row in raw["lines"]
    ]
    replacements = []
    for row in raw["lines"]:
        text = row["text"]
        replacements.append({"turn_id": row["turn_id"], "text": text[:42]})
    monkeypatch.setattr(
        daily_script_v2,
        "_chat_json_with_transient_retry",
        lambda *_args, **_kwargs: ({"replacements": replacements}, "text-model"),
    )

    repaired = daily_script_v2._repair_total_length(raw, ["总口播605字，不在280—600字保护区间"])

    assert [
        {key: value for key, value in row.items() if key != "text"}
        for row in repaired["lines"]
    ] == original_slots
    assert 280 <= sum(len(row["text"]) for row in repaired["lines"]) <= 600


def test_total_length_repair_may_drop_secondary_numbers_but_cannot_invent_one(monkeypatch):
    lines = [
        {"turn_id": "T001", "kind": "story", "text": "每日科技快讯来了，产品从9.39秒提升到8.64秒。" + "甲" * 35}
    ]
    lines.extend(
        {"turn_id": f"T{index:03d}", "kind": "story", "text": "另一个核心事实保持不变。" + "乙" * 50}
        for index in range(2, 11)
    )
    lines.append({"turn_id": "T011", "kind": "outro", "text": "这两项变化，你更关心哪一个？"})
    raw = {"lines": lines}
    replacements = [
        {"turn_id": "T001", "text": "每日科技快讯来了，产品跑出8.64秒，核心成绩保留。"},
        *[
            {"turn_id": f"T{index:03d}", "text": "另一个核心事实保持不变，具体边界也交代清楚，相关信息还能继续核对。"}
            for index in range(2, 11)
        ],
        {"turn_id": "T011", "text": "这两项变化，你更关心哪一个？"},
    ]
    monkeypatch.setattr(
        daily_script_v2,
        "_chat_json_with_transient_retry",
        lambda *_args, **_kwargs: ({"replacements": replacements}, "text-model"),
    )
    repaired = daily_script_v2._repair_total_length(raw, ["总口播超过600字"])
    assert "9.39" not in repaired["lines"][0]["text"]
    assert "8.64" in repaired["lines"][0]["text"]

    replacements[0]["text"] = "每日科技快讯来了，产品跑出8.65秒，核心成绩保留。"
    with pytest.raises(daily_script_v2.DailyScriptV2ValidationError, match="新增或改写了数字"):
        daily_script_v2._repair_total_length(raw, ["总口播超过600字"])


def test_total_length_repair_recovers_small_provider_overrun_by_removing_only_padding(monkeypatch):
    lines = [
        {"turn_id": "T001", "kind": "story", "text": "每日科技快讯来了，这次机器人跑出8.64秒的成绩。"},
        {"turn_id": "T002", "kind": "story", "text": "本次升级保留关节电机和控制算法两个事实。"},
    ]
    lines.extend(
        {"turn_id": f"T{index:03d}", "kind": "story", "text": "这句保留冻结事实并补足正常口播长度用于测试。"}
        for index in range(3, 13)
    )
    lines.append({"turn_id": "T013", "kind": "outro", "text": "你最想先看到机器人在哪个真实场景里工作？"})
    raw = {"lines": lines}

    def fake_chat(*_args, **_kwargs):
        return ({
            "replacements": [
                {"turn_id": row["turn_id"], "text": row["text"]}
                for row in raw["lines"]
            ]
        }, "test-model")

    monkeypatch.setattr(daily_script_v2, "_chat_json_with_transient_retry", fake_chat)
    target = sum(len(row["text"]) for row in raw["lines"]) - 4

    repaired = daily_script_v2._repair_total_length(raw, ["episode_duration_over_target"], target_max=target)

    assert sum(len(row["text"]) for row in repaired["lines"]) <= target
    assert "8.64" in repaired["lines"][0]["text"]


def test_only_user_approved_golden_examples_load_and_facts_are_anonymized(tmp_path, monkeypatch):
    approved = {
        "version": "1.0", "id": "approved-one", "approved": True, "approved_by": "user",
        "approved_at": "2026-08-25T10:00:00+08:00", "source_kind": "user_approved",
        "redact_terms": ["测试公司"], "title": "测试公司 GPT-9 降价20%",
        "lines": [
            {"speaker_name": "雅雅", "text": "每日科技快讯来了，测试公司 GPT-9 降价20%。"},
            {"speaker_name": "檬檬", "text": "这次调整会在9月落地。"},
            {"speaker_name": "雅雅", "text": "具体范围还要看官方名单。"},
            {"speaker_name": "檬檬", "text": "你会现在用，还是再等等？"},
        ],
    }
    unapproved = {**approved, "id": "seed", "approved": False, "approved_by": "seed_example"}
    (tmp_path / "approved.json").write_text(json.dumps(approved, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "seed.json").write_text(json.dumps(unapproved, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(daily_script_v2, "GOLDEN_SCRIPTS_DIR", tmp_path)

    examples = daily_script_v2._load_golden_examples()
    status = daily_script_v2.golden_script_status()

    assert [item["id"] for item in examples] == ["approved-one"]
    injected = json.dumps(examples, ensure_ascii=False)
    assert "测试公司" not in injected
    assert "GPT-9" not in injected
    assert "20%" not in injected
    assert "[对象]" in injected and "[型号]" in injected and "[数字]" in injected
    assert status["loaded_count"] == 1
    assert status["ignored"][0]["status"] == "not_user_approved"


def test_generated_script_records_writer_reviewer_and_golden_audit(tmp_path, monkeypatch):
    selection = _selection()
    raw = _raw_script(selection)
    golden_dir = tmp_path / "golden"
    golden_dir.mkdir()
    (golden_dir / "approved.json").write_text(json.dumps({
        "version": "1.0", "id": "approved-audit", "approved": True, "approved_by": "user",
        "approved_at": "2026-08-25T10:00:00+08:00", "source_kind": "user_approved",
        "redact_terms": [], "title": "批准样稿",
        "lines": [
            {"speaker_name": "雅雅", "text": "每日科技快讯来了，这次变化先说结果。"},
            {"speaker_name": "檬檬", "text": "关键在于具体影响已经说清楚。"},
            {"speaker_name": "雅雅", "text": "普通人真正关心的是使用变化。"},
            {"speaker_name": "檬檬", "text": "不过适用边界还要继续核对。"},
        ],
    }, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(daily_script_v2, "GOLDEN_SCRIPTS_DIR", golden_dir)
    monkeypatch.setattr(daily_script_v2, "RUNS_ROOT", tmp_path / "runs")
    monkeypatch.setattr(daily_script_v2, "_writer_provider", lambda: "doubao")
    monkeypatch.setattr(daily_script_v2, "_reviewer_provider", lambda: "doubao")
    monkeypatch.setattr(daily_script_v2, "_dialogue_move_issues", lambda _script: [])

    def fake_chat(system, _payload, **kwargs):
        if kwargs.get("temperature") == 0.0 and kwargs.get("provider") == "doubao":
            return ({
                "scores": {"hook": 16, "dialogue": 16, "information_density": 20, "public_value": 15, "interaction": 11},
                "issues": [], "verdict": "pass",
            }, "review-model")
        assert kwargs.get("provider") == "doubao"
        return raw, "doubao-writer-model"

    monkeypatch.setattr(daily_script_v2, "_chat_json_with_transient_retry", fake_chat)

    result = daily_script_v2.generate_script_v2(selection, {"candidates": []}, max_revision_rounds=0)

    audit = result["generation_audit"]
    assert audit["writer_provider"] == "doubao"
    assert audit["writer_model"] == "doubao-writer-model"
    assert audit["reviewer_provider"] == "doubao"
    assert audit["preferred_reviewer_provider"] == "doubao"
    assert audit["review_mode"] == "independent_cold_review"
    assert audit["reviewer_model"] == "review-model"
    assert audit["golden_examples"][0]["id"] == "approved-audit"
    assert len(audit["golden_examples"][0]["sha256"]) == 64


def test_cold_review_uses_luna_only_as_provider_failure_fallback(monkeypatch):
    calls = []

    def fake_chat(_system, _payload, **kwargs):
        provider = kwargs["provider"]
        calls.append(provider)
        if provider == "doubao":
            raise daily_script_v2.TextAIError("HTTP 503")
        return {"verdict": "pass"}, "luna-review"

    monkeypatch.setattr(daily_script_v2, "_chat_json_with_transient_retry", fake_chat)

    raw, model, provider, fallback = daily_script_v2._cold_review_with_fallback(
        "review", {"review_mode": "independent_cold_review"}, preferred_provider="doubao"
    )

    assert raw == {"verdict": "pass"}
    assert model == "luna-review"
    assert provider == "default"
    assert fallback["from"] == "doubao"
    assert calls == ["doubao", "default"]


def test_valid_doubao_rejection_is_not_sent_to_luna_for_override(monkeypatch):
    calls = []

    def fake_chat(_system, _payload, **kwargs):
        calls.append(kwargs["provider"])
        return {"verdict": "revise", "issues": ["T001钩子不具体"]}, "doubao-cold-review"

    monkeypatch.setattr(daily_script_v2, "_chat_json_with_transient_retry", fake_chat)

    raw, model, provider, fallback = daily_script_v2._cold_review_with_fallback(
        "review", {"review_mode": "independent_cold_review"}, preferred_provider="doubao"
    )

    assert raw["verdict"] == "revise"
    assert model == "doubao-cold-review"
    assert provider == "doubao"
    assert fallback is None
    assert calls == ["doubao"]


def test_script_estimated_over_120_seconds_is_blocked_even_when_reviewer_passes(tmp_path, monkeypatch):
    selection = _selection()
    raw = _raw_script(selection)
    # Stay inside both the 70-character line guard and the 600-character
    # whole-script guard while exceeding the former hidden 500 target.
    suffix = "这条补充仍然来自冻结事实并保留自然口播节奏"
    while sum(len(row["text"]) for row in raw["lines"]) <= 535:
        for row in raw["lines"][:-1]:
            if len(row["text"]) + len(suffix) <= 68:
                row["text"] += suffix
            if sum(len(item["text"]) for item in raw["lines"]) > 535:
                break
    monkeypatch.setattr(daily_script_v2, "RUNS_ROOT", tmp_path / "runs")
    monkeypatch.setattr(daily_script_v2, "_writer_provider", lambda: "doubao")
    monkeypatch.setattr(daily_script_v2, "_reviewer_provider", lambda: "doubao")
    monkeypatch.setattr(daily_script_v2, "_dialogue_move_issues", lambda _script: [])
    calls = []

    def fake_chat(system, _payload, **kwargs):
        calls.append((system, kwargs))
        if kwargs.get("temperature") == 0.0 and kwargs.get("provider") == "doubao":
            return ({
                "scores": {"hook": 16, "dialogue": 16, "information_density": 20, "public_value": 18, "interaction": 15},
                "issues": [], "verdict": "pass",
            }, "review-model")
        return raw, "doubao-writer-model"

    monkeypatch.setattr(daily_script_v2, "_chat_json_with_transient_retry", fake_chat)

    with pytest.raises(daily_script_v2.DailyScriptV2ValidationError) as exc_info:
        daily_script_v2.generate_script_v2(selection, {"candidates": []}, max_revision_rounds=0)

    assert "120" in str(exc_info.value)
    assert len(calls) == 2


def test_rejected_checkpoint_is_not_reused_just_because_scores_touch_threshold():
    policy = {"required_total": 85, "hook_min": 16, "dialogue_min": 16, "information_density_min": 20}
    review = {
        "scores": {"hook": 18, "dialogue": 16, "information_density": 21, "public_value": 18, "interaction": 12},
        "total": 85,
        "issues": ["预计时长超过110秒，需要压缩"],
        "verdict": "revise",
        "passed": False,
    }

    assert daily_script_v2._review_scores_meet_policy(review, policy) is False

    review.update({"issues": [], "verdict": "pass", "passed": True})
    assert daily_script_v2._review_scores_meet_policy(review, policy) is True


def test_rejected_checkpoint_is_bound_to_event_ids_not_reused_slot_ids(tmp_path, monkeypatch):
    selection = _selection()
    run_dir = tmp_path / "runs" / selection["target_date"]
    run_dir.mkdir(parents=True)
    stale_raw = _raw_script(selection)
    stale_raw["episode_title"] = "旧组合检查点"
    (run_dir / "daily_script_v2_last_rejected.json").write_text(
        json.dumps(
            {
                "status": "structural_rejected",
                "prompt_version": daily_script_v2.PROMPT_VERSION,
                "story_order": ["S01", "S02", "S03"],
                "story_event_order": ["E-OLD-1", "E-OLD-2", "E-OLD-3"],
                "repair_status": "untried",
                "raw_script": stale_raw,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    fresh_raw = _raw_script(selection)
    calls = []

    def fake_chat(_system, _payload, **kwargs):
        calls.append(kwargs.get("temperature"))
        if kwargs.get("temperature") == 0.0:
            return ({
                "scores": {
                    "hook": 16,
                    "dialogue": 16,
                    "information_density": 20,
                    "public_value": 17,
                    "interaction": 12,
                },
                "issues": [],
                "verdict": "pass",
            }, "review-model")
        return fresh_raw, "writer-model"

    monkeypatch.setattr(daily_script_v2, "RUNS_ROOT", tmp_path / "runs")
    monkeypatch.setattr(daily_script_v2, "_load_golden_examples", lambda: [])
    monkeypatch.setattr(daily_script_v2, "_writer_provider", lambda: "doubao")
    monkeypatch.setattr(daily_script_v2, "_reviewer_provider", lambda: "doubao")
    monkeypatch.setattr(daily_script_v2, "_dialogue_move_issues", lambda _script: [])
    monkeypatch.setattr(daily_script_v2, "_chat_json_with_transient_retry", fake_chat)

    result = daily_script_v2.generate_script_v2(
        selection,
        {"candidates": []},
        max_revision_rounds=0,
    )

    assert calls[0] == 0.8
    assert result["generation_audit"]["writer_model"] == "writer-model"
    assert result["episode_title"] != "旧组合检查点"


def test_editorial_review_cannot_pass_with_duration_over_target_even_at_high_score():
    review = daily_script_v2.validate_editorial_review({
        "scores": {"hook": 18, "dialogue": 17, "information_density": 22, "public_value": 18, "interaction": 13},
        "issues": ["validation显示预计时长136秒，超过110秒，需要压缩。"],
        "verdict": "pass",
    })

    assert review["total"] == 88
    assert review["passed"] is False
    assert review["verdict"] == "revise"
    assert review["structured_issues"][0]["code"] == "episode_duration_over_target"


def test_review_level_duration_failure_triggers_whole_script_compression(tmp_path, monkeypatch):
    selection = _selection()
    selection["selected_stories"][0]["heat_level"] = "H3"
    raw = _raw_script(selection)
    monkeypatch.setattr(daily_script_v2, "RUNS_ROOT", tmp_path / "runs")
    monkeypatch.setattr(daily_script_v2, "_writer_provider", lambda: "doubao")
    monkeypatch.setattr(daily_script_v2, "_reviewer_provider", lambda: "doubao")
    monkeypatch.setattr(daily_script_v2, "_dialogue_move_issues", lambda _script: [])
    reviews = iter((
        {
            "scores": {"hook": 17, "dialogue": 17, "information_density": 21, "public_value": 17, "interaction": 11},
            "issues": ["全稿预计119.5秒，超过110秒，需要压缩。"], "verdict": "revise",
        },
        {
            "scores": {"hook": 17, "dialogue": 17, "information_density": 21, "public_value": 18, "interaction": 12},
            "issues": [], "verdict": "pass",
        },
    ))
    compressed = []

    def fake_chat(_system, _payload, **kwargs):
        if kwargs.get("temperature") == 0.0 and kwargs.get("provider") == "doubao":
            return next(reviews), "review-model"
        return raw, "writer-model"

    monkeypatch.setattr(daily_script_v2, "_chat_json_with_transient_retry", fake_chat)
    monkeypatch.setattr(
        daily_script_v2,
        "_repair_editorial_lines",
        lambda _script, _issues, _selection, **_kwargs: (raw, "repair-model"),
    )

    def fake_compress(value, issues, **kwargs):
        compressed.append((issues, kwargs.get("target_max")))
        return value

    monkeypatch.setattr(daily_script_v2, "_repair_total_length", fake_compress)

    result = daily_script_v2.generate_script_v2(selection, {"candidates": []}, max_revision_rounds=1)

    assert result["editorial_review"]["passed"] is True
    assert compressed == [(["episode_duration_over_target"], daily_script_v2.EDITORIAL_DURATION_MAX_CHARS)]


def test_headline_overlay_does_not_split_chinese_response_word():
    overlay = daily_script_v2._headline_overlay("汤道生长文回应腾讯做AI慢了")

    assert overlay["mode"] == "two_line"
    assert overlay["line_1"] == "汤道生长文"
    assert overlay["line_2"] == "回应腾讯做AI慢了"
