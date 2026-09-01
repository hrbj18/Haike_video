"""Local HTTP service for the Haike Video embedded Qwen3-TTS runtime."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from tools.audio.haike_video_tts_engine import (
    IdempotencyConflict,
    JobLedgerError,
    ProfileError,
    ProfileSignatureMismatch,
    SerialTTSJobs,
    data_dir_from_env,
    runtime_dependencies,
)


class SpeakRequest(BaseModel):
    text: str = Field(min_length=1, max_length=20000)
    profile: str | None = None
    profile_id: str | None = None
    language: str = "zh"
    engine: str | None = None
    model_size: str | None = None
    instruct: str = ""
    personality: bool = False
    normalize: bool = True
    request_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=200,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$",
    )
    voice_signature: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


def create_app(data_dir: Path | None = None, jobs: SerialTTSJobs | None = None) -> FastAPI:
    service_jobs = jobs or SerialTTSJobs(data_dir or data_dir_from_env())
    app = FastAPI(title="Haike Video Local TTS", version="1.0.0")
    app.state.tts_jobs = service_jobs

    @app.get("/health")
    def health() -> dict[str, Any]:
        dependencies = runtime_dependencies()
        missing = [name for name, available in dependencies.items() if not available]
        return {
            "status": "healthy" if not missing else "degraded",
            "service": "haike_video-local-tts",
            "version": "1.0.0",
            "dependencies": dependencies,
            "missing_dependencies": missing,
            "loaded_models": service_jobs.runtime.loaded_models,
            "data_dir": str(service_jobs.data_dir),
        }

    @app.get("/profiles")
    def profiles() -> list[dict[str, Any]]:
        return service_jobs.runtime.store.public_profiles()

    def submit(request: SpeakRequest) -> dict[str, Any]:
        payload = request.model_dump()
        payload["profile"] = request.profile or request.profile_id
        try:
            return service_jobs.submit(payload)
        except (IdempotencyConflict, JobLedgerError, ProfileSignatureMismatch) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (ValueError, ProfileError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/speak")
    def speak(request: SpeakRequest) -> dict[str, Any]:
        return submit(request)

    @app.post("/generate")
    def generate(request: SpeakRequest) -> dict[str, Any]:
        return submit(request)

    def read_status(job_id: str) -> dict[str, Any]:
        try:
            return service_jobs.status(job_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="配音任务不存在") from exc
        except JobLedgerError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/generate/{job_id}/status")
    def generation_status(job_id: str) -> dict[str, Any]:
        return read_status(job_id)

    @app.get("/history/{job_id}")
    def history_status(job_id: str) -> dict[str, Any]:
        return read_status(job_id)

    @app.get("/audio/{job_id}")
    def audio(job_id: str) -> FileResponse:
        job = read_status(job_id)
        output = service_jobs.output_file(job_id)
        if job.get("status") != "completed" or not output.is_file():
            raise HTTPException(status_code=409, detail="配音音频尚未完成")
        return FileResponse(output, media_type="audio/wav", filename=f"{job_id}.wav")

    return app


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the Haike Video local TTS service")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=17494)
    parser.add_argument("--data-dir", type=Path)
    args = parser.parse_args()
    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        parser.error("The embedded TTS service may only bind to localhost")
    if args.data_dir:
        os.environ["HAIKE_VIDEO_TTS_DATA_DIR"] = str(args.data_dir.resolve())
    import uvicorn

    uvicorn.run(create_app(args.data_dir), host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
