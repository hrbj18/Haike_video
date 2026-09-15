from pathlib import Path
import shutil
import subprocess

import pytest

from backlot import material_audio_evidence as audio


def test_disabled_audio_never_calls_provider_and_does_not_require_identity(tmp_path):
    calls = []

    def provider(_source):
        calls.append(True)
        raise AssertionError("disabled audio must not call the provider")

    result = audio.resolve_audio_evidence(
        tmp_path / "source.mp4", duration=10, has_audio=True, recognize_audio=False,
        transcript_provider=provider,
    )
    assert result["status"] == "skipped" and result["policy"] == "disabled"
    assert result["utterances"] == [] and calls == []
    assert audio.cache_identity("fingerprint", recognize_audio=False, asr_identity=None)


def test_no_audio_skips_provider_even_when_recognition_requested(tmp_path):
    result = audio.resolve_audio_evidence(
        tmp_path / "source.mp4", duration=10, has_audio=False, recognize_audio=True,
        asr_identity="doubao-test", transcript_provider=lambda _: (_ for _ in ()).throw(AssertionError()),
    )
    assert result["status"] == "no_audio" and result["provider"] is None


def test_enabled_audio_normalizes_timestamped_utterances(tmp_path):
    result = audio.resolve_audio_evidence(
        tmp_path / "source.mp4", duration=10, has_audio=True, recognize_audio=True,
        asr_identity="doubao-test",
        transcript_provider=lambda _: ("你好世界", [
            {"start": 1, "end": 2, "text": "你好"},
            {"start": 3.5, "end": 4.1, "text": "世界"},
        ], {"timestamp_unit": "seconds", "secret": "must-not-be-copied"}),
    )
    assert result["status"] == "available"
    assert [row["id"] for row in result["utterances"]] == ["U00001", "U00002"]
    assert result["metadata"] == {"utterance_count": 2, "timestamp_unit": "seconds"}
    assert "secret" not in str(result)


def test_audio_policy_and_invalid_timestamps_are_rejected():
    with pytest.raises(audio.MaterialAudioEvidenceError, match="明确"):
        audio.audio_policy("yes")
    with pytest.raises(audio.MaterialAudioEvidenceError, match="倒序"):
        audio.normalize_utterances([{"start": 3, "end": 2, "text": "坏数据"}], 10)
    with pytest.raises(audio.MaterialAudioEvidenceError, match="冻结"):
        audio.cache_identity("fingerprint", recognize_audio=True, asr_identity=None)


def test_silencedetect_parser_offsets_bounded_interval():
    stderr = """
[silencedetect] silence_start: 0.25
[silencedetect] silence_end: 1.75 | silence_duration: 1.50
[silencedetect] silence_start: 2.4
"""
    assert audio.parse_silencedetect(stderr, range_start=5, range_end=8) == [
        {"start": 5.25, "end": 6.75}, {"start": 7.4, "end": 8.0},
    ]


def test_real_ffmpeg_silence_detection(tmp_path):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("FFmpeg unavailable")
    source = tmp_path / "tone-silence.wav"
    created = subprocess.run([
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "sine=frequency=600:sample_rate=48000:duration=0.5",
        "-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono:d=1",
        "-f", "lavfi", "-i", "sine=frequency=800:sample_rate=48000:duration=0.5",
        "-filter_complex", "[0:a][1:a][2:a]concat=n=3:v=0:a=1[out]", "-map", "[out]", str(source),
    ], capture_output=True, timeout=60)
    if created.returncode != 0:
        pytest.skip("Current FFmpeg cannot create the audio fixture")
    rows = audio.detect_silence(source, ffmpeg=ffmpeg, start=0, end=2, noise_db=-35, minimum_duration=.2)
    assert any(row["start"] <= .55 and row["end"] >= 1.45 for row in rows)


def test_audio_policy_is_specific_to_the_selected_transcript_engine(tmp_path):
    assert audio.audio_policy(True) == "doubao_transcript"
    assert audio.audio_policy(True, "doubao") == "doubao_transcript"
    assert audio.audio_policy(True, "tencent") == "tencent_transcript"
    assert audio.audio_policy(True, "local") == "local_transcript"
    assert audio.audio_policy(False, "tencent") == "disabled"
    assert audio.audio_policy(True, "TENCENT") == "tencent_transcript"
    assert set(audio.TRANSCRIPT_PROVIDERS) == {"doubao", "tencent", "local"}
    assert audio.POLICIES == {"disabled", "doubao_transcript", "tencent_transcript", "local_transcript"}


@pytest.mark.parametrize("value", ["openai", "gemini", "", None])
def test_unknown_transcript_engine_is_rejected_when_requested(value):
    if value in {"", None}:
        assert audio.transcript_policy(value) == "doubao_transcript"
        return
    with pytest.raises(audio.MaterialAudioEvidenceError, match="不支持的语音识别服务"):
        audio.transcript_policy(value)


def test_cache_identity_separates_engines_for_the_same_source():
    doubao = audio.cache_identity("same-source", recognize_audio=True, asr_identity="doubao-asr", provider="doubao")
    tencent = audio.cache_identity("same-source", recognize_audio=True, asr_identity="tencent-asr", provider="tencent")
    local = audio.cache_identity("same-source", recognize_audio=True, asr_identity="faster-whisper-local", provider="local")
    assert len({doubao, tencent, local}) == 3
    # Same engine and same frozen identity must stay stable so cached work is reused.
    assert tencent == audio.cache_identity("same-source", recognize_audio=True, asr_identity="tencent-asr", provider="tencent")


def test_tencent_evidence_records_its_own_policy(tmp_path):
    result = audio.resolve_audio_evidence(
        tmp_path / "source.mp4", duration=10, has_audio=True, recognize_audio=True,
        asr_identity="tencent-asr", provider="tencent",
        transcript_provider=lambda _: ("腾讯云转写", [
            {"start": 0, "end": 2.5, "text": "腾讯云转写"},
        ], {"timestamp_unit": "seconds"}),
    )
    assert result["policy"] == "tencent_transcript"
    assert result["status"] == "available" and result["provider"] == "tencent-asr"
    assert [row["text"] for row in result["utterances"]] == ["腾讯云转写"]


def test_policy_uses_transcript_only_for_real_transcript_policies():
    assert audio.policy_uses_transcript("doubao_transcript") is True
    assert audio.policy_uses_transcript("tencent_transcript") is True
    assert audio.policy_uses_transcript("local_transcript") is True
    assert audio.policy_uses_transcript("disabled") is False
    assert audio.policy_uses_transcript(None) is False
    assert audio.policy_uses_transcript("unknown_transcript") is False
