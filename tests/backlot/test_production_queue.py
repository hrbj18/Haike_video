from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from backlot.production_queue import ProductionQueue, ProductionQueueConflict


def _project(root: Path, project_id: str, title: str | None = None) -> None:
    project_dir = root / project_id
    project_dir.mkdir(parents=True)
    (project_dir / "project.json").write_text(
        json.dumps({"id": project_id, "title": title or project_id}, ensure_ascii=False),
        encoding="utf-8",
    )


def _queue(tmp_path: Path) -> tuple[ProductionQueue, Path]:
    projects = tmp_path / "projects"
    _project(projects, "alpha", "甲项目")
    _project(projects, "beta", "乙项目")
    return ProductionQueue(tmp_path / "state" / "queue.sqlite3", projects), projects


def _submit(
    queue: ProductionQueue,
    *,
    project_id: str,
    parent_job_id: str,
    priority: str = "normal",
    kind: str = "review_preview",
    source: str = "workbench",
    key: str | None = None,
) -> dict:
    return queue.submit(
        project_id=project_id,
        kind=kind,
        source=source,
        priority=priority,
        parent_job_id=parent_job_id,
        frozen_request={"confirmed": True},
        idempotency_key=key,
    )


def test_submit_is_idempotent_and_public_payload_redacts_secrets(tmp_path: Path) -> None:
    queue, _ = _queue(tmp_path)
    first = queue.submit(
        project_id="alpha",
        kind="review_preview",
        source="codex",
        priority="normal",
        parent_job_id="RP-1",
        frozen_request={
            "confirmed": True,
            "api_key": "must-not-leak",
            "nested": {"authorization": "must-not-leak", "budget": 5},
        },
        idempotency_key="stable-request",
    )
    second = queue.submit(
        project_id="alpha",
        kind="review_preview",
        source="codex",
        priority="normal",
        parent_job_id="RP-1",
        frozen_request={"confirmed": True},
        idempotency_key="stable-request",
    )

    assert first["job_id"] == second["job_id"]
    assert len(queue.list()["tasks"]) == 1
    assert "must-not-leak" not in json.dumps(queue.get(first["job_id"]), ensure_ascii=False)
    private = queue.get(first["job_id"], include_private=True)
    assert private["frozen_request"] == {"confirmed": True, "nested": {"budget": 5}}


def test_priority_then_fifo_and_single_global_execution_lease(tmp_path: Path) -> None:
    queue, _ = _queue(tmp_path)
    normal = _submit(queue, project_id="alpha", parent_job_id="RP-normal", priority="normal")
    background = _submit(queue, project_id="beta", parent_job_id="RP-background", priority="background")
    priority = _submit(
        queue,
        project_id="beta",
        parent_job_id="PRJ-priority",
        priority="priority",
        kind="full_preview",
    )

    claimed = queue.claim_next("worker-one")
    assert claimed and claimed["job_id"] == priority["job_id"]
    # A second process must not claim a different job while one global lane is occupied.
    assert queue.claim_next("worker-two") is None
    queue.finish(priority["job_id"], "worker-one", status="completed", stage="完成")

    claimed = queue.claim_next("worker-two")
    assert claimed and claimed["job_id"] == normal["job_id"]
    queue.finish(normal["job_id"], "worker-two", status="completed", stage="完成")
    claimed = queue.claim_next("worker-two")
    assert claimed and claimed["job_id"] == background["job_id"]


def test_concurrent_claim_never_grants_two_execution_slots(tmp_path: Path) -> None:
    queue, _ = _queue(tmp_path)
    _submit(queue, project_id="alpha", parent_job_id="RP-1")
    _submit(queue, project_id="beta", parent_job_id="RP-2")
    barrier = threading.Barrier(3)
    results: list[dict | None] = []

    def claim(worker: str) -> None:
        barrier.wait()
        results.append(queue.claim_next(worker))

    threads = [threading.Thread(target=claim, args=(f"worker-{index}",)) for index in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()

    assert sum(result is not None for result in results) == 1


def test_pause_resume_cancel_retry_and_ambiguous_guard(tmp_path: Path) -> None:
    queue, _ = _queue(tmp_path)
    paused = _submit(queue, project_id="alpha", parent_job_id="RP-pause")
    assert queue.pause(paused["job_id"])["status"] == "paused"
    assert queue.resume(paused["job_id"])["status"] == "queued"
    assert queue.cancel(paused["job_id"])["status"] == "cancelled"

    failed = _submit(queue, project_id="alpha", parent_job_id="RP-retry", key="retryable")
    claimed = queue.claim_next("worker")
    assert claimed and claimed["job_id"] == failed["job_id"]
    queue.finish(
        failed["job_id"],
        "worker",
        status="failed",
        stage="供应商暂时不可用",
        error={"message": "稍后重试"},
        retryable=True,
        safe_resume_point="audio",
    )
    assert queue.retry(failed["job_id"])["status"] == "queued"

    # Finish it as ambiguous and prove that ordinary retry cannot duplicate a paid submit.
    claimed = queue.claim_next("worker")
    assert claimed and claimed["job_id"] == failed["job_id"]
    queue.finish(failed["job_id"], "worker", status="ambiguous", stage="等待核对")
    with pytest.raises(ProductionQueueConflict, match="核对"):
        queue.retry(failed["job_id"])


def test_running_cancel_waits_for_parent_boundary_and_events_are_auditable(tmp_path: Path) -> None:
    queue, _ = _queue(tmp_path)
    task = _submit(queue, project_id="alpha", parent_job_id="RP-1")
    claimed = queue.claim_next("worker")
    assert claimed and claimed["job_id"] == task["job_id"]
    requested = queue.cancel(task["job_id"])
    assert requested["status"] == "running"
    assert requested["cancel_requested"] is True
    final = queue.finish(task["job_id"], "worker", status="completed", stage="底层任务完成")
    assert final["status"] == "cancelled"
    assert "安全任务边界" in final["stage"]
    assert [event["event_type"] for event in queue.events(task["job_id"])] == [
        "submitted",
        "claimed",
        "cancel_requested",
        "finished",
    ]


def test_public_failure_redacts_local_paths_and_inline_authorization(tmp_path: Path) -> None:
    queue, _ = _queue(tmp_path)
    task = _submit(queue, project_id="alpha", parent_job_id="RP-secret")
    queue.claim_next("worker")
    final = queue.finish(
        task["job_id"],
        "worker",
        status="failed",
        stage="失败",
        error={
            "message": r"C:\private\project\secret.json failed; x-api-key: visible-secret",
            "retryable": False,
        },
    )

    serialized = json.dumps(final, ensure_ascii=False)
    assert "C:\\private" not in serialized
    assert "visible-secret" not in serialized
    assert "[LOCAL_PATH]" in serialized
    assert "[REDACTED]" in serialized
