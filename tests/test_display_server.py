import json, threading, time, urllib.request
import pytest
from livetranslate.display.server import DisplayServer, DisplayState
from livetranslate.types import Sentence, StatusEvent, Translation

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
    # Read enough frames; tail frames are now interleaved — filter to translation-type.
    events = read_sse_events(resp, 5)
    trans = [e for e in events if e.get("type") == "translation"]
    assert trans and trans[0]["text"] == "Hola."                  # replay
    assert any(e.get("text") == "Mundo." for e in trans)          # live

def test_last_event_id_resumes_from_sid(server):
    srv, st = server
    for i in range(3):
        st.add_sentence(Sentence(i, f"S{i}.", i * 1000, i * 1000 + 900, 1.0))
        st.add_translation(Translation(i, "es", f"T{i}.", "ok", 1.0, "m", 1))
    req = urllib.request.Request(url(srv, "/events?lang=es"),
                                 headers={"Last-Event-ID": "0"})
    # Read enough frames; tail frames interleaved — filter to translation-type.
    events = read_sse_events(urllib.request.urlopen(req), 6)
    trans = [e for e in events if e.get("type") == "translation"]
    assert [e["sid"] for e in trans] == [1, 2]


# ---- DisplayState.statuses_since unit tests (no HTTP needed) ----

def test_statuses_since_returns_all_from_zero():
    st = DisplayState(langs=["es"])
    e1 = StatusEvent(level="info", source="asr", message="connected", t_wall=1.0)
    e2 = StatusEvent(level="error", source="asr", message="lost", t_wall=2.0)
    st.add_status(e1)
    st.add_status(e2)
    items, total = st.statuses_since(0)
    assert len(items) == 2
    assert total == 2
    assert items[0].message == "connected"
    assert items[1].message == "lost"


def test_statuses_since_returns_empty_when_up_to_date():
    st = DisplayState(langs=["es"])
    st.add_status(StatusEvent(level="info", source="asr", message="ok", t_wall=1.0))
    _, total = st.statuses_since(0)
    items, total2 = st.statuses_since(total)
    assert items == []
    assert total2 == total


def test_statuses_since_survives_trim_beyond_200():
    st = DisplayState(langs=["es"])
    for i in range(250):
        st.add_status(StatusEvent(level="info", source="asr", message=f"m{i}", t_wall=float(i)))
    items, total = st.statuses_since(0)
    # Should not crash; returns at most the 200 retained items
    assert len(items) <= 200
    assert total == 250
    # Asking for the current total returns empty
    items2, total2 = st.statuses_since(total)
    assert items2 == []
    assert total2 == 250


# ---- HTTP-level locking test: status SSE streams real StatusEvents ----

def test_status_sse_streams_level_and_message(server):
    srv, st = server
    resp = urllib.request.urlopen(url(srv, "/events?lang=status"))
    # Give the SSE handler time to send the initial lag frame, then inject a status event
    threading.Timer(0.1, lambda: st.add_status(
        StatusEvent(level="error", source="asr", message="reconnecting(1)", t_wall=1.0)
    )).start()
    events = read_sse_events(resp, 3, timeout=5)
    # Filter to the status events that carry level/message (not the periodic lag frame)
    level_events = [e for e in events if e.get("type") == "status" and "level" in e]
    assert any(e["level"] == "error" and "reconnecting" in e["message"]
               for e in level_events), f"no matching level event in: {events}"


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


def test_sse_draft_frame_delivered_without_keyerror(server):
    """Regression: draft frames have no 'sid' key; the SSE handler must not raise
    KeyError when iterating snapshot_lang output that includes a draft item."""
    srv, st = server
    # Add a confirmed translation so snapshot_lang returns a real item first.
    s0 = Sentence(0, "Hello.", 0, 900, 1.0)
    st.add_sentence(s0)
    st.add_translation(Translation(0, "es", "Hola.", "ok", 1.5, "m", 1))
    # Set a live draft — this produces a {"type":"draft",...} frame with NO "sid" key.
    st.set_draft("es", "Hola en vivo…")
    # Open the audience SSE stream.
    resp = urllib.request.urlopen(url(srv, "/events?lang=es"))
    # Read a few frames; with the bug present this raises KeyError inside the handler
    # thread and the response silently breaks (no draft frame arrives).
    events = read_sse_events(resp, 5, timeout=5)
    # The confirmed translation must arrive.
    assert any(e.get("type") == "translation" and e.get("text") == "Hola."
               for e in events), f"translation frame missing in: {events}"
    # The draft frame must also arrive — this is the critical assertion that fails
    # against the old code (KeyError prevents the draft from being emitted).
    assert any(e.get("type") == "draft" and e.get("text") == "Hola en vivo…"
               for e in events), f"draft frame missing in: {events}"
