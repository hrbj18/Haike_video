from __future__ import annotations

import shutil
import subprocess

import pytest

from backlot.material_interaction_second_pass import build_second_pass_plan
from backlot.material_interaction_second_pass_render import (
    InteractionSecondPassRenderError,
    render_second_pass_candidate,
)
from backlot.material_interaction_story import analyze_story
from backlot.media_index import media_content_fingerprint

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
                                reason="FFmpeg is unavailable")

RAW = {"groups": [
    {"id": "G1", "type": "greeting", "utterance_ids": ["U1"], "decision": "drop", "reason": "寒暄",
     "depends_on": [], "hook_eligible": False, "hook_score": 0},
    {"id": "G2", "type": "action", "utterance_ids": ["U2"], "decision": "keep", "reason": "动作",
     "depends_on": [], "hook_eligible": True, "hook_score": 1},
    {"id": "G3", "type": "result", "utterance_ids": ["U3"], "decision": "keep", "reason": "结果",
     "depends_on": ["G2"], "hook_eligible": False, "hook_score": .5},
], "hook_candidates": [{"id": "H1", "group_ids": ["G2"], "reason": "完整动作"}]}

UTTERANCES = [
    {"id": "U1", "start": .5, "end": 1.2, "text": "大家好。"},
    {"id": "U2", "start": 2.0, "end": 4.3, "text": "精彩动作正在发生。"},
    {"id": "U3", "start": 5.0, "end": 7.0, "text": "动作已经完成。"},
]

OPTIONS = {"speed": 1.25, "target_min_seconds": 15, "target_max_seconds": 60}


def _fixture(tmp_path, *, options=None):
    tmp_path.mkdir(parents=True, exist_ok=True)
    source = tmp_path / "source.mp4"
    subprocess.run([
        shutil.which("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=30:duration=8",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=8",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(source),
    ], check=True)
    parent = {
        "plan_id": "IEP-render", "version": "interaction-edit-plan-v2", "revision": 1,
        "preview": {"signature": "parent"}, "source": {"fingerprint": media_content_fingerprint(source)},
        "index_signature": "index", "review_revision": 1, "event_id": "R1", "group_id": "G",
        "keep_ranges": [{"start": .4, "end": 7.2}],
    }
    merged = {**OPTIONS, **(options or {})}
    index = {"audio": {"status": "available", "utterances": UTTERANCES}}
    story, identity = analyze_story(index, parent, merged, analyze=lambda context: (RAW, "fixture"))
    plan = build_second_pass_plan(parent_plan=parent, story=story, utterances=UTTERANCES,
                                  options=merged, story_identity=identity)
    return {"source": source, "plan": plan, "parent": parent, "story": story, "identity": identity,
            "options": merged, "ffmpeg": shutil.which("ffmpeg"), "ffprobe": shutil.which("ffprobe")}


def _recording_runner(sink, *, fail_frames=False):
    def run(command, **kwargs):
        sink.append(list(command))
        if fail_frames and "-frames:v" in command:
            return subprocess.CompletedProcess(command, 1, b"", "probe filter unavailable")
        return subprocess.run(command, **kwargs)

    return run


def _render(fixture, tmp_path, **kwargs):
    return render_second_pass_candidate(
        fixture["source"], fixture["plan"], tmp_path / "renders", ffmpeg=fixture["ffmpeg"],
        ffprobe=fixture["ffprobe"], longest_edge=320, **kwargs,
    )


def test_real_render_keeps_audio_video_clock_with_hook_and_speed(tmp_path):
    fixture = _fixture(tmp_path)
    plan = fixture["plan"]
    manifest = _render(fixture, tmp_path)
    assert manifest["qa"]["status"] == "passed"
    assert manifest["qa"]["checks"]["audio_video_tail"] is True
    assert abs(manifest["output_duration"] - plan["output_duration"]) <= manifest["qa"]["duration_tolerance_seconds"]
    assert manifest["hook_source_duration"] > 2
    cached = _render(fixture, tmp_path)
    assert cached["cache_hit"] is True


# --- A3: the render must read the proxy, never the original ---------------

def test_render_reads_frames_from_the_proxy_and_only_opens_one_input(tmp_path):
    fixture = _fixture(tmp_path)
    proxy = tmp_path / "review-proxy.mp4"
    shutil.copy2(fixture["source"], proxy)
    commands = []
    manifest = _render(fixture, tmp_path, render_source=proxy, runner=_recording_runner(commands))
    render_command = next(row for row in commands if "-filter_complex" in row)
    assert render_command.count("-i") == 1
    assert render_command[render_command.index("-i") + 1] == str(proxy.resolve())
    assert str(fixture["source"].resolve()) not in render_command
    assert manifest["render_source_fingerprint"]
    assert manifest["degradations"] == []


def test_a_missing_proxy_is_reported_instead_of_silently_reading_the_original(tmp_path):
    fixture = _fixture(tmp_path)
    commands = []
    with pytest.raises(InteractionSecondPassRenderError, match="审核代理不存在"):
        _render(fixture, tmp_path, render_source=tmp_path / "absent-proxy.mp4",
                runner=_recording_runner(commands))
    assert commands == []


def test_rendering_without_a_proxy_records_the_downgrade(tmp_path):
    fixture = _fixture(tmp_path)
    manifest = _render(fixture, tmp_path)
    assert any(row.startswith("proxy_missing:") for row in manifest["degradations"])
    assert manifest["qa"]["status"] == "passed"


def test_toggling_subtitles_changes_the_render_signature(tmp_path):
    fixture = _fixture(tmp_path)
    first = _render(fixture, tmp_path)
    off = _fixture(tmp_path / "off", options={"burn_subtitles": False})
    second = _render(off, tmp_path / "off")
    assert first["signature"] != second["signature"]
    assert first["subtitles"]["burned"] is True
    assert second["subtitles"]["burned"] is False


# --- A3: subtitles are burned in and proven -------------------------------

def test_subtitles_are_burned_and_the_file_matches_the_cues(tmp_path):
    fixture = _fixture(tmp_path)
    manifest = _render(fixture, tmp_path)
    assert manifest["subtitles"]["requested"] is True
    assert manifest["subtitles"]["burned"] is True
    assert manifest["subtitles"]["cue_count"] > 0
    assert not [row for row in manifest["degradations"] if row.startswith("subtitle_burn_failed")]
    srt_files = list((tmp_path / "renders").rglob("subtitles.srt"))
    assert len(srt_files) == 1
    text = srt_files[0].read_text(encoding="utf-8")
    assert "精彩动作正在发生。" in text
    assert "动作已经完成。" in text
    # "大家好。" is the clip's *opening word*, so the deterministic edge anchor
    # keeps its group even though the model called it disposable chatter — the
    # clip is supposed to start on the greeting, so all three groups caption.
    assert "大家好。" in text
    assert text.startswith("1\n00:00:00,")
    assert " --> " in text


def test_subtitling_can_be_turned_off_without_a_degradation(tmp_path):
    fixture = _fixture(tmp_path, options={"burn_subtitles": False})
    manifest = _render(fixture, tmp_path)
    assert manifest["subtitles"] == {"requested": False, "burned": False, "cue_count": 3, "style": ""}
    assert not [row for row in manifest["degradations"] if row.startswith("subtitle_burn_failed")]
    assert not list((tmp_path / "renders").rglob("subtitles.srt"))
    assert manifest["qa"]["status"] == "passed"


def test_an_unavailable_subtitle_filter_degrades_visibly_and_still_ships(tmp_path):
    fixture = _fixture(tmp_path)
    commands = []
    manifest = _render(fixture, tmp_path, runner=_recording_runner(commands, fail_frames=True))
    assert manifest["subtitles"]["burned"] is False
    assert manifest["subtitles"]["reason"]
    assert any(row.startswith("subtitle_burn_failed:") for row in manifest["degradations"])
    assert manifest["qa"]["status"] == "passed"
    render_command = next(row for row in commands if "-filter_complex" in row)
    assert "subtitles=" not in " ".join(render_command)


def test_the_srt_writer_formats_cues_and_ignores_blank_text():
    from backlot.material_interaction_second_pass_render import _srt_timestamp, srt_text

    assert _srt_timestamp(0) == "00:00:00,000"
    assert _srt_timestamp(3661.5) == "01:01:01,500"
    assert srt_text([]) == ""
    assert srt_text([{"output_start": 0.0, "output_end": 1.25, "text": "  "}]) == ""
    body = srt_text([
        {"output_start": 1.0, "output_end": 2.5, "text": "第二句"},
        {"output_start": 0.0, "output_end": 1.0, "text": "第一句"},
    ])
    assert body.startswith("1\n00:00:00,000 --> 00:00:01,000\n第一句\n")
    assert "2\n00:00:01,000 --> 00:00:02,500\n第二句" in body


def test_many_short_cuts_still_leave_the_picture_covering_the_audio(tmp_path):
    """A frame-based trim loses the sub-frame remainder of every segment.

    With one or two cuts that is within tolerance, but a plan that compresses a
    dozen pauses used to end with the picture roughly a third of a second short
    of the audio, which the audio/video tail check correctly rejected.  Audio is
    the master clock, so the picture has to be padded to cover it.
    """
    from backlot.material_interaction_second_pass_render import (
        _filters,
        _render_contract,
        _stream_duration,
        probe_media,
    )

    fixture = _fixture(tmp_path)
    contract = _render_contract(probe_media(fixture["source"], fixture["ffprobe"]), 320)
    occurrences = [
        {"occurrence_id": f"O-{index:02d}", "role": "body", "speed": 1.1,
         "source_start": round(0.1 + index * 0.5, 6),
         "source_end": round(0.1 + index * 0.5 + 0.47, 6)}
        for index in range(14)
    ]
    graph, mappings, expected = _filters(occurrences, contract, True)
    assert "tpad=stop_mode=clone:stop=-1" in graph
    output = tmp_path / "many-cuts.mp4"
    subprocess.run([
        fixture["ffmpeg"], "-hide_banner", "-loglevel", "error", "-y", "-i", str(fixture["source"]),
        "-filter_complex", graph, *mappings, "-c:v", "libx264", "-preset", "ultrafast",
        "-crf", "28", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", str(output),
    ], check=True)
    probe = probe_media(output, fixture["ffprobe"])
    video = next(row for row in probe["streams"] if row.get("codec_type") == "video")
    audio = next(row for row in probe["streams"] if row.get("codec_type") == "audio")
    tail = abs(_stream_duration(video) - _stream_duration(audio))
    tolerance = max(2.0 / contract["fps"], .05)
    assert tail <= tolerance, f"picture must cover the audio, tail gap was {tail:.3f}s"
    assert abs(float(probe["format"]["duration"]) - expected) <= tolerance


# --- P0-3: every seam carries an audio fade; the clock never moves ----------

def _demo_contract(longest_edge=320):
    return {"version": "x", "longest_edge": longest_edge, "width": 320, "height": 240, "fps": 30.0,
            "video": "h264/yuv420p/crf25", "audio": "aac/48kHz/128k", "faststart": True}


def test_every_audio_segment_gets_a_matched_fade_pair():
    from backlot.material_interaction_second_pass_render import _filters

    occurrences = [
        {"occurrence_id": "O1", "role": "body", "speed": 1.25, "source_start": 1.0, "source_end": 3.0},
        {"occurrence_id": "O2", "role": "body", "speed": 2.0, "source_start": 4.0, "source_end": 6.0},
    ]
    graph, _, expected = _filters(occurrences, _demo_contract(), True)
    assert graph.count("afade=t=in:st=0") == 2
    assert graph.count("afade=t=out") == 2
    # fade-out start is on the speed-adjusted timeline: dur/speed - fade.
    out_start = (6.0 - 4.0) / 2.0 - 8.0 / 1000.0
    assert f"afade=t=out:st={out_start:.6f}:d=0.008000" in graph
    # afade never changes duration.
    assert expected == pytest.approx((3.0 - 1.0) / 1.25 + (6.0 - 4.0) / 2.0)


def test_disabling_the_fade_is_byte_identical_to_the_previous_renderer():
    from backlot.material_interaction_second_pass_render import _filters

    occurrences = [{"occurrence_id": "O1", "role": "body", "speed": 1.25,
                    "source_start": 1.0, "source_end": 3.0}]
    graph, _, _ = _filters(occurrences, _demo_contract(), True, audio_fade=False, edge_fade=False)
    assert "[0:a]atrim=start=1.000000:end=3.000000,asetpts=PTS-STARTPTS," \
           "aresample=48000:async=0:first_pts=0,aformat=sample_rates=48000:channel_layouts=stereo," \
           "atempo=1.250000[a0]" in graph
    assert "afade" not in graph


def test_edge_fade_wraps_the_whole_clip_without_changing_duration():
    from backlot.material_interaction_second_pass_render import _filters

    occurrences = [{"occurrence_id": "O1", "role": "body", "speed": 1.0,
                    "source_start": 1.0, "source_end": 5.0}]
    graph, _, expected = _filters(occurrences, _demo_contract(), True,
                                  edge_fade=True, edge_fade_ms=200.0)
    assert f"[acat]atrim=duration={expected:.6f},asetpts=PTS-STARTPTS,afade=t=in:st=0:d=0.200000," \
           f"afade=t=out:st={expected - 0.2:.6f}:d=0.200000[aout]" in graph
    assert expected == pytest.approx(4.0)


def test_atempo_is_a_single_instance_and_matches_the_requested_ratio():
    from backlot.material_interaction_second_pass_render import (
        InteractionSecondPassRenderError,
        _atempo_chain,
    )

    for speed in (1.0, 1.1, 1.25, 1.5, 2.0, 3.0, 4.0):
        chain = _atempo_chain(speed)
        assert chain.startswith("atempo=")
        assert chain.count("atempo=") == 1, "段级倍速范围内必须单实例，不做链式"
        assert float(chain.split("=", 1)[1]) == pytest.approx(speed, abs=1e-6)
    # Measured range of this pinned binary is [0.5, 100]; beyond it we refuse in Chinese.
    assert _atempo_chain(100.0) == "atempo=100.000000"
    with pytest.raises(InteractionSecondPassRenderError, match="atempo 支持范围"):
        _atempo_chain(101.0)
    with pytest.raises(InteractionSecondPassRenderError, match="atempo 支持范围"):
        _atempo_chain(0.4)


def test_speed_up_plan_renders_and_keeps_the_clock(tmp_path):
    """A real render of a speed_up plan: 2x waiting passage, QA still passes."""
    fixture = _fixture(tmp_path)
    source, parent, ffmpeg, ffprobe = fixture["source"], fixture["parent"], fixture["ffmpeg"], fixture["ffprobe"]
    story = {
        "groups": [{"id": "G1", "sequence": 1, "type": "question_answer",
                    "utterance_ids": ["U1", "U2", "U3"], "summary": "完整对话",
                    "decision": "keep", "selected": True, "locked": False, "reason": "保留完整对话",
                    "depends_on": [], "hook_eligible": False, "hook_score": 0.0, "evidence_ids": [],
                    "source_ranges": [{"start": 0.45, "end": 7.15}],
                    "source_range": {"start": 0.45, "end": 7.15}}],
        "hook_candidates": [], "recommended_hook_id": None, "warnings": [], "summary": "对话",
    }
    options = {"speed": 1.0, "hook_enabled": False, "target_min_seconds": 15,
               "target_max_seconds": 60, "pause_handling": "speed_up", "pause_speed": 2.0}
    evidence = {"version": "material-pause-evidence-v1", "status": "available",
                "identity": {"signature": "x"}, "source_fingerprint": "source",
                "silences": [{"start": 4.4, "end": 4.85}]}
    plan = build_second_pass_plan(
        parent_plan=parent, story=story, utterances=UTTERANCES, options=options,
        story_identity={"model": "fixture"}, pause_evidence=evidence,
        speech_ranges=[{"start": 0.4, "end": 1.3}, {"start": 1.9, "end": 4.35},
                       {"start": 4.9, "end": 7.05}],
    )
    assert plan["pause_trims"] == [] and plan["speed_segments"]
    manifest = render_second_pass_candidate(
        source, plan, tmp_path / "renders-speed", ffmpeg=ffmpeg, ffprobe=ffprobe, longest_edge=320,
    )
    assert manifest["qa"]["status"] == "passed"
    assert abs(manifest["output_duration"] - plan["output_duration"]) <= manifest["qa"]["duration_tolerance_seconds"]
    assert manifest["audio_effects"]["audio_fade"] is True
