from __future__ import annotations

from pathlib import Path

import pytest

from backlot.ai_text import TextAIError
from backlot import material_interaction_second_pass_candidates as candidates


def _fixture(project_dir: Path):
    source = project_dir / "assets" / "source.mp4"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"source")
    utterances = [
        {"id": "U1", "start": .5, "end": 1.2, "text": "姐姐们好。"},
        {"id": "U2", "start": 2.0, "end": 3.0, "text": "我们拍一张吧。"},
        {"id": "U3", "start": 3.2, "end": 5.0, "text": "三二一，茄子。"},
        {"id": "U4", "start": 6.0, "end": 7.0, "text": "太好玩了。"},
    ]
    index = {"audio": {"status": "available", "utterances": utterances}}
    parent = {
        "plan_id": "IEP-parent", "version": "interaction-edit-plan-v2", "revision": 1,
        "preview": {"signature": "parent-render"},
        "source": {"fingerprint": "source-fingerprint"}, "index_signature": "index-1",
        "review_revision": 2, "event_id": "R1", "group_id": "people-a",
        "keep_ranges": [{"start": .4, "end": 7.2}],
    }
    raw = {
        "summary": "去掉独立寒暄，保留合照和反应",
        "groups": [
            {"id": "G1", "type": "greeting", "utterance_ids": ["U1"], "decision": "drop", "reason": "独立寒暄", "depends_on": [], "hook_eligible": False, "hook_score": 0},
            {"id": "G2", "type": "action", "utterance_ids": ["U2", "U3"], "decision": "keep", "reason": "完整合照", "depends_on": [], "hook_eligible": True, "hook_score": 1},
            {"id": "G3", "type": "reaction", "utterance_ids": ["U4"], "decision": "keep", "reason": "结果反应", "depends_on": ["G2"], "hook_eligible": False, "hook_score": .5},
        ],
        "hook_candidates": [{"id": "H1", "group_ids": ["G2"], "reason": "动作完整"}],
    }
    return source, index, parent, raw


def _fake_render(counter: dict[str, int]):
    def render(source, plan, output_root, **_kwargs):
        counter["render"] = counter.get("render", 0) + 1
        path = output_root / plan["plan_id"] / f"preview-r{plan['revision']}.mp4"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"preview")
        return {
            "plan_id": plan["plan_id"], "plan_revision": plan["revision"],
            "signature": f"render-{plan['revision']}", "path": str(path),
            "output_duration": plan["output_duration"],
            "qa": {"status": "passed", "checks": {"duration": True}},
        }
    return render


def test_completed_analysis_and_render_are_reused(tmp_path, monkeypatch):
    source, index, parent, raw = _fixture(tmp_path)
    calls = {"analysis": 0, "render": 0}

    def analyze(_context):
        calls["analysis"] += 1
        return raw, "configured-model"

    monkeypatch.setattr(candidates, "render_second_pass_candidate", _fake_render(calls))
    arguments = dict(
        project_dir=tmp_path, source=source, index=index, parent_plan=parent,
        output_root=tmp_path / "artifacts" / "second-pass", options={"speed": 1.1},
        runtime_identity={"provider": "default", "model": "configured-model"},
        ffmpeg="ffmpeg", ffprobe="ffprobe", analyze=analyze,
    )
    first = candidates.generate_second_pass_candidate(**arguments)
    second = candidates.generate_second_pass_candidate(**arguments)
    assert first["qa"]["status"] == "passed"
    assert first["usage"]["semantic_model_calls"] == 1
    assert first["usage"]["analysis_cache_hit"] is False
    assert first["usage"]["render_elapsed_seconds"] >= 0
    assert second["analysis_cache_hit"] is True and second["render_cache_hit"] is True
    assert calls == {"analysis": 1, "render": 1}


def test_ambiguous_model_submit_blocks_automatic_repeat(tmp_path, monkeypatch):
    source, index, parent, _ = _fixture(tmp_path)
    calls = {"analysis": 0}

    def ambiguous(_context):
        calls["analysis"] += 1
        raise TextAIError("request timeout after submit")

    arguments = dict(
        project_dir=tmp_path, source=source, index=index, parent_plan=parent,
        output_root=tmp_path / "artifacts" / "second-pass", options={},
        runtime_identity={"provider": "default", "model": "configured-model"},
        ffmpeg="ffmpeg", ffprobe="ffprobe", analyze=ambiguous,
    )
    with pytest.raises(candidates.InteractionSecondPassCandidateError, match="待核对"):
        candidates.generate_second_pass_candidate(**arguments)
    with pytest.raises(candidates.InteractionSecondPassCandidateError, match="待核对"):
        candidates.generate_second_pass_candidate(**arguments)
    assert calls["analysis"] == 1


def test_save_edits_rerenders_without_repeating_analysis(tmp_path, monkeypatch):
    source, index, parent, raw = _fixture(tmp_path)
    calls = {"analysis": 0, "render": 0}

    def analyze(_context):
        calls["analysis"] += 1
        return raw, "configured-model"

    monkeypatch.setattr(candidates, "render_second_pass_candidate", _fake_render(calls))
    root = tmp_path / "artifacts" / "second-pass"
    plan = candidates.generate_second_pass_candidate(
        project_dir=tmp_path, source=source, index=index, parent_plan=parent,
        output_root=root, options={"speed": 1.1},
        runtime_identity={"provider": "default", "model": "configured-model"},
        ffmpeg="ffmpeg", ffprobe="ffprobe", analyze=analyze,
    )
    updated = candidates.update_second_pass_candidate(
        project_dir=tmp_path, source=source, output_root=root, plan_id=plan["plan_id"],
        action="save_edits", expected_revision=plan["revision"], group_states=None,
        locked_states=None, speed=1.25, hook_candidate_id=None,
        ffmpeg="ffmpeg", ffprobe="ffprobe",
    )
    assert updated["options"]["speed"] == 1.25
    assert updated["revision"] == plan["revision"] + 2
    assert updated["usage"]["semantic_model_calls"] == 0
    assert updated["usage"]["analysis_cache_hit"] is True
    assert calls == {"analysis": 1, "render": 2}
