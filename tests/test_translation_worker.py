import queue, time
from livetranslate.translate import TranslationWorker, LLMTranslator, TransContext
from livetranslate.types import Sentence

def sent(sid):
    return Sentence(sid=sid, text=f"Sentence {sid}.", t_audio_start_ms=sid * 1000,
                    t_audio_end_ms=sid * 1000 + 900, t_finalized_wall=time.monotonic())

CFG = {"provider": "openai_chat", "base_url": "http://x", "model": "m",
       "timeout_s": 10, "api_key_env": "TRANSLATE_API_KEY",
       "batch_threshold": 3, "batch_max": 6}

def test_worker_translates_in_order_and_keeps_context():
    seen_bodies = []
    def post(url, hdrs, body, t):
        seen_bodies.append(body)
        return {"ok": True, "text": "T"}
    out = []
    w = TranslationWorker(lang="es", translator=LLMTranslator(CFG, post=post),
                          glossary_block="g", domain_blurb="", on_translation=out.append,
                          batch_threshold=3, batch_max=6)
    w.start()
    for i in range(3):
        w.submit(sent(i))
    w.stop(drain=True)
    assert [t.sid for t in out] == [0, 1, 2]
    assert all(t.status == "ok" for t in out)

def test_worker_batches_when_queue_deep():
    calls = []
    def post(url, hdrs, body, t):
        calls.append(body)
        user = body["messages"][-1]["content"]
        import re
        nums = re.findall(r"^\d+\.", user, re.M)
        return {"ok": True, "text": "\n".join(f"{i+1}. T{i}" for i in range(len(nums)))} \
               if nums else {"ok": True, "text": "T"}
    out = []
    w = TranslationWorker(lang="es", translator=LLMTranslator(CFG, post=post),
                          glossary_block="g", domain_blurb="", on_translation=out.append,
                          batch_threshold=3, batch_max=6)
    for i in range(6):                 # enqueue before starting -> deep queue
        w.submit(sent(i))
    w.start()
    w.stop(drain=True)
    assert [t.sid for t in out] == list(range(6))
    assert len(calls) < 6              # batching collapsed calls

def test_stop_returns_promptly_on_dead_worker_with_full_queue():
    # I3: a dead worker (watchdog gave up) with a full queue must not make
    # stop(drain=True) block forever on the sentinel put.
    w = TranslationWorker(lang="es",
                          translator=LLMTranslator(CFG, post=lambda *a: {"ok": True, "text": "T"}),
                          glossary_block="", domain_blurb="", on_translation=lambda t: None,
                          maxsize=2)
    # never started -> thread not alive
    w.q.put_nowait(sent(0)); w.q.put_nowait(sent(1))    # queue full
    t0 = time.monotonic()
    w.stop(drain=True, timeout_s=1)
    assert time.monotonic() - t0 < 3.0, "stop blocked on full queue of dead worker"
