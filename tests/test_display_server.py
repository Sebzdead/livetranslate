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
