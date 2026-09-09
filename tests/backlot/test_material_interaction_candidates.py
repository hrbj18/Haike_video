from pathlib import Path

import pytest

from backlot import material_interaction_candidates as candidates


def fixtures(project_dir: Path, *, audio: bool = True):
    source = project_dir / "assets" / "source.mp4"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"source")
    index = {
        "status": "completed", "signature": "IDX-1", "duration": 10,
        "source": {"fingerprint": "fingerprint-1"},
        "audio": {
            "policy": "doubao_transcript" if audio else "disabled",
            "status": "available" if audio else "skipped",
            "provider": "doubao-test" if audio else None,
            "utterances": [
                {"id": "U00001", "start": 1, "end": 2, "text": "你好"},
                {"id": "U00002", "start": 4, "end": 5, "text": "你好呀"},
            ],
        },
        "events": [{
            "event_id": "E1", "irrelevant_segments": [{
                "start": 6, "end": 7, "confidence": .96,
                "no_related_speech": True, "no_key_action": True,
                "context_preserved": True, "evidence_frame_ids": ["F1", "F2"],
                "reason": "无关路人",
            }],
        }],
    }
    review = {
        "index_signature": "IDX-1", "revision": 2,
        "events": [{
            "review_event_id": "R0001", "source_event_ids": ["E1"], "group_id": "G1",
            "status": "kept", "start": 0, "end": 8,
            "utterance_ids": ["U00001", "U00002"],
        }],
    }
    return source, index, review


def fake_render(source, plan, output_dir, **_kwargs):
    path = output_dir / plan["plan_id"] / "preview.mp4"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"preview")
    return {
        "plan_id": plan["plan_id"], "plan_revision": plan["revision"],
        "signature": f"render-{plan['revision']}", "path": str(path),
        "output_duration": plan["output_duration"],
        "qa": {"status": "passed", "checks": {"duration": True}},
    }


def enable_fake_vad(monkeypatch):
    monkeypatch.setattr(candidates, "_speech_activity", lambda *_a, **_k: {
        "status": "available", "identity": {"signature": "vad-test"},
        "speech_ranges": [{"start": 1, "end": 2}, {"start": 4, "end": 5}],
        "non_speech_ranges": [{"start": 2, "end": 4},
                              {"start": 5, "end": 8}],
    })
    monkeypatch.setattr(candidates, "_pause_visual_activity", lambda *_a, **_k: {
        "status": "available", "identity": {"signature": "visual-test"},
        "windows": [{"id": "PV001", "start": 2.15, "end": 3.85,
                     "stage": "waiting", "confidence": .95, "safe_to_shorten": True,
                     "evidence_frame_ids": []}],
    })


def test_generate_persists_pending_candidate_and_uses_visual_evidence(tmp_path, monkeypatch):
    source, index, review = fixtures(tmp_path)
    monkeypatch.setattr(candidates, "detect_silence", lambda *_a, **_k: [{"start": 2.1, "end": 3.9}])
    monkeypatch.setattr(candidates, "render_interaction_candidate", fake_render)
    enable_fake_vad(monkeypatch)
    root = tmp_path / "artifacts" / "interaction-candidates"
    plan = candidates.generate_candidate(
        project_dir=tmp_path, source=source, index=index, review=review,
        event_id="R0001", output_root=root, ffmpeg="ffmpeg", ffprobe="ffprobe",
    )
    assert plan["status"] == "pending_review" and plan["revision"] == 1
    assert {row["reason_code"] for row in plan["removed_ranges"]} == {
        "confirmed_silent_wait", "high_confidence_irrelevant_visual",
    }
    assert plan["preview"]["path"].startswith("artifacts/interaction-candidates/")
    assert candidates.list_candidates(root)[0]["plan_id"] == plan["plan_id"]


def test_audio_disabled_skips_silence_and_internal_deletions(tmp_path, monkeypatch):
    source, index, review = fixtures(tmp_path, audio=False)
    monkeypatch.setattr(candidates, "detect_silence", lambda *_a, **_k: pytest.fail("audio detector called"))
    monkeypatch.setattr(candidates, "render_interaction_candidate", fake_render)
    plan = candidates.generate_candidate(
        project_dir=tmp_path, source=source, index=index, review=review,
        event_id="R0001", output_root=tmp_path / "candidates", ffmpeg="f", ffprobe="p",
    )
    assert plan["removed_ranges"] == [] and plan["output_duration"] == 8


def test_terminal_candidate_is_not_overwritten_by_repeat_generation(tmp_path, monkeypatch):
    source, index, review = fixtures(tmp_path, audio=False)
    monkeypatch.setattr(candidates, "render_interaction_candidate", fake_render)
    root = tmp_path / "candidates"
    plan = candidates.generate_candidate(
        project_dir=tmp_path, source=source, index=index, review=review,
        event_id="R0001", output_root=root, ffmpeg="f", ffprobe="p",
    )
    approved = candidates.update_candidate(
        project_dir=tmp_path, source=source, output_root=root, plan_id=plan["plan_id"],
        action="approve", expected_revision=plan["revision"], ffmpeg="", ffprobe="",
    )
    monkeypatch.setattr(candidates, "render_interaction_candidate", lambda *_a, **_k: pytest.fail("terminal candidate rerendered"))
    repeated = candidates.generate_candidate(
        project_dir=tmp_path, source=source, index=index, review=review,
        event_id="R0001", output_root=root, ffmpeg="f", ffprobe="p",
    )
    assert repeated["status"] == "approved" and repeated["revision"] == approved["revision"]


def test_restore_rerenders_and_stale_revision_conflicts(tmp_path, monkeypatch):
    source, index, review = fixtures(tmp_path)
    monkeypatch.setattr(candidates, "detect_silence", lambda *_a, **_k: [{"start": 2.1, "end": 3.9}])
    monkeypatch.setattr(candidates, "render_interaction_candidate", fake_render)
    enable_fake_vad(monkeypatch)
    root = tmp_path / "candidates"
    plan = candidates.generate_candidate(
        project_dir=tmp_path, source=source, index=index, review=review,
        event_id="R0001", output_root=root, ffmpeg="f", ffprobe="p",
    )
    updated = candidates.update_candidate(
        project_dir=tmp_path, source=source, output_root=root, plan_id=plan["plan_id"],
        action="restore_removal", expected_revision=1, removal_id="D001", ffmpeg="f", ffprobe="p",
    )
    assert updated["revision"] == 3 and updated["removed_ranges"][0]["restored"] is True
    assert updated["qa"]["status"] == "passed"
    with pytest.raises(candidates.InteractionCandidateConflict):
        candidates.update_candidate(
            project_dir=tmp_path, source=source, output_root=root, plan_id=plan["plan_id"],
            action="reject", expected_revision=1, ffmpeg="f", ffprobe="p",
        )


def test_plan_id_rejects_path_traversal(tmp_path):
    with pytest.raises(candidates.InteractionCandidateError):
        candidates.update_candidate(
            project_dir=tmp_path, source=tmp_path / "x.mp4", output_root=tmp_path,
            plan_id="../outside", action="reject", expected_revision=0,
            ffmpeg="f", ffprobe="p",
        )


def test_repeat_generation_does_not_overwrite_pending_user_revision(tmp_path, monkeypatch):
    source, index, review = fixtures(tmp_path)
    monkeypatch.setattr(candidates, "detect_silence", lambda *_a, **_k: [{"start": 2.1, "end": 3.9}])
    monkeypatch.setattr(candidates, "render_interaction_candidate", fake_render)
    enable_fake_vad(monkeypatch)
    root = tmp_path / "candidates"
    first = candidates.generate_candidate(
        project_dir=tmp_path, source=source, index=index, review=review,
        event_id="R0001", output_root=root, ffmpeg="f", ffprobe="p",
    )
    changed = candidates.update_candidate(
        project_dir=tmp_path, source=source, output_root=root, plan_id=first["plan_id"],
        action="save_edits", expected_revision=first["revision"],
        removal_states={"D001": True}, ffmpeg="f", ffprobe="p",
    )
    repeated = candidates.generate_candidate(
        project_dir=tmp_path, source=source, index=index, review=review,
        event_id="R0001", output_root=root, ffmpeg="f", ffprobe="p",
    )
    assert repeated["revision"] == changed["revision"]
    assert repeated["removed_ranges"][0]["restored"] is True


def test_repeat_generation_resumes_plan_left_before_preview(tmp_path, monkeypatch):
    source, index, review = fixtures(tmp_path, audio=False)
    root = tmp_path / "candidates"
    render_calls = []

    def fail_once(*_args, **_kwargs):
        render_calls.append("failed")
        raise candidates.InteractionRenderError("受控 QA 失败")

    monkeypatch.setattr(candidates, "render_interaction_candidate", fail_once)
    with pytest.raises(candidates.InteractionCandidateError, match="受控 QA 失败"):
        candidates.generate_candidate(
            project_dir=tmp_path, source=source, index=index, review=review,
            event_id="R0001", output_root=root, ffmpeg="f", ffprobe="p",
        )
    staged = candidates.list_candidates(root)[0]
    assert staged["revision"] == 0 and staged["qa"]["status"] == "not_rendered"

    monkeypatch.setattr(candidates, "render_interaction_candidate", fake_render)
    resumed = candidates.generate_candidate(
        project_dir=tmp_path, source=source, index=index, review=review,
        event_id="R0001", output_root=root, ffmpeg="f", ffprobe="p",
    )
    assert resumed["revision"] == 1
    assert resumed["qa"]["status"] == "passed"
    assert render_calls == ["failed"]


def test_candidate_lineage_selects_one_active_plan_per_event():
    plans = [
        {"plan_id": "IEP-old", "event_id": "R0001", "version": "interaction-edit-plan-v1",
         "review_revision": 1, "created_at": "2026-09-07T00:00:00Z", "updated_at": "2026-09-07T00:00:00Z"},
        {"plan_id": "IEP-new", "event_id": "R0001", "version": "interaction-edit-plan-v2",
         "review_revision": 1, "created_at": "2026-09-08T00:00:00Z", "updated_at": "2026-09-08T00:00:00Z"},
        {"plan_id": "IEP-other", "event_id": "R0002", "version": "interaction-edit-plan-v2",
         "review_revision": 1, "created_at": "2026-09-08T00:00:00Z", "updated_at": "2026-09-08T00:00:00Z"},
    ]
    rows = candidates.classify_candidate_lineage(plans)
    active = [row["plan_id"] for row in rows if row["is_active"]]
    assert set(active) == {"IEP-new", "IEP-other"}
    old = next(row for row in rows if row["plan_id"] == "IEP-old")
    assert old["lineage_status"] == "superseded" and old["superseded_by"] == "IEP-new"
