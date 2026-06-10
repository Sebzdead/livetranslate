import random
import threading
import time
from typing import Callable, Protocol, runtime_checkable

from ..types import AudioChunk, TranscriptEvent, StatusEvent

OnEvent = Callable[[TranscriptEvent], None]
OnStatus = Callable[[StatusEvent], None]

@runtime_checkable
class ASRAdapter(Protocol):
    """Spec §5.2. Adapters own their WS + sender/receiver threads and emit
    normalized TranscriptEvents mapped onto the session stream timeline."""
    name: str
    def start(self, on_event: OnEvent, on_status: OnStatus) -> None: ...
    def send_audio(self, chunk: AudioChunk) -> None: ...
    def flush_and_stop(self, timeout_s: float = 8.0) -> None: ...

def status(level: str, source: str, message: str) -> StatusEvent:
    return StatusEvent(level=level, source=source, message=message,
                       t_wall=time.monotonic())


class ResilientASR:
    """Spec §5.2: wraps any adapter; the only ASR object the pipeline sees.
    Reconnect with jittered exponential backoff, replay from
    last_final_end_ms - overlap_ms out of the RingBuffer, then resume live."""

    def __init__(self, adapter_factory, ring, overlap_ms: int = 2000,
                 backoff_base_s: float = 0.5, backoff_max_s: float = 8.0,
                 give_up_after_s: float = 0, failover_factory=None):
        self._factory = adapter_factory
        self._failover_factory = failover_factory
        self.ring = ring
        self.overlap_ms = overlap_ms
        self.backoff_base_s = backoff_base_s
        self.backoff_max_s = backoff_max_s
        self.give_up_after_s = give_up_after_s
        self.last_final_end_ms = 0
        self.reconnect_count = 0
        self._lock = threading.RLock()
        self._adapter = None
        # _reconnecting is a plain Event used for the "is reconnect in progress" check;
        # _spawn_lock guards the test-and-set so only one thread enters _reconnect.
        self._reconnecting = threading.Event()
        self._spawn_lock = threading.Lock()

    @property
    def name(self):
        return self._adapter.name if self._adapter else "?"

    def start(self, on_event: OnEvent, on_status: OnStatus) -> None:
        self.on_status = on_status

        def tracking_on_event(ev):
            if ev.kind == "final":
                self.last_final_end_ms = max(self.last_final_end_ms, ev.t_audio_end_ms)
            on_event(ev)

        self.on_event = tracking_on_event
        with self._lock:
            self._adapter = self._factory()
            self._adapter.start(self.on_event, self._on_adapter_status)

    def _on_adapter_status(self, ev: StatusEvent) -> None:
        self.on_status(ev)
        if ev.level == "error":
            self._spawn_reconnect()

    def _spawn_reconnect(self) -> None:
        """Atomically test-and-set: only one reconnect thread runs at a time."""
        with self._spawn_lock:
            if self._reconnecting.is_set():
                return
            self._reconnecting.set()
        t = threading.Thread(target=self._reconnect, name="asr-reconnect", daemon=False)
        t.start()

    def send_audio(self, chunk: AudioChunk) -> None:
        if self._reconnecting.is_set():
            return                       # ring buffer covers the gap
        try:
            with self._lock:
                self._adapter.send_audio(chunk)
        except Exception:                # noqa: BLE001 — triggers reconnect
            self._spawn_reconnect()

    def force_reconnect(self) -> None:
        """Called by the stall watchdog and proactive session rotation."""
        self._spawn_reconnect()

    def _reconnect(self) -> None:
        # _reconnecting is already set by _spawn_reconnect before this runs.
        t0 = time.monotonic()
        attempt = 0
        factory = self._factory
        while True:
            attempt += 1
            self.on_status(status("warn", "asr", f"reconnecting({attempt})"))
            delay = min(self.backoff_max_s, self.backoff_base_s * 2 ** (attempt - 1))
            time.sleep(delay * random.uniform(0.5, 1.0))
            try:
                with self._lock:
                    # Stop the old adapter gracefully (best-effort).
                    try:
                        self._adapter.flush_and_stop(timeout_s=1.0)
                    except Exception:    # noqa: BLE001 — old socket already dead
                        pass
                    # Create and start the new adapter.
                    self._adapter = factory()
                    self._adapter.start(self.on_event, self._on_adapter_status)
                    # Replay from ring buffer with overlap.
                    replay_from = max(0, self.last_final_end_ms - self.overlap_ms)
                    self.on_status(status("info", "asr", f"replaying from {replay_from}ms"))
                    try:
                        for c in self.ring.replay_from(replay_from):
                            self._adapter.send_audio(c)
                    except KeyError:
                        # Requested ms was evicted; fall back to oldest available.
                        oldest = self.ring.oldest_ms()
                        self.on_status(status("warn", "asr",
                                              f"replay start {replay_from}ms evicted; "
                                              f"replaying from oldest {oldest}ms"))
                        for c in self.ring.replay_from(oldest):
                            self._adapter.send_audio(c)
                self.reconnect_count += 1
                self.on_status(status("info", "asr", "connected"))
                self._reconnecting.clear()
                return
            except Exception as e:       # noqa: BLE001 — keep backing off
                self.on_status(status("warn", "asr", f"reconnect failed: {e}"))
                if self.give_up_after_s and time.monotonic() - t0 > self.give_up_after_s:
                    if self._failover_factory and factory is self._factory:
                        self.on_status(status("error", "asr", "gave_up; failing over"))
                        factory = self._failover_factory
                        t0 = time.monotonic()
                        attempt = 0
                        continue
                    self.on_status(status("error", "asr", "gave_up"))
                    self._reconnecting.clear()
                    return

    def flush_and_stop(self, timeout_s: float = 8.0) -> None:
        with self._lock:
            if self._adapter:
                self._adapter.flush_and_stop(timeout_s)
