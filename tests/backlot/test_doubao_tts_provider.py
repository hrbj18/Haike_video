"""Doubao Speech 2.0 request, polling and redaction contracts."""

from __future__ import annotations

from pathlib import Path

import requests

from tools.audio.doubao_tts import DoubaoTTS


class _Response:
    def __init__(self, payload=None, *, content=b"", status_code=200):
        self.payload = payload
        self.content = content
        self.status_code = status_code

    def json(self):
        return self.payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def test_doubao_submit_poll_download_and_timing_metadata(tmp_path, monkeypatch):
    key = "test-key-never-log"
    monkeypatch.setenv("DOUBAO_SPEECH_API_KEY", key)
    calls = []

    def fake_post(url, *, headers, json, timeout):
        calls.append((url, headers, json))
        if url.endswith("/submit"):
            return _Response({"code": 20000000, "data": {"task_id": "task-1"}})
        return _Response({
            "code": 20000000,
            "data": {
                "task_id": "task-1",
                "task_status": 2,
                "audio_url": "https://audio.invalid/result.mp3",
                "sentences": [{"text": "测试。", "words": [{"word": "测试", "start_time": 0, "end_time": 500}]}],
                "usage": {"text_words": 3},
            },
        })

    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr(requests, "get", lambda url, timeout: _Response(content=b"mp3-data"))
    monkeypatch.setattr(DoubaoTTS, "_audio_duration", staticmethod(lambda path: 1.25))
    output = tmp_path / "sample.mp3"
    metadata = tmp_path / "sample.json"

    result = DoubaoTTS().execute({
        "text": "测试。",
        "voice_id": "voice-1",
        "output_path": str(output),
        "metadata_path": str(metadata),
        "poll_interval_seconds": 0,
    })

    assert result.success
    assert output.read_bytes() == b"mp3-data"
    assert result.data["task_id"] == "task-1"
    assert result.data["sentences"][0]["words"][0]["word"] == "测试"
    assert calls[0][1]["X-Api-Key"] == key
    assert calls[0][1]["X-Api-Resource-Id"] == "seed-tts-2.0"
    assert calls[0][2]["req_params"]["speaker"] == "voice-1"
    assert "explicit_language" not in calls[0][2]["req_params"]["additions"]


def test_doubao_errors_redact_api_key(monkeypatch):
    key = "test-key-never-log"
    monkeypatch.setenv("DOUBAO_SPEECH_API_KEY", key)

    def fail(self, inputs, *, api_key, voice_id):
        raise RuntimeError(f"request failed with {api_key}")

    monkeypatch.setattr(DoubaoTTS, "_generate", fail)
    result = DoubaoTTS().execute({"text": "测试", "voice_id": "voice-1"})

    assert not result.success
    assert key not in str(result.error)
    assert "[redacted]" in str(result.error)
