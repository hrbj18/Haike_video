from __future__ import annotations

import subprocess
import time

import numpy as np
import pytest

from backlot import material_audio_envelope as envelope


SR = 16000


def _pcm(samples: np.ndarray) -> bytes:
    return np.asarray(samples, dtype="<i2").tobytes()


def _tone(seconds: float, amplitude: float = 0.35, frequency: float = 220.0) -> np.ndarray:
    t = np.arange(int(SR * seconds)) / SR
    return (np.sin(2 * np.pi * frequency * t) * amplitude * 32767).astype("<i2")


def _silence(seconds: float) -> np.ndarray:
    return np.zeros(int(SR * seconds), dtype="<i2")


class _Recorder:
    """Fake runner: counts spawns and replays one fixed PCM payload."""

    def __init__(self, payload: bytes, returncode: int = 0, stderr: bytes = b"") -> None:
        self.payload = payload
        self.returncode = returncode
        self.stderr = stderr
        self.calls: list[list[str]] = []

    def __call__(self, command, **_kwargs):
        self.calls.append(list(command))
        return subprocess.CompletedProcess(command, self.returncode, self.payload, self.stderr)


def _media(tmp_path, name="media.mp4"):
    path = tmp_path / name
    path.write_bytes(b"fixture-audio")
    return path


# --- A1: primitives ----------------------------------------------------------

def test_little_endian_bytes_become_int16_samples(tmp_path):
    payload = _pcm(np.array([0, 1, -1, 32767, -32768], dtype="<i2"))
    recorder = _Recorder(payload)
    result = envelope.decode_envelope(_media(tmp_path), ffmpeg="ffmpeg", runner=recorder)
    assert result["sample_count"] == 5
    assert len(recorder.calls) == 1


def test_odd_byte_count_is_refused_instead_of_silently_truncated(tmp_path):
    recorder = _Recorder(b"\x01\x02\x03")
    with pytest.raises(envelope.MaterialAudioEnvelopeError, match="整数倍"):
        envelope.decode_envelope(_media(tmp_path), ffmpeg="ffmpeg", runner=recorder)


def test_frame_count_matches_the_window_formula():
    total = SR * 3
    audio = _tone(3.0)
    rms_db, peak_db = envelope.build_envelope(audio, sample_rate=SR, window=320, hop=160)
    assert int(rms_db.size) == (total - 320) // 160 + 1
    assert int(peak_db.size) == int(rms_db.size)


def test_timeline_never_runs_past_the_audio_duration():
    audio = _tone(2.0)
    window, hop = 320, 160
    rms_db, _ = envelope.build_envelope(audio, sample_rate=SR, window=window, hop=hop)
    holder = {"rms_db": rms_db, "window_samples": window, "hop_samples": hop, "sample_rate": SR}
    last = envelope.frame_time(holder, int(rms_db.size) - 1)
    assert last == pytest.approx((int(rms_db.size) - 1) * hop / SR)
    assert last + window / SR <= len(audio) / SR + 1e-9


def test_envelope_is_byte_identical_across_runs(tmp_path):
    recorder = _Recorder(_pcm(_tone(1.0)))
    first = envelope.decode_envelope(_media(tmp_path), ffmpeg="ffmpeg", duration=1.0, runner=recorder)
    second = envelope.decode_envelope(_media(tmp_path), ffmpeg="ffmpeg", duration=1.0, runner=recorder)
    assert first["rms_db"].tobytes() == second["rms_db"].tobytes()
    assert first["peak_db"].tobytes() == second["peak_db"].tobytes()


def test_all_silence_yields_one_interval_covering_the_material():
    silence = _silence(2.0)
    rms_db, peak_db = envelope.build_envelope(silence, sample_rate=SR, window=320, hop=160)
    holder = {"rms_db": rms_db, "peak_db": peak_db, "window_samples": 320, "hop_samples": 160,
              "sample_rate": SR}
    rows = envelope.silence_intervals(holder)
    assert len(rows) == 1
    assert rows[0]["start"] == 0.0
    assert rows[0]["end"] == pytest.approx((int(rms_db.size) - 1) * 160 / SR + 320 / SR)


def test_full_scale_input_yields_no_interval():
    loud = np.where(np.arange(SR) % 2 == 0, 32767, -32768).astype("<i2")
    rms_db, peak_db = envelope.build_envelope(loud, sample_rate=SR, window=320, hop=160)
    holder = {"rms_db": rms_db, "peak_db": peak_db, "window_samples": 320, "hop_samples": 160,
              "sample_rate": SR}
    assert envelope.silence_intervals(holder) == []


def test_silence_between_two_tones_is_found_once():
    audio = np.concatenate([_tone(1.0), _silence(0.8), _tone(1.0)])
    rms_db, peak_db = envelope.build_envelope(audio, sample_rate=SR, window=320, hop=160)
    holder = {"rms_db": rms_db, "peak_db": peak_db, "window_samples": 320, "hop_samples": 160,
              "sample_rate": SR}
    rows = envelope.silence_intervals(holder)
    assert len(rows) == 1
    assert rows[0]["start"] == pytest.approx(1.0, abs=0.05)
    assert rows[0]["end"] == pytest.approx(1.8, abs=0.05)
    assert rows[0]["length"] >= 0.45


def test_short_dip_below_the_minimum_is_not_an_interval():
    audio = np.concatenate([_tone(1.0), _silence(0.2), _tone(1.0)])
    rms_db, peak_db = envelope.build_envelope(audio, sample_rate=SR, window=320, hop=160)
    holder = {"rms_db": rms_db, "peak_db": peak_db, "window_samples": 320, "hop_samples": 160,
              "sample_rate": SR}
    assert envelope.silence_intervals(holder) == []


# --- A1: spawn accounting ----------------------------------------------------

def test_whole_file_envelope_costs_exactly_one_spawn_without_seek(tmp_path):
    recorder = _Recorder(_pcm(np.concatenate([_tone(1.0), _silence(1.0), _tone(1.0)])))
    result = envelope.decode_envelope(_media(tmp_path), ffmpeg="ffmpeg", duration=3.0, runner=recorder)
    assert len(recorder.calls) == 1
    command = recorder.calls[0]
    assert "-ss" not in command
    assert result["spawns"] == 1
    assert command[command.index("-f") + 1] == "s16le"
    assert command[command.index("-ar") + 1] == "16000"
    assert command[command.index("-ac") + 1] == "1"


# --- identity and validation -------------------------------------------------

def test_identity_is_stable_and_ignores_thresholds():
    first = envelope.envelope_identity()
    assert first == envelope.envelope_identity()
    assert first["signature"] == envelope.envelope_identity({"threshold_db": -55.0})["signature"]
    assert first["signature"] != envelope.envelope_identity({"window_ms": 40.0})["signature"]
    assert first["signature"] != envelope.envelope_identity({"hop_ms": 20.0})["signature"]
    assert first["signature"] != envelope.envelope_identity({"sample_rate": 8000})["signature"]


@pytest.mark.parametrize("preset", [
    {"threshold_db": -5.0}, {"threshold_db": -95.0}, {"threshold_db": float("nan")},
    {"min_silence_seconds": 0.01}, {"min_silence_seconds": 40.0},
    {"hop_ms": 50.0, "window_ms": 20.0}, {"metric": "peak"},
])
def test_out_of_range_preset_is_rejected(preset):
    with pytest.raises(envelope.MaterialAudioEnvelopeError):
        envelope.envelope_identity(preset)


def test_missing_media_is_refused_before_any_spawn(tmp_path):
    recorder = _Recorder(b"")
    with pytest.raises(envelope.MaterialAudioEnvelopeUnavailable, match="不存在"):
        envelope.decode_envelope(tmp_path / "nope.mp4", ffmpeg="ffmpeg", runner=recorder)
    assert recorder.calls == []


def test_decode_failure_raises_unavailable(tmp_path):
    recorder = _Recorder(b"", returncode=1, stderr=b"No such filter")
    with pytest.raises(envelope.MaterialAudioEnvelopeUnavailable):
        envelope.decode_envelope(_media(tmp_path), ffmpeg="ffmpeg", runner=recorder)


# --- A3: re-tuning is free ---------------------------------------------------

def test_seven_threshold_sweeps_cost_zero_extra_spawns_and_are_reproducible(tmp_path):
    audio = np.concatenate([_tone(0.7, amplitude=0.30), _silence(0.9), _tone(0.7, amplitude=0.004),
                            _silence(0.6), _tone(0.7, amplitude=0.25)])
    recorder = _Recorder(_pcm(audio))
    built = envelope.decode_envelope(_media(tmp_path), ffmpeg="ffmpeg", duration=3.0, runner=recorder)
    assert len(recorder.calls) == 1

    thresholds = (-24.0, -26.0, -28.0, -30.0, -32.0, -34.0, -36.0)
    started = time.perf_counter()
    swept = [envelope.silence_intervals(built, threshold_db=db) for db in thresholds]
    elapsed = (time.perf_counter() - started) * 1000
    assert len(recorder.calls) == 1                 # 0 extra spawns
    assert elapsed < 50.0, f"7 组阈值耗时 {elapsed:.2f}ms"

    # A fresh decode per threshold must agree with the reused envelope.
    for db, rows in zip(thresholds, swept):
        again = envelope.silence_intervals(
            envelope.decode_envelope(_media(tmp_path), ffmpeg="ffmpeg", duration=3.0, runner=recorder),
            threshold_db=db,
        )
        assert again == rows


def test_peak_guard_flags_a_near_full_scale_passage():
    loud = (np.sin(2 * np.pi * 220 * np.arange(SR * 2) / SR) * 0.999 * 32767).astype("<i2")
    rms_db, peak_db = envelope.build_envelope(loud, sample_rate=SR, window=320, hop=160)
    holder = {"rms_db": rms_db, "peak_db": peak_db, "window_samples": 320, "hop_samples": 160,
              "sample_rate": SR}
    assert envelope.peak_guard_intervals(holder)
    assert envelope.silence_intervals(holder) == []


# --- projection and comparison helpers ---------------------------------------

def test_intervals_are_clipped_and_merged_onto_ranges():
    intervals = [{"start": 0.0, "end": 5.0}, {"start": 4.8, "end": 8.0}, {"start": 20.0, "end": 22.0}]
    projected = envelope.intervals_within(intervals, [{"start": 2.0, "end": 6.0}])
    assert projected == [{"start": 2.0, "end": 6.0, "length": 4.0}]


def test_iou_and_coverage_are_one_for_identical_interval_sets():
    rows = [{"start": 1.0, "end": 2.0}, {"start": 4.0, "end": 5.0}]
    assert envelope.interval_iou(rows, rows) == 1.0
    assert envelope.interval_coverage(rows, rows) == 1.0


def test_calibration_report_aggregates_per_range_pairs():
    pairs = [
        {"range": {"start": 0, "end": 10},
         "silencedetect": [{"start": 1.0, "end": 3.0}],
         "envelope": [{"start": 1.1, "end": 2.9}]},
        {"range": {"start": 10, "end": 20},
         "silencedetect": [{"start": 12.0, "end": 14.0}],
         "envelope": [{"start": 12.0, "end": 14.0}]},
    ]
    report = envelope.calibrated_against_ffmpeg(pairs)
    assert report["range_count"] == 2
    assert 0.9 < report["mean_iou"] <= 1.0
    assert 0.9 < report["mean_coverage"] <= 1.0
    assert report["ratio"] == pytest.approx(3.8 / 4.0, abs=0.01)
