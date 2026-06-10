import argparse
import logging
import os
import sys

from livetranslate.audio import FileSource
from livetranslate.config import load_config
from livetranslate.glossary import Glossary
from livetranslate.logging_setup import setup_logging
from livetranslate.pipeline import Pipeline
from livetranslate.translate import LLMTranslator

log = logging.getLogger("harness")


def build_adapter(cfg: dict, name: str, glossary: Glossary):
    if name == "elevenlabs":
        from livetranslate.asr.elevenlabs import ElevenLabsScribeAdapter
        return ElevenLabsScribeAdapter(
            api_key=os.environ["ELEVENLABS_API_KEY"],
            language=cfg["session"]["source_language"],
            keyterms=glossary.keyterms(cap=cfg["asr"]["elevenlabs"]["keyterms_max"]))
    if name == "assemblyai":
        from livetranslate.asr.assemblyai import AssemblyAIStreamingAdapter
        return AssemblyAIStreamingAdapter(
            api_key=os.environ["ASSEMBLYAI_API_KEY"],
            language=cfg["session"]["source_language"],
            keyterms=glossary.keyterms(cap=100),
            prompt=glossary.domain_blurb if cfg["asr"]["assemblyai"]["use_domain_prompt"] else "")
    raise SystemExit(f"unknown adapter {name}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--audio", required=True, nargs="+")
    ap.add_argument("--ref")
    ap.add_argument("--adapter", default=None)
    ap.add_argument("--langs", default=None)
    ap.add_argument("--rtf", type=float, default=None)
    ap.add_argument("--loop", type=int, default=1)
    ap.add_argument("--no-display", action="store_true")
    args = ap.parse_args(argv)
    setup_logging()
    cfg = load_config(args.config)
    if args.langs:
        cfg["translate"]["targets"] = args.langs.split(",")
    rtf = args.rtf if args.rtf is not None else cfg["harness"]["rtf"]
    glossary = Glossary.load(cfg["glossary"]["path"], cfg["glossary"]["domain_blurb"])
    adapter = build_adapter(cfg, args.adapter or cfg["asr"]["adapter"], glossary)
    blocks = {l: glossary.block_for(l) for l in cfg["translate"]["targets"]}
    pipe = Pipeline(cfg, adapter=adapter, translator=LLMTranslator(cfg["translate"]),
                    glossary_blocks=blocks, domain_blurb=glossary.domain_blurb,
                    enable_display=not args.no_display,
                    glossary_hash=glossary.sha256)
    pipe.start()
    try:
        for _ in range(args.loop):
            for path in args.audio:
                for chunk in FileSource(path, chunk_ms=cfg["audio"]["chunk_ms"],
                                        rtf=rtf).chunks():
                    pipe.feed(chunk)
    finally:
        pipe.shutdown()
    try:
        from harness.metrics import write_report
        write_report(pipe.store.session_dir, ref_path=args.ref,
                     glossary=glossary, langs=cfg["translate"]["targets"])
    except ImportError:
        log.warning("write_report not available yet; skipping report")
    print(f"session: {pipe.store.session_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
