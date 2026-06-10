import json
from livetranslate.pipeline import Pipeline
from livetranslate.config import DEFAULTS
from livetranslate.translate import LLMTranslator
from livetranslate.types import AudioChunk
from tests.fakes import FakeAdapter


def _cfg(tmp_path):
    cfg = json.loads(json.dumps(DEFAULTS))
    cfg["session"]["output_dir"] = str(tmp_path)
    cfg["translate"].update(provider="openai_chat", base_url="http://x", model="m")
    cfg["translate"]["targets"] = ["es"]
    return cfg


def _run(cfg, adapter, resume_dir=None):
    p = Pipeline(cfg, adapter=adapter,
                 translator=LLMTranslator(cfg["translate"],
                                          post=lambda *a: {"ok": True, "text": "X."}),
                 glossary_blocks={"es": ""}, domain_blurb="",
                 enable_display=False, resume_dir=resume_dir)
    p.start()
    return p


def test_resume_restores_state_and_continues_sids(tmp_path):
    # session 1: two sentences, then simulated crash (no clean shutdown of store writes
    # is fine because writes are line-buffered + flushed on close; here we do close cleanly
    # but do NOT 'finish' the conceptual session)
    p1 = _run(_cfg(tmp_path), FakeAdapter(scripted=[("final", "One.", 0, 900),
                                                    ("final", "Two.", 1000, 1900)]))
    for i in range(25):
        p1.feed(AudioChunk(b"\x00" * 3200, 16000, i * 100, 100, i))
    p1.shutdown()
    sdir = p1.store.session_dir

    # session 2: resume the same dir; sid continues at 2; display state rebuilt
    p2 = _run(_cfg(tmp_path), FakeAdapter(scripted=[("final", "Three.", 3000, 3900)]),
              resume_dir=str(sdir))
    assert len(p2.state.sentences) == 2          # rebuilt from JSONL
    for i in range(45):
        p2.feed(AudioChunk(b"\x00" * 3200, 16000, i * 100, 100, i))
    p2.shutdown()
    sentences = [json.loads(l) for l in
                 (sdir / "sentences.jsonl").read_text().splitlines()]
    assert [s["sid"] for s in sentences] == [0, 1, 2]    # gapless across resume
    assert sentences[2]["text"] == "Three."
