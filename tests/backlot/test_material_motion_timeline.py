from __future__ import annotations

import subprocess

import numpy as np
import pytest

from backlot import material_motion_timeline as motion


WIDTH, HEIGHT, FPS = 96, 54, 2.0
FRAME_SIZE = WIDTH * HEIGHT


def _frames(count: int, *, moving: bool = True) -> np.ndarray:
    """Deterministic gray frames; motion alternates nonzero/zero diff."""
    base = np.arange(FRAME_SIZE, dtype=np.uint16) % 251
    rows = []
    for index in range(count):
        offset = (index * 7) % 97 if moving else 0
        rows.append(((base + offset) % 256).astype(np.uint8))
    return np.stack(rows)


def _raw(frames: np.ndarray) -> bytes:
    return np.ascontiguousarray(frames, dtype=np.uint8).tobytes()


def _runner(payload: bytes, returncode: int = 0, stderr: bytes = b""):
    def run(command, **_kwargs):
        return subprocess.CompletedProcess(command, returncode, payload, stderr)

    return run


def _media(tmp_path, name="media.mp4"):
    path = tmp_path / name
    path.write_bytes(b"fixture-video")
    return path


def _expected_scores(frames: np.ndarray) -> np.ndarray:
    data = frames.reshape((-1, FRAME_SIZE)).astype(np.float32)
    return (np.mean(np.abs(np.diff(data, axis=0)), axis=1) / 255.0).astype(np.float32)


def test_single_spawn_without_seek_and_with_the_frozen_filter(tmp_path):
    frames = _frames(9)
    seen: list[list[str]] = []

    def run(command, **_kwargs):
        seen.append(list(command))
        return subprocess.CompletedProcess(command, 0, _raw(frames), b"")

    timeline = motion.build_motion_timeline(_media(tmp_path), ffmpeg="ffmpeg", runner=run)
    assert len(seen) == 1
    assert timeline["spawns"] == 1
    assert "-ss" not in seen[0]
    assert seen[0][seen[0].index("-vf") + 1] == "fps=2,scale=96:54:flags=area,format=gray"
    assert seen[0][seen[0].index("-an")] == "-an"
    assert seen[0][seen[0].index("-f") + 1] == "rawvideo"
    assert seen[0][-1] == "pipe:1"


def test_scores_use_the_frozen_metric_and_frame_accounting(tmp_path):
    frames = _frames(10)
    timeline = motion.build_motion_timeline(_media(tmp_path), ffmpeg="ffmpeg",
                                            runner=_runner(_raw(frames)))
    assert timeline["frame_count"] == 10
    assert timeline["sample_count"] == 9
    assert np.allclose(timeline["scores"], _expected_scores(frames), atol=0, rtol=0)


def test_identical_input_produces_byte_identical_scores(tmp_path):
    frames = _frames(6)
    first = motion.build_motion_timeline(_media(tmp_path), ffmpeg="ffmpeg", runner=_runner(_raw(frames)))
    second = motion.build_motion_timeline(_media(tmp_path), ffmpeg="ffmpeg", runner=_runner(_raw(frames)))
    assert first["scores"].tobytes() == second["scores"].tobytes()


def test_odd_frame_bytes_are_refused(tmp_path):
    with pytest.raises(motion.MaterialMotionTimelineError, match="整数倍"):
        motion.build_motion_timeline(_media(tmp_path), ffmpeg="ffmpeg",
                                     runner=_runner(_raw(_frames(3))[:-1]))


def test_decode_failure_raises_unavailable(tmp_path):
    with pytest.raises(motion.MaterialMotionTimelineUnavailable):
        motion.build_motion_timeline(_media(tmp_path), ffmpeg="ffmpeg",
                                     runner=_runner(b"", returncode=1, stderr=b"boom"))


def test_missing_media_is_refused_before_any_spawn(tmp_path):
    with pytest.raises(motion.MaterialMotionTimelineUnavailable, match="不存在"):
        motion.build_motion_timeline(tmp_path / "nope.mp4", ffmpeg="ffmpeg", runner=_runner(b""))


# --- max_motion_in -----------------------------------------------------------

def _timeline(scores, *, fps=FPS):
    return {"scores": np.asarray(scores, dtype=np.float32), "fps": fps,
            "low_motion_threshold": 0.035, "sample_count": len(scores)}


def test_max_motion_covers_the_frame_pairs_inside_the_span():
    scores = [0.01, 0.20, 0.03, 0.90, 0.02]
    timeline = _timeline(scores)
    # [0,2): pairs starting at 0.0, 0.5, 1.0 -> indices 0..2
    assert motion.max_motion_in(timeline, 0.0, 2.0) == pytest.approx(0.20)
    # [1,2): pair starting at 1.0 only -> index 2
    assert motion.max_motion_in(timeline, 1.0, 2.0) == pytest.approx(0.03)
    # [1.5,2.5): pair starting at 1.5 -> index 3
    assert motion.max_motion_in(timeline, 1.5, 2.5) == pytest.approx(0.90)


def test_max_motion_is_exact_not_quantised():
    scores = [0.035123456789, 0.0]
    timeline = _timeline(scores)
    assert motion.max_motion_in(timeline, 0.0, 1.0) == float(np.float32(0.035123456789))


def test_empty_timeline_reports_zero_motion():
    assert motion.max_motion_in(_timeline([]), 0.0, 2.0) == 0.0


def test_reversed_window_is_rejected():
    with pytest.raises(motion.MaterialMotionTimelineError):
        motion.max_motion_in(_timeline([0.1]), 2.0, 1.0)


def test_sub_frame_span_falls_back_to_the_nearest_sample():
    timeline = _timeline([0.11, 0.22, 0.33])
    assert motion.max_motion_in(timeline, 0.0, 0.2) == pytest.approx(0.11, abs=1e-6)


# --- low motion --------------------------------------------------------------

def test_low_motion_intervals_are_continuous_runs():
    scores = [0.01, 0.02, 0.5, 0.01, 0.01, 0.01]
    timeline = _timeline(scores)
    rows = motion.low_motion_intervals(timeline, min_seconds=1.0)
    assert rows == [{"start": 0.0, "end": 1.0, "length": 1.0},
                    {"start": 1.5, "end": 3.0, "length": 1.5}]
    assert motion.low_motion_seconds(timeline, min_seconds=1.0) == 2.5


def test_low_motion_threshold_is_an_upper_bound():
    timeline = _timeline([0.035, 0.0351, 0.0349])
    assert motion.low_motion_intervals(timeline, min_seconds=0.0) == [
        {"start": 0.0, "end": 0.5, "length": 0.5},
        {"start": 1.0, "end": 1.5, "length": 0.5},
    ]


def test_short_low_motion_run_is_dropped():
    timeline = _timeline([0.01, 0.5, 0.01])
    assert motion.low_motion_intervals(timeline, min_seconds=1.0) == []


# --- identity and validation -------------------------------------------------

def test_identity_is_stable_and_sensitive():
    first = motion.motion_identity()
    assert first == motion.motion_identity()
    for tweak in ({"fps": 4.0}, {"width": 64}, {"height": 36}, {"low_motion_threshold": 0.05}):
        assert motion.motion_identity(tweak)["signature"] != first["signature"]


@pytest.mark.parametrize("preset", [
    {"fps": 0.1}, {"fps": 30.0}, {"width": 8}, {"pix_fmt": "rgb24"},
    {"metric": "optical_flow"}, {"low_motion_threshold": 2.0},
])
def test_invalid_presets_are_rejected(preset):
    with pytest.raises(motion.MaterialMotionTimelineError):
        motion.motion_identity(preset)


# --- persistence -------------------------------------------------------------

def test_npz_round_trip_is_bit_exact(tmp_path):
    frames = _frames(8)
    timeline = motion.build_motion_timeline(_media(tmp_path), ffmpeg="ffmpeg",
                                            runner=_runner(_raw(frames)))
    path = motion.save_motion_timeline(tmp_path / "out" / "motion.npz", timeline)
    loaded = motion.load_motion_timeline(path)
    assert loaded is not None
    assert loaded["scores"].tobytes() == timeline["scores"].tobytes()
    assert loaded["identity"] == timeline["identity"]
    assert loaded["fps"] == timeline["fps"]
    assert loaded["sample_count"] == timeline["sample_count"]
    assert motion.max_motion_in(loaded, 0.0, 2.0) == motion.max_motion_in(timeline, 0.0, 2.0)


def test_missing_npz_reads_as_none(tmp_path):
    assert motion.load_motion_timeline(tmp_path / "nope.npz") is None
