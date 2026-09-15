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


def test_candidate_render_reads_from_the_proxy_not_the_broken_timestamp_source(tmp_path):
    """时间戳断裂的原片不能直接 trim，渲染必须改读审核代理。

    真实事故：长直播回放原片是 TS 转封装录制的 HEVC，PTS 在部分区间断裂。单输入 +
    `trim=start:end` 会少取数据——46.042 秒的区间只取到 21.8 秒视频 / 25.0 秒音频，
    候选被 QA 判为不合格；改成逐段输入 seek 虽能取对（46.083 秒），但每段一次 seek
    在 1.7 GB 的 HEVC 上要付出分钟级代价，三段事件跑满 900 秒超时。
    所以渲染源换成时间戳连续的代理，而渲染契约仍按原片计算（规格不变）。
    """
    source, ffmpeg, ffprobe = create_fixture(tmp_path)
    proxy = tmp_path / "review-proxy.mp4"
    shutil.copy2(source, proxy)
    plan = plan_for(source)
    seen = {}

    def capture(command, **kwargs):
        if command[0] == ffprobe:
            return subprocess.run(command, **kwargs)
        seen["command"] = command
        return subprocess.CompletedProcess(command, 1, "", "stop-here")

    with pytest.raises(render.InteractionRenderError):
        render.render_interaction_candidate(source, plan, tmp_path / "out", ffmpeg=ffmpeg, ffprobe=ffprobe,
                                            render_source=proxy, runner=capture)

    command = seen["command"]
    assert command.count("-i") == 1, "只能开一个输入：多输入各自 seek 会超时"
    assert command[command.index("-i") + 1] == str(proxy.resolve()), "取帧必须走代理"
    assert str(source.resolve()) not in command, "原片只用于契约与指纹，不该出现在命令行"
    assert "-ss" not in command, "不能靠逐段输入 seek：大 HEVC 上代价是分钟级"
    assert "trim=start=0.000000:end=0.500000" in " ".join(command)
    assert abs(float(command[command.index("-t") + 1]) - 1.0) < 1e-6, "-t 应卡在各保留段之和上"
    assert plan["source"]["fingerprint"] not in command
