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


def test_run_live_registers_sigbreak_when_available(monkeypatch):
    """On Windows, CTRL_BREAK arrives as SIGBREAK; run_live must register it
    alongside SIGINT so the control panel can stop the pipeline gracefully."""
    import signal as signal_mod
    from livetranslate import runner

    if not hasattr(signal_mod, "SIGBREAK"):
        monkeypatch.setattr(signal_mod, "SIGBREAK", 21, raising=False)

    sigs = runner._shutdown_signals()
    assert signal_mod.SIGINT in sigs
    assert getattr(signal_mod, "SIGBREAK") in sigs


def test_adapter_factory_builds_speechmatics(monkeypatch, tmp_path):
    """The factory wires SPEECHMATICS_API_KEY, source language, and glossary
    keyterms (capped by additional_vocab_max) into the adapter."""
    import os
    from livetranslate import runner
    from livetranslate.glossary import Glossary, Term

    monkeypatch.setenv("SPEECHMATICS_API_KEY", "sm-test-key")
    glossary = Glossary(terms=[Term(src="Komintern", targets={}, priority=1, notes="")],
                        sha256="x")
    cfg = {
        "session": {"source_language": "de"},
        "asr": {"speechmatics": {"additional_vocab_max": 50, "max_delay": 1.0}},
        "translate": {"targets": ["es", "fr"]},
        "display": {"draft_translation": False},
    }
    make = runner._adapter_factory(cfg, "speechmatics", glossary)
    adapter = make()
    assert adapter.name == "speechmatics"
    assert adapter.api_key == "sm-test-key"
    assert adapter.language == "de"
    assert "Komintern" in adapter.additional_vocab
    assert adapter.target_languages == []   # draft_translation off in phase 1


def test_adapter_factory_speechmatics_targets_when_draft_on(monkeypatch):
    """With display.draft_translation on, the adapter receives the translate
    targets so Speechmatics emits realtime draft translations."""
    import os
    from livetranslate import runner
    from livetranslate.glossary import Glossary

    monkeypatch.setenv("SPEECHMATICS_API_KEY", "sm-test-key")
    glossary = Glossary(terms=[], sha256="x")
    cfg = {
        "session": {"source_language": "en"},
        "asr": {"speechmatics": {"additional_vocab_max": 50, "max_delay": 1.0}},
        "translate": {"targets": ["es", "fr"]},
        "display": {"draft_translation": True},
    }
    adapter = runner._adapter_factory(cfg, "speechmatics", glossary)()
    assert adapter.target_languages == ["es", "fr"]
