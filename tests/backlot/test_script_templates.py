"""Template-script discovery and one-click avatar preparation tests."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backlot import server as server_mod
from backlot import state as state_mod
from backlot.script_templates import build_avatar_script_from_template, list_avatar_script_templates, preview_avatar_script_template
from backlot.workbench import WorkbenchError, import_avatar_script_template
from schemas.artifacts import validate_artifact


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def make_empty_avatar_project(root: Path) -> Path:
    project = root / "template-avatar"
    project.mkdir(parents=True)
    write_json(project / "project.json", {
        "project_id": "template-avatar",
        "title": "测试 1",
        "pipeline_type": "avatar-spokesperson",
        "intake": {"duration_seconds": 60, "aspect": "portrait"},
    })
    return project


def public_dual_host_template() -> dict:
    templates = list_avatar_script_templates()["templates"]
    return next(item for item in templates if item["episode_id"] == "public" and item["filename"] == "dual-host-four-line.md")


def test_public_template_is_discoverable_and_reviewable():
    template = public_dual_host_template()
    preview = preview_avatar_script_template(template["template_id"])

    assert preview["title"] == "双主持四句科技口播"
    assert preview["turn_count"] == 4
    assert [turn["turn_id"] for turn in preview["turns"]] == [f"T{index:03d}" for index in range(1, 5)]
    assert [speaker["speaker_id"] for speaker in preview["speakers"]] == ["yaya", "mengmeng"]
    assert "source_text" not in preview


def test_template_builds_schema_valid_multi_speaker_script():
    template = public_dual_host_template()
    script, provenance = build_avatar_script_from_template(template["template_id"])

    validate_artifact("script", script)
    assert script["metadata"]["timing_basis"] == "script_estimate_pending_native_avatar_audio"
    assert [section["speaker_id"] for section in script["sections"][:4]] == ["yaya", "mengmeng", "yaya", "mengmeng"]
    assert provenance["template_id"] == template["template_id"]
    assert len(provenance["source_sha256"]) == 64


def test_template_import_initializes_script_scene_plan_and_cloud_avatar_contract(tmp_path: Path):
    project = make_empty_avatar_project(tmp_path)
    template = public_dual_host_template()

    state = import_avatar_script_template(project, {
        "template_id": template["template_id"],
        "generation_mode": "dashscope_wan_s2v",
        "background_mode": "opaque",
        "default_treatment": "fullscreen",
    })

    script = json.loads((project / "artifacts" / "script.json").read_text(encoding="utf-8"))
    scene_plan = json.loads((project / "artifacts" / "scene_plan.json").read_text(encoding="utf-8"))
    package = json.loads((project / "artifacts" / "avatar_source_package.json").read_text(encoding="utf-8"))
    record = json.loads((project / "artifacts" / "script_import.json").read_text(encoding="utf-8"))

    validate_artifact("script", script)
    validate_artifact("avatar_source_package", package)
    assert len(state["scenes"]) == len(script["sections"]) == len(scene_plan["scenes"]) == 4
    assert package["generation_mode"] == "dashscope_wan_s2v"
    assert package["import_mode"] == "per_turn"
    assert [binding["speaker_id"] for binding in package["speaker_bindings"]] == ["yaya", "mengmeng"]
    assert state["project"]["script_draft"]["status"] == "approved"
    assert (project / record["source_snapshot"]).is_file()
    history_root = project / "artifacts" / "script_import_history"
    assert not history_root.exists()


def test_reimport_requires_confirmation_and_creates_a_recoverable_backup(tmp_path: Path):
    project = make_empty_avatar_project(tmp_path)
    template = public_dual_host_template()
    payload = {"template_id": template["template_id"], "generation_mode": "manual_import"}
    import_avatar_script_template(project, payload)

    with pytest.raises(WorkbenchError, match="确认覆盖"):
        import_avatar_script_template(project, payload)

    import_avatar_script_template(project, {**payload, "replace_confirmed": True})
    archives = list((project / "artifacts" / "script_import_history").glob("*/artifacts/script.json"))
    assert len(archives) == 1
    assert json.loads(archives[0].read_text(encoding="utf-8"))["title"]


def test_template_api_lists_previews_and_initializes_a_project(tmp_path: Path, monkeypatch):
    projects_root = tmp_path / "projects"
    projects_root.mkdir()
    make_empty_avatar_project(projects_root)
    monkeypatch.setattr(state_mod, "PROJECTS_DIR", projects_root)
    monkeypatch.setattr(server_mod, "PROJECTS_DIR", projects_root)
    monkeypatch.setattr(server_mod, "_summary_cache", {})
    monkeypatch.setattr(server_mod, "_PROJECTS_ROOT_STR", os.path.normcase(str(projects_root.resolve())))

    async def no_watch():
        return None

    monkeypatch.setattr(server_mod, "_watch_projects", no_watch)
    with TestClient(server_mod.create_app()) as client:
        listed = client.get("/api/script-templates/avatar")
        assert listed.status_code == 200
        template = next(item for item in listed.json()["templates"] if item["episode_id"] == "public" and item["filename"] == "dual-host-four-line.md")
        preview = client.get("/api/script-templates/avatar/preview", params={"template_id": template["template_id"]})
        assert preview.status_code == 200
        assert preview.json()["turn_count"] == 4
        assert "source_text" not in preview.json()

        initialized = client.post("/api/project/template-avatar/workbench/avatar-script/template", json={
            "template_id": template["template_id"],
            "generation_mode": "manual_import",
        })
        assert initialized.status_code == 200
        assert len(initialized.json()["avatar_package"]["turns"]) == 4
