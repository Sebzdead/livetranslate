import logging
import time

from .types import Sentence, TranscriptEvent

log = logging.getLogger(__name__)

TERMINALS = ".!?…。！？"

class Segmenter:
    """Finalization state machine (spec §5.3). Single-threaded: only the
    segmenter thread calls on_event/check_pending/flush. Output sentences are
    monotonic, append-only, never revised.

    Known limitations
    -----------------
    Abbreviation splitting: the terminal-punctuation rule splits on any token
    ending in ``.!?…。！？``, so abbreviations like "Dr.", "U.S.", "e.g." will
    cause premature sentence splits. This matches the spec's literal rule
    (terminal punctuation + whitespace). Handling abbreviations is out of scope
    for v1 and would require a design change.

    Multi-sentence final timestamps: when one final yields multiple sentences,
    the 2nd+ sentences receive approximate (possibly zero-duration) audio
    timestamps because per-token times are not available. This is an accepted
    approximation (timestamps only feed latency metrics and paragraph hints).
    """

    DEDUPE_SLACK_MS = 250
    PARAGRAPH_GAP_MS = 4000

    def __init__(self, max_words: int = 45, max_pending_s: float = 12.0,
                 next_sid: int = 0):
        self.max_words, self.max_pending_s = max_words, max_pending_s
        self._next_sid = next_sid
        self._buf: list[str] = []            # committed, unemitted tokens
        self._buf_start_ms = None            # int | None
        self._buf_first_wall = None          # float | None
        self._last_committed_end_ms = -(self.DEDUPE_SLACK_MS + 1)  # newest committed audio time
        self._last_emitted_end_ms = -1       # for paragraph_break gap
        self._last_emitted_tokens: list[str] = []   # instance attr (NOT class-level)
        self.tentative_tail = ""

    # -- input ----------------------------------------------------------
    def on_event(self, ev: TranscriptEvent) -> list[Sentence]:
        if ev.kind == "partial":
            self.tentative_tail = _norm_ws(ev.text)
            return []
        text = _norm_ws(ev.text)
        if not text:
            return []
        # Drop as duplicate/out-of-order only when the final actually re-covers
        # already-committed audio (it starts before the committed point). The
        # SLACK absorbs timestamp jitter on reconnect-replay re-transcriptions.
        # Forward-only finals that merely END close to the committed point
        # introduce NEW audio (e.g. Speechmatics' contiguous word-level finals,
        # whose start == the previous final's end) and must fall through to the
        # normal append path; true boundary overlaps are handled by
        # _merge_overlap below.
        if (ev.t_audio_start_ms < self._last_committed_end_ms
                and ev.t_audio_end_ms <= self._last_committed_end_ms + self.DEDUPE_SLACK_MS):
            log.warning("segmenter: dropped duplicate/out-of-order final "
                        "(start=%d < committed=%d, end=%d <= committed+%d): %r",
                        ev.t_audio_start_ms, self._last_committed_end_ms,
                        ev.t_audio_end_ms, self.DEDUPE_SLACK_MS, text)
            return []
        tokens = text.split()
        if ev.t_audio_start_ms < self._last_committed_end_ms:
            tokens = self._merge_overlap(tokens)
            if not tokens:
                return []
        if self._buf_start_ms is None and tokens:
            self._buf_start_ms = max(ev.t_audio_start_ms, self._last_committed_end_ms)
            self._buf_first_wall = ev.t_received_wall
        self._buf.extend(tokens)
        self._last_committed_end_ms = ev.t_audio_end_ms
        self.tentative_tail = ""
        return self._emit_ready(ev.t_audio_end_ms)

    def _merge_overlap(self, tokens: list[str]) -> list[str]:
        """Boundary-overlapping final: drop the longest prefix of `tokens`
        that equals a suffix of what we already committed. Log decision.
        Scans the full current buffer (bounded by max_words) rather than a
        fixed 12-token window so that overlaps longer than 12 tokens are not
        missed."""
        committed_tail = self._buf if self._buf else self._last_emitted_tokens
        for n in range(min(len(committed_tail), len(tokens)), 0, -1):
            if [t.lower() for t in committed_tail[-n:]] == [t.lower() for t in tokens[:n]]:
                log.info("segmenter: merged boundary overlap, dropped %d tokens: %r",
                         n, tokens[:n])
                return tokens[n:]
        log.info("segmenter: boundary final had no token overlap; keeping all")
        return tokens

    # -- emission rules ---------------------------------------------------
    def _emit_ready(self, end_ms: int) -> list[Sentence]:
        out: list[Sentence] = []
        while True:
            s = self._try_take_sentence(end_ms)
            if s is None:
                return out
            out.append(s)

    def _try_take_sentence(self, end_ms: int):
        if not self._buf:
            return None
        for i, tok in enumerate(self._buf):
            if tok and tok[-1] in TERMINALS:
                return self._emit(self._buf[:i + 1], end_ms)
        if len(self._buf) >= self.max_words:
            cut = self.max_words
            for i in range(self.max_words - 1, -1, -1):
                if self._buf[i].endswith((",", ";", ":")):
                    cut = i + 1
                    break
            return self._emit(self._buf[:cut], end_ms)
        return None

    def check_pending(self, now_wall: float | None = None) -> list[Sentence]:
        """Spec rule 3c: force-cut text older than max_pending_s.

        After the forced cut the remainder may itself be ready (terminal
        punctuation or >= max_words), so we drain it with _emit_ready.
        """
        now_wall = time.monotonic() if now_wall is None else now_wall
        if (self._buf and self._buf_first_wall is not None
                and now_wall - self._buf_first_wall >= self.max_pending_s):
            cut = min(len(self._buf), self.max_words)
            for i in range(min(len(self._buf), self.max_words) - 1, -1, -1):
                if self._buf[i].endswith((",", ";", ":")):
                    cut = i + 1
                    break
            forced = self._emit(self._buf[:cut], self._last_committed_end_ms)
            return [forced] + self._emit_ready(self._last_committed_end_ms)
        return []

    def flush(self) -> list[Sentence]:
        """Session end: emit any remainder as a final sentence."""
        if not self._buf:
            return []
        return [self._emit(self._buf[:], self._last_committed_end_ms)]

    def _emit(self, tokens: list[str], end_ms: int) -> Sentence:
        start_ms = self._buf_start_ms if self._buf_start_ms is not None else end_ms
        gap = start_ms - self._last_emitted_end_ms if self._last_emitted_end_ms >= 0 else 0
        s = Sentence(sid=self._next_sid, text=" ".join(tokens),
                     t_audio_start_ms=start_ms, t_audio_end_ms=end_ms,
                     t_finalized_wall=time.monotonic(),
                     paragraph_break=gap > self.PARAGRAPH_GAP_MS)
        self._next_sid += 1
        self._last_emitted_end_ms = end_ms
        self._last_emitted_tokens = tokens[-64:]
        del self._buf[:len(tokens)]
        if self._buf:
            self._buf_start_ms = end_ms      # approximation for the remainder
            self._buf_first_wall = time.monotonic()
        else:
            self._buf_start_ms = self._buf_first_wall = None
        return s

def _norm_ws(text: str) -> str:
    return " ".join(text.split())
