from __future__ import annotations

import json

import pytest

from backlot import material_interaction_recommend as recommend


def _index() -> dict:
    utterances = [
        {"id": "U1", "start": 0.0, "end": 6.0, "text": "今天天气不错，你从哪里来"},
        {"id": "U2", "start": 10.0, "end": 13.0, "text": "哇 真的假的 太厉害了"},
        {"id": "U3", "start": 20.0, "end": 26.0, "text": "Hello, nice to meet you. Where are you from?"},
        {"id": "U4", "start": 30.0, "end": 35.0, "text": "我们一起唱首歌吧 然后合影一张"},
        {"id": "U5", "start": 40.0, "end": 70.0, "text": "我们聊聊你的旅行"},
        {"id": "U6", "start": 72.0, "end": 100.0, "text": "那你接下来去哪里"},
    ]
    events = [
        _event("E1", "G-boring", 0.0, 8.0, ["U1"], engagement=0.5),
        _event("E2", "G-emotion", 10.0, 14.0, ["U2"], engagement=0.9),
        _event("E3", "G-foreign", 20.0, 27.0, ["U3"], engagement=0.7),
        _event("E4", "G-perform", 30.0, 36.0, ["U4"], engagement=0.8),
        _event("E5", "G-same", 40.0, 70.0, ["U5"], engagement=0.6,
               participants="黑色短袖、深色长裤的男子，站在长椅左侧"),
        _event("E6", "G-same", 72.0, 100.0, ["U6"], engagement=0.6,
               participants="深色长裤、黑色短袖的男子，站在长椅左侧"),
    ]
    return {"signature": "index-sig", "source": {"fingerprint": "src"}, "duration": 120.0,
            "audio": {"status": "available", "utterances": utterances}, "events": events}


def _event(event_id, group_id, start, end, utterance_ids, *, engagement, participants="一名男子"):
    return {
        "event_id": event_id, "group_id": group_id, "start": start, "end": end,
        "participants": participants, "summary": f"{event_id} 的互动",
        "completeness": "complete", "score": 0.5 + engagement / 10,
        "quality": {"engagement": engagement, "visual_clarity": 0.8, "story_value": 0.7},
        "highlights": [], "utterance_ids": utterance_ids, "evidence_frame_ids": [],
    }


def _row(payload, event_id):
    return next(row for row in payload["events"] if row["event_id"] == event_id)


# --- helpers -----------------------------------------------------------------

def test_latin_ratio_separates_chinese_from_foreign_speech():
    assert recommend.latin_ratio("你好今天天气不错") == 0.0
    assert recommend.latin_ratio("Hello, nice to meet you") == 1.0
    assert 0.1 < recommend.latin_ratio("你好 Hello 世界") < 0.9


def test_subject_descriptions_of_the_same_person_score_much_higher():
    same = recommend.participants_similarity(
        "黑色短袖、深色长裤的男子，站在长椅左侧", "深色长裤、黑色短袖的男子，站在长椅左侧")
    different = recommend.participants_similarity(
        "黑色短袖、深色长裤的男子", "戴粉色帽子、白色连衣裙的女子")
    assert same > 0.4
    assert different < 0.1
    assert recommend.participants_similarity("", "") == 0.0


def test_ascii_terms_respect_word_boundaries():
    # "hi" used to fire inside "this"; the guard is what keeps ordinary Chinese
    # small talk from being scored as foreign speech.
    index = _index()
    for row in index["events"]:
        row["utterance_ids"] = ["U1"]
    payload = recommend.build_recommendations(index)
    assert _row(payload, "E1")["factors"]["foreign_speech"] == 0.0
    assert _row(payload, "E1")["evidence"]["foreign_terms"] == []
    # "sing" still finds "singing".
    assert recommend._hits("we are singing", ("sing",)) == ["sing"]


# --- the scored criteria -----------------------------------------------------

def test_each_criterion_marks_its_own_event():
    payload = recommend.build_recommendations(_index())
    assert _row(payload, "E2")["factors"]["high_emotion"] == max(
        row["factors"]["high_emotion"] for row in payload["events"])
    assert _row(payload, "E3")["factors"]["foreign_speech"] == max(
        row["factors"]["foreign_speech"] for row in payload["events"])
    assert _row(payload, "E4")["factors"]["performance"] == max(
        row["factors"]["performance"] for row in payload["events"])
    assert _row(payload, "E1")["factors"]["foreign_speech"] == 0.0
    assert _row(payload, "E1")["factors"]["performance"] == pytest.approx(0.4 * 0.5, abs=1e-3)


def test_duration_is_its_own_criterion_and_rewards_the_longer_subject():
    payload = recommend.build_recommendations(_index())
    # E5/E6 share one subject for 58 s; E1's subject was engaged for 8 s.
    assert _row(payload, "E5")["factors"]["duration"] > _row(payload, "E1")["factors"]["duration"]
    assert _row(payload, "E5")["factors"]["duration"] == pytest.approx(
        _row(payload, "E6")["factors"]["duration"], abs=1e-6)


def test_default_weights_follow_the_requested_order():
    weights = recommend.WEIGHTS
    assert weights["duration"] > weights["foreign_speech"] > weights["high_emotion"] > \
        weights["performance"]
    payload = recommend.build_recommendations(_index())
    assert set(payload["weights"]) == set(recommend.SCORED_FACTORS)
    assert pytest.approx(sum(payload["weights"].values())) == 1.0


def test_same_subject_is_a_requirement_and_never_enters_the_composite_score():
    payload = recommend.build_recommendations(_index())
    for row in payload["events"]:
        requirement = row["requirement"]
        assert requirement["key"] == "same_subject"
        assert isinstance(requirement["ok"], bool)
        assert 0.0 <= requirement["score"] <= 1.0
        # The composite is exactly the four scored factors: zeroing the
        # requirement's score must not move it by even a rounding step.
        expected = sum(payload["weights"][key] * row["factors"][key] for key in recommend.SCORED_FACTORS)
        assert row["recommendation_score"] == pytest.approx(expected, abs=1e-4)
        # The requirement is reported beside the score, never as one of its items.
        assert all(not reason.startswith("【选材要求】") for reason in row["reasons"])
        assert row["requirement_reason"].startswith("【选材要求】")


def test_one_subject_engaged_twice_is_aggregated_and_clears_the_requirement():
    payload = recommend.build_recommendations(_index())
    group = next(row for row in payload["groups"] if row["group_id"] == "G-same")
    assert group["event_count"] == 2
    assert group["total_seconds"] == pytest.approx(58.0)
    assert _row(payload, "E5")["evidence"]["group_total_seconds"] == pytest.approx(58.0)
    assert _row(payload, "E5")["requirement"]["ok"] is True
    assert _row(payload, "E6")["requirement"]["score"] == _row(payload, "E5")["requirement"]["score"]


def test_an_incomplete_encounter_scores_lower_on_the_requirement():
    index = _index()
    next(row for row in index["events"] if row["event_id"] == "E1")["completeness"] = "window_edge"
    payload = recommend.build_recommendations(index)
    complete = _row(recommend.build_recommendations(_index()), "E1")["requirement"]["score"]
    assert _row(payload, "E1")["requirement"]["score"] < complete
    assert "window_edge" in _row(payload, "E1")["requirement_reason"]


# --- ordering -----------------------------------------------------------------

def test_duration_desc_is_the_shipped_default_and_ranks_by_length():
    payload = recommend.build_recommendations(_index())
    assert recommend.ORDER_MODE_DEFAULT == "duration_desc"
    assert payload["order_mode"] == "duration_desc"
    assert payload["order_mode_label"] == "优先时间长"
    durations = [row["duration_seconds"] for row in payload["events"]]
    assert durations == sorted(durations, reverse=True)
    assert payload["events"][0]["event_id"] == "E5"


def test_every_order_mode_puts_its_own_criterion_first():
    index = _index()
    shortest = recommend.build_recommendations(index, sort_mode="duration_asc")
    assert shortest["events"][0]["duration_seconds"] == min(
        row["duration_seconds"] for row in shortest["events"])
    emotion = recommend.build_recommendations(index, sort_mode="emotion_desc")
    assert emotion["events"][0]["event_id"] == "E2"
    composite = recommend.build_recommendations(index, sort_mode="score_desc")
    scores = [row["recommendation_score"] for row in composite["events"]]
    assert scores == sorted(scores, reverse=True)
    assert set(composite["order_modes"]) == set(recommend.ORDER_MODES)


def test_ranks_are_contiguous_and_deterministic():
    first = recommend.build_recommendations(_index())
    second = recommend.build_recommendations(_index())
    assert [row["rank"] for row in first["events"]] == list(range(1, len(first["events"]) + 1))
    assert first["signature"] == second["signature"]
    assert [row["event_id"] for row in first["events"]] == [row["event_id"] for row in second["events"]]


def test_an_unknown_order_mode_is_rejected():
    with pytest.raises(recommend.InteractionRecommendError, match="排序方式无效"):
        recommend.build_recommendations(_index(), sort_mode="cheapest")


# --- weights ------------------------------------------------------------------

def test_weights_are_configurable_and_change_the_ranking():
    index = _index()
    default = recommend.build_recommendations(index, sort_mode="score_desc")
    foreign_first = recommend.build_recommendations(
        index, weights={"duration": 0, "high_emotion": 0, "foreign_speech": 1, "performance": 0},
        sort_mode="score_desc")
    emotion_first = recommend.build_recommendations(
        index, weights={"duration": 0, "high_emotion": 1, "foreign_speech": 0, "performance": 0},
        sort_mode="score_desc")
    assert foreign_first["events"][0]["event_id"] == "E3"
    assert emotion_first["events"][0]["event_id"] == "E2"
    assert foreign_first["weights"]["foreign_speech"] == 1.0
    # The shipped blend is not a disguised single-criterion ranking.
    assert ([row["event_id"] for row in default["events"]]
            != [row["event_id"] for row in foreign_first["events"]])


def test_the_retired_same_subject_weight_is_accepted_and_ignored():
    index = _index()
    legacy = recommend.build_recommendations(
        index, weights={"same_subject": 9, "duration": 0, "high_emotion": 1,
                        "foreign_speech": 0, "performance": 0})
    plain = recommend.build_recommendations(
        index, weights={"duration": 0, "high_emotion": 1, "foreign_speech": 0, "performance": 0})
    assert legacy["weights"] == plain["weights"] == {"duration": 0.0, "foreign_speech": 0.0,
                                                     "high_emotion": 1.0, "performance": 0.0}
    assert legacy["signature"] == plain["signature"]


def test_an_unspecified_weight_falls_back_to_the_shipped_default():
    partial = recommend.build_recommendations(_index(), weights={"high_emotion": 1})
    weights = partial["weights"]
    # The three unspecified keys keep their shipped relative order and their
    # non-zero share; only the requested key is raised.
    assert weights["duration"] > weights["performance"] > 0
    assert weights["high_emotion"] > weights["duration"]


def test_notes_report_which_criteria_could_not_be_used():
    index = _index()
    for row in index["events"]:
        row["utterance_ids"] = ["U1"]
    payload = recommend.build_recommendations(index)
    assert any("外语" in note for note in payload["notes"])
    assert any("唱歌跳舞" in note for note in payload["notes"])


@pytest.mark.parametrize("weights", [
    {"duration": 0, "high_emotion": 0, "foreign_speech": 0, "performance": 0},
    {"duration": -1, "high_emotion": 0, "foreign_speech": 0, "performance": 0},
    {"duration": "fast", "high_emotion": 0, "foreign_speech": 0, "performance": 0},
])
def test_impossible_weights_are_rejected(weights):
    with pytest.raises(recommend.InteractionRecommendError):
        recommend.build_recommendations(_index(), weights=weights)


def test_an_index_without_events_is_rejected():
    with pytest.raises(recommend.InteractionRecommendError, match="没有可排序"):
        recommend.build_recommendations({"events": []})


# --- safety and persistence --------------------------------------------------

def test_the_source_index_object_and_file_are_never_modified(tmp_path):
    index = _index()
    before = json.dumps(index, sort_keys=True, ensure_ascii=False)
    path = tmp_path / "material-interaction-index.json"
    path.write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")
    digest = __import__("hashlib").sha256(path.read_bytes()).hexdigest()
    recommend.build_recommendations(index)
    assert json.dumps(index, sort_keys=True, ensure_ascii=False) == before
    assert __import__("hashlib").sha256(path.read_bytes()).hexdigest() == digest
    assert list(tmp_path.iterdir()) == [path]


def test_recommendations_round_trip_and_survive_a_corrupt_file(tmp_path):
    path = tmp_path / "recommendations.json"
    payload = recommend.build_recommendations(_index())
    recommend.write_recommendations(path, payload)
    assert recommend.read_recommendations(path)["signature"] == payload["signature"]
    assert recommend.read_recommendations(tmp_path / "missing.json") is None
    path.write_text("{not json", encoding="utf-8")
    assert recommend.read_recommendations(path) is None


def test_the_result_is_deterministic():
    first = recommend.build_recommendations(_index())
    second = recommend.build_recommendations(_index())
    assert first["signature"] == second["signature"]
    assert [row["event_id"] for row in first["events"]] == [row["event_id"] for row in second["events"]]
