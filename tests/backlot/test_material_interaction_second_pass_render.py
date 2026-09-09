from __future__ import annotations

import shutil
import subprocess

import pytest

from backlot.material_interaction_second_pass import build_second_pass_plan
from backlot.material_interaction_second_pass_render import render_second_pass_candidate
from backlot.material_interaction_story import analyze_story
from backlot.media_index import media_content_fingerprint


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="FFmpeg is unavailable")
def test_real_render_keeps_audio_video_clock_with_hook_and_speed(tmp_path):
    source = tmp_path / "source.mp4"
    subprocess.run([
        shutil.which("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=30:duration=8",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=8",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(source),
    ], check=True)
    utterances = [
        {"id": "U1", "start": .5, "end": 1.2, "text": "大家好。"},
        {"id": "U2", "start": 2.0, "end": 4.3, "text": "精彩动作正在发生。"},
        {"id": "U3", "start": 5.0, "end": 7.0, "text": "动作已经完成。"},
    ]
    index = {"audio": {"status": "available", "utterances": utterances}}
    parent = {
        "plan_id": "IEP-render", "version": "interaction-edit-plan-v2", "revision": 1,
        "preview": {"signature": "parent"}, "source": {"fingerprint": media_content_fingerprint(source)},
        "index_signature": "index", "review_revision": 1, "event_id": "R1", "group_id": "G",
        "keep_ranges": [{"start": .4, "end": 7.2}],
    }
    raw = {"groups": [
        {"id": "G1", "type": "greeting", "utterance_ids": ["U1"], "decision": "drop", "reason": "寒暄", "depends_on": [], "hook_eligible": False, "hook_score": 0},
        {"id": "G2", "type": "action", "utterance_ids": ["U2"], "decision": "keep", "reason": "动作", "depends_on": [], "hook_eligible": True, "hook_score": 1},
        {"id": "G3", "type": "result", "utterance_ids": ["U3"], "decision": "keep", "reason": "结果", "depends_on": ["G2"], "hook_eligible": False, "hook_score": .5},
    ], "hook_candidates": [{"id": "H1", "group_ids": ["G2"], "reason": "完整动作"}]}
    options = {"speed": 1.25, "target_min_seconds": 15, "target_max_seconds": 60}
    story, identity = analyze_story(index, parent, options, analyze=lambda context: (raw, "fixture"))
    plan = build_second_pass_plan(parent_plan=parent, story=story, utterances=utterances, options=options, story_identity=identity)
    manifest = render_second_pass_candidate(
        source, plan, tmp_path / "renders", ffmpeg=shutil.which("ffmpeg"), ffprobe=shutil.which("ffprobe"), longest_edge=320,
    )
    assert manifest["qa"]["status"] == "passed"
    assert manifest["qa"]["checks"]["audio_video_tail"] is True
    assert abs(manifest["output_duration"] - plan["output_duration"]) <= manifest["qa"]["duration_tolerance_seconds"]
    assert manifest["hook_source_duration"] > 2
    cached = render_second_pass_candidate(
        source, plan, tmp_path / "renders", ffmpeg=shutil.which("ffmpeg"), ffprobe=shutil.which("ffprobe"), longest_edge=320,
    )
    assert cached["cache_hit"] is True
