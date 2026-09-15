from __future__ import annotations

import json
import subprocess

import pytest

from backlot import material_pause_evidence as evidence


def _runner(stderr: str, returncode: int = 0):
    def run(command, **_kwargs):
        return subprocess.CompletedProcess(command, returncode, b"", stderr.encode("utf-8"))

    return run


def _stderr(*spans):
    return "\n".join(
        f"[silencedetect @ 0x1] silence_start: {start}\n"
        f"[silencedetect @ 0x1] silence_end: {end} | silence_duration: {end - start}"
        for start, end in spans
    )


def _media(tmp_path, name="media.mp4"):
    path = tmp_path / name
    path.write_bytes(b"fixture-audio")
    return path


# --- A1: parsing -------------------------------------------------------------

def test_parse_maps_relative_timestamps_back_to_source_time():
    rows, anomalies = evidence.parse_silencedetect(
        _stderr((1.0, 3.5), (4.0, 6.0)), offset=100.0, span=10.0, min_silence=0.45,
    )
    assert rows == [
        {"start": 101.0, "end": 103.5},
        {"start": 104.0, "end": 106.0},
    ]
    assert anomalies == 0


def test_parse_drops_intervals_shorter_than_the_minimum():
    rows, _ = evidence.parse_silencedetect(
        _stderr((1.0, 1.2), (3.0, 5.0)), offset=0.0, span=10.0, min_silence=0.45,
    )
    assert rows == [{"start": 3.0, "end": 5.0}]


def test_parse_reports_impossible_timestamps_as_anomalies_instead_of_trusting_them():
    rows, anomalies = evidence.parse_silencedetect(
        _stderr((1.0, 2.0), (900.0, 905.0)), offset=0.0, span=10.0, min_silence=0.45,
    )
    assert rows == [{"start": 1.0, "end": 2.0}]
    assert anomalies == 1


def test_parse_treats_a_missing_end_as_running_to_the_span_end():
    rows, _ = evidence.parse_silencedetect(
        "[silencedetect @ 0x1] silence_start: 2.0", offset=50.0, span=10.0, min_silence=0.45,
    )
    assert rows == [{"start": 52.0, "end": 60.0}]


# --- A1: ranges and identity -------------------------------------------------

def test_range_normalization_merges_overlaps_and_sorts():
    rows = evidence.normalize_ranges([
        {"start": 5.0, "end": 6.0},
        {"start": 1.0, "end": 2.0},
        {"start": 1.5, "end": 3.0},
    ])
    assert rows == [{"start": 1.0, "end": 3.0}, {"start": 5.0, "end": 6.0}]


@pytest.mark.parametrize("ranges", [[], None, [{"start": 2, "end": 1}], [{"start": "x", "end": 1}]])
def test_invalid_ranges_are_rejected(ranges):
    with pytest.raises(evidence.InteractionPauseEvidenceError):
        evidence.normalize_ranges(ranges)


@pytest.mark.parametrize("noise", [-61.0, -9.0, float("nan"), True])
def test_noise_threshold_out_of_range_is_rejected(noise):
    with pytest.raises(evidence.InteractionPauseEvidenceError):
        evidence.runtime_identity(noise_db=noise)


def test_identity_is_deterministic_and_sensitive_to_parameters():
    first = evidence.runtime_identity()
    assert first == evidence.runtime_identity()
    assert first["signature"] != evidence.runtime_identity(noise_db=-40.0)["signature"]


# --- A1: detection, aggregation, caching -------------------------------------

def test_detection_aggregates_ranges_and_merges_across_boundaries(tmp_path):
    media = _media(tmp_path)
    payload = evidence.detect_pause_evidence(
        media, [{"start": 0.0, "end": 10.0}, {"start": 10.0, "end": 20.0}], ffmpeg="ffmpeg",
        runner=_runner(_stderr((1.0, 3.0), (8.5, 11.5))),
    )
    assert payload["status"] == "available"
    assert payload["silences"] == [
        {"start": 1.0, "end": 3.0, "length": 2.0},
        {"start": 8.5, "end": 11.5, "length": 3.0},
    ]
    assert payload["metadata"]["silence_seconds"] == 5.0
    assert payload["identity"]["engine"] == "ffmpeg/silencedetect"


def test_second_call_hits_the_cache_with_identical_content(tmp_path):
    media = _media(tmp_path)
    first = evidence.detect_pause_evidence(
        media, [{"start": 0.0, "end": 10.0}], ffmpeg="ffmpeg", output_root=tmp_path / "out",
        runner=_runner(_stderr((2.0, 4.0))),
    )
    assert first["cache_hit"] is False

    def exploding(command, **_kwargs):
        raise AssertionError("cache hit must not re-run ffmpeg")

    second = evidence.detect_pause_evidence(
        media, [{"start": 0.0, "end": 10.0}], ffmpeg="ffmpeg", output_root=tmp_path / "out",
        runner=exploding,
    )
    assert second["cache_hit"] is True
    assert second["silences"] == first["silences"]
    assert len(list((tmp_path / "out" / "pause-evidence").glob("*.json"))) == 1


def test_changing_the_media_invalidates_the_cache(tmp_path):
    media = _media(tmp_path)
    evidence.detect_pause_evidence(
        media, [{"start": 0.0, "end": 10.0}], ffmpeg="ffmpeg", output_root=tmp_path / "out",
        runner=_runner(_stderr((2.0, 4.0))),
    )
    media.write_bytes(b"different-audio")
    again = evidence.detect_pause_evidence(
        media, [{"start": 0.0, "end": 10.0}], ffmpeg="ffmpeg", output_root=tmp_path / "out",
        runner=_runner(_stderr((5.0, 7.0))),
    )
    assert again["cache_hit"] is False
    assert again["silences"] == [{"start": 5.0, "end": 7.0, "length": 2.0}]


# --- A1: degradation instead of exceptions ----------------------------------

def test_probe_failure_degrades_to_unavailable_and_keeps_an_explicit_reason(tmp_path):
    media = _media(tmp_path)
    payload = evidence.detect_pause_evidence(
        media, [{"start": 0.0, "end": 10.0}], ffmpeg="ffmpeg",
        runner=_runner("[silencedetect @ 0x1] No such filter: 'silencedetect'", returncode=1),
    )
    assert payload["status"] == "unavailable"
    assert payload["silences"] == []
    assert payload["metadata"]["failed_ranges"] == 1
    assert "silencedetect" in payload["failures"][0]["error"]


def test_one_unreadable_range_degrades_to_partial_and_keeps_the_rest(tmp_path):
    media = _media(tmp_path)
    calls = {"n": 0}

    def run(command, **_kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            return subprocess.CompletedProcess(command, 1, b"", b"decode error")
        return subprocess.CompletedProcess(command, 0, b"", _stderr((1.0, 3.0)).encode("utf-8"))

    payload = evidence.detect_pause_evidence(
        media, [{"start": 0.0, "end": 10.0}, {"start": 20.0, "end": 30.0}], ffmpeg="ffmpeg", runner=run,
    )
    assert payload["status"] == "partial"
    assert payload["metadata"]["failed_ranges"] == 1
    assert payload["metadata"]["silence_count"] == 1


def test_missing_media_is_reported_before_any_probe(tmp_path):
    def exploding(command, **_kwargs):
        raise AssertionError("no probe should run for a missing file")

    with pytest.raises(evidence.InteractionPauseEvidenceUnavailable, match="不存在"):
        evidence.detect_pause_evidence(
            tmp_path / "nope.mp4", [{"start": 0.0, "end": 1.0}], ffmpeg="ffmpeg", runner=exploding,
        )


def test_probe_command_uses_the_supplied_media_and_bounded_seek(tmp_path):
    media = _media(tmp_path)
    seen = {}

    def run(command, **_kwargs):
        seen["command"] = command
        return subprocess.CompletedProcess(command, 0, b"", _stderr((1.0, 2.0)).encode("utf-8"))

    evidence.detect_pause_evidence(
        media, [{"start": 30.0, "end": 40.0}], ffmpeg="ffmpeg", runner=run,
    )
    command = seen["command"]
    assert command[command.index("-ss") + 1] == "30.000000"
    assert command[command.index("-t") + 1] == "10.000000"
    assert command[command.index("-i") + 1] == str(media.resolve())
    assert "silencedetect=noise=-30dB:d=0.45" in command
    assert command[-4:] == ["-f", "null", "-"] or command[-3:] == ["-f", "null", "-"]
    assert "-loglevel" in command and command[command.index("-loglevel") + 1] == "info"


def test_cache_payload_is_valid_json_under_the_expected_directory(tmp_path):
    media = _media(tmp_path)
    evidence.detect_pause_evidence(
        media, [{"start": 0.0, "end": 10.0}], ffmpeg="ffmpeg", output_root=tmp_path / "out",
        runner=_runner(_stderr((2.0, 4.0))),
    )
    files = list((tmp_path / "out" / "pause-evidence").glob("*.json"))
    assert len(files) == 1
    payload = json.loads(files[0].read_text(encoding="utf-8"))
    assert payload["status"] == "available"
    assert payload["cache_key"]


# --- A4: backend selection ---------------------------------------------------

def _pcm(samples) -> bytes:
    import numpy as np

    return np.asarray(samples, dtype="<i2").tobytes()


def _tone(seconds: float, amplitude: float = 0.35, frequency: float = 220.0) -> bytes:
    import numpy as np

    t = np.arange(int(16000 * seconds)) / 16000
    return _pcm(np.sin(2 * np.pi * frequency * t) * amplitude * 32767)


def _silence(seconds: float) -> bytes:
    import numpy as np

    return _pcm(np.zeros(int(16000 * seconds), dtype="<i2"))


def _audio_runner(payload: bytes, stderr: str = ""):
    def run(command, **_kwargs):
        return subprocess.CompletedProcess(command, 0, payload, stderr.encode("utf-8"))

    return run


def test_envelope_backend_answers_from_a_single_decode_without_seeking(tmp_path):
    media = _media(tmp_path)
    seen: list[list[str]] = []

    def run(command, **_kwargs):
        seen.append(list(command))
        return subprocess.CompletedProcess(command, 0, _tone(1.0) + _silence(1.0) + _tone(1.0), b"")

    payload = evidence.detect_pause_evidence(
        media, [{"start": 0.0, "end": 3.0}], ffmpeg="ffmpeg", backend="envelope", runner=run,
    )
    assert payload["detector"] == "envelope/rms"
    assert payload["backend"] == "envelope"
    assert payload["metadata"]["spawns"] == 1
    assert len(seen) == 1
    assert "-ss" not in seen[0]
    assert payload["status"] == "available"
    assert len(payload["silences"]) == 1
    assert payload["silences"][0]["start"] == pytest.approx(1.0, abs=0.05)
    assert payload["silences"][0]["end"] == pytest.approx(2.0, abs=0.05)


def test_auto_falls_back_to_ffmpeg_when_the_decode_yields_nothing(tmp_path):
    media = _media(tmp_path)

    def run(command, **_kwargs):
        # Empty decode: stdout is empty, so the envelope cannot answer anything.
        return subprocess.CompletedProcess(command, 0, b"", _stderr((1.0, 3.0)).encode("utf-8"))

    payload = evidence.detect_pause_evidence(media, [{"start": 0.0, "end": 10.0}],
                                             ffmpeg="ffmpeg", runner=run)
    assert payload["detector"] == "ffmpeg/silencedetect"
    assert payload["backend"] == "ffmpeg"
    assert payload["silences"] == [{"start": 1.0, "end": 3.0, "length": 2.0}]
    assert any(item.startswith("audio_envelope_empty") for item in payload["degradations"])


def test_missing_numpy_degrades_the_envelope_and_records_the_reason(tmp_path, monkeypatch):
    from backlot import material_audio_envelope as envelope_module

    def refuse():
        raise envelope_module.MaterialAudioEnvelopeUnavailable("本机 numpy 不可用，音频包络整体降级")

    monkeypatch.setattr(envelope_module, "_import_numpy", refuse)
    media = _media(tmp_path)
    payload = evidence.detect_pause_evidence(
        media, [{"start": 0.0, "end": 10.0}], ffmpeg="ffmpeg",
        runner=_audio_runner(b"", _stderr((1.0, 3.0))),
    )
    assert payload["detector"] == "ffmpeg/silencedetect"
    assert any(item.startswith("audio_envelope_unavailable") for item in payload["degradations"])
    assert payload["silences"] == [{"start": 1.0, "end": 3.0, "length": 2.0}]


def test_ffmpeg_backend_keeps_the_legacy_payload_content(tmp_path):
    media = _media(tmp_path)
    payload = evidence.detect_pause_evidence(
        media, [{"start": 0.0, "end": 10.0}], ffmpeg="ffmpeg", backend="ffmpeg",
        runner=_runner(_stderr((2.0, 4.0))),
    )
    # Content fields must be exactly what the pre-envelope implementation produced.
    assert payload["status"] == "available"
    assert payload["silences"] == [{"start": 2.0, "end": 4.0, "length": 2.0}]
    assert payload["failures"] == []
    assert payload["metadata"]["range_count"] == 1
    assert payload["metadata"]["failed_ranges"] == 0
    assert payload["metadata"]["silence_count"] == 1
    assert payload["metadata"]["anomalies"] == 0
    assert payload["metadata"]["silence_seconds"] == 2.0
    assert payload["identity"]["engine"] == "ffmpeg/silencedetect"
    assert payload["degradations"] == []


def test_envelope_cache_is_reused_across_different_ranges(tmp_path):
    media = _media(tmp_path)
    calls = {"n": 0}

    def run(command, **_kwargs):
        calls["n"] += 1
        return subprocess.CompletedProcess(command, 0, _tone(1.0) + _silence(1.0) + _tone(1.0), b"")

    output = tmp_path / "out"
    first = evidence.detect_pause_evidence(media, [{"start": 0.0, "end": 3.0}], ffmpeg="ffmpeg",
                                           output_root=output, backend="envelope", runner=run)
    assert first["metadata"]["spawns"] == 1
    assert calls["n"] == 1
    # A different range set misses the JSON cache but must still hit the npz envelope.
    second = evidence.detect_pause_evidence(media, [{"start": 0.5, "end": 2.5}], ffmpeg="ffmpeg",
                                            output_root=output, backend="envelope", runner=run)
    assert second["metadata"]["spawns"] == 0
    assert second["metadata"]["envelope_cache_hit"] is True
    assert calls["n"] == 1


def test_requested_backend_honours_the_environment_switch(monkeypatch):
    monkeypatch.setenv(evidence.AUDIO_BACKEND_ENV, "ffmpeg")
    assert evidence.requested_backend() == "ffmpeg"
    assert evidence.requested_backend("envelope") == "envelope"
    monkeypatch.setenv(evidence.AUDIO_BACKEND_ENV, "nonsense")
    with pytest.raises(evidence.InteractionPauseEvidenceError):
        evidence.requested_backend()


def test_identity_records_the_detector_and_splits_backends():
    ffmpeg_identity = evidence.runtime_identity(backend="ffmpeg")
    envelope_identity = evidence.runtime_identity(backend="envelope")
    assert ffmpeg_identity["detector"] == evidence.DETECTOR_FFMPEG
    assert envelope_identity["detector"] == evidence.DETECTOR_ENVELOPE
    assert envelope_identity["window_ms"] == 20.0
    assert envelope_identity["hop_ms"] == 10.0
    assert ffmpeg_identity["signature"] != envelope_identity["signature"]
    # The envelope threshold is part of the identity, the ffmpeg noise level is not.
    assert (evidence.runtime_identity(backend="envelope", envelope_threshold_db=-44.0)["signature"]
            != envelope_identity["signature"])
    assert (evidence.runtime_identity(backend="ffmpeg", envelope_threshold_db=-44.0)["signature"]
            == ffmpeg_identity["signature"])


def test_envelope_threshold_out_of_range_is_rejected(tmp_path):
    media = _media(tmp_path)
    with pytest.raises(evidence.InteractionPauseEvidenceError):
        evidence.detect_pause_evidence(media, [{"start": 0.0, "end": 1.0}], ffmpeg="ffmpeg",
                                       backend="envelope", envelope_threshold_db=-3.0,
                                       runner=_audio_runner(_tone(1.0)))


def test_bridging_merges_quiet_runs_split_by_a_short_loud_blip():
    """A street gap usually contains one loud frame; without bridging it vanishes.

    ``silence_intervals`` needs contiguous quiet frames, so a single excursion
    truncates the run and the whole gap is offered to nobody — measured on the
    acceptance material, only 23.4 s of the 50.8 s of real VAD gaps were visible
    at −40 dB, and bridging 0.10 s raises that to 36.4 s.
    """
    rows = [{"start": 1.0, "end": 2.0}, {"start": 2.1, "end": 3.0}, {"start": 5.0, "end": 6.0}]
    assert evidence.bridge_intervals(rows, 0.0) == rows
    assert [(row["start"], row["end"]) for row in evidence.bridge_intervals(rows, 0.2)] == \
        [(1.0, 3.0), (5.0, 6.0)]
    assert [(row["start"], row["end"]) for row in evidence.bridge_intervals(rows, 0.05)] == \
        [(1.0, 2.0), (2.1, 3.0), (5.0, 6.0)]
    assert evidence.bridge_intervals([], 0.2) == []
    # Bridging is part of the identity: two probes that disagree about the gaps
    # must never share a cache entry.
    assert (evidence.runtime_identity(bridge_seconds=0.1)["signature"]
            != evidence.runtime_identity(bridge_seconds=0.0)["signature"])
    with pytest.raises(evidence.InteractionPauseEvidenceError):
        evidence.runtime_identity(bridge_seconds=2.0)
