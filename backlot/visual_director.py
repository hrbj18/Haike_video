"""Pre-download candidate selection for the autonomous visual director.

The service is intentionally small and pure: Pexels adapters retrieve metadata,
the configured project model recommends one whitelisted candidate, and the
workbench decides when that candidate may be downloaded.  This prevents a
runtime Codex conversation from affecting production asset choices.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Callable, Iterable

from backlot.ai_text import (
    TextAIError,
    VISUAL_CANDIDATE_DIRECTOR_PROMPT_VERSION,
    evaluate_visual_candidates,
)
from tools.video.stock_sources.base import Candidate


DIRECTOR_VERSION = "autonomous-visual-director-v1"
MIN_SEMANTIC_SCORE = 65.0
MIN_CONFIDENCE = 0.70
SCORE_WEIGHTS = {
    "semantic_score": 0.50,
    "aesthetic_score": 0.25,
    "continuity_score": 0.15,
    "technical_score": 0.10,
}


@dataclass(frozen=True)
class DirectorDecision:
    candidate: Candidate | None
    decision: str
    ledger: dict[str, Any]
    retry_queries: tuple[str, ...] = ()


def candidate_asset_id(candidate: Candidate) -> str:
    return f"{candidate.source}:{candidate.source_id}"


def official_image_candidate(
    *,
    image_url: str,
    attribution: str = "",
    title: str = "",
    source_url: str = "",
) -> Candidate:
    """Build a standard ``Candidate`` for an article's official share image.

    The image comes from the publisher page (og:image) and is treated as
    press/editorial material: it must keep its attribution and must not be
    repurposed commercially.  Dimensions are unknown until download, so the
    director must not apply the stock width/height floor to this candidate.
    """
    return Candidate(
        source="official_press",
        source_id=hashlib.sha256(image_url.encode("utf-8")).hexdigest()[:16],
        source_url=source_url or image_url,
        download_url=image_url,
        kind="image",
        width=0,
        height=0,
        duration=0.0,
        creator=attribution,
        license="press",
        source_tags=f"{title} 官方配图".strip(),
        thumbnail_url=image_url,
        extra={"official": True, "attribution": attribution},
    )


def prepare_candidates(
    candidates: Iterable[Candidate],
    *,
    media_kind: str,
    orientation: str,
    minimum_duration: float,
    used_provider_ids: set[str],
) -> list[Candidate]:
    """Apply deterministic pre-download gates and return stable order."""
    accepted: list[Candidate] = []
    seen: set[str] = set()
    for candidate in candidates:
        asset_id = candidate_asset_id(candidate)
        if asset_id in seen or candidate.source_id in used_provider_ids:
            continue
        if candidate.kind != media_kind or not candidate.download_url:
            continue
        if candidate.width < 480 or candidate.height < 480:
            continue
        if media_kind == "video" and candidate.duration + 0.001 < minimum_duration:
            continue
        if orientation == "portrait" and candidate.height < candidate.width:
            continue
        if orientation == "landscape" and candidate.width < candidate.height:
            continue
        seen.add(asset_id)
        accepted.append(candidate)
    return sorted(accepted, key=lambda item: (candidate_asset_id(item), -item.width, -item.height))


def _words(value: str) -> set[str]:
    return {
        word.lower() for word in re.findall(r"[a-zA-Z][a-zA-Z0-9-]{1,}", value or "")
        if word.lower() not in {"the", "and", "with", "for", "from", "video", "footage", "shot"}
    }


def _candidate_summary(candidate: Candidate) -> dict[str, Any]:
    preview_frames = candidate.extra.get("preview_frames") if isinstance(candidate.extra, dict) else []
    return {
        "asset_id": candidate_asset_id(candidate),
        "kind": candidate.kind,
        "width": candidate.width,
        "height": candidate.height,
        "duration_seconds": round(float(candidate.duration), 3),
        "source_tags": str(candidate.source_tags or "")[:360],
        "thumbnail_url": str(candidate.thumbnail_url or ""),
        "preview_frame_count": len(preview_frames) if isinstance(preview_frames, list) else 0,
        "creator": str(candidate.creator or "")[:120],
    }


def _safe_score(raw: Any, field: str) -> float:
    try:
        score = float(raw.get(field))
    except (AttributeError, TypeError, ValueError):
        raise ValueError(f"{field} 不是有效数字") from None
    if not 0 <= score <= 100:
        raise ValueError(f"{field} 必须在 0 到 100 之间")
    return round(score, 2)


def _safe_confidence(raw: Any) -> float:
    try:
        confidence = float(raw.get("confidence"))
    except (AttributeError, TypeError, ValueError):
        raise ValueError("confidence 不是有效数字") from None
    if not 0 <= confidence <= 1:
        raise ValueError("confidence 必须在 0 到 1 之间")
    return round(confidence, 3)


def _clean_retry_queries(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    queries: list[str] = []
    for entry in value:
        text = re.sub(r"\s+", " ", str(entry or "")).strip()[:120]
        if text and re.fullmatch(r"[A-Za-z0-9 ,&'/-]+", text) and text not in queries:
            queries.append(text)
        if len(queries) == 2:
            break
    return tuple(queries)


def _rule_decision(slot: dict[str, Any], candidates: list[Candidate], reason: str) -> DirectorDecision:
    """Safe deterministic fallback for an unavailable/invalid project model."""
    query_words = _words(" ".join(str(slot.get(key) or "") for key in ("query", "visual_intent", "context_text")))
    scored: list[tuple[float, Candidate]] = []
    for candidate in candidates:
        tags = _words(candidate.source_tags)
        overlap = len(query_words & tags)
        semantic = min(88.0, 62.0 + overlap * 9.0)
        technical = min(96.0, 70.0 + min(candidate.width, candidate.height) / 80.0)
        score = semantic * .7 + technical * .3
        scored.append((score, candidate))
    if not scored:
        return DirectorDecision(None, "fallback", {
            "director_version": DIRECTOR_VERSION,
            "prompt_version": VISUAL_CANDIDATE_DIRECTOR_PROMPT_VERSION,
            "decision_source": "deterministic_fallback",
            "decision": "fallback", "reason": reason[:240], "candidate_count": 0,
        })
    scored.sort(key=lambda row: (-row[0], candidate_asset_id(row[1])))
    score, candidate = scored[0]
    semantic = round(min(88.0, 62.0 + len(query_words & _words(candidate.source_tags)) * 9.0), 2)
    accepted = semantic >= MIN_SEMANTIC_SCORE
    ledger = {
        "director_version": DIRECTOR_VERSION,
        "prompt_version": VISUAL_CANDIDATE_DIRECTOR_PROMPT_VERSION,
        "decision_source": "deterministic_fallback",
        "model": None,
        "decision": "accept" if accepted else "retry",
        "selected_asset_id": candidate_asset_id(candidate) if accepted else None,
        "semantic_score": semantic,
        "aesthetic_score": 70.0,
        "continuity_score": 75.0,
        "technical_score": round(min(96.0, 70.0 + min(candidate.width, candidate.height) / 80.0), 2),
        "confidence": 0.7 if accepted else 0.45,
        "weighted_score": round(score, 2),
        "reason": reason[:240],
        "candidate_count": len(candidates),
    }
    retry = (f"{str(slot.get('query') or '').strip()} technology detail".strip(),) if not accepted else ()
    return DirectorDecision(candidate if accepted else None, ledger["decision"], ledger, retry)


def decide_candidate(
    slot: dict[str, Any],
    candidates: list[Candidate],
    *,
    evaluator: Callable[[dict[str, Any]], tuple[dict[str, Any], str]] = evaluate_visual_candidates,
) -> DirectorDecision:
    """Choose only from this search round's candidates and produce an audit ledger."""
    public_candidates = [_candidate_summary(item) for item in candidates]
    candidate_by_id = {candidate_asset_id(item): item for item in candidates}
    payload = {
        "director_version": DIRECTOR_VERSION,
        "prompt_version": VISUAL_CANDIDATE_DIRECTOR_PROMPT_VERSION,
        "slot": {
            "scene_id": str(slot.get("scene_id") or ""), "block_id": str(slot.get("block_id") or ""),
            "start_seconds": slot.get("start_seconds"), "end_seconds": slot.get("end_seconds"),
            "slot_text": str(slot.get("slot_text") or "")[:800],
            "context_text": str(slot.get("context_text") or "")[:1200],
            "visual_intent": str(slot.get("visual_intent") or "")[:200],
            "query": str(slot.get("query") or "")[:160],
            "search_role": str(slot.get("search_role") or ""),
            "recently_used_asset_ids": list(slot.get("recently_used_asset_ids") or [])[:24],
        },
        "candidates": public_candidates,
    }
    fingerprint = hashlib.sha256(repr(payload).encode("utf-8")).hexdigest()[:20]
    if not candidates:
        return _rule_decision(slot, [], "候选硬过滤后为空")
    try:
        raw, model = evaluator(payload)
        decision = str(raw.get("decision") or "").strip().lower()
        if decision not in {"accept", "retry", "fallback"}:
            raise ValueError("decision 不合法")
        scores = {field: _safe_score(raw, field) for field in SCORE_WEIGHTS}
        confidence = _safe_confidence(raw)
        selected_id = str(raw.get("selected_asset_id") or "").strip()
        selected = candidate_by_id.get(selected_id)
        if decision == "accept" and selected is None:
            raise ValueError("模型选择了本轮候选以外的素材")
        weighted = round(sum(scores[field] * weight for field, weight in SCORE_WEIGHTS.items()), 2)
        accepted = decision == "accept" and scores["semantic_score"] >= MIN_SEMANTIC_SCORE and confidence >= MIN_CONFIDENCE
        if decision == "accept" and not accepted:
            decision = "retry"
            selected = None
        ledger = {
            "director_version": DIRECTOR_VERSION,
            "prompt_version": VISUAL_CANDIDATE_DIRECTOR_PROMPT_VERSION,
            "decision_source": "project_text_model",
            "model": model,
            "decision": decision,
            "selected_asset_id": candidate_asset_id(selected) if selected else None,
            **scores, "confidence": confidence, "weighted_score": weighted,
            "reason": re.sub(r"\s+", " ", str(raw.get("reason") or "")).strip()[:240],
            "candidate_count": len(candidates), "fingerprint": fingerprint,
        }
        return DirectorDecision(selected, decision, ledger, _clean_retry_queries(raw.get("retry_queries")))
    except (TextAIError, ValueError, TypeError) as exc:
        return _rule_decision(slot, candidates, f"项目内视觉导演不可用，已启用确定性安全回退：{exc}")
