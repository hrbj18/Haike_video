"""Paid A/B probe for 48GB-instance InfiniteTalk tuning.

This diagnostic submits the *same published* workflow
(``2094449979141218305`` / ``infinitetalk_448x560_exact_clock_v2``) once per
variant, differing only in ``nodeInfoList`` field overrides.  That keeps the
comparison honest: identical presenter image, identical frame-aligned driving
audio, identical seed, identical instance type.  Only the graph parameters under
investigation change.

It never retries a submission (an ambiguous paid call must not be duplicated),
persists evidence after every step, and refuses to exceed a hard CNY budget.
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

import requests

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backlot.ai_text import _effective_value, _read_env_file, _secrets_path
from backlot.avatar_audio_clock import inspect_frame_clock_wav
from tools.avatar.runninghub_avatar import (
    INFINITETALK_448X560_EXACT_CLOCK_WORKFLOW_ID,
    billing_evidence,
)

DEFAULT_BUDGET_CNY = 2.90
RESERVE_PER_RUN_CNY = 0.45

# Round 2: the user imported the BlackwellKernel deliverable as a *separate*
# published workflow. That makes a true two-workflowId A/B possible — the same
# accusation ("you tested one workflow 10 times") can no longer apply.
KERNEL_WORKFLOW_ID = "2100142690905190402"  # user-imported sageattn_3 build

# Variant = ordered list of nodeInfoList overrides appended after the base ones.
# "33" is WanVideoBlockSwap, "11" is WanVideoModelLoader, "14" is the
# InfiniteTalk long-video conditioning node, "19" is WanVideoDecode.
# A variant may also pin its own ``workflow_id`` (round 2).
VARIANTS: dict[str, dict[str, Any]] = {
    # ---- round 2: untouched graphs, two distinct published workflowIds ------
    "prod_v2": {
        "label": "生产基线：2141图 / sageattn / swap=8（未覆写任何参数）",
        "overrides": [],
    },
    "kernel_v1": {
        "label": "候选：新导入图 / sageattn_3 / swap=0（未覆写任何参数）",
        "overrides": [],
        "workflow_id": KERNEL_WORKFLOW_ID,
    },
    # ---- round 1 variants (same-workflowId override channel, already proven) -
    "v2_orig": {
        "label": "V2 原版（现用生产配置）",
        "overrides": [],
    },
    "gpt_48g": {
        "label": "GPT-5.6 48GB极速版（只关掉 block swap）",
        "overrides": [
            {"nodeId": "33", "fieldName": "blocks_to_swap", "fieldValue": 0},
            {"nodeId": "33", "fieldName": "use_non_blocking", "fieldValue": False},
            {"nodeId": "33", "fieldName": "prefetch_blocks", "fieldValue": 0},
        ],
    },
    "mine_safe": {
        "label": "自研 A：48GB 常驻（去 swap + 主模型常驻显存）",
        "overrides": [
            {"nodeId": "33", "fieldName": "blocks_to_swap", "fieldValue": 0},
            {"nodeId": "33", "fieldName": "use_non_blocking", "fieldValue": True},
            {"nodeId": "33", "fieldName": "prefetch_blocks", "fieldValue": 1},
            {"nodeId": "11", "fieldName": "load_device", "fieldValue": "main_device"},
        ],
    },
    # Canary: an out-of-range value on the exact node GPT edited. If the override
    # channel honors node 33, the task MUST fail on that node; if the channel
    # silently drops node-33 overrides, the task succeeds normally. This is the
    # only cheap discriminator, because a *valid* swap value is timing-invisible.
    "canary_swap_bogus": {
        "label": "哨兵：node 33 blocks_to_swap=9999（越界，用来判定覆写是否真生效）",
        "overrides": [
            {"nodeId": "33", "fieldName": "blocks_to_swap", "fieldValue": 9999},
        ],
    },
    # Canary: out-of-range value on the sampler's ``steps`` field. Prove the
    # sampler — not just the block-swap node — really receives our overrides
    # before spending money on a 3-step timing run.
    "canary_steps_bogus": {
        "label": "哨兵：node 13 steps=99999（越界，判定采样器覆写是否生效）",
        "overrides": [
            {"nodeId": "13", "fieldName": "steps", "fieldValue": 99999},
        ],
    },
    # The only lever that removes compute linearly: 4 -> 3 sampling steps.
    # Everything else in this graph is already compute-bound.
    "fast3step": {
        "label": "自研 C：3 步采样（唯一线性降算力的旋钮）",
        "overrides": [
            {"nodeId": "33", "fieldName": "blocks_to_swap", "fieldValue": 0},
            {"nodeId": "33", "fieldName": "use_non_blocking", "fieldValue": True},
            {"nodeId": "33", "fieldName": "prefetch_blocks", "fieldValue": 1},
            {"nodeId": "11", "fieldName": "load_device", "fieldValue": "main_device"},
            {"nodeId": "13", "fieldName": "steps", "fieldValue": 3},
        ],
    },
    "mine_fp8": {
        "label": "自研 B：A + 主模型 fp8_e4m3fn 量化",
        "overrides": [
            {"nodeId": "33", "fieldName": "blocks_to_swap", "fieldValue": 0},
            {"nodeId": "33", "fieldName": "use_non_blocking", "fieldValue": True},
            {"nodeId": "33", "fieldName": "prefetch_blocks", "fieldValue": 1},
            {"nodeId": "11", "fieldName": "load_device", "fieldValue": "main_device"},
            {"nodeId": "11", "fieldName": "quantization", "fieldValue": "fp8_e4m3fn"},
        ],
    },
}


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


def _log(message: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {message}", flush=True)


class Probe:
    def __init__(
        self,
        *,
        budget_cny: float,
        work_dir: Path,
        reserve_cny: float = RESERVE_PER_RUN_CNY,
        instance_type: str = "plus",
    ) -> None:
        self.reserve_cny = float(reserve_cny)
        self.instance_type = str(instance_type or "plus").strip().lower()
        _, values = _read_env_file(_secrets_path())
        self.api_key = _effective_value("RUNNINGHUB_API_KEY", values)
        self.base_url = (_effective_value("RUNNINGHUB_BASE_URL", values) or "https://www.runninghub.cn").rstrip("/")
        self.workflow_id = (
            _effective_value("RUNNINGHUB_WORKFLOW_ID", values) or INFINITETALK_448X560_EXACT_CLOCK_WORKFLOW_ID
        )
        if not self.api_key:
            raise SystemExit("RUNNINGHUB_API_KEY 未配置")
        self.budget_cny = float(budget_cny)
        self.work_dir = work_dir
        self.state_path = work_dir / "probe.json"
        self.state = _read_json(self.state_path) or {
            "version": "1.0",
            "created_at": _now(),
            "workflow_id": self.workflow_id,
            "profile": "infinitetalk_448x560_exact_clock_v2",
            "budget_cny": self.budget_cny,
            "spent_cny": 0.0,
            "uploads": {},
            "runs": [],
        }
        self.session = requests.Session()
        self.uploaded: dict[str, str] = dict(self.state.get("uploads") or {})

    # ---------------------------------------------------------------- transport

    def _post(self, path: str, payload: dict[str, Any], *, timeout: int = 90) -> dict[str, Any]:
        response = self.session.post(
            f"{self.base_url}{path}",
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            json=payload,
            timeout=timeout,
        )
        try:
            body = response.json()
        except ValueError as exc:
            raise RuntimeError(f"{path} 返回非 JSON（HTTP {response.status_code}）") from exc
        if not response.ok:
            raise RuntimeError(f"{path} HTTP {response.status_code}: {json.dumps(body, ensure_ascii=False)[:400]}")
        return body

    def upload(self, path: Path) -> str:
        key = _sha256(path)
        cached = self.uploaded.get(key)
        if cached:
            _log(f"复用已上传 {path.name} -> {cached}")
            return cached
        with path.open("rb") as source:
            response = self.session.post(
                f"{self.base_url}/openapi/v2/media/upload/binary",
                headers={"Authorization": f"Bearer {self.api_key}"},
                files={"file": (path.name, source)},
                timeout=300,
            )
        payload = response.json()
        if not response.ok or payload.get("code") not in (None, 0, 200):
            raise RuntimeError(f"上传失败：{json.dumps(payload, ensure_ascii=False)[:400]}")
        data = payload.get("data")
        filename = data.get("fileName") if isinstance(data, dict) else data
        if not filename:
            raise RuntimeError(f"上传成功但未返回文件名：{json.dumps(payload, ensure_ascii=False)[:300]}")
        self.uploaded[key] = str(filename)
        self.state["uploads"] = self.uploaded
        _write_json(self.state_path, self.state)
        _log(f"上传 {path.name} -> {filename}")
        return str(filename)

    # ------------------------------------------------------------------- budget

    @property
    def spent(self) -> float:
        return round(sum(float(r.get("cost_cny") or 0.0) for r in self.state["runs"]), 4)

    def _assert_budget(self) -> None:
        if self.spent + self.reserve_cny > self.budget_cny + 1e-9:
            raise SystemExit(
                f"预算不足：已花 {self.spent:.3f} 元，下一轮需预留 {self.reserve_cny:.2f} 元，上限 {self.budget_cny:.2f} 元"
            )

    # ---------------------------------------------------------------------- run

    def run_once(
        self,
        *,
        variant: str,
        image_name: str,
        audio_name: str,
        total_frames: int,
        audio_path: Path,
    ) -> dict[str, Any]:
        spec = VARIANTS[variant]
        self._assert_budget()
        workflow_id = str(spec.get("workflow_id") or self.workflow_id)
        node_info_list: list[dict[str, Any]] = [
            {"nodeId": "36", "fieldName": "image", "fieldValue": image_name},
            {"nodeId": "34", "fieldName": "audio", "fieldValue": audio_name},
            {"nodeId": "35", "fieldName": "value", "fieldValue": total_frames},
            {"nodeId": "24", "fieldName": "trim_to_audio", "fieldValue": True},
        ] + [dict(item) for item in spec["overrides"]]
        record: dict[str, Any] = {
            "run_id": f"{variant}-{len([r for r in self.state['runs'] if r['variant'] == variant]) + 1}",
            "variant": variant,
            "label": spec["label"],
            "overrides": spec["overrides"],
            "node_info_list": node_info_list,
            "workflow_id": workflow_id,
            "instance_type": self.instance_type,
            "audio_path": str(audio_path),
            "audio_sha256": _sha256(audio_path),
            "total_frames": total_frames,
            "submitted_at": _now(),
            "task_id": None,
            "status": "SUBMITTING",
        }
        body = {
            "apiKey": self.api_key,
            "workflowId": workflow_id,
            "nodeInfoList": node_info_list,
            "instanceType": self.instance_type,
            "addMetadata": True,
        }
        started = time.monotonic()
        _log(f"提交 {record['run_id']}（{spec['label']}）")
        payload = self._post("/task/openapi/create", body)
        data = payload.get("data")
        task_id = (data.get("taskId") if isinstance(data, dict) else None) or payload.get("taskId")
        if not task_id:
            record["status"] = "SUBMIT_FAILED"
            record["error"] = json.dumps(payload, ensure_ascii=False)[:600]
            record["wall_seconds"] = round(time.monotonic() - started, 1)
            record["cost_cny"] = 0.0
            self.state["runs"].append(record)
            _write_json(self.state_path, self.state)
            _log(f"  !! 提交失败：{record['error']}")
            return record
        record["task_id"] = str(task_id)
        record["status"] = "RUNNING"
        self.state["runs"].append(record)
        _write_json(self.state_path, self.state)
        _log(f"  task_id={task_id}")

        deadline = started + 3600
        while time.monotonic() < deadline:
            time.sleep(10)
            try:
                result = self._post("/openapi/v2/query", {"taskId": str(task_id)}, timeout=60)
            except Exception as exc:  # transient query failure must not duplicate work
                _log(f"  查询异常（继续等待）：{exc}")
                continue
            source = result.get("data") if isinstance(result.get("data"), dict) else result
            status = str(
                source.get("status") or source.get("taskStatus") or source.get("state") or "RUNNING"
            ).upper()
            if status in {"SUCCESS", "SUCCEEDED", "COMPLETED", "FINISH", "FINISHED"}:
                record["status"] = "SUCCEEDED"
                record["provider_raw"] = result
                results = source.get("results") or source.get("outputs") or []
                for item in results if isinstance(results, list) else []:
                    url = str(item.get("url") or item.get("fileUrl") or "")
                    if str(item.get("nodeId") or "") == "24" and url:
                        record["video_url"] = url
                if not record.get("video_url"):
                    for item in results if isinstance(results, list) else []:
                        url = str(item.get("url") or item.get("fileUrl") or "")
                        if url.lower().split("?")[0].endswith(".mp4"):
                            record["video_url"] = url
                            break
                break
            if status in {"FAILED", "FAIL", "ERROR", "CANCELED", "CANCELLED"}:
                record["status"] = "FAILED"
                record["provider_raw"] = result
                failed = source.get("failedReason") if isinstance(source.get("failedReason"), dict) else {}
                record["failure"] = {
                    "error_code": source.get("errorCode"),
                    "error_message": str(source.get("errorMessage") or "")[:500],
                    "exception_type": str(failed.get("exception_type") or "")[:200],
                    "node_name": str(failed.get("node_name") or failed.get("node_id") or "")[:200],
                    "exception_message": str(failed.get("exception_message") or "")[:800],
                }
                break
        else:
            record["status"] = "TIMEOUT"

        record["wall_seconds"] = round(time.monotonic() - started, 1)
        record["finished_at"] = _now()
        billing = billing_evidence(record.get("provider_raw") or {})
        record["billing"] = billing
        record["cost_cny"] = billing["provider_usage"].get("consume_money")
        record["gpu_seconds"] = billing["provider_usage"].get("task_cost_seconds")
        self.state["runs"] = [r for r in self.state["runs"] if r["run_id"] != record["run_id"]]
        self.state["runs"].append(record)
        self.state["spent_cny"] = self.spent
        _write_json(self.state_path, self.state)

        if record.get("video_url"):
            target = self.work_dir / "out" / f"{record['run_id']}.mp4"
            try:
                self.download(record["video_url"], target)
                record["output_path"] = str(target)
                record["output_sha256"] = _sha256(target)
                record["output_bytes"] = target.stat().st_size
            except Exception as exc:
                record["download_error"] = str(exc)[:300]
            self.state["runs"] = [r for r in self.state["runs"] if r["run_id"] != record["run_id"]]
            self.state["runs"].append(record)
            self.state["spent_cny"] = self.spent
            _write_json(self.state_path, self.state)

        _log(
            f"  -> {record['status']}  墙钟 {record['wall_seconds']}s  "
            f"计费 {record.get('cost_cny')} 元 / {record.get('gpu_seconds')} GPU秒  累计 {self.spent:.3f} 元"
        )
        return record

    def download(self, url: str, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(target.suffix + ".download")
        response = self.session.get(url, stream=True, timeout=600)
        if not response.ok:
            raise RuntimeError(f"下载失败 HTTP {response.status_code}")
        with temporary.open("wb") as output:
            for chunk in response.iter_content(chunk_size=1 << 20):
                if chunk:
                    output.write(chunk)
        os.replace(temporary, target)


def main() -> int:
    parser = argparse.ArgumentParser(description="InfiniteTalk 48GB 机型工作流变体付费对比")
    parser.add_argument("--variant", choices=sorted(VARIANTS), required=True)
    parser.add_argument("--image", type=Path, required=True)
    parser.add_argument("--audio", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, default=REPO_ROOT / ".backlot" / "_diag" / "it48g")
    parser.add_argument("--budget-cny", type=float, default=DEFAULT_BUDGET_CNY)
    parser.add_argument("--reserve-cny", type=float, default=RESERVE_PER_RUN_CNY)
    parser.add_argument(
        "--instance-type",
        default="plus",
        choices=["plus", "default"],
        help="plus=48GB ¥6/h，default=24GB ¥4/h",
    )
    args = parser.parse_args()

    image = args.image.resolve()
    audio = args.audio.resolve()
    for path in (image, audio):
        if not path.is_file():
            raise SystemExit(f"输入不存在：{path}")
    clock = inspect_frame_clock_wav(audio, fps=25, require_aligned=True)
    probe = Probe(
        budget_cny=args.budget_cny,
        work_dir=args.work_dir.resolve(),
        reserve_cny=args.reserve_cny,
        instance_type=args.instance_type,
    )
    image_name = probe.upload(image)
    audio_name = probe.upload(audio)
    probe.state["inputs"] = {
        "image": {"path": str(image), "sha256": _sha256(image), "remote": image_name},
        "audio": {
            "path": str(audio),
            "sha256": _sha256(audio),
            "remote": audio_name,
            "duration_seconds": clock["duration_seconds"],
            "video_frame_count": clock["video_frame_count"],
        },
    }
    _write_json(probe.state_path, probe.state)
    record = probe.run_once(
        variant=args.variant,
        image_name=image_name,
        audio_name=audio_name,
        total_frames=int(clock["video_frame_count"]),
        audio_path=audio,
    )
    print(json.dumps({
        "run_id": record["run_id"],
        "variant": record["variant"],
        "instance_type": record.get("instance_type"),
        "status": record["status"],
        "wall_seconds": record.get("wall_seconds"),
        "gpu_seconds": record.get("gpu_seconds"),
        "cost_cny": record.get("cost_cny"),
        "spent_total_cny": probe.spent,
        "output": record.get("output_path"),
        "failure": record.get("failure"),
    }, ensure_ascii=False))
    return 0 if record["status"] == "SUCCEEDED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
