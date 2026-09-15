"""Wiring tests for the material-evidence layer at the workbench boundary.

Two invariants are protected here:

* the read-only snapshot and the preflight never spawn a process (the V3 rule:
  a diagnostic must not add a dependency to a path that never had one);
* the pause evidence can be answered entirely from a supplied envelope.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np
import pytest

from backlot import workbench as wb
from backlot.material_audio_envelope import build_envelope

SR = 16000


def _boom(*_args, **_kwargs):
    raise AssertionError("a read-only path must not spawn a process")


def _envelope():
    audio = (np.sin(2 * np.pi * 220 * np.arange(SR * 3, dtype=np.float32) / SR) * 0.3 * 32767).astype("<i2")
    audio[SR:2 * SR] = 0
    rms_db, peak_db = build_envelope(audio, sample_rate=SR, window=320, hop=160)
    return {"version": "material-audio-envelope-v1", "identity": {"signature": "fixture"},
            "sample_rate": SR, "window_ms": 20.0, "hop_ms": 10.0, "metric": "rms",
            "window_samples": 320, "hop_samples": 160, "sample_count": int(audio.size),
            "frame_count": int(rms_db.size), "audio_seconds": 3.0,
            "rms_db": rms_db, "peak_db": peak_db, "spawns": 0, "metadata": {}}


def test_snapshot_never_spawns_and_reports_absence(tmp_path, monkeypatch):
    monkeypatch.setattr(subprocess, "run", _boom)
    monkeypatch.setattr(subprocess, "Popen", _boom)
    summary = wb._material_evidence_snapshot(tmp_path, "S-001", None)
    assert summary["status"] == "absent"
    assert summary["sections"] == {}


def test_snapshot_is_read_only_for_a_media_that_has_no_document(tmp_path):
    media = tmp_path / "asset.mp4"
    media.write_bytes(b"media")
    summary = wb._material_evidence_snapshot(tmp_path, "S-001", media)
    assert summary["status"] == "absent"


def test_preflight_snapshot_mode_does_not_probe(tmp_path, monkeypatch):
    monkeypatch.setattr(wb, "_INTERACTION_DEPENDENCY_STATE", {})
    monkeypatch.setattr(wb, "_ffmpeg_available", lambda: True)
    payload = wb._interaction_dependency_preflight(None, "", refresh=True)
    assert "audio_envelope" not in {row["key"] for row in payload["checks"]}
    # A mutation path serves the cached snapshot without touching the disk.
    monkeypatch.setattr(subprocess, "run", _boom)
    monkeypatch.setattr(subprocess, "Popen", _boom)
    cached = wb._interaction_dependency_preflight(None, "", refresh=False)
    assert cached["checks"] == payload["checks"]


def test_preflight_reports_the_evidence_layer_checks(tmp_path, monkeypatch):
    monkeypatch.setattr(wb, "_INTERACTION_DEPENDENCY_STATE", {})
    monkeypatch.setattr(wb, "_ffmpeg_available", lambda: True)
    monkeypatch.setattr(wb, "_module_available", lambda name: name == "numpy")
    monkeypatch.setattr(wb, "_interaction_review_context",
                        lambda project_dir, asset_id: (tmp_path, {"path": "a.mp4"}, None, None, None))
    monkeypatch.setattr(wb, "_interaction_render_source", lambda project_dir, asset: None)
    payload = wb._interaction_dependency_preflight(tmp_path, "S-001", refresh=True)
    keys = {row["key"] for row in payload["checks"]}
    assert {"audio_envelope", "motion_timeline"} <= keys
    for row in payload["checks"]:
        assert {"key", "ok", "label", "remediation"} <= set(row)
    assert all(row["ok"] for row in payload["checks"] if row["key"] in {"audio_envelope", "motion_timeline"})


def test_preflight_marks_the_evidence_layer_unavailable_without_numpy(tmp_path, monkeypatch):
    monkeypatch.setattr(wb, "_INTERACTION_DEPENDENCY_STATE", {})
    monkeypatch.setattr(wb, "_ffmpeg_available", lambda: False)
    monkeypatch.setattr(wb, "_module_available", lambda name: False)
    monkeypatch.setattr(wb, "_interaction_review_context",
                        lambda project_dir, asset_id: (tmp_path, {"path": "a.mp4"}, None, None, None))
    monkeypatch.setattr(wb, "_interaction_render_source", lambda project_dir, asset: None)
    payload = wb._interaction_dependency_preflight(tmp_path, "S-001", refresh=True)
    failed = {row["key"] for row in payload["checks"] if not row["ok"]}
    assert {"audio_envelope", "motion_timeline"} <= failed
    assert payload["degraded"]


def test_pause_evidence_is_answered_from_a_supplied_envelope(tmp_path):
    media = tmp_path / "media.mp4"
    media.write_bytes(b"fixture-media")
    evidence, note = wb._interaction_pause_evidence(
        media, [{"start": 0.0, "end": 3.0}], ffmpeg="ffmpeg", output_root=tmp_path / "out",
        envelope_provider=lambda: _envelope(),
    )
    assert note == ""
    assert evidence["detector"] == "envelope/rms"
    assert evidence["metadata"]["spawns"] == 0
    assert evidence["metadata"]["envelope_cache_hit"] is True
    assert len(evidence["silences"]) == 1
    assert evidence["silences"][0]["start"] == pytest.approx(1.0, abs=0.05)


def test_ensure_material_evidence_records_a_degradation_without_ffmpeg(tmp_path):
    media = tmp_path / "media.mp4"
    media.write_bytes(b"fixture-media")
    payload = wb._ensure_material_evidence(tmp_path, "S-001", media, ffmpeg="", duration=3.0)
    assert payload["status"] == "unavailable"
    assert payload["degradations"]


def test_ensure_material_evidence_uses_the_asset_artifact_directory(tmp_path, monkeypatch):
    media = tmp_path / "media.mp4"
    media.write_bytes(b"fixture-media")
    captured = {}

    def fake_build(source, *, output_root, **kwargs):
        captured["output_root"] = Path(output_root)
        return {"status": "available", "sections": {}, "degradations": [], "metadata": {}}

    monkeypatch.setattr(wb, "build_material_evidence", fake_build)
    wb._ensure_material_evidence(tmp_path, "S-001", media, ffmpeg="ffmpeg", duration=3.0)
    assert captured["output_root"] == wb._material_evidence_root(tmp_path, "S-001")
    assert captured["output_root"].name == "S-001"
    assert captured["output_root"].parent.name == "media-index"
