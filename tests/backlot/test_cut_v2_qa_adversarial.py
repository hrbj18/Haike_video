"""Cut V2 adversarial boundary tests (added by QA).

These encode the boundary behaviour that the shipped suite did not cover, and the
two edge findings raised in ``docs/team-runs/2026-09-12-cut-v2/03-QA.md``:

* concurrency limits 0 / -1 / huge;
* ``audio_fade_ms`` / ``pause_speed`` out of range and exactly on the bound;
* the ``atempo`` instance count / supported range on the pinned binary;
* the ``afade`` fade-out basis attack (speed-adjusted vs source duration);
* odd / non-integer frame rates in the FCP7 frame numbering;
* export of an empty, a single-segment and an optional-field-poor plan;
* export never overwrites a previously written, content-different artefact.
"""
from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from backlot import interaction_concurrency as ic
from backlot import material_interaction_export as ex
from backlot import material_interaction_second_pass as sp
from backlot.material_interaction_second_pass_render import _atempo_chain, _filters

CONTRACT = {"fps": 30.0, "width": 1080, "height": 2274,
            "audio": {"sample_rate": 48000, "channels": 2}, "duration": 60.0}


def _one_mapping(source_start=0.0, source_end=4.0, speed=1.0):
    return [{"occurrence_id": "O-1", "role": "body",
             "source_start": source_start, "source_end": source_end,
             "output_start": 0.0, "output_end": (source_end - source_start) / speed,
             "speed": speed}]


def _plan(rows):
    return {"plan_id": "ISP-qa0001", "revision": 1, "status": "pending_review",
            "created_at": "2026-09-12T10:00:00+00:00",
            "occurrences": [{"occurrence_id": r["occurrence_id"], "role": "body", "speed": r["speed"]}
                            for r in rows],
            "timeline_mapping": rows, "subtitle_cues": [], "options": {},
            "pause_trims": [], "speed_segments": []}


# --- concurrency limits ---------------------------------------------------- #

@pytest.mark.parametrize("value", [0, -1])
def test_a_non_positive_concurrency_limit_is_refused_in_chinese(value):
    with pytest.raises(ic.InteractionConcurrencyError, match="不能小于 1"):
        ic.resolve_limit("interaction", value)


def test_an_absurdly_large_concurrency_limit_is_clamped_to_the_hard_cap():
    assert ic.resolve_limit("interaction", 999) == ic.MAX_CONCURRENCY
    assert ic.resolve_limit("asr", 10 ** 6) == ic.MAX_CONCURRENCY


@pytest.mark.parametrize("field", ["interaction_concurrency", "asr_concurrency"])
@pytest.mark.parametrize("value", [0, -1, 5, 999])
def test_plan_layer_rejects_out_of_range_concurrency_in_chinese(field, value):
    with pytest.raises(sp.InteractionSecondPassError, match="并发上限需在 1–4 之间"):
        sp.normalize_options({field: value})


# --- fade / speed bounds --------------------------------------------------- #

@pytest.mark.parametrize("value", [4.9, 15.1])
def test_audio_fade_ms_outside_5_15_is_refused_in_chinese(value):
    with pytest.raises(sp.InteractionSecondPassError, match="5–15 毫秒"):
        sp.normalize_options({"audio_fade_ms": value})


@pytest.mark.parametrize("value", [5.0, 15.0])
def test_audio_fade_ms_on_the_boundary_is_accepted(value):
    assert sp.normalize_options({"audio_fade_ms": value})["audio_fade_ms"] == value


def test_a_disabled_fade_ignores_an_out_of_range_value():
    assert sp.normalize_options({"audio_fade": False, "audio_fade_ms": 40.0})["audio_fade_ms"] == 8.0


@pytest.mark.parametrize("value", [1.4, 4.1])
def test_pause_speed_outside_1_5_4_0_is_refused_in_chinese(value):
    with pytest.raises(sp.InteractionSecondPassError, match="1.5–4.0"):
        sp.normalize_options({"pause_handling": "speed_up", "pause_speed": value})


@pytest.mark.parametrize("value", [1.5, 4.0])
def test_pause_speed_on_the_boundary_is_accepted(value):
    assert sp.normalize_options({"pause_handling": "speed_up", "pause_speed": value})["pause_speed"] == value


# --- atempo (single instance over the binary's real range) ----------------- #

@pytest.mark.parametrize("speed", [0.5, 1.5, 2.0, 3.0, 4.0, 100.0])
def test_atempo_is_a_single_instance_and_exactly_matches_the_ratio(speed):
    chain = _atempo_chain(speed)
    assert chain.startswith("atempo=") and chain.count("atempo=") == 1
    assert float(chain.split("=", 1)[1]) == pytest.approx(speed, abs=1e-6)


@pytest.mark.parametrize("speed", [0.49, 101.0])
def test_atempo_outside_the_binary_range_fails_with_a_chinese_remedy(speed):
    with pytest.raises(Exception) as info:
        _atempo_chain(speed)
    message = str(info.value)
    assert "atempo 支持范围" in message and "请" in message


# --- afade basis attack ---------------------------------------------------- #

def test_fade_out_starts_on_the_speed_adjusted_timeline_not_the_source_one():
    occ = [{"occurrence_id": "O1", "role": "body", "speed": 2.0, "source_start": 4.0, "source_end": 6.0}]
    graph, _, expected = _filters(occ, {"fps": 30.0, "width": 100, "height": 100,
                                        "audio": "aac/48kHz/128k"}, True, audio_fade_ms=8.0)
    correct = (6.0 - 4.0) / 2.0 - 0.008          # 0.992 s — speed-adjusted basis
    wrong = (6.0 - 4.0) - 0.008                  # 1.992 s — the bug we are hunting
    assert f"afade=t=out:st={correct:.6f}" in graph
    assert f"afade=t=out:st={wrong:.6f}" not in graph
    assert expected == pytest.approx(1.0)


def test_turning_the_fade_off_leaves_no_afade_and_keeps_the_duration():
    occ = [{"occurrence_id": "O1", "role": "body", "speed": 1.25, "source_start": 1.0, "source_end": 3.0}]
    contract = {"fps": 30.0, "width": 100, "height": 100, "audio": "aac/48kHz/128k"}
    on_graph, _, on_expected = _filters(occ, contract, True, audio_fade=True, edge_fade=False)
    off_graph, _, off_expected = _filters(occ, contract, True, audio_fade=False, edge_fade=False)
    assert "afade" in on_graph and "afade" not in off_graph
    assert on_expected == pytest.approx(off_expected)


# --- odd / non-integer frame rates ----------------------------------------- #

@pytest.mark.parametrize("fps", [22.965, 23.976, 29.97, 25.0])
def test_final_cut_frame_numbers_use_the_true_frame_rate(fps):
    rows = _one_mapping(source_start=100.0, source_end=140.0, speed=1.0)
    cut = ex.build_cut_list(_plan(rows), contract={"fps": fps, "width": 100, "height": 100, "duration": 200.0},
                            generated_at="T")
    seg = cut["segments"][0]
    assert seg["source_start_frame"] == math.floor(100.0 * fps + 0.5)
    assert seg["source_end_frame"] == math.floor(140.0 * fps + 0.5)
    assert cut["timeline"]["timebase"] == max(1, round(fps))
    # XML stays well-formed and keeps integer, non-negative frame numbers.
    root = ET.fromstring(ex.build_fcp7_xml(cut))
    clip = root.find("sequence/media/video/track/clipitem")
    assert int(clip.find("in").text) >= 0 and int(clip.find("out").text) >= int(clip.find("in").text)


# --- export of degenerate / partial plans ---------------------------------- #

def test_an_empty_timeline_is_refused_in_chinese():
    with pytest.raises(ex.InteractionExportError, match="没有可用片段"):
        ex.build_cut_list(_plan([]), contract=CONTRACT, generated_at="T")


def test_a_single_segment_plan_exports_cleanly():
    cut = ex.build_cut_list(_plan(_one_mapping()), contract=CONTRACT, generated_at="T")
    assert len(cut["segments"]) == 1
    assert cut["timeline"]["output_duration"] == pytest.approx(4.0)
    assert cut["timeline"]["frame_count"] == cut["segments"][0]["output_end_frame"]


def test_a_plan_missing_every_optional_field_still_exports():
    bare = {"plan_id": "ISP-bare01", "revision": 1, "status": "pending_review",
            "timeline_mapping": _one_mapping()}
    cut = ex.build_cut_list(bare, contract=CONTRACT, generated_at="T")
    assert cut["segments"][0]["group_ids"] == []
    assert cut["subtitles"] == [] and cut["warnings"] == []
    assert cut["pause_handling"] == "remove"
    assert ex.build_otio(cut)["OTIO_SCHEMA"] == "Timeline.1"


def test_export_never_overwrites_a_previously_written_artefact(tmp_path):
    """A different generated_at must not clobber the earlier export (no overwrite)."""
    plan = _plan(_one_mapping())
    first = ex.export_plan(plan, output_dir=tmp_path, formats=["json"], contract=CONTRACT,
                           generated_at="2026-01-01T00:00:00+00:00")
    first_path = Path(first["files"][0]["path"])
    first_bytes = first_path.read_bytes()

    second = ex.export_plan(plan, output_dir=tmp_path, formats=["json"], contract=CONTRACT,
                            generated_at="2030-12-31T23:59:59+00:00")
    second_path = Path(second["files"][0]["path"])

    assert first_path.read_bytes() == first_bytes, "既有产物不得被覆盖"
    assert second_path.exists()
    # Distinct timestamps yield distinct bytes, so the second write goes to a
    # sibling name rather than overwriting — documented, not masked.
    if second_path == first_path:
        assert second_path.read_bytes() == first_bytes
    else:
        assert second_path != first_path


def test_export_with_a_frozen_timestamp_is_byte_identical_and_idempotent(tmp_path):
    plan = _plan(_one_mapping())
    _write = dict(formats=["json", "fcp7_xml"], contract=CONTRACT, generated_at="T")
    first = ex.export_plan(plan, output_dir=tmp_path, **_write)
    digest_before = {Path(f["path"]).name: Path(f["path"]).read_bytes() for f in first["files"]}
    second = ex.export_plan(plan, output_dir=tmp_path, **_write)
    assert all(row["reused"] is True for row in second["files"])
    for row in second["files"]:
        assert Path(row["path"]).read_bytes() == digest_before[Path(row["path"]).name]
