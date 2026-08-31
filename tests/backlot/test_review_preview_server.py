from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backlot import server as server_mod


def _write_project(root: Path, project_id: str = "plain", *, pipeline_type: str = "animated-explainer") -> Path:
    project_dir = root / project_id
    project_dir.mkdir(parents=True)
    (project_dir / "project.json").write_text(
        json.dumps(
            {
                "project_id": project_id,
                "title": project_id,
                "pipeline_type": pipeline_type,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return project_dir


@pytest.fixture
def projects_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "projects"
    root.mkdir()
    monkeypatch.setattr(server_mod, "PROJECTS_DIR", root)
    monkeypatch.setattr(server_mod, "_summary_cache", {})
    return root


@pytest.fixture
def client(projects_root: Path, monkeypatch: pytest.MonkeyPatch):
    async def no_watch() -> None:
        return None

    async def no_recovery(_app: FastAPI) -> None:
        return None

    monkeypatch.setattr(server_mod, "_watch_projects", no_watch)
    monkeypatch.setattr(server_mod, "_recover_avatar_background_jobs", no_recovery)
    monkeypatch.setattr(server_mod, "_recover_workbench_background_jobs", no_recovery)
    with TestClient(server_mod.create_app()) as test_client:
        yield test_client


def _contains_private_key(value: object) -> bool:
    if isinstance(value, dict):
        return any(
            key in {"worker_token", "tts_terminal_retry_authorized"}
            or str(key).startswith("_review_preview_")
            or _contains_private_key(child)
            for key, child in value.items()
        )
    if isinstance(value, list):
        return any(_contains_private_key(child) for child in value)
    return False


def test_preflight_blockers_are_a_truthful_200(
    client: TestClient,
    projects_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_project(projects_root)
    launches: list[str] = []
    seen_payloads: list[dict] = []

    def preflight(project_dir: Path, payload: dict) -> dict:
        seen_payloads.append(payload)
        return {
            "ready": False,
            "blockers": ["雅雅不可用"],
            "project_type": "animated-explainer",
        }

    monkeypatch.setattr(server_mod, "review_preview_preflight", preflight)
    monkeypatch.setattr(server_mod, "_launch_review_preview_worker", lambda *args: launches.append("launch"))

    response = client.get("/api/project/plain/workbench/automation/review-preview/preflight")

    assert response.status_code == 200
    assert response.json()["ready"] is False
    assert response.json()["blockers"] == ["雅雅不可用"]
    assert seen_payloads == [{"visual": {"planning_mode": "ai_director"}}]
    assert launches == []


def test_preflight_accepts_only_an_explicit_visual_planning_mode(
    client: TestClient,
    projects_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_project(projects_root)
    seen: list[str] = []

    def preflight(project_dir: Path, payload: dict) -> dict:
        mode = payload["visual"]["planning_mode"]
        seen.append(mode)
        return {"ready": True, "visual_strategy": {"planning_mode": mode}}

    monkeypatch.setattr(server_mod, "review_preview_preflight", preflight)

    rules = client.get(
        "/api/project/plain/workbench/automation/review-preview/preflight?planning_mode=rule_mix"
    )
    invalid = client.get(
        "/api/project/plain/workbench/automation/review-preview/preflight?planning_mode=hidden_mode"
    )

    assert rules.status_code == 200
    assert rules.json()["visual_strategy"]["planning_mode"] == "rule_mix"
    assert invalid.status_code == 422
    assert seen == ["rule_mix"]


def test_avatar_parent_routes_keep_budget_and_worker_lease_server_side(
    client: TestClient,
    projects_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_project(projects_root, "avatar", pipeline_type="avatar-spokesperson")
    preflight_payloads: list[dict] = []
    starts: list[dict] = []
    launches: list[str] = []

    monkeypatch.setattr(
        server_mod,
        "avatar_review_preview_preflight",
        lambda _path, payload: preflight_payloads.append(payload) or {
            "ready": True, "budget": {"limit_cny": payload["budget_limit_cny"]},
        },
    )
    monkeypatch.setattr(
        server_mod,
        "start_avatar_review_preview_job",
        lambda _path, payload: starts.append(payload) or {
            "job_id": "ARP-one", "status": "queued", "launch_required": True,
            "worker_token": "must-not-leak", "frozen_input": {"project_type": "avatar-spokesperson"},
        },
    )
    monkeypatch.setattr(
        server_mod,
        "_launch_avatar_review_preview_worker",
        lambda _app, _path, job_id: launches.append(job_id),
    )

    default_checked = client.get(
        "/api/project/avatar/workbench/automation/avatar-review-preview/preflight"
        "?planning_mode=rule_mix&budget_limit_cny=5"
    )
    checked = client.get(
        "/api/project/avatar/workbench/automation/avatar-review-preview/preflight"
        "?planning_mode=rule_mix&budget_limit_cny=5&allow_plus_on_oom=true"
    )
    started = client.post(
        "/api/project/avatar/workbench/automation/avatar-review-preview/jobs",
        json={
            "confirmed": True,
            "budget_limit_cny": 5,
            "allow_plus_on_oom": True,
            "visual": {"planning_mode": "rule_mix"},
        },
    )
    rejected_internal_alias = client.post(
        "/api/project/avatar/workbench/automation/avatar-review-preview/jobs",
        json={
            "confirmed": True,
            "budget_limit_cny": 5,
            "allow_plus_on_oom": True,
            "plus_48gb_authorized": True,
            "visual": {"planning_mode": "rule_mix"},
        },
    )

    assert default_checked.status_code == 200
    assert checked.status_code == 200
    assert preflight_payloads == [
        {
            "visual": {"planning_mode": "rule_mix"},
            "budget_limit_cny": 5.0,
            "allow_plus_on_oom": False,
        },
        {
            "visual": {"planning_mode": "rule_mix"},
            "budget_limit_cny": 5.0,
            "allow_plus_on_oom": True,
        },
    ]
    assert started.status_code == 200
    assert starts == [{
        "confirmed": True,
        "budget_limit_cny": 5,
        "allow_plus_on_oom": True,
        "visual": {"planning_mode": "rule_mix"},
    }]
    assert rejected_internal_alias.status_code == 422
    assert launches == ["ARP-one"]
    assert not _contains_private_key(started.json())


def test_avatar_parent_recovery_runs_before_legacy_avatar_children(
    projects_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_project(projects_root, "avatar", pipeline_type="avatar-spokesperson")
    launches: list[str] = []
    child_reads: list[str] = []
    monkeypatch.setattr(
        server_mod,
        "recover_avatar_review_preview_job",
        lambda _path: {"job_id": "ARP-recover", "status": "queued", "launch_required": True},
    )
    monkeypatch.setattr(
        server_mod,
        "_launch_avatar_review_preview_worker",
        lambda _app, _path, job_id: launches.append(job_id),
    )
    monkeypatch.setattr(
        server_mod,
        "read_visual_batch_generation",
        lambda _path: child_reads.append("visual") or {"generation": {"status": "queued"}},
    )
    app = SimpleNamespace(state=SimpleNamespace(recovery_tasks=set()))

    asyncio.run(server_mod._recover_workbench_background_jobs(app))

    assert launches == ["ARP-recover"]
    assert child_reads == []


def test_start_dispatches_only_when_launch_is_explicitly_required(
    client: TestClient,
    projects_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_project(projects_root)
    states = iter(
        [
            {"job_id": "RPP-one", "status": "queued", "launch_required": True},
            {"job_id": "RPP-one", "status": "running", "launch_required": False},
            {"job_id": "RPP-one", "status": "completed", "launch_required": False},
        ]
    )
    launches: list[tuple[str, str]] = []
    monkeypatch.setattr(server_mod, "start_review_preview_job", lambda project_dir, payload: next(states))
    monkeypatch.setattr(
        server_mod,
        "_launch_review_preview_worker",
        lambda app, project_dir, job_id: launches.append((project_dir.name, job_id)),
    )
    body = {"confirmed": True, "network_confirmed": True, "text_ai_confirmed": False}

    responses = [
        client.post("/api/project/plain/workbench/automation/review-preview/jobs", json=body)
        for _ in range(3)
    ]

    assert [response.status_code for response in responses] == [200, 200, 200]
    assert launches == [("plain", "RPP-one")]


@pytest.mark.parametrize(
    ("error", "expected_status"),
    [
        (server_mod.ReviewPreviewError("预检失败"), 422),
        (server_mod.ReviewPreviewConflict("已有任务"), 409),
        (server_mod.StaleReviewPreviewWorker("旧 worker"), 409),
    ],
)
def test_parent_errors_have_stable_http_categories(
    client: TestClient,
    projects_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    expected_status: int,
) -> None:
    _write_project(projects_root)

    def fail(project_dir: Path, payload: dict) -> dict:
        raise error

    monkeypatch.setattr(server_mod, "start_review_preview_job", fail)
    response = client.post(
        "/api/project/plain/workbench/automation/review-preview/jobs",
        json={"confirmed": True, "network_confirmed": True},
    )
    assert response.status_code == expected_status


def test_resume_rejects_mismatched_job_id_and_completed_resume_does_not_launch(
    client: TestClient,
    projects_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_project(projects_root)
    launches: list[str] = []
    resumes: list[str] = []

    def resume(project_dir: Path, job_id: str, payload: dict) -> dict:
        resumes.append(job_id)
        return {"job_id": job_id, "status": "completed", "launch_required": False}

    monkeypatch.setattr(server_mod, "resume_review_preview_job", resume)
    monkeypatch.setattr(server_mod, "_launch_review_preview_worker", lambda *args: launches.append("launch"))

    mismatch = client.post(
        "/api/project/plain/workbench/automation/review-preview/jobs/RPP-one/resume",
        json={"job_id": "RPP-other"},
    )
    completed = client.post(
        "/api/project/plain/workbench/automation/review-preview/jobs/RPP-one/resume",
        json={"job_id": "RPP-one"},
    )

    assert mismatch.status_code == 409
    assert completed.status_code == 200
    assert resumes == ["RPP-one"]
    assert launches == []


def test_resume_dispatches_exact_returned_job_once(
    client: TestClient,
    projects_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_project(projects_root)
    launches: list[tuple[str, str]] = []
    monkeypatch.setattr(
        server_mod,
        "resume_review_preview_job",
        lambda project_dir, job_id, payload: {
            "job_id": job_id,
            "status": "queued",
            "launch_required": True,
        },
    )
    monkeypatch.setattr(
        server_mod,
        "_launch_review_preview_worker",
        lambda app, project_dir, job_id: launches.append((project_dir.name, job_id)),
    )

    response = client.post(
        "/api/project/plain/workbench/automation/review-preview/jobs/RPP-one/resume",
        json={"confirmed": True},
    )

    assert response.status_code == 200
    assert launches == [("plain", "RPP-one")]


@pytest.mark.parametrize(
    "body",
    [
        {"confirmed": True, "network_confirmed": True, "worker_token": "lease"},
        {"confirmed": True, "network_confirmed": True, "launch_required": True},
        {
            "confirmed": True,
            "network_confirmed": True,
            "visual": {"_review_preview_internal_capability": {"avatar": True}},
        },
        {
            "confirmed": True,
            "network_confirmed": True,
            "visual": {"nested": {"tts_terminal_retry_authorized": True}},
        },
    ],
)
def test_http_cannot_inject_parent_internal_fields(
    client: TestClient,
    projects_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    body: dict,
) -> None:
    _write_project(projects_root)
    starts: list[dict] = []
    monkeypatch.setattr(
        server_mod,
        "start_review_preview_job",
        lambda project_dir, payload: starts.append(payload) or {},
    )

    response = client.post("/api/project/plain/workbench/automation/review-preview/jobs", json=body)

    assert response.status_code == 422
    assert starts == []


def test_public_job_responses_recursively_strip_worker_leases(
    client: TestClient,
    projects_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_project(projects_root)
    private = {
        "job_id": "RPP-one",
        "status": "running",
        "launch_required": False,
        "worker_token": "top-secret-lease",
        "frozen_input": {
            "voice": {"profile_name": "雅雅"},
            "_review_preview_worker_token": "nested-secret-lease",
        },
        "phases": [{"tts_terminal_retry_authorized": True}],
    }
    monkeypatch.setattr(server_mod, "read_review_preview_job", lambda project_dir: private)

    response = client.get("/api/project/plain/workbench/automation/review-preview/jobs/current")

    assert response.status_code == 200
    assert not _contains_private_key(response.json())
    assert response.json()["frozen_input"]["voice"]["profile_name"] == "雅雅"


def test_parent_worker_failure_is_retrieved_published_and_removed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project_dir = tmp_path / "plain"
    project_dir.mkdir()
    published: list[str] = []
    invalidated: list[str] = []

    def fail(project: Path, job_id: str) -> None:
        raise RuntimeError("worker failed after durable state update")

    monkeypatch.setattr(server_mod, "run_review_preview_job", fail)
    monkeypatch.setattr(server_mod, "_invalidate_summary", invalidated.append)
    monkeypatch.setattr(server_mod.hub, "publish", published.append)
    app = FastAPI()
    app.state.recovery_tasks = set()

    async def exercise() -> None:
        task = server_mod._launch_review_preview_worker(app, project_dir, "RPP-one")
        await task
        await asyncio.sleep(0)

    asyncio.run(exercise())

    assert app.state.recovery_tasks == set()
    assert invalidated == ["plain"]
    assert published == ["plain"]


def test_task_tracker_retrieves_failure_and_cleans_cancelled_task() -> None:
    app = FastAPI()
    app.state.recovery_tasks = set()

    async def fail() -> None:
        raise RuntimeError("unhandled without tracker")

    async def wait_forever() -> None:
        await asyncio.Event().wait()

    async def exercise() -> None:
        failed = server_mod._track_background_task(app, fail())
        while not failed.done():
            await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert app.state.recovery_tasks == set()

        cancelled = server_mod._track_background_task(app, wait_forever())
        await asyncio.sleep(0)
        cancelled.cancel()
        await asyncio.gather(cancelled, return_exceptions=True)
        await asyncio.sleep(0)

    asyncio.run(exercise())

    assert app.state.recovery_tasks == set()


def test_recovery_launches_each_parent_once_and_never_starts_its_children(
    projects_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for project_id in ("alpha", "beta"):
        _write_project(projects_root, project_id)
    launches: list[tuple[str, str]] = []
    child_reads: list[str] = []

    monkeypatch.setattr(
        server_mod,
        "recover_review_preview_job",
        lambda project_dir: {
            "job_id": f"RPP-{project_dir.name}",
            "status": "queued",
            "launch_required": True,
        },
    )
    monkeypatch.setattr(
        server_mod,
        "_launch_review_preview_worker",
        lambda app, project_dir, job_id: launches.append((project_dir.name, job_id)),
    )
    monkeypatch.setattr(
        server_mod,
        "read_visual_batch_generation",
        lambda project_dir: child_reads.append(f"visual:{project_dir.name}") or {"generation": {"status": "idle"}},
    )
    monkeypatch.setattr(
        server_mod,
        "read_review_preview_sync",
        lambda project_dir: child_reads.append(f"sync:{project_dir.name}") or {"generation": {"status": "idle"}},
    )
    app = SimpleNamespace(state=SimpleNamespace(recovery_tasks=set()))

    asyncio.run(server_mod._recover_workbench_background_jobs(app))

    assert launches == [("alpha", "RPP-alpha"), ("beta", "RPP-beta")]
    assert child_reads == []


@pytest.mark.parametrize("parent", [None, [], "bad-state"])
def test_non_mapping_parent_recovery_is_conservatively_recorded(
    projects_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    parent: object,
) -> None:
    _write_project(projects_root)
    child_reads: list[str] = []
    published: list[str] = []
    monkeypatch.setattr(server_mod, "recover_review_preview_job", lambda project_dir: parent)
    monkeypatch.setattr(
        server_mod,
        "read_visual_batch_generation",
        lambda project_dir: child_reads.append("visual") or {},
    )
    monkeypatch.setattr(
        server_mod,
        "read_review_preview_sync",
        lambda project_dir: child_reads.append("sync") or {},
    )
    monkeypatch.setattr(server_mod.hub, "publish", published.append)
    app = SimpleNamespace(
        state=SimpleNamespace(recovery_tasks=set(), review_preview_recovery_errors={})
    )

    asyncio.run(server_mod._recover_workbench_background_jobs(app))

    assert child_reads == []
    assert "plain" in app.state.review_preview_recovery_errors
    assert published == ["plain"]


def test_launch_without_job_id_is_recorded_and_suppresses_children(
    projects_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_project(projects_root)
    child_reads: list[str] = []
    monkeypatch.setattr(
        server_mod,
        "recover_review_preview_job",
        lambda project_dir: {"status": "queued", "launch_required": True},
    )
    monkeypatch.setattr(
        server_mod,
        "read_visual_batch_generation",
        lambda project_dir: child_reads.append("visual") or {},
    )
    monkeypatch.setattr(
        server_mod,
        "read_review_preview_sync",
        lambda project_dir: child_reads.append("sync") or {},
    )
    app = SimpleNamespace(
        state=SimpleNamespace(recovery_tasks=set(), review_preview_recovery_errors={})
    )

    asyncio.run(server_mod._recover_workbench_background_jobs(app))

    assert child_reads == []
    assert "缺少任务编号" in app.state.review_preview_recovery_errors["plain"]


@pytest.mark.parametrize(
    "status",
    ["queued", "running", "awaiting_human", "failed", "unknown", "", None, "__missing__"],
)
def test_active_failed_or_malformed_parent_suppresses_child_recovery(
    projects_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: str | None,
) -> None:
    _write_project(projects_root)
    child_reads: list[str] = []
    monkeypatch.setattr(
        server_mod,
        "recover_review_preview_job",
        lambda project_dir: {}
        if status == "__missing__"
        else {"job_id": "RPP-one", "status": status, "launch_required": False},
    )
    monkeypatch.setattr(
        server_mod,
        "read_visual_batch_generation",
        lambda project_dir: child_reads.append("visual") or {},
    )
    monkeypatch.setattr(
        server_mod,
        "read_review_preview_sync",
        lambda project_dir: child_reads.append("sync") or {},
    )
    app = SimpleNamespace(state=SimpleNamespace(recovery_tasks=set()))

    asyncio.run(server_mod._recover_workbench_background_jobs(app))

    assert child_reads == []


def test_parent_recovery_failure_is_project_local_and_conservative(
    projects_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_project(projects_root, "broken")
    _write_project(projects_root, "healthy")
    child_reads: list[str] = []

    def recover(project_dir: Path) -> dict:
        if project_dir.name == "broken":
            raise RuntimeError("corrupt parent state")
        return {"status": "idle", "launch_required": False}

    monkeypatch.setattr(server_mod, "recover_review_preview_job", recover)
    monkeypatch.setattr(
        server_mod,
        "read_visual_batch_generation",
        lambda project_dir: child_reads.append(f"visual:{project_dir.name}") or {"generation": {"status": "idle"}},
    )
    monkeypatch.setattr(
        server_mod,
        "read_review_preview_sync",
        lambda project_dir: child_reads.append(f"sync:{project_dir.name}") or {"generation": {"status": "idle"}},
    )
    app = SimpleNamespace(state=SimpleNamespace(recovery_tasks=set()))

    asyncio.run(server_mod._recover_workbench_background_jobs(app))

    assert child_reads == ["visual:healthy", "sync:healthy"]


@pytest.mark.parametrize("parent_status", ["idle", "completed", "cancelled"])
def test_terminal_or_idle_parent_preserves_legacy_child_recovery(
    projects_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    parent_status: str,
) -> None:
    _write_project(projects_root)
    calls: list[str] = []
    monkeypatch.setattr(
        server_mod,
        "recover_review_preview_job",
        lambda project_dir: {"status": parent_status, "launch_required": False},
    )
    monkeypatch.setattr(
        server_mod,
        "read_visual_batch_generation",
        lambda project_dir: {
            "generation": {
                "job_id": "VB-one",
                "status": "queued",
                "items": [{"status": "queued"}],
            }
        },
    )
    monkeypatch.setattr(
        server_mod,
        "read_review_preview_sync",
        lambda project_dir: {"generation": {"status": "idle"}},
    )
    monkeypatch.setattr(
        server_mod,
        "generate_visual_batch",
        lambda project_dir, job_id: calls.append(f"visual:{project_dir.name}:{job_id}"),
    )
    app = SimpleNamespace(state=SimpleNamespace(recovery_tasks=set()))

    async def exercise() -> None:
        await server_mod._recover_workbench_background_jobs(app)
        pending = list(app.state.recovery_tasks)
        if pending:
            await asyncio.gather(*pending)
        await asyncio.sleep(0)

    asyncio.run(exercise())

    assert calls == ["visual:plain:VB-one"]
    assert app.state.recovery_tasks == set()
