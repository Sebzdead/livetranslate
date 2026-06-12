# livetranslate

Captures a conference speaker's audio, transcribes it in real time with ElevenLabs Scribe v2 Realtime (or AssemblyAI Universal-3 Pro as failover/bake-off adapter), segments the transcript into finalized sentences, translates each sentence into multiple target languages via an LLM API with a glossary-enforced terminology, and serves per-language reading displays to browsers on the local network. Attendees point their phones at a URL on the venue Wi-Fi and read the talk live in their own language.

**Documentation**

| Document | Contents |
|---|---|
| This README | Install, configuration, operator control panel, live runs, test harness |
| [`live-translation-pipeline-spec.md`](live-translation-pipeline-spec.md) | Full design specification — architecture, invariants, failure handling |
| [`docs/vendor-notes.md`](docs/vendor-notes.md) | ElevenLabs / AssemblyAI API verification notes and open uncertainties |
| [`docs/superpowers/plans/`](docs/superpowers/plans/) | Implementation plans (pipeline, operator control panel) |

---

## Install

**Requirements:** Python ≥ 3.11, ffmpeg (harness only).

```sh
python3 -m venv .venv
.venv/bin/pip install -e '.[harness,dev]'

# harness only — needed for FileSource (ffmpeg) and bakeoff (jiwer):
brew install ffmpeg
```

---

## Configuration

Edit `config.toml` before each event. All secrets go in environment variables only — never in config files or logs.

### Sections

| Section | Key settings |
|---|---|
| `[session]` | `source_language` — pin to `"en"` or `"de"` per event; `output_dir` — session JSONL root |
| `[audio]` | `device_substring` — substring of the audio interface name (app refuses to start if not matched); `chunk_ms` — feed size (default 100) |
| `[asr]` | `adapter` — `"elevenlabs"` or `"assemblyai"`; `failover` — optional second adapter; `overlap_ms` — replay overlap on reconnect |
| `[asr.elevenlabs]` | `keyterms_max = 50` — ElevenLabs realtime cap (surcharged at $0.05/hr extra; count logged at startup) |
| `[asr.assemblyai]` | `use_domain_prompt` — pass `domain_blurb.txt` as a free-form transcription hint (u3-rt-pro only) |
| `[segmenter]` | `max_words = 45`, `max_pending_s = 12` — sentence finalization thresholds |
| `[translate]` | `targets` — list of BCP-47 codes (default `["es","fr","de","pt"]`; add `"ar"`, `"zh"` to enable); `provider`, `base_url`, `model` — **must be set before translation works** (see below); `timeout_s`, `batch_threshold`, `batch_max` |
| `[glossary]` | `path = "glossary.tsv"`, `domain_blurb = "domain_blurb.txt"` |
| `[display]` | `host = "0.0.0.0"`, `port = 8080`, `font_scale = 1.6` |
| `[health]` | `stall_s = 10` — seconds before the watchdog flags a stall |
| `[harness]` | `rtf = 1.0` — playback rate for FileSource (do not raise above 1.0 without vendor confirmation) |

### Setting the translation provider

`translate.provider` selects the request/response mapping. Set exactly one before running:

```toml
[translate]
provider  = "anthropic"                        # or "openai_chat"
base_url  = "https://api.anthropic.com"        # or "https://api.openai.com"
model     = "claude-haiku-4-5"                 # fast tier; haiku-4-5 recommended
```

### Secrets — environment variables only

| Variable | Used by |
|---|---|
| `ELEVENLABS_API_KEY` | ElevenLabs adapter (live run + harness) |
| `ASSEMBLYAI_API_KEY` | AssemblyAI adapter (harness bakeoff/chaos) |
| `TRANSLATE_API_KEY` | LLM translator (all modes with translation enabled) |

### Glossary format

`glossary.tsv` — tab-separated, one term per row:

```
term_src  es  fr  de  pt  ar  zh  priority  notes
rate of profit  tasa de ganancia  taux de profit  Profitrate  ...  1
```

Columns `ar` and `zh` may be empty. `priority 1` terms are loaded first into the keyterm list; terms beyond `keyterms_max = 50` are logged and dropped from ASR boosting (still enforced in translation via glossary block).

---

## Operator control panel (recommended)

A local web app for the technical operator — no terminal needed.

**Start it:** double-click `Start LiveTranslate.command` (Mac) or
`Start LiveTranslate.bat` (Windows). First run creates the Python
environment automatically (needs Python ≥ 3.11 installed). Your browser
opens `http://127.0.0.1:8766/`.

From the panel you can:

- **Edit configuration** — common fields (audio device match, source
  language, adapter, target languages) or the raw `config.toml`
  (validated before save; comments preserved).
- **Edit the glossary** — validated with the real loader; shows term and
  keyterm counts against the ElevenLabs cap of 50.
- **Manage API keys** — writes `.env`; existing keys shown masked
  (last 4 chars), never echoed in full.
- **Check audio** — list input devices (the one matching
  `audio.device_substring` is flagged) and run a live RMS/peak level
  meter before going live. The meter is released automatically when the
  pipeline starts.
- **Launch / stop the server** — runs `python -m livetranslate` as a
  child process; SIGINT/CTRL_BREAK drain so sessions close cleanly;
  live log tail in the panel.
- **Share audience links** — operator console and per-language URLs
  built on the machine's Wi-Fi/LAN IP, with copy buttons; the operator
  console is embedded in the panel while running.

The control panel binds to **localhost only** (it can read and write API
keys). The display server it launches binds `0.0.0.0:<display.port>` as
before, so the audience links work for anyone on the venue network.

---

## Live run

```sh
ELEVENLABS_API_KEY=<key> TRANSLATE_API_KEY=<key> \
  .venv/bin/python -m livetranslate --config config.toml
```

Optional flags:

| Flag | Effect |
|---|---|
| `--resume sessions/<dir>` | Resume a crashed session; rebuilds displays byte-identical |
| `--log-level DEBUG` | Verbose logging (default: INFO) |

### Display URLs (served at `[display] host:port`)

| URL | Audience |
|---|---|
| `http://<host>:8080/` | Operator console — source text, health, per-language lag |
| `http://<host>:8080/v/es` | Spanish reading display |
| `http://<host>:8080/v/fr` | French reading display |
| `http://<host>:8080/v/de` | German reading display |
| `http://<host>:8080/v/pt` | Portuguese reading display |

Add `/v/<lang>` for any language in `translate.targets`. Arabic (`ar`) renders RTL automatically.

---

## Harness

All harness commands require API keys for the adapter being tested. `--no-display` suppresses the HTTP server (recommended in harness runs).

### run_file — end-to-end file test

```sh
ELEVENLABS_API_KEY=<key> TRANSLATE_API_KEY=<key> \
  .venv/bin/python -m harness.run_file \
    --config config.toml \
    --audio recordings/talk1.mp3 \
    --ref refs/talk1.txt \
    --adapter elevenlabs \
    --langs es,fr,de,pt \
    --rtf 1.0 \
    --loop 1 \
    --no-display
```

| Flag | Notes |
|---|---|
| `--audio` | One or more audio files (space-separated); any container ffmpeg can decode |
| `--ref` | Reference transcript for WER/jargon-recall metrics (optional; omit to skip metrics) |
| `--adapter` | `elevenlabs` or `assemblyai` (overrides `config.toml`); defaults to config value |
| `--langs` | Comma-separated target languages (overrides `translate.targets`) |
| `--rtf` | Playback rate (overrides `harness.rtf`; keep at 1.0) |
| `--loop N` | Repeat the audio N times (soak: use `--loop 4` on a 30-min file to reach ~2 h) |
| `--no-display` | Skip the HTTP display server |

Outputs written to `sessions/<timestamp>/`:
- `sentences.jsonl`, `translations.jsonl`, `events.jsonl` — append-only session state
- `report.json` — WER, jargon recall, latency percentiles (p50/p95)
- `report.md` — human-readable source/target table for translation adequacy review

### chaos — reconnect resilience test

Requires `ELEVENLABS_API_KEY` (uses the adapter from config).

```sh
ELEVENLABS_API_KEY=<key> TRANSLATE_API_KEY=<key> \
  .venv/bin/python -m harness.chaos \
    --config config.toml \
    --audio recordings/talk1.mp3 \
    --cuts-ms 30000,95000,150500
```

`--cuts-ms` is a comma-separated list of stream offsets (ms) at which the WebSocket is forcibly closed. The script asserts: reconnect count ≥ number of cuts, zero duplicate finalized sentences.

### bakeoff — compare both adapters

Requires both `ELEVENLABS_API_KEY` and `ASSEMBLYAI_API_KEY`.

```sh
ELEVENLABS_API_KEY=<key> ASSEMBLYAI_API_KEY=<key> TRANSLATE_API_KEY=<key> \
  .venv/bin/python -m harness.bakeoff \
    --config config.toml \
    --audio recordings/talk1.mp3 \
    --ref refs/talk1.txt \
    --langs es,fr,de,pt
```

Runs `elevenlabs` then `assemblyai` sequentially on the same input and emits:
- `bakeoff.csv` — WER, jargon recall, latency p50/p95, reconnects per adapter
- Markdown table printed to stdout

**Decision rule** (spec §8): prefer ElevenLabs unless AssemblyAI wins jargon recall by ≥ 3 points or WER by ≥ 1.5 points absolute, or recordings show heavy DE/EN code-switching that ElevenLabs visibly fumbles.

### soak — 2-hour stability test

```sh
ELEVENLABS_API_KEY=<key> TRANSLATE_API_KEY=<key> \
  .venv/bin/python -m harness.run_file \
    --config config.toml \
    --audio recordings/talk1.mp3 recordings/talk2.mp3 \
    --loop 4 \
    --no-display
```

Pass criteria: zero unrecovered disconnects, zero dead threads, RSS growth < 150 MB between minute 10 and end, invariants hold.

---

## Tests

No API keys needed. All 145 tests run offline using fixtures.

```sh
.venv/bin/python -m pytest tests/ -q
```

---

## Pre-event checklist (spec §10 M7)

Run through this the day before the event and again in the room before doors open.

- [ ] **Audio device:** `audio.device_substring` in `config.toml` matches the mixing-desk interface name. Start the app; it logs the matched device and refuses to start if not found.
- [ ] **Glossary loaded:** startup log shows term count (e.g. `glossary loaded: 42 terms`). Confirm the count matches expectation.
- [ ] **Keyterm count:** startup log shows keyterm count injected into the ASR adapter. Must be ≤ 50 for ElevenLabs realtime (terms beyond the cap are dropped from ASR boosting and logged as a warning). Surcharged at $0.05/hr extra — confirm billing is expected.
- [ ] **End-to-end language test:** run a short recording through `run_file` for all enabled languages and review `report.md` for translation adequacy and glossary term rendering.

  ```sh
  ELEVENLABS_API_KEY=<key> TRANSLATE_API_KEY=<key> \
    .venv/bin/python -m harness.run_file \
      --config config.toml \
      --audio recordings/sample.mp3 \
      --langs es,fr,de,pt \
      --no-display
  ```

- [ ] **Display URLs:** open every display URL in a browser and confirm the SSE stream is live:
  - `http://<host>:8080/` (operator console)
  - `http://<host>:8080/v/es`, `/v/fr`, `/v/de`, `/v/pt` (one per enabled language)

---

## Manual acceptance criteria (spec §9)

| # | Criterion | Automated? | Notes |
|---|---|---|---|
| 1 | Jargon recall ≥ 90% (keyterms enabled; show uplift vs keyterms-off) | Partial — `report.json` computes it | Needs owner recordings + `ELEVENLABS_API_KEY` |
| 2 | Latency p50 ≤ 4 s, p95 ≤ 7 s at rtf=1.0 | Partial — `report.json` `audio_end_to_translation` field (requires a harness run; field is absent for live sessions) | Needs real recordings + live keys |
| 3 | Chaos test passes at 3 cut offsets including one mid-sentence | Yes — `harness/chaos.py` asserts this | Needs `ELEVENLABS_API_KEY` |
| 4 | 2-hour soak passes (zero unrecovered disconnects, RSS growth < 150 MB) | Partial — invariant checks run; RSS monitored manually | Needs 2 h of recordings + live keys |
| 5 | `--resume` after `kill -9` restores displays byte-identical | Covered by unit tests for Store + resume path | Full live validation needs a live session |
| 6 | Operator console reflects forced disconnect within 5 s and shows recovery | Covered by watchdog unit tests + status SSE streaming (error-level StatusEvents now stream level/message to the banner in real time) | Live validation: pull the network cable and observe `/` |
| 7 | Bakeoff report generated for both adapters on same inputs | Yes — `harness/bakeoff.py` + `bakeoff.csv` | Needs both `ELEVENLABS_API_KEY` and `ASSEMBLYAI_API_KEY` |
| 8 | Glossary renderings present in ≥ 95% of translated sentences containing a glossary term | Yes — `report.json` `glossary_rendering_rate` (aggregate across all languages) | Computed by metrics; review `report.md` |

**Vendor items that must be validated against live keys before each event** (see `docs/vendor-notes.md` for full detail):

- ElevenLabs timestamp units: docs are marked ⚠️ UNCERTAIN — the adapter converts float seconds × 1000; confirm against a live response that word `.start`/`.end` values are indeed seconds (not ms).
- ElevenLabs max session duration: no official figure documented; proactive rotation is implemented via `asr.max_session_s` (0 = off by default; set to 5400 for 90-min rotation); the actual idle timeout must be tested empirically. AssemblyAI sessions hard-cap at 3 h — the reactive reconnect covers that, and `max_session_s = 10800` (or lower) can be set for a proactive guard.
- AssemblyAI schema: marked schema-derived; validate `Turn` message fields against a live `ASSEMBLYAI_API_KEY` before using in production.

---

## Architecture

Audio → MicSource/FileSource → RingBuffer → ResilientASR (ElevenLabs or AssemblyAI) → Segmenter → per-language TranslationWorker threads → append-only JSONL Store → ThreadingHTTPServer SSE display. See `live-translation-pipeline-spec.md` §3 for the full thread/queue inventory and `docs/superpowers/plans/2026-06-09-livetranslate-pipeline.md` for the implementation plan.

---

## License

[MIT](LICENSE)
