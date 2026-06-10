import json
from livetranslate.pipeline import Pipeline
from tests.fakes import FakeAdapter
from livetranslate.config import DEFAULTS
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
                 glossary_blocks={"es": ""}, domain_blurb="", enable_display=False)
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


def test_wedged_language_does_not_block_segmenter(tmp_path):
    # I4 (spec §3): a full translation queue must block the producer for that
    # language only, NEVER the segmenter. _emit_sentence must return promptly
    # and shed load with a warn status instead of blocking.
    import queue as _q
    import time
    from livetranslate.types import Sentence
    cfg = json.loads(json.dumps(DEFAULTS))
    cfg["session"]["output_dir"] = str(tmp_path)
    cfg["translate"].update(provider="openai_chat", base_url="http://x", model="m")
    cfg["translate"]["targets"] = ["es"]
    adapter = FakeAdapter(scripted=[])
    translator = LLMTranslator(cfg["translate"],
                               post=lambda *a: {"ok": True, "text": "X."})
    p = Pipeline(cfg, adapter=adapter, translator=translator,
                 glossary_blocks={"es": ""}, domain_blurb="", enable_display=False)
    try:
        def s(sid):
            return Sentence(sid=sid, text=f"S{sid}.", t_audio_start_ms=sid * 1000,
                            t_audio_end_ms=sid * 1000 + 900,
                            t_finalized_wall=time.monotonic())
        # Wedge the es worker: tiny queue, already full, thread never started.
        wedged = _q.Queue(maxsize=1)
        wedged.put(s(0))
        p.workers["es"].q = wedged
        t0 = time.monotonic()
        p._emit_sentence(s(1))
        elapsed = time.monotonic() - t0
        assert elapsed < 2.0, f"_emit_sentence blocked for {elapsed:.1f}s on wedged language"
        assert any(st.level == "warn" and "queue full" in st.message
                   for st in p.state.statuses), \
            f"expected a warn status about shed load, got {p.state.statuses}"
    finally:
        p.store.close()
