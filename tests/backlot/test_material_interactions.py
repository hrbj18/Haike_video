from copy import deepcopy
import json
from pathlib import Path

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


@pytest.mark.parametrize("value", [0, -1, 3601, float("nan"), float("inf"), True])
def test_invalid_duration_rejected(value):
    with pytest.raises(m.InteractionError):
        m.window_plan(value)


@pytest.mark.parametrize("key,value", [("start_frame_id", "fake"), ("end_frame_id", "F1"),
    ("evidence_frame_ids", ["not-supplied"]), ("utterance_ids", ["fake"]), ("confidence", float("nan")),
    ("group_id", "G-fabricated"), ("start_state", "maybe")])
def test_model_cannot_invent_evidence_or_invalid_contract(key, value):
    row = event_payload()
    row[key] = value
    with pytest.raises(m.InteractionError):
        normalize(row)


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
    {"evidence_frame_ids": ["F1"]},
    {"start_frame_id": "F2", "end_frame_id": "F1"},
    {"start_frame_id": "F1", "end_frame_id": "F3", "evidence_frame_ids": ["F1", "F2", "F3"]},
])
def test_invalid_irrelevant_segment_is_rejected(patch):
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


@pytest.mark.parametrize("duration,maximum", [(600, 5), (1800, 13), (3600, 24)])
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


def test_ui_has_unified_upload_analysis_and_guarded_ambiguous_retry():
    js = (Path(__file__).parents[2] / "backlot/ui/workbench.js").read_text(encoding="utf-8")
    card = js.split("function renderLocalMaterialPreparationCard()", 1)[1].split("function renderProjectLaunchpad", 1)[0]
    assert "本地素材分析场景" in card and "本地素材处理深度" in card
    assert "导入并开始智能分析" in card and "importAndStartLocalMaterialAnalysis" in card
    assert "/assets/media-index/interaction-preflight-batch" in js
    assert "/assets/media-index/interaction-batch" in js
    assert "识别音频（豆包语音转文字）" in card
    assert "每条素材最多 3 个本地预览" in card
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
