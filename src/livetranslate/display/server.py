import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from ..types import Sentence, StatusEvent, Translation

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"


class DisplayState:
    """Shared display state. Writers: segmenter / translation workers / watchdog.
    Readers: SSE handler threads. Condition variable wakes waiting clients."""

    def __init__(self, langs):
        self.langs = langs
        self._cond = threading.Condition()
        self.sentences = []
        self.translations = {l: {} for l in langs}
        self.tentative_tail = ""
        self.drafts = {l: "" for l in langs}   # live draft translation per lang
        self.statuses = []
        self.status_seq = 0   # monotonically increasing; incremented by add_status
        self.version = 0

    def _bump(self):
        self.version += 1
        self._cond.notify_all()

    def add_sentence(self, s: Sentence):
        with self._cond:
            self.sentences.append(s)
            self._bump()

    def add_translation(self, t: Translation):
        with self._cond:
            self.translations.setdefault(t.lang, {})[t.sid] = t
            self._bump()

    def set_tail(self, tail: str):
        with self._cond:
            self.tentative_tail = tail
            self._bump()

    def set_draft(self, lang: str, text: str):
        with self._cond:
            if lang in self.drafts:
                self.drafts[lang] = text
                self._bump()

    def add_status(self, e: StatusEvent):
        with self._cond:
            self.statuses.append(e)
            del self.statuses[:-200]
            self.status_seq += 1
            self._bump()

    def statuses_since(self, idx: int) -> tuple:
        """Return (new_items, new_total) where new_items are StatusEvents not yet seen.

        idx is the caller's last-known status_seq value (0 = never seen anything).
        Handles the trim-to-200 window gracefully: if idx is so old that some events
        have been trimmed, returns only the retained tail.
        Returns (list_of_StatusEvent, current_status_seq).
        """
        with self._cond:
            unseen = self.status_seq - idx
            if unseen <= 0:
                return ([], self.status_seq)
            # Clamp to what we actually retain (up to 200 items)
            new = self.statuses[max(0, len(self.statuses) - unseen):]
            return (list(new), self.status_seq)

    def wait_for_change(self, version, timeout=15.0):
        with self._cond:
            self._cond.wait_for(lambda: self.version != version, timeout=timeout)
            return self.version

    def snapshot_lang(self, lang, after_sid):
        with self._cond:
            if lang == "src":
                items = [{"type": "sentence", "sid": s.sid, "text": s.text,
                          "paragraph_break": s.paragraph_break}
                         for s in self.sentences if s.sid > after_sid]
            else:
                sent_by_sid = {s.sid: s for s in self.sentences}
                items = []
                for sid in sorted(self.translations.get(lang, {})):
                    if sid > after_sid:
                        t = self.translations[lang][sid]
                        pb = sent_by_sid[sid].paragraph_break if sid in sent_by_sid else False
                        items.append({"type": "translation", "sid": sid, "lang": lang,
                                      "text": t.text, "status": t.status,
                                      "paragraph_break": pb})
                draft = self.drafts.get(lang, "")
                if draft:
                    items.append({"type": "draft", "lang": lang, "text": draft})
            return items

    def lag_by_lang(self):
        with self._cond:
            newest = self.sentences[-1].sid if self.sentences else -1
            return {l: newest - max(self.translations[l], default=-1) for l in self.langs}


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    state = None
    font_scale = 1.6

    def log_message(self, fmt, *args):
        log.debug("http: " + fmt, *args)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._serve_static("index.html")
        elif parsed.path.startswith("/v/"):
            self._serve_view(parsed.path[3:])
        elif parsed.path == "/events":
            self._serve_sse(parse_qs(parsed.query).get("lang", ["src"])[0])
        elif parsed.path == "/api/health":
            body = json.dumps({"lag": self.state.lag_by_lang()}).encode()
            self._respond(200, "application/json", body)
        else:
            self._respond(404, "text/plain", b"not found")

    def _respond(self, code, ctype, body):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_static(self, name):
        body = (STATIC / name).read_bytes()
        self._respond(200, "text/html; charset=utf-8", body)

    def _serve_view(self, lang):
        html = (STATIC / "view.html").read_text(encoding="utf-8")
        html = (html.replace("{{LANG}}", lang)
                    .replace("{{DIR}}", "rtl" if lang == "ar" else "ltr")
                    .replace("{{FONT_SCALE}}", str(self.font_scale)))
        self._respond(200, "text/html; charset=utf-8", html.encode())

    def _serve_sse(self, lang):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        # Explicitly do NOT send Content-Length — this is an open stream.
        self.end_headers()
        self.wfile.flush()

        try:
            after_sid = int(self.headers.get("Last-Event-ID", -1))
        except ValueError:
            after_sid = -1
        version = -1
        sent_statuses = 0   # status_seq index: how many status events sent to this client
        try:
            while True:
                if lang == "status":
                    # Send any new StatusEvents that arrived since last iteration
                    new_items, sent_statuses = self.state.statuses_since(sent_statuses)
                    for e in new_items:
                        self._sse_send(None, {"type": "status", "level": e.level,
                                              "source": e.source, "message": e.message})
                    # Also send the periodic lag frame for the operator table
                    self._sse_send(None, {"type": "status", "lag": self.state.lag_by_lang()})
                else:
                    for item in self.state.snapshot_lang(lang, after_sid):
                        self._sse_send(item["sid"], item)
                        after_sid = max(after_sid, item["sid"])
                    # Send tail activity indicator on ALL non-status streams (src + audience)
                    self._sse_send(None, {"type": "tail", "text": self.state.tentative_tail})
                new_version = self.state.wait_for_change(version)
                if new_version == version:
                    # timeout — send keepalive
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                version = new_version
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

    def _sse_send(self, sid, obj):
        frame = b""
        if sid is not None:
            frame += f"id: {sid}\n".encode()
        frame += b"data: " + json.dumps(obj, ensure_ascii=False).encode() + b"\n\n"
        self.wfile.write(frame)
        self.wfile.flush()


class DisplayServer:
    def __init__(self, state: DisplayState, host: str, port: int, font_scale: float):
        handler = type("Handler", (_Handler,), {"state": state, "font_scale": font_scale})
        self._httpd = ThreadingHTTPServer((host, port), handler)
        # SSE client threads are daemon so they don't block shutdown()
        self._httpd.daemon_threads = True
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name="http-server", daemon=False
        )

    def start(self):
        self._thread.start()

    def stop(self):
        self._httpd.shutdown()
        self._thread.join(timeout=5)
        self._httpd.server_close()
