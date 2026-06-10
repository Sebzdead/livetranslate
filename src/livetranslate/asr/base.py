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
