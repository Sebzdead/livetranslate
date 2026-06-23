import time
from livetranslate.types import AudioChunk, TranscriptEvent

class FakeAdapter:
    """Scripted adapter for unit tests: emits the scripted events once audio
    covering their end time has been sent."""
    name = "fake"

    def __init__(self, scripted: list[tuple[str, str, int, int]]):
        self.scripted = list(scripted)
        self.audio_ms_sent = 0
        self.started = False
        self.stopped = False
        self.sent_chunks: list[AudioChunk] = []

    def start(self, on_event, on_status, on_draft=None):
        self.on_event, self.on_status = on_event, on_status
        self.started = True

    def send_audio(self, chunk: AudioChunk) -> None:
        self.sent_chunks.append(chunk)
        self.audio_ms_sent = chunk.t_start_ms + chunk.duration_ms
        self._emit_ready()

    def _emit_ready(self):
        while self.scripted and self.scripted[0][3] <= self.audio_ms_sent:
            kind, text, s, e = self.scripted.pop(0)
            self.on_event(TranscriptEvent(kind=kind, text=text, t_audio_start_ms=s,
                                          t_audio_end_ms=e, vendor="fake",
                                          t_received_wall=time.monotonic(), vendor_raw={}))

    def flush_and_stop(self, timeout_s: float = 8.0) -> None:
        self.audio_ms_sent = 10 ** 12
        self._emit_ready()
        self.stopped = True
