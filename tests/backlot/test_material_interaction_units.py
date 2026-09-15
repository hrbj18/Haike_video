from __future__ import annotations

import pytest

from backlot import material_interaction_units as units


def _compact(value: str) -> str:
    return "".join(ch for ch in value if not ch.isspace())


def _is_subsequence(needle: str, haystack: str) -> bool:
    cursor = 0
    for char in needle:
        cursor = haystack.find(char, cursor)
        if cursor < 0:
            return False
        cursor += 1
    return True


def _fixture():
    """Two 60-second ASR blocks over four VAD speech runs, like the real sample.

    The second block is deliberately longer than the speech inside it: that is
    the shape that used to make the tail of a block pile onto its last unit.
    """
    utterances = [
        {"id": "U00001", "start": 0.0, "end": 60.0,
         "text": "我们先热身一下 姐姐你好呀 你从哪里来的 我从湖南来的 今天天气不错"},
        {"id": "U00002", "start": 60.0, "end": 120.0,
         "text": "我们继续往前走 那我先走了 拜拜 然后还有别的人在拍我"},
    ]
    speech = [{"start": 12.0, "end": 18.0}, {"start": 20.0, "end": 26.0},
              {"start": 62.0, "end": 70.0}, {"start": 72.0, "end": 78.0}]
    return utterances, speech, [{"start": 10.0, "end": 80.0}]


# --- building ----------------------------------------------------------------

def test_units_are_deterministic():
    utterances, speech, allowed = _fixture()
    first, _ = units.build_spoken_units(utterances, speech, allowed=allowed)
    second, _ = units.build_spoken_units(utterances, speech, allowed=allowed)
    assert first == second
    assert units.units_signature(first) == units.units_signature(second)


def test_no_unit_edge_ever_falls_inside_speech():
    # This is the structural guarantee the whole layer rests on: a cut placed at
    # a unit boundary cannot clip a word, because every boundary is a VAD gap.
    utterances, speech, allowed = _fixture()
    built, _ = units.build_spoken_units(utterances, speech, allowed=allowed)
    assert built
    for row in built:
        for probe in (row["start"], row["end"]):
            for run in speech:
                assert not (run["start"] < probe < run["end"]), (row["id"], probe, run)


def test_unit_text_is_an_in_order_subsequence_of_the_asr_text():
    utterances, speech, allowed = _fixture()
    built, _ = units.build_spoken_units(utterances, speech, allowed=allowed)
    joined = _compact(" ".join(row["text"] for row in built))
    source = _compact(" ".join(row["text"] for row in utterances))
    assert joined
    assert _is_subsequence(joined, source)
    assert set(joined) <= set(source)


def test_units_are_ordered_and_do_not_overlap():
    utterances, speech, allowed = _fixture()
    built, _ = units.build_spoken_units(utterances, speech, allowed=allowed)
    ends = [row["end"] for row in built]
    starts = [row["start"] for row in built]
    assert starts == sorted(starts)
    assert all(start >= end for start, end in zip(starts[1:], ends[:-1]))


def test_an_utterance_outside_the_parent_range_contributes_no_text():
    # This is the defect that silently threw away 48 seconds of a 136-second
    # encounter: an ASR block overlapping the parent range only partially was
    # either dropped whole (losing real speech) or smeared onto a neighbour
    # (hiding where the closing word actually is).  An utterance with no overlap
    # at all must contribute nothing.
    utterances = [
        {"id": "U00001", "start": 0.0, "end": 40.0, "text": "姐姐你好呀 我们一起聊聊"},
        {"id": "U00002", "start": 40.0, "end": 100.0, "text": "这段完全在允许范围之外 不属于本次互动"},
    ]
    speech = [{"start": 12.0, "end": 18.0}, {"start": 20.0, "end": 26.0}]
    built, _ = units.build_spoken_units(utterances, speech, allowed=[{"start": 10.0, "end": 30.0}])
    assert built
    assert "不属于本次互动" not in " ".join(row["text"] for row in built)


def test_a_trailing_phrase_of_an_overlapping_utterance_stays_at_the_edge_unit():
    # The character-proportional estimate drifts where speech density is uneven,
    # so a phrase estimated past the last speech run is attached to it instead of
    # being dropped: dropping it would lose the very word the tail anchor looks
    # for ("那我先走了 拜拜").  Order is preserved either way.
    utterances, speech, allowed = _fixture()
    built, _ = units.build_spoken_units(utterances, speech, allowed=allowed)
    assert "拜拜" in built[-1]["text"]
    assert units.text_hits(built[-1]["text"], units.FAREWELL_LEXICON)


def test_a_short_speech_run_never_becomes_its_own_unit():
    utterances = [{"id": "U1", "start": 0.0, "end": 30.0, "text": "一句话 两句话 三句话"}]
    speech = [{"start": 1.0, "end": 6.0}, {"start": 6.15, "end": 6.35}, {"start": 6.5, "end": 12.0}]
    built, _ = units.build_spoken_units(utterances, speech, allowed=[{"start": 0.0, "end": 30.0}])
    assert all(row["duration_seconds"] >= units.MIN_UNIT_SECONDS for row in built)


def test_missing_speech_evidence_degrades_instead_of_raising():
    utterances, _, allowed = _fixture()
    built, degradations = units.build_spoken_units(utterances, [], allowed=allowed)
    assert built == []
    assert degradations and "spoken_units_unavailable" in degradations[0]


def test_missing_transcript_degrades_instead_of_raising():
    _, speech, allowed = _fixture()
    built, degradations = units.build_spoken_units([], speech, allowed=allowed)
    assert built == []
    assert degradations and "spoken_units_unavailable" in degradations[0]


def test_a_textless_unit_is_reported_so_the_caller_can_force_keep_it():
    utterances = [{"id": "U1", "start": 0.0, "end": 4.0, "text": "只有一句话"}]
    speech = [{"start": 0.5, "end": 3.0}, {"start": 3.5, "end": 9.0}]
    built, degradations = units.build_spoken_units(utterances, speech, allowed=[{"start": 0.0, "end": 10.0}])
    assert any(not row["text"] for row in built)
    assert any("spoken_units_partial_text" in note for note in degradations)


# --- speech blocks -----------------------------------------------------------

def test_close_speech_runs_are_merged_into_one_block():
    blocks = units.speech_blocks([{"start": 0.0, "end": 2.0}, {"start": 2.1, "end": 4.0}],
                                 [{"start": 0.0, "end": 10.0}])
    assert blocks == [{"start": 0.0, "end": 4.0}]


def test_runs_separated_by_a_real_pause_stay_separate():
    blocks = units.speech_blocks([{"start": 0.0, "end": 2.0}, {"start": 2.5, "end": 4.0}],
                                 [{"start": 0.0, "end": 10.0}])
    assert [(row["start"], row["end"]) for row in blocks] == [(0.0, 2.0), (2.5, 4.0)]


def test_speech_runs_are_clipped_to_the_parent_range():
    blocks = units.speech_blocks([{"start": 0.0, "end": 30.0}], [{"start": 5.0, "end": 12.0}])
    assert [(row["start"], row["end"]) for row in blocks] == [(5.0, 12.0)]


def test_overlapping_speech_runs_are_normalised_not_double_counted():
    blocks = units.speech_blocks([{"start": 1.0, "end": 5.0}, {"start": 3.0, "end": 7.0}],
                                 [{"start": 0.0, "end": 10.0}])
    assert blocks == [{"start": 1.0, "end": 7.0}]


# --- phrases -----------------------------------------------------------------

def test_phrase_windows_split_by_space_and_stay_inside_the_utterance():
    windows = units.phrase_windows({"id": "U1", "start": 10.0, "end": 20.0, "text": "第一句 第二句 第三句"})
    assert [row["text"] for row in windows] == ["第一句", "第二句", "第三句"]
    assert windows[0]["source_start"] == pytest.approx(10.0)
    assert windows[-1]["source_end"] == pytest.approx(20.0)
    for window in windows:
        assert 10.0 <= window["source_start"] < window["source_end"] <= 20.0


def test_a_long_unbroken_monologue_is_still_split():
    windows = units.phrase_windows({"id": "U1", "start": 0.0, "end": 12.0, "text": "啊" * 100})
    assert windows and all(len(row["text"]) <= units.DEFAULT_MAX_CHARS for row in windows)


def test_phrase_windows_of_an_empty_utterance_are_empty():
    assert units.phrase_windows(None) == []
    assert units.phrase_windows({"id": "U1", "start": 1.0, "end": 1.0, "text": "x"}) == []
    assert units.phrase_windows({"id": "U1", "start": 1.0, "end": 2.0, "text": "   "}) == []


# --- lexicons ----------------------------------------------------------------

def test_edge_terms_match_a_phrase_not_a_substring():
    # "hi" must not fire inside "this"; that trap is what made ordinary Chinese
    # small talk score as a foreign-language exchange in an earlier draft.
    assert units.text_hits("this is fine", ("hi",)) == []
    assert units.text_hits("hi there", ("hi",)) == ["hi"]
    assert units.text_hits("你好呀", units.GREETING_LEXICON) == ["你好"]
    assert units.text_hits("那就这样吧", units.FAREWELL_LEXICON) == []


def test_a_greeting_inside_a_longer_word_is_not_an_opening():
    # "你好聪明" is a phrase about being clever.  Treating it as the clip's
    # opening word cut the first 32 seconds off a measured 150-second encounter,
    # which is worse than leaving the edge alone.
    assert units.edge_token_hits("你好聪明", units.GREETING_LEXICON) == []
    assert units.edge_token_hits("你好聪明 你好聪明", units.GREETING_LEXICON) == []
    # Discourse particles and repeated characters still count.
    assert units.edge_token_hits("你好呀", units.GREETING_LEXICON) == ["你好"]
    assert units.edge_token_hits("拜拜拜拜拜拜", units.FAREWELL_LEXICON) == ["拜拜"]
    assert units.edge_token_hits("你们好", units.GREETING_LEXICON) == ["你们好"]
    # Phrase boundaries come from the ASR's own spacing and punctuation.  A
    # closing word buried inside a longer phrase ("那我先走了") is *not* treated
    # as the edge: anchoring on a guess would be worse than leaving the tail to
    # the model, and the phrase-level ASR ("那我先走了 拜拜") gives the real one.
    assert units.edge_token_hits("姐姐喂 你好 嗯", units.GREETING_LEXICON) == ["你好"]
    assert units.edge_token_hits("那我先走了，拜拜。", units.FAREWELL_LEXICON) == ["拜拜"]
    assert units.edge_token_hits("那我先走了", units.FAREWELL_LEXICON) == []


def test_units_signature_tracks_content_not_just_shape():
    utterances, speech, allowed = _fixture()
    built, _ = units.build_spoken_units(utterances, speech, allowed=allowed)
    shifted = [dict(row) for row in built]
    shifted[0] = {**shifted[0], "start": shifted[0]["start"] + 0.5}
    assert units.units_signature(built) != units.units_signature(shifted)
    assert units.units_signature(None) == units.units_signature([])


def test_units_signature_versions_the_derivation(monkeypatch):
    # A change of algorithm must invalidate cached analyses even when the inputs
    # are byte-identical, so the version is part of the identity.
    rows = [{"id": "P0001", "start": 1.0, "end": 2.0, "text": "x"}]
    before = units.units_signature(rows)
    monkeypatch.setattr(units, "VERSION", "material-interaction-units-test")
    assert units.units_signature(rows) != before
