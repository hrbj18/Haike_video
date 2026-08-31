"""Deterministic DOCX and pasted-script import tests."""

from __future__ import annotations

import io
import json
import os
from pathlib import Path

from docx import Document
from fastapi.testclient import TestClient

from backlot import server as server_mod
from backlot import state as state_mod
from backlot.script_imports import parse_docx_script, parse_text_script, stage_docx_preview
from backlot.workbench import import_avatar_user_script
from schemas.artifacts import validate_artifact


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def make_avatar_project(root: Path, project_id: str = "user-script-avatar") -> Path:
    project = root / project_id
    project.mkdir(parents=True)
    write_json(project / "project.json", {
        "project_id": project_id,
        "title": "用户脚本测试",
        "pipeline_type": "avatar-spokesperson",
        "intake": {"duration_seconds": 90, "aspect": "portrait"},
    })
    return project


def docx_bytes(lines: list[str]) -> bytes:
    document = Document()
    for line in lines:
        document.add_paragraph(line)
    output = io.BytesIO()
    document.save(output)
    return output.getvalue()


def first_episode_lines() -> list[str]:
    lines = ["抖音双主持 AI 新闻快报｜顺序编号版 + 纯净台词版"]
    speakers = ["雅雅", "雅雅", "檬檬", "雅雅", "檬檬", "雅雅", "檬檬", "雅雅", "檬檬", "雅雅", "檬檬", "雅雅", "檬檬", "雅雅"]
    lines.extend(f"T{index} {speaker}：第{index}段原始台词，不允许改写。" for index, speaker in enumerate(speakers, 1))
    lines.extend(["二、纯净台词版", "雅雅：这段内容不应被重复导入。", "檬檬：这段内容也不应被导入。"])
    return lines


def test_docx_parser_preserves_first_numbered_block_and_known_speakers():
    preview = parse_docx_script(docx_bytes(first_episode_lines()), filename="第一期.docx")

    assert preview["turn_count"] == 14
    assert [turn["turn_id"] for turn in preview["turns"]] == [f"T{index:03d}" for index in range(1, 15)]
    assert preview["turns"][0]["text"] == "第1段原始台词，不允许改写。"
    assert preview["turns"][-1]["text"] == "第14段原始台词，不允许改写。"
    assert preview["speakers"] == [
        {"speaker_id": "yaya", "name": "雅雅"},
        {"speaker_id": "mengmeng", "name": "檬檬"},
    ]


def test_pasted_script_without_turn_ids_gets_stable_ids_and_warning():
    preview = parse_text_script("雅雅：第一句。\n檬檬：第二句。", title="粘贴脚本")

    assert [turn["turn_id"] for turn in preview["turns"]] == ["T001", "T002"]
    assert any("补齐 T001" in warning for warning in preview["warnings"])


def test_user_docx_commit_initializes_schema_valid_avatar_contract(tmp_path: Path):
    project = make_avatar_project(tmp_path)
    preview = stage_docx_preview(project, docx_bytes(first_episode_lines()), filename="第一期.docx")

    state = import_avatar_user_script(project, {
        "import_token": preview["import_token"],
        "speaker_overrides": {"雅雅": "yaya", "檬檬": "mengmeng"},
        "generation_mode": "manual_import",
        "import_mode": "longform",
        "background_mode": "opaque",
        "default_treatment": "pip_top_left",
    })

    script = json.loads((project / "artifacts" / "script.json").read_text(encoding="utf-8"))
    package = json.loads((project / "artifacts" / "avatar_source_package.json").read_text(encoding="utf-8"))
    record = json.loads((project / "artifacts" / "script_import.json").read_text(encoding="utf-8"))
    validate_artifact("script", script)
    validate_artifact("avatar_source_package", package)
    assert len(script["sections"]) == len(state["scenes"]) == len(package["turns"]) == 14
    assert script["metadata"]["text_policy"] == "verbatim_no_ai_rewrite"
    assert [speaker["speaker_id"] for speaker in package["speakers"]] == ["yaya", "mengmeng"]
    assert package["import_mode"] == "longform"
    assert (project / record["source_snapshot"]).suffix == ".docx"
    assert not (project / "artifacts" / "script_import_staging" / f"{preview['import_token']}.json").exists()


def test_user_script_api_previews_docx_and_commits(tmp_path: Path, monkeypatch):
    projects_root = tmp_path / "projects"
    projects_root.mkdir()
    make_avatar_project(projects_root, "api-user-script")
    monkeypatch.setattr(state_mod, "PROJECTS_DIR", projects_root)
    monkeypatch.setattr(server_mod, "PROJECTS_DIR", projects_root)
    monkeypatch.setattr(server_mod, "_summary_cache", {})
    monkeypatch.setattr(server_mod, "_PROJECTS_ROOT_STR", os.path.normcase(str(projects_root.resolve())))

    async def no_watch():
        return None

    monkeypatch.setattr(server_mod, "_watch_projects", no_watch)
    with TestClient(server_mod.create_app()) as client:
        preview_response = client.put(
            "/api/project/api-user-script/workbench/avatar-script/imports/preview",
            params={"filename": "第一期.docx"},
            content=docx_bytes(first_episode_lines()),
            headers={"Content-Type": "application/octet-stream"},
        )
        assert preview_response.status_code == 200
        preview = preview_response.json()
        assert preview["turn_count"] == 14
        assert "import_token" in preview

        committed = client.post(
            "/api/project/api-user-script/workbench/avatar-script/imports/commit",
            json={
                "import_token": preview["import_token"],
                "generation_mode": "manual_import",
                "import_mode": "per_turn",
                "background_mode": "opaque",
                "default_treatment": "fullscreen",
            },
        )
        assert committed.status_code == 200
        assert len(committed.json()["avatar_package"]["turns"]) == 14
        assert committed.json()["project"]["script_draft"]["mode"] == "deterministic_user_import"
