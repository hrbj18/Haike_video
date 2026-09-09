"""Contract tests for the explicitly selected Doubao ASR provider."""

from __future__ import annotations

import base64
from urllib.parse import parse_qs, urlparse

import pytest

from backlot import doubao_asr as asr


class _Response:
    def __init__(self, status: str, payload=None, *, http_status: int = 200, message: str = "OK", content: bytes = b""):
        self.headers = {"X-Api-Status-Code": status, "X-Api-Message": message}
        self.status_code = http_status
        self._payload = payload
        self.content = content

    def json(self):
        if self._payload is None:
            raise ValueError("empty body")
        return self._payload


class _Requests:
    def __init__(self):
        self.calls = []

    def post(self, url, *, headers, json, timeout):
        self.calls.append((url, headers, json, timeout))
        if url.endswith("/submit"):
            return _Response("20000000")
        return _Response("20000000", {
            "result": {
                "text": "机器鸭正在行走。",
                "utterances": [
                    {"text": "机器鸭正在行走。", "start_time": 120, "end_time": 1580},
                ],
            },
        })

    def get(self, _url, *, timeout):
        self.calls.append(("GET", {}, {}, timeout))
        return _Response("", http_status=200, content=b"short-official-mp3")


def test_flash_direct_upload_uses_base64_and_normalizes_timestamps(monkeypatch):
    monkeypatch.setenv("DOUBAO_ASR_API_KEY", "asr-test-key-never-log")
    monkeypatch.setenv("DOUBAO_ASR_MODE", "flash")
    fake = _Requests()
    submitting = []

    text, segments, metadata = asr.transcribe_audio_bytes_flash(
        b"synthetic-mp3-bytes",
        on_submitting=submitting.append,
        requests_module=fake,
    )

    assert text == "机器鸭正在行走。"
    assert segments == [{"start": 0.12, "end": 1.58, "text": "机器鸭正在行走。"}]
    assert metadata["provider"] == "doubao-asr-1.0-flash"
    assert metadata["resource_id"] == "volc.bigasr.auc_turbo"
    assert submitting == [metadata["request_id"]]
    url, headers, body, _timeout = fake.calls[0]
    assert url == asr.FLASH_URL
    assert headers["X-Api-Resource-Id"] == "volc.bigasr.auc_turbo"
    assert headers["X-Api-Sequence"] == "-1"
    assert base64.b64decode(body["audio"]["data"]) == b"synthetic-mp3-bytes"
    assert "url" not in body["audio"]


def test_submit_query_normalizes_timestamped_transcript_and_never_sends_raw_codec(monkeypatch):
    monkeypatch.setenv("DOUBAO_ASR_API_KEY", "asr-test-key-never-log")
    monkeypatch.delenv("DOUBAO_SPEECH_API_KEY", raising=False)
    fake = _Requests()
    accepted = []

    text, segments, metadata = asr.transcribe_url(
        "https://media.example.test/sample.mp3",
        audio_format="mp3",
        poll_interval_seconds=0,
        on_accepted=accepted.append,
        requests_module=fake,
    )

    assert text == "机器鸭正在行走。"
    assert segments == [{"start": 0.12, "end": 1.58, "text": "机器鸭正在行走。"}]
    assert metadata["provider"] == "doubao-asr-2.0"
    assert metadata["resource_id"] == "volc.seedasr.auc"
    assert accepted == [metadata["request_id"]]
    submit_url, headers, body, _timeout = fake.calls[0]
    assert submit_url == asr.SUBMIT_URL
    assert headers["X-Api-Key"] == "asr-test-key-never-log"
    assert headers["X-Api-Resource-Id"] == "volc.seedasr.auc"
    assert headers["X-Api-Sequence"] == "-1"
    assert body["audio"] == {
        "url": "https://media.example.test/sample.mp3", "format": "mp3", "language": "zh-CN",
    }
    assert body["request"]["show_utterances"] is True
    assert "codec" not in body["audio"]


def test_submit_transport_failure_is_ambiguous_and_never_auto_retries(monkeypatch):
    monkeypatch.setenv("DOUBAO_ASR_API_KEY", "asr-test-key-never-log")

    class BrokenRequests:
        def post(self, *_args, **_kwargs):
            raise TimeoutError("connection timeout")

    with pytest.raises(asr.DoubaoASRAmbiguous, match="没有自动重提"):
        asr.transcribe_url(
            "https://media.example.test/sample.mp3",
            audio_format="mp3",
            requests_module=BrokenRequests(),
        )


def test_flash_transport_failure_is_ambiguous_and_checkpoints_one_uuid(monkeypatch):
    monkeypatch.setenv("DOUBAO_ASR_API_KEY", "asr-test-key-never-log")
    submitted = []

    class BrokenRequests:
        def post(self, *_args, **_kwargs):
            raise TimeoutError("connection timeout")

    with pytest.raises(asr.DoubaoASRAmbiguous, match="没有自动重提") as raised:
        asr.transcribe_audio_bytes_flash(
            b"synthetic-mp3-bytes",
            on_submitting=submitted.append,
            requests_module=BrokenRequests(),
        )

    assert submitted == [raised.value.request_id]


def test_asr_entitlement_error_has_a_actionable_chinese_remediation(monkeypatch):
    monkeypatch.setenv("DOUBAO_ASR_API_KEY", "asr-test-key-never-log")

    class MissingGrantRequests:
        def post(self, *_args, **_kwargs):
            return _Response("45000030", {"message": "requested resource not granted"}, message="requested resource not granted")

    with pytest.raises(asr.DoubaoASRError, match="录音文件识别模型 2.0.*volc.seedasr.auc"):
        asr.transcribe_url(
            "https://media.example.test/sample.mp3",
            audio_format="mp3",
            requests_module=MissingGrantRequests(),
        )


def test_signed_project_audio_url_is_short_lived_and_confined_to_asr_artifacts(monkeypatch, tmp_path):
    monkeypatch.setenv("DOUBAO_ASR_API_KEY", "asr-test-key-never-log")
    monkeypatch.setenv("BACKLOT_PUBLIC_BASE_URL", "https://video.example.test")
    monkeypatch.setenv("DOUBAO_ASR_MEDIA_SIGNING_SECRET", "signing-secret-never-log")
    project = tmp_path / "film"
    audio = project / "artifacts" / "asr" / "S-001" / "sample.mp3"
    audio.parent.mkdir(parents=True)
    audio.write_bytes(b"not-a-real-mp3-but-route-safe")

    url = asr.build_signed_project_audio_url("film", project, audio, ttl_seconds=300)
    query = parse_qs(urlparse(url).query)
    resolved = asr.resolve_signed_project_audio(
        "film", project, query["path"][0], int(query["expires_at"][0]), query["signature"][0],
    )

    assert resolved == audio.resolve()
    with pytest.raises(asr.DoubaoASRError, match="签名无效"):
        asr.resolve_signed_project_audio("film", project, query["path"][0], int(query["expires_at"][0]), "tampered")
    private_path = "assets/private.mp3"
    valid_but_outside_signature = asr._sign("film", private_path, int(query["expires_at"][0]), "signing-secret-never-log")
    with pytest.raises(asr.DoubaoASRError, match="越出允许目录"):
        asr.resolve_signed_project_audio("film", project, private_path, int(query["expires_at"][0]), valid_but_outside_signature)


def test_config_can_reuse_existing_doubao_speech_key_without_exposing_it(monkeypatch):
    monkeypatch.delenv("DOUBAO_ASR_API_KEY", raising=False)
    monkeypatch.setenv("DOUBAO_SPEECH_API_KEY", "speech-key-never-log")
    monkeypatch.delenv("BACKLOT_PUBLIC_BASE_URL", raising=False)
    monkeypatch.delenv("DOUBAO_ASR_MEDIA_SIGNING_SECRET", raising=False)

    config = asr.read_doubao_asr_config()

    assert config["configured"] is True
    assert config["api_key_masked"] != "speech-key-never-log"
    assert config["provider"] == "doubao-asr-1.0-flash"
    assert config["resource_id"] == "volc.bigasr.auc_turbo"
    assert config["direct_base64_upload"] is True
    assert config["project_media_ready"] is True


def test_standard_url_mode_still_requires_public_url_and_signing_secret(monkeypatch):
    monkeypatch.setenv("DOUBAO_ASR_API_KEY", "asr-test-key-never-log")
    monkeypatch.setenv("DOUBAO_ASR_MODE", "standard_url")
    monkeypatch.delenv("BACKLOT_PUBLIC_BASE_URL", raising=False)
    monkeypatch.delenv("DOUBAO_ASR_MEDIA_SIGNING_SECRET", raising=False)

    config = asr.read_doubao_asr_config()

    assert config["provider"] == "doubao-asr-2.0"
    assert config["project_media_ready"] is False


def test_project_provider_uses_flash_file_without_public_url(monkeypatch, tmp_path):
    monkeypatch.setenv("DOUBAO_ASR_API_KEY", "asr-test-key-never-log")
    monkeypatch.setenv("DOUBAO_ASR_MODE", "flash")
    monkeypatch.delenv("BACKLOT_PUBLIC_BASE_URL", raising=False)
    monkeypatch.delenv("DOUBAO_ASR_MEDIA_SIGNING_SECRET", raising=False)
    source = tmp_path / "source.mp4"
    source.write_bytes(b"video")
    extracted = tmp_path / "audio.mp3"
    extracted.write_bytes(b"audio")
    monkeypatch.setattr(asr, "_to_cloud_audio", lambda *_args: extracted)
    calls = []

    def fake_flash(path, *, on_submitting=None, requests_module=None, request_id=None):
        calls.append((path, on_submitting, request_id))
        return "文本", [{"start": 0.0, "end": 1.0, "text": "文本"}], {"provider": "doubao-asr-1.0-flash"}

    monkeypatch.setattr(asr, "transcribe_file_flash", fake_flash)
    checkpoint = []
    provider = asr.create_project_transcript_provider(
        project_id="film",
        project_dir=tmp_path,
        asset_id="S-001",
        ffmpeg="ffmpeg",
        on_submitting=checkpoint.append,
    )

    text, segments, metadata = provider(source)

    assert text == "文本"
    assert segments[0]["end"] == 1.0
    assert metadata["provider"] == "doubao-asr-1.0-flash"
    assert calls == [(extracted, checkpoint.append, None)]
