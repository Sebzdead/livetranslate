import queue as _queue
import types

from livetranslate.health import StallDetector, Watchdog


def test_stall_detected_after_threshold():
    sd = StallDetector(stall_s=10)
    sd.audio_sent(ms=12000)        # 12 s of audio sent...
    assert sd.stalled() is True    # ...zero events received
    sd.event_received()
    assert sd.stalled() is False


# ---------------------------------------------------------------------------
# Stub helpers for Watchdog._tick tests
# ---------------------------------------------------------------------------

def _make_stub_pipeline():
    """Minimal pipeline stub with the attributes _tick() needs."""
    p = types.SimpleNamespace()
    p.workers = {}
    p.state = types.SimpleNamespace(tentative_tail="", lag_by_lang=lambda: {})
    p.event_q = types.SimpleNamespace(qsize=lambda: 0)
    return p


def _make_stub_asr(age_s: float):
    """ASR stub whose session_age_s() returns a fixed value."""
    asr = types.SimpleNamespace()
    asr.session_age_s = lambda: age_s
    asr.reconnect_count = 0
    asr._reconnect_calls = []
    def force_reconnect():
        asr._reconnect_calls.append(True)
    asr.force_reconnect = force_reconnect
    return asr


def _make_stall():
    return StallDetector(stall_s=999)   # never trips during these tests


def test_proactive_rotation_triggers_at_80pct_at_pause():
    # A5: when age > 0.8 * max_session_s and tentative_tail is empty,
    # Watchdog._tick() must call asr.force_reconnect() exactly once.
    pipeline = _make_stub_pipeline()
    asr = _make_stub_asr(age_s=85)   # 85 > 0.8 * 100 = 80
    wd = Watchdog(pipeline, asr, _make_stall(), on_status=lambda e: None,
                  max_session_s=100)
    wd._tick()
    assert len(asr._reconnect_calls) == 1, (
        f"expected 1 rotation call, got {len(asr._reconnect_calls)}")


def test_proactive_rotation_not_triggered_below_80pct():
    # Below the 80 % threshold no rotation must occur.
    pipeline = _make_stub_pipeline()
    asr = _make_stub_asr(age_s=70)   # 70 < 0.8 * 100 = 80
    wd = Watchdog(pipeline, asr, _make_stall(), on_status=lambda e: None,
                  max_session_s=100)
    wd._tick()
    assert len(asr._reconnect_calls) == 0, (
        f"unexpected rotation call at age 70s (threshold 80s)")


def test_proactive_rotation_deferred_when_tail_active_below_95pct():
    # Between 80 % and 95 %, rotation is deferred if tentative_tail is non-empty.
    pipeline = _make_stub_pipeline()
    pipeline.state.tentative_tail = "some active speech"
    asr = _make_stub_asr(age_s=85)   # 80 % < 85 < 95 % (95 * 100 = 95)
    wd = Watchdog(pipeline, asr, _make_stall(), on_status=lambda e: None,
                  max_session_s=100)
    wd._tick()
    assert len(asr._reconnect_calls) == 0, (
        "rotation must be deferred when tentative_tail is active and age < 95%")


def test_proactive_rotation_forced_above_95pct_even_with_tail():
    # Above 95 %, rotation fires unconditionally.
    pipeline = _make_stub_pipeline()
    pipeline.state.tentative_tail = "still talking"
    asr = _make_stub_asr(age_s=96)   # 96 > 0.95 * 100 = 95
    wd = Watchdog(pipeline, asr, _make_stall(), on_status=lambda e: None,
                  max_session_s=100)
    wd._tick()
    assert len(asr._reconnect_calls) == 1, (
        "forced rotation must fire above 95 % even with non-empty tentative_tail")


def test_proactive_rotation_disabled_when_max_session_s_is_zero():
    # max_session_s=0 (default) must never trigger rotation regardless of age.
    pipeline = _make_stub_pipeline()
    asr = _make_stub_asr(age_s=99999)
    wd = Watchdog(pipeline, asr, _make_stall(), on_status=lambda e: None,
                  max_session_s=0)
    wd._tick()
    assert len(asr._reconnect_calls) == 0, (
        "rotation must not fire when max_session_s=0")
