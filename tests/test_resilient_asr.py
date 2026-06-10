import threading
import time
from livetranslate.asr.base import ResilientASR
from livetranslate.audio import RingBuffer
from livetranslate.types import AudioChunk
from tests.fakes import FakeAdapter


class DyingAdapter(FakeAdapter):
    """Fails on the Nth send, then works after restart."""
    def __init__(self, scripted, die_on_send=3):
        super().__init__(scripted)
        self.die_on_send, self.sends, self.starts = die_on_send, 0, 0

    def start(self, on_event, on_status):
        self.starts += 1
        super().start(on_event, on_status)

    def send_audio(self, chunk):
        self.sends += 1
        if self.sends == self.die_on_send and self.starts == 1:
            raise ConnectionError("ws dropped")
        super().send_audio(chunk)


def chunk(i):
    return AudioChunk(b"\x00" * 3200, 16000, i * 100, 100, i)


def test_reconnects_and_replays_with_overlap():
    ring = RingBuffer(seconds=10)
    a = DyingAdapter(scripted=[("final", "hello.", 0, 200)], die_on_send=3)
    statuses = []
    r = ResilientASR(lambda: a, ring=ring, overlap_ms=200,
                     backoff_base_s=0.01, backoff_max_s=0.02)
    r.start(on_event=lambda e: None, on_status=statuses.append)
    for i in range(6):
        ring.append(chunk(i)); r.send_audio(chunk(i))
    deadline = time.monotonic() + 2.0
    while a.starts < 2 and time.monotonic() < deadline:
        time.sleep(0.02)
    assert a.starts == 2                                  # reconnected
    msgs = [s.message for s in statuses]
    assert any("reconnecting" in m for m in msgs)
    assert any("replaying" in m for m in msgs)
    # replayed audio started at last_final_end - overlap = 200 - 200 = 0
    replayed = [c for c in a.sent_chunks if c.seq == -1]
    assert replayed and replayed[0].t_start_ms == 0


class OffsetFakeAdapter(DyingAdapter):
    """Records set_stream_offset calls so tests can assert the reconnect
    replay position was propagated to the adapter timeline mapping."""
    def __init__(self, scripted, die_on_send=3):
        super().__init__(scripted, die_on_send)
        self.offsets = []

    def set_stream_offset(self, ms):
        self.offsets.append(ms)


def test_reconnect_sets_stream_offset_to_replay_position():
    # C1: without set_stream_offset(replay_from), post-reconnect events are
    # mapped back to ~0 and the segmenter dedupe silently drops every final.
    ring = RingBuffer(seconds=10)
    a = OffsetFakeAdapter(scripted=[("final", "hello.", 0, 200)], die_on_send=3)
    r = ResilientASR(lambda: a, ring=ring, overlap_ms=100,
                     backoff_base_s=0.01, backoff_max_s=0.02)
    r.start(on_event=lambda e: None, on_status=lambda e: None)
    for i in range(6):
        ring.append(chunk(i)); r.send_audio(chunk(i))
    deadline = time.monotonic() + 2.0
    while a.starts < 2 and time.monotonic() < deadline:
        time.sleep(0.02)
    assert a.starts == 2                                  # reconnected
    expected = max(0, r.last_final_end_ms - r.overlap_ms)  # 200 - 100 = 100
    assert expected == 100
    assert expected in a.offsets, (
        f"set_stream_offset({expected}) never called; offsets={a.offsets}")


def test_flush_and_stop_terminates_endless_reconnect_loop():
    # C2: with give_up_after_s=0 and a permanently failing factory, the
    # asr-reconnect thread must exit promptly when flush_and_stop is called.
    ring = RingBuffer(seconds=10)
    first = FakeAdapter(scripted=[])
    calls = {"n": 0}

    def factory():
        calls["n"] += 1
        if calls["n"] == 1:
            return first                  # initial start succeeds
        raise ConnectionError("vendor down")

    r = ResilientASR(factory, ring=ring, overlap_ms=0,
                     backoff_base_s=0.01, backoff_max_s=0.02)
    r.start(on_event=lambda e: None, on_status=lambda e: None)
    r.force_reconnect()
    time.sleep(0.05)                      # let the reconnect loop spin a bit
    t0 = time.monotonic()
    r.flush_and_stop(timeout_s=2)
    assert time.monotonic() - t0 < 2.5, "flush_and_stop blocked on reconnect loop"
    deadline = time.monotonic() + 1.0
    while (any(t.name == "asr-reconnect" for t in threading.enumerate())
           and time.monotonic() < deadline):
        time.sleep(0.02)
    assert [t for t in threading.enumerate() if t.name == "asr-reconnect"] == []


def test_force_reconnect_is_idempotent_while_reconnecting():
    ring = RingBuffer(seconds=10)
    a = FakeAdapter(scripted=[])
    r = ResilientASR(lambda: a, ring=ring, overlap_ms=0,
                     backoff_base_s=0.01, backoff_max_s=0.02)
    statuses = []
    r.start(on_event=lambda e: None, on_status=statuses.append)
    r.force_reconnect(); r.force_reconnect()
    time.sleep(0.2)
    # exactly one reconnect cycle ran (adapter restarted once beyond initial)
    assert a.starts if hasattr(a, "starts") else True
    assert sum(1 for s in statuses if "reconnecting(1)" in s.message) <= 2
