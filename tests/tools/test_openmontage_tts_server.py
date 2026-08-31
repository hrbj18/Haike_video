from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from tools.audio.openmontage_tts_server import create_app
from tools.audio.openmontage_tts_engine import IdempotencyConflict, JobLedgerError


VOICE_SIGNATURE = "a" * 64


class _Store:
    def public_profiles(self):
        return [{
            "id": "yaya", "name": "雅雅", "voice_type": "cloned",
            "available": True, "voice_signature": VOICE_SIGNATURE,
        }]


class _Runtime:
    loaded_models = ["base"]
    store = _Store()


class _Jobs:
    def __init__(self, root: Path):
        self.data_dir = root
        self.runtime = _Runtime()
        self.job = {
            "id": "job-1", "status": "completed", "duration": 1.0,
            "request_id": "project.line.attempt-1", "voice_signature": VOICE_SIGNATURE,
        }
        self.output_file("job-1").parent.mkdir(parents=True, exist_ok=True)
        self.output_file("job-1").write_bytes(b"RIFFtest")

    def submit(self, payload):
        assert payload["profile"] == "yaya"
        assert payload["request_id"] == "project.line.attempt-1"
        assert payload["voice_signature"] == VOICE_SIGNATURE
        return {"id": "job-1", "status": "queued", "request_id": payload["request_id"], "voice_signature": payload["voice_signature"]}

    def status(self, job_id):
        if job_id != "job-1":
            raise KeyError(job_id)
        return self.job

    def output_file(self, job_id):
        return self.data_dir / f"{job_id}.wav"


def test_compatibility_api_lists_profiles_submits_and_downloads(tmp_path):
    client = TestClient(create_app(jobs=_Jobs(tmp_path)))

    profiles = client.get("/profiles")
    submitted = client.post("/speak", json={
        "text": "测试", "profile": "yaya",
        "request_id": "project.line.attempt-1", "voice_signature": VOICE_SIGNATURE,
    })
    status = client.get("/generate/job-1/status")
    audio = client.get("/audio/job-1")

    assert profiles.json()[0]["name"] == "雅雅"
    assert submitted.status_code == 200
    assert submitted.json()["id"] == "job-1"
    assert submitted.json()["request_id"] == "project.line.attempt-1"
    assert submitted.json()["voice_signature"] == VOICE_SIGNATURE
    assert status.json()["status"] == "completed"
    assert audio.content == b"RIFFtest"


def test_audio_is_not_exposed_before_completion(tmp_path):
    jobs = _Jobs(tmp_path)
    jobs.job["status"] = "generating"
    client = TestClient(create_app(jobs=jobs))

    response = client.get("/audio/job-1")

    assert response.status_code == 409


def test_schema_and_idempotency_conflicts_have_distinct_status_codes(tmp_path):
    jobs = _Jobs(tmp_path)
    client = TestClient(create_app(jobs=jobs))

    invalid = client.post("/speak", json={
        "text": "测试", "profile": "yaya", "request_id": "bad request id",
        "voice_signature": VOICE_SIGNATURE,
    })
    jobs.submit = lambda _payload: (_ for _ in ()).throw(IdempotencyConflict("同键异输入"))
    conflict = client.post("/speak", json={
        "text": "测试", "profile": "yaya", "request_id": "project.line.attempt-1",
        "voice_signature": VOICE_SIGNATURE,
    })

    assert invalid.status_code == 422
    assert conflict.status_code == 409
    assert "同键异输入" in conflict.json()["detail"]

    jobs.status = lambda _job_id: (_ for _ in ()).throw(JobLedgerError("任务账本损坏，禁止覆盖"))
    corrupt = client.get("/generate/job-1/status")
    assert corrupt.status_code == 409
    assert "禁止覆盖" in corrupt.json()["detail"]
