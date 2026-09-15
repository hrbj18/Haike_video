from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import time

import pytest
from fastapi.testclient import TestClient

from backlot import material_interaction_review as review_mod
from backlot import material_interaction_candidates as candidate_mod
from backlot import material_interaction_second_pass_candidates as second_candidate_mod
from backlot import material_interactions
from backlot import server, state as state_module
from backlot import workbench as wb


def index_fixture() -> dict:
    def event(event_id, group, start, end):
        return {
            "event_id": event_id, "group_id": group, "participants": "三位穿浅色衣服的阿姨",
            "summary": f"互动 {event_id}", "start": start, "end": end, "confidence": .8,
            "score": .75, "completeness": "complete", "requires_review": False,
            "recommend_reason": "交流过程清楚", "evidence_frame_ids": [f"F-{event_id}"],
            "utterance_ids": [f"U-{event_id}"], "highlights": [], "unknowns": [],
        }
    return {
        "version": material_interactions.VERSION, "signature": "index-signature", "status": "completed",
        "source": {"fingerprint": "source-fingerprint"}, "duration": 90.0,
        "profile": "efficient", "identity": {"model": "test"},
        "audio": {"status": "available", "utterances": []},
        "events": [event("E01", "G01", 5, 12), event("E02", "G01", 18, 25), event("E03", "G02", 40, 48)],
        "ranked_event_ids": ["E01", "E02", "E03"], "usage": {"model_calls": 1}, "notice": "仅候选",
    }


def apply(review, index, action, revision=None, **payload):
    return review_mod.apply_action(review, index, action, payload, review["revision"] if revision is None else revision)


def test_review_is_separate_and_raw_index_bytes_never_change(tmp_path):
    index = index_fixture()
    raw_path = tmp_path / "material-interaction-index.json"
    raw_path.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    before = hashlib.sha256(raw_path.read_bytes()).hexdigest()
    review_path = tmp_path / "material-interaction-review.json"
    review = review_mod.initialize_review(index)
    assert {item["status"] for item in review["events"]} == {"pending"}
    review = apply(review, index, "set_status", event_id="R0001", status="kept")
    review_mod.write_review(review_path, review, index)
    assert hashlib.sha256(raw_path.read_bytes()).hexdigest() == before
    assert review_path.is_file() and review["revision"] == 1


@pytest.mark.parametrize("start,end", [("nan", 5), (0, "inf"), (-1, 2), (1, 99), (8, 7), (1, 1.39)])
def test_range_validation_is_finite_bounded_and_minimum(start, end):
    index, review = index_fixture(), review_mod.initialize_review(index_fixture())
    before = deepcopy(review)
    with pytest.raises(review_mod.InteractionReviewError):
        apply(review, index, "set_range", event_id="R0001", start=start, end=end)
    assert review == before


def test_revision_conflict_rejects_stale_write():
    index, review = index_fixture(), review_mod.initialize_review(index_fixture())
    review = apply(review, index, "set_status", event_id="R0001", status="kept")
    with pytest.raises(review_mod.InteractionReviewConflict):
        apply(review, index, "set_status", revision=0, event_id="R0002", status="kept")


def test_merge_only_same_group_with_bounded_non_overlapping_gap():
    index = index_fixture()
    review = apply(review_mod.initialize_review(index), index, "merge", event_ids=["R0001", "R0002"])
    merged = next(item for item in review["events"] if item["review_event_id"] == "R0004")
    assert merged["start"] == 5 and merged["end"] == 25 and merged["source_event_ids"] == ["E01", "E02"]
    with pytest.raises(review_mod.InteractionReviewError, match="同一群组"):
        apply(review, index, "merge", event_ids=["R0004", "R0003"])
    far_index = index_fixture()
    far_index["events"][1]["start"], far_index["events"][1]["end"] = 50, 60
    far_index["events"][2]["start"], far_index["events"][2]["end"] = 70, 80
    far_review = review_mod.initialize_review(far_index)
    with pytest.raises(review_mod.InteractionReviewError, match="30 秒"):
        apply(far_review, far_index, "merge", event_ids=["R0001", "R0002"])


def test_split_and_undo_restore_previous_events_with_new_revision():
    index, review = index_fixture(), review_mod.initialize_review(index_fixture())
    original = deepcopy(review["events"])
    split = apply(review, index, "split", event_id="R0001", split_seconds=8)
    assert [item["review_event_id"] for item in split["events"][:2]] == ["R0004", "R0005"]
    assert [(item["start"], item["end"]) for item in split["events"][:2]] == [(5, 8), (8, 12)]
    undone = apply(split, index, "undo")
    assert undone["events"] == original and undone["revision"] == 2
    empty = review_mod.initialize_review(index)
    with pytest.raises(review_mod.InteractionReviewError, match="没有可以撤销"):
        apply(empty, index, "undo")


def test_confirmed_catalog_contains_only_kept_sorted_events():
    index, review = index_fixture(), review_mod.initialize_review(index_fixture())
    review = apply(review, index, "set_status", event_id="R0002", status="kept")
    review = apply(review, index, "set_status", event_id="R0001", status="discarded")
    catalog = review_mod.confirmed_catalog(review, index)
    assert [item["review_event_id"] for item in catalog["events"]] == ["R0002"]
    assert "尚未裁切" in catalog["notice"]


@pytest.mark.parametrize("mutation", ["signature", "fingerprint"])
def test_review_invalidates_when_index_or_source_changes(mutation):
    index, review = index_fixture(), review_mod.initialize_review(index_fixture())
    changed = deepcopy(index)
    if mutation == "signature":
        changed["signature"] = "changed"
    else:
        changed["source"]["fingerprint"] = "changed"
    with pytest.raises(review_mod.InteractionReviewError, match="已经变化"):
        review_mod.validate_review(review, changed)


def test_corrupt_undo_snapshot_is_rejected_before_action():
    index, review = index_fixture(), review_mod.initialize_review(index_fixture())
    review["history"] = [{"events": "not-a-list", "next_event_sequence": 1}]
    with pytest.raises(review_mod.InteractionReviewError, match="撤销快照"):
        review_mod.validate_review(review, index)


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    project = root / "review-test"
    (project / "assets").mkdir(parents=True)
    (project / "project.json").write_text(json.dumps({"project_id": project.name, "title": "审核测试", "pipeline_type": "cinematic"}), encoding="utf-8")
    source = project / "assets" / "source.mp4"
    source.write_bytes(b"source")
    state = wb.bootstrap_workbench(project)
    asset = wb._append_asset(project, state, {"name": "原片", "type": "video", "source_type": "human_provided", "path": "assets/source.mp4", "duration_seconds": 90})
    output = project / "artifacts" / "media-index" / asset["id"] / "interaction-v1" / "test"
    output.mkdir(parents=True)
    index = index_fixture()
    index["source"]["fingerprint"] = material_interactions.media_content_fingerprint(source)
    index_path = output / "material-interaction-index.json"
    index_path.write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")
    asset["media_index"] = {"interaction_index_path": index_path.relative_to(project).as_posix(), "status": "completed"}
    wb._save(project, state)
    monkeypatch.setattr(server, "PROJECTS_DIR", root)
    monkeypatch.setattr(state_module, "PROJECTS_DIR", root)
    monkeypatch.setattr(server, "_summary_cache", {})
    return project, asset["id"], index_path


def test_workbench_and_api_initialize_update_and_return_only_safe_paths(project):
    project_dir, asset_id, index_path = project
    raw = index_path.read_bytes()
    initialized = wb.initialize_asset_material_interaction_review(project_dir, asset_id)
    assert initialized["review"]["revision"] == 0 and initialized["playback_video_path"] == "assets/source.mp4"
    updated = wb.update_asset_material_interaction_review(project_dir, asset_id, {
        "action": "set_status", "event_id": "R0001", "status": "kept", "expected_revision": 0,
    })
    assert updated["review"]["confirmed_catalog"]["events"][0]["review_event_id"] == "R0001"
    assert index_path.read_bytes() == raw
    serialized = json.dumps(updated, ensure_ascii=False)
    assert str(project_dir) not in serialized and "material-interaction-index.json" not in serialized
    with TestClient(server.create_app()) as client:
        response = client.post(f"/api/project/{project_dir.name}/workbench/assets/{asset_id}/media-index/interactions/review", json={
            "action": "set_range", "event_id": "R0001", "start": 6, "end": 13, "expected_revision": 0,
        })
        assert response.status_code == 409
        response = client.post(f"/api/project/{project_dir.name}/workbench/assets/{asset_id}/media-index/interactions/review", json={
            "action": "set_range", "event_id": "R0001", "start": 6, "end": 13, "expected_revision": 1,
        })
        assert response.status_code == 200 and response.json()["review"]["revision"] == 2


def test_candidate_api_renders_then_requires_explicit_approval_before_registering_derived_asset(project, monkeypatch):
    project_dir, asset_id, _ = project
    initialized = wb.initialize_asset_material_interaction_review(project_dir, asset_id)
    kept = wb.update_asset_material_interaction_review(project_dir, asset_id, {
        "action": "set_status", "event_id": "R0001", "status": "kept", "expected_revision": 0,
    })
    assert kept["review"]["revision"] == 1
    monkeypatch.setattr(wb, "_ffmpeg_available", lambda: "ffmpeg")
    monkeypatch.setattr(wb, "_ffprobe_available", lambda _ffmpeg: "ffprobe")

    def fake_render(_source, plan, output_dir, **_kwargs):
        path = output_dir / plan["plan_id"] / "preview.mp4"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"browser-preview")
        return {
            "plan_id": plan["plan_id"], "plan_revision": plan["revision"],
            "signature": "preview-signature", "path": str(path),
            "output_duration": plan["output_duration"],
            "qa": {"status": "passed", "checks": {"duration": True, "video_codec": True}},
        }

    monkeypatch.setattr(candidate_mod, "render_interaction_candidate", fake_render)
    with TestClient(server.create_app()) as client:
        generated = client.post(
            f"/api/project/{project_dir.name}/workbench/assets/{asset_id}/media-index/interactions/candidates",
            json={"event_id": "R0001", "expected_review_revision": 1},
        )
        assert generated.status_code == 200, generated.text
        assert generated.json()["candidate_job"]["status"] in {"queued", "generating"}
        result = None
        for _ in range(100):
            result = client.get(
                f"/api/project/{project_dir.name}/workbench/assets/{asset_id}/media-index/interactions"
            ).json()
            if result["candidate_job"]["status"] in {"completed", "failed"}:
                break
            time.sleep(.01)
        assert result["candidate_job"]["status"] == "completed", result["candidate_job"]
        candidate = result["candidates"][0]
        assert candidate["status"] == "pending_review"
        assert candidate["preview"]["path"].endswith("preview.mp4")
        before = wb.read_workbench(project_dir)
        assert not any((row.get("generation") or {}).get("kind") in {"interaction_rough_cut", "interaction_fine_cut"} for row in before["assets"])

        preview_path = project_dir / candidate["preview"]["path"]
        preview_path.unlink()
        missing_preview = client.post(
            f"/api/project/{project_dir.name}/workbench/assets/{asset_id}/media-index/interactions/candidates/{candidate['plan_id']}",
            json={"action": "approve", "expected_revision": candidate["revision"]},
        )
        assert missing_preview.status_code == 422
        still_pending = wb.read_asset_material_interactions(project_dir, asset_id)["candidates"][0]
        assert still_pending["status"] == "pending_review" and still_pending["revision"] == candidate["revision"]
        preview_path.write_bytes(b"browser-preview")
        monkeypatch.setattr(wb, "_ffmpeg_available", lambda: pytest.fail("approval must not require FFmpeg"))

        approved = client.post(
            f"/api/project/{project_dir.name}/workbench/assets/{asset_id}/media-index/interactions/candidates/{candidate['plan_id']}",
            json={"action": "approve", "expected_revision": candidate["revision"]},
        )
        assert approved.status_code == 200
        final_candidate = approved.json()["candidates"][0]
        assert final_candidate["status"] == "approved"
        assert final_candidate["approved_asset_id"]

    after = wb.read_workbench(project_dir)
    derived = next(row for row in after["assets"] if row["id"] == final_candidate["approved_asset_id"])
    assert derived["source_type"] == "local_generated"
    assert derived["generation"]["parent_asset_id"] == asset_id
    assert derived["generation"]["analysis_excluded"] is True
    assert derived["generation"]["kind"] == "interaction_fine_cut"


def test_candidate_job_is_idempotent_while_active_and_records_terminal_result(project, monkeypatch):
    project_dir, asset_id, _ = project
    wb.initialize_asset_material_interaction_review(project_dir, asset_id)
    request = {"operation": "generate", "event_id": "R0001", "expected_review_revision": 0}

    first = wb.start_asset_material_interaction_candidate_job(project_dir, asset_id, request)
    first_job = first["automation"]["interaction_candidate"]
    duplicate = wb.start_asset_material_interaction_candidate_job(project_dir, asset_id, request)
    assert duplicate["automation"]["interaction_candidate"]["job_id"] == first_job["job_id"]
    with pytest.raises(wb.WorkbenchError, match="已有互动候选"):
        wb.start_asset_material_interaction_candidate_job(
            project_dir, asset_id,
            {"operation": "generate", "event_id": "R0002", "expected_review_revision": 0},
        )

    calls = []

    def fake_generate(_project_dir, _asset_id, payload):
        calls.append(dict(payload))
        return {"candidates": [{"event_id": "R0001", "plan_id": "P001", "revision": 3}]}

    monkeypatch.setattr(wb, "generate_asset_material_interaction_candidate", fake_generate)
    wb.run_asset_material_interaction_candidate_job(project_dir, first_job["job_id"])
    completed = wb.read_workbench(project_dir)["automation"]["interaction_candidate"]
    assert completed["status"] == "completed"
    assert completed["attempts"] == 1
    assert completed["result"] == {"plan_id": "P001", "revision": 3}
    wb.run_asset_material_interaction_candidate_job(project_dir, first_job["job_id"])
    assert len(calls) == 1


def test_candidate_job_failure_is_visible_and_generating_state_can_resume(project, monkeypatch):
    project_dir, asset_id, _ = project
    wb.initialize_asset_material_interaction_review(project_dir, asset_id)
    queued = wb.start_asset_material_interaction_candidate_job(
        project_dir, asset_id,
        {"operation": "generate", "event_id": "R0001", "expected_review_revision": 0},
    )
    job_id = queued["automation"]["interaction_candidate"]["job_id"]
    claimed = wb._claim_interaction_candidate_job(project_dir, job_id)
    assert claimed["status"] == "generating" and claimed["attempts"] == 1

    def fail_generate(*_args, **_kwargs):
        raise wb.WorkbenchError("受控渲染失败")

    monkeypatch.setattr(wb, "generate_asset_material_interaction_candidate", fail_generate)
    wb.run_asset_material_interaction_candidate_job(project_dir, job_id)
    failed = wb.read_workbench(project_dir)["automation"]["interaction_candidate"]
    assert failed["status"] == "failed"
    assert failed["attempts"] == 1
    assert "受控渲染失败" in failed["error"]
    assert failed["safe_resume_point"] == "candidate_job_failed"


def _prepare_second_pass_parent(project, monkeypatch):
    project_dir, asset_id, index_path = project
    index = json.loads(index_path.read_text(encoding="utf-8"))
    index["audio"]["utterances"] = [
        {"id": "U1", "start": 5.2, "end": 5.8, "text": "姐姐们好。"},
        {"id": "U2", "start": 6.0, "end": 7.2, "text": "我们拍一张吧。"},
        {"id": "U3", "start": 7.4, "end": 8.5, "text": "三二一，茄子。"},
        {"id": "U4", "start": 10.0, "end": 11.0, "text": "拜拜。"},
    ]
    index_path.write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")
    wb.initialize_asset_material_interaction_review(project_dir, asset_id)
    monkeypatch.setattr(wb, "_ffmpeg_available", lambda: "ffmpeg")
    monkeypatch.setattr(wb, "_ffprobe_available", lambda _ffmpeg: "ffprobe")

    def fake_first_render(_source, plan, output_dir, **_kwargs):
        path = output_dir / plan["plan_id"] / "first-preview.mp4"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"first-preview")
        return {
            "plan_id": plan["plan_id"], "plan_revision": plan["revision"],
            "signature": "first-signature", "path": str(path),
            "output_duration": plan["output_duration"],
            "qa": {"status": "passed", "checks": {"duration": True}},
        }

    monkeypatch.setattr(candidate_mod, "render_interaction_candidate", fake_first_render)
    parent_data = wb.generate_asset_material_interaction_candidate(
        project_dir, asset_id, {"event_id": "R0001", "expected_review_revision": 0},
    )
    return project_dir, asset_id, parent_data["candidates"][0]


def test_second_pass_api_requires_one_confirmation_then_supports_review_and_approval(project, monkeypatch):
    project_dir, asset_id, parent = _prepare_second_pass_parent(project, monkeypatch)
    monkeypatch.setattr(wb, "_second_pass_preflight", lambda: {
        "configured": True, "provider": "default", "model": "configured-model",
        "endpoint_hash": "endpoint-hash", "maximum_model_calls": 1,
        "confirmation_required": True, "message": "一次确认",
    })
    raw = {
        "summary": "删除寒暄和告别，保留完整合照",
        "groups": [
            {"id": "G1", "type": "greeting", "utterance_ids": ["U1"], "decision": "drop", "reason": "独立寒暄", "depends_on": [], "hook_eligible": False, "hook_score": 0},
            {"id": "G2", "type": "action", "utterance_ids": ["U2", "U3"], "decision": "keep", "reason": "完整合照", "depends_on": [], "hook_eligible": True, "hook_score": 1},
            {"id": "G3", "type": "farewell", "utterance_ids": ["U4"], "decision": "drop", "reason": "独立告别", "depends_on": [], "hook_eligible": False, "hook_score": 0},
        ],
        "hook_candidates": [{"id": "H1", "group_ids": ["G2"], "reason": "动作完整"}],
    }
    counters = {"analysis": 0, "render": 0}

    def analyze(_context):
        counters["analysis"] += 1
        return raw, "configured-model"

    def fake_second_render(_source, plan, output_dir, **_kwargs):
        counters["render"] += 1
        path = output_dir / plan["plan_id"] / f"second-r{plan['revision']}.mp4"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"second-preview")
        return {
            "plan_id": plan["plan_id"], "plan_revision": plan["revision"],
            "signature": f"second-signature-{plan['revision']}", "path": str(path),
            "output_duration": plan["output_duration"],
            "qa": {"status": "passed", "checks": {"duration": True, "audio_video_tail": True}},
        }

    monkeypatch.setattr(second_candidate_mod, "render_second_pass_candidate", fake_second_render)
    real_generate = second_candidate_mod.generate_second_pass_candidate

    def generate_with_fixture(**kwargs):
        return real_generate(**kwargs, analyze=analyze)

    monkeypatch.setattr(wb, "generate_second_pass_candidate", generate_with_fixture)
    with TestClient(server.create_app()) as client:
        url = f"/api/project/{project_dir.name}/workbench/assets/{asset_id}/media-index/interactions/second-pass"
        missing = client.post(url, json={
            "parent_plan_id": parent["plan_id"], "expected_parent_revision": parent["revision"],
        })
        assert missing.status_code == 422 and "确认" in missing.text
        queued = client.post(url, json={
            "parent_plan_id": parent["plan_id"], "expected_parent_revision": parent["revision"],
            "confirmed": True, "options": {"speed": 1.1, "target_min_seconds": 15, "target_max_seconds": 60},
        })
        assert queued.status_code == 200, queued.text
        data = None
        for _ in range(100):
            data = client.get(
                f"/api/project/{project_dir.name}/workbench/assets/{asset_id}/media-index/interactions"
            ).json()
            if data["second_pass_job"]["status"] in {"completed", "failed", "ambiguous"}:
                break
            time.sleep(.01)
        assert data["second_pass_job"]["status"] == "completed", data["second_pass_job"]
        second = data["second_pass_candidates"][0]
        assert second["status"] == "pending_review" and second["parent_current"] is True
        # G3 holds "拜拜。", the clip's closing word, so the deterministic edge
        # anchor keeps it even though the model called it disposable farewell —
        # the clip has to end *on* the farewell, not before it.
        assert [row["id"] for row in second["story"]["groups"] if row["selected"]] == ["G2", "G3"]
        anchored = next(row for row in second["story"]["groups"] if row["id"] == "G3")
        assert "锚定" in anchored["reason"]
        assert second["story_identity"]["confirmed"] is True
        assert second["story_identity"]["maximum_model_calls"] == 1
        assert counters == {"analysis": 1, "render": 1}
        assert not any((row.get("generation") or {}).get("kind") == "interaction_second_pass"
                       for row in wb.read_workbench(project_dir)["assets"])

        approved = client.post(f"{url}/{second['plan_id']}", json={
            "action": "approve", "expected_revision": second["revision"],
        })
        assert approved.status_code == 200, approved.text
        approved_plan = approved.json()["second_pass_candidates"][0]
        assert approved_plan["status"] == "approved" and approved_plan["approved_asset_id"]
    derived = next(row for row in wb.read_workbench(project_dir)["assets"]
                   if row["id"] == approved_plan["approved_asset_id"])
    assert derived["generation"]["kind"] == "interaction_second_pass"
    assert derived["generation"]["parent_plan_id"] == parent["plan_id"]
    assert derived["generation"]["analysis_excluded"] is True


def test_second_pass_job_marks_ambiguous_without_automatic_repeat(project, monkeypatch):
    project_dir, asset_id, parent = _prepare_second_pass_parent(project, monkeypatch)
    monkeypatch.setattr(wb, "_second_pass_preflight", lambda: {
        "configured": True, "provider": "default", "model": "configured-model",
        "endpoint_hash": "endpoint", "maximum_model_calls": 1,
        "confirmation_required": True, "message": "一次确认",
    })
    queued = wb.start_asset_material_interaction_second_pass_job(project_dir, asset_id, {
        "operation": "generate", "parent_plan_id": parent["plan_id"],
        "expected_parent_revision": parent["revision"], "confirmed": True,
    })
    job_id = queued["automation"]["interaction_second_pass"]["job_id"]
    calls = []

    def ambiguous(*_args, **_kwargs):
        calls.append(True)
        raise wb.WorkbenchError("内容精选请求受理状态待核对，已阻止重复计费请求")

    monkeypatch.setattr(wb, "generate_asset_material_interaction_second_pass", ambiguous)
    wb.run_asset_material_interaction_second_pass_job(project_dir, job_id)
    state = wb.read_workbench(project_dir)["automation"]["interaction_second_pass"]
    assert state["status"] == "ambiguous"
    assert state["safe_resume_point"] == "verify_story_request_acceptance"
    wb.run_asset_material_interaction_second_pass_job(project_dir, job_id)
    assert len(calls) == 1


def test_ui_contract_exposes_all_review_actions_and_proxy_playback():
    root = Path(__file__).parents[2]
    js = (root / "backlot/ui/workbench.js").read_text(encoding="utf-8")
    css = (root / "backlot/ui/workbench.css").read_text(encoding="utf-8")
    for action in ("set_range", "set_status", "merge", "split", "undo"):
        assert f'updateMaterialInteractionReview("{action}"' in js
    assert "expected_revision: data.review.revision" in js
    assert "data.playback_video_path || data.source_video_path" in js
    assert "尚未裁切媒体，也尚未采用" not in js  # server-derived notice is displayed rather than duplicated
    assert ".interaction-review-toolbar" in css


def test_ui_contract_exposes_human_triggered_second_pass_and_occurrence_mapping():
    root = Path(__file__).parents[2]
    js = (root / "backlot/ui/workbench.js").read_text(encoding="utf-8")
    css = (root / "backlot/ui/workbench.css").read_text(encoding="utf-8")
    assert "二次剪辑（人工触发）" in js
    assert "最多调用 1 次" in js
    assert "sourceTimeToSecondPassOutputs" in js
    assert "group_states: draft.groupStates" in js
    assert "locked_states: draft.lockedStates" in js
    assert "hook_mode: draft.hookMode" in js
    assert "移动到片头（正文不重复）" in js
    assert "candidate.content_qa?.status" in js
    assert "candidate.is_active !== true" in js
    assert "已定位到对应的二次剪辑方案" in js
    assert "保存调整并更新一次预览" in js
    assert "不会自动批准、采用或发布" in js
    assert ".interaction-second-pass-comparison" in css
    assert ".interaction-story-group.is-dropped" in css
    assert ".interaction-second-pass-card.is-located" in css


def test_workbench_html_carries_runtime_revision_contract():
    response = server._ui_html("workbench.html", ("workbench.css", "navigation.css", "workbench.js"))
    body = bytes(response.body).decode("utf-8")
    assert "window.__BACKLOT_UI_REVISION__" in body
    assert "/ui/navigation.css?v=" in body


def test_real_ffmpeg_browser_proxy_contract_and_cache(tmp_path):
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        pytest.skip("FFmpeg/ffprobe unavailable")
    source = tmp_path / "source.avi"
    created = subprocess.run([
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "testsrc=size=321x241:rate=25:duration=1.2",
        "-f", "lavfi", "-i", "sine=frequency=700:sample_rate=48000:duration=1.2",
        "-c:v", "mpeg4", "-c:a", "pcm_s16le", str(source),
    ], capture_output=True, timeout=60)
    if created.returncode != 0:
        pytest.skip("Current FFmpeg cannot create the non-browser fixture")
    original = source.read_bytes()
    fingerprint = material_interactions.media_content_fingerprint(source)
    first = review_mod.build_browser_proxy(source, tmp_path / "out", ffmpeg=ffmpeg, ffprobe=ffprobe, source_fingerprint=fingerprint)
    second = review_mod.build_browser_proxy(source, tmp_path / "out", ffmpeg=ffmpeg, ffprobe=ffprobe, source_fingerprint=fingerprint)
    assert first["video_codec"] == "h264" and first["pixel_format"] == "yuv420p"
    assert first["audio_codec"] == "aac" and first["faststart"] is True
    assert max(first["width"], first["height"]) <= 1280 and first["width"] % 2 == first["height"] % 2 == 0
    assert abs(first["duration"] - 1.2) <= .25 and second["cache_hit"] is True
    assert source.read_bytes() == original
