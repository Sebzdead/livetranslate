import argparse
import logging
import webbrowser
from pathlib import Path

from .server import ControlServer, ControlState


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="livetranslate-control",
                                description="LiveTranslate operator control panel")
    p.add_argument("--root", default=".", help="project root containing config.toml")
    p.add_argument("--port", type=int, default=8766)
    p.add_argument("--no-browser", action="store_true")
    p.add_argument("--smoke-test", action="store_true",
                   help=argparse.SUPPRESS)  # start, open URL, shut down (tests)
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = Path(args.root).resolve()
    if not (root / "config.toml").exists():
        print(f"config.toml not found in {root}")
        return 2

    state = ControlState(root)
    server = ControlServer(state, host="127.0.0.1", port=args.port)
    server.start()
    url = f"http://127.0.0.1:{server.port}/"
    print(f"LiveTranslate control panel: {url}  (Ctrl-C to quit)")
    if not args.no_browser:
        webbrowser.open(url)
    if args.smoke_test:
        server.stop()
        return 0
    try:
        server.join()
    except KeyboardInterrupt:
        print("shutting down...")
        state.pipeline.stop()
        state.stop_meter()
        server.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
