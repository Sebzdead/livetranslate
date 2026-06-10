import logging
import resource
import sys
import threading
import time

log = logging.getLogger(__name__)


class StallDetector:
    """Spec §5.8: >= stall_s of audio sent with zero events received -> stall."""

    def __init__(self, stall_s: float = 10.0):
        self.stall_s = stall_s
        self._audio_ms_since_event = 0.0
        self._lock = threading.Lock()

    def audio_sent(self, ms: float) -> None:
        with self._lock:
            self._audio_ms_since_event += ms

    def event_received(self) -> None:
        with self._lock:
            self._audio_ms_since_event = 0.0

    def stalled(self) -> bool:
        with self._lock:
            return self._audio_ms_since_event >= self.stall_s * 1000


class Watchdog:
    """Samples gauges every 5 s, logs every 60 s, forces reconnect on stall,
    restarts dead translation workers once (second death in 10 min -> error banner)."""

    def __init__(self, pipeline, resilient_asr, stall: StallDetector, on_status):
        self.p = pipeline
        self.asr = resilient_asr
        self.stall = stall
        self.on_status = on_status
        self._stop = threading.Event()
        self._deaths: dict[str, list[float]] = {}
        self._thread = threading.Thread(target=self._run, name="watchdog", daemon=False)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=6)

    def rss_mb(self) -> float:
        raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        # macOS reports bytes; Linux reports kilobytes.
        if sys.platform == "darwin":
            return raw / (1024 * 1024)
        else:
            return raw / 1024

    def _run(self) -> None:
        from .asr.base import status
        last_log = 0.0
        while not self._stop.wait(5.0):
            if self.stall.stalled():
                self.on_status(status("warn", "watchdog", "ASR stall -> forcing reconnect"))
                self.stall.event_received()
                self.asr.force_reconnect()

            for lang, w in list(self.p.workers.items()):
                if not w.alive() and not self._stop.is_set():
                    now = time.monotonic()
                    deaths = [t for t in self._deaths.get(lang, []) if now - t < 600]
                    deaths.append(now)
                    self._deaths[lang] = deaths
                    if len(deaths) >= 2:
                        self.on_status(status("error", "watchdog",
                                              f"worker {lang} died twice in 10 min"))
                    else:
                        self.on_status(status("error", "watchdog",
                                              f"worker {lang} died; restarting"))
                        self.p.restart_worker(lang)

            if time.monotonic() - last_log > 60:
                lag = self.p.state.lag_by_lang()
                log.info("gauges: rss=%.0fMB lag=%s reconnects=%d eventq=%d",
                         self.rss_mb(), lag, self.asr.reconnect_count,
                         self.p.event_q.qsize())
                last_log = time.monotonic()
