"""remake-spec-v1 → 项目产物纯函数适配层测试（不建项目、不写盘、不调 ffmpeg）。"""

from __future__ import annotations

import pytest

from backlot.remake_editorial import build_editorial_package, quantize_frames
from backlot.remake_project import asset_key_map, build_script, build_visual_blocks
from tests.backlot.test_remake_editorial import editorial_snapshot, stub_copywriter, stub_probe


def _spec() -> dict:
    return build_editorial_package(
        editorial_snapshot(),
        duration_probe=stub_probe,
        project_id="demo-1",
        copywriter=stub_copywriter,
    )["spec"]


def test_build_script_timeline_is_contiguous_and_matches_spec():
    spec = _spec()
    script = build_script(spec, speaker_id="yaya")
    assert script["version"] == "1.0"
    assert script["total_duration_seconds"] == spec["duration"]["total_seconds"]
    previous_end = 0.0
    for section, spec_section in zip(script["sections"], spec["sections"]):
        assert section["id"] == spec_section["id"]
        assert section["turn_id"] == spec_section["turn_id"]
        assert section["start_seconds"] == previous_end
        assert section["end_seconds"] == round(previous_end + spec_section["duration_seconds"], 3)
        assert section["speaker_id"] == "yaya"
        assert len(section["enhancement_cues"]) == len(spec_section["shots"])
        previous_end = section["end_seconds"]


def test_build_script_metadata_carries_theme_and_speaker():
    spec = _spec()
    script = build_script(spec, speaker_id="yaya")
    assert script["metadata"]["theme_id"] == spec["theme"]["theme_id"]
    assert script["metadata"]["audio_mode"] == "narration"
    assert script["metadata"]["speaker"] == "yaya"


def test_build_visual_blocks_is_frame_consistent_and_covers_section():
    spec = _spec()
    mapping = {source["key"]: f"asset-{source['key']}" for source in spec["sources"]}
    fps = spec["fps"]
    for section in spec["sections"]:
        blocks = build_visual_blocks(spec, section, mapping)
        assert blocks[0]["start_seconds"] == 0.0
        assert blocks[-1]["end_seconds"] == section["duration_seconds"]
        previous = None
        for block in blocks:
            if previous is not None:
                assert block["start_seconds"] == previous
            previous = block["end_seconds"]
            display = quantize_frames(block["end_seconds"], fps) - quantize_frames(block["start_seconds"], fps)
            source = quantize_frames(block["source_out_seconds"], fps) - quantize_frames(
                block["source_in_seconds"], fps
            )
            assert display == source
            assert block["asset_id"] == mapping[section["shots"][0]["source"]]


def test_build_visual_blocks_requires_asset_mapping():
    spec = _spec()
    with pytest.raises(KeyError):
        build_visual_blocks(spec, spec["sections"][0], {})


def test_asset_key_map_reports_missing_keys():
    spec = _spec()
    with pytest.raises(KeyError) as excinfo:
        asset_key_map(spec, {})
    assert "m1" in str(excinfo.value)
    mapping = asset_key_map(spec, {source["key"]: f"asset-{source['key']}" for source in spec["sources"]})
    assert set(mapping) == {source["key"] for source in spec["sources"]}
