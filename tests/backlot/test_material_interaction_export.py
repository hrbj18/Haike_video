"""剪辑决策导出契约测试：JSON ``cut-list-v1`` / FCP7 xmeml / OTIO。

全部离线：导出模块是纯函数，不碰网络、不碰 FFmpeg、不改计划文件。
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import time
import xml.etree.ElementTree as ET

import pytest

from backlot import material_interaction_export as ex


CONTRACT = {
    "fps": 30.0, "width": 1080, "height": 2274,
    "audio": {"sample_rate": 48000, "channels": 2}, "duration": 60.0,
}


def _mapping():
    return [
        {"occurrence_id": "O-BODY-001", "role": "body", "group_ids": ["G1"],
         "source_start": 12.5, "source_end": 20.0, "output_start": 0.0, "output_end": 6.818182, "speed": 1.1},
        {"occurrence_id": "O-BODY-002", "role": "body", "group_ids": ["G1"],
         "source_start": 30.0, "source_end": 42.5, "output_start": 6.818182, "output_end": 13.068182, "speed": 2.0},
    ]


def _plan(**overrides):
    plan = {
        "plan_id": "ISP-abcdef0123456789", "revision": 3, "status": "pending_review",
        "created_at": "2026-09-12T10:00:00+00:00",
        "occurrences": [
            {"occurrence_id": "O-BODY-001", "role": "body", "source_start": 12.5, "source_end": 20.0,
             "speed": 1.1, "actions": [{"kind": "speed", "value": 1.1, "unit": "ratio", "reason": "preset"}]},
            {"occurrence_id": "O-BODY-002", "role": "body", "source_start": 30.0, "source_end": 42.5,
             "speed": 2.0, "actions": [{"kind": "speed", "value": 2.0, "unit": "ratio",
                                        "reason": "waiting_segment"}]},
        ],
        "timeline_mapping": _mapping(),
        "subtitle_cues": [
            {"cue_id": "O-BODY-001:U00003", "occurrence_id": "O-BODY-001", "role": "body",
             "utterance_id": "U00003", "text": "家人们赶紧点进来",
             "source_start": 13.0, "source_end": 15.0, "output_start": 0.454545, "output_end": 2.272727},
        ],
        "options": {"pause_handling": "speed_up", "audio_fade": True, "audio_fade_ms": 8.0},
        "pause_trims": [], "speed_segments": [{"segment_id": "SS001", "speed": 2.0}],
        "warnings": ["仅在竖屏预览核对"],
    }
    plan.update(overrides)
    return plan


def _source():
    return {
        "fingerprint": "a" * 64,
        "original": {"path": "artifacts/media-index/S-001/raw.mp4", "codec": "hevc",
                     "container": "mp4", "r_frame_rate": "30/1"},
        "reference": {"kind": "review_proxy", "path": "artifacts/media-index/S-001/review.mp4",
                      "fingerprint": "b" * 64},
    }


def test_frame_number_uses_round_half_up():
    assert ex.FRAME_ROUNDING == "round_half_up"
    assert ex.frame_number(0.5 / 30, 30) == 1
    assert ex.frame_number(0.49 / 30, 30) == 0
    assert ex.frame_number(2.0, 30.0) == 60
    assert ex.frame_number(0.0, 30.0) == 0
    with pytest.raises(ex.InteractionExportError, match="帧率"):
        ex.frame_number(1.0, 0)


def test_cut_list_covers_every_timeline_segment_and_conserves_duration():
    cut = ex.build_cut_list(_plan(), source=_source(), contract=CONTRACT, generated_at="T")

    assert cut["schema"] == "cut-list-v1" and cut["schema_version"] == 1
    assert cut["generator"]["module"] == "material_interaction_export"
    assert [row["segment_id"] for row in cut["segments"]] == ["O-BODY-001", "O-BODY-002"]
    total = round(sum(row["output_end"] - row["output_start"] for row in cut["segments"]), 6)
    assert abs(total - cut["timeline"]["output_duration"]) <= 1e-6
    assert cut["timeline"]["timebase"] == 30
    assert cut["timeline"]["frame_count"] == cut["segments"][-1]["output_end_frame"]
    # actions 是从计划派生的可解释视图，等待段保留它自己的原因。
    assert cut["segments"][1]["actions"] == [
        {"kind": "speed", "value": 2.0, "unit": "ratio", "reason": "waiting_segment"}]
    assert cut["pause_handling"] == "speed_up"
    assert cut["subtitles"][0]["cue_id"] == "O-BODY-001:U00003"


def test_frame_numbers_are_integers_monotonic_and_inside_the_source():
    cut = ex.build_cut_list(_plan(), source=_source(), contract=CONTRACT, generated_at="T")
    previous = 0
    for row in cut["segments"]:
        for key in ("source_start_frame", "source_end_frame", "output_start_frame", "output_end_frame"):
            assert isinstance(row[key], int)
        assert row["source_start_frame"] <= row["source_end_frame"]
        assert row["output_end_frame"] >= previous
        assert 0 <= row["source_end_frame"] <= int(CONTRACT["duration"] * CONTRACT["fps"])
        previous = row["output_end_frame"]


def test_a_segment_outside_the_source_is_rejected_in_chinese():
    plan = _plan()
    plan["timeline_mapping"][1]["source_end"] = 600.0  # 远超声源时长 60s
    with pytest.raises(ex.InteractionExportError, match="越出素材范围"):
        ex.build_cut_list(plan, source=_source(), contract=CONTRACT, generated_at="T")


def test_missing_review_proxy_is_reported_as_a_degradation():
    source = _source()
    source["reference"] = {"kind": "original", "path": "artifacts/media-index/S-001/raw.mp4"}
    cut = ex.build_cut_list(_plan(), source=source, contract=CONTRACT, media_reference="review_proxy",
                            generated_at="T")
    assert cut["media_reference_policy"] == "review_proxy"
    assert any(row.startswith("proxy_missing:") for row in cut["degradations"])


def test_fcp7_xml_is_wellformed_and_uses_output_frames():
    cut = ex.build_cut_list(_plan(), source=_source(), contract=CONTRACT, generated_at="T")
    xml = ex.build_fcp7_xml(cut)
    root = ET.fromstring(xml)

    assert root.tag == "xmeml" and root.get("version") == "4"
    sequence = root.find("sequence")
    assert sequence.find("name").text == "ISP-abcdef0123456789"
    assert sequence.find("duration").text == str(cut["timeline"]["frame_count"])
    assert sequence.find("rate/timebase").text == "30"
    assert sequence.find("rate/ntsc").text == "FALSE"
    clipitems = sequence.findall("media/video/track/clipitem")
    assert len(clipitems) == 2
    assert clipitems[0].find("start").text == str(cut["segments"][0]["output_start_frame"])
    assert clipitems[0].find("in").text == str(cut["segments"][0]["source_start_frame"])
    assert clipitems[0].find("out").text == str(cut["segments"][0]["source_end_frame"])
    assert sequence.find("media/video/format/samplecharacteristics/width").text == "1080"
    assert sequence.find("media/video/format/samplecharacteristics/height").text == "2274"


def test_fcp7_time_remap_and_the_constant_length_fallback_are_both_implemented():
    cut = ex.build_cut_list(_plan(), source=_source(), contract=CONTRACT, generated_at="T")

    remap = ET.fromstring(ex.build_fcp7_xml(cut, speed_mode="timeremap"))
    effect = remap.find("sequence/media/video/track/clipitem/filter/effect")
    assert effect.find("name").text == "Time Remap"
    assert effect.find("effectid").text == "timeremap"
    assert effect.find("parameter/parameterid").text == "speed"
    assert effect.find("parameter/value").text == "110"  # 1.1x 写百分数

    fallback = ET.fromstring(ex.build_fcp7_xml(cut, speed_mode="constant_length"))
    first = fallback.find("sequence/media/video/track/clipitem")
    # 降级路径：片段长度回到源片长度，并留下一条可见标记。
    assert first.find("start").text == str(cut["segments"][0]["source_start_frame"])
    assert first.find("end").text == str(cut["segments"][0]["source_end_frame"])
    assert first.find("marker/comment").text.startswith("计划倍速 1.10x")
    assert first.find("filter") is None

    degradations = ex.fcp7_speed_degradations(cut, speed_mode="timeremap")
    assert degradations == ["fcp7_speed_degraded:O-BODY-001", "fcp7_speed_degraded:O-BODY-002"]
    assert ex.fcp7_speed_degradations(cut, speed_mode="constant_length") == degradations
    with pytest.raises(ex.InteractionExportError, match="变速模式"):
        ex.build_fcp7_xml(cut, speed_mode="guess")


def test_subtitle_markers_use_output_frame_numbers():
    cut = ex.build_cut_list(_plan(), source=_source(), contract=CONTRACT, generated_at="T")
    root = ET.fromstring(ex.build_fcp7_xml(cut))
    marker = root.find("sequence/media/video/track/generatoritem/marker")
    assert marker.find("comment").text == "家人们赶紧点进来"
    assert marker.find("in").text == str(ex.frame_number(0.454545, 30.0))
    assert marker.find("out").text == str(ex.frame_number(2.272727, 30.0))


def test_otio_hand_written_schema_is_self_consistent():
    cut = ex.build_cut_list(_plan(), source=_source(), contract=CONTRACT, generated_at="T")
    otio = ex.build_otio(cut)
    assert otio["OTIO_SCHEMA"] == "Timeline.1"
    track = otio["tracks"]["children"][0]
    assert track["OTIO_SCHEMA"] == "Track.1" and track["kind"] == "Video"
    clip = track["children"][0]
    assert clip["OTIO_SCHEMA"] == "Clip.2"
    assert clip["source_range"]["start_time"]["value"] == cut["segments"][0]["source_start_frame"]
    assert clip["source_range"]["duration"]["value"] == (
        cut["segments"][0]["source_end_frame"] - cut["segments"][0]["source_start_frame"])


def test_export_plan_writes_files_and_is_idempotent_and_side_effect_free(tmp_path):
    plan = _plan()
    before = hashlib.sha256(json.dumps(plan, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    output = tmp_path / "export"

    first = ex.export_plan(plan, output_dir=output, formats=["json", "fcp7_xml"],
                           source=_source(), contract=CONTRACT, generated_at="T", include_srt=True)
    names = sorted(Path(row["path"]).name for row in first["files"])
    assert names == ["ISP-abcdef0123456789.json", "ISP-abcdef0123456789.srt", "ISP-abcdef0123456789.xml"]
    assert all(row["reused"] is False for row in first["files"])
    assert first["notice"].startswith("此计划尚未确认入库")
    assert "fcp7_speed_degraded:O-BODY-002" in first["degradations"]

    second = ex.export_plan(plan, output_dir=output, formats=["json", "fcp7_xml"],
                            source=_source(), contract=CONTRACT, generated_at="T", include_srt=True)
    assert all(row["reused"] is True for row in second["files"])
    assert sorted(path.name for path in output.iterdir()) == names

    after = hashlib.sha256(json.dumps(plan, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    assert before == after, "导出绝不能修改传入的计划"
    payload = json.loads((output / "ISP-abcdef0123456789.json").read_text(encoding="utf-8"))
    assert payload["schema"] == "cut-list-v1"
    # 覆盖全部 timeline_mapping，且时长守恒。
    assert [row["segment_id"] for row in payload["segments"]] == [
        row["occurrence_id"] for row in plan["timeline_mapping"]]
    assert abs(sum(row["output_end"] - row["output_start"] for row in payload["segments"])
               - payload["timeline"]["output_duration"]) <= 1e-6


def test_repeated_default_export_appends_new_files_and_matches_except_timestamp(tmp_path):
    """默认路径（不固定 `generated_at`）重复导出：每次生成新文件、绝不覆盖、除时间戳外一致。"""
    plan = _plan()
    output = tmp_path / "export"

    first = ex.export_plan(plan, output_dir=output, formats=["json"], source=_source(), contract=CONTRACT)
    first_path = Path(first["files"][0]["path"])
    first_bytes = first_path.read_bytes()
    assert first_path == output / "ISP-abcdef0123456789.json"
    assert first["files"][0]["reused"] is False

    time.sleep(0.01)  # 等价于人类隔着一点时间点两次「导出」
    second = ex.export_plan(plan, output_dir=output, formats=["json"], source=_source(), contract=CONTRACT)
    second_path = Path(second["files"][0]["path"])

    # 第二次导出按内容哈希**另存新文件**，第一份原样保留（零覆盖，而非字节幂等）。
    assert second_path != first_path
    assert second_path.name.startswith("ISP-abcdef0123456789-") and second_path.suffix == ".json"
    assert first_path.read_bytes() == first_bytes
    assert sorted(path.name for path in output.iterdir()) == sorted([first_path.name, second_path.name])

    # 两份内容除生成时间字段外完全一致。
    left = json.loads(first_path.read_text(encoding="utf-8"))
    right = json.loads(second_path.read_text(encoding="utf-8"))
    assert left["generated_at"] != right["generated_at"]
    left.pop("generated_at")
    right.pop("generated_at")
    assert left == right


def test_non_standard_frame_rate_timebase_rounding_is_recorded_and_visible():
    non_standard = {**CONTRACT, "fps": 22.965}
    cut = ex.build_cut_list(_plan(), source=_source(), contract=non_standard, generated_at="T")

    assert cut["timeline"]["timebase"] == 23
    notes = [row for row in cut["degradations"] if row.startswith("fcp7_timebase_rounded:")]
    assert len(notes) == 1 and "22.965->23" in notes[0]

    xml = ex.build_fcp7_xml(cut)
    assert "<!--" in xml and "fcp7_timebase_rounded:22.965->23" in xml
    ET.fromstring(xml)  # 注释不影响 XML 合法性

    # 标准帧率（整数帧率与标准 NTSC 帧率）可被 FCP7 精确表达，不产生该降级。
    for standard in (24.0, 25.0, 30.0, 23.976, 29.97, 59.94):
        ok = ex.build_cut_list(_plan(), source=_source(), contract={**CONTRACT, "fps": standard},
                               generated_at="T")
        assert not any(row.startswith("fcp7_timebase_rounded:") for row in ok["degradations"]), standard
        assert "fcp7_timebase_rounded" not in ex.build_fcp7_xml(ok)


def test_export_plan_refuses_an_unknown_format():
    with pytest.raises(ex.InteractionExportError, match="不支持的导出格式"):
        ex.export_plan(_plan(), output_dir=Path("."), formats=["premiere_project"],
                       source=_source(), contract=CONTRACT)


def test_export_requires_a_frame_rate_contract():
    with pytest.raises(ex.InteractionExportError, match="帧率契约"):
        ex.build_cut_list(_plan(), source=_source(), contract={}, generated_at="T")


def test_approved_plan_is_marked_confirmed(tmp_path):
    plan = _plan(status="approved")
    result = ex.export_plan(plan, output_dir=tmp_path / "export", formats=["json"],
                            source=_source(), contract=CONTRACT, generated_at="T")
    assert result["notice"] == "计划已确认入库"
    assert result["cut_list"] is None


# --- 与 workbench 的接线：只读计划、只写 export/ 目录、零付费 -------------------------- #


def test_workbench_export_entry_reads_the_plan_and_writes_only_into_export(tmp_path, monkeypatch):
    from backlot import material_interaction_render as render
    from backlot import media_index
    from backlot import workbench as wb

    project_dir = tmp_path / "projects" / "P-1"
    asset_dir = project_dir / "artifacts" / "media-index" / "S-001"
    asset_dir.mkdir(parents=True)
    (asset_dir / "raw.mp4").write_bytes(b"raw")
    (asset_dir / "review.mp4").write_bytes(b"proxy")

    plan = _plan()
    plan_path = asset_dir / "interaction-second-pass" / plan["plan_id"] / "interaction-second-pass-plan.json"
    plan_path.parent.mkdir(parents=True)
    plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    plan_digest = hashlib.sha256(plan_path.read_bytes()).hexdigest()

    monkeypatch.setattr(wb, "_interaction_review_context",
                        lambda _p, _a: ({}, {}, asset_dir / "raw.mp4", {}, asset_dir / "review.json"))
    monkeypatch.setattr(wb, "interaction_second_pass_plan_path", lambda _root, plan_id: plan_path)
    monkeypatch.setattr(wb, "read_second_pass_plan", lambda _path: deepcopy(plan))
    monkeypatch.setattr(wb, "_ffmpeg_available", lambda: "ffmpeg")
    monkeypatch.setattr(wb, "_ffprobe_available", lambda _f: "ffprobe")
    monkeypatch.setattr(wb, "_interaction_render_source", lambda _p, _a: asset_dir / "review.mp4")
    monkeypatch.setattr(media_index, "probe_media", lambda _p, _f: {
        "duration_seconds": 60.0,
        "streams": [{"codec_type": "video", "width": 1080, "height": 2274,
                     "r_frame_rate": "30/1", "avg_frame_rate": "30/1", "codec_name": "hevc"}],
    })
    monkeypatch.setattr(render, "_render_contract", lambda _probe, _edge: dict(CONTRACT))

    result = wb.export_asset_material_interaction_second_pass(
        project_dir, "S-001", plan["plan_id"],
        {"formats": ["json", "fcp7_xml"], "media_reference": "review_proxy", "include_srt": True,
         "confirmed": True})

    assert [Path(row["path"]).name for row in result["files"]] == [
        f"{plan['plan_id']}.json", f"{plan['plan_id']}.xml", f"{plan['plan_id']}.srt"]
    for row in result["files"]:
        assert Path(row["path"]).parent == plan_path.parent / "export"
    # 计划文件必须逐字节不变（零副作用）。
    assert hashlib.sha256(plan_path.read_bytes()).hexdigest() == plan_digest
    assert result["cut_list"] is None
    body = json.loads((plan_path.parent / "export" / f"{plan['plan_id']}.json").read_text(encoding="utf-8"))
    assert body["source"]["reference"]["kind"] == "review_proxy"
    assert body["source"]["original"]["path"].endswith("raw.mp4")


def test_workbench_export_rejects_an_unknown_speed_mode(tmp_path, monkeypatch):
    from backlot import workbench as wb

    monkeypatch.setattr(wb, "_interaction_review_context", lambda _p, _a: ({}, {}, tmp_path, {}, None))
    monkeypatch.setattr(wb, "_interaction_second_pass_root", lambda _p, _a: tmp_path)
    monkeypatch.setattr(wb, "interaction_second_pass_plan_path", lambda _root, _plan_id: tmp_path / "plan.json")
    monkeypatch.setattr(wb, "read_second_pass_plan", lambda _path: _plan())

    with pytest.raises(wb.WorkbenchError, match="变速模式"):
        wb.export_asset_material_interaction_second_pass(
            tmp_path, "S-001", "ISP-abcdef0123456789", {"fcp7_speed_mode": "guess", "confirmed": True})
