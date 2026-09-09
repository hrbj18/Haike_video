"""Idempotently transcribe the reference video for mihoyo-ai-girlfriend-remake-1."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backlot.doubao_asr import DoubaoASRAmbiguous, create_project_transcript_provider


PROJECT_ID = "mihoyo-ai-girlfriend-remake-1"
PROJECT = ROOT / "projects" / PROJECT_ID
ASSET_ID = "S-001"
TASK_PATH = PROJECT / "artifacts" / "asr" / ASSET_ID / "task.json"
RESULT_PATH = PROJECT / "artifacts" / "asr" / ASSET_ID / "transcription.json"


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temporary_name, path)
    finally:
        temporary = Path(temporary_name)
        if temporary.exists():
            temporary.unlink()


def now() -> str:
    return datetime.now(UTC).isoformat()


def main() -> None:
    if RESULT_PATH.is_file():
        print(json.dumps({"status": "reused", "result": str(RESULT_PATH)}, ensure_ascii=False))
        return
    if TASK_PATH.is_file():
        prior = json.loads(TASK_PATH.read_text(encoding="utf-8"))
        if prior.get("status") in {"submitting", "ambiguous"} and prior.get("request_id"):
            raise SystemExit(
                "既有豆包ASR请求可能已受理但没有成功结果；禁止自动重复提交："
                + str(prior["request_id"])
            )

    state = json.loads((PROJECT / "artifacts" / "workbench.json").read_text(encoding="utf-8"))
    asset = next(item for item in state.get("assets", []) if item.get("id") == ASSET_ID)
    source = (PROJECT / asset["path"]).resolve()
    digest = hashlib.sha256(source.read_bytes()).hexdigest()

    def checkpoint(request_id: str) -> None:
        write_json(TASK_PATH, {
            "version": 1,
            "status": "submitting",
            "provider": "doubao-asr-1.0-flash",
            "resource_id": "volc.bigasr.auc_turbo",
            "request_id": request_id,
            "asset_id": ASSET_ID,
            "source_sha256": digest,
            "started_at": now(),
        })

    provider = create_project_transcript_provider(
        project_id=PROJECT_ID,
        project_dir=PROJECT,
        asset_id=ASSET_ID,
        ffmpeg="ffmpeg",
        on_submitting=checkpoint,
    )
    try:
        text, segments, metadata = provider(source)
    except DoubaoASRAmbiguous as exc:
        prior = json.loads(TASK_PATH.read_text(encoding="utf-8")) if TASK_PATH.is_file() else {}
        prior.update({"status": "ambiguous", "error": str(exc), "finished_at": now()})
        write_json(TASK_PATH, prior)
        raise
    write_json(RESULT_PATH, {
        "version": 1,
        "asset_id": ASSET_ID,
        "source_sha256": digest,
        "text": text,
        "segments": segments,
        "metadata": metadata,
        "created_at": now(),
    })
    prior = json.loads(TASK_PATH.read_text(encoding="utf-8"))
    prior.update({
        "status": "completed",
        "finished_at": now(),
        "utterance_count": len(segments),
        "result_path": RESULT_PATH.relative_to(PROJECT).as_posix(),
    })
    write_json(TASK_PATH, prior)
    print(json.dumps({
        "status": "completed",
        "request_id": metadata.get("request_id"),
        "provider": metadata.get("provider"),
        "utterance_count": len(segments),
        "text": text,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
