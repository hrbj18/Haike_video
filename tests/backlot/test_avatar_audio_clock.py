from __future__ import annotations

import hashlib
import wave
from pathlib import Path

import pytest

from backlot.avatar_audio_clock import (
    AvatarAudioClockError,
    align_pcm_wav_to_frame_clock,
    inspect_frame_clock_wav,
    nearest_video_frame,
)


def _write_wav(
    path: Path,
    *,
    sample_frames: int,
    sample_rate: int = 16_000,
    channels: int = 1,
    sample_width: int = 2,
) -> None:
    with wave.open(str(path), "wb") as output:
        output.setnchannels(channels)
        output.setsampwidth(sample_width)
        output.setframerate(sample_rate)
        output.writeframes(b"\x01" * sample_frames * channels * sample_width)


def test_final_track_is_padded_once_to_integer_25fps_clock(tmp_path: Path) -> None:
    path = tmp_path / "role.wav"
    _write_wav(path, sample_frames=36_000)

    clock = align_pcm_wav_to_frame_clock(path)

    assert clock["content_sample_frames"] == 36_000
    assert clock["final_padding_sample_frames"] == 480
    assert clock["sample_frame_count"] == 36_480
    assert clock["samples_per_video_frame"] == 640
    assert clock["video_frame_count"] == 57
    assert inspect_frame_clock_wav(path, require_aligned=True)["video_frame_count"] == 57


def test_repeated_alignment_is_byte_stable_and_never_adds_a_second_tail(tmp_path: Path) -> None:
    path = tmp_path / "role.wav"
    _write_wav(path, sample_frames=36_000)

    first = align_pcm_wav_to_frame_clock(path)
    first_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    second = align_pcm_wav_to_frame_clock(path)

    assert first["final_padding_sample_frames"] == 480
    assert second["final_padding_sample_frames"] == 0
    assert second["sample_frame_count"] == first["sample_frame_count"]
    assert hashlib.sha256(path.read_bytes()).hexdigest() == first_hash


def test_absolute_boundary_mapping_does_not_sum_per_turn_ceil() -> None:
    samples_per_frame = 640
    per_turn_ceil_sum = 2 * ((200 + samples_per_frame - 1) // samples_per_frame)
    final_track_ceil = (400 + samples_per_frame - 1) // samples_per_frame

    assert per_turn_ceil_sum == 2
    assert final_track_ceil == 1
    assert nearest_video_frame(200, samples_per_frame) == 0
    assert nearest_video_frame(400, samples_per_frame) == 1


@pytest.mark.parametrize(
    ("channels", "sample_width", "sample_rate", "message"),
    [
        (2, 2, 16_000, "PCM16 单声道"),
        (1, 3, 16_000, "PCM16 单声道"),
        (1, 2, 22_051, "不能建立整数 25FPS"),
    ],
)
def test_invalid_avatar_pcm_contract_fails_closed(
    tmp_path: Path,
    channels: int,
    sample_width: int,
    sample_rate: int,
    message: str,
) -> None:
    path = tmp_path / "invalid.wav"
    _write_wav(
        path,
        sample_frames=1_000,
        channels=channels,
        sample_width=sample_width,
        sample_rate=sample_rate,
    )

    with pytest.raises(AvatarAudioClockError, match=message):
        inspect_frame_clock_wav(path)
