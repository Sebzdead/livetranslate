# Operator Control Panel Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wrap livetranslate in an operator-friendly local web app (Mac + Windows) that edits `config.toml`/`glossary.tsv`/API keys, shows audio input devices with a live level meter, launches/stops the pipeline, embeds the operator console, and lists the per-language audience URLs on the LAN IP.

**Architecture:** A new `livetranslate.control` package runs a small stdlib HTTP server (same `ThreadingHTTPServer`/`BaseHTTPRequestHandler` idiom as `display/server.py`) bound to **127.0.0.1:8766** — localhost-only because it can read/write API keys. It serves a single-page UI and a JSON API, manages the pipeline (`python -m livetranslate --config config.toml`) as a **subprocess** (so the control panel survives pipeline crashes/restarts), and edits the three operator files on disk with atomic writes. Double-clickable launcher scripts (`.command` for macOS, `.bat` for Windows) bootstrap the venv and open the browser — the operator never touches a terminal.

**Why not Electron/Tauri:** a desktop shell adds a Node/Rust toolchain and per-OS code-signing for zero functional gain — the deliverable is already a browser UI. Why not extend the display server: the control plane must outlive pipeline restarts, so it cannot live inside the pipeline process.

**Tech Stack:** Python ≥ 3.11 stdlib (`http.server`, `subprocess`, `socket`, `webbrowser`), `tomlkit` (new dep — comment-preserving TOML round-trip), `sounddevice` + `numpy` (already deps) for device list + level meter. Frontend: one static HTML page + one JS file, no framework (matches `display/static`).

**New API surface (port 8766, localhost only):**

| Method+Path | Purpose |
|---|---|
| `GET /` | Control panel UI (static) |
| `GET /api/state` | running/pid/last_exit, LAN IP, operator+language links, masked key status |
| `GET /api/config` | raw `config.toml` text + parsed common fields |
| `POST /api/config` | `{"text": ...}` raw save **or** `{"fields": {"audio.device_substring": ...}}` round-trip update; both validated before write |
| `GET /api/glossary` / `POST /api/glossary` | raw TSV text; POST validates with the real `Glossary` loader, returns term/keyterm counts |
| `POST /api/keys` | `{"ELEVENLABS_API_KEY": "...", ...}` → update `.env` (only non-empty values; values never echoed back) |
| `GET /api/audio/devices` | input devices, flag which matches `audio.device_substring` |
| `POST /api/meter` / `GET /api/meter` / `POST /api/meter/stop` | start meter on device, poll RMS/peak dBFS, stop |
| `POST /api/server/start` / `POST /api/server/stop` | launch/stop pipeline subprocess (start auto-stops meter, checks required keys) |
| `GET /api/logs?after=N` | new pipeline stdout/stderr lines since N |

**File map:**

| File | Responsibility |
|---|---|
| `src/livetranslate/control/__init__.py` | empty package marker |
| `src/livetranslate/control/files.py` | read/validate/write `.env`, `config.toml`, `glossary.tsv`; atomic writes; key masking |
| `src/livetranslate/control/netinfo.py` | LAN IP detection; build operator/language link list |
| `src/livetranslate/control/audio_probe.py` | input device listing; `LevelMeter` (short-lived `RawInputStream` → RMS/peak dBFS) |
| `src/livetranslate/control/process.py` | `PipelineProcess` subprocess manager + log ring |
| `src/livetranslate/control/server.py` | `ControlState`, `_Handler`, `ControlServer` (HTTP wiring) |
| `src/livetranslate/control/static/index.html`, `static/app.js` | operator UI |
| `src/livetranslate/control/__main__.py` | argparse entrypoint, open browser |
| `Start LiveTranslate.command`, `Start LiveTranslate.bat` | double-click launchers (repo root) |
| Modify: `pyproject.toml` | add `tomlkit` |
| Modify: `src/livetranslate/runner.py:74-81` | also register `SIGBREAK` so Windows CTRL_BREAK drains gracefully |
| Modify: `README.md` | "Operator control panel" section |
| Tests: `tests/test_control_files.py`, `tests/test_control_netinfo.py`, `tests/test_control_audio.py`, `tests/test_control_process.py`, `tests/test_control_server.py` | offline, no API keys, mocked `sounddevice` |

**Conventions for the implementer (read first):**
- Run everything with the project venv: `.venv/bin/python -m pytest tests/ -q` (Mac dev machine). All tests must stay offline — no API keys, no network, no real microphone.
- `sounddevice` is always imported **lazily inside functions** (existing codebase rule — PortAudio must not load at import time). Tests inject a fake module via `sys.modules`.
- The display server (audience-facing, port 8765 from `config.toml [display]`) binds `0.0.0.0`. The control server (operator-facing, port 8766) binds `127.0.0.1` **only** — it can read/write `.env`. Never make the control bind address configurable to non-loopback in this plan.

---

### Task 1: Dependency + package skeleton

**Files:**
- Modify: `pyproject.toml`
- Create: `src/livetranslate/control/__init__.py`

- [ ] **Step 1: Add tomlkit dependency**

In `pyproject.toml`, change:

```toml
dependencies = ["sounddevice", "numpy", "websocket-client", "requests"]
```

to:

```toml
dependencies = ["sounddevice", "numpy", "websocket-client", "requests", "tomlkit"]
```

- [ ] **Step 2: Create the package marker**

Create `src/livetranslate/control/__init__.py` with exactly:

```python
"""Operator control panel: local web app that configures and launches the pipeline."""
```

- [ ] **Step 3: Install and verify**

Run: `.venv/bin/pip install -e '.[harness,dev]' -q && .venv/bin/python -c "import tomlkit, livetranslate.control; print('ok')"`
Expected: `ok`

- [ ] **Step 4: Run existing test suite to confirm nothing broke**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: all pass (74 at time of writing).

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml src/livetranslate/control/__init__.py
git commit -m "feat(control): package skeleton + tomlkit dependency"
```

---

### Task 2: files.py — .env read/write + key masking

**Files:**
- Create: `src/livetranslate/control/files.py`
- Test: `tests/test_control_files.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_control_files.py`:

```python
import pytest

from livetranslate.control import files


def test_read_env_parses_and_strips_quotes(tmp_path):
    p = tmp_path / ".env"
    p.write_text('# comment\n\nELEVENLABS_API_KEY="abc123"\nTRANSLATE_API_KEY=xyz789\n')
    env = files.read_env(p)
    assert env == {"ELEVENLABS_API_KEY": "abc123", "TRANSLATE_API_KEY": "xyz789"}


def test_read_env_missing_file_returns_empty(tmp_path):
    assert files.read_env(tmp_path / "nope.env") == {}


def test_write_env_keys_updates_in_place_preserving_comments(tmp_path):
    p = tmp_path / ".env"
    p.write_text("# keep me\nELEVENLABS_API_KEY=old\nOTHER=untouched\n")
    files.write_env_keys(p, {"ELEVENLABS_API_KEY": "new", "TRANSLATE_API_KEY": "added"})
    text = p.read_text()
    assert "# keep me" in text
    assert "ELEVENLABS_API_KEY=new" in text
    assert "OTHER=untouched" in text
    assert "TRANSLATE_API_KEY=added" in text
    assert "old" not in text


def test_write_env_keys_skips_empty_values(tmp_path):
    p = tmp_path / ".env"
    p.write_text("ELEVENLABS_API_KEY=keepme\n")
    files.write_env_keys(p, {"ELEVENLABS_API_KEY": ""})
    assert "keepme" in p.read_text()


def test_write_env_keys_creates_file(tmp_path):
    p = tmp_path / ".env"
    files.write_env_keys(p, {"TRANSLATE_API_KEY": "abc"})
    assert files.read_env(p) == {"TRANSLATE_API_KEY": "abc"}


def test_mask_shows_only_last_four():
    assert files.mask("sk-1234567890abcd") == "…abcd"
    assert files.mask("abc") == "…"
    assert files.mask("") == ""
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_control_files.py -q`
Expected: FAIL — `ImportError` / `AttributeError` (module has no `read_env`).

- [ ] **Step 3: Implement**

Create `src/livetranslate/control/files.py`:

```python
"""Read/validate/write the operator-editable files: .env, config.toml, glossary.tsv.

All writes are atomic (tmp file in the same directory + os.replace) so a crash
mid-write never corrupts an event-day file.
"""
import os
import tempfile
from pathlib import Path

SECRET_KEYS = ("ELEVENLABS_API_KEY", "ASSEMBLYAI_API_KEY", "TRANSLATE_API_KEY")


def atomic_write(path, text: str) -> None:
    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_env(path) -> dict:
    """Parse KEY=VALUE lines; skip comments/blanks; strip optional quotes."""
    p = Path(path)
    if not p.exists():
        return {}
    out = {}
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def write_env_keys(path, updates: dict) -> None:
    """Update KEY=VALUE lines in place, preserving unrelated lines and comments.

    Empty values are skipped so a blank form field never wipes a stored key.
    """
    p = Path(path)
    lines = p.read_text(encoding="utf-8").splitlines() if p.exists() else []
    remaining = {k: v for k, v in updates.items() if v}
    out = []
    for line in lines:
        stripped = line.strip()
        key = None
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.partition("=")[0].strip()
        if key in remaining:
            out.append(f"{key}={remaining.pop(key)}")
        else:
            out.append(line)
    for key, value in remaining.items():
        out.append(f"{key}={value}")
    atomic_write(p, "\n".join(out) + "\n")


def mask(value: str) -> str:
    """Never return enough of a secret to be useful: last 4 chars at most."""
    if not value:
        return ""
    return "…" + value[-4:] if len(value) > 4 else "…"
```

- [ ] **Step 4: Run tests**

Run: `.venv/bin/python -m pytest tests/test_control_files.py -q`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add src/livetranslate/control/files.py tests/test_control_files.py
git commit -m "feat(control): .env read/write with masking and atomic writes"
```

---

### Task 3: files.py — config.toml validate/round-trip

**Files:**
- Modify: `src/livetranslate/control/files.py`
- Test: `tests/test_control_files.py`

- [ ] **Step 1: Write the failing tests** (append to `tests/test_control_files.py`)

```python
GOOD_TOML = """\
[session]
source_language = "en"   # pinned
output_dir = "sessions"

[audio]
device_substring = "Scarlett"
chunk_ms = 100

[asr]
adapter = "elevenlabs"

[translate]
targets = ["es", "fr"]

[glossary]
path = "glossary.tsv"

[display]
host = "0.0.0.0"
port = 8765
"""


def test_validate_config_accepts_good_toml():
    assert files.validate_config_text(GOOD_TOML) == []


def test_validate_config_rejects_syntax_error():
    problems = files.validate_config_text("[session\nbroken")
    assert len(problems) == 1 and "TOML" in problems[0]


def test_validate_config_rejects_missing_section():
    problems = files.validate_config_text("[session]\nsource_language='en'\n")
    assert any("missing [audio]" in p for p in problems)


def test_validate_config_rejects_bad_adapter_and_port():
    bad = GOOD_TOML.replace('adapter = "elevenlabs"', 'adapter = "whisper"')
    bad = bad.replace("port = 8765", "port = 99999")
    problems = files.validate_config_text(bad)
    assert any("adapter" in p for p in problems)
    assert any("port" in p for p in problems)


def test_write_config_text_rejects_invalid(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(GOOD_TOML)
    with pytest.raises(ValueError):
        files.write_config_text(p, "[broken")
    assert p.read_text() == GOOD_TOML  # untouched


def test_update_config_fields_preserves_comments(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(GOOD_TOML)
    files.update_config_fields(p, {"audio.device_substring": "USB Audio",
                                   "translate.targets": ["es", "fr", "de"]})
    text = p.read_text()
    assert "# pinned" in text                      # comment survived
    assert 'device_substring = "USB Audio"' in text
    assert '"de"' in text
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_control_files.py -q`
Expected: new tests FAIL with `AttributeError: ... validate_config_text`.

- [ ] **Step 3: Implement** (append to `src/livetranslate/control/files.py`)

```python
import tomlkit

REQUIRED_SECTIONS = ("session", "audio", "asr", "translate", "glossary", "display")


def read_config_text(path) -> str:
    return Path(path).read_text(encoding="utf-8")


def validate_config_text(text: str) -> list:
    """Return a list of human-readable problems; empty list means valid."""
    try:
        doc = tomlkit.parse(text)
    except Exception as exc:
        return [f"TOML syntax error: {exc}"]
    problems = []
    for section in REQUIRED_SECTIONS:
        if section not in doc:
            problems.append(f"missing [{section}] section")
    if problems:
        return problems
    if doc["asr"].get("adapter") not in ("elevenlabs", "assemblyai"):
        problems.append("asr.adapter must be 'elevenlabs' or 'assemblyai'")
    if not list(doc["translate"].get("targets", [])):
        problems.append("translate.targets must list at least one language")
    port = doc["display"].get("port")
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        problems.append("display.port must be an integer between 1 and 65535")
    return problems


def write_config_text(path, text: str) -> None:
    problems = validate_config_text(text)
    if problems:
        raise ValueError("; ".join(problems))
    atomic_write(path, text)


def update_config_fields(path, updates: dict) -> None:
    """Apply {"audio.device_substring": "..."}-style updates via tomlkit so
    comments and ordering in config.toml are preserved."""
    doc = tomlkit.parse(read_config_text(path))
    for dotted, value in updates.items():
        node = doc
        *parents, leaf = dotted.split(".")
        for part in parents:
            node = node[part]
        node[leaf] = value
    write_config_text(path, tomlkit.dumps(doc))
```

Move the `import tomlkit` line to the top of the file with the other imports.

- [ ] **Step 4: Run tests**

Run: `.venv/bin/python -m pytest tests/test_control_files.py -q`
Expected: 12 passed.

- [ ] **Step 5: Commit**

```bash
git add src/livetranslate/control/files.py tests/test_control_files.py
git commit -m "feat(control): config.toml validation and comment-preserving updates"
```

---

### Task 4: files.py — glossary validate/write

**Files:**
- Modify: `src/livetranslate/control/files.py`
- Test: `tests/test_control_files.py`

- [ ] **Step 1: Write the failing tests** (append to `tests/test_control_files.py`)

```python
GOOD_TSV = (
    "term_src\tes\tfr\tde\tpt\tar\tzh\tpriority\tnotes\n"
    "rate of profit\ttasa de ganancia\ttaux de profit\tProfitrate\t\t\t\t1\t\n"
    "Comintern\tComintern\tComintern\tKomintern\t\t\t\t1\tname\n"
)


def test_validate_glossary_counts_terms():
    result = files.validate_glossary_text(GOOD_TSV)
    assert result["problems"] == []
    assert result["terms"] == 2
    assert result["keyterms"] == 2


def test_validate_glossary_rejects_missing_header():
    result = files.validate_glossary_text("no header here\n")
    assert result["terms"] == 0
    assert result["problems"]


def test_validate_glossary_rejects_bad_priority():
    bad = GOOD_TSV.replace("\t1\t\n", "\tone\t\n", 1)
    result = files.validate_glossary_text(bad)
    assert result["problems"]


def test_write_glossary_text_rejects_invalid(tmp_path):
    p = tmp_path / "glossary.tsv"
    p.write_text(GOOD_TSV)
    with pytest.raises(ValueError):
        files.write_glossary_text(p, "garbage")
    assert p.read_text() == GOOD_TSV


def test_write_glossary_text_accepts_valid(tmp_path):
    p = tmp_path / "glossary.tsv"
    files.write_glossary_text(p, GOOD_TSV)
    assert p.read_text() == GOOD_TSV
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_control_files.py -q`
Expected: new tests FAIL with `AttributeError`.

- [ ] **Step 3: Implement** (append to `src/livetranslate/control/files.py`)

```python
def validate_glossary_text(text: str) -> dict:
    """Validate TSV with the real Glossary loader (single source of truth).

    Returns {"terms": n, "keyterms": m, "problems": [...]}, where keyterms is
    the count that would be sent to ElevenLabs under the cap of 50.
    """
    from ..glossary import Glossary

    first = text.splitlines()[0] if text.strip() else ""
    if "term_src" not in first.split("\t"):
        return {"terms": 0, "keyterms": 0,
                "problems": ["first line must be a tab-separated header containing 'term_src'"]}
    fd, tmp = tempfile.mkstemp(suffix=".tsv")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        glossary = Glossary.load(tmp)
        return {"terms": len(glossary.terms),
                "keyterms": len(glossary.keyterms(cap=50)),
                "problems": []}
    except Exception as exc:
        return {"terms": 0, "keyterms": 0, "problems": [f"glossary parse error: {exc}"]}
    finally:
        os.unlink(tmp)


def write_glossary_text(path, text: str) -> None:
    result = validate_glossary_text(text)
    if result["problems"]:
        raise ValueError("; ".join(result["problems"]))
    atomic_write(path, text)
```

- [ ] **Step 4: Run tests**

Run: `.venv/bin/python -m pytest tests/test_control_files.py -q`
Expected: 17 passed.

- [ ] **Step 5: Commit**

```bash
git add src/livetranslate/control/files.py tests/test_control_files.py
git commit -m "feat(control): glossary validation via real Glossary loader"
```

---

### Task 5: netinfo.py — LAN IP + link builder

**Files:**
- Create: `src/livetranslate/control/netinfo.py`
- Test: `tests/test_control_netinfo.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_control_netinfo.py`:

```python
import re

from livetranslate.control import netinfo


def test_lan_ip_returns_ipv4_string():
    ip = netinfo.lan_ip()
    assert re.fullmatch(r"\d+\.\d+\.\d+\.\d+", ip)


def test_links_builds_operator_and_language_urls():
    out = netinfo.links("192.168.1.50", 8765, ["es", "fr", "xx"])
    assert out["operator"] == "http://192.168.1.50:8765/"
    langs = {entry["lang"]: entry for entry in out["languages"]}
    assert langs["es"]["url"] == "http://192.168.1.50:8765/v/es"
    assert langs["es"]["name"] == "Español"
    assert langs["xx"]["name"] == "xx"  # unknown code falls back to the code itself
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_control_netinfo.py -q`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

Create `src/livetranslate/control/netinfo.py`:

```python
"""LAN IP discovery and audience link building."""
import socket

LANG_NAMES = {"es": "Español", "fr": "Français", "de": "Deutsch",
              "pt": "Português", "ar": "العربية", "zh": "中文", "en": "English"}


def lan_ip() -> str:
    """Best-effort LAN IP of the outbound interface.

    UDP connect() picks a route without sending any packet; works offline as
    long as any interface is up. Falls back to loopback.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 80))  # TEST-NET-1: never actually routed
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def links(ip: str, port: int, targets: list) -> dict:
    base = f"http://{ip}:{port}"
    return {
        "operator": f"{base}/",
        "languages": [{"lang": code, "name": LANG_NAMES.get(code, code),
                       "url": f"{base}/v/{code}"} for code in targets],
    }
```

- [ ] **Step 4: Run tests**

Run: `.venv/bin/python -m pytest tests/test_control_netinfo.py -q`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add src/livetranslate/control/netinfo.py tests/test_control_netinfo.py
git commit -m "feat(control): LAN IP detection and audience link builder"
```

---

### Task 6: audio_probe.py — device list + level meter

**Files:**
- Create: `src/livetranslate/control/audio_probe.py`
- Test: `tests/test_control_audio.py`

`sounddevice` must be imported lazily (codebase rule — see `MicSource.resolve_device` in `src/livetranslate/audio.py:94`). Tests inject a fake `sounddevice` module into `sys.modules` so no PortAudio/mic is touched.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_control_audio.py`:

```python
import math
import sys
import types

import pytest


@pytest.fixture
def fake_sounddevice(monkeypatch):
    mod = types.SimpleNamespace()
    mod.query_devices = lambda: [
        {"name": "MacBook Pro Microphone", "max_input_channels": 1, "default_samplerate": 48000.0},
        {"name": "Scarlett 2i2 USB", "max_input_channels": 2, "default_samplerate": 48000.0},
        {"name": "External Headphones", "max_input_channels": 0, "default_samplerate": 48000.0},
    ]

    class FakeStream:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.started = False

        def start(self):
            self.started = True

        def stop(self):
            self.started = False

        def close(self):
            pass

    mod.RawInputStream = FakeStream
    monkeypatch.setitem(sys.modules, "sounddevice", mod)
    return mod


def test_list_input_devices_filters_outputs_and_flags_match(fake_sounddevice):
    from livetranslate.control.audio_probe import list_input_devices
    devices = list_input_devices("scarlett")
    names = [d["name"] for d in devices]
    assert "External Headphones" not in names          # output-only excluded
    assert [d["matches"] for d in devices] == [False, True]
    assert devices[1]["index"] == 1                    # original sounddevice index kept


def test_level_meter_computes_dbfs(fake_sounddevice):
    from livetranslate.control.audio_probe import LevelMeter
    meter = LevelMeter(device_index=1)
    meter.start()
    assert meter._stream.started
    # feed one block of a half-scale square wave: RMS = peak = 16384 -> ~ -6.02 dBFS
    pcm = (16384).to_bytes(2, "little", signed=True) * 1600
    meter._callback(pcm, 1600, None, None)
    reading = meter.read()
    assert math.isclose(reading["rms_dbfs"], -6.0, abs_tol=0.1)
    assert math.isclose(reading["peak_dbfs"], -6.0, abs_tol=0.1)
    meter.stop()
    assert meter._stream is None


def test_level_meter_silence_floor(fake_sounddevice):
    from livetranslate.control.audio_probe import LevelMeter
    meter = LevelMeter(device_index=0)
    meter._callback(b"\x00\x00" * 1600, 1600, None, None)
    assert meter.read()["rms_dbfs"] <= -90.0
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_control_audio.py -q`
Expected: FAIL — `ModuleNotFoundError: livetranslate.control.audio_probe`.

- [ ] **Step 3: Implement**

Create `src/livetranslate/control/audio_probe.py`:

```python
"""Input-device listing and a short-lived level meter for soundcheck.

The meter opens its own RawInputStream on the chosen device. It MUST be
stopped before the pipeline launches so the device is not opened twice;
server.py enforces this on /api/server/start.
"""
import math
import threading

import numpy as np

SILENCE_DBFS = -120.0


def list_input_devices(substring: str = "") -> list:
    import sounddevice  # lazy: PortAudio loads only when actually probing
    out = []
    for idx, dev in enumerate(sounddevice.query_devices()):
        if dev.get("max_input_channels", 0) <= 0:
            continue
        out.append({
            "index": idx,
            "name": dev.get("name", ""),
            "default_samplerate": dev.get("default_samplerate"),
            "matches": bool(substring) and substring.lower() in dev.get("name", "").lower(),
        })
    return out


class LevelMeter:
    """RMS/peak dBFS from one input device. start() / read() / stop()."""

    def __init__(self, device_index: int):
        self.device_index = device_index
        self._rms_dbfs = SILENCE_DBFS
        self._peak_dbfs = SILENCE_DBFS
        self._lock = threading.Lock()
        self._stream = None

    def _callback(self, indata, frames, time_info, status_flags):
        pcm = np.frombuffer(bytes(indata), dtype=np.int16).astype(np.float64)
        if pcm.size == 0:
            return
        rms = math.sqrt(float(np.mean(pcm * pcm)))
        peak = float(np.max(np.abs(pcm)))
        with self._lock:
            self._rms_dbfs = 20 * math.log10(max(rms, 1.0) / 32768.0)
            self._peak_dbfs = 20 * math.log10(max(peak, 1.0) / 32768.0)

    def start(self) -> None:
        import sounddevice
        self._stream = sounddevice.RawInputStream(
            samplerate=16000, channels=1, dtype="int16",
            device=self.device_index, blocksize=1600, callback=self._callback)
        self._stream.start()

    def read(self) -> dict:
        with self._lock:
            return {"rms_dbfs": round(self._rms_dbfs, 1),
                    "peak_dbfs": round(self._peak_dbfs, 1)}

    def stop(self) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None
```

- [ ] **Step 4: Run tests**

Run: `.venv/bin/python -m pytest tests/test_control_audio.py -q`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add src/livetranslate/control/audio_probe.py tests/test_control_audio.py
git commit -m "feat(control): input device listing + RMS/peak level meter"
```

---

### Task 7: process.py — pipeline subprocess manager

**Files:**
- Create: `src/livetranslate/control/process.py`
- Test: `tests/test_control_process.py`

Tests use a stub child command (injectable `cmd`) so no real pipeline/API keys are needed.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_control_process.py`:

```python
import sys
import time

import pytest

from livetranslate.control.process import PipelineProcess

# A stub child that prints, handles SIGINT gracefully, then idles.
STUB = """
import signal, sys, time
def bye(_s, _f):
    print("drained", flush=True)
    sys.exit(0)
signal.signal(signal.SIGINT, bye)
if hasattr(signal, "SIGBREAK"):
    signal.signal(signal.SIGBREAK, bye)
print("pipeline up", flush=True)
while True:
    time.sleep(0.1)
"""


def make_proc(tmp_path, stub=STUB):
    return PipelineProcess(tmp_path, cmd=[sys.executable, "-u", "-c", stub])


def wait_until(predicate, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def test_start_runs_and_captures_logs(tmp_path):
    proc = make_proc(tmp_path)
    proc.start(extra_env={"FAKE_KEY": "x"})
    assert proc.running()
    assert wait_until(lambda: any("pipeline up" in l for l in proc.logs_since(0)[0]))
    proc.stop()


def test_start_twice_raises(tmp_path):
    proc = make_proc(tmp_path)
    proc.start(extra_env={})
    with pytest.raises(RuntimeError):
        proc.start(extra_env={})
    proc.stop()


def test_stop_is_graceful_then_records_exit(tmp_path):
    proc = make_proc(tmp_path)
    proc.start(extra_env={})
    wait_until(lambda: any("pipeline up" in l for l in proc.logs_since(0)[0]))
    proc.stop()
    assert not proc.running()
    assert wait_until(lambda: any("drained" in l for l in proc.logs_since(0)[0]))
    assert proc.last_exit == 0


def test_logs_since_cursor(tmp_path):
    proc = make_proc(tmp_path)
    proc.start(extra_env={})
    wait_until(lambda: proc.logs_since(0)[1] >= 1)
    lines, seq = proc.logs_since(0)
    again, seq2 = proc.logs_since(seq)
    assert again == [] and seq2 == seq
    proc.stop()


def test_child_crash_sets_last_exit(tmp_path):
    proc = make_proc(tmp_path, stub="import sys; print('boom', flush=True); sys.exit(3)")
    proc.start(extra_env={})
    assert wait_until(lambda: proc.last_exit is not None)
    assert proc.last_exit == 3
    assert not proc.running()
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_control_process.py -q`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

Create `src/livetranslate/control/process.py`:

```python
"""Run the livetranslate pipeline as a child process; capture logs; stop gracefully.

Graceful stop sends SIGINT (CTRL_BREAK_EVENT on Windows) so runner.run_live
drains and closes the session store; kill() is the timeout fallback.
"""
import os
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path


class PipelineProcess:
    def __init__(self, project_root, config_path="config.toml", cmd=None):
        self.project_root = Path(project_root)
        self.config_path = config_path
        self.cmd = cmd or [sys.executable, "-u", "-m", "livetranslate",
                           "--config", config_path]
        self.proc = None
        self.started_at = None
        self.last_exit = None
        self._log = deque(maxlen=2000)
        self._log_seq = 0            # lines ever appended (ring may have dropped early ones)
        self._lock = threading.Lock()

    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self, extra_env: dict) -> None:
        if self.running():
            raise RuntimeError("pipeline already running")
        env = {**os.environ, **extra_env}
        kwargs = {}
        if sys.platform == "win32":
            # New process group so CTRL_BREAK_EVENT reaches only the child.
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        self.proc = subprocess.Popen(
            self.cmd, cwd=str(self.project_root), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, **kwargs)
        self.started_at = time.time()
        self.last_exit = None
        threading.Thread(target=self._pump, name="pipeline-log-pump",
                         daemon=True).start()

    def _pump(self) -> None:
        proc = self.proc
        for line in proc.stdout:
            with self._lock:
                self._log.append(line.rstrip("\n"))
                self._log_seq += 1
        code = proc.wait()
        with self._lock:
            self.last_exit = code
            self._log.append(f"--- pipeline exited with code {code} ---")
            self._log_seq += 1

    def logs_since(self, after: int):
        """Return (new_lines, cursor). Poll with the returned cursor."""
        with self._lock:
            dropped = self._log_seq - len(self._log)
            start = max(after - dropped, 0)
            return list(self._log)[start:], self._log_seq

    def stop(self, grace_s: float = 10.0) -> None:
        if not self.running():
            return
        if sys.platform == "win32":
            self.proc.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            self.proc.send_signal(signal.SIGINT)
        try:
            self.proc.wait(timeout=grace_s)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
```

- [ ] **Step 4: Run tests**

Run: `.venv/bin/python -m pytest tests/test_control_process.py -q`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add src/livetranslate/control/process.py tests/test_control_process.py
git commit -m "feat(control): pipeline subprocess manager with log ring and graceful stop"
```

---

### Task 8: Windows graceful shutdown in runner

**Files:**
- Modify: `src/livetranslate/runner.py:74-97`
- Test: `tests/test_runner.py` (append)

On Windows, `CTRL_BREAK_EVENT` arrives as `SIGBREAK`, which `run_live` does not currently handle — the child would die without draining. Register the same handler for `SIGBREAK` when it exists.

- [ ] **Step 1: Write the failing test** (append to `tests/test_runner.py`; follow the existing test style in that file for constructing cfg/fakes — read it first)

```python
def test_run_live_registers_sigbreak_when_available(monkeypatch):
    """On Windows, CTRL_BREAK arrives as SIGBREAK; run_live must register it
    alongside SIGINT so the control panel can stop the pipeline gracefully."""
    import signal as signal_mod
    from livetranslate import runner

    registered = []
    real_signal = signal_mod.signal

    def spy(sig, handler):
        registered.append(sig)
        return real_signal(sig, handler) if sig == signal_mod.SIGINT else None

    monkeypatch.setattr(signal_mod, "signal", spy)
    if not hasattr(signal_mod, "SIGBREAK"):
        monkeypatch.setattr(signal_mod, "SIGBREAK", 21, raising=False)

    sigs = runner._shutdown_signals()
    assert signal_mod.SIGINT in sigs
    assert getattr(signal_mod, "SIGBREAK") in sigs
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_runner.py -q`
Expected: FAIL — `AttributeError: ... _shutdown_signals`.

- [ ] **Step 3: Implement**

In `src/livetranslate/runner.py`, add above `run_live`:

```python
def _shutdown_signals():
    """SIGINT everywhere; SIGBREAK too on Windows (CTRL_BREAK_EVENT from the
    control panel arrives as SIGBREAK)."""
    sigs = [signal.SIGINT]
    if hasattr(signal, "SIGBREAK"):
        sigs.append(signal.SIGBREAK)
    return sigs
```

Then replace the existing single-signal registration block (currently `runner.py:74-81`):

```python
    def _sigint(_sig, _frm):
        log.info("SIGINT: draining and shutting down...")
        stop.set()

    try:
        prev_handler = signal.signal(signal.SIGINT, _sigint)
    except ValueError:
        prev_handler = None
```

with:

```python
    def _sigint(_sig, _frm):
        log.info("shutdown signal: draining and shutting down...")
        stop.set()

    prev_handlers = {}
    for sig in _shutdown_signals():
        try:
            prev_handlers[sig] = signal.signal(sig, _sigint)
        except ValueError:
            # signal.signal only works from the main thread; ignore otherwise
            pass
```

and the restore block in the `finally:` (currently `runner.py:91-95`):

```python
        if prev_handler is not None:
            try:
                signal.signal(signal.SIGINT, prev_handler)
            except ValueError:
                pass
```

with:

```python
        for sig, handler in prev_handlers.items():
            try:
                signal.signal(sig, handler)
            except ValueError:
                pass
```

- [ ] **Step 4: Run tests**

Run: `.venv/bin/python -m pytest tests/test_runner.py tests/ -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/livetranslate/runner.py tests/test_runner.py
git commit -m "fix(runner): handle SIGBREAK so Windows CTRL_BREAK drains gracefully"
```

---

### Task 9: server.py — control HTTP server + JSON API

**Files:**
- Create: `src/livetranslate/control/server.py`
- Test: `tests/test_control_server.py`

Mirrors `display/server.py`: `ThreadingHTTPServer`, handler subclass created per-instance via `type(...)`, port 0 supported for tests.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_control_server.py`:

```python
import json
import sys
import types
import urllib.error
import urllib.request

import pytest

from livetranslate.control.server import ControlServer, ControlState

CONFIG = """\
[session]
source_language = "en"
output_dir = "sessions"

[audio]
device_substring = "Scarlett"
chunk_ms = 100

[asr]
adapter = "elevenlabs"

[translate]
targets = ["es", "fr"]

[glossary]
path = "glossary.tsv"

[display]
host = "0.0.0.0"
port = 8765
"""

TSV = ("term_src\tes\tfr\tde\tpt\tar\tzh\tpriority\tnotes\n"
       "Comintern\tComintern\tComintern\tKomintern\t\t\t\t1\t\n")

STUB_CHILD = "import time; print('pipeline up', flush=True); time.sleep(60)"


@pytest.fixture
def fake_sounddevice(monkeypatch):
    mod = types.SimpleNamespace(
        query_devices=lambda: [
            {"name": "Scarlett 2i2 USB", "max_input_channels": 2,
             "default_samplerate": 48000.0}],
        RawInputStream=lambda **kw: types.SimpleNamespace(
            start=lambda: None, stop=lambda: None, close=lambda: None),
    )
    monkeypatch.setitem(sys.modules, "sounddevice", mod)
    return mod


@pytest.fixture
def srv(tmp_path):
    (tmp_path / "config.toml").write_text(CONFIG)
    (tmp_path / "glossary.tsv").write_text(TSV)
    (tmp_path / ".env").write_text("ELEVENLABS_API_KEY=el123456\nTRANSLATE_API_KEY=tr123456\n")
    state = ControlState(tmp_path)
    state.pipeline.cmd = [sys.executable, "-u", "-c", STUB_CHILD]
    server = ControlServer(state, host="127.0.0.1", port=0)
    server.start()
    yield f"http://127.0.0.1:{server.port}", state
    state.pipeline.stop()
    state.stop_meter()
    server.stop()


def get(base, path):
    with urllib.request.urlopen(base + path) as resp:
        return resp.status, json.loads(resp.read())


def post(base, path, payload=None):
    data = json.dumps(payload or {}).encode()
    req = urllib.request.Request(base + path, data=data,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_state_reports_links_and_masked_keys(srv):
    base, _ = srv
    status, body = get(base, "/api/state")
    assert status == 200
    assert body["running"] is False
    assert body["links"]["operator"].endswith(":8765/")
    assert {l["lang"] for l in body["links"]["languages"]} == {"es", "fr"}
    keys = {k["name"]: k for k in body["keys"]}
    assert keys["ELEVENLABS_API_KEY"]["set"] is True
    assert keys["ELEVENLABS_API_KEY"]["masked"] == "…3456"
    assert "el123456" not in json.dumps(body)          # never leak full secrets


def test_config_roundtrip_and_validation(srv):
    base, state = srv
    status, body = get(base, "/api/config")
    assert status == 200 and "Scarlett" in body["text"]
    status, _ = post(base, "/api/config",
                     {"fields": {"audio.device_substring": "USB Audio"}})
    assert status == 200
    assert "USB Audio" in (state.root / "config.toml").read_text()
    status, body = post(base, "/api/config", {"text": "[broken"})
    assert status == 400 and body["problems"]


def test_glossary_roundtrip(srv):
    base, _ = srv
    status, body = get(base, "/api/glossary")
    assert status == 200 and body["text"].startswith("term_src")
    status, body = post(base, "/api/glossary", {"text": TSV})
    assert status == 200 and body["terms"] == 1
    status, body = post(base, "/api/glossary", {"text": "junk"})
    assert status == 400


def test_keys_update_writes_env(srv):
    base, state = srv
    status, _ = post(base, "/api/keys", {"TRANSLATE_API_KEY": "newkey99"})
    assert status == 200
    assert "TRANSLATE_API_KEY=newkey99" in (state.root / ".env").read_text()


def test_audio_devices_and_meter(srv, fake_sounddevice):
    base, _ = srv
    status, body = get(base, "/api/audio/devices")
    assert status == 200 and body["devices"][0]["matches"] is True
    status, _ = post(base, "/api/meter", {"device_index": 0})
    assert status == 200
    status, body = get(base, "/api/meter")
    assert status == 200 and "rms_dbfs" in body
    status, _ = post(base, "/api/meter/stop")
    assert status == 200


def test_server_start_stop_and_logs(srv, fake_sounddevice):
    base, state = srv
    status, _ = post(base, "/api/meter", {"device_index": 0})
    assert status == 200
    status, body = post(base, "/api/server/start")
    assert status == 200
    assert state.meter is None                  # meter auto-stopped before launch
    import time
    deadline = time.time() + 10
    seen = False
    while time.time() < deadline and not seen:
        _, body = get(base, "/api/logs?after=0")
        seen = any("pipeline up" in l for l in body["lines"])
        time.sleep(0.05)
    assert seen
    status, body = get(base, "/api/state")
    assert body["running"] is True
    status, _ = post(base, "/api/server/start")
    assert status == 409                        # already running
    status, _ = post(base, "/api/server/stop")
    assert status == 200


def test_server_start_refuses_without_required_keys(srv):
    base, state = srv
    (state.root / ".env").write_text("TRANSLATE_API_KEY=tr123456\n")
    status, body = post(base, "/api/server/start")
    assert status == 400
    assert "ELEVENLABS_API_KEY" in body["error"]
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_control_server.py -q`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

Create `src/livetranslate/control/server.py`:

```python
"""Control-panel HTTP server. Binds loopback ONLY — it reads/writes API keys.

Same stdlib idiom as display/server.py: ThreadingHTTPServer + a handler class
specialised per instance. JSON API + one static page.
"""
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import tomlkit

from . import files, netinfo
from .audio_probe import LevelMeter, list_input_devices
from .process import PipelineProcess

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"

# adapter name -> env var it requires (translate key is always required)
ADAPTER_KEYS = {"elevenlabs": "ELEVENLABS_API_KEY", "assemblyai": "ASSEMBLYAI_API_KEY"}


class ControlState:
    """Paths + pipeline handle + (optional) running level meter."""

    def __init__(self, root):
        self.root = Path(root)
        self.config_path = self.root / "config.toml"
        self.env_path = self.root / ".env"
        self.pipeline = PipelineProcess(self.root)
        self.meter = None
        self.meter_lock = threading.Lock()

    def config_doc(self):
        return tomlkit.parse(files.read_config_text(self.config_path))

    def glossary_path(self) -> Path:
        return self.root / str(self.config_doc()["glossary"]["path"])

    def start_meter(self, device_index: int) -> None:
        with self.meter_lock:
            if self.meter is not None:
                self.meter.stop()
            meter = LevelMeter(device_index)
            meter.start()
            self.meter = meter

    def stop_meter(self) -> None:
        with self.meter_lock:
            if self.meter is not None:
                self.meter.stop()
                self.meter = None


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    state: ControlState = None

    def log_message(self, fmt, *args):
        log.debug("control http: " + fmt, *args)

    # ---------- plumbing ----------

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    def _static(self, name, ctype):
        body = (STATIC / name).read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ---------- routing ----------

    def do_GET(self):
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/":
                self._static("index.html", "text/html; charset=utf-8")
            elif parsed.path == "/app.js":
                self._static("app.js", "application/javascript; charset=utf-8")
            elif parsed.path == "/api/state":
                self._json(200, self._state_payload())
            elif parsed.path == "/api/config":
                self._json(200, {"text": files.read_config_text(self.state.config_path)})
            elif parsed.path == "/api/glossary":
                self._json(200, {"text": self.state.glossary_path().read_text(encoding="utf-8")})
            elif parsed.path == "/api/audio/devices":
                substring = str(self.state.config_doc()["audio"]["device_substring"])
                self._json(200, {"devices": list_input_devices(substring)})
            elif parsed.path == "/api/meter":
                with self.state.meter_lock:
                    meter = self.state.meter
                if meter is None:
                    self._json(409, {"error": "meter not running"})
                else:
                    self._json(200, meter.read())
            elif parsed.path == "/api/logs":
                after = int(parse_qs(parsed.query).get("after", ["0"])[0])
                lines, cursor = self.state.pipeline.logs_since(after)
                self._json(200, {"lines": lines, "cursor": cursor})
            else:
                self._json(404, {"error": "not found"})
        except Exception as exc:
            log.exception("GET %s failed", parsed.path)
            self._json(500, {"error": str(exc)})

    def do_POST(self):
        parsed = urlparse(self.path)
        payload = self._body()
        if payload is None:
            self._json(400, {"error": "invalid JSON body"})
            return
        try:
            if parsed.path == "/api/config":
                self._post_config(payload)
            elif parsed.path == "/api/glossary":
                self._post_glossary(payload)
            elif parsed.path == "/api/keys":
                updates = {k: v for k, v in payload.items() if k in files.SECRET_KEYS}
                files.write_env_keys(self.state.env_path, updates)
                self._json(200, {"ok": True})
            elif parsed.path == "/api/meter":
                if self.state.pipeline.running():
                    self._json(409, {"error": "pipeline running; meter unavailable"})
                else:
                    self.state.start_meter(int(payload["device_index"]))
                    self._json(200, {"ok": True})
            elif parsed.path == "/api/meter/stop":
                self.state.stop_meter()
                self._json(200, {"ok": True})
            elif parsed.path == "/api/server/start":
                self._post_start()
            elif parsed.path == "/api/server/stop":
                self.state.pipeline.stop()
                self._json(200, {"ok": True})
            else:
                self._json(404, {"error": "not found"})
        except Exception as exc:
            log.exception("POST %s failed", parsed.path)
            self._json(500, {"error": str(exc)})

    # ---------- handlers ----------

    def _state_payload(self):
        doc = self.state.config_doc()
        ip = netinfo.lan_ip()
        env = files.read_env(self.state.env_path)
        pipe = self.state.pipeline
        return {
            "running": pipe.running(),
            "pid": pipe.proc.pid if pipe.running() else None,
            "started_at": pipe.started_at,
            "last_exit": pipe.last_exit,
            "lan_ip": ip,
            "display_port": int(doc["display"]["port"]),
            "links": netinfo.links(ip, int(doc["display"]["port"]),
                                   [str(t) for t in doc["translate"]["targets"]]),
            "keys": [{"name": k, "set": k in env, "masked": files.mask(env.get(k, ""))}
                     for k in files.SECRET_KEYS],
            "meter_running": self.state.meter is not None,
        }

    def _post_config(self, payload):
        try:
            if "fields" in payload:
                files.update_config_fields(self.state.config_path, payload["fields"])
            else:
                files.write_config_text(self.state.config_path, payload.get("text", ""))
            self._json(200, {"ok": True})
        except ValueError as exc:
            self._json(400, {"problems": str(exc).split("; ")})

    def _post_glossary(self, payload):
        text = payload.get("text", "")
        result = files.validate_glossary_text(text)
        if result["problems"]:
            self._json(400, result)
            return
        files.write_glossary_text(self.state.glossary_path(), text)
        self._json(200, result)

    def _post_start(self):
        if self.state.pipeline.running():
            self._json(409, {"error": "pipeline already running"})
            return
        doc = self.state.config_doc()
        env = files.read_env(self.state.env_path)
        required = ["TRANSLATE_API_KEY", ADAPTER_KEYS[str(doc["asr"]["adapter"])]]
        if doc["asr"].get("failover"):
            required.append(ADAPTER_KEYS[str(doc["asr"]["failover"])])
        missing = [k for k in required if not env.get(k)]
        if missing:
            self._json(400, {"error": "missing API keys: " + ", ".join(missing)})
            return
        self.state.stop_meter()       # never hold the device open under the pipeline
        self.state.pipeline.start(extra_env=env)
        self._json(200, {"ok": True})


class ControlServer:
    def __init__(self, state: ControlState, host="127.0.0.1", port=8766):
        handler = type("Handler", (_Handler,), {"state": state})
        self._httpd = ThreadingHTTPServer((host, port), handler)
        self._httpd.daemon_threads = True
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                        name="control-http", daemon=False)

    def start(self):
        self._thread.start()

    def join(self):
        self._thread.join()

    def stop(self):
        self._httpd.shutdown()
        self._httpd.server_close()
```

Note: `index.html`/`app.js` don't exist yet — that's fine; only `test_control_server.py`'s API routes are exercised here. The static routes get covered by Task 10.

- [ ] **Step 4: Run tests**

Run: `.venv/bin/python -m pytest tests/test_control_server.py -q`
Expected: 7 passed.

- [ ] **Step 5: Run full suite**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add src/livetranslate/control/server.py tests/test_control_server.py
git commit -m "feat(control): HTTP control server with config/glossary/keys/audio/launch API"
```

---

### Task 10: Frontend — index.html + app.js

**Files:**
- Create: `src/livetranslate/control/static/index.html`
- Create: `src/livetranslate/control/static/app.js`
- Test: `tests/test_control_server.py` (append one static-route test)

Single dark page, five cards top-to-bottom: **Status & Launch**, **Audience links**, **Audio input**, **Configuration** (common fields + raw TOML in a `<details>`), **Glossary**, **API keys**, plus a log pane and an operator-console iframe shown while running. Poll `/api/state` every 2 s, `/api/meter` every 150 ms while metering, `/api/logs` every 1 s while running.

- [ ] **Step 1: Write the failing test** (append to `tests/test_control_server.py`)

```python
def test_index_and_appjs_served(srv):
    base, _ = srv
    with urllib.request.urlopen(base + "/") as resp:
        assert resp.status == 200
        assert b"LiveTranslate" in resp.read()
    with urllib.request.urlopen(base + "/app.js") as resp:
        assert resp.status == 200
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_control_server.py::test_index_and_appjs_served -q`
Expected: FAIL — `HTTPError: 500` (file missing).

- [ ] **Step 3: Create `src/livetranslate/control/static/index.html`**

```html
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>LiveTranslate — Operator Control Panel</title>
<style>
  :root { --bg:#101418; --card:#1a2128; --text:#e6edf3; --dim:#8b98a5;
          --accent:#3fb950; --warn:#d29922; --err:#f85149; --line:#2d3640; }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--bg); color:var(--text);
         font:15px/1.5 -apple-system, "Segoe UI", system-ui, sans-serif; }
  main { max-width: 880px; margin: 0 auto; padding: 24px 16px 80px; }
  h1 { font-size: 20px; } h2 { font-size: 15px; margin: 0 0 12px; color: var(--dim);
       text-transform: uppercase; letter-spacing: .06em; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:10px;
          padding:16px; margin-bottom:16px; }
  button { background:#238636; color:#fff; border:0; border-radius:6px;
           padding:8px 18px; font-size:15px; cursor:pointer; }
  button.secondary { background:#30363d; }
  button.danger { background:#b62324; }
  button:disabled { opacity:.45; cursor:default; }
  input[type=text], input[type=password], select, textarea {
    width:100%; background:#0d1117; color:var(--text); border:1px solid var(--line);
    border-radius:6px; padding:7px 9px; font-size:14px; }
  textarea { font-family: ui-monospace, Menlo, Consolas, monospace; min-height:200px; }
  label { display:block; margin:10px 0 4px; color:var(--dim); font-size:13px; }
  .row { display:flex; gap:12px; flex-wrap:wrap; align-items:center; }
  .row > * { flex:1; min-width:180px; }
  .pill { display:inline-block; padding:2px 10px; border-radius:999px; font-size:13px; }
  .pill.ok { background:#1f3326; color:var(--accent); }
  .pill.off { background:#33231f; color:var(--warn); }
  .pill.err { background:#3a1d1f; color:var(--err); }
  table { width:100%; border-collapse:collapse; }
  td, th { padding:6px 8px; border-bottom:1px solid var(--line); text-align:left; }
  a { color:#58a6ff; word-break:break-all; }
  #meterwrap { height:18px; background:#0d1117; border:1px solid var(--line);
               border-radius:6px; overflow:hidden; position:relative; }
  #meterbar { height:100%; width:0%; background:linear-gradient(90deg,#238636 70%,#d29922 88%,#f85149); }
  #logs { background:#0d1117; border:1px solid var(--line); border-radius:6px;
          font:12px/1.5 ui-monospace, Menlo, Consolas, monospace; padding:8px;
          height:200px; overflow-y:auto; white-space:pre-wrap; }
  iframe { width:100%; height:420px; border:1px solid var(--line); border-radius:10px;
           background:#fff; }
  .msg { font-size:13px; margin-top:8px; min-height:18px; }
  .msg.ok { color:var(--accent); } .msg.err { color:var(--err); }
  .copy { background:#30363d; padding:2px 10px; font-size:12px; }
</style>
</head>
<body>
<main>
  <h1>LiveTranslate <span id="status" class="pill off">stopped</span></h1>

  <section class="card" id="launch-card">
    <h2>Server</h2>
    <div class="row">
      <button id="btn-start">Start server</button>
      <button id="btn-stop" class="danger" disabled>Stop server</button>
      <span id="launch-info" style="color:var(--dim)"></span>
    </div>
    <div class="msg" id="launch-msg"></div>
  </section>

  <section class="card">
    <h2>Audience links</h2>
    <div style="color:var(--dim); font-size:13px; margin-bottom:8px;">
      Share these on the venue Wi-Fi. LAN IP: <strong id="lan-ip">…</strong></div>
    <table id="links"></table>
  </section>

  <section class="card">
    <h2>Audio input</h2>
    <div class="row">
      <select id="devices"></select>
      <button id="btn-meter" class="secondary">Test level</button>
    </div>
    <label>Input level (RMS / peak dBFS) <span id="meter-num" style="color:var(--dim)"></span></label>
    <div id="meterwrap"><div id="meterbar"></div></div>
    <div class="msg" id="audio-msg"></div>
  </section>

  <section class="card">
    <h2>Configuration</h2>
    <div class="row">
      <div><label>Audio device match</label><input type="text" id="cfg-device"></div>
      <div><label>Source language</label>
        <select id="cfg-srclang"><option>en</option><option>de</option></select></div>
      <div><label>ASR adapter</label>
        <select id="cfg-adapter"><option>elevenlabs</option><option>assemblyai</option></select></div>
    </div>
    <label>Target languages (comma-separated BCP-47, e.g. es, fr, de, pt)</label>
    <input type="text" id="cfg-targets">
    <details style="margin-top:12px;">
      <summary style="color:var(--dim); cursor:pointer;">Advanced: edit raw config.toml</summary>
      <textarea id="cfg-raw" spellcheck="false"></textarea>
      <div style="margin-top:8px;"><button id="btn-save-raw" class="secondary">Save raw TOML</button></div>
    </details>
    <div style="margin-top:12px;"><button id="btn-save-cfg">Save configuration</button></div>
    <div class="msg" id="cfg-msg"></div>
  </section>

  <section class="card">
    <h2>Glossary</h2>
    <textarea id="glossary" spellcheck="false"></textarea>
    <div class="row" style="margin-top:8px;">
      <button id="btn-save-glossary">Save glossary</button>
      <span id="glossary-counts" style="color:var(--dim)"></span>
    </div>
    <div class="msg" id="glossary-msg"></div>
  </section>

  <section class="card">
    <h2>API keys</h2>
    <div id="keys"></div>
    <div style="margin-top:12px;"><button id="btn-save-keys">Save keys</button></div>
    <div class="msg" id="keys-msg"></div>
  </section>

  <section class="card">
    <h2>Pipeline log</h2>
    <div id="logs"></div>
  </section>

  <section class="card" id="console-card" style="display:none;">
    <h2>Operator console</h2>
    <iframe id="console" src="about:blank" title="Operator console"></iframe>
  </section>
</main>
<script src="/app.js"></script>
</body>
</html>
```

- [ ] **Step 4: Create `src/livetranslate/control/static/app.js`**

```javascript
/* Control panel client: thin polling layer over the JSON API. */
const $ = (id) => document.getElementById(id);

async function api(path, payload) {
  const opts = payload === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  };
  const resp = await fetch(path, opts);
  const body = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(body.error || (body.problems || []).join("; ") || resp.status);
  return body;
}

function setMsg(id, text, ok) {
  const el = $(id);
  el.textContent = text;
  el.className = "msg " + (ok ? "ok" : "err");
  if (ok) setTimeout(() => { el.textContent = ""; }, 4000);
}

/* ---------- state poll ---------- */
let running = false, logCursor = 0, metering = false;

async function refreshState() {
  const st = await api("/api/state");
  running = st.running;
  const pill = $("status");
  if (st.running) { pill.textContent = "running"; pill.className = "pill ok"; }
  else if (st.last_exit !== null && st.last_exit !== 0) {
    pill.textContent = "exited (" + st.last_exit + ")"; pill.className = "pill err";
  } else { pill.textContent = "stopped"; pill.className = "pill off"; }

  $("btn-start").disabled = st.running;
  $("btn-stop").disabled = !st.running;
  $("launch-info").textContent = st.running ? "pid " + st.pid : "";
  $("lan-ip").textContent = st.lan_ip;

  const rows = [["Operator console", st.links.operator]].concat(
    st.links.languages.map((l) => [l.name + " (" + l.lang + ")", l.url]));
  $("links").innerHTML = rows.map(([name, url]) =>
    `<tr><td>${name}</td><td><a href="${url}" target="_blank">${url}</a></td>
     <td><button class="copy" data-url="${url}">copy</button></td></tr>`).join("");
  document.querySelectorAll(".copy").forEach((b) =>
    b.addEventListener("click", () => navigator.clipboard.writeText(b.dataset.url)));

  const consoleCard = $("console-card");
  if (st.running && consoleCard.style.display === "none") {
    consoleCard.style.display = "";
    $("console").src = "http://" + location.hostname + ":" + st.display_port + "/";
  } else if (!st.running) {
    consoleCard.style.display = "none";
    $("console").src = "about:blank";
  }

  $("keys").innerHTML = st.keys.map((k) => `
    <label>${k.name} ${k.set ? "(saved " + k.masked + ")" : "(not set)"}</label>
    <input type="password" data-key="${k.name}"
           placeholder="${k.set ? "leave blank to keep current" : "paste key"}">`).join("");
}

/* ---------- logs ---------- */
async function pollLogs() {
  const body = await api("/api/logs?after=" + logCursor);
  if (body.lines.length) {
    logCursor = body.cursor;
    const el = $("logs");
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 30;
    el.textContent += body.lines.join("\n") + "\n";
    if (atBottom) el.scrollTop = el.scrollHeight;
  }
}

/* ---------- audio ---------- */
async function refreshDevices() {
  const body = await api("/api/audio/devices");
  $("devices").innerHTML = body.devices.map((d) =>
    `<option value="${d.index}" ${d.matches ? "selected" : ""}>
       ${d.name}${d.matches ? " ✓ matches config" : ""}</option>`).join("");
}

async function toggleMeter() {
  if (metering) {
    await api("/api/meter/stop", {});
    metering = false;
    $("btn-meter").textContent = "Test level";
    $("meterbar").style.width = "0%";
    $("meter-num").textContent = "";
    return;
  }
  try {
    await api("/api/meter", { device_index: parseInt($("devices").value, 10) });
    metering = true;
    $("btn-meter").textContent = "Stop test";
    pollMeter();
  } catch (e) { setMsg("audio-msg", e.message, false); }
}

async function pollMeter() {
  if (!metering) return;
  try {
    const r = await api("/api/meter");
    const pct = Math.max(0, Math.min(100, (r.rms_dbfs + 60) / 60 * 100));
    $("meterbar").style.width = pct + "%";
    $("meter-num").textContent = r.rms_dbfs + " / " + r.peak_dbfs + " dBFS";
  } catch (e) { /* meter stopped server-side */ metering = false; }
  setTimeout(pollMeter, 150);
}

/* ---------- config ---------- */
function parseTomlValue(text, section, key) {
  // crude single-value extraction for prefilling the form; raw editor is authoritative
  const sec = text.split("[" + section + "]")[1] || "";
  const m = sec.split("[")[0].match(new RegExp(key + '\\s*=\\s*"?([^"\\n#]*)"?'));
  return m ? m[1].trim() : "";
}

async function refreshConfig() {
  const body = await api("/api/config");
  $("cfg-raw").value = body.text;
  $("cfg-device").value = parseTomlValue(body.text, "audio", "device_substring");
  $("cfg-srclang").value = parseTomlValue(body.text, "session", "source_language");
  $("cfg-adapter").value = parseTomlValue(body.text, "asr", "adapter");
  const targets = (body.text.match(/targets\s*=\s*\[([^\]]*)\]/) || [, ""])[1];
  $("cfg-targets").value = targets.replace(/["\s]/g, "");
}

async function saveConfigFields() {
  const targets = $("cfg-targets").value.split(",").map((s) => s.trim()).filter(Boolean);
  try {
    await api("/api/config", { fields: {
      "audio.device_substring": $("cfg-device").value,
      "session.source_language": $("cfg-srclang").value,
      "asr.adapter": $("cfg-adapter").value,
      "translate.targets": targets,
    }});
    setMsg("cfg-msg", "Saved.", true);
    await refreshConfig(); await refreshState(); await refreshDevices();
  } catch (e) { setMsg("cfg-msg", e.message, false); }
}

async function saveConfigRaw() {
  try {
    await api("/api/config", { text: $("cfg-raw").value });
    setMsg("cfg-msg", "Saved raw TOML.", true);
    await refreshConfig(); await refreshState(); await refreshDevices();
  } catch (e) { setMsg("cfg-msg", e.message, false); }
}

/* ---------- glossary ---------- */
async function refreshGlossary() {
  const body = await api("/api/glossary");
  $("glossary").value = body.text;
}

async function saveGlossary() {
  try {
    const r = await api("/api/glossary", { text: $("glossary").value });
    $("glossary-counts").textContent =
      r.terms + " terms; " + r.keyterms + " keyterms sent to ASR (cap 50)";
    setMsg("glossary-msg", "Saved.", true);
  } catch (e) { setMsg("glossary-msg", e.message, false); }
}

/* ---------- keys ---------- */
async function saveKeys() {
  const updates = {};
  document.querySelectorAll("#keys input").forEach((i) => {
    if (i.value) updates[i.dataset.key] = i.value;
  });
  try {
    await api("/api/keys", updates);
    setMsg("keys-msg", "Saved.", true);
    await refreshState();
  } catch (e) { setMsg("keys-msg", e.message, false); }
}

/* ---------- launch ---------- */
async function startServer() {
  try {
    await api("/api/server/start", {});
    metering = false; $("btn-meter").textContent = "Test level";
    setMsg("launch-msg", "Pipeline starting — watch the log below.", true);
  } catch (e) { setMsg("launch-msg", e.message, false); }
  await refreshState();
}

async function stopServer() {
  try { await api("/api/server/stop", {}); setMsg("launch-msg", "Stopped.", true); }
  catch (e) { setMsg("launch-msg", e.message, false); }
  await refreshState();
}

/* ---------- wiring ---------- */
$("btn-start").addEventListener("click", startServer);
$("btn-stop").addEventListener("click", stopServer);
$("btn-meter").addEventListener("click", toggleMeter);
$("btn-save-cfg").addEventListener("click", saveConfigFields);
$("btn-save-raw").addEventListener("click", saveConfigRaw);
$("btn-save-glossary").addEventListener("click", saveGlossary);
$("btn-save-keys").addEventListener("click", saveKeys);

(async function init() {
  await refreshState();
  await refreshConfig();
  await refreshGlossary();
  await refreshDevices().catch((e) => setMsg("audio-msg", e.message, false));
  setInterval(refreshState, 2000);
  setInterval(() => { if (running || logCursor > 0) pollLogs().catch(() => {}); }, 1000);
})();
```

One UX caveat to preserve: `refreshState` rebuilds the `#keys` inputs every 2 s, which would wipe a half-typed key. Guard it — in `refreshState`, wrap the `$("keys").innerHTML = ...` assignment with:

```javascript
  const typing = document.activeElement && document.activeElement.dataset &&
                 document.activeElement.dataset.key;
  if (!typing) {
    /* the existing $("keys").innerHTML = ... statement */
  }
```

- [ ] **Step 5: Run tests**

Run: `.venv/bin/python -m pytest tests/test_control_server.py -q`
Expected: 8 passed.

- [ ] **Step 6: Commit**

```bash
git add src/livetranslate/control/static/
git commit -m "feat(control): operator control panel UI"
```

---

### Task 11: __main__.py entrypoint

**Files:**
- Create: `src/livetranslate/control/__main__.py`
- Test: `tests/test_control_server.py` (append)

- [ ] **Step 1: Write the failing test** (append to `tests/test_control_server.py`)

```python
def test_main_builds_server_and_opens_browser(tmp_path, monkeypatch):
    from livetranslate.control import __main__ as main_mod
    (tmp_path / "config.toml").write_text(CONFIG)
    (tmp_path / "glossary.tsv").write_text(TSV)
    opened = []
    monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url))
    rc = main_mod.main(["--root", str(tmp_path), "--port", "0", "--smoke-test"])
    assert rc == 0
    assert opened and opened[0].startswith("http://127.0.0.1:")
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_control_server.py::test_main_builds_server_and_opens_browser -q`
Expected: FAIL — `ModuleNotFoundError` / `ImportError`.

- [ ] **Step 3: Implement**

Create `src/livetranslate/control/__main__.py`:

```python
import argparse
import logging
import webbrowser
from pathlib import Path

from .server import ControlServer, ControlState


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="livetranslate-control",
                                description="LiveTranslate operator control panel")
    p.add_argument("--root", default=".", help="project root containing config.toml")
    p.add_argument("--port", type=int, default=8766)
    p.add_argument("--no-browser", action="store_true")
    p.add_argument("--smoke-test", action="store_true",
                   help=argparse.SUPPRESS)  # start, open URL, shut down (tests)
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = Path(args.root).resolve()
    if not (root / "config.toml").exists():
        print(f"config.toml not found in {root}")
        return 2

    state = ControlState(root)
    server = ControlServer(state, host="127.0.0.1", port=args.port)
    server.start()
    url = f"http://127.0.0.1:{server.port}/"
    print(f"LiveTranslate control panel: {url}  (Ctrl-C to quit)")
    if not args.no_browser:
        webbrowser.open(url)
    if args.smoke_test:
        server.stop()
        return 0
    try:
        server.join()
    except KeyboardInterrupt:
        print("shutting down...")
        state.pipeline.stop()
        state.stop_meter()
        server.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run tests**

Run: `.venv/bin/python -m pytest tests/test_control_server.py -q`
Expected: 9 passed.

- [ ] **Step 5: Manual smoke check**

Run: `cd "/Users/sebastian/Documents/Workspace/Live Translation App" && .venv/bin/python -m livetranslate.control --no-browser & sleep 2 && curl -s http://127.0.0.1:8766/api/state | head -c 300; kill %1`
Expected: JSON with `"running": false`, `"lan_ip"`, `"links"`.

- [ ] **Step 6: Commit**

```bash
git add src/livetranslate/control/__main__.py tests/test_control_server.py
git commit -m "feat(control): entrypoint with browser auto-open"
```

---

### Task 12: Double-click launchers (Mac + Windows)

**Files:**
- Create: `Start LiveTranslate.command` (repo root)
- Create: `Start LiveTranslate.bat` (repo root)

- [ ] **Step 1: Create `Start LiveTranslate.command`**

```sh
#!/bin/zsh
# Double-clickable macOS launcher for the LiveTranslate operator control panel.
set -e
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  echo "First run: creating Python environment (one-time, ~1 minute)..."
  python3 -m venv .venv
  .venv/bin/pip install --quiet -e '.[dev]'
fi
exec .venv/bin/python -m livetranslate.control
```

- [ ] **Step 2: Make it executable**

Run: `chmod +x "Start LiveTranslate.command"`

- [ ] **Step 3: Create `Start LiveTranslate.bat`**

```bat
@echo off
rem Double-clickable Windows launcher for the LiveTranslate operator control panel.
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  echo First run: creating Python environment (one-time, ~1 minute)...
  py -3 -m venv .venv || python -m venv .venv
  .venv\Scripts\pip install --quiet -e .[dev]
)
.venv\Scripts\python -m livetranslate.control
pause
```

- [ ] **Step 4: Smoke-test the Mac launcher**

Run: `"./Start LiveTranslate.command" & sleep 3 && curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8766/ ; pkill -f "livetranslate.control"`
Expected: `200`

- [ ] **Step 5: Commit**

```bash
git add "Start LiveTranslate.command" "Start LiveTranslate.bat"
git commit -m "feat(control): double-click launchers for macOS and Windows"
```

---

### Task 13: README + final verification

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Add an "Operator control panel" section to README.md**, directly above the existing "## Live run" section:

```markdown
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
```

- [ ] **Step 2: Full test suite**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: all pass (74 pre-existing + ~25 new).

- [ ] **Step 3: End-to-end manual verification (Mac)**

1. Double-click `Start LiveTranslate.command` in Finder — browser opens the panel.
2. Audio card: select the built-in mic, click **Test level**, speak — bar moves, dBFS numbers update; click **Stop test**.
3. Config card: change `device_substring`, save, confirm `config.toml` on disk keeps its comments.
4. Glossary card: save — counts shown match the README's startup-log expectation.
5. Keys card: confirm masked values; paste a dummy key into a blank field, save, check `.env`.
6. With real keys present: **Start server** — log pane shows pipeline startup, status pill flips to *running*, the operator console iframe appears, and the audience links resolve from a phone on the same Wi-Fi.
7. **Stop server** — log shows `shutdown signal: draining...` and a clean session close.

- [ ] **Step 4: Commit**

```bash
git add README.md
git commit -m "docs: operator control panel section"
```

---

## Self-review notes

- **Spec coverage:** config editing (Tasks 3, 9, 10), glossary (4, 9, 10), API keys (2, 9, 10), device list + level meter (6, 9, 10), launch/stop server (7, 8, 9, 10), operator console (iframe, Task 10), language links on LAN IP (5, 9, 10), Mac+Windows (stdlib + sounddevice wheels; launchers Task 12; SIGBREAK Task 8).
- **Windows caveats for the implementer:** `CREATE_NEW_PROCESS_GROUP` + `CTRL_BREAK_EVENT` is the only reliable graceful-stop channel; `py -3` launcher preferred in the `.bat` with `python` fallback. The Windows path can only be fully verified on a Windows machine — everything else is covered by offline tests.
- **Open items deliberately excluded (YAGNI):** QR codes for audience links, editing `domain_blurb.txt` (rarely changes; raw TOML editor covers pointing at a different file), auth on the control panel (loopback-only binding makes it single-machine), packaging into a signed installer.
