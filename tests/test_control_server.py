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
    data = json.dumps({} if payload is None else payload).encode()
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


def test_post_with_non_dict_body_is_400(srv):
    base, _ = srv
    status, body = post(base, "/api/keys", [])
    assert status == 400


def test_meter_start_blocked_while_pipeline_runs(srv, fake_sounddevice):
    base, state = srv
    status, _ = post(base, "/api/server/start")
    assert status == 200
    status, body = post(base, "/api/meter", {"device_index": 0})
    assert status == 409
    status, _ = post(base, "/api/server/stop")
    assert status == 200


def test_index_and_appjs_served(srv):
    base, _ = srv
    with urllib.request.urlopen(base + "/") as resp:
        assert resp.status == 200
        assert b"LiveTranslate" in resp.read()
    with urllib.request.urlopen(base + "/app.js") as resp:
        assert resp.status == 200
