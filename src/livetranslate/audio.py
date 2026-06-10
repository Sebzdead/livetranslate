import logging
import subprocess
import threading
import time
from typing import Iterator

from .types import AudioChunk

log = logging.getLogger(__name__)
BYTES_PER_MS = 16 * 2  # 16 kHz mono PCM16


class RingBuffer:
    """Thread-safe PCM ring addressable by stream-time ms (spec §5.1).

    Only the reconnect path reads it, via replay_from(ms).
    """

    def __init__(self, seconds: int, sample_rate: int = 16000):
        self.capacity = seconds * 1000 * BYTES_PER_MS
        self._buf = bytearray()
        self._start_ms = 0           # stream time of _buf[0]
        self._lock = threading.Lock()

    def append(self, chunk: AudioChunk) -> None:
        with self._lock:
            self._buf.extend(chunk.pcm16)
            excess = len(self._buf) - self.capacity
            if excess > 0:
                # trim whole milliseconds so _start_ms stays exact
                trim = (excess // BYTES_PER_MS + (1 if excess % BYTES_PER_MS else 0)) * BYTES_PER_MS
                del self._buf[:trim]
                self._start_ms += trim // BYTES_PER_MS

    def oldest_ms(self) -> int:
        """Return the stream-time ms of the oldest byte in the ring."""
        with self._lock:
            return self._start_ms

    def replay_from(self, ms: int, chunk_ms: int = 100) -> Iterator[AudioChunk]:
        with self._lock:
            if ms < self._start_ms:
                raise KeyError(f"requested {ms} ms but ring starts at {self._start_ms} ms")
            data = bytes(self._buf[(ms - self._start_ms) * BYTES_PER_MS:])
        step = chunk_ms * BYTES_PER_MS
        for i, off in enumerate(range(0, len(data), step)):
            pcm = data[off:off + step]
            yield AudioChunk(pcm16=pcm, sample_rate=16000,
                             t_start_ms=ms + i * chunk_ms,
                             duration_ms=len(pcm) // BYTES_PER_MS, seq=-1)


class FileSource:
    """Decode any container via ffmpeg subprocess; emit chunks paced at rtf (spec §5.1)."""

    def __init__(self, path: str, chunk_ms: int = 100, rtf: float = 1.0):
        self.path, self.chunk_ms, self.rtf = path, chunk_ms, rtf

    def chunks(self) -> Iterator[AudioChunk]:
        cmd = ["ffmpeg", "-v", "error", "-i", self.path,
               "-f", "s16le", "-ac", "1", "-ar", "16000", "-"]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        step = self.chunk_ms * BYTES_PER_MS
        seq, t_ms, t0 = 0, 0, time.monotonic()
        try:
            while True:
                pcm = proc.stdout.read(step)
                if not pcm:
                    break
                yield AudioChunk(pcm16=pcm, sample_rate=16000, t_start_ms=t_ms,
                                 duration_ms=len(pcm) // BYTES_PER_MS, seq=seq)
                seq += 1
                t_ms += len(pcm) // BYTES_PER_MS
                target = t0 + (t_ms / 1000.0) / self.rtf
                delay = target - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
        finally:
            proc.stdout.close()
            err = proc.stderr.read().decode(errors="replace")
            if proc.wait() != 0:
                raise RuntimeError(f"ffmpeg failed: {err}")


class MicSource:
    """Live mic capture via sounddevice.RawInputStream (spec §5.1):
    16 kHz mono PCM16, block size = chunk_ms. Refuses to start if the
    configured device substring doesn't match an input device — never
    silently falls back to the built-in mic."""

    def __init__(self, device_substring: str, chunk_ms: int = 100):
        self.device_substring, self.chunk_ms = device_substring, chunk_ms

    def resolve_device(self) -> int:
        import sounddevice  # lazy: PortAudio loads only when actually capturing
        if not self.device_substring:
            raise SystemExit(
                "audio.device_substring is empty; set it in config.toml to the "
                "mixing-desk interface name (refusing to default to the built-in mic)")
        for idx, dev in enumerate(sounddevice.query_devices()):
            if (dev.get("max_input_channels", 0) > 0
                    and self.device_substring.lower() in dev.get("name", "").lower()):
                return idx
        raise SystemExit(
            f"audio device matching {self.device_substring!r} not found; refusing to start")

    def chunks(self) -> Iterator[AudioChunk]:
        import queue as _q
        import sounddevice
        device = self.resolve_device()
        blocksize = 16 * self.chunk_ms          # frames per chunk at 16 kHz
        buf_q: _q.Queue = _q.Queue(maxsize=64)

        def _callback(indata, frames, time_info, status_flags):
            if status_flags:
                log.warning("mic: %s", status_flags)
            try:
                buf_q.put_nowait(bytes(indata))
            except _q.Full:
                log.error("mic: capture queue full; dropping %d frames", frames)

        t_ms, seq = 0, 0
        with sounddevice.RawInputStream(samplerate=16000, channels=1, dtype="int16",
                                        blocksize=blocksize, device=device,
                                        callback=_callback):
            while True:
                pcm = buf_q.get()
                dur = len(pcm) // BYTES_PER_MS
                yield AudioChunk(pcm16=pcm, sample_rate=16000, t_start_ms=t_ms,
                                 duration_ms=dur, seq=seq)
                t_ms += dur
                seq += 1
