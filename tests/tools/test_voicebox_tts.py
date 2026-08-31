"""Voicebox API compatibility tests."""

from __future__ import annotations

import io
import wave
from pathlib import Path

import pytest

from tools.audio.voicebox_tts import TTSProtocolError, TTSServiceUnavailable, VoiceboxTTS
from tools.base_tool import ToolStatus


VOICE_SIGNATURE = "a" * 64


def _wav_bytes() -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(24000)
        wav.writeframes(b"\x00\x00" * 2400)
    return output.getvalue()


def test_voicebox_prefers_modern_speak_api_and_uses_profile_id(tmp_path, monkeypatch):
    calls: list[tuple[str, str, dict | None]] = []

    def fake_request(cls, method, path, body=None, timeout=30):
        calls.append((method, path, body))
        if path == "/profiles":
            return [{
                "id": "voice-yaya", "name": "雅雅", "language": "zh",
                "voice_type": "cloned", "default_engine": "qwen",
                "available": True, "voice_signature": VOICE_SIGNATURE,
            }]
        if path == "/speak":
            return {"id": "gen-1", "status": "generating"}
        if path == "/generate/gen-1/status":
            return {"id": "gen-1", "status": "completed", "duration": 1.2}
        if path == "/audio/gen-1":
            return _wav_bytes()
        raise AssertionError(f"unexpected Voicebox request: {method} {path}")

    monkeypatch.setattr(VoiceboxTTS, "get_status", classmethod(lambda cls: ToolStatus.AVAILABLE))
    monkeypatch.setattr(VoiceboxTTS, "_request", classmethod(fake_request))
    output = tmp_path / "preview.wav"

    result = VoiceboxTTS().execute({"text": "这是一段测试。", "profile_id": "voice-yaya", "output_path": str(output), "poll_seconds": 0.01})

    assert result.success is True
    assert output.read_bytes() == _wav_bytes()
    speak_payload = next(body for method, path, body in calls if method == "POST" and path == "/speak")
    assert speak_payload["profile"] == "voice-yaya"
    assert speak_payload["engine"] == "qwen"
    assert all(path != "/generate" for _, path, _ in calls)
    assert result.data["api_mode"] == "speak"


def test_recoverable_submit_requires_exact_id_and_voice_signature_echo(monkeypatch):
    calls: list[tuple[str, str, dict | None]] = []

    def fake_request(cls, method, path, body=None, timeout=30):
        calls.append((method, path, body))
        if path == "/profiles":
            return [{
                "id": "voice-yaya", "name": "雅雅", "language": "zh",
                "voice_type": "cloned", "default_engine": "qwen",
                "available": True, "voice_signature": VOICE_SIGNATURE,
            }]
        if path == "/speak":
            return {
                "id": "gen-2", "status": "queued",
                "request_id": body["request_id"], "voice_signature": body["voice_signature"],
            }
        raise AssertionError(path)

    monkeypatch.setattr(VoiceboxTTS, "_request", classmethod(fake_request))
    submitted = VoiceboxTTS.submit(
        {"text": "第一句。", "profile_id": "voice-yaya"},
        request_id="project.line.attempt-1",
    )

    assert submitted["generation_id"] == "gen-2"
    assert submitted["request_id"] == "project.line.attempt-1"
    payload = next(body for method, path, body in calls if method == "POST" and path == "/speak")
    assert payload["voice_signature"] == VOICE_SIGNATURE
    assert payload["request_id"] == "project.line.attempt-1"


def test_query_is_single_snapshot_and_rejects_protocol_drift(monkeypatch):
    calls = 0

    def fake_request(cls, method, path, body=None, timeout=30):
        nonlocal calls
        calls += 1
        return {"id": "gen-3", "status": "generating", "request_id": "r3"}

    monkeypatch.setattr(VoiceboxTTS, "_request", classmethod(fake_request))
    assert VoiceboxTTS.query("gen-3")["status"] == "generating"
    assert calls == 1

    monkeypatch.setattr(
        VoiceboxTTS,
        "_request",
        classmethod(lambda cls, method, path, body=None, timeout=30: {"id": "other", "status": "completed"}),
    )
    with pytest.raises(TTSProtocolError, match="任务 ID"):
        VoiceboxTTS.query("gen-3")


def test_download_is_atomic_and_malformed_wav_does_not_overwrite(monkeypatch, tmp_path):
    destination = tmp_path / "line.wav"
    destination.write_bytes(b"old-audio")

    def fake_request(cls, method, path, body=None, timeout=30):
        if path.endswith("/status"):
            return {"id": "gen-4", "status": "completed"}
        if path == "/audio/gen-4":
            return b"RIFFbroken"
        raise AssertionError(path)

    monkeypatch.setattr(VoiceboxTTS, "_request", classmethod(fake_request))
    with pytest.raises(TTSProtocolError, match="WAV"):
        VoiceboxTTS.download("gen-4", destination)

    assert destination.read_bytes() == b"old-audio"
    assert not list(tmp_path.glob(".*.tmp"))


def test_post_transport_timeout_is_classified_as_ambiguous_and_same_key_safe(monkeypatch):
    class _TimeoutOpener:
        def open(self, *_args, **_kwargs):
            raise TimeoutError("timed out after server may have accepted request")

    monkeypatch.setattr(VoiceboxTTS, "_base_url", classmethod(lambda cls: "http://127.0.0.1:17494"))
    monkeypatch.setattr("tools.audio.voicebox_tts.urllib.request.build_opener", lambda *_args: _TimeoutOpener())

    with pytest.raises(TTSServiceUnavailable) as caught:
        VoiceboxTTS._request("POST", "/speak", {"request_id": "stable-attempt"})

    assert caught.value.ambiguous_after_submit is True
    assert caught.value.same_request_id_safe is True
    assert "相同 request_id" in str(caught.value)
