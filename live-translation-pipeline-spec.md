# livetranslate — Design Specification v1.0

**Project:** Live conference speech → multilingual reading captions
**Implementation agent:** Claude Code
**Status:** Ready for implementation. Build order in §10. Vendor-fact verification checklist in §13 must be completed during M1/M6 — do not trust vendor wire-format details in this document.

---

## 1. Purpose & Scope

A single-machine pipeline that captures a conference speaker's audio, transcribes it in real time with **ElevenLabs Scribe v2 Realtime** (behind a swappable ASR adapter), segments the transcript into finalized sentences, translates each finalized sentence into multiple target languages via an LLM API with a strict terminology glossary, and serves per-language reading displays to browsers on the local network.

**Design priorities, in order:**
1. **Stability across a 2-hour continuous session** (auto-reconnect, append-only state, crash-resume, no unbounded memory growth).
2. **Accuracy on domain jargon and proper nouns** (ASR keyterms + glossary-enforced translation).
3. **Consistency across speaker accents** (vendor choice already made on this basis; bake-off harness validates it on our own recordings).
4. Readable, flicker-free output: finalized text is **never revised** on screen.

**Target languages:** Spanish (`es`), French (`fr`), German (`de`), Portuguese (`pt`) required; Arabic (`ar`) and Chinese (`zh`) behind config flags. Source language pinned per session (expected `en` or `de`).

**Non-goals (v1):**
- No speech output / TTS.
- No speaker labels / diarization.
- No audience-scale serving (design assumption: ≤ 25 concurrent display clients — projector machines and staff laptops, not audience phones).
- No local/offline inference fallback.
- No live translation of *tentative* (partial) text — target displays show finalized sentences plus an activity indicator. (A draft-translation mode exists as a config flag, default off, and may be skipped in v1.)
- No automatic MT quality scoring (BLEU/COMET); translation adequacy is reviewed by humans via the harness's side-by-side report.

---

## 2. Binding Implementation Constraints

These reflect the owner's engineering preferences and are **not negotiable** without checking back:

1. **Python ≥ 3.11. Plain synchronous Python.** Concurrency via `threading` + `queue.Queue` only. **No `asyncio`. No web frameworks** (no FastAPI/Flask/Starlette). The display server uses stdlib `http.server.ThreadingHTTPServer` with Server-Sent Events.
2. **Minimal dependencies.** Runtime: `sounddevice`, `numpy`, `websocket-client`, `requests`. Harness-only: `jiwer` (WER; acceptable), `ffmpeg` invoked via `subprocess` for decoding recordings (no Python audio-decode deps). Everything else stdlib (`tomllib`, `json`, `wave`, `dataclasses`, `unicodedata`, ...).
3. **Secrets only via environment variables** (`ELEVENLABS_API_KEY`, `ASSEMBLYAI_API_KEY`, `TRANSLATE_API_KEY`). Never in config files or logs.
4. **Append-only persistence.** All finalized state is written as JSONL as it happens; a process restart with `--resume` must rebuild displays exactly.
5. **Vendor wire formats are verified against live documentation at implementation time** (§13). This spec fixes *our* interfaces precisely; any vendor field names shown are indicative placeholders.
6. Runs on a base M4 Mac mini (24 GB) doing orchestration + display only. No model inference happens on this machine.

---

## 3. Architecture

```
 mixing desk / mic                    recordings (harness)
        │                                     │
   MicSource ──────────┐              FileSource (paced, rtf=1.0)
        │              │                      │
        └──► AudioChunk queue ◄───────────────┘
                  │
                  ├──────────────► RingBuffer (≥120 s, indexed by stream-time)
                  ▼                          │ replay_from(ms) on reconnect
        ResilientASR wrapper ◄───────────────┘
        └── ASRAdapter (ElevenLabsScribeAdapter │ AssemblyAIStreamingAdapter)
                  │  normalized TranscriptEvents (partial / final)
                  ▼
              Segmenter  ── tentative_tail ──► DisplayServer (source view)
                  │  Sentence (sid, text, t_audio_*)  [monotonic, never revised]
                  ▼
        ┌─── per-language queues (es, fr, de, pt, [ar], [zh]) ───┐
        │                                                        │
   TranslationWorker ×N  ── HTTP (requests) ──►  LLM provider (config)
        │  Translation (sid, lang, text)
        ▼
      Store (JSONL, append-only, fsync)
        │
        ▼
   DisplayServer (stdlib ThreadingHTTPServer + SSE)
        ├── GET /            operator console (source, health, lag per lang)
        ├── GET /v/{lang}    audience reading page (finalized paragraphs + "…")
        └── GET /events?lang SSE stream with Last-Event-ID replay
```

**Thread & queue inventory** (all threads daemon=False, joined on shutdown):

| Thread | Role | Reads | Writes |
|---|---|---|---|
| PortAudio callback | mic capture | device | audio queue, RingBuffer |
| `asr-sender` | pace/forward audio to WS | audio queue | vendor WS |
| `asr-receiver` | blocking WS recv loop | vendor WS | event queue |
| `segmenter` | finalization state machine | event queue | sentence fan-out, Store |
| `xlate-{lang}` ×N | per-language translation | lang queue | Store, display state |
| HTTP pool | one thread per SSE client | display state | client sockets |
| `watchdog` | health, restarts, failover | gauges | status events, Store |
| `store-flush` | periodic fsync | — | disk |

The pipeline must keep working (with status banners) if any single downstream stage stalls: queues are bounded (`maxsize` in config) and the policy on a full translation queue is **block the producer for that language only**, never the segmenter.

---

## 4. Data Model

```python
# src/livetranslate/types.py
from dataclasses import dataclass, field

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
    t_audio_start_ms: int   # mapped to stream timeline by the adapter
    t_audio_end_ms: int
    vendor: str             # "elevenlabs" | "assemblyai"
    t_received_wall: float  # time.monotonic()
    vendor_raw: dict        # full original message, logged for forensics

@dataclass(frozen=True)
class Sentence:
    sid: int                # monotonic, gapless per session
    text: str
    t_audio_start_ms: int
    t_audio_end_ms: int
    t_finalized_wall: float

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

Invariants the test suite must enforce: `sid` strictly increasing with no gaps; for every `sid` and every enabled `lang`, exactly one terminal `Translation` (status ok or failed); a `Sentence`'s text never appears twice (reconnect dedupe, §5.3).

---

## 5. Components

### 5.1 Audio sources (`audio.py`)

- **MicSource** — `sounddevice.RawInputStream`, 16 kHz mono PCM16, block size = `audio.chunk_ms` (default 100 ms; allowed 50–200). Input device selected by substring match on name in config (the mixing-desk interface, never the built-in mic by default — refuse to start if the configured device is not found).
- **FileSource** (harness) — decode any container with `ffmpeg -i <file> -f s16le -ac 1 -ar 16000 -` via `subprocess.Popen`; emit chunks **paced at `harness.rtf`** (default `1.0`, i.e., real time). Values > 1.0 are permitted only if the vendor's realtime API tolerates faster-than-realtime input — verify (§13); default behavior must remain paced.
- **RingBuffer** — thread-safe byte ring holding ≥ `audio.ring_seconds` (default 120 s) of PCM, addressable by stream-time ms: `replay_from(ms) -> Iterator[AudioChunk]`. Used only by the reconnect path.

### 5.2 ASR adapter layer (`asr/`)

```python
# asr/base.py
class ASRAdapter:
    name: str
    def start(self, on_event, on_status) -> None: ...   # spawns sender/receiver threads
    def send_audio(self, chunk: AudioChunk) -> None: ...
    def flush_and_stop(self, timeout_s: float = 8.0) -> None: ...
```

Adapters own their WebSocket (blocking `websocket-client`), their sender/receiver threads, and the mapping from vendor audio-relative timestamps to the session stream timeline (offset captured at session start / each reconnect). They emit **normalized** `TranscriptEvent`s and `StatusEvent`s only.

**ResilientASR** wraps any adapter and is the only component the rest of the pipeline sees:
- Reconnect with exponential backoff 0.5 s → 8 s (jittered), unlimited attempts until `asr.give_up_after_s` (default: never).
- On reconnect: re-send session config (language, keyterms, audio format), then replay audio from `last_final_end_ms − overlap_ms` (default 2000 ms) out of the RingBuffer, then resume live.
- Proactive rotation: if the vendor documents a max session duration (§13), schedule a reconnect at the next ≥ 1.5 s pause in `tentative_tail` activity, at ~80% of the limit.
- Emits `StatusEvent`s: `connected`, `reconnecting(n)`, `replaying`, `gave_up`.

**5.2.1 ElevenLabsScribeAdapter (primary).** Config: pinned `language` (no auto-detect mid-session), PCM 16 kHz input, partials enabled, **keyterms** from the glossary (cap 100; note: keyterm usage is surcharged — log the count at startup). All endpoint URLs, auth, message schema, keyterm field names/limits, keepalive and session-duration rules: **verify against current ElevenLabs docs first** (§13).

**5.2.2 AssemblyAIStreamingAdapter (bake-off + failover).** Universal-3 Pro Streaming. Same normalization contract. Additionally supports a free-form domain prompt (`asr.assemblyai.prompt`) generated from the glossary's `domain_blurb`. Verify schema per §13.

### 5.3 Segmenter (`segmenter.py`) — the finalization state machine

Input: stream of `TranscriptEvent`s. Output: `Sentence`s — **monotonic, append-only, never revised** — plus a continuously updated `tentative_tail` string (latest partial) for the source display.

Rules:
1. Only `final` events enter the committed buffer; partials only update `tentative_tail`.
2. **Reconnect dedupe:** drop any final whose `t_audio_end_ms ≤ last_committed_end_ms + 250`. For finals overlapping the boundary, merge by longest common token suffix/prefix; log every merge decision.
3. Emit a `Sentence` when any of:
   - committed buffer ends with terminal punctuation (`. ! ? … 。 ！ ？`) followed by whitespace/EOS;
   - committed buffer length ≥ `segmenter.max_words` (default 45) → cut at the last comma/clause boundary, else hard cut;
   - oldest committed-but-unemitted text is older than `segmenter.max_pending_s` (default 12 s) → same forced-cut rule. (Guarantees the display never silently stalls on a speaker who doesn't pause.)
4. Normalize whitespace; preserve vendor casing and punctuation otherwise.
5. Paragraph hint: tag a sentence `paragraph_break=True` when the audio gap to the previous sentence exceeds 4 s (displays use this; not part of the dataclass above — add field).

Edge cases to handle + unit-test: empty/whitespace finals, duplicate finals, out-of-order timestamps (log `warn`, drop), finals arriving during replay, session end with a non-empty buffer (`flush_and_stop` emits the remainder as a final sentence).

### 5.4 Glossary (`glossary.py`)

Single TSV, UTF-8, columns:

```
term_src	es	fr	de	pt	ar	zh	priority	notes
rate of profit	tasa de ganancia	taux de profit	Profitrate	taxa de lucro			1	
primitive accumulation	acumulación originaria	accumulation primitive	ursprüngliche Akkumulation	acumulação primitiva			1	canonical renderings only
Tübingen	Tübingen	Tübingen	Tübingen	Tübingen			2	keep as-is
```

- Empty target cell ⇒ **keep the source term untranslated** in that language.
- Derived artifacts: (a) ASR keyterm list = `term_src` sorted by `priority` then length, truncated to the vendor cap (warn if truncated); (b) per-language glossary block for the MT system prompt; (c) normalized term set for harness metrics (§8). A separate optional `domain_blurb.txt` (2–4 sentences describing the event's subject) feeds AssemblyAI's prompt field and the MT system prompt.

### 5.5 Translator (`translate.py`)

```python
class Translator:
    def translate(self, sentence: Sentence, lang: str, ctx: TransContext) -> Translation: ...
```

**LLMTranslator** — synchronous `requests` POST to a configurable chat-completion-style endpoint (`translate.base_url`, `translate.model`, `translate.api_key_env`, `translate.provider` selects the request/response mapping; implement mappings for at least two providers and verify request schemas from their docs, §13). Temperature 0. Timeout `translate.timeout_s` (default 10), 2 retries with backoff; on terminal failure emit `Translation(status="failed", text="⟨translation unavailable⟩")` and **continue** — a language queue must never wedge.

System prompt template (fixed, per language):

```
You are a professional simultaneous interpreter producing written captions for a live conference.
Translate the SOURCE sentence into {lang_name}.
Rules:
- Output ONLY the translation. No quotes, no notes, no commentary.
- Register: natural spoken-presentation {lang_name}; faithful to meaning; do not summarize or embellish.
- Keep numbers, units, and personal names exact.
- Apply this glossary strictly (source term → required rendering; identical rendering means keep the term untranslated):
{glossary_block}
- CONTEXT lines are for cohesion only. Translate ONLY the SOURCE line.
{domain_blurb_line}
```

User message: `CONTEXT (source): <previous 2 source sentences>` / `CONTEXT (your previous output): <previous translation in this lang>` / `SOURCE: <sentence>`.

**Worker model:** one thread per enabled language consuming its own ordered queue. **Catch-up batching:** if queue depth > 3, merge up to 6 pending sentences into a single numbered-list request and split the numbered response; on any parse mismatch, fall back to per-sentence calls for that batch. Optional fallback provider (`translate.fallback`) tried once per sentence after the primary fails terminally.

Arabic note: translations are plain text; RTL is purely a display concern (§5.7).

### 5.6 Store (`store.py`)

Session directory `sessions/<YYYYMMDD-HHMM>/` containing `events.jsonl` (every TranscriptEvent + StatusEvent), `sentences.jsonl`, `translations.jsonl`, `meta.json` (config snapshot, adapter, model, glossary hash). Writes are append-only, line-buffered, fsync every 2 s from `store-flush`. `--resume <dir>` reloads sentences + translations, rebuilds display state, and continues the `sid` counter.

### 5.7 DisplayServer (`display/`)

stdlib `ThreadingHTTPServer`; all HTML/CSS/JS inline in static files, no build step, no frameworks.

- `GET /v/{lang}` — audience page: finalized sentences joined into paragraphs (`paragraph_break` hints), large readable type (`display.font_scale`), auto-scroll that pauses while the user scrolls, a subtle "…" activity indicator while `tentative_tail` is non-empty, dark/light toggle. `lang=ar` sets `dir="rtl"`.
- `GET /` — operator console: live source transcript with tentative tail (tail styled italic/gray), connection status banner, per-language translation lag (newest sid translated vs newest sid finalized), reconnect counter, RSS.
- `GET /events?lang={lang|src|status}` — **SSE**. On connect (or `Last-Event-ID`), replay state from that sid, then stream live `{type: sentence|translation|tail|status, ...}` events. SSE event `id:` = sid so browser `EventSource` auto-reconnect is lossless. One handler thread per client; assume ≤ 25 clients.

### 5.8 Health & Watchdog (`health.py`)

- **ASR stall detection:** if ≥ `health.stall_s` (default 10) of audio has been sent with zero events received → force reconnect via ResilientASR.
- Gauges sampled every 5 s and logged every 60 s: queue depths, audio-time vs wall-time drift, RSS (via `resource.getrusage`), per-language lag.
- Restart policy: any dead worker thread is logged `error` and restarted once; a second death of the same worker within 10 min → red operator banner.
- Optional ASR failover: `asr.failover = "assemblyai"` — after `gave_up`, ResilientASR swaps adapters live (replay from RingBuffer as in a normal reconnect).
- `SIGINT`: stop source → `flush_and_stop` ASR → drain translation queues (≤ 15 s) → close store → exit 0.

---

## 6. Latency Budget (targets, measured by the harness)

| Stage | Target p50 | Target p95 |
|---|---|---|
| audio → first vendor partial | ≤ 1.0 s | ≤ 2.0 s |
| audio end of phrase → vendor final | ≤ 2.0 s | ≤ 3.5 s |
| final → Sentence emitted (segmenter) | ≤ 0.3 s | ≤ 0.6 s (excl. forced-cut waits) |
| Sentence → Translation per language | ≤ 1.2 s | ≤ 3.0 s |
| **end-to-end: audio end → translated caption on display** | **≤ 4.0 s** | **≤ 7.0 s** |

These match the 2–3 s "human simultaneous interpreter" comfort band plus translation; tentative source text keeps perceived latency ~1 s on the operator/source view.

---

## 7. Configuration (`config.toml`)

```toml
[session]
source_language = "en"          # pinned per session; "de" supported
output_dir = "sessions"

[audio]
device_substring = "Scarlett"   # refuse to start if not found
chunk_ms = 100
ring_seconds = 120

[asr]
adapter = "elevenlabs"          # "elevenlabs" | "assemblyai"
failover = ""                   # optional: "assemblyai"
give_up_after_s = 0             # 0 = never
overlap_ms = 2000

[asr.elevenlabs]
# endpoint/model fields filled in after doc verification (§13)
keyterms_max = 100

[asr.assemblyai]
use_domain_prompt = true

[segmenter]
max_words = 45
max_pending_s = 12

[translate]
targets = ["es", "fr", "de", "pt"]   # add "ar", "zh" to enable
provider = "..."                      # request/response mapping key
base_url = "..."
model = "..."                         # fast tier; set at deploy time
api_key_env = "TRANSLATE_API_KEY"
timeout_s = 10
batch_threshold = 3
batch_max = 6
# [translate.fallback] optional second provider block

[glossary]
path = "glossary.tsv"
domain_blurb = "domain_blurb.txt"

[display]
host = "0.0.0.0"
port = 8080
font_scale = 1.6

[health]
stall_s = 10

[harness]
rtf = 1.0
```

---

## 8. Test Harness (`harness/`)

The harness drives the **identical pipeline** (FileSource swapped for MicSource; DisplayServer optional) so that what we test is what runs live.

**`run_file.py`** — `python -m harness.run_file --config config.toml --audio recordings/talk1.mp3 --ref refs/talk1.txt --adapter elevenlabs --langs es,fr,de,pt [--rtf 1.0] [--no-display]`
Runs the file end-to-end, writes the session dir plus `report.json` and `report.md` (aligned source/target table per language for human review of translation adequacy).

**`metrics.py`** computes, from the session JSONL + reference transcript:
- **WER** (jiwer; normalization: lowercase, NFKC, strip punctuation, collapse whitespace).
- **Jargon recall** — for every glossary `term_src` occurrence in the reference, is it present (normalized; hyphen/space-insensitive) in the hypothesis? Report overall % and a per-term table sorted worst-first. This is the primary bake-off number.
- **Latency percentiles** per §6, derived from stream-time vs `t_received_wall` (the harness controls feed pacing, so audio-time ↔ wall-time mapping is exact).
- **Invariant checks:** gapless sids, no duplicated sentence text, one terminal translation per (sid, lang).

**`chaos.py`** — runs a file and severs the WS (close socket / drop route) at configured offsets; asserts recovery: 0 lost finalized sentences, ≤ 1 duplicated-then-deduped boundary sentence, audio gap covered by RingBuffer replay, reconnect count logged.

**Soak test** — `--audio` accepts a list; concatenate to ≥ 2 h (or `--loop 4`). Pass criteria: zero unrecovered disconnects, zero dead threads, RSS growth < 150 MB between minute 10 and end, invariants hold.

**`bakeoff.py`** — runs the same audio + same glossary through both adapters sequentially and emits `bakeoff.csv` + a markdown table: WER, jargon recall, final-lag p50/p95, reconnects, cost notes. **Decision rule:** prefer ElevenLabs unless AssemblyAI wins jargon recall by ≥ 3 points or WER by ≥ 1.5 points absolute on our recordings, or recordings show heavy DE/EN code-switching that ElevenLabs visibly fumbles.

Test data expected from the owner in `recordings/` + `refs/`: 3–6 past talks (20–30 min total minimum) covering the accent range and a hand-corrected reference transcript for at least 15 minutes of it, plus a first-draft `glossary.tsv` (~40–80 terms from past slides).

---

## 9. Acceptance Criteria (v1 done when)

1. Harness on the reference set: jargon recall ≥ 90% with keyterms enabled (and a measured uplift vs keyterms-off run).
2. End-to-end finalized-translation latency p50 ≤ 4 s, p95 ≤ 7 s at rtf=1.0.
3. Chaos test passes at 3 different cut offsets, including one mid-sentence.
4. 2-hour soak passes per §8.
5. `--resume` after `kill -9` mid-session restores all displays byte-identical for finalized content.
6. Operator console reflects a forced disconnect within 5 s and shows recovery.
7. Bake-off report generated for both adapters on the same inputs.
8. Glossary renderings verified present in ≥ 95% of translated sentences containing a glossary term (string check per language, harness-reported).

---

## 10. Build Milestones (each independently runnable & tested)

- **M0 — Skeleton.** Repo layout (§11), config loading, types, JSONL store, logging. DoD: `python -m livetranslate --config config.toml --help` works; unit tests green.
- **M1 — ElevenLabs adapter (verify docs first).** Complete §13 items for ElevenLabs + chosen LLM provider. FileSource → adapter → normalized finals printed to console. DoD: a 5-min recording transcribes with correct stream-time mapping.
- **M2 — Segmenter + invariants.** Sentences to console + store; unit tests for every §5.3 rule incl. dedupe with synthetic overlapping finals. DoD: invariant checks pass on a 20-min file.
- **M3 — Translator + glossary.** Single language end-to-end to console; then all four; catch-up batching. DoD: `report.md` side-by-side table renders; glossary check ≥ 95% on a seeded test sentence set.
- **M4 — DisplayServer.** SSE with Last-Event-ID replay; `/v/{lang}` incl. `ar` RTL; operator console. DoD: refresh mid-session loses nothing; 10 simulated SSE clients stable.
- **M5 — Resilience.** RingBuffer replay, ResilientASR reconnect/backoff, stall watchdog, chaos test, `--resume`, soak. DoD: §9 items 3–6.
- **M6 — AssemblyAI adapter + bake-off.** Verify AssemblyAI docs; implement adapter + domain prompt; `bakeoff.py`. DoD: §9 item 7.
- **M7 — Polish.** MicSource live path, device guard, SIGINT drain, metrics polish, README runbook (pre-event checklist: device check, glossary load count, keyterm count, test sentence through all languages).

---

## 11. Repository Layout

```
livetranslate/
  config.toml
  glossary.tsv
  domain_blurb.txt
  src/livetranslate/
    __main__.py        # CLI: live run / --resume
    types.py
    audio.py           # MicSource, FileSource, RingBuffer
    asr/
      base.py          # ASRAdapter, ResilientASR
      elevenlabs.py
      assemblyai.py
    segmenter.py
    glossary.py
    translate.py
    store.py
    health.py
    display/
      server.py
      static/          # index.html (operator), view.html (per-lang)
  harness/
    run_file.py
    metrics.py
    chaos.py
    bakeoff.py
  tests/               # unit tests; no network (vendor messages replayed from fixtures)
  recordings/          # owner-provided audio (gitignored)
  refs/                # reference transcripts
  sessions/            # output (gitignored)
```

---

## 12. Dependencies

| Package | Why | Scope |
|---|---|---|
| `sounddevice` | mic capture (PortAudio) | runtime |
| `numpy` | PCM handling | runtime |
| `websocket-client` | blocking WS for adapters (fits no-asyncio rule) | runtime |
| `requests` | LLM HTTP calls | runtime |
| `jiwer` | WER in harness | harness only |
| `ffmpeg` (system binary) | decode recordings via subprocess | harness only |

Anything beyond this list requires explicit justification in the PR description.

---

## 13. Verify-Against-Docs Checklist (complete during M1/M6 — record findings in `docs/vendor-notes.md`)

**ElevenLabs Scribe v2 Realtime:** WS endpoint + auth; exact message schema for config/audio/partials/finals; supported input encodings & sample rates (target: PCM16 @ 16 kHz); keyterm parameter name, per-request cap, and surcharge; language pinning parameter; keepalive requirements; **max session duration / idle timeout** (drives proactive rotation, §5.2); whether faster-than-realtime input is tolerated (harness `rtf`); finalization/commit semantics (does the API emit explicit finals, and on what cadence).

**AssemblyAI Universal-3 Pro Streaming:** WS endpoint + auth; partial/final message schema; keyterm prompting field + limits; free-form prompt field + limits and interaction with keyterms (docs have noted mutual-exclusivity in some modes — confirm for streaming); language parameter for `en`/`de`; session duration/keepalive; faster-than-realtime tolerance.

**LLM provider(s) for translation:** chat endpoint schema for the chosen provider + fallback; rate limits vs. our worst case (6 languages × catch-up batches); cost per session estimate logged in `meta.json`.

**Rule:** where this spec and current vendor docs disagree, the docs win — note the divergence in `docs/vendor-notes.md` and adjust the adapter, not the internal interfaces.

---

## 14. Risks & Open Questions

1. **Keyterm cap (100) vs glossary size** — mitigated by `priority` column; revisit if recall on low-priority terms suffers (the MT glossary still covers them).
2. **DE/EN code-switching** — if bake-off shows ElevenLabs fumbling mixed-language sentences, AssemblyAI's native code-switching flips the default (§8 decision rule).
3. **Vendor session caps** — unknown until §13; proactive rotation is designed in regardless.
4. **Draft translation of tentative text** (config `display.draft_translation`, default off) — adds API volume and flicker risk; only implement if reading-latency feedback from a live trial demands it.
5. **Audience-scale serving** — out of scope; the SSE design ports cleanly behind nginx if ever needed.
6. **Translation adequacy regression** — no automated metric in v1; the `report.md` review step is mandatory before each event when the glossary or model changes.
