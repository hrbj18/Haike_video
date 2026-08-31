from __future__ import annotations

import json

import pytest

from scripts.local_tts_profiles import export_pack, import_pack


def test_private_profile_pack_roundtrip(tmp_path):
    source = tmp_path / "source"
    sample = source / "profiles" / "yaya" / "sample.wav"
    sample.parent.mkdir(parents=True)
    sample.write_bytes(b"RIFFvoice")
    (source / "profiles.json").write_text(json.dumps({"profiles": [{
        "id": "yaya", "name": "雅雅", "voice_type": "cloned",
        "samples": [{"id": "s1", "audio_path": "profiles/yaya/sample.wav", "reference_text": "参考文本"}],
    }]}), encoding="utf-8")
    package = tmp_path / "voices.zip"

    exported = export_pack(source, package, ["yaya"])
    destination = tmp_path / "destination"
    imported = import_pack(destination, package)

    assert exported == {"output": str(package.resolve()), "profiles": 1, "audio_files": 1}
    assert imported["profiles"] == 1
    assert (destination / "profiles" / "yaya" / "sample.wav").read_bytes() == b"RIFFvoice"
    assert json.loads((destination / "profiles.json").read_text(encoding="utf-8"))["profiles"][0]["name"] == "雅雅"


def test_pack_export_rejects_path_escape(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "profiles.json").write_text(json.dumps({"profiles": [{
        "id": "bad", "name": "bad", "voice_type": "cloned",
        "samples": [{"audio_path": "../outside.wav", "reference_text": "x"}],
    }]}), encoding="utf-8")

    with pytest.raises(ValueError, match="不安全"):
        export_pack(source, tmp_path / "bad.zip")

