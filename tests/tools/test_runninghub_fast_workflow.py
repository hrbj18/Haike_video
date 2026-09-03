from __future__ import annotations

import json
from pathlib import Path

from scripts.build_runninghub_fast_workflow import PROFILES


ROOT = Path(__file__).resolve().parents[2]
RUNNINGHUB = ROOT / "config" / "runninghub"


def _load(name: str) -> dict:
    return json.loads((RUNNINGHUB / name).read_text(encoding="utf-8"))


def _project_node(workflow: dict, node_id: int) -> dict:
    return next(node for node in workflow["nodes"] if int(node["id"]) == node_id)


def test_fast_4x5_project_is_importable_and_keeps_audio_contract() -> None:
    workflow = _load("lc_fast_256x320_4s.json")
    node_ids = {int(node["id"]) for node in workflow["nodes"]}

    assert _project_node(workflow, 512)["widgets_values"][0:3] == ["custom", 4, 5]
    assert _project_node(workflow, 554)["widgets_values"] == [320]
    assert _project_node(workflow, 245)["widgets_values"] == [4]
    assert _project_node(workflow, 526)["widgets_values"] == [25.000000000000007]
    assert _project_node(workflow, 352)["widgets_values"]["trim_to_audio"] is True
    assert 292 not in node_ids
    assert 311 not in node_ids
    assert all(int(link[1]) in node_ids and int(link[3]) in node_ids for link in workflow["links"])


def test_fast_4x5_api_keeps_longcat_model_and_has_one_final_video_output() -> None:
    source = _load("longcat_avatar_api.json")
    workflow = _load("lc_fast_256x320_4s_api.json")

    assert workflow["512"]["inputs"]["aspect_ratio"] == "custom"
    assert workflow["512"]["inputs"]["proportional_width"] == 4
    assert workflow["512"]["inputs"]["proportional_height"] == 5
    assert workflow["554"]["inputs"]["value"] == 320
    assert workflow["245"]["inputs"]["value"] == 4
    assert workflow["71"]["inputs"] == source["71"]["inputs"]
    assert workflow["352"]["inputs"]["trim_to_audio"] is True
    assert "292" not in workflow
    assert "311" not in workflow
    assert [
        node_id
        for node_id, node in workflow.items()
        if node["class_type"] == "VHS_VideoCombine"
    ] == ["352"]


def test_standard24_profile_uses_gpu_residency_instead_of_full_block_swap() -> None:
    source = _load("longcat_avatar_api.json")
    workflow = _load("lc_24fast_256x320_4s_api.json")

    # LongCat has 48 transformer blocks.  The original swaps all 48; this
    # Standard profile keeps 40 resident and swaps only 8.
    assert source["104"]["inputs"]["blocks_to_swap"] == 48
    assert workflow["104"]["inputs"]["blocks_to_swap"] == 8
    assert workflow["104"]["inputs"]["use_non_blocking"] is True
    assert workflow["104"]["inputs"]["prefetch_blocks"] == 1
    assert workflow["536"]["inputs"]["force_offload"] is False
    assert workflow["538"]["inputs"]["force_offload"] is False
    assert workflow["71"]["inputs"] == source["71"]["inputs"]
    assert workflow["245"]["inputs"]["value"] == 4
    assert workflow["554"]["inputs"]["value"] == 320


def test_standard24_balanced_profile_relaxes_oom_pressure_without_full_swap() -> None:
    workflow = _load("lc_24bal_256x320_4s_api.json")

    assert workflow["104"]["inputs"]["blocks_to_swap"] == 16
    assert workflow["104"]["inputs"]["offload_img_emb"] is True
    assert workflow["104"]["inputs"]["offload_txt_emb"] is True
    assert workflow["104"]["inputs"]["use_non_blocking"] is True
    assert workflow["104"]["inputs"]["prefetch_blocks"] == 1
    assert workflow["536"]["inputs"]["force_offload"] is False
    assert workflow["538"]["inputs"]["force_offload"] is False
    assert workflow["245"]["inputs"]["value"] == 4
    assert workflow["554"]["inputs"]["value"] == 320


def test_standard24_safe_profile_addresses_second_sampler_oom() -> None:
    source = _load("longcat_avatar_api.json")
    workflow = _load("lc_safe_256x320_4s_api.json")

    assert workflow["104"]["inputs"]["blocks_to_swap"] == 48
    assert workflow["104"]["inputs"]["offload_img_emb"] is True
    assert workflow["104"]["inputs"]["offload_txt_emb"] is True
    assert workflow["104"]["inputs"]["use_non_blocking"] is True
    assert workflow["104"]["inputs"]["prefetch_blocks"] == 1
    assert source["534"]["inputs"]["load_device"] == "main_device"
    assert workflow["534"]["inputs"]["load_device"] == "offload_device"
    assert workflow["536"]["inputs"]["force_offload"] is True
    assert workflow["538"]["inputs"]["force_offload"] is True
    assert workflow["71"]["inputs"] == source["71"]["inputs"]
    assert workflow["245"]["inputs"]["value"] == 4
    assert workflow["554"]["inputs"]["value"] == 320


def test_generated_longcat_filenames_fit_runninghub_limit() -> None:
    for profile in PROFILES:
        assert len(profile.target_project.name) <= 50
        assert len(profile.target_api.name) <= 50
