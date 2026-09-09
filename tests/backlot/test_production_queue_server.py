from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backlot import server as server_mod


def _project(root: Path, project_id: str, *, pipeline_type: str) -> Path:
    project_dir = root / project_id
    project_dir.mkdir(parents=True)
    (project_dir / "project.json").write_text(
        json.dumps(
            {"project_id": project_id, "title": f"项目 {project_id}", "pipeline_type": pipeline_type},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return project_dir


@pytest.fixture
def queue_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    projects = tmp_path / "projects"
    projects.mkdir()
    _project(projects, "plain", pipeline_type="animated-explainer")
    _project(projects, "avatar", pipeline_type="avatar-spokesperson")
    monkeypatch.setattr(server_mod, "PROJECTS_DIR", projects)
    monkeypatch.setattr(server_mod, "_summary_cache", {})

    async def no_watch() -> None:
        await asyncio.Event().wait()

    async def no_recovery(_app: FastAPI) -> None:
        return None

    async def idle_dispatcher(_app: FastAPI) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(server_mod, "_watch_projects", no_watch)
    monkeypatch.setattr(server_mod, "_recover_avatar_background_jobs", no_recovery)
    monkeypatch.setattr(server_mod, "_recover_workbench_background_jobs", no_recovery)
    monkeypatch.setattr(server_mod, "_production_queue_loop", idle_dispatcher)
    with TestClient(server_mod.create_app()) as client:
        yield client, projects


def test_workbench_and_codex_enter_same_global_queue(
    queue_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _projects = queue_client
    run_calls: list[str] = []
    monkeypatch.setattr(
        server_mod,
        "start_review_preview_job",
        lambda project_dir, payload: {
            "job_id": "RPP-workbench",
            "status": "queued",
            "launch_required": True,
        },
    )
    monkeypatch.setattr(
        server_mod,
        "start_avatar_review_preview_job",
        lambda project_dir, payload: {
            "job_id": "ARP-codex",
            "status": "queued",
            "launch_required": True,
        },
    )
    monkeypatch.setattr(
        server_mod,
        "run_review_preview_job",
        lambda *_args: run_calls.append("unexpected-direct-run"),
    )

    web = client.post(
        "/api/project/plain/workbench/automation/review-preview/jobs",
        json={"confirmed": True},
    )
    codex = client.post(
        "/api/production-queue/jobs",
        json={
            "project_id": "avatar",
            "kind": "avatar-review-preview",
            "priority": "priority",
            "request": {"confirmed": True},
        },
    )

    assert web.status_code == 200, web.text
    assert codex.status_code == 200, codex.text
    assert web.json()["queue_job"]["source"] == "workbench"
    assert codex.json()["queue_job"]["source"] == "codex"
    listing = client.get("/api/production-queue").json()
    assert listing["queued_count"] == 2
    assert {(item["project_id"], item["kind"]) for item in listing["tasks"]} == {
        ("plain", "review_preview"),
        ("avatar", "avatar_review_preview"),
    }
    assert run_calls == []


def test_global_queue_actions_validate_state(queue_client) -> None:
    client, projects = queue_client
    queue = client.app.state.production_queue
    task = queue.submit(
        project_id="plain",
        kind="full_preview",
        source="codex",
        priority="normal",
        parent_job_id="PRJ-1",
        frozen_request={"confirmed": True},
    )

    changed = client.post(
        f"/api/production-queue/jobs/{task['job_id']}/priority",
        json={"priority": "background"},
    )
    assert changed.status_code == 200
    assert changed.json()["priority"] == "background"
    assert client.post(f"/api/production-queue/jobs/{task['job_id']}/pause", json={}).json()["status"] == "paused"
    assert client.post(f"/api/production-queue/jobs/{task['job_id']}/resume", json={}).json()["status"] == "queued"
    assert client.post(f"/api/production-queue/jobs/{task['job_id']}/cancel", json={}).json()["status"] == "cancelled"
    invalid = client.post(f"/api/production-queue/jobs/{task['job_id']}/resume", json={})
    assert invalid.status_code == 409


def test_workbench_full_preview_is_registered_without_direct_render(
    queue_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _projects = queue_client
    renders: list[str] = []
    monkeypatch.setattr(
        server_mod,
        "start_full_preview_render",
        lambda project_dir, payload: {
            "project": {"title": "plain"},
            "automation": {"preview_render": {"job_id": "PRJ-one", "status": "generating"}},
        },
    )
    monkeypatch.setattr(
        server_mod,
        "generate_full_preview_render",
        lambda _project: renders.append("unexpected-direct-render"),
    )

    response = client.post(
        "/api/project/plain/workbench/automation/full-preview/jobs",
        json={"confirmed": True},
    )

    assert response.status_code == 200, response.text
    assert response.json()["queue_job"]["kind"] == "full_preview"
    assert response.json()["queue_job"]["status"] == "queued"
    assert renders == []


def test_codex_endpoint_rejects_arbitrary_execution_fields(queue_client) -> None:
    client, _projects = queue_client
    response = client.post(
        "/api/production-queue/jobs",
        json={
            "project_id": "plain",
            "kind": "review-preview",
            "priority": "normal",
            "request": {"confirmed": True},
            "command": "python dangerous.py",
        },
    )
    assert response.status_code == 422
    assert "不受支持字段" in response.json()["detail"]


def test_codex_explicit_idempotency_replay_does_not_create_second_parent(
    queue_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _projects = queue_client
    starts: list[str] = []

    def start(project_dir: Path, payload: dict) -> dict:
        starts.append(project_dir.name)
        return {"job_id": "RPP-once", "status": "queued", "launch_required": True}

    monkeypatch.setattr(server_mod, "start_review_preview_job", start)
    request = {
        "project_id": "plain",
        "kind": "review-preview",
        "priority": "normal",
        "idempotency_key": "plain-news-once",
        "request": {"confirmed": True},
    }

    first = client.post("/api/production-queue/jobs", json=request)
    second = client.post("/api/production-queue/jobs", json=request)

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["idempotent_replay"] is True
    assert second.json()["queue_job"]["job_id"] == first.json()["queue_job"]["job_id"]
    assert starts == ["plain"]


def test_pytest_queue_database_never_uses_repository_runtime_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "isolated queue path (call)")
    path = server_mod._production_queue_db_path()

    assert "openmontage-production-queue-tests" in str(path)
    assert path != server_mod.PROJECTS_DIR.parent / ".backlot" / server_mod.PRODUCTION_QUEUE_DB_NAME
