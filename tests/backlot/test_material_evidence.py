from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import numpy as np
import pytest

from backlot import material_evidence as evidence


SR = 16000
WIDTH, HEIGHT = 96, 54
FRAME_SIZE = WIDTH * HEIGHT


def _media(tmp_path, name="media.mp4"):
    path = tmp_path / name
    path.write_bytes(b"fixture-media-bytes")
    return path


def _pcm(seconds: float, amplitude: float = 0.35) -> bytes:
    t = np.arange(int(SR * seconds)) / SR
    return (np.sin(2 * np.pi * 220 * t) * amplitude * 32767).astype("<i2").tobytes()


def _frames(count: int) -> bytes:
    base = np.arange(FRAME_SIZE, dtype=np.uint16) % 251
    rows = [((base + (index * 7) % 97) % 256).astype(np.uint8) for index in range(count)]
    return np.ascontiguousarray(np.stack(rows), dtype=np.uint8).tobytes()


def _runner(audio: bytes, video: bytes, fail: str | None = None):
    calls = {"audio": 0, "video": 0}

    def run(command, **_kwargs):
        if "-ac" in command:
            calls["audio"] += 1
            if fail == "audio":
                return subprocess.CompletedProcess(command, 1, b"", b"audio boom")
            return subprocess.CompletedProcess(command, 0, audio, b"")
        calls["video"] += 1
        if fail == "video":
            return subprocess.CompletedProcess(command, 1, b"", b"video boom")
        return subprocess.CompletedProcess(command, 0, video, b"")

    run.calls = calls
    return run


def _transcript():
    return {"policy": "tencent_transcript", "identity": "asr-v1",
            "utterances": [{"id": "U1", "start": 1.0, "end": 2.5},
                           {"id": "U2", "start": 4.0, "end": 5.0}]}


def _speech():
    return [{"start": 0.0, "end": 10.0, "speech_ranges": [{"start": 1.0, "end": 2.0}]},
            {"start": 20.0, "end": 30.0, "speech_ranges": [{"start": 21.0, "end": 22.0}]}]


def _build(tmp_path, **kwargs):
    return evidence.build_material_evidence(
        kwargs.pop("source", _media(tmp_path)), output_root=tmp_path / "artifacts",
        ffmpeg="ffmpeg", duration=6.0, speech_ranges_by_range=kwargs.pop("speech", _speech()),
        transcript=kwargs.pop("transcript", _transcript()),
        runner=kwargs.pop("runner", _runner(_pcm(6.0), _frames(24))), **kwargs,
    )


# --- A6: signature -----------------------------------------------------------

def test_signature_is_identity_only_and_stable():
    first = evidence.evidence_signature("fp", envelope_identity={"signature": "e"},
                                        speech_identity={"signature": "s"},
                                        motion_identity={"signature": "m"},
                                        transcript_identity={"signature": "t"})
    again = evidence.evidence_signature("fp", envelope_identity={"signature": "e"},
                                        speech_identity={"signature": "s"},
                                        motion_identity={"signature": "m"},
                                        transcript_identity={"signature": "t"})
    assert first == again


@pytest.mark.parametrize("section", ["envelope_identity", "speech_identity", "motion_identity",
                                     "transcript_identity"])
def test_changing_one_section_identity_changes_the_signature(section):
    base = dict(envelope_identity="e", speech_identity="s", motion_identity="m",
                transcript_identity="t")
    changed = {**base, section: "different"}
    assert evidence.evidence_signature("fp", **base) != evidence.evidence_signature("fp", **changed)


def test_source_fingerprint_changes_the_signature():
    assert evidence.evidence_signature("fp-a") != evidence.evidence_signature("fp-b")


def test_thresholds_are_not_part_of_the_signature(tmp_path):
    base = _build(tmp_path)
    other = _build(tmp_path, preset_overrides={"envelope": {"threshold_db": -33.0}})
    # threshold only parameterises the pure silence view; the decoder identity is
    # unchanged (see material_audio_envelope.envelope_identity).
    assert other["signature"] == base["signature"]


# --- A6: the artifact --------------------------------------------------------

def test_build_writes_the_document_and_arrays(tmp_path):
    payload = _build(tmp_path)
    assert payload["status"] == "available"
    assert payload["spawns"] == 2
    directory = Path(payload["directory"])
    assert directory.is_dir()
    assert (directory / "evidence.json").is_file()
    assert (directory / "envelope.npz").is_file()
    assert (directory / "motion.npz").is_file()
    assert (directory.name) == payload["signature"][:20]
    for name in ("version", "status", "source", "sections", "degradations", "metadata"):
        assert name in json.loads((directory / "evidence.json").read_text(encoding="utf-8"))
    sections = payload["sections"]
    assert sections["audio_envelope"]["frame_count"] > 0
    assert sections["audio_envelope"]["silence_view"]["total_seconds"] >= 0
    assert sections["motion"]["sample_count"] == 23
    assert sections["speech_activity"] == {"status": "available", "identity": None,
                                           "range_count": 2, "speech_ranges": 2,
                                           "ranges": [{"start": 0.0, "end": 10.0, "speech_ranges": 1},
                                                      {"start": 20.0, "end": 30.0, "speech_ranges": 1}]}
    assert sections["transcript"]["utterance_count"] == 2
    assert sections["transcript"]["timeline"][0] == {"id": "U1", "start": 1.0, "end": 2.5}
    assert payload["metadata"]["bytes"] <= evidence.MAX_BYTES


def test_second_build_hits_the_document_and_spawns_nothing(tmp_path):
    first = _build(tmp_path)
    second = _build(tmp_path, runner=_runner(b"", b""))
    assert second["cache_hit"] is True
    assert second["spawns"] == 0
    assert second["signature"] == first["signature"]


def test_read_back_returns_the_stored_arrays(tmp_path):
    built = _build(tmp_path)
    read = evidence.read_material_evidence(_media(tmp_path), output_root=tmp_path / "artifacts",
                                          with_arrays=True)
    assert read is not None
    assert read["signature"] == built["signature"]
    assert read["cache_hit"] is True
    assert read["spawns"] == 0
    assert read["envelope"]["rms_db"].tobytes() == built["envelope"]["rms_db"].tobytes()
    assert read["motion"]["scores"].tobytes() == built["motion"]["scores"].tobytes()


def test_read_without_arrays_stays_cheap(tmp_path):
    _build(tmp_path)
    read = evidence.read_material_evidence(_media(tmp_path), output_root=tmp_path / "artifacts")
    assert read is not None and read["envelope"] is None and read["motion"] is None


def test_read_returns_none_when_nothing_was_built(tmp_path):
    assert evidence.read_material_evidence(_media(tmp_path), output_root=tmp_path / "artifacts") is None


def test_read_ignores_a_document_built_for_another_media(tmp_path):
    _build(tmp_path)
    other = tmp_path / "other.mp4"
    other.write_bytes(b"a genuinely different media payload")
    assert evidence.read_material_evidence(other, output_root=tmp_path / "artifacts") is None


def test_read_degrades_instead_of_raising_when_numpy_is_missing(tmp_path, monkeypatch):
    _build(tmp_path)
    from backlot import material_audio_envelope as envelope_module

    def refuse():
        raise envelope_module.MaterialAudioEnvelopeUnavailable("本机 numpy 不可用")

    monkeypatch.setattr(envelope_module, "_import_numpy", refuse)
    read = evidence.read_material_evidence(_media(tmp_path), output_root=tmp_path / "artifacts",
                                          with_arrays=True)
    assert read is not None
    assert read["status"] == "unavailable"
    assert any("numpy" in item for item in read["degradations"])
    assert read["envelope"] is None and read["motion"] is None


# --- A6: section autonomy ----------------------------------------------------

@pytest.mark.parametrize("section,key", [("audio", "audio_envelope"), ("video", "motion")])
def test_one_failed_section_degrades_to_partial(tmp_path, section, key):
    payload = _build(tmp_path, runner=_runner(_pcm(6.0), _frames(24), fail=section))
    assert payload["status"] == "partial"
    assert payload["sections"][key]["status"] == "unavailable"
    assert payload["degradations"], "a degraded section must record a reason"
    others = [name for name, row in payload["sections"].items()
              if name != key and row["status"] == "available"]
    assert others, "the remaining sections must stay usable"
    if key == "audio_envelope":
        assert payload["sections"]["motion"]["status"] == "available"


def test_a_section_can_be_skipped_without_voiding_the_document(tmp_path):
    payload = _build(tmp_path, sections=("motion",))
    assert payload["status"] == "partial"
    assert payload["sections"]["motion"]["status"] == "available"
    assert payload["sections"]["audio_envelope"]["status"] == "unavailable"


def test_unknown_section_names_are_rejected(tmp_path):
    with pytest.raises(evidence.MaterialEvidenceError):
        _build(tmp_path, sections=("audio_envelope", "nonsense"))


def test_missing_media_is_rejected(tmp_path):
    with pytest.raises(evidence.MaterialEvidenceError, match="不存在"):
        evidence.build_material_evidence(tmp_path / "nope.mp4", output_root=tmp_path / "a",
                                         ffmpeg="ffmpeg", duration=1.0)


# --- isolation ---------------------------------------------------------------

def test_building_writes_nothing_outside_the_evidence_directory(tmp_path):
    artifacts = tmp_path / "artifacts"
    index = artifacts / "interaction-v1" / "sig" / "material-interaction-index.json"
    index.parent.mkdir(parents=True)
    index.write_text(json.dumps({"signature": "keep-me", "events": []}), encoding="utf-8")
    before = hashlib.sha256(index.read_bytes()).hexdigest()
    _build(tmp_path)
    assert hashlib.sha256(index.read_bytes()).hexdigest() == before
    written = {path.relative_to(artifacts).parts[0] for path in artifacts.rglob("*") if path.is_file()}
    assert written == {"interaction-v1", evidence.DIRECTORY}


def test_summary_is_short_and_reports_degradations(tmp_path):
    summary = evidence.summarize_material_evidence(_build(tmp_path, runner=_runner(_pcm(6.0), b"")))
    assert summary["status"] in {"partial", "unavailable"}
    assert summary["sections"]["motion"] == "unavailable"
    assert summary["degradations"]
    assert evidence.summarize_material_evidence(None)["status"] == "absent"
