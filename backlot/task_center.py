"""Project-local task-centre read model.

The workbench persists each worker near its domain (visual batch, narration,
keyframes and rendering).  This module deliberately does not create a second
queue.  It translates those records into one stable payload so the task drawer
can be refreshed without rebuilding the video-review surface.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any


RUNNING = {"queued", "generating", "running", "rendering", "uploading", "submitted", "downloading"}
WAITING = {"awaiting_human"}
TERMINAL = {"completed", "completed_with_warnings", "completed_with_failures", "failed", "cancelled"}

REVIEW_PREVIEW_STAGE_LABELS = {
    "preflight": "可信预检",
    "scene_plan": "建立分镜草案",
    "line_plan": "拆分逐句台账",
    "narration": "逐句生成配音",
    "audio_timeline": "建立真实音频时间线",
    "subtitles": "生成字幕",
    "visual_plan": "规划主体画面",
    "visual_generation": "生成主体画面",
    "audio_sample": "等待声音样板确认",
    "full_preview": "合成全片审核预览",
    "review_ready": "等待人工观看",
}


def _number(value: Any, fallback: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def _status(value: Any) -> str:
    raw = str(value or "idle")
    if raw in {"idle", "not_started", "saved", "ready", "needs_refresh", "stale"}:
        return "idle"
    if raw in RUNNING | WAITING | TERMINAL:
        return raw
    if raw in {"assets_ready", "review_ready", "timeline_applied", "native_avatar_audio", "succeeded", "passed"}:
        return "completed"
    return raw


def _error_message(value: Any) -> str:
    """Keep task-centre errors useful without exposing Python container repr."""

    if isinstance(value, dict):
        for key in ("message", "detail", "reason"):
            text = value.get(key)
            if isinstance(text, str) and text.strip():
                return text.strip()[:1200]
        return "任务失败；请打开任务详情查看可恢复位置和处理建议。" if value else ""
    if isinstance(value, str):
        return value.strip()[:1200]
    return ""


def _task(
    *,
    task_id: str,
    kind: str,
    title: str,
    job: dict[str, Any],
    stage: str = "",
    scene_id: str | None = None,
    target_view: str = "review",
    retry: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    status = _status(job.get("status"))
    if status == "idle":
        return None
    counts = job.get("counts") if isinstance(job.get("counts"), dict) else {}
    total_raw = counts.get("total", job.get("total_slots", job.get("total_scenes", job.get("expected_count", 0))))
    done_raw = counts.get("completed", job.get("completed_slots", job.get("completed_scenes", job.get("completed_count", 0))))
    failed_raw = counts.get("failed", job.get("failed_slots", job.get("failed_scenes", 0)))
    total = max(0, int(_number(total_raw)))
    done = max(0, int(_number(done_raw)))
    failed = max(0, int(_number(failed_raw)))
    progress = min(1.0, done / total) if total else (1.0 if status in TERMINAL else 0.0)
    result = job.get("result")
    if not isinstance(result, dict):
        result = {key: job.get(key) for key in ("asset_id", "output_path", "report_path", "review_id") if job.get(key)}
    raw_stage = str(job.get("stage") or stage or "处理中")
    if kind == "review_preview_pipeline":
        raw_stage = REVIEW_PREVIEW_STAGE_LABELS.get(raw_stage, raw_stage)
    error = job.get("error")
    error_contract = error if isinstance(error, dict) else {}
    gate = job.get("gate") if isinstance(job.get("gate"), dict) else None
    action = job.get("action") if isinstance(job.get("action"), dict) else None
    if action is None and gate:
        action = {
            "type": "human_gate",
            "label": gate.get("required_action") or "人工确认后继续",
        }
    return {
        "id": task_id,
        "kind": kind,
        "title": title,
        "status": status,
        "stage": raw_stage,
        "progress": {"completed": done, "total": total, "failed": failed, "ratio": round(progress, 4)},
        "scene_id": scene_id,
        "target_view": target_view,
        "created_at": job.get("created_at") or job.get("started_at"),
        "started_at": job.get("started_at"),
        "finished_at": job.get("finished_at"),
        "error": _error_message(error),
        "result": deepcopy(result),
        "retry": retry,
        "current": deepcopy(job.get("current")) if isinstance(job.get("current"), dict) else None,
        "gate": deepcopy(gate),
        "retryable": bool(error_contract.get("retryable")) if "retryable" in error_contract else None,
        "safe_resume_point": job.get("safe_resume_point"),
        "action": deepcopy(action),
    }


def collect_tasks(state: dict[str, Any]) -> dict[str, Any]:
    """Return a compact, non-mutating view of current and recent project jobs."""
    tasks: list[dict[str, Any]] = []
    automation = state.get("automation") if isinstance(state.get("automation"), dict) else {}
    mappings = (
        ("review_preview_pipeline", "review_preview_pipeline", "一键生成审核预览", "项目总览"),
        ("visual_batch", "visual_batch", "批量补全画面", "片段工作台"),
        ("preview_sync", "preview_sync", "同步片段审核预览", "片段工作台"),
        ("asset_generation", "network_assets", "下载网络素材", "项目总览"),
        ("narration_generation", "project_narration", "生成项目配音", "项目总览"),
        ("preview_render", "full_preview", "合成全片预览", "成片与版本"),
        ("render", "formal_render", "合成正式成片", "成片与版本"),
    )
    for key, kind, title, stage in mappings:
        job = automation.get(key)
        if isinstance(job, dict):
            task = _task(
                task_id=str(job.get("job_id") or f"automation:{key}"),
                kind=kind,
                title=title,
                job=job,
                stage=stage,
                target_view="quality" if key in {"preview_render", "render"} else "review",
            )
            if task:
                tasks.append(task)

    for scene in state.get("scenes") or []:
        if not isinstance(scene, dict):
            continue
        scene_id = str(scene.get("id") or "")
        title_suffix = str(scene.get("title") or scene_id)
        for field, kind, label in (
            ("keyframe_generation", "keyframes", "AI 关键帧"),
            ("motion_generation", "motion_visual", "动态画面"),
            ("ppt_card_generation", "ppt_card", "PPT 信息卡"),
        ):
            job = scene.get(field)
            if isinstance(job, dict):
                retry = None
                if field == "ppt_card_generation" and _status(job.get("status")) == "failed":
                    retry = {"action": "retry_ppt_card", "scene_id": scene_id, "job_id": job.get("job_id")}
                task = _task(
                    task_id=str(job.get("job_id") or f"scene:{scene_id}:{field}"),
                    kind=kind,
                    title=f"{label} · {title_suffix}",
                    job=job,
                    stage="片段工作台",
                    scene_id=scene_id,
                    retry=retry,
                )
                if task:
                    tasks.append(task)
        narration = scene.get("narration") if isinstance(scene.get("narration"), dict) else {}
        job = narration.get("job") if isinstance(narration.get("job"), dict) else None
        if job:
            task = _task(
                task_id=str(job.get("job_id") or job.get("version_id") or f"scene:{scene_id}:narration"),
                kind="scene_narration",
                title=f"片段配音 · {title_suffix}",
                job=job,
                stage="片段工作台",
                scene_id=scene_id,
            )
            if task:
                tasks.append(task)

    project = state.get("project") if isinstance(state.get("project"), dict) else {}
    project_id = str(project.get("project_id") or project.get("id") or "")
    for task in tasks:
        task["project_id"] = project_id

    rank = {
        "running": 0,
        "generating": 0,
        "queued": 1,
        "rendering": 1,
        "awaiting_human": 2,
        "failed": 3,
        "completed_with_warnings": 4,
        "completed_with_failures": 4,
        "completed": 5,
        "cancelled": 6,
    }
    tasks.sort(key=lambda item: (rank.get(item["status"], 6), str(item.get("started_at") or item.get("created_at") or "")))
    active = sum(1 for item in tasks if item["status"] in RUNNING)
    waiting = sum(1 for item in tasks if item["status"] in WAITING)
    failures = sum(1 for item in tasks if item["status"] in {"failed", "completed_with_failures"})
    return {
        "version": 1,
        "active_count": active,
        "waiting_count": waiting,
        "failure_count": failures,
        "completed_count": sum(1 for item in tasks if item["status"] in TERMINAL - {"failed", "cancelled"}),
        "tasks": tasks[:80],
    }
