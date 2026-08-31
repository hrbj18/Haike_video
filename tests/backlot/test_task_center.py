from __future__ import annotations

from backlot.task_center import collect_tasks


def _state(job: dict) -> dict:
    return {
        "project": {"project_id": "film"},
        "automation": {"review_preview_pipeline": job},
        "scenes": [],
    }


def test_review_preview_waiting_job_exposes_nested_progress_and_resume_contract():
    payload = collect_tasks(
        _state(
            {
                "job_id": "RPP-1",
                "status": "awaiting_human",
                "stage": "audio_sample",
                "counts": {"total": 3, "completed": 2, "failed": 0},
                "current": {"kind": "gate", "id": "audio_sample", "label": "等待人工试听"},
                "gate": {
                    "stage": "audio_sample",
                    "reason": "需要试听声音样板",
                    "required_action": "确认声音样板后继续",
                },
                "safe_resume_point": "audio_sample",
                "error": None,
            }
        )
    )

    assert payload["active_count"] == 0
    assert payload["waiting_count"] == 1
    task = payload["tasks"][0]
    assert task["kind"] == "review_preview_pipeline"
    assert task["status"] == "awaiting_human"
    assert task["stage"] == "等待声音样板确认"
    assert task["progress"] == {"completed": 2, "total": 3, "failed": 0, "ratio": 0.6667}
    assert task["safe_resume_point"] == "audio_sample"
    assert task["action"] == {"type": "human_gate", "label": "确认声音样板后继续"}


def test_review_preview_failure_uses_chinese_message_not_python_dict_repr():
    payload = collect_tasks(
        _state(
            {
                "job_id": "RPP-2",
                "status": "failed",
                "stage": "narration",
                "counts": {"total": 3, "completed": 1, "failed": 1},
                "safe_resume_point": "narration",
                "error": {"type": "LocalTTSUnavailable", "message": "本地配音暂不可用，请修复后续跑。", "retryable": True},
            }
        )
    )

    task = payload["tasks"][0]
    assert payload["failure_count"] == 1
    assert payload["waiting_count"] == 0
    assert task["error"] == "本地配音暂不可用，请修复后续跑。"
    assert "{'" not in task["error"]
    assert task["retryable"] is True
    assert task["safe_resume_point"] == "narration"


def test_review_preview_running_and_completed_counts_remain_independent():
    running = collect_tasks(
        _state(
            {
                "job_id": "RPP-3",
                "status": "running",
                "stage": "full_preview",
                "counts": {"total": 3, "completed": 3, "failed": 0},
            }
        )
    )
    completed = collect_tasks(
        _state(
            {
                "job_id": "RPP-4",
                "status": "completed",
                "stage": "review_ready",
                "counts": {"total": 3, "completed": 3, "failed": 0},
                "result": {"readiness": "preview_ready"},
            }
        )
    )

    assert running["active_count"] == 1
    assert running["waiting_count"] == 0
    assert completed["completed_count"] == 1
    assert completed["tasks"][0]["stage"] == "等待人工观看"
    assert completed["tasks"][0]["result"]["readiness"] == "preview_ready"
