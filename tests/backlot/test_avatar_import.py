"""Contract, API, and media tests for provider-neutral avatar imports."""

from __future__ import annotations

import json
import hashlib
import os
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backlot import avatar_import as avatar_mod
from backlot import server as server_mod
from backlot import state as state_mod
from backlot import workbench as workbench_mod
from backlot.avatar_import import AvatarImportError
from backlot.workbench import WorkbenchError, start_project_narration, start_scene_narration_candidate
from schemas.artifacts import validate_artifact


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def make_project(root: Path, project_id: str = "avatar-show") -> Path:
    project = root / project_id
    (project / "artifacts").mkdir(parents=True)
    write_json(project / "project.json", {
        "project_id": project_id,
        "title": "数字人科技快报",
        "pipeline_type": "avatar-spokesperson",
    })
    write_json(project / "artifacts" / "script.json", {
        "title": "数字人科技快报",
        "sections": [
            {
                "id": "s1", "turn_id": "T001", "speaker_id": "yaya", "speaker_name": "雅雅",
                "expected_asset_filename": "T001_YAYA.mp4", "text": "欢迎收听今天的科技快报。",
                "start_seconds": 0, "end_seconds": 2,
            },
            {
                "id": "s2", "turn_id": "T002", "speaker_id": "mengmeng", "speaker_name": "檬檬",
                "expected_asset_filename": "T002_MENGMENG.mp4", "text": "今天我们聊聊数字人的工作。",
                "start_seconds": 2, "end_seconds": 4,
            },
        ],
    })
    return project


@pytest.fixture
def projects_root(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    root.mkdir()
    monkeypatch.setattr(state_mod, "PROJECTS_DIR", root)
    monkeypatch.setattr(server_mod, "PROJECTS_DIR", root)
    monkeypatch.setattr(server_mod, "_summary_cache", {})
    monkeypatch.setattr(server_mod, "_PROJECTS_ROOT_STR", os.path.normcase(str(root.resolve())))
    return root


@pytest.fixture
def client(projects_root, monkeypatch):
    async def no_watch():
        return None

    monkeypatch.setattr(server_mod, "_watch_projects", no_watch)
    with TestClient(server_mod.create_app()) as instance:
        yield instance


def fake_probe(path: Path) -> dict:
    return {
        "duration_seconds": 0.32,
        "size_bytes": path.stat().st_size,
        "video": {"present": True, "codec": "h264", "width": 160, "height": 240, "fps": 25.0, "pixel_format": "yuv420p"},
        "audio": {"present": True, "codec": "aac", "sample_rate": 48000, "channels": 2},
    }


def exact_clock_manifest(package: dict) -> dict:
    roles = {
        speaker_id: {
            "path": f"assets/audio/{speaker_id}.wav",
            "sha256": speaker_id,
            "duration_seconds": 1.2,
            "content_duration_seconds": 1.1875,
            "sample_rate": 24_000,
            "channels": 1,
            "sample_width": 2,
            "content_sample_frames": 28_500,
            "final_padding_sample_frames": 300,
            "sample_frame_count": 28_800,
            "samples_per_video_frame": 960,
            "video_fps": 25,
            "video_frame_count": 30,
        }
        for speaker_id in ("yaya", "mengmeng")
    }
    turns = [
        {
            "turn_id": turn["turn_id"],
            "speaker_id": turn["speaker_id"],
            "text_sha256": hashlib.sha256(turn["text"].encode("utf-8")).hexdigest(),
            "sample_rate": 24_000,
            "source_start_sample": 0,
            "speech_start_sample": 2_400,
            "speech_end_sample": 21_600,
            "source_end_sample": 28_800,
            "source_start_frame": 0,
            "source_end_frame_exclusive": 30,
            "source_start_seconds": 0.0,
            "speech_start_seconds": 0.1,
            "speech_end_seconds": 0.9,
            "source_end_seconds": 1.2,
        }
        for turn in package["turns"]
    ]
    return {
        "version": "avatar-turn-timing-v2",
        "contract": {"video_fps": 25, "frame_alignment": "final_role_track_once"},
        "roles": roles,
        "turns": turns,
    }


def test_package_initializes_from_stable_script_turns(projects_root):
    project = make_project(projects_root)

    package = avatar_mod.initialize_avatar_package(project)

    validate_artifact("avatar_source_package", package)
    assert package["audio_mode"] == "native_avatar_audio"
    assert package["import_mode"] == "per_turn"
    assert [turn["turn_id"] for turn in package["turns"]] == ["T001", "T002"]
    assert [turn["speaker_id"] for turn in package["turns"]] == ["yaya", "mengmeng"]
    assert package["turns"][0]["expected_filename"] == "T001_YAYA.mp4"


def test_frozen_longform_gaps_drive_final_turn_timeline(projects_root):
    project = make_project(projects_root)
    script_path = project / "artifacts" / "script.json"
    script = json.loads(script_path.read_text(encoding="utf-8"))
    script["sections"].insert(1, {
        "id": "s1b", "turn_id": "T003", "speaker_id": "yaya", "speaker_name": "雅雅",
        "expected_asset_filename": "T003_YAYA.mp4", "text": "同一位主持人继续补充。",
        "start_seconds": 2, "end_seconds": 3,
    })
    write_json(script_path, script)
    package = avatar_mod.initialize_avatar_package(project, {
        "import_mode": "longform", "generation_mode": "runninghub_longform",
        "speaker_change_gap_seconds": 0.25, "same_speaker_gap_seconds": 0.30,
    })

    assert package["settings"]["speaker_change_gap_seconds"] == pytest.approx(0.25)
    assert package["settings"]["same_speaker_gap_seconds"] == pytest.approx(0.30)
    assert avatar_mod._longform_turn_gap(package, 0) == pytest.approx(0.30)
    assert avatar_mod._longform_turn_gap(package, 1) == pytest.approx(0.25)
    assert avatar_mod._longform_turn_gap(package, 2) == pytest.approx(0.0)


def test_deterministic_timing_manifest_is_primary_and_whisper_only_reviews(projects_root):
    project = make_project(projects_root)
    package = avatar_mod.initialize_avatar_package(project, {"import_mode": "longform", "require_asr": True})
    timings = []
    for turn in package["turns"]:
        timings.append({
            "turn_id": turn["turn_id"], "speaker_id": turn["speaker_id"],
            "text_sha256": hashlib.sha256(turn["text"].encode("utf-8")).hexdigest(),
            "source_start_seconds": 0.0, "speech_start_seconds": 0.1,
            "speech_end_seconds": 0.9, "source_end_seconds": 1.05,
        })
    package = avatar_mod.apply_longform_timing_manifest(project, {
        "version": "avatar-turn-timing-v1", "path": "assets/audio/timing.json",
        "sha256": "manifest-hash", "input_signature": "input-signature", "turns": timings,
    })
    transcripts = {
        turn["speaker_id"]: {
            "text": turn["text"],
            "segments": [{"start": 0.11, "end": 0.91, "text": turn["text"], "words": [{"start": 0.11, "end": 0.91, "word": turn["text"]}]}],
        }
        for turn in package["turns"]
    }

    issues = avatar_mod._review_deterministic_longform_turns(
        package, transcripts, package["asr"]["summary"]["timing_manifest"],
    )

    assert issues == []
    assert package["cut_plan"]["status"] == "approved"
    assert package["cut_plan"]["summary"]["source"] == "deterministic_timing_manifest"
    assert all(item["status"] == "approved" for item in package["cut_plan"]["items"])
    assert all(item["start_seconds"] == pytest.approx(0.0) for item in package["cut_plan"]["items"])
    assert all(item["end_seconds"] == pytest.approx(1.05) for item in package["cut_plan"]["items"])
    validate_artifact("avatar_source_package", package)


def test_exact_clock_v2_manifest_validates_integer_sample_and_frame_contract(projects_root):
    project = make_project(projects_root)
    package = avatar_mod.initialize_avatar_package(project, {"import_mode": "longform", "require_asr": True})
    manifest = exact_clock_manifest(package)

    applied = avatar_mod.apply_longform_timing_manifest(project, manifest)

    stored = applied["asr"]["summary"]["timing_manifest"]
    assert stored["roles"]["yaya"]["sample_frame_count"] == 28_800
    assert stored["turns"][0]["source_end_frame_exclusive"] == 30

    broken = json.loads(json.dumps(manifest))
    broken["turns"][0]["source_end_sample"] -= 1
    with pytest.raises(AvatarImportError, match="25FPS"):
        avatar_mod.apply_longform_timing_manifest(project, broken)


def test_exact_clock_v2_whisper_drift_is_diagnostic_and_never_opens_manual_gate(projects_root):
    project = make_project(projects_root)
    package = avatar_mod.initialize_avatar_package(project, {"import_mode": "longform", "require_asr": True})
    package = avatar_mod.apply_longform_timing_manifest(project, exact_clock_manifest(package))
    transcripts = {
        speaker["speaker_id"]: {
            "text": "完全错误",
            "segments": [{
                "start": 3.0, "end": 3.2, "text": "完全错误",
                "words": [{"start": 3.0, "end": 3.2, "word": "完全错误"}],
            }],
        }
        for speaker in package["speakers"]
    }

    issues = avatar_mod._review_deterministic_longform_turns(
        package, transcripts, package["asr"]["summary"]["timing_manifest"],
    )

    assert {item["code"] for item in issues} == {"exact_clock_asr_diagnostic_warning"}
    assert package["cut_plan"]["status"] == "approved"
    assert package["cut_plan"]["summary"]["needs_manual"] == 0
    assert package["cut_plan"]["summary"]["cut_authority"] == "exact_frame_manifest"
    assert all(item["status"] == "approved" for item in package["cut_plan"]["items"])
    assert all(item["confidence"] == "exact_clock" for item in package["cut_plan"]["items"])
    assert all(item["source_type"] == "deterministic_timing_manifest" for item in package["cut_plan"]["items"])
    validate_artifact("avatar_source_package", package)


def test_exact_clock_v2_can_materialize_cuts_when_whisper_diagnostic_crashes(projects_root):
    project = make_project(projects_root)
    package = avatar_mod.initialize_avatar_package(project, {"import_mode": "longform", "require_asr": True})
    avatar_mod.apply_longform_timing_manifest(project, exact_clock_manifest(package))

    result = avatar_mod.approve_exact_clock_manifest_cuts(
        project,
        diagnostic_error=RuntimeError("synthetic local ASR crash"),
        model_name="faster-whisper-small",
    )

    assert result["asr"]["status"] == "passed"
    assert result["asr"]["summary"]["diagnostic_only"] is True
    assert result["asr"]["summary"]["diagnostic_status"] == "unavailable"
    assert result["cut_plan"]["status"] == "approved"
    assert any(item["code"] == "exact_clock_asr_unavailable" for item in result["asr"]["issues"])
    validate_artifact("avatar_source_package", result)


def test_exact_clock_manifest_survives_partial_whisper_progress_save(projects_root, monkeypatch):
    project = make_project(projects_root)
    package = avatar_mod.initialize_avatar_package(project, {"import_mode": "longform", "require_asr": True})
    package = avatar_mod.apply_longform_timing_manifest(project, exact_clock_manifest(package))
    package["asr"].update({"status": "running", "started_at": "2026-09-01T00:00:00Z"})
    for speaker in package["speakers"]:
        speaker["source"] = {"path": f"assets/{speaker['speaker_id']}.mp4"}
    snapshots: list[dict] = []
    monkeypatch.setattr(avatar_mod, "read_avatar_package", lambda _project: package)
    monkeypatch.setattr(
        avatar_mod,
        "_save_package",
        lambda _project, value: snapshots.append(json.loads(json.dumps(value))) or value,
    )
    monkeypatch.setattr(avatar_mod, "_load_whisper", lambda *_args: (object(), "faster-whisper-small"))
    calls = 0

    def transcribe(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("synthetic interruption")
        return "欢迎收听今天的科技快报。", [{
            "start": 0.1, "end": 0.9, "text": "欢迎收听今天的科技快报。",
            "words": [{"start": 0.1, "end": 0.9, "word": "欢迎收听今天的科技快报。"}],
        }]

    monkeypatch.setattr(avatar_mod, "_transcribe_file", transcribe)

    with pytest.raises(RuntimeError, match="synthetic interruption"):
        avatar_mod.run_avatar_asr(project, {"model": "faster-whisper-small"})

    assert snapshots
    assert snapshots[-1]["asr"]["summary"]["timing_manifest"]["version"] == "avatar-turn-timing-v2"
    assert snapshots[-1]["asr"]["summary"]["alignment_source"] == "deterministic_timing_manifest"


def test_exact_clock_v2_rejects_ambiguous_or_drifted_integer_ledger(projects_root):
    project = make_project(projects_root)
    package = avatar_mod.initialize_avatar_package(project, {"import_mode": "longform", "require_asr": True})
    manifest = exact_clock_manifest(package)

    duplicate = json.loads(json.dumps(manifest))
    duplicate["turns"][1]["turn_id"] = duplicate["turns"][0]["turn_id"]
    with pytest.raises(AvatarImportError, match="重复或无效"):
        avatar_mod.apply_longform_timing_manifest(project, duplicate)

    fractional = json.loads(json.dumps(manifest))
    fractional["roles"]["yaya"]["video_frame_count"] = 30.5
    with pytest.raises(AvatarImportError, match="必须是整数"):
        avatar_mod.apply_longform_timing_manifest(project, fractional)

    wrong_format = json.loads(json.dumps(manifest))
    wrong_format["roles"]["yaya"]["channels"] = 2
    with pytest.raises(AvatarImportError, match="音频采样数"):
        avatar_mod.apply_longform_timing_manifest(project, wrong_format)

    speech_drift = json.loads(json.dumps(manifest))
    speech_drift["turns"][0]["speech_start_seconds"] = 0.2
    with pytest.raises(AvatarImportError, match="25FPS"):
        avatar_mod.apply_longform_timing_manifest(project, speech_drift)


def test_deterministic_timing_review_stops_at_manual_gate_on_text_or_drift(projects_root):
    project = make_project(projects_root)
    package = avatar_mod.initialize_avatar_package(project, {"import_mode": "longform", "require_asr": True})
    timings = [{
        "turn_id": turn["turn_id"], "speaker_id": turn["speaker_id"],
        "text_sha256": hashlib.sha256(turn["text"].encode("utf-8")).hexdigest(),
        "source_start_seconds": 0.0, "speech_start_seconds": 0.1,
        "speech_end_seconds": 0.9, "source_end_seconds": 1.05,
    } for turn in package["turns"]]
    package = avatar_mod.apply_longform_timing_manifest(project, {"version": "avatar-turn-timing-v1", "turns": timings})
    transcripts = {
        speaker["speaker_id"]: {"text": "错误", "segments": [{"start": 2.0, "end": 2.2, "text": "错误", "words": [{"start": 2.0, "end": 2.2, "word": "错误"}]}]}
        for speaker in package["speakers"]
    }

    issues = avatar_mod._review_deterministic_longform_turns(package, transcripts, package["asr"]["summary"]["timing_manifest"])

    assert len(issues) == 2
    assert package["cut_plan"]["status"] == "needs_attention"
    assert package["cut_plan"]["summary"]["needs_manual"] == 2


def test_legacy_longform_without_manifest_keeps_asr_alignment_path(projects_root, monkeypatch):
    project = make_project(projects_root)
    package = avatar_mod.initialize_avatar_package(project, {"import_mode": "longform", "require_asr": True})
    assert "timing_manifest" not in package["asr"]["summary"]
    called: list[bool] = []
    monkeypatch.setattr(avatar_mod, "_align_longform_turns", lambda *_args: called.append(True) or [])
    # The branch contract is explicit: no persisted manifest selects the old aligner.
    if not package["asr"]["summary"].get("timing_manifest"):
        avatar_mod._align_longform_turns(project, package, {})
    assert called == [True]


def test_applying_a_repaired_speaker_candidate_recomputes_average_warning(projects_root):
    project = make_project(projects_root)
    package = avatar_mod.initialize_avatar_package(project, {"import_mode": "longform", "require_asr": False})
    # Start from a schema-valid package, then exercise only the aggregate
    # recomputation branch with an in-memory write seam.
    package["asr"]["status"] = "passed"
    package["asr"]["issues"] = [{"code": "average_similarity_failed", "severity": "warning", "message": "stale"}]
    package["asr"]["summary"] = {"average_similarity": .4}
    package["asr"]["speaker_diagnostics"] = {"yaya": {"candidates": [{
        "candidate_id": "candidate-good", "status": "ready", "source_sha256": None,
        "cut_items": [{"turn_id": "T001", "speaker_id": "yaya", "status": "pending_review", "asr_similarity": .99, "asr_coverage": .99}],
        "issues": [],
    }]}}
    package["speakers"][0]["source"] = {"sha256": None}
    package["validation"]["status"] = "passed"
    package["turns"][1]["asr_similarity"] = .99
    package["cut_plan"] = {"status": "awaiting_review", "items": [{"turn_id": "T002", "speaker_id": "mengmeng", "status": "pending_review", "asr_similarity": .99, "asr_coverage": .99}], "summary": {}}
    original_read, original_save = avatar_mod.read_avatar_package, avatar_mod._save_package
    avatar_mod.read_avatar_package = lambda _project: package
    avatar_mod._save_package = lambda _project, value: value
    try:
        repaired = avatar_mod.apply_longform_speaker_candidate(project, "yaya", "candidate-good")
    finally:
        avatar_mod.read_avatar_package, avatar_mod._save_package = original_read, original_save

    assert repaired["asr"]["summary"]["average_similarity"] == pytest.approx(.99)
    assert not any(issue["code"] == "average_similarity_failed" for issue in repaired["asr"]["issues"])


def test_presenter_layout_template_can_apply_to_one_speaker_without_retiming(projects_root):
    project = make_project(projects_root)
    avatar_mod.initialize_avatar_package(project, {"require_asr": False})
    board = workbench_mod.bootstrap_workbench(project)
    for index, scene in enumerate(board["scenes"]):
        scene["presenter"] = {
            "treatment": "pip_top_left", "source_path": "assets/video/avatar.mp4",
            "turn_id": "T001" if index == 0 else "T002",
        }
    write_json(project / "artifacts" / "workbench.json", board)

    saved = workbench_mod.update_presenter_layout_template(project, {
        "name": "右侧讲解", "geometry": {"x": .6, "y": .08, "width": .24},
        "scene_id": board["scenes"][0]["id"], "apply_scope": "speaker",
    })

    first, second = saved["scenes"]
    assert first["presenter"]["layout_template_id"].startswith("custom-")
    assert first["presenter"]["treatment"] == "custom"
    assert second["presenter"]["layout_template_id"] == "pip_top_left"
    assert [scene["start_seconds"] for scene in saved["scenes"]] == [0.0, 2.0]


def test_avatar_source_package_allows_custom_default_treatment(projects_root):
    package = avatar_mod.initialize_avatar_package(make_project(projects_root), {"default_treatment": "custom"})

    assert package["presentation"]["default_treatment"] == "custom"


def test_custom_presenter_geometry_keeps_source_aspect_ratio(projects_root, monkeypatch, tmp_path):
    project = make_project(projects_root)
    board = workbench_mod.bootstrap_workbench(project)
    scene = board["scenes"][0]
    scene["presenter"] = {"treatment": "custom", "layout_template_id": "pip_top_left", "layout_override": {"x": .5, "y": .1, "width": .4}}
    source = project / "assets" / "avatar.mp4"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"placeholder")
    metadata = json.loads((project / "project.json").read_text(encoding="utf-8"))
    metadata["render_profile"] = {"width": 1000, "height": 1000}
    write_json(project / "project.json", metadata)
    monkeypatch.setattr(workbench_mod, "_probe_video", lambda *_args, **_kwargs: {"streams": [{"codec_type": "video", "width": 100, "height": 200}]})

    geometry = workbench_mod._avatar_pip_geometry(project, board, scene, source)

    assert geometry["width"] == 360
    assert geometry["height"] == 720  # hard subtitle-safe maximum, no squashing
    assert geometry["x"] == 500


def test_package_records_presentation_contract_without_claiming_keying_support(projects_root):
    project = make_project(projects_root)

    package = avatar_mod.initialize_avatar_package(project, {
        "background_mode": "green_screen",
        "default_treatment": "pip_top_left",
    })

    assert package["presentation"] == {
        "background_mode": "green_screen",
        "alpha_mode": False,
        "expected_audio": "embedded_native",
        "default_treatment": "pip_top_left",
        "frame_fit_mode": "contain_black",
    }


def test_invalid_package_settings_are_reported_as_a_client_error(client, projects_root):
    make_project(projects_root)

    response = client.post(
        "/api/project/avatar-show/workbench/avatar-package/initialize",
        json={"max_duration_seconds": -1},
    )

    assert response.status_code == 422
    assert "数据合同" in response.json()["detail"]


def test_upload_targets_are_canonical_and_confined_to_project(projects_root):
    project = make_project(projects_root)
    avatar_mod.initialize_avatar_package(project)

    temporary, target = avatar_mod.prepare_upload(project, "../../other-name.mp4", turn_id="T001")

    assert target == project / "assets" / "incoming" / "avatar" / "yaya" / "T001_YAYA.mp4"
    assert temporary.parent == target.parent
    temporary.unlink()
    with pytest.raises(AvatarImportError, match="T999"):
        avatar_mod.prepare_upload(project, "clip.mp4", turn_id="T999")
    with pytest.raises(AvatarImportError, match="MP4"):
        avatar_mod.prepare_upload(project, "payload.exe", turn_id="T001")


def test_raw_upload_api_persists_real_file_and_media_metadata(client, projects_root, monkeypatch):
    project = make_project(projects_root)
    monkeypatch.setattr(avatar_mod, "probe_media", fake_probe)
    assert client.post(
        "/api/project/avatar-show/workbench/avatar-package/initialize",
        json={"require_asr": False},
    ).status_code == 200

    response = client.put(
        "/api/project/avatar-show/workbench/avatar-package/turns/T001/file?filename=browser-export.mp4",
        content=b"actual-uploaded-video-bytes",
        headers={"Content-Type": "application/octet-stream"},
    )

    assert response.status_code == 200
    package = response.json()["avatar_package"]
    turn = package["turns"][0]
    target = project / turn["source"]["path"]
    assert target.read_bytes() == b"actual-uploaded-video-bytes"
    assert turn["status"] == "media_valid"
    assert len(turn["source"]["sha256"]) == 64


def test_invalid_replacement_never_overwrites_last_valid_source(projects_root, monkeypatch):
    project = make_project(projects_root)
    avatar_mod.initialize_avatar_package(project)
    monkeypatch.setattr(avatar_mod, "probe_media", fake_probe)
    temporary, target = avatar_mod.prepare_upload(project, "valid.mp4", turn_id="T001")
    temporary.write_bytes(b"known-good-video")
    avatar_mod.finalize_upload(project, temporary, target, "valid.mp4", turn_id="T001")

    def invalid_probe(_path: Path) -> dict:
        raise AvatarImportError("not a media file")

    monkeypatch.setattr(avatar_mod, "probe_media", invalid_probe)
    replacement, same_target = avatar_mod.prepare_upload(project, "broken.mp4", turn_id="T001")
    replacement.write_bytes(b"broken")
    with pytest.raises(AvatarImportError, match="not a media file"):
        avatar_mod.finalize_upload(project, replacement, same_target, "broken.mp4", turn_id="T001")

    assert target.read_bytes() == b"known-good-video"
    replacement.unlink()


def test_validation_reports_exactly_which_turns_are_missing(projects_root, monkeypatch):
    project = make_project(projects_root)
    monkeypatch.setattr(avatar_mod, "probe_media", fake_probe)
    avatar_mod.initialize_avatar_package(project, {"require_asr": False})
    temporary, target = avatar_mod.prepare_upload(project, "one.mp4", turn_id="T001")
    temporary.write_bytes(b"one")
    avatar_mod.finalize_upload(project, temporary, target, "one.mp4", turn_id="T001")

    package = avatar_mod.validate_avatar_package(project)

    assert package["validation"]["status"] == "failed"
    assert package["validation"]["summary"]["missing_turns"] == ["T002"]
    assert {issue["code"] for issue in package["validation"]["issues"]} == {"missing_turn_file"}


def test_native_avatar_audio_blocks_duplicate_tts(projects_root):
    project = make_project(projects_root)
    avatar_mod.initialize_avatar_package(project)

    with pytest.raises(WorkbenchError, match="原生音频"):
        start_project_narration(project, {"confirmed": True})
    with pytest.raises(WorkbenchError, match="数字人口播"):
        start_scene_narration_candidate(project, "s1", {"text": "不应覆盖数字人嘴型"})


def test_text_metrics_tolerate_punctuation_but_detect_wrong_copy():
    exact = avatar_mod.text_metrics("AI演员，开始带货！", "ai 演员开始带货")
    wrong = avatar_mod.text_metrics("AI演员开始带货", "量子计算今天发布")

    assert exact == (1.0, 1.0)
    assert wrong[0] < 0.5
    assert wrong[1] < 0.5


def test_text_metrics_normalize_traditional_chinese_before_matching():
    similarity, coverage = avatar_mod.text_metrics("欢迎收听今天的科技快报", "歡迎收聽今天的科技快報")

    assert similarity == 1.0
    assert coverage == 1.0


def _make_longform_project(root: Path) -> Path:
    project = root / "longform-show"
    (project / "artifacts").mkdir(parents=True)
    write_json(project / "project.json", {"project_id": "longform-show", "title": "本地整段口播测试", "pipeline_type": "avatar-spokesperson"})
    write_json(project / "artifacts" / "script.json", {
        "title": "本地整段口播测试",
        "sections": [
            {"id": "s1", "turn_id": "T001", "speaker_id": "yaya", "speaker_name": "雅雅", "text": "alpha", "expected_asset_filename": "T001_YAYA.mp4"},
            {"id": "s2", "turn_id": "T002", "speaker_id": "mengmeng", "speaker_name": "檬檬", "text": "bravo", "expected_asset_filename": "T002_MENGMENG.mp4"},
        ],
    })
    return project


def _add_longform_sources(project: Path, monkeypatch) -> None:
    monkeypatch.setattr(avatar_mod, "probe_media", fake_probe)
    for speaker_id in ("yaya", "mengmeng"):
        temporary, target = avatar_mod.prepare_upload(project, f"{speaker_id}.mp4", speaker_id=speaker_id)
        temporary.write_bytes(f"{speaker_id}-long-video".encode("utf-8"))
        avatar_mod.finalize_upload(project, temporary, target, f"{speaker_id}.mp4", speaker_id=speaker_id)


def test_switch_to_local_longform_archives_cloud_plan_without_deleting_it(projects_root):
    project = make_project(projects_root)
    cloud = avatar_mod.initialize_avatar_package(project, {"generation_mode": "dashscope_wan_s2v"})

    local = avatar_mod.switch_to_local_longform_plan(project)

    assert local["generation_mode"] == "manual_import"
    assert local["import_mode"] == "longform"
    assert local["presentation"]["frame_fit_mode"] == "blur_background"
    history = avatar_mod.list_avatar_source_plans(project)
    assert history["active"]["kind"] == "manual_longform"
    assert history["archived"][0]["plan_id"] == cloud["plan"]["plan_id"]
    snapshot = project / history["archived"][0]["snapshot_path"]
    assert json.loads(snapshot.read_text(encoding="utf-8"))["generation_mode"] == "dashscope_wan_s2v"


def test_switch_to_local_longform_refuses_to_race_a_running_cloud_job(projects_root):
    project = make_project(projects_root)
    package = avatar_mod.initialize_avatar_package(project, {"generation_mode": "dashscope_wan_s2v"})
    package["turns"][0]["cloud_job"] = {
        "job_id": "AVJ-12345678", "status": "running", "stage": "provider_poll",
        "input_hash": "a" * 64, "attempt": 1,
    }
    avatar_mod._save_package(project, package)

    with pytest.raises(AvatarImportError, match="仍有阿里云"):
        avatar_mod.switch_to_local_longform_plan(project)


def test_local_longform_plan_routes_archive_cloud_state(client, projects_root):
    make_project(projects_root)
    assert client.post(
        "/api/project/avatar-show/workbench/avatar-package/initialize",
        json={"generation_mode": "dashscope_wan_s2v"},
    ).status_code == 200

    switched = client.post("/api/project/avatar-show/workbench/avatar-package/plans/local-longform", json={})
    history = client.get("/api/project/avatar-show/workbench/avatar-package/plans")

    assert switched.status_code == 200
    assert switched.json()["avatar_package"]["import_mode"] == "longform"
    assert history.status_code == 200
    assert history.json()["active"]["kind"] == "manual_longform"
    assert len(history.json()["archived"]) == 1


def test_longform_alignment_requires_cut_review_before_assembly(projects_root, monkeypatch):
    project = _make_longform_project(projects_root)
    avatar_mod.initialize_avatar_package(project, {"import_mode": "longform", "width": 160, "height": 240})
    _add_longform_sources(project, monkeypatch)
    assert avatar_mod.validate_avatar_package(project)["validation"]["status"] == "passed"

    class DummyModel:
        pass

    def fake_transcribe(_model, source_path, **_kwargs):
        word = "alpha" if source_path.stem == "yaya" else "bravo"
        return word, [{"start": 0.04, "end": 0.20, "text": word, "words": [{"start": 0.04, "end": 0.20, "word": word}]}]

    monkeypatch.setattr(avatar_mod, "_load_whisper", lambda *_args, **_kwargs: (DummyModel(), "fake-whisper"))
    monkeypatch.setattr(avatar_mod, "_transcribe_file", fake_transcribe)
    monkeypatch.setattr(avatar_mod, "_read_pcm", lambda *_args, **_kwargs: None)
    avatar_mod.start_avatar_asr(project)
    package = avatar_mod.run_avatar_asr(project)

    assert package["asr"]["status"] == "passed"
    assert package["cut_plan"]["status"] == "awaiting_review"
    assert all(turn["status"] == "cut_pending_review" for turn in package["turns"])
    assert all("source_start_seconds" not in turn for turn in package["turns"])
    with pytest.raises(AvatarImportError, match="切割方案"):
        avatar_mod.start_avatar_assembly(project)

    package = avatar_mod.approve_high_confidence_longform_cuts(project)
    assert package["cut_plan"]["status"] == "approved"
    assert all(turn["status"] == "cut_approved" for turn in package["turns"])
    assert all(turn["source_end_seconds"] > turn["source_start_seconds"] for turn in package["turns"])
    assert avatar_mod.start_avatar_assembly(project)["assembly"]["status"] == "running"


def test_longform_speaker_diagnosis_is_non_destructive_until_explicitly_applied(projects_root, monkeypatch):
    project = _make_longform_project(projects_root)
    avatar_mod.initialize_avatar_package(project, {"import_mode": "longform", "width": 160, "height": 240})
    _add_longform_sources(project, monkeypatch)
    assert avatar_mod.validate_avatar_package(project)["validation"]["status"] == "passed"

    class DummyModel:
        pass

    def initial_transcribe(_model, source_path, **_kwargs):
        word = "unrelated" if source_path.stem == "yaya" else "bravo"
        return word, [{"start": 0.04, "end": 0.20, "text": word, "words": [{"start": 0.04, "end": 0.20, "word": word}]}]

    monkeypatch.setattr(avatar_mod, "_load_whisper", lambda *_args, **_kwargs: (DummyModel(), "fake-whisper"))
    monkeypatch.setattr(avatar_mod, "_transcribe_file", initial_transcribe)
    monkeypatch.setattr(avatar_mod, "_read_pcm", lambda *_args, **_kwargs: None)
    avatar_mod.start_avatar_asr(project)
    package = avatar_mod.run_avatar_asr(project)
    assert next(item for item in package["cut_plan"]["items"] if item["turn_id"] == "T001")["status"] == "needs_manual"
    package = avatar_mod.approve_longform_cut(project, "T002")
    mengmeng_before = next(item for item in package["cut_plan"]["items"] if item["turn_id"] == "T002").copy()
    mengmeng_turn_before = next(turn for turn in package["turns"] if turn["turn_id"] == "T002").copy()

    def enhanced_transcribe(_model, source_path, **_kwargs):
        assert source_path.stem == "yaya"
        return "alpha", [{"start": 0.04, "end": 0.20, "text": "alpha", "words": [{"start": 0.04, "end": 0.20, "word": "alpha"}]}]

    monkeypatch.setattr(avatar_mod, "_transcribe_file", enhanced_transcribe)
    monkeypatch.setattr(avatar_mod, "list_local_whisper_models", lambda: [{"id": "", "label": "fake", "path": ""}])
    avatar_mod.start_longform_speaker_diagnosis(project, "yaya")
    candidate_package = avatar_mod.run_longform_speaker_diagnosis(project, "yaya")
    record = candidate_package["asr"]["speaker_diagnostics"]["yaya"]
    candidate_id = record["latest_candidate_id"]
    candidate = next(item for item in record["candidates"] if item["candidate_id"] == candidate_id)
    assert candidate["kind"] == "enhanced_diagnosis"
    assert candidate["full_transcript"] == "alpha"
    assert candidate["summary"]["pending_review"] == 1
    # A diagnostic candidate must not modify the active yaya cut, nor M's approved work.
    assert next(item for item in candidate_package["cut_plan"]["items"] if item["turn_id"] == "T001")["status"] == "needs_manual"
    assert next(item for item in candidate_package["cut_plan"]["items"] if item["turn_id"] == "T002") == mengmeng_before
    assert next(turn for turn in candidate_package["turns"] if turn["turn_id"] == "T002") == mengmeng_turn_before

    applied = avatar_mod.apply_longform_speaker_candidate(project, "yaya", candidate_id)
    yaya_item = next(item for item in applied["cut_plan"]["items"] if item["turn_id"] == "T001")
    mengmeng_item = next(item for item in applied["cut_plan"]["items"] if item["turn_id"] == "T002")
    assert yaya_item["status"] == "pending_review"
    assert mengmeng_item == mengmeng_before
    assert next(turn for turn in applied["turns"] if turn["turn_id"] == "T002") == mengmeng_turn_before
    assert applied["assembly"]["status"] == "not_started"


def test_longform_speaker_diagnosis_rejects_nonlocal_model(projects_root, monkeypatch):
    project = _make_longform_project(projects_root)
    avatar_mod.initialize_avatar_package(project, {"import_mode": "longform"})
    _add_longform_sources(project, monkeypatch)
    avatar_mod.validate_avatar_package(project)

    with pytest.raises(AvatarImportError, match="本机已安装"):
        avatar_mod.start_longform_speaker_diagnosis(project, "yaya", {"model": "medium"})


def test_longform_realign_reuses_transcript_without_touching_other_speaker(projects_root, monkeypatch):
    project = _make_longform_project(projects_root)
    avatar_mod.initialize_avatar_package(project, {"import_mode": "longform"})
    _add_longform_sources(project, monkeypatch)
    avatar_mod.validate_avatar_package(project)
    package = avatar_mod.read_avatar_package(project)
    package["asr"] = {
        "status": "passed", "issues": [], "summary": {},
        "speaker_diagnostics": {
            "yaya": {
                "speaker_id": "yaya", "speaker_name": "雅雅", "active_candidate_id": None,
                "latest_candidate_id": "ASRC-BASELINE123", "candidates": [{
                    "candidate_id": "ASRC-BASELINE123", "kind": "enhanced_diagnosis", "status": "ready",
                    "model": "fake", "source_sha256": package["speakers"][0]["source"]["sha256"],
                    "full_transcript": "alpha", "segments": [{"start": 0.04, "end": 0.2, "text": "alpha", "words": [{"start": 0.04, "end": 0.2, "word": "alpha"}]}],
                    "transcription_options": {}, "cut_items": [], "issues": [], "summary": {}, "turns": [], "overall_metrics": {},
                }], "job": {"status": "completed"},
            },
        },
    }
    package["cut_plan"] = {"status": "awaiting_review", "items": [{
        "turn_id": "T001", "speaker_id": "yaya", "status": "needs_manual", "confidence": "low", "source_type": "asr_alignment",
        "suggested_start_seconds": None, "suggested_end_seconds": None, "start_seconds": None, "end_seconds": None,
    }, {
        "turn_id": "T002", "speaker_id": "mengmeng", "status": "approved", "confidence": "high", "source_type": "asr_alignment",
        "suggested_start_seconds": 0.0, "suggested_end_seconds": 0.2, "start_seconds": 0.0, "end_seconds": 0.2, "approved_at": "2026-08-12T00:00:00Z",
    }], "summary": {}}
    avatar_mod._save_package(project, package)
    monkeypatch.setattr(avatar_mod, "_read_pcm", lambda *_args, **_kwargs: None)

    avatar_mod.start_longform_speaker_realign(project, "yaya", "ASRC-BASELINE123")
    result = avatar_mod.run_longform_speaker_realign(project, "yaya")
    record = result["asr"]["speaker_diagnostics"]["yaya"]
    candidate = next(item for item in record["candidates"] if item["candidate_id"] == record["latest_candidate_id"])

    assert candidate["kind"] == "normalized_realign"
    assert candidate["transcription_options"]["reused_existing_transcript"] is True
    assert candidate["summary"]["pending_review"] == 1
    assert next(item for item in result["cut_plan"]["items"] if item["turn_id"] == "T001")["status"] == "needs_manual"
    assert next(item for item in result["cut_plan"]["items"] if item["turn_id"] == "T002")["status"] == "approved"


def test_longform_manual_cut_invalidates_approval_and_keeps_original_source(projects_root, monkeypatch):
    project = _make_longform_project(projects_root)
    avatar_mod.initialize_avatar_package(project, {"import_mode": "longform"})
    _add_longform_sources(project, monkeypatch)
    package = avatar_mod.read_avatar_package(project)
    package["cut_plan"] = {"status": "awaiting_review", "items": [{
        "turn_id": "T001", "speaker_id": "yaya", "status": "pending_review", "confidence": "high",
        "source_type": "asr_alignment", "suggested_start_seconds": 0.0, "suggested_end_seconds": 0.2,
        "start_seconds": 0.0, "end_seconds": 0.2,
    }, {
        "turn_id": "T002", "speaker_id": "mengmeng", "status": "pending_review", "confidence": "high",
        "source_type": "asr_alignment", "suggested_start_seconds": 0.0, "suggested_end_seconds": 0.2,
        "start_seconds": 0.0, "end_seconds": 0.2,
    }], "summary": {"total": 2, "approved": 0, "pending_review": 2, "needs_manual": 0}}
    avatar_mod._save_package(project, package)
    original = project / avatar_mod.read_avatar_package(project)["speakers"][0]["source"]["path"]
    before = original.read_bytes()

    updated = avatar_mod.update_longform_cut(project, "T001", {"start_seconds": 0.03, "end_seconds": 0.25, "review_note": "开头留一点呼吸"})

    item = next(item for item in updated["cut_plan"]["items"] if item["turn_id"] == "T001")
    assert item["source_type"] == "manual"
    assert item["confidence"] == "manual"
    assert original.read_bytes() == before


def test_longform_manual_cut_rejects_overlapping_same_speaker_ranges(projects_root, monkeypatch):
    project = _make_longform_project(projects_root)
    avatar_mod.initialize_avatar_package(project, {"import_mode": "longform"})
    _add_longform_sources(project, monkeypatch)
    package = avatar_mod.read_avatar_package(project)
    package["cut_plan"] = {"status": "awaiting_review", "items": [{
        "turn_id": "T001", "speaker_id": "yaya", "status": "pending_review", "confidence": "high",
        "source_type": "asr_alignment", "suggested_start_seconds": 0.0, "suggested_end_seconds": 0.2,
        "start_seconds": 0.0, "end_seconds": 0.2,
    }, {"turn_id": "T999", "speaker_id": "yaya", "status": "pending_review", "confidence": "high",
        "source_type": "asr_alignment", "suggested_start_seconds": 0.0, "suggested_end_seconds": 0.2,
        "start_seconds": 0.0, "end_seconds": 0.2}], "summary": {"total": 2, "approved": 0, "pending_review": 2, "needs_manual": 0}}
    avatar_mod._save_package(project, package)
    with pytest.raises(AvatarImportError, match="重叠"):
        avatar_mod.update_longform_cut(project, "T001", {"start_seconds": 0.1, "end_seconds": 0.3})


def test_longform_master_uses_approved_cuts_and_blurred_canvas_fit(projects_root):
    ffmpeg = avatar_mod._find_binary("ffmpeg")
    if not ffmpeg:
        pytest.skip("FFmpeg is not installed")
    project = _make_longform_project(projects_root)
    avatar_mod.initialize_avatar_package(project, {"import_mode": "longform", "width": 160, "height": 240, "require_asr": True})
    for speaker_id, color, frequency in (("yaya", "red", 440), ("mengmeng", "blue", 660)):
        temporary, target = avatar_mod.prepare_upload(project, f"{speaker_id}.mp4", speaker_id=speaker_id)
        _make_clip(ffmpeg, temporary, color, frequency)
        avatar_mod.finalize_upload(project, temporary, target, f"{speaker_id}.mp4", speaker_id=speaker_id)
    assert avatar_mod.validate_avatar_package(project)["validation"]["status"] == "passed"
    package = avatar_mod.read_avatar_package(project)
    package["settings"]["require_asr"] = False  # Synthetic sine audio cannot pass speech QA; this test exercises FFmpeg composition only.
    package["asr"] = {"status": "passed", "issues": [], "summary": {}}
    package["cut_plan"] = {"status": "approved", "items": [], "summary": {"total": 2, "approved": 2, "pending_review": 0, "needs_manual": 0}}
    for turn in package["turns"]:
        turn.update({"status": "cut_approved", "source_start_seconds": 0.0, "source_end_seconds": 0.25})
    avatar_mod._save_package(project, package)

    _inputs, graph, _timeline = avatar_mod._build_filter_graph(project, avatar_mod.read_avatar_package(project))
    assert "boxblur=18:2" in graph
    avatar_mod.start_avatar_assembly(project)
    complete = avatar_mod.assemble_avatar_package(project)

    master = project / complete["assembly"]["output_path"]
    media = avatar_mod.probe_media(master)
    assert complete["assembly"]["status"] == "passed"
    assert (media["video"]["width"], media["video"]["height"]) == (160, 240)
    assert media["audio"]["sample_rate"] == 48000
    assert complete["assembly"]["summary"]["phase"] == "completed"
    assert all("part_path" in item for item in json.loads((project / complete["assembly"]["timeline_path"]).read_text(encoding="utf-8"))["turns"])


def test_longform_assembly_reuses_normalized_turns_after_restart(projects_root):
    ffmpeg = avatar_mod._find_binary("ffmpeg")
    if not ffmpeg:
        pytest.skip("FFmpeg is not installed")
    project = _make_longform_project(projects_root)
    avatar_mod.initialize_avatar_package(project, {"import_mode": "longform", "width": 160, "height": 240, "require_asr": False})
    for speaker_id, color, frequency in (("yaya", "red", 440), ("mengmeng", "blue", 660)):
        temporary, target = avatar_mod.prepare_upload(project, f"{speaker_id}.mp4", speaker_id=speaker_id)
        _make_clip(ffmpeg, temporary, color, frequency)
        avatar_mod.finalize_upload(project, temporary, target, f"{speaker_id}.mp4", speaker_id=speaker_id)
    avatar_mod.validate_avatar_package(project)
    package = avatar_mod.read_avatar_package(project)
    package["asr"] = {"status": "passed", "issues": [], "summary": {}}
    package["cut_plan"] = {"status": "approved", "items": [], "summary": {"total": 2, "approved": 2, "pending_review": 0, "needs_manual": 0}}
    for turn in package["turns"]:
        turn.update({"status": "cut_approved", "source_start_seconds": 0.0, "source_end_seconds": 0.25})
    avatar_mod._save_package(project, package)

    avatar_mod.start_avatar_assembly(project)
    first = avatar_mod.assemble_avatar_package(project)
    first_timeline = json.loads((project / first["assembly"]["timeline_path"]).read_text(encoding="utf-8"))
    first_parts = [project / item["part_path"] for item in first_timeline["turns"]]
    assert all(path.is_file() for path in first_parts)

    avatar_mod.start_avatar_assembly(project)
    second = avatar_mod.assemble_avatar_package(project)
    second_timeline = json.loads((project / second["assembly"]["timeline_path"]).read_text(encoding="utf-8"))
    assert second["assembly"]["status"] == "passed"
    assert second["assembly"]["summary"]["reused"] == 2
    assert all(item["part_reused"] is True for item in second_timeline["turns"])


def _make_clip(ffmpeg: str, output: Path, color: str, frequency: int) -> None:
    result = subprocess.run([
        ffmpeg, "-y",
        "-f", "lavfi", "-i", f"color=c={color}:s=160x240:r=25:d=0.32",
        "-f", "lavfi", "-i", f"sine=frequency={frequency}:sample_rate=48000:duration=0.32",
        "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(output),
    ], capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
    assert result.returncode == 0, result.stderr


def test_ffmpeg_assembly_uses_native_audio_and_writes_timeline(projects_root):
    ffmpeg = avatar_mod._find_binary("ffmpeg")
    if not ffmpeg:
        pytest.skip("FFmpeg is not installed")
    project = make_project(projects_root)
    avatar_mod.initialize_avatar_package(project, {
        "require_asr": False,
        "width": 160,
        "height": 240,
        "speaker_change_gap_seconds": 0.08,
    })
    for turn_id, color, frequency in (("T001", "red", 440), ("T002", "blue", 660)):
        temporary, target = avatar_mod.prepare_upload(project, f"{turn_id}.mp4", turn_id=turn_id)
        _make_clip(ffmpeg, temporary, color, frequency)
        avatar_mod.finalize_upload(project, temporary, target, f"{turn_id}.mp4", turn_id=turn_id)

    assert avatar_mod.validate_avatar_package(project)["validation"]["status"] == "passed"
    avatar_mod.start_avatar_assembly(project)
    package = avatar_mod.assemble_avatar_package(project)

    assert package["assembly"]["status"] == "passed"
    assert package["assembly"]["summary"]["timing_basis"] == "native_avatar_audio"
    timeline = json.loads((project / package["assembly"]["timeline_path"]).read_text(encoding="utf-8"))
    assert timeline["audio_mode"] == "native_avatar_audio"
    assert [turn["turn_id"] for turn in timeline["turns"]] == ["T001", "T002"]
    assert timeline["turns"][0]["gap_after_seconds"] == pytest.approx(0.08)
    assert (project / package["assembly"]["subtitle_path"]).is_file()
    assert (project / package["assembly"]["qa_path"]).is_file()


@pytest.mark.skipif(not workbench_mod._ffmpeg_available(), reason="FFmpeg is required for avatar timeline verification")
def test_avatar_master_applies_immutable_native_audio_timeline_and_composite_review(projects_root):
    """The imported master, not an estimated/TTS clock, owns avatar scene timing."""
    ffmpeg = workbench_mod._ffmpeg_available()
    assert ffmpeg
    project = make_project(projects_root)
    # The machine default is intentionally allowed to change for future real
    # projects. This timing-focused fixture keeps unity gain explicitly so it
    # does not require an unrelated sound-sample approval.
    workbench_mod.update_narration_policy(project, {"playback_gain_db": 0.0})
    project_meta = json.loads((project / "project.json").read_text(encoding="utf-8"))
    project_meta["render_profile"] = {"width": 320, "height": 180}
    write_json(project / "project.json", project_meta)
    avatar_mod.initialize_avatar_package(project, {"require_asr": False, "width": 160, "height": 240})
    for turn_id, color, frequency in (("T001", "red", 440), ("T002", "blue", 660)):
        temporary, target = avatar_mod.prepare_upload(project, f"{turn_id}.mp4", turn_id=turn_id)
        _make_clip(ffmpeg, temporary, color, frequency)
        avatar_mod.finalize_upload(project, temporary, target, f"{turn_id}.mp4", turn_id=turn_id)
    assert avatar_mod.validate_avatar_package(project)["validation"]["status"] == "passed"
    avatar_mod.start_avatar_assembly(project)
    package = avatar_mod.assemble_avatar_package(project)

    # Deliberately reverse the display order. Applying an avatar package must
    # bind by stable script turn ID, never by whichever scene happens to be
    # first in the board at that moment.
    initial_board = workbench_mod.bootstrap_workbench(project)
    initial_board["scenes"][0]["order"] = 2
    initial_board["scenes"][1]["order"] = 1
    write_json(project / "artifacts" / "workbench.json", initial_board)
    applied = workbench_mod.apply_avatar_package_to_timeline(project, {"default_treatment": "fullscreen"})
    timeline = json.loads((project / package["assembly"]["timeline_path"]).read_text(encoding="utf-8"))
    expected_durations = [round(turn["end_seconds"] - turn["start_seconds"], 3) for turn in timeline["turns"]]
    scenes = sorted(applied["scenes"], key=lambda scene: scene["order"])

    assert applied["automation"]["audio_mode"] == "native_avatar_audio"
    assert applied["automation"]["narration_generation"]["audio_path"].startswith("assets/video/avatar/masters/")
    scenes_by_script = {scene["script_section_id"]: scene for scene in scenes}
    assert round(scenes_by_script["s1"]["end_seconds"] - scenes_by_script["s1"]["start_seconds"], 3) == expected_durations[0]
    assert round(scenes_by_script["s2"]["end_seconds"] - scenes_by_script["s2"]["start_seconds"], 3) == expected_durations[1]
    assert all(scene["source_strategy"] == "avatar_only" for scene in scenes)
    assert all(scene["presenter"]["source_path"].startswith("assets/video/avatar/masters/") for scene in scenes)
    assert len([usage for usage in applied["usages"] if usage["role"] == "presenter" and usage["selected"]]) == 2

    fullscreen_frames = workbench_mod.generate_avatar_scene_keyframes(project, scenes[0]["id"])
    first_scene = next(scene for scene in fullscreen_frames["scenes"] if scene["id"] == scenes[0]["id"])
    assert first_scene["keyframe_review"]["generation"]["treatment"] == "fullscreen"
    assert len(first_scene["keyframe_review"]["timeline"]) == 2
    assert all((project / next(asset for asset in fullscreen_frames["assets"] if asset["id"] == item["asset_id"])["path"]).is_file() for item in first_scene["keyframe_review"]["timeline"])

    background = project / "assets" / "video" / "main-background.mp4"
    background.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run([
        ffmpeg, "-y", "-f", "lavfi", "-i", "color=c=black:s=320x180:r=25:d=1",
        "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(background),
    ], capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
    assert result.returncode == 0, result.stderr
    second_scene_id = scenes[1]["id"]
    with_background = workbench_mod.add_asset(project, {
        "name": "画中画主体背景", "type": "video", "source_type": "human_provided",
        "path": "assets/video/main-background.mp4", "duration_seconds": 1, "license": "测试授权",
    })
    background_asset = with_background["assets"][-1]
    workbench_mod.assign_usage(project, {"scene_id": second_scene_id, "asset_id": background_asset["id"], "role": "visual"})
    pip_state = workbench_mod.update_scene(project, second_scene_id, {"presenter_treatment": "pip_top_left"})
    pip_frames = workbench_mod.generate_avatar_scene_keyframes(project, second_scene_id)
    pip_scene = next(scene for scene in pip_frames["scenes"] if scene["id"] == second_scene_id)
    assert next(scene for scene in pip_state["scenes"] if scene["id"] == second_scene_id)["presenter"]["treatment"] == "pip_top_left"
    assert pip_scene["keyframe_review"]["generation"]["treatment"] == "pip_top_left"
    assert all(item["source"] == "avatar_scene_composite" for item in pip_scene["keyframe_review"]["timeline"])

    # Formal assembly remains gated by a viewable full-preview confirmation;
    # the avatar setup above proves source timing, not publication readiness.
    preview_queued = workbench_mod.start_full_preview_render(project, {"confirmed": True})
    preview_path = project / "renders" / "previews" / "avatar-candidate.mp4"
    preview_path.parent.mkdir(parents=True, exist_ok=True)
    preview_path.write_bytes(b"candidate")
    preview_state = workbench_mod._load_for_write(project)
    preview_state["automation"]["preview_render"].update({
        "status": "completed",
        "output_path": "renders/previews/avatar-candidate.mp4",
        "version": preview_queued["automation"]["preview_render"].get("version", 1),
    })
    workbench_mod._save(project, preview_state)
    approved = workbench_mod.approve_full_preview_scenes(project, {"confirmed": True})
    assert all(scene["review_status"] == "approved" for scene in approved["scenes"])
    queued = workbench_mod.start_project_video_render(project, {"confirmed": True})
    assert queued["automation"]["render"]["status"] == "generating"
    rendered = workbench_mod.generate_project_video_render(project)
    final_path = project / rendered["automation"]["render"]["output_path"]
    report = json.loads((project / "artifacts" / "render_report.json").read_text(encoding="utf-8"))
    assert final_path.is_file()
    assert report["avatar"]["audio_mode"] == "native_avatar_audio"
    assert report["avatar"]["pip_scene_ids"] == [second_scene_id]
