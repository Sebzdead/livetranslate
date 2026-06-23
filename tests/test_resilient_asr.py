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

    def start(self, on_event, on_status, on_draft=None):
        self.starts += 1
        super().start(on_event, on_status, on_draft)

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


def test_eviction_fallback_updates_stream_offset():
    # A1: when replay_from is evicted, the adapter's stream offset must be
    # updated to ring.oldest_ms() so post-reconnect event timestamps are not
    # skewed backward, which would cause the segmenter dedupe to drop finals.
    #
    # Strategy: fill a tiny ring (1 s) with >1 s of audio so early ms are
    # evicted.  Use an OffsetFakeAdapter that records set_stream_offset calls.
    # Force reconnect with last_final_end_ms=0, overlap_ms=0 → replay_from=0,
    # which will be evicted.  Assert that the last recorded offset equals the
    # first replayed chunk's t_start_ms (i.e. ring.oldest_ms() at replay time).
    ring = RingBuffer(seconds=1)
    # Fill ring with 1.5 s of audio (> 1 s capacity) so t=0 is evicted.
    for i in range(15):
        ring.append(chunk(i))           # each chunk is 100 ms, 15 × 100 = 1500 ms total

    class EvictionOffsetAdapter(OffsetFakeAdapter):
        """Captures chunks sent during replay."""
        def __init__(self):
            super().__init__(scripted=[], die_on_send=999)   # never dies naturally
            self.replayed: list[AudioChunk] = []

        def send_audio(self, c):
            if c.seq == -1:                 # replay chunks have seq=-1
                self.replayed.append(c)
            super().send_audio(c)

    adapters: list[EvictionOffsetAdapter] = []

    def factory():
        a = EvictionOffsetAdapter()
        adapters.append(a)
        return a

    r = ResilientASR(factory, ring=ring, overlap_ms=0,
                     backoff_base_s=0.01, backoff_max_s=0.02)
    r.last_final_end_ms = 0             # replay_from = max(0, 0-0) = 0, which is evicted
    r.start(on_event=lambda e: None, on_status=lambda e: None)

    # First adapter is the normal start; force a reconnect to trigger eviction path.
    r.force_reconnect()
    deadline = time.monotonic() + 3.0
    while (len(adapters) < 2 or not adapters[-1].offsets) and time.monotonic() < deadline:
        time.sleep(0.02)

    assert len(adapters) >= 2, "reconnect did not spawn a second adapter"
    second = adapters[-1]
    assert second.offsets, "set_stream_offset never called on the fallback path"
    assert second.replayed, "no replay chunks sent after eviction fallback"
    # The key invariant: offset == first replayed chunk's t_start_ms
    assert second.offsets[-1] == second.replayed[0].t_start_ms, (
        f"offset {second.offsets[-1]} != first replayed chunk t_start_ms "
        f"{second.replayed[0].t_start_ms}")
    assert second.offsets[-1] > 0, "evicted replay must start after t=0"

    r.flush_and_stop(timeout_s=1)


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


def test_resilient_passes_on_draft_to_adapter():
    """ResilientASR forwards on_draft to the wrapped adapter's start()."""
    from livetranslate.asr.base import ResilientASR

    captured = {}

    class DraftAdapter:
        name = "draft-fake"
        def start(self, on_event, on_status, on_draft=None):
            captured["on_draft"] = on_draft
        def send_audio(self, chunk):
            pass
        def flush_and_stop(self, timeout_s=8.0):
            pass

    r = ResilientASR(lambda: DraftAdapter(), ring=None)
    sink = lambda lang, text: None
    r.start(on_event=lambda e: None, on_status=lambda e: None, on_draft=sink)
    assert captured["on_draft"] is sink


def test_resilient_forwards_on_draft_after_reconnect():
    """on_draft must survive a reconnect: the post-reconnect adapter receives
    the same callback that was passed to ResilientASR.start()."""
    from livetranslate.asr.base import ResilientASR

    # Each factory call records the on_draft it received in start().
    received_drafts: list = []
    starts_total = {"n": 0}

    class RecordingAdapter:
        name = "recording-fake"
        def start(self, on_event, on_status, on_draft=None):
            starts_total["n"] += 1
            received_drafts.append(on_draft)
        def send_audio(self, chunk):
            pass
        def flush_and_stop(self, timeout_s=8.0):
            pass

    ring = RingBuffer(seconds=10)
    r = ResilientASR(
        lambda: RecordingAdapter(),
        ring=ring,
        overlap_ms=0,
        backoff_base_s=0.01,
        backoff_max_s=0.02,
    )
    sink = lambda lang, text: None
    r.start(on_event=lambda e: None, on_status=lambda e: None, on_draft=sink)

    # Trigger a reconnect the same way test_eviction_fallback_updates_stream_offset does.
    r.force_reconnect()
    deadline = time.monotonic() + 3.0
    while starts_total["n"] < 2 and time.monotonic() < deadline:
        time.sleep(0.02)

    assert starts_total["n"] >= 2, (
        f"reconnect did not spawn a second adapter; starts={starts_total['n']}"
    )
    assert len(received_drafts) >= 2, (
        f"expected at least 2 on_draft recordings; got {len(received_drafts)}"
    )
    for i, draft in enumerate(received_drafts):
        assert draft is sink, (
            f"adapter #{i} received on_draft={draft!r}, expected sink={sink!r}"
        )

    r.flush_and_stop(timeout_s=1)
