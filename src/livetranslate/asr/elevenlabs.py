import base64
import json
import logging
import queue
import threading
import time
from urllib.parse import urlencode

import websocket  # websocket-client (blocking)

from ..types import AudioChunk, TranscriptEvent
from .base import OnEvent, OnStatus, status

log = logging.getLogger(__name__)

# message_type -> normalized kind (verified in docs/vendor-notes.md, Task 8)
_TRANSCRIPT_KINDS = {
    "partial_transcript": "partial",
    "committed_transcript_with_timestamps": "final",
}
# Verified live 2026-06-10: every commit is sent BOTH as a bare
# committed_transcript (no words/timestamps) and as
# committed_transcript_with_timestamps. We request timestamps, so the bare
# variant is ignored — mapping both to "final" would emit the first one with
# zero timestamps and corrupt the segmenter timeline.
_IGNORED_MESSAGE_TYPES = ("session_started", "committed_transcript")
KEYTERMS_CAP = 50    # ElevenLabs realtime cap (docs); surcharged
KEYTERM_MAX_LEN = 20  # ElevenLabs realtime per-term character limit

class ElevenLabsScribeAdapter:
    name = "elevenlabs"
    WS_BASE = "wss://api.elevenlabs.io/v1/speech-to-text/realtime"
    MODEL_ID = "scribe_v2_realtime"

    def __init__(self, api_key: str, language: str, keyterms: list[str],
                 sample_rate: int = 16000):
        # Drop terms that exceed the per-term character cap
        long_terms = [k for k in keyterms if len(k) > KEYTERM_MAX_LEN]
        if long_terms:
            log.warning(
                "elevenlabs: %d keyterm(s) exceeded %d-char limit and were dropped: %s",
                len(long_terms), KEYTERM_MAX_LEN,
                ", ".join(repr(t) for t in long_terms),
            )
            keyterms = [k for k in keyterms if len(k) <= KEYTERM_MAX_LEN]
        if len(keyterms) > KEYTERMS_CAP:
            log.warning("elevenlabs: keyterms truncated %d -> %d", len(keyterms), KEYTERMS_CAP)
            keyterms = keyterms[:KEYTERMS_CAP]
        log.info("elevenlabs adapter: %d keyterms (surcharged)", len(keyterms))
        self.api_key, self.language, self.keyterms = api_key, language, keyterms
        self.sample_rate = sample_rate
        self._send_q: queue.Queue = queue.Queue(maxsize=64)
        self._stream_offset_ms = 0      # stream time at vendor t=0 (set on connect/reconnect)
        self._ws = None
        self._stop = threading.Event()

    # -- lifecycle ------------------------------------------------------
    def start(self, on_event: OnEvent, on_status: OnStatus, on_draft=None) -> None:
        # on_draft ignored: ElevenLabs has no realtime translation.
        self.on_event, self.on_status = on_event, on_status
        self._stop.clear()
        self._connect()
        self._sender = threading.Thread(target=self._send_loop, name="asr-sender")
        self._receiver = threading.Thread(target=self._recv_loop, name="asr-receiver")
        self._sender.start(); self._receiver.start()

    def _ws_url(self) -> str:
        params = [
            ("model_id", self.MODEL_ID),
            ("audio_format", f"pcm_{self.sample_rate}"),
            ("sample_rate", str(self.sample_rate)),
            ("language_code", self.language),
            ("commit_strategy", "vad"),
            ("include_timestamps", "true"),
        ]
        params += [("keyterms", k) for k in self.keyterms]
        return f"{self.WS_BASE}?{urlencode(params)}"

    def _connect(self) -> None:
        self._ws = websocket.create_connection(
            self._ws_url(), header=[f"xi-api-key: {self.api_key}"], timeout=10)
        self.on_status(status("info", "asr", "connected"))

    def set_stream_offset(self, offset_ms: int) -> None:
        """Stream-timeline position corresponding to vendor audio t=0.
        Called by ResilientASR at session start and on every reconnect."""
        self._stream_offset_ms = offset_ms

    # -- audio path -----------------------------------------------------
    def send_audio(self, chunk: AudioChunk) -> None:
        self._send_q.put(chunk)        # blocks if full: backpressure to source

    def _send_loop(self) -> None:
        while not self._stop.is_set():
            chunk = self._send_q.get()
            if chunk is None:
                self._send_commit()
                return
            try:
                self._ws.send(json.dumps(self._audio_frame(chunk.pcm16)))
            except Exception as e:                   # noqa: BLE001 — surfaced as status
                self.on_status(status("error", "asr", f"send failed: {e}"))
                return

    def _audio_frame(self, pcm16: bytes, commit: bool = False) -> dict:
        """Verified live 2026-06-10: audio messages MUST be tagged
        message_type=input_audio_chunk; commit is a flag on the chunk itself
        (there is no separate commit message)."""
        return {"message_type": "input_audio_chunk",
                "audio_base_64": base64.b64encode(pcm16).decode("ascii"),
                "commit": commit,
                "sample_rate": self.sample_rate}

    def _send_commit(self) -> None:
        # 100 ms of silence carrying the commit flag flushes the last segment.
        try:
            self._ws.send(json.dumps(self._audio_frame(b"\x00" * (self.sample_rate // 10 * 2),
                                                       commit=True)))
        except Exception:                            # noqa: BLE001 — already closing
            pass

    # -- receive path ---------------------------------------------------
    def _recv_loop(self) -> None:
        while not self._stop.is_set():
            try:
                raw = self._ws.recv()
            except Exception as e:                   # noqa: BLE001
                if not self._stop.is_set():
                    self.on_status(status("error", "asr", f"recv failed: {e}"))
                return
            if not raw:
                continue
            try:
                msg = json.loads(raw)
            except (ValueError, TypeError):
                continue
            ev = self._normalize(msg)
            if ev is not None:
                self.on_event(ev)
            elif msg.get("message_type") not in _IGNORED_MESSAGE_TYPES:
                # Never swallow protocol/server errors silently (an input_error
                # storm is how a wrong audio-frame schema manifests). Warn, not
                # error: per-message rejections must not trigger reconnect loops.
                self.on_status(status("warn", "asr",
                                      f"vendor message: {json.dumps(msg)[:200]}"))

    # -- normalization (unit-tested against fixtures) -------------------
    def _normalize(self, msg: dict) -> TranscriptEvent | None:
        kind = _TRANSCRIPT_KINDS.get(msg.get("message_type"))
        if kind is None:
            return None
        start_rel, end_rel = self._timestamps_ms(msg)
        return TranscriptEvent(
            kind=kind,
            text=msg.get("text", ""),
            t_audio_start_ms=self._stream_offset_ms + start_rel,
            t_audio_end_ms=self._stream_offset_ms + end_rel,
            vendor=self.name, t_received_wall=time.monotonic(), vendor_raw=msg)

    @staticmethod
    def _timestamps_ms(msg: dict) -> tuple[int, int]:
        """Return (start_ms, end_ms) relative to vendor t=0, from word entries.
        Timestamps are in SECONDS in the vendor schema. Partials have no words."""
        words = [w for w in msg.get("words", [])
                 if w.get("type") == "word" and isinstance(w.get("start"), (int, float))]
        if not words:
            return 0, 0
        return int(round(words[0]["start"] * 1000)), int(round(words[-1]["end"] * 1000))

    def flush_and_stop(self, timeout_s: float = 8.0) -> None:
        self._send_q.put(None)
        self._sender.join(timeout=timeout_s)
        self._stop.set()
        # Close the WS BEFORE joining the receiver so its blocking recv()
        # unblocks immediately instead of burning the join timeout.
        try:
            self._ws.close()
        except Exception:                            # noqa: BLE001 — already closing
            pass
        self._receiver.join(timeout=timeout_s)
