"""Focused static/mock tests for the no-avatar review-preview parent job."""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import wave
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from backlot import narration_lines
from backlot import review_preview_pipeline as pipeline
from backlot import workbench as wb


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def write_pcm_wav(
    path: Path,
    *,
    duration: float = 0.1,
    sample_rate: int = 24000,
    channels: int = 1,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    frame_count = max(1, round(duration * sample_rate))
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(b"\x00\x00" * frame_count * channels)


def concat_pcm_wav(_project_dir: Path, parts: list[Path], *, output_path: Path | None = None) -> Path:
    assert output_path is not None
    frames: list[bytes] = []
    sample_rate = 24000
    channels = 1
    for part in parts:
        with wave.open(str(part), "rb") as handle:
            assert handle.getsampwidth() == 2
            sample_rate = handle.getframerate()
            channels = handle.getnchannels()
            frames.append(handle.readframes(handle.getnframes()))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(output_path), "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(b"".join(frames))
    return output_path


def probe_mock_preview(project_dir: Path, preview_path: str, report_path: str) -> dict:
    preview = project_dir / preview_path
    report = project_dir / report_path
    assert preview.is_file() and report.is_file()
    return {
        "preview_sha256": hashlib.sha256(preview.read_bytes()).hexdigest(),
        "preview_size_bytes": preview.stat().st_size,
        "report_sha256": hashlib.sha256(report.read_bytes()).hexdigest(),
        "media_probe": {
            "format": {"duration": "1.0"},
            "streams": [{"codec_type": "video"}, {"codec_type": "audio"}],
        },
    }


class FakeRecoverableTTS:
    def __init__(
        self,
        *,
        voice_signature: str = "sig-yaya",
        lose_first_submit_response: bool = False,
        fail_first_task: bool = False,
    ) -> None:
        self.voice_signature = voice_signature
        self.lose_first_submit_response = lose_first_submit_response
        self.fail_first_task = fail_first_task
        self.submit_calls: list[str] = []
        self.query_calls: list[str] = []
        self.download_calls: list[str] = []
        self.tasks_by_request: dict[str, str] = {}
        self._lost = False

    def submit(self, inputs: dict, *, request_id: str) -> dict:
        assert inputs["voice_signature"] == "sig-yaya"
        self.submit_calls.append(request_id)
        generation_id = self.tasks_by_request.setdefault(request_id, f"GEN-{len(self.tasks_by_request) + 1}")
        if self.lose_first_submit_response and not self._lost:
            self._lost = True
            raise RuntimeError("mock response lost after accepted POST")
        return {
            "generation_id": generation_id,
            "request_id": request_id,
            "status": "queued",
            "api_mode": "speak",
            "profile_id": inputs["profile_id"],
            "profile_name": "雅雅",
            "engine": inputs["engine"],
            "language": inputs["language"],
            "voice_signature": self.voice_signature,
        }

    def query(self, generation_id: str, *, api_mode: str = "speak") -> dict:
        assert api_mode == "speak"
        self.query_calls.append(generation_id)
        first_generation = next(iter(self.tasks_by_request.values()), None)
        status = "failed" if self.fail_first_task and generation_id == first_generation else "completed"
        return {
            "generation_id": generation_id,
            "status": status,
            "api_mode": api_mode,
            "voice_signature": self.voice_signature,
        }

    def download(self, generation_id: str, output_path: Path, *, api_mode: str = "speak") -> dict:
        assert api_mode == "speak"
        self.download_calls.append(generation_id)
        write_pcm_wav(output_path, duration=0.1)
        media = narration_lines.inspect_pcm_wav(output_path)
        return {
            "generation_id": generation_id,
            "api_mode": api_mode,
            "voice_signature": self.voice_signature,
            **media,
        }


def recoverable_line_plan() -> dict:
    return narration_lines.build_line_plan(
        [{"id": "sec-01", "text": "可恢复的一句。"}],
        {
            "profile_id": "voice-yaya",
            "profile_name": "雅雅",
            "engine": "qwen_voice_clone",
            "voice_signature": "sig-yaya",
        },
    )


def capabilities(*, explicit: bool = False, selected: str | None = None, yaya: bool = True) -> dict:
    profiles = []
    if yaya:
        profiles.append(
            {
                "id": "voice-yaya",
                "name": "雅雅",
                "language": "zh",
                "voice_type": "clone",
                "default_engine": "qwen_voice_clone",
                "voice_signature": "sig-yaya",
            }
        )
    profiles.append(
        {
            "id": "voice-mengmeng",
            "name": "檬檬",
            "language": "zh",
            "voice_type": "clone",
            "default_engine": "qwen_voice_clone",
            "voice_signature": "sig-mengmeng",
        }
    )
    return {
        "tts": {
            "available": True,
            "status": "available",
            "profiles": profiles,
            "persisted_default_profile_id": selected,
            "explicit_default": explicit,
        },
        "ffmpeg": {"available": True, "path": "mock-ffmpeg"},
        "ffprobe": {"available": True, "path": "mock-ffprobe"},
        "pexels": {"available": True, "network": True, "paid": False},
        "text_ai": {"available": True, "provider": "mock", "model": "mock-text", "network": True},
        "hyperframes": {"available": True, "status": "available"},
        "avatar": {"used": False, "providers": []},
    }


def make_project(tmp_path: Path, *, avatar: bool = False, with_visuals: bool = True) -> Path:
    project = tmp_path / ("avatar-film" if avatar else "film")
    script = {
        "version": "1.0",
        "title": "逐句审核预览测试",
        "total_duration_seconds": 4,
        "sections": [
            {"id": "sec-01", "text": "第一句。第二句！", "start_seconds": 0, "end_seconds": 2},
            {"id": "sec-02", "text": "第三句。", "start_seconds": 2, "end_seconds": 4},
        ],
    }
    write_json(
        project / "project.json",
        {
            "project_id": project.name,
            "title": script["title"],
            "pipeline_type": wb.AVATAR_PIPELINE if avatar else "animated-explainer",
        },
    )
    write_json(project / "artifacts" / "script.json", script)
    if avatar:
        write_json(project / "artifacts" / "workbench.json", {"sentinel": "avatar-state-must-not-change"})
        return project
    write_json(
        project / "artifacts" / "scene_plan.json",
        {
            "version": "1.0",
            "script_sha256": pipeline._json_hash(script),
            "scenes": [
                {"id": "scene-01", "description": "第一段", "start_seconds": 0, "end_seconds": 2, "script_section_id": "sec-01"},
                {"id": "scene-02", "description": "第二段", "start_seconds": 2, "end_seconds": 4, "script_section_id": "sec-02"},
            ],
        },
    )
    state = wb.bootstrap_workbench(project)
    state["project"]["script_draft"] = {
        "status": "approved",
        "approved_at": "2026-08-29T00:00:00Z",
        "script": script,
    }
    state["project"]["intake"]["script_status"] = "draft_approved"
    state["narration_policy"]["playback_gain_db"] = 0.0
    state["music_policy"]["enabled"] = False
    if with_visuals:
        visual_path = project / "assets" / "visual.png"
        visual_path.parent.mkdir(parents=True, exist_ok=True)
        visual_path.write_bytes(b"local-test-visual")
        asset = wb._append_asset(
            project,
            state,
            {
                "name": "本地测试主体画面",
                "type": "image",
                "source_type": "human_provided",
                "path": str(visual_path),
                "provider": "local",
                "source_tool": "provided_asset",
                "license": "test",
            },
        )
        for scene in state["scenes"]:
            wb._append_selected_usage(state, scene["id"], asset["id"], "visual")
            wb._set_single_visual_block(state, scene, asset)
    wb._save(project, state)
    return project


def start_payload(**extra: object) -> dict:
    return {
        "confirmed": True,
        "network_confirmed": True,
        "visual": {"planning_mode": "rule_mix", "image_source": "web_download"},
        **extra,
    }


def test_frozen_visual_signature_tracks_render_fields_and_asset_bytes_but_not_lock(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    state = wb.read_workbench(project)
    script = state["project"]["script_draft"]["script"]
    scene = state["scenes"][0]
    asset = state["assets"][0]
    scene["visual_composition"] = {
        **wb._default_visual_composition(),
        "layout_recipe": "focus_card",
        "overlays": [{
            "id": "VL-001", "role": "hero", "asset_id": asset["id"],
            "start_seconds": .25, "end_seconds": 1.5,
            "source_in_seconds": 0, "source_out_seconds": 1.25,
            "fit": "contain", "locked": False,
        }],
    }
    first = pipeline._current_input_contract(project, state, script)["scene_visual_signature"]
    scene["visual_composition"]["overlays"][0]["locked"] = True
    locked = pipeline._current_input_contract(project, state, script)["scene_visual_signature"]
    assert locked == first

    scene["visual_composition"]["overlays"][0]["end_seconds"] = 1.75
    changed_contract = pipeline._current_input_contract(project, state, script)["scene_visual_signature"]
    assert changed_contract != first

    scene["visual_composition"]["overlays"][0]["end_seconds"] = 1.5
    asset_path = project / str(asset["path"])
    asset_path.write_bytes(asset_path.read_bytes() + b"-changed")
    changed_bytes = pipeline._current_input_contract(project, state, script)["scene_visual_signature"]
    assert changed_bytes != first


def complete_no_gate_review_job(
    project: Path,
    *,
    payload: dict | None = None,
    voice_capabilities: dict | None = None,
) -> tuple[dict, list[str]]:
    """Produce a fully evidenced local/mock completed parent for cache tests."""
    calls: list[str] = []
    started = pipeline.start_review_preview_job(
        project,
        payload or start_payload(),
        capabilities=voice_capabilities or capabilities(),
    )

    def synth(line: dict, output: Path, _voice: dict) -> dict:
        calls.append(f"tts:{line['line_id']}")
        write_pcm_wav(output, duration=0.1)
        return {"task_id": f"task-{line['project_ordinal']}"}

    def start_preview(project_dir: Path, child_payload: dict) -> dict:
        latest = wb._load_for_write(project_dir)
        latest["automation"]["preview_render"].update(
            {
                "status": "generating",
                "version": 1,
                "parent_job_id": child_payload.get("_review_preview_job_id"),
                "input_fingerprint": child_payload.get("_review_preview_input_fingerprint"),
            }
        )
        calls.append("preview:start")
        return wb._save(project_dir, latest)

    def generate_preview(project_dir: Path) -> dict:
        latest = wb._load_for_write(project_dir)
        preview = project_dir / "renders" / "previews" / "cache-evidence.mp4"
        report = project_dir / wb.AUTOMATION_PREVIEW_RENDER_REPORT
        preview.parent.mkdir(parents=True, exist_ok=True)
        preview.write_bytes(b"cache-evidence-preview")
        write_json(report, {"status": "completed", "kind": "full_preview"})
        latest["automation"]["preview_render"].update(
            {
                "status": "completed",
                "output_path": preview.relative_to(project_dir).as_posix(),
                "report_path": wb.AUTOMATION_PREVIEW_RENDER_REPORT,
            }
        )
        calls.append("preview:completed")
        return wb._save(project_dir, latest)

    completed = pipeline.run_review_preview_job(
        project,
        started["job_id"],
        dependencies={
            "synthesize_line": synth,
            "concat_audio": concat_pcm_wav,
            "start_full_preview": start_preview,
            "generate_full_preview": generate_preview,
            "probe_preview": probe_mock_preview,
        },
    )
    assert completed["status"] == "completed", completed
    return completed, calls


def test_sentence_plan_is_stable_and_splits_long_minor_clauses() -> None:
    text = "这是第一句。" + "非常长的说明，" * 12 + "到这里结束！"
    first = narration_lines.split_section_text(text, max_chars=24)
    second = narration_lines.split_section_text(text, max_chars=24)
    assert first == second
    assert first[0] == "这是第一句。"
    assert all(line.strip() for line in first)
    assert all(len(line) <= 24 for line in first)
    assert narration_lines.stable_line_id("sec-a", 1, first[0]) == narration_lines.stable_line_id("sec-a", 1, first[0])


def test_line_ledger_reuses_completed_audio_and_regenerates_only_changed_line(tmp_path: Path) -> None:
    project = tmp_path / "ledger"
    voice = {"profile_id": "voice-yaya", "profile_name": "雅雅", "engine": "qwen"}
    first_plan = narration_lines.build_line_plan([{"id": "s1", "text": "甲句。乙句。"}], voice)
    calls: list[str] = []

    def synth(line: dict, output: Path, _voice: dict) -> dict:
        calls.append(line["text"])
        write_pcm_wav(output, duration=0.08)
        return {"generation_id": f"tts-{len(calls)}"}

    first = narration_lines.materialize_line_audio(project, first_plan, synth)
    assert first["completed_count"] == 2
    assert calls == ["甲句。", "乙句。"]
    for record in first["lines"]:
        assert record["codec"] == "pcm_s16le"
        assert record["sample_rate"] == 24000
        assert record["channels"] == 1
        assert record["duration_seconds"] == pytest.approx(0.08, abs=0.001)
        assert record["sha256"] == hashlib.sha256((project / record["output_path"]).read_bytes()).hexdigest()

    stale_evidence = narration_lines.load_ledger(project)
    stale_evidence["lines"][0].update({"sample_rate": 1, "duration_seconds": 999, "size_bytes": 0})
    write_json(project / narration_lines.LEDGER_PATH, stale_evidence)
    reprobed = narration_lines.materialize_line_audio(project, first_plan, synth)
    assert reprobed["lines"][0]["sample_rate"] == 24000
    assert reprobed["lines"][0]["duration_seconds"] == pytest.approx(0.08, abs=0.001)
    assert reprobed["lines"][0]["size_bytes"] > 44
    assert calls == ["甲句。", "乙句。"]

    changed_plan = narration_lines.build_line_plan([{"id": "s1", "text": "甲句。乙句改了。"}], voice)
    second = narration_lines.materialize_line_audio(project, changed_plan, synth)
    assert calls == ["甲句。", "乙句。", "乙句改了。"]
    assert second["lines"][0]["reused"] is True
    assert second["lines"][1]["reused"] is False
    assert any(item["text"] == "乙句。" for item in second["history"])


def test_line_ledger_failure_resumes_after_completed_line(tmp_path: Path) -> None:
    project = tmp_path / "resume-ledger"
    plan = narration_lines.build_line_plan(
        [{"id": "s1", "text": "第一句。第二句。第三句。"}],
        {"profile_id": "voice-yaya", "profile_name": "雅雅", "engine": "qwen"},
    )
    calls: list[str] = []
    failed_once = {"value": False}

    def flaky(line: dict, output: Path, _voice: dict) -> dict:
        calls.append(line["text"])
        if line["text"] == "第二句。" and not failed_once["value"]:
            failed_once["value"] = True
            raise RuntimeError("mock tts interruption")
        write_pcm_wav(output)
        return {"task_id": "mock-task"}

    with pytest.raises(narration_lines.NarrationLineError, match="mock tts interruption"):
        narration_lines.materialize_line_audio(project, plan, flaky)
    failed = narration_lines.load_ledger(project)
    assert [item["status"] for item in failed["lines"]] == ["completed", "failed", "planned"]

    completed = narration_lines.materialize_line_audio(project, plan, flaky)
    assert completed["status"] == "completed"
    assert calls.count("第一句。") == 1
    assert calls.count("第二句。") == 2
    assert calls.count("第三句。") == 1


def test_yaya_is_exact_default_only_without_explicit_persisted_choice(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    default_report = pipeline.review_preview_preflight(project, capabilities=capabilities())
    assert default_report["ready"] is True
    assert default_report["frozen_voice"]["profile_name"] == "雅雅"
    assert default_report["frozen_voice"]["selection_source"] == "required_yaya_default"

    explicit_report = pipeline.review_preview_preflight(
        project,
        capabilities=capabilities(explicit=True, selected="voice-mengmeng"),
    )
    assert explicit_report["ready"] is True
    assert explicit_report["frozen_voice"]["profile_name"] == "檬檬"
    assert explicit_report["frozen_voice"]["selection_source"] == "explicit_global_default"

    missing = pipeline.review_preview_preflight(project, capabilities=capabilities(yaya=False))
    assert missing["ready"] is False
    assert any("雅雅" in blocker and "静默回退" in blocker for blocker in missing["blockers"])


def test_explicit_doubao_default_is_visible_to_review_preview_preflight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = make_project(tmp_path)
    public_profile = {
        "id": "doubao:public_female",
        "name": "豆包公版女声",
        "language": "zh",
        "voice_type": "cloud",
        "default_engine": "doubao_speech_2_0",
        "voice_signature": "doubao:fixture-signature",
        "provider_id": "doubao",
        "provider_name": "豆包云端配音",
        "available": True,
    }
    runtime_profile = {**public_profile, "provider_voice_id": "fixture-voice", "resource_id": "seed-tts-2.0"}
    monkeypatch.setattr(
        pipeline.audio_center,
        "_load",
        lambda: {"default_profile_id": public_profile["id"], "default_updated_at": "2026-09-02T00:00:00Z"},
    )
    monkeypatch.setattr(
        pipeline.audio_center,
        "read_audio_center",
        lambda: {"provider": {"status": "available"}, "profiles": [public_profile]},
    )
    monkeypatch.setattr(pipeline.audio_center, "get_voice_profile", lambda profile_id: runtime_profile if profile_id == public_profile["id"] else None)
    monkeypatch.setattr(wb, "_ffmpeg_available", lambda: "mock-ffmpeg")
    monkeypatch.setattr(wb, "_ffprobe_available", lambda _ffmpeg: "mock-ffprobe")

    report = pipeline.review_preview_preflight(
        project,
        capabilities=pipeline.collect_review_preview_capabilities(include_visual_runtime=False),
    )
    assert report["ready"] is True
    assert report["frozen_voice"]["provider"] == "doubao"
    assert report["frozen_voice"]["profile_id"] == "doubao:public_female"
    assert not any("默认音色已不存在" in blocker for blocker in report["blockers"])


def test_frozen_cloud_line_uses_unified_runtime_without_local_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = {
        "id": "doubao:public_female",
        "name": "豆包公版女声",
        "provider_id": "doubao",
        "provider_name": "豆包云端配音",
        "provider_voice_id": "fixture-voice",
        "available": True,
    }
    monkeypatch.setattr(pipeline.audio_center, "get_voice_profile", lambda _profile_id: profile)
    received: dict = {}

    def generate(*, text: str, profile: dict, output_path: Path, language: str):
        received.update({"text": text, "profile": profile, "language": language})
        write_pcm_wav(output_path)
        return SimpleNamespace(success=True, error="", data={"task_id": "doubao-fixture"})

    monkeypatch.setattr(pipeline, "generate_voice_audio", generate)
    output = tmp_path / "line.wav"
    result = pipeline._synthesize_frozen_line(
        {"text": "云端配音测试。"},
        output,
        {"provider": "doubao", "profile_id": "doubao:public_female", "voice_signature": "doubao:fixture-signature"},
    )
    assert output.is_file()
    assert received["profile"]["provider_id"] == "doubao"
    assert result["voice_signature"] == "doubao:fixture-signature"


def test_inherited_yaya_gain_is_trusted_but_user_mix_changes_keep_audio_gate(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    state = wb._load_for_write(project)
    state["narration_policy"].update({"playback_gain_db": 3.0, "updated_at": None})
    state["music_policy"]["enabled"] = False
    voice = {"profile_name": "雅雅", "selection_source": "required_yaya_default"}

    inherited = pipeline._audio_gate_policy(state, voice)
    assert inherited["required"] is False
    assert inherited["trusted_default"] is True
    assert inherited["will_pause"] is False

    state["narration_policy"]["updated_at"] = "2026-08-30T00:00:00Z"
    changed = pipeline._audio_gate_policy(state, voice)
    assert changed["required"] is True
    assert changed["trusted_default"] is False

    state["narration_policy"]["updated_at"] = None
    state["music_policy"]["enabled"] = True
    assert pipeline._audio_gate_policy(state, voice)["required"] is True

    state["music_policy"]["enabled"] = False
    assert pipeline._audio_gate_policy(state, {"profile_name": "檬檬"})["required"] is True

    state["narration_policy"].update({"playback_gain_db": 0.0, "updated_at": "changed"})
    unity = pipeline._audio_gate_policy(state, {"profile_name": "檬檬"})
    assert unity["required"] is False
    assert unity["trusted_default"] is False


def test_trusted_default_parent_crosses_real_full_preview_starter_without_sample(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = make_project(tmp_path, with_visuals=True)
    state = wb._load_for_write(project)
    state["narration_policy"].update({"playback_gain_db": 3.0, "updated_at": None})
    state["music_policy"]["enabled"] = False
    wb._save(project, state)
    preflight = pipeline.review_preview_preflight(project, capabilities=capabilities())
    assert preflight["music_gate"]["trusted_default"] is True
    assert preflight["will_pause_for_audio_sample"] is False
    started = pipeline.start_review_preview_job(project, start_payload(), capabilities=capabilities())
    calls: list[str] = []

    def synth(line: dict, output: Path, _voice: dict) -> dict:
        calls.append(f"tts:{line['line_id']}")
        write_pcm_wav(output, duration=0.1)
        return {"task_id": f"task-{line['project_ordinal']}"}

    def generate_preview(project_dir: Path) -> dict:
        latest = wb._load_for_write(project_dir)
        preview_job = latest["automation"]["preview_render"]
        assert preview_job["status"] == "generating", "must use the real workbench starter"
        assert preview_job["parent_job_id"] == started["job_id"]
        preview = project_dir / "renders" / "previews" / "trusted-default.mp4"
        report = project_dir / wb.AUTOMATION_PREVIEW_RENDER_REPORT
        preview.parent.mkdir(parents=True, exist_ok=True)
        preview.write_bytes(b"trusted-default-preview")
        write_json(report, {"status": "completed", "kind": "full_preview"})
        preview_job.update(
            {
                "status": "completed",
                "output_path": preview.relative_to(project_dir).as_posix(),
                "report_path": wb.AUTOMATION_PREVIEW_RENDER_REPORT,
            }
        )
        calls.append("preview:completed")
        return wb._save(project_dir, latest)

    monkeypatch.setattr(wb, "_ffmpeg_available", lambda: "mock-ffmpeg")
    completed = pipeline.run_review_preview_job(
        project,
        started["job_id"],
        dependencies={
            "synthesize_line": synth,
            "concat_audio": concat_pcm_wav,
            "generate_full_preview": generate_preview,
            "probe_preview": probe_mock_preview,
            "start_audio_sample": lambda *_args: pytest.fail("trusted default must not start a sample"),
            "generate_audio_sample": lambda *_args: pytest.fail("trusted default must not generate a sample"),
        },
    )
    assert completed["status"] == "completed", completed
    assert completed["phases"]["audio_sample"]["output"] == {"required": False}
    assert "preview:completed" in calls


def test_zero_network_preflight_skips_all_visual_runtime_probes(tmp_path: Path, monkeypatch) -> None:
    project = make_project(tmp_path, with_visuals=True)
    seen: list[bool] = []

    def capability_probe(*, include_visual_runtime: bool) -> dict:
        seen.append(include_visual_runtime)
        return capabilities()

    monkeypatch.setattr(pipeline, "collect_review_preview_capabilities", capability_probe)
    report = pipeline.review_preview_preflight(project)
    assert report["ready"] is True
    assert report["visual_generation_required"] is False
    assert seen == [False]


def test_capability_collection_does_not_touch_visual_tools_when_not_required(monkeypatch) -> None:
    def forbidden(*_args, **_kwargs):
        raise AssertionError("zero-network capability collection touched a visual runtime")

    monkeypatch.setattr(pipeline.audio_center, "_load", lambda: {"default_profile_id": None})
    monkeypatch.setattr(
        pipeline.audio_center,
        "read_audio_center",
        lambda: {"provider": {"status": "available"}, "profiles": capabilities()["tts"]["profiles"]},
    )
    monkeypatch.setattr(wb, "_ffmpeg_available", lambda: "mock-ffmpeg")
    monkeypatch.setattr(wb, "_ffprobe_available", lambda _ffmpeg: "mock-ffprobe")
    monkeypatch.setattr(pipeline, "PexelsSource", forbidden)
    monkeypatch.setattr(pipeline, "HyperFramesCompose", forbidden)
    monkeypatch.setattr(pipeline, "read_text_ai_config", forbidden)

    report = pipeline.collect_review_preview_capabilities(include_visual_runtime=False)
    assert report["tts"]["available"] is True
    assert report["ffmpeg"]["available"] is True
    assert report["ffprobe"]["available"] is True
    assert report["pexels"]["status"] == "skipped_not_required"
    assert report["text_ai"]["available"] is False
    assert report["hyperframes"]["status"] == "skipped_not_required"


def test_start_is_idempotent_freezes_voice_and_rejects_parallel_manual_jobs(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    first = pipeline.start_review_preview_job(project, start_payload(), capabilities=capabilities())
    repeated = pipeline.start_review_preview_job(project, start_payload(), capabilities=capabilities())
    assert repeated["job_id"] == first["job_id"]
    assert first["launch_required"] is True
    assert repeated["launch_required"] is False
    assert "worker_token" not in first
    assert "launch_required" not in wb._load_for_write(project)["automation"]["review_preview_pipeline"]
    (project / "assets" / "visual.png").write_bytes(b"changed-while-parent-active")
    progressed_repeat = pipeline.start_review_preview_job(project, start_payload(), capabilities=capabilities())
    assert progressed_repeat["job_id"] == first["job_id"]
    assert progressed_repeat["launch_required"] is False
    assert first["frozen_input"]["voice"]["profile_name"] == "雅雅"

    with pytest.raises(pipeline.ReviewPreviewError, match="已有一键审核预览"):
        pipeline.start_review_preview_job(
            project,
            start_payload(),
            capabilities=capabilities(explicit=True, selected="voice-mengmeng"),
        )
    with pytest.raises(wb.WorkbenchError, match="一键审核预览"):
        wb.start_music_sample(project)
    with pytest.raises(wb.WorkbenchError, match="一键审核预览"):
        wb.start_project_narration(project, {"confirmed": True})
    with pytest.raises(wb.WorkbenchError, match="一键审核预览"):
        wb.start_full_preview_render(project, {"confirmed": True})
    with pytest.raises(wb.WorkbenchError, match="一键审核预览"):
        wb.start_visual_batch_generation(project, {"confirmed": True})


def test_unsaved_ppt_card_brief_is_read_stable_and_start_does_not_false_conflict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = make_project(tmp_path)
    workbench_path = project / wb.WORKBENCH_FILE
    persisted = wb._read_json(workbench_path)
    for scene in persisted["scenes"]:
        scene.pop("ppt_card_brief", None)
    write_json(workbench_path, persisted)

    monkeypatch.setattr(wb, "_now", lambda: "2026-08-29T01:00:00Z")
    first = wb.read_workbench(project)
    monkeypatch.setattr(wb, "_now", lambda: "2026-08-29T01:00:02Z")
    second = wb.read_workbench(project)

    assert pipeline._json_hash(first) == pipeline._json_hash(second)
    assert all(scene["ppt_card_brief"]["updated_at"] is None for scene in first["scenes"])
    assert wb._read_json(workbench_path) == persisted

    started = pipeline.start_review_preview_job(project, start_payload(), capabilities=capabilities())
    assert started["status"] == "queued"
    assert started["launch_required"] is True


def test_start_persisted_revision_cas_rejects_relevant_concurrent_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = make_project(tmp_path)
    original_lock = pipeline._project_lock

    @contextmanager
    def interleaved_project_lock(project_dir: Path):
        with original_lock(project_dir):
            latest = wb._load_for_write(project_dir)
            latest["narration_policy"]["playback_gain_db"] = 3.0
            wb._save(project_dir, latest)
            yield

    monkeypatch.setattr(pipeline, "_project_lock", interleaved_project_lock)
    with pytest.raises(pipeline.ReviewPreviewConflict, match="项目输入在预检后已变化"):
        pipeline.start_review_preview_job(project, start_payload(), capabilities=capabilities())

    latest = wb._load_for_write(project)
    assert latest["narration_policy"]["playback_gain_db"] == 3.0
    assert latest["automation"]["review_preview_pipeline"]["status"] == "idle"


def test_existing_manual_job_blocks_parent_start(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    state = wb._load_for_write(project)
    state["automation"]["visual_batch"]["status"] = "queued"
    wb._save(project, state)
    with pytest.raises(pipeline.ReviewPreviewError, match="手动画面批量"):
        pipeline.start_review_preview_job(project, start_payload(), capabilities=capabilities())


def test_avatar_project_is_hard_rejected_without_touching_avatar_state(tmp_path: Path) -> None:
    project = make_project(tmp_path, avatar=True)
    state_path = project / "artifacts" / "workbench.json"
    before = state_path.read_bytes()
    report = pipeline.review_preview_preflight(project)
    assert report["ready"] is False
    assert "数字人口播" in report["blockers"][0]
    assert state_path.read_bytes() == before
    assert not (project / "artifacts" / "narration_lines.json").exists()


def test_stale_worker_cannot_overwrite_replaced_parent_job(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    original = pipeline.start_review_preview_job(project, start_payload(), capabilities=capabilities())
    state = wb._load_for_write(project)
    state["automation"]["review_preview_pipeline"]["job_id"] = "RPP-newer"
    state["automation"]["review_preview_pipeline"]["worker_token"] = "new-token"
    wb._save(project, state)
    with pytest.raises(pipeline.StaleReviewPreviewWorker, match="拒绝写入"):
        pipeline._mutate_job(project, original["job_id"], None, lambda _state, job: job.update(status="completed"))
    assert pipeline.read_review_preview_job(project)["job_id"] == "RPP-newer"


def test_restart_recovery_requeues_same_job_at_safe_point(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    job = pipeline.start_review_preview_job(project, start_payload(), capabilities=capabilities())
    state = wb._load_for_write(project)
    parent = state["automation"]["review_preview_pipeline"]
    parent.update({"status": "running", "stage": "narration", "safe_resume_point": "narration", "worker_token": "dead-process"})
    wb._save(project, state)
    recovered = pipeline.recover_review_preview_job(project)
    assert recovered["job_id"] == job["job_id"]
    assert recovered["status"] == "queued"
    assert recovered["stage"] == "narration"
    assert recovered["launch_required"] is True
    assert "worker_token" not in recovered


def test_parent_uses_real_line_clock_waits_for_audio_gate_and_stops_at_preview_ready(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    preflight = pipeline.review_preview_preflight(project, capabilities=capabilities())
    assert preflight["ready"] is True
    assert preflight["project_type"] == "animated-explainer"
    state = wb._load_for_write(project)
    state["narration_policy"]["playback_gain_db"] = 3.0
    state["narration_policy"]["updated_at"] = "user-changed"
    wb._save(project, state)
    job = pipeline.start_review_preview_job(project, start_payload(), capabilities=capabilities())
    calls: list[str] = []

    def synth(line: dict, output: Path, voice: dict) -> dict:
        calls.append(f"tts:{line['line_id']}:{voice['profile_name']}")
        write_pcm_wav(output, duration=0.1)
        return {"task_id": f"task-{line['project_ordinal']}"}

    def start_sample(project_dir: Path, payload: dict) -> dict:
        assert payload["_review_preview_job_id"] == job["job_id"]
        assert payload["_review_preview_worker_token"]
        latest = wb._load_for_write(project_dir)
        sample = latest["music_policy"]["sample"]
        sample.update(
            {
                "status": "generating",
                "job_id": "sample-1",
                "policy_signature": wb._audio_mix_signature(latest),
                "scene_id": "scene-01",
            }
        )
        wb._save(project_dir, latest)
        calls.append("sample:start")
        return latest

    def generate_sample(project_dir: Path) -> dict:
        latest = wb._load_for_write(project_dir)
        path = project_dir / "renders" / "music-samples" / "sample.mp4"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"mock-sample")
        latest["music_policy"]["sample"].update(
            {
                "status": "ready",
                "output_path": path.relative_to(project_dir).as_posix(),
                "policy_signature": wb._audio_mix_signature(latest),
            }
        )
        wb._save(project_dir, latest)
        calls.append("sample:ready")
        return latest

    def approve_sample(project_dir: Path, payload: dict) -> dict:
        assert payload["confirmed"] is True
        latest = wb._load_for_write(project_dir)
        latest["music_policy"]["sample"]["status"] = "approved"
        latest["music_policy"]["sample"]["approved_at"] = "now"
        wb._save(project_dir, latest)
        calls.append("sample:approved")
        return latest

    def start_preview(project_dir: Path, payload: dict) -> dict:
        assert payload["confirmed"] is True
        assert payload["_review_preview_job_id"] == job["job_id"]
        assert payload["_review_preview_worker_token"]
        latest = wb._load_for_write(project_dir)
        latest["automation"]["preview_render"].update({"status": "generating", "version": 1})
        wb._save(project_dir, latest)
        calls.append("preview:start")
        return latest

    def generate_preview(project_dir: Path) -> dict:
        latest = wb._load_for_write(project_dir)
        preview = project_dir / "renders" / "previews" / "mock-review.mp4"
        report = project_dir / wb.AUTOMATION_PREVIEW_RENDER_REPORT
        preview.parent.mkdir(parents=True, exist_ok=True)
        preview.write_bytes(b"mock-review-preview")
        write_json(report, {"status": "completed", "kind": "full_preview"})
        latest["automation"]["preview_render"].update(
            {
                "status": "completed",
                "output_path": preview.relative_to(project_dir).as_posix(),
                "report_path": wb.AUTOMATION_PREVIEW_RENDER_REPORT,
                "version": 1,
            }
        )
        wb._save(project_dir, latest)
        calls.append("preview:completed")
        return latest

    deps = {
        "synthesize_line": synth,
        "concat_audio": concat_pcm_wav,
        "start_audio_sample": start_sample,
        "generate_audio_sample": generate_sample,
        "approve_audio_sample": approve_sample,
        "start_full_preview": start_preview,
        "generate_full_preview": generate_preview,
        "probe_preview": probe_mock_preview,
    }
    waiting = pipeline.run_review_preview_job(project, job["job_id"], dependencies=deps)
    assert waiting["status"] == "awaiting_human"
    assert waiting["stage"] == "audio_sample"
    assert waiting["gate"]["sample_path"].endswith("sample.mp4")
    ledger = narration_lines.load_ledger(project)
    assert ledger["parent_job_id"] == job["job_id"]
    assert ledger["worker_token"]
    assert all(item["status"] == "completed" for item in ledger["lines"])

    queued = pipeline.resume_review_preview_job(
        project,
        job["job_id"],
        {"confirmed": True},
        dependencies=deps,
    )
    assert queued["status"] == "queued"
    assert queued["launch_required"] is True
    assert queued["stage"] == "full_preview"
    completed = pipeline.run_review_preview_job(project, job["job_id"], dependencies=deps)
    assert completed["status"] == "completed"
    assert completed["stage"] == "review_ready"
    assert completed["result"]["readiness"] == "preview_ready"
    assert completed["result"]["script_hash"] == completed["script_hash"]
    assert completed["result"]["voice"]["profile_name"] == "雅雅"
    assert "sample:approved" in calls
    assert "preview:completed" in calls
    assert all("formal" not in call and "approve_scene" not in call for call in calls)
    reused = pipeline.start_review_preview_job(project, start_payload(), capabilities=capabilities())
    assert reused["job_id"] == completed["job_id"]
    assert reused["launch_required"] is False

    refreshed_state = wb._load_for_write(project)
    refreshed_state["automation"]["preview_render"]["needs_refresh"] = True
    wb._save(project, refreshed_state)
    replacement = pipeline.start_review_preview_job(project, start_payload(), capabilities=capabilities())
    assert replacement["job_id"] != completed["job_id"]
    assert replacement["launch_required"] is True

    latest = wb.read_workbench(project)
    assert all(scene["review_status"] != "approved" for scene in latest["scenes"])
    assert latest["automation"]["render"]["status"] != "completed"
    first_scene = latest["scenes"][0]
    version = next(
        item
        for item in first_scene["narration"]["versions"]
        if item["id"] == first_scene["narration"]["current_version_id"]
    )
    assert [cue["line_id"] for cue in version["subtitle_cues"]] == [
        version["line_ids"][0],
        version["line_ids"][1],
    ]
    assert version["subtitle_cues"][0]["end_seconds"] == pytest.approx(0.1, abs=0.001)
    assert version["subtitle_cues"][1]["start_seconds"] == pytest.approx(0.1, abs=0.001)
    assert (project / latest["automation"]["narration_generation"]["subtitle_path"]).is_file()


def test_full_preview_failure_marks_subjob_failed_and_resumes_from_same_safe_point(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    job = pipeline.start_review_preview_job(project, start_payload(), capabilities=capabilities())
    failed_once = {"value": False}

    def synth(_line: dict, output: Path, _voice: dict) -> dict:
        write_pcm_wav(output)
        return {"task_id": "mock"}

    def start_preview(project_dir: Path, _payload: dict) -> dict:
        state = wb._load_for_write(project_dir)
        state["automation"]["preview_render"].update({"status": "generating", "version": 1, "error": ""})
        wb._save(project_dir, state)
        return state

    def generate_preview(project_dir: Path) -> dict:
        if not failed_once["value"]:
            failed_once["value"] = True
            raise wb.WorkbenchError("mock ffmpeg interruption")
        state = wb._load_for_write(project_dir)
        preview = project_dir / "renders" / "previews" / "resumed.mp4"
        report = project_dir / wb.AUTOMATION_PREVIEW_RENDER_REPORT
        preview.parent.mkdir(parents=True, exist_ok=True)
        preview.write_bytes(b"resumed")
        write_json(report, {"status": "completed"})
        state["automation"]["preview_render"].update(
            {
                "status": "completed",
                "output_path": preview.relative_to(project_dir).as_posix(),
                "report_path": wb.AUTOMATION_PREVIEW_RENDER_REPORT,
                "error": "",
            }
        )
        wb._save(project_dir, state)
        return state

    deps = {
        "synthesize_line": synth,
        "concat_audio": concat_pcm_wav,
        "start_full_preview": start_preview,
        "generate_full_preview": generate_preview,
        "probe_preview": probe_mock_preview,
    }
    failed = pipeline.run_review_preview_job(project, job["job_id"], dependencies=deps)
    assert failed["status"] == "failed"
    assert failed["safe_resume_point"] == "full_preview"
    assert wb.read_workbench(project)["automation"]["preview_render"]["status"] == "failed"

    resumed = pipeline.resume_review_preview_job(project, job["job_id"])
    assert resumed["status"] == "queued"
    assert resumed["launch_required"] is True
    completed = pipeline.run_review_preview_job(project, job["job_id"], dependencies=deps)
    assert completed["status"] == "completed"
    assert completed["result"]["readiness"] == "preview_ready"


def test_preflight_exposes_frozen_contract_and_never_authorizes_avatar_or_openai_image(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    report = pipeline.review_preview_preflight(project, capabilities=capabilities())
    assert report["ready"] is True
    assert report["line_count"] == 3
    assert report["script_review_status"] == "approved"
    assert report["script_hash"]
    assert report["capabilities"]["ffmpeg"]["available"] is True
    assert report["capabilities"]["ffprobe"]["available"] is True
    assert report["capabilities"]["pexels"]["available"] is True
    assert report["visual_generation_required"] is False
    assert report["visual_strategy"]["openai_image"] is False
    assert report["visual_strategy"]["avatar"] is False
    assert "不调用 RunningHub" in report["declaration"]


def test_default_visual_route_requires_ai_director_model_and_explicit_rule_mix_can_opt_out(
    tmp_path: Path,
) -> None:
    default_project = make_project(tmp_path / "default-ai", with_visuals=False)
    available = pipeline.review_preview_preflight(default_project, capabilities=capabilities())
    assert available["ready"] is True
    assert available["visual_strategy"]["planning_mode"] == "ai_director"
    assert available["visual_strategy"]["text_ai_model"] == "mock-text"
    started = pipeline.start_review_preview_job(
        default_project,
        {"confirmed": True, "network_confirmed": True, "text_ai_confirmed": True},
        capabilities=capabilities(),
    )
    assert started["frozen_input"]["visual"]["planning_mode"] == "ai_director"
    assert started["frozen_input"]["authorizations"]["text_ai"] is True

    unavailable_caps = capabilities()
    unavailable_caps["text_ai"] = {
        "available": False,
        "provider": "mock",
        "model": None,
        "network": True,
    }
    blocked_project = make_project(tmp_path / "blocked-ai", with_visuals=False)
    blocked = pipeline.review_preview_preflight(blocked_project, capabilities=unavailable_caps)
    assert blocked["ready"] is False
    assert blocked["visual_strategy"]["planning_mode"] == "ai_director"
    assert any("文本模型尚未配置" in item for item in blocked["blockers"])
    with pytest.raises(pipeline.ReviewPreviewError, match="文本模型尚未配置"):
        pipeline.start_review_preview_job(
            blocked_project,
            {"confirmed": True, "network_confirmed": True, "text_ai_confirmed": True},
            capabilities=unavailable_caps,
        )
    assert not (blocked_project / narration_lines.LEDGER_PATH).exists()

    rule_project = make_project(tmp_path / "explicit-rule", with_visuals=False)
    rule_payload = start_payload()
    rule_report = pipeline.review_preview_preflight(
        rule_project,
        rule_payload,
        capabilities=unavailable_caps,
    )
    assert rule_report["ready"] is True
    assert rule_report["visual_strategy"]["planning_mode"] == "rule_mix"
    assert rule_report["visual_strategy"]["text_ai_model"] is None
    rule_started = pipeline.start_review_preview_job(
        rule_project,
        rule_payload,
        capabilities=unavailable_caps,
    )
    assert rule_started["frozen_input"]["authorizations"]["text_ai"] is False


def test_local_visuals_allow_zero_network_preflight_start_and_worker(
    tmp_path: Path,
) -> None:
    project = make_project(tmp_path)
    local_caps = capabilities()
    local_caps["pexels"] = {"available": False, "network": True, "paid": False}
    local_caps["text_ai"] = {"available": False, "provider": "mock", "model": None, "network": True}
    local_caps["hyperframes"] = {"available": False, "status": "unavailable"}
    payload = {"confirmed": True}

    report = pipeline.review_preview_preflight(project, payload, capabilities=local_caps)
    assert report["ready"] is True
    assert report["visual_generation_required"] is False
    assert report["visual_strategy"]["visual_generation_required"] is False
    assert report["visual_strategy"]["pexels_network"] is False
    assert report["visual_strategy"]["text_ai_model"] is None
    assert not any("Pexels 尚未配置" in item for item in report["blockers"])
    assert not any("文本模型尚未配置" in item for item in report["blockers"])

    started = pipeline.start_review_preview_job(project, payload, capabilities=local_caps)
    assert started["launch_required"] is True
    assert started["frozen_input"]["visual_generation_required"] is False
    assert started["frozen_input"]["authorizations"]["pexels_network"] is False
    assert started["frozen_input"]["authorizations"]["text_ai"] is False
    assert started["preflight"]["visual_generation_required"] is False

    calls = {"visual_plan": 0, "visual_start": 0, "visual_generate": 0}

    def forbidden_visual(name: str):
        def invoke(*_args, **_kwargs):
            calls[name] += 1
            raise AssertionError(f"zero-network local path called {name}")
        return invoke

    def synth(line: dict, output: Path, _voice: dict) -> dict:
        write_pcm_wav(output, duration=0.1)
        return {"task_id": f"task-{line['project_ordinal']}"}

    def start_preview(project_dir: Path, child_payload: dict) -> dict:
        latest = wb._load_for_write(project_dir)
        latest["automation"]["preview_render"].update(
            {
                "status": "generating",
                "version": 1,
                "parent_job_id": child_payload.get("_review_preview_job_id"),
                "input_fingerprint": child_payload.get("_review_preview_input_fingerprint"),
            }
        )
        return wb._save(project_dir, latest)

    def generate_preview(project_dir: Path) -> dict:
        latest = wb._load_for_write(project_dir)
        preview = project_dir / "renders" / "previews" / "zero-network.mp4"
        report_path = project_dir / wb.AUTOMATION_PREVIEW_RENDER_REPORT
        preview.parent.mkdir(parents=True, exist_ok=True)
        preview.write_bytes(b"zero-network-preview")
        write_json(report_path, {"status": "completed", "kind": "full_preview"})
        latest["automation"]["preview_render"].update(
            {
                "status": "completed",
                "output_path": preview.relative_to(project_dir).as_posix(),
                "report_path": wb.AUTOMATION_PREVIEW_RENDER_REPORT,
            }
        )
        return wb._save(project_dir, latest)

    completed = pipeline.run_review_preview_job(
        project,
        started["job_id"],
        dependencies={
            "synthesize_line": synth,
            "concat_audio": concat_pcm_wav,
            "preview_visual_plan": forbidden_visual("visual_plan"),
            "start_visual_generation": forbidden_visual("visual_start"),
            "generate_visuals": forbidden_visual("visual_generate"),
            "start_full_preview": start_preview,
            "generate_full_preview": generate_preview,
            "probe_preview": probe_mock_preview,
        },
    )
    assert completed["status"] == "completed"
    assert completed["phases"]["visual_plan"]["output"]["reused_existing_visuals"] is True
    assert completed["phases"]["visual_plan"]["output"]["visual_generation_required"] is False
    assert calls == {"visual_plan": 0, "visual_start": 0, "visual_generate": 0}


def test_missing_visuals_still_require_pexels_and_network_confirmation(tmp_path: Path) -> None:
    missing = make_project(tmp_path / "missing", with_visuals=False)
    unavailable = capabilities()
    unavailable["pexels"] = {"available": False, "network": True, "paid": False}
    report = pipeline.review_preview_preflight(missing, capabilities=unavailable)
    assert report["visual_generation_required"] is True
    assert report["ready"] is False
    assert any("Pexels 尚未配置" in item for item in report["blockers"])
    with pytest.raises(pipeline.ReviewPreviewError, match="Pexels 尚未配置"):
        pipeline.start_review_preview_job(
            missing,
            {"confirmed": True, "text_ai_confirmed": True},
            capabilities=unavailable,
        )

    available = make_project(tmp_path / "available", with_visuals=False)
    with pytest.raises(pipeline.ReviewPreviewError, match="Pexels 网络检索"):
        pipeline.start_review_preview_job(
            available,
            {"confirmed": True, "network_confirmed": False, "text_ai_confirmed": True},
            capabilities=capabilities(),
        )
    assert pipeline.read_review_preview_job(available)["status"] == "idle"


def test_missing_visuals_block_before_start_when_hyperframes_fallback_is_unavailable(
    tmp_path: Path,
) -> None:
    project = make_project(tmp_path, with_visuals=False)
    unavailable = capabilities()
    unavailable["hyperframes"] = {
        "available": False,
        "status": "unavailable",
        "reason_code": "hyperframes_cli_missing",
        "user_message": "本机尚未完成 HyperFrames 本地初始化，请先初始化后重新预检。",
    }

    report = pipeline.review_preview_preflight(
        project,
        start_payload(),
        capabilities=unavailable,
    )

    assert report["visual_generation_required"] is True
    assert report["ready"] is False
    assert any("HyperFrames 本地初始化" in blocker for blocker in report["blockers"])
    assert not any("HyperFrames" in warning for warning in report["warnings"])
    with pytest.raises(pipeline.ReviewPreviewError, match="HyperFrames 本地初始化"):
        pipeline.start_review_preview_job(
            project,
            start_payload(),
            capabilities=unavailable,
        )
    assert pipeline.read_review_preview_job(project)["status"] == "idle"


def test_visual_resume_requeues_only_failed_slots_and_preserves_completed_media(
    tmp_path: Path,
) -> None:
    project = make_project(tmp_path)
    state = wb._load_for_write(project)
    parent = state["automation"]["review_preview_pipeline"]
    parent.update(
        {
            "job_id": "RPP-retry",
            "status": "failed",
            "stage": "visual_generation",
            "safe_resume_point": "visual_generation",
            "request_fingerprint": "request-frozen",
            "updated_at": "2026-08-31T00:00:00Z",
            "error": {"retryable": True, "message": "8 个画面失败"},
        }
    )
    scene = state["scenes"][0]
    blocks: list[dict] = []
    items: list[dict] = []
    for index in range(19):
        block_id = f"block-{index + 1:02d}"
        completed = index < 11
        asset_id = f"asset-{index + 1:02d}" if completed else None
        usage_id = f"usage-{index + 1:02d}" if completed else None
        blocks.append(
            {
                "id": block_id,
                "status": "ready" if completed else "failed",
                "asset_id": asset_id,
                "usage_id": usage_id,
                "attempt": 1,
                "error": "" if completed else "HyperFrames runtime unavailable",
            }
        )
        items.append(
            {
                "scene_id": scene["id"],
                "block_id": block_id,
                "status": "completed" if completed else "failed",
                "route": "stock_video" if index % 2 else "hyperframes",
                "fallback_route": "hyperframes",
                "asset_id": asset_id,
                "usage_id": usage_id,
                "attempt": 1,
                "stage": "已完成" if completed else "筛选失败",
                "finished_at": "2026-08-31T00:00:00Z",
                "error": "" if completed else "HyperFrames runtime unavailable",
            }
        )
        if completed:
            state.setdefault("assets", []).append(
                {"id": asset_id, "path": f"assets/visual/{asset_id}.mp4", "type": "video"}
            )
            state.setdefault("usages", []).append(
                {
                    "id": usage_id,
                    "asset_id": asset_id,
                    "scene_id": scene["id"],
                    "role": "visual_block",
                    "selected": True,
                    "transform": {"block_id": block_id},
                }
            )
    scene["visual_timeline"] = {"version": 2, "blocks": blocks}
    state["automation"]["visual_batch"] = {
        "status": "completed_with_failures",
        "job_id": "VBJ-retry",
        "parent_job_id": "RPP-retry",
        "request_fingerprint": "request-frozen",
        "items": items,
        "total_slots": 19,
        "completed_slots": 11,
        "failed_slots": 8,
        "finished_at": "2026-08-31T00:00:00Z",
        "error": "仍有失败画面",
    }
    wb._save(project, state)
    before = wb.read_workbench(project)
    completed_evidence = {
        "assets": [item for item in before["assets"] if str(item.get("id") or "").startswith("asset-")],
        "usages": [item for item in before["usages"] if str(item.get("id") or "").startswith("usage-")],
    }

    resumed = pipeline.resume_review_preview_job(
        project,
        "RPP-retry",
        dependencies={
            "collect_capabilities": lambda **_kwargs: capabilities(),
        },
    )

    assert resumed["status"] == "queued"
    assert resumed["stage"] == "visual_generation"
    assert resumed["resume_scope"] == {
        "preserved_completed_slots": 11,
        "retry_failed_slots": 8,
    }
    latest = wb.read_workbench(project)
    batch = latest["automation"]["visual_batch"]
    assert batch["status"] == "queued"
    assert batch["total_slots"] == 19
    assert batch["completed_slots"] == 11
    assert batch["failed_slots"] == 0
    assert batch["retry_slot_count"] == 8
    assert sum(item["status"] == "completed" for item in batch["items"]) == 11
    assert sum(item["status"] == "queued" for item in batch["items"]) == 8
    assert all(item.get("failure_history") for item in batch["items"] if item["status"] == "queued")
    assert {
        "assets": [item for item in latest["assets"] if str(item.get("id") or "").startswith("asset-")],
        "usages": [item for item in latest["usages"] if str(item.get("id") or "").startswith("usage-")],
    } == completed_evidence
    with pytest.raises(pipeline.ReviewPreviewConflict, match="可恢复状态"):
        pipeline.resume_review_preview_job(project, "RPP-retry")


def test_visual_resume_keeps_parent_failed_when_runtime_is_still_unavailable(
    tmp_path: Path,
) -> None:
    project = make_project(tmp_path)
    state = wb._load_for_write(project)
    state["automation"]["review_preview_pipeline"].update(
        {
            "job_id": "RPP-blocked",
            "status": "failed",
            "stage": "visual_generation",
            "safe_resume_point": "visual_generation",
            "request_fingerprint": "request-blocked",
            "updated_at": "2026-08-31T00:00:00Z",
            "error": {"retryable": True},
        }
    )
    scene = state["scenes"][0]
    scene["visual_timeline"] = {
        "version": 2,
        "blocks": [{"id": "block-failed", "status": "failed", "error": "runtime unavailable"}],
    }
    state["automation"]["visual_batch"] = {
        "status": "completed_with_failures",
        "job_id": "VBJ-blocked",
        "parent_job_id": "RPP-blocked",
        "request_fingerprint": "request-blocked",
        "items": [
            {
                "scene_id": scene["id"],
                "block_id": "block-failed",
                "status": "failed",
                "route": "hyperframes",
                "fallback_route": "",
                "error": "runtime unavailable",
            }
        ],
        "total_slots": 1,
        "completed_slots": 0,
        "failed_slots": 1,
    }
    wb._save(project, state)
    unavailable = capabilities()
    unavailable["hyperframes"] = {
        "available": False,
        "status": "unavailable",
        "user_message": "HyperFrames 本地程序无法启动，请修复后重新预检。",
    }

    with pytest.raises(pipeline.ReviewPreviewError, match="未启动新 worker"):
        pipeline.resume_review_preview_job(
            project,
            "RPP-blocked",
            dependencies={"collect_capabilities": lambda **_kwargs: unavailable},
        )

    latest = wb.read_workbench(project)
    assert latest["automation"]["review_preview_pipeline"]["status"] == "failed"
    assert latest["automation"]["visual_batch"]["status"] == "completed_with_failures"


def test_missing_visual_scope_is_frozen_and_reaches_real_batch_planner(tmp_path: Path) -> None:
    project = make_project(tmp_path, with_visuals=False)
    report = pipeline.review_preview_preflight(project, start_payload(), capabilities=capabilities())
    assert report["visual_generation_required"] is True
    assert report["visual_target_scene_ids"] == ["scene-01", "scene-02"]
    assert report["visual_target_scene_count"] == 2

    started = pipeline.start_review_preview_job(project, start_payload(), capabilities=capabilities())
    frozen_visual = started["frozen_input"]["visual"]
    assert frozen_visual["operation_mode"] == "fill_missing"
    assert frozen_visual["selection_mode"] == "custom"
    assert frozen_visual["scene_ids"] == ["scene-01", "scene-02"]

    state = wb._load_for_write(project)
    state["automation"]["review_preview_pipeline"].update(
        {"status": "queued", "stage": "visual_plan", "safe_resume_point": "visual_plan", "worker_token": None}
    )
    wb._save(project, state)
    captured: list[dict] = []

    def stop_after_real_plan(_project_dir: Path, payload: dict) -> dict:
        captured.append(
            {
                "selection_mode": payload.get("selection_mode"),
                "scene_ids": list(payload.get("scene_ids") or []),
                "reviewed_plan": payload.get("reviewed_plan"),
            }
        )
        raise pipeline.StaleReviewPreviewWorker("stop after verified visual selection")

    with pytest.raises(pipeline.StaleReviewPreviewWorker, match="verified visual selection"):
        pipeline.run_review_preview_job(
            project,
            started["job_id"],
            dependencies={
                "preview_visual_plan": wb.preview_visual_batch_plan,
                "start_visual_generation": stop_after_real_plan,
            },
        )
    assert captured
    assert captured[0]["selection_mode"] == "custom"
    assert captured[0]["scene_ids"] == ["scene-01", "scene-02"]
    assert captured[0]["reviewed_plan"]["scene_count"] == 2
    assert captured[0]["reviewed_plan"]["total_slots"] > 0


def test_partial_visual_scope_targets_only_missing_scene_and_preserves_ready_scene(
    tmp_path: Path,
) -> None:
    project = make_project(tmp_path)
    state = wb._load_for_write(project)
    missing_scene = next(scene for scene in state["scenes"] if scene["id"] == "scene-02")
    missing_scene["visual_timeline"] = {"version": 2, "blocks": []}
    for usage in state.get("usages") or []:
        if usage.get("scene_id") == "scene-02" and usage.get("role") in {"visual", "visual_block"}:
            usage["selected"] = False
    wb._save(project, state)

    report = pipeline.review_preview_preflight(project, start_payload(), capabilities=capabilities())
    assert report["visual_generation_required"] is True
    assert report["visual_target_scene_ids"] == ["scene-02"]
    assert report["visual_target_scene_count"] == 1

    started = pipeline.start_review_preview_job(project, start_payload(), capabilities=capabilities())
    assert started["frozen_input"]["visual"]["scene_ids"] == ["scene-02"]
    assert started["frozen_input"]["visual"]["operation_mode"] == "fill_missing"
    latest = wb.read_workbench(project)
    ready_scene = next(scene for scene in latest["scenes"] if scene["id"] == "scene-01")
    assert wb._scene_has_renderable_visual(latest, ready_scene) is True


def test_empty_scene_preflight_freezes_anticipated_visual_scope_and_authorization(tmp_path: Path) -> None:
    project = make_project(tmp_path, with_visuals=False)
    state = wb._load_for_write(project)
    state["scenes"] = []
    state["segments"] = []
    wb._save(project, state)
    (project / "artifacts" / "scene_plan.json").unlink()

    report = pipeline.review_preview_preflight(project, start_payload(), capabilities=capabilities())
    assert report["visual_generation_required"] is True
    assert report["visual_scope_pending_scene_plan"] is True
    assert report["visual_target_scene_ids"] == ["sec-01", "sec-02"]
    assert report["visual_target_scene_count"] == 2

    with pytest.raises(pipeline.ReviewPreviewError, match="Pexels 网络检索"):
        pipeline.start_review_preview_job(
            project,
            {
                "confirmed": True,
                "network_confirmed": False,
                "visual": {"planning_mode": "rule_mix", "image_source": "web_download"},
            },
            capabilities=capabilities(),
        )

    started = pipeline.start_review_preview_job(project, start_payload(), capabilities=capabilities())
    frozen = started["frozen_input"]
    assert frozen["visual_generation_required"] is True
    assert frozen["authorizations"]["pexels_network"] is True
    assert frozen["visual"]["visual_generation_required"] is True
    assert frozen["visual"]["pexels_network"] is True
    assert frozen["visual"]["selection_mode"] == "custom"
    assert frozen["visual"]["scene_ids"] == ["sec-01", "sec-02"]


def test_empty_project_runs_real_visual_batch_contract_through_pexels_and_hyperframes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project = make_project(tmp_path, with_visuals=False)
    state = wb._load_for_write(project)
    state["scenes"] = []
    state["segments"] = []
    wb._save(project, state)
    (project / "artifacts" / "scene_plan.json").unlink()
    monkeypatch.setenv("PEXELS_API_KEY", "test-only-key")

    planner_payloads: list[dict] = []
    provider_calls: list[str] = []

    def reviewed_mixed_plan(project_dir: Path, payload: dict) -> dict:
        planner_payloads.append(
            {
                "selection_mode": payload.get("selection_mode"),
                "scene_ids": list(payload.get("scene_ids") or []),
                "operation_mode": payload.get("operation_mode"),
            }
        )
        reviewed = wb.preview_visual_batch_plan(project_dir, payload)
        planned = [
            block
            for item in reviewed["items"]
            for block in item["blocks"]
            if block.get("status") == "planned"
        ]
        assert len(planned) >= 2
        for index, block in enumerate(planned):
            route = "stock_video" if index == 0 else "hyperframes"
            wb._apply_visual_route(block, route)
            block["fallback_route"] = "hyperframes" if route == "stock_video" else "stock_video"
        reviewed.update(wb._visual_batch_counts(reviewed["items"]))
        reviewed["plan_id"] = wb._visual_batch_plan_digest(reviewed["items"])
        return reviewed

    def fake_pexels(project_dir: Path, _candidate_state: dict, *_args, **_kwargs):
        provider_calls.append("pexels")
        media = project_dir / "assets" / "video" / "pexels" / "pipeline-e2e.mp4"
        media.parent.mkdir(parents=True, exist_ok=True)
        media.write_bytes(b"pipeline-pexels")
        return (
            SimpleNamespace(
                success=True,
                data={
                    "video_id": "pipeline-e2e",
                    "width": 1080,
                    "height": 1920,
                    "duration_seconds": 2.0,
                    "pexels_url": "https://www.pexels.com/video/pipeline-e2e/",
                    "license": "Pexels License",
                },
                error=None,
            ),
            media.relative_to(project_dir).as_posix(),
            {"status": "accepted"},
        )

    def fake_hyperframes(
        project_dir: Path,
        candidate_state: dict,
        _scene: dict,
        _block: dict,
        _item: dict,
        duration: float,
    ) -> dict:
        provider_calls.append("hyperframes")
        output = project_dir / "assets" / "video" / "hyperframes" / f"pipeline-{len(provider_calls)}.mp4"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"pipeline-hyperframes")
        return wb._append_asset(
            project_dir,
            candidate_state,
            {
                "name": "Pipeline HyperFrames",
                "type": "video",
                "source_type": "local_generated",
                "path": output.relative_to(project_dir).as_posix(),
                "duration_seconds": duration,
                "resolution": "1080x1920",
                "provider": "HyperFrames",
                "source_tool": "hyperframes_compose",
                "license": "test",
            },
        )

    monkeypatch.setattr(wb, "_find_screened_pexels_candidate", fake_pexels)
    monkeypatch.setattr(wb, "_generate_hyperframes_visual_block", fake_hyperframes)
    monkeypatch.setattr(wb, "_probe_duration_seconds", lambda *_args, **_kwargs: 2.0)

    def synth(line: dict, output: Path, _voice: dict) -> dict:
        write_pcm_wav(output, duration=0.1)
        return {"task_id": f"pipeline-{line['project_ordinal']}"}

    def start_preview(project_dir: Path, child_payload: dict) -> dict:
        latest = wb._load_for_write(project_dir)
        latest["automation"]["preview_render"].update(
            {
                "status": "generating",
                "version": 1,
                "parent_job_id": child_payload.get("_review_preview_job_id"),
                "input_fingerprint": child_payload.get("_review_preview_input_fingerprint"),
            }
        )
        return wb._save(project_dir, latest)

    def generate_preview(project_dir: Path) -> dict:
        latest = wb._load_for_write(project_dir)
        preview = project_dir / "renders" / "previews" / "visual-batch-e2e.mp4"
        report_path = project_dir / wb.AUTOMATION_PREVIEW_RENDER_REPORT
        preview.parent.mkdir(parents=True, exist_ok=True)
        preview.write_bytes(b"visual-batch-e2e-preview")
        write_json(report_path, {"status": "completed", "kind": "full_preview"})
        latest["automation"]["preview_render"].update(
            {
                "status": "completed",
                "output_path": preview.relative_to(project_dir).as_posix(),
                "report_path": wb.AUTOMATION_PREVIEW_RENDER_REPORT,
            }
        )
        return wb._save(project_dir, latest)

    started = pipeline.start_review_preview_job(project, start_payload(), capabilities=capabilities())
    completed = pipeline.run_review_preview_job(
        project,
        started["job_id"],
        dependencies={
            "synthesize_line": synth,
            "concat_audio": concat_pcm_wav,
            "preview_visual_plan": reviewed_mixed_plan,
            "start_full_preview": start_preview,
            "generate_full_preview": generate_preview,
            "probe_preview": probe_mock_preview,
        },
    )

    assert completed["status"] == "completed"
    assert completed["result"]["readiness"] == "preview_ready"
    assert planner_payloads == [
        {
            "selection_mode": "custom",
            "scene_ids": ["sec-01", "sec-02"],
            "operation_mode": "fill_missing",
        }
    ]
    assert provider_calls == ["pexels", "hyperframes"]
    latest = wb.read_workbench(project)
    batch = latest["automation"]["visual_batch"]
    assert batch["parent_job_id"] == started["job_id"]
    assert batch["request_fingerprint"] == completed["request_fingerprint"]
    assert batch["status"] == "completed"
    assert batch["completed_slots"] == batch["total_slots"] == 2
    assert pipeline._needs_visual_generation(latest) is False
    assert all(scene.get("review_status") != "approved" for scene in latest["scenes"])
    assert not (project / "renders" / "final.mp4").exists()

    duplicate = pipeline.start_review_preview_job(project, start_payload(), capabilities=capabilities())
    assert duplicate["job_id"] == completed["job_id"]
    assert duplicate["launch_required"] is False


def test_frozen_zero_network_visual_loss_fails_run_and_recovery_without_provider_calls(
    tmp_path: Path,
) -> None:
    local_caps = capabilities()
    local_caps["pexels"] = {"available": False, "network": True, "paid": False}
    calls: list[str] = []

    def forbidden_provider(*_args, **_kwargs):
        calls.append("provider")
        raise AssertionError("frozen zero-network task must not call a visual provider")

    run_project = make_project(tmp_path / "run")
    started = pipeline.start_review_preview_job(run_project, {"confirmed": True}, capabilities=local_caps)
    (run_project / "assets" / "visual.png").unlink()
    failed = pipeline.run_review_preview_job(
        run_project,
        started["job_id"],
        dependencies={
            "preview_visual_plan": forbidden_provider,
            "start_visual_generation": forbidden_provider,
            "generate_visuals": forbidden_provider,
        },
    )
    assert failed["status"] == "failed"
    assert failed["error"]["type"] == "InputDriftError"
    assert failed["error"]["retryable"] is False
    assert "零网络路线" in failed["error"]["message"]
    assert calls == []
    assert not (run_project / narration_lines.LEDGER_PATH).exists()

    recover_project = make_project(tmp_path / "recover")
    queued = pipeline.start_review_preview_job(recover_project, {"confirmed": True}, capabilities=local_caps)
    (recover_project / "assets" / "visual.png").unlink()
    recovered = pipeline.recover_review_preview_job(recover_project)
    assert recovered["job_id"] == queued["job_id"]
    assert recovered["status"] == "failed"
    assert recovered["launch_required"] is False
    assert recovered["error"]["type"] == "InputDriftError"
    assert recovered["error"]["retryable"] is False
    assert calls == []


def test_static_ffmpeg_is_discovered_from_current_python_environment_without_import(tmp_path: Path, monkeypatch) -> None:
    package = tmp_path / "site-packages" / "static_ffmpeg"
    ffmpeg = package / "bin" / "win32" / "ffmpeg.exe"
    ffprobe = package / "bin" / "win32" / "ffprobe.exe"
    ffmpeg.parent.mkdir(parents=True)
    ffmpeg.write_bytes(b"mock")
    ffprobe.write_bytes(b"mock")
    monkeypatch.setattr(
        wb.video_compose_runtime,
        "_discover_ffmpeg_pair",
        lambda: (str(ffmpeg), str(ffprobe)),
        raising=False,
    )

    assert Path(wb._ffmpeg_available()) == ffmpeg
    assert Path(wb._ffprobe_available()) == ffprobe
    assert Path(wb._ffprobe_available(wb._ffmpeg_available())) == ffprobe


def test_ffmpeg_is_unavailable_when_pair_lacks_ffprobe(tmp_path: Path, monkeypatch) -> None:
    ffmpeg = tmp_path / "partial" / "ffmpeg.exe"
    missing_probe = ffmpeg.with_name("ffprobe.exe")
    ffmpeg.parent.mkdir(parents=True)
    ffmpeg.write_bytes(b"mock")
    monkeypatch.setattr(
        wb.video_compose_runtime,
        "_discover_ffmpeg_pair",
        lambda: (str(ffmpeg), str(missing_probe)),
        raising=False,
    )
    assert wb._ffmpeg_available() is None
    assert wb._ffprobe_available() is None


def test_explicit_ffmpeg_uses_only_its_sibling_probe(tmp_path: Path, monkeypatch) -> None:
    explicit_ffmpeg = tmp_path / "explicit" / "ffmpeg.exe"
    explicit_ffmpeg.parent.mkdir(parents=True)
    explicit_ffmpeg.write_bytes(b"mock")
    path_pair = tmp_path / "path-runtime"
    path_ffmpeg = path_pair / "ffmpeg.exe"
    path_ffprobe = path_pair / "ffprobe.exe"
    path_pair.mkdir(parents=True)
    path_ffmpeg.write_bytes(b"mock")
    path_ffprobe.write_bytes(b"mock")
    resolver_calls = {"count": 0}

    def path_resolver() -> tuple[str, str]:
        resolver_calls["count"] += 1
        return str(path_ffmpeg), str(path_ffprobe)

    monkeypatch.setattr(wb.video_compose_runtime, "_discover_ffmpeg_pair", path_resolver, raising=False)
    assert wb._ffprobe_available(str(explicit_ffmpeg)) is None
    assert resolver_calls["count"] == 0
    sibling = explicit_ffmpeg.with_name("ffprobe.exe")
    sibling.write_bytes(b"mock")
    assert Path(wb._ffprobe_available(str(explicit_ffmpeg))) == sibling
    assert resolver_calls["count"] == 0


def test_sentence_split_strict_limit_ascii_boundary_and_duplicate_sections() -> None:
    limit = 24
    assert all(len(item) <= limit for item in narration_lines.split_section_text("A" * (limit + 1), max_chars=limit))
    assert all(len(item) <= limit for item in narration_lines.split_section_text("verylongasciitoken" * 8, max_chars=limit))
    with pytest.raises(narration_lines.NarrationLineError, match="重复"):
        narration_lines.build_line_plan(
            [{"id": "dup", "text": "第一句。"}, {"id": "dup", "text": "第二句。"}],
            {"profile_id": "voice-yaya"},
        )


def test_sentence_split_preserves_normalized_content_at_hard_punctuation_boundary() -> None:
    limit = 24
    text = "甲" * limit + "，" + "乙" * (limit + 3) + "：结尾。"
    lines = narration_lines.split_section_text(text, max_chars=limit)
    assert all(0 < len(item) <= limit for item in lines)
    assert "".join(lines) == narration_lines.normalize_line_text(text)
    assert "，" in "".join(lines) and "：" in "".join(lines)


def test_reusable_lines_survive_crash_after_initial_planned_ledger_commit(tmp_path: Path) -> None:
    project = tmp_path / "planned-crash-window"
    plan = narration_lines.build_line_plan(
        [{"id": "s1", "text": "第一句。第二句。"}],
        {"profile_id": "voice-yaya", "profile_name": "雅雅"},
    )
    calls: list[str] = []

    def synth(line: dict, output: Path, _voice: dict) -> dict:
        calls.append(line["line_id"])
        write_pcm_wav(output)
        return {}

    narration_lines.materialize_line_audio(project, plan, synth)
    assert len(calls) == 2
    calls.clear()

    def crash_after_first_commit(action) -> None:
        action()
        raise RuntimeError("simulated crash after initial ledger replace")

    with pytest.raises(RuntimeError, match="simulated crash"):
        narration_lines.materialize_line_audio(project, plan, synth, commit=crash_after_first_commit)
    crashed = narration_lines.load_ledger(project)
    assert all(item["status"] == "completed" for item in crashed["lines"])
    assert calls == []

    resumed = narration_lines.materialize_line_audio(project, plan, synth)
    assert all(item["reused"] is True for item in resumed["lines"])
    assert calls == []


def test_parent_scoped_materialization_requires_commit_cas_and_rejects_stale_worker(tmp_path: Path) -> None:
    project = tmp_path / "parent-cas-required"
    plan = narration_lines.build_line_plan(
        [{"id": "s1", "text": "唯一一句。"}],
        {"profile_id": "voice-yaya", "profile_name": "雅雅"},
    )
    synth_calls: list[str] = []

    def synth(line: dict, output: Path, _voice: dict) -> dict:
        synth_calls.append(line["line_id"])
        write_pcm_wav(output)
        return {}

    with pytest.raises(narration_lines.NarrationLineError, match="commit/CAS"):
        narration_lines.materialize_line_audio(
            project,
            plan,
            synth,
            parent_job_id="RPP-parent",
            worker_token="worker-a",
        )
    assert not (project / narration_lines.LEDGER_PATH).exists()

    def stale_commit(_action) -> None:
        raise narration_lines.StaleNarrationWorker("旧 worker 租约已失效")

    with pytest.raises(narration_lines.StaleNarrationWorker, match="租约"):
        narration_lines.materialize_line_audio(
            project,
            plan,
            synth,
            parent_job_id="RPP-parent",
            worker_token="worker-a",
            commit=stale_commit,
        )
    assert synth_calls == []
    assert not (project / narration_lines.LEDGER_PATH).exists()


def test_wav_truncation_and_unsafe_ledger_output_are_rejected(tmp_path: Path) -> None:
    wav = tmp_path / "truncated.wav"
    write_pcm_wav(wav, duration=0.2)
    wav.write_bytes(wav.read_bytes()[:-100])
    with pytest.raises(narration_lines.NarrationLineError, match="截断"):
        narration_lines.inspect_pcm_wav(wav)

    project = tmp_path / "unsafe-ledger"
    plan = narration_lines.build_line_plan([{"id": "s1", "text": "一句。"}], {"profile_id": "voice-yaya"})
    record = {**plan["lines"][0], "status": "completed", "output_path": "../escape.wav", "sha256": "bad"}
    write_json(project / narration_lines.LEDGER_PATH, {**plan, "lines": [record], "history": []})
    with pytest.raises(narration_lines.NarrationLineError, match="安全相对路径|逃逸"):
        narration_lines.materialize_line_audio(project, plan, lambda *_args: None)


def test_voice_a_b_a_reuses_historical_valid_line_version(tmp_path: Path) -> None:
    project = tmp_path / "voice-history"
    calls: list[str] = []

    def synth(line: dict, output: Path, voice: dict) -> dict:
        calls.append(str(voice["profile_id"]))
        write_pcm_wav(output)
        return {}

    sections = [{"id": "s1", "text": "一句。"}]
    voice_a = {"profile_id": "A", "profile_name": "A"}
    voice_b = {"profile_id": "B", "profile_name": "B"}
    narration_lines.materialize_line_audio(project, narration_lines.build_line_plan(sections, voice_a), synth)
    narration_lines.materialize_line_audio(project, narration_lines.build_line_plan(sections, voice_b), synth)
    third = narration_lines.materialize_line_audio(project, narration_lines.build_line_plan(sections, voice_a), synth)
    assert calls == ["A", "B"]
    assert third["lines"][0]["reused"] is True
    assert len(third["history"]) == 1


def test_cinematic_unknown_and_stale_scene_contract_are_rejected(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    raw = json.loads((project / "project.json").read_text(encoding="utf-8"))
    raw["pipeline_type"] = "legacy-narration"
    write_json(project / "project.json", raw)
    report = pipeline.review_preview_preflight(project, capabilities=capabilities())
    assert report["ready"] is False
    assert report["script_review_status"] == "unknown"
    assert "旧版或未知" in report["blockers"][0]

    raw["pipeline_type"] = "cinematic"
    write_json(project / "project.json", raw)
    cinematic = pipeline.review_preview_preflight(project, capabilities=capabilities())
    assert cinematic["ready"] is False
    assert cinematic["project_type"] == "cinematic"
    assert "不是明确支持" in cinematic["blockers"][0]
    with pytest.raises(pipeline.ReviewPreviewError, match="不是明确支持"):
        pipeline.start_review_preview_job(project, start_payload(), capabilities=capabilities())

    raw["pipeline_type"] = "animated-explainer"
    write_json(project / "project.json", raw)
    state = wb._load_for_write(project)
    state["scenes"][1]["script_section_id"] = "sec-01"
    wb._save(project, state)
    job = pipeline.start_review_preview_job(project, start_payload(), capabilities=capabilities())
    failed = pipeline.run_review_preview_job(project, job["job_id"])
    assert failed["status"] == "failed"
    assert failed["safe_resume_point"] == "scene_plan"
    assert failed["error"]["retryable"] is False
    with pytest.raises(pipeline.ReviewPreviewError, match="禁止续跑"):
        pipeline.resume_review_preview_job(project, job["job_id"])


def test_scene_contract_requires_script_order_and_rejects_reordered_media_clock(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    state = wb.read_workbench(project)
    script = wb._read_json(project / "artifacts" / "script.json")
    script_hash = pipeline._json_hash(script)
    normal = pipeline._scene_contract(project, state, script, script_hash)
    assert [item["section_id"] for item in normal["mapping"]] == ["sec-01", "sec-02"]

    reordered = wb._load_for_write(project)
    reordered["scenes"] = list(reversed(reordered["scenes"]))
    scenes_before = [item["id"] for item in reordered["scenes"]]
    assets_before = pipeline._json_hash(reordered.get("assets") or [])
    narration_before = pipeline._json_hash(reordered["automation"]["narration_generation"])
    wb._save(project, reordered)
    synth_calls: list[str] = []
    started = pipeline.start_review_preview_job(project, start_payload(), capabilities=capabilities())
    failed = pipeline.run_review_preview_job(
        project,
        started["job_id"],
        dependencies={"synthesize_line": lambda *_args: synth_calls.append("unexpected")},
    )
    assert failed["status"] == "failed"
    assert failed["safe_resume_point"] == "scene_plan"
    assert failed["error"]["retryable"] is False
    assert "顺序不一致" in failed["error"]["message"]
    latest = wb.read_workbench(project)
    assert [item["id"] for item in latest["scenes"]] == scenes_before
    assert pipeline._json_hash(latest.get("assets") or []) == assets_before
    assert pipeline._json_hash(latest["automation"]["narration_generation"]) == narration_before
    assert synth_calls == []
    assert not (project / narration_lines.LEDGER_PATH).exists()


def test_internal_child_bypass_requires_running_parent_and_worker_token(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    started = pipeline.start_review_preview_job(project, start_payload(), capabilities=capabilities())
    state = wb._load_for_write(project)
    parent = state["automation"]["review_preview_pipeline"]
    parent.update({"status": "running", "worker_token": "secret-worker"})
    wb._save(project, state)
    automation = wb._automation(wb._load_for_write(project))
    with pytest.raises(wb.WorkbenchError, match="一键审核预览"):
        wb._require_no_review_preview_conflict(automation, started["job_id"])
    with pytest.raises(wb.WorkbenchError, match="一键审核预览"):
        wb._require_no_review_preview_conflict(automation, started["job_id"], "secret-worker")
    wb._require_no_review_preview_conflict(
        automation,
        started["job_id"],
        "secret-worker",
        wb._REVIEW_PREVIEW_INTERNAL_CAPABILITY,
    )
    with pytest.raises(wb.WorkbenchError, match="一键审核预览"):
        wb.start_review_preview_sync(project, {"confirmed": True, "_review_preview_job_id": started["job_id"]})
    with pytest.raises(wb.WorkbenchError, match="一键审核预览"):
        wb.start_scene_narration_candidate(project, "scene-01", {"_review_preview_job_id": started["job_id"]})


def test_script_hash_is_stable_across_parent_status_sequence(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    queued = pipeline.start_review_preview_job(project, start_payload(), capabilities=capabilities())
    expected = queued["script_hash"]
    assert expected == queued["frozen_input"]["script_hash"]
    for status in ("running", "awaiting_human", "failed"):
        state = wb._load_for_write(project)
        state["automation"]["review_preview_pipeline"]["status"] = status
        wb._save(project, state)
        assert pipeline.read_review_preview_job(project)["script_hash"] == expected


def test_script_drift_is_nonretryable_and_preserves_frozen_script_hash(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    job = pipeline.start_review_preview_job(project, start_payload(), capabilities=capabilities())
    script_path = project / "artifacts" / "script.json"
    changed = json.loads(script_path.read_text(encoding="utf-8"))
    changed["sections"][0]["text"] = "脚本已经变化。"
    write_json(script_path, changed)
    failed = pipeline.run_review_preview_job(project, job["job_id"])
    assert failed["status"] == "failed"
    assert failed["error"]["retryable"] is False
    assert failed["script_hash"] == job["script_hash"]
    with pytest.raises(pipeline.ReviewPreviewError, match="禁止续跑"):
        pipeline.resume_review_preview_job(project, job["job_id"])


def test_concat_audio_uses_atomic_temp_and_validates_total_duration(tmp_path: Path, monkeypatch) -> None:
    project = tmp_path / "concat"
    parts = [project / "a.wav", project / "b.wav"]
    for part in parts:
        write_pcm_wav(part, duration=0.1)
    output = project / "joined.wav"
    output.write_bytes(b"old-output")
    monkeypatch.setattr(wb, "_ffmpeg_available", lambda: "mock-ffmpeg")

    def run_ok(command: list[str]) -> tuple[bool, str]:
        write_pcm_wav(Path(command[-1]), duration=0.2)
        return True, "ok"

    monkeypatch.setattr(wb, "_run_media", run_ok)
    wb._concat_audio(project, parts, output_path=output)
    assert narration_lines.inspect_pcm_wav(output)["duration_seconds"] == pytest.approx(0.2, abs=0.001)
    assert not list(project.glob(".joined-*.wav"))

    old_hash = hashlib.sha256(output.read_bytes()).hexdigest()

    def run_bad(command: list[str]) -> tuple[bool, str]:
        write_pcm_wav(Path(command[-1]), duration=0.05)
        return True, "bad duration"

    monkeypatch.setattr(wb, "_run_media", run_bad)
    with pytest.raises(wb.WorkbenchError, match="时长校验失败"):
        wb._concat_audio(project, parts, output_path=output)
    assert hashlib.sha256(output.read_bytes()).hexdigest() == old_hash


def test_subtitle_style_drift_and_stale_ledger_worker_are_rejected(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    job = pipeline.start_review_preview_job(project, start_payload(), capabilities=capabilities())
    state = wb._load_for_write(project)
    style = state["subtitle_styles"]["templates"][0]["style"]
    style["font_size"] = int(style.get("font_size") or 42) + 1
    wb._save(project, state)
    failed = pipeline.run_review_preview_job(project, job["job_id"])
    assert failed["status"] == "failed"
    assert failed["error"]["retryable"] is False
    assert "字幕样式" in failed["error"]["message"]

    project2 = make_project(tmp_path / "lease")
    queued = pipeline.start_review_preview_job(project2, start_payload(), capabilities=capabilities())
    state2 = wb._load_for_write(project2)
    parent = state2["automation"]["review_preview_pipeline"]
    parent.update({"status": "running", "worker_token": "new-worker"})
    wb._save(project2, state2)
    called = {"value": False}
    with pytest.raises(narration_lines.StaleNarrationWorker, match="租约"):
        pipeline._commit_line_ledger(
            project2,
            queued["job_id"],
            "old-worker",
            lambda: called.update(value=True),
        )
    assert called["value"] is False


def test_invalid_preview_evidence_fails_at_full_preview_safe_point(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    job = pipeline.start_review_preview_job(project, start_payload(), capabilities=capabilities())
    state = wb._load_for_write(project)
    parent = state["automation"]["review_preview_pipeline"]
    visual_signature = pipeline._current_input_contract(project, state, parent["frozen_input"]["script"])["scene_visual_signature"]
    parent.update({"status": "queued", "stage": "full_preview", "safe_resume_point": "full_preview", "worker_token": None})
    parent.setdefault("phases", {})["visual_plan"] = {"status": "completed", "output": {"visual_signature": visual_signature}}
    wb._save(project, state)

    def start_preview(project_dir: Path, _payload: dict) -> dict:
        latest = wb._load_for_write(project_dir)
        latest["automation"]["preview_render"].update({"status": "generating", "version": 1})
        return wb._save(project_dir, latest)

    def generate_preview(project_dir: Path) -> dict:
        latest = wb._load_for_write(project_dir)
        preview = project_dir / "renders" / "previews" / "invalid.mp4"
        report = project_dir / wb.AUTOMATION_PREVIEW_RENDER_REPORT
        preview.parent.mkdir(parents=True, exist_ok=True)
        preview.write_bytes(b"not-a-valid-preview")
        write_json(report, {"status": "completed"})
        latest["automation"]["preview_render"].update(
            {
                "status": "completed",
                "output_path": preview.relative_to(project_dir).as_posix(),
                "report_path": wb.AUTOMATION_PREVIEW_RENDER_REPORT,
            }
        )
        return wb._save(project_dir, latest)

    def invalid_probe(_project_dir: Path, _preview: str, _report: str) -> dict:
        raise pipeline.ReviewPreviewError("全片审核预览媒体探测未发现完整音视频流")

    failed = pipeline.run_review_preview_job(
        project,
        job["job_id"],
        dependencies={
            "start_full_preview": start_preview,
            "generate_full_preview": generate_preview,
            "probe_preview": invalid_probe,
        },
    )
    assert failed["status"] == "failed"
    assert failed["stage"] == "full_preview"
    assert failed["safe_resume_point"] == "full_preview"
    assert failed["error"]["retryable"] is True


def test_ambiguous_external_submission_stays_at_human_gate(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    started = pipeline.start_review_preview_job(project, start_payload(), capabilities=capabilities())
    state = wb._load_for_write(project)
    parent = state["automation"]["review_preview_pipeline"]
    parent.update({"status": "running", "stage": "visual_generation", "worker_token": "worker-a"})
    wb._save(project, state)
    gated = pipeline._fail_job(
        project,
        started["job_id"],
        "worker-a",
        pipeline.AmbiguousExternalOperation("提交后连接中断，结果不明"),
        ambiguous=True,
    )
    assert gated["status"] == "awaiting_human"
    assert gated["safe_resume_point"] == "visual_generation"
    assert gated["error"]["ambiguous_external_operation"] is True
    with pytest.raises(pipeline.ReviewPreviewError, match="必须先确认"):
        pipeline.resume_review_preview_job(project, started["job_id"])


def test_empty_scene_plan_is_deterministically_refrozen_and_completes(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    state = wb._load_for_write(project)
    saved_scenes = json.loads(json.dumps(state["scenes"], ensure_ascii=False))
    saved_segments = json.loads(json.dumps(state.get("segments") or [], ensure_ascii=False))
    state["scenes"] = []
    state["segments"] = []
    wb._save(project, state)
    (project / "artifacts" / "scene_plan.json").unlink()
    started = pipeline.start_review_preview_job(project, start_payload(), capabilities=capabilities())
    assert started["frozen_input"]["scene_plan_was_missing"] is True
    duplicate_starts: list[dict] = []

    def generate_scene_plan(project_dir: Path) -> dict:
        latest = wb._load_for_write(project_dir)
        latest["scenes"] = saved_scenes
        latest["segments"] = saved_segments
        script = wb._read_json(project_dir / "artifacts" / "script.json")
        latest["project"]["scene_plan_script_hash"] = pipeline._json_hash(script)
        wb._save(project_dir, latest)
        write_json(
            project_dir / "artifacts" / "scene_plan.json",
            {"script_sha256": pipeline._json_hash(script), "scenes": saved_scenes},
        )
        # The deterministic scene plan is already visible, but the parent has
        # not yet executed its refreeze CAS.  This is the narrow historical
        # race that must still reuse the active parent.
        duplicate_starts.append(
            pipeline.start_review_preview_job(project_dir, start_payload(), capabilities=capabilities())
        )
        return latest

    def synth(_line: dict, output: Path, _voice: dict) -> dict:
        if len(duplicate_starts) == 1:
            duplicate_starts.append(
                pipeline.start_review_preview_job(project, start_payload(), capabilities=capabilities())
            )
        write_pcm_wav(output, duration=0.1)
        return {}

    def start_preview(project_dir: Path, _payload: dict) -> dict:
        latest = wb._load_for_write(project_dir)
        latest["automation"]["preview_render"].update({"status": "generating", "version": 1})
        return wb._save(project_dir, latest)

    def generate_preview(project_dir: Path) -> dict:
        latest = wb._load_for_write(project_dir)
        preview = project_dir / "renders" / "previews" / "empty-scenes.mp4"
        report = project_dir / wb.AUTOMATION_PREVIEW_RENDER_REPORT
        preview.parent.mkdir(parents=True, exist_ok=True)
        preview.write_bytes(b"empty-scenes-preview")
        write_json(report, {"status": "completed"})
        latest["automation"]["preview_render"].update(
            {
                "status": "completed",
                "output_path": preview.relative_to(project_dir).as_posix(),
                "report_path": wb.AUTOMATION_PREVIEW_RENDER_REPORT,
            }
        )
        return wb._save(project_dir, latest)

    completed = pipeline.run_review_preview_job(
        project,
        started["job_id"],
        dependencies={
            "generate_scene_plan": generate_scene_plan,
            "synthesize_line": synth,
            "concat_audio": concat_pcm_wav,
            "start_full_preview": start_preview,
            "generate_full_preview": generate_preview,
            "probe_preview": probe_mock_preview,
        },
    )
    assert completed["status"] == "completed"
    assert completed["frozen_input"]["scene_plan_was_missing"] is False
    assert completed["input_fingerprint"] == pipeline._json_hash(completed["frozen_input"])
    assert [item["job_id"] for item in duplicate_starts] == [started["job_id"], started["job_id"]]
    assert all(item["launch_required"] is False for item in duplicate_starts)


def test_slow_preflight_does_not_hold_project_transaction_lock(tmp_path: Path, monkeypatch) -> None:
    project = make_project(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    acquired = threading.Event()
    result: list[dict] = []
    original = pipeline.review_preview_preflight

    def slow_preflight(*args, **kwargs):
        entered.set()
        assert release.wait(2)
        return original(*args, **kwargs)

    def start_parent() -> None:
        result.append(
            pipeline.start_review_preview_job(project, start_payload(), capabilities=capabilities())
        )

    def probe_lock() -> None:
        with wb._project_transaction_lock(project):
            acquired.set()

    monkeypatch.setattr(pipeline, "review_preview_preflight", slow_preflight)
    starter = threading.Thread(target=start_parent)
    starter.start()
    assert entered.wait(1)
    lock_probe = threading.Thread(target=probe_lock)
    lock_probe.start()
    lock_probe.join(0.5)
    assert acquired.is_set(), "capability/preflight unexpectedly held the project transaction lock"
    release.set()
    starter.join(3)
    lock_probe.join(1)
    assert result and result[0]["status"] == "queued"


def test_queued_job_recovery_requires_launch_after_save_before_dispatch(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    queued = pipeline.start_review_preview_job(project, start_payload(), capabilities=capabilities())
    first_recovery = pipeline.recover_review_preview_job(project)
    second_recovery = pipeline.recover_review_preview_job(project)
    assert first_recovery["job_id"] == queued["job_id"]
    assert first_recovery["launch_required"] is True
    assert second_recovery["launch_required"] is True

    state = wb._load_for_write(project)
    state["automation"]["review_preview_pipeline"].update(
        {"status": "running", "worker_token": "leased", "stage": "preflight"}
    )
    wb._save(project, state)
    synth_calls: list[str] = []
    duplicate_worker = pipeline.run_review_preview_job(
        project,
        queued["job_id"],
        dependencies={"synthesize_line": lambda *_args: synth_calls.append("unexpected")},
    )
    assert duplicate_worker["status"] == "running"
    assert duplicate_worker["launch_required"] is False
    assert synth_calls == []
    assert pipeline.recover_review_preview_job(project)["launch_required"] is True
    assert pipeline.recover_review_preview_job(project)["launch_required"] is True


def test_new_invalid_wav_is_retryable_but_completed_evidence_drift_is_not(tmp_path: Path) -> None:
    project = tmp_path / "typed-wav-errors"
    plan = narration_lines.build_line_plan([{"id": "s1", "text": "一句。"}], {"profile_id": "voice-yaya"})

    def invalid_synth(_line: dict, output: Path, _voice: dict) -> dict:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"invalid-new-wav")
        return {}

    with pytest.raises(narration_lines.NarrationOutputValidationError):
        narration_lines.materialize_line_audio(project, plan, invalid_synth)

    parent_project = make_project(tmp_path / "parent")
    started = pipeline.start_review_preview_job(parent_project, start_payload(), capabilities=capabilities())
    state = wb._load_for_write(parent_project)
    state["automation"]["review_preview_pipeline"].update(
        {"status": "running", "stage": "narration", "worker_token": "typed-worker"}
    )
    wb._save(parent_project, state)
    retryable = pipeline._fail_job(
        parent_project,
        started["job_id"],
        "typed-worker",
        narration_lines.NarrationOutputValidationError("new output invalid"),
    )
    assert retryable["error"]["retryable"] is True

    state = wb._load_for_write(parent_project)
    state["automation"]["review_preview_pipeline"].update(
        {"status": "running", "stage": "narration", "worker_token": "drift-worker", "error": None}
    )
    wb._save(parent_project, state)
    drifted = pipeline._fail_job(
        parent_project,
        started["job_id"],
        "drift-worker",
        narration_lines.NarrationEvidenceDriftError("completed evidence drift"),
    )
    assert drifted["error"]["retryable"] is False


def test_stale_subtitle_worker_cannot_replace_formal_subtitle(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    started = pipeline.start_review_preview_job(project, start_payload(), capabilities=capabilities())
    formal = project / "assets" / "subtitles.srt"
    formal.parent.mkdir(parents=True, exist_ok=True)
    formal.write_text("FORMAL-SENTINEL", encoding="utf-8")
    state = wb._load_for_write(project)
    state["automation"]["review_preview_pipeline"].update(
        {"status": "running", "stage": "subtitles", "worker_token": "new-worker"}
    )
    wb._save(project, state)
    with pytest.raises(pipeline.StaleReviewPreviewWorker):
        pipeline._write_sentence_subtitles(project, started["job_id"], "old-worker")
    assert formal.read_text(encoding="utf-8") == "FORMAL-SENTINEL"
    assert not list(formal.parent.glob(".subtitles-*.tmp.srt"))


def test_parent_and_manual_starters_share_one_transaction_lock(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    entered = threading.Event()
    errors: list[Exception] = []

    def start_manual() -> None:
        entered.set()
        try:
            wb.start_review_preview_sync(project, {"confirmed": True})
        except Exception as exc:
            errors.append(exc)

    with wb._project_transaction_lock(project):
        thread = threading.Thread(target=start_manual)
        thread.start()
        assert entered.wait(1)
        time.sleep(0.05)
        assert thread.is_alive()
        parent = pipeline.start_review_preview_job(project, start_payload(), capabilities=capabilities())
    thread.join(2)
    assert parent["status"] == "queued"
    assert len(errors) == 1 and isinstance(errors[0], wb.WorkbenchError)
    assert wb._automation(wb.read_workbench(project))["preview_sync"]["status"] == "idle"


@pytest.mark.parametrize("tamper", [False, True])
def test_review_ready_missing_or_tampered_evidence_falls_back_to_full_preview(
    tmp_path: Path, tamper: bool
) -> None:
    project = make_project(tmp_path)
    completed, _calls = complete_no_gate_review_job(project)
    preview = project / completed["result"]["preview_path"]
    report = project / completed["result"]["report_path"]
    state = wb._load_for_write(project)
    state["automation"]["review_preview_pipeline"].update(
        {
            "status": "queued",
            "stage": "review_ready",
            "safe_resume_point": "review_ready",
            "worker_token": None,
            "result": None,
        }
    )
    wb._save(project, state)
    if not tamper:
        report.unlink()

    def tampering_probe(project_dir: Path, preview_path: str, report_path: str) -> dict:
        evidence = probe_mock_preview(project_dir, preview_path, report_path)
        (project_dir / preview_path).write_bytes(b"tampered-after-probe")
        return evidence

    failed = pipeline.run_review_preview_job(
        project,
        completed["job_id"],
        dependencies={"probe_preview": tampering_probe if tamper else pipeline._probe_preview_evidence},
    )
    assert failed["status"] == "failed"
    assert failed["stage"] == "full_preview"
    assert failed["safe_resume_point"] == "full_preview"
    assert wb.read_workbench(project)["automation"]["preview_render"]["status"] == "failed"
    resumed = pipeline.resume_review_preview_job(project, completed["job_id"])
    assert resumed["stage"] == "full_preview"


def test_phase_schema_migrates_and_persists_recovery_fields(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    started = pipeline.start_review_preview_job(project, start_payload(), capabilities=capabilities())
    state = wb._load_for_write(project)
    state["automation"]["review_preview_pipeline"]["phases"] = {"legacy": {"status": "failed"}}
    wb._save(project, state)
    pipeline._mutate_job(project, started["job_id"], None, lambda _state, _job: None)
    phase = wb._load_for_write(project)["automation"]["review_preview_pipeline"]["phases"]["legacy"]
    assert phase["status"] == "failed"
    assert phase["attempts"] == 0
    assert phase["started_at"] is None
    assert phase["output"] == {}
    assert phase["error"] is None
    assert phase["finished_at"] is None
    assert phase["input_fingerprint"] == started["input_fingerprint"]
    assert phase["retryable"] is True
    assert phase["safe_resume_point"] == "legacy"


def test_complete_parent_probe_runs_without_holding_project_lock(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    completed, _calls = complete_no_gate_review_job(project)
    state = wb._load_for_write(project)
    state["automation"]["review_preview_pipeline"].update(
        {"status": "queued", "stage": "review_ready", "worker_token": None, "result": None}
    )
    wb._save(project, state)
    acquired = threading.Event()

    def slow_probe(project_dir: Path, preview_path: str, report_path: str) -> dict:
        def lock_probe() -> None:
            with wb._project_transaction_lock(project_dir):
                acquired.set()

        thread = threading.Thread(target=lock_probe)
        thread.start()
        thread.join(1)
        assert acquired.is_set(), "preview probe unexpectedly held the project transaction lock"
        return probe_mock_preview(project_dir, preview_path, report_path)

    completed = pipeline.run_review_preview_job(
        project, completed["job_id"], dependencies={"probe_preview": slow_probe}
    )
    assert completed["status"] == "completed"


def test_aggregate_reuse_rejects_valid_but_shortened_replacement(tmp_path: Path) -> None:
    project = tmp_path / "aggregate-evidence"
    parts = [project / "line-a.wav", project / "line-b.wav"]
    for part in parts:
        write_pcm_wav(part, duration=0.1)
    output = project / "scene.wav"
    concat_pcm_wav(project, parts, output_path=output)
    media = narration_lines.inspect_pcm_wav(output)
    evidence = {
        "aggregate_input_fingerprint": "aggregate-fp",
        "aggregate_sha256": media["sha256"],
        "expected_duration_seconds": 0.2,
    }
    calls: list[Path] = []

    def tracked_concat(project_dir: Path, inputs: list[Path], *, output_path: Path | None = None) -> Path:
        calls.append(output_path)
        return concat_pcm_wav(project_dir, inputs, output_path=output_path)

    reused, temporary = pipeline._prepare_aggregate_audio(
        project, output, parts, "aggregate-fp", 0.2, evidence, "RPP-a", "worker-a", {"concat_audio": tracked_concat}
    )
    assert temporary is None and calls == []
    assert reused["sha256"] == media["sha256"]

    write_pcm_wav(output, duration=0.1)
    regenerated, temporary = pipeline._prepare_aggregate_audio(
        project, output, parts, "aggregate-fp", 0.2, evidence, "RPP-a", "worker-a", {"concat_audio": tracked_concat}
    )
    assert temporary is not None and temporary.is_file()
    assert len(calls) == 1
    assert regenerated["duration_seconds"] == pytest.approx(0.2, abs=0.001)
    temporary.unlink()


def test_review_preview_conflict_is_public_and_separates_409_from_422(tmp_path: Path) -> None:
    assert issubclass(pipeline.ReviewPreviewConflict, pipeline.ReviewPreviewError)
    assert issubclass(pipeline.StaleReviewPreviewWorker, pipeline.ReviewPreviewConflict)
    project = make_project(tmp_path)
    started = pipeline.start_review_preview_job(project, start_payload(), capabilities=capabilities())
    with pytest.raises(pipeline.ReviewPreviewConflict):
        pipeline.start_review_preview_job(
            project,
            start_payload(),
            capabilities=capabilities(explicit=True, selected="voice-mengmeng"),
        )
    with pytest.raises(pipeline.ReviewPreviewConflict):
        pipeline.resume_review_preview_job(project, "RPP-does-not-exist")
    with pytest.raises(pipeline.ReviewPreviewConflict):
        pipeline.resume_review_preview_job(project, started["job_id"])

    invalid = make_project(tmp_path / "invalid-input")
    with pytest.raises(pipeline.ReviewPreviewError) as exc_info:
        pipeline.start_review_preview_job(invalid, {}, capabilities=capabilities())
    assert type(exc_info.value) is pipeline.ReviewPreviewError

    manual = make_project(tmp_path / "manual-conflict")
    state = wb._load_for_write(manual)
    state["automation"]["visual_batch"]["status"] = "queued"
    wb._save(manual, state)
    with pytest.raises(pipeline.ReviewPreviewConflict):
        pipeline.start_review_preview_job(manual, start_payload(), capabilities=capabilities())


def test_tts_request_id_is_durable_before_post_and_reused_after_pre_post_crash(tmp_path: Path) -> None:
    project = tmp_path / "tts-before-post"
    write_json(project / "project.json", {"project_id": "project-stable-id"})
    fake = FakeRecoverableTTS()
    commits = {"count": 0}

    def crash_after_request_checkpoint(action) -> None:
        action()
        commits["count"] += 1
        if commits["count"] == 2:
            raise RuntimeError("crash after request checkpoint before POST")

    with pytest.raises(RuntimeError, match="before POST"):
        narration_lines.materialize_line_audio(
            project,
            recoverable_line_plan(),
            tts_client=fake,
            parent_job_id="RPP-parent",
            worker_token="worker-a",
            commit=crash_after_request_checkpoint,
            poll_interval_seconds=0,
        )
    checkpoint = narration_lines.load_ledger(project)["lines"][0]
    request_id = checkpoint["tts_request_id"]
    assert checkpoint["attempts"] == 1
    assert request_id.startswith("rpp-tts-")
    assert str(project) not in request_id
    assert fake.submit_calls == []

    completed = narration_lines.materialize_line_audio(
        project,
        recoverable_line_plan(),
        tts_client=fake,
        parent_job_id="RPP-parent",
        worker_token="worker-b",
        commit=lambda action: action(),
        poll_interval_seconds=0,
    )
    assert completed["lines"][0]["tts_request_id"] == request_id
    assert fake.submit_calls == [request_id]


def test_tts_submit_response_loss_replays_same_request_without_new_attempt(tmp_path: Path) -> None:
    project = tmp_path / "tts-response-loss"
    write_json(project / "project.json", {"project_id": "response-loss"})
    fake = FakeRecoverableTTS(lose_first_submit_response=True)
    kwargs = {
        "tts_client": fake,
        "parent_job_id": "RPP-parent",
        "worker_token": "worker-a",
        "commit": lambda action: action(),
        "poll_interval_seconds": 0,
    }
    with pytest.raises(narration_lines.NarrationTTSSubmitUncertainError):
        narration_lines.materialize_line_audio(project, recoverable_line_plan(), **kwargs)
    uncertain = narration_lines.load_ledger(project)["lines"][0]
    assert uncertain["status"] == "generating"
    assert uncertain["tts_status"] == "submit_unknown"
    assert uncertain["attempts"] == 1

    completed = narration_lines.materialize_line_audio(project, recoverable_line_plan(), **kwargs)
    record = completed["lines"][0]
    assert fake.submit_calls == [record["tts_request_id"], record["tts_request_id"]]
    assert len(fake.tasks_by_request) == 1
    assert record["attempts"] == 1


def test_tts_existing_task_recovery_queries_without_submit(tmp_path: Path) -> None:
    project = tmp_path / "tts-existing-task"
    write_json(project / "project.json", {"project_id": "existing-task"})
    fake = FakeRecoverableTTS()
    commits = {"count": 0}

    def crash_after_task_checkpoint(action) -> None:
        action()
        commits["count"] += 1
        if commits["count"] == 5:
            raise RuntimeError("crash after task checkpoint")

    with pytest.raises(narration_lines.NarrationLineError, match="task checkpoint"):
        narration_lines.materialize_line_audio(
            project,
            recoverable_line_plan(),
            tts_client=fake,
            parent_job_id="RPP-parent",
            worker_token="worker-a",
            commit=crash_after_task_checkpoint,
            poll_interval_seconds=0,
        )
    checkpoint = narration_lines.load_ledger(project)["lines"][0]
    assert checkpoint["tts_task_id"]
    submit_count = len(fake.submit_calls)

    completed = narration_lines.materialize_line_audio(
        project,
        recoverable_line_plan(),
        tts_client=fake,
        parent_job_id="RPP-parent",
        worker_token="worker-b",
        commit=lambda action: action(),
        poll_interval_seconds=0,
    )
    assert len(fake.submit_calls) == submit_count
    assert fake.query_calls == [checkpoint["tts_task_id"]]
    assert completed["lines"][0]["status"] == "completed"


def test_tts_terminal_failure_requires_human_retry_and_new_request_attempt(tmp_path: Path) -> None:
    project = tmp_path / "tts-terminal-retry"
    write_json(project / "project.json", {"project_id": "terminal-retry"})
    fake = FakeRecoverableTTS(fail_first_task=True)
    kwargs = {
        "tts_client": fake,
        "parent_job_id": "RPP-parent",
        "worker_token": "worker-a",
        "commit": lambda action: action(),
        "poll_interval_seconds": 0,
    }
    with pytest.raises(narration_lines.NarrationTTSTerminalError):
        narration_lines.materialize_line_audio(project, recoverable_line_plan(), **kwargs)
    first = narration_lines.load_ledger(project)["lines"][0]
    assert first["attempts"] == 1 and first["tts_status"] == "failed"
    first_request = first["tts_request_id"]
    submit_count = len(fake.submit_calls)

    with pytest.raises(narration_lines.NarrationTTSTerminalError, match="人工安全恢复"):
        narration_lines.materialize_line_audio(project, recoverable_line_plan(), **kwargs)
    assert len(fake.submit_calls) == submit_count

    completed = narration_lines.materialize_line_audio(
        project,
        recoverable_line_plan(),
        allow_terminal_retry=True,
        **kwargs,
    )
    second = completed["lines"][0]
    assert second["attempts"] == 2
    assert second["tts_request_id"] != first_request
    assert second["status"] == "completed"

    parent_project = make_project(tmp_path / "parent-resume")
    parent = pipeline.start_review_preview_job(parent_project, start_payload(), capabilities=capabilities())
    state = wb._load_for_write(parent_project)
    state["automation"]["review_preview_pipeline"].update(
        {
            "status": "failed",
            "stage": "narration",
            "safe_resume_point": "narration",
            "error": {"retryable": True, "type": "NarrationTTSTerminalError"},
        }
    )
    wb._save(parent_project, state)
    write_json(
        parent_project / narration_lines.LEDGER_PATH,
        {"lines": [{"line_id": "line", "tts_status": "failed"}], "history": []},
    )
    resumed = pipeline.resume_review_preview_job(parent_project, parent["job_id"])
    assert resumed["status"] == "queued"
    assert wb._load_for_write(parent_project)["automation"]["review_preview_pipeline"][
        "tts_terminal_retry_authorized"
    ] is True


def test_tts_voice_signature_drift_is_checkpointed_and_nonretryable(tmp_path: Path) -> None:
    project = tmp_path / "tts-voice-drift"
    write_json(project / "project.json", {"project_id": "voice-drift"})
    fake = FakeRecoverableTTS(voice_signature="wrong-signature")
    with pytest.raises(narration_lines.NarrationVoiceDriftError):
        narration_lines.materialize_line_audio(
            project,
            recoverable_line_plan(),
            tts_client=fake,
            parent_job_id="RPP-parent",
            worker_token="worker-a",
            commit=lambda action: action(),
            poll_interval_seconds=0,
        )
    record = narration_lines.load_ledger(project)["lines"][0]
    assert record["tts_task_id"]
    assert record["voice_signature"] == "wrong-signature"
    assert fake.download_calls == []
    assert issubclass(narration_lines.NarrationVoiceDriftError, narration_lines.NarrationEvidenceDriftError)


def test_tts_old_lease_cannot_checkpoint_download_or_promote(tmp_path: Path) -> None:
    project = tmp_path / "tts-stale-lease"
    write_json(project / "project.json", {"project_id": "stale-lease"})
    fake = FakeRecoverableTTS()
    commits = {"count": 0}

    def expire_before_download(action) -> None:
        commits["count"] += 1
        if commits["count"] == 7:
            raise narration_lines.StaleNarrationWorker("old lease expired before download")
        action()

    with pytest.raises(narration_lines.StaleNarrationWorker, match="before download"):
        narration_lines.materialize_line_audio(
            project,
            recoverable_line_plan(),
            tts_client=fake,
            parent_job_id="RPP-parent",
            worker_token="old-worker",
            commit=expire_before_download,
            poll_interval_seconds=0,
        )
    checkpoint = narration_lines.load_ledger(project)["lines"][0]
    assert checkpoint["tts_task_id"]
    assert fake.download_calls == []
    assert not checkpoint.get("output_path")

    completed = narration_lines.materialize_line_audio(
        project,
        recoverable_line_plan(),
        tts_client=fake,
        parent_job_id="RPP-parent",
        worker_token="new-worker",
        commit=lambda action: action(),
        poll_interval_seconds=0,
    )
    assert len(fake.submit_calls) == 1
    assert fake.query_calls[-1] == checkpoint["tts_task_id"]
    assert len(fake.download_calls) == 1
    assert completed["lines"][0]["status"] == "completed"


def test_duplicate_section_preflight_is_safe_parent_error_before_capability_probe(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project = make_project(tmp_path)
    script = wb._read_json(project / "artifacts" / "script.json")
    script["sections"][1]["id"] = script["sections"][0]["id"]
    write_json(project / "artifacts" / "script.json", script)
    probed = {"value": False}

    def forbidden_probe() -> dict:
        probed["value"] = True
        raise AssertionError("capabilities must not be probed")

    monkeypatch.setattr(pipeline, "collect_review_preview_capabilities", forbidden_probe)
    with pytest.raises(pipeline.ReviewPreviewError, match="正式脚本合同无效.*重复"):
        pipeline.review_preview_preflight(project)
    with pytest.raises(pipeline.ReviewPreviewError, match="正式脚本合同无效.*重复"):
        pipeline.start_review_preview_job(project, start_payload())
    assert probed["value"] is False
    assert not (project / narration_lines.LEDGER_PATH).exists()


def test_audio_gate_drift_terminates_old_parent_and_allows_new_job(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    state = wb._load_for_write(project)
    state["narration_policy"]["playback_gain_db"] = 3.0
    state["narration_policy"]["updated_at"] = "user-changed"
    wb._save(project, state)
    started = pipeline.start_review_preview_job(project, start_payload(), capabilities=capabilities())
    state = wb._load_for_write(project)
    parent = state["automation"]["review_preview_pipeline"]
    parent.update({"status": "queued", "stage": "audio_sample", "safe_resume_point": "audio_sample", "worker_token": None})
    sample_path = project / "renders" / "music-samples" / "ready.mp4"
    sample_path.parent.mkdir(parents=True, exist_ok=True)
    sample_path.write_bytes(b"ready-sample")
    state["music_policy"]["sample"].update(
        {
            "status": "ready",
            "output_path": sample_path.relative_to(project).as_posix(),
            "policy_signature": parent["frozen_input"]["audio_mix_signature"],
            "parent_job_id": started["job_id"],
            "request_fingerprint": started["request_fingerprint"],
        }
    )
    wb._save(project, state)
    waiting = pipeline.run_review_preview_job(project, started["job_id"])
    assert waiting["status"] == "awaiting_human"

    wb.update_narration_policy(project, {"playback_gain_db": 6.0})
    approve_calls: list[str] = []
    with pytest.raises(pipeline.ReviewPreviewConflict, match="声音设置已变化"):
        pipeline.resume_review_preview_job(
            project,
            started["job_id"],
            {"confirmed": True},
            dependencies={"approve_audio_sample": lambda *_args: approve_calls.append("approved")},
        )
    failed = wb._load_for_write(project)["automation"]["review_preview_pipeline"]
    assert failed["status"] == "failed"
    assert failed["error"]["retryable"] is False
    assert failed["worker_token"] is None and failed["gate"] is None
    assert approve_calls == []
    replacement = pipeline.start_review_preview_job(project, start_payload(), capabilities=capabilities())
    assert replacement["job_id"] != started["job_id"]
    assert replacement["launch_required"] is True
    assert sample_path.read_bytes() == b"ready-sample"


def test_audio_gate_approved_crash_window_requeues_idempotently(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    state = wb._load_for_write(project)
    state["narration_policy"]["playback_gain_db"] = 3.0
    state["narration_policy"]["updated_at"] = "user-changed"
    wb._save(project, state)
    started = pipeline.start_review_preview_job(project, start_payload(), capabilities=capabilities())
    state = wb._load_for_write(project)
    parent = state["automation"]["review_preview_pipeline"]
    parent.update(
        {
            "status": "awaiting_human",
            "stage": "audio_sample",
            "safe_resume_point": "audio_sample",
            "worker_token": None,
            "gate": {"stage": "audio_sample", "reason": "等待试听"},
        }
    )
    sample_path = project / "renders" / "music-samples" / "approved.mp4"
    sample_path.parent.mkdir(parents=True, exist_ok=True)
    sample_path.write_bytes(b"approved")
    state["music_policy"]["sample"].update(
        {
            "status": "approved",
            "output_path": sample_path.relative_to(project).as_posix(),
            "policy_signature": parent["frozen_input"]["audio_mix_signature"],
            "parent_job_id": started["job_id"],
            "request_fingerprint": started["request_fingerprint"],
        }
    )
    wb._save(project, state)
    approve_calls: list[str] = []
    resumed = pipeline.resume_review_preview_job(
        project,
        started["job_id"],
        {"confirmed": True},
        dependencies={"approve_audio_sample": lambda *_args: approve_calls.append("duplicate")},
    )
    assert resumed["status"] == "queued" and resumed["stage"] == "full_preview"
    assert resumed["launch_required"] is True
    assert approve_calls == []


def test_audio_sample_generating_child_recovers_by_owner_without_second_start(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    state = wb._load_for_write(project)
    state["narration_policy"]["playback_gain_db"] = 3.0
    state["narration_policy"]["updated_at"] = "user-changed"
    wb._save(project, state)
    started = pipeline.start_review_preview_job(project, start_payload(), capabilities=capabilities())
    state = wb._load_for_write(project)
    parent = state["automation"]["review_preview_pipeline"]
    parent.update({"status": "queued", "stage": "audio_sample", "safe_resume_point": "audio_sample", "worker_token": None})
    state["music_policy"]["sample"].update(
        {
            "status": "generating",
            "job_id": "sample-child",
            "scene_id": "scene-01",
            "policy_signature": parent["frozen_input"]["audio_mix_signature"],
            "parent_job_id": started["job_id"],
            "request_fingerprint": started["request_fingerprint"],
        }
    )
    wb._save(project, state)
    calls: list[str] = []

    def generate(project_dir: Path) -> dict:
        latest = wb._load_for_write(project_dir)
        output = project_dir / "renders" / "music-samples" / "recovered.mp4"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"recovered")
        latest["music_policy"]["sample"].update(
            {"status": "ready", "output_path": output.relative_to(project_dir).as_posix()}
        )
        calls.append("generate")
        return wb._save(project_dir, latest)

    waiting = pipeline.run_review_preview_job(
        project,
        started["job_id"],
        dependencies={
            "start_audio_sample": lambda *_args: pytest.fail("must not start the existing child twice"),
            "generate_audio_sample": generate,
        },
    )
    assert waiting["status"] == "awaiting_human"
    assert calls == ["generate"]

    foreign = make_project(tmp_path / "foreign")
    foreign_state = wb._load_for_write(foreign)
    foreign_state["narration_policy"]["playback_gain_db"] = 3.0
    foreign_state["narration_policy"]["updated_at"] = "user-changed"
    wb._save(foreign, foreign_state)
    foreign_parent = pipeline.start_review_preview_job(foreign, start_payload(), capabilities=capabilities())
    foreign_state = wb._load_for_write(foreign)
    parent = foreign_state["automation"]["review_preview_pipeline"]
    parent.update({"status": "queued", "stage": "audio_sample", "safe_resume_point": "audio_sample", "worker_token": None})
    foreign_state["music_policy"]["sample"].update(
        {
            "status": "generating",
            "job_id": "foreign-child",
            "policy_signature": parent["frozen_input"]["audio_mix_signature"],
            "parent_job_id": "RPP-someone-else",
            "request_fingerprint": parent["request_fingerprint"],
        }
    )
    wb._save(foreign, foreign_state)
    blocked = pipeline.run_review_preview_job(
        foreign,
        foreign_parent["job_id"],
        dependencies={"generate_audio_sample": lambda *_args: pytest.fail("foreign child must not run")},
    )
    assert blocked["status"] == "failed"
    assert blocked["error"]["type"] == "ReviewPreviewConflict"
    unchanged = wb.read_workbench(foreign)["music_policy"]["sample"]
    assert unchanged["status"] == "generating" and unchanged["parent_job_id"] == "RPP-someone-else"


def test_visual_plan_recovers_owned_persisted_child_without_replanning(tmp_path: Path) -> None:
    project = make_project(tmp_path, with_visuals=False)
    started = pipeline.start_review_preview_job(project, start_payload(), capabilities=capabilities())
    state = wb._load_for_write(project)
    parent = state["automation"]["review_preview_pipeline"]
    parent.update({"status": "queued", "stage": "visual_plan", "safe_resume_point": "visual_plan", "worker_token": None})
    state["automation"]["visual_batch"].update(
        {
            "status": "queued",
            "job_id": "visual-child",
            "parent_job_id": started["job_id"],
            "request_fingerprint": started["request_fingerprint"],
            "preview_plan_id": "stable-plan",
            "items": [],
        }
    )
    wb._save(project, state)
    calls: list[str] = []

    def generate_visuals(project_dir: Path, expected_job_id: str | None = None) -> dict:
        assert expected_job_id == "visual-child"
        latest = wb._load_for_write(project_dir)
        visual_path = project_dir / "assets" / "recovered-visual.png"
        visual_path.parent.mkdir(parents=True, exist_ok=True)
        visual_path.write_bytes(b"visual")
        asset = wb._append_asset(
            project_dir,
            latest,
            {"name": "恢复画面", "type": "image", "source_type": "human_provided", "path": str(visual_path)},
        )
        for scene in latest["scenes"]:
            wb._append_selected_usage(latest, scene["id"], asset["id"], "visual")
            wb._set_single_visual_block(latest, scene, asset)
        latest["automation"]["visual_batch"].update(
            {"status": "completed", "completed_slots": 2, "failed_slots": 0}
        )
        calls.append("visual:generate")
        return wb._save(project_dir, latest)

    def start_preview(project_dir: Path, payload: dict) -> dict:
        latest = wb._load_for_write(project_dir)
        latest["automation"]["preview_render"].update(
            {
                "status": "generating",
                "parent_job_id": payload["_review_preview_job_id"],
                "input_fingerprint": payload["_review_preview_input_fingerprint"],
            }
        )
        return wb._save(project_dir, latest)

    def generate_preview(project_dir: Path) -> dict:
        latest = wb._load_for_write(project_dir)
        preview = project_dir / "renders" / "previews" / "visual-recovery.mp4"
        report = project_dir / wb.AUTOMATION_PREVIEW_RENDER_REPORT
        preview.parent.mkdir(parents=True, exist_ok=True)
        preview.write_bytes(b"preview")
        write_json(report, {"status": "completed"})
        latest["automation"]["preview_render"].update(
            {"status": "completed", "output_path": preview.relative_to(project_dir).as_posix(), "report_path": wb.AUTOMATION_PREVIEW_RENDER_REPORT}
        )
        return wb._save(project_dir, latest)

    completed = pipeline.run_review_preview_job(
        project,
        started["job_id"],
        dependencies={
            "preview_visual_plan": lambda *_args: pytest.fail("owned child must skip replanning"),
            "start_visual_generation": lambda *_args: pytest.fail("owned child must not start twice"),
            "generate_visuals": generate_visuals,
            "start_full_preview": start_preview,
            "generate_full_preview": generate_preview,
            "probe_preview": probe_mock_preview,
        },
    )
    assert completed["status"] == "failed"
    assert completed["safe_resume_point"] == "narration"
    assert calls == ["visual:generate"]
    assert completed["phases"]["visual_plan"]["output"]["recovered_child"] is True


def test_visual_plan_refuses_foreign_child_without_planner_or_media_calls(tmp_path: Path) -> None:
    project = make_project(tmp_path, with_visuals=False)
    started = pipeline.start_review_preview_job(project, start_payload(), capabilities=capabilities())
    state = wb._load_for_write(project)
    parent = state["automation"]["review_preview_pipeline"]
    parent.update({"status": "queued", "stage": "visual_plan", "safe_resume_point": "visual_plan", "worker_token": None})
    state["automation"]["visual_batch"].update(
        {
            "status": "queued",
            "job_id": "foreign-visual",
            "parent_job_id": "RPP-foreign",
            "request_fingerprint": parent["request_fingerprint"],
            "preview_plan_id": "foreign-plan",
        }
    )
    wb._save(project, state)
    blocked = pipeline.run_review_preview_job(
        project,
        started["job_id"],
        dependencies={
            "preview_visual_plan": lambda *_args: pytest.fail("must not replan over foreign child"),
            "start_visual_generation": lambda *_args: pytest.fail("must not replace foreign child"),
            "generate_visuals": lambda *_args, **_kwargs: pytest.fail("must not run foreign child"),
        },
    )
    assert blocked["status"] == "failed"
    assert blocked["error"]["type"] == "ReviewPreviewConflict"
    child = wb.read_workbench(project)["automation"]["visual_batch"]
    assert child["status"] == "queued" and child["parent_job_id"] == "RPP-foreign"


@pytest.mark.parametrize(
    ("owner_mode", "fingerprint_mode", "should_generate"),
    [
        ("missing", "same", False),
        ("same", "different", False),
        ("same", "same", True),
    ],
)
def test_visual_generation_requires_exact_parent_and_request_identity(
    tmp_path: Path,
    owner_mode: str,
    fingerprint_mode: str,
    should_generate: bool,
) -> None:
    project = make_project(tmp_path / f"{owner_mode}-{fingerprint_mode}", with_visuals=False)
    started = pipeline.start_review_preview_job(project, start_payload(), capabilities=capabilities())
    state = wb._load_for_write(project)
    parent = state["automation"]["review_preview_pipeline"]
    parent.update(
        {
            "status": "queued",
            "stage": "visual_generation",
            "safe_resume_point": "visual_generation",
            "worker_token": None,
        }
    )
    state["automation"]["visual_batch"].update(
        {
            "status": "queued",
            "job_id": "visual-identity-child",
            "parent_job_id": started["job_id"] if owner_mode == "same" else None,
            "request_fingerprint": (
                started["request_fingerprint"] if fingerprint_mode == "same" else "different-request"
            ),
        }
    )
    wb._save(project, state)
    calls: list[str] = []

    def generate_visuals(project_dir: Path, expected_job_id: str | None = None) -> dict:
        assert expected_job_id == "visual-identity-child"
        calls.append("generate")
        return wb.read_workbench(project_dir)

    result = pipeline.run_review_preview_job(
        project,
        started["job_id"],
        dependencies={"generate_visuals": generate_visuals},
    )
    assert calls == (["generate"] if should_generate else [])
    if not should_generate:
        assert result["status"] == "failed"
        assert result["error"]["type"] == "ReviewPreviewConflict"
        child = wb.read_workbench(project)["automation"]["visual_batch"]
        assert child["status"] == "queued"
    else:
        assert result["status"] == "failed"
        assert result["error"]["type"] == "ReviewPreviewError"


def test_full_preview_generating_child_recovers_by_owner_without_second_start(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    started = pipeline.start_review_preview_job(project, start_payload(), capabilities=capabilities())
    state = wb._load_for_write(project)
    parent = state["automation"]["review_preview_pipeline"]
    visual_signature = pipeline._current_input_contract(
        project, state, parent["frozen_input"]["script"]
    )["scene_visual_signature"]
    parent.update({"status": "queued", "stage": "full_preview", "safe_resume_point": "full_preview", "worker_token": None})
    parent.setdefault("phases", {})["visual_plan"] = {
        "status": "completed",
        "output": {"visual_signature": visual_signature},
    }
    state["automation"]["preview_render"].update(
        {
            "status": "generating",
            "version": 1,
            "parent_job_id": started["job_id"],
            "input_fingerprint": started["input_fingerprint"],
        }
    )
    wb._save(project, state)
    calls: list[str] = []

    def generate(project_dir: Path) -> dict:
        latest = wb._load_for_write(project_dir)
        preview = project_dir / "renders" / "previews" / "child-resume.mp4"
        report = project_dir / wb.AUTOMATION_PREVIEW_RENDER_REPORT
        preview.parent.mkdir(parents=True, exist_ok=True)
        preview.write_bytes(b"child-resume")
        write_json(report, {"status": "completed"})
        latest["automation"]["preview_render"].update(
            {"status": "completed", "output_path": preview.relative_to(project_dir).as_posix(), "report_path": wb.AUTOMATION_PREVIEW_RENDER_REPORT}
        )
        calls.append("generate")
        return wb._save(project_dir, latest)

    completed = pipeline.run_review_preview_job(
        project,
        started["job_id"],
        dependencies={
            "start_full_preview": lambda *_args: pytest.fail("must not start existing preview twice"),
            "generate_full_preview": generate,
            "probe_preview": probe_mock_preview,
        },
    )
    assert completed["status"] == "failed"
    assert completed["safe_resume_point"] == "narration"
    assert calls == ["generate"]

    foreign = make_project(tmp_path / "foreign")
    foreign_job = pipeline.start_review_preview_job(foreign, start_payload(), capabilities=capabilities())
    foreign_state = wb._load_for_write(foreign)
    foreign_parent = foreign_state["automation"]["review_preview_pipeline"]
    foreign_signature = pipeline._current_input_contract(
        foreign, foreign_state, foreign_parent["frozen_input"]["script"]
    )["scene_visual_signature"]
    foreign_parent.update({"status": "queued", "stage": "full_preview", "safe_resume_point": "full_preview", "worker_token": None})
    foreign_parent.setdefault("phases", {})["visual_plan"] = {
        "status": "completed",
        "output": {"visual_signature": foreign_signature},
    }
    foreign_state["automation"]["preview_render"].update(
        {
            "status": "generating",
            "parent_job_id": "RPP-foreign",
            "input_fingerprint": foreign_parent["input_fingerprint"],
        }
    )
    wb._save(foreign, foreign_state)
    blocked = pipeline.run_review_preview_job(
        foreign,
        foreign_job["job_id"],
        dependencies={
            "start_full_preview": lambda *_args: pytest.fail("must not replace foreign preview"),
            "generate_full_preview": lambda *_args: pytest.fail("must not run foreign preview"),
        },
    )
    assert blocked["status"] == "failed"
    unchanged = wb.read_workbench(foreign)["automation"]["preview_render"]
    assert unchanged["status"] == "generating" and unchanged["parent_job_id"] == "RPP-foreign"


def test_pipeline_type_drift_blocks_worker_and_unsupported_recovery(tmp_path: Path) -> None:
    avatar_project = make_project(tmp_path / "avatar-drift")
    avatar_job = pipeline.start_review_preview_job(avatar_project, start_payload(), capabilities=capabilities())
    raw = wb._read_json(avatar_project / "project.json")
    raw["pipeline_type"] = wb.AVATAR_PIPELINE
    write_json(avatar_project / "project.json", raw)
    recovered = pipeline.recover_review_preview_job(avatar_project)
    assert recovered["status"] == "idle" and recovered["launch_required"] is False
    result = pipeline.run_review_preview_job(
        avatar_project,
        avatar_job["job_id"],
        dependencies={"synthesize_line": lambda *_args: pytest.fail("avatar drift must not start TTS")},
    )
    assert result["status"] == "idle" and result["job_id"] is None
    hidden_parent = wb._load_for_write(avatar_project)["automation"]["review_preview_pipeline"]
    assert hidden_parent["status"] == "failed" and hidden_parent["error"]["retryable"] is False
    assert hidden_parent["worker_token"] is None
    assert not (avatar_project / narration_lines.LEDGER_PATH).exists()

    unsupported = make_project(tmp_path / "unsupported")
    unsupported_job = pipeline.start_review_preview_job(unsupported, start_payload(), capabilities=capabilities())
    raw = wb._read_json(unsupported / "project.json")
    raw["pipeline_type"] = "legacy-narration"
    write_json(unsupported / "project.json", raw)
    recovery = pipeline.recover_review_preview_job(unsupported)
    assert recovery["job_id"] == unsupported_job["job_id"]
    assert recovery["status"] == "failed" and recovery["launch_required"] is False
    assert recovery["error"]["retryable"] is False
    assert not (unsupported / narration_lines.LEDGER_PATH).exists()
    raw["pipeline_type"] = "animated-explainer"
    write_json(unsupported / "project.json", raw)
    replacement = pipeline.start_review_preview_job(
        unsupported, start_payload(), capabilities=capabilities()
    )
    assert replacement["job_id"] != unsupported_job["job_id"]


def test_contract_version_upgrade_never_reuses_or_resumes_old_semantics(
    tmp_path: Path,
    monkeypatch,
) -> None:
    completed_project = make_project(tmp_path / "completed")
    completed, _calls = complete_no_gate_review_job(completed_project)
    old_preview = completed_project / completed["result"]["preview_path"]
    assert old_preview.is_file()
    monkeypatch.setattr(pipeline, "PIPELINE_VERSION", "2.0-test")
    replacement = pipeline.start_review_preview_job(
        completed_project, start_payload(), capabilities=capabilities()
    )
    assert replacement["job_id"] != completed["job_id"]
    assert replacement["frozen_input"]["versions"]["pipeline"] == "2.0-test"
    assert old_preview.is_file()

    queued_project = make_project(tmp_path / "queued")
    queued = pipeline.start_review_preview_job(queued_project, start_payload(), capabilities=capabilities())
    state = wb._load_for_write(queued_project)
    state["automation"]["review_preview_pipeline"]["frozen_input"]["versions"]["pipeline"] = "1.0-old"
    state["automation"]["visual_batch"].update(
        {
            "status": "queued",
            "job_id": "owned-old-visual",
            "parent_job_id": queued["job_id"],
            "request_fingerprint": queued["request_fingerprint"],
        }
    )
    state["automation"]["preview_render"].update(
        {
            "status": "completed",
            "parent_job_id": queued["job_id"],
            "input_fingerprint": queued["input_fingerprint"],
            "error": "completed-evidence",
        }
    )
    wb._save(queued_project, state)
    recovery = pipeline.recover_review_preview_job(queued_project)
    assert recovery["launch_required"] is False and recovery["status"] == "failed"
    assert recovery["error"]["retryable"] is False
    assert "版本已升级" in recovery["error"]["message"]
    cleaned = wb.read_workbench(queued_project)["automation"]
    assert cleaned["visual_batch"]["status"] == "failed"
    assert cleaned["preview_render"]["status"] == "completed"
    assert cleaned["preview_render"]["error"] == "completed-evidence"
    replacement = pipeline.start_review_preview_job(
        queued_project, start_payload(), capabilities=capabilities()
    )
    assert replacement["job_id"] != queued["job_id"]


@pytest.mark.parametrize("damage", ["ledger_deleted", "line_wav_tampered", "aggregate_wav_tampered"])
def test_completed_cache_requires_full_sentence_and_aggregate_audio_evidence(
    tmp_path: Path,
    damage: str,
) -> None:
    project = make_project(tmp_path / damage)
    completed, _calls = complete_no_gate_review_job(project)
    old_preview = project / completed["result"]["preview_path"]
    ledger = narration_lines.load_ledger(project)
    if damage == "ledger_deleted":
        (project / narration_lines.LEDGER_PATH).unlink()
    elif damage == "line_wav_tampered":
        line_path = project / ledger["lines"][0]["output_path"]
        payload = bytearray(line_path.read_bytes())
        payload[-1] ^= 1
        line_path.write_bytes(payload)
    else:
        state = wb.read_workbench(project)
        aggregate = project / state["automation"]["narration_generation"]["audio_path"]
        payload = bytearray(aggregate.read_bytes())
        payload[-1] ^= 1
        aggregate.write_bytes(payload)
    replacement = pipeline.start_review_preview_job(project, start_payload(), capabilities=capabilities())
    assert replacement["job_id"] != completed["job_id"]
    assert replacement["launch_required"] is True
    assert old_preview.is_file()


def test_completed_cache_matches_voice_and_visual_request_identity(tmp_path: Path) -> None:
    voice_project = make_project(tmp_path / "voice")
    completed, _calls = complete_no_gate_review_job(voice_project)
    old_preview = voice_project / completed["result"]["preview_path"]
    replacement = pipeline.start_review_preview_job(
        voice_project,
        start_payload(),
        capabilities=capabilities(explicit=True, selected="voice-mengmeng"),
    )
    assert replacement["job_id"] != completed["job_id"]
    assert replacement["frozen_input"]["voice"]["profile_name"] == "檬檬"
    assert old_preview.is_file()

    visual_project = make_project(tmp_path / "visual")
    visual_completed, _calls = complete_no_gate_review_job(visual_project)
    changed_payload = start_payload(
        visual={"planning_mode": "ai_director", "image_source": "web_download"},
        text_ai_confirmed=True,
    )
    visual_replacement = pipeline.start_review_preview_job(
        visual_project,
        changed_payload,
        capabilities=capabilities(),
    )
    assert visual_replacement["job_id"] != visual_completed["job_id"]
    assert visual_replacement["frozen_input"]["visual"]["planning_mode"] == "ai_director"


@pytest.mark.parametrize("child_kind", ["visual", "preview", "sample"])
def test_nonretryable_drift_cleans_only_owned_orphan_child_and_allows_new_parent(
    tmp_path: Path,
    child_kind: str,
) -> None:
    project = make_project(tmp_path / child_kind)
    started = pipeline.start_review_preview_job(project, start_payload(), capabilities=capabilities())
    state = wb._load_for_write(project)
    parent = state["automation"]["review_preview_pipeline"]
    if child_kind == "visual":
        state["automation"]["visual_batch"].update(
            {
                "status": "queued",
                "parent_job_id": started["job_id"],
                "request_fingerprint": started["request_fingerprint"],
            }
        )
    elif child_kind == "preview":
        state["automation"]["preview_render"].update(
            {
                "status": "generating",
                "parent_job_id": started["job_id"],
                "input_fingerprint": started["input_fingerprint"],
            }
        )
    else:
        state["music_policy"]["sample"].update(
            {
                "status": "generating",
                "parent_job_id": started["job_id"],
                "request_fingerprint": started["request_fingerprint"],
                "policy_signature": parent["frozen_input"]["audio_mix_signature"],
            }
        )
    wb._save(project, state)
    script = wb._read_json(project / "artifacts" / "script.json")
    script["title"] = f"漂移后的脚本-{child_kind}"
    write_json(project / "artifacts" / "script.json", script)
    failed = pipeline.run_review_preview_job(project, started["job_id"])
    assert failed["status"] == "failed" and failed["error"]["retryable"] is False
    latest = wb.read_workbench(project)
    child = (
        latest["automation"]["visual_batch"]
        if child_kind == "visual"
        else latest["automation"]["preview_render"]
        if child_kind == "preview"
        else latest["music_policy"]["sample"]
    )
    assert child["status"] == "failed"
    assert "父任务已安全终止" in child["error"]
    replacement = pipeline.start_review_preview_job(project, start_payload(), capabilities=capabilities())
    assert replacement["job_id"] != started["job_id"] and replacement["launch_required"] is True


def test_nonretryable_drift_never_cleans_foreign_or_mismatched_child(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    started = pipeline.start_review_preview_job(project, start_payload(), capabilities=capabilities())
    state = wb._load_for_write(project)
    state["automation"]["visual_batch"].update(
        {
            "status": "queued",
            "parent_job_id": "RPP-foreign",
            "request_fingerprint": started["request_fingerprint"],
            "error": "foreign-evidence",
        }
    )
    state["automation"]["preview_render"].update(
        {
            "status": "generating",
            "parent_job_id": started["job_id"],
            "input_fingerprint": "different-input",
            "error": "different-evidence",
        }
    )
    wb._save(project, state)
    script = wb._read_json(project / "artifacts" / "script.json")
    script["title"] = "触发非重试漂移"
    write_json(project / "artifacts" / "script.json", script)
    failed = pipeline.run_review_preview_job(project, started["job_id"])
    assert failed["status"] == "failed" and failed["error"]["retryable"] is False
    latest = wb.read_workbench(project)
    assert latest["automation"]["visual_batch"]["status"] == "queued"
    assert latest["automation"]["visual_batch"]["error"] == "foreign-evidence"
    assert latest["automation"]["preview_render"]["status"] == "generating"
    assert latest["automation"]["preview_render"]["error"] == "different-evidence"


@pytest.mark.parametrize("damage", ["ledger", "line_wav", "aggregate_wav"])
def test_final_completion_revalidates_all_audio_evidence(tmp_path: Path, damage: str) -> None:
    project = make_project(tmp_path / damage)
    completed, _calls = complete_no_gate_review_job(project)
    ledger = narration_lines.load_ledger(project)
    state = wb._load_for_write(project)
    parent = state["automation"]["review_preview_pipeline"]
    parent.update(
        {
            "status": "queued",
            "stage": "review_ready",
            "safe_resume_point": "review_ready",
            "worker_token": None,
            "result": None,
        }
    )
    wb._save(project, state)
    if damage == "ledger":
        (project / narration_lines.LEDGER_PATH).unlink()
    elif damage == "line_wav":
        line_path = project / ledger["lines"][0]["output_path"]
        payload = bytearray(line_path.read_bytes())
        payload[-1] ^= 1
        line_path.write_bytes(payload)
    else:
        aggregate_path = project / state["automation"]["narration_generation"]["audio_path"]
        payload = bytearray(aggregate_path.read_bytes())
        payload[-1] ^= 1
        aggregate_path.write_bytes(payload)

    failed = pipeline.run_review_preview_job(
        project,
        completed["job_id"],
        dependencies={"probe_preview": probe_mock_preview},
    )
    assert failed["status"] == "failed"
    assert failed["stage"] == "narration"
    assert failed["safe_resume_point"] == "narration"
    assert failed["error"]["type"] == "CompletedAudioEvidenceError"
    assert failed["error"]["retryable"] is False


def test_completed_review_ready_phase_has_machine_auditable_shape(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    completed, _calls = complete_no_gate_review_job(project)
    phase = completed["phases"]["review_ready"]
    assert phase["status"] == "completed"
    assert phase["attempts"] >= 1
    assert phase["started_at"]
    assert phase["finished_at"]
    assert phase["error"] is None
    assert phase["retryable"] is False
    assert phase["safe_resume_point"] is None
    assert phase["input_fingerprint"] == completed["input_fingerprint"]
    assert phase["output"]["preview_sha256"] == completed["result"]["preview_sha256"]


def test_non_dict_script_section_is_safe_parent_error_before_capability_or_media(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project = make_project(tmp_path)
    script_path = project / "artifacts" / "script.json"
    script = wb._read_json(script_path)
    script["sections"][1] = "这段不能被静默丢弃"
    write_json(script_path, script)
    probe_calls: list[str] = []

    def forbidden_probe() -> dict:
        probe_calls.append("capability")
        raise AssertionError("invalid script must fail before capability probes")

    monkeypatch.setattr(pipeline, "collect_review_preview_capabilities", forbidden_probe)
    with pytest.raises(pipeline.ReviewPreviewError, match="section 不是对象"):
        pipeline.review_preview_preflight(project)
    with pytest.raises(pipeline.ReviewPreviewError, match="section 不是对象"):
        pipeline.start_review_preview_job(
            project,
            {"confirmed": True, "network_confirmed": True, "text_ai_confirmed": True},
        )
    assert probe_calls == []
    assert not (project / narration_lines.LEDGER_PATH).exists()
    assert not list((project / "assets" / "audio").glob("**/*.wav"))


def test_narration_line_root_symlink_cannot_escape_project(tmp_path: Path, monkeypatch) -> None:
    project = tmp_path / "symlink-project"
    external = tmp_path / "external-audio"
    external.mkdir(parents=True)
    sentinel = external / "sentinel.wav"
    sentinel.write_bytes(b"external-sentinel")
    line_root = project / narration_lines.LINE_AUDIO_DIRECTORY
    line_root.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.symlink(external, line_root, target_is_directory=True)
    except (OSError, NotImplementedError):
        # Windows hosts without Developer Mode cannot create a test symlink.
        # Simulate the same resolved-root escape while retaining a real local
        # directory, so the fail-closed branch remains mandatory and unskipped.
        line_root.mkdir(parents=True, exist_ok=True)
        original_resolve = Path.resolve
        external_resolved = external.resolve()

        def escaped_resolve(path: Path, *args, **kwargs) -> Path:
            if path == line_root:
                return external_resolved
            return original_resolve(path, *args, **kwargs)

        monkeypatch.setattr(Path, "resolve", escaped_resolve)
    calls: list[str] = []
    plan = narration_lines.build_line_plan(
        [{"id": "sec-01", "text": "不得逃逸。"}],
        {"profile_id": "voice-yaya", "profile_name": "雅雅", "engine": "qwen"},
    )

    def synth(*_args) -> dict:
        calls.append("tts")
        return {}

    with pytest.raises(narration_lines.NarrationLineError, match="symlink|junction|reparse|逃逸"):
        narration_lines.materialize_line_audio(project, plan, synth)
    assert calls == []
    assert sentinel.read_bytes() == b"external-sentinel"
    assert list(external.iterdir()) == [sentinel]
    assert not (project / narration_lines.LEDGER_PATH).exists()


def test_pipeline_type_drift_after_first_line_stops_all_later_tts(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    started = pipeline.start_review_preview_job(project, start_payload(), capabilities=capabilities())

    class DriftAfterFirstSubmit(FakeRecoverableTTS):
        def submit(self, inputs: dict, *, request_id: str) -> dict:
            result = super().submit(inputs, request_id=request_id)
            raw = wb._read_json(project / "project.json")
            raw["pipeline_type"] = "legacy-narration"
            write_json(project / "project.json", raw)
            return result

    fake = DriftAfterFirstSubmit()

    failed = pipeline.run_review_preview_job(
        project,
        started["job_id"],
        dependencies={"synthesize_line": None, "tts_client": fake},
    )
    assert len(fake.submit_calls) == 1
    assert fake.query_calls == []
    assert fake.download_calls == []
    assert failed["status"] == "failed"
    assert failed["error"]["type"] == "InputDriftError"
    assert failed["error"]["retryable"] is False
    ledger = narration_lines.load_ledger(project)
    assert ledger["lines"][1]["status"] == "planned"
    assert ledger["lines"][2]["status"] == "planned"


def _start_ai_visual_plan_job(project: Path) -> dict:
    started = pipeline.start_review_preview_job(
        project,
        {"confirmed": True, "network_confirmed": True, "text_ai_confirmed": True},
        capabilities=capabilities(),
    )
    state = wb._load_for_write(project)
    state["automation"]["review_preview_pipeline"].update(
        {
            "status": "queued",
            "stage": "visual_plan",
            "safe_resume_point": "visual_plan",
            "worker_token": None,
        }
    )
    wb._save(project, state)
    return started


def test_ai_visual_plan_crash_after_dispatch_marker_never_calls_model_on_recovery(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project = make_project(tmp_path, with_visuals=False)
    started = _start_ai_visual_plan_job(project)
    original_mutate = pipeline._mutate_job
    crashed = {"done": False}

    def crash_after_dispatch(project_dir, job_id, worker_token, mutator):
        result = original_mutate(project_dir, job_id, worker_token, mutator)
        if mutator.__name__ == "mark_planner_dispatched" and not crashed["done"]:
            crashed["done"] = True
            raise pipeline.StaleReviewPreviewWorker("crash after durable dispatch marker")
        return result

    monkeypatch.setattr(pipeline, "_mutate_job", crash_after_dispatch)
    model_calls: list[str] = []
    with pytest.raises(pipeline.StaleReviewPreviewWorker):
        pipeline.run_review_preview_job(
            project,
            started["job_id"],
            dependencies={"preview_visual_plan": lambda *_args: model_calls.append("model")},
        )
    monkeypatch.setattr(pipeline, "_mutate_job", original_mutate)
    recovery = pipeline.recover_review_preview_job(project)
    assert recovery["launch_required"] is True
    gated = pipeline.run_review_preview_job(
        project,
        started["job_id"],
        dependencies={"preview_visual_plan": lambda *_args: model_calls.append("model")},
    )
    assert model_calls == []
    assert gated["status"] == "awaiting_human"
    assert gated["phases"]["visual_plan"]["planner_status"] == "dispatched_ambiguous"


def test_ai_visual_plan_result_lost_before_checkpoint_is_ambiguous_not_retried(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project = make_project(tmp_path, with_visuals=False)
    started = _start_ai_visual_plan_job(project)
    original_mutate = pipeline._mutate_job
    model_calls: list[str] = []

    def lose_result_checkpoint(project_dir, job_id, worker_token, mutator):
        if mutator.__name__ == "checkpoint_reviewed_plan":
            raise pipeline.StaleReviewPreviewWorker("crash before reviewed plan checkpoint")
        return original_mutate(project_dir, job_id, worker_token, mutator)

    monkeypatch.setattr(pipeline, "_mutate_job", lose_result_checkpoint)

    def model(*_args) -> dict:
        model_calls.append("model")
        return {"plan_id": "returned-but-not-checkpointed", "items": []}

    with pytest.raises(pipeline.StaleReviewPreviewWorker):
        pipeline.run_review_preview_job(
            project,
            started["job_id"],
            dependencies={"preview_visual_plan": model},
        )
    monkeypatch.setattr(pipeline, "_mutate_job", original_mutate)
    pipeline.recover_review_preview_job(project)
    gated = pipeline.run_review_preview_job(
        project,
        started["job_id"],
        dependencies={"preview_visual_plan": model},
    )
    assert model_calls == ["model"]
    assert gated["status"] == "awaiting_human"
    phase = gated["phases"]["visual_plan"]
    assert phase["reviewed_plan"] is None
    assert phase["planner_status"] == "dispatched_ambiguous"


def test_ai_visual_plan_checkpoint_is_reused_after_child_start_crash(tmp_path: Path) -> None:
    project = make_project(tmp_path, with_visuals=False)
    started = _start_ai_visual_plan_job(project)
    model_calls: list[str] = []
    start_calls: list[str] = []

    def model(*_args) -> dict:
        model_calls.append("model")
        return {"plan_id": "durable-reviewed-plan", "items": []}

    def crash_start(*_args) -> dict:
        start_calls.append("start")
        raise pipeline.StaleReviewPreviewWorker("crash before visual child commit")

    with pytest.raises(pipeline.StaleReviewPreviewWorker):
        pipeline.run_review_preview_job(
            project,
            started["job_id"],
            dependencies={
                "preview_visual_plan": model,
                "start_visual_generation": crash_start,
            },
        )
    checkpoint = wb.read_workbench(project)["automation"]["review_preview_pipeline"]["phases"]["visual_plan"]
    assert checkpoint["planner_status"] == "completed"
    assert checkpoint["reviewed_plan"]["plan_id"] == "durable-reviewed-plan"
    assert checkpoint["reviewed_plan_hash"] == pipeline._json_hash(checkpoint["reviewed_plan"])
    pipeline.recover_review_preview_job(project)
    with pytest.raises(pipeline.StaleReviewPreviewWorker):
        pipeline.run_review_preview_job(
            project,
            started["job_id"],
            dependencies={
                "preview_visual_plan": model,
                "start_visual_generation": crash_start,
            },
        )
    assert model_calls == ["model"]
    assert start_calls == ["start", "start"]


def _prepare_parent_owned_preview_render(project: Path) -> tuple[dict, str]:
    started = pipeline.start_review_preview_job(project, start_payload(), capabilities=capabilities())
    narration_path = project / "assets" / "audio" / "project-narration.wav"
    write_pcm_wav(narration_path, duration=0.3)
    state = wb._load_for_write(project)
    parent = state["automation"]["review_preview_pipeline"]
    parent.update({"status": "running", "worker_token": "render-worker"})
    state["automation"]["narration_generation"].update(
        {
            "status": "completed",
            "audio_path": narration_path.relative_to(project).as_posix(),
        }
    )
    state["automation"]["preview_render"] = {
        "status": "generating",
        "runtime": "ffmpeg",
        "output_path": None,
        "version": 1,
        "job_id": "PRJ-parent-owned",
        "parent_job_id": started["job_id"],
        "input_fingerprint": started["input_fingerprint"],
        "error": "",
    }
    wb._save(project, state)
    return started, "PRJ-parent-owned"


def _install_blocking_preview_render_mocks(monkeypatch, entered: threading.Event, release: threading.Event) -> None:
    monkeypatch.setattr(wb, "_ffmpeg_available", lambda: "mock-ffmpeg")

    def visual_timeline(project_dir: Path, _state: dict, scene: dict, _ffmpeg: str) -> Path:
        output = project_dir / "renders" / "tmp" / f"{scene['id']}.mp4"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"visual-timeline")
        return output

    monkeypatch.setattr(wb, "_materialize_scene_visual_timeline", visual_timeline)
    monkeypatch.setattr(wb, "_daily_story_headline_overlays", lambda *_args: ([], {"assets": []}))
    monkeypatch.setattr(wb, "_apply_surgical_directives_to_video", lambda *_args: [])
    monkeypatch.setattr(
        wb,
        "_apply_project_audio_mix",
        lambda *_args: {
            "narration": {"enabled": False},
            "background_music": {"enabled": False},
        },
    )
    monkeypatch.setattr(
        wb,
        "_normalize_video_loudness",
        lambda *_args, **_kwargs: {
            "integrated_lufs": -14.0,
            "true_peak_dbtp": -1.2,
        },
    )

    class BlockingCompose:
        def execute(self, inputs: dict) -> SimpleNamespace:
            entered.set()
            assert release.wait(3), "test did not release blocked render"
            output = Path(inputs["output_path"])
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b"isolated-preview-output")
            return SimpleNamespace(success=True, data={"mock": True}, error=None)

    monkeypatch.setattr(wb, "VideoCompose", BlockingCompose)


@pytest.mark.parametrize("replacement", ["parent", "child"])
def test_preview_render_stale_worker_cannot_commit_over_replaced_identity(
    tmp_path: Path,
    monkeypatch,
    replacement: str,
) -> None:
    project = make_project(tmp_path / replacement)
    started, child_job_id = _prepare_parent_owned_preview_render(project)
    entered = threading.Event()
    release = threading.Event()
    _install_blocking_preview_render_mocks(monkeypatch, entered, release)
    errors: list[Exception] = []

    def render() -> None:
        try:
            wb.generate_full_preview_render(project)
        except Exception as exc:
            errors.append(exc)

    thread = threading.Thread(target=render)
    thread.start()
    assert entered.wait(2)
    with wb._project_transaction_lock(project):
        latest = wb._load_for_write(project)
        latest["concurrent_marker"] = "must-survive"
        if replacement == "parent":
            latest["automation"]["review_preview_pipeline"].update(
                {"job_id": "RPP-replacement", "worker_token": "replacement-worker"}
            )
        else:
            latest["automation"]["preview_render"]["job_id"] = "PRJ-replacement"
        wb._save(project, latest)
    release.set()
    thread.join(5)
    assert not thread.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], wb.WorkbenchError)
    latest = wb.read_workbench(project)
    assert latest["concurrent_marker"] == "must-survive"
    if replacement == "parent":
        assert latest["automation"]["review_preview_pipeline"]["job_id"] == "RPP-replacement"
        assert latest["automation"]["preview_render"]["job_id"] == child_job_id
    else:
        assert latest["automation"]["review_preview_pipeline"]["job_id"] == started["job_id"]
        assert latest["automation"]["preview_render"]["job_id"] == "PRJ-replacement"
    assert not (project / "renders" / "previews" / "full-preview-v001.mp4").exists()
    assert list((project / "renders" / "previews").glob(".full-preview-v001-*.staged.mp4"))


def test_preview_render_cas_merges_into_latest_state_without_losing_unrelated_change(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project = make_project(tmp_path)
    started, child_job_id = _prepare_parent_owned_preview_render(project)
    entered = threading.Event()
    release = threading.Event()
    _install_blocking_preview_render_mocks(monkeypatch, entered, release)
    results: list[dict] = []

    thread = threading.Thread(target=lambda: results.append(wb.generate_full_preview_render(project)))
    thread.start()
    assert entered.wait(2)
    with wb._project_transaction_lock(project):
        latest = wb._load_for_write(project)
        latest["concurrent_marker"] = {"revision": 2}
        wb._save(project, latest)
    release.set()
    thread.join(5)
    assert not thread.is_alive()
    assert len(results) == 1
    latest = wb.read_workbench(project)
    assert latest["concurrent_marker"] == {"revision": 2}
    assert latest["automation"]["review_preview_pipeline"]["job_id"] == started["job_id"]
    preview = latest["automation"]["preview_render"]
    assert preview["status"] == "completed"
    assert preview["job_id"] == child_job_id
    assert (project / preview["output_path"]).read_bytes() == b"isolated-preview-output"
    assert (project / preview["report_path"]).is_file()


def test_visual_batch_stops_before_second_external_call_after_parent_type_drift(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project = make_project(tmp_path, with_visuals=False)
    started = pipeline.start_review_preview_job(project, start_payload(), capabilities=capabilities())
    state = wb._load_for_write(project)
    parent = state["automation"]["review_preview_pipeline"]
    parent.update({"status": "running", "worker_token": "visual-worker"})
    items: list[dict] = []
    for index, scene in enumerate(state["scenes"], 1):
        block_id = f"block-{index:02d}"
        scene["visual_timeline"]["blocks"] = [
            {
                "id": block_id,
                "start_seconds": 0.0,
                "end_seconds": 1.0,
                "status": "planned",
            }
        ]
        items.append(
            {
                "scene_id": scene["id"],
                "block_id": block_id,
                "status": "queued",
                "slot_index": index - 1,
                "target_duration_seconds": 1.0,
                "query": f"query-{index}",
                "route": "stock_video",
                "source_mode": "web_download",
                "media_kind": "video",
                "planning_mode": "rule_mix",
            }
        )
    state["automation"]["visual_batch"].update(
        {
            "status": "queued",
            "job_id": "visual-loop-child",
            "parent_job_id": started["job_id"],
            "request_fingerprint": started["request_fingerprint"],
            "items": items,
        }
    )
    wb._save(project, state)
    calls: list[str] = []

    def first_external(project_dir: Path, *_args, **_kwargs):
        calls.append("pexels")
        raw = wb._read_json(project_dir / "project.json")
        raw["pipeline_type"] = "legacy-narration"
        write_json(project_dir / "project.json", raw)
        media = project_dir / "assets" / "video" / "first.mp4"
        media.parent.mkdir(parents=True, exist_ok=True)
        media.write_bytes(b"mock-video")
        return (
            SimpleNamespace(
                success=True,
                data={
                    "video_id": "video-1",
                    "width": 1080,
                    "height": 1920,
                    "duration_seconds": 1.0,
                },
                error=None,
            ),
            media.relative_to(project_dir).as_posix(),
            {"status": "accepted"},
        )

    monkeypatch.setattr(wb, "_find_screened_pexels_candidate", first_external)
    monkeypatch.setattr(wb, "_probe_duration_seconds", lambda *_args: 1.0)
    with pytest.raises(wb.WorkbenchError, match="停止后续画面外部调用"):
        wb.generate_visual_batch(
            project,
            expected_job_id="visual-loop-child",
            expected_parent_job_id=started["job_id"],
            expected_worker_token="visual-worker",
            expected_request_fingerprint=started["request_fingerprint"],
            expected_contract_versions=pipeline._current_contract_versions(),
        )
    assert calls == ["pexels"]


def _prepare_two_slot_visual_batch(
    project: Path,
    *,
    parent: dict | None,
) -> None:
    state = wb._load_for_write(project)
    if parent is not None:
        state["automation"]["review_preview_pipeline"].update(
            {"status": "running", "worker_token": "visual-cas-worker"}
        )
    items: list[dict] = []
    for index, scene in enumerate(state["scenes"], 1):
        block_id = f"cas-block-{index:02d}"
        scene["visual_timeline"]["blocks"] = [
            {
                "id": block_id,
                "start_seconds": 0.0,
                "end_seconds": 1.0,
                "status": "planned",
            }
        ]
        items.append(
            {
                "scene_id": scene["id"],
                "block_id": block_id,
                "status": "queued",
                "slot_index": index - 1,
                "target_duration_seconds": 1.0,
                "query": f"cas-query-{index}",
                "route": "stock_video",
                "source_mode": "web_download",
                "media_kind": "video",
                "planning_mode": "rule_mix",
            }
        )
    state["automation"]["visual_batch"] = {
        "status": "queued",
        "job_id": "visual-cas-child",
        "parent_job_id": parent["job_id"] if parent is not None else None,
        "request_fingerprint": parent["request_fingerprint"] if parent is not None else None,
        "items": items,
        "total_slots": 2,
        "completed_slots": 0,
        "failed_slots": 0,
        "current": None,
        "error": "",
    }
    wb._save(project, state)


@pytest.mark.parametrize("replacement", ["parent", "child"])
def test_visual_slot_cas_rejects_replaced_parent_or_child_after_blocking_external_call(
    tmp_path: Path,
    monkeypatch,
    replacement: str,
) -> None:
    project = make_project(tmp_path / replacement, with_visuals=False)
    parent = pipeline.start_review_preview_job(project, start_payload(), capabilities=capabilities())
    _prepare_two_slot_visual_batch(project, parent=parent)
    entered = threading.Event()
    release = threading.Event()
    calls: list[str] = []
    isolated = project / "assets" / "video" / "isolated" / "first-result.mp4"

    def blocking_external(project_dir: Path, candidate_state: dict, *_args, **_kwargs):
        calls.append("pexels")
        entered.set()
        assert release.wait(3)
        # Reproduce nested Pexels/director progress saves from the stale
        # snapshot; the slot transaction must defer this write.
        wb._save(project_dir, candidate_state)
        isolated.parent.mkdir(parents=True, exist_ok=True)
        isolated.write_bytes(b"isolated-evidence")
        return (
            SimpleNamespace(
                success=True,
                data={
                    "video_id": "isolated-1",
                    "width": 1080,
                    "height": 1920,
                    "duration_seconds": 1.0,
                },
                error=None,
            ),
            isolated.relative_to(project_dir).as_posix(),
            {"status": "accepted"},
        )

    monkeypatch.setattr(wb, "_find_screened_pexels_candidate", blocking_external)
    monkeypatch.setattr(wb, "_probe_duration_seconds", lambda *_args: 1.0)
    errors: list[Exception] = []

    def generate() -> None:
        try:
            wb.generate_visual_batch(
                project,
                expected_job_id="visual-cas-child",
                expected_parent_job_id=parent["job_id"],
                expected_worker_token="visual-cas-worker",
                expected_request_fingerprint=parent["request_fingerprint"],
                expected_contract_versions=pipeline._current_contract_versions(),
            )
        except Exception as exc:
            errors.append(exc)

    thread = threading.Thread(target=generate)
    thread.start()
    assert entered.wait(2)
    with wb._project_transaction_lock(project):
        latest = wb._load_for_write(project)
        latest["concurrent_visual_marker"] = "new-state-must-survive"
        if replacement == "parent":
            latest["automation"]["review_preview_pipeline"].update(
                {"job_id": "RPP-new-parent", "worker_token": "new-worker"}
            )
        else:
            latest["automation"]["visual_batch"].update(
                {"job_id": "visual-new-child", "status": "queued", "current": None}
            )
        wb._save(project, latest)
    release.set()
    thread.join(5)
    assert not thread.is_alive()
    assert calls == ["pexels"]
    assert len(errors) == 1 and isinstance(errors[0], wb.WorkbenchError)
    latest = wb.read_workbench(project)
    assert latest["concurrent_visual_marker"] == "new-state-must-survive"
    if replacement == "parent":
        assert latest["automation"]["review_preview_pipeline"]["job_id"] == "RPP-new-parent"
        assert latest["automation"]["visual_batch"]["job_id"] == "visual-cas-child"
    else:
        assert latest["automation"]["review_preview_pipeline"]["job_id"] == parent["job_id"]
        assert latest["automation"]["visual_batch"]["job_id"] == "visual-new-child"
    assert isolated.read_bytes() == b"isolated-evidence"
    assert all(asset.get("path") != isolated.relative_to(project).as_posix() for asset in latest["assets"])


@pytest.mark.parametrize("parent_scoped", [True, False])
def test_visual_slot_cas_completes_two_slots_and_preserves_unrelated_state(
    tmp_path: Path,
    monkeypatch,
    parent_scoped: bool,
) -> None:
    project = make_project(tmp_path / ("parent" if parent_scoped else "manual"), with_visuals=False)
    parent = (
        pipeline.start_review_preview_job(project, start_payload(), capabilities=capabilities())
        if parent_scoped
        else None
    )
    _prepare_two_slot_visual_batch(project, parent=parent)
    calls: list[str] = []

    def successful_external(project_dir: Path, candidate_state: dict, *_args, **_kwargs):
        calls.append("pexels")
        with wb._project_transaction_lock(project_dir):
            latest = wb._load_for_write(project_dir)
            latest["concurrent_visual_marker"] = {"revision": len(calls)}
            wb._save(project_dir, latest)
        wb._save(project_dir, candidate_state)
        media = project_dir / "assets" / "video" / "isolated" / f"slot-{len(calls)}.mp4"
        media.parent.mkdir(parents=True, exist_ok=True)
        media.write_bytes(f"slot-{len(calls)}".encode("ascii"))
        return (
            SimpleNamespace(
                success=True,
                data={
                    "video_id": f"video-{len(calls)}",
                    "width": 1080,
                    "height": 1920,
                    "duration_seconds": 1.0,
                },
                error=None,
            ),
            media.relative_to(project_dir).as_posix(),
            {"status": "accepted"},
        )

    monkeypatch.setattr(wb, "_find_screened_pexels_candidate", successful_external)
    monkeypatch.setattr(wb, "_probe_duration_seconds", lambda *_args: 1.0)
    kwargs = {}
    if parent is not None:
        kwargs = {
            "expected_parent_job_id": parent["job_id"],
            "expected_worker_token": "visual-cas-worker",
            "expected_request_fingerprint": parent["request_fingerprint"],
            "expected_contract_versions": pipeline._current_contract_versions(),
        }
    completed = wb.generate_visual_batch(
        project,
        expected_job_id="visual-cas-child",
        **kwargs,
    )
    assert calls == ["pexels", "pexels"]
    assert completed["concurrent_visual_marker"] == {"revision": 2}
    batch = completed["automation"]["visual_batch"]
    assert batch["status"] == "completed"
    assert batch["completed_slots"] == 2
    assert all(item["status"] == "completed" for item in batch["items"])
    assert all("worker_claim_id" not in item for item in batch["items"])
    assert len(
        [
            asset
            for asset in completed["assets"]
            if (asset.get("provenance") or {}).get("provider") == "Pexels"
        ]
    ) == 2


def _parent_visual_generate_kwargs(parent: dict) -> dict:
    return {
        "expected_job_id": "visual-cas-child",
        "expected_parent_job_id": parent["job_id"],
        "expected_worker_token": "visual-cas-worker",
        "expected_request_fingerprint": parent["request_fingerprint"],
        "expected_contract_versions": pipeline._current_contract_versions(),
    }


def _replace_visual_owner_during_provider_call(
    project: Path,
    *,
    replacement: str,
) -> None:
    with wb._project_transaction_lock(project):
        latest = wb._load_for_write(project)
        latest["provider_guard_marker"] = f"kept-{replacement}"
        if replacement == "parent":
            latest["automation"]["review_preview_pipeline"].update(
                {"job_id": "RPP-provider-replacement", "worker_token": "new-provider-worker"}
            )
        else:
            latest["automation"]["visual_batch"].update(
                {"job_id": "visual-provider-replacement", "status": "queued", "current": None}
            )
        wb._save(project, latest)


@pytest.mark.parametrize("replacement", ["parent", "child"])
def test_real_screened_helper_stops_after_first_empty_execute_when_owner_changes(
    tmp_path: Path,
    monkeypatch,
    replacement: str,
) -> None:
    project = make_project(tmp_path / replacement, with_visuals=False)
    parent = pipeline.start_review_preview_job(project, start_payload(), capabilities=capabilities())
    _prepare_two_slot_visual_batch(project, parent=parent)
    entered = threading.Event()
    release = threading.Event()
    calls: list[str] = []

    class BlockingEmptyTool:
        def execute(self, _inputs: dict) -> SimpleNamespace:
            calls.append("execute")
            entered.set()
            assert release.wait(3)
            return SimpleNamespace(success=False, artifacts=[], data={}, error="empty")

    monkeypatch.setattr(wb, "PexelsVideo", BlockingEmptyTool)
    monkeypatch.setattr(wb, "PexelsImage", BlockingEmptyTool)
    errors: list[Exception] = []

    def guarded_target() -> None:
        try:
            wb.generate_visual_batch(project, **_parent_visual_generate_kwargs(parent))
        except Exception as exc:
            errors.append(exc)

    thread = threading.Thread(target=guarded_target)
    thread.start()
    assert entered.wait(2)
    _replace_visual_owner_during_provider_call(project, replacement=replacement)
    release.set()
    thread.join(5)
    assert not thread.is_alive()
    assert calls == ["execute"]
    assert len(errors) == 1 and isinstance(errors[0], wb.WorkbenchError)
    latest = wb.read_workbench(project)
    assert latest["provider_guard_marker"] == f"kept-{replacement}"
    assert all((asset.get("provenance") or {}).get("provider") != "Pexels" for asset in latest["assets"])


def test_real_screened_helper_legacy_batch_uses_expected_job_guard_between_executes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project = make_project(tmp_path, with_visuals=False)
    _prepare_two_slot_visual_batch(project, parent=None)
    entered = threading.Event()
    release = threading.Event()
    calls: list[str] = []

    class BlockingEmptyTool:
        def execute(self, _inputs: dict) -> SimpleNamespace:
            calls.append("execute")
            entered.set()
            assert release.wait(3)
            return SimpleNamespace(success=False, artifacts=[], data={}, error="empty")

    monkeypatch.setattr(wb, "PexelsVideo", BlockingEmptyTool)
    monkeypatch.setattr(wb, "PexelsImage", BlockingEmptyTool)
    errors: list[Exception] = []

    def generate() -> None:
        try:
            wb.generate_visual_batch(project, expected_job_id="visual-cas-child")
        except Exception as exc:
            errors.append(exc)

    thread = threading.Thread(target=generate)
    thread.start()
    assert entered.wait(2)
    _replace_visual_owner_during_provider_call(project, replacement="child")
    release.set()
    thread.join(5)
    assert calls == ["execute"]
    assert len(errors) == 1 and isinstance(errors[0], wb.WorkbenchError)
    latest = wb.read_workbench(project)
    assert latest["automation"]["visual_batch"]["job_id"] == "visual-provider-replacement"
    assert latest["provider_guard_marker"] == "kept-child"


def test_real_autonomous_helper_stops_after_first_empty_search_when_child_changes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project = make_project(tmp_path, with_visuals=False)
    parent = pipeline.start_review_preview_job(project, start_payload(), capabilities=capabilities())
    _prepare_two_slot_visual_batch(project, parent=parent)
    state = wb._load_for_write(project)
    state["automation"]["visual_batch"]["planning_mode"] = "ai_director"
    for item in state["automation"]["visual_batch"]["items"]:
        item["planning_mode"] = "ai_director"
    wb._save(project, state)
    entered = threading.Event()
    release = threading.Event()
    calls: list[str] = []

    class BlockingSource:
        def is_available(self) -> bool:
            return True

        def search(self, _query, _filters):
            calls.append("search")
            entered.set()
            assert release.wait(3)
            return []

        def download(self, *_args):
            pytest.fail("stale search must not reach download")

    monkeypatch.setattr(wb, "PexelsSource", BlockingSource)
    errors: list[Exception] = []

    def generate() -> None:
        try:
            wb.generate_visual_batch(project, **_parent_visual_generate_kwargs(parent))
        except Exception as exc:
            errors.append(exc)

    thread = threading.Thread(target=generate)
    thread.start()
    assert entered.wait(2)
    _replace_visual_owner_during_provider_call(project, replacement="child")
    release.set()
    thread.join(5)
    assert calls == ["search"]
    assert len(errors) == 1 and isinstance(errors[0], wb.WorkbenchError)
    latest = wb.read_workbench(project)
    assert latest["provider_guard_marker"] == "kept-child"
    assert latest["automation"]["visual_batch"]["job_id"] == "visual-provider-replacement"


@pytest.mark.parametrize("replacement_timing", ["before_download", "during_download"])
def test_real_autonomous_helper_guards_before_and_after_download(
    tmp_path: Path,
    monkeypatch,
    replacement_timing: str,
) -> None:
    project = make_project(tmp_path / replacement_timing, with_visuals=False)
    parent = pipeline.start_review_preview_job(project, start_payload(), capabilities=capabilities())
    _prepare_two_slot_visual_batch(project, parent=parent)
    state = wb._load_for_write(project)
    state["automation"]["visual_batch"]["planning_mode"] = "ai_director"
    for item in state["automation"]["visual_batch"]["items"]:
        item["planning_mode"] = "ai_director"
    wb._save(project, state)
    candidate = SimpleNamespace(
        source="pexels",
        source_id="candidate-1",
        kind="video",
        width=1080,
        height=1920,
        duration=3.0,
        source_url="https://example.invalid/candidate-1",
        thumbnail_url="",
        source_tags="technology",
        license="test",
        extra={},
    )
    entered = threading.Event()
    release = threading.Event()
    search_calls: list[str] = []
    download_calls: list[str] = []
    isolated_paths: list[Path] = []

    class BlockingSource:
        def is_available(self) -> bool:
            return True

        def search(self, _query, _filters):
            search_calls.append("search")
            if replacement_timing == "before_download":
                entered.set()
                assert release.wait(3)
            return [candidate]

        def download(self, _candidate, output: Path) -> None:
            download_calls.append("download")
            isolated_paths.append(output)
            if replacement_timing == "during_download":
                entered.set()
                assert release.wait(3)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b"isolated-download")

    monkeypatch.setattr(wb, "PexelsSource", BlockingSource)
    monkeypatch.setattr(wb, "prepare_candidates", lambda values, **_kwargs: list(values))
    monkeypatch.setattr(
        wb,
        "decide_candidate",
        lambda *_args, **_kwargs: SimpleNamespace(
            candidate=candidate,
            decision="accept",
            ledger={"reason": "mock", "weighted_score": 90.0},
            retry_queries=(),
        ),
    )
    errors: list[Exception] = []

    def generate() -> None:
        try:
            wb.generate_visual_batch(project, **_parent_visual_generate_kwargs(parent))
        except Exception as exc:
            errors.append(exc)

    thread = threading.Thread(target=generate)
    thread.start()
    assert entered.wait(2)
    _replace_visual_owner_during_provider_call(project, replacement="child")
    release.set()
    thread.join(5)
    assert search_calls == ["search"]
    assert download_calls == ([] if replacement_timing == "before_download" else ["download"])
    assert len(errors) == 1 and isinstance(errors[0], wb.WorkbenchError)
    latest = wb.read_workbench(project)
    assert latest["provider_guard_marker"] == "kept-child"
    assert all(
        asset.get("path") not in {path.relative_to(project).as_posix() for path in isolated_paths}
        for asset in latest["assets"]
    )


def test_real_official_image_fetch_is_guarded_before_fallback_provider(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import requests

    project = make_project(tmp_path, with_visuals=False)
    parent = pipeline.start_review_preview_job(project, start_payload(), capabilities=capabilities())
    _prepare_two_slot_visual_batch(project, parent=parent)
    state = wb._load_for_write(project)
    state["automation"]["visual_batch"]["planning_mode"] = "ai_director"
    first_item = state["automation"]["visual_batch"]["items"][0]
    first_item.update({"planning_mode": "ai_director", "media_kind": "image"})
    first_scene = state["scenes"][0]
    first_scene["official_image_url"] = "https://example.invalid/official.jpg"
    wb._save(project, state)
    entered = threading.Event()
    release = threading.Event()
    calls: list[str] = []

    def blocking_get(*_args, **_kwargs):
        calls.append("official_get")
        entered.set()
        assert release.wait(3)
        return SimpleNamespace(content=b"official", raise_for_status=lambda: None)

    monkeypatch.setattr(requests, "get", blocking_get)
    monkeypatch.setattr(wb, "PexelsSource", lambda: pytest.fail("stale official fetch must not fall back"))
    errors: list[Exception] = []

    def generate() -> None:
        try:
            wb.generate_visual_batch(project, **_parent_visual_generate_kwargs(parent))
        except Exception as exc:
            errors.append(exc)

    thread = threading.Thread(target=generate)
    thread.start()
    assert entered.wait(2)
    _replace_visual_owner_during_provider_call(project, replacement="parent")
    release.set()
    thread.join(5)
    assert calls == ["official_get"]
    assert len(errors) == 1 and isinstance(errors[0], wb.WorkbenchError)
    latest = wb.read_workbench(project)
    assert latest["provider_guard_marker"] == "kept-parent"
    assert not list((project / "assets" / "images" / "official_press").glob("*.jpg"))
