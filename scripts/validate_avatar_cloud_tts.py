"""Run a bounded two-role cloud TTS and exact-frame composition acceptance.

This script deliberately stops before RunningHub. It exercises the same
provider-neutral TTS runtime and role-track composer used by the avatar parent
job, producing safe evidence without exposing provider voice identifiers.
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from backlot.avatar_review_preview_pipeline import (
    ROLE_LABELS,
    _avatar_voice_profiles,
    _compose_role_track,
    _sha256_file,
    _timing_contract,
    _wav_facts,
)
from backlot.tts_runtime import generate_voice_audio


SAMPLE_LINES = (
    ("T001", "yaya", "大家好，欢迎收看今天的科技快报。"),
    ("T002", "mengmeng", "今天我们先从人工智能的新进展说起。"),
    ("T003", "yaya", "所有音频都会先统一格式，再进入数字人工作流。"),
    ("T004", "mengmeng", "这样可以保证切点连续，并避免声音发生错位。"),
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    profiles = _avatar_voice_profiles()
    records: dict[str, list[dict]] = {role: [] for role in ROLE_LABELS}
    turns: list[dict] = []
    for turn_id, role, text in SAMPLE_LINES:
        profile = profiles[role]
        target = output_dir / "turns" / f"{turn_id}-{role}.wav"
        result = generate_voice_audio(
            text=text,
            profile=profile,
            output_path=target,
            language="zh",
        )
        if not result.success or not target.is_file():
            raise RuntimeError(str(result.error or f"{turn_id} 配音未生成"))
        facts = _wav_facts(target)
        record = {
            "status": "completed",
            "turn_id": turn_id,
            "speaker_id": role,
            "path": str(target.relative_to(output_dir)).replace("\\", "/"),
            "profile_id": str(profile["id"]),
            "profile_name": str(profile.get("name") or ROLE_LABELS[role]),
            "provider_id": str(profile.get("provider_id") or "voicebox_tts"),
            "provider_name": str(profile.get("provider_name") or "Haike Video 本地配音"),
            "voice_signature": str(profile.get("voice_signature") or ""),
            "wav_sha256": _sha256_file(target),
            **facts,
        }
        records[role].append(record)
        turns.append(record)
    role_tracks = {}
    manifest_turns = []
    for role in ROLE_LABELS:
        track, timed_turns = _compose_role_track(output_dir, role, records[role])
        role_tracks[role] = track
        manifest_turns.extend(timed_turns)
    report = {
        "version": "avatar-cloud-tts-acceptance-v1",
        "status": "passed",
        "generated_at": datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "timing_contract": _timing_contract(),
        "turns": turns,
        "role_tracks": role_tracks,
        "timed_turns": manifest_turns,
    }
    report_path = output_dir / "acceptance.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    summary = {
        "status": report["status"],
        "turn_count": len(turns),
        "roles": {
            role: {
                "profile_id": profiles[role]["id"],
                "provider_id": profiles[role].get("provider_id"),
                "duration_seconds": role_tracks[role]["duration_seconds"],
                "sample_rate": role_tracks[role]["sample_rate"],
                "channels": role_tracks[role]["channels"],
                "sample_width": role_tracks[role]["sample_width"],
                "video_frame_count": role_tracks[role]["video_frame_count"],
                "frame_aligned": role_tracks[role]["sample_frame_count"]
                % role_tracks[role]["samples_per_video_frame"]
                == 0,
            }
            for role in ROLE_LABELS
        },
        "report": str(report_path),
    }
    print(json.dumps(summary, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
