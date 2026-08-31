"""Regression tests for secure AI config and HyperFrames copy planning."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backlot import ai_text
from backlot import server
from backlot import workbench


@pytest.fixture
def isolated_ai_config(tmp_path, monkeypatch):
    monkeypatch.setattr(ai_text, "CONFIG_ROOT", tmp_path)
    for key in (*ai_text.CONFIG_KEYS, *ai_text.DOUBAO_CONFIG_KEYS, "OPENAI_SCRIPT_MODEL"):
        monkeypatch.delenv(key, raising=False)
    return tmp_path


def test_ai_config_is_masked_atomic_and_preserves_unrelated_values(isolated_ai_config):
    path = isolated_ai_config / ai_text.SECRETS_FILENAME
    path.write_text("# keep this comment\nPEXELS_API_KEY=pexels-value\n", encoding="utf-8")

    saved = ai_text.save_text_ai_config({
        "base_url": "https://relay.example/v1/",
        "model": "text-model",
        "api_key": "sk-secret-value-123456",
    })

    assert saved == {
        "configured": True,
        "api_key_masked": "sk-••••••3456",
        "base_url": "https://relay.example/v1",
        "model": "text-model",
        "storage": ".env.secrets.local",
    }
    assert "api_key" not in saved
    content = path.read_text(encoding="utf-8")
    assert "# keep this comment" in content
    assert "PEXELS_API_KEY=pexels-value" in content
    assert "OPENAI_API_KEY=\"sk-secret-value-123456\"" in content
    assert not list(isolated_ai_config.glob("..env.secrets.local.*.tmp"))


def test_blank_key_preserves_existing_secret(isolated_ai_config):
    ai_text.save_text_ai_config({
        "base_url": "https://relay.example/v1",
        "model": "first-model",
        "api_key": "sk-preserve-this-9999",
    })
    updated = ai_text.save_text_ai_config({
        "base_url": "https://relay.example/v1",
        "model": "second-model",
        "api_key": "",
    })
    assert updated["configured"] is True
    assert updated["model"] == "second-model"
    assert "sk-preserve-this-9999" in (isolated_ai_config / ai_text.SECRETS_FILENAME).read_text(encoding="utf-8")


def test_ai_config_rejects_invalid_base_url(isolated_ai_config):
    with pytest.raises(ai_text.TextAIError, match="完整地址"):
        ai_text.save_text_ai_config({"base_url": "not-a-url", "model": "model", "api_key": "sk-test"})


def test_default_text_model_is_gpt_5_6_terra(isolated_ai_config):
    config = ai_text.read_text_ai_config()
    assert config["model"] == "gpt-5.6-luna"


def test_ai_config_api_never_returns_plaintext_secret(isolated_ai_config, monkeypatch):
    async def no_watch():
        return None

    monkeypatch.setattr(server, "_watch_projects", no_watch)
    with TestClient(server.create_app()) as client:
        saved = client.put("/api/ai-text/config", json={
            "base_url": "https://relay.example/v1",
            "model": "text-model",
            "api_key": "TEST_PLACEHOLDER_PLAINTEXT_NOT_REAL",
        })
        assert saved.status_code == 200
        body = saved.json()
        assert body["configured"] is True
        assert "TEST_PLACEHOLDER_PLAINTEXT_NOT_REAL" not in saved.text
        fetched = client.get("/api/ai-text/config")
        assert fetched.status_code == 200
        assert "TEST_PLACEHOLDER_PLAINTEXT_NOT_REAL" not in fetched.text


def test_doubao_config_is_independent_masked_and_preserves_main_model(isolated_ai_config):
    ai_text.save_text_ai_config({
        "base_url": "https://relay.example/v1", "model": "review-model", "api_key": "TEST_MAIN_KEY_NOT_REAL",
    })
    saved = ai_text.save_doubao_text_ai_config({
        "base_url": "https://ark.example/v1", "model": "doubao-writer", "api_key": "TEST_DOUBAO_KEY_NOT_REAL",
    })

    secret_file = (isolated_ai_config / ai_text.SECRETS_FILENAME).read_text(encoding="utf-8")
    assert saved["configured"] is True
    assert saved["provider"] == "doubao"
    assert "TEST_DOUBAO_KEY_NOT_REAL" not in json.dumps(saved, ensure_ascii=False)
    assert "OPENAI_TEXT_MODEL=\"review-model\"" in secret_file
    assert "DOUBAO_TEXT_MODEL=\"doubao-writer\"" in secret_file
    providers = ai_text.read_text_provider_status()
    assert providers["selection_provider"] == "doubao"
    assert providers["writer_provider"] == "doubao"
    assert providers["reviewer_provider"] == "doubao"
    assert providers["technical_fallback_provider"] == "default"


def test_chat_json_uses_doubao_endpoint_and_model(monkeypatch, isolated_ai_config):
    ai_text.save_doubao_text_ai_config({
        "base_url": "https://ark.example/v1", "model": "doubao-writer", "api_key": "TEST_PROVIDER_KEY_NOT_REAL",
    })
    seen = {}

    class Response:
        status_code = 200
        text = ""

        @staticmethod
        def iter_lines(decode_unicode=False):
            yield b'data: {"choices":[{"delta":{"content":"{\\"ok\\":true}"}}]}'
            yield b"data: [DONE]"

    def fake_post(url, *, headers, json, timeout, stream):
        seen.update({
            "url": url,
            "authorization": headers["Authorization"],
            "model": json["model"],
            "thinking": json.get("thinking"),
            "max_tokens": json.get("max_tokens"),
        })
        return Response()

    import requests
    monkeypatch.setattr(requests, "post", fake_post)
    result, model = ai_text._chat_json("json", {"task": "writer"}, provider="doubao")

    assert result == {"ok": True}
    assert model == "doubao-writer"
    assert seen["url"] == "https://ark.example/v1/chat/completions"
    assert seen["model"] == "doubao-writer"
    assert seen["authorization"] == "Bearer TEST_PROVIDER_KEY_NOT_REAL"
    assert seen["thinking"] == {"type": "disabled"}
    assert seen["max_tokens"] == 4096


def test_doubao_config_api_masks_secret_and_exposes_provider_role(isolated_ai_config, monkeypatch):
    async def no_watch():
        return None

    monkeypatch.setattr(server, "_watch_projects", no_watch)
    with TestClient(server.create_app()) as client:
        saved = client.put("/api/ai-text/doubao/config", json={
            "base_url": "https://ark.example/v1",
            "model": "doubao-writer",
            "api_key": "db-api-plaintext-must-not-return-8888",
        })
        providers = client.get("/api/ai-text/providers")

    assert saved.status_code == 200
    assert providers.status_code == 200
    assert "db-api-plaintext-must-not-return-8888" not in saved.text
    assert "db-api-plaintext-must-not-return-8888" not in providers.text
    assert providers.json()["writer_provider"] == "doubao"
    assert providers.json()["doubao"]["role"] == "daily_editorial_owner"


def test_visual_copy_planner_normalizes_structured_result(monkeypatch):
    monkeypatch.setattr(ai_text, "_chat_json", lambda system, context: ({
        "scene_goal": "解释数字人如何从虚构形象走向商业角色",
        "headline": "数字人的商业化进程",
        "center_label": "商业价值",
        "nodes": ["虚拟身份", "粉丝积累", "品牌合作", "粉丝积累"],
        "scene_recipe": "relationship_map",
    }, "mock-model"))
    planned = ai_text.plan_visual_copy({"current_spoken_text": "测试台词"})
    assert planned["headline"] == "数字人的商业化进程"
    assert planned["center_label"] == "商业价值"
    assert planned["nodes"] == ["虚拟身份", "粉丝积累", "品牌合作"]
    assert planned["layout_variant"] == "radial_map"
    assert planned["motion_variant"] == "node_bloom"
    assert planned["model"] == "mock-model"
    assert planned["fingerprint"]


def test_chat_json_uses_longer_default_timeout(monkeypatch, isolated_ai_config):
    ai_text.save_text_ai_config({
        "base_url": "https://relay.example/v1", "model": "text-model", "api_key": "sk-timeout-test",
    })
    seen = {}

    class Response:
        status_code = 200

        def json(self):
            return {"choices": [{"message": {"content": '{"ok": true}'}}]}

    def fake_post(_url, *, headers, json, timeout, stream):
        seen["timeout"] = timeout
        seen["stream"] = stream
        return Response()

    import requests
    monkeypatch.setattr(requests, "post", fake_post)
    result, _model = ai_text._chat_json("json", {"task": "test"})
    assert result == {"ok": True}
    assert seen["timeout"] == ai_text.DEFAULT_JSON_REQUEST_TIMEOUT_SECONDS
    assert seen["stream"] is True


def test_chat_json_collects_streaming_sse_deltas(monkeypatch, isolated_ai_config):
    ai_text.save_text_ai_config({
        "base_url": "https://relay.example/v1", "model": "text-model", "api_key": "sk-stream-test",
    })

    class Response:
        status_code = 200
        text = ""

        def iter_lines(self, decode_unicode=True):
            yield 'data: {"choices":[{"delta":{"content":"{\\"ok\\":"}}]}'
            yield 'data: {"choices":[{"delta":{"content":"true}"}}]}'
            yield "data: [DONE]"

        def json(self):
            raise AssertionError("流式响应不应回退读取普通JSON")

    def fake_post(_url, *, headers, json, timeout, stream):
        assert headers["Accept"] == "text/event-stream"
        assert json["stream"] is True
        assert stream is True
        return Response()

    import requests
    monkeypatch.setattr(requests, "post", fake_post)

    result, model = ai_text._chat_json("json", {"task": "test"})

    assert result == {"ok": True}
    assert model == "text-model"


def test_chat_json_accepts_complete_json_when_stream_ends_without_done(monkeypatch, isolated_ai_config):
    ai_text.save_text_ai_config({
        "base_url": "https://relay.example/v1", "model": "text-model", "api_key": "sk-stream-cutoff-test",
    })

    class Response:
        status_code = 200
        text = ""

        def iter_lines(self, decode_unicode=True):
            yield 'data: {"choices":[{"delta":{"content":"{\\"ok\\":true}"}}]}'
            raise RuntimeError("Response ended prematurely")

        def json(self):
            raise AssertionError("完整流式JSON不应回退")

    import requests
    monkeypatch.setattr(requests, "post", lambda *args, **kwargs: Response())

    result, _ = ai_text._chat_json("json", {"task": "test"})

    assert result == {"ok": True}


def test_chat_json_decodes_chinese_sse_as_utf8_when_charset_is_missing(monkeypatch, isolated_ai_config):
    ai_text.save_text_ai_config({
        "base_url": "https://relay.example/v1", "model": "text-model", "api_key": "sk-stream-utf8-test",
    })

    class Response:
        status_code = 200
        text = ""

        def iter_lines(self, decode_unicode=False):
            assert decode_unicode is False
            yield 'data: {"choices":[{"delta":{"content":"{\\"标题\\":\\"每日科技快讯\\"}"}}]}'.encode("utf-8")
            yield b"data: [DONE]"

        def json(self):
            raise AssertionError("流式响应不应回退")

    import requests
    monkeypatch.setattr(requests, "post", lambda *args, **kwargs: Response())

    result, _ = ai_text._chat_json("json", {"task": "test"})

    assert result == {"标题": "每日科技快讯"}


def test_streaming_response_has_a_total_deadline(monkeypatch):
    moments = iter((10.0, 191.0))
    monkeypatch.setattr(ai_text.time, "monotonic", lambda: next(moments))

    class Response:
        @staticmethod
        def iter_lines(decode_unicode=False):
            yield b""

    with pytest.raises(ai_text.TextAIError, match="超过总时限"):
        ai_text._streamed_chat_content(Response(), max_elapsed_seconds=180)


def test_visual_route_planner_preserves_server_owned_slot_timing(monkeypatch):
    context = {
        "scenes": [{
            "scene_id": "scene-b",
            "slots": [
                {"block_id": "VB-001", "start_seconds": 0, "end_seconds": 4},
                {"block_id": "VB-002", "start_seconds": 4, "end_seconds": 8},
            ],
        }],
    }
    monkeypatch.setattr(ai_text, "_chat_json", lambda system, payload: ({
        "summary": "前半段用真实设备，后半段用关系图",
        "blocks": [
            {
                "scene_id": "scene-b", "block_id": "VB-001", "route": "stock_video",
                "visual_intent": "机器人生产线", "reason": "需要真实运动", "confidence": .91,
                "search_query": "industrial robot assembly line", "scene_recipe": "process",
                "fallback_route": "stock_image", "start_seconds": 99, "end_seconds": 100,
            },
            {
                "scene_id": "scene-b", "block_id": "VB-002", "route": "hyperframes",
                "visual_intent": "商业价值关系", "reason": "抽象关系更适合动态图形", "confidence": .83,
                "search_query": "", "scene_recipe": "relationship_map", "fallback_route": "stock_video",
                "graphic_copy": {
                    "scene_goal": "解释数字人如何产生商业价值",
                    "headline": "数字人走向商业化",
                    "supporting_statement": "身份经营让虚拟角色连接现实业务",
                    "center_label": "数字人",
                    "nodes": ["固定形象", "粉丝关系", "宣传参与", "商业价值"],
                },
            },
        ],
    }, "mock-model"))

    planned = ai_text.plan_visual_routes(context)

    assert planned["model"] == "mock-model"
    assert planned["fingerprint"]
    assert [(item["route"], item["start_seconds"], item["end_seconds"]) for item in planned["blocks"]] == [
        ("stock_video", 0, 4), ("hyperframes", 4, 8),
    ]
    assert planned["blocks"][0]["search_query"] == "industrial robot assembly line"
    assert planned["blocks"][1]["graphic_copy"]["headline"] == "数字人走向商业化"
    assert planned["blocks"][1]["layout_variant"] == "radial_map"
    assert planned["blocks"][1]["motion_variant"] == "node_bloom"


def test_visual_route_planner_rejects_hyperframes_without_reviewable_copy(monkeypatch):
    context = {"scenes": [{"scene_id": "scene-b", "slots": [{"block_id": "VB-001", "start_seconds": 0, "end_seconds": 4}]}]}
    monkeypatch.setattr(ai_text, "_chat_json", lambda system, payload: ({
        "blocks": [{
            "scene_id": "scene-b", "block_id": "VB-001", "route": "hyperframes",
            "visual_intent": "抽象关系", "scene_recipe": "relationship_map",
        }],
    }, "mock-model"))

    with pytest.raises(ai_text.TextAIError, match="画面目标或归纳标题"):
        ai_text.plan_visual_routes(context)


def test_visual_route_planner_rejects_missing_or_unknown_slot(monkeypatch):
    context = {"scenes": [{"scene_id": "scene-b", "slots": [{"block_id": "VB-001", "start_seconds": 0, "end_seconds": 4}]}]}
    monkeypatch.setattr(ai_text, "_chat_json", lambda system, payload: ({
        "blocks": [{"scene_id": "scene-b", "block_id": "VB-404", "route": "hyperframes"}],
    }, "mock-model"))

    with pytest.raises(ai_text.TextAIError, match="缺少槽位"):
        ai_text.plan_visual_routes(context)


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _make_project(root: Path) -> Path:
    project = root / "film"
    _write_json(project / "project.json", {
        "project_id": "film", "title": "科技快报", "pipeline_type": "animated-explainer",
    })
    sections = [
        {"id": "s1", "text": "上一段说明数字角色开始走红。", "start_seconds": 0, "end_seconds": 4},
        {"id": "s2", "text": "重点不是表演本身，而是数字人能够积累粉丝并产生商业价值。", "start_seconds": 4, "end_seconds": 9},
        {"id": "s3", "text": "下一段将讨论品牌合作。", "start_seconds": 9, "end_seconds": 13},
    ]
    _write_json(project / "artifacts" / "script.json", {"title": "科技快报", "sections": sections})
    _write_json(project / "artifacts" / "scene_plan.json", {"scenes": [
        {"id": "scene-a", "title": "上一段", "description": sections[0]["text"], "start_seconds": 0, "end_seconds": 4, "script_section_id": "s1"},
        {"id": "scene-b", "title": "核心段", "description": sections[1]["text"], "start_seconds": 4, "end_seconds": 9, "script_section_id": "s2"},
        {"id": "scene-c", "title": "下一段", "description": sections[2]["text"], "start_seconds": 9, "end_seconds": 13, "script_section_id": "s3"},
    ]})
    return project


def test_refine_scene_copy_uses_neighbours_and_persists_editable_fields(tmp_path, monkeypatch):
    project = _make_project(tmp_path)
    workbench.bootstrap_workbench(project)
    workbench.update_scene_visual_plan(project, "scene-b", {
        "engine": "hyperframes",
        "prompt": "设计数字人商业价值关系图。",
        "structured_spec": {"headline": "规则草稿", "components": [], "scene_recipe": "relationship_map"},
    })
    captured = {}
    monkeypatch.setattr(workbench, "read_text_ai_config", lambda: {"configured": True})

    def fake_plan(context):
        captured.update(context)
        return {
            "scene_goal": "解释数字人的商业价值",
            "headline": "数字人走向商业角色",
            "center_label": "商业价值",
            "nodes": ["虚拟身份", "粉丝积累", "品牌合作"],
            "scene_recipe": "relationship_map",
            "model": "mock-model",
            "fingerprint": "fingerprint",
            "generated_at": "2026-08-17T00:00:00+00:00",
        }

    monkeypatch.setattr(workbench, "plan_visual_copy", fake_plan)
    state = workbench.refine_scene_visual_copy(project, "scene-b", {})
    scene = next(item for item in state["scenes"] if item["id"] == "scene-b")
    spec = scene["visual_plan"]["structured_spec"]
    assert captured["previous_context"] == "上一段说明数字角色开始走红。"
    assert captured["current_spoken_text"] == "重点不是表演本身，而是数字人能够积累粉丝并产生商业价值。"
    assert captured["next_context"] == "下一段将讨论品牌合作。"
    assert spec["headline"] == "数字人走向商业角色"
    assert spec["center_label"] == "商业价值"
    assert spec["components"] == ["虚拟身份", "粉丝积累", "品牌合作"]
    assert spec["copy_plan"]["status"] == "ready"
    assert "api_key" not in json.dumps(state, ensure_ascii=False).lower()
