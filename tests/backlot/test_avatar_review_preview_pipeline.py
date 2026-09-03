"""Focused contract tests for the paid, long-form avatar parent job."""

from __future__ import annotations

import hashlib
import json
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest

from backlot import avatar_import as avatar_mod
from backlot import avatar_review_preview_pipeline as pipeline
from backlot import workbench as wb


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def make_project(root: Path) -> tuple[Path, dict]:
    project = root / "avatar-one-click"
    project.mkdir()
    script = {
        "title": "双主持测试",
        "sections": [
            {
                "id": "s1", "turn_id": "T001", "speaker_id": "yaya", "speaker_name": "雅雅",
                "text": "欢迎收看今天的科技快报。", "start_seconds": 0, "end_seconds": 2,
            },
            {
                "id": "s2", "turn_id": "T002", "speaker_id": "mengmeng", "speaker_name": "檬檬",
                "text": "下面进入今天的重点消息。", "start_seconds": 2, "end_seconds": 4,
            },
        ],
    }
    write_json(project / "project.json", {
        "project_id": project.name,
        "title": "双主持测试",
        "pipeline_type": "avatar-spokesperson",
        "render_profile": {"width": 320, "height": 568, "fps": 30},
    })
    write_json(project / "artifacts" / "script.json", script)
    state = wb.bootstrap_workbench(project)
    state["project"]["script_draft"] = {
        "status": "approved", "script": script, "script_hash": pipeline._json_hash(script),
    }
    wb._save(project, state)
    return project, script


@pytest.fixture
def prepared(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, dict]:
    project, script = make_project(tmp_path)
    image_dir = tmp_path / "roles"
    image_dir.mkdir()
    images = {"yaya": image_dir / "雅雅.png", "mengmeng": image_dir / "檬檬.png"}
    for index, path in enumerate(images.values(), 1):
        path.write_bytes((f"role-{index}" * 100).encode("utf-8"))
    profiles = {
        "yaya": {"id": "voice-yaya", "name": "雅雅"},
        "mengmeng": {"id": "voice-mengmeng", "name": "檬檬"},
    }
    asr = {
        "status": "passed", "model_id": "faster-whisper-small", "snapshot_revision": "rev-test",
        "fingerprint": "asr-fingerprint", "model_size_bytes": 10_000_000,
        "faster_whisper_version": "1.2.1", "ctranslate2_version": "4.8.1",
        "device": "cpu", "compute_type": "int8", "language": "zh", "local_only": True,
        "load_tested": False,
    }
    runninghub = {
        "ready": True, "provider": "RunningHub", "workflow_id": pipeline.PRODUCTION_WORKFLOW_ID,
        "workflow_profile": pipeline.PRODUCTION_WORKFLOW_PROFILE, "resolution": "448x560",
        "fps": 25, "frame_clock": "final_pcm_samples_exact", "instance_type": "default",
        "instance_label": "Standard 24GB", "plus_allowed": False, "max_concurrency": 1, "issues": [],
    }
    capabilities = {
        "tts": {"available": True, "status": "available"},
        "ffmpeg": {"available": True}, "ffprobe": {"available": True},
        "pexels": {"available": True}, "text_ai": {"available": True, "model": "fake-text"},
        "hyperframes": {"available": True, "status": "available"},
    }
    presenter_bindings = {
        role: {
            "role_id": f"AR-{role}-test", "role_name": label,
            "voice_profile_id": profiles[role]["id"], "voice_profile_name": profiles[role]["name"],
            "presenter_path": str(images[role]), "presenter_filename": images[role].name,
            "presenter_sha256": hashlib.sha256(images[role].read_bytes()).hexdigest(),
        }
        for role, label in pipeline.ROLE_LABELS.items()
    }
    monkeypatch.setattr(
        pipeline,
        "_resolve_presenter_images",
        lambda *_args, **_kwargs: (images, presenter_bindings),
    )
    monkeypatch.setattr(pipeline, "_voicebox_profiles", lambda: profiles)
    monkeypatch.setattr(
        pipeline,
        "_avatar_voice_profiles",
        lambda _payload=None, *, roles=None: {
            role: profiles[role] for role in (roles if roles is not None else profiles)
        },
    )
    monkeypatch.setattr(pipeline, "preflight_local_whisper", lambda *args, **kwargs: {**asr, "load_tested": bool(kwargs.get("load_test"))})
    monkeypatch.setattr(pipeline, "list_local_whisper_models", lambda: [{"id": str(tmp_path / "rev-test"), "label": "faster-whisper-small"}])
    monkeypatch.setattr(
        pipeline,
        "_runninghub_preflight",
        lambda *, allow_plus_on_oom=False: {
            **runninghub,
            "plus_allowed": bool(allow_plus_on_oom),
            "plus_fallback_only": True,
            "plus_instance_type": "plus" if allow_plus_on_oom else None,
            "plus_instance_label": "Plus 48GB" if allow_plus_on_oom else None,
            "recovery_sequence": ["default", "default", "plus"] if allow_plus_on_oom else ["default", "default"],
        },
    )
    monkeypatch.setattr(pipeline, "collect_review_preview_capabilities", lambda **_kwargs: capabilities)
    return project, script


def test_role_binding_materializes_presenter_images_into_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = tmp_path / "role-binding"
    project.mkdir()
    library = tmp_path / "library"
    library.mkdir()
    profiles = {role: {"id": f"voice-{role}", "name": label} for role, label in pipeline.ROLE_LABELS.items()}
    source_images = {}
    roles_by_voice = {}
    for role, label in pipeline.ROLE_LABELS.items():
        source = library / f"{role}.png"
        source.write_bytes((role * 150).encode("utf-8"))
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        avatar_role = {
            "role_id": f"AR-{role}-binding", "name": label,
            "references": [{"slot": "front", "path": f"assets/{role}.png", "sha256": digest}],
        }
        source_images[avatar_role["role_id"]] = source
        roles_by_voice[profiles[role]["id"]] = avatar_role
    monkeypatch.setattr(pipeline, "find_avatar_role_by_voice_profile", lambda profile_id: roles_by_voice.get(profile_id))
    monkeypatch.setattr(pipeline, "role_front_reference", lambda role: role["references"][0])
    monkeypatch.setattr(pipeline, "avatar_role_asset_file", lambda role_id, _path: source_images[role_id])

    images, bindings = pipeline._resolve_presenter_images(project, profiles, refresh_from_role_library=True)

    assert all(path.is_file() and project in path.parents for path in images.values())
    assert {binding["role_name"] for binding in bindings.values()} == {"雅雅", "檬檬"}
    assert (project / "artifacts" / "avatar-review-presenter-bindings.json").is_file()
    _, frozen = pipeline._resolve_presenter_images(project, profiles, refresh_from_role_library=False)
    assert frozen == bindings


def test_role_binding_reports_missing_audio_center_association(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = tmp_path / "missing-role-binding"
    project.mkdir()
    monkeypatch.setattr(pipeline, "find_avatar_role_by_voice_profile", lambda _profile_id: None)
    with pytest.raises(pipeline.AvatarReviewPreviewError, match="配音中心尚未为雅雅当前音色"):
        pipeline._resolve_presenter_images(
            project,
            {"yaya": {"id": "voice-yaya", "name": "雅雅"}, "mengmeng": {"id": "voice-mengmeng", "name": "檬檬"}},
            refresh_from_role_library=True,
        )


def test_complete_cloud_role_bindings_are_selected_without_local_tts(monkeypatch: pytest.MonkeyPatch) -> None:
    profiles = {
        "doubao:public_female": {
            "id": "doubao:public_female", "name": "豆包公版女声", "provider_id": "doubao",
            "provider_name": "豆包云端配音", "voice_signature": "sig-female", "available": True,
        },
        "doubao:public_male": {
            "id": "doubao:public_male", "name": "豆包公版男声", "provider_id": "doubao",
            "provider_name": "豆包云端配音", "voice_signature": "sig-male", "available": True,
        },
    }
    roles = [
        {
            "role_id": "AR-yaya-cloud", "name": "雅雅",
            "voice_binding": {"profile_id": "doubao:public_female"},
            "references": [{"slot": "front", "path": "assets/yaya.png", "sha256": "y"}],
        },
        {
            "role_id": "AR-mengmeng-cloud", "name": "檬檬",
            "voice_binding": {"profile_id": "doubao:public_male"},
            "references": [{"slot": "front", "path": "assets/mengmeng.png", "sha256": "m"}],
        },
    ]
    monkeypatch.setattr(pipeline, "list_avatar_roles", lambda: {"roles": roles})
    monkeypatch.setattr(pipeline, "get_voice_profile", lambda profile_id: profiles.get(profile_id))
    monkeypatch.setattr(
        pipeline,
        "_voicebox_profiles",
        lambda: pytest.fail("complete cloud bindings must not start or inspect local TTS"),
    )

    selected = pipeline._avatar_voice_profiles()

    assert selected["yaya"]["id"] == "doubao:public_female"
    assert selected["mengmeng"]["id"] == "doubao:public_male"


class FakeTTS:
    def execute(self, inputs: dict) -> SimpleNamespace:
        target = Path(inputs["output_path"])
        target.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(target), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(16_000)
            output.writeframes(b"\x01\x00" * 16_000)
        return SimpleNamespace(success=True, error=None)


class FakeRunningHub:
    def __init__(self) -> None:
        self.submits: list[dict] = []

    def upload_file(self, path: Path, *, file_type: str) -> str:
        assert path.is_file()
        return f"remote-{file_type}-{path.name}"

    def submit(self, **payload: str) -> dict:
        assert payload["instance_type"] == "default"
        self.submits.append(payload)
        return {"task_id": f"RH-{len(self.submits)}"}

    def poll(self, task_id: str) -> dict:
        return {
            "status": "SUCCEEDED", "video_url": f"https://example.invalid/{task_id}.mp4",
            "consume_money_cny": 0.2,
            "billing": {"observed_instance": "standard_24gb"},
        }

    def download(self, _url: str, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"avatar" * 1000)


class SequencedRunningHub(FakeRunningHub):
    """A fully local RunningHub double with one terminal result per submit."""

    def __init__(self, results: list[dict]) -> None:
        super().__init__()
        self.results = results
        self.uploads: list[tuple[str, str]] = []

    def upload_file(self, path: Path, *, file_type: str) -> str:
        self.uploads.append((file_type, path.name))
        return super().upload_file(path, file_type=file_type)

    def submit(self, **payload: object) -> dict:
        self.submits.append(dict(payload))
        return {"task_id": f"RH-{len(self.submits)}"}

    def poll(self, task_id: str) -> dict:
        submit_index = int(task_id.rsplit("-", 1)[-1]) - 1
        if submit_index < len(self.results):
            return self.results[submit_index]
        requested = str(self.submits[submit_index]["instance_type"])
        return {
            "status": "SUCCEEDED",
            "video_url": f"https://example.invalid/{task_id}.mp4",
            "consume_money_cny": 0.2,
            "billing": {
                "observed_instance": "plus_48gb" if requested == "plus" else "standard_24gb",
            },
        }


def structured_oom_result(*, observed_instance: str = "standard_24gb") -> dict:
    return {
        "status": "FAILED",
        "error": "workflow execution failed",
        "failure_details": {
            "exception_type": "torch.cuda.OutOfMemoryError",
            "exception_message": "CUDA out of memory while allocating tensor",
            "node_name": "InfiniteTalkSampler",
        },
        "consume_money_cny": 0.1,
        "billing": {"observed_instance": observed_instance},
    }


def completed_parent_overrides(client: FakeRunningHub) -> dict:
    return {
        "tts_factory": FakeTTS,
        "runninghub_client_factory": lambda: client,
        "poll_interval": 0,
        "visual_runner": lambda *_args: {"completed_slots": 2, "failed_slots": 0},
        "preview_runner": lambda *_args: {
            "preview_path": "renders/full-preview/avatar-review.mp4",
            "report_path": "artifacts/avatar-review-report.json",
            "preview_sha256": "preview-hash",
            "preview_size_bytes": 12345,
        },
    }


def fake_media(path: Path) -> dict:
    return {
        "duration_seconds": 1.28, "size_bytes": path.stat().st_size,
        "video": {
            "present": path.suffix.lower() != ".wav", "width": 448, "height": 560,
            "fps": 25.0, "average_fps": 25.0, "frame_count": 32,
            "duration_seconds": 1.28,
        },
        "audio": {"present": True, "sample_rate": 16_000, "channels": 1, "duration_seconds": 1.28},
    }


def patch_successful_local_stages(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pipeline, "probe_media", fake_media)
    monkeypatch.setattr(pipeline, "_align_avatar_package", lambda *_args: {
        "asr": {"status": "passed"},
        "cut_plan": {"status": "approved", "summary": {"approved": 2, "needs_manual": 0}},
    })
    monkeypatch.setattr(pipeline, "_assemble_and_apply", lambda *_args: {
        "assembly": {"status": "passed"}, "timeline_revision": 1, "scene_count": 2,
    })


def test_preflight_and_start_freeze_two_roles_without_leaking_worker(prepared: tuple[Path, dict]) -> None:
    project, _script = prepared
    preflight = pipeline.avatar_review_preview_preflight(project, {"visual": {"planning_mode": "ai_director"}})
    assert preflight["ready"] is True
    assert preflight["speaker_count"] == 2
    assert preflight["avatar_contract"]["instance_type"] == "default"
    assert preflight["avatar_contract"]["plus_allowed"] is False
    assert preflight["avatar_contract"]["recovery_sequence"] == ["default", "default"]
    assert preflight["avatar_recovery"] == {
        "version": "runninghub-oom-recovery-v1",
        "automatic": True,
        "oom_only": True,
        "standard_max_attempts": 2,
        "plus_max_attempts": 1,
        "plus_48gb_authorized": False,
        "ambiguous_policy": "stop",
        "budget_recheck_each_attempt": True,
        "preserve_completed_roles": True,
        "budget_limit_cny": 5.0,
    }
    assert preflight["avatar_contract"]["workflow_id"] == pipeline.PRODUCTION_WORKFLOW_ID
    assert preflight["avatar_contract"]["workflow_profile"] == pipeline.PRODUCTION_WORKFLOW_PROFILE
    assert preflight["avatar_contract"]["resolution"] == "448x560"
    assert preflight["avatar_contract"]["fps"] == 25
    assert preflight["avatar_contract"]["frame_clock"] == "final_pcm_samples_exact"
    assert preflight["budget"] == {
        "limit_cny": 5.0, "absolute_user_limit_cny": 8.0,
        "reservation_per_role_cny": pipeline.ROLE_RESERVATION_CNY,
    }

    started = pipeline.start_avatar_review_preview_job(project, {
        "confirmed": True, "budget_limit_cny": 5.0, "visual": {"planning_mode": "ai_director"},
    })
    repeated = pipeline.start_avatar_review_preview_job(project, {
        "confirmed": True, "budget_limit_cny": 5.0, "visual": {"planning_mode": "ai_director"},
    })
    assert started["job_id"] == repeated["job_id"]
    assert started["pipeline_kind"] == "avatar_review_preview"
    assert started["frozen_input"]["asr"]["local_only"] is True
    assert started["frozen_input"]["avatar_recovery"] == preflight["avatar_recovery"]
    assert started["frozen_input"]["turn_timing"]["speaker_change_gap_ms"] == 250
    assert started["frozen_input"]["turn_timing"]["same_speaker_gap_ms"] == 300
    assert "worker_token" not in started


def test_preflight_and_start_freeze_only_the_single_yaya_role(
    prepared: tuple[Path, dict],
) -> None:
    project, script = prepared
    single_script = {**script, "title": "单主持测试", "sections": [script["sections"][0]]}
    write_json(project / "artifacts" / "script.json", single_script)
    state = wb._load_for_write(project)
    state["project"]["script_draft"] = {
        "status": "approved", "script": single_script, "script_hash": pipeline._json_hash(single_script),
    }
    wb._save(project, state)

    preflight = pipeline.avatar_review_preview_preflight(project, {"visual": {"planning_mode": "rule_mix"}})
    assert preflight["ready"] is True
    assert preflight["speaker_count"] == 1
    assert preflight["active_roles"] == ["yaya"]
    assert set(preflight["roles"]) == {"yaya"}

    started = pipeline.start_avatar_review_preview_job(project, {
        "confirmed": True, "budget_limit_cny": 5.0, "visual": {"planning_mode": "rule_mix"},
    })
    assert started["frozen_input"]["active_roles"] == ["yaya"]
    assert set(started["frozen_input"]["roles"]) == {"yaya"}
    frozen = pipeline._assert_frozen(project, pipeline._read_internal(project))
    assert set(frozen["profiles"]) == {"yaya"}


def test_prepare_longform_package_rebuilds_legacy_gap_settings(
    prepared: tuple[Path, dict], monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, _script = prepared
    legacy = {
        "generation_mode": "runninghub_longform", "import_mode": "longform", "revision": 1,
        "settings": {"speaker_change_gap_seconds": 0.16, "same_speaker_gap_seconds": 0.0},
        "speakers": [{"speaker_id": role, "name": label} for role, label in pipeline.ROLE_LABELS.items()],
    }
    rebuilt = {
        **legacy,
        "settings": {"speaker_change_gap_seconds": 0.25, "same_speaker_gap_seconds": 0.30},
        "speakers": [{"speaker_id": role, "name": label} for role, label in pipeline.ROLE_LABELS.items()],
    }
    captured: list[dict] = []
    monkeypatch.setattr(pipeline, "read_avatar_package", lambda _project: legacy)
    monkeypatch.setattr(pipeline, "initialize_avatar_package", lambda _project, payload: captured.append(payload) or rebuilt)
    monkeypatch.setattr(pipeline, "prepare_upload", lambda _project, name, speaker_id: (project / f".{speaker_id}.tmp", project / f"{speaker_id}.mp4"))
    monkeypatch.setattr(pipeline, "finalize_upload", lambda *_args, **_kwargs: rebuilt)
    records = {"roles": {}}
    for role in pipeline.ROLE_LABELS:
        source = project / f"source-{role}.mp4"
        source.write_bytes(role.encode("utf-8") * 100)
        records["roles"][role] = {"output_path": str(source.relative_to(project)).replace("\\", "/")}

    package = pipeline._prepare_longform_package(project, records)

    assert package["settings"]["speaker_change_gap_seconds"] == pytest.approx(0.25)
    assert package["settings"]["same_speaker_gap_seconds"] == pytest.approx(0.30)
    assert captured == [{
        "replace": True, "generation_mode": "runninghub_longform", "import_mode": "longform",
        "require_asr": True, "speaker_change_gap_seconds": 0.25,
        "same_speaker_gap_seconds": 0.3, "default_treatment": "custom",
        "background_mode": "opaque",
    }]


def test_exact_clock_voice_plan_checks_five_minute_limit_before_runninghub(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = {
        "roles": {
            "yaya": {"video_frame_count": 2_500},
            "mengmeng": {"video_frame_count": 2_500},
        },
        "turns": [
            {"speaker_id": "yaya"}, {"speaker_id": "mengmeng"},
            {"speaker_id": "yaya"},
        ],
        "sha256": "verified-manifest",
    }
    monkeypatch.setattr(pipeline, "_verified_voice_timing_manifest", lambda *_args: manifest)
    voice: dict = {}

    plan = pipeline._validate_one_click_avatar_duration(tmp_path, voice)

    assert plan["planned_master_seconds"] == pytest.approx(200.5)
    assert plan["maximum_seconds"] == 300.0
    assert voice["avatar_duration_plan"] == plan
    monkeypatch.setattr(pipeline, "ONE_CLICK_AVATAR_MAX_DURATION_SECONDS", 200.0)
    with pytest.raises(pipeline.AvatarReviewPreviewError, match="未提交 RunningHub"):
        pipeline._validate_one_click_avatar_duration(tmp_path, {})


def test_frozen_final_gap_contract_rejects_worker_input_drift(
    prepared: tuple[Path, dict], monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, _script = prepared
    started = pipeline.start_avatar_review_preview_job(project, {
        "confirmed": True, "budget_limit_cny": 5.0, "visual": {"planning_mode": "rule_mix"},
    })
    monkeypatch.setattr(pipeline, "SAME_SPEAKER_GAP_MS", 301)

    with pytest.raises(pipeline.AvatarInputDriftError, match="静音合同或清单版本已变化"):
        pipeline._assert_frozen(project, started)


def test_frozen_dual_avatar_default_mix_crosses_only_internal_trusted_gate(
    prepared: tuple[Path, dict],
) -> None:
    project, _script = prepared
    pipeline.start_avatar_review_preview_job(project, {
        "confirmed": True, "budget_limit_cny": 5.0, "visual": {"planning_mode": "rule_mix"},
    })
    state = wb._load_for_write(project)
    state["narration_policy"].update({"playback_gain_db": 3.0, "updated_at": None})
    state["music_policy"]["enabled"] = False

    with pytest.raises(wb.WorkbenchError, match="声音设置已修改"):
        wb._require_approved_music_sample(state)
    wb._require_approved_music_sample(state, trusted_default=True)

    state["narration_policy"]["updated_at"] = "2026-08-31T00:00:00Z"
    with pytest.raises(wb.WorkbenchError, match="声音设置已修改"):
        wb._require_approved_music_sample(state, trusted_default=True)
    state["narration_policy"]["updated_at"] = None
    state["music_policy"]["enabled"] = True
    with pytest.raises(wb.WorkbenchError, match="声音设置已修改"):
        wb._require_approved_music_sample(state, trusted_default=True)


def test_upfront_confirmation_freezes_audio_and_replaces_midflow_sample_gate(
    prepared: tuple[Path, dict],
) -> None:
    project, _script = prepared
    state = wb._load_for_write(project)
    state["narration_policy"].update({"playback_gain_db": 6.0, "updated_at": "2026-08-31T00:00:00Z"})
    state["music_policy"].update({
        "enabled": True, "track_id": "news-opening-01", "playback_gain_db": -3.0,
        "source_start_seconds": 1.0, "source_end_seconds": 20.0,
    })
    wb._save(project, state)

    preflight = pipeline.avatar_review_preview_preflight(project, {"visual": {"planning_mode": "rule_mix"}})
    started = pipeline.start_avatar_review_preview_job(project, {
        "confirmed": True, "budget_limit_cny": 5.0, "visual": {"planning_mode": "rule_mix"},
    })
    frozen_audio = started["frozen_input"]["audio"]

    assert preflight["music_contract"]["will_pause_for_audio_sample"] is False
    assert frozen_audio["authorization_mode"] == "upfront_one_click"
    assert frozen_audio["narration_gain_db"] == pytest.approx(6.0)
    assert frozen_audio["music_gain_db"] == pytest.approx(-3.0)
    wb._require_approved_music_sample(
        wb.read_workbench(project),
        upfront_authorized_signature=frozen_audio["audio_mix_signature"],
    )
    with pytest.raises(wb.WorkbenchError, match="声音设置已修改"):
        wb._require_approved_music_sample(
            wb.read_workbench(project), upfront_authorized_signature="wrong-signature",
        )

    changed = wb._load_for_write(project)
    changed["narration_policy"]["playback_gain_db"] = 5.0
    wb._save(project, changed)
    with pytest.raises(pipeline.AvatarInputDriftError, match="确认后发生变化"):
        pipeline._assert_frozen(project, started)


def test_assemble_and_apply_builds_missing_scene_plan(
    prepared: tuple[Path, dict], monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, _script = prepared
    state = wb._load_for_write(project)
    state["scenes"] = []
    state["segments"] = []
    wb._save(project, state)
    assert wb.read_workbench(project)["scenes"] == []
    package = {
        "cut_plan": {"status": "approved"},
        "assembly": {"status": "passed", "output_path": "renders/avatar/master.mp4"},
    }
    monkeypatch.setattr(pipeline, "read_avatar_package", lambda _project: package)

    applied: list[dict] = []

    def apply_timeline(project_dir: Path, payload: dict) -> dict:
        state = wb.read_workbench(project_dir)
        assert len(state["scenes"]) == 2
        applied.append(payload)
        return state

    monkeypatch.setattr(wb, "apply_avatar_package_to_timeline", apply_timeline)

    result = pipeline._assemble_and_apply(project)

    assert result["scene_count"] == 2
    assert applied == [{"default_treatment": "custom"}]
    assert (project / "artifacts" / "scene_plan.json").is_file()


def test_mocked_full_parent_reaches_review_ready_and_settles_budget(
    prepared: tuple[Path, dict], monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, _script = prepared
    original_preflight = pipeline.preflight_local_whisper
    whisper_load_tests: list[bool] = []

    def record_whisper_preflight(*args, **kwargs):
        whisper_load_tests.append(bool(kwargs.get("load_test")))
        return original_preflight(*args, **kwargs)

    monkeypatch.setattr(pipeline, "preflight_local_whisper", record_whisper_preflight)
    client = FakeRunningHub()
    monkeypatch.setattr(pipeline, "probe_media", fake_media)
    monkeypatch.setattr(pipeline, "_align_avatar_package", lambda *_args: {
        "asr": {"status": "passed"},
        "cut_plan": {"status": "approved", "summary": {"approved": 2, "needs_manual": 0}},
    })
    monkeypatch.setattr(pipeline, "_assemble_and_apply", lambda *_args: {
        "assembly": {"status": "passed"}, "timeline_revision": 1, "scene_count": 2,
    })
    started = pipeline.start_avatar_review_preview_job(project, {
        "confirmed": True, "budget_limit_cny": 5.0, "visual": {"planning_mode": "rule_mix"},
    })
    completed = pipeline.run_avatar_review_preview_job(project, started["job_id"], overrides={
        "tts_factory": FakeTTS,
        "runninghub_client_factory": lambda: client,
        "poll_interval": 0,
        "visual_runner": lambda *_args: {"completed_slots": 2, "failed_slots": 0},
        "preview_runner": lambda *_args: {
            "preview_path": "renders/full-preview/avatar-review.mp4",
            "report_path": "artifacts/avatar-review-report.json",
            "preview_sha256": "preview-hash", "preview_size_bytes": 12345,
        },
    })
    assert completed["status"] == "completed"
    assert completed["stage"] == "review_ready"
    assert completed["counts"] == {"total": 7, "completed": 7, "failed": 0}
    assert completed["result"]["readiness"] == "preview_ready"
    assert completed["budget"]["reserved"] == 0
    assert completed["budget"]["spent"] == pytest.approx(0.4)
    assert len(client.submits) == 2
    assert all(item["instance_type"] == "default" for item in client.submits)
    assert whisper_load_tests and not any(whisper_load_tests)
    records = wb.read_workbench(project)["automation"]["review_preview_pipeline"]["phases"]["avatar_generation"]["output"]["roles"]
    assert all(item["clock_validation"]["status"] == "passed" for item in records.values())

    # Older recovered jobs could reach review_ready while retaining a stale
    # headline failure count. Reconciliation must be local and must not launch
    # or mutate the paid-operation ledger.
    state = wb._load_for_write(project)
    parent = state["automation"]["review_preview_pipeline"]
    paid_operations = json.loads(json.dumps(parent["paid_operations"]))
    parent["counts"]["failed"] = 1
    wb._save(project, state)

    reconciled = pipeline.resume_avatar_review_preview_job(project, started["job_id"], {"confirmed": True})

    assert reconciled["status"] == "completed"
    assert reconciled["stage"] == "review_ready"
    assert reconciled["launch_required"] is False
    assert reconciled["counts"] == {"total": 7, "completed": 7, "failed": 0}
    assert reconciled["paid_operations"] == paid_operations
    assert len(client.submits) == 2


def test_structured_oom_recovers_in_one_parent_run_as_default_default_plus(
    prepared: tuple[Path, dict], monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, _script = prepared
    client = SequencedRunningHub([
        structured_oom_result(),
        structured_oom_result(),
        {
            "status": "SUCCEEDED",
            "video_url": "https://example.invalid/RH-3.mp4",
            "consume_money_cny": 0.2,
            "billing": {"observed_instance": "plus_48gb"},
        },
    ])
    patch_successful_local_stages(monkeypatch)
    started = pipeline.start_avatar_review_preview_job(project, {
        "confirmed": True,
        "budget_limit_cny": 5.0,
        "allow_plus_on_oom": True,
        "visual": {"planning_mode": "rule_mix"},
    })

    completed = pipeline.run_avatar_review_preview_job(
        project,
        started["job_id"],
        overrides=completed_parent_overrides(client),
    )

    assert completed["status"] == "completed"
    assert started["frozen_input"]["avatar_recovery"]["plus_48gb_authorized"] is True
    assert [item["instance_type"] for item in client.submits] == [
        "default", "default", "plus", "default",
    ]
    state = wb.read_workbench(project)
    roles = state["automation"]["review_preview_pipeline"]["phases"]["avatar_generation"]["output"]["roles"]
    assert sorted(len(item["history"]) for item in roles.values()) == [1, 3]
    recovered = next(item for item in roles.values() if len(item["history"]) == 3)
    assert [item.get("requested_instance") or item.get("instance") for item in recovered["history"]] == [
        "default", "default", "plus",
    ]


def test_non_oom_terminal_failure_is_nonretryable_even_with_plus_authorized(
    prepared: tuple[Path, dict], monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, _script = prepared
    client = SequencedRunningHub([{
        "status": "FAILED",
        "error": "model input validation failed",
        "failure_details": {
            "exception_type": "ValueError",
            "exception_message": "image dimensions are unsupported",
            "node_name": "LoadImage",
        },
        "consume_money_cny": 0.1,
        "billing": {"observed_instance": "standard_24gb"},
    }])
    started = pipeline.start_avatar_review_preview_job(project, {
        "confirmed": True,
        "budget_limit_cny": 5.0,
        "allow_plus_on_oom": True,
        "visual": {"planning_mode": "rule_mix"},
    })

    failed = pipeline.run_avatar_review_preview_job(project, started["job_id"], overrides={
        "tts_factory": FakeTTS,
        "runninghub_client_factory": lambda: client,
        "poll_interval": 0,
    })

    assert failed["status"] == "failed"
    assert failed["error"]["type"] == "AvatarProviderTerminalError"
    assert failed["error"]["retryable"] is False
    assert failed["safe_resume_point"] is None
    assert [item["instance_type"] for item in client.submits] == ["default"]
    with pytest.raises(pipeline.AvatarReviewPreviewError):
        pipeline.resume_avatar_review_preview_job(project, started["job_id"])
    assert len(client.submits) == 1


def test_budget_is_rechecked_before_uploading_or_submitting_an_oom_retry(
    prepared: tuple[Path, dict], monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, _script = prepared
    client = SequencedRunningHub([structured_oom_result()])
    started = pipeline.start_avatar_review_preview_job(project, {
        "confirmed": True,
        "budget_limit_cny": pipeline.ROLE_RESERVATION_CNY,
        "allow_plus_on_oom": True,
        "visual": {"planning_mode": "rule_mix"},
    })

    failed = pipeline.run_avatar_review_preview_job(project, started["job_id"], overrides={
        "tts_factory": FakeTTS,
        "runninghub_client_factory": lambda: client,
        "poll_interval": 0,
    })

    assert failed["status"] == "failed"
    assert failed["error"]["type"] == "AvatarBudgetBlockedError"
    assert failed["error"]["retryable"] is False
    assert failed["safe_resume_point"] is None
    assert [item["instance_type"] for item in client.submits] == ["default"]
    assert len(client.uploads) == 2


def test_plus_third_failure_is_exhausted_and_resume_never_submits_a_fourth_task(
    prepared: tuple[Path, dict], monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, _script = prepared
    client = SequencedRunningHub([
        structured_oom_result(),
        structured_oom_result(),
        structured_oom_result(observed_instance="plus_48gb"),
    ])
    started = pipeline.start_avatar_review_preview_job(project, {
        "confirmed": True,
        "budget_limit_cny": 5.0,
        "allow_plus_on_oom": True,
        "visual": {"planning_mode": "rule_mix"},
    })

    failed = pipeline.run_avatar_review_preview_job(project, started["job_id"], overrides={
        "tts_factory": FakeTTS,
        "runninghub_client_factory": lambda: client,
        "poll_interval": 0,
    })

    assert failed["status"] == "failed"
    assert failed["error"]["type"] == "AvatarRecoveryExhaustedError"
    assert failed["error"]["retryable"] is False
    assert failed["safe_resume_point"] is None
    assert [item["instance_type"] for item in client.submits] == ["default", "default", "plus"]
    with pytest.raises(pipeline.AvatarReviewPreviewError):
        pipeline.resume_avatar_review_preview_job(project, started["job_id"])
    assert len(client.submits) == 3


def test_exact_clock_output_contract_rejects_frame_rate_duration_and_audio_drift() -> None:
    valid = fake_media(Path(__file__))
    evidence = pipeline._validate_exact_clock_avatar_output(
        valid, exact_total_frames=32, expected_sample_rate=16_000, label="雅雅",
    )
    assert evidence["actual_frames"] == 32
    assert evidence["audio_duration_seconds"] == pytest.approx(1.28)

    cases = [
        (("video", "frame_count"), 31, "实际 31 帧"),
        (("video", "fps"), 24.0, "帧率"),
        (("audio", "duration_seconds"), 1.24, "音频流时长"),
        (("video", "width"), 447, "画面规格"),
        (("audio", "sample_rate"), 24_000, "采样率"),
    ]
    for path, value, message in cases:
        media = json.loads(json.dumps(valid))
        media[path[0]][path[1]] = value
        with pytest.raises(pipeline.AvatarInputDriftError, match=message):
            pipeline._validate_exact_clock_avatar_output(
                media, exact_total_frames=32, expected_sample_rate=16_000, label="雅雅",
            )


def test_trailing_silence_padding_plan_never_masks_missing_speech_frames() -> None:
    media = {
        "duration_seconds": 7.56,
        "video": {
            "present": True, "width": 448, "height": 560, "fps": 25.0,
            "frame_count": 186, "duration_seconds": 7.44,
        },
        "audio": {
            "present": True, "sample_rate": 24_000, "channels": 1,
            "duration_seconds": 7.56,
        },
    }
    turns = [{"speaker_id": "mengmeng", "speech_end_sample": 177_600}]

    plan = pipeline._trailing_silence_padding_plan(
        media,
        role="mengmeng",
        exact_total_frames=189,
        expected_sample_rate=24_000,
        sample_frame_count=181_440,
        samples_per_video_frame=960,
        timing_turns=turns,
    )

    assert plan == {
        "reason": "provider_video_tail_short_within_frozen_silence",
        "source_frames": 186,
        "expected_frames": 189,
        "added_frames": 3,
        "last_speech_end_frame": 185,
        "trailing_silence_frames": 4,
    }
    speech_truncated = json.loads(json.dumps(media))
    speech_truncated["video"]["frame_count"] = 185
    speech_truncated["video"]["duration_seconds"] = 7.4
    assert pipeline._trailing_silence_padding_plan(
        speech_truncated,
        role="mengmeng",
        exact_total_frames=189,
        expected_sample_rate=24_000,
        sample_frame_count=181_440,
        samples_per_video_frame=960,
        timing_turns=turns,
    ) is None


def test_pcm_tail_proof_rejects_any_nonzero_sample(tmp_path: Path) -> None:
    audio = tmp_path / "tail.wav"
    total = 960 * 4
    with wave.open(str(audio), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(24_000)
        output.writeframes(b"\x00\x00" * total)
    evidence = pipeline._verify_pcm_tail_is_silent(
        audio,
        start_sample=960 * 2,
        expected_sample_rate=24_000,
        expected_sample_frames=total,
    )
    assert evidence["silent_sample_frames"] == 960 * 2

    with wave.open(str(audio), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(24_000)
        output.writeframes((b"\x00\x00" * (total - 1)) + b"\x01\x00")
    with pytest.raises(pipeline.AvatarInputDriftError, match="并非全零静音"):
        pipeline._verify_pcm_tail_is_silent(
            audio,
            start_sample=960 * 2,
            expected_sample_rate=24_000,
            expected_sample_frames=total,
        )


def test_pts_prefix_probe_rejects_midstream_gap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pipeline, "_find_media_binary", lambda _name: "ffprobe")
    monkeypatch.setattr(
        pipeline,
        "_run_media_command",
        lambda _command: SimpleNamespace(returncode=0, stdout="0.000000\n0.040000\n0.080000\n", stderr=""),
    )
    assert pipeline._verify_contiguous_video_prefix(tmp_path / "fake.mp4", 3)["status"] == "passed"
    monkeypatch.setattr(
        pipeline,
        "_run_media_command",
        lambda _command: SimpleNamespace(returncode=0, stdout="0.000000\n0.040000\n0.120000\n", stderr=""),
    )
    with pytest.raises(pipeline.AvatarInputDriftError, match="时间戳 .*不连续"):
        pipeline._verify_contiguous_video_prefix(tmp_path / "fake.mp4", 3)


def test_tail_normalization_ffmpeg_failure_preserves_provider_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "mengmeng-longform.mp4"
    target.write_bytes(b"provider-original")
    original_sha = pipeline._sha256_file(target)
    audio = tmp_path / "mengmeng.wav"
    total_samples = 189 * 960
    with wave.open(str(audio), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(24_000)
        output.writeframes(b"\x00\x00" * total_samples)
    media = {
        "duration_seconds": 7.56,
        "video": {"present": True, "width": 448, "height": 560, "fps": 25.0, "frame_count": 186, "duration_seconds": 7.44},
        "audio": {"present": True, "sample_rate": 24_000, "channels": 1, "duration_seconds": 7.56},
    }
    monkeypatch.setattr(pipeline, "_verify_contiguous_video_prefix", lambda *_args: {"status": "passed"})
    monkeypatch.setattr(pipeline, "_find_media_binary", lambda _name: "ffmpeg")
    monkeypatch.setattr(
        pipeline, "_run_media_command",
        lambda _command: SimpleNamespace(returncode=1, stdout="", stderr="synthetic ffmpeg failure"),
    )

    with pytest.raises(pipeline.AvatarReviewPreviewError, match="synthetic ffmpeg failure"):
        pipeline._validate_or_normalize_exact_clock_avatar_output(
            target, audio, media,
            role="mengmeng", exact_total_frames=189, expected_sample_rate=24_000,
            sample_frame_count=total_samples, samples_per_video_frame=960,
            timing_turns=[{"speaker_id": "mengmeng", "speech_end_sample": 177_600}],
            label="檬檬", expected_provider_sha256=original_sha,
        )
    assert pipeline._sha256_file(target) == original_sha
    assert not list(tmp_path.glob("*provider-raw*"))


def test_tail_normalization_recovers_provenance_after_atomic_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "mengmeng-longform.mp4"
    target.write_bytes(b"already-normalized")
    raw_bytes = b"provider-original"
    raw_sha = hashlib.sha256(raw_bytes).hexdigest()
    raw_target = tmp_path / f"mengmeng-longform.provider-raw-{raw_sha[:12]}.mp4"
    raw_target.write_bytes(raw_bytes)
    audio = tmp_path / "mengmeng.wav"
    total_samples = 189 * 960
    with wave.open(str(audio), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(24_000)
        output.writeframes(b"\x00\x00" * total_samples)
    normalized_media = fake_media(target)
    normalized_media["duration_seconds"] = 7.56
    normalized_media["video"].update({"frame_count": 189, "duration_seconds": 7.56})
    normalized_media["audio"].update({"sample_rate": 24_000, "duration_seconds": 7.56})
    raw_media = json.loads(json.dumps(normalized_media))
    raw_media["video"].update({"frame_count": 186, "duration_seconds": 7.44})
    monkeypatch.setattr(pipeline, "probe_media", lambda path: raw_media if path == raw_target else normalized_media)
    monkeypatch.setattr(pipeline, "_verify_contiguous_video_prefix", lambda *_args: {"status": "passed"})

    _media, evidence = pipeline._validate_or_normalize_exact_clock_avatar_output(
        target, audio, normalized_media,
        role="mengmeng", exact_total_frames=189, expected_sample_rate=24_000,
        sample_frame_count=total_samples, samples_per_video_frame=960,
        timing_turns=[{"speaker_id": "mengmeng", "speech_end_sample": 177_600}],
        label="檬檬", expected_provider_sha256=raw_sha,
    )

    assert evidence["normalization"]["status"] == "recovered_after_atomic_replace"
    assert evidence["normalization"]["raw_sha256"] == raw_sha


def test_verified_voice_manifest_rejects_disk_sha_drift(
    prepared: tuple[Path, dict],
) -> None:
    project, _script = prepared
    started = pipeline.start_avatar_review_preview_job(project, {
        "confirmed": True, "budget_limit_cny": 5.0,
        "allow_plus_on_oom": True, "visual": {"planning_mode": "rule_mix"},
    })
    acquired = pipeline._acquire_worker(project, started["job_id"])
    assert acquired is not None
    _job_id, worker_token = acquired
    context = pipeline._assert_frozen(project, pipeline._read_internal(project))
    voice = pipeline._generate_voice_tracks(
        project, started["job_id"], worker_token, context, tts_factory=FakeTTS,
    )
    assert pipeline._verified_voice_timing_manifest(project, voice)["version"] == pipeline.TURN_TIMING_MANIFEST_VERSION
    manifest_path = project / voice["timing_manifest"]["path"]
    manifest_path.write_text(manifest_path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises(pipeline.AvatarInputDriftError, match="内容已变化"):
        pipeline._verified_voice_timing_manifest(project, voice)


def test_nonretryable_output_clock_drift_can_resume_only_for_settled_local_tail_repair(
    prepared: tuple[Path, dict],
) -> None:
    project, _script = prepared
    started = pipeline.start_avatar_review_preview_job(project, {
        "confirmed": True, "budget_limit_cny": 5.0,
        "allow_plus_on_oom": True, "visual": {"planning_mode": "rule_mix"},
    })
    target = project / "assets" / "video" / "avatar-review-preview" / "mengmeng-longform.mp4"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"provider-output")
    operation_id = "runninghub:repair-candidate"
    state = wb._load_for_write(project)
    job = state["automation"]["review_preview_pipeline"]
    job.update({
        "status": "failed", "stage": "avatar_generation",
        "safe_resume_point": None,
    })
    job.setdefault("phases", {})["avatar_generation"] = {
        "status": "failed", "retryable": False, "safe_resume_point": None,
        "output": {"roles": {"mengmeng": {
            "status": "failed", "task_id": "RH-1", "operation_id": operation_id,
            "output_path": str(target.relative_to(project)).replace("\\", "/"),
            "history": [{"status": "failed", "terminal_reason": "output_contract_drift"}],
        }}},
    }
    job["paid_operations"] = {
        operation_id: {"task_id": "RH-1", "settled": True, "state": "failed"},
    }
    wb._save(project, state)

    resumed = pipeline.resume_avatar_review_preview_job(project, started["job_id"])

    assert resumed["status"] == "queued"
    assert resumed["stage"] == "avatar_generation"
    assert resumed["safe_resume_point"] == "avatar_generation"
    assert len(resumed["paid_operations"]) == 1
    assert resumed["budget"]["spent"] == 0


def test_align_exact_clock_falls_back_to_manifest_when_whisper_diagnostic_crashes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    package = {
        "asr": {"status": "not_started", "summary": {"timing_manifest": {"version": pipeline.TURN_TIMING_MANIFEST_VERSION}}},
        "cut_plan": {"status": "not_started"},
    }
    approved = {
        "asr": {"status": "passed", "summary": {"diagnostic_only": True}},
        "cut_plan": {"status": "approved", "summary": {"cut_authority": "exact_frame_manifest"}},
    }
    fallback_calls: list[dict] = []
    monkeypatch.setattr(pipeline, "_prepare_longform_package", lambda *_args: package)
    monkeypatch.setattr(pipeline, "start_avatar_asr", lambda *_args, **_kwargs: package)
    monkeypatch.setattr(pipeline, "run_avatar_asr", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("synthetic whisper crash")))
    monkeypatch.setattr(
        pipeline,
        "approve_exact_clock_manifest_cuts",
        lambda _project, **kwargs: fallback_calls.append(kwargs) or approved,
    )

    result = pipeline._align_avatar_package(project, {}, "faster-whisper-small", {})

    assert result == approved
    assert fallback_calls[0]["model_name"] == "faster-whisper-small"
    assert "synthetic whisper crash" in str(fallback_calls[0]["diagnostic_error"])


def test_submit_timeout_is_ambiguous_and_never_retried(
    prepared: tuple[Path, dict], monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, _script = prepared

    class AmbiguousClient(FakeRunningHub):
        def submit(self, **payload: str) -> dict:
            self.submits.append(payload)
            raise TimeoutError("timeout while waiting for create response")

    client = AmbiguousClient()
    monkeypatch.setattr(pipeline, "probe_media", fake_media)
    started = pipeline.start_avatar_review_preview_job(project, {
        "confirmed": True, "budget_limit_cny": 5.0, "allow_plus_on_oom": True,
        "visual": {"planning_mode": "rule_mix"},
    })
    failed = pipeline.run_avatar_review_preview_job(project, started["job_id"], overrides={
        "tts_factory": FakeTTS,
        "runninghub_client_factory": lambda: client,
        "poll_interval": 0,
    })
    assert failed["status"] == "ambiguous"
    assert failed["error"]["retryable"] is False
    assert failed["safe_resume_point"] is None
    assert len(client.submits) == 1
    with pytest.raises(pipeline.AmbiguousAvatarOperation):
        pipeline.resume_avatar_review_preview_job(project, started["job_id"])


def test_process_loss_during_submit_becomes_ambiguous_without_second_paid_submit(
    prepared: tuple[Path, dict], monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, _script = prepared

    class InterruptedSubmitClient(FakeRunningHub):
        def submit(self, **payload: str) -> dict:
            self.submits.append(payload)
            raise KeyboardInterrupt("synthetic process loss after request started")

    client = InterruptedSubmitClient()
    started = pipeline.start_avatar_review_preview_job(project, {
        "confirmed": True, "budget_limit_cny": 5.0, "allow_plus_on_oom": True,
        "visual": {"planning_mode": "rule_mix"},
    })
    with pytest.raises(KeyboardInterrupt):
        pipeline.run_avatar_review_preview_job(project, started["job_id"], overrides={
            "tts_factory": FakeTTS,
            "runninghub_client_factory": lambda: client,
            "poll_interval": 0,
        })

    recovered = pipeline.recover_avatar_review_preview_job(project)
    assert recovered["status"] == "queued"
    stopped = pipeline.run_avatar_review_preview_job(project, started["job_id"], overrides={
        "runninghub_client_factory": lambda: client,
        "poll_interval": 0,
    })

    assert stopped["status"] == "ambiguous"
    assert stopped["safe_resume_point"] is None
    assert len(client.submits) == 1


def test_settlement_is_idempotent_even_before_operation_terminal_transition(
    prepared: tuple[Path, dict],
) -> None:
    project, _script = prepared
    started = pipeline.start_avatar_review_preview_job(project, {
        "confirmed": True, "budget_limit_cny": 5.0,
        "visual": {"planning_mode": "rule_mix"},
    })
    acquired = pipeline._acquire_worker(project, started["job_id"])
    assert acquired is not None
    _job_id, worker_token = acquired
    operation_id = "runninghub:test:yaya:idempotent:a1:default"
    pipeline._reserve_budget(
        project, started["job_id"], worker_token, operation_id,
        "雅雅 Standard 24GB 测试", requested_instance="default",
    )
    pipeline._transition_operation(
        project, started["job_id"], worker_token, operation_id, "submitted",
        task_id="RH-idempotent", requested_instance="default",
    )
    result = {"status": "FAILED", "consume_money_cny": 0.2}

    first = pipeline._settle_budget(
        project, started["job_id"], worker_token, operation_id, result, 180.0,
    )
    second = pipeline._settle_budget(
        project, started["job_id"], worker_token, operation_id, result, 180.0,
    )
    current = wb.read_workbench(project)["automation"]["review_preview_pipeline"]

    assert first == pytest.approx(0.2)
    assert second == pytest.approx(0.2)
    assert current["budget"]["spent"] == pytest.approx(0.2)
    assert current["budget"]["reserved"] == pytest.approx(0.0)
    assert sum(
        1 for item in current["budget"]["entries"]
        if item.get("type") == "settle" and item.get("operation_id") == operation_id
    ) == 1


@pytest.mark.parametrize(
    "details",
    [
        {"exception_type": "CUDAError", "exception_message": "unsupported memory format"},
        {"exception_type": "CUDAInitError", "exception_message": "shared memory unavailable"},
    ],
)
def test_cuda_memory_wording_without_explicit_oom_never_authorizes_paid_recovery(
    details: dict,
) -> None:
    failure = pipeline.classify_runninghub_failure({
        "status": "FAILED", "error": "CUDA execution failed", "failure_details": details,
    })

    assert failure["is_oom"] is False
    assert failure["explicit"] is False
    assert failure["kind"] != "oom"


def test_actual_cost_over_budget_is_persisted_and_blocks_resume(
    prepared: tuple[Path, dict],
) -> None:
    project, _script = prepared
    client = SequencedRunningHub([{
        "status": "SUCCEEDED", "video_url": "https://example.invalid/over-budget.mp4",
        "consume_money_cny": 5.1,
        "billing": {"observed_instance": "standard_24gb"},
    }])
    started = pipeline.start_avatar_review_preview_job(project, {
        "confirmed": True, "budget_limit_cny": 5.0,
        "allow_plus_on_oom": True, "visual": {"planning_mode": "rule_mix"},
    })

    failed = pipeline.run_avatar_review_preview_job(project, started["job_id"], overrides={
        "tts_factory": FakeTTS,
        "runninghub_client_factory": lambda: client,
        "poll_interval": 0,
    })
    current = wb.read_workbench(project)["automation"]["review_preview_pipeline"]

    assert failed["status"] == "failed"
    assert failed["error"]["type"] == "AvatarBudgetBlockedError"
    assert failed["error"]["retryable"] is False
    assert failed["safe_resume_point"] is None
    assert current["budget"]["spent"] == pytest.approx(5.1)
    assert current["budget"]["reserved"] == pytest.approx(0.0)
    assert current["budget"]["over_limit"]["limit_cny"] == pytest.approx(5.0)
    assert len(client.submits) == 1
    with pytest.raises(pipeline.AvatarReviewPreviewError):
        pipeline.resume_avatar_review_preview_job(project, started["job_id"])


def test_active_plus_third_attempt_after_restart_is_polled_not_resubmitted(
    prepared: tuple[Path, dict], monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, _script = prepared

    class InterruptThirdPollClient(SequencedRunningHub):
        interrupted = False

        def poll(self, task_id: str) -> dict:
            if task_id == "RH-3" and not self.interrupted:
                self.interrupted = True
                raise KeyboardInterrupt("synthetic restart while Plus task is active")
            return super().poll(task_id)

    client = InterruptThirdPollClient([structured_oom_result(), structured_oom_result()])
    patch_successful_local_stages(monkeypatch)
    started = pipeline.start_avatar_review_preview_job(project, {
        "confirmed": True, "budget_limit_cny": 5.0,
        "allow_plus_on_oom": True, "visual": {"planning_mode": "rule_mix"},
    })
    with pytest.raises(KeyboardInterrupt):
        pipeline.run_avatar_review_preview_job(
            project, started["job_id"], overrides=completed_parent_overrides(client),
        )
    before = wb.read_workbench(project)["automation"]["review_preview_pipeline"]
    active = next(
        item for item in before["phases"]["avatar_generation"]["output"]["roles"].values()
        if item.get("task_id") == "RH-3"
    )
    assert active["requested_instance"] == "plus"
    assert len(active["history"]) == 3

    pipeline.recover_avatar_review_preview_job(project)
    completed = pipeline.run_avatar_review_preview_job(
        project, started["job_id"], overrides=completed_parent_overrides(client),
    )

    assert completed["status"] == "completed"
    assert [item["instance_type"] for item in client.submits] == [
        "default", "default", "plus", "default",
    ]


def test_transient_runninghub_status_query_retries_same_task_without_resubmit(
    prepared: tuple[Path, dict], monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, _script = prepared

    class FlakyQueryClient(SequencedRunningHub):
        interrupted = False

        def poll(self, task_id: str) -> dict:
            if not self.interrupted:
                self.interrupted = True
                raise ConnectionResetError("synthetic transient query reset")
            return super().poll(task_id)

    client = FlakyQueryClient([])
    patch_successful_local_stages(monkeypatch)
    started = pipeline.start_avatar_review_preview_job(project, {
        "confirmed": True, "budget_limit_cny": 5.0,
        "allow_plus_on_oom": True, "visual": {"planning_mode": "rule_mix"},
    })

    completed = pipeline.run_avatar_review_preview_job(
        project, started["job_id"], overrides=completed_parent_overrides(client),
    )

    assert completed["status"] == "completed"
    assert len(client.submits) == 2
    records = completed["phases"]["avatar_generation"]["output"]["roles"]
    assert all("transient_poll_error_count" not in record for record in records.values())


def test_standard_request_observed_as_plus_is_nonretryable_instance_drift(
    prepared: tuple[Path, dict],
) -> None:
    project, _script = prepared
    client = SequencedRunningHub([{
        "status": "SUCCEEDED", "video_url": "https://example.invalid/wrong-instance.mp4",
        "consume_money_cny": 0.2,
        "billing": {"observed_instance": "plus_48gb"},
    }])
    started = pipeline.start_avatar_review_preview_job(project, {
        "confirmed": True, "budget_limit_cny": 5.0,
        "allow_plus_on_oom": True, "visual": {"planning_mode": "rule_mix"},
    })

    failed = pipeline.run_avatar_review_preview_job(project, started["job_id"], overrides={
        "tts_factory": FakeTTS,
        "runninghub_client_factory": lambda: client,
        "poll_interval": 0,
    })

    assert failed["status"] == "failed"
    assert failed["error"]["type"] == "AvatarInputDriftError"
    assert failed["error"]["retryable"] is False
    assert failed["safe_resume_point"] is None
    assert len(client.submits) == 1


def test_structured_torch_oom_without_plus_authorization_stops_after_two_standard_attempts(
    prepared: tuple[Path, dict], monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, _script = prepared
    first = structured_oom_result()
    first["failure_details"]["exception_type"] = "torch.OutOfMemoryError"
    client = SequencedRunningHub([first, structured_oom_result()])
    started = pipeline.start_avatar_review_preview_job(project, {
        "confirmed": True, "budget_limit_cny": 5.0, "visual": {"planning_mode": "rule_mix"},
    })
    failed = pipeline.run_avatar_review_preview_job(project, started["job_id"], overrides={
        "tts_factory": FakeTTS,
        "runninghub_client_factory": lambda: client,
        "poll_interval": 0,
    })

    assert failed["status"] == "failed"
    assert failed["error"]["retryable"] is False
    assert failed["safe_resume_point"] is None
    assert started["frozen_input"]["avatar_recovery"]["plus_48gb_authorized"] is False
    assert [item["instance_type"] for item in client.submits] == ["default", "default"]


def test_visual_resume_requeues_only_owned_failed_slots(
    prepared: tuple[Path, dict], monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, _script = prepared
    started = pipeline.start_avatar_review_preview_job(project, {
        "confirmed": True, "budget_limit_cny": 5.0, "visual": {"planning_mode": "rule_mix"},
    })
    state = wb._load_for_write(project)
    parent = state["automation"]["review_preview_pipeline"]
    parent.update({"status": "failed", "stage": "visual_generation", "safe_resume_point": "visual_generation"})
    state["automation"]["visual_batch"] = {
        "status": "completed_with_failures", "job_id": "VB-owned",
        "parent_job_id": started["job_id"], "request_fingerprint": started["request_fingerprint"],
        "failed_slots": 1,
    }
    wb._save(project, state)
    calls: list[dict] = []
    monkeypatch.setattr(
        wb,
        "requeue_failed_visual_batch",
        lambda _path, **kwargs: calls.append(kwargs) or wb.read_workbench(project),
    )

    resumed = pipeline.resume_avatar_review_preview_job(project, started["job_id"])

    assert resumed["status"] == "queued"
    assert resumed["safe_resume_point"] == "visual_generation"
    assert calls == [{
        "expected_job_id": "VB-owned",
        "expected_parent_job_id": started["job_id"],
        "expected_request_fingerprint": started["request_fingerprint"],
    }]


def test_turn_tts_is_serial_ordered_and_failed_turn_resume_is_granular(
    prepared: tuple[Path, dict],
) -> None:
    project, _script = prepared
    started = pipeline.start_avatar_review_preview_job(project, {
        "confirmed": True, "budget_limit_cny": 5.0, "visual": {"planning_mode": "rule_mix"},
    })
    acquired = pipeline._acquire_worker(project, started["job_id"])
    assert acquired is not None
    _job_id, worker_token = acquired
    context = pipeline._assert_frozen(project, pipeline._read_internal(project))
    calls: list[str] = []
    active = 0
    maximum_active = 0
    fail_once = {"T002"}

    class RecordingTTS(FakeTTS):
        def execute(self, inputs: dict) -> SimpleNamespace:
            nonlocal active, maximum_active
            turn_id = Path(inputs["output_path"]).stem.split("-")[0]
            calls.append(turn_id)
            active += 1
            maximum_active = max(maximum_active, active)
            try:
                if turn_id in fail_once:
                    fail_once.remove(turn_id)
                    return SimpleNamespace(success=False, error="synthetic failure")
                return super().execute(inputs)
            finally:
                active -= 1

    with pytest.raises(pipeline.AvatarReviewPreviewError, match="synthetic failure"):
        pipeline._generate_voice_tracks(project, started["job_id"], worker_token, context, tts_factory=RecordingTTS)
    first_hash = pipeline._read_internal(project)["phases"]["voice"]["output"]["turns"]["T001"]["wav_sha256"]
    result = pipeline._generate_voice_tracks(project, started["job_id"], worker_token, context, tts_factory=RecordingTTS)

    assert calls == ["T001", "T002", "T002"]
    assert maximum_active == 1
    assert result["turns"]["T001"]["wav_sha256"] == first_hash
    assert result["timing_manifest"]["version"] == pipeline.TURN_TIMING_MANIFEST_VERSION


def test_provider_neutral_turn_tts_normalizes_then_composes_exact_clock(
    prepared: tuple[Path, dict], monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, _script = prepared
    started = pipeline.start_avatar_review_preview_job(project, {
        "confirmed": True, "budget_limit_cny": 5.0, "visual": {"planning_mode": "rule_mix"},
    })
    acquired = pipeline._acquire_worker(project, started["job_id"])
    assert acquired is not None
    _job_id, worker_token = acquired
    context = pipeline._assert_frozen(project, pipeline._read_internal(project))
    for role, profile in context["profiles"].items():
        profile.update({
            "provider_id": "doubao",
            "provider_name": "豆包云端配音",
            "voice_signature": f"cloud-{role}",
        })
    calls: list[tuple[str, str]] = []

    def fake_generate_voice_audio(*, text: str, profile: dict, output_path: Path, language: str) -> SimpleNamespace:
        calls.append((str(profile["id"]), text))
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(output_path), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(24_000)
            output.writeframes(b"\x01\x00" * 24_000)
        return SimpleNamespace(success=True, error=None)

    monkeypatch.setattr(pipeline, "generate_voice_audio", fake_generate_voice_audio)

    result = pipeline._generate_voice_tracks(project, started["job_id"], worker_token, context)

    assert [profile_id for profile_id, _text in calls] == ["voice-yaya", "voice-mengmeng"]
    assert all(turn["provider_id"] == "doubao" for turn in result["turns"].values())
    for role in pipeline.ROLE_LABELS:
        track = result["roles"][role]
        assert track["sample_rate"] == 24_000
        assert track["channels"] == 1
        assert track["sample_width"] == 2
        assert track["sample_frame_count"] % track["samples_per_video_frame"] == 0


def test_role_track_silence_and_manifest_are_sample_exact(tmp_path: Path) -> None:
    project = tmp_path / "project"
    records = []
    for turn_id, frames in (("T001", 8_000), ("T003", 16_000)):
        path = project / "turns" / f"{turn_id}.wav"
        path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(path), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(16_000)
            output.writeframes(b"\x01\x00" * frames)
        records.append({"turn_id": turn_id, "speaker_id": "yaya", "path": str(path.relative_to(project)).replace("\\", "/"), "text_sha256": turn_id, "voice_signature": "voice", "wav_sha256": pipeline._sha256_file(path)})

    role, turns = pipeline._compose_role_track(project, "yaya", records)

    assert role["content_sample_frames"] == 36_000
    assert role["final_padding_sample_frames"] == 480
    assert role["sample_frame_count"] == 36_480
    assert role["samples_per_video_frame"] == 640
    assert role["video_frame_count"] == 57
    assert role["duration_seconds"] == pytest.approx(2.28)
    assert turns[0]["speech_start_seconds"] == pytest.approx(0.1)
    assert turns[0]["speech_end_seconds"] == pytest.approx(0.6)
    assert turns[1]["speech_start_seconds"] - turns[0]["speech_end_seconds"] == pytest.approx(0.5)
    assert turns[0]["source_end_frame_exclusive"] == turns[1]["source_start_frame"]
    assert turns[0]["source_start_frame"] == 0
    assert turns[1]["source_end_frame_exclusive"] == 57
    assert turns[1]["source_end_seconds"] == pytest.approx(2.28)


def test_four_turn_two_presenter_clock_manifest_stays_contiguous_without_manual_cut_gate(tmp_path: Path) -> None:
    project = tmp_path / "four-turn"
    (project / "artifacts").mkdir(parents=True)
    sections = [
        {"id": "s1", "turn_id": "T001", "speaker_id": "yaya", "speaker_name": "雅雅", "text": "第一句。", "start_seconds": 0, "end_seconds": 1},
        {"id": "s2", "turn_id": "T002", "speaker_id": "mengmeng", "speaker_name": "檬檬", "text": "第二句。", "start_seconds": 1, "end_seconds": 2},
        {"id": "s3", "turn_id": "T003", "speaker_id": "yaya", "speaker_name": "雅雅", "text": "第三句。", "start_seconds": 2, "end_seconds": 3},
        {"id": "s4", "turn_id": "T004", "speaker_id": "mengmeng", "speaker_name": "檬檬", "text": "第四句。", "start_seconds": 3, "end_seconds": 4},
    ]
    write_json(project / "project.json", {
        "project_id": project.name, "title": "四句精确帧回归", "pipeline_type": "avatar-spokesperson",
    })
    write_json(project / "artifacts" / "script.json", {"title": "四句精确帧回归", "sections": sections})
    package = avatar_mod.initialize_avatar_package(project, {"import_mode": "longform", "require_asr": True})
    role_ledgers: dict[str, dict] = {}
    manifest_turns: list[dict] = []
    for role in ("yaya", "mengmeng"):
        records = []
        for section in (item for item in sections if item["speaker_id"] == role):
            path = project / "turns" / f"{section['turn_id']}.wav"
            path.parent.mkdir(parents=True, exist_ok=True)
            with wave.open(str(path), "wb") as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(16_000)
                output.writeframes(b"\x01\x00" * 4_000)
            records.append({
                "turn_id": section["turn_id"], "speaker_id": role,
                "path": str(path.relative_to(project)).replace("\\", "/"),
                "text_sha256": hashlib.sha256(section["text"].encode("utf-8")).hexdigest(),
                "voice_signature": section["turn_id"], "wav_sha256": pipeline._sha256_file(path),
            })
        role_ledger, role_turns = pipeline._compose_role_track(project, role, records)
        role_ledgers[role] = role_ledger
        manifest_turns.extend(role_turns)
    order = {section["turn_id"]: index for index, section in enumerate(sections)}
    manifest_turns.sort(key=lambda item: order[item["turn_id"]])
    manifest = {
        "version": pipeline.TURN_TIMING_MANIFEST_VERSION,
        "contract": {"video_fps": 25, "frame_alignment": "final_role_track_once"},
        "roles": role_ledgers,
        "turns": manifest_turns,
    }

    package = avatar_mod.apply_longform_timing_manifest(project, manifest)
    issues = avatar_mod._review_deterministic_longform_turns(
        package,
        {role: {"text": "", "segments": []} for role in role_ledgers},
        package["asr"]["summary"]["timing_manifest"],
    )

    assert package["cut_plan"]["status"] == "approved"
    assert package["cut_plan"]["summary"]["needs_manual"] == 0
    assert len([item for item in issues if item["code"] == "exact_clock_asr_diagnostic_warning"]) == 4
    by_role: dict[str, list[dict]] = {role: [] for role in role_ledgers}
    for item in manifest_turns:
        by_role[item["speaker_id"]].append(item)
        assert item["source_start_sample"] <= item["speech_start_sample"]
        assert item["speech_end_sample"] <= item["source_end_sample"]
    for role, turns in by_role.items():
        assert turns[0]["source_start_frame"] == 0
        assert turns[0]["source_end_frame_exclusive"] == turns[1]["source_start_frame"]
        assert turns[-1]["source_end_frame_exclusive"] == role_ledgers[role]["video_frame_count"]


def test_visual_planner_error_falls_back_to_auditable_rule_mix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, _script = make_project(tmp_path)
    calls: list[str] = []

    def fake_preview(_project: Path, policy: dict) -> dict:
        mode = str(policy.get("planning_mode") or "")
        calls.append(mode)
        if mode == "ai_director":
            raise wb.WorkbenchError("AI 画面规划失败：中转站无有效响应")
        return {"status": "planned", "planner": {"mode": mode}, "items": []}

    monkeypatch.setattr(pipeline.wb, "preview_visual_batch_plan", fake_preview)
    reviewed, execution_policy, reason = pipeline._preview_supporting_visual_plan(project, {
        "planning_mode": "ai_director",
        "ai_planning_confirmed": True,
    })

    assert calls == ["ai_director", "rule_mix"]
    assert execution_policy["planning_mode"] == "rule_mix"
    assert "中转站无有效响应" in str(reason)
    assert reviewed["planner"]["fallback_from"] == "ai_director"
    assert "规则混合" in str(reviewed["planner"]["fallback_reason"])
