"""Unit contracts for download-last autonomous visual selection."""
from __future__ import annotations

from backlot.visual_director import (
    DirectorDecision,
    candidate_asset_id,
    decide_candidate,
    prepare_candidates,
)
from tools.video.stock_sources.base import Candidate


def _candidate(source_id: str, *, tags: str = "industrial robot factory", duration: float = 9) -> Candidate:
    return Candidate(
        source="pexels", source_id=source_id,
        source_url=f"https://www.pexels.com/video/{source_id}/",
        download_url=f"https://signed.example/{source_id}.mp4?secret=never-persist",
        kind="video", width=1080, height=1920, duration=duration,
        source_tags=tags, thumbnail_url=f"https://images.example/{source_id}.jpg",
        extra={"preview_frames": ["https://images.example/one.jpg"]},
    )


def _slot() -> dict:
    return {
        "scene_id": "T001", "block_id": "VB-001", "slot_text": "机器人正在进入工厂。",
        "context_text": "这条新闻讲机器人在自动化工厂中的应用。",
        "visual_intent": "工业机器人生产过程", "query": "industrial robot factory",
        "recently_used_asset_ids": [],
    }


def test_prepare_candidates_is_metadata_only_and_filters_before_download():
    accepted = prepare_candidates(
        [_candidate("good"), _candidate("short", duration=2), _candidate("wide")],
        media_kind="video", orientation="portrait", minimum_duration=5, used_provider_ids=set(),
    )
    assert [candidate.source_id for candidate in accepted] == ["good", "wide"]
    # This service has no download side effect; signed URLs remain only in-memory.
    assert all("secret=" in candidate.download_url for candidate in accepted)


def test_model_choice_is_whitelisted_and_ledger_never_contains_download_url():
    candidate = _candidate("34775736")

    def evaluator(_payload):
        return ({
            "selected_asset_id": candidate_asset_id(candidate), "semantic_score": 92,
            "aesthetic_score": 84, "continuity_score": 88, "technical_score": 95,
            "confidence": .91, "decision": "accept", "reason": "主体、场景和科技纪录片质感均匹配。",
            "retry_queries": [],
        }, "project-fixed-model")

    decision = decide_candidate(_slot(), [candidate], evaluator=evaluator)
    assert decision.decision == "accept"
    assert decision.candidate == candidate
    assert decision.ledger["selected_asset_id"] == "pexels:34775736"
    assert "secret=" not in repr(decision.ledger)


def test_invalid_model_asset_id_uses_deterministic_safe_fallback():
    candidate = _candidate("good")

    def evaluator(_payload):
        return ({
            "selected_asset_id": "pexels:not-in-candidates", "semantic_score": 90,
            "aesthetic_score": 90, "continuity_score": 90, "technical_score": 90,
            "confidence": .9, "decision": "accept", "reason": "bad", "retry_queries": [],
        }, "project-fixed-model")

    decision = decide_candidate(_slot(), [candidate], evaluator=evaluator)
    assert decision.candidate == candidate
    assert decision.ledger["decision_source"] == "deterministic_fallback"


def test_low_confidence_accept_becomes_automatic_retry():
    candidate = _candidate("good")

    def evaluator(_payload):
        return ({
            "selected_asset_id": candidate_asset_id(candidate), "semantic_score": 91,
            "aesthetic_score": 85, "continuity_score": 84, "technical_score": 95,
            "confidence": .32, "decision": "accept", "reason": "证据不足。",
            "retry_queries": ["robot arm assembly line"],
        }, "project-fixed-model")

    decision = decide_candidate(_slot(), [candidate], evaluator=evaluator)
    assert decision.decision == "retry"
    assert decision.candidate is None
    assert decision.retry_queries == ("robot arm assembly line",)
