import subprocess

from backlot import material_interaction_refinement as refinement


def fixtures():
    index = {
        "status": "completed", "duration": 40,
        "audio": {"status": "available", "utterances": [
            {"id": "U1", "start": .8, "end": 3.56, "text": "开场招呼"},
            {"id": "U2", "start": 5, "end": 6, "text": "回答"},
            {"id": "U3", "start": 18.7, "end": 20.5, "text": "道别"},
            {"id": "U4", "start": 22, "end": 24, "text": "下一组"},
        ]},
    }
    review = {"events": [
        {"review_event_id": "R1", "group_id": "G1", "status": "kept", "start": 2.067, "end": 20},
        {"review_event_id": "R2", "group_id": "G2", "status": "kept", "start": 22, "end": 30},
    ]}
    return index, review


def test_boundary_inside_utterance_expands_to_full_utterance_and_protects_speech():
    index, review = fixtures()
    result = refinement.refine_event(index, review, "R1", speech_ranges=[{"start": 4.9, "end": 6.1}])
    assert result["protected_range"] == {"start": .8, "end": 20.5}
    assert result["boundary_changes"] == {
        "start_seconds": -1.267, "end_seconds": .5,
        "evidence_utterance_ids": ["U1", "U3"],
    }
    assert result["safe_for_internal_edit"] is True
    assert any(row["start"] <= .8 and row["end"] >= 3.56 for row in result["protected_ranges"])


def test_boundary_extension_cannot_cross_different_group():
    index, review = fixtures()
    index["audio"]["utterances"].append({"id": "U5", "start": 19.8, "end": 22.5, "text": "归属不明"})
    result = refinement.refine_event(index, review, "R1")
    assert result["protected_range"] == {"start": 2.067, "end": 20.0}
    assert result["safe_for_internal_edit"] is False
    assert "其他互动组" in result["warnings"][0]


def test_unknown_frame_reference_rejects_only_bad_annotation():
    index, review = fixtures()
    result = refinement.refine_event(index, review, "R1", allowed_frame_ids={"F1", "F2"}, semantic_annotations=[
        {"id": "A1", "start": 7, "end": 8, "stage": "action", "confidence": .95,
         "evidence_frame_ids": ["F1", "F2"]},
        {"id": "A2", "start": 9, "end": 10, "stage": "unrelated", "confidence": .95,
         "evidence_frame_ids": ["UNKNOWN"]},
    ])
    assert [row["id"] for row in result["annotations"]] == ["A1"]
    assert len(result["rejected_annotations"]) == 1
    assert "1条局部标注" in result["warnings"][0]


def test_action_and_reaction_are_protected_but_waiting_is_not():
    index, review = fixtures()
    result = refinement.refine_event(index, review, "R1", semantic_annotations=[
        {"id": "A1", "start": 7, "end": 8, "stage": "action", "confidence": .95},
        {"id": "A2", "start": 9, "end": 10, "stage": "waiting", "confidence": .95},
    ])
    evidence = [item for row in result["protected_ranges"] for item in row["evidence_ids"]]
    assert "A1" in evidence and "A2" not in evidence


def test_audio_unavailable_produces_conservative_candidate_status():
    index, review = fixtures()
    index["audio"] = {"status": "skipped", "utterances": []}
    result = refinement.refine_event(index, review, "R1")
    assert result["protected_range"] == {"start": 2.067, "end": 20.0}
    assert result["safe_for_internal_edit"] is False
    assert "没有可用分句转写" in result["warnings"][-1]


def test_pause_visual_budget_is_global_and_each_window_is_bounded():
    index, _ = fixtures()
    windows = refinement.plan_pause_visual_windows(index, max_windows=2)
    assert len(windows) == 2
    assert all(row["end"] - row["start"] <= 2.001 for row in windows)
    assert windows == sorted(windows, key=lambda row: row["start"])


def test_local_pause_visual_activity_only_authorizes_low_motion(tmp_path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    index, _ = fixtures()
    frame_size = refinement.PAUSE_VISUAL_WIDTH * refinement.PAUSE_VISUAL_HEIGHT
    calls = []

    def fake_runner(command, **_kwargs):
        calls.append(command)
        if len(calls) == 1:
            raw = bytes(frame_size * 3)
        else:
            raw = bytes(frame_size) + bytes([255]) * frame_size + bytes(frame_size)
        return subprocess.CompletedProcess(command, 0, stdout=raw, stderr=b"")

    result = refinement.analyze_pause_visual_activity(
        source, index, ffmpeg="ffmpeg", runner=fake_runner,
    )
    assert len(result["windows"]) == len(calls)
    assert result["windows"][0]["stage"] == "waiting"
    assert result["windows"][0]["safe_to_shorten"] is True
    assert all(row["stage"] == "action" and not row["safe_to_shorten"]
               for row in result["windows"][1:])
    assert result["metadata"]["frame_count"] <= refinement.PAUSE_VISUAL_MAX_WINDOWS * 5
