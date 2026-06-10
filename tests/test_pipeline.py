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
