import logging
import signal
import threading

from .asr.base import ResilientASR
from .audio import MicSource
from .glossary import Glossary
from .health import StallDetector, Watchdog
from .pipeline import Pipeline
from .translate import LLMTranslator

log = logging.getLogger(__name__)


def _adapter_factory(cfg, name, glossary):
    """Returns a zero-arg factory for ResilientASR (fresh adapter per (re)connect)."""
    import os
    if name == "elevenlabs":
        from .asr.elevenlabs import ElevenLabsScribeAdapter
        def make():
            return ElevenLabsScribeAdapter(
                api_key=os.environ["ELEVENLABS_API_KEY"],
                language=cfg["session"]["source_language"],
                keyterms=glossary.keyterms(cap=cfg["asr"]["elevenlabs"]["keyterms_max"]))
        return make
    if name == "assemblyai":
        from .asr.assemblyai import AssemblyAIStreamingAdapter
        def make():
            return AssemblyAIStreamingAdapter(
                api_key=os.environ["ASSEMBLYAI_API_KEY"],
                language=cfg["session"]["source_language"],
                keyterms=glossary.keyterms(cap=100),
                prompt=glossary.domain_blurb if cfg["asr"]["assemblyai"]["use_domain_prompt"] else "")
        return make
    raise SystemExit(f"unknown ASR adapter: {name!r}")


def _make_source(cfg):
    return MicSource(cfg["audio"]["device_substring"], chunk_ms=cfg["audio"]["chunk_ms"])


def _make_translator(cfg):
    return LLMTranslator(cfg["translate"])


def _shutdown_signals():
    """SIGINT everywhere; SIGBREAK too on Windows (CTRL_BREAK_EVENT from the
    control panel arrives as SIGBREAK)."""
    sigs = [signal.SIGINT]
    if hasattr(signal, "SIGBREAK"):
        sigs.append(signal.SIGBREAK)
    return sigs


def run_live(cfg, resume_dir=None) -> int:
    glossary = Glossary.load(cfg["glossary"]["path"], cfg["glossary"]["domain_blurb"])
    log.info("glossary: %d terms (hash %s); keyterms sent: %d",
             len(glossary.terms), glossary.sha256[:8],
             len(glossary.keyterms(cap=cfg["asr"]["elevenlabs"]["keyterms_max"])))

    primary = _adapter_factory(cfg, cfg["asr"]["adapter"], glossary)
    failover = (_adapter_factory(cfg, cfg["asr"]["failover"], glossary)
                if cfg["asr"]["failover"] else None)

    resilient = ResilientASR(primary, ring=None,
                             overlap_ms=cfg["asr"]["overlap_ms"],
                             give_up_after_s=cfg["asr"]["give_up_after_s"],
                             failover_factory=failover)

    stall = StallDetector(stall_s=cfg["health"]["stall_s"])
    blocks = {l: glossary.block_for(l) for l in cfg["translate"]["targets"]}
    pipe = Pipeline(cfg, adapter=resilient, translator=_make_translator(cfg),
                    glossary_blocks=blocks, domain_blurb=glossary.domain_blurb,
                    enable_display=True, resume_dir=resume_dir,
                    glossary_hash=glossary.sha256, stall_detector=stall)
    resilient.ring = pipe.ring

    watchdog = Watchdog(pipe, resilient, stall, on_status=pipe._on_status,
                        max_session_s=cfg["asr"]["max_session_s"])

    stop = threading.Event()

    def _sigint(_sig, _frm):
        log.info("shutdown signal: draining and shutting down...")
        stop.set()

    prev_handlers = {}
    for sig in _shutdown_signals():
        try:
            prev_handlers[sig] = signal.signal(sig, _sigint)
        except ValueError:
            # signal.signal only works from the main thread; ignore otherwise
            pass

    pipe.start()
    watchdog.start()
    try:
        source = _make_source(cfg)
        for chunk in source.chunks():
            if stop.is_set():
                break
            pipe.feed(chunk)
    finally:
        # spec §5.8 order: stop source (loop exited) -> flush ASR -> drain xlate -> close store
        watchdog.stop()
        pipe.shutdown()
        for sig, handler in prev_handlers.items():
            try:
                signal.signal(sig, handler)
            except ValueError:
                pass

    log.info("session closed: %s", pipe.store.session_dir)
    return 0
