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
