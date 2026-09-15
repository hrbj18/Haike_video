from copy import deepcopy
import json
from pathlib import Path
import threading
import time

import pytest
from PIL import Image
from fastapi.testclient import TestClient

from backlot import material_interactions as m
from backlot import workbench as wb
from backlot import server, state as state_module


def event_payload(group="W001-G01", start="F1", end="F3"):
    return {"group_id": group, "participants": "穿红衣和粉衣的互动群组", "summary": "同组人与机器狗互动",
            "start_frame_id": start, "end_frame_id": end, "start_state": "observed", "end_state": "observed",
            "confidence": .8, "quality": {"engagement": .8, "visual_clarity": .7, "story_value": .9},
            "evidence_frame_ids": [start, end], "utterance_ids": [], "highlights": [], "unknowns": [],
            "recommend_reason": "有完整共同参与过程"}


def normalize(row=None):
    return m.normalize_events({"events": [row or event_payload()]},
        {"F1": {"pts": 6}, "F2": {"pts": 12}, "F3": {"pts": 18}}, {},
        {"id": "W001", "start": 0, "end": 30, "has_next": False}, set())[0]


@pytest.mark.parametrize("duration", [1, 30, 303.369, 600, 1800, 3600])
def test_windows_cover_whole_source_with_bounded_overlap(duration):
    windows = m.window_plan(duration)
    assert windows[0]["start"] == 0 and windows[-1]["end"] == duration
    for previous, current in zip(windows, windows[1:]):
        assert previous["end"] - current["start"] == 20
    for window in windows:
        assert window["end"] - window["start"] <= 180
        assert max((b-a for a, b in zip(window["times"], window["times"][1:])), default=0) <= 6
        assert len(window["times"]) <= 31


@pytest.mark.parametrize("value", [0, -1, 21601, float("nan"), float("inf"), True])
def test_invalid_duration_rejected(value):
    with pytest.raises(m.InteractionError):
        m.window_plan(value)


@pytest.mark.parametrize("duration", [3601, 5333.035, 21600])
def test_long_livestream_duration_is_accepted_by_the_window_plan(duration):
    """60 分钟以上的直播回放必须能进入互动/切割链路。"""
    windows = m.window_plan(duration)
    assert windows[0]["start"] == 0 and windows[-1]["end"] == duration
    for window in windows:
        assert window["end"] - window["start"] <= 180
    # 88.9 分钟的素材按 180/20 秒递进应落在 34 个窗口，即 35 次视觉预算。
    if duration == 5333.035:
        assert len(windows) == 34


def test_transcript_end_overshoot_is_clamped_instead_of_failing_the_whole_run():
    """实测腾讯末段 EndMs 会越过音频末尾约 33 ms，不能让整条长音轨分析失败。"""
    rows = [
        {"start": 0.88, "end": 61.36, "text": "刚刚开播"},
        {"start": 5273.66, "end": 5333.1, "text": "拜拜大家"},
    ]
    normalized = m.normalize_transcript(rows, 5333.035)

    assert [row["id"] for row in normalized] == ["U00001", "U00002"]
    assert normalized[-1]["end"] == 5333.035


def test_transcript_segment_entirely_inside_the_padding_is_dropped():
    rows = [
        {"start": 0.5, "end": 2.0, "text": "正常一句"},
        {"start": 5333.05, "end": 5333.09, "text": "只落在填充区"},
    ]
    normalized = m.normalize_transcript(rows, 5333.035)

    assert [row["text"] for row in normalized] == ["正常一句"]


@pytest.mark.parametrize("row", [
    {"start": 0.0, "end": 5335.0, "text": "越界太多"},      # 超过容忍带
    {"start": 10.0, "end": 5.0, "text": "时间倒序"},
    {"start": -1.0, "end": 5.0, "text": "负时间"},
])
def test_grossly_invalid_transcript_timestamps_are_still_rejected(row):
    with pytest.raises(m.InteractionError):
        m.normalize_transcript([row], 5333.035)


@pytest.mark.parametrize("key,value", [("start_frame_id", "fake"), ("end_frame_id", "F1"),
    ("evidence_frame_ids", ["not-supplied"]), ("utterance_ids", ["fake"]), ("confidence", float("nan")),
    ("group_id", "G-fabricated"), ("start_state", "maybe")])
def test_model_cannot_invent_evidence_or_invalid_contract(key, value):
    row = event_payload()
    row[key] = value
    with pytest.raises(m.InteractionError):
        normalize(row)


def test_out_of_range_utterance_is_dropped_with_a_trace_instead_of_failing_the_run():
    """腾讯 ASR 单句常跨 30–60 秒，模型为事件选对白时引到稍远的一句是常态。

    实测 88.9 分钟直播回放第 15 个窗口（2240–2420 秒）就是这样：模型把一个
    2335.6 秒才开始的句子挂到 2300–2324 秒的事件上。这类引用只能剔除并留痕，
    不能让整条 34 窗口的分析作废（重跑一次要重新付费）。
    """
    frames = {"F1": {"pts": 6}, "F2": {"pts": 12}, "F3": {"pts": 18}}
    utterances = {
        "U00001": {"id": "U00001", "start": 4.0, "end": 20.0, "text": "在范围内"},
        "U00002": {"id": "U00002", "start": 100.0, "end": 130.0, "text": "远在窗口之外"},
    }
    row = event_payload()
    row["utterance_ids"] = ["U00001", "U00002"]
    row["unknowns"] = ["模型不确定这两位是否同组"]

    result = m.normalize_events({"events": [row]}, frames, utterances,
        {"id": "W001", "start": 0, "end": 30, "has_next": False}, set())[0]

    assert result["utterance_ids"] == ["U00001"]
    assert len(result["unknowns"]) == 2
    # 留痕放在最前面，因为事件只保留前 12 条不确定项。
    assert "U00002" in result["unknowns"][0]
    assert result["unknowns"][1] == "模型不确定这两位是否同组"


def test_event_whose_every_utterance_is_out_of_range_keeps_its_visual_evidence():
    """对白证据全被剔除时事件本身仍要保留——画面证据才是切割依据。"""
    frames = {"F1": {"pts": 6}, "F2": {"pts": 12}, "F3": {"pts": 18}}
    utterances = {"U00009": {"id": "U00009", "start": 500.0, "end": 560.0, "text": "别处的对白"}}
    row = event_payload()
    row["utterance_ids"] = ["U00009"]

    result = m.normalize_events({"events": [row]}, frames, utterances,
        {"id": "W001", "start": 0, "end": 30, "has_next": False}, set())[0]

    assert result["utterance_ids"] == []
    assert result["evidence_frame_ids"] == ["F1", "F3"]
    assert "U00009" in result["unknowns"][0]


def test_highlight_and_irrelevant_segment_outside_the_event_are_dropped_not_fatal():
    """模型把「事件刚结束那一刻」标成亮点、或把无关段伸到事件之外，都不该作废整条分析。

    实测 88.9 分钟直播回放 W015：模型声明事件为 2300–2324 秒，却把 2336 秒
    （机器狗转入唱歌环节）的画面标成该事件的亮点。
    """
    frames = {"F1": {"pts": 6}, "F2": {"pts": 12}, "F3": {"pts": 18}, "F9": {"pts": 90}}
    row = event_payload()
    row["highlights"] = [
        {"label": "事件内的亮点", "frame_id": "F2"},
        {"label": "事件外的亮点", "frame_id": "F9"},
    ]
    row["irrelevant_segments"] = [{
        "start_frame_id": "F1", "end_frame_id": "F9", "confidence": .6,
        "evidence_frame_ids": ["F1", "F9"], "reason": "伸到事件之外的无关段",
        "no_related_speech": True, "no_key_action": True, "context_preserved": True,
    }]

    result = m.normalize_events({"events": [row]}, frames, {},
        {"id": "W001", "start": 0, "end": 30, "has_next": False}, set())[0]

    assert [x["frame_id"] for x in result["highlights"]] == ["F2"]
    assert result["irrelevant_segments"] == []
    traces = " ".join(result["unknowns"])
    assert "F9" in traces and "亮点画面" in traces and "无关段" in traces


def test_unknown_reference_rejects_only_invalid_sibling_event():
    valid = event_payload()
    invalid = event_payload(group="W001-G02")
    invalid["start_frame_id"] = "F-NOT-SUPPLIED"
    raw, rejected = m.reject_events_with_unknown_references(
        {"events": [valid, invalid]},
        {"F1": {"pts": 6}, "F2": {"pts": 12}, "F3": {"pts": 18}},
        {},
    )
    assert raw["events"] == [valid]
    assert rejected == [{
        "event_number": 2,
        "group_id": "W001-G02",
        "unknown_frame_ids": ["F-NOT-SUPPLIED"],
        "unknown_utterance_ids": [],
        "reason": "model_referenced_unsupplied_evidence",
    }]
    assert m.normalize_events(raw, {"F1": {"pts": 6}, "F2": {"pts": 12}, "F3": {"pts": 18}}, {},
                              {"id": "W001", "start": 0, "end": 30, "has_next": False}, set())


def test_all_events_with_unknown_references_still_fail():
    first = event_payload(start="F-NOT-SUPPLIED")
    second = event_payload(group="W001-G02", end="F-NOT-SUPPLIED")
    with pytest.raises(m.InteractionError, match="所有事件"):
        m.reject_events_with_unknown_references(
            {"events": [first, second]},
            {"F1": {"pts": 6}, "F2": {"pts": 12}, "F3": {"pts": 18}},
            {},
        )


def test_recording_edges_are_missing_not_claimed_complete():
    result = m.normalize_events({"events": [event_payload()]}, {"F1": {"pts": 0}, "F3": {"pts": 29.9}}, {},
        {"id": "W001", "start": 0, "end": 30, "has_next": False}, set())[0]
    assert result["completeness"] == "both_missing"


def test_irrelevant_segment_requires_bounded_multi_frame_evidence():
    row = event_payload()
    row["irrelevant_segments"] = [{
        "start_frame_id": "F1", "end_frame_id": "F2", "confidence": .96,
        "evidence_frame_ids": ["F1", "F2"], "no_related_speech": True,
        "no_key_action": True, "context_preserved": True, "reason": "短暂拍到无关路人",
    }]
    result = normalize(row)
    candidate = result["irrelevant_segments"][0]
    assert (candidate["start"], candidate["end"]) == (6, 12)
    assert candidate["no_related_speech"] is True
    assert candidate["segment_id"] == "W001-E01-I01"


@pytest.mark.parametrize("patch", [
    {"evidence_frame_ids": ["F1"]},                    # 证据帧不足两张
    {"start_frame_id": "F2", "end_frame_id": "F1"},    # 时间倒序
])
def test_invalid_irrelevant_segment_is_rejected(patch):
    """结构性错误仍然硬拦；「伸到事件之外」属于模型越界，改由容错剔除处理。"""
    row = event_payload(start="F1", end="F2")
    segment = {
        "start_frame_id": "F1", "end_frame_id": "F2", "confidence": .96,
        "evidence_frame_ids": ["F1", "F2"], "no_related_speech": True,
        "no_key_action": True, "context_preserved": True, "reason": "无关镜头",
    }
    segment.update(patch)
    row["irrelevant_segments"] = [segment]
    with pytest.raises(m.InteractionError):
        normalize(row)


def test_irrelevant_segment_evidence_outside_its_own_span_is_still_rejected():
    """段本身在事件内，但证据帧落在段外——这是真正说不通的声明，必须拦下。"""
    row = event_payload(start="F1", end="F3")           # 事件 6–18 秒
    row["irrelevant_segments"] = [{
        "start_frame_id": "F1", "end_frame_id": "F2",   # 无关段 6–12 秒
        "confidence": .96, "evidence_frame_ids": ["F1", "F3"],
        "no_related_speech": True, "no_key_action": True,
        "context_preserved": True, "reason": "证据帧落在段外",
    }]
    with pytest.raises(m.InteractionError, match="证据不属于候选范围"):
        normalize(row)


def test_evidence_frame_just_outside_the_edge_is_tolerated():
    """实测 2348.043 对边界 2348.044 只差 1 毫秒，却把整条 34 窗口的分析否掉了。

    抽帧是 6 秒一格，实际 PTS 与请求时刻总有毫秒级偏差，容差必须吸收这点抖动。
    """
    frames = {"F1": {"pts": 12}, "F2": {"pts": 18}, "F3": {"pts": 5.95}}
    row = event_payload(start="F1", end="F2")
    row["evidence_frame_ids"] = ["F1", "F2", "F3"]

    result = m.normalize_events({"events": [row]}, frames, {},
        {"id": "W001", "start": 0, "end": 30, "has_next": False}, set())[0]

    assert result["evidence_frame_ids"] == ["F1", "F2", "F3"]


def test_evidence_far_outside_the_event_is_dropped_and_cannot_be_the_only_evidence():
    frames = {"F1": {"pts": 12}, "F2": {"pts": 18}, "F9": {"pts": 90}}
    window = {"id": "W001", "start": 0, "end": 30, "has_next": False}

    row = event_payload(start="F1", end="F2")
    row["evidence_frame_ids"] = ["F1", "F2", "F9"]
    result = m.normalize_events({"events": [row]}, frames, {}, window, set())[0]
    assert result["evidence_frame_ids"] == ["F1", "F2"]
    assert "F9" in " ".join(result["unknowns"])

    # 一个事件不能把全部画面依据都放在自己的时间范围之外。
    row["evidence_frame_ids"] = ["F9"]
    with pytest.raises(m.InteractionError, match="证据不属于事件时间范围"):
        m.normalize_events({"events": [row]}, frames, {}, window, set())


def test_merge_requires_same_group_and_temporal_overlap_not_just_topic():
    previous = normalize()
    incoming = deepcopy(previous)
    incoming.update({"windows": ["W002"], "start": 15, "end": 28, "event_id": "W002-E01"})
    assert len(m.merge_events([previous], [incoming])) == 1
    incoming["group_id"] = "W002-G01"
    assert len(m.merge_events([previous], [incoming])) == 2
    incoming.update({"group_id": previous["group_id"], "start": 25, "end": 35})
    assert len(m.merge_events([previous], [incoming])) == 2


def test_boundary_cannot_shift_outside_allowed_frames_and_is_atomic():
    event = normalize()
    old = deepcopy(event)
    target = {event["event_id"]: {"allowed_start": ["F1"], "allowed_end": ["F3"]}}
    raw = {"boundaries": [{"event_id": event["event_id"], "start_frame_id": "F1", "end_frame_id": "FAKE",
                          "start_state": "observed", "end_state": "observed", "reason": "结束"}]}
    with pytest.raises(m.InteractionError):
        m.apply_boundaries([event], raw, target, {"F1": {"pts": 6}, "F3": {"pts": 18}}, 30)
    assert event == old


def test_boundary_budget_is_distributed_across_events_before_second_edges():
    events = []
    for index in range(4):
        event = normalize()
        event.update({"event_id": f"E{index}", "start": index * 20 + 1, "end": index * 20 + 15,
                      "start_state": "observed", "end_state": "uncertain", "completeness": "uncertain"})
        events.append(event)
    plan = m.boundary_edge_plan(events, 4)
    assert {event["event_id"] for event, _ in plan} == {"E0", "E1", "E2", "E3"}
    assert all(side == "end" for _, side in plan)


def test_journal_completed_is_reused_and_unknown_acceptance_never_resubmitted(tmp_path):
    calls = []
    path = tmp_path / "journal.json"
    assert m._remote(path, lambda: calls.append(1) or {"ok": True}, kind="test", request_signature="x")["ok"]
    m._remote(path, lambda: calls.append(2), kind="test", request_signature="x")
    assert calls == [1]
    m._write_json(path, {"status": "submitting", "signature": "x"})
    with pytest.raises(m.InteractionError) as error:
        m._remote(path, lambda: calls.append(3), kind="test", request_signature="x")
    assert error.value.status == "ambiguous" and calls == [1]


def test_journal_timeout_blocks_reentry_and_redacts_provider_error(tmp_path):
    path = tmp_path / "journal.json"
    def fail():
        raise TimeoutError("secret-value")
    with pytest.raises(m.InteractionError):
        m._remote(path, fail, kind="test", request_signature="x")
    assert "secret-value" not in path.read_text()
    with pytest.raises(m.InteractionError) as error:
        m._remote(path, lambda: pytest.fail("must not retry"), kind="test", request_signature="x")
    assert error.value.status == "ambiguous"


@pytest.fixture
def fake_media(tmp_path, monkeypatch):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"private-source")
    monkeypatch.setattr(m, "probe_media", lambda *_: {"duration_seconds": 30, "streams": [{"codec_type": "audio"}]})
    def frame(_source, _ffmpeg, timestamp, target):
        target.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (160, 90), "tan").save(target)
        return timestamp, m.digest(timestamp)
    monkeypatch.setattr(m, "_extract_frame", frame)
    return source


def test_pipeline_caches_asr_windows_detail_and_never_modifies_source(fake_media, tmp_path):
    calls = []
    def asr(_):
        calls.append("asr")
        return "你好", [{"start": 1, "end": 2, "text": "你好"}], {}
    def model(kind, payload, images):
        calls.append(kind)
        if kind == "boundary":
            assert len(images) <= 12
            return {"boundaries": [{**{k: t[k] for k in ("event_id", "start_frame_id", "end_frame_id", "start_state", "end_state")},
                                      "reason": "保留原边界"} for t in payload["targets"]]}
        cells = payload["cells"]
        return {"events": [event_payload(start=cells[1]["frame_id"], end=cells[-2]["frame_id"])]}
    arguments = dict(ffmpeg="ffmpeg", ffprobe="ffprobe", identity={"model": "test"}, asr_identity="test", transcript_provider=asr, analyze=model)
    first = m.build_interaction_index(fake_media, tmp_path / "out", **arguments)
    second = m.build_interaction_index(fake_media, tmp_path / "out", **arguments)
    assert first["events"] and first["usage"]["detail_frames"] <= 6
    assert second["cache_hit"] and calls == ["asr", "events", "boundary"]
    assert fake_media.read_bytes() == b"private-source"
    # Changing visual model must not regenerate the same paid audio.
    arguments["identity"] = {"model": "test2"}
    m.build_interaction_index(fake_media, tmp_path / "out", **arguments)
    assert calls.count("asr") == 1 and calls.count("events") == 2


def test_asr_failure_is_not_converted_to_silent_video(fake_media, tmp_path):
    def asr(_):
        raise RuntimeError("service down")
    with pytest.raises(m.InteractionError):
        m.build_interaction_index(fake_media, tmp_path / "out", ffmpeg="f", ffprobe="p", identity={}, asr_identity="t",
                                  transcript_provider=asr, analyze=lambda *_: pytest.fail("cannot mask ASR failure"))


def test_one_bad_window_does_not_discard_the_paid_analysis_of_the_others(fake_media, tmp_path, monkeypatch):
    """34 个窗口的视觉识别是已经付过费的；一个窗口回复格式出格只能跳过该窗口。

    实测 88.9 分钟直播回放就在第 15 个窗口上摔了三次，每次都要从头重跑。
    """
    monkeypatch.setattr(m, "probe_media", lambda *_: {"duration_seconds": 400, "streams": [{"codec_type": "audio"}]})

    def asr(_):
        return "转写", [{"start": 1, "end": 2, "text": "转写"}], {}

    seen = []
    def model(kind, payload, images):
        if kind == "boundary":
            return {"boundaries": [
                {**{k: t[k] for k in ("event_id", "start_frame_id", "end_frame_id", "start_state", "end_state")},
                 "reason": "保留原边界"} for t in payload["targets"]]}
        seen.append(payload["window_id"])
        cells = payload["cells"]
        if payload["window_id"] == "W002":
            # 群组编号既不属于本窗口、也不在提供的上下文里 —— 结构性错误。
            return {"events": [event_payload(group="W999-G01", start=cells[1]["frame_id"], end=cells[-2]["frame_id"])]}
        return {"events": [event_payload(group=f"{payload['window_id']}-G01",
                                          start=cells[1]["frame_id"], end=cells[-2]["frame_id"])]}

    index = m.build_interaction_index(fake_media, tmp_path / "out", ffmpeg="ffmpeg", ffprobe="ffprobe",
                                      identity={"model": "test"}, asr_identity="test",
                                      transcript_provider=asr, analyze=model)

    assert seen == ["W001", "W002", "W003"]
    assert index["status"] == "completed" and len(index["windows"]) == 3
    warnings = index["rejected_model_events"]
    assert [row["window_id"] for row in warnings] == ["W002"]
    assert warnings[0]["reason"] == "window_failed_local_validation"
    assert "群组编号" in warnings[0]["detail"]
    assert {event["windows"][0] for event in index["events"]} == {"W001", "W003"}


def test_workbench_surfaces_a_skipped_window_as_a_readable_warning():
    """整窗跳过必须在审核端可见，而且不能套用「引用了未提供的证据」这句错文案。"""
    warnings = wb._interaction_analysis_warnings([
        {"window_id": "W002", "event_number": None, "reason": "window_failed_local_validation",
         "detail": "互动群组必须使用当前窗口或已有群组编号"},
        {"window_id": "W007", "event_number": 2, "reason": "model_referenced_unsupplied_evidence"},
    ])

    assert warnings[0]["code"] == "rejected_model_event" and warnings[0]["window_id"] == "W002"
    assert warnings[0]["reason"].startswith("该窗口的模型回复未通过本地校验")
    assert "互动群组必须使用当前窗口或已有群组编号" in warnings[0]["reason"]
    assert warnings[1]["reason"] == "模型引用了未提供的证据，该事件已单独拒绝并保留其他合法结果"
    assert wb._interaction_analysis_warnings(None) == []


def _interaction_arguments(**overrides):
    def asr(_):
        return "腾讯云转写", [{"start": 1, "end": 2, "text": "腾讯云转写"}], {}

    def model(kind, payload, images):
        if kind == "boundary":
            return {"boundaries": [
                {**{k: t[k] for k in ("event_id", "start_frame_id", "end_frame_id", "start_state", "end_state")},
                 "reason": "保留原边界"} for t in payload["targets"]]}
        cells = payload["cells"]
        return {"events": [event_payload(start=cells[1]["frame_id"], end=cells[-2]["frame_id"])]}

    arguments = dict(ffmpeg="ffmpeg", ffprobe="ffprobe", identity={"model": "test"},
                     asr_identity="tencent-asr", transcript_provider=asr, analyze=model,
                     transcript_provider_id="tencent")
    arguments.update(overrides)
    return arguments


def test_interaction_index_records_the_selected_engine(fake_media, tmp_path):
    index = m.build_interaction_index(fake_media, tmp_path / "out", **_interaction_arguments())
    assert index["audio"]["policy"] == "tencent_transcript"
    assert index["audio"]["provider"] == "tencent-asr"
    assert index["audio"]["status"] == "available"
    assert [row["text"] for row in index["audio"]["utterances"]] == ["腾讯云转写"]


def test_switching_engine_produces_a_separate_cache_entry(fake_media, tmp_path):
    tencent = m.build_interaction_index(fake_media, tmp_path / "out", **_interaction_arguments())
    # A different engine must not reuse the tencent run directory or its evidence.
    doubao = m.build_interaction_index(
        fake_media, tmp_path / "out",
        **_interaction_arguments(asr_identity="doubao-asr", transcript_provider_id="doubao"),
    )
    assert tencent["audio"]["policy"] == "tencent_transcript"
    assert doubao["audio"]["policy"] == "doubao_transcript"
    assert doubao["cache_hit"] is False
    assert doubao["signature"] != tencent["signature"]


def test_unsupported_engine_is_refused_before_any_paid_call(fake_media, tmp_path):
    with pytest.raises(m.InteractionError, match="不支持的语音识别服务"):
        m.build_interaction_index(
            fake_media, tmp_path / "bad",
            **_interaction_arguments(transcript_provider_id="gemini"),
        )


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    p = root / "interaction-test"
    (p / "assets").mkdir(parents=True)
    m._write_json(p / "project.json", {"project_id": p.name, "title": "互动测试", "pipeline_type": "cinematic"})
    (p / "assets" / "source.mp4").write_bytes(b"source")
    state = wb.bootstrap_workbench(p)
    asset = wb._append_asset(p, state, {"name": "原片", "type": "video", "source_type": "human_provided", "path": "assets/source.mp4", "duration_seconds": 30})
    wb._save(p, state)
    monkeypatch.setattr(server, "PROJECTS_DIR", root)
    monkeypatch.setattr(state_module, "PROJECTS_DIR", root)
    monkeypatch.setattr(server, "_summary_cache", {})
    monkeypatch.setattr(wb, "_ffmpeg_available", lambda: "ffmpeg")
    monkeypatch.setattr(wb, "_ffprobe_available", lambda _: "ffprobe")
    monkeypatch.setattr("backlot.media_index.probe_media", lambda *_: {"duration_seconds": 30})
    monkeypatch.setattr(m, "runtime_identity", lambda: {"model": "test"})
    # These cases pin the doubao contract; keep the auto-picked default stable so
    # the suite does not depend on which cloud ASR happens to be configured locally.
    monkeypatch.setattr(wb, "default_transcript_provider", lambda: "doubao")
    monkeypatch.setattr(wb, "assert_doubao_asr_media_ready", lambda: None)
    monkeypatch.setattr(wb, "doubao_asr_runtime_identity", lambda: "asr-test")
    return p, asset["id"]


@pytest.mark.parametrize("flags", [{}, {"remote_vision_confirmed": True}, {"remote_asr_confirmed": True}])
def test_workbench_requires_single_combined_confirmation(project, flags):
    p, asset_id = project
    with pytest.raises(wb.WorkbenchError, match="一次确认"):
        wb.start_asset_media_index(p, asset_id, {"stage": "interaction", **flags})


def test_shared_worker_keeps_scenes_and_exposes_safe_result(project, monkeypatch):
    p, asset_id = project
    before = wb.read_workbench(p)
    def build(source, output, **kwargs):
        index = {"source": {"fingerprint": m.media_content_fingerprint(source)}, "version": m.VERSION,
                 "signature": "s", "status": "completed", "duration": 30, "profile": "efficient", "identity": {"model": "test"},
                 "audio": {"status": "available", "utterances": []}, "events": [], "ranked_event_ids": [], "usage": {}, "notice": "仅候选", "cache_hit": False,
                 "index_path": str(output / "interaction-v1" / "index.json")}
        m._write_json(Path(index["index_path"]), index)
        return index
    monkeypatch.setattr(m, "build_interaction_index", build)
    monkeypatch.setattr(wb, "_media_transcript_provider", lambda *_args, **_kwargs: lambda _: None)
    preflight = wb.preflight_asset_material_interactions(p, asset_id)
    queued = wb.start_asset_media_index(p, asset_id, {"stage": "interaction", "remote_vision_confirmed": True,
        "remote_asr_confirmed": True, "preflight_signature": preflight["signature"]})
    job = queued["automation"]["media_index"]
    assert job["request"]["interaction_budget"]["model_calls_max"] == 2
    result = wb.generate_asset_media_index(p, job["job_id"])
    assert result["scenes"] == before["scenes"] and result["automation"]["media_index"]["status"] == "completed"
    public = wb.read_asset_material_interactions(p, asset_id)
    assert "index_path" not in public and "frames" not in public
    assert public["source_video_path"] == "assets/source.mp4"
    (p / "assets" / "source.mp4").write_bytes(b"changed")
    with pytest.raises(wb.WorkbenchError, match="变化"):
        wb.read_asset_material_interactions(p, asset_id)


def test_result_reader_blocks_path_escape(project):
    p, asset_id = project
    state = wb.read_workbench(p)
    state["assets"][0]["media_index"] = {"interaction_index_path": "../../outside.json"}
    wb._save(p, state)
    with pytest.raises(wb.WorkbenchError):
        wb.read_asset_material_interactions(p, asset_id)


def test_new_endpoint_uses_existing_worker_and_ui_has_one_confirmation(project, monkeypatch):
    p, asset_id = project
    launches = []
    monkeypatch.setattr(server, "_launch_media_index_worker", lambda *args: launches.append(args))
    with TestClient(server.create_app()) as client:
        preflight = client.get(f"/api/project/{p.name}/workbench/assets/{asset_id}/media-index/interaction-preflight?profile=efficient")
        assert preflight.status_code == 200 and preflight.json()["identity"]["model"] == "test"
        response = client.post(f"/api/project/{p.name}/workbench/assets/{asset_id}/media-index/jobs",
            json={"stage": "interaction", "remote_vision_confirmed": True, "remote_asr_confirmed": True,
                  "preflight_signature": preflight.json()["signature"]})
        assert response.status_code == 200 and len(launches) == 1
        assert client.get(f"/api/project/{p.name}/workbench/assets/{asset_id}/media-index/interactions").status_code == 422
    js = (Path(__file__).parents[2] / "backlot/ui/workbench.js").read_text(encoding="utf-8")
    controls = js.split("function materialAnalysisControls(", 1)[1].split("async function loadAssetMaterialInteractions", 1)[0]
    assert controls.count("window.confirm(") == 1
    assert "renderMaterialInteractions()" in js and "互动原片回看" in js


def test_503_is_durable_ambiguous_not_a_new_charge_on_resume(tmp_path):
    from backlot.ai_vision import VisionAIError
    path = tmp_path / "request.json"
    def unavailable():
        raise VisionAIError("HTTP 503: private-token endpoint-secret")
    with pytest.raises(m.InteractionError, match="HTTP 503") as failed:
        m._remote(path, unavailable, kind="互动识别", request_signature="same")
    assert failed.value.status == "ambiguous"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["http_status"] == 503 and data["attempts"] == 1 and not data["retryable"]
    assert "private-token" not in path.read_text(encoding="utf-8")
    with pytest.raises(m.InteractionError):
        m._remote(path, lambda: pytest.fail("no second HTTP"), kind="互动识别", request_signature="same")


def test_confirmed_non_acceptance_retries_only_unfinished_model_step(fake_media, tmp_path):
    from backlot.ai_vision import VisionAIError

    calls = []
    identity = {"model": "test"}
    asr_identity = "asr-test"
    output = tmp_path / "out"

    def asr(_source):
        calls.append("asr")
        return "你好", [{"start": 1, "end": 2, "text": "你好"}], {}

    def model(kind, payload, _images):
        calls.append(kind)
        if kind == "events" and calls.count("events") == 1:
            raise VisionAIError("HTTP 503: redacted")
        if kind == "events":
            cells = payload["cells"]
            return {"events": [event_payload(start=cells[1]["frame_id"], end=cells[-2]["frame_id"])]}
        return {"boundaries": [{
            **{key: target[key] for key in ("event_id", "start_frame_id", "end_frame_id", "start_state", "end_state")},
            "reason": "保留原边界",
        } for target in payload["targets"]]}

    arguments = dict(
        ffmpeg="ffmpeg", ffprobe="ffprobe", identity=identity, asr_identity=asr_identity,
        transcript_provider=asr, analyze=model,
    )
    with pytest.raises(m.InteractionError) as first:
        m.build_interaction_index(fake_media, output, **arguments)
    assert first.value.status == "ambiguous" and first.value.safe_resume_point == "W001-model.json"
    run_dir = m.interaction_run_directory(
        output, m.media_content_fingerprint(fake_media), identity, asr_identity, "efficient",
    )
    m.confirm_ambiguous_not_accepted(run_dir / first.value.safe_resume_point)
    completed = m.build_interaction_index(fake_media, output, **arguments)
    assert completed["status"] == "completed"
    assert calls.count("asr") == 1
    assert calls.count("events") == 2
    assert calls.count("boundary") == 1


def test_paid_asr_result_survives_local_timestamp_validation_failure(fake_media, tmp_path):
    calls = []
    def asr(_):
        calls.append(1)
        return "你好", [{"start": 8, "end": 3, "text": "你好"}], {}
    for _ in range(2):
        with pytest.raises(m.InteractionError, match="倒序"):
            m.build_interaction_index(fake_media, tmp_path / "out", ffmpeg="f", ffprobe="p", identity={},
                asr_identity="test", transcript_provider=asr, analyze=lambda *_: pytest.fail("invalid ASR"))
    assert len(calls) == 1


def test_missing_audio_is_explicit_and_does_not_transcribe(fake_media, tmp_path, monkeypatch):
    monkeypatch.setattr(m, "probe_media", lambda *_: {"duration_seconds": 30, "streams": [{"codec_type": "video"}]})
    result = m.build_interaction_index(fake_media, tmp_path / "out", ffmpeg="f", ffprobe="p", identity={},
        asr_identity="test", transcript_provider=lambda _: pytest.fail("no audio"), analyze=lambda *_: {"events": []})
    assert result["audio"]["status"] == "no_audio" and result["usage"]["asr_calls"] == 0
    assert result["usage"]["model_calls"] == 1 and result["usage"]["detail_frames"] == 0


def test_explicit_audio_off_never_calls_provider_and_keeps_visual_analysis(fake_media, tmp_path):
    calls = []
    result = m.build_interaction_index(
        fake_media, tmp_path / "out", ffmpeg="f", ffprobe="p", identity={"model": "test"},
        asr_identity="must-not-be-used", transcript_provider=lambda _: pytest.fail("audio is disabled"),
        recognize_audio=False,
        analyze=lambda kind, payload, _images: calls.append((kind, payload["audio_status"])) or {"events": []},
    )
    assert calls == [("events", "skipped")]
    assert result["audio"] == {"policy": "disabled", "status": "skipped", "provider": None, "utterances": []}
    assert result["usage"]["asr_calls"] == 0 and result["usage"]["audio_seconds"] == 0


def test_workbench_explicit_audio_off_never_validates_doubao_and_freezes_candidate_option(project, monkeypatch):
    p, asset_id = project
    monkeypatch.setattr(wb, "assert_doubao_asr_media_ready", lambda: pytest.fail("audio-off must not inspect Doubao"))
    preflight = wb.preflight_asset_material_interactions(p, asset_id, recognize_audio=False)
    assert preflight["recognize_audio"] is False
    assert preflight["effective_recognize_audio"] is False
    assert preflight["asr_identity"] == "disabled"
    assert preflight["budget"]["audio_seconds_max"] == 0

    queued = wb.start_asset_media_index(p, asset_id, {
        "stage": "interaction", "profile": "efficient",
        "recognize_audio": False, "generate_candidates": True,
        "transcript_provider": "none", "remote_vision_confirmed": True,
        "remote_asr_confirmed": False, "preflight_signature": preflight["signature"],
    })
    request = queued["automation"]["media_index"]["request"]
    assert request["recognize_audio"] is False
    assert request["effective_recognize_audio"] is False
    assert request["transcript_provider"] == "none"
    assert request["generate_candidates"] is True


def test_workbench_audio_requested_on_silent_video_skips_doubao_without_second_confirmation(project, monkeypatch):
    p, asset_id = project
    monkeypatch.setattr(wb, "assert_doubao_asr_media_ready", lambda: pytest.fail("no-audio input must not inspect Doubao"))
    preflight = wb.preflight_asset_material_interactions(p, asset_id, recognize_audio=True)
    assert preflight["has_audio"] is False
    assert preflight["effective_recognize_audio"] is False
    assert preflight["asr_identity"] == "no_audio"

    queued = wb.start_asset_media_index(p, asset_id, {
        "stage": "interaction", "profile": "efficient",
        "recognize_audio": True, "generate_candidates": False,
        "transcript_provider": "none", "remote_vision_confirmed": True,
        "remote_asr_confirmed": False, "preflight_signature": preflight["signature"],
    })
    request = queued["automation"]["media_index"]["request"]
    assert request["recognize_audio"] is True
    assert request["effective_recognize_audio"] is False
    assert request["transcript_provider"] == "none"


def test_partial_boundary_check_is_not_full_verification():
    event = normalize()
    target = {event["event_id"]: {"allowed_start": ["F1"], "allowed_end": ["F3", "B1"]}}
    raw = {"boundaries": [{"event_id": event["event_id"], "start_frame_id": "F1", "end_frame_id": "B1",
                         "start_state": "observed", "end_state": "observed", "reason": "只验证尾部"}]}
    m.apply_boundaries([event], raw, target, {"F1": {"pts": 6}, "F3": {"pts": 18}, "B1": {"pts": 20}}, 30)
    assert event["boundary_review"] == "partial" and event["boundary_reviewed_sides"] == ["end"]
    assert "B1" in event["evidence_frame_ids"]


def test_preflight_is_read_only_and_rejects_changed_confirmation(project):
    p, asset_id = project
    before = wb.read_workbench(p)
    plan = wb.preflight_asset_material_interactions(p, asset_id, "detailed")
    assert plan["budget"]["detail_frames_max"] == 12 and plan["identity"]["model"] == "test"
    assert wb.read_workbench(p) == before
    with pytest.raises(wb.WorkbenchError, match="确认期间变化"):
        wb.start_asset_media_index(p, asset_id, {"stage": "interaction", "remote_vision_confirmed": True,
            "remote_asr_confirmed": True, "preflight_signature": "stale"})


def test_source_changes_after_queue_are_blocked_before_provider(project, monkeypatch):
    p, asset_id = project
    preflight = wb.preflight_asset_material_interactions(p, asset_id)
    queued = wb.start_asset_media_index(p, asset_id, {"stage": "interaction", "remote_vision_confirmed": True,
        "remote_asr_confirmed": True, "preflight_signature": preflight["signature"]})
    (p / "assets/source.mp4").write_bytes(b"new input")
    monkeypatch.setattr(m, "build_interaction_index", lambda *_a, **_k: pytest.fail("no paid call"))
    with pytest.raises(wb.WorkbenchError, match="源素材"):
        wb.generate_asset_media_index(p, queued["automation"]["media_index"]["job_id"])


@pytest.mark.parametrize("duration,maximum", [(600, 5), (1800, 13), (3600, 24), (5333.035, 35)])
def test_long_video_preflight_budget_is_not_a_short_video_estimate(project, monkeypatch, duration, maximum):
    p, asset_id = project
    monkeypatch.setattr("backlot.media_index.probe_media", lambda *_: {"duration_seconds": duration, "streams": [{"codec_type": "audio"}]})
    result = wb.preflight_asset_material_interactions(p, asset_id)
    assert result["budget"]["model_calls_max"] == maximum
    assert result["budget"]["audio_seconds_max"] == duration


def test_interaction_batch_preflight_is_read_only_and_queue_freezes_each_asset(project):
    p, asset_id = project
    before = wb.read_workbench(p)
    plan = wb.preflight_asset_material_interactions_batch(p, [asset_id], "detailed")
    assert wb.read_workbench(p) == before
    assert plan["asset_count"] == 1
    assert plan["items"][0]["asset_id"] == asset_id
    assert plan["budget"] == {
        "model_calls_max": 2,
        "detail_frames_max": 12,
        "audio_seconds_max": 0.0,
        "windows": 1,
    }
    with pytest.raises(wb.WorkbenchError, match="一次确认"):
        wb.start_asset_media_index_batch(p, {
            "stage": "interaction", "asset_ids": [asset_id], "profile": "detailed",
            "preflight_signature": plan["signature"],
        })
    queued = wb.start_asset_media_index_batch(p, {
        "stage": "interaction", "asset_ids": [asset_id], "profile": "detailed",
        "remote_vision_confirmed": True, "remote_asr_confirmed": True,
        "preflight_signature": plan["signature"],
    })
    batch = queued["automation"]["media_index_batch"]
    assert batch["stage"] == "interaction" and batch["pending_asset_ids"] == [asset_id]
    assert batch["request"]["interaction_preflights"][asset_id]["signature"] == plan["items"][0]["signature"]


def test_interaction_batch_runs_existing_worker_serially(project, monkeypatch):
    p, asset_id = project
    plan = wb.preflight_asset_material_interactions_batch(p, [asset_id])
    queued = wb.start_asset_media_index_batch(p, {
        "stage": "interaction", "asset_ids": [asset_id], "profile": "efficient",
        "remote_vision_confirmed": True, "remote_asr_confirmed": True,
        "preflight_signature": plan["signature"],
    })

    def build(_source, output, **_kwargs):
        index_path = output / "interaction-v1" / "batch" / "material-interaction-index.json"
        result = {
            "source": {"fingerprint": plan["items"][0]["source"]},
            "version": m.VERSION, "signature": "batch-index", "status": "completed",
            "duration": 30, "profile": "efficient", "identity": {"model": "test"},
            "audio": {"status": "available", "utterances": []}, "events": [],
            "ranked_event_ids": [], "usage": {"model_calls": 1}, "notice": "仅候选",
            "cache_hit": False, "index_path": str(index_path),
        }
        m._write_json(index_path, result)
        return result

    monkeypatch.setattr(m, "build_interaction_index", build)
    monkeypatch.setattr(wb, "_media_transcript_provider", lambda *_args, **_kwargs: lambda _: None)
    monkeypatch.setattr(wb, "build_interaction_browser_proxy", lambda source, *_args, **_kwargs: {
        "status": "source_compatible", "path": str(source), "duration": 30,
        "width": 160, "height": 90, "video_codec": "h264", "audio_codec": "aac", "cache_hit": True,
    })
    completed = wb.generate_asset_media_index_batch(p, queued["automation"]["media_index_batch"]["job_id"])
    batch = completed["automation"]["media_index_batch"]
    assert batch["status"] == "completed" and batch["completed_asset_ids"] == [asset_id]
    assert completed["automation"]["media_index"]["stage"] == "interaction"


def test_ambiguous_retry_requires_human_non_acceptance_and_preserves_journal(project):
    p, asset_id = project
    plan = wb.preflight_asset_material_interactions(p, asset_id)
    queued = wb.start_asset_media_index(p, asset_id, {
        "stage": "interaction", "profile": "efficient", "transcript_provider": "doubao",
        "remote_vision_confirmed": True, "remote_asr_confirmed": True,
        "preflight_signature": plan["signature"],
    })
    old_job_id = queued["automation"]["media_index"]["job_id"]
    output = p / "artifacts" / "media-index" / asset_id
    run_dir = m.interaction_run_directory(output, plan["source"], plan["identity"], plan["asr_identity"], "efficient")
    journal = run_dir / "W001-model.json"
    m._write_json(journal, {
        "status": "ambiguous", "signature": "remote-step", "kind": "互动识别",
        "attempts": 1, "safe_resume_point": journal.name, "retryable": False, "http_status": 503,
    })
    error = m.InteractionError(
        "互动识别未完成（云端 HTTP 503）",
        status="ambiguous", safe_resume_point=journal.name, retryable=False,
        error_class="VisionAIError", http_status=503,
    )
    wb.mark_asset_media_index_failed(p, old_job_id, error)
    with pytest.raises(wb.WorkbenchError, match="确认供应商"):
        wb.resolve_asset_material_interaction_ambiguity(p, asset_id, {
            "preflight_signature": plan["signature"],
        })
    restarted = wb.resolve_asset_material_interaction_ambiguity(p, asset_id, {
        "confirmed_not_accepted": True,
        "preflight_signature": plan["signature"],
    })
    new_job = restarted["automation"]["media_index"]
    record = json.loads(journal.read_text(encoding="utf-8"))
    assert new_job["status"] == "queued" and new_job["job_id"] != old_job_id
    assert new_job["resolved_from_job_id"] == old_job_id
    assert record["status"] == "failed" and record["resolution"] == "confirmed_not_accepted"
    assert record["attempts"] == 1


def test_batch_item_keeps_its_own_ambiguity_recovery_snapshot(project):
    p, asset_id = project
    plan = wb.preflight_asset_material_interactions(p, asset_id)
    queued = wb.start_asset_media_index(p, asset_id, {
        "stage": "interaction", "profile": "efficient", "transcript_provider": "doubao",
        "remote_vision_confirmed": True, "remote_asr_confirmed": True,
        "preflight_signature": plan["signature"],
    })
    old_job = queued["automation"]["media_index"]
    output = p / "artifacts" / "media-index" / asset_id
    run_dir = m.interaction_run_directory(output, plan["source"], plan["identity"], plan["asr_identity"], "efficient")
    journal = run_dir / "W001-model.json"
    m._write_json(journal, {"status": "ambiguous", "signature": "remote", "kind": "互动识别", "attempts": 1})
    wb.mark_asset_media_index_failed(p, old_job["job_id"], m.InteractionError(
        "HTTP 503", status="ambiguous", safe_resume_point=journal.name, retryable=False,
    ))
    state = wb.read_workbench(p)
    state["automation"]["media_index"] = {
        "status": "completed", "job_id": "later-item", "asset_id": "S-999", "stage": "interaction",
    }
    wb._save(p, state)
    restarted = wb.resolve_asset_material_interaction_ambiguity(p, asset_id, {
        "confirmed_not_accepted": True, "preflight_signature": plan["signature"],
    })
    assert restarted["automation"]["media_index"]["status"] == "queued"
    assert restarted["automation"]["media_index"]["resolved_from_job_id"] == old_job["job_id"]


def test_tencent_unknown_acceptance_checkpoint_freezes_job_instead_of_resubmitting(project):
    """腾讯云：cloud_asr 停在 submitting（上次提交受理不明）时，重跑必须冻结为 ambiguous。"""
    p, asset_id = project
    queued = wb.start_asset_media_index(p, asset_id, {
        "stage": "overview", "remote_vision_confirmed": True,
    })
    job_id = queued["automation"]["media_index"]["job_id"]

    # 模拟「上一次提交已发出、没拿到确定应答」：任务级 checkpoint 停在 submitting。
    state = wb.read_workbench(p)
    state["automation"]["media_index"]["request"]["cloud_asr"] = {
        "status": "submitting", "engine": "tencent", "provider": "腾讯云语音识别",
        "request_id": "req-x",
    }
    wb._save(p, state)

    with pytest.raises(wb.TencentASRAmbiguous) as raised:
        wb.generate_asset_media_index(p, job_id)
    assert getattr(raised.value, "status", "") == "ambiguous"
    assert getattr(raised.value, "retryable", True) is False

    wb.mark_asset_media_index_failed(p, job_id, raised.value)
    failed = wb.read_workbench(p)["automation"]["media_index"]
    assert failed["status"] == "ambiguous", "受理不明必须冻结人工核对，而不是 failed"
    assert failed["error_detail"]["retryable"] is False
    assert failed["error_detail"]["next_action"].startswith("请先核对供应商")


def test_ui_has_unified_upload_analysis_and_guarded_ambiguous_retry():
    js = (Path(__file__).parents[2] / "backlot/ui/workbench.js").read_text(encoding="utf-8")
    card = js.split("function renderLocalMaterialPreparationCard()", 1)[1].split("function renderProjectLaunchpad", 1)[0]
    assert "本地素材分析场景" in card and "本地素材处理深度" in card
    assert "导入并开始智能分析" in card and "importAndStartLocalMaterialAnalysis" in card
    assert "/assets/media-index/interaction-preflight-batch" in js
    assert "/assets/media-index/interaction-batch" in js
    assert "识别音频（云端语音转文字）" in card
    assert "每条素材最多 3 个本地预览" in card
    # The transcript engine is chosen by the backend, so the UI must both render
    # the reported default and offer 腾讯云 alongside 豆包 / 本地 Whisper.
    assert "/workbench/transcript-providers" in js
    assert "tencent（腾讯云 ASR）" in js
    assert "defaultTranscriptProvider()" in js
    assert "uploadedIds.length !== selectedFileCount" in js
    assert "/media-index/interactions/candidates" in js
    assert "保留原片内容" in js and "保存调整并更新一次预览" in js
    assert "原片切口" in js and "候选切口" in js
    assert "登记为可用素材" in js and "弃用候选" in js
    assert "查看带时间线的音频转写" in js
    assert "分析结果独立于素材治理扫描" in js
    assert "查看互动候选" in js
    retry = js.split("async function resolveAmbiguousMaterialInteraction", 1)[1].split("async function updateMaterialInteractionReview", 1)[0]
    assert "确认上一次请求“没有受理、没有生成、不会计费”" in retry
    assert "confirmed_not_accepted: true" in retry


def test_interaction_batch_api_preflights_and_launches_one_durable_worker(project, monkeypatch):
    p, asset_id = project
    launches = []
    monkeypatch.setattr(server, "_launch_media_index_batch_worker", lambda *args: launches.append(args))
    with TestClient(server.create_app()) as client:
        preflight = client.post(
            f"/api/project/{p.name}/workbench/assets/media-index/interaction-preflight-batch",
            json={"asset_ids": [asset_id], "profile": "efficient"},
        )
        assert preflight.status_code == 200
        plan = preflight.json()
        started = client.post(
            f"/api/project/{p.name}/workbench/assets/media-index/interaction-batch",
            json={
                "asset_ids": [asset_id], "profile": "efficient",
                "remote_vision_confirmed": True, "remote_asr_confirmed": True,
                "preflight_signature": plan["signature"],
            },
        )
        assert started.status_code == 200
        assert started.json()["automation"]["media_index_batch"]["stage"] == "interaction"
        assert len(launches) == 1


def test_ambiguous_resolution_api_requires_confirmation_and_launches_retry(project, monkeypatch):
    p, asset_id = project
    plan = wb.preflight_asset_material_interactions(p, asset_id)
    queued = wb.start_asset_media_index(p, asset_id, {
        "stage": "interaction", "profile": "efficient", "transcript_provider": "doubao",
        "remote_vision_confirmed": True, "remote_asr_confirmed": True,
        "preflight_signature": plan["signature"],
    })
    job_id = queued["automation"]["media_index"]["job_id"]
    output = p / "artifacts" / "media-index" / asset_id
    run_dir = m.interaction_run_directory(output, plan["source"], plan["identity"], plan["asr_identity"], "efficient")
    journal = run_dir / "W001-model.json"
    m._write_json(journal, {
        "status": "ambiguous", "signature": "remote", "kind": "互动识别", "attempts": 1,
    })
    wb.mark_asset_media_index_failed(p, job_id, m.InteractionError(
        "HTTP 503", status="ambiguous", safe_resume_point=journal.name, retryable=False,
        error_class="VisionAIError", http_status=503,
    ))
    launches = []
    monkeypatch.setattr(server, "_launch_media_index_worker", lambda *args: launches.append(args))
    with TestClient(server.create_app()) as client:
        denied = client.post(
            f"/api/project/{p.name}/workbench/assets/{asset_id}/media-index/interactions/resolve-ambiguous",
            json={"preflight_signature": plan["signature"]},
        )
        assert denied.status_code == 422
        restarted = client.post(
            f"/api/project/{p.name}/workbench/assets/{asset_id}/media-index/interactions/resolve-ambiguous",
            json={"confirmed_not_accepted": True, "preflight_signature": plan["signature"]},
        )
        assert restarted.status_code == 200
        assert restarted.json()["automation"]["media_index"]["status"] == "queued"
        assert len(launches) == 1


def _probe_with_audio(monkeypatch, duration=30):
    """Report a real audio stream so the transcript branch is actually exercised."""
    monkeypatch.setattr("backlot.media_index.probe_media",
                        lambda *_: {"duration_seconds": duration, "streams": [{"codec_type": "audio"}]})


def _use_tencent_asr(monkeypatch, identity="tencent-asr-test"):
    monkeypatch.setattr(wb, "default_transcript_provider", lambda: "tencent")
    monkeypatch.setattr(wb, "assert_tencent_asr_ready", lambda: None)
    monkeypatch.setattr(wb, "tencent_asr_runtime_identity", lambda: identity)


def test_cloud_asr_checkpoint_never_labels_tencent_as_doubao():
    """进度与任务记录必须按实际引擎标注；腾讯任务曾被写成豆包极速版。"""
    label, submitting, accepted = wb._cloud_asr_checkpoint_fields("tencent", "media")
    assert label == "tencent-asr"
    assert "腾讯云" in submitting and "豆包" not in submitting
    assert "腾讯云" in accepted and "豆包" not in accepted

    label, submitting, _accepted = wb._cloud_asr_checkpoint_fields("doubao", "flash")
    assert label == "doubao-asr-1.0-flash"
    assert "豆包" in submitting

    label, _submitting, _accepted = wb._cloud_asr_checkpoint_fields("doubao", "media")
    assert label == "doubao-asr-2.0"


def test_interaction_preflight_selects_tencent_when_it_is_the_configured_engine(project, monkeypatch):
    p, asset_id = project
    _use_tencent_asr(monkeypatch)
    _probe_with_audio(monkeypatch)
    plan = wb.preflight_asset_material_interactions(p, asset_id, "efficient", True)
    assert plan["effective_recognize_audio"] is True
    assert plan["transcript_provider"] == "tencent"
    assert plan["asr_identity"] == "tencent-asr-test"


def test_interaction_preflight_honours_an_explicit_engine(project, monkeypatch):
    p, asset_id = project
    _use_tencent_asr(monkeypatch)
    _probe_with_audio(monkeypatch)
    plan = wb.preflight_asset_material_interactions(p, asset_id, "efficient", True, "doubao")
    assert plan["transcript_provider"] == "doubao"
    assert plan["asr_identity"] == "asr-test"


def test_interaction_preflight_signature_changes_with_the_engine(project, monkeypatch):
    p, asset_id = project
    _use_tencent_asr(monkeypatch)
    _probe_with_audio(monkeypatch)
    tencent = wb.preflight_asset_material_interactions(p, asset_id, "efficient", True, "tencent")
    doubao = wb.preflight_asset_material_interactions(p, asset_id, "efficient", True, "doubao")
    # Confirming a budget for one engine must never authorise another one.
    assert tencent["signature"] != doubao["signature"]
    assert tencent["signature"] == wb.preflight_asset_material_interactions(
        p, asset_id, "efficient", True, "tencent"
    )["signature"]


def test_disabled_audio_preflight_reports_no_engine(project):
    p, asset_id = project
    plan = wb.preflight_asset_material_interactions(p, asset_id, "efficient", False)
    assert plan["effective_recognize_audio"] is False
    assert plan["transcript_provider"] == "none"


def test_confirming_one_engine_does_not_authorise_another(project, monkeypatch):
    p, asset_id = project
    _use_tencent_asr(monkeypatch)
    _probe_with_audio(monkeypatch)
    plan = wb.preflight_asset_material_interactions(p, asset_id, "efficient", True)
    assert plan["transcript_provider"] == "tencent"
    # Swapping the engine after the budget confirmation is refused: either the
    # signature no longer matches, or the engine disagrees with this audio switch.
    with pytest.raises(wb.WorkbenchError, match="确认期间变化|不一致"):
        wb.start_asset_media_index(p, asset_id, {
            "stage": "interaction", "profile": "efficient", "transcript_provider": "doubao",
            "remote_vision_confirmed": True, "remote_asr_confirmed": True,
            "preflight_signature": plan["signature"],
        })
    # The genuine engine choice still launches.
    queued = wb.start_asset_media_index(p, asset_id, {
        "stage": "interaction", "profile": "efficient", "transcript_provider": "tencent",
        "remote_vision_confirmed": True, "remote_asr_confirmed": True,
        "preflight_signature": plan["signature"],
    })
    assert queued["automation"]["media_index"]["request"]["transcript_provider"] == "tencent"


def test_cloud_engine_without_audio_consent_is_rejected(project, monkeypatch):
    p, asset_id = project
    _use_tencent_asr(monkeypatch)
    with pytest.raises(wb.WorkbenchError, match="确认"):
        wb.start_asset_media_index(p, asset_id, {
            "stage": "coarse", "transcribe": True, "transcript_provider": "tencent",
            "remote_asr_confirmed": False,
        })


def test_unknown_transcript_engine_is_rejected(project):
    p, asset_id = project
    with pytest.raises(wb.WorkbenchError, match="语音识别只能选择"):
        wb.start_asset_media_index(p, asset_id, {
            "stage": "coarse", "transcribe": True, "transcript_provider": "gemini",
            "remote_asr_confirmed": True,
        })


def test_media_index_job_freezes_the_tencent_engine(project, monkeypatch):
    p, asset_id = project
    _use_tencent_asr(monkeypatch)
    queued = wb.start_asset_media_index(p, asset_id, {
        "stage": "coarse", "transcribe": True, "transcript_provider": "tencent",
        "remote_asr_confirmed": True,
    })
    request = queued["automation"]["media_index"]["request"]
    assert request["transcript_provider"] == "tencent" and request["transcribe"] is True


def test_cached_interaction_index_is_invalidated_when_the_engine_changes(project, tmp_path):
    p, asset_id = project
    path = p / "artifacts" / "media-index" / asset_id / "interaction-index.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    index = {"status": "completed", "profile": "efficient", "audio": {"policy": "tencent_transcript"}}
    path.write_text(json.dumps(index), encoding="utf-8")
    asset = {"id": asset_id, "media_index": {"interaction_index_path": str(path.relative_to(p))}}
    assert wb._interaction_index_matches_options(
        p, asset, profile="efficient", recognize_audio=True, transcript_provider="tencent"
    )
    # Same engine but audio off, or a different engine, must never reuse this index.
    assert not wb._interaction_index_matches_options(
        p, asset, profile="efficient", recognize_audio=False, transcript_provider="tencent"
    )
    assert not wb._interaction_index_matches_options(
        p, asset, profile="efficient", recognize_audio=True, transcript_provider="doubao"
    )
    assert not wb._interaction_index_matches_options(
        p, asset, profile="detailed", recognize_audio=True, transcript_provider="tencent"
    )


def test_transcript_provider_catalog_reflects_local_readiness(monkeypatch):
    from backlot import avatar_import

    monkeypatch.setattr(wb, "assert_tencent_asr_ready", lambda: None)
    monkeypatch.setattr(wb, "tencent_asr_runtime_identity", lambda: "tencent-asr-test")

    def no_doubao():
        raise wb.DoubaoASRError("未配置豆包 ASR 密钥")

    monkeypatch.setattr(wb, "assert_doubao_asr_media_ready", no_doubao)
    monkeypatch.setattr(avatar_import, "list_local_whisper_models", lambda: [])
    catalog = wb.transcript_provider_catalog()
    assert set(catalog) == {"tencent", "doubao", "local"}
    assert catalog["tencent"]["available"] is True
    assert catalog["doubao"]["available"] is False and "豆包" in catalog["doubao"]["detail"]
    assert catalog["local"]["available"] is False
    assert wb.default_transcript_provider() == "tencent"

    monkeypatch.setattr(avatar_import, "list_local_whisper_models", lambda: [{"id": "snapshot"}])
    monkeypatch.setattr(wb, "assert_tencent_asr_ready", lambda: (_ for _ in ()).throw(wb.TencentASRError("未配置")))
    catalog = wb.transcript_provider_catalog()
    assert catalog["tencent"]["available"] is False and catalog["local"]["available"] is True
    # 豆包 is still unavailable in this scenario, so the offline engine is next in line.
    assert wb.default_transcript_provider() == "local"


def test_tencent_is_preferred_over_the_offline_engine(monkeypatch):
    from backlot import avatar_import

    monkeypatch.setattr(wb, "assert_tencent_asr_ready", lambda: None)
    monkeypatch.setattr(avatar_import, "list_local_whisper_models", lambda: [{"id": "snapshot"}])
    monkeypatch.setattr(wb, "assert_doubao_asr_media_ready", lambda: None)
    assert wb.default_transcript_provider() == "tencent"


def test_transcript_provider_catalog_defaults_to_none_when_nothing_is_runnable(monkeypatch):
    from backlot import avatar_import

    monkeypatch.setattr(wb, "assert_tencent_asr_ready", lambda: (_ for _ in ()).throw(wb.TencentASRError("未配置")))
    monkeypatch.setattr(wb, "assert_doubao_asr_media_ready", lambda: (_ for _ in ()).throw(wb.DoubaoASRError("未配置")))
    monkeypatch.setattr(avatar_import, "list_local_whisper_models", lambda: [])
    assert wb.default_transcript_provider() == "none"
    with pytest.raises(wb.WorkbenchError, match="没有可用的语音识别服务"):
        wb._assert_any_transcript_provider_ready()


def test_transcript_provider_endpoint_lists_engines_and_default(project, monkeypatch):
    p, asset_id = project
    _use_tencent_asr(monkeypatch)
    with TestClient(server.create_app()) as client:
        response = client.get(f"/api/project/{p.name}/workbench/transcript-providers")
        assert response.status_code == 200
        body = response.json()
        assert body["default"] == "tencent"
        assert set(body["providers"]) == {"tencent", "doubao", "local"}
        assert body["providers"]["tencent"]["available"] is True
        assert body["providers"]["tencent"]["label"] == "腾讯云 ASR"


def test_interaction_preflight_endpoint_accepts_an_explicit_engine(project, monkeypatch):
    p, asset_id = project
    _use_tencent_asr(monkeypatch)
    _probe_with_audio(monkeypatch)
    with TestClient(server.create_app()) as client:
        tencent = client.get(
            f"/api/project/{p.name}/workbench/assets/{asset_id}/media-index/interaction-preflight"
            "?profile=efficient&recognize_audio=true&transcript_provider=tencent"
        )
        assert tencent.status_code == 200
        assert tencent.json()["transcript_provider"] == "tencent"
        assert tencent.json()["asr_identity"] == "tencent-asr-test"
        doubao = client.get(
            f"/api/project/{p.name}/workbench/assets/{asset_id}/media-index/interaction-preflight"
            "?profile=efficient&recognize_audio=true&transcript_provider=doubao"
        )
        assert doubao.status_code == 200
        assert doubao.json()["transcript_provider"] == "doubao"
        unknown = client.get(
            f"/api/project/{p.name}/workbench/assets/{asset_id}/media-index/interaction-preflight"
            "?profile=efficient&recognize_audio=true&transcript_provider=gemini"
        )
        assert unknown.status_code == 422


def test_interaction_batch_preflight_signature_covers_the_engine(project, monkeypatch):
    p, asset_id = project
    _use_tencent_asr(monkeypatch)
    _probe_with_audio(monkeypatch)
    with TestClient(server.create_app()) as client:
        tencent = client.post(
            f"/api/project/{p.name}/workbench/assets/media-index/interaction-preflight-batch",
            json={"asset_ids": [asset_id], "profile": "efficient", "transcript_provider": "tencent"},
        )
        assert tencent.status_code == 200
        assert tencent.json()["transcript_provider"] == "tencent"
        assert tencent.json()["items"][0]["transcript_provider"] == "tencent"
        default = client.post(
            f"/api/project/{p.name}/workbench/assets/media-index/interaction-preflight-batch",
            json={"asset_ids": [asset_id], "profile": "efficient"},
        )
        # The auto-picked default is 腾讯云 here, so both confirmations agree.
        assert default.json()["transcript_provider"] == "tencent"
        assert default.json()["signature"] == tencent.json()["signature"]


# --- P0-1 窗口并发调度（D1 双路线，全部离线、零付费）-----------------------------------


class _Probe:
    """线程安全的并发探针：在飞峰值、窗口调用、上一批上下文。"""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.live = 0
        self.peak = 0
        self.order: list[str] = []
        self.contexts: dict[str, list[str]] = {}
        self.kinds: list[str] = []

    def enter(self) -> None:
        with self.lock:
            self.live += 1
            self.peak = max(self.peak, self.live)

    def exit(self) -> None:
        with self.lock:
            self.live -= 1

    def window(self, window_id: str, context: list[str]) -> None:
        with self.lock:
            self.order.append(window_id)
            self.contexts[window_id] = context

    def kind(self, kind: str) -> None:
        with self.lock:
            self.kinds.append(kind)


def _stub_contact_sheets(monkeypatch) -> None:
    """用便宜的假联系表替换真实 PIL 拼图：并发契约与图像像素无关。"""

    def sheets(run_dir, frames, plan):
        chapter = str(plan["chapters"][0]["chapter_id"])
        cells = [{"cell_id": f"{chapter}-C{index + 1:02d}", "frame_id": frame["frame_id"],
                  "actual_pts_seconds": frame["actual_pts_seconds"]}
                 for index, frame in enumerate(frames[:9])]
        sheet = {"sheet_id": f"{chapter}-SHEET-0001", "path": str(Path(run_dir) / "sheet.jpg"),
                 "sha256": m.digest(cells)}
        return [sheet], cells

    monkeypatch.setattr(m, "_contact_sheets", sheets)


def _fake_long_media(tmp_path, monkeypatch, duration: float):
    source = tmp_path / "long.mp4"
    source.write_bytes(b"private-source")
    monkeypatch.setattr(m, "probe_media", lambda *_: {"duration_seconds": duration, "streams": [{"codec_type": "audio"}]})

    def frame(_source, _ffmpeg, timestamp, target):
        target.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (32, 18), "tan").save(target)
        return timestamp, m.digest(timestamp)

    monkeypatch.setattr(m, "_extract_frame", frame)
    return source


def _asr(_source):
    return "转写", [{"start": 1, "end": 2, "text": "转写"}], {}


def _probe_model(probe: _Probe, *, fail_window: str | None = None):
    def model(kind, payload, images):
        if kind == "boundary":
            probe.kind("boundary")
            return {"boundaries": [
                {**{k: target[k] for k in ("event_id", "start_frame_id", "end_frame_id",
                                           "start_state", "end_state")},
                 "reason": "保留原边界"} for target in payload["targets"]]}
        probe.kind("events")
        window_id = payload["window_id"]
        probe.window(window_id, [row["windows"][0] for row in payload["previous_events"]])
        probe.enter()
        try:
            time.sleep(0.01)
            if fail_window is not None and window_id == fail_window:
                raise m.InteractionError("视觉识别受理状态不明确，请核对服务记录",
                                         status="ambiguous", retryable=False)
            cells = payload["cells"]
            if len(cells) < 3:
                return {"events": []}
            return {"events": [event_payload(group=f"{window_id}-G01",
                                             start=cells[1]["frame_id"], end=cells[-2]["frame_id"])]}
        finally:
            probe.exit()

    return model


def _build(source, output, probe, *, policy="serial_equivalent", concurrency=None, fail_window=None):
    kwargs = dict(ffmpeg="ffmpeg", ffprobe="ffprobe", identity={"model": "test"}, asr_identity="test",
                  transcript_provider=_asr, analyze=_probe_model(probe, fail_window=fail_window),
                  window_context_policy=policy)
    if concurrency is not None:
        kwargs["interaction_concurrency"] = concurrency
    return m.build_interaction_index(source, output, **kwargs)


def _canonical(index, output_dir):
    """抹掉墙钟与绝对输出路径，只比较语义内容。"""
    copy = json.loads(json.dumps(index, ensure_ascii=False))
    copy["usage"].pop("elapsed_seconds", None)
    copy.pop("index_path", None)
    copy.pop("cache_hit", None)
    blob = json.dumps(copy, ensure_ascii=False, sort_keys=True)
    # JSON 里的反斜杠是转义过的，用同样的转义形式做替换，Windows 路径才匹配得上。
    needle = json.dumps(str(output_dir))[1:-1]
    return blob.replace(needle, "<OUT>")


def test_34_window_clip_keeps_its_call_count_formula(tmp_path, monkeypatch):
    """S-001 规模：34 窗口、付费调用仍是「窗口数 + 1 次边界复核」，与并发无关。"""
    monkeypatch.delenv("HAIKE_FORCE_SERIAL", raising=False)
    source = _fake_long_media(tmp_path, monkeypatch, 5460.0)
    _stub_contact_sheets(monkeypatch)
    probe = _Probe()

    index = _build(source, tmp_path / "out", probe)

    assert index["status"] == "completed"
    assert len(index["windows"]) == 34
    assert index["usage"]["model_calls"] == 35
    assert probe.kinds.count("events") == 34
    assert probe.kinds.count("boundary") == 1
    assert index["rejected_model_events"] == []


def test_serial_route_and_local_prefetch_route_are_byte_identical(tmp_path, monkeypatch):
    """路线甲：本地预取只改墙钟，不改结果；C=1 与 C=3 逐字节一致（保现状）。"""
    source = _fake_long_media(tmp_path, monkeypatch, 1000.0)
    _stub_contact_sheets(monkeypatch)

    first = _build(source, tmp_path / "serial", _Probe(), concurrency=1)
    prefetch_probe = _Probe()
    second = _build(source, tmp_path / "prefetch", prefetch_probe, concurrency=3)

    assert _canonical(first, tmp_path / "serial") == _canonical(second, tmp_path / "prefetch")
    assert prefetch_probe.peak >= 1 and prefetch_probe.peak <= 3


def test_concurrency_knobs_never_enter_the_paid_signature(tmp_path, monkeypatch):
    """N2：并发旋钮绝不进付费签名，否则会作废已付费的窗口日志。"""
    source = _fake_long_media(tmp_path, monkeypatch, 400.0)
    _stub_contact_sheets(monkeypatch)

    serial = _build(source, tmp_path / "a", _Probe(), concurrency=1)
    concurrent = _build(source, tmp_path / "b", _Probe(), policy="concurrent", concurrency=4)

    assert m.VERSION == "outdoor-interaction-v1"
    assert serial["signature"] == concurrent["signature"]


def test_concurrent_route_respects_the_cap_and_keeps_window_order(tmp_path, monkeypatch):
    source = _fake_long_media(tmp_path, monkeypatch, 1000.0)
    _stub_contact_sheets(monkeypatch)
    probe = _Probe()

    index = _build(source, tmp_path / "out", probe, policy="concurrent", concurrency=3)

    assert len(index["windows"]) == 7
    assert index["usage"]["model_calls"] == 8
    assert probe.peak <= 3 and probe.peak >= 2
    assert sorted(probe.order) == [f"W{i:03d}" for i in range(1, 8)]
    assert index["rejected_model_events"] == []


def test_concurrent_batches_only_see_strictly_earlier_batches(tmp_path, monkeypatch):
    """批次屏障：批内窗口互不可见，只拿严格更早批次已归并的事件（结果可复现）。"""
    source = _fake_long_media(tmp_path, monkeypatch, 1000.0)
    _stub_contact_sheets(monkeypatch)
    probe = _Probe()

    _build(source, tmp_path / "out", probe, policy="concurrent", concurrency=3)

    early, middle = {"W001", "W002", "W003"}, {"W004", "W005", "W006"}
    assert probe.contexts["W001"] == probe.contexts["W002"] == probe.contexts["W003"] == []
    for window_id in ("W004", "W005", "W006"):
        assert set(probe.contexts[window_id]) <= early
    assert set(probe.contexts["W007"]) <= early | middle


def test_concurrent_route_resumes_without_repaying(tmp_path, monkeypatch):
    source = _fake_long_media(tmp_path, monkeypatch, 1000.0)
    _stub_contact_sheets(monkeypatch)
    probe = _Probe()

    _build(source, tmp_path / "out", probe, policy="concurrent", concurrency=3)
    paid = len(probe.kinds)
    second = _build(source, tmp_path / "out", probe, policy="concurrent", concurrency=3)

    assert second["cache_hit"] is True
    assert len(probe.kinds) == paid, "命中缓存后不得再发起任何付费调用"


def test_ambiguous_window_freezes_and_keeps_the_paid_ones(tmp_path, monkeypatch):
    """受理状态不明确的窗口冻结人工核对；同批其他窗口的付费结果必须保留。"""
    source = _fake_long_media(tmp_path, monkeypatch, 400.0)
    _stub_contact_sheets(monkeypatch)
    probe = _Probe()

    with pytest.raises(m.InteractionError) as first:
        _build(source, tmp_path / "out", probe, policy="concurrent", concurrency=3, fail_window="W002")

    assert first.value.status == "ambiguous"
    assert first.value.retryable is False
    journal = next((tmp_path / "out").rglob("W002-model.json"))
    assert json.loads(journal.read_text(encoding="utf-8"))["status"] == "ambiguous"
    completed = {path.name for path in (tmp_path / "out").rglob("*-model.json")
                 if json.loads(path.read_text(encoding="utf-8"))["status"] == "completed"}
    assert "W001-model.json" in completed and "W003-model.json" in completed

    calls_after_first = len(probe.kinds)
    with pytest.raises(m.InteractionError) as again:
        _build(source, tmp_path / "out", probe, policy="concurrent", concurrency=3, fail_window="W002")

    assert again.value.status == "ambiguous"
    assert len(probe.kinds) == calls_after_first, "冻结窗口不得被自动重复提交"


def test_one_click_serial_kill_switch_overrides_the_concurrent_policy(tmp_path, monkeypatch):
    source = _fake_long_media(tmp_path, monkeypatch, 1000.0)
    _stub_contact_sheets(monkeypatch)
    monkeypatch.setenv("HAIKE_FORCE_SERIAL", "1")
    probe = _Probe()
    messages: list[str] = []

    index = m.build_interaction_index(
        source, tmp_path / "out", ffmpeg="ffmpeg", ffprobe="ffprobe", identity={"model": "test"},
        asr_identity="test", transcript_provider=_asr, analyze=_probe_model(probe),
        window_context_policy="concurrent", interaction_concurrency=4,
        progress=lambda stage, message: messages.append(message))

    assert probe.peak == 1
    assert len(index["windows"]) == 7
    assert any("一键回串行" in message for message in messages)


def test_invalid_window_context_policy_is_rejected_before_any_paid_call(fake_media, tmp_path):
    with pytest.raises(m.InteractionError, match="窗口上下文策略"):
        m.build_interaction_index(
            fake_media, tmp_path / "out", ffmpeg="f", ffprobe="p", identity={}, asr_identity="t",
            transcript_provider=lambda _: pytest.fail("no asr"),
            analyze=lambda *_: pytest.fail("no paid call"),
            window_context_policy="fully_async")


def test_invalid_concurrency_is_reported_in_chinese_before_any_paid_call(fake_media, tmp_path, monkeypatch):
    monkeypatch.setenv("HAIKE_INTERACTION_CONCURRENCY", "many")
    with pytest.raises(m.InteractionError, match="并发上限"):
        m.build_interaction_index(
            fake_media, tmp_path / "out", ffmpeg="f", ffprobe="p", identity={}, asr_identity="t",
            transcript_provider=lambda _: pytest.fail("no asr"),
            analyze=lambda *_: pytest.fail("no paid call"))
