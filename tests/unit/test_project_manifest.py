from pathlib import Path

import pytest

from lib.project_manifest import ProjectManifestError, load_project_manifest


def test_sample_episode_manifest_is_valid(sample_episode_repo: Path):
    manifest = sample_episode_repo / "content" / "episodes" / "sample-tech-chat" / "project.yaml"
    data = load_project_manifest(manifest)
    assert data["id"] == "sample-tech-chat"
    assert data["providers"]["tts"] == "voicebox"
    assert all(item["native_audio_required"] for item in data["speakers"])


def test_invalid_project_id_is_rejected():
    with pytest.raises(ProjectManifestError):
        from lib.project_manifest import validate_project_manifest

        validate_project_manifest(
            {
                "id": "../bad",
                "title": "Bad",
                "version": 1,
                "format": {"fps": 25, "aspect_ratio": "9:16"},
                "speakers": [{"id": "a", "display_name": "A"}],
                "providers": {"tts": "voicebox"},
                "quality_gates": {"audio_alignment": True},
            }
        )
