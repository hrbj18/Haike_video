"""Durable, cross-project production queue for Backlot.

The queue owns admission and ordering only.  Existing project pipelines remain
the source of truth for media stages, provider task IDs, budgets and recovery.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator


QUEUE_SCHEMA_VERSION = 1
QUEUE_KINDS = {"review_preview", "avatar_review_preview", "full_preview"}
QUEUE_SOURCES = {"workbench", "codex", "scheduler", "recovery"}
PRIORITIES = {"priority": 300, "normal": 200, "background": 100}
ACTIVE_STATUSES = {"queued", "running", "paused"}
WAITING_STATUSES = {"awaiting_human", "ambiguous"}
TERMINAL_STATUSES = {"completed", "failed", "cancelled"}
PUBLIC_SECRET_PARTS = ("api_key", "apikey", "secret", "token", "authorization", "cookie")
_WINDOWS_ABSOLUTE_PATH = re.compile(r"(?<![A-Za-z0-9])(?:[A-Za-z]:[\\/])[^\s\"']+")
_AUTH_VALUE = re.compile(
    r"(?i)\b(bearer\s+|(?:x-)?api[-_ ]?key\s*[:=]\s*|authorization\s*[:=]\s*)[^\s,;]+"
)


class ProductionQueueError(ValueError):
    """A queue contract error safe to present to the user."""


class ProductionQueueConflict(ProductionQueueError):
    """A state transition or ownership conflict."""


def _now() -> str:
    # Microseconds preserve FIFO order for bursts submitted within one second.
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _future(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=max(1, seconds))).isoformat(
        timespec="microseconds"
    ).replace("+00:00", "Z")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _decode(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return fallback


def _safe_public(value: Any) -> Any:
    """Remove credentials and implementation-only leases from public payloads."""

    if isinstance(value, dict):
        clean: dict[str, Any] = {}
        for key, child in value.items():
            lowered = str(key).lower()
            if lowered in {"worker_id", "lease_expires_at", "frozen_request"}:
                continue
            if any(part in lowered for part in PUBLIC_SECRET_PARTS):
                continue
            clean[str(key)] = _safe_public(child)
        return clean
    if isinstance(value, list):
        return [_safe_public(child) for child in value]
    if isinstance(value, str):
        value = _AUTH_VALUE.sub(lambda match: match.group(1) + "[REDACTED]", value)
        return _WINDOWS_ABSOLUTE_PATH.sub("[LOCAL_PATH]", value)
    return value


def _validate_identifier(value: Any, label: str, *, maximum: int = 240) -> str:
    text = str(value or "").strip()
    if not text or len(text) > maximum or any(ch in text for ch in "\r\n\x00"):
        raise ProductionQueueError(f"{label}无效")
    return text


class ProductionQueue:
    def __init__(self, db_path: str | Path, projects_root: str | Path):
        self.db_path = Path(db_path)
        self.projects_root = Path(projects_root)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connection(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.db_path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=10000")
        try:
            connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        connection = sqlite3.connect(self.db_path, timeout=10, isolation_level=None)
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=10000")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS production_jobs (
                    job_id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    project_id TEXT NOT NULL,
                    project_title TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    source TEXT NOT NULL,
                    priority TEXT NOT NULL,
                    priority_rank INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    parent_job_id TEXT NOT NULL,
                    frozen_request TEXT NOT NULL,
                    result_json TEXT NOT NULL DEFAULT '{}',
                    error_json TEXT NOT NULL DEFAULT '{}',
                    safe_resume_point TEXT,
                    retryable INTEGER NOT NULL DEFAULT 0,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    worker_id TEXT,
                    lease_expires_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    revision INTEGER NOT NULL DEFAULT 1
                );
                CREATE INDEX IF NOT EXISTS production_jobs_claim_idx
                    ON production_jobs(status, priority_rank DESC, created_at ASC, job_id ASC);
                CREATE INDEX IF NOT EXISTS production_jobs_project_idx
                    ON production_jobs(project_id, created_at DESC);
                CREATE TABLE IF NOT EXISTS production_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    from_status TEXT,
                    to_status TEXT,
                    detail_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(job_id) REFERENCES production_jobs(job_id) ON DELETE CASCADE
                );
                PRAGMA user_version=1;
                """
            )
        finally:
            connection.close()

    def _project_title(self, project_id: str) -> str:
        project_dir = (self.projects_root / project_id).resolve()
        root = os.path.normcase(str(self.projects_root.resolve()))
        candidate = os.path.normcase(str(project_dir))
        try:
            inside_root = os.path.commonpath([root, candidate]) == root
        except ValueError:
            inside_root = False
        if not inside_root or not project_dir.is_dir():
            raise ProductionQueueError("项目不存在或不在工作区内")
        marker = project_dir / "project.json"
        if not marker.is_file():
            raise ProductionQueueError("项目缺少 project.json")
        try:
            payload = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ProductionQueueError("项目配置无法读取") from exc
        return str(payload.get("title") or project_id).strip()[:240]

    @staticmethod
    def _job_id(idempotency_key: str) -> str:
        return "PQ-" + hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:20]

    @staticmethod
    def _event(
        connection: sqlite3.Connection,
        job_id: str,
        event_type: str,
        from_status: str | None,
        to_status: str | None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        connection.execute(
            "INSERT INTO production_events(job_id,event_type,from_status,to_status,detail_json,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (job_id, event_type, from_status, to_status, _json(detail or {}), _now()),
        )

    def submit(
        self,
        *,
        project_id: str,
        kind: str,
        source: str,
        priority: str,
        parent_job_id: str,
        frozen_request: dict[str, Any] | None,
        idempotency_key: str | None = None,
        revive: bool = False,
    ) -> dict[str, Any]:
        project_id = _validate_identifier(project_id, "项目编号", maximum=80)
        kind = _validate_identifier(kind, "生产类型", maximum=64)
        source = _validate_identifier(source, "任务来源", maximum=32)
        priority = _validate_identifier(priority, "优先级", maximum=32)
        parent_job_id = _validate_identifier(parent_job_id, "底层任务编号")
        if kind not in QUEUE_KINDS:
            raise ProductionQueueError("生产类型不在统一队列白名单中")
        if source not in QUEUE_SOURCES:
            raise ProductionQueueError("任务来源无效")
        if priority not in PRIORITIES:
            raise ProductionQueueError("优先级只能是 priority、normal 或 background")
        project_title = self._project_title(project_id)
        key = _validate_identifier(
            idempotency_key or f"{kind}:{project_id}:{parent_job_id}",
            "幂等键",
            maximum=300,
        )
        job_id = self._job_id(key)
        now = _now()
        request = _safe_public(frozen_request or {})
        with self._connection(immediate=True) as connection:
            existing = connection.execute(
                "SELECT * FROM production_jobs WHERE idempotency_key=?", (key,)
            ).fetchone()
            if existing is not None:
                if (
                    str(existing["project_id"]) != project_id
                    or str(existing["kind"]) != kind
                    or str(existing["parent_job_id"]) != parent_job_id
                ):
                    raise ProductionQueueConflict("幂等键已被另一条生产请求占用")
                if revive and existing["status"] in {"failed", "awaiting_human"}:
                    old = str(existing["status"])
                    connection.execute(
                        "UPDATE production_jobs SET status='queued',stage='等待统一调度',error_json='{}',"
                        "retryable=0,cancel_requested=0,worker_id=NULL,lease_expires_at=NULL,finished_at=NULL,"
                        "updated_at=?,revision=revision+1 WHERE job_id=?",
                        (now, existing["job_id"]),
                    )
                    self._event(connection, existing["job_id"], "requeued", old, "queued")
                return self._public_row(
                    connection.execute(
                        "SELECT * FROM production_jobs WHERE idempotency_key=?", (key,)
                    ).fetchone()
                )
            conflict = connection.execute(
                "SELECT job_id,status FROM production_jobs WHERE project_id=? AND kind=? "
                "AND status IN ('queued','running','paused') LIMIT 1",
                (project_id, kind),
            ).fetchone()
            if conflict is not None:
                raise ProductionQueueConflict(
                    f"该项目已有同类生产任务 {conflict['job_id']}（{conflict['status']}）"
                )
            connection.execute(
                """
                INSERT INTO production_jobs(
                    job_id,idempotency_key,project_id,project_title,kind,source,priority,priority_rank,
                    status,stage,parent_job_id,frozen_request,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    job_id, key, project_id, project_title, kind, source, priority,
                    PRIORITIES[priority], "queued", "等待统一调度", parent_job_id,
                    _json(request), now, now,
                ),
            )
            self._event(connection, job_id, "submitted", None, "queued", {"source": source})
            row = connection.execute("SELECT * FROM production_jobs WHERE job_id=?", (job_id,)).fetchone()
        return self._public_row(row)

    def find_by_idempotency_key(self, idempotency_key: str) -> dict[str, Any] | None:
        key = _validate_identifier(idempotency_key, "幂等键", maximum=300)
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM production_jobs WHERE idempotency_key=?", (key,)
            ).fetchone()
        return self._public_row(row) if row is not None else None

    def get(self, job_id: str, *, include_private: bool = False) -> dict[str, Any]:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM production_jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            raise ProductionQueueError("生产任务不存在")
        return self._private_row(row) if include_private else self._public_row(row)

    def list(self, *, project_id: str | None = None, limit: int = 80) -> dict[str, Any]:
        limit = min(200, max(1, int(limit)))
        ordering = (
            "CASE status WHEN 'running' THEN 0 WHEN 'queued' THEN 1 WHEN 'paused' THEN 2 "
            "WHEN 'awaiting_human' THEN 3 WHEN 'ambiguous' THEN 3 WHEN 'failed' THEN 4 "
            "WHEN 'completed' THEN 5 ELSE 6 END ASC, "
            "CASE WHEN status='queued' THEN priority_rank ELSE 0 END DESC, "
            "CASE WHEN status IN ('running','queued','paused') THEN created_at END ASC, "
            "created_at DESC,job_id ASC"
        )
        with self._connection() as connection:
            if project_id:
                rows = connection.execute(
                    f"SELECT * FROM production_jobs WHERE project_id=? ORDER BY {ordering} LIMIT ?",
                    (project_id, limit),
                ).fetchall()
                count_rows = connection.execute(
                    "SELECT status,COUNT(*) AS count FROM production_jobs WHERE project_id=? GROUP BY status",
                    (project_id,),
                ).fetchall()
            else:
                rows = connection.execute(
                    f"SELECT * FROM production_jobs ORDER BY {ordering} LIMIT ?", (limit,)
                ).fetchall()
                count_rows = connection.execute(
                    "SELECT status,COUNT(*) AS count FROM production_jobs GROUP BY status"
                ).fetchall()
            queued = connection.execute(
                "SELECT job_id FROM production_jobs WHERE status='queued' "
                "ORDER BY priority_rank DESC,created_at ASC,job_id ASC"
            ).fetchall()
        positions = {str(row["job_id"]): index + 1 for index, row in enumerate(queued)}
        tasks = []
        for row in rows:
            task = self._public_row(row)
            task["queue_position"] = positions.get(task["job_id"])
            tasks.append(task)
        summary = self._summary(tasks)
        counts = {str(row["status"]): int(row["count"]) for row in count_rows}
        summary.update(
            {
                "active_count": counts.get("running", 0),
                "queued_count": counts.get("queued", 0),
                "paused_count": counts.get("paused", 0),
                "waiting_count": counts.get("awaiting_human", 0) + counts.get("ambiguous", 0),
                "failure_count": counts.get("failed", 0),
                "completed_count": counts.get("completed", 0),
            }
        )
        return summary

    @staticmethod
    def _summary(tasks: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "version": QUEUE_SCHEMA_VERSION,
            "active_count": sum(1 for task in tasks if task["status"] == "running"),
            "queued_count": sum(1 for task in tasks if task["status"] == "queued"),
            "paused_count": sum(1 for task in tasks if task["status"] == "paused"),
            "waiting_count": sum(1 for task in tasks if task["status"] in WAITING_STATUSES),
            "failure_count": sum(1 for task in tasks if task["status"] == "failed"),
            "completed_count": sum(1 for task in tasks if task["status"] == "completed"),
            "tasks": tasks,
        }

    def claim_next(self, worker_id: str, *, lease_seconds: int = 120) -> dict[str, Any] | None:
        worker_id = _validate_identifier(worker_id, "工作者编号", maximum=120)
        now = _now()
        with self._connection(immediate=True) as connection:
            # The queue is deliberately single-lane in V1.  This guard lives
            # in the same write transaction as the claim, so even two server
            # processes cannot each claim a different heavy/paid task.
            running = connection.execute(
                "SELECT job_id FROM production_jobs WHERE status='running' LIMIT 1"
            ).fetchone()
            if running is not None:
                return None
            row = connection.execute(
                "SELECT * FROM production_jobs WHERE status='queued' "
                "ORDER BY priority_rank DESC,created_at ASC,job_id ASC LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            changed = connection.execute(
                "UPDATE production_jobs SET status='running',stage='正在取得生产执行权',worker_id=?,"
                "lease_expires_at=?,started_at=COALESCE(started_at,?),updated_at=?,"
                "attempt_count=attempt_count+1,revision=revision+1 "
                "WHERE job_id=? AND status='queued'",
                (worker_id, _future(lease_seconds), now, now, row["job_id"]),
            ).rowcount
            if changed != 1:
                return None
            self._event(connection, row["job_id"], "claimed", "queued", "running", {"worker_id": worker_id})
            claimed = connection.execute(
                "SELECT * FROM production_jobs WHERE job_id=?", (row["job_id"],)
            ).fetchone()
        return self._private_row(claimed)

    def heartbeat(self, job_id: str, worker_id: str, *, lease_seconds: int = 120) -> None:
        with self._connection(immediate=True) as connection:
            changed = connection.execute(
                "UPDATE production_jobs SET lease_expires_at=?,updated_at=? "
                "WHERE job_id=? AND status='running' AND worker_id=?",
                (_future(lease_seconds), _now(), job_id, worker_id),
            ).rowcount
            if changed != 1:
                raise ProductionQueueConflict("生产任务租约已经失效")

    def update_stage(self, job_id: str, worker_id: str, stage: str) -> None:
        stage = _validate_identifier(stage, "任务阶段", maximum=300)
        with self._connection(immediate=True) as connection:
            changed = connection.execute(
                "UPDATE production_jobs SET stage=?,updated_at=?,revision=revision+1 "
                "WHERE job_id=? AND status='running' AND worker_id=?",
                (stage, _now(), job_id, worker_id),
            ).rowcount
            if changed != 1:
                raise ProductionQueueConflict("生产任务已经不属于当前工作者")

    def finish(
        self,
        job_id: str,
        worker_id: str,
        *,
        status: str,
        stage: str,
        result: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
        retryable: bool = False,
        safe_resume_point: str | None = None,
    ) -> dict[str, Any]:
        if status not in {"completed", "awaiting_human", "ambiguous", "failed"}:
            raise ProductionQueueError("执行器返回了不受支持的终态")
        now = _now()
        with self._connection(immediate=True) as connection:
            row = connection.execute(
                "SELECT status,cancel_requested FROM production_jobs WHERE job_id=? AND worker_id=?",
                (job_id, worker_id),
            ).fetchone()
            if row is None or row["status"] != "running":
                raise ProductionQueueConflict("生产任务已经不属于当前工作者")
            effective_status = "cancelled" if bool(row["cancel_requested"]) and status != "ambiguous" else status
            effective_stage = "已在安全任务边界取消后续执行" if effective_status == "cancelled" else stage
            connection.execute(
                "UPDATE production_jobs SET status=?,stage=?,result_json=?,error_json=?,retryable=?,"
                "safe_resume_point=?,worker_id=NULL,lease_expires_at=NULL,finished_at=?,updated_at=?,"
                "revision=revision+1 WHERE job_id=?",
                (
                    effective_status, effective_stage, _json(_safe_public(result or {})),
                    _json(_safe_public(error or {})), int(bool(retryable)), safe_resume_point,
                    now, now, job_id,
                ),
            )
            self._event(connection, job_id, "finished", "running", effective_status)
            final = connection.execute("SELECT * FROM production_jobs WHERE job_id=?", (job_id,)).fetchone()
        return self._public_row(final)

    def set_priority(self, job_id: str, priority: str) -> dict[str, Any]:
        if priority not in PRIORITIES:
            raise ProductionQueueError("优先级只能是 priority、normal 或 background")
        with self._connection(immediate=True) as connection:
            row = connection.execute("SELECT * FROM production_jobs WHERE job_id=?", (job_id,)).fetchone()
            if row is None:
                raise ProductionQueueError("生产任务不存在")
            if row["status"] not in {"queued", "paused"}:
                raise ProductionQueueConflict("只有待执行或暂停的任务可以调整优先级")
            connection.execute(
                "UPDATE production_jobs SET priority=?,priority_rank=?,updated_at=?,revision=revision+1 "
                "WHERE job_id=?",
                (priority, PRIORITIES[priority], _now(), job_id),
            )
            self._event(connection, job_id, "priority_changed", row["status"], row["status"], {"priority": priority})
            updated = connection.execute("SELECT * FROM production_jobs WHERE job_id=?", (job_id,)).fetchone()
        return self._public_row(updated)

    def pause(self, job_id: str) -> dict[str, Any]:
        return self._transition(job_id, {"queued"}, "paused", "已暂停，等待恢复", "paused")

    def resume(self, job_id: str) -> dict[str, Any]:
        return self._transition(job_id, {"paused"}, "queued", "等待统一调度", "resumed")

    def cancel(self, job_id: str) -> dict[str, Any]:
        with self._connection(immediate=True) as connection:
            row = connection.execute("SELECT * FROM production_jobs WHERE job_id=?", (job_id,)).fetchone()
            if row is None:
                raise ProductionQueueError("生产任务不存在")
            status = str(row["status"])
            if status in {"queued", "paused"}:
                now = _now()
                connection.execute(
                    "UPDATE production_jobs SET status='cancelled',stage='已取消待执行任务',"
                    "finished_at=?,updated_at=?,revision=revision+1 WHERE job_id=?",
                    (now, now, job_id),
                )
                self._event(connection, job_id, "cancelled", status, "cancelled")
            elif status == "running":
                connection.execute(
                    "UPDATE production_jobs SET cancel_requested=1,stage='已请求在安全任务边界停止',"
                    "updated_at=?,revision=revision+1 WHERE job_id=?",
                    (_now(), job_id),
                )
                self._event(connection, job_id, "cancel_requested", "running", "running")
            else:
                raise ProductionQueueConflict("当前状态不能取消")
            updated = connection.execute("SELECT * FROM production_jobs WHERE job_id=?", (job_id,)).fetchone()
        return self._public_row(updated)

    def retry(self, job_id: str) -> dict[str, Any]:
        with self._connection(immediate=True) as connection:
            row = connection.execute("SELECT * FROM production_jobs WHERE job_id=?", (job_id,)).fetchone()
            if row is None:
                raise ProductionQueueError("生产任务不存在")
            if row["status"] == "ambiguous":
                raise ProductionQueueConflict("供应商受理状态不明确，核对原任务后才能恢复")
            if row["status"] != "failed" or not bool(row["retryable"]):
                raise ProductionQueueConflict("该失败任务没有安全重试条件")
            now = _now()
            connection.execute(
                "UPDATE production_jobs SET status='queued',stage='等待从安全点继续',error_json='{}',"
                "retryable=0,cancel_requested=0,worker_id=NULL,lease_expires_at=NULL,finished_at=NULL,"
                "updated_at=?,revision=revision+1 WHERE job_id=?",
                (now, job_id),
            )
            self._event(connection, job_id, "retried", "failed", "queued")
            updated = connection.execute("SELECT * FROM production_jobs WHERE job_id=?", (job_id,)).fetchone()
        return self._public_row(updated)

    def expired_running(self) -> list[dict[str, Any]]:
        now = _now()
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM production_jobs WHERE status='running' AND lease_expires_at IS NOT NULL "
                "AND lease_expires_at < ? ORDER BY created_at ASC",
                (now,),
            ).fetchall()
        return [self._private_row(row) for row in rows]

    def recover_expired(
        self,
        job_id: str,
        *,
        status: str,
        stage: str,
        result: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
        retryable: bool = False,
        safe_resume_point: str | None = None,
    ) -> dict[str, Any]:
        if status not in {"queued", "completed", "awaiting_human", "ambiguous", "failed"}:
            raise ProductionQueueError("恢复状态无效")
        with self._connection(immediate=True) as connection:
            row = connection.execute("SELECT * FROM production_jobs WHERE job_id=?", (job_id,)).fetchone()
            if row is None:
                raise ProductionQueueError("生产任务不存在")
            if row["status"] != "running":
                return self._public_row(row)
            now = _now()
            connection.execute(
                "UPDATE production_jobs SET status=?,stage=?,result_json=?,error_json=?,retryable=?,"
                "safe_resume_point=?,worker_id=NULL,lease_expires_at=NULL,finished_at=?,updated_at=?,"
                "revision=revision+1 WHERE job_id=?",
                (
                    status, stage, _json(_safe_public(result or {})), _json(_safe_public(error or {})),
                    int(bool(retryable)), safe_resume_point,
                    now if status != "queued" else None, now, job_id,
                ),
            )
            self._event(connection, job_id, "lease_reconciled", "running", status)
            updated = connection.execute("SELECT * FROM production_jobs WHERE job_id=?", (job_id,)).fetchone()
        return self._public_row(updated)

    def events(self, job_id: str) -> list[dict[str, Any]]:
        self.get(job_id)
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT event_type,from_status,to_status,detail_json,created_at "
                "FROM production_events WHERE job_id=? ORDER BY event_id ASC",
                (job_id,),
            ).fetchall()
        return [
            {
                "event_type": row["event_type"], "from_status": row["from_status"],
                "to_status": row["to_status"], "detail": _safe_public(_decode(row["detail_json"], {})),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def _transition(
        self,
        job_id: str,
        allowed: set[str],
        target: str,
        stage: str,
        event_type: str,
    ) -> dict[str, Any]:
        with self._connection(immediate=True) as connection:
            row = connection.execute("SELECT * FROM production_jobs WHERE job_id=?", (job_id,)).fetchone()
            if row is None:
                raise ProductionQueueError("生产任务不存在")
            if row["status"] not in allowed:
                raise ProductionQueueConflict("当前状态不能执行该操作")
            connection.execute(
                "UPDATE production_jobs SET status=?,stage=?,updated_at=?,revision=revision+1 WHERE job_id=?",
                (target, stage, _now(), job_id),
            )
            self._event(connection, job_id, event_type, row["status"], target)
            updated = connection.execute("SELECT * FROM production_jobs WHERE job_id=?", (job_id,)).fetchone()
        return self._public_row(updated)

    @staticmethod
    def _private_row(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["frozen_request"] = _decode(result.pop("frozen_request", "{}"), {})
        result["result"] = _decode(result.pop("result_json", "{}"), {})
        result["error"] = _decode(result.pop("error_json", "{}"), {})
        for key in ("retryable", "cancel_requested"):
            result[key] = bool(result.get(key))
        return result

    @classmethod
    def _public_row(cls, row: sqlite3.Row) -> dict[str, Any]:
        private = cls._private_row(row)
        private.pop("idempotency_key", None)
        private.pop("priority_rank", None)
        private.pop("worker_id", None)
        private.pop("lease_expires_at", None)
        private.pop("frozen_request", None)
        private["id"] = private["job_id"]
        private["title"] = {
            "review_preview": "无数字人一键审核预览",
            "avatar_review_preview": "有数字人一键审核预览",
            "full_preview": "合成全片审核预览",
        }.get(private["kind"], private["kind"])
        private["progress"] = {
            "completed": 1 if private["status"] == "completed" else 0,
            "total": 1,
            "failed": 1 if private["status"] == "failed" else 0,
            "ratio": 1.0 if private["status"] == "completed" else 0.0,
        }
        private["target_view"] = "quality" if private["kind"] == "full_preview" else "review"
        private["wait_reason"] = (
            "等待前序生产任务释放执行资格" if private["status"] == "queued"
            else "任务已暂停" if private["status"] == "paused"
            else "供应商受理状态需要人工核对" if private["status"] == "ambiguous"
            else "底层生产管线正在等待人工确认" if private["status"] == "awaiting_human"
            else ""
        )
        private["executor"] = "本机统一调度器" if private["status"] == "running" else ""
        return _safe_public(private)
