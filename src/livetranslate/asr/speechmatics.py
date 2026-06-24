import json
import logging
import queue
import threading
import time

import websocket  # websocket-client (blocking)

from ..types import AudioChunk, TranscriptEvent
from .base import OnEvent, OnStatus, status

log = logging.getLogger(__name__)

WS_URL = "wss://eu.rt.speechmatics.com/v2/"

# message -> normalized transcript kind
_TRANSCRIPT_KINDS = {"AddPartialTranscript": "partial", "AddTranscript": "final"}
_TRANSLATION_MESSAGES = ("AddPartialTranslation", "AddTranslation")

# app code <-> Speechmatics code (only Chinese differs)
_APP_TO_SM = {"zh": "cmn"}
_SM_TO_APP = {"cmn": "zh"}


def to_sm(lang: str) -> str:
    return _APP_TO_SM.get(lang, lang)


def to_app(lang: str) -> str:
    return _SM_TO_APP.get(lang, lang)


class SpeechmaticsRTAdapter:
    """Speechmatics Realtime v2 adapter. Implements the ASRAdapter protocol and,
    when target_languages is set, also emits draft translations via on_draft."""
    name = "speechmatics"

    def __init__(self, api_key: str, language: str, additional_vocab: list[str],
                 target_languages: list[str] | None = None,
                 additional_vocab_max: int = 50, max_delay: float = 1.0,
                 sample_rate: int = 16000):
        if len(additional_vocab) > additional_vocab_max:
            log.warning("speechmatics: additional_vocab truncated %d -> %d",
                        len(additional_vocab), additional_vocab_max)
            additional_vocab = additional_vocab[:additional_vocab_max]
        self.api_key = api_key
        self.language = language
        self.additional_vocab = additional_vocab
        self.target_languages = target_languages or []
        self.max_delay = max_delay
        self.sample_rate = sample_rate
        self._send_q: queue.Queue = queue.Queue(maxsize=64)
        self._stream_offset_ms = 0   # stream time at vendor t=0 (set on connect/reconnect)
        self._seq = 0                # binary audio frames sent (for EndOfStream)
        self._started = threading.Event()   # set on RecognitionStarted
        self._ws = None
        self._stop = threading.Event()
        self.on_draft = None         # set in start(); only used when targets present

    # -- config -------------------------------------------------------------
    def _start_recognition(self) -> dict:
        tcfg = {"language": to_sm(self.language), "enable_partials": True,
                "max_delay": self.max_delay}
        if self.additional_vocab:
            tcfg["additional_vocab"] = [{"content": t} for t in self.additional_vocab]
        msg = {"message": "StartRecognition",
               "audio_format": {"type": "raw", "encoding": "pcm_s16le",
                                "sample_rate": self.sample_rate},
               "transcription_config": tcfg}
        if self.target_languages:
            msg["translation_config"] = {
                "target_languages": [to_sm(l) for l in self.target_languages],
                "enable_partials": True}
        return msg

    def set_stream_offset(self, offset_ms: int) -> None:
        """Stream-timeline position corresponding to vendor audio t=0.
        Called by ResilientASR at session start and on every reconnect."""
        self._stream_offset_ms = offset_ms

    # -- normalization (unit-tested against fixtures) -----------------------
    def _normalize(self, msg: dict) -> TranscriptEvent | None:
        kind = _TRANSCRIPT_KINDS.get(msg.get("message"))
        if kind is None:
            return None
        meta = msg.get("metadata", {})
        start_rel = int(round(float(meta.get("start_time", 0.0)) * 1000))
        end_rel = int(round(float(meta.get("end_time", 0.0)) * 1000))
        return TranscriptEvent(
            kind=kind,
            text=meta.get("transcript", "").strip(),
            t_audio_start_ms=self._stream_offset_ms + start_rel,
            t_audio_end_ms=self._stream_offset_ms + end_rel,
            vendor=self.name,
            t_received_wall=time.monotonic(),
            vendor_raw=msg)

    def _draft(self, msg: dict):
        """Return (app_lang, text) for a translation message, else None."""
        if msg.get("message") not in _TRANSLATION_MESSAGES:
            return None
        text = " ".join(r.get("content", "") for r in msg.get("results", [])).strip()
        return to_app(msg.get("language", "")), text

    # -- lifecycle ----------------------------------------------------------
    def start(self, on_event: OnEvent, on_status: OnStatus, on_draft=None) -> None:
        self.on_event, self.on_status, self.on_draft = on_event, on_status, on_draft
        self._stop.clear()
        self._started.clear()
        self._seq = 0
        self._connect()
        self._sender = threading.Thread(target=self._send_loop, name="asr-sender",
                                        daemon=False)
        self._receiver = threading.Thread(target=self._recv_loop, name="asr-receiver",
                                          daemon=False)
        self._sender.start()
        self._receiver.start()

    def _connect(self) -> None:
        self._ws = websocket.create_connection(
            WS_URL, header=[f"Authorization: Bearer {self.api_key}"], timeout=10)
        # StartRecognition first; "connected" is emitted on RecognitionStarted.
        self._ws.send(json.dumps(self._start_recognition()))

    # -- audio path ---------------------------------------------------------
    def send_audio(self, chunk: AudioChunk) -> None:
        # Non-blocking: the audio feed thread must NEVER block here. The send
        # loop is gated on RecognitionStarted, so until that arrives nothing
        # drains the queue; a blocking put() would wedge the feed thread and, in
        # turn, deadlock flush_and_stop / reconnect. On a full queue, shed the
        # oldest chunk. ResilientASR replays from the ring buffer on reconnect,
        # so a chunk shed during a stalled/failed start is recovered.
        self._enqueue(chunk)

    def _enqueue(self, item) -> None:
        try:
            self._send_q.put_nowait(item)
        except queue.Full:
            try:
                self._send_q.get_nowait()
            except queue.Empty:
                pass
            try:
                self._send_q.put_nowait(item)
            except queue.Full:
                pass

    def _send_loop(self) -> None:
        # Audio must not be sent before RecognitionStarted, or the server errors.
        # Poll so we stay responsive to shutdown (_stop) and give up after ~15s
        # so a dead start triggers reconnect via the error status instead of
        # hanging the sender thread.
        deadline = time.monotonic() + 15.0
        while not self._started.wait(timeout=0.2):
            if self._stop.is_set():
                return
            if time.monotonic() >= deadline:
                self.on_status(status("error", "asr", "RecognitionStarted not received"))
                return
        while not self._stop.is_set():
            try:
                chunk = self._send_q.get(timeout=0.2)
            except queue.Empty:
                continue
            if chunk is None:
                break
            try:
                self._ws.send_binary(chunk.pcm16)
                self._seq += 1
            except Exception as e:   # noqa: BLE001 — surfaced as status
                self.on_status(status("error", "asr", f"send failed: {e}"))
                return
        self._send_end_of_stream()

    def _send_end_of_stream(self) -> None:
        try:
            self._ws.send(json.dumps({"message": "EndOfStream",
                                      "last_seq_no": self._seq}))
        except Exception:   # noqa: BLE001 — already closing
            pass

    # -- receive path -------------------------------------------------------
    def _recv_loop(self) -> None:
        while not self._stop.is_set():
            try:
                raw = self._ws.recv()
            except Exception as e:   # noqa: BLE001
                if not self._stop.is_set():
                    self.on_status(status("error", "asr", f"recv failed: {e}"))
                return
            if not raw:
                continue
            try:
                msg = json.loads(raw)
            except (ValueError, TypeError):
                continue
            m = msg.get("message")
            if m == "RecognitionStarted":
                self._started.set()
                self.on_status(status("info", "asr", "connected"))
                continue
            if m == "EndOfTranscript":
                return
            if m == "Error":
                self.on_status(status("error", "asr",
                                      f"vendor error {msg.get('type')}: {msg.get('reason', '')}"))
                return
            if m == "Warning":
                self.on_status(status("warn", "asr",
                                      f"{msg.get('type')}: {msg.get('reason', '')}"))
                continue
            if m in ("Info", "AudioAdded"):
                continue
            ev = self._normalize(msg)
            if ev is not None:
                self.on_event(ev)
                continue
            draft = self._draft(msg)
            if draft is not None:
                # Translation message: emit only if drafts are enabled, but always
                # consume it (do not fall through to the unknown-message warning).
                if self.on_draft is not None:
                    lang, text = draft
                    if text:
                        self.on_draft(lang, text)
                continue
            # Never swallow unrecognized vendor messages silently — a wrong schema
            # manifests as a storm of these. Warn (not error: a per-message issue
            # must not trigger a reconnect loop). Mirrors the ElevenLabs adapter.
            self.on_status(status("warn", "asr",
                                  f"vendor message: {json.dumps(msg)[:200]}"))

    def flush_and_stop(self, timeout_s: float = 8.0) -> None:
        # Enqueue the EndOfStream sentinel without blocking (the queue may be
        # full and the sender may still be gated on RecognitionStarted).
        self._enqueue(None)
        # _stop is the backstop: it unblocks a sender still waiting for
        # RecognitionStarted, so the join below can never hang on a session that
        # never started.
        self._stop.set()
        self._sender.join(timeout=timeout_s)
        # Close the WS BEFORE joining the receiver so its blocking recv()
        # unblocks immediately instead of burning the join timeout.
        try:
            self._ws.close()
        except Exception:   # noqa: BLE001 — already closing
            pass
        self._receiver.join(timeout=timeout_s)
