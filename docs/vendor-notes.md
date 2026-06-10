# Vendor Notes — §13 Verification
**Date verified:** 2026-06-10  
**Verified by:** Task 8 research agent  
**Purpose:** Gates ElevenLabs adapter implementation (Task 9) and LLM translator (Task 13).

---

### Fixture field mapping

This mapping is what the Task 9 implementer codes against. Every field name below matches exactly the fields in `tests/fixtures/elevenlabs_messages.json`.

| Purpose | Field | Notes |
|---|---|---|
| Transcript text | `text` | Present in `partial_transcript` and `committed_transcript` / `committed_transcript_with_timestamps` messages |
| Segment start time | `words[0].start` | Float, **seconds** from stream start. API reports seconds. Convert to ms by `* 1000` before storing in `TranscriptEvent.t_start_ms`. |
| Segment end time | `words[-1].end` | Float, **seconds** from stream start. Convert to ms by `* 1000` before storing in `TranscriptEvent.t_end_ms`. |
| Partial vs final vs control | `message_type` | `"partial_transcript"` → partial; `"committed_transcript"` or `"committed_transcript_with_timestamps"` → final; `"session_started"` → control/ack → adapter returns `None` |
| Is final? | `message_type == "committed_transcript"` or `message_type == "committed_transcript_with_timestamps"` | Both are final; the `_with_timestamps` variant carries per-word timing |

**Timestamp conversion note:** ElevenLabs reports all word `start`/`end` values in **seconds** (float, e.g. `1.24`). The adapter must multiply by 1000 and round to int to produce `t_start_ms` / `t_end_ms` for `TranscriptEvent`. The fixtures in `tests/fixtures/elevenlabs_messages.json` store timestamps **in seconds** matching the raw API wire format — the unit conversion happens inside the adapter, not the fixture.

⚠️ schema-derived, not captured live — structure is consistent with official docs + SDK source + GitHub issue examples, but must be validated against a live key before Task 9 ships.

---

## ElevenLabs Scribe Realtime

**Sources consulted (2026-06-10):**
- <https://elevenlabs.io/docs/api-reference/speech-to-text/v-1-speech-to-text-realtime>
- <https://elevenlabs.io/docs/eleven-api/guides/how-to/speech-to-text/realtime/transcripts-and-commit-strategies>
- <https://elevenlabs.io/docs/eleven-api/guides/how-to/speech-to-text/realtime/client-side-streaming>
- <https://elevenlabs.io/docs/overview/capabilities/speech-to-text>
- <https://elevenlabs.io/docs/changelog/2026/4/27>
- <https://deepwiki.com/elevenlabs/elevenlabs-python/5.2-real-time-speech-to-text>
- <https://github.com/elevenlabs/elevenlabs-python/issues/607>
- <https://docs.pipecat.ai/server/services/stt/elevenlabs>

### WebSocket endpoint & authentication

**Endpoint:**
```
wss://api.elevenlabs.io/v1/speech-to-text/realtime
```

Regional variants are available (US, EU, India, Singapore) but the global URL above is the default.

**Authentication — two mutually exclusive methods:**

| Method | How |
|---|---|
| Server-side (recommended for this project) | HTTP header `xi-api-key: <ELEVENLABS_API_KEY>` sent at WebSocket upgrade |
| Client-side (browser / untrusted environment) | Query param `token=<single-use-token>` — tokens have a 15-minute expiry and are obtained server-side via the single-use token endpoint. **Not used by this project.** |

### Connection query parameters (all appended to the WebSocket URL)

| Parameter | Type | Default | Notes |
|---|---|---|---|
| `model_id` | string | — | Use `"scribe_v2_realtime"` |
| `audio_format` | enum | `pcm_16000` | Supported values: `pcm_8000`, `pcm_16000`, `pcm_22050`, `pcm_24000`, `pcm_44100`, `pcm_48000`, `ulaw_8000` |
| `language_code` | string | — | ISO 639-1 (e.g. `"en"`, `"de"`). Set to pin language and suppress mid-session auto-detection. |
| `keyterms` | array of strings | — | Max **50 entries × 20 chars each** (realtime limit; batch allows 1000 × 50). Passed as repeated `keyterms=term` params. |
| `commit_strategy` | enum | `manual` | `"manual"` or `"vad"` (Voice Activity Detection) |
| `include_timestamps` | boolean | `false` | Enables word-level timestamps in `committed_transcript_with_timestamps` messages |
| `include_language_detection` | boolean | `false` | Adds detected `language_code` to committed transcripts |
| `no_verbatim` | boolean | `false` | Removes filler words / false starts. Added in SDK v2.45.0 / April 2026. |
| `vad_silence_threshold_secs` | float | `1.5` | Only relevant if `commit_strategy=vad` |
| `vad_threshold` | float | `0.4` | Only relevant if `commit_strategy=vad` |
| `min_speech_duration_ms` | integer | `100` | |
| `min_silence_duration_ms` | integer | `100` | |
| `enable_logging` | boolean | `true` | Set `false` for zero-retention mode |

### Message schemas

#### (a) Session started — server → client (control/ack)

This is the first message received after a successful connection. The adapter must classify this as NOT a transcript (return `None`).

```json
{
  "message_type": "session_started",
  "session_id": "0b0a72b57fd743ebbed6555d44836cf2",
  "config": {
    "model_id": "scribe_v2_realtime",
    "audio_format": "pcm_16000",
    "sample_rate": 16000,
    "language_code": "en",
    "commit_strategy": "manual",
    "include_timestamps": true,
    "keyterms": ["ElevenLabs", "Scribe"]
  }
}
```

The `config` object echoes back the connection parameters (including `keyterms` and `no_verbatim`), confirmed by the April 2026 changelog.

#### (b) Audio input message — client → server

Audio is sent as **base64-encoded JSON frames**, not raw binary WebSocket frames.

```json
{
  "audio_base_64": "<base64-encoded PCM bytes>",
  "sample_rate": 16000,
  "commit": false
}
```

| Field | Notes |
|---|---|
| `audio_base_64` | Base64 string of raw PCM audio bytes |
| `sample_rate` | Integer Hz; must match the `audio_format` used at connection time |
| `commit` | Set to `true` to explicitly finalize the current segment (manual commit strategy) |
| `previous_text` | Optional string; context hint for the first chunk only |

#### (c) Partial/interim transcript — server → client

```json
{
  "message_type": "partial_transcript",
  "text": "The first move is what sets everything in"
}
```

The `text` field may change with each subsequent `partial_transcript` message for the same segment. No timestamps are included in partial messages.

#### (d) Final/committed transcript — server → client

Two variants are emitted depending on whether `include_timestamps=true` was set:

**Without timestamps:**
```json
{
  "message_type": "committed_transcript",
  "text": "The first move is what sets everything in motion."
}
```

**With word-level timestamps (`include_timestamps=true` — recommended for this project):**
```json
{
  "message_type": "committed_transcript_with_timestamps",
  "text": "The first move is what sets everything in motion.",
  "language_code": "en",
  "words": [
    {
      "text": "The",
      "start": 1.24,
      "end": 1.38,
      "type": "word",
      "speaker_id": "speaker_0",
      "logprob": -0.012
    },
    {
      "text": " ",
      "start": 1.38,
      "end": 1.38,
      "type": "spacing",
      "speaker_id": "speaker_0",
      "logprob": -0.001
    },
    {
      "text": "first",
      "start": 1.38,
      "end": 1.62,
      "type": "word",
      "speaker_id": "speaker_0",
      "logprob": -0.008
    }
  ]
}
```

**Word object fields:**

| Field | Type | Notes |
|---|---|---|
| `text` | string | The word or spacing character |
| `start` | float | Start time in **seconds** from stream start (e.g. `1.24`) |
| `end` | float | End time in **seconds** from stream start |
| `type` | string | `"word"` or `"spacing"` |
| `speaker_id` | string | e.g. `"speaker_0"` (no diarization in v1, always `speaker_0`) |
| `logprob` | float | Log-probability confidence score |
| `characters` | array | Optional per-character array (present when character-level detail requested) |

⚠️ **UNCERTAIN — timestamp units:** Multiple sources (GitHub issue #607, word values like `452.3` and `0.15`) strongly indicate **seconds**. However, the official API reference page does not explicitly state "seconds" vs "milliseconds". Implementer must confirm by inspecting a live response against known audio before relying on the ms-conversion arithmetic.

### Supported input encodings & sample rates

PCM (raw signed 16-bit little-endian) is fully supported. Supported sample rates: **8 000, 16 000, 22 050, 24 000, 44 100, 48 000 Hz**.

**PCM 16-bit @ 16 kHz mono is supported** and is the default (`audio_format=pcm_16000`). This is the recommended format for this project and matches sounddevice's output at `dtype='int16'`.

µ-law (`ulaw_8000`) is also supported for telephony compatibility.

### Keyterm / keyword boosting

| Item | Value |
|---|---|
| Parameter name | `keyterms` (query param, repeated; also in Python SDK as `keyterms: list[str]`) |
| Realtime cap | **50 keyterms, max 20 characters each** |
| Batch cap (for reference) | 1 000 keyterms, max 50 characters each |
| Billing | Yes — keyterm prompting is billed at an additional **$0.050/hour** on top of the Scribe Realtime base rate ($0.39/hour). Total with keyterms: ~$0.44/hour. |
| Confirmed available since | April 27, 2026 changelog (SDK v2.45.0) |

### Language pinning

Pass `language_code=<ISO 639-1 code>` (e.g. `language_code=en` or `language_code=de`) as a query parameter at connection time. This pins the model to that language and prevents mid-session auto-detection switching. Supported: 90+ languages per the product page.

⚠️ **UNCERTAIN — mid-session language lock:** The product page states "handles mid-conversation language switches," implying auto-detect is on by default. Whether `language_code` fully disables switching or merely biases toward the specified language has not been explicitly confirmed in docs. Implementer should test with a bilingual audio clip.

### Keepalive requirements

The official ElevenLabs documentation does not specify a keepalive/ping-pong protocol for the STT realtime WebSocket. However:

- The **TTS WebSocket** (different product) closes after ~20 seconds of inactivity per the help center.
- Third-party integrations (Pipecat) send silent audio chunks every **5 seconds** as keepalive, with a 10-second timeout threshold.
- Community observation: connections held idle >60 seconds may be closed server-side without notification.

**Recommendation for this project:** Send a minimal silent PCM chunk (e.g. 160 bytes of zero-valued samples = 10 ms at 16 kHz) every **15 seconds** during natural pauses to prevent idle disconnection.

⚠️ **UNCERTAIN — exact idle timeout value for STT realtime:** No official figure found. Treat as unknown; test empirically before M1 DoD sign-off.

### Max session duration / idle timeout

⚠️ **UNCERTAIN:** No maximum WebSocket session duration is documented for the Scribe Realtime endpoint. The batch STT endpoint supports "up to 10 hours," but this does not directly apply to WebSocket sessions.

**Recommendation:** Proactively rotate the WebSocket connection every **90 minutes** (well under any plausible limit) to keep sessions clean across a 2-hour conference. The `ResilientASR` wrapper (Task 18) must handle this rotation transparently using the `RingBuffer.replay_from(ms)` mechanism.

### Faster-than-realtime audio input

⚠️ **UNCERTAIN — not documented.** The API is designed for real-time input; deliberately pacing at RTF=1.0 (as `FileSource` already does) is the safe approach. Whether the server tolerates bursts faster than real-time without quality degradation has not been confirmed.

### Commit / finalization semantics

Two commit strategies are supported:

| Strategy | Behavior |
|---|---|
| `manual` (default) | Client sends audio chunks and sets `"commit": true` on a chunk (or sends a standalone commit message) to explicitly finalize the current segment. **Auto-commit fires every 90 seconds** if no manual commit has been sent, per the transcripts guide ("committing every 20-30 seconds is good practice"). |
| `vad` | Server automatically commits when silence exceeds `vad_silence_threshold_secs` (default 1.5 s). |

Upon commit, the server emits a `committed_transcript` (or `committed_transcript_with_timestamps`) message followed by the next `partial_transcript` sequence starting fresh. The committed transcript text is **final and will not change**. This maps 1-to-1 to `is_final=True` in `TranscriptEvent`.

**Recommendation for this project:** Use `commit_strategy=vad` with `include_timestamps=true`. The segmenter (Task 10) adds sentence-level finalization on top of VAD commits. Manual commit can be used as a fallback flush at session teardown.

---

## AssemblyAI Streaming (Universal)

**Sources consulted (2026-06-10):**
- <https://www.assemblyai.com/docs/guides/streaming>
- <https://www.assemblyai.com/docs/speech-to-text/universal-streaming>
- <https://assemblyai.com/docs/api-reference/streaming-api/streaming-api>
- <https://www.assemblyai.com/docs/api-reference/streaming-api/generate-streaming-token>
- <https://www.assemblyai.com/docs/streaming/prompting>
- <https://www.assemblyai.com/docs/streaming/migration-guides/universal-to-u3-pro-streaming.md>
- <https://www.assemblyai.com/blog/introducing-universal-streaming>
- <https://www.assemblyai.com/blog/introducing-multilingual-universal-streaming>
- <https://www.assemblyai.com/blog/streaming-keyterms-prompting>
- <https://www.assemblyai.com/blog/universal-3-pro-streaming>
- <https://www.assemblyai.com/blog/assemblyai-october-2025-releases>
- <https://www.assemblyai.com/blog/multilingual-speech-to-text-api-universal-3-pro>
- <https://www.assemblyai.com/blog/raw-websocket-voice-agent-with-assemblyai-universal-3-pro-streaming>
- <https://www.assemblyai.com/docs/faq/language-support-for-real-time-transcription>

### Current product naming

The spec calls this "Universal-3 Pro Streaming." The current (2026-06-10) AssemblyAI product line for streaming STT is:

| `speech_model` value | Product name | Notes |
|---|---|---|
| `universal-streaming-english` | Universal Streaming (English) | English-only; lowest cost ($0.15/hr) |
| `universal-streaming-multilingual` | Universal Streaming (Multilingual) | 6 languages; per-turn language detection |
| `u3-rt-pro` | Universal-3 Pro Streaming | 99+ languages; native code-switching; keyterms + prompt; $0.45/hr |

**The spec's "Universal-3 Pro Streaming" maps to `speech_model=u3-rt-pro`.** This is the correct model for the bake-off adapter because it supports German, keyterm boosting, and a free-form domain `prompt`.

### WebSocket endpoint & authentication

**Endpoint (all models, all regions):**
```
wss://streaming.assemblyai.com/v3/ws
```

EU regional variant: `wss://streaming.eu.assemblyai.com/v3/ws`

**Authentication — two methods:**

| Method | How | Recommended for |
|---|---|---|
| API key in `Authorization` header | `Authorization: <ASSEMBLYAI_API_KEY>` (no `Bearer` prefix) | Server-side (this project) |
| Temporary token in query param | `?token=<token>` | Browser / untrusted clients |

**Temporary-token flow (for reference):**
1. Server-side: `GET https://streaming.assemblyai.com/v3/token` with `Authorization: <API_KEY>` header and `?expires_in_seconds=<1–600>` (and optionally `&max_session_duration_seconds=<60–10800>`).
2. Response JSON contains `{ "token": "...", "expires_in_seconds": ... }`.
3. Client uses `?token=<token>` in the WebSocket URL. Token is single-use; must be redeemed within the window.

**Not used by this project** (server-side only; use the `Authorization` header).

### Connection query parameters

All parameters are appended to the WebSocket upgrade URL. Authentication uses an extra HTTP header (see above) rather than a query param for server-side use.

| Parameter | Type | Default | Notes |
|---|---|---|---|
| `speech_model` | string | — | **Required.** Use `"u3-rt-pro"` for this project |
| `sample_rate` | integer | 16000 | Must match audio source |
| `encoding` | string | `pcm_s16le` | `"pcm_s16le"` (raw signed 16-bit LE PCM) or `"pcm_mulaw"` |
| `keyterms_prompt` | string (repeated) | — | Repeated query param, one entry per term. Max **100 terms × 50 characters** each. On U3 Pro, can be used together with `prompt`. |
| `prompt` | string | — | Free-form transcription instruction string. U3 Pro only. Max length not explicitly documented. See §prompt section. |
| `inactivity_timeout` | integer | — | Seconds (5–3600). Server closes connection after this many seconds of silence. Not set = no idle timeout (session held open until 3-hour hard limit or client close). |
| `min_turn_silence` | integer | 100 | Milliseconds. Minimum silence for end-of-turn (U3 Pro default: 100 ms; Universal default: 400 ms) |
| `max_turn_silence` | integer | 1000 | Milliseconds (U3 Pro default: 1000 ms; Universal default: 1280 ms) |
| `vad_threshold` | float | 0.3 | Voice activity detection threshold (U3 Pro default: 0.3; Universal: 0.4) |
| `language_detection` | boolean | false | Multilingual models only. Adds detected `language_code` to Turn messages |
| `speaker_labels` | boolean | false | Enable diarization |
| `max_speakers` | integer | — | 1–10; requires `speaker_labels=true` |
| `domain` | enum | — | Domain-specific vocabulary LM. Currently `"medical-v1"` (supports EN, ES, DE, FR). |
| `format_turns` | boolean | — | **Removed in U3 Pro** (always on). Present in Universal Streaming only. |
| `language` | string | — | **Deprecated.** Replaced by `speech_model` selection. |

### Audio framing — client → server

Audio is sent as **raw binary WebSocket frames** (not base64 JSON). There is no JSON envelope.

| Property | Value |
|---|---|
| Encoding | `pcm_s16le` (mono signed 16-bit little-endian PCM) |
| Sample rate | 16000 Hz (must match `sample_rate` query param) |
| Channels | Mono |
| Recommended frame size | ~50 ms of audio per frame (800 samples at 16 kHz = 1 600 bytes) |
| Frame type | WebSocket binary opcode (`OPCODE_BINARY`) |

**PCM16 @ 16 kHz mono is confirmed supported** and matches `sounddevice` output at `dtype='int16'` / `samplerate=16000`. This is the format used by this project.

### Message schemas

#### (a) Session begin — server → client (control/ack)

First message after successful connection. The adapter must classify this as NOT a transcript (return `None`).

```json
{
  "type": "Begin",
  "id": "3e4f2a1b-8c7d-4e9f-a0b1-2c3d4e5f6a7b",
  "expires_at": 1749600000
}
```

| Field | Type | Notes |
|---|---|---|
| `type` | string | Always `"Begin"` |
| `id` | string | UUID for this session |
| `expires_at` | integer | Unix timestamp when the session will be force-closed (≤ start + 3 h) |

#### (b) Partial/interim transcript — server → client

Emitted continuously while the user is speaking. `end_of_turn` is `false`.

```json
{
  "type": "Turn",
  "turn_order": 0,
  "turn_is_formatted": true,
  "end_of_turn": false,
  "transcript": "The first move is what sets everything in",
  "end_of_turn_confidence": 0.12,
  "words": [
    {"text": "The",  "start": 1240, "end": 1380, "confidence": 0.99, "word_is_final": true},
    {"text": "in",   "start": 2980, "end": 3100, "confidence": 0.99, "word_is_final": false}
  ]
}
```

#### (c) Final/end-of-turn transcript — server → client

Emitted when U3 Pro's punctuation-based endpointing decides the speaker has finished. `end_of_turn` is `true`. Text will not change after this.

```json
{
  "type": "Turn",
  "turn_order": 0,
  "turn_is_formatted": true,
  "end_of_turn": true,
  "transcript": "The first move is what sets everything in motion.",
  "end_of_turn_confidence": 0.94,
  "words": [
    {"text": "The",     "start": 1240, "end": 1380, "confidence": 0.99, "word_is_final": true},
    {"text": "motion.", "start": 3100, "end": 3550, "confidence": 0.97, "word_is_final": true}
  ]
}
```

#### Full Turn message field reference

| Field | Type | Notes |
|---|---|---|
| `type` | string | Always `"Turn"` for transcript messages |
| `turn_order` | integer | Monotonically increasing turn counter (resets to 0 per session) |
| `turn_is_formatted` | boolean | `true` when punctuation/casing is applied (always `true` for U3 Pro) |
| `end_of_turn` | boolean | **`false` = partial; `true` = final/committed.** This is the primary discriminator. |
| `transcript` | string | The full accumulated text of this turn so far |
| `utterance` | string | Optional: raw unformatted text of the utterance |
| `language_code` | string | ISO 639-1 code; present when `language_detection=true` |
| `language_confidence` | float | Confidence for `language_code`; present with `language_detection=true` |
| `speaker_label` | string | Speaker label; present when `speaker_labels=true` |
| `end_of_turn_confidence` | float | Model's confidence that the turn is complete (0.0–1.0) |
| `words[]` | array | Per-word timing array |
| `words[].text` | string | The word text |
| `words[].start` | integer | Word start time in **milliseconds** from stream start |
| `words[].end` | integer | Word end time in **milliseconds** from stream start |
| `words[].confidence` | float | Per-word ASR confidence |
| `words[].word_is_final` | boolean | `true` = this word will not change in subsequent Turn messages |
| `words[].speaker` | string | Per-word speaker label; present when `speaker_labels=true` |

#### (d) Session termination — server → client

```json
{
  "type": "Termination",
  "audio_duration_seconds": 65.4,
  "session_duration_seconds": 68.1
}
```

The adapter should treat this as a disconnect signal; it is not a transcript.

#### (e) Force end-of-turn — client → server

To programmatically trigger end-of-turn detection (e.g. at session teardown), send a JSON text frame:

```json
{"type": "ForceEndpoint"}
```

### Partial vs. final discrimination

**The sole discriminator is `end_of_turn` (boolean) in the Turn message.**

- `"type": "Begin"` → control / session-ack → adapter returns `None`
- `"type": "Turn"` + `end_of_turn: false` → partial → adapter emits `TranscriptEvent(kind="partial", ...)`
- `"type": "Turn"` + `end_of_turn: true` → final → adapter emits `TranscriptEvent(kind="final", ...)`
- `"type": "Termination"` → teardown signal → adapter returns `None`

Unlike ElevenLabs' `message_type` string, AssemblyAI uses a **single message type (`"Turn"`) for both partials and finals**, with `end_of_turn` as the boolean flag.

### Timestamp units

**Milliseconds (integer).** All `start` and `end` values in the `words[]` array are integer milliseconds from stream start (e.g. `1240`, `3550`). No conversion needed to produce `t_audio_start_ms` / `t_audio_end_ms` for `TranscriptEvent`.

**Contrast with ElevenLabs:** ElevenLabs uses float seconds (e.g. `1.24`), requiring `× 1000`. AssemblyAI uses integer milliseconds — pass through directly.

The segment timestamps are derived as:
- `t_audio_start_ms` = `words[0].start` (first word in the Turn)
- `t_audio_end_ms` = `words[-1].end` (last word in the Turn)

If `words` is empty, fall back to `0` / `0` and log a warning.

### Keyterm prompting

| Item | Value |
|---|---|
| Parameter name | `keyterms_prompt` (query param, **repeated** one entry per term: `&keyterms_prompt=Profitrate&keyterms_prompt=Tübingen`) |
| Max count | **100 terms** per session (requests > 100 terms → server error) |
| Max length per term | **50 characters** each (terms > 50 chars are silently ignored) |
| Pricing | +$0.04/hr on top of base rate (Universal Streaming base: $0.15/hr → $0.19/hr with keyterms) |
| Model support | `universal-streaming-english`, `universal-streaming-multilingual`, `u3-rt-pro` all support `keyterms_prompt` |
| Mid-stream updates | On U3 Pro, `keyterms_prompt` can be updated mid-stream (send a JSON text frame) |
| Multi-word phrases | Supported (e.g. `"rate of profit"`); each counts as one keyterm toward the 100-term cap |

**For this project:** Pass glossary `term_src` values sorted by `priority` then length, truncated at 100 terms (warn if truncated). Multi-word terms within the 50-char limit are fully supported.

### Free-form domain prompt

The `prompt` query parameter is supported on `u3-rt-pro` **only**.

| Item | Value |
|---|---|
| Parameter name | `prompt` (query param, URL-encoded string) |
| Character limit | ⚠️ Not explicitly stated in docs — estimated up to several hundred characters based on examples |
| Content | Free-form transcription instructions (e.g. "Transcribe academic lecture. Maintain formal register. Use Oxford English.") |
| Domain blurb | Can contain a 2–4 sentence domain description matching `domain_blurb.txt` |
| Model support | `u3-rt-pro` **only** (not available on Universal Streaming models) |

**Prompt + keyterms mutual exclusivity (spec flag):**

The spec flagged this as a potential concern. **The finding is:**

- **Universal Streaming** (`universal-streaming-english` / `universal-streaming-multilingual`): `prompt` **not supported** at all; `keyterms_prompt` is the only boosting mechanism.
- **Universal-3 Pro** (`u3-rt-pro`): `prompt` **and** `keyterms_prompt` **CAN be used together** in the same session. When combined, keyterm-boosted words are automatically appended to the effective prompt. There is **no mutual exclusivity on U3 Pro**.

**Conclusion for this project:** Use `speech_model=u3-rt-pro` with both `prompt=<domain_blurb>` and `keyterms_prompt=<term>` (repeated). This is the correct and supported configuration.

### Language support

| Language | Code | Supported in streaming? | Model |
|---|---|---|---|
| English | `en` | Yes | `universal-streaming-english`, `universal-streaming-multilingual`, `u3-rt-pro` |
| German | `de` | **Yes** | `universal-streaming-multilingual`, `u3-rt-pro` |
| Spanish | `es` | Yes | `universal-streaming-multilingual`, `u3-rt-pro` |
| French | `fr` | Yes | `universal-streaming-multilingual`, `u3-rt-pro` |
| Portuguese | `pt` | Yes | `universal-streaming-multilingual`, `u3-rt-pro` |
| Italian | `it` | Yes | `universal-streaming-multilingual`, `u3-rt-pro` |
| 99+ others | — | Yes | `u3-rt-pro` only |

**German is fully supported** in both `universal-streaming-multilingual` and `u3-rt-pro`. For a session pinned to German (`source_language = "de"`), use `speech_model=u3-rt-pro`.

**Language pinning:**

The deprecated `language` query param (`"en"` / `"multi"`) has been removed in U3 Pro. Instead:

- For English-only sessions: use `speech_model=universal-streaming-english`.
- For German or other sessions: use `speech_model=u3-rt-pro`. There is **no explicit language pin parameter** in U3 Pro; the model relies on native code-switching and prompt-based hints.
- To bias the model toward a specific language, include it in the `prompt` (e.g. `"Transcribe German academic lecture."`).
- `language_detection=true` enables per-turn language code reporting but does not pin the language.

⚠️ **UNCERTAIN — strict language pinning on U3 Pro:** There is no documented parameter that prevents the model from accepting audio in other languages. If the operator needs to guarantee German-only transcription (no accidental English interjection transcription), test empirically. The `prompt` language hint is the best available mechanism.

### Session duration limits / keepalive

| Item | Value |
|---|---|
| Hard session limit | **3 hours** — server auto-closes the session and sends `Termination` |
| Billing unit | Total WebSocket connection duration (not audio duration) |
| Idle timeout | **No default idle timeout** — connections remain open until explicit close or the 3-hour limit, unless `inactivity_timeout` is set |
| `inactivity_timeout` param | Optional, 5–3600 seconds. Server sends `Termination` after this many seconds of silence. |
| Keepalive / ping-pong | Not required when `inactivity_timeout` is not set. If set, send audio or a `ForceEndpoint` frame to reset the timer. |

**Recommendation for this project:** Do **not** set `inactivity_timeout` (speakers pause naturally; no risk of unexpected close). Implement proactive reconnect at 80% of the 3-hour limit (~2 h 24 min) via `ResilientASR`'s rotation logic.

### Faster-than-realtime audio input

⚠️ **UNCERTAIN — not documented.** The API is designed for real-time input paced at RTF=1.0. The harness `FileSource` already paces at `rtf=1.0` by default; this is the safe approach. Whether the server tolerates deliberate burst input (RTF > 1.0) without quality degradation has not been confirmed in any public documentation. Keep `harness.rtf = 1.0` for the bake-off.

### Pricing summary

| Configuration | Rate |
|---|---|
| Universal Streaming (English) | $0.15/hr |
| Universal Streaming (Multilingual) | $0.15/hr |
| Universal-3 Pro Streaming | **$0.45/hr** |
| + `keyterms_prompt` add-on | +$0.04/hr |
| U3 Pro + keyterms (this project) | **~$0.49/hr** |

Rough 2-hour session cost (U3 Pro + keyterms): **~$0.98** (cf. ElevenLabs Scribe: ~$0.88/hr → $0.88 + $0.10 keyterms = ~$1.96).

### Fixture field mapping (AssemblyAI)

This mapping is what the Task 21 implementer codes the `AssemblyAIStreamingAdapter._normalize()` method against. Every field name below matches exactly the fields in `tests/fixtures/assemblyai_messages.json`.

| Purpose | Field | Notes |
|---|---|---|
| Transcript text | `transcript` | Present in all `Turn` messages (partials and finals) |
| Segment start time | `words[0].start` | Integer, **milliseconds** from stream start. Use directly as `t_audio_start_ms` — NO unit conversion needed. |
| Segment end time | `words[-1].end` | Integer, **milliseconds** from stream start. Use directly as `t_audio_end_ms`. |
| Partial vs final vs control | `type` + `end_of_turn` | `type == "Begin"` → control/ack → return `None`; `type == "Turn"` + `end_of_turn == false` → partial; `type == "Turn"` + `end_of_turn == true` → final; `type == "Termination"` → teardown → return `None` |
| Is final? | `end_of_turn == true` | Boolean field inside a `Turn` message |
| Type discriminator | `type` | String: `"Begin"`, `"Turn"`, `"Termination"` |

**Timestamp note:** AssemblyAI reports all `words[].start` / `words[].end` values in **integer milliseconds** (e.g. `1240`, `3550`). The adapter must pass these through as-is to `t_audio_start_ms` / `t_audio_end_ms` without multiplication. This is the opposite of ElevenLabs, which uses float seconds requiring `× 1000`.

**Edge cases the adapter must handle:**
- `words` array is empty → set both timestamps to `0`; log a warning.
- `type == "Termination"` → close the receiver loop gracefully (do not raise).
- `type` field is missing or unknown → log and skip.

⚠️ schema-derived, not captured live — structure is consistent with official v3 docs + migration guide + blog examples, but must be validated against a live `ASSEMBLYAI_API_KEY` before Task 21 ships.

---

## Translation LLM Providers

**Sources consulted (2026-06-10):**
- <https://platform.claude.com/docs/en/about-claude/models/overview>
- <https://platform.claude.com/docs/en/api/messages>
- <https://developers.openai.com/api/reference/chat-completions/overview>
- <https://platform.claude.com/docs/en/about-claude/pricing>

### Primary: Anthropic Messages API

**Endpoint:**
```
POST https://api.anthropic.com/v1/messages
```

**Required HTTP headers:**

| Header | Value |
|---|---|
| `x-api-key` | `$TRANSLATE_API_KEY` (maps to the Anthropic API key env var) |
| `anthropic-version` | `2023-06-01` |
| `content-type` | `application/json` |

**Request body shape:**

```json
{
  "model": "claude-haiku-4-5",
  "max_tokens": 512,
  "temperature": 0.2,
  "system": "You are a professional conference interpreter. Translate the following sentence from English to Spanish. Output only the translation.",
  "messages": [
    {
      "role": "user",
      "content": "The first move is what sets everything in motion."
    }
  ]
}
```

**Response body — text lives at `content[0].text`:**

```json
{
  "id": "msg_01XFDUDYJgAACzvnptvVoYEL",
  "type": "message",
  "role": "assistant",
  "model": "claude-haiku-4-5-20251001",
  "content": [
    {
      "type": "text",
      "text": "El primer movimiento es lo que pone todo en marcha."
    }
  ],
  "stop_reason": "end_turn",
  "usage": {
    "input_tokens": 48,
    "output_tokens": 17
  }
}
```

**Recommended fast model (as of 2026-06-10):**

| Model | API ID | Speed | Pricing |
|---|---|---|---|
| Claude Haiku 4.5 | `claude-haiku-4-5` (alias) / `claude-haiku-4-5-20251001` (pinned) | Fastest | $1.00 / $5.00 per MTok in/out |
| Claude Sonnet 4.6 | `claude-sonnet-4-6` | Fast | $3.00 / $15.00 per MTok in/out |

**Use `claude-haiku-4-5` as the default translation model** (fastest, lowest cost, sufficient for single-sentence translation). Fall back to `claude-sonnet-4-6` if translation quality is unacceptable for complex domain terms.

### Fallback: OpenAI-compatible chat completions

Any OpenAI-compatible endpoint (OpenAI API, Azure OpenAI, local vLLM, Ollama with OpenAI compat mode) works as a drop-in fallback.

**Endpoint:**
```
POST https://api.openai.com/v1/chat/completions
```
(Replace host for Azure/local deployments.)

**Authentication:**
```
Authorization: Bearer $TRANSLATE_API_KEY
Content-Type: application/json
```

**Request body shape:**

```json
{
  "model": "gpt-4o-mini",
  "temperature": 0.2,
  "max_tokens": 512,
  "messages": [
    {
      "role": "system",
      "content": "You are a professional conference interpreter. Translate the following sentence from English to Spanish. Output only the translation."
    },
    {
      "role": "user",
      "content": "The first move is what sets everything in motion."
    }
  ]
}
```

**Response — text lives at `choices[0].message.content`:**

```json
{
  "id": "chatcmpl-...",
  "object": "chat.completion",
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "El primer movimiento es lo que pone todo en marcha."
      },
      "finish_reason": "stop"
    }
  ],
  "usage": {
    "prompt_tokens": 52,
    "completion_tokens": 16
  }
}
```

### Rate limit & cost considerations

**Worst-case request burst:** 6 target languages × up to 6 sentences in a catch-up batch = 36 requests/burst. With sentence finalization rate ~1 sentence/10–15 s at normal conference pace, steady-state is ~0.4 req/s total (≈2–3 req/s during bursts).

**Anthropic rate limits (Haiku 4.5):** 2 000 RPM / 100 000 TPM on the standard tier. Well within limits for this workload.

**Rough 2-hour session cost (Haiku 4.5, 6 languages):**
- Assume ~720 sentences over 2 h (1 sentence / 10 s average)
- 6 languages → 4 320 translation calls
- ~50 input tokens + ~30 output tokens per call = 80 tokens avg
- Input: 4 320 × 50 / 1 000 000 × $1.00 = **$0.22**
- Output: 4 320 × 30 / 1 000 000 × $5.00 = **$0.65**
- **Total ≈ $0.87 / 2-hour session** (plus ~$0.88 for Scribe Realtime; ~$1.75 total API cost)

With keyterms enabled on ElevenLabs: add $0.05/h × 2 = $0.10 → **~$1.85 total**.

---

## Config mapping (for `config.toml`)

The following `config.toml` keys map to the parameters above:

```toml
[asr]
provider = "elevenlabs"
model = "scribe_v2_realtime"
audio_format = "pcm_16000"        # pcm_16000 recommended
language_code = "en"              # pin source language; set per session
commit_strategy = "vad"
include_timestamps = true
keyterms = []                     # populated per event by operator

[translate]
provider = "anthropic"
model = "claude-haiku-4-5"
max_tokens = 512
temperature = 0.2
```
