from __future__ import annotations

import json
import sqlite3

from scripts.migrate_voicebox_tts import migrate_profiles


def test_migration_preserves_profile_id_and_copies_private_sample(tmp_path):
    source = tmp_path / "voicebox"
    destination = tmp_path / "openmontage"
    sample = source / "profiles" / "voice-yaya" / "sample.wav"
    sample.parent.mkdir(parents=True)
    sample.write_bytes(b"RIFFreference")
    connection = sqlite3.connect(source / "voicebox.db")
    connection.executescript("""
        CREATE TABLE profiles (
          id TEXT PRIMARY KEY, name TEXT, description TEXT, language TEXT,
          voice_type TEXT, preset_engine TEXT, preset_voice_id TEXT,
          default_engine TEXT, personality TEXT, created_at TEXT
        );
        CREATE TABLE profile_samples (
          id TEXT PRIMARY KEY, profile_id TEXT, audio_path TEXT, reference_text TEXT
        );
    """)
    connection.execute(
        "INSERT INTO profiles VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("voice-yaya", "雅雅", "", "zh", "cloned", None, None, "qwen", None, "2026-01-01"),
    )
    connection.execute(
        "INSERT INTO profile_samples VALUES (?,?,?,?)",
        ("sample", "voice-yaya", "profiles/voice-yaya/sample.wav", "这是参考台词。"),
    )
    connection.commit()
    connection.close()

    report = migrate_profiles(source, destination)
    manifest = json.loads((destination / "profiles.json").read_text(encoding="utf-8"))

    assert report["migrated_profiles"] == 1
    assert manifest["profiles"][0]["id"] == "voice-yaya"
    assert manifest["profiles"][0]["preferred_sample_id"] == "sample"
    assert (destination / "profiles" / "voice-yaya" / "sample.wav").read_bytes() == b"RIFFreference"

