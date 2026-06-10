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
    assert (0, "es") in {(t.sid, t.lang) for t in translations}
    assert next_sid == 2
