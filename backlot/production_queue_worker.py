"""Execution adapter for the durable Backlot production queue."""

from __future__ import annotations

import os
import socket
import threading
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from backlot.avatar_review_preview_pipeline import (
    read_avatar_review_preview_job,
    run_avatar_review_preview_job,
)
from backlot.production_queue import ProductionQueue, ProductionQueueConflict
from backlot.workbench import (
    generate_full_preview_render,
    mark_full_preview_render_failed,
    read_review_preview_job,
    read_workbench,
    run_review_preview_job,
    start_full_preview_render,
)


Handler = Callable[[dict[str, Any]], dict[str, Any]]


class ProductionQueueWorker:
    """Claim and execute at most one registered parent production job at a time."""

    def __init__(
        self,
        queue: ProductionQueue,
        projects_root: str | Path,
        *,
        handlers: dict[str, Handler] | None = None,
        worker_id: str | None = None,
        lease_seconds: int = 120,
    ):
        self.queue = queue
        self.projects_root = Path(projects_root)
        self.worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}:{uuid4().hex[:8]}"
        self.lease_seconds = max(30, int(lease_seconds))
        self.handlers = handlers or {
            "review_preview": self._run_review_preview,
            "avatar_review_preview": self._run_avatar_review_preview,
            "full_preview": self._run_full_preview,
        }

    def _project_dir(self, job: dict[str, Any]) -> Path:
        project_dir = (self.projects_root / str(job["project_id"])).resolve()
        root = os.path.normcase(str(self.projects_root.resolve()))
        try:
            inside_root = os.path.commonpath([root, os.path.normcase(str(project_dir))]) == root
        except ValueError:
            inside_root = False
        if not inside_root:
            raise ValueError("项目目录越界")
        if not project_dir.is_dir():
            raise ValueError("排队项目已经不存在")
        return project_dir

    @staticmethod
    def _error_contract(value: Any) -> tuple[dict[str, Any], bool, str | None]:
        if isinstance(value, dict):
            if not value:
                return {}, False, None
            message = str(value.get("message") or value.get("detail") or value.get("reason") or "任务失败")
            return (
                {"type": str(value.get("type") or "ProductionError"), "message": message[:1200]},
                bool(value.get("retryable")),
                str(value.get("safe_resume_point") or "") or None,
            )
        message = str(value or "任务失败")
        return {"type": "ProductionError", "message": message[:1200]}, False, None

    @classmethod
    def _normalize_parent(cls, state: dict[str, Any], *, fallback_stage: str) -> dict[str, Any]:
        status = str(state.get("status") or "failed")
        stage = str(state.get("stage") or fallback_stage)
        error, retryable, safe = cls._error_contract(state.get("error"))
        safe = str(state.get("safe_resume_point") or safe or "") or None
        result = state.get("result") if isinstance(state.get("result"), dict) else {}
        if status == "completed":
            return {"status": "completed", "stage": stage, "result": result}
        if status in {"awaiting_human", "ambiguous"}:
            return {
                "status": status, "stage": stage, "result": result, "error": error,
                "retryable": False, "safe_resume_point": safe,
            }
        if status in {"queued", "running", "generating", "rendering", "submitted", "downloading"}:
            return {
                "status": "failed", "stage": "底层任务未到达可确认终点",
                "error": {"type": "IncompleteParentJob", "message": "底层任务返回后仍在执行，请核对原任务"},
                "retryable": False, "safe_resume_point": safe,
            }
        return {
            "status": "failed", "stage": stage, "result": result, "error": error,
            "retryable": retryable, "safe_resume_point": safe,
        }

    def _run_review_preview(self, job: dict[str, Any]) -> dict[str, Any]:
        project_dir = self._project_dir(job)
        state = run_review_preview_job(project_dir, str(job["parent_job_id"]))
        return self._normalize_parent(state, fallback_stage="无数字人一键审核预览")

    def _run_avatar_review_preview(self, job: dict[str, Any]) -> dict[str, Any]:
        project_dir = self._project_dir(job)
        state = run_avatar_review_preview_job(project_dir, str(job["parent_job_id"]))
        return self._normalize_parent(state, fallback_stage="有数字人一键审核预览")

    def _run_full_preview(self, job: dict[str, Any]) -> dict[str, Any]:
        project_dir = self._project_dir(job)
        try:
            current = read_workbench(project_dir)
            preview = ((current.get("automation") or {}).get("preview_render") or {})
            if str(preview.get("status") or "") == "failed":
                current = start_full_preview_render(project_dir, job.get("frozen_request") or {"confirmed": True})
                preview = ((current.get("automation") or {}).get("preview_render") or {})
            if str(preview.get("status") or "") not in {"completed", "preview_ready"}:
                current = generate_full_preview_render(project_dir)
                preview = ((current.get("automation") or {}).get("preview_render") or {})
        except Exception as exc:  # noqa: BLE001 - keep the project child state truthful
            try:
                mark_full_preview_render_failed(project_dir, exc)
            except Exception:
                pass
            raise
        status = str(preview.get("status") or "failed")
        if status in {"completed", "preview_ready"}:
            return {
                "status": "completed", "stage": "等待人工观看",
                "result": {
                    "output_path": preview.get("output_path"),
                    "version": preview.get("version"),
                },
            }
        error, retryable, safe = self._error_contract(preview.get("error"))
        return {
            "status": "failed", "stage": "合成全片审核预览", "error": error,
            "retryable": retryable, "safe_resume_point": safe or "full_preview",
        }

    def inspect_parent(self, job: dict[str, Any]) -> dict[str, Any]:
        project_dir = self._project_dir(job)
        if job["kind"] == "review_preview":
            return self._normalize_parent(read_review_preview_job(project_dir), fallback_stage="无数字人一键审核预览")
        if job["kind"] == "avatar_review_preview":
            return self._normalize_parent(read_avatar_review_preview_job(project_dir), fallback_stage="有数字人一键审核预览")
        state = read_workbench(project_dir)
        preview = ((state.get("automation") or {}).get("preview_render") or {})
        status = str(preview.get("status") or "idle")
        if status in {"completed", "preview_ready"}:
            return {
                "status": "completed", "stage": "等待人工观看",
                "result": {"output_path": preview.get("output_path"), "version": preview.get("version")},
            }
        if status in {"generating", "running", "rendering"}:
            return {"status": "ambiguous", "stage": "底层合成仍标记运行，等待核对"}
        if status == "failed":
            error, retryable, safe = self._error_contract(preview.get("error"))
            return {"status": "failed", "stage": "合成失败", "error": error, "retryable": retryable, "safe_resume_point": safe or "full_preview"}
        return {"status": "queued", "stage": "等待从底层安全点恢复"}

    def reconcile_expired(self) -> list[dict[str, Any]]:
        reconciled: list[dict[str, Any]] = []
        for job in self.queue.expired_running():
            try:
                outcome = self.inspect_parent(job)
            except Exception as exc:  # noqa: BLE001 - recovery must preserve the queue record
                outcome = {
                    "status": "failed", "stage": "无法核对遗留生产任务",
                    "error": {"type": type(exc).__name__, "message": str(exc)[:1200]},
                    "retryable": False,
                }
            reconciled.append(self.queue.recover_expired(job["job_id"], **outcome))
        return reconciled

    def run_once(self) -> dict[str, Any] | None:
        self.reconcile_expired()
        job = self.queue.claim_next(self.worker_id, lease_seconds=self.lease_seconds)
        if job is None:
            return None
        stop = threading.Event()

        def renew() -> None:
            interval = max(5.0, self.lease_seconds / 3)
            while not stop.wait(interval):
                try:
                    self.queue.heartbeat(job["job_id"], self.worker_id, lease_seconds=self.lease_seconds)
                except ProductionQueueConflict:
                    return

        heartbeat = threading.Thread(target=renew, name=f"queue-heartbeat-{job['job_id']}", daemon=True)
        heartbeat.start()
        try:
            self.queue.update_stage(job["job_id"], self.worker_id, "正在执行统一生产任务")
            handler = self.handlers.get(str(job["kind"]))
            if handler is None:
                raise ValueError("生产类型没有注册执行器")
            outcome = handler(job)
            if not isinstance(outcome, dict):
                raise TypeError("生产执行器没有返回状态合同")
            return self.queue.finish(job["job_id"], self.worker_id, **outcome)
        except Exception as exc:  # noqa: BLE001 - queue records a durable, user-visible failure
            return self.queue.finish(
                job["job_id"], self.worker_id,
                status="failed", stage="统一生产任务执行失败",
                error={"type": type(exc).__name__, "message": str(exc)[:1200]},
                retryable=False,
            )
        finally:
            stop.set()
            heartbeat.join(timeout=1)
