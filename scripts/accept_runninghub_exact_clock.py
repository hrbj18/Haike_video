"""One-task paid acceptance for the deployed InfiniteTalk exact-clock graph.

The manifest is an idempotency ledger: once submission starts without a saved
task id, the run becomes ambiguous and this script refuses to submit again.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backlot.avatar_audio_clock import inspect_frame_clock_wav
from tools.avatar.runninghub_avatar import (
    INFINITETALK_448X560_EXACT_CLOCK_PROFILE,
    INFINITETALK_448X560_EXACT_CLOCK_TEMPLATE_PATH,
    INFINITETALK_448X560_EXACT_CLOCK_WORKFLOW_ID,
    RunningHubLongCatClient,
    _validate_infinitetalk_448x560_exact_clock_template,
)


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _print(value: dict[str, Any]) -> None:
    print(json.dumps(value, ensure_ascii=True), flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="RunningHub 精确帧工作流单任务付费验收")
    parser.add_argument("--image", type=Path, required=True)
    parser.add_argument("--audio", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--max-budget-cny", type=float, default=5.0)
    parser.add_argument("--poll-seconds", type=float, default=20.0)
    parser.add_argument("--timeout-seconds", type=float, default=7_200.0)
    parser.add_argument("--submit", action="store_true", help="显式允许创建唯一一个 Standard 24GB 任务")
    args = parser.parse_args()

    if not args.submit:
        raise SystemExit("缺少 --submit；只读预检不会创建付费任务")
    if not 0 < args.max_budget_cny <= 5.0:
        raise SystemExit("本次验收预算必须大于0且不超过5元")
    image = args.image.resolve()
    audio = args.audio.resolve()
    if not image.is_file() or not audio.is_file():
        raise SystemExit("验收人物图或音频不存在")
    api_template = REPO_ROOT / INFINITETALK_448X560_EXACT_CLOCK_TEMPLATE_PATH
    template_sha256 = _validate_infinitetalk_448x560_exact_clock_template(api_template)
    clock = inspect_frame_clock_wav(audio, require_aligned=True)
    image_sha256 = _sha256(image)
    audio_sha256 = _sha256(audio)
    contract = {
        "workflow_id": INFINITETALK_448X560_EXACT_CLOCK_WORKFLOW_ID,
        "workflow_profile": INFINITETALK_448X560_EXACT_CLOCK_PROFILE,
        "workflow_api_sha256": template_sha256,
        "instance_type": "default",
        "instance_label": "Standard 24GB",
        "image_sha256": image_sha256,
        "audio_sha256": audio_sha256,
        "sample_rate": int(clock["sample_rate"]),
        "sample_frame_count": int(clock["sample_frame_count"]),
        "samples_per_video_frame": int(clock["samples_per_video_frame"]),
        "video_fps": int(clock["video_fps"]),
        "exact_total_frames": int(clock["video_frame_count"]),
        "expected_duration_seconds": float(clock["aligned_duration_seconds"]),
        "max_budget_cny": float(args.max_budget_cny),
        "max_paid_submissions": 1,
        "automatic_retry": False,
        "plus_allowed": False,
    }
    contract_hash = hashlib.sha256(
        json.dumps(contract, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = output_dir / "acceptance-ledger.json"
    output_path = output_dir / f"runninghub-exact-clock-{int(clock['video_frame_count'])}f.mp4"
    manifest: dict[str, Any] = {}
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if str(manifest.get("contract_hash") or "") != contract_hash:
            raise SystemExit("验收目录已有不同输入合同；拒绝覆盖或混用付费任务账本")
    else:
        manifest = {
            "schema_version": 1,
            "operation_id": f"runninghub-acceptance:{INFINITETALK_448X560_EXACT_CLOCK_WORKFLOW_ID}:default:{contract_hash[:20]}:a1",
            "contract_hash": contract_hash,
            "contract": contract,
            "state": "planned",
            "paid_submission_count": 0,
            "reserved_cny": 0.0,
            "spent_cny": 0.0,
            "task_id": None,
            "history": [{"at": _now(), "to": "planned"}],
        }
        _write_json(manifest_path, manifest)

    state = str(manifest.get("state") or "")
    if state == "succeeded" and output_path.is_file():
        _print({"state": state, "task_id": manifest.get("task_id"), "output": str(output_path)})
        return 0
    if state in {"ambiguous", "failed"}:
        raise SystemExit(f"验收任务当前为 {state}；禁止自动创建第二个付费任务")
    if state == "submitting" and not manifest.get("task_id"):
        manifest["state"] = "ambiguous"
        manifest["history"].append({"at": _now(), "from": "submitting", "to": "ambiguous"})
        _write_json(manifest_path, manifest)
        raise SystemExit("上次提交已开始但没有保存任务号；状态不明，禁止自动重提")

    client = RunningHubLongCatClient(
        workflow_id=INFINITETALK_448X560_EXACT_CLOCK_WORKFLOW_ID,
        workflow_profile=INFINITETALK_448X560_EXACT_CLOCK_PROFILE,
    )
    task_id = str(manifest.get("task_id") or "")
    if not task_id:
        if int(manifest.get("paid_submission_count") or 0) >= 1:
            raise SystemExit("验收账本已经用完唯一一次付费提交额度")
        manifest.update({"state": "reserved", "reserved_cny": float(args.max_budget_cny)})
        manifest["history"].append({"at": _now(), "from": state, "to": "reserved", "amount_cny": float(args.max_budget_cny)})
        _write_json(manifest_path, manifest)
        image_remote = client.upload_file(image, file_type="image")
        audio_remote = client.upload_file(audio, file_type="audio")
        manifest.update({"state": "submitting", "paid_submission_count": 1})
        manifest["history"].append({"at": _now(), "from": "reserved", "to": "submitting"})
        _write_json(manifest_path, manifest)
        try:
            submitted = client.submit(
                presenter_filename=image_remote,
                audio_filename=audio_remote,
                instance_type="default",
                exact_total_frames=int(clock["video_frame_count"]),
            )
        except Exception as exc:
            manifest.update({"state": "ambiguous", "error": str(exc)[:500]})
            manifest["history"].append({"at": _now(), "from": "submitting", "to": "ambiguous"})
            _write_json(manifest_path, manifest)
            raise
        task_id = str(submitted["task_id"])
        manifest.update({"state": "submitted", "task_id": task_id})
        manifest["history"].append({"at": _now(), "from": "submitting", "to": "submitted", "task_id": task_id})
        _write_json(manifest_path, manifest)
        _print({"state": "submitted", "task_id": task_id, "exact_total_frames": clock["video_frame_count"]})

    deadline = time.monotonic() + max(1.0, float(args.timeout_seconds))
    while time.monotonic() < deadline:
        try:
            result = client.poll(task_id)
        except Exception as exc:
            manifest.update({"state": "running", "last_poll_error": str(exc)[:500]})
            _write_json(manifest_path, manifest)
            _print({"state": "running", "task_id": task_id, "poll": "interrupted_same_task_only"})
            time.sleep(max(1.0, float(args.poll_seconds)))
            continue
        status = str(result.get("status") or "UNKNOWN")
        if status == "RUNNING":
            if manifest.get("state") != "running":
                manifest.update({"state": "running"})
                manifest["history"].append({"at": _now(), "to": "running", "task_id": task_id})
                _write_json(manifest_path, manifest)
            _print({"state": "running", "task_id": task_id})
            time.sleep(max(1.0, float(args.poll_seconds)))
            continue
        spent = float(result.get("consume_money_cny") or 0.0)
        billing = result.get("billing") if isinstance(result.get("billing"), dict) else {}
        manifest.update({
            "reserved_cny": 0.0,
            "spent_cny": spent,
            "billing": billing,
            "finished_at": _now(),
        })
        if status == "SUCCEEDED" and result.get("video_url"):
            client.download(str(result["video_url"]), output_path)
            manifest.update({
                "state": "succeeded",
                "output_path": output_path.name,
                "output_sha256": _sha256(output_path),
            })
            manifest["history"].append({"at": _now(), "to": "succeeded", "task_id": task_id, "spent_cny": spent})
            _write_json(manifest_path, manifest)
            _print({"state": "succeeded", "task_id": task_id, "spent_cny": spent, "output": str(output_path)})
            return 0 if spent <= float(args.max_budget_cny) + 1e-9 else 3
        manifest.update({"state": "failed", "error": str(result.get("error") or "RunningHub task failed")[:500]})
        manifest["history"].append({"at": _now(), "to": "failed", "task_id": task_id, "spent_cny": spent})
        _write_json(manifest_path, manifest)
        _print({"state": "failed", "task_id": task_id, "spent_cny": spent, "error": manifest["error"]})
        return 1

    manifest.update({"state": "running", "last_wait_timeout_at": _now()})
    _write_json(manifest_path, manifest)
    _print({"state": "running", "task_id": task_id, "wait": "timed_out_same_task_resumable"})
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
