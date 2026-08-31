"""Vertical / arbitrary-resolution support for video_compose's FFmpeg compose.

Regression test for a silent-dimension bug: the compose target resolution was
resolved from `profile` (and the documented `metadata.compose_target` hook) but
the per-segment scale/pad filter hardcoded 1920x1080, so vertical profiles like
`tiktok` silently produced landscape output. These tests run the real FFmpeg
path on a tiny lavfi fixture and assert the output dimensions.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tools.video.video_compose import VideoCompose, _discover_ffmpeg_pair, _ensure_ffmpeg_on_path

_MEDIA_PAIR = _discover_ffmpeg_pair()
if _MEDIA_PAIR:
    _ensure_ffmpeg_on_path()
pytestmark = pytest.mark.skipif(
    _MEDIA_PAIR is None,
    reason="ffmpeg/ffprobe not available",
)


def _make_clip(path: Path, w: int = 1280, h: int = 720, d: int = 2) -> None:
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i",
         f"color=c=teal:s={w}x{h}:d={d}:r=30",
         "-c:v", "libx264", "-crf", "28", "-pix_fmt", "yuv420p",
         "-g", "30", "-keyint_min", "30", str(path)],
        capture_output=True, check=True,
    )


def _dims(path: Path) -> tuple[int, int]:
    out = subprocess.check_output(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0", str(path)]
    ).decode().strip()
    w, h = out.split(",")
    return int(w), int(h)


def _edit_decisions(src: Path, metadata: dict | None = None) -> dict:
    ed = {
        "version": "1.0",
        "render_runtime": "ffmpeg",
        "cuts": [{"id": "c1", "source": str(src), "in_seconds": 0, "out_seconds": 2}],
    }
    if metadata:
        ed["metadata"] = metadata
    return ed


def test_compose_default_is_landscape_hd(tmp_path):
    """No profile / no target → unchanged 1920x1080 default (backward compatible)."""
    src = tmp_path / "in.mp4"
    _make_clip(src)
    out = tmp_path / "out.mp4"
    r = VideoCompose().execute(
        {"operation": "compose", "edit_decisions": _edit_decisions(src), "output_path": str(out)}
    )
    assert r.success, r.error
    assert _dims(out) == (1920, 1080)


def test_compose_vertical_profile(tmp_path):
    """profile='tiktok' → 1080x1920 (the bug: previously stayed 1920x1080)."""
    src = tmp_path / "in.mp4"
    _make_clip(src)
    out = tmp_path / "out.mp4"
    r = VideoCompose().execute(
        {"operation": "compose", "edit_decisions": _edit_decisions(src),
         "profile": "tiktok", "output_path": str(out)}
    )
    assert r.success, r.error
    assert _dims(out) == (1080, 1920)


def test_compose_target_override_cover(tmp_path):
    """metadata.compose_target with fit='cover' → exact requested dims, cropped to fill."""
    src = tmp_path / "in.mp4"
    _make_clip(src)
    out = tmp_path / "out.mp4"
    ed = _edit_decisions(src, metadata={"compose_target": {"width": 720, "height": 1280, "fit": "cover"}})
    r = VideoCompose().execute(
        {"operation": "compose", "edit_decisions": ed, "output_path": str(out)}
    )
    assert r.success, r.error
    assert _dims(out) == (720, 1280)


def test_subtitle_filter_preserves_portrait_font_scale(tmp_path):
    subtitle = tmp_path / "captions.srt"
    subtitle.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\n测试字幕。\n",
        encoding="utf-8",
    )

    filter_text = VideoCompose._build_subtitles_filter(
        subtitle,
        {"font": "Microsoft YaHei", "responsive": True, "font_size": 34, "margin_v": 250, "alignment": 2},
        original_size="1080x1920",
    )

    ass_path = subtitle.with_suffix(".ass")
    ass_content = ass_path.read_text(encoding="utf-8")
    assert filter_text.startswith("ass='")
    assert "PlayResX: 1080" in ass_content
    assert "PlayResY: 1920" in ass_content
    assert "ScaleX, ScaleY" in ass_content
    assert "100,100" in ass_content
    assert ",49," in ass_content


def _frame_md5(path: Path, seconds: float) -> str:
    return subprocess.check_output([
        "ffmpeg", "-v", "error", "-ss", f"{seconds:.3f}", "-i", str(path),
        "-frames:v", "1", "-f", "md5", "-",
    ]).decode().strip()


def test_timed_video_overlay_starts_its_local_clock_at_global_start(tmp_path):
    """A later scene overlay must animate, then disappear at its own boundary."""
    base = tmp_path / "base.mp4"
    presenter = tmp_path / "presenter.mp4"
    output = tmp_path / "overlay.mp4"
    subprocess.run([
        "ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=black:s=160x90:r=20:d=4",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(base),
    ], check=True, capture_output=True)
    subprocess.run([
        "ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc2=s=80x80:r=20:d=1",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(presenter),
    ], check=True, capture_output=True)

    result = VideoCompose().execute({
        "operation": "overlay",
        "input_path": str(base),
        "overlays": [{
            "asset_path": str(presenter),
            "x": 0,
            "y": 0,
            "width": 80,
            "height": 80,
            "start_seconds": 2.0,
            "end_seconds": 3.0,
        }],
        "output_path": str(output),
    })

    assert result.success, result.error
    assert _frame_md5(output, 1.8) == _frame_md5(base, 1.8)
    assert _frame_md5(output, 2.1) != _frame_md5(output, 2.7)
    # The re-encode can leave a one-level chroma rounding difference after a
    # colourful frame, so compare two post-overlay frames rather than the
    # independently encoded source file.
    assert _frame_md5(output, 3.2) == _frame_md5(output, 3.8)
    assert _frame_md5(output, 3.2) != _frame_md5(output, 2.7)


def test_audio_stream_probe_distinguishes_silent_media_from_probe_failure(monkeypatch, tmp_path):
    import tools.video.video_compose as video_compose_module

    monkeypatch.setattr(
        video_compose_module,
        "_discover_ffmpeg_pair",
        lambda: ("ffmpeg", "ffprobe"),
    )
    monkeypatch.setattr(
        video_compose_module.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, stdout="", stderr=""),
    )
    assert VideoCompose._has_audio_stream(tmp_path / "silent.mp4") is False

    monkeypatch.setattr(
        video_compose_module.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 1, stdout="", stderr="invalid media"),
    )
    with pytest.raises(RuntimeError, match="ffprobe failed"):
        VideoCompose._has_audio_stream(tmp_path / "broken.mp4")
