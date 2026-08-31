from pathlib import Path

from backlot import workbench as workbench_mod
from backlot.visual_director import DirectorDecision
from tools.video.stock_sources.base import Candidate


def _state_with_existing_pexels(project: Path, payload: bytes = b"same-video") -> dict:
    existing = project / "assets" / "video" / "pexels" / "existing.mp4"
    existing.parent.mkdir(parents=True, exist_ok=True)
    existing.write_bytes(payload)
    return {
        "assets": [{
            "id": "A-existing",
            "type": "video",
            "provider": "Pexels",
            "source_tool": "pexels_video",
            "path": "assets/video/pexels/existing.mp4",
            "generation": {"video_id": "provider-1"},
        }],
    }


def test_downloaded_visual_identity_matches_bytes_not_provider_id(tmp_path: Path) -> None:
    state = _state_with_existing_pexels(tmp_path)
    candidate = tmp_path / "assets" / "video" / "pexels" / "candidate.mp4"
    candidate.write_bytes(b"same-video")

    sha256, duplicate = workbench_mod._downloaded_visual_content_identity(
        tmp_path, state, candidate, "video"
    )

    assert len(sha256) == 64
    assert duplicate == {
        "asset_id": "A-existing",
        "path": "assets/video/pexels/existing.mp4",
        "provider_id": "provider-1",
        "sha256": sha256,
    }


def test_downloaded_visual_identity_accepts_different_content(tmp_path: Path) -> None:
    state = _state_with_existing_pexels(tmp_path)
    candidate = tmp_path / "assets" / "video" / "pexels" / "candidate.mp4"
    candidate.write_bytes(b"different-video")

    sha256, duplicate = workbench_mod._downloaded_visual_content_identity(
        tmp_path, state, candidate, "video"
    )

    assert len(sha256) == 64
    assert duplicate is None


def test_autonomous_pexels_retries_when_different_id_downloads_same_content(
    tmp_path: Path, monkeypatch
) -> None:
    state = _state_with_existing_pexels(tmp_path)
    scene = {"id": "section-002", "title": "场景 2"}
    block = {"id": "VB-003", "slot_text": "第三个画面"}
    item = {
        "scene_id": scene["id"],
        "block_id": block["id"],
        "query": "technology keyboard",
        "slot_text": "第三个画面",
        "context_text": "不同键盘画面",
        "visual_intent": "键盘安全",
        "candidate_limit": 4,
        "director_ledger": {"attempts": [], "status": "pending"},
    }
    candidates = [
        Candidate(
            source="pexels", source_id="provider-2",
            source_url="https://www.pexels.com/video/provider-2/",
            download_url="https://video.example/provider-2.mp4", kind="video",
            width=1080, height=1920, duration=6, source_tags="keyboard",
        ),
        Candidate(
            source="pexels", source_id="provider-3",
            source_url="https://www.pexels.com/video/provider-3/",
            download_url="https://video.example/provider-3.mp4", kind="video",
            width=1080, height=1920, duration=6, source_tags="keyboard typing",
        ),
    ]
    search_calls: list[str] = []
    download_calls: list[str] = []

    def fake_search(_self, query, _filters):
        search_calls.append(query)
        return [candidates[len(search_calls) - 1]]

    def fake_download(_self, candidate, output):
        download_calls.append(candidate.source_id)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"same-video" if candidate.source_id == "provider-2" else b"new-video")
        return output

    decisions = [
        DirectorDecision(candidates[0], "accept", {"decision": "accept", "reason": "候选 1", "weighted_score": 85}, ("keyboard typing",)),
        DirectorDecision(candidates[1], "accept", {"decision": "accept", "reason": "候选 2", "weighted_score": 88}, ()),
    ]
    monkeypatch.setenv("PEXELS_API_KEY", "test-key")
    monkeypatch.setattr(workbench_mod.PexelsSource, "search", fake_search)
    monkeypatch.setattr(workbench_mod.PexelsSource, "download", fake_download)
    monkeypatch.setattr(workbench_mod, "decide_candidate", lambda *_args, **_kwargs: decisions.pop(0))
    monkeypatch.setattr(workbench_mod, "_save", lambda _project, current: current)
    monkeypatch.setattr(
        workbench_mod,
        "_screen_visual_candidate",
        lambda *_args: {"status": "passed", "score": 90, "reasons": [], "metrics": {}, "mode": "test"},
    )

    result, path, screening = workbench_mod._find_autonomous_pexels_candidate(
        tmp_path, state, item, scene, block,
        media_kind="video", query=item["query"], orientation="portrait",
        target_duration=5, content_rules=[], person_policy="balanced",
        used_provider_ids=set(),
    )

    assert result.success is True
    assert screening["status"] == "passed"
    assert (tmp_path / path).read_bytes() == b"new-video"
    assert download_calls == ["provider-2", "provider-3"]
    assert item["director_ledger"]["attempts"][0]["post_download_duplicate"]["asset_id"] == "A-existing"
    assert item["director_ledger"]["selected"]["provider_id"] == "provider-3"
    assert item["downloaded_content_sha256"]
