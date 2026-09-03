"""Generate the three Voicebox listening assets for an episode.

The two speaker previews are generated as one text block per speaker. The full
dialogue preview is generated one stable turn at a time, then concatenated in
script order so a speaker profile can never leak into the other character.
These are pre-listening/reference assets; native avatar audio remains the
authoritative master once avatar videos are imported.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backlot.avatar_import import _find_binary  # noqa: PLC2701 - shared binary resolution
from tools.audio.voicebox_tts import VoiceboxTTS


def now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def probe_audio(path: Path) -> dict:
    ffprobe = _find_binary("ffprobe")
    if not ffprobe:
        raise RuntimeError("ffprobe is required to verify Voicebox audio")
    result = subprocess.run(
        [
            ffprobe, "-v", "error", "-show_entries",
            "format=duration,size:stream=codec_type,codec_name,sample_rate,channels",
            "-of", "json", str(path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr[-2000:] or f"ffprobe failed for {path.name}")
    payload = json.loads(result.stdout)
    audio = next((item for item in payload.get("streams", []) if item.get("codec_type") == "audio"), None)
    if not audio:
        raise RuntimeError(f"{path.name} contains no audio stream")
    duration = float((payload.get("format") or {}).get("duration") or 0)
    if duration <= 0:
        raise RuntimeError(f"{path.name} has no positive duration")
    return {
        "duration_seconds": round(duration, 3),
        "size_bytes": path.stat().st_size,
        "codec": audio.get("codec_name"),
        "sample_rate": int(audio.get("sample_rate") or 0),
        "channels": int(audio.get("channels") or 0),
    }


def run_voicebox(
    text: str,
    output_path: Path,
    *,
    profile_id: str,
    profile_name: str,
    engine: str,
    force: bool,
) -> dict:
    if output_path.exists() and not force:
        media = probe_audio(output_path)
        return {
            "path": str(output_path.relative_to(REPO_ROOT)).replace("\\", "/"),
            "profile_id": profile_id,
            "profile_name": profile_name,
            "engine": engine,
            "text_characters": len(text),
            "sha256": sha256(output_path),
            "media": media,
            "generation_id": None,
            "reused_existing": True,
        }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    print(f"Voicebox: {profile_name} -> {output_path.name}", flush=True)
    result = VoiceboxTTS().execute({
        "text": text,
        "profile_id": profile_id,
        "profile_name": profile_name,
        "engine": engine,
        "language": "zh",
        "normalize": True,
        "personality": False,
        "output_path": str(output_path),
    })
    if not result.success:
        raise RuntimeError(result.error or f"Voicebox failed for {output_path.name}")
    if not output_path.is_file() or output_path.stat().st_size == 0:
        raise RuntimeError(f"Voicebox reported success but wrote no file: {output_path}")
    media = probe_audio(output_path)
    return {
        "path": str(output_path.relative_to(REPO_ROOT)).replace("\\", "/"),
        "profile_id": profile_id,
        "profile_name": profile_name,
        "engine": engine,
        "text_characters": len(text),
        "sha256": sha256(output_path),
        "media": media,
        "generation_id": result.data.get("generation_id"),
    }


def concatenate_turns(turn_paths: list[Path], output_path: Path, *, force: bool) -> dict:
    ffmpeg = _find_binary("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is required to assemble the full dialogue preview")
    if output_path.exists() and not force:
        return {
            "path": str(output_path.relative_to(REPO_ROOT)).replace("\\", "/"),
            "sha256": sha256(output_path),
            "media": probe_audio(output_path),
            "turn_count": len(turn_paths),
            "reused_existing": True,
        }
    list_path = output_path.parent / "turns" / "concat-list.txt"
    list_path.write_text(
        "\n".join(f"file '{path.as_posix().replace(chr(39), chr(39) + chr(92) + chr(39) + chr(39))}'" for path in turn_paths) + "\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            ffmpeg, "-y", "-v", "error", "-f", "concat", "-safe", "0",
            "-i", str(list_path), "-ar", "48000", "-ac", "2", "-c:a", "pcm_s16le", str(output_path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0 or not output_path.is_file():
        raise RuntimeError(result.stderr[-3000:] or "ffmpeg could not concatenate the dialogue preview")
    return {
        "path": str(output_path.relative_to(REPO_ROOT)).replace("\\", "/"),
        "sha256": sha256(output_path),
        "media": probe_audio(output_path),
        "turn_count": len(turn_paths),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate Voicebox preview audio for one episode")
    parser.add_argument("project_id")
    parser.add_argument("--yaya-profile", required=True)
    parser.add_argument("--mengmeng-profile", required=True)
    parser.add_argument("--yaya-engine", default="qwen")
    parser.add_argument("--mengmeng-engine", default="qwen")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    project_dir = (REPO_ROOT / "projects" / args.project_id).resolve()
    script_path = project_dir / "artifacts" / "script.json"
    if not script_path.is_file():
        raise SystemExit(f"missing script artifact: {script_path}")
    script = json.loads(script_path.read_text(encoding="utf-8"))
    sections = script.get("sections") or []
    if not sections:
        raise SystemExit("script has no sections")

    output_dir = project_dir / "assets" / "audio" / "voicebox-preview"
    output_dir.mkdir(parents=True, exist_ok=True)
    yaya_sections = [section for section in sections if section.get("speaker_id") == "yaya"]
    mengmeng_sections = [section for section in sections if section.get("speaker_id") == "mengmeng"]
    if not yaya_sections or not mengmeng_sections:
        raise SystemExit("script must contain both yaya and mengmeng turns")

    outputs: dict[str, dict] = {}
    outputs["yaya_single"] = run_voicebox(
        "\n\n".join(section["text"] for section in yaya_sections),
        output_dir / "yaya-single.wav",
        profile_id=args.yaya_profile, profile_name="雅雅", engine=args.yaya_engine, force=args.force,
    )
    outputs["mengmeng_single"] = run_voicebox(
        "\n\n".join(section["text"] for section in mengmeng_sections),
        output_dir / "mengmeng-single.wav",
        profile_id=args.mengmeng_profile, profile_name="檬檬", engine=args.mengmeng_engine, force=args.force,
    )

    turn_dir = output_dir / "turns"
    turn_entries: list[dict] = []
    turn_paths: list[Path] = []
    for section in sections:
        speaker_id = section["speaker_id"]
        profile_id = args.yaya_profile if speaker_id == "yaya" else args.mengmeng_profile
        profile_name = "雅雅" if speaker_id == "yaya" else "檬檬"
        engine = args.yaya_engine if speaker_id == "yaya" else args.mengmeng_engine
        path = turn_dir / f"{section['turn_id']}-{speaker_id}.wav"
        turn_entries.append({
            "turn_id": section["turn_id"],
            "speaker_id": speaker_id,
            "text": section["text"],
            "audio": run_voicebox(
                section["text"], path, profile_id=profile_id, profile_name=profile_name,
                engine=engine, force=args.force,
            ),
        })
        turn_paths.append(path)

    outputs["dialogue_full"] = concatenate_turns(turn_paths, output_dir / "dialogue-full.wav", force=args.force)
    manifest = {
        "version": "1.0",
        "project_id": args.project_id,
        "generated_at": now(),
        "provider": "voicebox",
        "purpose": "pre_listening_reference_only",
        "native_avatar_audio_remains_master": True,
        "profiles": {"yaya": args.yaya_profile, "mengmeng": args.mengmeng_profile},
        "outputs": outputs,
        "turns": turn_entries,
    }
    (output_dir / "voicebox-preview-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    print(json.dumps({"outputs": outputs, "turn_count": len(turn_entries)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
