from __future__ import annotations

import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "config" / "runninghub"
SOURCE = CONFIG / "infinitetalk_source_original_0016e65b.json"
TARGET = CONFIG / "it24_fast_256x320_4s_v2.json"
RECOMMENDED_TARGET = CONFIG / "InfiniteTalk 工作流 384×480推荐档 V2.json"


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _node(workflow: dict, node_id: int) -> dict:
    return next(node for node in workflow["nodes"] if int(node["id"]) == node_id)


def test_source_snapshot_is_immutable() -> None:
    assert hashlib.sha256(SOURCE.read_bytes()).hexdigest() == (
        "0016e65bf817b2887576ca9bbc549ff29a84a33c166ec5d4641e10154d9fd2ec"
    )


def test_infinitetalk_filename_fits_runninghub_limit() -> None:
    assert len(TARGET.name) <= 50
    assert RECOMMENDED_TARGET.name == "InfiniteTalk 工作流 384×480推荐档 V2.json"
    assert len(RECOMMENDED_TARGET.name) <= 50


def test_infinitetalk_standard24_profile_is_speed_first_and_4x5() -> None:
    workflow = _load(TARGET)
    node_ids = {int(node["id"]) for node in workflow["nodes"]}

    assert _node(workflow, 2)["widgets_values"][0:3] == ["custom", 4, 5]
    assert _node(workflow, 25)["widgets_values"] == [320]
    assert _node(workflow, 14)["widgets_values"][0:2] == [256, 320]
    assert _node(workflow, 13)["widgets_values"][0] == 4
    assert _node(workflow, 13)["widgets_values"][5] is False
    assert _node(workflow, 33)["widgets_values"] == [8, False, False, True, 0, 1, False]
    assert _node(workflow, 24)["widgets_values"]["trim_to_audio"] is True
    assert _node(workflow, 24)["widgets_values"]["frame_rate"] == 25
    assert {5, 40, 42}.isdisjoint(node_ids)
    assert [node["id"] for node in workflow["nodes"] if node["type"] == "VHS_VideoCombine"] == [24]


def test_infinitetalk_recommended_profile_raises_detail_resolution_safely() -> None:
    workflow = _load(RECOMMENDED_TARGET)

    assert _node(workflow, 2)["widgets_values"][0:3] == ["custom", 4, 5]
    assert _node(workflow, 25)["widgets_values"] == [480]
    assert _node(workflow, 14)["widgets_values"][0:2] == [384, 480]
    assert _node(workflow, 13)["widgets_values"][0] == 4
    assert _node(workflow, 33)["widgets_values"] == [16, True, True, True, 0, 1, False]
    assert "35%—45%" in _node(workflow, 36)["title"]
    assert "中近景" in _node(workflow, 30)["widgets_values"][0]
    assert _node(workflow, 24)["widgets_values"]["filename_prefix"] == (
        "InfiniteTalk_384x480_recommended_V2"
    )


def test_infinitetalk_model_stack_is_unchanged() -> None:
    source = _load(SOURCE)
    workflow = _load(TARGET)

    for node_id in (3, 4, 6, 9, 11, 15, 16, 32):
        assert _node(workflow, node_id)["widgets_values"] == _node(source, node_id)[
            "widgets_values"
        ]


def test_infinitetalk_project_has_no_dangling_links() -> None:
    workflow = _load(TARGET)
    node_ids = {int(node["id"]) for node in workflow["nodes"]}
    link_ids = {int(link[0]) for link in workflow["links"]}

    assert all(int(link[1]) in node_ids and int(link[3]) in node_ids for link in workflow["links"])
    for node in workflow["nodes"]:
        for input_slot in node.get("inputs", []):
            assert input_slot.get("link") is None or int(input_slot["link"]) in link_ids
        for output_slot in node.get("outputs", []):
            assert all(int(link_id) in link_ids for link_id in output_slot.get("links") or [])


def test_infinitetalk_prompt_is_synchronized_for_natural_speaking() -> None:
    workflow = _load(TARGET)
    external_prompt_values = _node(workflow, 30)["widgets_values"]
    encoder_values = _node(workflow, 12)["widgets_values"]

    assert isinstance(external_prompt_values, list)
    assert len(external_prompt_values) == 1
    external_prompt = external_prompt_values[0]
    assert external_prompt == encoder_values[0]
    assert "自然说话" in external_prompt
    assert "不要唱歌" in external_prompt
    assert "不要跳舞" in external_prompt
    assert "唱歌" in encoder_values[1]
    assert "跳舞" in encoder_values[1]
    assert encoder_values[2:5] == [True, True, "gpu"]
