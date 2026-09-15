"""Versioned second-pass plans for hook-led outdoor interaction edits."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
import os
import re
from pathlib import Path
import tempfile
import time
from typing import Any

from backlot.material_interaction_units import (
    phrase_windows as _shared_phrase_windows,
    split_phrases as _shared_split_phrases,
    units_signature,
)


# v2 introduced deterministic content QA; v3 adds pause compression; v4 adds the
# action model (``actions``) and segment-level speed (``speed_segments``); v5
# turns the burned-in captions from "one cue per whole utterance" into "one cue
# per spoken phrase" (see ``subtitle_cues``).  Every version stays readable
# because frozen plans live on disk and must survive an upgrade (N7).  The gate
# sets below are **explicit constants** on purpose: a wrong condition would
# demand a newer field from an older plan and fail every historical plan with
# "与语义选择不一致".
LEGACY_VERSION = "interaction-second-pass-plan-v1"
CONTENT_QA_VERSION = "interaction-second-pass-plan-v2"
PAUSE_VERSION = "interaction-second-pass-plan-v3"
ACTION_VERSION = "interaction-second-pass-plan-v4"
SUBTITLE_SENTENCE_VERSION = "interaction-second-pass-plan-v5"
# v6 groups *phrase-level speech units* instead of 27—60 second ASR blocks and
# freezes them as ``spoken_units``.  The units are frozen input, exactly like
# ``utterances`` — nothing downstream is re-derived from them, so v6 needs no
# extra gate set: every derived field still comes from the frozen ``story``.
UNIT_VERSION = "interaction-second-pass-plan-v6"
# v7 makes the pause target a *promise about the output*: the surviving gap
# between two adjacent lines is exactly ``pause_target_gap_seconds`` (0.30 s by
# default) instead of "2*guard + gap".  It also builds captions from the
# VAD-aligned spoken units instead of whole 60-second ASR blocks, which is what
# made burned-in captions lag several seconds behind the speech.
GAP_TARGET_VERSION = "interaction-second-pass-plan-v7"
VERSION = GAP_TARGET_VERSION
SUPPORTED_VERSIONS = {
    LEGACY_VERSION, CONTENT_QA_VERSION, PAUSE_VERSION, ACTION_VERSION,
    SUBTITLE_SENTENCE_VERSION, UNIT_VERSION, GAP_TARGET_VERSION,
}
# Pause compression only exists where pause evidence was frozen into the plan,
# so the derived fields must not be demanded from older plans.  v3 introduced it
# and every newer version keeps it.
PAUSE_AWARE_VERSIONS = {PAUSE_VERSION, ACTION_VERSION, SUBTITLE_SENTENCE_VERSION,
                        UNIT_VERSION, GAP_TARGET_VERSION}
# ``actions`` is only produced for v4+ occurrences; older plans have no such key
# and must never be asked for it.
ACTION_AWARE_VERSIONS = {ACTION_VERSION, SUBTITLE_SENTENCE_VERSION, UNIT_VERSION, GAP_TARGET_VERSION}
# ``speed_segments`` (segment-level speed for waiting passages) is v4+ only.
SPEED_SEGMENT_AWARE_VERSIONS = {ACTION_VERSION, SUBTITLE_SENTENCE_VERSION,
                                UNIT_VERSION, GAP_TARGET_VERSION}
# ``subtitle_cues`` changed shape in v5: one cue per spoken phrase instead of one
# cue per whole utterance.  Only v5+ plans may be asked to reproduce the new
# derivation; v1-v4 keep whatever captions the older build wrote and are read
# back verbatim, otherwise every historical plan would become unreadable (N7).
SUBTITLE_SENTENCE_AWARE_VERSIONS = {SUBTITLE_SENTENCE_VERSION, UNIT_VERSION, GAP_TARGET_VERSION}
# v7 only: captions are derived from the spoken units, so a cue's time is a
# VAD-anchored estimate rather than a position inside a 60-second ASR block.
UNIT_CAPTION_AWARE_VERSIONS = {GAP_TARGET_VERSION}
# v7 only: the "surviving gap equals the target" geometry.  v1-v6 must keep the
# old arithmetic byte-for-byte or every frozen plan fails its own reconstruction.
GAP_TARGET_AWARE_VERSIONS = {GAP_TARGET_VERSION}
PLAN_FILENAME = "interaction-second-pass-plan.json"
ALLOWED_SPEEDS = (1.0, 1.1, 1.25)
ALLOWED_STATUSES = {"pending_review", "approved", "rejected"}
ALLOWED_HOOK_MODES = {"move", "repeat", "none"}
ALLOWED_PAUSE_SCOPES = {"body", "all"}
# --- v4 options: pause handling, audio fades, margins, concurrency ----------
# ``remove`` keeps the V3 behaviour (delete the middle of a pause); ``speed_up``
# fast-forwards the whole waiting passage instead of cutting it; ``off`` leaves
# everything alone.  ``speed_up`` and ``compress_pauses`` are mutually exclusive.
PAUSE_HANDLING_MODES = {"remove", "speed_up", "off"}
PAUSE_HANDLING_DEFAULT = "remove"
# --- what counts as a compressible gap ---------------------------------------
# ``quiet`` keeps the historical three-permission contract: the passage must not
# touch VAD speech **and** the probe must call it silence.  ``any`` compresses
# every gap between two VAD speech runs, so the surviving gap really is the
# target — and it deletes whatever ambience sat in there.
#
# Measured on the acceptance material (R0007, 149.6 s parent): of the 65 gaps
# that survive in the output, 55 are quiet and 10 (9.09 s) sit at −18…−29 dB,
# i.e. at or above the median speech frame — they are the machine's motor,
# traffic and crowd, not dead air.  ``any`` is the default because the product
# asked for a uniform 0.30 s rhythm; ``quiet`` is one click away.
GAP_POLICIES = {"quiet", "any"}
GAP_POLICY_DEFAULT = "any"
GAP_POLICY_LABELS = {"any": "连环境音一起压（默认）", "quiet": "只压真静音（三重许可）"}
PAUSE_SPEED_DEFAULT = 2.0
PAUSE_SPEED_BOUNDS = (1.5, 4.0)
PAUSE_SPEED_PRESETS = (1.5, 2.0, 3.0, 4.0)
# --- pause strength presets ---------------------------------------------------
# v7 reshaped the arithmetic: ``pause_target_gap_seconds`` is the **surviving
# gap between two adjacent lines**, exactly, and ``pause_guard_seconds`` is only a
# floor on the per-side margin (it can no longer add itself on top of the target).
# Measured on the acceptance material's parent range (149.6 s, 68 VAD gaps /
# 50.8 s): the probe at the calibrated −40 dB could only see 23.4 s of those gaps
# because a single loud frame truncates a quiet run, so the presets also carry a
# probe floor and a bridge width.  See
# ``docs/SINGLE_DEVELOPMENT_GUIDE_INTERACTION_PHRASE_UNIT_RANK_V4_ZH-CN.md`` §13.
PAUSE_PRESETS: dict[str, dict[str, float]] = {
    # Target survivor 0.50 s — keeps more air, for material where the pauses are
    # part of the performance.
    "conservative": {"probe_min_silence_seconds": 0.45, "probe_bridge_seconds": 0.00,
                     "pause_min_seconds": 0.30, "pause_guard_seconds": 0.15,
                     "pause_target_gap_seconds": 0.50},
    "standard": {"probe_min_silence_seconds": 0.30, "probe_bridge_seconds": 0.10,
                 "pause_min_seconds": 0.25, "pause_guard_seconds": 0.12,
                 "pause_target_gap_seconds": 0.40},
    # The shipped default: 0.30 s between lines, which is the rhythm the user
    # asked for, and 0.15 s of untouched audio on each side of every cut.
    "tight": {"probe_min_silence_seconds": 0.20, "probe_bridge_seconds": 0.10,
              "pause_min_seconds": 0.20, "pause_guard_seconds": 0.10,
              "pause_target_gap_seconds": 0.30},
}
PAUSE_PRESET_ORDER = ("conservative", "standard", "tight")
PAUSE_PRESET_DEFAULT = "tight"
PAUSE_PRESET_LABELS = {"conservative": "保守（间隙留 0.5 秒）",
                       "standard": "标准（间隙留 0.4 秒）",
                       "tight": "紧凑（间隙留 0.3 秒，默认）"}
# --- duration policy ---------------------------------------------------------
# The clip is a *tightened* version of one interaction, not a 45—60 second
# trailer.  An absolute target made the model delete half of a 136-second
# encounter ("信息密度相对较低") to land inside the window; the proportional
# policy keeps the narrative whole and lets the pause layer do the tightening.
# `absolute` exists so any explicit numeric target stays reproducible.
DURATION_POLICIES = {"proportional", "absolute"}
DURATION_POLICY_DEFAULT = "proportional"
PROPORTIONAL_MIN_RATIO = 0.70
PROPORTIONAL_MAX_RATIO = 1.00
DURATION_BOUNDS = (15.0, 1800.0)
# 5-15 ms of fade at every seam removes the click; it never changes the duration.
AUDIO_FADE_MS_DEFAULT = 8.0
AUDIO_FADE_MS_BOUNDS = (5.0, 15.0)
EDGE_FADE_MS_DEFAULT = 200.0
EDGE_FADE_MS_BOUNDS = (150.0, 300.0)
# Asymmetric breathing room around a cut (P1-3).  ``None`` keeps the symmetric
# ``pause_guard_seconds`` of V3, so historical plans are unaffected.
PAUSE_MARGIN_BOUNDS = (0.0, 0.5)
WINDOW_CONTEXT_POLICIES = {"serial_equivalent", "concurrent"}
WINDOW_CONTEXT_POLICY_DEFAULT = "serial_equivalent"
INTERACTION_CONCURRENCY_DEFAULT = 1
ASR_CONCURRENCY_DEFAULT = 3
CONCURRENCY_BOUNDS = (1, 4)
# --- v5 options: caption granularity ----------------------------------------
# Captions used to be one giant block per utterance; a 60-second, 198-character
# utterance was stamped in full into every occurrence it touched, so the viewer
# saw the same wall of text four times.  A cue is now built from the spoken
# phrases and merged back up to these limits.  16 Chinese characters is about
# one readable line at the shipped style; 4 s matches how long a viewer can
# comfortably hold a single line before it feels stuck.
#
# ``subtitle_max_chars`` is a **target**, not a hard ceiling, and the UI/labels
# must say so: keeping the minimum on-screen time wins when the two conflict, so
# a lone short phrase can push one caption a few characters past the target.  A
# one-frame flash is worse than a slightly long line.
SUBTITLE_MAX_CHARS_DEFAULT = 16
SUBTITLE_MAX_CHARS_BOUNDS = (4, 60)
SUBTITLE_MAX_SECONDS_DEFAULT = 4.0
SUBTITLE_MAX_SECONDS_BOUNDS = (1.0, 15.0)
# A caption shorter than this flashes for a single frame, which reads as a
# glitch; merging it into its neighbour is the lesser evil.
SUBTITLE_MIN_SECONDS_DEFAULT = 0.8
SUBTITLE_MIN_SECONDS_BOUNDS = (0.2, 3.0)
# Punctuation that ends a spoken phrase.  Tencent ASR separates phrases with
# spaces, so punctuation is only a secondary break point for over-long tokens.
SUBTITLE_PHRASE_PUNCTUATION = "。！？；，、"
# Shipped compression preset, chosen from measurements on the 88.9-minute
# sample rather than by taste.  Per cut the fixed cost is 2*guard + gap, and the
# material's dead air is mostly many short 0.5-1.0s gaps, so the preset — not
# the probe resolution — is what decides how much can be removed.  Every preset
# tested (down to guard 0.10 / gap 0.15) produced zero cuts overlapping a VAD
# speech frame, so this stays on the conservative side of a safe range.
PAUSE_MIN_SECONDS_DEFAULT = 0.4
PAUSE_MIN_SECONDS_BOUNDS = (0.2, 3.0)
PAUSE_TARGET_GAP_DEFAULT = 0.2
PAUSE_TARGET_GAP_BOUNDS = (0.1, 1.0)
# 0.12s of untouched audio beside every cut absorbs the fact that VAD marks a
# speech onset a little late; it is the last line of defence for a clipped word.
PAUSE_GUARD_DEFAULT = 0.12
PAUSE_GUARD_BOUNDS = (0.05, 0.5)
PAUSE_EVIDENCE_STATUSES = {"available", "partial"}
# Human-readable labels so a degradation is actionable instead of a raw enum.
PAUSE_STATUS_LABELS = {"available": "可用", "partial": "部分可用", "unavailable": "探测不可用", "": "缺失"}
# Below this the edit is not worth a cut, and a cut costs a re-encode anyway.
MIN_PAUSE_REMOVAL_SECONDS = 0.05


class InteractionSecondPassError(ValueError):
    pass


class InteractionSecondPassConflict(InteractionSecondPassError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _number(value: Any, *, minimum: float, maximum: float, label: str) -> float:
    if isinstance(value, bool):
        raise InteractionSecondPassError(f"{label}格式无效")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise InteractionSecondPassError(f"{label}格式无效") from exc
    if not math.isfinite(result) or not minimum <= result <= maximum:
        raise InteractionSecondPassError(f"{label}超出范围")
    return result


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
        for attempt in range(12):
            try:
                os.replace(temporary, path)
                return
            except PermissionError:
                if attempt == 11:
                    raise
                time.sleep(.01 * (attempt + 1))
    finally:
        temporary.unlink(missing_ok=True)


def normalize_options(raw: dict[str, Any] | None, *, legacy: bool = False,
                      parent_seconds: float | None = None) -> dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    requested_speed = _number(raw.get("speed", 1.1), minimum=1.0, maximum=1.25, label="播放速度")
    speed = requested_speed
    speed = min(ALLOWED_SPEEDS, key=lambda candidate: abs(candidate - speed))
    if abs(requested_speed - speed) > .001:
        raise InteractionSecondPassError("播放速度只支持 1.0、1.1 或 1.25 倍")
    # --- v6: how long should the clip be ---------------------------------
    # The policy is inferred only when the caller stayed silent about it: a
    # request that spells out `target_min_seconds`/`target_max_seconds` is asking
    # for exactly those numbers, and silently re-deriving them would override a
    # human decision (an older caller, or the "固定秒数" switch).
    raw_policy = raw.get("duration_policy")
    if raw_policy is None:
        duration_policy = ("absolute" if ("target_min_seconds" in raw or "target_max_seconds" in raw)
                           else DURATION_POLICY_DEFAULT)
    else:
        duration_policy = str(raw_policy)
    if duration_policy not in DURATION_POLICIES:
        raise InteractionSecondPassError("目标时长策略无效，请选择按素材比例或固定秒数")
    proportional = duration_policy == "proportional" and parent_seconds is not None
    if proportional:
        parent = _number(parent_seconds, minimum=0.0, maximum=24 * 3600, label="父内容时长")
        minimum = round(min(DURATION_BOUNDS[1], max(DURATION_BOUNDS[0], parent * PROPORTIONAL_MIN_RATIO)), 3)
        maximum = round(min(DURATION_BOUNDS[1], max(DURATION_BOUNDS[0], parent * PROPORTIONAL_MAX_RATIO)), 3)
        if maximum < minimum:
            maximum = minimum
    else:
        # Explicit numbers (or a stored plan being validated) keep their values.
        minimum = _number(raw.get("target_min_seconds", 45), minimum=DURATION_BOUNDS[0],
                          maximum=DURATION_BOUNDS[1], label="目标最短时长")
        maximum = _number(raw.get("target_max_seconds", 60), minimum=DURATION_BOUNDS[0],
                          maximum=DURATION_BOUNDS[1], label="目标最长时长")
    if maximum < minimum:
        raise InteractionSecondPassError("目标最长时长不能小于最短时长")
    hook_enabled = raw.get("hook_enabled") is not False
    default_hook_mode = "repeat" if legacy else "move"
    hook_mode = str(raw.get("hook_mode") or default_hook_mode)
    if hook_mode not in ALLOWED_HOOK_MODES:
        raise InteractionSecondPassError("精彩前置方式无效")
    if not hook_enabled:
        hook_mode = "none"
    pause_scope = str(raw.get("pause_scope") or "body")
    if pause_scope not in ALLOWED_PAUSE_SCOPES:
        raise InteractionSecondPassError("停顿压缩范围无效")
    # --- v4: waiting-passage handling -------------------------------------
    pause_handling = str(raw.get("pause_handling") or PAUSE_HANDLING_DEFAULT)
    if pause_handling not in PAUSE_HANDLING_MODES:
        raise InteractionSecondPassError("等待段处理方式无效，请选择删掉停顿、快放等待段或不处理")
    try:
        pause_speed = round(_number(
            raw.get("pause_speed", PAUSE_SPEED_DEFAULT),
            minimum=PAUSE_SPEED_BOUNDS[0], maximum=PAUSE_SPEED_BOUNDS[1], label="等待段快放倍速",
        ), 3)
    except InteractionSecondPassError as exc:
        raise InteractionSecondPassError("等待段快放倍速需在 1.5–4.0 之间，请调整后重试。") from exc
    compress_explicit = raw.get("compress_pauses") is True
    if pause_handling != PAUSE_HANDLING_DEFAULT:
        if compress_explicit:
            raise InteractionSecondPassError("「快放等待段」与「压缩对话间停顿」互斥，不能对同一段既删又快放")
        compress_pauses = False
    else:
        compress_pauses = raw.get("compress_pauses") is not False
    # --- v6: pause strength preset (probe floor + plan-layer overhead) -----
    pause_preset = str(raw.get("pause_preset") or PAUSE_PRESET_DEFAULT)
    if pause_preset not in PAUSE_PRESETS:
        raise InteractionSecondPassError("停顿压缩强度无效，请选择保守、标准或紧凑")
    preset = PAUSE_PRESETS[pause_preset]
    # --- v4: audio fades (P0-3 / P1-3) -----------------------------------
    audio_fade = raw.get("audio_fade") is not False
    audio_fade_ms = AUDIO_FADE_MS_DEFAULT
    if audio_fade:
        try:
            audio_fade_ms = round(_number(
                raw.get("audio_fade_ms", AUDIO_FADE_MS_DEFAULT),
                minimum=AUDIO_FADE_MS_BOUNDS[0], maximum=AUDIO_FADE_MS_BOUNDS[1], label="切口淡化时长",
            ), 3)
        except InteractionSecondPassError as exc:
            raise InteractionSecondPassError("切口淡化时长需在 5–15 毫秒之间，请调整后重试。") from exc
    edge_fade = raw.get("edge_fade") is True
    edge_fade_ms = EDGE_FADE_MS_DEFAULT
    if edge_fade:
        try:
            edge_fade_ms = round(_number(
                raw.get("edge_fade_ms", EDGE_FADE_MS_DEFAULT),
                minimum=EDGE_FADE_MS_BOUNDS[0], maximum=EDGE_FADE_MS_BOUNDS[1], label="整片首尾淡化时长",
            ), 3)
        except InteractionSecondPassError as exc:
            raise InteractionSecondPassError("整片首尾淡化时长需在 150–300 毫秒之间，请调整后重试。") from exc
    # --- v4: asymmetric pause margins (P1-3) -----------------------------
    try:
        margin_head = _optional_margin(raw.get("pause_margin_head_seconds"), label="停顿前留白")
        margin_tail = _optional_margin(raw.get("pause_margin_tail_seconds"), label="停顿后留白")
    except InteractionSecondPassError as exc:
        raise InteractionSecondPassError("停顿留白需在 0–0.5 秒之间，请调整后重试。") from exc
    # --- v7: what counts as a compressible gap ---------------------------
    gap_policy = str(raw.get("gap_policy") or GAP_POLICY_DEFAULT)
    if gap_policy not in GAP_POLICIES:
        raise InteractionSecondPassError("间隙压缩范围无效，请选择连环境音一起压或只压真静音")
    # --- v4: coarse-cut concurrency knobs (never enter any paid signature) -
    window_context_policy = str(raw.get("window_context_policy") or WINDOW_CONTEXT_POLICY_DEFAULT)
    if window_context_policy not in WINDOW_CONTEXT_POLICIES:
        raise InteractionSecondPassError("窗口并发策略无效，请选择串行等价或并发")
    try:
        interaction_concurrency = int(raw.get("interaction_concurrency", INTERACTION_CONCURRENCY_DEFAULT))
        asr_concurrency = int(raw.get("asr_concurrency", ASR_CONCURRENCY_DEFAULT))
    except (TypeError, ValueError) as exc:
        raise InteractionSecondPassError("并发上限需在 1–4 之间，请调整后重试。") from exc
    if not CONCURRENCY_BOUNDS[0] <= interaction_concurrency <= CONCURRENCY_BOUNDS[1] or \
            not CONCURRENCY_BOUNDS[0] <= asr_concurrency <= CONCURRENCY_BOUNDS[1]:
        raise InteractionSecondPassError("并发上限需在 1–4 之间，请调整后重试。")
    # --- v5: caption granularity -----------------------------------------
    # The character limit is a **target**, not a hard promise: a phrase that is
    # itself shorter than the minimum on-screen time may push one caption a few
    # characters past it, because a one-frame flash is worse than a slightly long
    # line.  The message says "目标" so the UI never over-promises.
    raw_max_chars = raw.get("subtitle_max_chars", SUBTITLE_MAX_CHARS_DEFAULT)
    if isinstance(raw_max_chars, bool):
        raise InteractionSecondPassError("字幕单条目标字数需在 4–60 之间，请调整后重试。")
    try:
        subtitle_max_chars = int(raw_max_chars)
    except (TypeError, ValueError) as exc:
        raise InteractionSecondPassError("字幕单条目标字数需在 4–60 之间，请调整后重试。") from exc
    if not SUBTITLE_MAX_CHARS_BOUNDS[0] <= subtitle_max_chars <= SUBTITLE_MAX_CHARS_BOUNDS[1]:
        raise InteractionSecondPassError("字幕单条目标字数需在 4–60 之间，请调整后重试。")
    try:
        subtitle_max_seconds = round(_number(
            raw.get("subtitle_max_seconds", SUBTITLE_MAX_SECONDS_DEFAULT),
            minimum=SUBTITLE_MAX_SECONDS_BOUNDS[0], maximum=SUBTITLE_MAX_SECONDS_BOUNDS[1],
            label="字幕单条最长时长",
        ), 3)
    except InteractionSecondPassError as exc:
        raise InteractionSecondPassError("字幕单条最长时长需在 1–15 秒之间，请调整后重试。") from exc
    try:
        subtitle_min_seconds = round(_number(
            raw.get("subtitle_min_seconds", SUBTITLE_MIN_SECONDS_DEFAULT),
            minimum=SUBTITLE_MIN_SECONDS_BOUNDS[0], maximum=SUBTITLE_MIN_SECONDS_BOUNDS[1],
            label="字幕单条最短时长",
        ), 3)
    except InteractionSecondPassError as exc:
        raise InteractionSecondPassError("字幕单条最短时长需在 0.2–3.0 秒之间，请调整后重试。") from exc
    if subtitle_min_seconds > subtitle_max_seconds:
        raise InteractionSecondPassError("字幕单条最短时长不能大于最长时长，请调整后重试。")
    return {
        "preset": "outdoor_interaction_fine_cut",
        "trim_head": raw.get("trim_head") is not False,
        "trim_tail": raw.get("trim_tail") is not False,
        "extract_highlights": raw.get("extract_highlights") is not False,
        "hook_enabled": hook_mode != "none",
        "hook_mode": hook_mode,
        "speed": speed,
        "target_min_seconds": round(minimum, 3),
        "target_max_seconds": round(maximum, 3),
        # Removing the dead air *between* lines is what makes a taken clip feel
        # edited instead of merely shortened.  It is on by default because the
        # user asked for it and because every cut is guarded by two independent
        # pieces of evidence at plan time.
        "compress_pauses": compress_pauses,
        "pause_scope": pause_scope,
        "pause_handling": pause_handling,
        "pause_speed": pause_speed,
        "pause_preset": pause_preset,
        "pause_min_seconds": round(_number(
            raw.get("pause_min_seconds", preset["pause_min_seconds"]),
            minimum=PAUSE_MIN_SECONDS_BOUNDS[0], maximum=PAUSE_MIN_SECONDS_BOUNDS[1], label="最短可压缩停顿",
        ), 3),
        "pause_target_gap_seconds": round(_number(
            raw.get("pause_target_gap_seconds", preset["pause_target_gap_seconds"]),
            minimum=PAUSE_TARGET_GAP_BOUNDS[0], maximum=PAUSE_TARGET_GAP_BOUNDS[1], label="停顿保留时长",
        ), 3),
        "pause_guard_seconds": round(_number(
            raw.get("pause_guard_seconds", preset["pause_guard_seconds"]),
            minimum=PAUSE_GUARD_BOUNDS[0], maximum=PAUSE_GUARD_BOUNDS[1], label="停顿安全边距",
        ), 3),
        "duration_policy": duration_policy,
        "pause_margin_head_seconds": margin_head,
        "pause_margin_tail_seconds": margin_tail,
        "gap_policy": gap_policy,
        "audio_fade": audio_fade,
        "audio_fade_ms": audio_fade_ms,
        "edge_fade": edge_fade,
        "edge_fade_ms": edge_fade_ms,
        "window_context_policy": window_context_policy,
        "interaction_concurrency": interaction_concurrency,
        "asr_concurrency": asr_concurrency,
        "subtitle_max_chars": subtitle_max_chars,
        "subtitle_max_seconds": subtitle_max_seconds,
        "subtitle_min_seconds": subtitle_min_seconds,
        "burn_subtitles": raw.get("burn_subtitles") is not False,
    }


def _optional_margin(value: Any, *, label: str) -> float | None:
    """Asymmetric pause margin: ``None`` keeps the symmetric V3 guard."""
    if value is None:
        return None
    number = _number(value, minimum=PAUSE_MARGIN_BOUNDS[0], maximum=PAUSE_MARGIN_BOUNDS[1], label=label)
    return round(number, 3)


def _range_within_allowed(start: float, end: float, allowed: list[dict[str, Any]]) -> bool:
    return any(start >= float(row["start"]) and end <= float(row["end"]) for row in allowed)


def _selected_groups(plan: dict[str, Any]) -> list[dict[str, Any]]:
    return [row for row in (plan.get("story") or {}).get("groups") or [] if row.get("selected") is True]


def _validate_dependencies(groups: list[dict[str, Any]]) -> None:
    by_id = {str(row.get("id")): row for row in groups}
    selected = {group_id for group_id, row in by_id.items() if row.get("selected") is True}
    for row in groups:
        for dependency in row.get("depends_on") or []:
            if dependency not in by_id:
                raise InteractionSecondPassError("对话组依赖引用无效")
            if row.get("selected") is True and dependency not in selected:
                raise InteractionSecondPassError(
                    f"“{row.get('summary') or row.get('id')}”依赖前文“{by_id[dependency].get('summary') or dependency}”，请一并保留"
                )


def _body_occurrences(groups: list[dict[str, Any]], speed: float, *, excluded_group_ids: set[str] | None = None) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    excluded_group_ids = excluded_group_ids or set()
    for row in sorted(
        (item for item in groups if item.get("selected") is True and str(item.get("id")) not in excluded_group_ids),
        key=lambda item: int(item.get("sequence") or 0),
    ):
        ranges = row.get("source_ranges") or [row.get("source_range") or {}]
        for source_range in ranges:
            start, end = float(source_range["start"]), float(source_range["end"])
            if result and start <= result[-1]["source_end"]:
                result[-1]["source_end"] = round(max(result[-1]["source_end"], end), 3)
                if str(row["id"]) not in result[-1]["group_ids"]:
                    result[-1]["group_ids"].append(str(row["id"]))
                continue
            result.append({
                "occurrence_id": f"O-BODY-{len(result)+1:03d}",
                "role": "body", "group_ids": [str(row["id"])],
                "source_start": round(start, 3), "source_end": round(end, 3), "speed": speed,
            })
    return result


def _overlaps(a: float, b: float, c: float, d: float) -> bool:
    return a < d and c < b


def _subtract(interval: tuple[float, float], holes: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """``interval`` minus every hole, as the list of surviving sub-intervals."""
    pieces = [(float(interval[0]), float(interval[1]))]
    for hole_start, hole_end in holes:
        surviving: list[tuple[float, float]] = []
        for start, end in pieces:
            if hole_end <= start or hole_start >= end:
                surviving.append((start, end))
                continue
            if hole_start > start:
                surviving.append((start, min(hole_start, end)))
            if hole_end < end:
                surviving.append((max(hole_end, start), end))
        pieces = surviving
    return [(start, end) for start, end in pieces if end - start > 1e-9]


def _normalized_silences(pause_evidence: Any) -> tuple[list[dict[str, float]], list[str]]:
    """Usable quiet intervals plus the reasons nothing else can be used."""
    evidence = pause_evidence if isinstance(pause_evidence, dict) else {}
    status = str(evidence.get("status") or "")
    if status not in PAUSE_EVIDENCE_STATUSES:
        label = PAUSE_STATUS_LABELS.get(status, status or "缺失")
        return [], [f"pause_compression_disabled:停顿证据不可用（{label}）"]
    rows = []
    for raw in evidence.get("silences") or []:
        if not isinstance(raw, dict):
            continue
        try:
            start, end = float(raw["start"]), float(raw["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if end > start:
            rows.append({"start": start, "end": end})
    if not rows:
        return [], ["pause_compression_skipped:所选范围内没有可用的静音证据"]
    return sorted(rows, key=lambda row: (row["start"], row["end"])), []


def _normalized_speech(speech_ranges: Any) -> list[dict[str, float]]:
    rows = []
    for raw in speech_ranges if isinstance(speech_ranges, list) else []:
        if not isinstance(raw, dict):
            continue
        try:
            start, end = float(raw["start"]), float(raw["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if end > start:
            rows.append({"start": start, "end": end})
    return sorted(rows, key=lambda row: (row["start"], row["end"]))


def _pause_keep_seconds(guard: float, gap: float, *, gap_target: bool) -> float:
    """How much untouched audio survives on *each* side of a cut.

    ``gap_target`` (v7) treats ``gap`` as the **total surviving interval between
    two lines**, so each side keeps ``gap / 2`` unless the guard asks for more —
    the guard can no longer silently add itself on top of the target.  The older
    geometry kept ``guard + gap/2`` per side (total = 2*guard + gap), which is why
    a "0.3 s gap" was unreachable before: guard 0.06 + 0.3 would have left 0.42 s.
    """
    return max(guard, gap / 2) if gap_target else guard + gap / 2


def _candidate_pieces(occurrence: dict[str, Any], silences: list[dict[str, float]],
                      speech: list[dict[str, float]], *, minimum: float,
                      policy: str) -> list[tuple[float, float, float, float]]:
    """The intervals inside one occurrence that a cut may shorten.

    Returns ``(reported_start, reported_end, piece_start, piece_end)``: the first
    two describe the passage as the probe saw it (which is what the frozen plans
    record and therefore must not change), the last two are the piece actually
    cut, i.e. the passage minus any VAD speech inside it.

    ``quiet`` = the probe's silence minus every VAD speech frame (the historical
    three-permission contract).  ``any`` = every gap between two VAD speech runs,
    including the head and tail of the occurrence, so that the surviving gap
    between two lines really is the target; the ambience inside those gaps is
    deleted along with them (measured cost is recorded in ``GAP_POLICIES``).
    """
    start, end = float(occurrence["source_start"]), float(occurrence["source_end"])
    holes = [(max(float(row["start"]), start), min(float(row["end"]), end)) for row in speech]
    holes = [(a, b) for a, b in holes if b - a > 1e-9]
    if policy == "any":
        return [(a, b, a, b) for a, b in _subtract((start, end), holes) if b - a >= minimum]
    pieces: list[tuple[float, float, float, float]] = []
    for silence in silences:
        clipped = (max(start, silence["start"]), min(end, silence["end"]))
        if clipped[1] - clipped[0] < minimum:
            continue
        pieces.extend((clipped[0], clipped[1], a, b) for a, b in _subtract(clipped, holes)
                      if b - a >= minimum)
    return pieces


def _pause_trims(occurrences: list[dict[str, Any]], pause_evidence: Any, speech_ranges: Any,
                 options: dict[str, Any], *, budget_seconds: float | None = None,
                 gap_target: bool = False,
                 ) -> list[dict[str, Any]]:
    """Plan every safe removal of the dead air between lines.

    Three permissions must all hold, otherwise the pause is kept:

    1. **No speech.**  The passage must not intersect a VAD speech frame, so no
       word can ever be clipped.  VAD is the authority on "somebody is talking".
    2. **Actually quiet.**  ``silencedetect`` (or the calibrated envelope) must
       call it silence, so laughter, applause or a dropped object is never
       mistaken for dead air.
    3. **Worth a cut.**  It must last at least ``pause_min_seconds``.

    With ``gap_target`` (v7) each cut leaves exactly ``pause_target_gap_seconds``
    between the two lines; without it (v1–v6) the old ``2*guard + gap`` arithmetic
    is reproduced byte-for-byte so frozen plans stay readable (N7).

    ``budget_seconds`` bounds the total removal (the caller uses it to avoid
    pushing the clip below the requested minimum duration).  ``None`` removes
    everything that is safe.
    """
    # v1–v6 predate the policy: they always meant "only real silence" and must
    # keep deriving exactly that, or every frozen plan fails its reconstruction.
    policy = (str(options.get("gap_policy") or GAP_POLICY_DEFAULT) if gap_target else "quiet")
    silences = _normalized_silences(pause_evidence)[0]
    if not silences and policy == "quiet":
        return []
    if budget_seconds is not None and budget_seconds <= MIN_PAUSE_REMOVAL_SECONDS:
        return []
    speech = _normalized_speech(speech_ranges)
    if not speech:
        return []
    guard = float(options.get("pause_guard_seconds") or PAUSE_GUARD_DEFAULT)
    gap = float(options.get("pause_target_gap_seconds") or PAUSE_TARGET_GAP_DEFAULT)
    minimum = float(options.get("pause_min_seconds") or PAUSE_MIN_SECONDS_DEFAULT)
    keep = _pause_keep_seconds(guard, gap, gap_target=gap_target)
    scope = str(options.get("pause_scope") or "body")
    candidates: list[dict[str, Any]] = []
    for occurrence in occurrences:
        if scope == "body" and occurrence.get("role") != "body":
            continue
        for reported_start, reported_end, piece_start, piece_end in _candidate_pieces(
                occurrence, silences, speech, minimum=minimum, policy=policy):
            cut_start, cut_end = piece_start + keep, piece_end - keep
            if cut_end - cut_start <= MIN_PAUSE_REMOVAL_SECONDS:
                continue
            if any(_overlaps(cut_start, cut_end, row["start"], row["end"]) for row in speech):
                # Defence in depth: subtraction should already have removed it.
                continue
            loud = not any(piece_start >= row["start"] - 1e-9 and piece_end <= row["end"] + 1e-9
                           for row in silences)
            candidates.append({
                "occurrence_id": str(occurrence["occurrence_id"]),
                "role": str(occurrence.get("role") or ""),
                "silence_start": round(reported_start, 6), "silence_end": round(reported_end, 6),
                "source_start": round(cut_start, 6),
                "source_end": round(cut_end, 6),
                "removed_seconds": round(cut_end - cut_start, 6),
                # v7: the gap that actually survives between the two lines.
                # v1–v6: the old field meant "breathing room kept in the
                # middle" and must stay byte-identical or every frozen plan
                # fails its own reconstruction (N7).
                "kept_pause_seconds": round(2 * keep if gap_target else gap, 6),
                "reason": (
                    ("按目标间隙压缩对话间停顿；两侧各留 %.2f 秒不动" % keep)
                    + ("；该段不是静音（连环境音一起压）" if loud else "")
                ) if gap_target else
                "VAD 与静音探测双重确认的等待段；已保留呼吸与两侧安全边距",
            })
    if budget_seconds is not None:
        candidates = _apply_pause_budget(candidates, budget_seconds)
    candidates.sort(key=lambda row: (row["source_start"], row["source_end"]))
    return [{"trim_id": f"PT{index:03d}", **row} for index, row in enumerate(candidates, 1)]


def _apply_pause_budget(candidates: list[dict[str, Any]], budget_seconds: float) -> list[dict[str, Any]]:
    """Keep the biggest pauses first until the removal budget is spent.

    Compression exists to tighten the clip, not to shrink it past the duration
    the user asked for.  When the budget cannot fit a whole pause, the last cut
    is shortened instead of dropped so the target is met exactly.
    """
    kept: list[dict[str, Any]] = []
    remaining = float(budget_seconds)
    for row in sorted(candidates, key=lambda item: (-item["removed_seconds"], item["source_start"])):
        if remaining <= MIN_PAUSE_REMOVAL_SECONDS:
            break
        if row["removed_seconds"] <= remaining:
            kept.append(dict(row))
            remaining -= row["removed_seconds"]
            continue
        shortened_end = round(row["source_start"] + remaining, 6)
        if shortened_end < row["source_end"] - 1e-6 and remaining > MIN_PAUSE_REMOVAL_SECONDS:
            kept.append({**row, "source_end": shortened_end, "removed_seconds": round(remaining, 6),
                         "shortened": True})
        remaining = 0.0
    return kept


def _apply_pause_trims(occurrences: list[dict[str, Any]], trims: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Split occurrences so the renderer plays fewer, shorter pieces.

    The render layer already concatenates an arbitrary number of trimmed
    segments, so compression needs no new FFmpeg machinery — only a more honest
    list of pieces.  That is also why the cuts stay inside single occurrences
    and never cross a hook/body boundary.
    """
    by_parent: dict[str, list[dict[str, Any]]] = {}
    for trim in trims:
        by_parent.setdefault(str(trim["occurrence_id"]), []).append(trim)
    result: list[dict[str, Any]] = []
    for occurrence in occurrences:
        cuts = sorted(by_parent.get(str(occurrence["occurrence_id"])) or [],
                      key=lambda row: (row["source_start"], row["source_end"]))
        if not cuts:
            result.append(dict(occurrence))
            continue
        start, end = float(occurrence["source_start"]), float(occurrence["source_end"])
        pieces: list[tuple[float, float]] = []
        cursor = start
        for cut in cuts:
            cut_start, cut_end = float(cut["source_start"]), float(cut["source_end"])
            if cut_start - cursor > 1e-6:
                pieces.append((cursor, cut_start))
            cursor = max(cursor, cut_end)
        if end - cursor > 1e-6:
            pieces.append((cursor, end))
        if len(pieces) < 2:
            # Every planned cut is strictly interior by construction, so this
            # only happens for corrupt input.  Dropping the piece is safer than
            # silently playing the untrimmed original.
            continue
        for index, (piece_start, piece_end) in enumerate(pieces, 1):
            result.append({
                **occurrence, "occurrence_id": f"{occurrence['occurrence_id']}-P{index:02d}",
                "source_start": round(piece_start, 6), "source_end": round(piece_end, 6),
            })
    return result


def _speed_actions(speed: float, reason: str) -> list[dict[str, Any]]:
    """The explainable view of one segment's speed.

    v4 stores every occurrence's speed both as ``speed`` (the render layer's
    single source of truth) and as an ``actions`` list so P2-1 can promote the
    action model to first class without another plan-version bump.
    """
    return [{"kind": "speed", "value": round(float(speed), 6), "unit": "ratio", "reason": str(reason)}]


def _pause_speed_segments(occurrences: list[dict[str, Any]], pause_evidence: Any, speech_ranges: Any,
                          options: dict[str, Any]) -> list[dict[str, Any]]:
    """Plan every safe waiting passage to *fast-forward* instead of delete.

    Reuses the exact three permissions and guard maths of :func:`_pause_trims`
    (no VAD speech, real silence, long enough) — only the outcome differs: the
    passage is kept and played at ``pause_speed`` so the picture never jumps.
    """
    silences = _normalized_silences(pause_evidence)[0]
    if not silences:
        return []
    speech = _normalized_speech(speech_ranges)
    guard = float(options.get("pause_guard_seconds") or PAUSE_GUARD_DEFAULT)
    minimum = float(options.get("pause_min_seconds") or PAUSE_MIN_SECONDS_DEFAULT)
    scope = str(options.get("pause_scope") or "body")
    speed = round(float(options.get("pause_speed") or PAUSE_SPEED_DEFAULT), 6)
    candidates: list[dict[str, Any]] = []
    for occurrence in occurrences:
        if scope == "body" and occurrence.get("role") != "body":
            continue
        start, end = float(occurrence["source_start"]), float(occurrence["source_end"])
        for silence in silences:
            clipped_start, clipped_end = max(start, silence["start"]), min(end, silence["end"])
            if clipped_end - clipped_start < minimum:
                continue
            holes = [(max(float(row["start"]), clipped_start), min(float(row["end"]), clipped_end))
                     for row in speech]
            for piece_start, piece_end in _subtract((clipped_start, clipped_end), holes):
                if piece_end - piece_start < minimum:
                    continue
                inner_start, inner_end = piece_start + guard, piece_end - guard
                if inner_end - inner_start <= MIN_PAUSE_REMOVAL_SECONDS:
                    continue
                if any(_overlaps(inner_start, inner_end, row["start"], row["end"]) for row in speech):
                    continue
                candidates.append({
                    "occurrence_id": str(occurrence["occurrence_id"]),
                    "role": str(occurrence.get("role") or ""),
                    "silence_start": round(clipped_start, 6), "silence_end": round(clipped_end, 6),
                    "source_start": round(inner_start, 6), "source_end": round(inner_end, 6),
                    "speed": speed, "unit": "ratio",
                    "reason": "VAD 与静音探测双重确认的等待段；快放而非删除",
                })
    candidates.sort(key=lambda row: (row["source_start"], row["source_end"]))
    return [{"segment_id": f"SS{index:03d}", **row} for index, row in enumerate(candidates, 1)]


def _validate_speed_segments(segments: list[dict[str, Any]], allowed: list[dict[str, Any]],
                             speech_ranges: list[dict[str, float]], options: dict[str, Any]) -> None:
    """Re-check every waiting passage against the same two safety permissions."""
    expected = round(float(options.get("pause_speed") or PAUSE_SPEED_DEFAULT), 6)
    for row in segments:
        start = _number(row.get("source_start"), minimum=0, maximum=24 * 3600, label="快放段开始")
        end = _number(row.get("source_end"), minimum=0, maximum=24 * 3600, label="快放段结束")
        speed = _number(row.get("speed"), minimum=PAUSE_SPEED_BOUNDS[0], maximum=PAUSE_SPEED_BOUNDS[1],
                        label="快放段倍速")
        if end <= start:
            raise InteractionSecondPassError("快放等待段为空或倒序")
        if not _range_within_allowed(start, end, allowed):
            raise InteractionSecondPassError("快放等待段越出父候选允许范围")
        if abs(speed - expected) > 1e-6:
            raise InteractionSecondPassError("快放等待段倍速与所选倍速不一致")
        if str(row.get("unit") or "ratio") != "ratio":
            raise InteractionSecondPassError("快放等待段单位无效")
        if any(_overlaps(start, end, item["start"], item["end"]) for item in speech_ranges):
            raise InteractionSecondPassError("快放等待段与语音帧相交，已阻止生成")


def _apply_speed_segments(occurrences: list[dict[str, Any]], segments: list[dict[str, Any]],
                          options: dict[str, Any]) -> list[dict[str, Any]]:
    """Split occurrences so the waiting pieces play at ``pause_speed``.

    Nothing is removed and no source syllable is repeated: the output duration
    equals ``Σ(source_end - source_start) / segment_speed`` by construction, which
    is exactly what :func:`timeline_mapping` recomputes.
    """
    by_parent: dict[str, list[dict[str, Any]]] = {}
    for segment in segments:
        by_parent.setdefault(str(segment["occurrence_id"]), []).append(segment)
    pause_speed = round(float(options.get("pause_speed") or PAUSE_SPEED_DEFAULT), 6)
    result: list[dict[str, Any]] = []
    for occurrence in occurrences:
        cuts = sorted(by_parent.get(str(occurrence["occurrence_id"])) or [],
                      key=lambda row: (row["source_start"], row["source_end"]))
        base_speed = round(float(occurrence["speed"]), 6)
        if not cuts:
            result.append({**occurrence, "actions": _speed_actions(base_speed, "preset")})
            continue
        start, end = float(occurrence["source_start"]), float(occurrence["source_end"])
        pieces: list[tuple[float, float, float, list[dict[str, Any]]]] = []
        cursor = start
        for cut in cuts:
            cut_start, cut_end = float(cut["source_start"]), float(cut["source_end"])
            if cut_start - cursor > 1e-6:
                pieces.append((cursor, cut_start, base_speed, _speed_actions(base_speed, "preset")))
            pieces.append((cut_start, cut_end, pause_speed, _speed_actions(pause_speed, "waiting_segment")))
            cursor = max(cursor, cut_end)
        if end - cursor > 1e-6:
            pieces.append((cursor, end, base_speed, _speed_actions(base_speed, "preset")))
        if len(pieces) < 2:
            result.append({**occurrence, "speed": pause_speed,
                           "actions": _speed_actions(pause_speed, "waiting_segment")})
            continue
        for index, (piece_start, piece_end, speed, actions) in enumerate(pieces, 1):
            result.append({
                **occurrence, "occurrence_id": f"{occurrence['occurrence_id']}-S{index:02d}",
                "source_start": round(piece_start, 6), "source_end": round(piece_end, 6),
                "speed": speed, "actions": actions,
            })
    return result


def _validate_occurrences(rows: list[dict[str, Any]], allowed: list[dict[str, Any]], *,
                          allow_speed_range: bool = False) -> None:
    ids: set[str] = set()
    previous_body_start = -1.0
    for row in rows:
        start = _number(row.get("source_start"), minimum=0, maximum=24 * 3600, label="片段开始")
        end = _number(row.get("source_end"), minimum=0, maximum=24 * 3600, label="片段结束")
        # v4 speed_up plans carry a segment-level speed inside [1.5, 4.0]; every
        # other plan (and every non-waiting piece) stays on the preset 1.0/1.1/1.25.
        if allow_speed_range:
            # v4 speed_up plans mix preset pieces (1.0/1.1/1.25) with waiting
            # pieces in [1.5, 4.0]; the numeric bound spans both.
            speed = _number(row.get("speed"), minimum=ALLOWED_SPEEDS[0], maximum=PAUSE_SPEED_BOUNDS[1],
                            label="片段倍速")
            allowed_speed = speed in ALLOWED_SPEEDS or PAUSE_SPEED_BOUNDS[0] - 1e-6 <= speed <= PAUSE_SPEED_BOUNDS[1] + 1e-6
        else:
            speed = _number(row.get("speed"), minimum=1.0, maximum=1.25, label="片段倍速")
            allowed_speed = speed in ALLOWED_SPEEDS
        if end <= start or not _range_within_allowed(start, end, allowed):
            raise InteractionSecondPassError("播放片段越出父候选允许范围")
        if not allowed_speed:
            raise InteractionSecondPassError("播放片段倍速无效")
        if row["occurrence_id"] in ids:
            raise InteractionSecondPassError("播放片段出现编号重复")
        ids.add(row["occurrence_id"])
        if row["role"] == "body":
            if start < previous_body_start:
                raise InteractionSecondPassError("正文片段必须保持原片顺序")
            previous_body_start = start


def _validate_pause_trims(trims: list[dict[str, Any]], allowed: list[dict[str, Any]],
                          speech_ranges: list[dict[str, float]]) -> None:
    """Re-check every cut against the two safety permissions.

    This is the contract that makes compression reviewable: a cut that touches
    speech, escapes the parent candidate or removes nothing is a hard error and
    never reaches the renderer.
    """
    for row in trims:
        start = _number(row.get("source_start"), minimum=0, maximum=24 * 3600, label="停顿切口开始")
        end = _number(row.get("source_end"), minimum=0, maximum=24 * 3600, label="停顿切口结束")
        removed = _number(row.get("removed_seconds"), minimum=0, maximum=24 * 3600, label="停顿删除时长")
        if end <= start:
            raise InteractionSecondPassError("停顿切口为空或倒序")
        if not _range_within_allowed(start, end, allowed):
            raise InteractionSecondPassError("停顿切口越出父候选允许范围")
        if removed <= MIN_PAUSE_REMOVAL_SECONDS:
            raise InteractionSecondPassError("停顿切口删除时长过短")
        if abs(removed - (end - start)) > 1e-3:
            raise InteractionSecondPassError("停顿切口删除时长与区间不一致")
        if str(row.get("role") or "") not in {"hook", "body"}:
            raise InteractionSecondPassError("停顿切口角色无效")
        if any(_overlaps(start, end, item["start"], item["end"]) for item in speech_ranges):
            raise InteractionSecondPassError("停顿切口与语音帧相交，已阻止生成")


def build_occurrences(plan: dict[str, Any]) -> list[dict[str, Any]]:
    story = plan.get("story") or {}
    groups = story.get("groups") or []
    _validate_dependencies(groups)
    speed = float((plan.get("options") or {}).get("speed") or 1.0)
    legacy = plan.get("version") == LEGACY_VERSION
    options = normalize_options(plan.get("options"), legacy=legacy)
    hook_mode = options["hook_mode"]
    result = []
    moved_group_ids: set[str] = set()
    hook_id = plan.get("selected_hook_id")
    if hook_id and hook_mode != "none":
        hook = next((row for row in story.get("hook_candidates") or [] if row.get("id") == hook_id), None)
        if not hook:
            raise InteractionSecondPassError("选中的精彩开场不存在")
        by_id = {row["id"]: row for row in groups}
        if any(group_id not in by_id or by_id[group_id].get("selected") is not True for group_id in hook.get("group_ids") or []):
            raise InteractionSecondPassError("精彩开场引用了已经删除的对话组")
        hook_group_ids = {str(group_id) for group_id in hook.get("group_ids") or []}
        selected_group_ids = {str(row.get("id")) for row in groups if row.get("selected") is True}
        # A one-group story is already front-loaded.  Emitting a separate hook
        # would either duplicate the only content or leave the plan body-less.
        should_frontload = hook_mode == "repeat" or bool(selected_group_ids - hook_group_ids)
        if should_frontload:
            if hook_mode == "move":
                moved_group_ids.update(hook_group_ids)
            hook_ranges = hook.get("source_ranges") or [hook.get("source_range") or {}]
            for sequence, source_range in enumerate(hook_ranges, 1):
                result.append({
                    "occurrence_id": f"O-HOOK-{sequence:03d}", "role": "hook",
                    "group_ids": list(hook["group_ids"]),
                    "source_start": float(source_range["start"]),
                    "source_end": float(source_range["end"]), "speed": speed,
                })
    result.extend(_body_occurrences(groups, speed, excluded_group_ids=moved_group_ids))
    if not any(row["role"] == "body" for row in result):
        raise InteractionSecondPassError("二次剪辑没有可用正文")
    return result


def timeline_mapping(occurrences: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cursor = 0.0
    result = []
    for occurrence in occurrences:
        start, end = float(occurrence["source_start"]), float(occurrence["source_end"])
        speed = float(occurrence["speed"])
        duration = (end - start) / speed
        result.append({
            "occurrence_id": occurrence["occurrence_id"], "role": occurrence["role"],
            "group_ids": list(occurrence.get("group_ids") or []),
            "source_start": round(start, 6), "source_end": round(end, 6),
            "output_start": round(cursor, 6), "output_end": round(cursor + duration, 6),
            "speed": round(speed, 6),
        })
        cursor += duration
    return result


def _explode_subtitle_token(token: str, *, max_chars: int) -> list[str]:
    """Deprecated: the one implementation now lives in ``material_interaction_units``.

    Kept as a thin alias so older callers and probes keep working; the phrase
    splitter is shared with the spoken-unit layer on purpose, because two copies
    would drift and the captions would silently disagree with the grouping.
    """
    return _shared_split_phrases(token, max_chars=max_chars)


def _split_subtitle_phrases(text: str, *, max_chars: int) -> list[str]:
    """Split an utterance into caption-sized phrases (delegates to the shared impl)."""
    return _shared_split_phrases(text, max_chars=max_chars)


def _join_cue_text(left: str, right: str) -> str:
    """Glue two phrases back together without inventing a stray space."""
    if not left:
        return right
    if not right:
        return left
    if left[-1] in SUBTITLE_PHRASE_PUNCTUATION:
        return left + right
    return f"{left} {right}"


def _group_text(group: list[dict[str, Any]]) -> str:
    text = ""
    for window in group:
        text = _join_cue_text(text, str(window["text"]))
    return text


def _group_duration(group: list[dict[str, Any]]) -> float:
    if not group:
        return 0.0
    return float(group[-1]["out_end"]) - float(group[0]["out_start"])


def _rebalance_subtitle_groups(groups: list[list[dict[str, Any]]], *,
                               max_chars: int, min_seconds: float) -> None:
    """Slide phrase boundaries so no cue stays below ``min_seconds``.

    The greedy merge can close a cue right before a very short phrase, leaving a
    caption that would flash for a fraction of a second.  Merging the two blindly
    would blow the character limit, so the boundary is walked backwards one
    phrase at a time, keeping both neighbours within their limits.  Phrases only
    ever move to the right, so the loop always terminates.
    """
    for index in range(1, len(groups)):
        while _group_duration(groups[index]) < min_seconds and len(groups[index - 1]) > 1:
            shortened = groups[index - 1][:-1]
            if _group_duration(shortened) < min_seconds:
                break
            grown = [groups[index - 1][-1], *groups[index]]
            if len(_group_text(grown)) > max_chars:
                break
            groups[index].insert(0, groups[index - 1].pop())
    # The opening cue has no left neighbour to borrow from; folding it into the
    # next cue is the only fix left, and a slightly long line beats a flash.
    if len(groups) >= 2 and _group_duration(groups[0]) < min_seconds:
        groups[1][0:0] = groups.pop(0)


def _merge_subtitle_windows(
    windows: list[dict[str, Any]], *, occurrence: dict[str, Any], utterance_id: str,
    max_chars: int, max_seconds: float, min_seconds: float,
) -> list[dict[str, Any]]:
    """Clip owned phrase windows to the occurrence, then merge into cues."""
    occurrence_start = float(occurrence["source_start"])
    occurrence_end = float(occurrence["source_end"])
    speed = float(occurrence["speed"]) or 1.0
    output_start = float(occurrence["output_start"])
    projected: list[dict[str, Any]] = []
    for window in windows:
        start = max(float(window["source_start"]), occurrence_start)
        end = min(float(window["source_end"]), occurrence_end)
        if end - start <= 1e-9:
            continue
        projected.append({
            "text": str(window["text"]),
            "out_start": output_start + (start - occurrence_start) / speed,
            "out_end": output_start + (end - occurrence_start) / speed,
        })
    if not projected:
        return []
    projected.sort(key=lambda row: (row["out_start"], row["out_end"]))
    groups: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for window in projected:
        if not current:
            current = [window]
            continue
        grown = [*current, window]
        fits = (len(_group_text(grown)) <= max_chars
                and window["out_end"] - current[0]["out_start"] <= max_seconds)
        if fits or _group_duration(current) < min_seconds:
            # ``fits`` respects both limits; the second clause stops a too-short
            # cue from closing before it has absorbed enough to be readable.
            current = grown
            continue
        groups.append(current)
        current = [window]
    if current:
        groups.append(current)
    _rebalance_subtitle_groups(groups, max_chars=max_chars, min_seconds=min_seconds)
    result: list[dict[str, Any]] = []
    for index, group in enumerate(groups, 1):
        result.append({
            "cue_id": f"{occurrence['occurrence_id']}:{utterance_id}:{index:03d}",
            "occurrence_id": occurrence["occurrence_id"], "role": occurrence["role"],
            "utterance_id": utterance_id, "text": _group_text(group),
            "output_start": round(float(group[0]["out_start"]), 6),
            "output_end": round(float(group[-1]["out_end"]), 6),
        })
    return result


def _utterance_phrase_windows(utterance: dict[str, Any] | None, *,
                              max_chars: int) -> list[dict[str, Any]]:
    """Estimated **source-timeline** windows for one utterance's phrases.

    Tencent ASR returns sentence-level timestamps only, so a phrase's time is a
    character-proportional estimate over the utterance's own span -- there is no
    word-level timing to borrow (see ``docs/handoff/DECISIONS.md``).  This is an
    estimate, not precise synchronisation, and captions inherit its margin.

    Implementation lives in :mod:`backlot.material_interaction_units` so the
    caption layer and the spoken-unit layer cannot drift apart.
    """
    return _shared_phrase_windows(utterance, max_chars=max_chars)


def _owned_occurrence_indexes(window: dict[str, Any], mapping: list[dict[str, Any]]) -> list[int]:
    """Occurrences that may caption ``window``.

    A phrase belongs to every occurrence that plays **most of it** (>= 50%).  A
    phrase that straddles a pause cut is played mostly on one side, so it is
    captioned once there instead of being echoed on both sides -- the defect that
    stamped the whole utterance everywhere.  A genuine ``repeat`` hook plays the
    phrase in full twice, so both plays keep their caption.  When a cut splits a
    phrase almost evenly no side reaches the threshold, and it is kept once, in
    whichever occurrence plays the largest share, so no text vanishes.
    """
    start, end = float(window["source_start"]), float(window["source_end"])
    length = end - start
    if length <= 0:
        return []
    overlaps: list[tuple[int, float]] = []
    for index, occurrence in enumerate(mapping):
        overlap = min(end, float(occurrence["source_end"])) - max(start, float(occurrence["source_start"]))
        if overlap > 1e-9:
            overlaps.append((index, overlap))
    if not overlaps:
        return []
    owners = [index for index, overlap in overlaps if overlap >= 0.5 * length - 1e-9]
    if owners:
        return owners
    best_index, best_overlap = overlaps[0]
    for index, overlap in overlaps[1:]:
        if overlap > best_overlap + 1e-9:
            best_index, best_overlap = index, overlap
    return [best_index]


def _caption_utterance_ids(group: dict[str, Any] | None, by_utterance: dict[str, dict[str, Any]],
                           unit_lookup: dict[str, dict[str, Any]]) -> list[str]:
    """Bridge a group's atoms back to the ASR utterances that own its captions.

    v6 groups *spoken units* while captions are still derived from the raw ASR
    utterances (one cue per spoken phrase).  Re-deriving captions from unit text
    instead would turn a 6-second unit into one unreadable long line, so the two
    id spaces are bridged here.
    """
    result: list[str] = []
    for value in (group or {}).get("utterance_ids") or []:
        identifier = str(value)
        if identifier in by_utterance:
            result.append(identifier)
            continue
        unit = unit_lookup.get(identifier)
        if not unit:
            continue
        owner = str(unit.get("utterance_id") or "")
        if owner in by_utterance:
            result.append(owner)
            continue
        start, end = float(unit.get("start") or 0.0), float(unit.get("end") or 0.0)
        for row in by_utterance.values():
            if min(end, float(row["end"])) - max(start, float(row["start"])) > 1e-9:
                result.append(str(row["id"]))
    return list(dict.fromkeys(result))


def subtitle_cues(plan: dict[str, Any], mapping: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One cue per spoken phrase, on the **output** timeline.

    Tencent ASR gives sentence-level timestamps only -- there is no word-level
    timing to borrow (see ``docs/handoff/DECISIONS.md``) -- so a phrase's time is
    *estimated* by spreading its own atom's span across its characters in
    proportion.  That is an estimate, not precise synchronisation.

    **Which atom** matters, and v7 changed it.  Up to v6 the atom was the whole
    ASR block, which on long material is 60 seconds wide: a phrase that actually
    happens at 1050 s could be estimated at 1056 s, so burned-in captions lagged
    visibly behind the speech.  From v7 the atom is the *spoken unit* — a 2–6
    second VAD-aligned run — so the estimate inherits that run's own bounds and
    lands within a few hundred milliseconds (``UNIT_CAPTION_AWARE_VERSIONS``).

    The defect this all replaces: a whole 198-character utterance was stamped, in
    full, into every occurrence it touched, so a viewer saw the same wall of text
    four times.  Here each phrase is owned by the one occurrence that plays most
    of it, and neighbouring phrases are merged back into readable one-line cues
    targeting ``subtitle_max_chars`` / ``subtitle_max_seconds``.  Those are
    targets, not hard ceilings: a caption that would otherwise flash for a single
    frame may run a few characters long (see ``_rebalance_subtitle_groups``).
    """
    options = normalize_options(plan.get("options"), legacy=plan.get("version") == LEGACY_VERSION)
    max_chars = int(options["subtitle_max_chars"])
    max_seconds = float(options["subtitle_max_seconds"])
    min_seconds = float(options["subtitle_min_seconds"])
    by_group = {row["id"]: row for row in (plan.get("story") or {}).get("groups") or []}
    by_utterance = {row["id"]: row for row in plan.get("utterances") or []}
    unit_lookup = {str(row.get("id")): row for row in plan.get("spoken_units") or []
                   if isinstance(row, dict) and row.get("id")}
    # v7 captions address the spoken units directly; older plans must keep the
    # utterance-derived captions they were reviewed with (N7).
    unit_captions = str(plan.get("version") or "") in UNIT_CAPTION_AWARE_VERSIONS and bool(unit_lookup)
    by_atom = unit_lookup if unit_captions else by_utterance
    if unit_captions:
        group_owners = {
            group_id: [str(value) for value in row.get("utterance_ids") or [] if str(value) in by_atom]
            for group_id, row in by_group.items()
        }
    else:
        group_owners = {group_id: _caption_utterance_ids(row, by_utterance, unit_lookup)
                        for group_id, row in by_group.items()}
    output_duration = float(mapping[-1]["output_end"]) if mapping else 0.0

    referenced: list[str] = []
    for occurrence in mapping:
        for group_id in occurrence.get("group_ids") or []:
            referenced.extend(group_owners.get(group_id) or [])
    windows_by_utterance: dict[str, list[dict[str, Any]]] = {}
    owners_by_utterance: dict[str, list[list[int]]] = {}
    for utterance_id in dict.fromkeys(referenced):
        windows = _utterance_phrase_windows(by_atom.get(utterance_id), max_chars=max_chars)
        windows_by_utterance[utterance_id] = windows
        owners_by_utterance[utterance_id] = [_owned_occurrence_indexes(window, mapping) for window in windows]

    result: list[dict[str, Any]] = []
    for index, occurrence in enumerate(mapping):
        utterance_ids: list[str] = []
        for group_id in occurrence.get("group_ids") or []:
            utterance_ids.extend(group_owners.get(group_id) or [])
        occurrence_cues: list[dict[str, Any]] = []
        for utterance_id in dict.fromkeys(utterance_ids):
            windows = windows_by_utterance.get(utterance_id) or []
            owners = owners_by_utterance.get(utterance_id) or []
            owned = [window for window, owned_by in zip(windows, owners) if index in owned_by]
            occurrence_cues.extend(_merge_subtitle_windows(
                owned, occurrence=occurrence, utterance_id=utterance_id,
                max_chars=max_chars, max_seconds=max_seconds, min_seconds=min_seconds,
            ))
        # Occurrences are ordered by output time; ordering the cues inside one
        # keeps the whole list monotonic even if a group lists its utterances out
        # of source order.
        occurrence_cues.sort(key=lambda row: (row["output_start"], row["output_end"], row["cue_id"]))
        result.extend(occurrence_cues)
    # Defensive clamp: a caption must never fall outside the film.
    for cue in result:
        cue["output_start"] = round(min(max(cue["output_start"], 0.0), output_duration), 6)
        cue["output_end"] = round(min(max(cue["output_end"], 0.0), output_duration), 6)
    return result


def _unique_source_duration(occurrences: list[dict[str, Any]], *, role: str = "body") -> float:
    ranges = sorted((float(row["source_start"]), float(row["source_end"])) for row in occurrences if row["role"] == role)
    merged: list[list[float]] = []
    for start, end in ranges:
        if not merged or start > merged[-1][1]:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    return sum(end - start for start, end in merged)


def _cross_role_overlap(occurrences: list[dict[str, Any]]) -> float:
    hooks = [(float(row["source_start"]), float(row["source_end"])) for row in occurrences if row["role"] == "hook"]
    bodies = [(float(row["source_start"]), float(row["source_end"])) for row in occurrences if row["role"] == "body"]
    return sum(max(0.0, min(hook_end, body_end) - max(hook_start, body_start))
               for hook_start, hook_end in hooks for body_start, body_end in bodies)


def _content_qa(result: dict[str, Any]) -> dict[str, Any]:
    output_duration = float(result.get("output_duration") or 0)
    maximum = float((result.get("options") or {}).get("target_max_seconds") or 24 * 3600)
    target_ok = output_duration <= maximum
    hook_mode = str((result.get("options") or {}).get("hook_mode") or "repeat")
    repeated = float(result.get("repeated_source_seconds") or 0)
    repetition_ok = repeated <= .001 or hook_mode == "repeat"
    checks = [
        {"name": "target_duration", "ok": target_ok,
         "detail": "输出未超过目标最长时长" if target_ok else "输出超过目标最长时长，请调整语义组、倍速或目标范围"},
        {"name": "source_repetition", "ok": repetition_ok,
         "detail": ("未发现非预期的源片段重复" if repetition_ok else f"发现 {repeated:.3f} 秒非预期源片段重复")},
    ]
    return {"status": "passed" if all(row["ok"] for row in checks) else "needs_adjustment", "checks": checks}


def _output_seconds(occurrences: list[dict[str, Any]]) -> float:
    return round(sum((float(row["source_end"]) - float(row["source_start"])) / float(row["speed"])
                     for row in occurrences), 6)


def _rebuild(plan: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(plan)
    allowed = result.get("allowed_source_ranges") or []
    options = result.get("options") or {}
    occurrences = build_occurrences(result)
    _validate_occurrences(occurrences, allowed)
    # The semantic selection, before any pause cut.  Which source the semantic
    # layer decided to keep must not change just because the dead air inside it
    # was later tightened, otherwise "removed by story" would double-count the
    # pauses and the parent contract would look violated.
    semantic_occurrences = [dict(row) for row in occurrences]
    version = result.get("version")
    pause_aware = version in PAUSE_AWARE_VERSIONS
    action_aware = version in ACTION_AWARE_VERSIONS
    speed_segment_aware = version in SPEED_SEGMENT_AWARE_VERSIONS
    pause_handling = str(options.get("pause_handling") or PAUSE_HANDLING_DEFAULT)
    compress_enabled = (
        pause_aware and pause_handling == PAUSE_HANDLING_DEFAULT
        and options.get("compress_pauses") is not False
    )
    degradations: list[str] = []
    trims: list[dict[str, Any]] = []
    speed_segments: list[dict[str, Any]] = []
    notes: list[str] = []
    budget: float | None = None
    untrimmed_output = _output_seconds(occurrences)
    if compress_enabled:
        silences, refusals = _normalized_silences(result.get("pause_evidence"))
        # ``any`` cuts VAD gaps, so it does not need the probe at all: gating on
        # ``silences`` would silently disable it exactly when the probe found
        # nothing (which is the noisy material it exists for), and reporting a
        # missing probe as a degradation would be a false alarm.
        needs_probe = version not in GAP_TARGET_AWARE_VERSIONS or \
            str(options.get("gap_policy") or GAP_POLICY_DEFAULT) == "quiet"
        if needs_probe:
            degradations.extend(refusals)
        if silences or not needs_probe:
            speech = _normalized_speech(result.get("speech_ranges"))
            minimum_output = float(options.get("target_min_seconds") or 0)
            # Compression tightens the clip; it must not shorten it past the
            # duration the user asked for.  When the selection is already
            # shorter than that there is no floor left to protect.
            budget = max(0.0, untrimmed_output - minimum_output) if untrimmed_output > minimum_output else None
            trims = _pause_trims(occurrences, result.get("pause_evidence"), speech, options,
                                 budget_seconds=budget,
                                 gap_target=version in GAP_TARGET_AWARE_VERSIONS)
            _validate_pause_trims(trims, allowed, speech)
            occurrences = _apply_pause_trims(occurrences, trims)
            _validate_occurrences(occurrences, allowed)
    elif speed_segment_aware and pause_handling == "speed_up":
        # Fast-forward instead of delete: the waiting passage stays in the
        # picture (no jump cut) and audio is the master clock, so the output
        # duration is exactly Σ(source_end - source_start) / segment_speed.
        silences, refusals = _normalized_silences(result.get("pause_evidence"))
        degradations.extend(refusals)
        if silences:
            speech = _normalized_speech(result.get("speech_ranges"))
            speed_segments = _pause_speed_segments(occurrences, result.get("pause_evidence"), speech, options)
            _validate_speed_segments(speed_segments, allowed, speech, options)
            occurrences = _apply_speed_segments(occurrences, speed_segments, options)
            _validate_occurrences(occurrences, allowed, allow_speed_range=True)
    if action_aware:
        for row in occurrences:
            if not row.get("actions"):
                row["actions"] = _speed_actions(row["speed"], "preset")
        result["speed_segments"] = speed_segments
    result["pause_trims"] = trims
    result["occurrences"] = occurrences
    result["timeline_mapping"] = timeline_mapping(occurrences)
    result["subtitle_cues"] = subtitle_cues(result, result["timeline_mapping"])
    result["body_source_duration"] = round(_unique_source_duration(semantic_occurrences), 3)
    result["hook_source_duration"] = round(sum(row["source_end"] - row["source_start"] for row in semantic_occurrences if row["role"] == "hook"), 3)
    result["repeated_source_seconds"] = round(_cross_role_overlap(semantic_occurrences), 3)
    result["played_source_seconds"] = round(sum(float(row["source_end"]) - float(row["source_start"]) for row in occurrences), 3)
    result["removed_by_pause_seconds"] = round(sum(float(row["removed_seconds"]) for row in trims), 3)
    result["output_duration"] = round(sum(row["output_end"] - row["output_start"] for row in result["timeline_mapping"]), 3)
    result["removed_source_seconds"] = round(max(0.0, float(result["source_duration"]) - result["body_source_duration"]), 3)
    minimum = float(options.get("target_min_seconds") or 0)
    maximum = float(options.get("target_max_seconds") or 24 * 3600)
    if trims:
        notes.append(f"已压缩 {len(trims)} 处对话间停顿，共缩短 {result['removed_by_pause_seconds']:.1f} 秒")
    if trims and untrimmed_output >= minimum > result["output_duration"]:
        notes.append("压缩后已短于目标最短时长；如需更长请调小可压缩停顿下限，或放宽目标时长")
    if speed_segments:
        pause_speed = float(options.get("pause_speed") or PAUSE_SPEED_DEFAULT)
        sped = sum((float(row["source_end"]) - float(row["source_start"])) * (1.0 - 1.0 / float(row["speed"]))
                   for row in speed_segments)
        notes.append(f"已将 {len(speed_segments)} 处等待段快放 {pause_speed:g}×，共缩短 {round(sped, 1):.1f} 秒，画面不跳切")
    compression = {
        "enabled": compress_enabled,
        "trim_count": len(trims), "removed_seconds": result["removed_by_pause_seconds"],
        "scope": options.get("pause_scope"), "min_pause_seconds": options.get("pause_min_seconds"),
        "target_gap_seconds": options.get("pause_target_gap_seconds"),
        "guard_seconds": options.get("pause_guard_seconds"),
        "budget_seconds": None if budget is None else round(budget, 3),
        "untrimmed_output_seconds": untrimmed_output, "notes": notes,
    }
    if speed_segment_aware:
        # v4-only keys; older plans must keep the byte-identical V3 compression
        # dict or every historical plan would fail its own reconstruction check.
        compression["pause_handling"] = pause_handling
        compression["speed_segment_count"] = len(speed_segments)
    result["compression"] = compression
    result["degradations"] = degradations
    result["target_duration_status"] = "within_target" if minimum <= result["output_duration"] <= maximum else "outside_target"
    result["content_qa"] = _content_qa(result)
    return result


def _frozen_pause_evidence(pause_evidence: Any) -> dict[str, Any]:
    """Keep only what the plan layer needs so plans stay small and portable.

    "No probe was run at all" and "the probe ran and failed" are different
    situations and must not collapse into one message: the first means the
    caller forgot the evidence, the second means the environment is degraded.
    """
    if not isinstance(pause_evidence, dict) or not pause_evidence:
        return {"version": "", "status": "", "identity": None, "source_fingerprint": "", "silences": []}
    evidence = pause_evidence
    rows = []
    for raw in evidence.get("silences") or []:
        if not isinstance(raw, dict):
            continue
        try:
            start, end = round(float(raw["start"]), 6), round(float(raw["end"]), 6)
        except (KeyError, TypeError, ValueError):
            continue
        if end > start:
            rows.append({"start": start, "end": end})
    rows.sort(key=lambda row: (row["start"], row["end"]))
    identity = evidence.get("identity")
    return {
        "version": str(evidence.get("version") or "")[:60],
        "status": str(evidence.get("status") or "unavailable")[:40],
        "identity": deepcopy(identity) if isinstance(identity, dict) else None,
        "source_fingerprint": str(evidence.get("source_fingerprint") or "")[:128],
        "silences": rows,
    }


def parent_source_seconds(parent_plan: dict[str, Any]) -> float:
    """How much source the first-pass candidate allows, in seconds."""
    total = 0.0
    for row in parent_plan.get("keep_ranges") or []:
        try:
            total += float(row["end"]) - float(row["start"])
        except (KeyError, TypeError, ValueError):
            continue
    return round(total, 3)


def pause_probe_min_silence(options: dict[str, Any] | None) -> float:
    """The probe floor a preset needs, so the prober and the plan agree.

    Tuning one without the other is what made "压缩对话间停顿" look implemented
    but remove nothing: a floor far above the plan layer's target can never yield
    a cut that also survives it.
    """
    preset = str((options or {}).get("pause_preset") or PAUSE_PRESET_DEFAULT)
    return float(PAUSE_PRESETS.get(preset, PAUSE_PRESETS[PAUSE_PRESET_DEFAULT])["probe_min_silence_seconds"])


def pause_probe_bridge_seconds(options: dict[str, Any] | None) -> float:
    """How long a loud excursion inside a quiet stretch may be, and be ignored.

    A street recording's dialogue gaps usually contain one or two loud frames; the
    probe needs contiguous quiet frames, so without bridging a 1.5 s gap can be
    invisible.  See :func:`material_pause_evidence.bridge_intervals`.
    """
    preset = str((options or {}).get("pause_preset") or PAUSE_PRESET_DEFAULT)
    return float(PAUSE_PRESETS.get(preset, PAUSE_PRESETS[PAUSE_PRESET_DEFAULT])["probe_bridge_seconds"])


def pause_survivor_seconds(options: dict[str, Any] | None) -> float:
    """The gap that will actually survive between two adjacent lines (v7 geometry)."""
    configured = options or {}
    guard = float(configured.get("pause_guard_seconds") or PAUSE_GUARD_DEFAULT)
    gap = float(configured.get("pause_target_gap_seconds") or PAUSE_TARGET_GAP_DEFAULT)
    return round(2 * _pause_keep_seconds(guard, gap, gap_target=True), 3)


def build_second_pass_plan(
    *,
    parent_plan: dict[str, Any],
    story: dict[str, Any],
    utterances: list[dict[str, Any]],
    options: dict[str, Any],
    story_identity: dict[str, Any],
    pause_evidence: dict[str, Any] | None = None,
    speech_ranges: Any = None,
    units: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    allowed = deepcopy(parent_plan.get("keep_ranges") or [])
    options = normalize_options(options, parent_seconds=parent_source_seconds(parent_plan))
    parent_id = str(parent_plan.get("plan_id") or "")
    source = deepcopy(parent_plan.get("source") or {})
    if not parent_id.startswith("IEP-") or not source.get("fingerprint"):
        raise InteractionSecondPassError("父候选合同不完整")
    frozen = {
        "version": VERSION,
        "parent": {
            "plan_id": parent_id, "version": parent_plan.get("version"),
            "revision": int(parent_plan.get("revision") or 0),
            "preview_signature": ((parent_plan.get("preview") or {}).get("signature") if isinstance(parent_plan.get("preview"), dict) else None),
        },
        "source": source,
        "index_signature": parent_plan.get("index_signature"),
        "review_revision": parent_plan.get("review_revision"),
        "event_id": parent_plan.get("event_id"), "group_id": parent_plan.get("group_id"),
        "allowed_source_ranges": allowed,
        "source_duration": parent_source_seconds(parent_plan),
        "options": options,
        "story_identity": deepcopy(story_identity),
        "story": deepcopy(story),
        "utterances": deepcopy(utterances),
        # v6: the phrase-level atoms the story layer grouped.  Frozen input like
        # ``utterances``, and bound into the plan id, so a change of unit
        # derivation can never silently reuse an older plan.
        "unit_source": "vad_aligned" if units else "asr_utterance",
        "spoken_units": deepcopy(units) if units else [],
        "unit_signature": units_signature(units) if units else "",
        # The two pieces of evidence every pause cut is validated against.  They
        # are frozen into the plan so the derivation stays deterministic and a
        # re-render can never disagree with what the user reviewed.
        "pause_evidence": _frozen_pause_evidence(pause_evidence),
        "speech_ranges": _normalized_speech(speech_ranges),
    }
    plan_id = "ISP-" + _digest(frozen)[:16]
    plan = {
        **frozen, "plan_id": plan_id, "revision": 0, "status": "pending_review",
        "selected_hook_id": story.get("recommended_hook_id") if options["hook_enabled"] else None,
        "warnings": list(story.get("warnings") or []), "history": [],
        "qa": {"status": "not_rendered"}, "preview": None,
        "created_at": _now(), "updated_at": _now(),
    }
    plan = _rebuild(plan)
    validate_second_pass_plan(plan)
    return plan


def validate_second_pass_plan(plan: dict[str, Any]) -> None:
    if plan.get("version") not in SUPPORTED_VERSIONS:
        raise InteractionSecondPassError("二次剪辑清单版本不受支持")
    plan_id = str(plan.get("plan_id") or "")
    if not plan_id.startswith("ISP-") or not plan_id[4:].isalnum():
        raise InteractionSecondPassError("二次剪辑编号无效")
    if plan.get("status") not in ALLOWED_STATUSES:
        raise InteractionSecondPassError("二次剪辑状态无效")
    legacy = plan.get("version") == LEGACY_VERSION
    normalize_options(plan.get("options"), legacy=legacy)
    allowed = plan.get("allowed_source_ranges")
    groups = (plan.get("story") or {}).get("groups")
    if not isinstance(allowed, list) or not allowed or not isinstance(groups, list) or not groups:
        raise InteractionSecondPassError("二次剪辑缺少父范围或语义组")
    group_ids = [str(row.get("id") or "") for row in groups]
    if any(not value for value in group_ids) or len(group_ids) != len(set(group_ids)):
        raise InteractionSecondPassError("二次剪辑语义组编号无效")
    for group in groups:
        ranges = group.get("source_ranges")
        if not isinstance(ranges, list) or not ranges:
            raise InteractionSecondPassError("二次剪辑语义组缺少父范围内的播放片段")
        previous_end = -1.0
        normalized_ranges = []
        for source_range in ranges:
            if not isinstance(source_range, dict):
                raise InteractionSecondPassError("二次剪辑语义组片段格式无效")
            start = _number(source_range.get("start"), minimum=0, maximum=24 * 3600, label="语义组开始")
            end = _number(source_range.get("end"), minimum=0, maximum=24 * 3600, label="语义组结束")
            if end <= start or start < previous_end or not _range_within_allowed(start, end, allowed):
                raise InteractionSecondPassError("二次剪辑语义组片段越出父候选范围或顺序无效")
            normalized_ranges.append({"start": round(start, 3), "end": round(end, 3)})
            previous_end = end
        expected_envelope = {
            "start": normalized_ranges[0]["start"],
            "end": normalized_ranges[-1]["end"],
        }
        if group.get("source_range") != expected_envelope:
            raise InteractionSecondPassError("二次剪辑语义组展示范围与播放片段不一致")
    _validate_dependencies(groups)
    derived_keys = {
        "occurrences", "timeline_mapping", "subtitle_cues", "body_source_duration", "hook_source_duration",
        "output_duration", "removed_source_seconds", "target_duration_status", "repeated_source_seconds", "content_qa",
        "pause_trims", "played_source_seconds", "removed_by_pause_seconds", "compression", "degradations",
        "speed_segments",
    }
    expected = _rebuild({key: deepcopy(value) for key, value in plan.items() if key not in derived_keys})
    compare_keys = ("occurrences", "timeline_mapping", "body_source_duration", "hook_source_duration",
                    "output_duration", "removed_source_seconds", "target_duration_status")
    if not legacy:
        compare_keys += ("repeated_source_seconds", "content_qa")
    if plan.get("version") in PAUSE_AWARE_VERSIONS:
        # A pause cut that survives validation must be reproducible from the
        # frozen evidence alone; otherwise the reviewed plan and the rendered
        # file could differ.
        compare_keys += ("pause_trims", "played_source_seconds", "removed_by_pause_seconds", "compression",
                         "degradations")
    if plan.get("version") in SPEED_SEGMENT_AWARE_VERSIONS:
        # v4+ only: ``actions`` ride inside ``occurrences`` (already compared),
        # and ``speed_segments`` is compared here.  Never demanded from v1/v2/v3.
        compare_keys += ("speed_segments",)
    if plan.get("version") in SUBTITLE_SENTENCE_AWARE_VERSIONS:
        # v5+ only: the captions are re-derived from the frozen utterances.  A
        # v1-v4 plan carries whatever the older build wrote (one cue per whole
        # utterance), so demanding the new shape from it would make every
        # historical plan unreadable (N7); those are read back verbatim.
        compare_keys += ("subtitle_cues",)
    for key in compare_keys:
        if plan.get(key) != expected.get(key):
            raise InteractionSecondPassError(f"二次剪辑{key}与语义选择不一致")
    if not isinstance(plan.get("history"), list) or len(plan["history"]) > 50:
        raise InteractionSecondPassError("二次剪辑撤销记录无效")


def plan_path(root: Path, plan_id: str) -> Path:
    if not str(plan_id).startswith("ISP-") or not str(plan_id)[4:].isalnum():
        raise InteractionSecondPassError("二次剪辑编号无效")
    return root.resolve() / str(plan_id) / PLAN_FILENAME


def write_second_pass_plan(path: Path, plan: dict[str, Any]) -> dict[str, Any]:
    validate_second_pass_plan(plan)
    _atomic_write(path, plan)
    return plan


def read_second_pass_plan(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise InteractionSecondPassError("二次剪辑方案不存在")
    try:
        plan = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InteractionSecondPassError("二次剪辑方案损坏") from exc
    validate_second_pass_plan(plan)
    return plan


def list_second_pass_plans(root: Path) -> list[dict[str, Any]]:
    if not root.is_dir():
        return []
    result = []
    for path in root.glob(f"ISP-*/{PLAN_FILENAME}"):
        try:
            result.append(read_second_pass_plan(path))
        except InteractionSecondPassError:
            continue
    return sorted(result, key=lambda row: str(row.get("updated_at") or ""), reverse=True)


def _snapshot(plan: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "story", "selected_hook_id", "options", "warnings", "occurrences", "timeline_mapping",
        "subtitle_cues", "body_source_duration", "hook_source_duration", "output_duration",
        "removed_source_seconds", "target_duration_status", "repeated_source_seconds", "content_qa", "status", "qa", "preview",
    )
    return {key: deepcopy(plan.get(key)) for key in keys}


def apply_second_pass_action(
    plan: dict[str, Any],
    *,
    action: str,
    expected_revision: int,
    group_states: dict[str, bool] | None = None,
    locked_states: dict[str, bool] | None = None,
    speed: float | None = None,
    hook_candidate_id: str | None = None,
    hook_mode: str | None = None,
) -> dict[str, Any]:
    validate_second_pass_plan(plan)
    if int(plan.get("revision", -1)) != int(expected_revision):
        raise InteractionSecondPassConflict("二次剪辑方案已被其他操作更新，请刷新后重试")
    if plan.get("status") in {"approved", "rejected"}:
        raise InteractionSecondPassError("已确认或弃用的二次剪辑不可继续改写")
    result = deepcopy(plan)
    if action == "undo":
        history = result.get("history") or []
        if not history:
            raise InteractionSecondPassError("没有可以撤销的二次剪辑操作")
        snapshot = history.pop()
        for key, value in snapshot.items():
            result[key] = deepcopy(value)
        result["history"] = history
    elif action == "save_edits":
        if not any((isinstance(group_states, dict), isinstance(locked_states, dict), speed is not None,
                    hook_candidate_id is not None, hook_mode is not None)):
            raise InteractionSecondPassError("没有需要保存的二次剪辑调整")
        result.setdefault("history", []).append(_snapshot(result))
        result["history"] = result["history"][-50:]
        groups = (result.get("story") or {}).get("groups") or []
        by_id = {str(row["id"]): row for row in groups}
        group_states = group_states if isinstance(group_states, dict) else {}
        locked_states = locked_states if isinstance(locked_states, dict) else {}
        if any(str(key) not in by_id or not isinstance(value, bool) for key, value in {**group_states, **locked_states}.items()):
            raise InteractionSecondPassError("二次剪辑调整包含未知对话组")
        future_locks = {group_id: bool(locked_states.get(group_id, row.get("locked"))) for group_id, row in by_id.items()}
        for group_id, selected in group_states.items():
            row = by_id[str(group_id)]
            if row.get("locked") is True and future_locks[str(group_id)] and row.get("selected") is not selected:
                raise InteractionSecondPassError(f"对话组“{row.get('summary') or group_id}”已锁定，请先解锁")
            row["selected"] = selected
            row["decision"] = "keep" if selected else "drop"
            row["reason"] = "用户在二次剪辑审核中选择保留" if selected else "用户在二次剪辑审核中选择删除"
        for group_id, locked in locked_states.items():
            by_id[str(group_id)]["locked"] = locked
        _validate_dependencies(groups)
        if speed is not None:
            result["options"]["speed"] = normalize_options({**result["options"], "speed": speed})["speed"]
        if hook_mode is not None:
            result["options"] = normalize_options({**result["options"], "hook_mode": hook_mode})
            if result["options"]["hook_mode"] == "none":
                result["selected_hook_id"] = None
        if hook_candidate_id is not None:
            normalized_hook = None if hook_candidate_id in {"", "none", "__none__"} else str(hook_candidate_id)
            if normalized_hook and not any(row.get("id") == normalized_hook for row in (result.get("story") or {}).get("hook_candidates") or []):
                raise InteractionSecondPassError("选中的精彩开场不存在")
            result["selected_hook_id"] = normalized_hook
        current_hook = next((row for row in (result.get("story") or {}).get("hook_candidates") or [] if row.get("id") == result.get("selected_hook_id")), None)
        if current_hook and any(by_id[group_id].get("selected") is not True for group_id in current_hook.get("group_ids") or []):
            result["selected_hook_id"] = None
            result.setdefault("warnings", []).append("当前精彩开场包含已删除的对话组，已关闭精彩前置")
        result = _rebuild(result)
        result["status"] = "pending_review"
        result["qa"] = {"status": "stale"}
        if isinstance(result.get("preview"), dict):
            result["preview"] = {**result["preview"], "stale": True}
    elif action in {"approve", "reject"}:
        legacy = result.get("version") == LEGACY_VERSION
        if action == "approve" and (result.get("qa") or {}).get("status") != "passed":
            raise InteractionSecondPassError("二次剪辑预览尚未通过媒体 QA，不能确认入库")
        if action == "approve" and not legacy and (result.get("content_qa") or {}).get("status") != "passed":
            raise InteractionSecondPassError("二次剪辑仍超出目标或存在非预期重复，请调整后再确认入库")
        result.setdefault("history", []).append(_snapshot(result))
        result["history"] = result["history"][-50:]
        result["status"] = "approved" if action == "approve" else "rejected"
    else:
        raise InteractionSecondPassError("不支持的二次剪辑操作")
    result["revision"] = int(result["revision"]) + 1
    result["updated_at"] = _now()
    validate_second_pass_plan(result)
    return result


def attach_render_result(
    plan: dict[str, Any], manifest: dict[str, Any], *, expected_revision: int, preview_path: str,
) -> dict[str, Any]:
    validate_second_pass_plan(plan)
    if int(plan.get("revision", -1)) != int(expected_revision):
        raise InteractionSecondPassConflict("二次剪辑方案已变化，旧预览不会覆盖新方案")
    if manifest.get("plan_id") != plan.get("plan_id") or int(manifest.get("plan_revision", -1)) != int(plan["revision"]):
        raise InteractionSecondPassError("二次剪辑预览不属于当前方案")
    if (manifest.get("qa") or {}).get("status") != "passed" or not manifest.get("signature"):
        raise InteractionSecondPassError("二次剪辑预览尚未通过 QA")
    result = deepcopy(plan)
    result["qa"] = deepcopy(manifest["qa"])
    # Render-time degradations (e.g. an unavailable subtitle filter) land on the
    # plan so the user sees them next to the plan they reviewed instead of only
    # inside a manifest file.
    result["render_degradations"] = [str(value) for value in (manifest.get("degradations") or []) if value]
    result["subtitles"] = deepcopy(manifest.get("subtitles") or {})
    result["preview"] = {
        "path": str(preview_path), "signature": str(manifest["signature"]),
        "output_duration": manifest.get("output_duration"), "stale": False,
    }
    result["status"] = "pending_review"
    result["revision"] = int(result["revision"]) + 1
    result["updated_at"] = _now()
    validate_second_pass_plan(result)
    return result


def source_time_to_outputs(plan: dict[str, Any], seconds: float) -> list[dict[str, Any]]:
    value = float(seconds)
    return [
        {
            "occurrence_id": row["occurrence_id"], "role": row["role"],
            "output_seconds": round(float(row["output_start"]) + (value - float(row["source_start"])) / float(row["speed"]), 6),
        }
        for row in plan.get("timeline_mapping") or []
        if float(row["source_start"]) <= value <= float(row["source_end"])
    ]


def output_time_to_source(plan: dict[str, Any], seconds: float) -> dict[str, Any] | None:
    value = float(seconds)
    for row in plan.get("timeline_mapping") or []:
        if float(row["output_start"]) <= value <= float(row["output_end"]):
            return {
                "occurrence_id": row["occurrence_id"], "role": row["role"],
                "source_seconds": round(float(row["source_start"]) + (value - float(row["output_start"])) * float(row["speed"]), 6),
            }
    return None
