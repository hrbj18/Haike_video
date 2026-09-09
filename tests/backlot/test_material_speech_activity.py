import hashlib
from pathlib import Path
import struct
import subprocess

import pytest

from backlot import material_speech_activity as vad


def _runner_with_samples(values):
    raw = b"".join(struct.pack("<f", value) for value in values)

    def run(command, **_kwargs):
        return subprocess.CompletedProcess(command, 0, raw, b"")

    return run


def test_runtime_identity_is_local_stable_and_hashed():
    capability = vad.capability()
    if capability["status"] != "available":
        pytest.skip(capability["error"])
    identity = capability["identity"]
    assert identity["engine"] == "faster-whisper/silero-vad-v6"
    assert len(identity["model_sha256"]) == 64
    assert identity["options"]["speech_pad_ms"] == 0
    assert identity == vad.runtime_identity()


def test_detect_maps_chunk_samples_back_to_source_time(tmp_path, monkeypatch):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"fixture")
    monkeypatch.setattr(vad, "runtime_identity", lambda *_a, **_k: {"signature": "local-vad"})
    calls = []

    def detector(audio, **_kwargs):
        calls.append(len(audio))
        return [{"start": 1600, "end": 4800}]

    result = vad.detect_speech_activity(
        source, ffmpeg="ffmpeg", start=10, end=12,
        runner=_runner_with_samples([0.0] * 16_000), detector=detector,
        options=vad.SpeechActivityOptions(chunk_seconds=10),
    )
    assert calls == [16_000]
    assert result["speech_ranges"] == [{"start": 10.1, "end": 10.3}]
    assert result["non_speech_ranges"] == [
        {"start": 10.0, "end": 10.1}, {"start": 10.3, "end": 12.0},
    ]


def test_overlapping_chunks_merge_without_duplicate_speech(tmp_path, monkeypatch):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"fixture")
    monkeypatch.setattr(vad, "runtime_identity", lambda *_a, **_k: {"signature": "local-vad"})

    result = vad.detect_speech_activity(
        source, ffmpeg="ffmpeg", start=0, end=12,
        runner=_runner_with_samples([0.0] * 16_000),
        detector=lambda *_a, **_k: [{"start": 0, "end": 16000}],
        options=vad.SpeechActivityOptions(chunk_seconds=5, chunk_overlap_seconds=1),
    )
    assert result["metadata"]["chunk_count"] == 3
    assert result["speech_ranges"] == [
        {"start": 0.0, "end": 1.0}, {"start": 4.0, "end": 5.0},
        {"start": 8.0, "end": 9.0},
    ]


@pytest.mark.parametrize("raw", [b"\x00", b"\x00\x00"])
def test_invalid_pcm_length_fails_safely(tmp_path, monkeypatch, raw):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"fixture")
    monkeypatch.setattr(vad, "runtime_identity", lambda *_a, **_k: {"signature": "local-vad"})
    runner = lambda command, **_kwargs: subprocess.CompletedProcess(command, 0, raw, b"")
    with pytest.raises(vad.SpeechActivityError, match="样本长度"):
        vad.detect_speech_activity(
            source, ffmpeg="ffmpeg", start=0, end=1, runner=runner,
            detector=lambda *_a, **_k: [],
        )


def test_decode_failure_does_not_create_or_modify_files(tmp_path, monkeypatch):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"fixture")
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    monkeypatch.setattr(vad, "runtime_identity", lambda *_a, **_k: {"signature": "local-vad"})
    runner = lambda command, **_kwargs: subprocess.CompletedProcess(command, 1, b"", b"bad")
    with pytest.raises(vad.SpeechActivityError, match="解码失败"):
        vad.detect_speech_activity(
            source, ffmpeg="ffmpeg", start=0, end=1, runner=runner,
            detector=lambda *_a, **_k: [],
        )
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
    assert list(tmp_path.iterdir()) == [source]


def test_no_audio_samples_is_explicit(tmp_path, monkeypatch):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"fixture")
    monkeypatch.setattr(vad, "runtime_identity", lambda *_a, **_k: {"signature": "local-vad"})
    result = vad.detect_speech_activity(
        source, ffmpeg="ffmpeg", start=0, end=1,
        runner=_runner_with_samples([]), detector=lambda *_a, **_k: [],
    )
    assert result["status"] == "no_audio_samples"
    assert result["speech_ranges"] == []
