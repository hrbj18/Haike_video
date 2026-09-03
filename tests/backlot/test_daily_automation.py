from __future__ import annotations

from datetime import date, datetime
from datetime import timedelta, timezone
import json

import pytest
import backlot.daily_automation as daily_automation

from backlot.daily_automation import (
    BudgetLedger,
    DailyAutomationError,
    DailyScriptValidationError,
    DailyTopicValidationError,
    classify_runninghub_failure,
    evaluate_media_release,
    previous_target_date,
    target_window,
    validate_daily_script,
    validate_topic_selection,
)


def _release_script(total=86, hook=16, dialogue=16, density=20, heat="H2"):
    return {
        "validation": {"passed": True},
        "editorial_review": {"passed": True, "quality_band": "fallback_publishable", "total": total,
                             "scores": {"hook": hook, "dialogue": dialogue, "information_density": density}},
        "topic_selection": {"selected_stories": [{"heat_level": heat}]},
    }


def test_media_release_matrix_is_versioned_and_deterministic():
    premium = evaluate_media_release(_release_script())
    assert premium["decision"] == "auto_release"
    assert evaluate_media_release(_release_script())["input_fingerprint"] == premium["input_fingerprint"]
    assert evaluate_media_release(_release_script(total=82))["decision"] == "fallback_review_candidate"
    assert evaluate_media_release(_release_script(dialogue=15))["decision"] == "fallback_review_candidate"
    assert evaluate_media_release(_release_script(heat="H3"))["decision"] == "auto_release"
    broken = _release_script()
    broken["validation"] = {"passed": False, "errors": ["角色未交替"]}
    assert evaluate_media_release(broken)["decision"] == "blocked"


def test_budget_operations_are_idempotent_and_settled_cannot_release(monkeypatch):
    run = {"target_date": "2026-08-27", "budget": {"limit": 5.0, "reserved": 0.0, "spent": 0.0, "entries": []}}
    monkeypatch.setattr(daily_automation, "_save_run", lambda value: value)
    ledger = BudgetLedger(run)
    for _ in range(10):
        ledger.reserve_once("op-1", 2.5, purpose="雅雅")
    assert run["budget"]["reserved"] == 2.5
    for _ in range(10):
        ledger.settle_once("op-1", 2.5, 0.4, purpose="雅雅", task_id="task-1")
    assert run["budget"]["reserved"] == 0
    assert run["budget"]["spent"] == 0.4
    with pytest.raises(DailyAutomationError):
        ledger.release_once("op-1", 2.5, purpose="雅雅", reason="错误释放")


def _valid_selection() -> dict:
    stories = [
        {
            "story_id": "S01", "candidate_ids": ["N-ONE"], "headline": "声音克隆诈骗风险",
            "tier": "S", "category": "民生吃瓜", "three_second_summary": "十秒录音就可能克隆熟人声音",
            "why_public_cares": "普通人可能因熟悉声音放松警惕并转账",
            "public_value": "提醒普通人遇到熟悉声音催款时先核实",
            "terms_to_explain": [],
            "event_tension": "技术门槛降低与诈骗风险上升形成冲突",
            "comment_hook": "接到熟人声音催转账时会不会先挂断核实",
        },
        {
            "story_id": "S02", "candidate_ids": ["N-TWO"], "headline": "国产机器人高难动作",
            "tier": "A", "category": "国产突破", "three_second_summary": "国产机器人现场完成高难动作",
            "why_public_cares": "大众能直观看到国产机器人运动能力进步",
            "public_value": "让普通人更直观看懂机器人动作能力进展",
            "terms_to_explain": [],
            "event_tension": "科幻画面正在变成真实产品演示",
            "comment_hook": "机器人炫技和进厂打工哪个更有价值",
        },
        {
            "story_id": "S03", "candidate_ids": ["N-THREE"], "headline": "国产芯片能力突破",
            "tier": "A", "category": "国产突破", "three_second_summary": "国产芯片展示了新的实际运行能力",
            "why_public_cares": "大众可以更直观看到国产硬件的真实进展",
            "public_value": "更直观看懂进展",
            "terms_to_explain": [],
            "event_tension": "从参数宣传走到真实场景接受检验",
            "comment_hook": "你更关心参数领先还是实际使用表现",
        },
        {
            "story_id": "S04", "candidate_ids": ["N-FOUR"], "headline": "企业数据规则更新",
            "tier": "B", "category": "行业事件", "three_second_summary": "企业使用模型时可减少数据留存",
            "why_public_cares": "企业员工上传工作资料时隐私风险可能降低",
            "public_value": "能避坑",
            "terms_to_explain": [],
            "event_tension": "模型便利与企业数据安全需要同时兼顾",
            "comment_hook": "公司是否应该允许员工上传内部资料",
        },
    ]
    return {
        "selected_stories": stories,
        "combination_reason": "民生风险拉关注，国产突破提调性，行业规则快速收尾。",
        "topic_scores": {
            "sab_combination": 27, "public_relevance": 21, "event_tension": 16,
            "interaction_potential": 12, "reliability_freshness": 10, "total": 86,
        },
        "validation": {"valid": True, "story_count": 4, "tiers": ["S", "A", "A", "B"], "topic_score": 86},
    }


def test_standard_24gb_authorization_is_scoped_to_one_run(monkeypatch, tmp_path):
    monkeypatch.setattr(daily_automation, "RUNS_ROOT", tmp_path)

    run = daily_automation.authorize_runninghub_standard_for_run(
        "2026-08-24",
        reason="用户明确允许本次凌晨生产使用24GB显存任务",
        max_budget_cny=5.0,
    )

    assert run["provider_policy"]["authorized_instance"] == "default"
    assert run["provider_policy"]["authorization_scope"] == "single_daily_run"
    assert run["provider_policy"]["authorization_target_date"] == "2026-08-24"
    assert run["approval_policy"]["runninghub_standard_24gb_preapproved"] is True
    assert run["approval_policy"]["formal_publish_requires_human"] is True
    assert run["budget"]["limit"] <= 5.0


def test_baidu_public_heat_snapshot_is_parsed_as_signal_not_evidence():
    class Response:
        text = '<html><!--s-data:{"data":{"cards":[{"content":[{"word":"国产机器人比赛","hotScore":"98765","rawUrl":"https://example.com/a"}]}]}}--></html>'

        @staticmethod
        def raise_for_status():
            return None

    signals, sources = daily_automation.collect_heat_signals(
        sources=[{"id": "baidu-realtime", "name": "百度实时热榜", "kind": "baidu_board", "url": "https://top.baidu.com/board?tab=realtime"}],
        request_get=lambda *_args, **_kwargs: Response(),
    )

    assert sources[0]["status"] == "ok"
    assert signals[0]["title"] == "国产机器人比赛"
    assert signals[0]["rank"] == 1
    assert signals[0]["heat_value"] == 98765
    assert signals[0]["scope"] == "domestic_public_heat_snapshot"


def test_douyin_api_signal_uses_bearer_and_never_becomes_evidence(monkeypatch):
    monkeypatch.setenv("DOUYIN_API_KEY", "douyin-test-secret")
    seen = {}

    class Response:
        @staticmethod
        def raise_for_status():
            return None

        @staticmethod
        def json():
            return {"data": {"list": [{"word": "国产机器人走进工厂", "hotScore": 9988, "extra": "drop-me"}]}}

    def fake_get(url, **kwargs):
        seen.update({"url": url, **kwargs})
        return Response()

    signals, sources = daily_automation.collect_douyin_sources([{
        "id": "douyin-hotboard", "name": "抖音热榜", "kind": "douyin_board",
        "api_url": "https://data.example/douyin/hot", "max_items": 20,
    }], request_get=fake_get)

    assert sources[0]["status"] == "ok"
    assert sources[0]["mode"] == "api"
    assert signals[0]["title"] == "国产机器人走进工厂"
    assert signals[0]["scope"] == "douyin_public_heat_snapshot"
    assert "claim" not in signals[0] and "evidence" not in signals[0]
    assert seen["headers"]["Authorization"] == "Bearer douyin-test-secret"
    assert "douyin-test-secret" not in seen["url"]


def test_douyin_api_failure_falls_back_to_offline_snapshot(tmp_path, monkeypatch):
    monkeypatch.setenv("DOUYIN_API_KEY", "douyin-test-secret")
    snapshot = tmp_path / "hotboard.json"
    snapshot.write_text(json.dumps({"items": [{"title": "AI手机降价", "heat_value": 77}]}), encoding="utf-8")

    def failed_get(*_args, **_kwargs):
        raise RuntimeError("temporary 503 Bearer douyin-test-secret")

    signals, sources = daily_automation.collect_douyin_sources([{
        "id": "douyin-hotboard", "name": "抖音热榜", "kind": "douyin_board",
        "api_url": "https://data.example/douyin/hot", "snapshot_path": str(snapshot),
    }], request_get=failed_get)

    assert sources[0]["status"] == "ok"
    assert sources[0]["mode"] == "snapshot"
    assert "fallback_reason" in sources[0]
    assert "douyin-test-secret" not in sources[0]["fallback_reason"]
    assert signals[0]["title"] == "AI手机降价"


def test_fallback_script_approval_is_persisted_without_starting_media(tmp_path, monkeypatch):
    target = "2026-08-24"
    run_dir = tmp_path / target
    run_dir.mkdir(parents=True)
    run = {
        "target_date": target, "status": "awaiting_human", "current_stage": "voice",
        "approval_policy": {"fallback_script_approved": False}, "project_id": None,
    }
    (run_dir / "daily_run.json").write_text(json.dumps(run), encoding="utf-8")
    (run_dir / "daily_script.json").write_text(json.dumps({
        "validation": {"passed": True, "valid": True, "errors": []},
        "topic_selection": {"selected_stories": [{"heat_level": "H2"}]},
        "editorial_review": {
            "quality_band": "fallback_publishable", "total": 80, "passed": True,
            "scores": {"hook": 14, "dialogue": 15, "information_density": 19, "public_value": 17, "interaction": 15},
            "structured_issues": [],
        },
    }), encoding="utf-8")
    monkeypatch.setattr(daily_automation, "RUNS_ROOT", tmp_path)

    approved = daily_automation.approve_fallback_script(target)

    assert approved["status"] == "queued"
    assert approved["current_stage"] == "voice"
    assert approved["approval_policy"]["fallback_script_approved"] is True
    assert approved["approval_policy"]["fallback_script_approved_by"] == "human"


def test_manual_story_replacement_keeps_lead_and_resets_only_text_stage(tmp_path, monkeypatch):
    target = "2026-08-26"
    run_dir = tmp_path / target
    run_dir.mkdir(parents=True)
    stages = {name: daily_automation._new_stage(name) for name in daily_automation.STAGE_ORDER}
    stages["research"]["status"] = "succeeded"
    stages["script"].update({"status": "awaiting_human", "finished_at": "2026-08-27T03:02:00+08:00"})
    (run_dir / "daily_run.json").write_text(json.dumps({
        "target_date": target, "status": "awaiting_human", "current_stage": "script",
        "approval_policy": {"editorial_recovery_reason": "最佳稿待人工处理"},
        "project_id": None, "stages": stages,
    }), encoding="utf-8")
    combo_a = {
        "combination_id": "EC-A", "rank": 1, "event_ids": ["ROBOT", "GAME-A", "GAME-B"],
        "blocking_issues": [], "episode_score": 70, "duration_profile": "full_episode",
        "selected_stories": [{"event_id": "ROBOT"}, {"event_id": "GAME-A"}, {"event_id": "GAME-B"}],
    }
    combo_b = {
        "combination_id": "EC-B", "rank": 2, "event_ids": ["ROBOT", "CHIP"],
        "blocking_issues": [], "episode_score": 74, "duration_profile": "compact_high_value",
        "selected_stories": [{"event_id": "ROBOT"}, {"event_id": "CHIP"}],
    }
    (run_dir / "topic_selection_v2.json").write_text(json.dumps({
        "selected_stories": combo_a["selected_stories"],
        "episode_combinations": [combo_a, combo_b],
        "selection_summary": {"selected_combination_id": "EC-A"},
    }), encoding="utf-8")
    (run_dir / "daily_text_attempts.json").write_text(json.dumps({
        "best_candidate": {"event_ids": combo_a["event_ids"]}, "attempts": [{"attempt_id": "TA-01"}],
    }), encoding="utf-8")
    monkeypatch.setattr(daily_automation, "RUNS_ROOT", tmp_path)

    updated = daily_automation.request_text_story_replacement(target)
    selection = json.loads((run_dir / "topic_selection_v2.json").read_text(encoding="utf-8"))

    assert updated["status"] == "queued"
    assert updated["current_stage"] == "script"
    assert updated["stages"]["research"]["status"] == "succeeded"
    assert updated["stages"]["script"]["status"] == "pending"
    assert selection["selected_stories"] == combo_b["selected_stories"]
    assert selection["manual_preferences"]["locked_event_ids"] == ["ROBOT"]
    assert list(run_dir.glob("daily_text_attempts.before-manual-*.json"))


def test_douyin_signal_status_reports_latest_snapshot_mode(tmp_path, monkeypatch):
    target = "2026-08-24"
    run_dir = tmp_path / target
    run_dir.mkdir(parents=True)
    (run_dir / "news_research.json").write_text(json.dumps({
        "douyin_sources": [{"id": "douyin-hotboard", "status": "ok", "mode": "snapshot", "count": 12}],
    }), encoding="utf-8")
    monkeypatch.setattr(daily_automation, "RUNS_ROOT", tmp_path)
    monkeypatch.delenv("DOUYIN_API_KEY", raising=False)

    status = daily_automation.douyin_signal_status({"douyin_sources": []}, {"target_date": target})

    assert status["state"] == "ok"
    assert status["latest_modes"] == ["snapshot"]
    assert status["latest_ok_count"] == 12


def _valid_script() -> dict:
    stories = [
        {"story_id": "S01", "tier": "S", "headline": "声音克隆诈骗风险", "event_identity": "熟人声音", "plain_summary": "短录音就可能被用来复刻熟人的声音。", "why_viewers_care": "普通人可能因为熟悉声音放松警惕并转账。", "foreign_term_glosses": [], "line_count": 4, "source_ids": ["N-ONE"]},
        {"story_id": "S02", "tier": "A", "headline": "国产机器人高难动作", "event_identity": "国产机器人", "plain_summary": "国产机器人现场展示了更强的运动控制能力。", "why_viewers_care": "大众可以直接看到机器人技术离生活又近了一步。", "foreign_term_glosses": [], "line_count": 4, "source_ids": ["N-TWO"]},
        {"story_id": "S03", "tier": "A", "headline": "国产芯片能力突破", "event_identity": "国产芯片", "plain_summary": "国产芯片在真实设备中展示了新的运行能力。", "why_viewers_care": "大众可以进一步判断国产硬件的实际进展。", "foreign_term_glosses": [], "line_count": 4, "source_ids": ["N-THREE"]},
        {"story_id": "S04", "tier": "B", "headline": "企业数据规则更新", "event_identity": "平台", "plain_summary": "企业使用模型时可以进一步减少数据留存。", "why_viewers_care": "员工上传工作资料时能够多一层隐私保障。", "foreign_term_glosses": [], "line_count": 2, "source_ids": ["N-FOUR"]},
    ]
    raw_lines = [
        ("yaya", "hook", "hook", "scene_consequence", "S01", ["N-ONE"], "科技圈快讯来了！短短十秒公开录音，骗子就可能克隆熟人声音催你转账。"),
        ("mengmeng", "story", "translate", "mechanism_or_pattern", "S01", ["N-ONE"], "声音像亲人，背后却可能是骗子在说话。"),
        ("yaya", "story", "impact", "user_impact", "S01", ["N-ONE"], "熟悉声线会让人放松警惕，工作资料和辛苦攒下的钱都可能受损。"),
        ("mengmeng", "story", "reference_tip", "action_tip", "S01", ["N-ONE"], "催款先挂断，再用常用号码核实本人。"),
        ("yaya", "story", "fact", "event", "S02", ["N-TWO"], "国产机器人现场完成高难动作，接下来还要进入公开比赛接受真实检验。"),
        ("mengmeng", "story", "quip", "audience_reaction_or_test", "S02", ["N-TWO"], "以前台上摆造型，这回赛场见真章。"),
        ("yaya", "story", "fact", "distinct_detail", "S02", ["N-TWO"], "现场连续动作同时考验身体协调和运动控制，不再只是看静态外观。"),
        ("mengmeng", "story", "summary", "why_it_matters", "S02", ["N-TWO"], "动作稳不稳，观众一眼就能看明白。"),
        ("yaya", "story", "fact", "event", "S03", ["N-THREE"], "国产芯片已经装进真实设备运行，公开展示新的实际应用和处理能力。"),
        ("mengmeng", "story", "translate", "audience_reaction_or_test", "S03", ["N-THREE"], "参数再漂亮，装进设备才算真本事。"),
        ("yaya", "story", "context", "distinct_detail", "S03", ["N-THREE"], "这次展示重点落在真实工作负载和持续运行，而非只强调峰值数据。"),
        ("mengmeng", "story", "summary", "why_it_matters", "S03", ["N-THREE"], "能否稳定使用，才是大家关心的答案。"),
        ("yaya", "story", "fact", "event", "S04", ["N-FOUR"], "平台更新企业数据规则，合规客户可以进一步减少请求内容留存并降低内部风险。"),
        ("mengmeng", "story", "reference_tip", "user_value", "S04", ["N-FOUR"], "传工作资料前，多一层隐私判断更稳。"),
        ("yaya", "outro", "closing", "", "", [], "克隆熟人声音要核实，工作资料要慎传，你遇过哪种风险？欢迎在评论区讨论。"),
    ]
    rows = [
        {
            "turn_id": f"T{index:03d}", "speaker_id": speaker,
            "speaker_name": "雅雅" if speaker == "yaya" else "檬檬", "kind": kind,
            "function": function, "information_dimension": dimension,
            "information_key": f"{story_id}-{function}-{index}" if story_id else "",
            "reply_to": f"T{index - 1:03d}" if speaker == "mengmeng" else "",
            "story_id": story_id, "source_ids": source_ids, "text": text,
        }
        for index, (speaker, kind, function, dimension, story_id, source_ids, text) in enumerate(raw_lines, 1)
    ]
    return {
        "editorial": {
            "daily_theme": "AI能力加速落地，也带来新的安全问题",
            "audience_promise": "看懂技术进步和普通人需要防范的风险",
            "outro_story_ids": ["S01", "S04"],
            "outro_mentions": [
                {"story_id": "S01", "phrase": "克隆熟人声音"},
                {"story_id": "S04", "phrase": "工作资料"},
            ],
        },
        "stories": stories,
        "estimated_duration_seconds": 90,
        "script_scores": {
            "hook_strength": 22, "dual_host_structure": 18, "plain_density": 21,
            "pacing_duration": 13, "outro_interaction": 12, "total": 86,
        },
        "lines": rows,
    }


def test_previous_day_is_calendar_day_in_shanghai():
    now = datetime(2026, 8, 21, 3, 0, tzinfo=timezone(timedelta(hours=8)))
    assert previous_target_date(now) == date(2026, 8, 20)
    start, end = target_window(date(2026, 8, 20))
    assert start.isoformat() == "2026-08-20T00:00:00+08:00"
    assert end.isoformat() == "2026-08-21T00:00:00+08:00"


def test_valid_topic_and_tier_elastic_script_contract():
    topic_result = validate_topic_selection(_valid_selection())
    result = validate_daily_script(_valid_script(), _valid_selection())
    assert topic_result["tiers"] == ["S", "A", "A", "B"]
    assert result["line_count"] == 15
    assert 0.65 <= result["yaya_ratio"] <= 0.75
    assert result["combined_score"] >= 78


def test_generation_prompt_declares_current_story_and_scoring_contract():
    prompt = daily_automation._script_generation_prompt()
    selection_prompt = daily_automation._topic_selection_prompt()
    assert "S级4句1212、第一条A级4句1212" in prompt
    assert "前三秒钩子25" in prompt
    assert "总口播365—425字" in prompt
    assert "禁止第一句为了报型号挤掉钩子" in prompt
    assert "同一 story_id 下禁止重复" in prompt
    assert "两人都可以说事实" in prompt
    assert "直接用中文构思并输出中文台词" in prompt
    assert "事实层严谨，表达层可以有力度" in prompt
    assert "降价超过两成可概括为“大幅降低”" in prompt
    assert "机器人未来做饭、收拾、陪护" in prompt
    assert "没错、确实、精准" in prompt
    assert "第一句优先以“每日科技快讯来了”" in prompt
    assert "event_identity" in prompt
    assert "这也太狠了吧、太提气了" in prompt
    assert "禁止“好家伙、撒胡椒面" in prompt
    assert "每条新闻口播最多保留一个英文专名" in prompt
    assert "选题总分不足70不得写脚本" in selection_prompt
    assert "90秒版本严格采用四条 1S+2A+1B" in selection_prompt
    assert "常规产品发布一律判B级" in selection_prompt
    assert "不得根据自己想要的组合反向抬级" in selection_prompt
    assert "不得把双来源要求扩大到所有普通新闻" in selection_prompt
    assert "4×新闻数+2" not in prompt


def test_script_rejects_editorially_empty_mengmeng_role():
    script = _valid_script()
    for line in script["lines"]:
        if line.get("speaker_id") == "mengmeng":
            line["function"] = "translate"
    with pytest.raises(DailyScriptValidationError, match="至少三种有效功能"):
        validate_daily_script(script, _valid_selection())


def test_topic_selection_rejects_missing_s_tier():
    selection = _valid_selection()
    selection["selected_stories"][0]["tier"] = "A"
    with pytest.raises(DailyTopicValidationError, match="第一条必须是 S 级|缺少 S 级"):
        validate_topic_selection(selection)


def test_topic_selection_rejects_score_below_admission_line():
    selection = _valid_selection()
    selection["topic_scores"].update({"public_relevance": 14, "total": 79})
    with pytest.raises(DailyTopicValidationError, match="大众感知评分未达到 15 分"):
        validate_topic_selection(selection)


def test_topic_selection_rejects_crypto_platform_as_core_topic():
    selection = _valid_selection()
    research = {"candidates": [
        {"candidate_id": "N-ONE", "title": "声音克隆风险"},
        {"candidate_id": "N-TWO", "title": "Binance lets AI agents trade cryptocurrency"},
        {"candidate_id": "N-THREE", "title": "企业数据规则"},
    ]}
    with pytest.raises(DailyTopicValidationError, match="加密交易平台功能"):
        validate_topic_selection(selection, research)


def test_topic_selection_rejects_one_category_for_whole_episode():
    selection = _valid_selection()
    for story in selection["selected_stories"]:
        story["category"] = "产业事件"
    with pytest.raises(DailyTopicValidationError, match="至少覆盖两个内容类别"):
        validate_topic_selection(selection)


def test_topic_selection_rejects_unverified_extreme_percentage():
    selection = _valid_selection()
    selection["selected_stories"][1]["headline"] = "机器人公司上市首日大涨629%"
    research = {"candidates": [
        {"candidate_id": "N-ONE", "title": "声音克隆风险", "authority": "media", "source_name": "媒体甲"},
        {"candidate_id": "N-TWO", "title": "机器人公司上市首日大涨629%", "authority": "media", "source_name": "媒体乙"},
        {"candidate_id": "N-THREE", "title": "企业数据规则", "authority": "media", "source_name": "媒体丙"},
    ]}
    with pytest.raises(DailyTopicValidationError, match="异常数字"):
        validate_topic_selection(selection, research)


def test_topic_selection_rejects_two_terms_to_explain():
    selection = _valid_selection()
    selection["selected_stories"][3]["terms_to_explain"] = ["GeForce NOW", "Firefox"]
    with pytest.raises(DailyTopicValidationError, match="两个以上陌生名词"):
        validate_topic_selection(selection)


def test_topic_selection_accepts_short_public_value_label():
    selection = _valid_selection()
    selection["selected_stories"][3]["public_value"] = "省钱"
    assert validate_topic_selection(selection)["valid"] is True


def test_topic_selection_rejects_abstract_b_tier_public_value():
    selection = _valid_selection()
    selection["selected_stories"][3]["public_value"] = "更直观看懂行业进展"
    with pytest.raises(DailyTopicValidationError, match="B级用户价值必须落到"):
        validate_topic_selection(selection)


def test_script_rejects_unsupported_mengmeng_function():
    script = _valid_script()
    target = next(line for line in script["lines"] if line.get("speaker_id") == "mengmeng")
    target.update({"function": "question", "text": "这里没有真正提出具体问题。"})
    with pytest.raises(DailyScriptValidationError, match="檬檬台词功能无效"):
        validate_daily_script(script, _valid_selection())


def test_script_rejects_jargon_first_hook():
    script = _valid_script()
    script["lines"][0]["text"] = "加密指令可能套走你交给模型的工作文件。"
    with pytest.raises(DailyScriptValidationError, match="不能用行业术语起句"):
        validate_daily_script(script, _valid_selection())


def test_script_rejects_hook_without_program_identity():
    script = _valid_script()
    script["lines"][0]["text"] = "短短十秒公开录音，骗子就可能克隆熟人声音，在电话里催你转账。"
    with pytest.raises(DailyScriptValidationError, match="栏目身份"):
        validate_daily_script(script, _valid_selection())


def test_script_rejects_story_without_concrete_event_identity_in_first_line():
    script = _valid_script()
    script["stories"][1]["event_identity"] = "宇树机器人"
    with pytest.raises(DailyScriptValidationError, match="第一句必须明确说出具体事件身份"):
        validate_daily_script(script, _valid_selection())


def test_s_tier_allows_hook_first_and_concrete_identity_in_second_line():
    script = _valid_script()
    script["stories"][0]["event_identity"] = "声音克隆骗局"
    script["lines"][1]["text"] = "没错，这种声音克隆骗局会冒充亲人，在电话里催你转账。"
    script["lines"][1]["function"] = "fact"
    assert validate_daily_script(script, _valid_selection())["valid"] is True


def test_script_accepts_company_and_model_as_one_grouped_identity():
    script = _valid_script()
    script["stories"][2]["event_identity"] = "OpenAI的GPT-5.6 Sol"
    script["stories"][2]["foreign_term_glosses"] = [
        {"term": "OpenAI的GPT-5.6 Sol", "chinese_label": "旗舰模型"}
    ]
    script["lines"][8]["text"] = "OpenAI的GPT-5.6 Sol旗舰模型公开展示了新的实际应用能力。"
    assert validate_daily_script(script, _valid_selection())["valid"] is True


def test_script_rejects_overly_roguish_filler():
    script = _valid_script()
    script["lines"][1]["text"] = "好家伙，声音像亲人，背后却可能是骗子在说话。"
    with pytest.raises(DailyScriptValidationError, match="语气过痞"):
        validate_daily_script(script, _valid_selection())


def test_script_rejects_generic_risk_hook_without_scene_and_consequence():
    script = _valid_script()
    script["lines"][0]["text"] = "你的微信账号最近可能存在新的安全风险，大家使用时需要注意。"
    with pytest.raises(DailyScriptValidationError, match="具体使用场景和明确后果"):
        validate_daily_script(script, _valid_selection())


def test_script_rejects_outro_mentions_not_present_in_spoken_text():
    script = _valid_script()
    script["editorial"]["outro_mentions"][0]["phrase"] = "声音识别风险"
    with pytest.raises(DailyScriptValidationError, match="真实带出至少两条"):
        validate_daily_script(script, _valid_selection())


def test_script_rejects_fake_binary_outro():
    script = _valid_script()
    script["lines"][-1]["text"] = "换渠道二次确认，隐私和进步该先选谁？欢迎在评论区讨论。"
    with pytest.raises(DailyScriptValidationError, match="硬凑成二选一"):
        validate_daily_script(script, _valid_selection())


def test_script_rejects_repeated_information_key_within_story():
    script = _valid_script()
    script["lines"][2]["information_key"] = script["lines"][1]["information_key"]
    with pytest.raises(DailyScriptValidationError, match="information_key 重复"):
        validate_daily_script(script, _valid_selection())


def test_script_rejects_wrong_information_dimension():
    script = _valid_script()
    script["lines"][2]["information_dimension"] = "mechanism_or_pattern"
    with pytest.raises(DailyScriptValidationError, match="信息维度应为"):
        validate_daily_script(script, _valid_selection())


def test_script_rejects_two_english_product_names_in_one_story():
    script = _valid_script()
    script["lines"][12]["text"] = "GeForce支持Firefox浏览器进入云游戏服务并提供新的使用入口。"
    with pytest.raises(DailyScriptValidationError, match="两个以上英文专名"):
        validate_daily_script(script, _valid_selection())


def test_script_requires_chinese_gloss_for_first_english_name():
    script = _valid_script()
    script["lines"][12]["text"] = "Firefox浏览器现在可以直接进入云游戏服务和相关功能页面。"
    with pytest.raises(DailyScriptValidationError, match="首次出现时必须附中文"):
        validate_daily_script(script, _valid_selection())


def test_script_rejects_mengmeng_hiding_core_fact_under_quip_function():
    script = _valid_script()
    script["lines"][5]["text"] = "这是首次公布的机器人正式比赛数据内容。"
    with pytest.raises(DailyScriptValidationError, match="播报核心事实时必须标注"):
        validate_daily_script(script, _valid_selection())


def test_script_rejects_broadcast_style_mengmeng_filler():
    script = _valid_script()
    target = next(line for line in script["lines"] if line.get("speaker_id") == "mengmeng")
    target["text"] = "这件事情确实值得关注，后续表现让我们拭目以待。"
    with pytest.raises(DailyScriptValidationError, match="广播腔"):
        validate_daily_script(script, _valid_selection())


def test_script_allows_explicitly_marked_future_robot_outlook():
    script = _valid_script()
    script["lines"][13]["text"] = "传工作资料前要稳；未来真能做饭、收拾和陪护也不错。"
    assert validate_daily_script(script, _valid_selection())["valid"] is True


def test_script_rejects_unmarked_robot_capability_as_current_fact():
    script = _valid_script()
    script["lines"][13]["text"] = "传工作资料前要稳；机器人已经能做饭、收拾和陪护。"
    with pytest.raises(DailyScriptValidationError, match="只能作为明确展望"):
        validate_daily_script(script, _valid_selection())


def test_script_rejects_mengmeng_without_adjacent_reply_binding():
    script = _valid_script()
    target = next(line for line in script["lines"] if line.get("speaker_id") == "mengmeng")
    target["reply_to"] = ""
    with pytest.raises(DailyScriptValidationError, match="reply_to"):
        validate_daily_script(script, _valid_selection())


def test_script_rejects_three_story_outro_mashup():
    script = _valid_script()
    script["editorial"]["outro_story_ids"].append("S03")
    script["editorial"]["outro_mentions"].append({"story_id": "S03", "phrase": "国产芯片"})
    with pytest.raises(DailyScriptValidationError, match="只选择两条"):
        validate_daily_script(script, _valid_selection())


def test_topic_selection_rejects_attempted_but_failed_source_evidence():
    selection = _valid_selection()
    research = {"candidates": [
        {"candidate_id": "N-ONE", "title": "声音克隆风险", "evidence_status": "failed"},
        {"candidate_id": "N-TWO", "title": "机器人动作", "evidence_status": "ok", "evidence_excerpt": "正文证据"},
        {"candidate_id": "N-THREE", "title": "国产芯片", "evidence_status": "ok", "evidence_excerpt": "正文证据"},
        {"candidate_id": "N-FOUR", "title": "规则更新"},
    ]}
    with pytest.raises(DailyTopicValidationError, match="缺少原站正文证据"):
        validate_topic_selection(selection, research)


def test_topic_selection_keeps_one_turn_after_final_evidence_refresh(monkeypatch):
    raw_selection = {
        "selected_stories": [{"candidate_ids": ["N-ONE"]}],
        "topic_scores": {},
    }
    calls = []

    monkeypatch.setattr(daily_automation, "_prefetch_selection_evidence", lambda _candidates: set())
    def return_selection(*_args, **_kwargs):
        calls.append(1)
        return raw_selection, "test-model"

    monkeypatch.setattr(daily_automation, "_chat_json_with_transient_retry", return_selection)
    monkeypatch.setattr(daily_automation, "validate_topic_selection", lambda *_args: {"valid": True})

    def add_evidence(candidate):
        candidate["evidence_status"] = "ok"
        candidate["evidence_excerpt"] = "已补充的正文证据"

    monkeypatch.setattr(daily_automation, "_enrich_candidate_evidence", add_evidence)
    result = daily_automation.select_daily_topics(
        {"target_date": "2026-08-22", "candidates": [{"candidate_id": "N-ONE"}, {"candidate_id": "N-TWO"}, {"candidate_id": "N-THREE"}]},
        max_revision_rounds=0,
    )

    assert len(calls) == 2
    assert result["model"] == "test-model"
    assert result["validation"] == {"valid": True}


def test_topic_selection_payload_is_bounded_and_source_diverse():
    candidates = [
        {
            "candidate_id": f"N-{index:02d}",
            "source_id": "same-source" if index < 12 else f"source-{index}",
            "title": f"机器人进厂 {index}",
            "summary": "机器人高难动作",
            "evidence_status": "ok" if index % 2 == 0 else "failed",
            "china_short_video_hint": {"likely_china_relevance": "high"},
        }
        for index in range(40)
    ]

    result = daily_automation._topic_selection_payload_candidates(candidates)

    assert len(result) <= daily_automation.MAX_TOPIC_SELECTION_CANDIDATES
    assert sum(item["source_id"] == "same-source" for item in result) <= 5
    assert any(item["source_id"] != "same-source" for item in result)


def test_google_news_decoder_uses_signed_batch_response():
    class Response:
        def __init__(self, text):
            self.text = text

        def raise_for_status(self):
            return None

    class Session:
        def get(self, *_args, **_kwargs):
            return Response('<div data-n-a-id="article-id" data-n-a-ts="123" data-n-a-sg="signature"></div>')

        def post(self, *_args, **_kwargs):
            payload = json.dumps([["wrb.fr", "Fbv4je", json.dumps(["garturlres", "https://example.com/news", 1])]])
            return Response(")]}'\n\n" + payload)

    resolved = daily_automation._decode_google_news_url(
        "https://news.google.com/rss/articles/article-id", session=Session()
    )
    assert resolved == "https://example.com/news"


def test_script_generation_retries_transient_upstream_gateway_failure(monkeypatch):
    calls = []

    def fake_chat(_system, _payload, **_kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise daily_automation.TextAIError("HTTP 502: Upstream request failed")
        return _valid_script(), "gpt-5.6-terra"

    monkeypatch.setattr(daily_automation, "_chat_json", fake_chat)
    result = daily_automation.generate_daily_script({
        "target_date": "2026-08-20",
        "candidates": [{"candidate_id": candidate_id} for candidate_id in ("N-ONE", "N-TWO", "N-THREE", "N-FOUR")],
    }, selection=_valid_selection())
    assert len(calls) == 2
    assert result["model"] == "gpt-5.6-terra"


def test_script_rejects_story_without_plain_language_audience_context():
    script = _valid_script()
    script["stories"][0].pop("why_viewers_care")
    with pytest.raises(DailyScriptValidationError):
        validate_daily_script(script, _valid_selection())


def test_default_daily_avatar_is_circle_for_4_by_5_source():
    config = daily_automation.default_config()
    assert config["avatar"]["source_aspect"] == "4:5"
    assert config["avatar"]["shape"] == "circle"


def test_unattended_schedule_blocks_without_verified_lite_billing(monkeypatch):
    monkeypatch.setattr(daily_automation, "list_runs", lambda _limit=20: [])
    guard = daily_automation.daily_billing_safety()
    assert guard["auto_schedule_eligible"] is False
    assert guard["state"] == "lite_verification_required"


def test_audited_lite_pilot_unlocks_unattended_schedule(monkeypatch):
    monkeypatch.setattr(daily_automation, "list_runs", lambda _limit=20: [{
        "target_date": "2026-08-20",
        "stages": {"avatar": {"output": {"roles": {
            "yaya": {
                "task_id": "safe-pilot", "requested_instance": "auto_lite",
                "billing": {"observed_instance": "lite"},
            },
        }}}},
    }])
    guard = daily_automation.daily_billing_safety()
    assert guard["auto_schedule_eligible"] is True
    assert guard["state"] == "verified_lite"


def test_historical_standard_observation_blocks_new_paid_submission(monkeypatch):
    monkeypatch.setattr(daily_automation, "list_runs", lambda _limit=20: [{
        "target_date": "2026-08-19",
        "stages": {"avatar": {"output": {"roles": {
            "yaya": {
                "task_id": "legacy-standard", "requested_instance": "auto_lite",
                "billing": {"observed_instance": "standard_24gb"},
            },
        }}}},
    }])
    guard = daily_automation.daily_billing_safety()
    assert guard["auto_schedule_eligible"] is False
    assert guard["state"] == "provider_did_not_honor_lite"


def test_newer_standard_billing_cannot_be_overridden_by_older_lite(monkeypatch):
    monkeypatch.setattr(daily_automation, "list_runs", lambda _limit=20: [
        {
            "target_date": "2026-08-23",
            "stages": {"avatar": {"output": {"roles": {"yaya": {
                "task_id": "new-standard", "requested_instance": "auto_lite",
                "billing": {"observed_instance": "standard_24gb"},
            }}}}},
        },
        {
            "target_date": "2026-08-19",
            "stages": {"avatar": {"output": {"roles": {"yaya": {
                "task_id": "old-lite", "requested_instance": "auto_lite",
                "billing": {"observed_instance": "lite"},
            }}}}},
        },
    ])

    guard = daily_automation.daily_billing_safety()

    assert guard["auto_schedule_eligible"] is False
    assert guard["latest_evidence"]["task_id"] == "new-standard"


def test_script_rejects_wrong_speaker_order():
    script = _valid_script()
    script["lines"][1]["speaker_id"] = "yaya"
    with pytest.raises(DailyScriptValidationError, match="说话人顺序错误"):
        validate_daily_script(script, _valid_selection())


@pytest.mark.parametrize("message", [
    "CUDA out of memory while allocating tensor",
    "显存不足，模型加载失败",
    "failed to allocate GPU VRAM",
])
def test_only_explicit_memory_errors_allow_standard_upgrade(message):
    result = classify_runninghub_failure(message)
    assert result["is_oom"] is True
    assert result["may_upgrade_to_standard"] is True


@pytest.mark.parametrize("message", [
    "task queue timeout",
    "HTTP 429 rate limit",
    "network connection reset",
    "任务排队时间较长",
])
def test_transient_errors_never_allow_standard_upgrade(message):
    result = classify_runninghub_failure(message)
    assert result["is_oom"] is False
    assert result["may_upgrade_to_standard"] is False


def test_budget_hard_stops_above_five_yuan(monkeypatch):
    run = {
        "target_date": "2026-08-19",
        "budget": {"limit": 5.0, "reserved": 4.8, "spent": 0.0, "entries": []},
        "project_id": None,
        "updated_at": None,
    }
    monkeypatch.setattr("backlot.daily_automation._save_run", lambda value: value)
    ledger = BudgetLedger(run)
    with pytest.raises(DailyAutomationError, match="超过每日 5 元预算"):
        ledger.reserve(0.3, purpose="檬檬数字人")


def test_machine_run_lock_prevents_duplicate_process(tmp_path, monkeypatch):
    lock_path = tmp_path / ".daily-production.lock"
    monkeypatch.setattr(daily_automation, "RUN_LOCK_PATH", lock_path)
    assert daily_automation.try_acquire_run_lock("2026-08-19", trigger="test") is True
    try:
        assert lock_path.is_file()
        assert daily_automation.try_acquire_run_lock("2026-08-19", trigger="duplicate") is False
    finally:
        daily_automation.release_run_lock()
    assert not lock_path.exists()


def test_machine_run_lock_reclaims_dead_owner(tmp_path, monkeypatch):
    lock_path = tmp_path / ".daily-production.lock"
    lock_path.write_text('{"pid": 987654, "token": "stale"}', encoding="utf-8")
    monkeypatch.setattr(daily_automation, "RUN_LOCK_PATH", lock_path)
    monkeypatch.setattr(daily_automation, "_process_is_alive", lambda _pid: False)
    assert daily_automation.try_acquire_run_lock("2026-08-19", trigger="recovery") is True
    daily_automation.release_run_lock()
    assert not lock_path.exists()


def test_windows_process_probe_never_uses_os_kill(monkeypatch):
    monkeypatch.setattr(daily_automation.os, "name", "nt")
    monkeypatch.setattr(daily_automation, "_windows_process_is_alive", lambda pid: pid == 123)
    monkeypatch.setattr(
        daily_automation.os,
        "kill",
        lambda *_args: pytest.fail("Windows PID探测不得调用os.kill"),
    )

    assert daily_automation._process_is_alive(123) is True
    assert daily_automation._process_is_alive(456) is False


def test_daily_decision_log_records_lite_budget_and_human_gate(tmp_path):
    value = daily_automation._ensure_daily_decision_log(tmp_path)
    decisions = {item["decision_id"]: item for item in value["decisions"]}
    assert decisions["daily-provider-policy-v1"]["selected"] == "enterprise-lite"
    assert decisions["daily-provider-policy-v1"]["user_approved"] is True
    assert decisions["daily-budget-policy-v1"]["selected"] == "cny-5-cap"
    assert decisions["daily-human-publish-gate-v1"]["selected"] == "review-candidate"


def test_daily_decision_log_replaces_lite_with_run_authorized_standard(tmp_path):
    daily_automation._ensure_daily_decision_log(tmp_path)
    value = daily_automation._ensure_daily_decision_log(
        tmp_path,
        {
            "target_date": "2026-08-24",
            "provider_policy": {"authorized_instance": "default"},
        },
    )
    decisions = [item for item in value["decisions"] if item["decision_id"] == "daily-provider-policy-v1"]

    assert len(decisions) == 1
    assert decisions[0]["selected"] == "standard-24gb"
    assert "2026-08-24" in decisions[0]["reason"]


def test_feed_parser_repairs_bare_ampersand_in_real_world_rss():
    root = daily_automation._parse_feed_root(
        b'<?xml version="1.0"?><rss><channel><item><title>A & B</title></item></channel></rss>'
    )
    assert next(item for item in root.iter() if item.tag == "title").text == "A & B"


def test_news_source_templates_expand_and_duplicate_candidate_ids_are_dropped():
    requested_urls = []

    class Response:
        content = b'''<rss><channel>
          <item><title>Same headline</title><link>https://example.com/a</link><pubDate>Thu, 20 Aug 2026 08:00:00 GMT</pubDate></item>
          <item><title>Same headline</title><link>https://example.com/b</link><pubDate>Thu, 20 Aug 2026 09:00:00 GMT</pubDate></item>
        </channel></rss>'''

        @staticmethod
        def raise_for_status():
            return None

    def fake_get(url, **_kwargs):
        requested_urls.append(url)
        return Response()

    research = daily_automation.collect_news_candidates(
        "2026-08-20",
        sources=[{
            "id": "templated",
            "name": "模板源",
            "url": "https://example.com/feed?after={previous_date}&before={next_date}",
        }],
        request_get=fake_get,
    )
    assert "after=2026-08-19" in requested_urls[0]
    assert "before=2026-08-21" in requested_urls[0]
    assert len(research["candidates"]) == 1


def test_scheduler_xml_has_missed_run_and_duplicate_guards():
    spec = {
        "schedule_time": "03:00",
        "working_directory": r"C:\OpenMontage",
        "command": [r"C:\OpenMontage\.venv\Scripts\python.exe", "wrapper.py", "run"],
        "execution_time_limit_hours": 12,
    }
    xml = daily_automation._scheduler_task_xml(spec, username=r"TEST\\operator").decode("utf-16")
    assert "<StartWhenAvailable>true</StartWhenAvailable>" in xml
    assert "<WakeToRun>true</WakeToRun>" in xml
    assert "<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>" in xml
    assert "<WorkingDirectory>C:\\OpenMontage</WorkingDirectory>" in xml
    assert "<ExecutionTimeLimit>PT12H</ExecutionTimeLimit>" in xml


def test_reliable_review_candidate_can_continue_to_review_only_media():
    script = {
        "validation": {"passed": True, "valid": True, "errors": []},
        "topic_selection": {"selected_stories": [{"heat_level": "H3"}]},
        "editorial_review": {
            "passed": False,
            "total": 82,
            "scores": {"hook": 15, "dialogue": 16, "information_density": 20, "public_value": 17, "interaction": 14},
            "structured_issues": [{"code": "dialogue_flow", "hard_fact_boundary": False}],
            "hard_gate_failures": [],
        },
    }

    release = daily_automation.evaluate_media_release(script)

    assert release["decision"] == "fallback_review_candidate"
    assert release["publish_requires_human"] is True


def test_reliable_review_candidate_does_not_hide_fact_or_structure_redlines():
    script = {
        "validation": {"passed": True, "valid": True, "errors": []},
        "topic_selection": {"selected_stories": [{"heat_level": "H3"}]},
        "editorial_review": {
            "passed": False,
            "total": 84,
            "scores": {"hook": 16, "dialogue": 16, "information_density": 21, "public_value": 17, "interaction": 14},
            "structured_issues": [{"code": "factual_boundary", "hard_fact_boundary": True}],
        },
    }

    assert daily_automation.evaluate_media_release(script)["decision"] == "blocked"


def test_scheduler_effective_state_surfaces_last_failed_run_without_disabling_schedule():
    value = daily_automation.scheduler_effective_state(
        {"enabled": True},
        {"installed": True, "runtime_enabled": True, "command_matches": True, "last_result": 1},
    )

    assert value["effective_enabled"] is True
    assert value["healthy"] is False
    assert value["last_run_succeeded"] is False
    assert "退出码 1" in value["message"]


def test_today_cannot_be_frozen_as_a_complete_news_day(monkeypatch):
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 8, 20, 12, 0, tzinfo=tz)

    monkeypatch.setattr(daily_automation, "datetime", FixedDateTime)
    with pytest.raises(DailyAutomationError, match="目标自然日结束后"):
        daily_automation.run_research_and_script("2026-08-20")


def test_scheduler_runtime_parser_detects_disabled_chinese_task():
    spec = {
        "task_name": "OpenMontage-Daily-Tech-Brief",
        "enabled": True,
        "schedule_time": "03:00",
        "command": [r"D:\OpenMontage\.venv\Scripts\python.exe", r"D:\OpenMontage\scripts\run_daily_automation.py"],
    }
    raw = r"""
下次运行时间: N/A
模式: 已禁用
上次运行时间: 2026/8/21 3:00:00
上次结果: 1
要运行的任务: D:\OpenMontage\.venv\Scripts\python.exe D:\OpenMontage\scripts\run_daily_automation.py run
计划任务状态: 已禁用
"""
    value = daily_automation._parse_scheduler_runtime_output(raw, installed=True, spec=spec)
    assert value["runtime_enabled"] is False
    assert value["last_result"] == 1
    assert value["command_matches"] is True


def test_effective_scheduler_state_never_calls_disabled_task_enabled():
    value = daily_automation.scheduler_effective_state(
        {"enabled": True},
        {"installed": True, "runtime_enabled": False, "command_matches": True},
    )
    assert value["effective_enabled"] is False
    assert value["conflict"] is True
    assert "不会自动运行" in value["message"]


def test_config_is_not_persisted_when_scheduler_update_fails(tmp_path, monkeypatch):
    config_path = tmp_path / "daily.json"
    monkeypatch.setattr(daily_automation, "CONFIG_PATH", config_path)

    def fail_scheduler(_config):
        raise DailyAutomationError("计划任务创建失败")

    monkeypatch.setattr(daily_automation, "sync_windows_scheduler", fail_scheduler)
    with pytest.raises(DailyAutomationError, match="计划任务创建失败"):
        daily_automation.apply_config_with_scheduler({"enabled": True})
    assert not config_path.exists()
