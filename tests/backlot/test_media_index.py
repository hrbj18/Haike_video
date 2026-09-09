from __future__ import annotations

import subprocess
import json
from pathlib import Path

import pytest

from backlot.media_index import (
    build_coarse_index,
    build_fine_index,
    build_material_vision_index,
    media_content_fingerprint,
    recommend_coarse_segments,
    recommend_vision_shots,
)
from backlot import media_index as mi
from backlot.material_overview import sampling_plan
from backlot import workbench as wb
from backlot.workbench import _ffmpeg_available, _ffprobe_available


def test_explicit_cloud_transcript_failure_is_not_silently_downgraded(monkeypatch, tmp_path: Path) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"synthetic-source")
    monkeypatch.setattr(mi, "media_fingerprint", lambda _path: "fingerprint")
    monkeypatch.setattr(mi, "probe_media", lambda _path, _ffprobe: {"duration_seconds": 2.0})
    monkeypatch.setattr(mi, "_representative_frames", lambda *_args, **_kwargs: [
        {"time_seconds": 1.0, "path": "frame.jpg"},
    ])
    monkeypatch.setattr(mi, "_scene_change_times", lambda *_args, **_kwargs: [])

    class RequiredCloudFailure(RuntimeError):
        required = True

    def fail_transcript(_path):
        raise RequiredCloudFailure("paid ASR failed")

    with pytest.raises(RequiredCloudFailure, match="paid ASR failed"):
        build_coarse_index(
            source,
            tmp_path / "index",
            ffmpeg="ffmpeg",
            ffprobe="ffprobe",
            transcript_provider=fail_transcript,
            transcript_identity="doubao-asr-1.0-flash:test",
        )


def test_v2_content_fingerprint_is_stable_across_rename(tmp_path: Path) -> None:
    first = tmp_path / "first.bin"
    second = tmp_path / "renamed.bin"
    first.write_bytes(b"stable-material-content" * 100)
    second.write_bytes(first.read_bytes())

    assert media_content_fingerprint(first) == media_content_fingerprint(second)


def test_overview_long_material_requires_selected_chapter_and_reuses_each_confirmed_chapter() -> None:
    sampling = sampling_plan(61 * 60, [])
    selected, notice = wb._overview_chapters_for_request({"sampling": sampling}, [])
    assert selected == []
    assert "请选择章节" in str(notice)

    first, second = sampling["chapters"][:2]
    selected, notice = wb._overview_chapters_for_request({"sampling": sampling}, [first["chapter_id"]])
    assert notice is None and selected == [first]
    identity = {
        "provider": "test", "model": "test", "prompt_version": "v1",
        "schema_version": "v1", "image_detail": "auto", "image_longest_edge": "2048",
    }
    parent_signature = "f" * 64
    overview = {
        "status": "completed", "vision": identity,
        "chapters": [{"chapter_id": first["chapter_id"]}],
        "chapter_run_signatures": {
            first["chapter_id"]: wb._overview_vision_signature(parent_signature, [first], identity),
        },
    }
    assert wb._overview_vision_is_reusable(overview, [first], identity, parent_signature) is True
    assert wb._overview_vision_is_reusable(overview, [second], identity, parent_signature) is False


def test_perceptual_dedupe_keeps_different_motion_states() -> None:
    cv2 = pytest.importorskip("cv2")
    numpy = pytest.importorskip("numpy")
    left = numpy.full((120, 180, 3), 255, dtype=numpy.uint8)
    right = left.copy()
    cv2.rectangle(left, (15, 40), (55, 90), (0, 0, 0), -1)
    cv2.rectangle(right, (125, 40), (165, 90), (0, 0, 0), -1)

    def record(frame, frame_id, timestamp):
        encoded = cv2.imencode(".jpg", frame)[1].tobytes()
        return {
            "frame_id": frame_id, "shot_id": "SHOT-0001", "time_seconds": timestamp,
            "content_sha256": __import__("hashlib").sha256(encoded).hexdigest(),
            "dhash": mi._dhash(frame, cv2), "sharpness": 10,
            "_histogram": mi._histogram(frame, cv2), "selected_for_vision": True,
        }

    frames = [record(left, "FRAME-00001", 1.0), record(left.copy(), "FRAME-00002", 1.2), record(right, "FRAME-00003", 1.8)]
    mi._deduplicate_shot_frames(frames, cv2)

    assert sum(frame["selected_for_vision"] for frame in frames[:2]) == 1
    assert frames[2]["selected_for_vision"] is True
    assert frames[2]["duplicate_group_id"] != frames[0]["duplicate_group_id"]


@pytest.mark.skipif(not _ffmpeg_available(), reason="ffmpeg is required for local media-index verification")
def test_coarse_and_fine_index_are_local_cached_and_evidence_first(tmp_path: Path) -> None:
    ffmpeg = _ffmpeg_available()
    ffprobe = _ffprobe_available(ffmpeg)
    assert ffmpeg and ffprobe
    source = tmp_path / "robot-duck-demo.mp4"
    subprocess.run([
        ffmpeg, "-y",
        "-f", "lavfi", "-i", "color=c=red:s=320x180:r=12:d=2",
        "-f", "lavfi", "-i", "color=c=green:s=320x180:r=12:d=2",
        "-f", "lavfi", "-i", "color=c=blue:s=320x180:r=12:d=2",
        "-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]",
        "-map", "[v]", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(source),
    ], check=True, capture_output=True)

    coarse = build_coarse_index(
        source,
        tmp_path / "index",
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        interval_seconds=1,
        window_seconds=2,
        scene_threshold=.1,
    )
    cached = build_coarse_index(
        source,
        tmp_path / "index",
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        interval_seconds=1,
        window_seconds=2,
        scene_threshold=.1,
    )

    assert coarse["status"] == "completed"
    assert coarse["transcript_status"]["status"] == "transcript_unavailable"
    assert len(coarse["representative_frames"]) >= 5
    assert len(coarse["segments"]) >= 2
    assert cached["cache_hit"] is True

    fine = build_fine_index(coarse, 1, 3, ffmpeg=ffmpeg, fps=2)
    fine_cached = build_fine_index(coarse, 1, 3, ffmpeg=ffmpeg, fps=2)
    assert fine["stage"] == "fine"
    assert len(fine["frames"]) >= 3
    assert fine["transcript_status"]["status"] == "transcript_unavailable"
    assert fine_cached["cache_hit"] is True


def test_recommendations_rank_transcript_evidence_and_label_visual_only() -> None:
    index = {
        "source": {"name": "robot-duck-demo.mp4"},
        "segments": [
            {"id": "COARSE-0001", "start_seconds": 0, "end_seconds": 10, "transcript": "这只机器鸭可以用强化学习行走", "representative_frame": {}},
            {"id": "COARSE-0002", "start_seconds": 10, "end_seconds": 20, "transcript": "", "representative_frame": {}},
        ],
    }

    ranked = recommend_coarse_segments(index, "机器鸭如何学习行走")

    assert ranked[0]["segment_id"] == "COARSE-0001"
    assert ranked[0]["evidence_kind"] == "transcript"
    assert ranked[0]["score"] > 0
    assert ranked[1]["evidence_kind"] == "visual_only"
    assert "不能宣称语义匹配" in ranked[1]["reason"]


@pytest.mark.skipif(not _ffmpeg_available(), reason="ffmpeg is required for V2 material-vision verification")
def test_material_vision_index_is_adaptive_evidence_backed_and_cached(tmp_path: Path) -> None:
    ffmpeg = _ffmpeg_available()
    ffprobe = _ffprobe_available(ffmpeg)
    assert ffmpeg and ffprobe
    source = tmp_path / "robot-duck-visual.mp4"
    subprocess.run([
        ffmpeg, "-y",
        "-f", "lavfi", "-i", "color=c=red:s=320x180:r=12:d=2",
        "-f", "lavfi", "-i", "color=c=green:s=320x180:r=12:d=2",
        "-f", "lavfi", "-i", "color=c=blue:s=320x180:r=12:d=2",
        "-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]",
        "-map", "[v]", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(source),
    ], check=True, capture_output=True)
    calls = []

    def fake_describer(shots):
        calls.append([shot["shot_id"] for shot in shots])
        rows = []
        for shot in shots:
            evidence = next(frame for frame in shot["frames"] if frame["selected_for_vision"])["frame_id"]
            rows.append({
                "shot_id": shot["shot_id"],
                "summary": "彩色测试画面中的机器鸭",
                "entities": [{"name": "机器鸭", "confidence": .9, "evidence_frame_ids": [evidence]}],
                "actions": [{"name": "展示", "confidence": .8, "evidence_frame_ids": [evidence]}],
                "environment": "测试背景",
                "shot_type": "中景",
                "camera_motion": "固定",
                "state_changes": [],
                "screen_text": [],
                "quality": {"blur": "low", "occlusion": "none", "notes": ""},
                "unknowns": [],
                "overall_confidence": .85,
                "evidence_frame_ids": [evidence],
            })
        return rows, {"provider": "fake", "model": "fake-vision", "request_count": 1, "image_count": len(rows)}

    first = build_material_vision_index(
        source,
        tmp_path / "index",
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        scene_threshold=.1,
        maximum_shot_seconds=3,
        vision_describer=fake_describer,
        vision_identity={"provider": "fake", "model": "fake-vision"},
    )
    second = build_material_vision_index(
        source,
        tmp_path / "index",
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        scene_threshold=.1,
        maximum_shot_seconds=3,
        vision_describer=fake_describer,
        vision_identity={"provider": "fake", "model": "fake-vision"},
    )

    assert first["version"] == 2
    assert first["status"] == "completed"
    assert len(first["shots"]) >= 2
    assert all(1 <= len(shot["frames"]) <= 5 for shot in first["shots"])
    assert all(any(frame["selected_for_vision"] for frame in shot["frames"]) for shot in first["shots"])
    assert all(shot["description"]["entities"][0]["evidence_frame_ids"] for shot in first["shots"])
    assert second["cache_hit"] is True
    assert len(calls) == 1

    ranked = recommend_vision_shots(first, "机器鸭展示")
    assert ranked[0]["evidence_kind"] == "vision"
    assert ranked[0]["score"] > 0


@pytest.mark.skipif(not _ffmpeg_available(), reason="ffmpeg is required for V2 batch checkpoint verification")
def test_material_vision_index_resumes_after_a_later_batch_failure(tmp_path: Path) -> None:
    ffmpeg = _ffmpeg_available()
    ffprobe = _ffprobe_available(ffmpeg)
    assert ffmpeg and ffprobe
    source = tmp_path / "long-enough-for-batches.mp4"
    subprocess.run([
        ffmpeg, "-y", "-f", "lavfi", "-i", "testsrc2=s=240x136:r=12:d=12",
        "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(source),
    ], check=True, capture_output=True)

    def rows(shots):
        result = []
        for shot in shots:
            evidence = next(frame for frame in shot["frames"] if frame["selected_for_vision"])["frame_id"]
            result.append({
                "shot_id": shot["shot_id"], "summary": "测试镜头",
                "entities": [{"name": "测试图形", "confidence": .9, "evidence_frame_ids": [evidence]}],
                "actions": [], "environment": "测试", "shot_type": "固定", "camera_motion": "固定",
                "state_changes": [], "screen_text": [], "quality": {}, "unknowns": [],
                "overall_confidence": .9, "evidence_frame_ids": [evidence],
            })
        return result

    first_batches = []

    def fail_second_batch(shots):
        first_batches.append([shot["shot_id"] for shot in shots])
        if len(first_batches) == 2:
            raise mi.MediaIndexError("模拟第二批失败")
        return rows(shots), {"provider": "fake", "model": "fake-vision", "request_count": 1, "image_count": len(shots)}

    kwargs = {
        "ffmpeg": ffmpeg,
        "ffprobe": ffprobe,
        "maximum_shot_seconds": 1,
        "vision_identity": {"provider": "fake", "model": "fake-vision"},
    }
    with pytest.raises(mi.MediaIndexError, match="模拟第二批失败"):
        build_material_vision_index(source, tmp_path / "index", vision_describer=fail_second_batch, **kwargs)
    partial_path = next((tmp_path / "index").rglob("material-vision-index.json"))
    partial = json.loads(partial_path.read_text(encoding="utf-8"))
    completed_before_retry = sum(isinstance(shot.get("description"), dict) for shot in partial["shots"])
    assert completed_before_retry == len(first_batches[0])

    retried_batches = []

    def finish_remaining(shots):
        retried_batches.append([shot["shot_id"] for shot in shots])
        return rows(shots), {"provider": "fake", "model": "fake-vision", "request_count": 1, "image_count": len(shots)}

    completed = build_material_vision_index(source, tmp_path / "index", vision_describer=finish_remaining, **kwargs)

    assert completed["status"] == "completed"
    assert len(retried_batches) == 1
    assert set(retried_batches[0]).isdisjoint(first_batches[0])
    assert completed["vision"]["request_count"] == 2


@pytest.mark.skipif(not _ffmpeg_available(), reason="ffmpeg is required for workbench media-index integration")
def test_workbench_media_index_job_persists_coarse_fine_and_rejects_changed_source(tmp_path: Path) -> None:
    ffmpeg = _ffmpeg_available()
    project = tmp_path / "film"
    (project / "artifacts").mkdir(parents=True)
    (project / "assets").mkdir()
    (project / "project.json").write_text(json.dumps({
        "project_id": "film", "title": "素材索引集成", "pipeline_type": "animated-explainer",
    }, ensure_ascii=False), encoding="utf-8")
    (project / "artifacts" / "script.json").write_text(json.dumps({
        "title": "素材索引集成", "sections": [{
            "id": "s1", "text": "机器鸭行走", "start_seconds": 0, "end_seconds": 2,
        }],
    }, ensure_ascii=False), encoding="utf-8")
    (project / "artifacts" / "scene_plan.json").write_text(json.dumps({
        "scenes": [{
            "id": "scene-a", "description": "机器鸭行走", "start_seconds": 0,
            "end_seconds": 2, "script_section_id": "s1",
        }],
    }, ensure_ascii=False), encoding="utf-8")
    source = project / "assets" / "robot-duck.mp4"
    subprocess.run([
        ffmpeg, "-y", "-f", "lavfi", "-i", "testsrc2=s=240x135:r=12:d=2",
        "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(source),
    ], check=True, capture_output=True)
    state = wb.bootstrap_workbench(project)
    asset = wb._append_asset(project, state, {
        "name": "机器鸭原片", "type": "video", "source_type": "human_provided",
        "path": "assets/robot-duck.mp4", "duration_seconds": 2,
    })
    wb._save(project, state)

    queued = wb.start_asset_media_index(project, asset["id"], {"stage": "coarse", "query": "机器鸭行走"})
    coarse_id = queued["automation"]["media_index"]["job_id"]
    coarse = wb.generate_asset_media_index(project, coarse_id)
    media_state = next(item for item in coarse["assets"] if item["id"] == asset["id"])["media_index"]
    assert coarse["automation"]["media_index"]["status"] == "completed"
    assert media_state["coarse_index_path"].startswith("artifacts/media-index/")
    assert (project / media_state["coarse_index_path"]).is_file()
    candidates = wb.recommend_asset_media_segments(project, asset["id"], "机器鸭行走", 3)
    assert candidates["candidates"]
    assert candidates["candidates"][0]["evidence_kind"] in {"filename", "visual_only"}

    fine_queued = wb.start_asset_media_index(project, asset["id"], {
        "stage": "fine", "start_seconds": 0, "end_seconds": 1,
    })
    fine_id = fine_queued["automation"]["media_index"]["job_id"]
    fine = wb.generate_asset_media_index(project, fine_id)
    assert fine["automation"]["media_index"]["status"] == "completed"
    assert fine["automation"]["media_index"]["result"]["frame_count"] >= 1

    source.write_bytes(source.read_bytes() + b"changed")
    stale_queued = wb.start_asset_media_index(project, asset["id"], {
        "stage": "fine", "start_seconds": 0, "end_seconds": 1,
    })
    with pytest.raises(wb.WorkbenchError, match="粗筛后已经变化"):
        wb.generate_asset_media_index(project, stale_queued["automation"]["media_index"]["job_id"])


@pytest.mark.skipif(not _ffmpeg_available(), reason="ffmpeg is required for workbench visual-index integration")
def test_workbench_visual_job_requires_confirmation_exposes_details_and_reuses_cache(monkeypatch, tmp_path: Path) -> None:
    ffmpeg = _ffmpeg_available()
    project = tmp_path / "vision-film"
    (project / "artifacts").mkdir(parents=True)
    (project / "assets").mkdir()
    (project / "project.json").write_text(json.dumps({
        "project_id": "vision-film", "title": "画面理解集成", "pipeline_type": "animated-explainer",
    }, ensure_ascii=False), encoding="utf-8")
    (project / "artifacts" / "script.json").write_text(json.dumps({
        "title": "画面理解集成", "sections": [{"id": "s1", "text": "机器鸭展示", "start_seconds": 0, "end_seconds": 2}],
    }, ensure_ascii=False), encoding="utf-8")
    (project / "artifacts" / "scene_plan.json").write_text(json.dumps({
        "scenes": [{"id": "scene-a", "description": "机器鸭展示", "start_seconds": 0, "end_seconds": 2, "script_section_id": "s1"}],
    }, ensure_ascii=False), encoding="utf-8")
    source = project / "assets" / "robot-duck.mp4"
    subprocess.run([
        ffmpeg, "-y", "-f", "lavfi", "-i", "testsrc2=s=240x136:r=12:d=2",
        "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(source),
    ], check=True, capture_output=True)
    state = wb.bootstrap_workbench(project)
    asset = wb._append_asset(project, state, {
        "name": "机器鸭画面", "type": "video", "source_type": "human_provided",
        "path": "assets/robot-duck.mp4", "duration_seconds": 2,
    })
    wb._save(project, state)

    with pytest.raises(wb.WorkbenchError, match="明确确认"):
        wb.start_asset_media_index(project, asset["id"], {"stage": "vision"})

    calls = {"preflight": 0, "describe": 0}
    monkeypatch.setattr(wb, "vision_runtime_identity", lambda: {"provider": "fake", "model": "fake-vision"})

    def fake_preflight():
        calls["preflight"] += 1
        return {"ok": True, "status": "passed", "provider": "fake", "model": "fake-vision"}

    def fake_describe(shots):
        calls["describe"] += 1
        descriptions = []
        for shot in shots:
            evidence = next(frame for frame in shot["frames"] if frame["selected_for_vision"])["frame_id"]
            descriptions.append({
                "shot_id": shot["shot_id"], "summary": "机器鸭测试画面",
                "entities": [{"name": "机器鸭", "confidence": .9, "evidence_frame_ids": [evidence]}],
                "actions": [{"name": "展示", "confidence": .8, "evidence_frame_ids": [evidence]}],
                "environment": "测试", "shot_type": "中景", "camera_motion": "固定",
                "state_changes": [], "screen_text": [], "quality": {}, "unknowns": [],
                "overall_confidence": .85, "evidence_frame_ids": [evidence],
            })
        return descriptions, {"provider": "fake", "model": "fake-vision", "request_count": 1, "image_count": len(descriptions)}

    monkeypatch.setattr(wb, "test_vision_ai_connection", fake_preflight)
    monkeypatch.setattr(wb, "describe_shots", fake_describe)
    queued = wb.start_asset_media_index(project, asset["id"], {"stage": "vision", "remote_vision_confirmed": True})
    completed = wb.generate_asset_media_index(project, queued["automation"]["media_index"]["job_id"])

    media_state = next(item for item in completed["assets"] if item["id"] == asset["id"])["media_index"]
    assert media_state["vision_index_path"].startswith("artifacts/media-index/")
    details = wb.read_asset_material_vision(project, asset["id"])
    assert details["shots"][0]["description"]["summary"] == "机器鸭测试画面"
    assert details["shots"][0]["frames"][0]["path"].startswith("artifacts/media-index/")
    recommendation = wb.recommend_asset_media_segments(project, asset["id"], "机器鸭展示", 3)
    assert recommendation["evidence_source"] == "vision_v2"

    cached_job = wb.start_asset_media_index(project, asset["id"], {"stage": "vision", "remote_vision_confirmed": True})
    cached = wb.generate_asset_media_index(project, cached_job["automation"]["media_index"]["job_id"])
    assert cached["automation"]["media_index"]["result"]["cache_hit"] is True
    assert calls == {"preflight": 1, "describe": 1}


@pytest.mark.skipif(not _ffmpeg_available(), reason="ffmpeg is required for workbench overview verification")
def test_workbench_material_overview_runs_local_and_model_stages_after_one_confirmation(monkeypatch, tmp_path: Path) -> None:
    ffmpeg = _ffmpeg_available()
    assert ffmpeg
    project = tmp_path / "overview-film"
    (project / "artifacts").mkdir(parents=True)
    (project / "assets").mkdir()
    (project / "project.json").write_text(json.dumps({
        "project_id": "overview-film", "title": "快速概览集成", "pipeline_type": "animated-explainer",
    }, ensure_ascii=False), encoding="utf-8")
    (project / "artifacts" / "script.json").write_text(json.dumps({
        "title": "快速概览集成", "sections": [{"id": "s1", "text": "机械鸭抓取收纳框", "start_seconds": 0, "end_seconds": 3}],
    }, ensure_ascii=False), encoding="utf-8")
    (project / "artifacts" / "scene_plan.json").write_text(json.dumps({
        "scenes": [{"id": "scene-a", "description": "机械鸭抓取收纳框", "start_seconds": 0, "end_seconds": 3, "script_section_id": "s1"}],
    }, ensure_ascii=False), encoding="utf-8")
    source = project / "assets" / "robot-duck.mp4"
    subprocess.run([
        ffmpeg, "-y", "-f", "lavfi", "-i", "testsrc2=s=240x136:r=12:d=3",
        "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(source),
    ], check=True, capture_output=True)
    state = wb.bootstrap_workbench(project)
    asset = wb._append_asset(project, state, {
        "name": "机器鸭概览", "type": "video", "source_type": "human_provided",
        "path": "assets/robot-duck.mp4", "duration_seconds": 3,
    })
    wb._save(project, state)

    with pytest.raises(wb.WorkbenchError, match="高效率内容处理会在本地生成联系表后发送"):
        wb.start_asset_media_index(project, asset["id"], {"stage": "overview"})

    calls = {"preflight": 0, "overview": 0, "detail": 0}
    monkeypatch.setattr(wb, "overview_runtime_identity", lambda provider="default", detailed=False: {
        "provider": "fake", "model": "fake-vision",
        "prompt_version": "detail" if detailed else "overview",
        "schema_version": "1", "image_detail": "auto", "image_longest_edge": "1280" if detailed else "2048",
    })
    monkeypatch.setattr(wb, "test_vision_ai_connection", lambda: calls.__setitem__("preflight", calls["preflight"] + 1) or {"ok": True, "status": "passed"})

    def fake_overview(sheets, chapters, cells):
        calls["overview"] += 1
        assert sheets and cells
        return ([{
            "chapter_id": chapter["chapter_id"], "start_seconds": chapter["start_seconds"], "end_seconds": chapter["end_seconds"],
            "summary": "机械鸭抓取收纳框", "subjects": [{"name": "机械鸭"}], "actions": [{"name": "抓取"}], "quality": {},
            "usable_ranges": [{"label": "抓取画面", "start_seconds": cells[0]["actual_pts_seconds"], "end_seconds": cells[-1]["actual_pts_seconds"], "evidence_cell_ids": [cells[0]["cell_id"], cells[-1]["cell_id"]]}],
            "detail_candidates": [{"cell_id": cells[0]["cell_id"], "reason": "small_subject"}], "unknowns": [],
        } for chapter in chapters], {"provider": "fake", "model": "fake-vision", "request_count": 1, "image_count": len(sheets)})

    monkeypatch.setattr(wb, "describe_contact_sheets", fake_overview)

    def fake_detail(frames):
        calls["detail"] += 1
        assert 1 <= len(frames) <= 12
        return ([{
            "detail_id": frame["detail_id"], "cell_id": frame["cell_id"], "summary": "原始帧中可见机械鸭",
            "observations": ["可见主体"], "unknowns": [], "confidence": .9,
        } for frame in frames], {"provider": "fake", "model": "fake-vision", "request_count": 1, "image_count": len(frames)})

    monkeypatch.setattr(wb, "describe_detail_frames", fake_detail)
    confirmed_job = wb.start_asset_media_index(project, asset["id"], {
        "stage": "overview", "profile": "detailed", "remote_vision_confirmed": True,
    })
    confirmed = wb.generate_asset_media_index(project, confirmed_job["automation"]["media_index"]["job_id"])
    result = confirmed["automation"]["media_index"]["result"]
    assert result["overview_status"] == "completed"
    assert result["detail_status"] == "completed"
    assert result["remote_request_count"] == 2
    assert calls == {"preflight": 1, "overview": 1, "detail": 1}
    details = wb.read_asset_material_overview(project, asset["id"])
    assert details["sheets"] and details["cells"]
    assert all(not Path(item["path"]).is_absolute() for item in details["sheets"])
    assert all(float(item["actual_pts_seconds"]) >= 0 for item in details["cells"])
    draft = wb.create_local_material_orchestration(project, {"input_mode": "existing_script"})["local_material_orchestration"]
    assert draft["material_capability_map"][0]["evidence_level"] == "overview"
    assert draft["scene_plans"][0]["status"] == "needs_source_preview"
    assert draft["sequences"] == []

    cached_job = wb.start_asset_media_index(project, asset["id"], {
        "stage": "overview", "profile": "detailed", "remote_vision_confirmed": True,
    })
    cached = wb.generate_asset_media_index(project, cached_job["automation"]["media_index"]["job_id"])
    assert cached["automation"]["media_index"]["result"]["cache_hit"] is True
    assert calls == {"preflight": 1, "overview": 1, "detail": 1}


@pytest.mark.skipif(not _ffmpeg_available(), reason="ffmpeg is required for workbench overview verification")
def test_workbench_material_overview_keeps_completed_sheets_when_detail_read_fails(monkeypatch, tmp_path: Path) -> None:
    ffmpeg = _ffmpeg_available()
    assert ffmpeg
    project = tmp_path / "overview-detail-failure"
    (project / "artifacts").mkdir(parents=True)
    (project / "assets").mkdir()
    (project / "project.json").write_text(json.dumps({
        "project_id": "overview-detail-failure", "title": "精细化可恢复", "pipeline_type": "animated-explainer",
    }, ensure_ascii=False), encoding="utf-8")
    source = project / "assets" / "source.mp4"
    subprocess.run([
        ffmpeg, "-y", "-f", "lavfi", "-i", "testsrc2=s=240x136:r=12:d=2",
        "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(source),
    ], check=True, capture_output=True)
    state = wb.bootstrap_workbench(project)
    asset = wb._append_asset(project, state, {
        "name": "待精细化素材", "type": "video", "source_type": "human_provided",
        "path": "assets/source.mp4", "duration_seconds": 2,
    })
    wb._save(project, state)
    identity = {
        "provider": "fake", "model": "fake-vision", "prompt_version": "overview",
        "schema_version": "1", "image_detail": "auto", "image_longest_edge": "2048",
    }
    monkeypatch.setattr(wb, "overview_runtime_identity", lambda provider="default", detailed=False: {
        **identity, "prompt_version": "detail" if detailed else "overview",
        "image_longest_edge": "1280" if detailed else "2048",
    })
    monkeypatch.setattr(wb, "test_vision_ai_connection", lambda: {"ok": True, "status": "passed"})
    monkeypatch.setattr(wb, "describe_contact_sheets", lambda sheets, chapters, cells: ([{
        "chapter_id": chapter["chapter_id"], "start_seconds": chapter["start_seconds"], "end_seconds": chapter["end_seconds"],
        "summary": "测试画面", "subjects": [], "actions": [], "quality": {}, "usable_ranges": [],
        "detail_candidates": [{"cell_id": cells[0]["cell_id"], "reason": "verify"}], "unknowns": [],
    } for chapter in chapters], {"provider": "fake", "model": "fake-vision", "request_count": 1, "image_count": len(sheets)}))
    monkeypatch.setattr(wb, "describe_detail_frames", lambda frames: (_ for _ in ()).throw(RuntimeError("模拟精细化读取故障")))

    job = wb.start_asset_media_index(project, asset["id"], {
        "stage": "overview", "profile": "detailed", "remote_vision_confirmed": True,
    })
    completed = wb.generate_asset_media_index(project, job["automation"]["media_index"]["job_id"])
    result = completed["automation"]["media_index"]["result"]
    assert completed["automation"]["media_index"]["status"] == "completed"
    assert result["overview_status"] == "completed"
    assert result["detail_status"] == "failed"
    preserved = wb.read_asset_material_overview(project, asset["id"])
    assert preserved["status"] == "overview_completed_detail_failed"
    assert preserved["overview"]["status"] == "completed"
    assert preserved["sheets"]
