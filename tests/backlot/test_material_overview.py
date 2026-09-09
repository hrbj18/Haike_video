from __future__ import annotations

import subprocess
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from backlot.material_overview import (
    build_material_overview_index,
    duration_policy,
    sampling_plan,
    validate_material_overview_index,
)
from backlot.workbench import _ffmpeg_available, _ffprobe_available


def test_duration_budgets_and_chapter_contracts_are_bounded() -> None:
    short = duration_policy(30)
    medium = duration_policy(2 * 60)
    long = duration_policy(30 * 60)
    hour = duration_policy(60 * 60)
    extra_long = duration_policy(61 * 60)

    assert short["budget_max"] == 60
    assert medium["budget_max"] == 81
    assert medium["grid_rows"] == 3
    assert long["budget_max"] == 96 and long["chapter_count"] == 6
    assert hour["budget_max"] == 96 and hour["grid_columns"] == 4
    assert extra_long["remote_whole_video_allowed"] is False
    assert extra_long["chapter_count"] == 7


def test_sampling_keeps_half_the_budget_as_anchors_and_uses_activity_only_in_information_layer() -> None:
    static = sampling_plan(120, [])
    active = sampling_plan(120, [
        {"timestamp_seconds": 8, "activity_score": 9, "reason": "scene_change"},
        {"timestamp_seconds": 34, "activity_score": 8, "reason": "scene_change"},
        {"timestamp_seconds": 92, "activity_score": 7, "reason": "motion_difference"},
    ])

    for plan in (static, active):
        requested = plan["requested_frames"]
        anchors = [item for item in requested if item["protected_anchor"]]
        assert len(requested) <= plan["budget_max"]
        assert len(anchors) >= (plan["budget_max"] + 1) // 2
        assert min(item["timestamp_seconds"] for item in anchors) == 0
        assert max(item["timestamp_seconds"] for item in anchors) >= 119.5

    static_information = {item["timestamp_seconds"] for item in static["requested_frames"] if not item["protected_anchor"]}
    active_information = {item["timestamp_seconds"] for item in active["requested_frames"] if not item["protected_anchor"]}
    assert {8.0, 34.0, 92.0}.issubset(active_information)
    assert active_information != static_information


def test_long_plan_preserves_every_chapter_start_middle_and_end_anchor() -> None:
    plan = sampling_plan(30 * 60, [])
    rows = plan["requested_frames"]
    for chapter in plan["chapters"]:
        chapter_rows = [row for row in rows if row["chapter_id"] == chapter["chapter_id"] and row["protected_anchor"]]
        assert len(chapter_rows) >= 8
        assert min(row["timestamp_seconds"] for row in chapter_rows) == chapter["start_seconds"]
        assert max(row["timestamp_seconds"] for row in chapter_rows) >= chapter["end_seconds"] - .5


@pytest.mark.skipif(not _ffmpeg_available(), reason="ffmpeg is required for overview verification")
def test_local_overview_creates_contact_sheets_with_actual_pts_and_reuses_cache(tmp_path: Path) -> None:
    ffmpeg = _ffmpeg_available()
    ffprobe = _ffprobe_available(ffmpeg)
    assert ffmpeg and ffprobe
    source = tmp_path / "overview-source.mp4"
    subprocess.run([
        ffmpeg, "-y",
        "-f", "lavfi", "-i", "color=c=red:s=320x180:r=12:d=1",
        "-f", "lavfi", "-i", "color=c=green:s=320x180:r=12:d=1",
        "-f", "lavfi", "-i", "color=c=blue:s=320x180:r=12:d=1",
        "-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]",
        "-map", "[v]", "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(source),
    ], check=True, capture_output=True)

    first = build_material_overview_index(source, tmp_path / "index", ffmpeg=ffmpeg, ffprobe=ffprobe)
    second = build_material_overview_index(source, tmp_path / "index", ffmpeg=ffmpeg, ffprobe=ffprobe)

    assert first["status"] == "sheets_ready"
    assert first["overview"]["status"] == "not_requested"
    assert first["detail"]["status"] == "not_requested"
    assert first["sheets"] and all(Path(sheet["path"]).is_file() for sheet in first["sheets"])
    assert first["cells"] and all(cell["actual_pts_seconds"] >= 0 for cell in first["cells"])
    assert all(frame.get("actual_pts_seconds") is not None for frame in first["frames"])
    assert second["cache_hit"] is True
    validate_material_overview_index(second)
    schema_path = Path(__file__).resolve().parents[2] / "config" / "schemas" / "material_overview_index.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    assert list(Draft202012Validator(schema).iter_errors(second)) == []
