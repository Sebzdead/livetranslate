import logging
import queue
import threading
import time

from .audio import RingBuffer
from .display.server import DisplayServer, DisplayState
from .segmenter import Segmenter
from .store import Store
from .translate import LLMTranslator, TranslationWorker
from .types import AudioChunk, Sentence, StatusEvent, TranscriptEvent, Translation

log = logging.getLogger(__name__)


class Pipeline:
    """Wires source-agnostic stages: audio in via feed(), everything else
    happens on owned threads. Used identically by live runs and the harness
    (spec §8: 'what we test is what runs live')."""

    def __init__(self, cfg: dict, adapter, translator: LLMTranslator,
                 glossary_blocks: dict, domain_blurb: str,
                 enable_display: bool = True, resume_dir: str | None = None,
                 glossary_hash: str = "", stall_detector=None):
        self.cfg = cfg
        self.adapter = adapter
        self.translator = translator
        self.glossary_blocks = glossary_blocks
        self.domain_blurb = domain_blurb
        self.stall_detector = stall_detector   # optional; wired by the watchdog task
        langs = cfg["translate"]["targets"]
        if resume_dir:
            self.store = Store.open_resume(resume_dir)
            sentences, translations, next_sid = Store.load_resume(resume_dir)
        else:
            self.store = Store.create(cfg["session"]["output_dir"],
                                      config_snapshot=cfg,
                                      adapter=getattr(adapter, "name", "?"),
                                      model=cfg["translate"]["model"],
                                      glossary_hash=glossary_hash)
            sentences, translations, next_sid = [], [], 0
        self.state = DisplayState(langs=langs)
        for s in sentences:
            self.state.add_sentence(s)
        for t in translations:
            self.state.add_translation(t)
        self.ring = RingBuffer(seconds=cfg["audio"]["ring_seconds"])
        self.segmenter = Segmenter(max_words=cfg["segmenter"]["max_words"],
                                   max_pending_s=cfg["segmenter"]["max_pending_s"],
                                   next_sid=next_sid)
        self.event_q: queue.Queue = queue.Queue(maxsize=1024)
        self.workers = {lang: self._make_worker(lang) for lang in langs}
        self.display = (DisplayServer(self.state, cfg["display"]["host"],
                                      cfg["display"]["port"],
                                      cfg["display"]["font_scale"])
                        if enable_display else None)
        self._seg_thread = threading.Thread(target=self._segment_loop, name="segmenter")
        self._stop = threading.Event()
        self._draining = False
        self._draft_enabled = bool(cfg["display"].get("draft_translation"))

    def _make_worker(self, lang: str) -> TranslationWorker:
        return TranslationWorker(
            lang=lang, translator=self.translator,
            glossary_block=self.glossary_blocks.get(lang, ""),
            domain_blurb=self.domain_blurb, on_translation=self._on_translation,
            batch_threshold=self.cfg["translate"]["batch_threshold"],
            batch_max=self.cfg["translate"]["batch_max"])

    def restart_worker(self, lang: str) -> None:
        """Watchdog hook: recreate and start a dead worker (spec §5.8)."""
        self.workers[lang] = self._make_worker(lang)
        self.workers[lang].start()

    # -- callbacks from adapter threads ----------------------------------
    def _on_event(self, ev: TranscriptEvent) -> None:
        self.store.write_event(ev)
        if self.stall_detector is not None:
            self.stall_detector.event_received()
        self.event_q.put(ev)

    def _on_status(self, ev: StatusEvent) -> None:
        self.store.write_status(ev)
        self.state.add_status(ev)

    def _on_translation(self, t) -> None:
        self.store.write_translation(t)
        self.state.add_translation(t)

    def _on_draft(self, lang: str, text: str) -> None:
        # Speechmatics realtime translation: a fast, glossary-unaware draft shown
        # as a provisional italic line until the LLM translation for that region
        # of speech lands. Disabled unless display.draft_translation is set.
        # (The adapter also won't emit drafts when disabled; this guard is
        # defence-in-depth for any future draft-capable adapter.)
        if self._draft_enabled:
            self.state.set_draft(lang, text)

    # -- lifecycle --------------------------------------------------------
    def start(self) -> None:
        self.adapter.start(on_event=self._on_event, on_status=self._on_status,
                           on_draft=self._on_draft)
        for w in self.workers.values():
            w.start()
        self._seg_thread.start()
        if self.display:
            self.display.start()

    def feed(self, chunk: AudioChunk) -> None:
        self.ring.append(chunk)
        if self.stall_detector is not None:
            self.stall_detector.audio_sent(chunk.duration_ms)
        self.adapter.send_audio(chunk)

    def _segment_loop(self) -> None:
        while not self._stop.is_set():
            try:
                ev = self.event_q.get(timeout=0.5)
            except queue.Empty:
                for s in self.segmenter.check_pending():
                    self._emit_sentence(s)
                continue
            if ev is None:
                break
            if ev.kind == "partial":
                self.segmenter.on_event(ev)
                self.state.set_tail(self.segmenter.tentative_tail)
            else:
                for s in self.segmenter.on_event(ev):
                    self._emit_sentence(s)
                self.state.set_tail("")

    def _emit_sentence(self, s: Sentence) -> None:
        self.store.write_sentence(s)
        self.state.add_sentence(s)
        # Spec §3: a full translation queue must block the producer for that
        # language only, NEVER the segmenter. Use submit_nowait which sheds the
        # oldest pending sentence (a gap beats a global stall; the invariant
        # checker will surface the missing translation via the synthetic failed
        # Translation emitted here for the shed sentence).
        for lang, w in self.workers.items():
            if self._draining and not w.alive():
                continue              # dead worker during shutdown: skip it
            shed = w.submit_nowait(s)
            if shed is not None:
                # shed is the oldest sentence evicted to make room; synthesize
                # a terminal failed Translation so (sid, lang) invariant holds.
                self._on_status(StatusEvent(
                    level="warn", source=f"translate.{lang}",
                    message=f"queue full; shed sid={shed.sid}",
                    t_wall=time.monotonic()))
                self._on_translation(Translation(
                    sid=shed.sid, lang=lang,
                    text="⟨translation unavailable⟩", status="failed",
                    t_done_wall=time.monotonic(),
                    model=self.cfg["translate"]["model"],
                    attempt=0))

    def shutdown(self) -> None:
        """SIGINT order (spec §5.8): stop source (caller's job) ->
        flush_and_stop ASR -> drain translators (<=15 s) -> close store."""
        self._draining = True
        self.adapter.flush_and_stop()
        self.event_q.put(None)
        self._seg_thread.join(timeout=10)
        self._stop.set()
        for s in self.segmenter.flush():
            self._emit_sentence(s)
        for w in self.workers.values():
            w.stop(drain=True, timeout_s=15)
        if self.display:
            self.display.stop()
        self.store.close()
