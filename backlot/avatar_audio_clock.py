"""Integer sample-clock helpers for audio-driven 25 FPS avatar workflows."""

from __future__ import annotations

import os
import wave
from pathlib import Path
from typing import Any


AVATAR_VIDEO_FPS = 25


class AvatarAudioClockError(ValueError):
    """The WAV cannot satisfy the deterministic avatar frame-clock contract."""


def inspect_frame_clock_wav(
    path: Path,
    *,
    fps: int = AVATAR_VIDEO_FPS,
    require_aligned: bool = False,
) -> dict[str, Any]:
    """Read integer PCM facts without deriving frames from rounded seconds."""
    if fps <= 0:
        raise AvatarAudioClockError("数字人视频帧率必须是正整数")
    try:
        with wave.open(str(path), "rb") as source:
            channels = source.getnchannels()
            sample_width = source.getsampwidth()
            sample_rate = source.getframerate()
            sample_frames = source.getnframes()
            compression = source.getcomptype()
    except (OSError, EOFError, wave.Error) as exc:
        raise AvatarAudioClockError(f"数字人驱动音频不是可读取的 PCM WAV：{path.name}") from exc
    if compression != "NONE" or channels != 1 or sample_width != 2:
        raise AvatarAudioClockError("数字人驱动音频必须是未压缩 PCM16 单声道 WAV")
    if sample_rate <= 0 or sample_frames <= 0:
        raise AvatarAudioClockError("数字人驱动音频没有有效采样率或采样数据")
    if sample_rate % fps:
        raise AvatarAudioClockError(
            f"数字人驱动音频采样率 {sample_rate}Hz 不能建立整数 {fps}FPS 采样时钟"
        )
    samples_per_video_frame = sample_rate // fps
    video_frame_count = (sample_frames + samples_per_video_frame - 1) // samples_per_video_frame
    aligned_sample_frames = video_frame_count * samples_per_video_frame
    padding = aligned_sample_frames - sample_frames
    if require_aligned and padding:
        raise AvatarAudioClockError(
            f"数字人驱动音频尚未对齐 {fps}FPS：还需补 {padding} 个零采样"
        )
    return {
        "channels": channels,
        "sample_width": sample_width,
        "sample_rate": sample_rate,
        "sample_frame_count": sample_frames,
        "samples_per_video_frame": samples_per_video_frame,
        "video_fps": fps,
        "video_frame_count": video_frame_count,
        "aligned_sample_frame_count": aligned_sample_frames,
        "required_padding_sample_frames": padding,
        "duration_seconds": sample_frames / sample_rate,
        "aligned_duration_seconds": video_frame_count / fps,
    }


def align_pcm_wav_to_frame_clock(
    path: Path,
    *,
    fps: int = AVATAR_VIDEO_FPS,
) -> dict[str, Any]:
    """Append silence once at the final WAV tail and atomically replace it."""
    before = inspect_frame_clock_wav(path, fps=fps)
    padding = int(before["required_padding_sample_frames"])
    content_sample_frames = int(before["sample_frame_count"])
    if padding:
        temporary = path.with_suffix(path.suffix + ".frame-clock.tmp")
        try:
            with wave.open(str(path), "rb") as source, wave.open(str(temporary), "wb") as output:
                output.setparams(source.getparams())
                while True:
                    audio = source.readframes(262_144)
                    if not audio:
                        break
                    output.writeframesraw(audio)
                output.writeframesraw(b"\0\0" * padding)
            os.replace(temporary, path)
        finally:
            try:
                temporary.unlink()
            except OSError:
                pass
    after = inspect_frame_clock_wav(path, fps=fps, require_aligned=True)
    if int(after["sample_frame_count"]) != int(before["aligned_sample_frame_count"]):
        raise AvatarAudioClockError("数字人驱动音频帧对齐后的采样数校验失败")
    return {
        **after,
        "content_sample_frames": content_sample_frames,
        "final_padding_sample_frames": padding,
    }


def nearest_video_frame(sample_frame: int, samples_per_video_frame: int) -> int:
    """Map one absolute sample boundary to a shared frame boundary, half-up."""
    if sample_frame < 0 or samples_per_video_frame <= 0:
        raise AvatarAudioClockError("音频边界采样数无效")
    return (2 * sample_frame + samples_per_video_frame) // (2 * samples_per_video_frame)
