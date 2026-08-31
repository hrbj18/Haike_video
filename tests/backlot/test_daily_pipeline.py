from __future__ import annotations

from pathlib import Path
import json
import os
import shutil
import subprocess
import wave

import pytest

import backlot.daily_pipeline as daily_pipeline
from backlot.daily_automation import DailyAutomationError
from backlot.daily_pipeline import (
    _can_reuse_voice_track,
    generate_runninghub_avatars,
    generate_supporting_visuals,
    validate_daily_review_candidate,
)


def test_fallback_review_candidate_continues_to_voice_then_stops_at_provider_gate(tmp_path, monkeypatch):
    target = "2026-08-24"
    run_dir = tmp_path / target
    run_dir.mkdir(parents=True)
    (run_dir / "daily_script.json").write_text(json.dumps({
        "validation": {"passed": True, "valid": True, "errors": []},
        "topic_selection": {"selected_stories": [{"heat_level": "H3"}]},
        "editorial_review": {
            "quality_band": "premium", "total": 80, "passed": False,
            "scores": {"hook": 15, "dialogue": 15, "information_density": 19, "public_value": 17, "interaction": 14},
            "structured_issues": [],
        },
    }), encoding="utf-8")
    run = {
        "target_date": target,
        "status": "running",
        "current_stage": "voice",
        "approval_policy": {"fallback_script_approved": False},
        "stages": {name: {"status": "pending"} for name in daily_pipeline.STAGE_ORDER},
    }
    monkeypatch.setattr(daily_pipeline, "RUNS_ROOT", tmp_path)
    monkeypatch.setattr(daily_pipeline, "run_research_and_script", lambda *_args, **_kwargs: run)
    monkeypatch.setattr(daily_pipeline, "read_run", lambda *_args, **_kwargs: run)
    monkeypatch.setattr(daily_pipeline, "_save_run", lambda value: value)
    calls = {"voice": 0, "avatar": 0}
    monkeypatch.setattr(daily_pipeline, "preflight_daily_media", lambda *_args: {"status": "passed"})
    monkeypatch.setattr(daily_pipeline, "_initial_avatar_instance", lambda *_args: ({}, "default", False))
    monkeypatch.setattr(daily_pipeline, "provider_media_eligibility", lambda _run: {"eligible": False, "reason": "测试停止点"})
    monkeypatch.setattr(daily_pipeline, "generate_long_voice_tracks", lambda *_args: calls.__setitem__("voice", 1) or {})
    monkeypatch.setattr(daily_pipeline, "generate_runninghub_avatars", lambda *_args: calls.__setitem__("avatar", 1) or {})

    result = daily_pipeline.run_daily_pipeline(target)

    assert calls == {"voice": 1, "avatar": 0}
    assert result["status"] == "awaiting_provider_authorization"
    assert result["current_stage"] == "avatar"


def test_text_resilience_awaiting_human_stops_before_project_and_media(monkeypatch):
    run = {
        "target_date": "2026-08-26",
        "status": "awaiting_human",
        "current_stage": "script",
        "project_id": "",
        "stages": {
            "research": {"status": "succeeded"},
            "script": {"status": "awaiting_human"},
            "voice": {"status": "pending"},
            "avatar": {"status": "pending"},
        },
    }
    monkeypatch.setattr(daily_pipeline, "run_research_and_script", lambda *_args, **_kwargs: run)
    monkeypatch.setattr(daily_pipeline, "read_run", lambda *_args, **_kwargs: run)
    monkeypatch.setattr(
        daily_pipeline,
        "generate_long_voice_tracks",
        lambda *_args: pytest.fail("文本未过门时不得启动任何媒体 worker"),
    )

    result = daily_pipeline.run_daily_pipeline("2026-08-26")

    assert result["status"] == "awaiting_human"
    assert result["current_stage"] == "script"
    assert result["project_id"] == ""


def test_supporting_visuals_resume_only_failed_slots(tmp_path, monkeypatch):
    failed_state = {
        "automation": {"visual_batch": {"status": "completed_with_failures", "items": [
            {"scene_id": "section-009", "block_id": "VB-004", "status": "failed", "error": "inspect failed"},
        ]}},
        "scenes": [],
    }
    completed_state = {
        "automation": {"visual_batch": {"status": "completed", "items": [
            {"scene_id": "section-009", "block_id": "VB-004", "status": "completed"},
        ]}},
        "scenes": [{"visual_timeline": {"blocks": [
            {"id": "VB-004", "status": "ready", "asset_id": "S-027"},
        ]}}],
    }
    reads = iter([failed_state, completed_state])
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(daily_pipeline, "_project_dir", lambda _run: tmp_path)
    monkeypatch.setattr(daily_pipeline, "read_workbench", lambda _project: next(reads))
    monkeypatch.setattr(
        daily_pipeline,
        "start_visual_block_refresh",
        lambda _project, scene_id, block_id, _payload: calls.append((scene_id, block_id)) or {
            "automation": {"visual_batch": {"job_id": "VBJ-resume"}}
        },
    )
    monkeypatch.setattr(daily_pipeline, "generate_visual_batch", lambda _project, expected_job_id=None: completed_state)
    monkeypatch.setattr(
        daily_pipeline,
        "preview_visual_batch_plan",
        lambda *_args, **_kwargs: pytest.fail("已有失败槽时不得重新规划整期"),
    )

    result = generate_supporting_visuals({"project_id": "daily-test"})

    assert calls == [("section-009", "VB-004")]
    assert result["planning_mode"] == "failed_slot_resume"
    assert result["failed_slots"] == 0
    assert result["completed_slots"] == 1


def test_premium_h2_script_reaches_voice_then_stops_at_provider_gate(tmp_path, monkeypatch):
    target = "2026-08-27"
    run_dir = tmp_path / target
    run_dir.mkdir(parents=True)
    script = {
        "validation": {"passed": True},
        "editorial_review": {"passed": True, "quality_band": "fallback_publishable", "total": 86,
                             "scores": {"hook": 16, "dialogue": 16, "information_density": 20}},
        "topic_selection": {"selected_stories": [{"heat_level": "H2"}]},
    }
    (run_dir / "daily_script.json").write_text(json.dumps(script), encoding="utf-8")
    run = {
        "target_date": target, "status": "queued", "current_stage": "voice",
        "approval_policy": {"fallback_script_approved": False},
        "stages": {name: {"status": "succeeded" if name in {"research", "script", "project"} else "pending", "output": {}}
                   for name in daily_pipeline.STAGE_ORDER},
    }
    calls = {"voice": 0, "avatar": 0}

    def update(value, stage, status, **kwargs):
        value["stages"][stage]["status"] = status
        if kwargs.get("output"):
            value["stages"][stage]["output"] = kwargs["output"]
        value["current_stage"] = stage
        return value

    monkeypatch.setattr(daily_pipeline, "RUNS_ROOT", tmp_path)
    monkeypatch.setattr(daily_pipeline, "run_research_and_script", lambda *_a, **_k: run)
    monkeypatch.setattr(daily_pipeline, "read_run", lambda *_a, **_k: run)
    monkeypatch.setattr(daily_pipeline, "_save_run", lambda value: value)
    monkeypatch.setattr(daily_pipeline, "update_stage", update)
    monkeypatch.setattr(daily_pipeline, "preflight_daily_media", lambda *_args: {"status": "passed"})
    monkeypatch.setattr(daily_pipeline, "provider_media_eligibility", lambda _run: {"eligible": False, "state": "lite_verification_required", "reason": "Lite未核验"})
    monkeypatch.setattr(daily_pipeline, "generate_long_voice_tracks", lambda _run: calls.__setitem__("voice", calls["voice"] + 1) or {})
    monkeypatch.setattr(daily_pipeline, "generate_runninghub_avatars", lambda _run: calls.__setitem__("avatar", calls["avatar"] + 1) or {})

    result = daily_pipeline.run_daily_pipeline(target)

    assert calls == {"voice": 1, "avatar": 0}
    assert result["media_release_decision"]["decision"] == "auto_release"
    assert result["status"] == "awaiting_provider_authorization"


def test_voicebox_preflight_starts_local_service_once(monkeypatch):
    calls = {"profiles": 0, "process": 0}

    def profiles(cls):
        calls["profiles"] += 1
        if calls["profiles"] == 1:
            raise RuntimeError("connection refused")
        return [{"id": "voice-yaya", "name": "雅雅"}]

    monkeypatch.setattr(daily_pipeline.VoiceboxTTS, "list_profiles", classmethod(profiles))
    monkeypatch.setattr(daily_pipeline.os, "name", "nt")
    monkeypatch.setattr(daily_pipeline.shutil, "which", lambda name: "powershell.exe" if "powershell" in name else None)

    class Result:
        returncode = 0
        stdout = "healthy"
        stderr = ""

    def run(*_args, **_kwargs):
        calls["process"] += 1
        return Result()

    monkeypatch.setattr(daily_pipeline.subprocess, "run", run)

    result = daily_pipeline.ensure_voicebox_ready()

    assert result["started"] is True
    assert calls == {"profiles": 2, "process": 1}


def test_clean_install_uses_distinct_checked_in_role_presets(monkeypatch):
    monkeypatch.setattr(daily_pipeline, "ensure_voicebox_ready", lambda: {"profiles": [
        {"id": "openmontage-qwen-serena", "name": "Qwen Serena"},
        {"id": "openmontage-qwen-dylan", "name": "Qwen Dylan"},
    ]})
    monkeypatch.setattr(daily_pipeline, "ROLE_PROFILE_IDS", {"yaya": "missing-yaya", "mengmeng": "missing-mengmeng"})

    profiles = daily_pipeline._voicebox_profiles()

    assert profiles["yaya"]["id"] == "openmontage-qwen-serena"
    assert profiles["mengmeng"]["id"] == "openmontage-qwen-dylan"


def test_transient_media_stage_retries_only_current_worker(monkeypatch):
    attempts = {"count": 0}
    sleeps = []
    monkeypatch.setattr(daily_pipeline, "heartbeat_stage", lambda run, *_args, **_kwargs: run)

    def worker(_run):
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise RuntimeError("HTTP 503 temporary")
        return {"ok": True}

    result = daily_pipeline._run_stage_with_retry(
        {"target_date": "2026-08-28"}, "visuals", worker, sleeper=sleeps.append
    )

    assert result == {"ok": True}
    assert attempts["count"] == 3
    assert sleeps == [30, 90]


def test_ambiguous_paid_avatar_stage_is_never_retried(monkeypatch):
    attempts = {"count": 0}

    def worker(_run):
        attempts["count"] += 1
        raise RuntimeError("HTTP 503 after submit")

    with pytest.raises(RuntimeError, match="503"):
        daily_pipeline._run_stage_with_retry(
            {"target_date": "2026-08-28"}, "avatar", worker, sleeper=lambda _delay: None
        )

    assert attempts["count"] == 1


class FakeRunningHubClient:
    def __init__(self, outcomes: dict[str, list[dict]]):
        self.outcomes = outcomes
        self.submissions: list[dict] = []
        self._role_attempts = {"yaya": 0, "mengmeng": 0}

    def upload_file(self, path: Path, *, file_type: str) -> str:
        return f"remote-{path.name}"

    def submit(self, *, presenter_filename: str, audio_filename: str, instance_type=None, exact_total_frames: int) -> dict:
        role = "yaya" if "yaya" in audio_filename else "mengmeng"
        self._role_attempts[role] += 1
        task_id = f"{role}-{self._role_attempts[role]}"
        self.submissions.append({
            "role": role,
            "task_id": task_id,
            "instance_type": instance_type,
            "exact_total_frames": exact_total_frames,
        })
        return {"task_id": task_id}

    def poll(self, task_id: str) -> dict:
        result = self.outcomes[task_id].pop(0)
        # The production client always provides a documented usage snapshot.
        # Keep fake outcomes equally auditable so Lite rate guards are tested.
        if result.get("status") != "RUNNING" and "billing" not in result:
            cost = float(result.get("consume_money_cny") or 0)
            # These fixtures model Lite's ¥0.4/h rate.
            result["billing"] = {
                "provider_usage": {"consume_money": cost, "task_cost_seconds": cost * 3600 / 0.4},
                "observed_hourly_rate_cny": 0.4,
                "observed_instance": "lite",
                "instance_evidence": "fixture",
            }
        return result

    def download(self, url: str, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"generated-video" * 512)


def _run(project_dir: Path) -> dict:
    audio_dir = project_dir / "assets" / "audio" / "daily-voice"
    audio_dir.mkdir(parents=True)
    tracks = {}
    for role in ("yaya", "mengmeng"):
        path = audio_dir / f"{role}-longform.wav"
        with wave.open(str(path), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(24_000)
            output.writeframes(b"\x01\x00" * 24_000)
        tracks[role] = {
            "path": f"assets/audio/daily-voice/{role}-longform.wav",
            "sha256": daily_pipeline._file_sha256(path),
            "sample_rate": 24_000,
            "sample_frame_count": 24_000,
            "samples_per_video_frame": 960,
            "video_fps": 25,
            "video_frame_count": 25,
        }
    return {
        "target_date": "2026-08-19",
        "project_id": project_dir.name,
        "status": "running",
        "current_stage": "avatar",
        "budget": {"limit": 5.0, "reserved": 0.0, "spent": 0.0, "entries": []},
        "stages": {
            "voice": {"output": tracks},
            "avatar": {"status": "running", "output": {}},
        },
    }


def test_voice_reuse_requires_matching_text_fingerprint(tmp_path: Path):
    target = tmp_path / "yaya-longform.wav"
    with wave.open(str(target), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(16000)
        stream.writeframes(b"\0\0" * 1600)
    manifest = {
        "tracks": {
            "yaya": {"profile_id": "profile-yaya", "text_sha256": "matching"},
        },
    }

    assert _can_reuse_voice_track(
        manifest, role="yaya", target=target, profile_id="profile-yaya", text_sha256="matching",
    )
    assert not _can_reuse_voice_track(
        manifest, role="yaya", target=target, profile_id="profile-yaya", text_sha256="changed-script",
    )

    target.write_bytes(b"not-a-decodable-wave" * 100)
    assert not _can_reuse_voice_track(
        manifest, role="yaya", target=target, profile_id="profile-yaya", text_sha256="matching",
    )


@pytest.fixture
def pipeline_context(tmp_path: Path, monkeypatch):
    project_dir = tmp_path / "daily-project"
    project_dir.mkdir()
    images = {}
    for role in ("yaya", "mengmeng"):
        path = tmp_path / f"{role}.png"
        path.write_bytes(b"image")
        images[role] = path
    monkeypatch.setattr("backlot.daily_pipeline._project_dir", lambda run: project_dir)
    monkeypatch.setattr("backlot.daily_pipeline._presenter_images", lambda: images)
    monkeypatch.setattr("backlot.daily_pipeline.heartbeat_stage", lambda run, *args, **kwargs: run)
    monkeypatch.setattr("backlot.daily_pipeline.time.sleep", lambda seconds: None)
    monkeypatch.setattr("backlot.daily_automation._save_run", lambda run: run)
    monkeypatch.setattr(
        "backlot.daily_pipeline.read_config",
        lambda: {"runninghub": {"max_lite_attempts": 3}},
    )
    return project_dir


def test_transient_failure_retries_lite_and_never_uses_standard(pipeline_context: Path, monkeypatch):
    fake = FakeRunningHubClient({
        "yaya-1": [{"status": "FAILED", "error": "task queue timeout", "consume_money_cny": 0.1}],
        "yaya-2": [{"status": "SUCCEEDED", "video_url": "https://example/yaya.mp4", "consume_money_cny": 0.1}],
        "mengmeng-1": [{"status": "SUCCEEDED", "video_url": "https://example/mengmeng.mp4", "consume_money_cny": 0.1}],
    })
    monkeypatch.setattr("backlot.daily_pipeline.RunningHubLongCatClient", lambda: fake)

    result = generate_runninghub_avatars(_run(pipeline_context))

    assert {item["instance_type"] for item in fake.submissions} == {None}
    assert [item["role"] for item in fake.submissions].count("yaya") == 2
    assert result["yaya"]["status"] == "completed"
    assert result["mengmeng"]["status"] == "completed"


def test_confirmed_oom_is_the_only_path_to_standard_24gb(pipeline_context: Path, monkeypatch):
    fake = FakeRunningHubClient({
        "yaya-1": [{"status": "FAILED", "error": "CUDA out of memory", "consume_money_cny": 0.1}],
        "yaya-2": [{"status": "SUCCEEDED", "video_url": "https://example/yaya.mp4", "consume_money_cny": 0.2}],
        "mengmeng-1": [{"status": "SUCCEEDED", "video_url": "https://example/mengmeng.mp4", "consume_money_cny": 0.1}],
    })
    monkeypatch.setattr("backlot.daily_pipeline.RunningHubLongCatClient", lambda: fake)

    result = generate_runninghub_avatars(_run(pipeline_context))

    standard = [item for item in fake.submissions if item["instance_type"] == "default"]
    assert len(standard) == 1
    assert standard[0]["role"] == "yaya"
    assert result["yaya"]["history"][-1]["reason"] == "confirmed_oom_only"


def test_user_lite_only_policy_stops_on_oom_without_standard(pipeline_context: Path, monkeypatch):
    fake = FakeRunningHubClient({
        "yaya-1": [{"status": "FAILED", "error": "CUDA out of memory", "consume_money_cny": 0.1}],
    })
    monkeypatch.setattr("backlot.daily_pipeline.RunningHubLongCatClient", lambda: fake)
    run = _run(pipeline_context)
    run["provider_policy"] = {
        "runninghub_primary": "lite",
        "lite_only": True,
        "lite_verified": True,
        "standard_24gb_only_on_oom": False,
    }

    with pytest.raises(DailyAutomationError, match="只授权 0.4 元/小时 Lite"):
        generate_runninghub_avatars(run)

    assert [item["instance_type"] for item in fake.submissions] == [None]
    assert run["stages"]["avatar"]["output"]["roles"]["yaya"]["status"] == "failed"


def test_lite_only_policy_blocks_before_upload_when_rate_is_not_verified(pipeline_context: Path, monkeypatch):
    fake = FakeRunningHubClient({})
    monkeypatch.setattr("backlot.daily_pipeline.RunningHubLongCatClient", lambda: fake)
    monkeypatch.setattr("backlot.daily_pipeline.daily_billing_safety", lambda: {
        "auto_schedule_eligible": False,
        "message": "Lite 账单未验证，禁止付费提交",
    })
    run = _run(pipeline_context)
    run["provider_policy"] = {"runninghub_primary": "lite", "lite_only": True, "lite_verified": False}

    with pytest.raises(DailyAutomationError, match="禁止付费提交"):
        generate_runninghub_avatars(run)

    assert fake.submissions == []


def test_submission_timeout_becomes_ambiguous_and_never_auto_resubmits(pipeline_context: Path, monkeypatch):
    class AmbiguousClient(FakeRunningHubClient):
        def submit(self, *, presenter_filename: str, audio_filename: str, instance_type=None, exact_total_frames: int) -> dict:
            self.submissions.append({"role": "yaya", "instance_type": instance_type})
            raise TimeoutError("network timeout after request body was sent")

    fake = AmbiguousClient({})
    monkeypatch.setattr("backlot.daily_pipeline.RunningHubLongCatClient", lambda: fake)
    run = _run(pipeline_context)

    with pytest.raises(DailyAutomationError, match="禁止自动重提"):
        generate_runninghub_avatars(run)
    assert len(fake.submissions) == 1
    record = run["stages"]["avatar"]["output"]["roles"]["yaya"]
    assert record["status"] == "ambiguous"
    assert run["paid_operations"]["operations"][record["operation_id"]]["state"] == "ambiguous"

    with pytest.raises(DailyAutomationError, match="禁止自动重提"):
        generate_runninghub_avatars(run)
    assert len(fake.submissions) == 1


def test_unverified_or_unexpected_lite_billing_stops_before_second_role(pipeline_context: Path, monkeypatch):
    fake = FakeRunningHubClient({
        "yaya-1": [{
            "status": "SUCCEEDED", "video_url": "https://example/yaya.mp4", "consume_money_cny": 2.871,
            "billing": {
                "provider_usage": {"consume_money": 2.871, "task_cost_seconds": 2584},
                "observed_hourly_rate_cny": 4.0,
                "observed_instance": "standard_24gb",
            },
        }],
    })
    monkeypatch.setattr("backlot.daily_pipeline.RunningHubLongCatClient", lambda: fake)

    with pytest.raises(DailyAutomationError, match="已停止提交后续角色"):
        generate_runninghub_avatars(_run(pipeline_context))

    assert [item["role"] for item in fake.submissions] == ["yaya"]


def test_run_authorized_standard_uses_default_without_lite_guard(pipeline_context: Path, monkeypatch):
    fake = FakeRunningHubClient({
        "yaya-1": [{
            "status": "SUCCEEDED", "video_url": "https://example/yaya.mp4", "consume_money_cny": 0.2,
            "billing": {
                "provider_usage": {"consume_money": 0.2, "task_cost_seconds": 180},
                "observed_hourly_rate_cny": 4.0,
                "observed_instance": "standard_24gb",
            },
        }],
        "mengmeng-1": [{
            "status": "SUCCEEDED", "video_url": "https://example/mengmeng.mp4", "consume_money_cny": 0.2,
            "billing": {
                "provider_usage": {"consume_money": 0.2, "task_cost_seconds": 180},
                "observed_hourly_rate_cny": 4.0,
                "observed_instance": "standard_24gb",
            },
        }],
    })
    monkeypatch.setattr("backlot.daily_pipeline.RunningHubLongCatClient", lambda: fake)
    run = _run(pipeline_context)
    run["provider_policy"] = {
        "runninghub_primary": "standard_24gb",
        "authorized_instance": "default",
        "lite_only": False,
    }

    result = generate_runninghub_avatars(run)

    assert [item["instance_type"] for item in fake.submissions] == ["default", "default"]
    assert result["yaya"]["status"] == "completed"
    assert result["mengmeng"]["status"] == "completed"


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="ffmpeg required")
def test_daily_review_candidate_probes_actual_final_file(tmp_path: Path, monkeypatch):
    project_dir = tmp_path / "project"
    output = project_dir / "renders" / "previews" / "full.mp4"
    output.parent.mkdir(parents=True)
    subprocess.run([
        shutil.which("ffmpeg"), "-y", "-f", "lavfi", "-i", "color=c=blue:s=1080x1920:d=2",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-shortest",
        "-c:v", "libx264", "-preset", "ultrafast", "-af", "loudnorm=I=-14:LRA=11:TP=-1.5", "-c:a", "aac", str(output),
    ], capture_output=True, check=True)
    (project_dir / "artifacts").mkdir(parents=True)
    (project_dir / "artifacts" / "full_preview_render_report.json").write_text(json.dumps({
        "data": {"final_review": {"status": "pass"}},
    }), encoding="utf-8")
    monkeypatch.setattr("backlot.daily_pipeline.read_workbench", lambda _project: {
        "scenes": [{
            "id": "section-001", "end_seconds": 2.0,
            "presenter": {"source_path": "renders/avatar/master.mp4"},
            "visual_timeline": {"blocks": [{"status": "ready", "asset_id": "S-001"}]},
        }],
    })

    qa = validate_daily_review_candidate(project_dir, output)

    assert qa["status"] == "passed"
    assert qa["technical_probe"]["width"] == 1080
    assert qa["technical_probe"]["height"] == 1920
    assert len(qa["frames"]) == 5
    assert (project_dir / "artifacts" / "daily_delivery_qa.json").is_file()


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="ffmpeg required")
def test_daily_review_candidate_rejects_reused_network_asset(tmp_path: Path, monkeypatch):
    project_dir = tmp_path / "project"
    output = project_dir / "renders" / "previews" / "full.mp4"
    output.parent.mkdir(parents=True)
    subprocess.run([
        shutil.which("ffmpeg"), "-y", "-f", "lavfi", "-i", "color=c=blue:s=1080x1920:d=2",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-shortest",
        "-c:v", "libx264", "-preset", "ultrafast", "-af", "loudnorm=I=-14:LRA=11:TP=-1.5",
        "-c:a", "aac", str(output),
    ], capture_output=True, check=True)
    (project_dir / "artifacts").mkdir(parents=True)
    (project_dir / "artifacts" / "full_preview_render_report.json").write_text(json.dumps({
        "data": {"final_review": {"status": "pass"}},
    }), encoding="utf-8")
    monkeypatch.setattr("backlot.daily_pipeline.read_workbench", lambda _project: {
        "assets": [{
            "id": "S-001", "path": "assets/video/pexels/reused.mp4",
            "source_type": "web_download", "provider": "Pexels",
            "generation": {"video_id": "12345"},
        }],
        "scenes": [
            {
                "id": "section-001", "end_seconds": 1.0,
                "presenter": {"source_path": "renders/avatar/master.mp4"},
                "visual_timeline": {"blocks": [{
                    "id": "VB-001", "status": "ready", "asset_id": "S-001",
                    "start_seconds": 0.0, "end_seconds": 1.0,
                }]},
            },
            {
                "id": "section-002", "end_seconds": 2.0,
                "presenter": {"source_path": "renders/avatar/master.mp4"},
                "visual_timeline": {"blocks": [{
                    "id": "VB-001", "status": "ready", "asset_id": "S-001",
                    "start_seconds": 0.0, "end_seconds": 1.0,
                }]},
            },
        ],
    })

    with pytest.raises(DailyAutomationError, match="网络素材重复使用"):
        validate_daily_review_candidate(project_dir, output)
