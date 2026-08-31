import json
from pathlib import Path


def test_sample_episode_has_reusable_source_files(sample_episode_repo: Path):
    project = sample_episode_repo / "content" / "episodes" / "sample-tech-chat"
    assert (project / "project.yaml").exists()
    assert (project / "script" / "yaya-clean.txt").exists()
    assert (project / "script" / "mengmeng-clean.txt").exists()
    assert (project / "script" / "full-dialogue.md").exists()


def test_dialogue_timeline_has_four_ordered_segments(sample_episode_repo: Path):
    project = sample_episode_repo / "content" / "episodes" / "sample-tech-chat"
    data = json.loads((project / "timeline" / "dialogue.json").read_text(encoding="utf-8"))
    segments = data["segments"]
    assert len(segments) == 4
    assert [item["index"] for item in segments] == list(range(1, 5))
    assert segments[0]["speaker_id"] == "yaya"
    assert segments[1]["speaker_id"] == "mengmeng"
