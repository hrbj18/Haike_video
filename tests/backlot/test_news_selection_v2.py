from __future__ import annotations

import json

import pytest

from backlot import daily_cli
from backlot import news_selection_v2 as selection_v2


def test_heat_match_rejects_generic_model_overlap_between_unrelated_events():
    event = {
        "canonical_title": "美团CEO王兴称AI模型用于主业，不做Token工厂",
    }
    signals = [{
        "source_id": "copy_skill-douyin-hotspot-v2",
        "title": "本地部署大模型被颠覆，8G显存跑35B大模型",
        "rank": 10,
    }]
    assert selection_v2._external_heat_matches(event, signals) == []


def test_heat_match_rejects_same_company_but_different_event():
    event = {"canonical_title": "Anthropic赢下五角大楼供应链风险标签诉讼"}
    signals = [{
        "source_id": "copy_skill-douyin-hotspot-v2",
        "title": "Anthropic上市在即，估值引发讨论",
        "rank": 11,
    }]
    assert selection_v2._external_heat_matches(event, signals) == []


def test_heat_match_rejects_generic_robot_fragments_between_different_products():
    event = {"canonical_title": "一段视频让机器人学会开门并穿越"}
    signals = [{
        "source_id": "copy_skill-douyin-hotspot-v2",
        "title": "云鲸JXUltra洗地机器人发布",
        "rank": 4,
    }]
    assert selection_v2._external_heat_matches(event, signals) == []


@pytest.mark.parametrize(
    ("event_title", "signal_title"),
    [
        (
            "聚焦人工智能和民生服务，这家企业用AI重塑房产交易体验",
            "腾讯混元HY4 preview正式开源 #人工智能 #科技 #大模型",
        ),
        (
            "AI游戏风口爆发，创作者一年赚走百亿",
            "腾讯Hy4突然开源 #AI #大模型 #抖音创作者激励计划",
        ),
        (
            "英伟达CEO黄仁勋呼吁AI行业争取公众支持",
            "GLM5.3发布，又一脱离英伟达体系的大模型 #AI #科技",
        ),
        (
            "吉利AI新能源越野技术首发",
            "427万辆新能源车陷逃生困境",
        ),
    ],
)
def test_heat_match_rejects_category_and_hashtag_overlap(event_title, signal_title):
    event = {"canonical_title": event_title}
    signals = [{
        "source_id": "copy_skill-douyin-hotspot-v2",
        "title": signal_title,
        "rank": 1,
    }]
    assert selection_v2._external_heat_matches(event, signals) == []


def test_heat_match_rejects_same_acronym_for_different_vla_projects():
    event = {"canonical_title": "大晓联合香港大学发布StreamPI，让VLA理解时间"}
    signals = [{
        "source_id": "copy_skill-douyin-hotspot-v2",
        "title": "小鹏Master Agent上车，VLA与VLM驾舱融合",
        "rank": 15,
    }]
    assert selection_v2._external_heat_matches(event, signals) == []


@pytest.mark.parametrize(
    ("event_title", "signal_title"),
    [
        ("腾讯混元Hy4 preview开源上线", "腾讯Hy4突然开源，770B总参数"),
        ("Gemini Omni开放预览", "谷歌Gemini Omni正式上线"),
        ("小鹏第二代VLA升级上车", "小鹏VLA新版本九月推送"),
    ],
)
def test_heat_match_keeps_specific_product_identity(event_title, signal_title):
    event = {"canonical_title": event_title}
    signals = [{
        "source_id": "copy_skill-douyin-hotspot-v2",
        "title": signal_title,
        "rank": 3,
    }]
    assert selection_v2._external_heat_matches(event, signals)


def test_heat_match_keeps_multiple_specific_event_anchors():
    event = {"canonical_title": "长鑫存储成为玄戒O3的LPDDR6内存合作伙伴"}
    signals = [{
        "source_id": "baidu-realtime",
        "title": "长鑫LPDDR6内存正式量产",
        "rank": 7,
    }]
    matches = selection_v2._external_heat_matches(event, signals)
    assert matches and matches[0]["rank"] == 7


def test_lead_rank_prefers_direct_public_savings_over_abstract_industry_value():
    chip_event = {"canonical_title": "企业发布三款自研芯片", "observed_heat_score": 49}
    chip_assessment = {
        "audience_fit_score": 72,
        "editorial_potential_score": 78,
        "coverage_plan": [{"claim": "自研芯片有助于完善全场景算力布局"}],
    }
    subsidy_event = {"canonical_title": "耳机参与国补", "observed_heat_score": 41}
    subsidy_assessment = {
        "audience_fit_score": 78,
        "editorial_potential_score": 66,
        "coverage_plan": [{"claim": "部分地区收货地址可享15%国补"}],
    }

    assert selection_v2._lead_rank(subsidy_event, subsidy_assessment) > selection_v2._lead_rank(chip_event, chip_assessment)


def test_editorial_comparison_or_faq_is_not_treated_as_news_event():
    assert selection_v2._editorial_non_event({
        "canonical_title": "华为和小米怎么选？2026年旗舰对比：AI与信号成关键分水岭+FAQ"
    }) is True
    assert selection_v2._editorial_non_event({
        "canonical_title": "小鹏机器人业务完成首轮融资"
    }) is False


def _candidate(candidate_id, title, publisher, *, authority="media", evidence="足够长的原站正文证据，明确说明事件已经发生，并提供可以核对的事实细节。"):
    return {
        "candidate_id": candidate_id,
        "title": f"{title} - {publisher}",
        "summary": title,
        "url": f"https://news.google.com/articles/{candidate_id}",
        "evidence_url": f"https://{publisher.lower()}.example.com/{candidate_id}",
        "source_id": "google-news-cn-tech-media",
        "source_name": "中文科技媒体新闻聚合",
        "authority": authority,
        "evidence_status": "ok" if evidence else "failed",
        "evidence_excerpt": evidence,
        "china_short_video_hint": {"likely_china_relevance": "high"},
    }


def _event(event_id, heat="H3", score=70):
    return {
        "event_id": event_id,
        "canonical_title": f"事件{event_id}",
        "candidate_ids": [f"N-{event_id}"],
        "evidence_candidate_ids": [f"N-{event_id}"],
        "official_evidence_candidate_ids": [],
        "members": [],
        "evidence_gate": "pass",
        "risk_gate": "pass",
        "heat_level": heat,
        "observed_heat_score": score,
        "heat_signals": {},
    }


def _assessment(event_id, capacity="C2", understanding="U0", base=75):
    dimensions = ["event_core", "user_impact", "evidence_detail", "constraint"][: int(capacity[-1])]
    return {
        "event_id": event_id,
        "story_type": "product",
        "content_capacity": capacity,
        "understanding_cost": understanding,
        "editorial_potential_score": base,
        "audience_fit_score": base,
        "visual_potential_score": base,
        "selection_reason": "有事实、有受众价值，也容易形成清晰画面。",
        "coverage_plan": [
            {"dim": dim, "claim": f"{dim}对应的可靠事实", "source_candidate_ids": [f"N-{event_id}"]}
            for dim in dimensions
        ],
    }


def test_cluster_news_events_merges_same_event_and_deduplicates_publishers():
    candidates = [
        _candidate("N-1", "荣耀机器人百米跑出9.32秒", "ithome"),
        _candidate("N-2", "荣耀机器人百米跑出 9.32 秒", "ithome"),
        _candidate("N-3", "荣耀机器人百米9.32秒刷新纪录", "36kr"),
        _candidate("N-4", "英伟达发布新的机器人研究工具", "theverge"),
    ]

    events = selection_v2.cluster_news_events(candidates)

    assert len(events) == 2
    robot = next(event for event in events if len(event["candidate_ids"]) == 3)
    assert robot["independent_publisher_count"] == 2
    assert set(robot["publisher_keys"]) == {"36kr.example.com", "ithome.example.com"}


def test_cluster_keeps_different_products_from_same_brand_separate():
    candidates = [
        _candidate("N-MINI", "苹果全新 Mac mini 发布，6999元起售", "ithome"),
        _candidate("N-STUDIO", "苹果推出全新 Mac Studio，搭载M5 Ultra", "ithome"),
    ]

    events = selection_v2.cluster_news_events(candidates)

    assert len(events) == 2


def test_cluster_merges_different_angles_of_the_same_named_product():
    candidates = [
        _candidate("N-LAUNCH", "苹果全新 Mac mini 发布，6999元起售", "ithome"),
        _candidate("N-SPECS", "苹果新款 Mac mini 支持连接三台显示器", "ifanr"),
    ]

    events = selection_v2.cluster_news_events(candidates)

    assert len(events) == 1


def test_google_repost_and_direct_article_share_one_publisher_identity():
    direct = {
        "candidate_id": "N-DIRECT",
        "title": "苹果发布新款电脑",
        "summary": "苹果发布新款电脑",
        "url": "https://www.ithome.com/0/1.htm",
        "evidence_url": "https://www.ithome.com/0/1.htm",
        "evidence_status": "ok",
        "evidence_excerpt": "苹果发布新款电脑，价格和配置已经公布。",
        "source_name": "IT之家",
        "authority": "media",
    }
    repost = {
        "candidate_id": "N-REPOST",
        "title": "苹果发布新款电脑 - IT之家",
        "summary": "苹果发布新款电脑",
        "url": "https://news.google.com/articles/repost",
        "source_name": "中文科技媒体新闻聚合",
        "authority": "media",
    }

    event = selection_v2.cluster_news_events([direct, repost])[0]

    assert event["independent_publisher_count"] == 1
    assert event["publisher_keys"] == ["ithome.com"]


def test_gates_reject_seo_pollution_and_hold_single_source_rumour():
    seo = selection_v2.cluster_news_events([_candidate("N-1", "某楼盘售楼处咨询电话和户型图", "spam")])[0]
    rumour = selection_v2.cluster_news_events([_candidate("N-2", "消息称某旗舰模型参数泄露", "media")])[0]

    assert selection_v2.evaluate_event_gates(seo)["risk_gate"] == "fail"
    assert selection_v2.evaluate_event_gates(rumour)["risk_gate"] == "review"


def test_gate_rejects_evergreen_selection_guide_but_keeps_real_tool_launch():
    evergreen = selection_v2.cluster_news_events([
        _candidate(
            "N-GUIDE",
            "2026人形机器人公司选型参考：立足场景看企业落地路径",
            "media",
        )
    ])[0]
    launch = selection_v2.cluster_news_events([
        _candidate(
            "N-LAUNCH",
            "某公司今日发布机器人选型工具",
            "media",
        )
    ])[0]

    rejected = selection_v2.evaluate_event_gates(evergreen)
    accepted = selection_v2.evaluate_event_gates(launch)

    assert rejected["risk_gate"] == "fail"
    assert any("搜索污染" in reason for reason in rejected["gate_reasons"])
    assert accepted["risk_gate"] == "pass"


def test_2026_08_22_known_bad_leads_are_not_promoted_by_heat_or_wording():
    """Regression replay for the failure mix observed during the Aug-22 run."""
    leads = [
        _candidate("N-GTA", "GTA6未发布画面泄露，黑客放出完整视频", "media"),
        _candidate("N-CHIP", "DeepSeek与华为芯片完全替代GB300", "media"),
        _candidate("N-SEO", "AI机器人优选好房售楼处咨询电话", "spam"),
        _candidate("N-PRIVACY", "TikTok就儿童隐私诉讼达成和解", "reuters"),
    ]
    events = selection_v2.cluster_news_events(leads)
    results = {event["candidate_ids"][0]: selection_v2.evaluate_event_gates(event) for event in events}

    assert results["N-GTA"]["risk_gate"] == "review"
    assert results["N-CHIP"]["evidence_gate"] == "fail"
    assert results["N-SEO"]["risk_gate"] == "fail"
    assert results["N-PRIVACY"] == {
        "evidence_gate": "pass",
        "risk_gate": "pass",
        "strong_claim": False,
        "wording_policy": "verified_facts",
        "gate_reasons": [],
    }


def test_strong_claim_requires_official_or_two_independent_sources():
    event = selection_v2.cluster_news_events([_candidate("N-1", "国产芯片完全替代海外芯片", "media")])[0]

    result = selection_v2.evaluate_event_gates(event)

    assert result["strong_claim"] is True
    assert result["evidence_gate"] == "fail"


def test_platform_hot_event_keeps_expressive_title_wording_with_grounded_core_facts():
    event = selection_v2.cluster_news_events([
        _candidate("N-1", "8.86秒！人形机器人百米再破人类纪录", "ithome")
    ])[0]
    event["external_heat_matches"] = [{"source_id": "baidu-realtime", "rank": 37, "title": event["canonical_title"]}]

    result = selection_v2.evaluate_event_gates(event)

    assert result["evidence_gate"] == "pass"
    assert result["risk_gate"] == "pass"
    assert result["wording_policy"] == "hot_topic_expressive"
    assert any("传播性标题口径" in reason for reason in result["gate_reasons"])


def test_public_rank_is_primary_heat_signal_for_entertainment_brief():
    event = selection_v2.cluster_news_events([
        _candidate("N-1", "8.86秒！人形机器人百米再破纪录", "ithome")
    ])[0]
    event["external_heat_matches"] = [{"source_id": "baidu-realtime", "rank": 37, "title": event["canonical_title"]}]

    heat = selection_v2.observed_heat(event)

    assert heat["heat_level"] == "H3"
    assert heat["observed_heat_score"] >= 65


def test_product_specific_heat_does_not_spill_into_a_different_product():
    event = {"canonical_title": "苹果推出全新 Mac Studio，搭载M5 Ultra"}
    signals = [{"source_id": "baidu-realtime", "title": "苹果全新Mac mini发布 6999元起售", "rank": 23}]

    assert selection_v2._external_heat_matches(event, signals) == []


def test_product_family_matches_when_english_name_touches_chinese_text():
    signals = [{"source_id": "baidu-realtime", "title": "苹果全新Mac mini发布 6999元起售", "rank": 23}]

    assert selection_v2._external_heat_matches(
        {"canonical_title": "新款 Mac mini 发布！价格大涨2500元"}, signals
    )
    assert selection_v2._external_heat_matches(
        {"canonical_title": "MINI车机接入阿里与DeepSeek模型"}, signals
    ) == []


def test_general_public_heat_outside_technology_scope_does_not_enter_selection():
    event = selection_v2.cluster_news_events([
        _candidate("N-1", "公司批量劝退应届生，当地发布通报", "media")
    ])[0]
    event["external_heat_matches"] = [{"source_id": "baidu-realtime", "rank": 5}]

    result = selection_v2.evaluate_event_gates(event)

    assert result["risk_gate"] == "fail"
    assert "热榜事件不属于科技快报内容范围" in result["gate_reasons"]


def test_strong_claim_needs_two_independent_evidence_publishers_not_two_titles():
    candidates = [
        _candidate("N-1", "国产芯片完全替代海外芯片", "media-a"),
        _candidate("N-2", "国产芯片完全替代海外芯片", "media-b", evidence=""),
    ]
    event = selection_v2.cluster_news_events(candidates)[0]

    result = selection_v2.evaluate_event_gates(event)

    assert event["independent_publisher_count"] == 2
    assert event["independent_evidence_publisher_count"] == 1
    assert result["evidence_gate"] == "fail"


def test_marketing_record_claim_with_intervening_distance_is_still_strong():
    event = selection_v2.cluster_news_events([
        _candidate("N-1", "荣耀机器人打破1500米人类世界纪录", "media")
    ])[0]

    result = selection_v2.evaluate_event_gates(event)

    assert result["strong_claim"] is True
    assert result["evidence_gate"] == "fail"


def test_observed_heat_uses_unique_publishers_not_duplicate_headlines():
    one_publisher = selection_v2.cluster_news_events([
        _candidate(f"N-{index}", "机器人完成高难度工业装配", "ithome") for index in range(4)
    ])[0]
    two_publishers = selection_v2.cluster_news_events([
        _candidate("N-A", "机器人完成高难度工业装配", "ithome"),
        _candidate("N-B", "机器人完成高难度工业装配", "36kr"),
    ])[0]

    assert selection_v2.observed_heat(one_publisher)["observed_heat_score"] < selection_v2.observed_heat(two_publishers)["observed_heat_score"]


def test_v2_evidence_prefetch_is_bounded_diverse_and_failure_isolated(monkeypatch):
    candidates = [
        {
            "candidate_id": f"N-{index}",
            "title": "同一事件重复标题" if index < 4 else f"不同事件 {index}",
            "summary": "机器人高难动作",
        }
        for index in range(12)
    ]

    def enrich(candidate):
        if candidate["candidate_id"] == "N-5":
            raise RuntimeError("temporary failure")
        candidate["evidence_status"] = "ok"

    monkeypatch.setattr(selection_v2, "_enrich_candidate_evidence", enrich)
    attempted = selection_v2.prefetch_selection_evidence_v2(candidates, limit=6, max_workers=3)

    assert len(attempted) == 6
    assert len(attempted & {"N-0", "N-1", "N-2", "N-3"}) == 1
    assert all("evidence_status" in candidate for candidate in candidates if candidate["candidate_id"] in attempted)


def test_v2_evidence_prefetch_prioritizes_platform_hot_candidates(monkeypatch):
    candidates = [
        {"candidate_id": "N-ORDINARY", "title": "普通行业参数更新", "summary": "普通更新"},
        {"candidate_id": "N-HOT", "title": "人形机器人百米竞速", "summary": "平台热点"},
    ]
    attempted_order = []

    def enrich(candidate):
        attempted_order.append(candidate["candidate_id"])
        candidate["evidence_status"] = "ok"

    monkeypatch.setattr(selection_v2, "_enrich_candidate_evidence", enrich)

    selection_v2.prefetch_selection_evidence_v2(
        candidates,
        limit=1,
        max_workers=1,
        priority_candidate_ids=["N-HOT"],
    )

    assert attempted_order == ["N-HOT"]


def test_validation_rejects_capacity_inflated_by_repeated_dimension():
    event = _event("ONE")
    raw = {"assessments": [_assessment("ONE", capacity="C3")]}
    raw["assessments"][0]["coverage_plan"][2]["dim"] = "user_impact"

    with pytest.raises(selection_v2.NewsSelectionV2ValidationError, match="重复使用信息维度|容量"):
        selection_v2._validate_assessments(raw, [event])


def test_validated_coverage_plan_gets_deterministic_claim_ids():
    rows = selection_v2._validate_assessments({"assessments": [_assessment("ONE", "C2")]}, [_event("ONE")])

    assert [item["claim_id"] for item in rows[0]["coverage_plan"]] == [
        "ONE-event_core",
        "ONE-user_impact",
    ]


def test_reported_capacity_is_normalized_without_rejecting_the_packet():
    raw = {"assessments": [_assessment("ONE", "C2")]}
    raw["assessments"][0]["coverage_plan"] = raw["assessments"][0]["coverage_plan"][:1]

    rows = selection_v2._validate_assessments(raw, [_event("ONE")])

    assert rows[0]["reported_content_capacity"] == "C2"
    assert rows[0]["content_capacity"] == "C1"
    assert rows[0]["capacity_normalized"] is True


def test_c1_is_rejected_and_u2_adds_explanation_without_adding_facts():
    research = {"target_date": "2026-08-22", "candidates": [{}, {}, {}, {}]}
    events = [_event("ONE", "H4", 90), _event("TWO"), _event("THREE"), _event("FOUR")]
    assessments = [
        _assessment("ONE", "C1", "U2", 99),
        _assessment("TWO", "C2", "U2", 80),
        _assessment("THREE", "C3", "U0", 75),
        _assessment("FOUR", "C3", "U0", 70),
    ]

    result = selection_v2.build_selection_result(research, events, assessments, model="test-model")

    assert "ONE" not in {story["event_id"] for story in result["selected_stories"]}
    u2_story = next(story for story in result["selected_stories"] if story["event_id"] == "TWO")
    assert u2_story["max_fact_lines"] == 2
    assert u2_story["explanation_slots"] == 1
    assert u2_story["allocated_planning_units"] == 3


def test_dynamic_selection_accepts_all_high_heat_without_fixed_combination():
    research = {"target_date": "2026-08-22", "candidates": [{}, {}, {}, {}]}
    events = [_event("ONE", "H4", 94), _event("TWO", "H4", 90), _event("THREE", "H3", 76), _event("FOUR", "H3", 70)]
    assessments = [_assessment(event["event_id"], "C3", base=90 - index) for index, event in enumerate(events)]

    result = selection_v2.build_selection_result(research, events, assessments, model="test-model")

    assert len(result["selected_stories"]) == 4
    assert set(result["selection_summary"]["heat_distribution"]) == {"H4", "H3", "H2", "H1"}
    assert result["selection_summary"]["heat_distribution"]["H4"] == 2
    assert result["selection_summary"]["fixed_heat_or_capacity_combination_used"] is False


def test_portfolio_prefers_platform_hot_visual_event_over_niche_overseas_phones():
    research = {"target_date": "2026-08-25", "candidates": [{}, {}, {}, {}, {}, {}]}
    events = [
        _event("ROBOT", "H3", 68),
        _event("HARDTECH", "H2", 58),
        _event("AI", "H2", 55),
        _event("OPEN", "H2", 52),
        _event("XIAOMI-PHONE", "H2", 49),
        _event("INDIA-PHONE", "H2", 45),
    ]
    events[0]["canonical_title"] = "8.86秒！人形机器人百米竞速"
    events[0]["external_heat_matches"] = [{"source_id": "baidu-realtime", "rank": 37}]
    events[0]["heat_signals"] = {"domestic_public_heat_match_count": 1, "domestic_public_best_rank": 37}
    events[4]["canonical_title"] = "小米REDMI手机海外发布"
    events[5]["canonical_title"] = "印度品牌Boltt手机发布"
    assessments = [_assessment(event["event_id"], "C3", base=78) for event in events]
    assessments[0].update({"story_type": "visual_event", "visual_potential_score": 96, "audience_fit_score": 90})
    for index in (4, 5):
        assessments[index].update({"editorial_potential_score": 95, "audience_fit_score": 90})
        assessments[index]["coverage_plan"][0]["claim"] = "海外发布，配备6000mAh电池和128GB存储，售价999元"

    result = selection_v2.build_selection_result(research, events, assessments, model="test-model")

    selected_ids = {story["event_id"] for story in result["selected_stories"]}
    assert "ROBOT" in selected_ids
    assert "XIAOMI-PHONE" not in selected_ids
    assert "INDIA-PHONE" not in selected_ids
    assert result["selection_summary"]["public_heat_selected_count"] == 1
    assert result["selection_summary"]["low_value_parameter_selected_count"] == 0


def test_h1_supplement_is_capped_at_two_information_units():
    research = {"target_date": "2026-08-22", "candidates": [{}, {}, {}]}
    events = [_event("ONE", "H3"), _event("TWO", "H2"), _event("THREE", "H1")]
    assessments = [_assessment(event["event_id"], "C4") for event in events]

    result = selection_v2.build_selection_result(research, events, assessments, model="test-model")
    h1 = next(story for story in result["selected_stories"] if story["event_id"] == "THREE")

    assert h1["allocated_planning_units"] == 2
    assert len(h1["coverage_plan"]) == 2
    assert len(h1["available_coverage_plan"]) == 4


def test_selection_deduplicates_different_headline_angles_of_same_frozen_event():
    research = {"target_date": "2026-08-24", "candidates": [{}, {}, {}, {}]}
    events = [
        _event("XIAOMI-QUOTE", "H3", 80),
        _event("XIAOMI-LAUNCH", "H3", 78),
        _event("AIRPODS", "H2", 70),
        _event("ROBOT", "H2", 68),
    ]
    events[0]["canonical_title"] = "小米高管谈玄戒芯片投入"
    events[1]["canonical_title"] = "小米发布新一代玄戒芯片"
    assessments = [_assessment(event["event_id"], "C3", base=80 - index) for index, event in enumerate(events)]
    assessments[0]["coverage_plan"][0]["claim"] = "小米发布玄戒O3、O100、D100三款芯片"
    assessments[1]["coverage_plan"][0]["claim"] = "玄戒O3、O100和D100在沟通会上发布"

    result = selection_v2.build_selection_result(research, events, assessments, model="test-model")

    selected_ids = {story["event_id"] for story in result["selected_stories"]}
    assert len(selected_ids & {"XIAOMI-QUOTE", "XIAOMI-LAUNCH"}) == 1
    assert result["funnel"]["raw_eligible_event_count"] == 4
    assert any("duplicate_frozen_fact_event" in item["reasons"] for item in result["funnel"]["rejections"])


def test_selection_deduplicates_one_shared_model_with_same_named_product():
    left = (_event("LEFT"), _assessment("LEFT", "C3"))
    right = (_event("RIGHT"), _assessment("RIGHT", "C3"))
    left[0]["canonical_title"] = "小米谈玄戒芯片长期投入"
    right[0]["canonical_title"] = "玄戒D100智驾芯片明年商用"
    left[1]["coverage_plan"][0]["claim"] = "小米发布玄戒O3、O100和D100"
    right[1]["coverage_plan"][0]["claim"] = "玄戒D100面向智能驾驶场景"

    assert selection_v2._selection_fact_overlap(left, right) is True


def test_selection_keeps_high_value_compact_script_instead_of_padding_to_90_seconds():
    research = {"target_date": "2026-08-22", "candidates": [{}, {}, {}]}
    events = [_event("ONE"), _event("TWO"), _event("THREE")]
    assessments = [_assessment(event["event_id"], "C2", "U0") for event in events]

    result = selection_v2.build_selection_result(research, events, assessments, model="test-model")

    assert result["selection_summary"]["planned_units_total"] == 6
    assert result["selection_summary"]["duration_profile"] == "compact_high_value"
    assert "禁止以低价值选题或重复台词凑时长" in result["selection_summary"]["planning_warnings"][0]


def test_episode_portfolio_drops_low_marginal_fourth_story_instead_of_padding():
    research = {"target_date": "2026-08-28", "candidates": [{}, {}, {}, {}]}
    events = [
        _event("HOT-A", "H4", 94),
        _event("HOT-B", "H3", 82),
        _event("VISUAL", "H2", 62),
        _event("WEAK-TAIL", "H1", 20),
    ]
    assessments = [
        _assessment("HOT-A", "C4", base=94),
        _assessment("HOT-B", "C3", base=88),
        _assessment("VISUAL", "C2", base=94),
        _assessment("WEAK-TAIL", "C2", base=25),
    ]
    assessments[0]["topic_family"] = "ai_models"
    assessments[1]["topic_family"] = "chips_compute"
    assessments[2]["topic_family"] = "robotics"
    assessments[3]["topic_family"] = "other"

    result = selection_v2.build_selection_result(research, events, assessments, model="test-model")

    assert len(result["selected_stories"]) == 3
    assert "WEAK-TAIL" not in {story["event_id"] for story in result["selected_stories"]}


def test_episode_portfolio_keeps_more_public_heat_before_diversity_score():
    research = {"target_date": "2026-08-28", "candidates": [{}, {}, {}, {}]}
    events = [
        _event("HOT-A", "H4", 94),
        _event("HOT-B", "H4", 90),
        _event("VISUAL", "H2", 60),
        _event("DIVERSE", "H2", 58),
    ]
    for rank, event in enumerate(events[:2], 1):
        event["external_heat_matches"] = [{"source_id": "douyin", "rank": rank}]
    assessments = [
        _assessment("HOT-A", "C4", base=88),
        _assessment("HOT-B", "C3", base=86),
        _assessment("VISUAL", "C2", base=92),
        _assessment("DIVERSE", "C2", base=92),
    ]
    assessments[0]["topic_family"] = "ai_models"
    assessments[1]["topic_family"] = "chips_compute"
    assessments[2]["topic_family"] = "robotics"
    assessments[3]["topic_family"] = "gaming"

    result = selection_v2.build_selection_result(research, events, assessments, model="test-model")

    selected_ids = {story["event_id"] for story in result["selected_stories"]}
    assert {"HOT-A", "HOT-B"} <= selected_ids
    assert result["selection_summary"]["public_heat_selected_count"] == 2


def test_episode_portfolio_does_not_keep_two_unheated_game_trailers_after_hot_robot_lead():
    research = {"target_date": "2026-08-26", "candidates": [{}, {}, {}, {}]}
    events = [
        _event("ROBOT", "H3", 68),
        _event("RESISTANCE", "H1", 24),
        _event("SHOWA", "H1", 24),
        _event("FOLD", "H1", 20),
    ]
    events[0]["canonical_title"] = "8.64秒！天工Ultra机器人百米夺冠"
    events[0]["external_heat_matches"] = [{"source_id": "baidu-realtime", "rank": 46}]
    events[1]["canonical_title"] = "国产游戏《抵抗者》公布科隆展实机预告"
    events[2]["canonical_title"] = "《昭和米国物语》公布实机预告和配音阵容"
    events[3]["canonical_title"] = "苹果折叠手机主板曝光"
    assessments = [
        _assessment("ROBOT", "C4", base=88),
        _assessment("RESISTANCE", "C2", base=61),
        _assessment("SHOWA", "C2", base=60),
        _assessment("FOLD", "C2", base=48),
    ]
    assessments[0]["story_type"] = "visual_event"
    for assessment in assessments[1:3]:
        assessment["story_type"] = "product"

    result = selection_v2.build_selection_result(research, events, assessments, model="test-model")

    selected = result["selected_stories"]
    selected_game_trailers = [
        story for story in selected
        if story["topic_family"] == "gaming" and story["event_form"] == "trailer_announcement"
    ]
    assert selected[0]["event_id"] == "ROBOT"
    assert len(selected_game_trailers) <= 1
    assert len(result["episode_combinations"]) == 3
    assert all("marginal_contribution" in story for story in selected)


def test_episode_portfolio_uses_two_story_compact_version_when_only_third_story_is_duplicate_h1():
    research = {"target_date": "2026-08-26", "candidates": [{}, {}, {}]}
    events = [_event("ROBOT", "H3", 68), _event("GAME-A", "H1", 24), _event("GAME-B", "H1", 23)]
    events[0]["canonical_title"] = "天工机器人百米比赛跑出8.64秒"
    events[0]["external_heat_matches"] = [{"source_id": "baidu-realtime", "rank": 46}]
    events[1]["canonical_title"] = "国产游戏甲公布实机预告"
    events[2]["canonical_title"] = "国产游戏乙公布实机预告"
    assessments = [_assessment("ROBOT", "C4", base=88), _assessment("GAME-A", "C2", base=61), _assessment("GAME-B", "C2", base=60)]
    assessments[0]["story_type"] = "visual_event"

    result = selection_v2.build_selection_result(research, events, assessments, model="test-model")

    assert len(result["selected_stories"]) == 2
    assert result["selection_summary"]["duration_profile"] == "compact_high_value"
    assert result["selection_summary"]["rescue_recommended"] is True


def test_qualified_backup_contains_complete_frozen_story_contract():
    research = {"target_date": "2026-08-26", "candidates": [{}, {}, {}, {}, {}]}
    events = [_event("ONE", "H3"), _event("TWO", "H2"), _event("THREE", "H2"), _event("FOUR", "H1"), _event("FIVE", "H1")]
    assessments = [_assessment(event["event_id"], "C3") for event in events]

    result = selection_v2.build_selection_result(research, events, assessments, model="test-model")
    backup = result["funnel"]["qualified_backups"][0]

    assert backup["coverage_plan"]
    assert backup["evidence_candidate_ids"]
    assert backup["topic_family"] in selection_v2.ALLOWED_TOPIC_FAMILIES
    assert backup["event_form"] in selection_v2.ALLOWED_EVENT_FORMS
    assert isinstance(backup["difference_fit_score"], float)


def test_selector_strictly_validates_model_event_ids(monkeypatch):
    research = {"target_date": "2026-08-22", "candidates": [{"candidate_id": f"N-{index}"} for index in range(3)]}
    events = [_event("ONE"), _event("TWO"), _event("THREE")]
    monkeypatch.setattr(selection_v2, "prepare_selection_events", lambda _research: events)
    monkeypatch.setattr(
        selection_v2,
        "_chat_json",
        lambda *_args, **_kwargs: ({"assessments": [_assessment("UNKNOWN")]}, "test-model"),
    )

    with pytest.raises(selection_v2.NewsSelectionV2ValidationError, match="未知事件"):
        selection_v2.select_daily_news_v2(research, max_revision_rounds=0)


def test_selector_uses_doubao_as_china_market_editor(monkeypatch):
    research = {"target_date": "2026-08-26", "candidates": [{"candidate_id": f"N-{index}"} for index in range(3)]}
    events = [_event(name) for name in ("ROBOT", "CHIP", "AI")]
    monkeypatch.setattr(selection_v2, "prepare_selection_events", lambda _research: events)
    monkeypatch.setattr(selection_v2, "daily_editorial_provider", lambda: "doubao")
    calls = []

    def assess(_prompt, payload, **kwargs):
        calls.append(kwargs.get("provider"))
        return {"assessments": [_assessment(event["event_id"], "C3") for event in payload["events"]]}, "doubao-editor"

    monkeypatch.setattr(selection_v2, "_chat_json", assess)

    result = selection_v2.select_daily_news_v2(research, max_revision_rounds=0)

    assert calls == ["doubao"]
    assert result["provider"] == "doubao"
    assert result["selection_audit"] == {
        "role": "china_short_video_editor",
        "preferred_provider": "doubao",
        "actual_provider": "doubao",
        "provider_fallback": None,
        "model": "doubao-editor",
    }


def test_selector_falls_back_to_default_only_when_doubao_is_unavailable(monkeypatch):
    research = {"target_date": "2026-08-26", "candidates": [{"candidate_id": f"N-{index}"} for index in range(3)]}
    events = [_event(name) for name in ("ROBOT", "CHIP", "AI")]
    monkeypatch.setattr(selection_v2, "prepare_selection_events", lambda _research: events)
    monkeypatch.setattr(selection_v2, "daily_editorial_provider", lambda: "doubao")
    calls = []

    def assess(_prompt, payload, **kwargs):
        provider = kwargs.get("provider")
        calls.append(provider)
        if provider == "doubao":
            raise selection_v2.TextAIError("HTTP 503 provider unavailable")
        return {"assessments": [_assessment(event["event_id"], "C3") for event in payload["events"]]}, "luna-fallback"

    monkeypatch.setattr(selection_v2, "_chat_json", assess)

    result = selection_v2.select_daily_news_v2(research, max_revision_rounds=0)

    assert calls == ["doubao", "default"]
    assert result["provider"] == "default"
    assert result["selection_audit"]["preferred_provider"] == "doubao"
    assert result["selection_audit"]["provider_fallback"]["to"] == "default"


def test_selector_backfills_when_first_batch_contains_only_low_capacity_events(monkeypatch):
    research = {"target_date": "2026-08-22", "candidates": [{"candidate_id": f"N-{index}"} for index in range(6)]}
    events = [_event(name, "H3", 80 - index) for index, name in enumerate(("ONE", "TWO", "THREE", "FOUR", "FIVE", "SIX"))]
    monkeypatch.setattr(selection_v2, "prepare_selection_events", lambda _research: events)
    calls = []

    def assess(_prompt, payload, **_kwargs):
        calls.append([event["event_id"] for event in payload["events"]])
        rows = []
        for event in payload["events"]:
            capacity = "C1" if event["event_id"] in {"ONE", "TWO", "THREE"} else "C3"
            rows.append(_assessment(event["event_id"], capacity))
        return {"assessments": rows}, "test-model"

    monkeypatch.setattr(selection_v2, "_chat_json", assess)

    result = selection_v2.select_daily_news_v2(research, model_events_limit=3, max_revision_rounds=0)

    assert calls == [["ONE", "TWO", "THREE"], ["FOUR", "FIVE", "SIX"]]
    assert {story["event_id"] for story in result["selected_stories"]} == {"FOUR", "FIVE", "SIX"}


def test_selector_assesses_one_extra_batch_when_first_valid_portfolio_is_too_narrow(monkeypatch):
    research = {"target_date": "2026-08-26", "candidates": [{"candidate_id": f"N-{index}"} for index in range(6)]}
    events = [_event(name, "H1", 30) for name in ("ROBOT", "GAME-A", "GAME-B", "CHIP", "SECURITY", "AI")]
    events[0].update({
        "canonical_title": "天工人形机器人百米比赛跑出8.64秒",
        "heat_level": "H3",
        "observed_heat_score": 72,
        "external_heat_matches": [{"source_id": "baidu-realtime", "rank": 37}],
    })
    events[1]["canonical_title"] = "国产游戏甲公布实机预告"
    events[2]["canonical_title"] = "国产游戏乙公布实机预告"
    events[3].update({"canonical_title": "国产公司发布三款自研芯片", "heat_level": "H2", "observed_heat_score": 58})
    events[4].update({"canonical_title": "平台修复人工智能隐私漏洞", "heat_level": "H2", "observed_heat_score": 56})
    events[5].update({"canonical_title": "开源大模型发布新版本", "heat_level": "H2", "observed_heat_score": 54})
    monkeypatch.setattr(selection_v2, "prepare_selection_events", lambda _research: events)
    calls = []

    def assess(_prompt, payload, **_kwargs):
        calls.append([event["event_id"] for event in payload["events"]])
        rows = []
        for event in payload["events"]:
            capacity = "C4" if event["event_id"] == "ROBOT" else "C3"
            base = 88 if event["event_id"] == "ROBOT" else 76
            rows.append(_assessment(event["event_id"], capacity, base=base))
        return {"assessments": rows}, "test-model"

    monkeypatch.setattr(selection_v2, "_chat_json", assess)

    result = selection_v2.select_daily_news_v2(research, model_events_limit=3, max_revision_rounds=0)

    assert calls == [["ROBOT", "GAME-A", "GAME-B"], ["CHIP", "SECURITY", "AI"]]
    assert result["selection_summary"]["rescue_rounds_used"] == 1
    assert result["selection_summary"]["rescue_mode"] == "remaining_evidence_pool"
    assert {story["event_id"] for story in result["selected_stories"]} & {"CHIP", "SECURITY", "AI"}


def test_selector_assesses_one_extra_batch_when_first_portfolio_contains_weak_h1_rumor(monkeypatch):
    research = {"target_date": "2026-08-26", "candidates": [{"candidate_id": f"N-{index}"} for index in range(6)]}
    events = [_event(name, "H1", 32) for name in ("ROBOT", "GAME", "PHONE", "CHIP", "SECURITY", "SCIENCE")]
    events[0].update({
        "canonical_title": "人形机器人百米比赛跑出新成绩",
        "heat_level": "H3",
        "observed_heat_score": 75,
        "external_heat_matches": [{"source_id": "douyin", "rank": 37}],
    })
    events[1]["canonical_title"] = "国产游戏公布实机演示"
    events[2]["canonical_title"] = "消息曝光苹果折叠手机主板参数"
    events[3].update({"canonical_title": "国产公司发布自研芯片", "heat_level": "H2", "observed_heat_score": 58})
    events[4].update({"canonical_title": "平台治理人工智能诈骗风险", "heat_level": "H2", "observed_heat_score": 57})
    events[5].update({"canonical_title": "中国团队公布航天实验进展", "heat_level": "H2", "observed_heat_score": 55})
    monkeypatch.setattr(selection_v2, "prepare_selection_events", lambda _research: events)
    calls = []

    def assess(_prompt, payload, **_kwargs):
        calls.append([event["event_id"] for event in payload["events"]])
        return {
            "assessments": [
                _assessment(event["event_id"], "C3", base=86 if event["event_id"] == "ROBOT" else 76)
                for event in payload["events"]
            ]
        }, "test-model"

    monkeypatch.setattr(selection_v2, "_chat_json", assess)

    result = selection_v2.select_daily_news_v2(research, model_events_limit=3, max_revision_rounds=0)

    assert calls == [["ROBOT", "GAME", "PHONE"], ["CHIP", "SECURITY", "SCIENCE"]]
    assert result["selection_summary"]["rescue_rounds_used"] == 1
    assert all(story["event_id"] != "PHONE" for story in result["selected_stories"])


def test_run_persists_v2_without_overwriting_legacy_selection(monkeypatch, tmp_path):
    monkeypatch.setattr(selection_v2, "RUNS_ROOT", tmp_path)
    run_dir = tmp_path / "2026-08-22"
    run_dir.mkdir(parents=True)
    research = {"target_date": "2026-08-22", "candidates": [{"candidate_id": "N-1"}]}
    (run_dir / "news_research.json").write_text(json.dumps(research), encoding="utf-8")
    legacy = {"legacy": True}
    (run_dir / "topic_selection.json").write_text(json.dumps(legacy), encoding="utf-8")
    monkeypatch.setattr(
        selection_v2,
        "select_daily_news_v2",
        lambda value: {
            "selection_summary": {"selected_count": 3},
            "selected_stories": [],
            "research_seen": value["target_date"],
        },
    )

    result = selection_v2.run_news_selection_v2("2026-08-22", trigger="test")

    assert result["run"]["legacy_topic_selection_untouched"] is True
    assert json.loads((run_dir / "topic_selection.json").read_text(encoding="utf-8")) == legacy
    assert (run_dir / "topic_selection_v2.json").is_file()
    assert (run_dir / "news_research_v2.json").is_file()


def test_run_persists_failed_state_without_overwriting_legacy_selection(monkeypatch, tmp_path):
    monkeypatch.setattr(selection_v2, "RUNS_ROOT", tmp_path)
    run_dir = tmp_path / "2026-08-22"
    run_dir.mkdir(parents=True)
    (run_dir / "news_research.json").write_text(
        json.dumps({"target_date": "2026-08-22", "candidates": [{"candidate_id": "N-1"}]}),
        encoding="utf-8",
    )
    (run_dir / "topic_selection.json").write_text('{"legacy":true}', encoding="utf-8")
    monkeypatch.setattr(selection_v2, "select_daily_news_v2", lambda _value: (_ for _ in ()).throw(RuntimeError("provider unavailable")))

    with pytest.raises(RuntimeError, match="provider unavailable"):
        selection_v2.run_news_selection_v2("2026-08-22", trigger="test")

    manifest = json.loads((run_dir / "news_selection_v2_run.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "failed"
    assert manifest["legacy_topic_selection_untouched"] is True
    assert json.loads((run_dir / "topic_selection.json").read_text(encoding="utf-8")) == {"legacy": True}


def test_cli_select_v2_uses_independent_entrypoint(monkeypatch, capsys):
    called = {}
    monkeypatch.setattr(daily_cli, "try_acquire_run_lock", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(daily_cli, "release_run_lock", lambda: None)
    monkeypatch.setattr(
        daily_cli,
        "run_news_selection_v2",
        lambda target, trigger: called.update({"target": target.isoformat(), "trigger": trigger}) or {"ok": True},
    )
    monkeypatch.setattr(
        "sys.argv",
        ["daily_cli.py", "select-v2", "--target-date", "2026-08-22", "--trigger", "test"],
    )

    assert daily_cli.main() == 0
    assert called == {"target": "2026-08-22", "trigger": "test"}
    assert json.loads(capsys.readouterr().out) == {"ok": True}
