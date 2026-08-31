from pathlib import Path

from scripts.qa_project import qa_project


def test_sample_episode_contract_qa_passes(sample_episode_repo: Path):
    report = qa_project("sample-tech-chat", sample_episode_repo)
    assert report["passed"] is True
    assert report["timeline_segments"] == 4
    assert report["errors"] == []
