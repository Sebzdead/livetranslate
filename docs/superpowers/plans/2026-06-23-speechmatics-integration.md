# Speechmatics Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Speechmatics as the primary realtime ASR adapter (with ElevenLabs as failover), and use Speechmatics' bundled realtime translation as an instant "draft" caption layer — an italic, dimmed line that fades in immediately and is replaced by the glossary-accurate LLM translation when it lands.

**Architecture:** Speechmatics slots in behind the existing `ASRAdapter` protocol exactly like the ElevenLabs and AssemblyAI adapters — a WebSocket with a sender + receiver thread, normalizing vendor messages into `TranscriptEvent`s on the session stream timeline. The LLM translation layer (`translate.py`) is **unchanged** — it remains the authoritative, glossary-enforcing translator. The draft layer is additive: the Speechmatics adapter also emits `AddPartialTranslation`/`AddTranslation` text through a new optional `on_draft(lang, text)` callback, threaded through `ResilientASR` → `Pipeline` → `DisplayState` → SSE → a single provisional italic line at the live edge of each audience view. The dormant `display.draft_translation` config flag gates the whole draft layer.

**Tech Stack:** Python ≥ 3.11, `websocket-client` (already a dep), stdlib threading/json. No new dependencies. TDD against schema-derived fixtures (validated live in Phase 0), mirroring the existing `tests/test_elevenlabs_adapter.py` / `tests/test_assemblyai_adapter.py` pattern.

**Why a draft *tail* and not per-sentence draft/final swapping:** Speechmatics segments translations on its own punctuation boundaries; the app's `Segmenter` produces its own `sid`-keyed sentences. The two will not align 1:1, so matching "draft N" to "final N" is fragile. Instead the draft is rendered as a single provisional line at the live edge (the same mechanism as the existing source `tentative_tail`): it always shows the latest Speechmatics translation of what is being spoken now, in italics; authoritative LLM translations append above it as regular text. Visually this delivers exactly "italic draft that becomes regular text once the real translation arrives," without cross-vendor sentence alignment.

**Conventions for the implementer (read first):**
- Run everything with the project venv: `.venv/bin/python -m pytest tests/ -q`. All tests stay offline — no API keys, no network, no real microphone or WebSocket.
- New env var: `SPEECHMATICS_API_KEY` (lives in the gitignored `.env`, never in `config.toml` — `config.py:_scan_for_secrets` enforces this).
- The Speechmatics adapter is modeled on `src/livetranslate/asr/assemblyai.py` (binary audio frames) but the receive side resembles `elevenlabs.py` (string `message` discriminator). Read both before starting.
- Speechmatics timestamps are in **seconds** (like ElevenLabs) — multiply by 1000. AssemblyAI was milliseconds; do not copy its passthrough.
- Language codes: the app uses `zh` for Chinese; Speechmatics uses `cmn`. All app↔Speechmatics code conversion goes through the `to_sm()` / `to_app()` helpers in Task 4. Everywhere else keeps app codes.

**File map:**

| File | Responsibility |
|---|---|
| Create: `tests/fixtures/speechmatics_messages.json` | Captured/schema-derived vendor messages for adapter unit tests |
| Modify: `docs/vendor-notes.md` | Speechmatics §13 verification section + live-validation checklist |
| Create: `src/livetranslate/asr/speechmatics.py` | `SpeechmaticsRTAdapter` (ASR + draft-translation emission) |
| Create: `tests/test_speechmatics_adapter.py` | Adapter unit tests |
| Modify: `src/livetranslate/asr/base.py` | Add `OnDraft` type; thread `on_draft` through `ASRAdapter` protocol + `ResilientASR` |
| Modify: `src/livetranslate/asr/elevenlabs.py`, `src/livetranslate/asr/assemblyai.py` | Accept and ignore `on_draft` in `start()` |
| Modify: `src/livetranslate/config.py` | `[asr.speechmatics]` defaults; allow `speechmatics` adapter |
| Modify: `src/livetranslate/control/files.py` | Allow `speechmatics` adapter; add `SPEECHMATICS_API_KEY` secret |
| Modify: `src/livetranslate/control/server.py` | `ADAPTER_KEYS["speechmatics"]` |
| Modify: `src/livetranslate/runner.py` | `_adapter_factory` Speechmatics branch; pass `on_draft` to `resilient.start` |
| Modify: `src/livetranslate/pipeline.py` | `on_draft` callback → `DisplayState.set_draft`; gate on `display.draft_translation` |
| Modify: `src/livetranslate/display/server.py` | `DisplayState` draft storage + SSE `draft` frame |
| Modify: `src/livetranslate/display/static/view.html` | Italic fade-in draft line at the live edge |
| Modify: `config.toml`, `README.md` | Document Speechmatics + draft layer |

---

## Phase 0 — Live verification & fixtures

### Task 1: Speechmatics fixtures + vendor notes

**Files:**
- Create: `tests/fixtures/speechmatics_messages.json`
- Modify: `docs/vendor-notes.md`

- [ ] **Step 1: Create the fixtures file**

These are schema-derived from the Speechmatics RT v2 docs (`StartRecognition`/`AddTranscript`/`AddTranslation`). They are marked uncertain and re-validated against a live key in Step 3. Create `tests/fixtures/speechmatics_messages.json`:

```json
{
  "recognition_started": {
    "message": "RecognitionStarted",
    "id": "807670e9-14af-4fa2-9e8f-5d525c22156e"
  },
  "audio_added": { "message": "AudioAdded", "seq_no": 1 },
  "partial": {
    "message": "AddPartialTranscript",
    "format": "2.9",
    "metadata": { "start_time": 1.24, "end_time": 2.10, "transcript": "the first move is what sets" },
    "results": [
      { "type": "word", "start_time": 1.24, "end_time": 1.38, "alternatives": [{ "content": "the", "confidence": 0.91 }] },
      { "type": "word", "start_time": 1.38, "end_time": 1.62, "alternatives": [{ "content": "first", "confidence": 0.95 }] }
    ]
  },
  "final": {
    "message": "AddTranscript",
    "format": "2.9",
    "metadata": { "start_time": 1.24, "end_time": 3.55, "transcript": "The first move is what sets everything in motion. " },
    "results": [
      { "type": "word", "start_time": 1.24, "end_time": 1.38, "alternatives": [{ "content": "The", "confidence": 0.98 }] },
      { "type": "word", "start_time": 3.30, "end_time": 3.55, "alternatives": [{ "content": "motion", "confidence": 0.97 }] },
      { "type": "punctuation", "attaches_to": "previous", "start_time": 3.55, "end_time": 3.55, "alternatives": [{ "content": ".", "confidence": 1.0 }] }
    ]
  },
  "partial_translation": {
    "message": "AddPartialTranslation",
    "format": "2.9",
    "language": "es",
    "results": [
      { "start_time": 1.24, "end_time": 2.10, "content": "el primer movimiento es lo que" }
    ]
  },
  "translation": {
    "message": "AddTranslation",
    "format": "2.9",
    "language": "es",
    "results": [
      { "start_time": 1.24, "end_time": 3.55, "content": "El primer movimiento es lo que pone todo en marcha." }
    ]
  },
  "translation_cmn": {
    "message": "AddTranslation",
    "format": "2.9",
    "language": "cmn",
    "results": [{ "start_time": 1.24, "end_time": 3.55, "content": "第一步决定了一切。" }]
  },
  "end_of_transcript": { "message": "EndOfTranscript" },
  "warning": { "message": "Warning", "type": "duration_limit_exceeded", "reason": "Audio duration limit exceeded" },
  "error": { "message": "Error", "type": "invalid_audio_type", "reason": "Audio type not supported" }
}
```

- [ ] **Step 2: Append the Speechmatics section to `docs/vendor-notes.md`**

Append:

```markdown
---

## Speechmatics Realtime (RT v2)

**Date verified:** SCHEMA-DERIVED 2026-06-23 — ⚠️ NOT yet captured live. Validate against a live `SPEECHMATICS_API_KEY` (Task 1 Step 3) before the event.

**Sources consulted (2026-06-23):**
- <https://docs.speechmatics.com/rt-api-ref>
- <https://docs.speechmatics.com/features-other/translation>
- <https://docs.speechmatics.com/speech-to-text/features/custom-dictionary>
- <https://docs.speechmatics.com/speech-to-text/languages>

### Endpoint & auth
- `wss://eu.rt.speechmatics.com/v2/` (EU; data residency). Server-side auth: HTTP header `Authorization: Bearer <SPEECHMATICS_API_KEY>` at the WS upgrade.

### Protocol flow
1. Client → `StartRecognition` (JSON text frame) with `audio_format` + `transcription_config` (+ optional `translation_config`).
2. Server → `RecognitionStarted` (carries session `id`). **Audio must not be sent until this arrives.**
3. Client → raw **binary** audio frames (`pcm_s16le`, 16 kHz mono). Server acks each with `AudioAdded` (`seq_no`).
4. Server → `AddPartialTranscript` (changeable) and `AddTranscript` (final). With translation: `AddPartialTranslation` / `AddTranslation`.
5. Client → `EndOfStream` (`{"message":"EndOfStream","last_seq_no": <n>}`) at teardown. Server → `EndOfTranscript`.
6. `Error` (fatal → reconnect), `Warning`/`Info` (non-fatal).

### Transcript schema
- `metadata.transcript` = full segment text. `metadata.start_time` / `metadata.end_time` = segment bounds in **SECONDS** (× 1000 for ms). `results[]` carries per-word/punctuation timing (not needed; metadata bounds suffice).

### Translation schema
- `AddTranslation` / `AddPartialTranslation`: `language` (ISO; Chinese = `cmn`), `results[].content` = translated text (join `results` with spaces).
- `translation_config`: only `target_languages` (max 5) + `enable_partials`. **No glossary/terminology control** — this is why the LLM translator stays authoritative.

### Custom dictionary
- `transcription_config.additional_vocab`: list of `{"content": "...", "sounds_like": [...]}` or bare strings. **Transcription only** — does not affect translation. ⚠️ Latency/memory penalty for large lists; cap conservatively (default 50).

### Language codes (app ↔ Speechmatics)
- Source: `en`→`en`, `de`→`de`. Targets identity except **`zh`→`cmn`**. ⚠️ Confirm `ar` (Arabic) target support live — the public docs were ambiguous ("bilingual pack").
```

- [ ] **Step 3: Add the live-validation checklist** (append to the section above)

```markdown
### Live validation checklist (run with a real key before the event)
- [ ] Connect to `wss://eu.rt.speechmatics.com/v2/`; confirm `RecognitionStarted` arrives.
- [ ] Confirm `metadata.transcript` + seconds timestamps match `tests/fixtures/speechmatics_messages.json`; fix the fixtures + adapter if not.
- [ ] Confirm `AddTranslation.results[].content` shape and that `cmn` is the Chinese code; confirm `ar` is accepted as a target (or note unsupported).
- [ ] Measure `additional_vocab` latency penalty at the real glossary size; tune `asr.speechmatics.additional_vocab_max`.
- [ ] Confirm `max_delay` behavior (lower = faster partials, more revisions).
- [ ] Update the "Date verified" line to a live date once confirmed.
```

- [ ] **Step 4: Verify the fixtures parse**

Run: `.venv/bin/python -c "import json,pathlib; json.loads(pathlib.Path('tests/fixtures/speechmatics_messages.json').read_text()); print('ok')"`
Expected: `ok`

- [ ] **Step 5: Commit**

```bash
git add tests/fixtures/speechmatics_messages.json docs/vendor-notes.md
git commit -m "docs(asr): Speechmatics RT schema notes + fixtures + live-validation checklist"
```

---

## Phase 1 — Speechmatics as primary ASR (ElevenLabs failover)

### Task 2: Config — allow the Speechmatics adapter

**Files:**
- Modify: `src/livetranslate/config.py:11-16`
- Modify: `src/livetranslate/control/files.py:12,128-129`
- Modify: `src/livetranslate/control/server.py:24`
- Test: `tests/test_config.py` (append)

- [ ] **Step 1: Write the failing test** (append to `tests/test_config.py`; match the file's existing style for building a temp config — read it first)

```python
def test_config_accepts_speechmatics_adapter(tmp_path):
    from livetranslate.config import load_config
    p = tmp_path / "config.toml"
    p.write_text(
        '[session]\nsource_language = "en"\n'
        '[asr]\nadapter = "speechmatics"\nfailover = "elevenlabs"\n'
        '[translate]\ntargets = ["es"]\nprovider = "openai_chat"\n'
    )
    cfg = load_config(p)
    assert cfg["asr"]["adapter"] == "speechmatics"
    assert cfg["asr"]["speechmatics"]["additional_vocab_max"] == 50
    assert cfg["asr"]["speechmatics"]["max_delay"] == 1.0
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_config.py -q`
Expected: FAIL — `KeyError: 'speechmatics'`.

- [ ] **Step 3: Implement — add Speechmatics defaults**

In `src/livetranslate/config.py`, change the `"asr"` defaults block (currently `config.py:11-16`) from:

```python
    "asr": {
        "adapter": "elevenlabs", "failover": "", "give_up_after_s": 0,
        "overlap_ms": 2000,
        "max_session_s": 0,  # 0 = off; set 5400 (90 min) for ElevenLabs; AAI hard limit 3 h
        "elevenlabs": {"keyterms_max": 50},  # realtime cap per docs/vendor-notes.md
        "assemblyai": {"use_domain_prompt": True},
    },
```

to:

```python
    "asr": {
        "adapter": "elevenlabs", "failover": "", "give_up_after_s": 0,
        "overlap_ms": 2000,
        "max_session_s": 0,  # 0 = off; set 5400 (90 min) for ElevenLabs; AAI hard limit 3 h
        "elevenlabs": {"keyterms_max": 50},  # realtime cap per docs/vendor-notes.md
        "assemblyai": {"use_domain_prompt": True},
        # additional_vocab has a latency penalty for large lists (docs/vendor-notes.md);
        # max_delay trades partial latency vs revision churn (lower = faster, choppier).
        "speechmatics": {"additional_vocab_max": 50, "max_delay": 1.0},
    },
```

- [ ] **Step 4: Implement — allow the adapter in the control validator**

In `src/livetranslate/control/files.py`, change line 12:

```python
SECRET_KEYS = ("ELEVENLABS_API_KEY", "ASSEMBLYAI_API_KEY", "TRANSLATE_API_KEY")
```

to:

```python
SECRET_KEYS = ("ELEVENLABS_API_KEY", "ASSEMBLYAI_API_KEY",
               "SPEECHMATICS_API_KEY", "TRANSLATE_API_KEY")
```

and change lines 128-129:

```python
    if doc["asr"].get("adapter") not in ("elevenlabs", "assemblyai"):
        problems.append("asr.adapter must be 'elevenlabs' or 'assemblyai'")
```

to:

```python
    if doc["asr"].get("adapter") not in ("elevenlabs", "assemblyai", "speechmatics"):
        problems.append("asr.adapter must be 'elevenlabs', 'assemblyai', or 'speechmatics'")
```

- [ ] **Step 5: Implement — map the adapter to its key in the control server**

In `src/livetranslate/control/server.py`, change line 24:

```python
ADAPTER_KEYS = {"elevenlabs": "ELEVENLABS_API_KEY", "assemblyai": "ASSEMBLYAI_API_KEY"}
```

to:

```python
ADAPTER_KEYS = {"elevenlabs": "ELEVENLABS_API_KEY", "assemblyai": "ASSEMBLYAI_API_KEY",
                "speechmatics": "SPEECHMATICS_API_KEY"}
```

- [ ] **Step 6: Run tests**

Run: `.venv/bin/python -m pytest tests/test_config.py tests/test_control_files.py tests/test_control_server.py -q`
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add src/livetranslate/config.py src/livetranslate/control/files.py src/livetranslate/control/server.py tests/test_config.py
git commit -m "feat(asr): allow speechmatics adapter in config + control panel"
```

---

### Task 3: Speechmatics adapter — normalization & StartRecognition

**Files:**
- Create: `src/livetranslate/asr/speechmatics.py`
- Test: `tests/test_speechmatics_adapter.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_speechmatics_adapter.py`:

```python
import json
import pathlib

from livetranslate.asr.speechmatics import SpeechmaticsRTAdapter, to_app, to_sm

FIXTURES = json.loads((pathlib.Path(__file__).parent / "fixtures" /
                       "speechmatics_messages.json").read_text())


def make_adapter(**kw):
    kw.setdefault("api_key", "test")
    kw.setdefault("language", "en")
    kw.setdefault("additional_vocab", ["Tübingen"])
    return SpeechmaticsRTAdapter(**kw)


def test_lang_code_mapping_zh_is_cmn():
    assert to_sm("zh") == "cmn" and to_sm("es") == "es"
    assert to_app("cmn") == "zh" and to_app("es") == "es"


def test_partial_normalized_with_stream_offset():
    a = make_adapter()
    a._stream_offset_ms = 5000
    ev = a._normalize(FIXTURES["partial"])
    assert ev.kind == "partial" and ev.vendor == "speechmatics"
    assert ev.text == "the first move is what sets"
    assert ev.t_audio_start_ms == 5000 + 1240   # 1.24 s -> ms, plus offset
    assert ev.vendor_raw == FIXTURES["partial"]


def test_final_normalized_seconds_to_ms():
    a = make_adapter()
    a._stream_offset_ms = 0
    ev = a._normalize(FIXTURES["final"])
    assert ev.kind == "final"
    assert ev.text == "The first move is what sets everything in motion."
    assert ev.t_audio_start_ms == 1240 and ev.t_audio_end_ms == 3550


def test_control_messages_are_not_transcripts():
    a = make_adapter()
    for key in ("recognition_started", "audio_added", "end_of_transcript",
                "warning", "error", "translation"):
        assert a._normalize(FIXTURES[key]) is None


def test_start_recognition_includes_audio_format_and_vocab():
    a = make_adapter(language="de", additional_vocab=["Profitrate", "Komintern"])
    msg = a._start_recognition()
    assert msg["message"] == "StartRecognition"
    assert msg["audio_format"] == {"type": "raw", "encoding": "pcm_s16le", "sample_rate": 16000}
    assert msg["transcription_config"]["language"] == "de"
    assert msg["transcription_config"]["enable_partials"] is True
    assert msg["transcription_config"]["additional_vocab"] == [
        {"content": "Profitrate"}, {"content": "Komintern"}]
    assert "translation_config" not in msg   # phase 1: no targets


def test_start_recognition_omits_empty_vocab():
    a = make_adapter(additional_vocab=[])
    assert "additional_vocab" not in a._start_recognition()["transcription_config"]


def test_additional_vocab_truncated_to_cap():
    a = make_adapter(additional_vocab=[f"t{i}" for i in range(60)],
                     additional_vocab_max=50)
    assert len(a.additional_vocab) == 50
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_speechmatics_adapter.py -q`
Expected: FAIL — `ModuleNotFoundError: livetranslate.asr.speechmatics`.

- [ ] **Step 3: Implement the module skeleton (construction, mapping, normalization, StartRecognition)**

Create `src/livetranslate/asr/speechmatics.py`:

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
```

- [ ] **Step 4: Run tests**

Run: `.venv/bin/python -m pytest tests/test_speechmatics_adapter.py -q`
Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add src/livetranslate/asr/speechmatics.py tests/test_speechmatics_adapter.py
git commit -m "feat(asr): Speechmatics adapter normalization + StartRecognition config"
```

---

### Task 4: Speechmatics adapter — lifecycle, audio, receive loop

**Files:**
- Modify: `src/livetranslate/asr/speechmatics.py`
- Test: `tests/test_speechmatics_adapter.py` (append)

- [ ] **Step 1: Write the failing tests** (append to `tests/test_speechmatics_adapter.py`)

```python
def test_recv_loop_routes_recognition_started_and_transcripts():
    a = make_adapter()
    events, statuses = [], []
    a.on_event = events.append
    a.on_status = statuses.append
    a.on_draft = None

    class ScriptWS:
        def __init__(self, msgs):
            self._msgs = list(msgs)
        def recv(self):
            if self._msgs:
                return json.dumps(self._msgs.pop(0))
            raise ConnectionError("done")

    a._ws = ScriptWS([FIXTURES["recognition_started"], FIXTURES["audio_added"],
                      FIXTURES["partial"], FIXTURES["final"],
                      FIXTURES["end_of_transcript"]])
    a._recv_loop()
    assert a._started.is_set()
    assert [e.kind for e in events] == ["partial", "final"]
    assert any("connected" in s.message for s in statuses)


def test_recv_loop_error_surfaces_as_error_status():
    a = make_adapter()
    statuses = []
    a.on_event = lambda e: None
    a.on_status = statuses.append
    a.on_draft = None

    class OneShot:
        def __init__(self, msg):
            self.msg, self.done = msg, False
        def recv(self):
            if self.done:
                raise ConnectionError("done")
            self.done = True
            return json.dumps(self.msg)

    a._ws = OneShot(FIXTURES["error"])
    a._recv_loop()
    assert any(s.level == "error" and "invalid_audio_type" in s.message for s in statuses)


def test_recv_loop_emits_draft_when_callback_present():
    a = make_adapter(target_languages=["es"])
    drafts = []
    a.on_event = lambda e: None
    a.on_status = lambda s: None
    a.on_draft = lambda lang, text: drafts.append((lang, text))

    class OneShot:
        def __init__(self, msg):
            self.msg, self.done = msg, False
        def recv(self):
            if self.done:
                raise ConnectionError("done")
            self.done = True
            return json.dumps(self.msg)

    a._ws = OneShot(FIXTURES["translation"])
    a._recv_loop()
    assert drafts == [("es", "El primer movimiento es lo que pone todo en marcha.")]


def test_recv_loop_maps_cmn_draft_to_zh():
    a = make_adapter(target_languages=["zh"])
    drafts = []
    a.on_event = lambda e: None
    a.on_status = lambda s: None
    a.on_draft = lambda lang, text: drafts.append((lang, text))

    class OneShot:
        def __init__(self, msg):
            self.msg, self.done = msg, False
        def recv(self):
            if self.done:
                raise ConnectionError("done")
            self.done = True
            return json.dumps(self.msg)

    a._ws = OneShot(FIXTURES["translation_cmn"])
    a._recv_loop()
    assert drafts and drafts[0][0] == "zh"


def test_end_of_stream_carries_last_seq_no():
    a = make_adapter()
    sent = []

    class CaptureWS:
        def send(self, data):
            sent.append(json.loads(data))
        def send_binary(self, data):
            pass

    a._ws = CaptureWS()
    a._seq = 7
    a._send_end_of_stream()
    assert sent == [{"message": "EndOfStream", "last_seq_no": 7}]
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_speechmatics_adapter.py -q`
Expected: FAIL — `AttributeError: ... _recv_loop` / `_send_end_of_stream`.

- [ ] **Step 3: Implement the lifecycle, audio, and receive methods** (append to `src/livetranslate/asr/speechmatics.py`)

```python
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
        self._send_q.put(chunk)   # blocks if full: backpressure to source

    def _send_loop(self) -> None:
        # Audio must not be sent before RecognitionStarted, or the server errors.
        if not self._started.wait(timeout=15):
            self.on_status(status("error", "asr", "RecognitionStarted not received"))
            return
        while not self._stop.is_set():
            chunk = self._send_q.get()
            if chunk is None:
                self._send_end_of_stream()
                return
            try:
                self._ws.send_binary(chunk.pcm16)
                self._seq += 1
            except Exception as e:   # noqa: BLE001 — surfaced as status
                self.on_status(status("error", "asr", f"send failed: {e}"))
                return

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
            if draft is not None and self.on_draft is not None:
                lang, text = draft
                if text:
                    self.on_draft(lang, text)

    def flush_and_stop(self, timeout_s: float = 8.0) -> None:
        self._send_q.put(None)
        self._sender.join(timeout=timeout_s)
        self._stop.set()
        # Close the WS BEFORE joining the receiver so its blocking recv()
        # unblocks immediately instead of burning the join timeout.
        try:
            self._ws.close()
        except Exception:   # noqa: BLE001 — already closing
            pass
        self._receiver.join(timeout=timeout_s)
```

- [ ] **Step 4: Run tests**

Run: `.venv/bin/python -m pytest tests/test_speechmatics_adapter.py -q`
Expected: 12 passed.

- [ ] **Step 5: Commit**

```bash
git add src/livetranslate/asr/speechmatics.py tests/test_speechmatics_adapter.py
git commit -m "feat(asr): Speechmatics adapter lifecycle, binary audio, receive routing"
```

---

### Task 5: Wire Speechmatics into the runner factory (Phase 1 — transcription only)

**Files:**
- Modify: `src/livetranslate/runner.py:15-35`
- Test: `tests/test_runner.py` (append)

- [ ] **Step 1: Write the failing test** (append to `tests/test_runner.py`; read the file first to match how it builds `cfg`/`glossary` — reuse the existing helpers/fixtures)

```python
def test_adapter_factory_builds_speechmatics(monkeypatch, tmp_path):
    """The factory wires SPEECHMATICS_API_KEY, source language, and glossary
    keyterms (capped by additional_vocab_max) into the adapter."""
    import os
    from livetranslate import runner
    from livetranslate.glossary import Glossary, Term

    monkeypatch.setenv("SPEECHMATICS_API_KEY", "sm-test-key")
    glossary = Glossary(terms=[Term(src="Komintern", targets={}, priority=1, notes="")],
                        sha256="x")
    cfg = {
        "session": {"source_language": "de"},
        "asr": {"speechmatics": {"additional_vocab_max": 50, "max_delay": 1.0}},
        "translate": {"targets": ["es", "fr"]},
        "display": {"draft_translation": False},
    }
    make = runner._adapter_factory(cfg, "speechmatics", glossary)
    adapter = make()
    assert adapter.name == "speechmatics"
    assert adapter.api_key == "sm-test-key"
    assert adapter.language == "de"
    assert "Komintern" in adapter.additional_vocab
    assert adapter.target_languages == []   # draft_translation off in phase 1
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_runner.py::test_adapter_factory_builds_speechmatics -q`
Expected: FAIL — `SystemExit: unknown ASR adapter: 'speechmatics'`.

- [ ] **Step 3: Implement** — add the branch to `_adapter_factory` in `src/livetranslate/runner.py`, immediately before the `raise SystemExit(...)` line (currently `runner.py:35`):

```python
    if name == "speechmatics":
        from .asr.speechmatics import SpeechmaticsRTAdapter
        sm = cfg["asr"]["speechmatics"]
        # Phase 2 turns this on; phase 1 keeps targets empty (transcription only).
        targets = (cfg["translate"]["targets"]
                   if cfg.get("display", {}).get("draft_translation") else [])
        def make():
            return SpeechmaticsRTAdapter(
                api_key=os.environ["SPEECHMATICS_API_KEY"],
                language=cfg["session"]["source_language"],
                additional_vocab=glossary.keyterms(cap=sm["additional_vocab_max"]),
                target_languages=targets,
                additional_vocab_max=sm["additional_vocab_max"],
                max_delay=sm["max_delay"])
        return make
```

- [ ] **Step 4: Run tests**

Run: `.venv/bin/python -m pytest tests/test_runner.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/livetranslate/runner.py tests/test_runner.py
git commit -m "feat(asr): wire Speechmatics into the runner adapter factory"
```

---

**Phase 1 is complete and shippable here.** To run Speechmatics as primary with ElevenLabs failover, set in `config.toml`:

```toml
[asr]
adapter = "speechmatics"
failover = "elevenlabs"
```

and add `SPEECHMATICS_API_KEY=...` to `.env`. Phase 2 adds the draft translation layer.

---

## Phase 2 — Instant draft translation layer

### Task 6: Thread `on_draft` through the ASR protocol and ResilientASR

**Files:**
- Modify: `src/livetranslate/asr/base.py`
- Modify: `src/livetranslate/asr/elevenlabs.py:58`
- Modify: `src/livetranslate/asr/assemblyai.py:58`
- Test: `tests/test_resilient_asr.py` (append)

- [ ] **Step 1: Write the failing test** (append to `tests/test_resilient_asr.py`; read the file first to reuse its existing fake-adapter helper)

```python
def test_resilient_passes_on_draft_to_adapter():
    """ResilientASR forwards on_draft to the wrapped adapter's start()."""
    from livetranslate.asr.base import ResilientASR

    captured = {}

    class DraftAdapter:
        name = "draft-fake"
        def start(self, on_event, on_status, on_draft=None):
            captured["on_draft"] = on_draft
        def send_audio(self, chunk):
            pass
        def flush_and_stop(self, timeout_s=8.0):
            pass

    r = ResilientASR(lambda: DraftAdapter(), ring=None)
    sink = lambda lang, text: None
    r.start(on_event=lambda e: None, on_status=lambda e: None, on_draft=sink)
    assert captured["on_draft"] is sink
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_resilient_asr.py::test_resilient_passes_on_draft_to_adapter -q`
Expected: FAIL — `TypeError: start() got an unexpected keyword argument 'on_draft'`.

- [ ] **Step 3: Implement — protocol + type in `base.py`**

In `src/livetranslate/asr/base.py`, after the `OnStatus` alias (line 9) add:

```python
OnDraft = Callable[[str, str], None]   # (app_lang, draft_text)
```

Change the protocol `start` signature (line 16) from:

```python
    def start(self, on_event: OnEvent, on_status: OnStatus) -> None: ...
```

to:

```python
    def start(self, on_event: OnEvent, on_status: OnStatus,
              on_draft: "OnDraft | None" = None) -> None: ...
```

- [ ] **Step 4: Implement — `ResilientASR.start` stores and forwards `on_draft`**

In `ResilientASR.start` (currently `base.py:56-68`), change the signature and body. Replace:

```python
    def start(self, on_event: OnEvent, on_status: OnStatus) -> None:
        self.on_status = on_status

        def tracking_on_event(ev):
            if ev.kind == "final":
                self.last_final_end_ms = max(self.last_final_end_ms, ev.t_audio_end_ms)
            on_event(ev)

        self.on_event = tracking_on_event
        with self._lock:
            self._adapter = self._factory()
            self._adapter.start(self.on_event, self._on_adapter_status)
            self.session_started_at = time.monotonic()
```

with:

```python
    def start(self, on_event: OnEvent, on_status: OnStatus,
              on_draft=None) -> None:
        self.on_status = on_status
        self.on_draft = on_draft

        def tracking_on_event(ev):
            if ev.kind == "final":
                self.last_final_end_ms = max(self.last_final_end_ms, ev.t_audio_end_ms)
            on_event(ev)

        self.on_event = tracking_on_event
        with self._lock:
            self._adapter = self._factory()
            self._adapter.start(self.on_event, self._on_adapter_status, self.on_draft)
            self.session_started_at = time.monotonic()
```

Also add `self.on_draft = None` to `ResilientASR.__init__` next to the other handler defaults (after `self._adapter = None`, around `base.py:44`):

```python
        self.on_draft = None
```

- [ ] **Step 5: Implement — forward `on_draft` on reconnect**

In `ResilientASR._reconnect`, the adapter is restarted at (currently) `base.py:135`:

```python
                    self._adapter.start(self.on_event, self._on_adapter_status)
```

Change it to:

```python
                    self._adapter.start(self.on_event, self._on_adapter_status, self.on_draft)
```

- [ ] **Step 6: Implement — existing adapters accept and ignore `on_draft`**

In `src/livetranslate/asr/elevenlabs.py`, change `start` (line 58):

```python
    def start(self, on_event: OnEvent, on_status: OnStatus) -> None:
        self.on_event, self.on_status = on_event, on_status
```

to:

```python
    def start(self, on_event: OnEvent, on_status: OnStatus, on_draft=None) -> None:
        # on_draft ignored: ElevenLabs has no realtime translation.
        self.on_event, self.on_status = on_event, on_status
```

In `src/livetranslate/asr/assemblyai.py`, change `start` (line 58):

```python
    def start(self, on_event: OnEvent, on_status: OnStatus) -> None:
        self.on_event, self.on_status = on_event, on_status
```

to:

```python
    def start(self, on_event: OnEvent, on_status: OnStatus, on_draft=None) -> None:
        # on_draft ignored: AssemblyAI has no realtime translation.
        self.on_event, self.on_status = on_event, on_status
```

- [ ] **Step 7: Run tests**

Run: `.venv/bin/python -m pytest tests/test_resilient_asr.py tests/test_elevenlabs_adapter.py tests/test_assemblyai_adapter.py tests/test_asr_base.py -q`
Expected: all pass.

- [ ] **Step 8: Commit**

```bash
git add src/livetranslate/asr/base.py src/livetranslate/asr/elevenlabs.py src/livetranslate/asr/assemblyai.py tests/test_resilient_asr.py
git commit -m "feat(asr): thread optional on_draft callback through ResilientASR and adapters"
```

---

### Task 7: DisplayState — draft storage + SSE frame

**Files:**
- Modify: `src/livetranslate/display/server.py`
- Test: `tests/test_display_server.py` (append)

- [ ] **Step 1: Write the failing tests** (append to `tests/test_display_server.py`; read the file first to match how it constructs `DisplayState`)

```python
def test_display_state_set_draft_bumps_version_and_stores_per_lang():
    from livetranslate.display.server import DisplayState
    st = DisplayState(langs=["es", "fr"])
    v0 = st.version
    st.set_draft("es", "el primer movimiento")
    assert st.version != v0
    assert st.drafts["es"] == "el primer movimiento"
    assert st.drafts["fr"] == ""


def test_snapshot_lang_includes_current_draft_frame():
    from livetranslate.display.server import DisplayState
    st = DisplayState(langs=["es"])
    st.set_draft("es", "borrador en vivo")
    items = st.snapshot_lang("es", after_sid=-1)
    drafts = [i for i in items if i["type"] == "draft"]
    assert drafts == [{"type": "draft", "lang": "es", "text": "borrador en vivo"}]


def test_snapshot_src_has_no_draft_frame():
    from livetranslate.display.server import DisplayState
    st = DisplayState(langs=["es"])
    st.set_draft("es", "x")
    assert all(i["type"] != "draft" for i in st.snapshot_lang("src", after_sid=-1))
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_display_server.py -q`
Expected: FAIL — `AttributeError: 'DisplayState' object has no attribute 'set_draft'`.

- [ ] **Step 3: Implement — draft storage in `DisplayState`**

In `src/livetranslate/display/server.py`, in `DisplayState.__init__` (after `self.tentative_tail = ""`, line 24) add:

```python
        self.drafts = {l: "" for l in langs}   # live draft translation per lang
```

After the `set_tail` method (line 45) add:

```python
    def set_draft(self, lang: str, text: str):
        with self._cond:
            if lang in self.drafts:
                self.drafts[lang] = text
                self._bump()
```

- [ ] **Step 4: Implement — emit the draft in the per-language snapshot**

In `DisplayState.snapshot_lang`, the non-`src` branch currently returns `items` built from translations (lines 83-90). Append the current draft to that branch. Change the `else` branch's `return items` so the method ends like this — replace the whole `else:` block body (lines 82-91) with:

```python
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
```

(The `if lang == "src":` branch above it is unchanged; only the `else` branch and the shared `return items` are shown.)

- [ ] **Step 5: Run tests**

Run: `.venv/bin/python -m pytest tests/test_display_server.py -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add src/livetranslate/display/server.py tests/test_display_server.py
git commit -m "feat(display): per-language draft translation state + SSE draft frame"
```

---

### Task 8: Pipeline — route `on_draft` to DisplayState (gated by config)

**Files:**
- Modify: `src/livetranslate/pipeline.py`
- Modify: `src/livetranslate/runner.py` (start call)
- Test: `tests/test_pipeline.py` (append)

- [ ] **Step 1: Write the failing test** (append to `tests/test_pipeline.py`; read the file first to reuse its existing pipeline/adapter fakes and `cfg` builder)

```python
def test_pipeline_on_draft_sets_display_state_when_enabled(make_pipeline):
    """on_draft updates the per-language draft when display.draft_translation is on.

    `make_pipeline` is the existing helper in this file; if it does not accept a
    cfg override, build the Pipeline the same way the other tests in this file do,
    with cfg['display']['draft_translation'] = True and targets ['es'].
    """
    pipe = make_pipeline(targets=["es"], draft_translation=True)
    pipe._on_draft("es", "borrador")
    assert pipe.state.drafts["es"] == "borrador"


def test_pipeline_on_draft_noop_when_disabled(make_pipeline):
    pipe = make_pipeline(targets=["es"], draft_translation=False)
    pipe._on_draft("es", "borrador")
    assert pipe.state.drafts["es"] == ""
```

> Note for the implementer: if `make_pipeline` in `tests/test_pipeline.py` does not already take `targets`/`draft_translation` kwargs, extend that helper (or construct `Pipeline` inline using the same fakes the file already uses) so these two tests can set `cfg["display"]["draft_translation"]` and `cfg["translate"]["targets"]`. Do not change `Pipeline`'s constructor signature for this.

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_pipeline.py -q`
Expected: FAIL — `AttributeError: 'Pipeline' object has no attribute '_on_draft'`.

- [ ] **Step 3: Implement — `_on_draft` on the Pipeline**

In `src/livetranslate/pipeline.py`, in `Pipeline.__init__`, after `self._draining = False` (line 59) add:

```python
        self._draft_enabled = bool(cfg.get("display", {}).get("draft_translation"))
```

Add the callback next to the other adapter callbacks (after `_on_translation`, line 87):

```python
    def _on_draft(self, lang: str, text: str) -> None:
        # Speechmatics realtime translation: a fast, glossary-unaware draft shown
        # as a provisional italic line until the LLM translation for that region
        # of speech lands. Disabled unless display.draft_translation is set.
        if self._draft_enabled:
            self.state.set_draft(lang, text)
```

- [ ] **Step 4: Implement — pass `on_draft` when the adapter starts**

In `src/livetranslate/pipeline.py`, `Pipeline.start` currently calls (line 91):

```python
        self.adapter.start(on_event=self._on_event, on_status=self._on_status)
```

Change to:

```python
        self.adapter.start(on_event=self._on_event, on_status=self._on_status,
                           on_draft=self._on_draft)
```

- [ ] **Step 5: Run tests**

Run: `.venv/bin/python -m pytest tests/test_pipeline.py -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add src/livetranslate/pipeline.py tests/test_pipeline.py
git commit -m "feat(pipeline): route Speechmatics draft translations to the display"
```

---

### Task 9: Audience view — italic fade-in draft line

**Files:**
- Modify: `src/livetranslate/display/static/view.html`

This task is client-side rendering; verified by manual smoke (no unit test framework for the static page in this repo).

- [ ] **Step 1: Add the draft styling**

In `src/livetranslate/display/static/view.html`, inside the `<style>` block, after the `p.s.pbreak` rule (line 15) add:

```css
  /* draft = Speechmatics realtime translation, shown until the LLM caption lands */
  p.s.draft { font-style: italic; color: var(--dim);
              animation: draftfade .4s ease-in; }
  @keyframes draftfade { from { opacity: 0; } to { opacity: 1; } }
```

- [ ] **Step 2: Render and replace the draft line in the SSE handler**

In the `<script>` block, replace the entire `es.onmessage` handler (lines 56-68) with:

```javascript
// The draft line is a single provisional <p> pinned at the bottom. Real
// translations append ABOVE it as regular text; the draft updates in place as
// new partials arrive, and is removed when empty. This yields "italic draft
// that becomes regular text once the authoritative translation arrives".
let draftEl = null;
function removeDraft() {
  if (draftEl) { draftEl.remove(); draftEl = null; }
}
es.onmessage = (m) => {
  const ev = JSON.parse(m.data);
  if (ev.type === "translation" || ev.type === "sentence") {
    const wasAtBottom = atBottom();
    // Insert the finalized line before the draft so the draft stays at the edge.
    const p = document.createElement("p");
    p.className = "s" + (ev.paragraph_break && textEl.childElementCount ? " pbreak" : "");
    p.textContent = ev.text;
    textEl.insertBefore(p, draftEl);   // draftEl null -> appends at end
    if (wasAtBottom) scrollTo(0, document.body.scrollHeight); else maybeFollow();
  } else if (ev.type === "draft") {
    const wasAtBottom = atBottom();
    if (!ev.text) { removeDraft(); return; }
    if (!draftEl) {
      draftEl = document.createElement("p");
      draftEl.className = "s draft";
      textEl.appendChild(draftEl);
    }
    draftEl.textContent = ev.text;
    if (wasAtBottom) scrollTo(0, document.body.scrollHeight);
  } else if (ev.type === "tail") {
    document.getElementById("activity").hidden = !ev.text;
  }
};
```

- [ ] **Step 3: Smoke-check the page renders** (offline; no Speechmatics needed)

Run: `.venv/bin/python -c "import re,pathlib; h=pathlib.Path('src/livetranslate/display/static/view.html').read_text(); assert 's draft' in h and 'draftfade' in h and 'insertBefore' in h; print('view.html ok')"`
Expected: `view.html ok`

- [ ] **Step 4: Commit**

```bash
git add src/livetranslate/display/static/view.html
git commit -m "feat(display): italic fade-in draft caption replaced by final translation"
```

---

### Task 10: Documentation & config example

**Files:**
- Modify: `config.toml`
- Modify: `README.md`

- [ ] **Step 1: Update `config.toml`**

In `config.toml`, change the `[asr]` block comment for `adapter` (line 11) and `failover` (line 12) to include speechmatics, and add an `[asr.speechmatics]` block after the `[asr.assemblyai]` block (after line 24):

```toml
[asr.speechmatics]
# additional_vocab has a latency/memory penalty for large lists (docs/vendor-notes.md).
additional_vocab_max = 50
# max_delay (seconds): lower = faster partials but more revisions. 1.0 is a good default.
max_delay = 1.0
```

Change line 11 from:

```toml
adapter = "elevenlabs"          # "elevenlabs" | "assemblyai"
```

to:

```toml
adapter = "elevenlabs"          # "elevenlabs" | "assemblyai" | "speechmatics"
```

And in the `[display]` block, after `font_scale` (line 48), add:

```toml
# Show Speechmatics realtime translation as an instant italic draft caption,
# replaced by the glossary-accurate LLM translation when it lands. Requires
# adapter = "speechmatics". No effect with other adapters.
draft_translation = false
```

- [ ] **Step 2: Update `README.md`**

Add a subsection under the ASR/providers documentation describing: Speechmatics as primary ASR (`adapter = "speechmatics"`, `failover = "elevenlabs"`), the `SPEECHMATICS_API_KEY` env var, the EU endpoint, and the `display.draft_translation` instant-draft layer (italic → regular). Mirror the wording/structure of the existing ElevenLabs/AssemblyAI provider notes already in the README.

- [ ] **Step 3: Verify the example config still loads**

Run: `.venv/bin/python -c "from livetranslate.config import load_config; c=load_config('config.toml'); print(c['asr']['speechmatics'], c['display']['draft_translation'])"`
Expected: prints `{'additional_vocab_max': 50, 'max_delay': 1.0} False`

- [ ] **Step 4: Full suite**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add config.toml README.md
git commit -m "docs: document Speechmatics adapter and instant draft translation layer"
```

---

## Verification & rollout

- [ ] Run the full suite green: `.venv/bin/python -m pytest tests/ -q`
- [ ] **Live Phase 0 validation** (Task 1 Step 3 checklist) with a real `SPEECHMATICS_API_KEY` — correct fixtures/adapter if the live wire format differs from the schema-derived fixtures.
- [ ] Bake-off via `harness/bakeoff.py`: Speechmatics vs ElevenLabs on a recorded talk — glossary-term WER, partial/final latency, draft-vs-final lag, cost. Decide primary on data.
- [ ] Confirm failover: kill the Speechmatics connection mid-session and verify `ResilientASR` fails over to ElevenLabs (`give_up_after_s` > 0 + `failover = "elevenlabs"`).

---

## Self-review notes

- **Spec coverage:** Phase 1 (adapter + failover) = Tasks 2–5; Phase 2 (draft layer) = Tasks 6–10; italic fade-in replaced-by-regular = Task 9; ElevenLabs failover = Task 5 + config (already supported by `ResilientASR.failover_factory`). Live verification = Task 1 + rollout.
- **Glossary fidelity preserved:** the LLM translator (`translate.py`) and its `block_for(lang)` rendering are untouched; the draft is explicitly non-authoritative.
- **Type consistency:** `on_draft(app_lang: str, text: str)` is used identically in `base.py` (`OnDraft`), the Speechmatics adapter, `ResilientASR`, and `Pipeline._on_draft`. Draft SSE frame shape `{"type":"draft","lang","text"}` matches between `snapshot_lang` (Task 7) and `view.html` (Task 9). Language codes cross the app/Speechmatics boundary only via `to_sm`/`to_app` (Task 3/4).
