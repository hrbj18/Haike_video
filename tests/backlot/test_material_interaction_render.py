import hashlib
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from backlot import material_interaction_render as render
from backlot.media_index import media_content_fingerprint


def plan_for(source: Path, *, duration=1.6):
    return {
        "version": "interaction-edit-plan-v1", "plan_id": "IEP-test", "revision": 0,
        "status": "pending_review", "source": {"fingerprint": media_content_fingerprint(source)},
        "event_start": 0.0, "event_end": duration, "source_duration": duration,
        "removed_ranges": [{"id": "D001", "start": .5, "end": 1.1,
                            "reason_code": "confirmed_silent_wait", "reason": "test",
                            "evidence_ids": ["U1", "U2", "silence"], "restored": False}],
        "keep_ranges": [{"start": 0.0, "end": .5}, {"start": 1.1, "end": duration}],
        "output_duration": round(duration - .6, 3), "qa": {"status": "not_rendered"},
    }


def create_fixture(tmp_path: Path, *, audio=True, codec="mpeg4") -> tuple[Path, str, str]:
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        pytest.skip("FFmpeg/ffprobe unavailable")
    source = tmp_path / ("source.avi" if codec == "mpeg4" else "source.mp4")
    command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
               "-f", "lavfi", "-i", "testsrc2=size=322x242:rate=25:duration=1.6"]
    if audio:
        command.extend(["-f", "lavfi", "-i", "sine=frequency=700:sample_rate=48000:duration=1.6"])
    command.extend(["-c:v", codec])
    if audio:
        command.extend(["-c:a", "pcm_s16le" if source.suffix == ".avi" else "aac"])
    else:
        command.append("-an")
    command.append(str(source))
    completed = subprocess.run(command, capture_output=True, timeout=60)
    if completed.returncode != 0:
        pytest.skip("Current FFmpeg cannot create the media fixture")
    return source, ffmpeg, ffprobe


def test_real_ffmpeg_candidate_is_precise_browser_mp4_cached_and_source_unchanged(tmp_path):
    source, ffmpeg, ffprobe = create_fixture(tmp_path)
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    plan = plan_for(source)
    first = render.render_interaction_candidate(source, plan, tmp_path / "out", ffmpeg=ffmpeg, ffprobe=ffprobe)
    second = render.render_interaction_candidate(source, plan, tmp_path / "out", ffmpeg=ffmpeg, ffprobe=ffprobe)
    assert first["qa"]["status"] == "passed" and all(first["qa"]["checks"].values())
    assert first["status"] == "pending_review" and first["removed_seconds"] > .5
    assert Path(first["path"]).is_file() and second["cache_hit"] is True
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
    probe = render.probe_media(Path(first["path"]), ffprobe)
    streams = {row["codec_type"]: row for row in probe["streams"]}
    assert streams["video"]["codec_name"] == "h264" and streams["video"]["pix_fmt"] == "yuv420p"
    assert streams["audio"]["codec_name"] == "aac"


def test_real_ffmpeg_candidate_without_audio_remains_video_only(tmp_path):
    source, ffmpeg, ffprobe = create_fixture(tmp_path, audio=False)
    result = render.render_interaction_candidate(source, plan_for(source), tmp_path / "out", ffmpeg=ffmpeg, ffprobe=ffprobe)
    streams = render.probe_media(Path(result["path"]), ffprobe)["streams"]
    assert [row["codec_type"] for row in streams] == ["video"]


def test_changed_source_and_empty_keep_ranges_are_rejected(tmp_path):
    source, ffmpeg, ffprobe = create_fixture(tmp_path)
    plan = plan_for(source)
    plan["source"]["fingerprint"] = "changed"
    with pytest.raises(render.InteractionRenderError, match="指纹"):
        render.render_interaction_candidate(source, plan, tmp_path / "out", ffmpeg=ffmpeg, ffprobe=ffprobe)
    plan = plan_for(source)
    plan["removed_ranges"] = [{"id": "D1", "start": 0, "end": 1.6,
                               "reason_code": "test", "reason": "test", "evidence_ids": ["F1"], "restored": False}]
    plan["keep_ranges"] = []
    plan["output_duration"] = 0
    with pytest.raises(render.InteractionRenderError, match="没有可保留"):
        render.render_interaction_candidate(source, plan, tmp_path / "out", ffmpeg=ffmpeg, ffprobe=ffprobe)


def test_failed_render_does_not_publish_temporary_or_manifest(tmp_path):
    source, ffmpeg, ffprobe = create_fixture(tmp_path)
    plan = plan_for(source)

    def fail_runner(command, **kwargs):
        if command[0] == ffprobe:
            return subprocess.run(command, **kwargs)
        return subprocess.CompletedProcess(command, 1, "", "failed")

    with pytest.raises(render.InteractionRenderError):
        render.render_interaction_candidate(source, plan, tmp_path / "out", ffmpeg=ffmpeg, ffprobe=ffprobe,
                                            runner=fail_runner)
    assert not list((tmp_path / "out").rglob("*.mp4"))
    assert not list((tmp_path / "out").rglob("manifest.json"))
