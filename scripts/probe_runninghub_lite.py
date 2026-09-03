"""Submit exactly one minimal RunningHub Lite compatibility probe.

This diagnostic never retries and never falls back to Standard or Plus.  It
persists enough billing evidence to decide whether an explicit ``lite`` value
is safe for production without placing the probe output in the formal asset
tree.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backlot.ai_text import _effective_value, _read_env_file, _secrets_path
from tools.avatar.runninghub_avatar import RunningHubLongCatClient


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", type=Path, required=True)
    parser.add_argument("--audio", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--workflow-id")
    parser.add_argument("--workflow-profile")
    parser.add_argument(
        "--request-mode",
        choices=("omit_instance_type",),
        default="omit_instance_type",
    )
    parser.add_argument("--max-recorded-cost-cny", type=float, default=0.25)
    parser.add_argument("--poll-seconds", type=float, default=10.0)
    parser.add_argument("--timeout-seconds", type=float, default=3600.0)
    args = parser.parse_args()

    if not args.image.is_file() or not args.audio.is_file():
        raise SystemExit("探测输入文件不存在")

    _, values = _read_env_file(_secrets_path())
    api_key = _effective_value("RUNNINGHUB_API_KEY", values)
    workflow_id = str(args.workflow_id or _effective_value("RUNNINGHUB_WORKFLOW_ID", values)).strip()
    workflow_profile = str(args.workflow_profile or _effective_value("RUNNINGHUB_WORKFLOW_PROFILE", values)).strip()
    base_url = _effective_value("RUNNINGHUB_BASE_URL", values) or "https://www.runninghub.cn"
    if not api_key or not workflow_id:
        raise SystemExit("RunningHub API Key 或 workflowId 尚未配置")

    output_dir = args.output_dir.resolve()
    manifest_path = output_dir / "probe.json"
    image_sha256 = hashlib.sha256(args.image.read_bytes()).hexdigest()
    audio_sha256 = hashlib.sha256(args.audio.read_bytes()).hexdigest()
    existing: dict[str, Any] = {}
    if manifest_path.is_file():
        try:
            loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
            existing = loaded if isinstance(loaded, dict) else {}
        except (OSError, json.JSONDecodeError):
            existing = {}
    manifest: dict[str, Any] = {
        "version": "1.0",
        "provider": "runninghub",
        "workflow_id_suffix": workflow_id[-6:],
        "workflow_profile": workflow_profile or None,
        "image_sha256": image_sha256,
        "audio_sha256": audio_sha256,
        "requested_instance_type": None,
        "request_mode": args.request_mode,
        "instance_type_field_present": False,
        "single_submission_only": True,
        "fallback_allowed": False,
        "max_recorded_cost_cny": args.max_recorded_cost_cny,
        "status": "preparing",
        "created_at": _now(),
    }

    client = RunningHubLongCatClient(
        api_key=api_key,
        workflow_id=workflow_id,
        base_url=base_url,
        workflow_profile=workflow_profile or None,
    )
    resumable = (
        existing.get("status") in {"submitted", "running", "timeout"}
        and existing.get("task_id")
        and existing.get("request_mode") == args.request_mode
        and existing.get("workflow_id_suffix") == workflow_id[-6:]
        and existing.get("workflow_profile") == (workflow_profile or None)
        and existing.get("image_sha256") == image_sha256
        and existing.get("audio_sha256") == audio_sha256
    )
    if resumable:
        manifest = existing
        task_id = str(existing["task_id"])
        print(json.dumps({"status": "resuming", "task_id": task_id}, ensure_ascii=False), flush=True)
    else:
        _write_json(manifest_path, manifest)
        image_remote = client.upload_file(args.image.resolve(), file_type="image")
        audio_remote = client.upload_file(args.audio.resolve(), file_type="audio")
        submitted = client.submit(
            presenter_filename=image_remote,
            audio_filename=audio_remote,
            instance_type=None,
        )
        task_id = str(submitted["task_id"])
        manifest.update({
            "status": "submitted",
            "task_id": task_id,
            "submitted_at": _now(),
            "request_contract": submitted.get("request_contract"),
        })
        _write_json(manifest_path, manifest)
        print(json.dumps({"status": "submitted", "task_id": task_id}, ensure_ascii=False), flush=True)

    deadline = time.monotonic() + args.timeout_seconds
    while time.monotonic() < deadline:
        try:
            result = client.poll(task_id)
        except Exception as exc:
            manifest.update({"status": "running", "last_poll_error": str(exc)[:500], "last_polled_at": _now()})
            _write_json(manifest_path, manifest)
            time.sleep(args.poll_seconds)
            continue
        if result.get("status") == "RUNNING":
            time.sleep(args.poll_seconds)
            continue

        billing = result.get("billing") if isinstance(result.get("billing"), dict) else {}
        usage = billing.get("provider_usage") if isinstance(billing.get("provider_usage"), dict) else {}
        cost = usage.get("consume_money")
        observed = str(billing.get("observed_instance") or "unverified")
        manifest.update({
            "status": str(result.get("status") or "FAILED").lower(),
            "finished_at": _now(),
            "observed_instance": observed,
            "observed_hourly_rate_cny": billing.get("observed_hourly_rate_cny"),
            "provider_usage": usage,
            "within_recorded_budget": isinstance(cost, (int, float)) and cost <= args.max_recorded_cost_cny,
            "lite_verified": result.get("status") == "SUCCEEDED" and observed == "lite",
            "error": result.get("error"),
        })
        if result.get("status") == "SUCCEEDED" and result.get("video_url"):
            target = output_dir / f"probe-{task_id}.mp4"
            client.download(str(result["video_url"]), target)
            manifest["output_path"] = str(target)
        _write_json(manifest_path, manifest)
        print(json.dumps({
            "status": manifest["status"],
            "task_id": task_id,
            "observed_instance": observed,
            "observed_hourly_rate_cny": manifest.get("observed_hourly_rate_cny"),
            "consume_money_cny": cost,
            "lite_verified": manifest["lite_verified"],
        }, ensure_ascii=False), flush=True)
        return 0 if manifest["lite_verified"] else 2

    manifest.update({"status": "timeout", "finished_at": _now(), "lite_verified": False})
    _write_json(manifest_path, manifest)
    print(json.dumps({"status": "timeout", "task_id": task_id}, ensure_ascii=False), flush=True)
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
