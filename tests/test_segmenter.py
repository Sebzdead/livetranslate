import time
from livetranslate.segmenter import Segmenter
from livetranslate.types import TranscriptEvent

def ev(kind, text, s, e):
    return TranscriptEvent(kind=kind, text=text, t_audio_start_ms=s, t_audio_end_ms=e,
                           vendor="fake", t_received_wall=time.monotonic(), vendor_raw={})

def make(**kw):
    return Segmenter(max_words=kw.get("max_words", 45),
                     max_pending_s=kw.get("max_pending_s", 12))

def test_partial_only_updates_tentative_tail():
    sg = make()
    out = sg.on_event(ev("partial", "hello wor", 0, 500))
    assert out == [] and sg.tentative_tail == "hello wor"

def test_terminal_punctuation_emits_sentence():
    sg = make()
    out = sg.on_event(ev("final", "Hello world.", 0, 900))
    assert len(out) == 1
    s = out[0]
    assert s.sid == 0 and s.text == "Hello world."
    assert (s.t_audio_start_ms, s.t_audio_end_ms) == (0, 900)
    assert sg.tentative_tail == ""

def test_sids_gapless_and_multiple_sentences_in_one_final():
    sg = make()
    out = sg.on_event(ev("final", "One. Two!", 0, 2000))
    assert [s.sid for s in out] == [0, 1]
    assert [s.text for s in out] == ["One.", "Two!"]

def test_cjk_terminal_punctuation():
    sg = make()
    out = sg.on_event(ev("final", "你好。", 0, 800))
    assert len(out) == 1

def test_max_words_cuts_at_clause_boundary():
    sg = make(max_words=8)
    text = "one two three four, five six seven eight nine ten"
    out = sg.on_event(ev("final", text, 0, 5000))
    assert out and out[0].text == "one two three four,"

def test_max_words_hard_cut_without_comma():
    sg = make(max_words=5)
    out = sg.on_event(ev("final", "a b c d e f g", 0, 3000))
    assert out and out[0].text == "a b c d e"

def test_max_pending_forces_emission():
    sg = make()
    sg.on_event(ev("final", "no punctuation here", 0, 1000))
    assert sg.check_pending(now_wall=time.monotonic() + 13) != []

def test_reconnect_dedupe_drops_covered_final():
    sg = make()
    sg.on_event(ev("final", "Hello there.", 0, 1000))
    out = sg.on_event(ev("final", "Hello there.", 0, 1000))
    assert out == []

def test_boundary_overlap_merged_by_token_overlap():
    sg = make()
    sg.on_event(ev("final", "the rate of", 0, 1500))
    out = sg.on_event(ev("final", "rate of profit is falling.", 1300, 3500))
    assert len(out) == 1
    assert out[0].text == "the rate of profit is falling."

def test_empty_and_whitespace_finals_ignored():
    sg = make()
    assert sg.on_event(ev("final", "   ", 0, 100)) == []
    assert sg.on_event(ev("final", "", 100, 200)) == []

def test_out_of_order_final_dropped():
    sg = make()
    sg.on_event(ev("final", "First sentence here.", 1000, 2000))
    assert sg.on_event(ev("final", "old.", 100, 500)) == []

def test_flush_emits_remainder():
    sg = make()
    sg.on_event(ev("final", "trailing words without punct", 0, 1000))
    out = sg.flush()
    assert len(out) == 1 and out[0].text == "trailing words without punct"

def test_paragraph_break_on_4s_gap():
    sg = make()
    a = sg.on_event(ev("final", "First.", 0, 1000))[0]
    b = sg.on_event(ev("final", "Second.", 6000, 7000))[0]
    assert a.paragraph_break is False and b.paragraph_break is True

def test_whitespace_normalized_casing_preserved():
    sg = make()
    out = sg.on_event(ev("final", "  Hello\t  World.  ", 0, 900))
    assert out[0].text == "Hello World."

def test_independent_instances_dont_share_last_emitted_tokens():
    """Verify _last_emitted_tokens is a true instance attribute, not a
    class-level mutable default that would be shared across instances."""
    sg1 = make()
    sg2 = make()
    sg1.on_event(ev("final", "alpha beta gamma.", 0, 1000))
    # sg2 starts fresh — its _last_emitted_tokens must not contain sg1's tokens
    assert sg2._last_emitted_tokens == []
    # Mutating sg1's internal list must not affect sg2
    sg1._last_emitted_tokens.append("POISONED")
    assert "POISONED" not in sg2._last_emitted_tokens


# ---------------------------------------------------------------------------
# Fix 1 (C2): check_pending must drain ready sentences after the forced cut
# ---------------------------------------------------------------------------
def test_check_pending_drains_ready_remainder():
    """After a time-based forced cut, check_pending must also drain any
    emittable content left in the remainder.

    Scenario used: direct state injection (max_words=4).
    We set _buf = ['a,', 'b', 'c', 'd', 'e'] — 5 tokens, no terminal punct,
    comma at index 0.  on_event never gets to see this buffer (state is set
    directly), so the buffer legitimately sits uncommitted.
    check_pending fires:
      - forced cut at comma index 0  ->  emits 'a,'  (sid=0)
      - remainder ['b','c','d','e'] has 4 tokens == max_words=4 -> _emit_ready
        must cut it again  ->  emits 'b c d e'  (sid=1)
    Current code returns only ['a,'] (the bug); fixed code returns both.

    We cannot use terminal-punctuation in the buffer because any terminal
    token would be caught by _emit_ready during on_event, preventing
    accumulation.  We cannot use the multi-event approach because on_event's
    _emit_ready greedily drains before check_pending ever runs.  Direct state
    injection is the only faithful way to prove the drain-after-forced-cut path.
    """
    sg = make(max_words=4)
    # Inject state directly: 5-token buffer, old wall time, comma at index 0.
    sg._buf = ["a,", "b", "c", "d", "e"]
    sg._buf_start_ms = 0
    sg._buf_first_wall = time.monotonic() - 15   # older than max_pending_s=12
    sg._last_committed_end_ms = 1000

    out = sg.check_pending()
    assert [s.text for s in out] == ["a,", "b c d e"], (
        f"Expected ['a,', 'b c d e'] but got {[s.text for s in out]!r}"
    )
    assert [s.sid for s in out] == [0, 1]


# ---------------------------------------------------------------------------
# Fix 2 (I3): off-by-one in initial dedupe state
# ---------------------------------------------------------------------------
def test_short_first_final_not_dropped():
    """First utterance ending at 200 ms must NOT be silently dropped.
    With _last_committed_end_ms = -1, threshold = -1 + 250 = 249, so
    end_ms=200 <= 249 causes a drop.  Fix: initialise to -(DEDUPE_SLACK_MS+1).
    """
    sg = make()
    out = sg.on_event(ev("final", "Hi.", 0, 200))   # ends at 200ms, < old 249 threshold
    assert len(out) == 1 and out[0].text == "Hi."


# ---------------------------------------------------------------------------
# Speechmatics emits forward-only, contiguous, fine-grained finals (often a
# single word; each final's start == the previous final's end). Such finals can
# legitimately end within DEDUPE_SLACK_MS of the committed point yet introduce
# entirely NEW audio. The end-only dedupe must NOT drop them — captured live
# 2026-06-25, where it silently lost "what/this/of/not/are/..." function words.
# ---------------------------------------------------------------------------
def test_forward_only_fine_grained_finals_not_dropped():
    sg = make()
    # Real captured Speechmatics sequence (first sentence): contiguous word finals.
    seq = [
        ("I ", 0, 360), ("think ", 360, 720), ("what ", 720, 960),
        ("we are ", 960, 1240), ("discussing ", 1240, 1720), ("this ", 1720, 1960),
        ("week is ", 1960, 2400), ("how ", 2400, 2880), ("hundreds ", 2880, 3520),
        ("of ", 3520, 3640), ("thousands, if ", 3640, 4280), ("not ", 4280, 4440),
        ("millions of ", 4440, 5400), ("people ", 5400, 5760), ("are ", 5760, 5920),
        ("looking to the ", 5920, 6440), ("ideas ", 6440, 6800), ("of ", 6800, 6960),
        ("communism. ", 6960, 7840),
    ]
    out = []
    for text, s, e in seq:
        out += sg.on_event(ev("final", text, s, e))
    assert len(out) == 1, f"expected 1 sentence, got {[s.text for s in out]}"
    assert out[0].text == ("I think what we are discussing this week is how "
                           "hundreds of thousands, if not millions of people are "
                           "looking to the ideas of communism.")


# ---------------------------------------------------------------------------
# Fix 3 (C1): widen overlap-merge window so long overlaps aren't missed
# ---------------------------------------------------------------------------
def test_overlap_merge_longer_than_12_tokens():
    """A boundary overlap of 15 tokens must be correctly de-duplicated.
    With the old [-12:] window the 15-token overlap is only partially seen,
    so the new final's tokens are appended whole, duplicating content.
    """
    sg = make()
    base = " ".join(f"w{i}" for i in range(15))            # 15 tokens, no terminal punct
    sg.on_event(ev("final", base, 0, 1500))                # buffered, not emitted
    out = sg.on_event(ev("final", base + " end.", 200, 4000))  # 15-token overlap + new word
    assert len(out) == 1, f"Expected 1 sentence, got {len(out)}: {[s.text for s in out]}"
    # no duplicated content: each w_i appears exactly once
    text = out[0].text
    assert text == base + " end.", f"Unexpected text: {text!r}"
    for i in range(15):
        assert text.split().count(f"w{i}") == 1, (
            f"Token w{i} duplicated in: {text!r}"
        )
