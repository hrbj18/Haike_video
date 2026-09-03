from __future__ import annotations

import json
from pathlib import Path

from PIL import Image

from backlot import ai_vision


class FakeResponse:
    status_code = 200
    text = ""

    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def json(self) -> dict:
        return self.payload


def _chat_response(payload: dict) -> FakeResponse:
    return FakeResponse({"choices": [{"message": {"content": json.dumps(payload, ensure_ascii=False)}}]})


def test_describe_shots_sends_ordered_images_and_rejects_no_evidence(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(ai_vision, "_vision_runtime", lambda provider="default": ("secret", "https://example.invalid/v1", "luna-test"))
    frames = []
    for index, color in enumerate(("red", "blue"), 1):
        path = tmp_path / f"frame-{index}.jpg"
        Image.new("RGB", (80, 60), color).save(path)
        frames.append({"frame_id": f"FRAME-0000{index}", "path": str(path), "selected_for_vision": True})
    shot = {"shot_id": "SHOT-0001", "frames": frames}
    observed = {}

    def fake_post(url, **kwargs):
        observed.update(kwargs["json"])
        return _chat_response({
            "shots": [{
                "shot_id": "SHOT-0001",
                "summary": "红色画面切换到蓝色画面",
                "entities": [{"name": "彩色画面", "type": "test", "confidence": .9, "evidence_frame_ids": ["FRAME-00001", "FRAME-00002"]}],
                "actions": [{"name": "颜色变化", "subject": "画面", "confidence": .8, "evidence_frame_ids": ["FRAME-00001", "FRAME-00002"]}],
                "environment": "合成测试",
                "shot_type": "固定画面",
                "camera_motion": "固定",
                "state_changes": ["红色变为蓝色"],
                "screen_text": [],
                "quality": {"blur": "low", "occlusion": "none", "notes": ""},
                "unknowns": [],
                "overall_confidence": .9,
            }]
        })

    descriptions, metadata = ai_vision.describe_shots([shot], post=fake_post)

    content = observed["messages"][1]["content"]
    image_items = [item for item in content if item["type"] == "image_url"]
    assert len(image_items) == 2
    assert all(item["image_url"]["url"].startswith("data:image/jpeg;base64,") for item in image_items)
    assert all(item["image_url"]["detail"] == "auto" for item in image_items)
    assert "frame-1.jpg" not in json.dumps(observed)
    assert descriptions[0]["evidence_frame_ids"] == ["FRAME-00001", "FRAME-00002"]
    assert metadata["model"] == "luna-test"


def test_vision_preflight_requires_real_image_order(monkeypatch) -> None:
    monkeypatch.setattr(ai_vision, "_vision_runtime", lambda provider="default": ("secret", "https://example.invalid/v1", "luna-test"))
    seen = {}

    def fake_post(url, **kwargs):
        seen.update(kwargs["json"])
        return _chat_response({"sequence": [
            {"code": "K7", "color": "red"},
            {"code": "M4", "color": "green"},
            {"code": "R9", "color": "blue"},
        ]})

    result = ai_vision.test_vision_ai_connection(post=fake_post)

    assert result["status"] == "passed"
    assert sum(item["type"] == "image_url" for item in seen["messages"][1]["content"]) == 3


def test_vision_runtime_identity_invalidates_cache_when_prompt_or_image_contract_changes(monkeypatch) -> None:
    monkeypatch.setattr(ai_vision, "_vision_runtime", lambda provider="default": ("secret", "https://example.invalid/v1", "luna-test"))

    identity = ai_vision.vision_runtime_identity()

    assert identity == {
        "provider": "default",
        "model": "luna-test",
        "prompt_version": ai_vision.VISION_PROMPT_VERSION,
        "schema_version": str(ai_vision.VISION_SCHEMA_VERSION),
        "image_detail": "auto",
        "image_longest_edge": "768",
    }
