"""Regression tests: segmented_music must not attenuate narration.

`_segmented_music` mixed the video's audio with the shaped music via
`amix=inputs=2`, whose default `normalize=1` divides every input by the input
count (x0.5 / -6 dB). Unlike `_mix` / `_full_mix`, this path has no `loudnorm`
stage afterward, so the narration was permanently attenuated across the whole
timeline — including stretches where the music volume expression is 0. The fix
adds `normalize=0` (music is already scaled by the `volume` expression, so
speech must pass at unity).
"""

import shutil
import subprocess
import sys
import math
from array import array
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from tools.audio.audio_mixer import AudioMixer  # noqa: E402


def test_segmented_music_amix_disables_normalize(tmp_path, monkeypatch):
    """The generated amix must carry normalize=0 (offline, no ffmpeg)."""
    video = tmp_path / "v.mp4"
    music = tmp_path / "m.wav"
    video.write_bytes(b"stub")
    music.write_bytes(b"stub")

    captured = []

    def fake_run(self, cmd, **kwargs):
        captured.append(list(cmd))

        class _R:
            stdout = "10.0\n"
            stderr = ""

        return _R()

    monkeypatch.setattr(AudioMixer, "run_command", fake_run)

    AudioMixer().execute(
        {
            "operation": "segmented_music",
            "video_path": str(video),
            "music_path": str(music),
            "music_volume": 0.2,
            "segments": [{"start": 1.0, "end": 2.0}],
            "output_path": str(tmp_path / "out.mp4"),
        }
    )

    ffmpeg_cmds = [c for c in captured if c and c[0] == "ffmpeg"]
    assert ffmpeg_cmds, "no ffmpeg command was built"
    fc = ffmpeg_cmds[0][ffmpeg_cmds[0].index("-filter_complex") + 1]
    assert "amix=inputs=2" in fc
    assert "normalize=0" in fc, f"amix must disable normalize; got: {fc}"
    assert "atrim=start=0.0:end=10.0" in fc
    assert "aloop=loop=-1" in fc


def test_segmented_music_uses_independent_fades_and_clamps_to_video(tmp_path, monkeypatch):
    video = tmp_path / "v.mp4"
    music = tmp_path / "m.wav"
    video.write_bytes(b"stub")
    music.write_bytes(b"stub")
    captured = []

    def fake_run(self, cmd, **kwargs):
        captured.append(list(cmd))

        class _R:
            stdout = "4.0\n"
            stderr = ""

        return _R()

    monkeypatch.setattr(AudioMixer, "run_command", fake_run)
    AudioMixer().execute({
        "operation": "segmented_music",
        "video_path": str(video),
        "music_path": str(music),
        "music_volume": 1.0,
        "segments": [{"start": 0, "end": 86400}],
        "fade_in_seconds": 0.8,
        "fade_out_seconds": 1.5,
        "output_path": str(tmp_path / "out.mp4"),
    })
    command = next(item for item in captured if item and item[0] == "ffmpeg")
    graph = command[command.index("-filter_complex") + 1]
    assert "t,0.8" in graph
    assert "4.0-t)/1.5" in graph
    assert "sample_rates=48000" in graph
    assert command[command.index("-ar") + 1] == "48000"


@pytest.mark.parametrize("start,end", [(-1, 2), (2, 2), (4, 6), (1, 1.5)])
def test_segmented_music_rejects_invalid_source_range(tmp_path, monkeypatch, start, end):
    video = tmp_path / "v.mp4"
    music = tmp_path / "m.wav"
    video.write_bytes(b"stub")
    music.write_bytes(b"stub")

    class _R:
        stdout = "5.0\n"
        stderr = ""

    monkeypatch.setattr(AudioMixer, "run_command", lambda self, cmd, **kwargs: _R())
    result = AudioMixer().execute({
        "operation": "segmented_music", "video_path": str(video), "music_path": str(music),
        "segments": [{"start": 0, "end": 3}], "source_start_seconds": start,
        "source_end_seconds": end, "output_path": str(tmp_path / "out.mp4"),
    })
    assert not result.success
    assert "source range" in result.error


def test_segmented_music_builds_selected_source_loop(tmp_path, monkeypatch):
    video = tmp_path / "v.mp4"
    music = tmp_path / "m.wav"
    video.write_bytes(b"stub")
    music.write_bytes(b"stub")
    captured = []

    class _R:
        stdout = "6.0\n"
        stderr = ""

    monkeypatch.setattr(AudioMixer, "run_command", lambda self, cmd, **kwargs: captured.append(list(cmd)) or _R())
    AudioMixer().execute({
        "operation": "segmented_music", "video_path": str(video), "music_path": str(music),
        "segments": [{"start": 0, "end": 6}], "source_start_seconds": 2,
        "source_end_seconds": 4, "output_path": str(tmp_path / "out.mp4"),
    })
    command = next(cmd for cmd in captured if cmd[0] == "ffmpeg")
    graph = command[command.index("-filter_complex") + 1]
    assert "atrim=start=2.0:end=4.0" in graph
    assert "aloop=loop=-1" in graph
    assert "-stream_loop" not in command


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg required")
def test_segmented_music_preserves_narration_level(tmp_path):
    """End-to-end: narration in a no-music region is not ~6 dB quieter."""

    def _mean_db(path, ss, t):
        out = subprocess.run(
            ["ffmpeg", "-ss", str(ss), "-t", str(t), "-i", str(path),
             "-vn", "-af", "volumedetect", "-f", "null", "-"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
        )
        for line in out.stderr.splitlines():
            if "mean_volume" in line:
                return float(line.split("mean_volume:")[1].strip().split(" ")[0])
        raise AssertionError("no mean_volume in ffmpeg output")

    video = tmp_path / "vspeech.mp4"
    music = tmp_path / "mus.wav"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=black:s=320x240:d=5",
         "-f", "lavfi", "-i", "sine=frequency=300:duration=5",
         "-c:v", "libx264", "-c:a", "aac", "-shortest", str(video)],
        capture_output=True, check=True, timeout=60,
    )
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=800:duration=3", str(music)],
        capture_output=True, check=True, timeout=60,
    )

    # Baseline: the same stereo/aac conversion the tool applies, without any mix.
    baseline = tmp_path / "base.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(video),
         "-af", "aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo",
         "-c:a", "aac", "-b:a", "192k", str(baseline)],
        capture_output=True, check=True, timeout=60,
    )

    out = tmp_path / "out.mp4"
    result = AudioMixer().execute(
        {
            "operation": "segmented_music",
            "video_path": str(video),
            "music_path": str(music),
            "music_volume": 0.2,
            "segments": [{"start": 1.0, "end": 2.0}],  # music only during [1,2]
            "output_path": str(out),
        }
    )
    assert result.success, result.error

    baseline_db = _mean_db(baseline, 3, 1)   # no-music region baseline
    out_db = _mean_db(out, 3, 1)             # no-music region through the tool

    # Narration must track the conversion baseline, not sit ~6 dB below it.
    assert out_db > baseline_db - 2.0, (
        f"narration attenuated: baseline {baseline_db} dB, output {out_db} dB"
    )


@pytest.mark.skipif(shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None, reason="ffmpeg required")
def test_segmented_music_real_output_contains_only_selected_source_range(tmp_path):
    """A 440 Hz first half must not leak when only the 1200 Hz half is selected."""
    video = tmp_path / "silent.mp4"
    music = tmp_path / "two-frequencies.wav"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=black:s=160x120:d=3",
         "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-t", "3",
         "-c:v", "libx264", "-c:a", "aac", str(video)],
        capture_output=True, check=True, timeout=30,
    )
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=2:sample_rate=48000",
         "-f", "lavfi", "-i", "sine=frequency=1200:duration=2:sample_rate=48000",
         "-filter_complex", "[0:a][1:a]concat=n=2:v=0:a=1[out]", "-map", "[out]", str(music)],
        capture_output=True, check=True, timeout=30,
    )
    output = tmp_path / "selected.mp4"
    result = AudioMixer().execute({
        "operation": "segmented_music", "video_path": str(video), "music_path": str(music),
        "music_volume": 0.8, "segments": [{"start": 0, "end": 3}],
        "source_start_seconds": 2, "source_end_seconds": 4,
        "fade_in_seconds": 0, "fade_out_seconds": 0, "output_path": str(output),
    })
    assert result.success, result.error
    decoded = subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", "0.25", "-t", "1.5", "-i", str(output),
         "-vn", "-ac", "1", "-ar", "48000", "-f", "f32le", "pipe:1"],
        capture_output=True, check=True, timeout=30,
    )
    samples = array("f")
    samples.frombytes(decoded.stdout)

    def spectral_power(frequency: float) -> float:
        cosine = sum(sample * math.cos(2 * math.pi * frequency * i / 48000) for i, sample in enumerate(samples))
        sine = sum(sample * math.sin(2 * math.pi * frequency * i / 48000) for i, sample in enumerate(samples))
        return cosine * cosine + sine * sine

    assert spectral_power(1200) > spectral_power(440) * 20
