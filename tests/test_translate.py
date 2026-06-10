import time
import pytest
from livetranslate.translate import LLMTranslator, TransContext, build_messages
from livetranslate.types import Sentence

def sent(sid=0, text="The rate of profit falls."):
    return Sentence(sid=sid, text=text, t_audio_start_ms=0, t_audio_end_ms=1000,
                    t_finalized_wall=time.monotonic())

CFG = {"provider": "openai_chat", "base_url": "http://x", "model": "m",
       "timeout_s": 10, "api_key_env": "TRANSLATE_API_KEY",
       "batch_threshold": 3, "batch_max": 6}

def test_prompt_contains_glossary_and_context():
    ctx = TransContext(prev_source=["A.", "B."], prev_target="Bee.",
                       glossary_block="rate of profit → tasa de ganancia",
                       domain_blurb="A talk about economics.")
    msgs = build_messages(sent(), "es", "Spanish", ctx)
    system, user = msgs[0]["content"], msgs[1]["content"]
    assert "tasa de ganancia" in system and "Spanish" in system
    assert "Output ONLY the translation" in system
    assert "CONTEXT (source): A. B." in user
    assert "SOURCE: The rate of profit falls." in user

def test_success_returns_ok():
    tr = LLMTranslator(CFG, post=lambda url, hdrs, body, t: {"ok": True, "text": "La tasa cae."})
    out = tr.translate(sent(), "es", TransContext.empty("g"))
    assert out.status == "ok" and out.text == "La tasa cae." and out.lang == "es"

def test_retries_then_failed_placeholder():
    calls = []
    def post(url, hdrs, body, t):
        calls.append(1); raise TimeoutError("slow")
    tr = LLMTranslator(CFG, post=post, backoff_s=0)
    out = tr.translate(sent(), "es", TransContext.empty("g"))
    assert out.status == "failed" and out.text == "⟨translation unavailable⟩"
    assert len(calls) == 3                    # 1 try + 2 retries
    assert out.attempt == 3

def test_batch_translate_splits_numbered_response():
    resp = "1. Uno.\n2. Dos.\n3. Tres."
    tr = LLMTranslator(CFG, post=lambda *a: {"ok": True, "text": resp})
    sents = [sent(i, f"S{i}.") for i in range(3)]
    outs = tr.translate_batch(sents, "es", TransContext.empty("g"))
    assert [o.text for o in outs] == ["Uno.", "Dos.", "Tres."]
    assert [o.sid for o in outs] == [0, 1, 2]

def test_batch_parse_mismatch_falls_back_per_sentence():
    calls = {"n": 0}
    def post(url, hdrs, body, t):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"ok": True, "text": "garbled single line"}   # bad batch
        return {"ok": True, "text": f"T{calls['n']}"}
    tr = LLMTranslator(CFG, post=post)
    outs = tr.translate_batch([sent(0, "A."), sent(1, "B.")], "es", TransContext.empty("g"))
    assert len(outs) == 2 and all(o.status == "ok" for o in outs)
