"""Explainable first-pass ranking for outdoor interaction events.

The four scored criteria are the product's own words, and the weights ship in the
order the user asked for — **时长 > 老外 > 情绪 > 互动/歌舞**:

===========================  =======
criterion                    weight
===========================  =======
``duration`` (时间长)         0.40
``foreign_speech`` (老外)     0.25
``high_emotion`` (情绪)       0.20
``performance`` (互动/歌舞)    0.15
===========================  =======

"同一主体" is **not** part of the composite any more: it is a *selection
requirement* — how a material was chosen at all — and lives in
``requirements.same_subject`` with its own pass/fail verdict.  Mixing it into the
score meant a clip could buy its way past "one subject, enough time" with
emotion, which is not what the requirement means.

Everything here is derived from evidence the vision pass already paid for —
``summary``, ``participants``, ``highlights``, ``quality`` and the ASR
utterances.  The module therefore only *reads* the index and writes a side-car:
the index signature can never change, so the paid analysis cache stays valid and
no new model call is ever triggered.  Tuning these weights is free and instant —
including from the browser, which re-asks for a different ``order_mode`` /
``weights`` instead of reimplementing the ranking in JavaScript.

Two honest limits, measured on the 88.9-minute sample rather than assumed:

* that material yields 27 groups for 28 events, so "same subject" mostly
  expresses *how long one subject was engaged*, not cross-window regrouping;
* it contains no singing or dancing at all, so criterion 4 fires through its
  own fallback clause ("or a strong interaction beat": 合影 / 比心 / 握手).
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any, Iterable

VERSION = "material-interaction-recommend-v2"

# Weights add up to 1 and ship in the user's requested order: how long the clip
# is matters most, then a foreign-language exchange, then the emotional beat,
# then singing/dancing/strong interaction.
WEIGHTS = {"duration": 0.40, "foreign_speech": 0.25, "high_emotion": 0.20, "performance": 0.15}

SCORED_FACTORS = ("duration", "foreign_speech", "high_emotion", "performance")

CRITERIA = {
    "duration": "互动时长充分（按同一主体累计时长计分）优先",
    "foreign_speech": "讲外语的互动优先",
    "high_emotion": "惊讶 / 惊喜 / 惊吓等高情绪互动优先",
    "performance": "唱歌跳舞或互动效果好的素材优先",
}
REQUIREMENT_CRITERION = "同一互动主体、完整且时长充分的互动（选材要求，不计入综合分）"
# A subject that is engaged long enough and whose encounter is not truncated
# clears the requirement.  It is deliberately lenient: the requirement exists to
# keep a stray 3-second cameo out, not to re-rank good material.
REQUIREMENT_THRESHOLD = 0.5

# --- ordering modes ----------------------------------------------------------
# The product's four switches.  `duration_desc` is the default because a long,
# complete interaction is the safest thing to build a short video from.
ORDER_MODES: dict[str, dict[str, Any]] = {
    "duration_desc": {"label": "优先时间长", "factor": "duration_seconds", "descending": True},
    "duration_asc": {"label": "优先时间短", "factor": "duration_seconds", "descending": False},
    "score_desc": {"label": "优先综合分", "factor": "recommendation_score", "descending": True},
    "emotion_desc": {"label": "优先情绪值", "factor": "emotion", "descending": True},
    # Not one of the four ranking switches: this is the review order, kept so the
    # panel can still walk the events in the order they happened on the tape.
    "source_asc": {"label": "按原片时间顺序", "factor": "start", "descending": False},
}
ORDER_MODE_DEFAULT = "duration_desc"

# A single subject engaged for this long scores a full mark on duration.
SUBJECT_DURATION_PIVOT = 300.0
# A third of the characters being Latin is already plainly a foreign exchange.
FOREIGN_PIVOT = 0.30
EMOTION_HIT_PIVOT = 2
PERFORMANCE_HIT_PIVOT = 2

CJK = re.compile(r"[\u4e00-\u9fff]")
LATIN = re.compile(r"[A-Za-z]")

EMOTION_TERMS = (
    "哇", "哎哟", "天呐", "天哪", "不会吧", "真的假的", "吓", "惊", "太厉害", "好厉害", "太酷", "好酷",
    "太可爱", "好可爱", "绝了", "笑死", "太棒", "牛啊",
    "omg", "oh my god", "wow", "amazing", "unbelievable", "no way", "crazy",
)
# Criterion 4 is "singing, dancing, or a strong interaction beat": the second
# half matters here because this material has no singing or dancing in it.
# Terms are kept mutually non-overlapping on purpose — "合唱" would also match
# "唱" and quietly double-count one utterance.
PERFORMANCE_TERMS = (
    "唱", "歌", "跳", "舞", "表演", "才艺", "乐器", "弹",
    "song", "sing", "dance", "music",
    "合影", "合照", "拍照", "比心", "拥抱", "握手", "击掌", "自拍", "互动",
)
FOREIGN_TERMS = (
    "hello", "thank", "sorry", "excuse", "welcome", "beautiful", "amazing",
    "where are you from", "nice to meet", "oh my god", "this is",
)


class InteractionRecommendError(ValueError):
    pass


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _text(value: Any) -> str:
    return str(value or "").strip()


def _trigrams(value: str) -> set[str]:
    compact = re.sub(r"\s+", "", value)
    if len(compact) < 3:
        return {compact} if compact else set()
    return {compact[index:index + 3] for index in range(len(compact) - 2)}


def participants_similarity(first: str, second: str) -> float:
    """Trigram Jaccard over the free-text subject descriptions.

    The vision pass describes a subject by clothing and position, so two
    descriptions of the same person share most of their wording while two
    different people share almost none.
    """
    left, right = _trigrams(first), _trigrams(second)
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def latin_ratio(value: str) -> float:
    latin = len(LATIN.findall(value))
    cjk = len(CJK.findall(value))
    return latin / max(1, latin + cjk)


def _hits(value: str, terms: Iterable[str]) -> list[str]:
    """Which terms appear.

    ASCII terms match on a *leading* word boundary only, so ``sing`` still finds
    "singing" while ``hi`` can no longer fire inside "this" — a trap that made an
    earlier draft score ordinary Chinese small-talk as foreign speech.
    """
    lowered = value.lower()
    found = []
    for term in terms:
        needle = term.lower()
        if not needle:
            continue
        if needle.isascii() and needle[0].isalnum():
            if re.search(r"(?<![a-z0-9])" + re.escape(needle), lowered):
                found.append(term)
        elif needle in lowered:
            found.append(term)
    return found


def _utterance_index(index: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rows = (index.get("audio") or {}).get("utterances") or []
    return {_text(row.get("id")): row for row in rows if isinstance(row, dict) and _text(row.get("id"))}


def _event_text(event: dict[str, Any], utterances: dict[str, dict[str, Any]]) -> str:
    texts = [_text(utterances[uid].get("text")) for uid in event.get("utterance_ids") or [] if uid in utterances]
    return " ".join(text for text in texts if text)


def _group_summaries(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        grouped.setdefault(_text(event.get("group_id")) or "未分组", []).append(event)
    summaries: dict[str, dict[str, Any]] = {}
    for group_id, rows in grouped.items():
        participants = [_text(row.get("participants")) for row in rows]
        scores = [participants_similarity(participants[0], other) for other in participants[1:]]
        summaries[group_id] = {
            "group_id": group_id,
            "event_ids": [str(row.get("event_id")) for row in rows],
            "event_count": len(rows),
            "total_seconds": round(sum(float(row.get("end") or 0) - float(row.get("start") or 0)
                                       for row in rows), 3),
            "participants_consistency": round(1.0 if not scores else sum(scores) / len(scores), 3),
        }
    return summaries


def _completeness_score(event: dict[str, Any]) -> float:
    return {"complete": 1.0, "partial": 0.6}.get(_text(event.get("completeness")), 0.3)


def _duration_score(seconds: float) -> float:
    """Log-shaped on purpose: 12s and 30s must not read as the same clip."""
    return min(1.0, math.log1p(max(0.0, seconds)) / math.log1p(SUBJECT_DURATION_PIVOT))


def _event_factors(event: dict[str, Any], group: dict[str, Any], text: str,
                   ) -> tuple[dict[str, float], dict[str, Any], dict[str, Any], list[str], str]:
    quality = event.get("quality") if isinstance(event.get("quality"), dict) else {}
    engagement = float(quality.get("engagement") or 0.0)
    duration = float(event.get("end") or 0) - float(event.get("start") or 0)
    consistency = float(group.get("participants_consistency") or 1.0)
    group_seconds = float(group.get("total_seconds") or duration)
    completeness = _completeness_score(event)

    duration_component = _duration_score(group_seconds)
    # `duration` is now its own criterion and carries the heaviest weight, so it
    # must not be folded into "same subject" any more.  It reads the *group*
    # total: the product criterion is "how long one subject stayed engaged",
    # not how long one window happens to be.
    duration_factor = round(duration_component, 3)

    emotion_terms = _hits(text, EMOTION_TERMS)
    emotion_hits = min(1.0, len(emotion_terms) / EMOTION_HIT_PIVOT)
    highlight_text = " ".join(_text(row.get("label")) for row in event.get("highlights") or []
                             if isinstance(row, dict))
    highlight_hits = min(1.0, len(_hits(highlight_text, EMOTION_TERMS + ("情绪", "表情", "笑脸", "喊"))) / 1.0)
    high_emotion = round(min(1.0, 0.45 * emotion_hits + 0.35 * engagement + 0.20 * highlight_hits), 3)

    ratio = latin_ratio(text)
    foreign_terms = _hits(text, FOREIGN_TERMS)
    foreign_speech = round(min(1.0, ratio / FOREIGN_PIVOT + (0.2 if foreign_terms else 0.0)), 3)

    performance_terms = _hits(text, PERFORMANCE_TERMS)
    performance_hits = min(1.0, len(performance_terms) / PERFORMANCE_HIT_PIVOT)
    performance = round(min(1.0, 0.6 * performance_hits + 0.4 * engagement), 3)

    factors = {"duration": duration_factor, "foreign_speech": foreign_speech,
               "high_emotion": high_emotion, "performance": performance}
    # The selection requirement: same subject, engaged long enough, encounter not
    # truncated.  It is reported, never scored.
    subject_score = round(0.25 * consistency + 0.55 * duration_component + 0.20 * completeness, 3)
    requirement = {
        "key": "same_subject",
        "label": "同一主体、时长充分",
        "ok": subject_score >= REQUIREMENT_THRESHOLD,
        "score": subject_score,
        "threshold": REQUIREMENT_THRESHOLD,
        "evidence": {
            "group_id": group.get("group_id"), "group_event_count": group.get("event_count"),
            "group_total_seconds": round(group_seconds, 3), "event_seconds": round(duration, 3),
            "participants_consistency": consistency, "completeness": _text(event.get("completeness")),
        },
    }
    evidence = {
        "group_id": group.get("group_id"), "group_event_count": group.get("event_count"),
        "group_total_seconds": round(group_seconds, 3), "event_seconds": round(duration, 3),
        "participants_consistency": consistency, "completeness": _text(event.get("completeness")),
        "engagement": round(engagement, 3), "emotion_terms": emotion_terms,
        "foreign_ratio": round(ratio, 4), "foreign_terms": foreign_terms,
        "performance_terms": performance_terms,
    }
    reasons = [
        f"【时间长度】本段 {duration:.0f} 秒，同一互动对象累计 {group_seconds:.0f} 秒"
        f"（计分 {duration_factor:.2f}，满分对应 {SUBJECT_DURATION_PIVOT:.0f} 秒）",
        "【高情绪】" + ("、".join(f"“{term}”" for term in emotion_terms[:5]) if emotion_terms else "对白未命中高情绪词")
        + f"；模型互动度 {engagement:.2f}",
        "【讲外语】" + (f"拉丁字符占比 {ratio * 100:.1f}%" + ("，命中 " + "、".join(f"“{t}”" for t in foreign_terms[:4])
                                                           if foreign_terms else "")
                        if ratio >= .02 else "几乎全为中文"),
        "【歌舞/互动效果】" + ("、".join(f"“{term}”" for term in performance_terms[:5])
                              if performance_terms else "未命中歌舞或互动动作词")
        + f"；模型互动度 {engagement:.2f}",
    ]
    requirement_reason = (
        f"【选材要求】同一主体 {group.get('group_id')} 共 {group.get('event_count')} 段、合计 "
        f"{group_seconds:.0f} 秒，主体描述一致度 {consistency:.2f}，完整性 "
        f"{_text(event.get('completeness')) or '未知'} → "
        + ("满足" if requirement["ok"] else "未满足")
    )
    return factors, evidence, requirement, reasons, requirement_reason


def resolve_order_mode(value: Any) -> str:
    mode = str(value or ORDER_MODE_DEFAULT)
    if mode not in ORDER_MODES:
        raise InteractionRecommendError("排序方式无效，请选择优先时间长、优先时间短、优先综合分或优先情绪值")
    return mode


def rank_events(rows: list[dict[str, Any]], *, sort_mode: Any = ORDER_MODE_DEFAULT) -> list[dict[str, Any]]:
    """Return the rows in one order mode, stamping ``rank`` on them.

    The tie-breakers are fixed so the ranking is deterministic and a second
    criterion never has to break a tie by event id alone: the composite score
    first, then the vision pass's own baseline, then the id.  The returned list
    is what callers must publish — ranking a copy and shipping the original
    would give every row a rank from one ordering and a position from another.
    """
    mode = resolve_order_mode(sort_mode)
    spec = ORDER_MODES[mode]

    def key(row: dict[str, Any]) -> tuple:
        if spec["factor"] == "emotion":
            primary = float(row["factors"]["high_emotion"])
        else:
            primary = float(row[spec["factor"]])
        if not spec["descending"]:
            return (primary, -float(row["recommendation_score"]), str(row["event_id"]))
        return (-primary, -float(row["recommendation_score"]), str(row["event_id"]))

    ranked = sorted(rows, key=key)
    for rank, row in enumerate(ranked, 1):
        row["rank"] = rank
    return ranked


def normalize_weights(weights: dict[str, float] | None) -> dict[str, float]:
    """Read the four scored weights, ignoring the retired ``same_subject`` key.

    Older callers passed ``same_subject`` as a fifth weight.  It is accepted for
    compatibility and dropped, because the requirement is no longer a score.
    """
    raw = weights if isinstance(weights, dict) else {}
    configured: dict[str, float] = {}
    for key in SCORED_FACTORS:
        value = raw.get(key, WEIGHTS[key])
        try:
            configured[key] = float(value)
        except (TypeError, ValueError) as exc:
            raise InteractionRecommendError("推荐权重必须是数值") from exc
        if not math.isfinite(configured[key]) or configured[key] < 0:
            raise InteractionRecommendError("推荐权重不能为负数")
    total = sum(configured.values())
    if total <= 0:
        raise InteractionRecommendError("推荐权重之和必须大于零")
    return {key: value / total for key, value in configured.items()}


def build_recommendations(index: dict[str, Any], *, weights: dict[str, float] | None = None,
                          sort_mode: Any = ORDER_MODE_DEFAULT) -> dict[str, Any]:
    """Rank the events of one interaction index by the four scored criteria."""
    mode = resolve_order_mode(sort_mode)
    configured = normalize_weights(weights)
    events = [row for row in index.get("events") or [] if isinstance(row, dict)]
    if not events:
        raise InteractionRecommendError("互动索引里没有可排序的事件")
    utterances = _utterance_index(index)
    groups = _group_summaries(events)
    rows = []
    for event in events:
        group = groups.get(_text(event.get("group_id")) or "未分组", {
            "group_id": _text(event.get("group_id")), "event_ids": [str(event.get("event_id"))],
            "event_count": 1, "total_seconds": round(float(event.get("end") or 0) - float(event.get("start") or 0), 3),
            "participants_consistency": 1.0,
        })
        text = _event_text(event, utterances)
        factors, evidence, requirement, reasons, requirement_reason = _event_factors(event, group, text)
        score = round(sum(configured[key] * factors[key] for key in configured), 4)
        rows.append({
            "event_id": str(event.get("event_id")), "group_id": _text(event.get("group_id")),
            "start": round(float(event.get("start") or 0), 3), "end": round(float(event.get("end") or 0), 3),
            "duration_seconds": round(float(event.get("end") or 0) - float(event.get("start") or 0), 3),
            "baseline_score": float(event.get("score") or 0.0),
            "recommendation_score": score, "factors": factors, "evidence": evidence,
            "requirement": requirement, "reasons": reasons, "requirement_reason": requirement_reason,
            "summary": _text(event.get("summary"))[:200],
        })
    ranked = rank_events(rows, sort_mode=mode)
    notes = []
    if max(row["factors"]["foreign_speech"] for row in rows) < 0.3:
        notes.append("本次素材没有明显的讲外语互动，外语因子整体未参与区分")
    if max(row["factors"]["performance"] for row in rows) < 0.5:
        notes.append("本次素材没有唱歌跳舞，互动效果因子按“合影/比心”等动作计分")
    unmet = [row["event_id"] for row in rows if not row["requirement"]["ok"]]
    if unmet:
        notes.append(f"有 {len(unmet)} 条素材未满足“同一主体、时长充分”的选材要求")
    payload = {
        "version": VERSION, "index_signature": str(index.get("signature") or ""),
        "source_fingerprint": str((index.get("source") or {}).get("fingerprint") or ""),
        "weights": configured, "criteria": dict(CRITERIA),
        "requirement_criterion": REQUIREMENT_CRITERION,
        "requirement_threshold": REQUIREMENT_THRESHOLD,
        "order_mode": mode, "order_mode_label": ORDER_MODES[mode]["label"],
        "order_modes": {key: value["label"] for key, value in ORDER_MODES.items()},
        "events": ranked,
        "groups": sorted(groups.values(), key=lambda row: -row["total_seconds"]),
        "notes": notes,
    }
    payload["signature"] = _digest({key: value for key, value in payload.items() if key != "signature"})
    return payload


def read_recommendations(path: Path) -> dict[str, Any] | None:
    if not Path(path).is_file():
        return None
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def write_recommendations(path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
        for attempt in range(12):
            try:
                os.replace(temporary, path)
                return payload
            except PermissionError:
                if attempt == 11:
                    raise
                time.sleep(.01 * (attempt + 1))
    finally:
        temporary.unlink(missing_ok=True)
    return payload


__all__ = [
    "CRITERIA", "InteractionRecommendError", "ORDER_MODES", "ORDER_MODE_DEFAULT",
    "REQUIREMENT_CRITERION", "REQUIREMENT_THRESHOLD", "SCORED_FACTORS", "VERSION", "WEIGHTS",
    "build_recommendations", "latin_ratio", "normalize_weights", "participants_similarity",
    "rank_events", "read_recommendations", "resolve_order_mode", "write_recommendations",
]
