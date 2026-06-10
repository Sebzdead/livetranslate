import dataclasses
import json
import threading
import time
from datetime import datetime
from pathlib import Path

from .types import Sentence, Translation, StatusEvent, TranscriptEvent


class Store:
    """Append-only JSONL persistence (spec §5.6). One instance per session.

    Thread-safe: every writer thread may call write_* concurrently. A
    store-flush thread fsyncs all files every 2 s.
    """

    FLUSH_INTERVAL_S = 2.0

    def __init__(self, session_dir: Path):
        self.session_dir = session_dir
        self._lock = threading.Lock()
        self._files = {
            "events": open(session_dir / "events.jsonl", "a", buffering=1, encoding="utf-8"),
            "sentences": open(session_dir / "sentences.jsonl", "a", buffering=1, encoding="utf-8"),
            "translations": open(session_dir / "translations.jsonl", "a", buffering=1, encoding="utf-8"),
        }
        self._stop = threading.Event()
        self._flusher = threading.Thread(target=self._flush_loop, name="store-flush")
        self._flusher.start()

    @classmethod
    def create(cls, output_dir, *, config_snapshot: dict,
               adapter: str, model: str, glossary_hash: str) -> "Store":
        session_dir = Path(output_dir) / datetime.now().strftime("%Y%m%d-%H%M")
        session_dir.mkdir(parents=True, exist_ok=True)
        (session_dir / "meta.json").write_text(json.dumps({
            "config": config_snapshot, "adapter": adapter, "model": model,
            "glossary_hash": glossary_hash, "started": time.time(),
        }, indent=2, default=str), encoding="utf-8")
        return cls(session_dir)

    @classmethod
    def open_resume(cls, session_dir) -> "Store":
        return cls(Path(session_dir))

    def _append(self, name: str, obj: dict) -> None:
        line = json.dumps(obj, ensure_ascii=False)
        with self._lock:
            self._files[name].write(line + "\n")

    def write_sentence(self, s: Sentence) -> None:
        self._append("sentences", dataclasses.asdict(s))

    def write_translation(self, t: Translation) -> None:
        self._append("translations", dataclasses.asdict(t))

    def write_event(self, e: TranscriptEvent) -> None:
        self._append("events", {"type": "transcript", **dataclasses.asdict(e)})

    def write_status(self, e: StatusEvent) -> None:
        self._append("events", {"type": "status", **dataclasses.asdict(e)})

    def _flush_loop(self) -> None:
        import os
        while not self._stop.wait(self.FLUSH_INTERVAL_S):
            with self._lock:
                for f in self._files.values():
                    f.flush()
                    os.fsync(f.fileno())

    def close(self) -> None:
        import os
        self._stop.set()
        self._flusher.join(timeout=5)
        with self._lock:
            for f in self._files.values():
                f.flush()
                os.fsync(f.fileno())
                f.close()

    @staticmethod
    def load_resume(session_dir):
        """Rebuild finalized state for --resume. Returns (sentences, translations, next_sid)."""
        session_dir = Path(session_dir)
        sentences, translations = [], []
        sp = session_dir / "sentences.jsonl"
        if sp.exists():
            for line in sp.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    sentences.append(Sentence(**json.loads(line)))
        tp = session_dir / "translations.jsonl"
        if tp.exists():
            for line in tp.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    translations.append(Translation(**json.loads(line)))
        next_sid = (max((s.sid for s in sentences), default=-1)) + 1
        return sentences, translations, next_sid
