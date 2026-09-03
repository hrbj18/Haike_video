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
from backlot import workbench as wb
from backlot.workbench import _ffmpeg_available, _ffprobe_available


def test_v2_content_fingerprint_is_stable_across_rename(tmp_path: Path) -> None:
    first = tmp_path / "first.bin"
    second = tmp_path / "renamed.bin"
    first.write_bytes(b"stable-material-content" * 100)
    second.write_bytes(first.read_bytes())

    assert media_content_fingerprint(first) == media_content_fingerprint(second)


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
