import json
import signal
import threading
import time
from livetranslate import runner
from livetranslate.config import DEFAULTS
from tests.fakes import FakeAdapter

def test_run_live_smoke(tmp_path, monkeypatch):
    """run_live with a fake adapter factory + fake source: starts, processes, shuts down cleanly."""
    cfg = json.loads(json.dumps(DEFAULTS))
    cfg["session"]["output_dir"] = str(tmp_path)
    cfg["translate"].update(provider="openai_chat", base_url="http://x", model="m")
    cfg["translate"]["targets"] = ["es"]
    cfg["display"]["port"] = 0   # ephemeral

    fake = FakeAdapter(scripted=[("final", "Hello live.", 0, 900)])
    monkeypatch.setattr(runner, "_adapter_factory", lambda c, n, g: (lambda: fake))

    class FakeSource:
        def chunks(self):
            from livetranslate.types import AudioChunk
            for i in range(12):
                yield AudioChunk(b"\x00" * 3200, 16000, i * 100, 100, i)
    monkeypatch.setattr(runner, "_make_source", lambda c: FakeSource())

    # use injected translator transport: patch runner's translator builder
    monkeypatch.setattr(runner, "_make_translator",
                        lambda c: __import__("livetranslate.translate", fromlist=["LLMTranslator"]
                                             ).LLMTranslator(c["translate"],
                                                             post=lambda *a: {"ok": True, "text": "X."}))

    rc = runner.run_live(cfg, resume_dir=None)
    assert rc == 0
    sdirs = list(tmp_path.iterdir())
    assert sdirs, "session dir created"
    sentences = [json.loads(l) for l in (sdirs[0] / "sentences.jsonl").read_text().splitlines()]
    assert sentences and sentences[0]["text"] == "Hello live."
