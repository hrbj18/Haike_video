from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture
def sample_episode_repo(tmp_path: Path) -> Path:
    project = tmp_path / "content" / "episodes" / "sample-tech-chat"
    for name in ("docs", "script", "timeline", "composition", "media", "qa"):
        (project / name).mkdir(parents=True, exist_ok=True)

    (project / "project.yaml").write_text(
        """id: sample-tech-chat
title: Portable sample episode
version: 1
format:
  fps: 25
  aspect_ratio: '9:16'
speakers:
  - id: yaya
    display_name: Yaya
    native_audio_required: true
  - id: mengmeng
    display_name: Mengmeng
    native_audio_required: true
providers:
  tts: voicebox
quality_gates:
  audio_alignment: true
  source_citation: false
""",
        encoding="utf-8",
    )
    (project / "script" / "yaya-clean.txt").write_text("First line.\n\nThird line.\n", encoding="utf-8")
    (project / "script" / "mengmeng-clean.txt").write_text("Second line.\n\nFourth line.\n", encoding="utf-8")
    (project / "script" / "full-dialogue.md").write_text(
        "Yaya: First line.\n\nMengmeng: Second line.\n\nYaya: Third line.\n\nMengmeng: Fourth line.\n",
        encoding="utf-8",
    )
    segments = [
        {"index": 1, "speaker_id": "yaya", "text_ref": "script/yaya-clean.txt", "paragraph": 1},
        {"index": 2, "speaker_id": "mengmeng", "text_ref": "script/mengmeng-clean.txt", "paragraph": 1},
        {"index": 3, "speaker_id": "yaya", "text_ref": "script/yaya-clean.txt", "paragraph": 2},
        {"index": 4, "speaker_id": "mengmeng", "text_ref": "script/mengmeng-clean.txt", "paragraph": 2},
    ]
    (project / "timeline" / "dialogue.json").write_text(
        json.dumps({"segments": segments}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return tmp_path
