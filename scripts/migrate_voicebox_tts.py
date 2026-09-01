"""One-time migration from Voicebox profiles into Haike Video local TTS data.

The source database is opened read-only.  Original profiles and audio samples
are never modified.  Model cache import supports hard links on the same volume
so the resulting Haike Video cache survives deletion of the old directory
without duplicating multi-gigabyte blobs.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = Path(os.environ.get("APPDATA", "")) / "sh.voicebox.app"
DEFAULT_DESTINATION = REPO_ROOT / ".backlot" / "tts"
SUPPORTED_PRESET_ENGINES = {"qwen_custom_voice"}
MODEL_CACHE_NAMES = {
    "base": "models--Qwen--Qwen3-TTS-12Hz-1.7B-Base",
    "custom": "models--Qwen--Qwen3-TTS-12Hz-1.7B-CustomVoice",
}


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return default


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _source_audio_path(source: Path, raw: str) -> Path:
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = (source / candidate).resolve()
        try:
            candidate.relative_to(source.resolve())
        except ValueError as exc:
            raise ValueError(f"Voicebox sample escapes the source directory: {raw}") from exc
    if not candidate.is_file():
        raise FileNotFoundError(f"Voicebox sample is missing: {candidate}")
    return candidate


def migrate_profiles(source: Path, destination: Path) -> dict[str, Any]:
    database = source / "voicebox.db"
    if not database.is_file():
        raise FileNotFoundError(f"Voicebox database not found: {database}")
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    migrated: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    copied_samples = 0
    try:
        rows = connection.execute(
            """
            SELECT id, name, description, language, voice_type, preset_engine,
                   preset_voice_id, default_engine, personality
              FROM profiles ORDER BY created_at, name
            """
        ).fetchall()
        for row in rows:
            profile = dict(row)
            voice_type = str(profile.get("voice_type") or "cloned")
            preset_engine = str(profile.get("preset_engine") or "")
            if voice_type == "preset" and preset_engine not in SUPPORTED_PRESET_ENGINES:
                skipped.append({"id": profile["id"], "name": profile["name"], "reason": f"unsupported preset engine: {preset_engine}"})
                continue
            samples: list[dict[str, Any]] = []
            sample_rows = connection.execute(
                "SELECT id, audio_path, reference_text FROM profile_samples WHERE profile_id=? ORDER BY rowid",
                (profile["id"],),
            ).fetchall()
            for sample_row in sample_rows:
                sample = dict(sample_row)
                source_audio = _source_audio_path(source, str(sample["audio_path"]))
                relative_audio = Path("profiles") / profile["id"] / f"{sample['id']}{source_audio.suffix.lower()}"
                target_audio = destination / relative_audio
                target_audio.parent.mkdir(parents=True, exist_ok=True)
                if not target_audio.is_file() or target_audio.stat().st_size != source_audio.stat().st_size:
                    shutil.copy2(source_audio, target_audio)
                    copied_samples += 1
                samples.append({
                    "id": str(sample["id"]),
                    "audio_path": relative_audio.as_posix(),
                    "reference_text": str(sample["reference_text"]),
                })
            if voice_type == "cloned" and not samples:
                skipped.append({"id": profile["id"], "name": profile["name"], "reason": "cloned profile has no samples"})
                continue

            item = {
                "id": str(profile["id"]),
                "name": str(profile["name"]),
                "description": str(profile.get("description") or ""),
                "language": str(profile.get("language") or "zh"),
                "voice_type": voice_type,
                "preset_engine": profile.get("preset_engine"),
                "preset_voice_id": profile.get("preset_voice_id"),
                "default_engine": str(profile.get("default_engine") or ("qwen" if voice_type == "cloned" else "qwen_custom_voice")),
                "personality": profile.get("personality"),
                "samples": samples,
                "migrated_from": "Voicebox",
            }
            if samples:
                if "强情感" in item["name"] and len(samples) > 1:
                    preferred = samples[1]
                else:
                    preferred = max(samples, key=lambda value: len(str(value.get("reference_text") or "")))
                item["preferred_sample_id"] = preferred["id"]
            migrated.append(item)
    finally:
        connection.close()

    local_file = destination / "profiles.json"
    existing = _read_json(local_file, {}).get("profiles", [])
    merged = {str(item.get("id")): item for item in existing if isinstance(item, dict) and item.get("id")}
    merged.update({item["id"]: item for item in migrated})
    _write_json(local_file, {"version": 1, "profiles": list(merged.values())})
    report = {
        "source": str(source.resolve()),
        "destination": str(destination.resolve()),
        "migrated_profiles": len(migrated),
        "copied_samples": copied_samples,
        "skipped": skipped,
        "profile_ids": [item["id"] for item in migrated],
    }
    _write_json(destination / "migration-report.json", report)
    return report


def _link_or_copy(source: str, destination: str, mode: str) -> str:
    if mode == "copy":
        return shutil.copy2(source, destination)
    try:
        os.link(source, destination)
        return destination
    except OSError:
        return shutil.copy2(source, destination)


def import_models(source_cache: Path, destination: Path, selection: str, mode: str) -> list[str]:
    names = list(MODEL_CACHE_NAMES.values()) if selection == "all" else [MODEL_CACHE_NAMES[selection]]
    imported: list[str] = []
    destination.mkdir(parents=True, exist_ok=True)
    for name in names:
        source = source_cache / name
        if not source.is_dir():
            raise FileNotFoundError(f"Voicebox model cache not found: {source}")
        target = destination / name
        shutil.copytree(
            source,
            target,
            dirs_exist_ok=True,
            symlinks=True,
            copy_function=lambda src, dst: _link_or_copy(src, dst, mode),
        )
        imported.append(name)
    return imported


def main() -> int:
    parser = argparse.ArgumentParser(description="Migrate Voicebox profiles into Haike Video local TTS")
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--destination", type=Path, default=DEFAULT_DESTINATION)
    parser.add_argument("--model-cache", type=Path)
    parser.add_argument("--models", choices=("none", "base", "custom", "all"), default="none")
    parser.add_argument("--model-mode", choices=("hardlink", "copy"), default="hardlink")
    args = parser.parse_args()
    report = migrate_profiles(args.source.resolve(), args.destination.resolve())
    if args.models != "none":
        if not args.model_cache:
            parser.error("--model-cache is required when --models is not none")
        report["imported_models"] = import_models(
            args.model_cache.resolve(),
            args.destination.resolve() / "models",
            args.models,
            args.model_mode,
        )
        _write_json(args.destination.resolve() / "migration-report.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

