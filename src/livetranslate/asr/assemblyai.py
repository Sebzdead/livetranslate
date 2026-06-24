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

KEYTERMS_CAP = 100       # AssemblyAI v3 cap: max 100 terms × 50 chars each
KEYTERM_MAX_LEN = 50     # characters per term (API limit)

WS_BASE = "wss://streaming.assemblyai.com/v3/ws"


class AssemblyAIStreamingAdapter:
    name = "assemblyai"

    def __init__(self, api_key: str, language: str, keyterms: list[str],
                 prompt: str = "", sample_rate: int = 16000):
        # language is stored for logging/meta only; v3 has no language query param
        # (deprecated in v3 — model auto-detects language)
        self.api_key = api_key
        self.language = language
        self.prompt = prompt
        self.sample_rate = sample_rate

        # Drop terms that exceed the character cap
        long_terms = [k for k in keyterms if len(k) > KEYTERM_MAX_LEN]
        if long_terms:
            log.warning(
                "assemblyai: %d keyterm(s) exceeded %d-char limit and were dropped: %s",
                len(long_terms), KEYTERM_MAX_LEN,
                ", ".join(repr(t) for t in long_terms),
            )
            keyterms = [k for k in keyterms if len(k) <= KEYTERM_MAX_LEN]

        # Truncate to count cap
        if len(keyterms) > KEYTERMS_CAP:
            log.warning(
                "assemblyai: keyterms truncated %d -> %d", len(keyterms), KEYTERMS_CAP
            )
            keyterms = keyterms[:KEYTERMS_CAP]

        self.keyterms = keyterms
        self._send_q: queue.Queue = queue.Queue(maxsize=64)
        self._stream_offset_ms = 0   # stream time at vendor t=0 (set on connect/reconnect)
        self._ws = None
        self._stop = threading.Event()

    # -- lifecycle ----------------------------------------------------------

    def start(self, on_event: OnEvent, on_status: OnStatus, on_draft=None) -> None:
        # on_draft ignored: AssemblyAI has no realtime translation.
        self.on_event, self.on_status = on_event, on_status
        self._stop.clear()
        self._connect()
        self._sender = threading.Thread(target=self._send_loop, name="asr-sender",
                                        daemon=False)
        self._receiver = threading.Thread(target=self._recv_loop, name="asr-receiver",
                                          daemon=False)
        self._sender.start()
        self._receiver.start()

    def _ws_url(self) -> str:
        params = [
            ("sample_rate", str(self.sample_rate)),
            ("encoding", "pcm_s16le"),
            ("speech_model", "u3-rt-pro"),
            # format_turns is not a valid parameter for u3-rt-pro (removed in U3 Pro;
            # formatting is always on). See docs/vendor-notes.md AssemblyAI section.
        ]
        # Repeated keyterms_prompt params (one per term)
        params += [("keyterms_prompt", k) for k in self.keyterms]
        # Optional domain prompt (only if non-empty)
        if self.prompt:
            params.append(("prompt", self.prompt))
        return f"{WS_BASE}?{urlencode(params)}"

    def _connect(self) -> None:
        # Auth via HTTP header; NO "Bearer" prefix per AssemblyAI v3 docs
        self._ws = websocket.create_connection(
            self._ws_url(),
            header=[f"Authorization: {self.api_key}"],
            timeout=10,
        )
        self.on_status(status("info", "asr", "connected"))

    def set_stream_offset(self, offset_ms: int) -> None:
        """Stream-timeline position corresponding to vendor audio t=0.
        Called by ResilientASR at session start and on every reconnect."""
        self._stream_offset_ms = offset_ms

    # -- audio path ---------------------------------------------------------

    def send_audio(self, chunk: AudioChunk) -> None:
        self._send_q.put(chunk)   # blocks if full: backpressure to source

    def _send_loop(self) -> None:
        while not self._stop.is_set():
            chunk = self._send_q.get()
            if chunk is None:
                # Graceful termination: send Terminate message as a TEXT frame
                try:
                    self._ws.send(json.dumps({"type": "Terminate"}))
                except Exception:   # noqa: BLE001 — already closing
                    pass
                return
            try:
                # Audio is sent as raw binary WebSocket frames (not JSON/base64)
                self._ws.send_binary(chunk.pcm16)
            except Exception as e:  # noqa: BLE001 — surfaced as status
                self.on_status(status("error", "asr", f"send failed: {e}"))
                return

    # -- receive path -------------------------------------------------------

    def _recv_loop(self) -> None:
        while not self._stop.is_set():
            try:
                raw = self._ws.recv()
            except Exception as e:  # noqa: BLE001
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

    # -- normalization (unit-tested against fixtures) -----------------------

    def _normalize(self, msg: dict) -> TranscriptEvent | None:
        if msg.get("type") != "Turn":
            return None
        kind = "final" if msg.get("end_of_turn") else "partial"
        start_rel, end_rel = self._timestamps_ms(msg)
        return TranscriptEvent(
            kind=kind,
            text=msg.get("transcript", ""),
            t_audio_start_ms=self._stream_offset_ms + start_rel,
            t_audio_end_ms=self._stream_offset_ms + end_rel,
            vendor=self.name,
            t_received_wall=time.monotonic(),
            vendor_raw=msg,
        )

    @staticmethod
    def _timestamps_ms(msg: dict) -> tuple[int, int]:
        """Return (start_ms, end_ms) relative to vendor t=0.
        AssemblyAI v3 timestamps are INTEGER MILLISECONDS — use directly, no conversion."""
        words = [w for w in msg.get("words", [])
                 if isinstance(w.get("start"), (int, float))]
        if not words:
            return 0, 0
        return int(words[0]["start"]), int(words[-1]["end"])

    def flush_and_stop(self, timeout_s: float = 8.0) -> None:
        self._send_q.put(None)
        self._sender.join(timeout=timeout_s)
        self._stop.set()
        # Close the WS BEFORE joining the receiver so its blocking recv()
        # unblocks immediately (the server usually closes after Terminate,
        # but this makes the order deterministic).
        try:
            self._ws.close()
        except Exception:   # noqa: BLE001 — already closing
            pass
        self._receiver.join(timeout=timeout_s)
