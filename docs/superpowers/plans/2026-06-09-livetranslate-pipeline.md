# livetranslate Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the single-machine live conference speech → multilingual reading-captions pipeline specified in `live-translation-pipeline-spec.md` (v1.0), milestone by milestone (M0–M7).

**Architecture:** Plain synchronous Python ≥ 3.11; concurrency via `threading` + `queue.Queue` only (no asyncio, no web frameworks). Audio source → ASR adapter (ElevenLabs primary, AssemblyAI failover/bake-off) behind a `ResilientASR` wrapper with RingBuffer replay → Segmenter (append-only Sentences) → per-language LLM translation workers → append-only JSONL Store → stdlib `ThreadingHTTPServer` + SSE displays. A file-driven harness runs the identical pipeline for WER/jargon/latency/chaos/soak testing.

**Tech Stack:** Python 3.11+, `sounddevice`, `numpy`, `websocket-client`, `requests`; harness-only `jiwer` + system `ffmpeg`; everything else stdlib.

**Binding constraints (from spec §2 — never violate):** no asyncio; no web frameworks; secrets only via env vars; append-only JSONL persistence; vendor wire formats verified against live docs (§13) before adapter implementation; dependency list is closed (§12).

**Source of truth:** `live-translation-pipeline-spec.md`. Where this plan and the spec disagree, the spec wins. Where the spec and live vendor docs disagree, the docs win (record divergence in `docs/vendor-notes.md`).

---

## File Structure

```
livetranslate/                      # repo root = current project dir
  pyproject.toml
  config.toml
  glossary.tsv                      # sample/dev copy; owner provides real one
  domain_blurb.txt
  docs/vendor-notes.md              # §13 findings, written during Tasks 8 & 20
  src/livetranslate/
    __init__.py
    __main__.py                     # CLI: live run / --resume / --help
    config.py                       # tomllib load + defaults + validation
    types.py                        # dataclasses (§4)
    logging_setup.py
    audio.py                        # MicSource, FileSource, RingBuffer
    asr/__init__.py
    asr/base.py                     # ASRAdapter protocol, ResilientASR
    asr/elevenlabs.py
    asr/assemblyai.py
    segmenter.py
    glossary.py
    translate.py
    store.py
    health.py
    display/__init__.py
    display/server.py
    display/static/index.html       # operator console
    display/static/view.html        # per-language audience page
  harness/
    __init__.py
    run_file.py
    metrics.py
    chaos.py
    bakeoff.py
  tests/                            # unit tests, no network; vendor fixtures
    fixtures/
  recordings/ refs/ sessions/       # gitignored data dirs
```

Each task below is one commit-sized unit. Run all tests with `python -m pytest tests/ -q` from the repo root.

---

## Milestone M0 — Skeleton

### Task 1: Repo bootstrap

**Files:**
- Create: `pyproject.toml`, `.gitignore`, `src/livetranslate/__init__.py`, `tests/__init__.py`

- [ ] **Step 1: Initialize git and package layout**

```bash
cd "/Users/sebastian/Documents/Workspace/Live Translation App"
git init
mkdir -p src/livetranslate/asr src/livetranslate/display/static harness tests/fixtures docs recordings refs sessions
touch src/livetranslate/__init__.py src/livetranslate/asr/__init__.py src/livetranslate/display/__init__.py harness/__init__.py tests/__init__.py
```

- [ ] **Step 2: Write `pyproject.toml`**

```toml
[project]
name = "livetranslate"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = ["sounddevice", "numpy", "websocket-client", "requests"]

[project.optional-dependencies]
harness = ["jiwer"]
dev = ["pytest"]

[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[tool.setuptools.packages.find]
where = ["src"]

[tool.pytest.ini_options]
testpaths = ["tests"]
```

- [ ] **Step 3: Write `.gitignore`**

```
__pycache__/
*.egg-info/
.venv/
recordings/
sessions/
.DS_Store
```

- [ ] **Step 4: Create venv, install editable, verify pytest runs**

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[harness,dev]'
.venv/bin/python -m pytest tests/ -q
```
Expected: `no tests ran` (exit 5 is fine at this stage).

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "chore: repo skeleton, packaging, gitignore"
```

---

### Task 2: Data types (`types.py`)

**Files:**
- Create: `src/livetranslate/types.py`
- Test: `tests/test_types.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_types.py
from livetranslate.types import AudioChunk, TranscriptEvent, Sentence, Translation, StatusEvent

def test_sentence_has_paragraph_break_default_false():
    s = Sentence(sid=1, text="Hello.", t_audio_start_ms=0, t_audio_end_ms=900,
                 t_finalized_wall=1.0)
    assert s.paragraph_break is False

def test_frozen_dataclasses_are_immutable():
    c = AudioChunk(pcm16=b"\x00\x00", sample_rate=16000, t_start_ms=0, duration_ms=100, seq=0)
    import dataclasses, pytest
    with pytest.raises(dataclasses.FrozenInstanceError):
        c.seq = 1  # type: ignore[misc]

def test_translation_fields():
    t = Translation(sid=3, lang="es", text="hola", status="ok",
                    t_done_wall=2.0, model="m", attempt=1)
    assert t.status in ("ok", "failed")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_types.py -q` — Expected: FAIL (ModuleNotFoundError / ImportError).

- [ ] **Step 3: Implement `src/livetranslate/types.py`** — exactly the spec §4 dataclasses, plus the `paragraph_break` field the spec §5.3 rule 5 requires:

```python
from dataclasses import dataclass

@dataclass(frozen=True)
class AudioChunk:
    pcm16: bytes            # mono, little-endian
    sample_rate: int        # 16000
    t_start_ms: int         # position on the session stream timeline
    duration_ms: int
    seq: int

@dataclass(frozen=True)
class TranscriptEvent:
    kind: str               # "partial" | "final"
    text: str
    t_audio_start_ms: int
    t_audio_end_ms: int
    vendor: str             # "elevenlabs" | "assemblyai"
    t_received_wall: float
    vendor_raw: dict

@dataclass(frozen=True)
class Sentence:
    sid: int                # monotonic, gapless per session
    text: str
    t_audio_start_ms: int
    t_audio_end_ms: int
    t_finalized_wall: float
    paragraph_break: bool = False

@dataclass(frozen=True)
class Translation:
    sid: int
    lang: str
    text: str
    status: str             # "ok" | "failed"
    t_done_wall: float
    model: str
    attempt: int

@dataclass
class StatusEvent:
    level: str              # "info" | "warn" | "error"
    source: str             # "asr" | "translate.es" | "watchdog" | ...
    message: str
    t_wall: float
```

- [ ] **Step 4: Run test to verify it passes** — `.venv/bin/python -m pytest tests/test_types.py -q` → PASS.
- [ ] **Step 5: Commit** — `git add -A && git commit -m "feat: core dataclasses (spec §4 + paragraph_break)"`

---

### Task 3: Config loading (`config.py`) + `config.toml`

**Files:**
- Create: `src/livetranslate/config.py`, `config.toml`
- Test: `tests/test_config.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_config.py
import pytest
from livetranslate.config import load_config

MINIMAL = b"""
[session]
source_language = "en"
[translate]
targets = ["es", "fr"]
"""

def test_defaults_applied(tmp_path):
    p = tmp_path / "c.toml"; p.write_bytes(MINIMAL)
    cfg = load_config(p)
    assert cfg["audio"]["chunk_ms"] == 100
    assert cfg["audio"]["ring_seconds"] == 120
    assert cfg["segmenter"]["max_words"] == 45
    assert cfg["segmenter"]["max_pending_s"] == 12
    assert cfg["asr"]["overlap_ms"] == 2000
    assert cfg["health"]["stall_s"] == 10
    assert cfg["harness"]["rtf"] == 1.0
    assert cfg["display"]["port"] == 8080
    assert cfg["translate"]["targets"] == ["es", "fr"]

def test_chunk_ms_range_validated(tmp_path):
    p = tmp_path / "c.toml"
    p.write_bytes(MINIMAL + b"\n[audio]\nchunk_ms = 500\n")
    with pytest.raises(ValueError, match="chunk_ms"):
        load_config(p)

def test_unknown_target_lang_rejected(tmp_path):
    p = tmp_path / "c.toml"
    p.write_bytes(b'[session]\nsource_language="en"\n[translate]\ntargets=["xx"]\n')
    with pytest.raises(ValueError, match="target"):
        load_config(p)

def test_no_secrets_in_config(tmp_path):
    p = tmp_path / "c.toml"
    p.write_bytes(MINIMAL + b'\n[asr.elevenlabs]\napi_key = "sk-123"\n')
    with pytest.raises(ValueError, match="secret"):
        load_config(p)
```

- [ ] **Step 2: Run test — FAIL** (`load_config` missing).

- [ ] **Step 3: Implement `src/livetranslate/config.py`**

```python
import copy
import tomllib
from pathlib import Path

ALLOWED_LANGS = {"es", "fr", "de", "pt", "ar", "zh"}

DEFAULTS: dict = {
    "session": {"source_language": "en", "output_dir": "sessions"},
    "audio": {"device_substring": "", "chunk_ms": 100, "ring_seconds": 120},
    "asr": {
        "adapter": "elevenlabs", "failover": "", "give_up_after_s": 0,
        "overlap_ms": 2000,
        "elevenlabs": {"keyterms_max": 100},
        "assemblyai": {"use_domain_prompt": True},
    },
    "segmenter": {"max_words": 45, "max_pending_s": 12},
    "translate": {
        "targets": ["es", "fr", "de", "pt"], "provider": "", "base_url": "",
        "model": "", "api_key_env": "TRANSLATE_API_KEY", "timeout_s": 10,
        "batch_threshold": 3, "batch_max": 6,
    },
    "glossary": {"path": "glossary.tsv", "domain_blurb": "domain_blurb.txt"},
    "display": {"host": "0.0.0.0", "port": 8080, "font_scale": 1.6,
                "draft_translation": False},
    "health": {"stall_s": 10},
    "harness": {"rtf": 1.0},
}

def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out

def _scan_for_secrets(d: dict, path: str = "") -> None:
    for k, v in d.items():
        if isinstance(v, dict):
            _scan_for_secrets(v, f"{path}{k}.")
        elif "key" in k.lower() and not k.endswith("_env"):
            raise ValueError(
                f"config field {path}{k} looks like a secret; "
                "secrets must come from environment variables only")

def load_config(path: str | Path) -> dict:
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    _scan_for_secrets(raw)
    cfg = _deep_merge(DEFAULTS, raw)
    if not 50 <= cfg["audio"]["chunk_ms"] <= 200:
        raise ValueError("audio.chunk_ms must be in [50, 200]")
    if cfg["audio"]["ring_seconds"] < 120:
        raise ValueError("audio.ring_seconds must be >= 120")
    bad = set(cfg["translate"]["targets"]) - ALLOWED_LANGS
    if bad:
        raise ValueError(f"unknown translate target(s): {sorted(bad)}")
    if cfg["session"]["source_language"] not in ("en", "de"):
        raise ValueError("session.source_language must be 'en' or 'de'")
    return cfg
```

- [ ] **Step 4: Write `config.toml`** at repo root — copy the spec §7 block verbatim, with `provider`/`base_url`/`model` left as `""` (filled at deploy/M3 after §13 verification; `"..."` placeholders from the spec would fail validation as real values, empty string means "not configured yet").

- [ ] **Step 5: Run tests — PASS.** Commit: `git commit -am "feat: config loading with defaults, validation, secret guard"`

---

### Task 4: JSONL Store (`store.py`)

**Files:**
- Create: `src/livetranslate/store.py`
- Test: `tests/test_store.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_store.py
import json, time
from livetranslate.store import Store
from livetranslate.types import Sentence, Translation, StatusEvent, TranscriptEvent

def _sentence(sid, text="Hello."):
    return Sentence(sid=sid, text=text, t_audio_start_ms=sid * 1000,
                    t_audio_end_ms=sid * 1000 + 900, t_finalized_wall=time.monotonic())

def test_appends_jsonl_lines(tmp_path):
    st = Store.create(tmp_path, config_snapshot={"a": 1}, adapter="elevenlabs",
                      model="test-model", glossary_hash="abc")
    st.write_sentence(_sentence(0))
    st.write_translation(Translation(sid=0, lang="es", text="Hola.", status="ok",
                                     t_done_wall=1.0, model="m", attempt=1))
    st.write_event(TranscriptEvent(kind="final", text="Hello.", t_audio_start_ms=0,
                                   t_audio_end_ms=900, vendor="elevenlabs",
                                   t_received_wall=1.0, vendor_raw={"x": 1}))
    st.write_status(StatusEvent(level="info", source="asr", message="connected", t_wall=1.0))
    st.close()
    sdir = st.session_dir
    assert json.loads((sdir / "sentences.jsonl").read_text().splitlines()[0])["sid"] == 0
    assert json.loads((sdir / "translations.jsonl").read_text().splitlines()[0])["lang"] == "es"
    events = [json.loads(l) for l in (sdir / "events.jsonl").read_text().splitlines()]
    assert {e["type"] for e in events} == {"transcript", "status"}
    meta = json.loads((sdir / "meta.json").read_text())
    assert meta["adapter"] == "elevenlabs" and meta["glossary_hash"] == "abc"

def test_resume_rebuilds_state_and_sid_counter(tmp_path):
    st = Store.create(tmp_path, config_snapshot={}, adapter="a", model="m", glossary_hash="h")
    st.write_sentence(_sentence(0)); st.write_sentence(_sentence(1, "World."))
    st.write_translation(Translation(sid=0, lang="es", text="Hola.", status="ok",
                                     t_done_wall=1.0, model="m", attempt=1))
    st.close()
    sentences, translations, next_sid = Store.load_resume(st.session_dir)
    assert [s.sid for s in sentences] == [0, 1]
    assert translations[0][("es",) if False else 0] if False else True  # see assert below
    assert (0, "es") in {(t.sid, t.lang) for t in translations}
    assert next_sid == 2
```

- [ ] **Step 2: Run test — FAIL.**

- [ ] **Step 3: Implement `src/livetranslate/store.py`**

```python
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
    def create(cls, output_dir: str | Path, *, config_snapshot: dict,
               adapter: str, model: str, glossary_hash: str) -> "Store":
        session_dir = Path(output_dir) / datetime.now().strftime("%Y%m%d-%H%M")
        session_dir.mkdir(parents=True, exist_ok=True)
        (session_dir / "meta.json").write_text(json.dumps({
            "config": config_snapshot, "adapter": adapter, "model": model,
            "glossary_hash": glossary_hash, "started": time.time(),
        }, indent=2, default=str), encoding="utf-8")
        return cls(session_dir)

    @classmethod
    def open_resume(cls, session_dir: str | Path) -> "Store":
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
                    f.flush(); os.fsync(f.fileno())

    def close(self) -> None:
        import os
        self._stop.set()
        self._flusher.join(timeout=5)
        with self._lock:
            for f in self._files.values():
                f.flush(); os.fsync(f.fileno()); f.close()

    @staticmethod
    def load_resume(session_dir: str | Path) -> tuple[list[Sentence], list[Translation], int]:
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
```

- [ ] **Step 4: Run tests — PASS.**
- [ ] **Step 5: Commit** — `git commit -am "feat: append-only JSONL store with fsync flusher and resume loader"`

---

### Task 5: Logging + CLI entry (`logging_setup.py`, `__main__.py`) — M0 DoD

**Files:**
- Create: `src/livetranslate/logging_setup.py`, `src/livetranslate/__main__.py`
- Test: `tests/test_cli.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cli.py
import subprocess, sys

def test_help_runs():
    r = subprocess.run([sys.executable, "-m", "livetranslate", "--help"],
                       capture_output=True, text=True)
    assert r.returncode == 0
    assert "--config" in r.stdout and "--resume" in r.stdout

def test_no_secrets_logged():
    from livetranslate.logging_setup import SecretRedactingFilter
    import logging
    f = SecretRedactingFilter(["sk-supersecret"])
    rec = logging.LogRecord("x", logging.INFO, "", 0, "key is sk-supersecret ok", (), None)
    f.filter(rec)
    assert "sk-supersecret" not in rec.getMessage()
```

- [ ] **Step 2: Run test — FAIL.**

- [ ] **Step 3: Implement `src/livetranslate/logging_setup.py`**

```python
import logging
import os

class SecretRedactingFilter(logging.Filter):
    def __init__(self, secrets: list[str] | None = None):
        super().__init__()
        env_secrets = [os.environ.get(k, "") for k in
                       ("ELEVENLABS_API_KEY", "ASSEMBLYAI_API_KEY", "TRANSLATE_API_KEY")]
        self.secrets = [s for s in (secrets or []) + env_secrets if s]

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        for s in self.secrets:
            if s in msg:
                msg = msg.replace(s, "***REDACTED***")
        record.msg, record.args = msg, ()
        return True

def setup_logging(level: str = "INFO") -> None:
    h = logging.StreamHandler()
    h.addFilter(SecretRedactingFilter())
    logging.basicConfig(level=level,
                        format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
                        handlers=[h])
```

- [ ] **Step 4: Implement `src/livetranslate/__main__.py`** (wiring grows in later tasks; for M0 it parses args, loads config, and exits — the `run_live` body is completed in Task 22):

```python
import argparse
import sys

from .config import load_config
from .logging_setup import setup_logging

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="livetranslate",
                                description="Live speech -> multilingual captions")
    p.add_argument("--config", required=True, help="path to config.toml")
    p.add_argument("--resume", metavar="SESSION_DIR",
                   help="resume a previous session directory")
    p.add_argument("--log-level", default="INFO")
    return p

def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.log_level)
    cfg = load_config(args.config)
    from .runner import run_live  # imported lazily; module added in Task 22
    return run_live(cfg, resume_dir=args.resume)

if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 5: Run tests — PASS.** Also verify M0 DoD manually: `.venv/bin/python -m livetranslate --config config.toml --help` exits 0.
- [ ] **Step 6: Commit** — `git commit -am "feat: CLI entrypoint, secret-redacting logging (M0 done)"`

---

## Milestone M1 — Audio plumbing + ElevenLabs adapter

### Task 6: RingBuffer + FileSource (`audio.py`)

**Files:**
- Create: `src/livetranslate/audio.py`
- Test: `tests/test_audio.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_audio.py
import shutil, subprocess, sys
import pytest
from livetranslate.audio import RingBuffer, FileSource
from livetranslate.types import AudioChunk

def chunk(t_ms, seq, dur=100, fill=b"\x01\x02"):
    return AudioChunk(pcm16=fill * (16 * dur), sample_rate=16000,
                      t_start_ms=t_ms, duration_ms=dur, seq=seq)

def test_ring_replay_from_returns_audio_from_ms():
    rb = RingBuffer(seconds=2, sample_rate=16000)
    for i in range(10):                       # 1 s of audio in 100 ms chunks
        rb.append(chunk(i * 100, i))
    out = list(rb.replay_from(300))
    assert out[0].t_start_ms == 300
    assert sum(c.duration_ms for c in out) == 700

def test_ring_evicts_old_audio():
    rb = RingBuffer(seconds=1, sample_rate=16000)
    for i in range(30):                       # 3 s into a 1 s ring
        rb.append(chunk(i * 100, i))
    with pytest.raises(KeyError):             # too old: evicted
        list(rb.replay_from(0))
    out = list(rb.replay_from(2500))
    assert out[0].t_start_ms == 2500

@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_filesource_decodes_and_paces(tmp_path):
    wav = tmp_path / "t.wav"                  # 1 s of silence @16k mono
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i",
                    "anullsrc=r=16000:cl=mono", "-t", "1", str(wav)],
                   check=True, capture_output=True)
    src = FileSource(str(wav), chunk_ms=100, rtf=20.0)   # fast for test
    chunks = list(src.chunks())
    assert sum(c.duration_ms for c in chunks) == pytest.approx(1000, abs=100)
    assert chunks[0].t_start_ms == 0 and chunks[1].seq == 1
    assert all(c.sample_rate == 16000 for c in chunks)
```

- [ ] **Step 2: Run — FAIL.**

- [ ] **Step 3: Implement `src/livetranslate/audio.py`**

```python
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
    """sounddevice capture; implemented in Task 23 (M7). Defined here so imports resolve."""

    def __init__(self, device_substring: str, chunk_ms: int = 100):
        self.device_substring, self.chunk_ms = device_substring, chunk_ms

    def chunks(self) -> Iterator[AudioChunk]:
        raise NotImplementedError("MicSource is implemented in M7 (Task 23)")
```

- [ ] **Step 4: Run — PASS.** Commit: `git commit -am "feat: RingBuffer with ms-addressed replay; ffmpeg-paced FileSource"`

---

### Task 7: ASR adapter base + fake adapter for tests (`asr/base.py`)

**Files:**
- Create: `src/livetranslate/asr/base.py`, `tests/fakes.py`
- Test: `tests/test_asr_base.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_asr_base.py
from livetranslate.asr.base import ASRAdapter
from tests.fakes import FakeAdapter
from livetranslate.types import AudioChunk

def test_fake_adapter_conforms_and_emits():
    events, statuses = [], []
    a = FakeAdapter(scripted=[("partial", "hel", 0, 300), ("final", "hello.", 0, 600)])
    a.start(on_event=events.append, on_status=statuses.append)
    a.send_audio(AudioChunk(b"\x00\x00" * 1600, 16000, 0, 100, 0))
    a.flush_and_stop()
    assert [e.kind for e in events] == ["partial", "final"]
    assert events[1].text == "hello."
    assert isinstance(a, ASRAdapter)
```

- [ ] **Step 2: Run — FAIL.**

- [ ] **Step 3: Implement `src/livetranslate/asr/base.py`** (interface only here; `ResilientASR` is Task 18):

```python
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
```

And `tests/fakes.py`:

```python
import time
from livetranslate.types import AudioChunk, TranscriptEvent

class FakeAdapter:
    """Scripted adapter for unit tests: emits the scripted events once audio
    covering their end time has been sent."""
    name = "fake"

    def __init__(self, scripted: list[tuple[str, str, int, int]]):
        self.scripted = list(scripted)
        self.audio_ms_sent = 0
        self.started = False
        self.stopped = False
        self.sent_chunks: list[AudioChunk] = []

    def start(self, on_event, on_status):
        self.on_event, self.on_status = on_event, on_status
        self.started = True

    def send_audio(self, chunk: AudioChunk) -> None:
        self.sent_chunks.append(chunk)
        self.audio_ms_sent = chunk.t_start_ms + chunk.duration_ms
        self._emit_ready()

    def _emit_ready(self):
        while self.scripted and self.scripted[0][3] <= self.audio_ms_sent:
            kind, text, s, e = self.scripted.pop(0)
            self.on_event(TranscriptEvent(kind=kind, text=text, t_audio_start_ms=s,
                                          t_audio_end_ms=e, vendor="fake",
                                          t_received_wall=time.monotonic(), vendor_raw={}))

    def flush_and_stop(self, timeout_s: float = 8.0) -> None:
        self.audio_ms_sent = 10 ** 12
        self._emit_ready()
        self.stopped = True
```

- [ ] **Step 4: Run — PASS.** Commit: `git commit -am "feat: ASRAdapter protocol + scripted FakeAdapter for tests"`

---

### Task 8: §13 doc verification — ElevenLabs + LLM provider (`docs/vendor-notes.md`)

No code. This gates Task 9 and Task 13. **Do not skip; do not trust the spec's indicative field names.**

- [ ] **Step 1:** WebFetch/WebSearch the current **ElevenLabs Scribe v2 Realtime** docs and record in `docs/vendor-notes.md`: WS endpoint URL + auth header/query param; exact JSON schema for session config, audio messages, partial and final transcript messages; supported encodings (confirm PCM16 @ 16 kHz); keyterm parameter name, cap, surcharge; language pinning param; keepalive requirement; **max session duration / idle timeout**; faster-than-realtime tolerance; finalization/commit semantics.
- [ ] **Step 2:** Pick + document the translation LLM provider(s) (chat endpoint schema for primary and one fallback — e.g. Anthropic Messages API and an OpenAI-compatible endpoint); rate limits vs worst case (6 languages × batch of 6); rough cost per 2 h session (log into `meta.json` per §13).
- [ ] **Step 3:** Save 2–3 **verbatim sample WS messages** (a config ack, a partial, a final) into `tests/fixtures/elevenlabs_messages.json` — these become the unit-test fixtures for Task 9.
- [ ] **Step 4:** Note any spec-vs-docs divergences in `docs/vendor-notes.md` ("docs win" rule). Commit: `git commit -am "docs: §13 vendor verification — ElevenLabs + LLM provider"`

### Task 9: ElevenLabsScribeAdapter (`asr/elevenlabs.py`) — M1 DoD

**Files:**
- Create: `src/livetranslate/asr/elevenlabs.py`
- Test: `tests/test_elevenlabs_adapter.py` (fixtures from Task 8; **no network in tests**)

The wire details below MUST be adjusted to `docs/vendor-notes.md`; the *structure* is fixed:

- [ ] **Step 1: Write the failing tests** — feed fixture messages through the adapter's message-normalization function and assert normalized `TranscriptEvent`s:

```python
# tests/test_elevenlabs_adapter.py
import json, pathlib
from livetranslate.asr.elevenlabs import ElevenLabsScribeAdapter

FIXTURES = json.loads((pathlib.Path(__file__).parent / "fixtures" /
                       "elevenlabs_messages.json").read_text())

def make_adapter():
    return ElevenLabsScribeAdapter(api_key="test", language="en", keyterms=["Tübingen"])

def test_partial_normalized_with_stream_offset():
    a = make_adapter()
    a._stream_offset_ms = 5000      # session connected when stream time was 5 s
    ev = a._normalize(FIXTURES["partial"])
    assert ev.kind == "partial" and ev.vendor == "elevenlabs"
    assert ev.t_audio_start_ms >= 5000          # vendor-relative -> stream timeline
    assert ev.vendor_raw == FIXTURES["partial"]

def test_final_normalized():
    a = make_adapter()
    a._stream_offset_ms = 0
    ev = a._normalize(FIXTURES["final"])
    assert ev.kind == "final" and ev.text.strip()

def test_non_transcript_messages_return_none():
    a = make_adapter()
    assert a._normalize(FIXTURES["config_ack"]) is None
```

- [ ] **Step 2: Run — FAIL.**

- [ ] **Step 3: Implement** `src/livetranslate/asr/elevenlabs.py` with this fixed structure (fill `WS_URL`, auth, message field names from vendor-notes):

```python
import json
import logging
import queue
import threading
import time

import websocket  # websocket-client (blocking)

from ..types import AudioChunk, TranscriptEvent
from .base import OnEvent, OnStatus, status

log = logging.getLogger(__name__)

class ElevenLabsScribeAdapter:
    name = "elevenlabs"
    # >>> All wire constants below come from docs/vendor-notes.md (§13) <<<
    WS_URL = "<from vendor-notes>"

    def __init__(self, api_key: str, language: str, keyterms: list[str],
                 sample_rate: int = 16000):
        if len(keyterms) > 100:
            keyterms = keyterms[:100]
        log.info("elevenlabs adapter: %d keyterms (surcharged)", len(keyterms))
        self.api_key, self.language, self.keyterms = api_key, language, keyterms
        self.sample_rate = sample_rate
        self._send_q: queue.Queue[AudioChunk | None] = queue.Queue(maxsize=64)
        self._stream_offset_ms = 0      # stream time at vendor t=0 (set on connect)
        self._ws = None
        self._stop = threading.Event()

    # -- lifecycle ------------------------------------------------------
    def start(self, on_event: OnEvent, on_status: OnStatus) -> None:
        self.on_event, self.on_status = on_event, on_status
        self._connect()
        self._sender = threading.Thread(target=self._send_loop, name="asr-sender")
        self._receiver = threading.Thread(target=self._recv_loop, name="asr-receiver")
        self._sender.start(); self._receiver.start()

    def _connect(self) -> None:
        self._ws = websocket.create_connection(
            self.WS_URL, header=self._auth_headers(), timeout=10)
        self._ws.send(json.dumps(self._session_config()))
        self.on_status(status("info", "asr", "connected"))

    def _auth_headers(self) -> list[str]:
        return [f"xi-api-key: {self.api_key}"]          # verify per vendor-notes

    def _session_config(self) -> dict:
        # Exact field names per vendor-notes: language pin, PCM16/16k, partials on, keyterms.
        return {"language": self.language, "keyterms": self.keyterms,
                "audio_format": {"encoding": "pcm_s16le", "sample_rate": self.sample_rate}}

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
                self._send_end_of_audio()
                return
            try:
                self._ws.send_binary(chunk.pcm16)   # or JSON-wrapped per vendor-notes
            except Exception as e:                   # noqa: BLE001 — surfaced as status
                self.on_status(status("error", "asr", f"send failed: {e}"))
                return

    def _send_end_of_audio(self) -> None:
        self._ws.send(json.dumps({"type": "end_of_audio"}))  # verify per vendor-notes

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
            msg = json.loads(raw)
            ev = self._normalize(msg)
            if ev is not None:
                self.on_event(ev)

    def _normalize(self, msg: dict) -> TranscriptEvent | None:
        """Vendor message -> normalized TranscriptEvent on the stream timeline.
        Field names per docs/vendor-notes.md; tested against fixtures."""
        kind = self._classify(msg)                  # "partial" | "final" | None
        if kind is None:
            return None
        return TranscriptEvent(
            kind=kind,
            text=self._extract_text(msg),
            t_audio_start_ms=self._stream_offset_ms + self._extract_start_ms(msg),
            t_audio_end_ms=self._stream_offset_ms + self._extract_end_ms(msg),
            vendor=self.name, t_received_wall=time.monotonic(), vendor_raw=msg)

    # _classify/_extract_text/_extract_start_ms/_extract_end_ms: pure functions
    # over the vendor schema; write them directly from the Task 8 fixtures.

    def flush_and_stop(self, timeout_s: float = 8.0) -> None:
        self._send_q.put(None)
        self._sender.join(timeout=timeout_s)
        deadline = time.monotonic() + timeout_s
        self._receiver.join(timeout=max(0.1, deadline - time.monotonic()))
        self._stop.set()
        try:
            self._ws.close()
        except Exception:                            # noqa: BLE001 — already closing
            pass
```

- [ ] **Step 4: Run unit tests — PASS.**

- [ ] **Step 5: M1 DoD smoke (manual, networked, not in pytest):** add `harness/smoke_m1.py` that wires `FileSource → adapter → print finals`:

```python
# harness/smoke_m1.py
import os, sys
from livetranslate.audio import FileSource
from livetranslate.asr.elevenlabs import ElevenLabsScribeAdapter

def main(path: str):
    a = ElevenLabsScribeAdapter(api_key=os.environ["ELEVENLABS_API_KEY"],
                                language="en", keyterms=[])
    a.start(on_event=lambda e: print(f"[{e.kind}] {e.t_audio_start_ms}-{e.t_audio_end_ms}ms: {e.text}"),
            on_status=lambda s: print(f"-- {s.level}: {s.message}"))
    for chunk in FileSource(path, chunk_ms=100, rtf=1.0).chunks():
        a.send_audio(chunk)
    a.flush_and_stop()

if __name__ == "__main__":
    main(sys.argv[1])
```

Run: `ELEVENLABS_API_KEY=... .venv/bin/python -m harness.smoke_m1 recordings/<5min-clip>` — Expected: finals print with sensible monotonically increasing stream-time ranges.

- [ ] **Step 6: Commit** — `git commit -am "feat: ElevenLabs Scribe v2 realtime adapter (M1 done)"`

---

## Milestone M2 — Segmenter

### Task 10: Segmenter state machine (`segmenter.py`)

**Files:**
- Create: `src/livetranslate/segmenter.py`
- Test: `tests/test_segmenter.py`

- [ ] **Step 1: Write the failing tests** — one per §5.3 rule + every listed edge case:

```python
# tests/test_segmenter.py
import time
from livetranslate.segmenter import Segmenter
from livetranslate.types import TranscriptEvent

def ev(kind, text, s, e):
    return TranscriptEvent(kind=kind, text=text, t_audio_start_ms=s, t_audio_end_ms=e,
                           vendor="fake", t_received_wall=time.monotonic(), vendor_raw={})

def make(**kw):
    return Segmenter(max_words=kw.get("max_words", 45),
                     max_pending_s=kw.get("max_pending_s", 12))

def test_partial_only_updates_tentative_tail():
    sg = make()
    out = sg.on_event(ev("partial", "hello wor", 0, 500))
    assert out == [] and sg.tentative_tail == "hello wor"

def test_terminal_punctuation_emits_sentence():
    sg = make()
    out = sg.on_event(ev("final", "Hello world.", 0, 900))
    assert len(out) == 1
    s = out[0]
    assert s.sid == 0 and s.text == "Hello world."
    assert (s.t_audio_start_ms, s.t_audio_end_ms) == (0, 900)
    assert sg.tentative_tail == ""          # cleared once content finalized past it

def test_sids_gapless_and_multiple_sentences_in_one_final():
    sg = make()
    out = sg.on_event(ev("final", "One. Two!", 0, 2000))
    assert [s.sid for s in out] == [0, 1]
    assert [s.text for s in out] == ["One.", "Two!"]

def test_cjk_terminal_punctuation():
    sg = make()
    out = sg.on_event(ev("final", "你好。", 0, 800))
    assert len(out) == 1

def test_max_words_cuts_at_clause_boundary():
    sg = make(max_words=8)
    text = "one two three four, five six seven eight nine ten"
    out = sg.on_event(ev("final", text, 0, 5000))
    assert out and out[0].text == "one two three four,"

def test_max_words_hard_cut_without_comma():
    sg = make(max_words=5)
    out = sg.on_event(ev("final", "a b c d e f g", 0, 3000))
    assert out and out[0].text == "a b c d e"

def test_max_pending_forces_emission():
    sg = make()
    sg.on_event(ev("final", "no punctuation here", 0, 1000))
    assert sg.check_pending(now_wall=time.monotonic() + 13) != []   # >12 s old

def test_reconnect_dedupe_drops_covered_final():
    sg = make()
    sg.on_event(ev("final", "Hello there.", 0, 1000))
    out = sg.on_event(ev("final", "Hello there.", 0, 1000))   # replayed duplicate
    assert out == []

def test_boundary_overlap_merged_by_token_overlap():
    sg = make()
    sg.on_event(ev("final", "the rate of", 0, 1500))
    out = sg.on_event(ev("final", "rate of profit is falling.", 1300, 3500))
    assert len(out) == 1
    assert out[0].text == "the rate of profit is falling."

def test_empty_and_whitespace_finals_ignored():
    sg = make()
    assert sg.on_event(ev("final", "   ", 0, 100)) == []
    assert sg.on_event(ev("final", "", 100, 200)) == []

def test_out_of_order_final_dropped():
    sg = make()
    sg.on_event(ev("final", "First sentence here.", 1000, 2000))
    assert sg.on_event(ev("final", "old.", 100, 500)) == []   # end <= last+250

def test_flush_emits_remainder():
    sg = make()
    sg.on_event(ev("final", "trailing words without punct", 0, 1000))
    out = sg.flush()
    assert len(out) == 1 and out[0].text == "trailing words without punct"

def test_paragraph_break_on_4s_gap():
    sg = make()
    a = sg.on_event(ev("final", "First.", 0, 1000))[0]
    b = sg.on_event(ev("final", "Second.", 6000, 7000))[0]
    assert a.paragraph_break is False and b.paragraph_break is True

def test_whitespace_normalized_casing_preserved():
    sg = make()
    out = sg.on_event(ev("final", "  Hello\t  World.  ", 0, 900))
    assert out[0].text == "Hello World."
```

- [ ] **Step 2: Run — FAIL.**

- [ ] **Step 3: Implement `src/livetranslate/segmenter.py`**

```python
import logging
import re
import time

from .types import Sentence, TranscriptEvent

log = logging.getLogger(__name__)

TERMINALS = ".!?…。！？"
_SPLIT_RE = re.compile(rf"(?<=[{re.escape(TERMINALS)}])\s+")

class Segmenter:
    """Finalization state machine (spec §5.3). Single-threaded: only the
    segmenter thread calls on_event/check_pending/flush. Output sentences are
    monotonic, append-only, never revised."""

    DEDUPE_SLACK_MS = 250
    PARAGRAPH_GAP_MS = 4000

    def __init__(self, max_words: int = 45, max_pending_s: float = 12.0,
                 next_sid: int = 0):
        self.max_words, self.max_pending_s = max_words, max_pending_s
        self._next_sid = next_sid
        self._buf: list[str] = []            # committed, unemitted tokens
        self._buf_start_ms: int | None = None
        self._buf_first_wall: float | None = None
        self._last_committed_end_ms = -1     # newest committed audio time
        self._last_emitted_end_ms = -1       # for paragraph_break gap
        self.tentative_tail = ""

    # -- input ----------------------------------------------------------
    def on_event(self, ev: TranscriptEvent) -> list[Sentence]:
        if ev.kind == "partial":
            self.tentative_tail = _norm_ws(ev.text)
            return []
        text = _norm_ws(ev.text)
        if not text:
            return []
        if ev.t_audio_end_ms <= self._last_committed_end_ms + self.DEDUPE_SLACK_MS:
            log.warning("segmenter: dropped duplicate/out-of-order final "
                        "(end=%d <= committed=%d+%d): %r", ev.t_audio_end_ms,
                        self._last_committed_end_ms, self.DEDUPE_SLACK_MS, text)
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
        that equals a suffix of what we already committed. Log decision."""
        committed_tail = self._buf[-12:] if self._buf else self._last_emitted_tokens[-12:]
        for n in range(min(len(committed_tail), len(tokens)), 0, -1):
            if [t.lower() for t in committed_tail[-n:]] == [t.lower() for t in tokens[:n]]:
                log.info("segmenter: merged boundary overlap, dropped %d tokens: %r",
                         n, tokens[:n])
                return tokens[n:]
        log.info("segmenter: boundary final had no token overlap; keeping all")
        return tokens

    _last_emitted_tokens: list[str] = []

    # -- emission rules ---------------------------------------------------
    def _emit_ready(self, end_ms: int) -> list[Sentence]:
        out: list[Sentence] = []
        while True:
            s = self._try_take_sentence(end_ms)
            if s is None:
                return out
            out.append(s)

    def _try_take_sentence(self, end_ms: int) -> Sentence | None:
        if not self._buf:
            return None
        # rule: terminal punctuation anywhere in the committed buffer
        for i, tok in enumerate(self._buf):
            if tok and tok[-1] in TERMINALS:
                return self._emit(self._buf[:i + 1], end_ms)
        # rule: max_words -> cut at last comma/clause boundary, else hard cut
        if len(self._buf) >= self.max_words:
            cut = self.max_words
            for i in range(self.max_words - 1, -1, -1):
                if self._buf[i].endswith((",", ";", ":")):
                    cut = i + 1
                    break
            return self._emit(self._buf[:cut], end_ms)
        return None

    def check_pending(self, now_wall: float | None = None) -> list[Sentence]:
        """Called periodically by the segmenter thread (spec rule 3c):
        force-cut text older than max_pending_s."""
        now_wall = time.monotonic() if now_wall is None else now_wall
        if (self._buf and self._buf_first_wall is not None
                and now_wall - self._buf_first_wall >= self.max_pending_s):
            cut = len(self._buf)
            for i in range(min(len(self._buf), self.max_words) - 1, -1, -1):
                if self._buf[i].endswith((",", ";", ":")):
                    cut = i + 1
                    break
            return [self._emit(self._buf[:cut], self._last_committed_end_ms)]
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
        self._last_emitted_tokens = tokens[-12:]
        del self._buf[:len(tokens)]
        if self._buf:
            self._buf_start_ms = end_ms      # approximation for the remainder
            self._buf_first_wall = time.monotonic()
        else:
            self._buf_start_ms = self._buf_first_wall = None
        return s

def _norm_ws(text: str) -> str:
    return " ".join(text.split())
```

Note: `_last_emitted_tokens` must be an **instance** attribute — initialize it in `__init__` (`self._last_emitted_tokens = []`) and delete the class-level line; the class-level form shown above is a known trap with mutable state shared across instances. Write a test for two independent Segmenter instances if in doubt.

- [ ] **Step 4: Run — PASS** (iterate on the emit bookkeeping until all 14 tests pass; the start_ms approximation for split buffers is acceptable per spec — timestamps only feed latency metrics and paragraph hints).
- [ ] **Step 5: Commit** — `git commit -am "feat: segmenter finalization state machine with dedupe + forced cuts"`

---

### Task 11: Invariant checks (`harness/metrics.py`, part 1) — M2 DoD

**Files:**
- Create: `harness/metrics.py`
- Test: `tests/test_invariants.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_invariants.py
from harness.metrics import check_invariants

def test_gapless_sids_pass():
    sentences = [{"sid": 0, "text": "A."}, {"sid": 1, "text": "B."}]
    translations = [{"sid": 0, "lang": "es", "status": "ok"},
                    {"sid": 1, "lang": "es", "status": "failed"}]
    assert check_invariants(sentences, translations, langs=["es"]) == []

def test_gap_in_sids_reported():
    errs = check_invariants([{"sid": 0, "text": "A."}, {"sid": 2, "text": "B."}], [], ["es"])
    assert any("gap" in e for e in errs)

def test_duplicate_text_reported():
    errs = check_invariants([{"sid": 0, "text": "Same."}, {"sid": 1, "text": "Same."}], [], [])
    assert any("duplicate" in e for e in errs)

def test_missing_or_double_translation_reported():
    sentences = [{"sid": 0, "text": "A."}]
    errs = check_invariants(sentences, [], ["es"])
    assert any("missing" in e for e in errs)
    errs = check_invariants(sentences,
                            [{"sid": 0, "lang": "es", "status": "ok"},
                             {"sid": 0, "lang": "es", "status": "ok"}], ["es"])
    assert any("multiple" in e for e in errs)
```

- [ ] **Step 2: Run — FAIL.**

- [ ] **Step 3: Implement in `harness/metrics.py`**

```python
from collections import Counter

def check_invariants(sentences: list[dict], translations: list[dict],
                     langs: list[str]) -> list[str]:
    """Spec §4 invariants. Returns a list of human-readable violations (empty = pass)."""
    errs: list[str] = []
    sids = [s["sid"] for s in sentences]
    for prev, cur in zip(sids, sids[1:]):
        if cur != prev + 1:
            errs.append(f"sid gap/regression: {prev} -> {cur}")
    texts = Counter(s["text"] for s in sentences)
    for text, n in texts.items():
        if n > 1:
            errs.append(f"duplicate sentence text x{n}: {text[:60]!r}")
    per = Counter((t["sid"], t["lang"]) for t in translations)
    for s in sentences:
        for lang in langs:
            n = per.get((s["sid"], lang), 0)
            if n == 0:
                errs.append(f"missing terminal translation: sid={s['sid']} lang={lang}")
            elif n > 1:
                errs.append(f"multiple terminal translations: sid={s['sid']} lang={lang}")
    return errs
```

- [ ] **Step 4: Run — PASS.**
- [ ] **Step 5: M2 DoD (manual, networked):** extend `harness/smoke_m1.py` into `harness/smoke_m2.py` that pipes adapter events through a `Segmenter` and a `Store`, then run a 20-min file and `check_invariants` over the session JSONL. Expected: zero violations.
- [ ] **Step 6: Commit** — `git commit -am "feat: invariant checks (M2 done)"`

---

## Milestone M3 — Glossary + Translator

### Task 12: Glossary (`glossary.py`)

**Files:**
- Create: `src/livetranslate/glossary.py`, `glossary.tsv` (3-row sample from spec §5.4), `domain_blurb.txt` (placeholder 2 sentences)
- Test: `tests/test_glossary.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_glossary.py
from livetranslate.glossary import Glossary

TSV = ("term_src\tes\tfr\tde\tpt\tar\tzh\tpriority\tnotes\n"
       "rate of profit\ttasa de ganancia\ttaux de profit\tProfitrate\ttaxa de lucro\t\t\t1\t\n"
       "Tübingen\tTübingen\tTübingen\tTübingen\tTübingen\t\t\t2\tkeep as-is\n"
       "long extra term here\t\t\t\t\t\t\t1\t\n")

def make(tmp_path):
    p = tmp_path / "g.tsv"; p.write_text(TSV, encoding="utf-8")
    return Glossary.load(p)

def test_keyterms_sorted_by_priority_then_length(tmp_path):
    g = make(tmp_path)
    # priority 1 first; within priority 1, longer first
    assert g.keyterms(cap=100) == ["long extra term here", "rate of profit", "Tübingen"]

def test_keyterms_truncated_with_warning(tmp_path, caplog):
    g = make(tmp_path)
    assert len(g.keyterms(cap=2)) == 2
    assert any("truncat" in r.message for r in caplog.records)

def test_glossary_block_keeps_empty_cells_as_source(tmp_path):
    g = make(tmp_path)
    block = g.block_for("es")
    assert "rate of profit → tasa de ganancia" in block
    # empty target cell => keep source term untranslated => identical rendering
    assert "long extra term here → long extra term here" in block

def test_normalized_term_set_for_metrics(tmp_path):
    g = make(tmp_path)
    assert "rate of profit" in g.normalized_terms()
    assert "tübingen" in g.normalized_terms()

def test_hash_stable(tmp_path):
    assert make(tmp_path).sha256 == make(tmp_path).sha256
```

- [ ] **Step 2: Run — FAIL.**

- [ ] **Step 3: Implement `src/livetranslate/glossary.py`**

```python
import csv
import hashlib
import logging
import unicodedata
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)
LANG_COLS = ["es", "fr", "de", "pt", "ar", "zh"]

@dataclass(frozen=True)
class Term:
    src: str
    targets: dict          # lang -> rendering ("" = keep source untranslated)
    priority: int
    notes: str

class Glossary:
    def __init__(self, terms: list[Term], sha256: str,
                 domain_blurb: str = ""):
        self.terms, self.sha256, self.domain_blurb = terms, sha256, domain_blurb

    @classmethod
    def load(cls, path: str | Path, blurb_path: str | Path | None = None) -> "Glossary":
        raw = Path(path).read_bytes()
        terms = []
        rows = csv.DictReader(raw.decode("utf-8").splitlines(), delimiter="\t")
        for row in rows:
            terms.append(Term(src=row["term_src"].strip(),
                              targets={l: (row.get(l) or "").strip() for l in LANG_COLS},
                              priority=int(row.get("priority") or 99),
                              notes=(row.get("notes") or "").strip()))
        blurb = ""
        if blurb_path and Path(blurb_path).exists():
            blurb = Path(blurb_path).read_text(encoding="utf-8").strip()
        return cls(terms, hashlib.sha256(raw).hexdigest(), blurb)

    def keyterms(self, cap: int) -> list[str]:
        ordered = sorted(self.terms, key=lambda t: (t.priority, -len(t.src)))
        out = [t.src for t in ordered]
        if len(out) > cap:
            log.warning("glossary: keyterm list truncated %d -> %d", len(out), cap)
            out = out[:cap]
        return out

    def block_for(self, lang: str) -> str:
        lines = []
        for t in self.terms:
            target = t.targets.get(lang, "") or t.src   # empty cell = keep source
            lines.append(f"{t.src} → {target}")
        return "\n".join(lines)

    def normalized_terms(self) -> set[str]:
        out = set()
        for t in self.terms:
            out.add(_norm(t.src))
            for v in t.targets.values():
                if v:
                    out.add(_norm(v))
        return out

def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s).lower()
    return " ".join(s.replace("-", " ").split())
```

- [ ] **Step 4: Run — PASS.** Create `glossary.tsv` with the spec §5.4 three sample rows and `domain_blurb.txt` with two placeholder sentences.
- [ ] **Step 5: Commit** — `git commit -am "feat: glossary TSV loader, keyterm derivation, MT blocks, metrics term set"`

---

### Task 13: LLMTranslator (`translate.py`)

**Files:**
- Create: `src/livetranslate/translate.py`
- Test: `tests/test_translate.py` (HTTP mocked via a tiny injected transport — no network)

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_translate.py
import time
import pytest
from livetranslate.translate import LLMTranslator, TransContext, build_messages
from livetranslate.types import Sentence

def sent(sid=0, text="The rate of profit falls."):
    return Sentence(sid=sid, text=text, t_audio_start_ms=0, t_audio_end_ms=1000,
                    t_finalized_wall=time.monotonic())

CFG = {"provider": "openai_chat", "base_url": "http://x", "model": "m",
       "timeout_s": 10, "api_key_env": "TRANSLATE_API_KEY",
       "batch_threshold": 3, "batch_max": 6}

def test_prompt_contains_glossary_and_context():
    ctx = TransContext(prev_source=["A.", "B."], prev_target="Bee.",
                       glossary_block="rate of profit → tasa de ganancia",
                       domain_blurb="A talk about economics.")
    msgs = build_messages(sent(), "es", "Spanish", ctx)
    system, user = msgs[0]["content"], msgs[1]["content"]
    assert "tasa de ganancia" in system and "Spanish" in system
    assert "Output ONLY the translation" in system
    assert "CONTEXT (source): A. B." in user
    assert "SOURCE: The rate of profit falls." in user

def test_success_returns_ok():
    tr = LLMTranslator(CFG, post=lambda url, hdrs, body, t: {"ok": True, "text": "La tasa cae."})
    out = tr.translate(sent(), "es", TransContext.empty("g"))
    assert out.status == "ok" and out.text == "La tasa cae." and out.lang == "es"

def test_retries_then_failed_placeholder():
    calls = []
    def post(url, hdrs, body, t):
        calls.append(1); raise TimeoutError("slow")
    tr = LLMTranslator(CFG, post=post, backoff_s=0)
    out = tr.translate(sent(), "es", TransContext.empty("g"))
    assert out.status == "failed" and out.text == "⟨translation unavailable⟩"
    assert len(calls) == 3                    # 1 try + 2 retries
    assert out.attempt == 3

def test_batch_translate_splits_numbered_response():
    resp = "1. Uno.\n2. Dos.\n3. Tres."
    tr = LLMTranslator(CFG, post=lambda *a: {"ok": True, "text": resp})
    sents = [sent(i, f"S{i}.") for i in range(3)]
    outs = tr.translate_batch(sents, "es", TransContext.empty("g"))
    assert [o.text for o in outs] == ["Uno.", "Dos.", "Tres."]
    assert [o.sid for o in outs] == [0, 1, 2]

def test_batch_parse_mismatch_falls_back_per_sentence():
    calls = {"n": 0}
    def post(url, hdrs, body, t):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"ok": True, "text": "garbled single line"}   # bad batch
        return {"ok": True, "text": f"T{calls['n']}"}
    tr = LLMTranslator(CFG, post=post)
    outs = tr.translate_batch([sent(0, "A."), sent(1, "B.")], "es", TransContext.empty("g"))
    assert len(outs) == 2 and all(o.status == "ok" for o in outs)
```

- [ ] **Step 2: Run — FAIL.**

- [ ] **Step 3: Implement `src/livetranslate/translate.py`**

```python
import logging
import os
import re
import time
from dataclasses import dataclass, field

import requests

from .types import Sentence, Translation

log = logging.getLogger(__name__)

LANG_NAMES = {"es": "Spanish", "fr": "French", "de": "German",
              "pt": "Portuguese", "ar": "Arabic", "zh": "Chinese"}

SYSTEM_TEMPLATE = """You are a professional simultaneous interpreter producing written captions for a live conference.
Translate the SOURCE sentence into {lang_name}.
Rules:
- Output ONLY the translation. No quotes, no notes, no commentary.
- Register: natural spoken-presentation {lang_name}; faithful to meaning; do not summarize or embellish.
- Keep numbers, units, and personal names exact.
- Apply this glossary strictly (source term → required rendering; identical rendering means keep the term untranslated):
{glossary_block}
- CONTEXT lines are for cohesion only. Translate ONLY the SOURCE line.
{domain_blurb_line}"""

@dataclass
class TransContext:
    prev_source: list[str] = field(default_factory=list)   # previous 2 source sentences
    prev_target: str = ""                                  # previous output in this lang
    glossary_block: str = ""
    domain_blurb: str = ""

    @classmethod
    def empty(cls, glossary_block: str = "") -> "TransContext":
        return cls(glossary_block=glossary_block)

def build_messages(sentence: Sentence, lang: str, lang_name: str,
                   ctx: TransContext) -> list[dict]:
    blurb_line = f"Subject of the event: {ctx.domain_blurb}" if ctx.domain_blurb else ""
    system = SYSTEM_TEMPLATE.format(lang_name=lang_name,
                                    glossary_block=ctx.glossary_block,
                                    domain_blurb_line=blurb_line)
    user_lines = []
    if ctx.prev_source:
        user_lines.append("CONTEXT (source): " + " ".join(ctx.prev_source[-2:]))
    if ctx.prev_target:
        user_lines.append("CONTEXT (your previous output): " + ctx.prev_target)
    user_lines.append("SOURCE: " + sentence.text)
    return [{"role": "system", "content": system},
            {"role": "user", "content": "\n".join(user_lines)}]

# ---- provider request/response mappings (verify schemas per §13) ----------
def _map_openai_chat(cfg: dict, messages: list[dict]) -> tuple[str, dict, dict]:
    url = cfg["base_url"].rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {os.environ[cfg['api_key_env']]}"}
    body = {"model": cfg["model"], "messages": messages, "temperature": 0}
    return url, headers, body

def _parse_openai_chat(resp_json: dict) -> str:
    return resp_json["choices"][0]["message"]["content"].strip()

def _map_anthropic(cfg: dict, messages: list[dict]) -> tuple[str, dict, dict]:
    url = cfg["base_url"].rstrip("/") + "/v1/messages"
    headers = {"x-api-key": os.environ[cfg["api_key_env"]],
               "anthropic-version": "2023-06-01"}
    system = next(m["content"] for m in messages if m["role"] == "system")
    rest = [m for m in messages if m["role"] != "system"]
    body = {"model": cfg["model"], "system": system, "messages": rest,
            "max_tokens": 1024, "temperature": 0}
    return url, headers, body

def _parse_anthropic(resp_json: dict) -> str:
    return resp_json["content"][0]["text"].strip()

PROVIDERS = {"openai_chat": (_map_openai_chat, _parse_openai_chat),
             "anthropic": (_map_anthropic, _parse_anthropic)}

def _default_post(url: str, headers: dict, body: dict, timeout_s: float) -> dict:
    r = requests.post(url, headers=headers, json=body, timeout=timeout_s)
    r.raise_for_status()
    return {"ok": True, "json": r.json()}

class LLMTranslator:
    """Synchronous translator (spec §5.5). `post` is injectable for tests;
    in production it is the requests-based default."""

    MAX_ATTEMPTS = 3      # 1 try + 2 retries

    def __init__(self, cfg: dict, post=None, backoff_s: float = 0.5,
                 fallback_cfg: dict | None = None):
        self.cfg, self.backoff_s = cfg, backoff_s
        self.fallback_cfg = fallback_cfg
        self._post = post or _default_post

    def _call(self, cfg: dict, messages: list[dict]) -> str:
        mapper, parser = PROVIDERS[cfg["provider"]]
        url, headers, body = mapper(cfg, messages)
        resp = self._post(url, headers, body, cfg["timeout_s"])
        if "text" in resp:           # test transport returns text directly
            return resp["text"]
        return parser(resp["json"])

    def _attempt_loop(self, messages: list[dict]) -> tuple[str, int, str]:
        """Returns (text, attempts_used, model) or raises last error."""
        last = None
        for attempt in range(1, self.MAX_ATTEMPTS + 1):
            try:
                return self._call(self.cfg, messages), attempt, self.cfg["model"]
            except Exception as e:                       # noqa: BLE001
                last = e
                log.warning("translate attempt %d failed: %s", attempt, e)
                if attempt < self.MAX_ATTEMPTS:
                    time.sleep(self.backoff_s * (2 ** (attempt - 1)))
        if self.fallback_cfg:
            try:
                return self._call(self.fallback_cfg, messages), self.MAX_ATTEMPTS, \
                       self.fallback_cfg["model"]
            except Exception as e:                       # noqa: BLE001
                last = e
        raise last

    def translate(self, sentence: Sentence, lang: str, ctx: TransContext) -> Translation:
        messages = build_messages(sentence, lang, LANG_NAMES[lang], ctx)
        try:
            text, attempts, model = self._attempt_loop(messages)
            return Translation(sid=sentence.sid, lang=lang, text=text, status="ok",
                               t_done_wall=time.monotonic(), model=model, attempt=attempts)
        except Exception:                                # noqa: BLE001 — terminal failure
            return Translation(sid=sentence.sid, lang=lang,
                               text="⟨translation unavailable⟩", status="failed",
                               t_done_wall=time.monotonic(), model=self.cfg["model"],
                               attempt=self.MAX_ATTEMPTS)

    def translate_batch(self, sentences: list[Sentence], lang: str,
                        ctx: TransContext) -> list[Translation]:
        """Catch-up batching: numbered-list request; on parse mismatch fall
        back to per-sentence calls (spec §5.5)."""
        numbered = "\n".join(f"{i+1}. {s.text}" for i, s in enumerate(sentences))
        pseudo = Sentence(sid=sentences[0].sid,
                          text=("Translate each numbered sentence separately; "
                                "reply with the same numbered list.\n" + numbered),
                          t_audio_start_ms=sentences[0].t_audio_start_ms,
                          t_audio_end_ms=sentences[-1].t_audio_end_ms,
                          t_finalized_wall=sentences[0].t_finalized_wall)
        messages = build_messages(pseudo, lang, LANG_NAMES[lang], ctx)
        try:
            text, attempts, model = self._attempt_loop(messages)
            parts = _split_numbered(text, len(sentences))
            if parts is not None:
                now = time.monotonic()
                return [Translation(sid=s.sid, lang=lang, text=p, status="ok",
                                    t_done_wall=now, model=model, attempt=attempts)
                        for s, p in zip(sentences, parts)]
            log.warning("batch parse mismatch (%d expected); falling back per-sentence",
                        len(sentences))
        except Exception as e:                           # noqa: BLE001
            log.warning("batch call failed (%s); falling back per-sentence", e)
        return [self.translate(s, lang, ctx) for s in sentences]

def _split_numbered(text: str, n: int) -> list[str] | None:
    items = re.findall(r"^\s*(\d+)[.)]\s*(.+?)(?=^\s*\d+[.)]|\Z)", text,
                       re.MULTILINE | re.DOTALL)
    if len(items) != n:
        return None
    return [body.strip() for _num, body in items]
```

- [ ] **Step 4: Run — PASS.**
- [ ] **Step 5: Commit** — `git commit -am "feat: LLM translator with provider mappings, retries, batch catch-up"`

---

### Task 14: TranslationWorker threads (`translate.py` additions)

**Files:**
- Modify: `src/livetranslate/translate.py`
- Test: `tests/test_translation_worker.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_translation_worker.py
import queue, time
from livetranslate.translate import TranslationWorker, LLMTranslator, TransContext
from livetranslate.types import Sentence

def sent(sid):
    return Sentence(sid=sid, text=f"Sentence {sid}.", t_audio_start_ms=sid * 1000,
                    t_audio_end_ms=sid * 1000 + 900, t_finalized_wall=time.monotonic())

CFG = {"provider": "openai_chat", "base_url": "http://x", "model": "m",
       "timeout_s": 10, "api_key_env": "TRANSLATE_API_KEY",
       "batch_threshold": 3, "batch_max": 6}

def test_worker_translates_in_order_and_keeps_context():
    seen_bodies = []
    def post(url, hdrs, body, t):
        seen_bodies.append(body)
        return {"ok": True, "text": "T"}
    out = []
    w = TranslationWorker(lang="es", translator=LLMTranslator(CFG, post=post),
                          glossary_block="g", domain_blurb="", on_translation=out.append,
                          batch_threshold=3, batch_max=6)
    w.start()
    for i in range(3):
        w.submit(sent(i))
    w.stop(drain=True)
    assert [t.sid for t in out] == [0, 1, 2]
    assert all(t.status == "ok" for t in out)

def test_worker_batches_when_queue_deep():
    calls = []
    def post(url, hdrs, body, t):
        calls.append(body)
        user = body["messages"][-1]["content"]
        n = user.count("\n1. ") + user.count("\n2. ")  # crude; real check below
        import re
        nums = re.findall(r"^\d+\.", user, re.M)
        return {"ok": True, "text": "\n".join(f"{i+1}. T{i}" for i in range(len(nums)))} \
               if nums else {"ok": True, "text": "T"}
    out = []
    w = TranslationWorker(lang="es", translator=LLMTranslator(CFG, post=post),
                          glossary_block="g", domain_blurb="", on_translation=out.append,
                          batch_threshold=3, batch_max=6)
    for i in range(6):                 # enqueue before starting -> deep queue
        w.submit(sent(i))
    w.start()
    w.stop(drain=True)
    assert [t.sid for t in out] == list(range(6))
    assert len(calls) < 6              # batching collapsed calls
```

- [ ] **Step 2: Run — FAIL.**

- [ ] **Step 3: Append to `src/livetranslate/translate.py`**

```python
import queue as _queue
import threading

class TranslationWorker:
    """One thread per enabled language consuming its own ordered queue (spec §5.5).
    Full queue blocks the producer for this language only (spec §3)."""

    def __init__(self, lang: str, translator: LLMTranslator, glossary_block: str,
                 domain_blurb: str, on_translation, batch_threshold: int = 3,
                 batch_max: int = 6, maxsize: int = 256):
        self.lang, self.translator = lang, translator
        self.on_translation = on_translation
        self.batch_threshold, self.batch_max = batch_threshold, batch_max
        self.q: _queue.Queue = _queue.Queue(maxsize=maxsize)
        self._ctx = TransContext(glossary_block=glossary_block, domain_blurb=domain_blurb)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"xlate-{lang}")
        self.alive = lambda: self._thread.is_alive()

    def start(self) -> None:
        self._thread.start()

    def submit(self, sentence: Sentence) -> None:
        self.q.put(sentence)           # blocking: per-language backpressure only

    def _run(self) -> None:
        while not (self._stop.is_set() and self.q.empty()):
            try:
                first = self.q.get(timeout=0.2)
            except _queue.Empty:
                continue
            if first is None:
                break
            batch = [first]
            if self.q.qsize() > self.batch_threshold:
                while len(batch) < self.batch_max:
                    try:
                        nxt = self.q.get_nowait()
                    except _queue.Empty:
                        break
                    if nxt is None:
                        self._stop.set(); break
                    batch.append(nxt)
            if len(batch) == 1:
                results = [self.translator.translate(batch[0], self.lang, self._ctx)]
            else:
                results = self.translator.translate_batch(batch, self.lang, self._ctx)
            for s, t in zip(batch, results):
                self._ctx.prev_source = (self._ctx.prev_source + [s.text])[-2:]
                if t.status == "ok":
                    self._ctx.prev_target = t.text
                self.on_translation(t)

    def stop(self, drain: bool = True, timeout_s: float = 15.0) -> None:
        if drain:
            self.q.put(None)
            self._thread.join(timeout=timeout_s)
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=1.0)
```

- [ ] **Step 4: Run — PASS.**
- [ ] **Step 5: M3 DoD (manual, networked):** wire smoke script with one language → console, then all four; check a seeded test set of ~20 glossary-bearing sentences and assert the required rendering appears in ≥ 95% of translations (`harness/metrics.py` glossary check arrives in Task 19; an inline grep is fine here).
- [ ] **Step 6: Commit** — `git commit -am "feat: per-language translation workers with catch-up batching (M3 done)"`

---

## Milestone M4 — DisplayServer

### Task 15: Display state + SSE server (`display/server.py`)

**Files:**
- Create: `src/livetranslate/display/server.py`
- Test: `tests/test_display_server.py` (real HTTP against an ephemeral port — stdlib server is cheap; still no external network)

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_display_server.py
import json, threading, time, urllib.request
import pytest
from livetranslate.display.server import DisplayServer, DisplayState
from livetranslate.types import Sentence, Translation

@pytest.fixture
def server():
    st = DisplayState(langs=["es", "ar"])
    srv = DisplayServer(state=st, host="127.0.0.1", port=0, font_scale=1.6)
    srv.start()
    yield srv, st
    srv.stop()

def url(srv, path):
    return f"http://127.0.0.1:{srv.port}{path}"

def test_view_page_serves_and_rtl_for_ar(server):
    srv, _ = server
    html = urllib.request.urlopen(url(srv, "/v/es")).read().decode()
    assert "EventSource" in html
    ar = urllib.request.urlopen(url(srv, "/v/ar")).read().decode()
    assert 'dir="rtl"' in ar

def test_operator_console_serves(server):
    srv, _ = server
    assert urllib.request.urlopen(url(srv, "/")).status == 200

def read_sse_events(resp, n, timeout=5):
    events, buf, t0 = [], b"", time.monotonic()
    while len(events) < n and time.monotonic() - t0 < timeout:
        chunk = resp.read1(4096)
        if not chunk:
            break
        buf += chunk
        while b"\n\n" in buf:
            frame, buf = buf.split(b"\n\n", 1)
            data = [l[5:].strip() for l in frame.split(b"\n") if l.startswith(b"data:")]
            if data:
                events.append(json.loads(b"".join(data)))
    return events

def test_sse_replays_then_streams(server):
    srv, st = server
    s0 = Sentence(0, "Hello.", 0, 900, 1.0)
    st.add_sentence(s0)
    st.add_translation(Translation(0, "es", "Hola.", "ok", 1.5, "m", 1))
    resp = urllib.request.urlopen(url(srv, "/events?lang=es"))
    threading.Timer(0.3, lambda: st.add_translation(
        Translation(1, "es", "Mundo.", "ok", 2.0, "m", 1))).start()
    threading.Timer(0.2, lambda: st.add_sentence(Sentence(1, "World.", 1000, 1900, 2.0))).start()
    events = read_sse_events(resp, 2)
    assert events[0]["type"] == "translation" and events[0]["text"] == "Hola."  # replay
    assert any(e.get("text") == "Mundo." for e in events)                       # live

def test_last_event_id_resumes_from_sid(server):
    srv, st = server
    for i in range(3):
        st.add_sentence(Sentence(i, f"S{i}.", i * 1000, i * 1000 + 900, 1.0))
        st.add_translation(Translation(i, "es", f"T{i}.", "ok", 1.0, "m", 1))
    req = urllib.request.Request(url(srv, "/events?lang=es"),
                                 headers={"Last-Event-ID": "0"})
    events = read_sse_events(urllib.request.urlopen(req), 2)
    assert [e["sid"] for e in events] == [1, 2]
```

- [ ] **Step 2: Run — FAIL.**

- [ ] **Step 3: Implement `src/livetranslate/display/server.py`**

```python
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

    def __init__(self, langs: list[str]):
        self.langs = langs
        self._cond = threading.Condition()
        self.sentences: list[Sentence] = []
        self.translations: dict[str, dict[int, Translation]] = {l: {} for l in langs}
        self.tentative_tail = ""
        self.statuses: list[StatusEvent] = []
        self.version = 0                       # bump on every mutation

    def _bump(self):
        self.version += 1
        self._cond.notify_all()

    def add_sentence(self, s: Sentence):
        with self._cond:
            self.sentences.append(s); self._bump()

    def add_translation(self, t: Translation):
        with self._cond:
            self.translations.setdefault(t.lang, {})[t.sid] = t; self._bump()

    def set_tail(self, tail: str):
        with self._cond:
            self.tentative_tail = tail; self._bump()

    def add_status(self, e: StatusEvent):
        with self._cond:
            self.statuses.append(e)
            del self.statuses[:-200]           # bounded
            self._bump()

    def wait_for_change(self, version: int, timeout: float = 15.0) -> int:
        with self._cond:
            self._cond.wait_for(lambda: self.version != version, timeout=timeout)
            return self.version

    def snapshot_lang(self, lang: str, after_sid: int) -> list[dict]:
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
            return items

    def lag_by_lang(self) -> dict[str, int]:
        with self._cond:
            newest = self.sentences[-1].sid if self.sentences else -1
            return {l: newest - max(self.translations[l], default=-1)
                    for l in self.langs}

class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    state: DisplayState = None        # injected by DisplayServer
    font_scale: float = 1.6

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
        self.end_headers()
        after_sid = int(self.headers.get("Last-Event-ID", -1))
        version = -1
        try:
            while True:
                for item in self.state.snapshot_lang(lang, after_sid):
                    self._sse_send(item["sid"], item)
                    after_sid = max(after_sid, item["sid"])
                if lang == "src":
                    self._sse_send(None, {"type": "tail",
                                          "text": self.state.tentative_tail})
                if lang == "status":
                    self._sse_send(None, {"type": "status",
                                          "lag": self.state.lag_by_lang()})
                new_version = self.state.wait_for_change(version)
                if new_version == version:     # timeout: SSE keepalive comment
                    self.wfile.write(b": keepalive\n\n"); self.wfile.flush()
                version = new_version
        except (BrokenPipeError, ConnectionResetError):
            return

    def _sse_send(self, sid, obj):
        frame = b""
        if sid is not None:
            frame += f"id: {sid}\n".encode()
        frame += b"data: " + json.dumps(obj, ensure_ascii=False).encode() + b"\n\n"
        self.wfile.write(frame); self.wfile.flush()

class DisplayServer:
    def __init__(self, state: DisplayState, host: str, port: int, font_scale: float):
        handler = type("Handler", (_Handler,), {"state": state, "font_scale": font_scale})
        self._httpd = ThreadingHTTPServer((host, port), handler)
        self._httpd.daemon_threads = False
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                        name="http-server")

    def start(self):
        self._thread.start()

    def stop(self):
        self._httpd.shutdown()
        self._thread.join(timeout=5)
        self._httpd.server_close()
```

- [ ] **Step 4: Run — PASS** (iterate; SSE framing over `http.server` is fiddly — keep `Content-Length` off the SSE response and don't call `_respond` there).
- [ ] **Step 5: Commit** — `git commit -am "feat: DisplayState + SSE server with Last-Event-ID replay"`

---

### Task 16: Static pages (`view.html`, `index.html`) — M4 DoD

**Files:**
- Create: `src/livetranslate/display/static/view.html`, `src/livetranslate/display/static/index.html`

- [ ] **Step 1: Write `view.html`** — audience page, single file, no build step:

```html
<!doctype html>
<html lang="{{LANG}}" dir="{{DIR}}">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>livetranslate — {{LANG}}</title>
<style>
  :root { --fg:#e8e8e8; --bg:#111; --dim:#888; }
  body.light { --fg:#111; --bg:#fafafa; --dim:#777; }
  body { background:var(--bg); color:var(--fg); margin:0;
         font:calc(1rem * {{FONT_SCALE}})/1.5 Georgia, "Noto Serif", serif; }
  #text { max-width: 60rem; margin: 0 auto; padding: 2rem; }
  p { margin: 0 0 1.2em; }
  #activity { color: var(--dim); }
  #toggle { position: fixed; top: .5rem; inset-inline-end: .5rem;
            background:none; border:1px solid var(--dim); color:var(--dim);
            border-radius: 4px; cursor: pointer; }
</style>
</head>
<body>
<button id="toggle">☀/☾</button>
<div id="text"><p id="current"></p></div>
<span id="activity" hidden>…</span>
<script>
const lang = "{{LANG}}";
let autoscroll = true, userScrollTimer = null;
addEventListener("scroll", () => {
  autoscroll = false;
  clearTimeout(userScrollTimer);
  userScrollTimer = setTimeout(() => { autoscroll = true; }, 4000);
});
document.getElementById("toggle").onclick = () => document.body.classList.toggle("light");
const textEl = document.getElementById("text");
let para = document.getElementById("current");
const es = new EventSource(`/events?lang=${lang}`);
es.onmessage = (m) => {
  const ev = JSON.parse(m.data);
  if (ev.type === "translation" || ev.type === "sentence") {
    if (ev.paragraph_break && para.textContent) {
      para = document.createElement("p"); textEl.appendChild(para);
    }
    para.textContent += (para.textContent ? " " : "") + ev.text;
    if (autoscroll) scrollTo(0, document.body.scrollHeight);
  } else if (ev.type === "tail") {
    document.getElementById("activity").hidden = !ev.text;
  }
};
</script>
</body>
</html>
```

- [ ] **Step 2: Write `index.html`** — operator console: subscribes to `/events?lang=src` (sentences + tail, tail in italic gray) and `/events?lang=status` (connection banner, per-language lag table, reconnect count, RSS). Same inline-everything style; render `lag` dict as a table refreshed on each status event; banner turns red on `level=="error"` status events. (Full file follows the same pattern as `view.html`; keep it under ~120 lines.)

- [ ] **Step 3: Manual M4 DoD check:** run a smoke session feeding fake sentences, open `/v/es`, refresh mid-stream — page must repopulate fully (Last-Event-ID replay). Then 10 parallel `curl -N` SSE clients stay stable for 2 min:

```bash
for i in $(seq 10); do curl -sN "http://127.0.0.1:8080/events?lang=es" >/dev/null & done
```

- [ ] **Step 4: Commit** — `git commit -am "feat: audience + operator pages (M4 done)"`

---

## Milestone M5 — Resilience

### Task 17: Harness pipeline runner (`harness/run_file.py`)

This comes before chaos because chaos and soak drive it.

**Files:**
- Create: `harness/run_file.py`, `src/livetranslate/pipeline.py`
- Test: `tests/test_pipeline.py` (FakeAdapter + fake translator transport; full offline run)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_pipeline.py
import json
from livetranslate.pipeline import Pipeline
from tests.fakes import FakeAdapter
from livetranslate.config import load_config, DEFAULTS
from livetranslate.translate import LLMTranslator

def test_offline_end_to_end(tmp_path):
    cfg = json.loads(json.dumps(DEFAULTS))
    cfg["session"]["output_dir"] = str(tmp_path)
    cfg["translate"].update(provider="openai_chat", base_url="http://x", model="m")
    cfg["translate"]["targets"] = ["es"]
    adapter = FakeAdapter(scripted=[("final", "Hello world.", 0, 900),
                                    ("final", "Second sentence.", 1000, 2000)])
    translator = LLMTranslator(cfg["translate"],
                               post=lambda *a: {"ok": True, "text": "X."})
    p = Pipeline(cfg, adapter=adapter, translator=translator,
                 glossary_block="", domain_blurb="", enable_display=False)
    p.start()
    from livetranslate.types import AudioChunk
    for i in range(25):    # 2.5 s of audio
        p.feed(AudioChunk(b"\x00" * 3200, 16000, i * 100, 100, i))
    p.shutdown()
    sentences = [json.loads(l) for l in
                 (p.store.session_dir / "sentences.jsonl").read_text().splitlines()]
    translations = [json.loads(l) for l in
                    (p.store.session_dir / "translations.jsonl").read_text().splitlines()]
    assert [s["sid"] for s in sentences] == [0, 1]
    assert {(t["sid"], t["lang"]) for t in translations} == {(0, "es"), (1, "es")}
```

- [ ] **Step 2: Run — FAIL.**

- [ ] **Step 3: Implement `src/livetranslate/pipeline.py`** — the composition root used by both live runs and the harness:

```python
import logging
import queue
import threading

from .audio import RingBuffer
from .display.server import DisplayServer, DisplayState
from .glossary import Glossary
from .segmenter import Segmenter
from .store import Store
from .translate import LLMTranslator, TranslationWorker
from .types import AudioChunk, Sentence, StatusEvent, TranscriptEvent

log = logging.getLogger(__name__)

class Pipeline:
    """Wires source-agnostic stages: audio in via feed(), everything else
    happens on owned threads. Used identically by live runs and the harness
    (spec §8: 'what we test is what runs live')."""

    def __init__(self, cfg: dict, adapter, translator: LLMTranslator,
                 glossary_block: str, domain_blurb: str,
                 enable_display: bool = True, resume_dir: str | None = None,
                 glossary_hash: str = ""):
        self.cfg = cfg
        self.adapter = adapter
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
        self.workers = {
            lang: TranslationWorker(
                lang=lang, translator=translator, glossary_block=glossary_block,
                domain_blurb=domain_blurb, on_translation=self._on_translation,
                batch_threshold=cfg["translate"]["batch_threshold"],
                batch_max=cfg["translate"]["batch_max"])
            for lang in langs}
        self.display = (DisplayServer(self.state, cfg["display"]["host"],
                                      cfg["display"]["port"],
                                      cfg["display"]["font_scale"])
                        if enable_display else None)
        self._seg_thread = threading.Thread(target=self._segment_loop, name="segmenter")
        self._stop = threading.Event()

    # -- callbacks from adapter threads ----------------------------------
    def _on_event(self, ev: TranscriptEvent) -> None:
        self.store.write_event(ev)
        self.event_q.put(ev)

    def _on_status(self, ev: StatusEvent) -> None:
        self.store.write_status(ev)
        self.state.add_status(ev)

    def _on_translation(self, t) -> None:
        self.store.write_translation(t)
        self.state.add_translation(t)

    # -- lifecycle --------------------------------------------------------
    def start(self) -> None:
        self.adapter.start(on_event=self._on_event, on_status=self._on_status)
        for w in self.workers.values():
            w.start()
        self._seg_thread.start()
        if self.display:
            self.display.start()

    def feed(self, chunk: AudioChunk) -> None:
        self.ring.append(chunk)
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
        for w in self.workers.values():
            w.submit(s)               # blocks only the wedged language (spec §3)

    def shutdown(self) -> None:
        """SIGINT order (spec §5.8): stop source (caller's job) ->
        flush_and_stop ASR -> drain translators (<=15 s) -> close store."""
        self.adapter.flush_and_stop()
        self.event_q.put(None)
        self._stop.set()
        self._seg_thread.join(timeout=10)
        for s in self.segmenter.flush():
            self._emit_sentence(s)
        for w in self.workers.values():
            w.stop(drain=True, timeout_s=15)
        if self.display:
            self.display.stop()
        self.store.close()
```

- [ ] **Step 4: Implement `harness/run_file.py`**

```python
import argparse
import json
import logging
import os
import sys

from livetranslate.audio import FileSource
from livetranslate.config import load_config
from livetranslate.glossary import Glossary
from livetranslate.logging_setup import setup_logging
from livetranslate.pipeline import Pipeline
from livetranslate.translate import LLMTranslator

log = logging.getLogger("harness")

def build_adapter(cfg: dict, name: str, glossary: Glossary):
    if name == "elevenlabs":
        from livetranslate.asr.elevenlabs import ElevenLabsScribeAdapter
        return ElevenLabsScribeAdapter(
            api_key=os.environ["ELEVENLABS_API_KEY"],
            language=cfg["session"]["source_language"],
            keyterms=glossary.keyterms(cap=cfg["asr"]["elevenlabs"]["keyterms_max"]))
    if name == "assemblyai":
        from livetranslate.asr.assemblyai import AssemblyAIStreamingAdapter
        return AssemblyAIStreamingAdapter(
            api_key=os.environ["ASSEMBLYAI_API_KEY"],
            language=cfg["session"]["source_language"],
            keyterms=glossary.keyterms(cap=100),
            prompt=glossary.domain_blurb if cfg["asr"]["assemblyai"]["use_domain_prompt"] else "")
    raise SystemExit(f"unknown adapter {name}")

def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--audio", required=True, nargs="+")
    ap.add_argument("--ref")
    ap.add_argument("--adapter", default=None)
    ap.add_argument("--langs", default=None)
    ap.add_argument("--rtf", type=float, default=None)
    ap.add_argument("--loop", type=int, default=1)
    ap.add_argument("--no-display", action="store_true")
    args = ap.parse_args(argv)
    setup_logging()
    cfg = load_config(args.config)
    if args.langs:
        cfg["translate"]["targets"] = args.langs.split(",")
    rtf = args.rtf if args.rtf is not None else cfg["harness"]["rtf"]
    glossary = Glossary.load(cfg["glossary"]["path"], cfg["glossary"]["domain_blurb"])
    adapter = build_adapter(cfg, args.adapter or cfg["asr"]["adapter"], glossary)
    blocks = {l: glossary.block_for(l) for l in cfg["translate"]["targets"]}
    # NOTE: TranslationWorker takes one block per lang; Pipeline passes the
    # right one per worker — adjust Pipeline to accept the dict (small change:
    # glossary_block -> glossary_blocks: dict[str, str]).
    pipe = Pipeline(cfg, adapter=adapter, translator=LLMTranslator(cfg["translate"]),
                    glossary_blocks=blocks, domain_blurb=glossary.domain_blurb,
                    enable_display=not args.no_display,
                    glossary_hash=glossary.sha256)
    pipe.start()
    try:
        for _ in range(args.loop):
            for path in args.audio:
                for chunk in FileSource(path, chunk_ms=cfg["audio"]["chunk_ms"],
                                        rtf=rtf).chunks():
                    pipe.feed(chunk)
    finally:
        pipe.shutdown()
    from harness.metrics import write_report
    write_report(pipe.store.session_dir, ref_path=args.ref,
                 glossary=glossary, langs=cfg["translate"]["targets"])
    print(f"session: {pipe.store.session_dir}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
```

Apply the noted small refactor: `Pipeline(..., glossary_blocks: dict[str, str])`, each worker gets `glossary_blocks[lang]`; update Task 17's test accordingly (`glossary_blocks={"es": ""}`). `write_report` arrives in Task 19 — until then guard the import with `try/except ImportError` or stub it.

- [ ] **Step 5: Run tests — PASS.** Commit: `git commit -am "feat: Pipeline composition root + harness run_file"`

---

### Task 18: ResilientASR + watchdog (`asr/base.py`, `health.py`)

**Files:**
- Modify: `src/livetranslate/asr/base.py`
- Create: `src/livetranslate/health.py`
- Test: `tests/test_resilient_asr.py`, `tests/test_health.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_resilient_asr.py
import time
from livetranslate.asr.base import ResilientASR
from livetranslate.audio import RingBuffer
from livetranslate.types import AudioChunk
from tests.fakes import FakeAdapter

class DyingAdapter(FakeAdapter):
    """Fails on the Nth send, then works after restart()."""
    def __init__(self, scripted, die_on_send=3):
        super().__init__(scripted)
        self.die_on_send, self.sends, self.starts = die_on_send, 0, 0
    def start(self, on_event, on_status):
        self.starts += 1
        super().start(on_event, on_status)
    def send_audio(self, chunk):
        self.sends += 1
        if self.sends == self.die_on_send and self.starts == 1:
            raise ConnectionError("ws dropped")
        super().send_audio(chunk)

def chunk(i):
    return AudioChunk(b"\x00" * 3200, 16000, i * 100, 100, i)

def test_reconnects_and_replays_with_overlap():
    ring = RingBuffer(seconds=10)
    a = DyingAdapter(scripted=[("final", "hello.", 0, 200)], die_on_send=3)
    statuses = []
    r = ResilientASR(lambda: a, ring=ring, overlap_ms=200,
                     backoff_base_s=0.01, backoff_max_s=0.02)
    r.start(on_event=lambda e: None, on_status=statuses.append)
    for i in range(6):
        ring.append(chunk(i)); r.send_audio(chunk(i))
    time.sleep(0.3)
    assert a.starts == 2                                  # reconnected
    msgs = [s.message for s in statuses]
    assert any("reconnecting" in m for m in msgs)
    assert any("replaying" in m for m in msgs)
    # replayed audio started at last_final_end - overlap = 200 - 200 = 0
    replayed = [c for c in a.sent_chunks if c.seq == -1]
    assert replayed and replayed[0].t_start_ms == 0
```

```python
# tests/test_health.py
from livetranslate.health import StallDetector

def test_stall_detected_after_threshold():
    sd = StallDetector(stall_s=10)
    sd.audio_sent(ms=12000)        # 12 s of audio sent...
    assert sd.stalled() is True    # ...zero events received
    sd.event_received()
    assert sd.stalled() is False
```

- [ ] **Step 2: Run — FAIL.**

- [ ] **Step 3: Implement `ResilientASR`** (append to `asr/base.py`):

```python
import random
import threading

from ..audio import RingBuffer

class ResilientASR:
    """Spec §5.2: wraps any adapter; the only ASR object the pipeline sees.
    Reconnect with jittered exponential backoff 0.5->8 s, replay from
    last_final_end_ms - overlap_ms out of the RingBuffer, then resume live."""

    def __init__(self, adapter_factory, ring: RingBuffer, overlap_ms: int = 2000,
                 backoff_base_s: float = 0.5, backoff_max_s: float = 8.0,
                 give_up_after_s: float = 0, failover_factory=None):
        self._factory = adapter_factory
        self._failover_factory = failover_factory
        self.ring, self.overlap_ms = ring, overlap_ms
        self.backoff_base_s, self.backoff_max_s = backoff_base_s, backoff_max_s
        self.give_up_after_s = give_up_after_s
        self.last_final_end_ms = 0
        self.reconnect_count = 0
        self._lock = threading.RLock()
        self._adapter = None
        self._reconnecting = threading.Event()

    @property
    def name(self):
        return self._adapter.name if self._adapter else "?"

    def start(self, on_event, on_status):
        self.on_status = on_status
        def tracking_on_event(ev):
            if ev.kind == "final":
                self.last_final_end_ms = max(self.last_final_end_ms, ev.t_audio_end_ms)
            on_event(ev)
        self.on_event = tracking_on_event
        with self._lock:
            self._adapter = self._factory()
            self._adapter.start(self.on_event, self._on_adapter_status)

    def _on_adapter_status(self, ev):
        self.on_status(ev)
        if ev.level == "error" and not self._reconnecting.is_set():
            threading.Thread(target=self._reconnect, name="asr-reconnect").start()

    def send_audio(self, chunk):
        if self._reconnecting.is_set():
            return                       # ring buffer covers the gap
        try:
            with self._lock:
                self._adapter.send_audio(chunk)
        except Exception:                # noqa: BLE001 — triggers reconnect
            if not self._reconnecting.is_set():
                threading.Thread(target=self._reconnect, name="asr-reconnect").start()

    def force_reconnect(self):
        """Called by the stall watchdog and proactive session rotation."""
        if not self._reconnecting.is_set():
            threading.Thread(target=self._reconnect, name="asr-reconnect").start()

    def _reconnect(self):
        import time as _t
        if self._reconnecting.is_set():
            return
        self._reconnecting.set()
        t0 = _t.monotonic()
        attempt = 0
        factory = self._factory
        while True:
            attempt += 1
            self.on_status(status("warn", "asr", f"reconnecting({attempt})"))
            delay = min(self.backoff_max_s, self.backoff_base_s * 2 ** (attempt - 1))
            _t.sleep(delay * random.uniform(0.5, 1.0))
            try:
                with self._lock:
                    try:
                        self._adapter.flush_and_stop(timeout_s=1.0)
                    except Exception:    # noqa: BLE001 — old socket already dead
                        pass
                    self._adapter = factory()
                    self._adapter.start(self.on_event, self._on_adapter_status)
                    replay_from = max(0, self.last_final_end_ms - self.overlap_ms)
                    if hasattr(self._adapter, "set_stream_offset"):
                        self._adapter.set_stream_offset(replay_from)
                    self.on_status(status("info", "asr", f"replaying from {replay_from}ms"))
                    for c in self.ring.replay_from(replay_from):
                        self._adapter.send_audio(c)
                self.reconnect_count += 1
                self.on_status(status("info", "asr", "connected"))
                self._reconnecting.clear()
                return
            except Exception as e:       # noqa: BLE001 — keep backing off
                self.on_status(status("warn", "asr", f"reconnect failed: {e}"))
                if self.give_up_after_s and _t.monotonic() - t0 > self.give_up_after_s:
                    if self._failover_factory and factory is self._factory:
                        self.on_status(status("error", "asr", "gave_up; failing over"))
                        factory, t0, attempt = self._failover_factory, _t.monotonic(), 0
                        continue
                    self.on_status(status("error", "asr", "gave_up"))
                    self._reconnecting.clear()
                    return

    def flush_and_stop(self, timeout_s: float = 8.0):
        with self._lock:
            if self._adapter:
                self._adapter.flush_and_stop(timeout_s)
```

- [ ] **Step 4: Implement `src/livetranslate/health.py`**

```python
import logging
import resource
import threading
import time

log = logging.getLogger(__name__)

class StallDetector:
    """Spec §5.8: >= stall_s of audio sent with zero events received -> stall."""
    def __init__(self, stall_s: float = 10.0):
        self.stall_s = stall_s
        self._audio_ms_since_event = 0.0
        self._lock = threading.Lock()

    def audio_sent(self, ms: float) -> None:
        with self._lock:
            self._audio_ms_since_event += ms

    def event_received(self) -> None:
        with self._lock:
            self._audio_ms_since_event = 0.0

    def stalled(self) -> bool:
        with self._lock:
            return self._audio_ms_since_event >= self.stall_s * 1000

class Watchdog:
    """Samples gauges every 5 s, logs every 60 s, forces reconnect on stall,
    restarts dead translation workers once (second death in 10 min -> error banner)."""

    def __init__(self, pipeline, resilient_asr, stall: StallDetector, on_status):
        self.p, self.asr, self.stall, self.on_status = pipeline, resilient_asr, stall, on_status
        self._stop = threading.Event()
        self._deaths: dict[str, list[float]] = {}
        self._thread = threading.Thread(target=self._run, name="watchdog")

    def start(self): self._thread.start()
    def stop(self):
        self._stop.set(); self._thread.join(timeout=6)

    def rss_mb(self) -> float:
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024 * 1024)

    def _run(self):
        last_log = 0.0
        while not self._stop.wait(5.0):
            if self.stall.stalled():
                from .asr.base import status
                self.on_status(status("warn", "watchdog", "ASR stall -> forcing reconnect"))
                self.stall.event_received()
                self.asr.force_reconnect()
            for lang, w in self.p.workers.items():
                if not w.alive() and not self._stop.is_set():
                    now = time.monotonic()
                    deaths = [t for t in self._deaths.get(lang, []) if now - t < 600]
                    deaths.append(now)
                    self._deaths[lang] = deaths
                    from .asr.base import status
                    if len(deaths) >= 2:
                        self.on_status(status("error", "watchdog",
                                              f"worker {lang} died twice in 10 min"))
                    else:
                        self.on_status(status("error", "watchdog",
                                              f"worker {lang} died; restarting"))
                        self.p.restart_worker(lang)
            if time.monotonic() - last_log > 60:
                lag = self.p.state.lag_by_lang()
                log.info("gauges: rss=%.0fMB lag=%s reconnects=%d eventq=%d",
                         self.rss_mb(), lag, self.asr.reconnect_count,
                         self.p.event_q.qsize())
                last_log = time.monotonic()
```

Add `Pipeline.restart_worker(lang)` (recreate the `TranslationWorker` with the same args and `start()` it — keep worker construction in a `Pipeline._make_worker(lang)` helper so restart reuses it), and wire `StallDetector` calls into `Pipeline.feed` (`stall.audio_sent`) and `_on_event` (`stall.event_received`). Also wire proactive session rotation: if `docs/vendor-notes.md` records a max session duration, ResilientASR schedules `force_reconnect()` at ~80% of the limit at the next ≥1.5 s pause in tail activity (skip if no limit documented).

- [ ] **Step 5: Run — PASS.** Commit: `git commit -am "feat: ResilientASR reconnect/replay/failover, stall watchdog"`

---

### Task 19: Metrics, report, chaos, soak, resume (`harness/metrics.py`, `harness/chaos.py`) — M5 DoD

**Files:**
- Modify: `harness/metrics.py`
- Create: `harness/chaos.py`
- Test: `tests/test_metrics.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_metrics.py
from harness.metrics import wer, jargon_recall, latency_percentiles

def test_wer_normalizes():
    assert wer("Hello, World!", "hello world") == 0.0
    assert wer("a b c d", "a b x d") == 0.25

def test_jargon_recall_hyphen_space_insensitive():
    terms = ["rate of profit", "Tübingen"]
    ref = "the rate of profit falls in Tübingen and the rate-of-profit rises"
    hyp = "the rate-of-profit falls in tubingen"   # missing diacritic = miss
    r = jargon_recall(terms, ref, hyp)
    assert r["per_term"]["rate of profit"] == (2, 2)
    assert r["per_term"]["Tübingen"] == (0, 1)
    assert r["overall"] == 2 / 3

def test_latency_percentiles():
    p = latency_percentiles([1.0, 2.0, 3.0, 4.0, 10.0])
    assert p["p50"] == 3.0 and p["p95"] >= 4.0
```

- [ ] **Step 2: Run — FAIL.**

- [ ] **Step 3: Implement (append to `harness/metrics.py`)**

```python
import json
import re
import unicodedata
from pathlib import Path

import jiwer

def _norm_text(s: str) -> str:
    s = unicodedata.normalize("NFKC", s).lower()
    s = re.sub(r"[^\w\s]", " ", s, flags=re.UNICODE)
    return " ".join(s.split())

def wer(ref: str, hyp: str) -> float:
    return jiwer.wer(_norm_text(ref), _norm_text(hyp))

def _norm_term(s: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", s).lower().replace("-", " ").split())

def jargon_recall(terms: list[str], ref: str, hyp: str) -> dict:
    nref, nhyp = _norm_term(ref), _norm_term(hyp)
    per_term, found_total, occ_total = {}, 0, 0
    for term in terms:
        nt = _norm_term(term)
        occurrences = nref.count(nt)
        if occurrences == 0:
            continue
        found = min(occurrences, nhyp.count(nt))
        per_term[term] = (found, occurrences)
        found_total += found; occ_total += occurrences
    return {"overall": (found_total / occ_total) if occ_total else None,
            "per_term": per_term}

def latency_percentiles(values: list[float]) -> dict:
    if not values:
        return {"p50": None, "p95": None}
    v = sorted(values)
    def pct(p):
        i = min(len(v) - 1, round(p / 100 * (len(v) - 1)))
        return v[i]
    return {"p50": pct(50), "p95": pct(95)}

def session_latencies(session_dir: Path) -> dict:
    """Latency stages per spec §6 from the session JSONL. The harness paces
    audio at rtf so stream-time t ms corresponds to wall t0 + t/1000."""
    sentences = [json.loads(l) for l in
                 (session_dir / "sentences.jsonl").read_text().splitlines() if l.strip()]
    translations = [json.loads(l) for l in
                    (session_dir / "translations.jsonl").read_text().splitlines() if l.strip()]
    t_by = {(t["sid"], t["lang"]): t for t in translations}
    sent_to_trans = [t_by[(s["sid"], lang)]["t_done_wall"] - s["t_finalized_wall"]
                     for s in sentences for lang in
                     {k[1] for k in t_by} if (s["sid"], lang) in t_by]
    return {"sentence_to_translation": latency_percentiles(sent_to_trans)}

def write_report(session_dir, ref_path, glossary, langs) -> None:
    session_dir = Path(session_dir)
    sentences = [json.loads(l) for l in
                 (session_dir / "sentences.jsonl").read_text().splitlines() if l.strip()]
    translations = [json.loads(l) for l in
                    (session_dir / "translations.jsonl").read_text().splitlines() if l.strip()]
    report = {"invariants": check_invariants(sentences, translations, langs),
              "latency": session_latencies(session_dir)}
    hyp = " ".join(s["text"] for s in sentences)
    if ref_path:
        ref = Path(ref_path).read_text(encoding="utf-8")
        report["wer"] = wer(ref, hyp)
        report["jargon_recall"] = jargon_recall([t.src for t in glossary.terms], ref, hyp)
    # glossary-rendering check per lang (spec §9.8)
    gloss_ok, gloss_n = 0, 0
    t_by = {(t["sid"], t["lang"]): t for t in translations}
    for s in sentences:
        for term in glossary.terms:
            if _norm_term(term.src) in _norm_term(s["text"]):
                for lang in langs:
                    t = t_by.get((s["sid"], lang))
                    if not t or t["status"] != "ok":
                        continue
                    required = term.targets.get(lang) or term.src
                    gloss_n += 1
                    if _norm_term(required) in _norm_term(t["text"]):
                        gloss_ok += 1
    report["glossary_rendering_rate"] = (gloss_ok / gloss_n) if gloss_n else None
    (session_dir / "report.json").write_text(json.dumps(report, indent=2,
                                                        ensure_ascii=False))
    # report.md: aligned source/target table per language for human review
    lines = ["# Session report", "", f"WER: {report.get('wer', 'n/a')}",
             f"Jargon recall: {report.get('jargon_recall', {}).get('overall', 'n/a')}",
             f"Glossary rendering: {report['glossary_rendering_rate']}", ""]
    for lang in langs:
        lines += [f"## {lang}", "", "| sid | source | translation |", "|---|---|---|"]
        for s in sentences:
            t = t_by.get((s["sid"], lang))
            cell = (t["text"] if t else "—").replace("|", "\\|")
            lines.append(f"| {s['sid']} | {s['text'].replace('|', chr(92)+'|')} | {cell} |")
        lines.append("")
    (session_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")
```

- [ ] **Step 4: Implement `harness/chaos.py`** — runs `run_file` with a `ChaosAdapter` wrapper that closes the adapter's WS socket at configured stream-time offsets, then asserts recovery from the session JSONL:

```python
import argparse
import json
import sys
from pathlib import Path

from harness import run_file
from harness.metrics import check_invariants

class ChaosWrapper:
    """Wraps a real adapter; severs its socket when audio crosses each cut offset."""
    def __init__(self, inner, cut_offsets_ms: list[int]):
        self.inner, self.cuts = inner, sorted(cut_offsets_ms)
        self.name = inner.name
    def start(self, on_event, on_status):
        self.inner.start(on_event, on_status)
    def send_audio(self, chunk):
        if self.cuts and chunk.t_start_ms >= self.cuts[0]:
            self.cuts.pop(0)
            try:
                self.inner._ws.sock.close()      # simulate network drop
            except Exception:                    # noqa: BLE001
                pass
        self.inner.send_audio(chunk)
    def flush_and_stop(self, timeout_s=8.0):
        self.inner.flush_and_stop(timeout_s)
    def __getattr__(self, item):
        return getattr(self.inner, item)

def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--audio", required=True)
    ap.add_argument("--cuts-ms", required=True,
                    help="comma-separated stream offsets, e.g. 30000,95000,150500")
    args = ap.parse_args(argv)
    cuts = [int(x) for x in args.cuts_ms.split(",")]
    # monkey-patch build_adapter to wrap with ChaosWrapper
    orig = run_file.build_adapter
    run_file.build_adapter = lambda cfg, name, g: ChaosWrapper(orig(cfg, name, g), cuts)
    run_file.main(["--config", args.config, "--audio", args.audio, "--no-display"])
    sdir = sorted(Path("sessions").iterdir())[-1]
    sentences = [json.loads(l) for l in (sdir / "sentences.jsonl").read_text().splitlines()]
    events = [json.loads(l) for l in (sdir / "events.jsonl").read_text().splitlines()]
    reconnects = sum(1 for e in events
                     if e.get("type") == "status" and "reconnecting" in e.get("message", ""))
    errs = check_invariants(sentences, [], [])
    assert reconnects >= len(cuts), f"expected >= {len(cuts)} reconnects, saw {reconnects}"
    assert not [e for e in errs if "duplicate" in e], errs
    print(f"chaos OK: {reconnects} reconnects, {len(sentences)} sentences, 0 dupes")
    return 0

if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 5: Wire `--resume`** into `__main__.py`/`pipeline.py` (already supported by `Pipeline(resume_dir=...)`); add an offline test: create a session with Task 17's offline pipeline, `kill` is simulated by just not calling `shutdown()` on the Store (open a second Store on the dir), resume, assert `DisplayState` contains all previous sentences and the next emitted `sid` continues the counter.

- [ ] **Step 6: Run tests — PASS.** Manual M5 DoD (networked): chaos at 3 offsets incl. mid-sentence; 2 h soak (`--loop 4`, RSS growth < 150 MB from minute 10); `kill -9` + `--resume` byte-identical finalized content; operator console shows disconnect within 5 s.
- [ ] **Step 7: Commit** — `git commit -am "feat: metrics, report.md, chaos harness, resume (M5 done)"`

---

## Milestone M6 — AssemblyAI + bake-off

### Task 20: §13 doc verification — AssemblyAI

- [ ] **Step 1:** Verify against current AssemblyAI Universal-3 Pro Streaming docs and append to `docs/vendor-notes.md`: WS endpoint + auth; partial/final schema; keyterm field + limits; free-form prompt field + limits **and keyterm/prompt mutual-exclusivity in streaming**; `en`/`de` language param; session duration/keepalive; faster-than-realtime tolerance.
- [ ] **Step 2:** Save verbatim sample messages to `tests/fixtures/assemblyai_messages.json`. Commit.

### Task 21: AssemblyAIStreamingAdapter + bakeoff (`asr/assemblyai.py`, `harness/bakeoff.py`) — M6 DoD

**Files:**
- Create: `src/livetranslate/asr/assemblyai.py` — same structure as `elevenlabs.py` (Task 9): same thread layout, same `_normalize` pattern tested against the Task 20 fixtures, plus the `prompt` field from `domain_blurb` in the session config. Test: `tests/test_assemblyai_adapter.py` mirroring Task 9's tests (partial normalized with offset; final normalized; non-transcript → None).
- Create: `harness/bakeoff.py`:

- [ ] **Step 1: Write adapter tests from fixtures (as in Task 9) — FAIL — implement — PASS.**

- [ ] **Step 2: Implement `harness/bakeoff.py`**

```python
import argparse
import csv
import json
import sys
from pathlib import Path

from harness import run_file

DECISION_NOTE = """Decision rule (spec §8): prefer ElevenLabs unless AssemblyAI wins
jargon recall by >= 3 points or WER by >= 1.5 points absolute, or recordings show
heavy DE/EN code-switching that ElevenLabs visibly fumbles."""

def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--audio", required=True)
    ap.add_argument("--ref", required=True)
    ap.add_argument("--langs", default="es,fr,de,pt")
    args = ap.parse_args(argv)
    rows = []
    for adapter in ("elevenlabs", "assemblyai"):
        run_file.main(["--config", args.config, "--audio", args.audio,
                       "--ref", args.ref, "--adapter", adapter,
                       "--langs", args.langs, "--no-display"])
        sdir = sorted(Path("sessions").iterdir())[-1]
        rep = json.loads((sdir / "report.json").read_text())
        events = [json.loads(l) for l in (sdir / "events.jsonl").read_text().splitlines()]
        reconnects = sum(1 for e in events if e.get("type") == "status"
                         and "reconnecting" in e.get("message", ""))
        rows.append({"adapter": adapter, "wer": rep.get("wer"),
                     "jargon_recall": (rep.get("jargon_recall") or {}).get("overall"),
                     "lat_p50": rep["latency"]["sentence_to_translation"]["p50"],
                     "lat_p95": rep["latency"]["sentence_to_translation"]["p95"],
                     "reconnects": reconnects, "session": str(sdir)})
    with open("bakeoff.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=rows[0].keys())
        w.writeheader(); w.writerows(rows)
    print("| adapter | WER | jargon recall | lat p50 | lat p95 | reconnects |")
    print("|---|---|---|---|---|---|")
    for r in rows:
        print(f"| {r['adapter']} | {r['wer']} | {r['jargon_recall']} "
              f"| {r['lat_p50']} | {r['lat_p95']} | {r['reconnects']} |")
    print("\n" + DECISION_NOTE)
    return 0

if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 3: Run unit tests — PASS.** Manual M6 DoD: bake-off on owner recordings produces `bakeoff.csv` + table (§9 item 7).
- [ ] **Step 4: Commit** — `git commit -am "feat: AssemblyAI adapter + bakeoff harness (M6 done)"`

---

## Milestone M7 — Live path & polish

### Task 22: Live runner (`runner.py`) wiring `__main__`

**Files:**
- Create: `src/livetranslate/runner.py`
- Test: covered by existing offline pipeline test + manual

- [ ] **Step 1: Implement `run_live(cfg, resume_dir)`** — compose: `Glossary.load` → adapter factory (+ failover factory if `asr.failover` set) → `ResilientASR(factory, ring, overlap_ms, give_up_after_s, failover_factory)` → `Pipeline` (pass the ResilientASR as the adapter; pass the pipeline's `RingBuffer` into it) → `StallDetector` + `Watchdog` → `MicSource` feed loop. Install a `signal.SIGINT` handler that sets a stop flag; the feed loop exits, then run the spec §5.8 shutdown order: stop source → `pipeline.shutdown()` (which flushes ASR, drains ≤ 15 s, closes store) → `watchdog.stop()` → return 0.
- [ ] **Step 2: Manual check:** `python -m livetranslate --config config.toml` starts, refuses politely if device or API keys missing; Ctrl-C exits 0 within ~15 s.
- [ ] **Step 3: Commit** — `git commit -am "feat: live runner with SIGINT drain"`

### Task 23: MicSource + device guard (`audio.py`)

**Files:**
- Modify: `src/livetranslate/audio.py`
- Test: `tests/test_mic_guard.py` (mock `sounddevice.query_devices`)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_mic_guard.py
import pytest
from unittest.mock import patch
from livetranslate.audio import MicSource

def test_refuses_to_start_if_device_not_found():
    with patch("sounddevice.query_devices",
               return_value=[{"name": "MacBook Pro Microphone", "max_input_channels": 1}]):
        with pytest.raises(SystemExit, match="Scarlett"):
            MicSource("Scarlett", chunk_ms=100).resolve_device()
```

- [ ] **Step 2: Implement** — replace the Task 6 stub: `resolve_device()` substring-matches input devices and raises `SystemExit(f"audio device matching {sub!r} not found; refusing to start")` on miss (spec: never silently fall back to the built-in mic). `chunks()` uses `sounddevice.RawInputStream(samplerate=16000, channels=1, dtype="int16", blocksize=16 * chunk_ms, device=idx)` with a PortAudio callback pushing `AudioChunk`s onto an internal `queue.Queue(maxsize=64)`; `chunks()` is a generator draining that queue; track `t_start_ms`/`seq` exactly as FileSource does.
- [ ] **Step 3: Run — PASS. Commit.**

### Task 24: README runbook + final acceptance — M7 DoD

- [ ] **Step 1: Write `README.md`** — install, config, env vars, live run, resume, harness commands, and the pre-event checklist from spec §10 M7: device check, glossary load count, keyterm count (+ surcharge note), one test sentence through all enabled languages, display URLs.
- [ ] **Step 2: Run the full §9 acceptance list** against owner recordings and record results in `docs/acceptance-v1.md`: (1) jargon recall ≥ 90% with keyterms + measured uplift vs keyterms-off; (2) e2e p50 ≤ 4 s / p95 ≤ 7 s at rtf=1.0; (3) chaos at 3 offsets; (4) 2 h soak; (5) `kill -9` + resume byte-identical; (6) console reflects disconnect ≤ 5 s; (7) bake-off report; (8) glossary rendering ≥ 95%.
- [ ] **Step 3: Final commit** — `git commit -am "docs: README runbook + v1 acceptance results (M7 done)"`

---

## Self-Review Notes (already applied)

- Spec coverage: all of §2–§13 map to tasks; §5.3 rule 5 (`paragraph_break`) added to the dataclass in Task 2; forced-cut timer runs in the segmenter loop (Task 17); proactive session rotation noted in Task 18 (conditional on §13 findings); draft-translation mode (spec §1 non-goal / §14.4) intentionally not built in v1.
- Known intentional simplifications, all spec-permitted: segmenter start-ms approximation for split buffers; harness `audio → first partial` / `phrase end → final` latencies derive from `events.jsonl` (`t_received_wall` vs paced stream time) — implement inside `session_latencies` when the first real session file exists.
- Type consistency checked: `Pipeline` uses `glossary_blocks: dict[str, str]` after Task 17's noted refactor — apply it in Task 17, not later; `ResilientASR` exposes `send_audio/flush_and_stop/name` so it is drop-in where adapters are expected.
- Vendor adapters deliberately contain `<from vendor-notes>` markers — those are **gates on Tasks 8/20**, not placeholders to be guessed.
