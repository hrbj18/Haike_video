"""Phrase-level speech units: the atoms an interaction edit can actually trade.

Why this layer exists
---------------------
Tencent ASR returns **sentence-level, and in practice 60-second-level**
timestamps for a long file: the acceptance sample's six utterances are
60.26 / 27.40 / 60.48 / 60.04 / 60.02 / 46.45 seconds long.  Both first-pass and
second-pass edits therefore had exactly one granularity to work with, and the
consequences were measured rather than theorised:

* a whole 27.4-second block was dropped as "low information density", and the
  preceding 48 seconds were dropped *without ever reaching the model* because the
  utterance containing them was not fully inside the parent candidate's range;
* a 3—5 second hook was impossible to construct, so "精彩前置" stayed at 0.0 s;
* the material's own dialogue gaps (mostly 0.45—0.60 s) sat below the plan
  layer's effective threshold and were never compressed.

The fix is not to loosen any safety rule but to **recover the granularity that
is already paid for**: the VAD pass produced ~50 speech ranges for the same
material, i.e. roughly one run of speech per phrase.  This module turns
(ASR phrases × VAD speech ranges) into units whose **boundaries are always real
VAD gaps**, so:

1. no cut point can ever fall inside a spoken word — a structural guarantee, not
   a probability;
2. a unit is 2—6 seconds, so a hook and a fine-grained keep/drop both become
   expressible;
3. "keep from 你好 to 拜拜" becomes a deterministic lookup instead of a prayer.

Discipline
----------
Everything here is a pure function of evidence that was already paid for
(``audio.utterances`` + ``speech_ranges`` + the parent's allowed ranges).  It
never calls a model, never writes to the index, and never changes any signature
that gates a paid call.  When the VAD evidence is missing the layer degrades to
the previous behaviour and says so.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

VERSION = "material-interaction-units-v1"

# A unit shorter than this is a VAD blip (breath, lip smack, single syllable).
# It is absorbed into a neighbour instead of becoming a group nobody can use.
MIN_UNIT_SECONDS = 0.35
# Two speech runs separated by less than this are *the same* run of talking as
# far as an editor is concerned: the gap is too short to cut and cutting there
# would sound like a stutter.  Merging first is what makes every remaining
# boundary a real, cuttable gap.
MERGE_GAP_SECONDS = 0.22
# Ignore microscopic speech blips outright so they cannot seed a block.
MIN_SPEECH_SECONDS = 0.08

DEFAULT_MAX_CHARS = 16

# ---------------------------------------------------------------- lexicons --
# Edge anchoring uses explicit word lists, not the model.  The product rule is
# "从明显的词开始/结束" — a *deterministic* lookup is auditable and cannot drift
# with a model upgrade.  Matching is order-preserving over units, so the first
# hit is the opening and the last hit is the closing.
GREETING_LEXICON = (
    "你好", "您好", "你们好", "大家好", "各位好", "姐姐好",
    "哈喽", "哈啰", "嗨", "喂", "早上好", "晚上好", "下午好",
    "hello", "hi", "good morning", "good afternoon", "good evening",
)
FAREWELL_LEXICON = (
    "拜拜", "再见", "拜拜啦", "下次见", "下次再来", "回头见", "再会", "走啦",
    "bye", "bye bye", "goodbye", "see you",
)
# A greeting counts as the clip's opening only when it is (nearly) the whole
# phrase.  Tencent ASR separates phrases with spaces, so "你好" inside
# "你好聪明" ("you are so clever") is a *different word*: matching it as an
# opening deleted the first 32 seconds of a 150-second encounter during the
# acceptance run — worse than not anchoring at all.
_EDGE_PARTICLES = ("呀", "啊", "嘛", "啦", "哦", "喔", "噢", "呢", "吧", "了", "哟", "喂", "的", "哈")
_MAX_PARTICLE_SUFFIX = 3

_SPACE_RE = re.compile(r"[ \t\u3000]+")
# What separates one ASR phrase from the next.  The edge lexicons are matched
# against these tokens rather than against raw substrings.
_TOKEN_SPLIT_RE = re.compile(r"[ \t\u3000，。！？；、,.!?;：:]+")
_PUNCTUATION = "。！？；，、"
# Split *after* each closing punctuation mark so nothing is dropped: a leading
# standalone mark still lands in its own piece instead of being skipped.
_PUNCTUATION_SPLIT_RE = re.compile("(?<=[" + re.escape(_PUNCTUATION) + "])")


class InteractionUnitsError(ValueError):
    pass


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _text(value: Any) -> str:
    return str(value or "").strip()


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result


# ------------------------------------------------------------------ phrases --

def _explode_token(token: str, *, max_chars: int) -> list[str]:
    """Break a whitespace-free token down to phrases no longer than ``max_chars``."""
    if len(token) <= max_chars:
        return [token]
    pieces = [piece for piece in _PUNCTUATION_SPLIT_RE.split(token) if piece]
    if len(pieces) <= 1:
        # No usable punctuation: fall back to a fixed-width hard split so a
        # 198-character monologue still becomes readable lines.
        return [token[index:index + max_chars] for index in range(0, len(token), max_chars)]
    result: list[str] = []
    for piece in pieces:
        if len(piece) <= max_chars:
            result.append(piece)
        else:
            result.extend(piece[index:index + max_chars] for index in range(0, len(piece), max_chars))
    return result


def split_phrases(text: str, *, max_chars: int = DEFAULT_MAX_CHARS) -> list[str]:
    """Split an utterance into caption-sized phrases, coarsest break point first.

    Tencent ASR separates spoken phrases with spaces, so that is the natural
    first cut; punctuation and a fixed width only matter for a monologue the ASR
    returned as one unbroken run.  Characters are never dropped.
    """
    phrases: list[str] = []
    for token in _SPACE_RE.split(str(text or "").strip()):
        if not token:
            continue
        phrases.extend(_explode_token(token, max_chars=max_chars))
    return [phrase for phrase in phrases if phrase.strip()]


def phrase_windows(utterance: dict[str, Any] | None, *,
                   max_chars: int = DEFAULT_MAX_CHARS) -> list[dict[str, Any]]:
    """Estimated **source-timeline** windows for one utterance's phrases.

    Identical in behaviour to the second-pass renderer's helper (it now lives
    here so both layers share one implementation): a phrase's time is a
    character-proportional estimate over the utterance's own span, because there
    is no word-level timing to borrow.  It is an estimate — this module never
    uses it as a cut point; it only uses it to decide *which* VAD block a phrase
    belongs to.
    """
    if not isinstance(utterance, dict):
        return []
    start, end = _number(utterance.get("start")), _number(utterance.get("end"))
    if start is None or end is None:
        return []
    text = str(utterance.get("text") or "")
    span = end - start
    if span <= 0 or not text.strip():
        return []
    phrases = split_phrases(text, max_chars=max_chars)
    total = sum(len(piece) for piece in phrases)
    if not phrases or total <= 0:
        return []
    windows: list[dict[str, Any]] = []
    consumed = 0
    for piece in phrases:
        window_start = start + span * (consumed / total)
        consumed += len(piece)
        window_end = start + span * (consumed / total)
        windows.append({"text": piece, "source_start": window_start, "source_end": window_end})
    return windows


# -------------------------------------------------------------- speech runs --

def normalize_speech_ranges(rows: Any) -> list[dict[str, float]]:
    """Read-only normalisation: sorted, overlap-merged, degenerate rows dropped."""
    parsed: list[dict[str, float]] = []
    for raw in rows if isinstance(rows, list) else []:
        if not isinstance(raw, dict):
            continue
        start, end = _number(raw.get("start")), _number(raw.get("end"))
        if start is None or end is None or end - start < MIN_SPEECH_SECONDS:
            continue
        parsed.append({"start": start, "end": end})
    parsed.sort(key=lambda row: (row["start"], row["end"]))
    merged: list[dict[str, float]] = []
    for row in parsed:
        if merged and row["start"] <= merged[-1]["end"] + 1e-9:
            merged[-1]["end"] = max(merged[-1]["end"], row["end"])
            continue
        merged.append(dict(row))
    return merged


def _clip(rows: list[dict[str, float]], allowed: list[dict[str, float]]) -> list[dict[str, float]]:
    """Clip speech runs to the parent-approved ranges, keeping source order."""
    result: list[dict[str, float]] = []
    for window in allowed:
        for row in rows:
            start = max(row["start"], window["start"])
            end = min(row["end"], window["end"])
            if end - start < MIN_SPEECH_SECONDS:
                continue
            result.append({"start": round(start, 6), "end": round(end, 6)})
    result.sort(key=lambda row: (row["start"], row["end"]))
    return result


def speech_blocks(speech_ranges: Any, allowed: Any, *,
                  merge_gap: float = MERGE_GAP_SECONDS,
                  min_seconds: float = MIN_UNIT_SECONDS) -> list[dict[str, float]]:
    """Maximal runs of speech separated by gaps an editor would accept cutting.

    Two runs closer than ``merge_gap`` become one block, and any block still
    shorter than ``min_seconds`` is absorbed into a neighbour.  The result is
    therefore: **every boundary between two blocks is a real gap, and every block
    is long enough to be worth naming.**
    """
    windows = [row for row in (allowed or []) if isinstance(row, dict)]
    normalized_allowed = []
    for row in windows:
        start, end = _number(row.get("start")), _number(row.get("end"))
        if start is None or end is None or end <= start:
            continue
        normalized_allowed.append({"start": start, "end": end})
    normalized_allowed.sort(key=lambda row: row["start"])
    if not normalized_allowed:
        return []
    runs = _clip(normalize_speech_ranges(speech_ranges), normalized_allowed)
    blocks: list[dict[str, float]] = []
    for row in runs:
        if blocks and row["start"] - blocks[-1]["end"] <= merge_gap:
            blocks[-1]["end"] = max(blocks[-1]["end"], row["end"])
            continue
        blocks.append(dict(row))
    # Absorb survivors that are still too short.  Each pass removes one block, so
    # the loop terminates; preferring the previous neighbour keeps the absorbing
    # unit's start stable, which is what the head anchor reads.
    while len(blocks) > 1:
        short = next((index for index, row in enumerate(blocks)
                      if row["end"] - row["start"] < min_seconds), None)
        if short is None:
            break
        target = short - 1 if short > 0 else short + 1
        blocks[target]["start"] = round(min(blocks[target]["start"], blocks[short]["start"]), 6)
        blocks[target]["end"] = round(max(blocks[target]["end"], blocks[short]["end"]), 6)
        blocks.pop(short)
    return blocks


# --------------------------------------------------------------- the units --

def _unit_id(index: int) -> str:
    return f"P{index:04d}"


def build_spoken_units(utterances: Any, speech_ranges: Any, *, allowed: Any,
                       max_chars: int = DEFAULT_MAX_CHARS,
                       merge_gap: float = MERGE_GAP_SECONDS,
                       min_seconds: float = MIN_UNIT_SECONDS,
                       ) -> tuple[list[dict[str, Any]], list[str]]:
    """Phrase-level units for one interaction, plus the reasons any were lost.

    Returns ``(units, degradations)``.  ``units`` is empty only when there is no
    usable input at all; the caller decides what to fall back to.  Each unit is
    ``{id, start, end, text, utterance_id, duration_seconds}`` and ``text`` may be
    empty (a run of speech the ASR text did not reach) — an empty-text unit is
    still real speech and must never be dropped by a model that cannot read it.
    """
    degradations: list[str] = []
    blocks = speech_blocks(speech_ranges, allowed, merge_gap=merge_gap, min_seconds=min_seconds)
    if not blocks:
        return [], ["spoken_units_unavailable:缺少语音活动证据，已退回分句级取舍"]

    lower = min(float(row["start"]) for row in blocks)
    upper = max(float(row["end"]) for row in blocks)
    ordered_utterances = []
    for raw in utterances if isinstance(utterances, list) else []:
        if not isinstance(raw, dict):
            continue
        start, end = _number(raw.get("start")), _number(raw.get("end"))
        if start is None or end is None or end <= start:
            continue
        # An utterance that does not overlap the blocks at all contributes no
        # text.  Without this filter its phrases would pile up into the last
        # block (the cursor cannot advance past the end), which is how an
        # unrelated 60-second block used to smear itself over the tail.
        if min(end, upper) - max(start, lower) <= 0:
            continue
        ordered_utterances.append({"id": _text(raw.get("id")), "start": start, "end": end,
                                   "text": str(raw.get("text") or "")})
    ordered_utterances.sort(key=lambda row: (row["start"], row["end"], row["id"]))
    if not ordered_utterances:
        return [], ["spoken_units_unavailable:缺少带时间戳的转写，已退回分句级取舍"]

    buckets: list[list[str]] = [[] for _ in blocks]
    cursor = 0
    placed = 0
    for utterance in ordered_utterances:
        for window in phrase_windows(utterance, max_chars=max_chars):
            # A phrase the estimate places *before* the first block or *after* the
            # last one stays attached to that edge block instead of being
            # dropped: the estimate drifts where speech density is uneven, and
            # losing a phrase would lose the very word the tail anchor looks for
            # ("那我先走了 拜拜").  Order is preserved either way.
            if cursor >= len(blocks):
                cursor = len(blocks) - 1
            while cursor < len(blocks) - 1 and blocks[cursor]["end"] <= window["source_start"]:
                cursor += 1
            buckets[cursor].append(window["text"])
            placed += 1

    units: list[dict[str, Any]] = []
    for index, block in enumerate(blocks):
        text = " ".join(piece for piece in buckets[index] if piece).strip()
        units.append({
            "id": _unit_id(index + 1),
            "start": round(block["start"], 6), "end": round(block["end"], 6),
            "duration_seconds": round(block["end"] - block["start"], 3),
            "text": text,
            "utterance_id": _owner_utterance(ordered_utterances, block),
        })
    if not placed:
        degradations.append("spoken_units_without_text:转写与语音活动没有交集，单元只作切点使用")
    textless = sum(1 for row in units if not row["text"])
    if textless:
        degradations.append(f"spoken_units_partial_text:{textless}个语音单元没有对应转写文本，已强制保留")
    return units, degradations


def _owner_utterance(utterances: list[dict[str, Any]], block: dict[str, float]) -> str:
    """The utterance that covers most of ``block`` — audit only, never a range."""
    best_id, best_overlap = "", 0.0
    for row in utterances:
        overlap = min(block["end"], row["end"]) - max(block["start"], row["start"])
        if overlap > best_overlap:
            best_id, best_overlap = row["id"], overlap
    return best_id


def units_signature(units: Any) -> str:
    """Identity of a unit derivation, for cache keys that must not collide."""
    rows = [{"id": row.get("id"), "start": row.get("start"), "end": row.get("end"),
             "text": row.get("text")} for row in (units or []) if isinstance(row, dict)]
    return _digest({"version": VERSION, "units": rows})


def edge_token_hits(text: str, lexicon: Any) -> list[str]:
    """Which edge terms appear as a phrase of their own.

    A term counts when the ASR phrase equals it, or continues it only by at most
    one character, by a repeated character (``拜拜拜拜``), or by a short run of
    discourse particles (``你好呀``).  "你好聪明" is a phrase about being clever,
    not an opening; treating it as one is what silently cut 32 seconds off a
    measured encounter.
    """
    tokens = [token for token in _TOKEN_SPLIT_RE.split(str(text or "")) if token]
    if not tokens:
        return []
    found: list[str] = []
    for term in lexicon:
        needle = str(term).lower()
        if not needle:
            continue
        for token in tokens:
            lowered = token.lower()
            if lowered == needle:
                found.append(term)
                break
            if not lowered.startswith(needle):
                continue
            remainder = lowered[len(needle):]
            if len(remainder) <= 1 or all(char == remainder[0] for char in remainder):
                found.append(term)
                break
            if len(remainder) <= _MAX_PARTICLE_SUFFIX and all(
                    char in _EDGE_PARTICLES for char in remainder):
                found.append(term)
                break
    return found


def text_hits(text: str, lexicon: Any) -> list[str]:
    """Backwards-compatible alias for :func:`edge_token_hits`.

    Earlier drafts matched raw substrings, which made ``hi`` fire inside "this".
    Both the word-boundary rule and the phrase rule now live in one place.
    """
    return edge_token_hits(text, lexicon)


__all__ = [
    "DEFAULT_MAX_CHARS", "FAREWELL_LEXICON", "GREETING_LEXICON", "InteractionUnitsError",
    "MERGE_GAP_SECONDS", "MIN_SPEECH_SECONDS", "MIN_UNIT_SECONDS", "VERSION",
    "build_spoken_units", "edge_token_hits", "normalize_speech_ranges", "phrase_windows",
    "speech_blocks", "split_phrases", "text_hits", "units_signature",
]
