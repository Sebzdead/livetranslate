import argparse
import sys

from .config import load_config
from .logging_setup import setup_logging

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="livetranslate",
                                description="Live speech -> multilingual captions")
    p.add_argument("--config", required=True, help="path to config.toml")
    p.add_argument("--resume", metavar="SESSION_DIR",
                   help="resume a previous session directory")
    p.add_argument("--log-level", default="INFO")
    return p

def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.log_level)
    cfg = load_config(args.config)
    from .runner import run_live  # imported lazily; module added in a later task
    return run_live(cfg, resume_dir=args.resume)

if __name__ == "__main__":
    sys.exit(main())
