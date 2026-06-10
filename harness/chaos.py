import argparse
import json
import sys
from pathlib import Path

from harness import run_file
from harness.metrics import check_invariants


class ChaosWrapper:
    """Wraps a real adapter; severs its socket when audio crosses each cut offset."""

    def __init__(self, inner, cuts):
        # cuts is a SHARED mutable list — do not copy it
        self.inner, self.cuts = inner, cuts
        self.name = inner.name

    def start(self, on_event, on_status):
        self.inner.start(on_event, on_status)

    def send_audio(self, chunk):
        if self.cuts and chunk.t_start_ms >= self.cuts[0]:
            self.cuts.pop(0)
            try:
                self.inner._ws.sock.close()      # simulate network drop
            except Exception:                    # noqa: BLE001
                pass
        self.inner.send_audio(chunk)

    def flush_and_stop(self, timeout_s=8.0):
        self.inner.flush_and_stop(timeout_s)

    def __getattr__(self, item):
        return getattr(self.inner, item)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--audio", required=True)
    ap.add_argument("--cuts-ms", required=True,
                    help="comma-separated stream offsets, e.g. 30000,95000,150500")
    args = ap.parse_args(argv)
    all_cuts = sorted(int(x) for x in args.cuts_ms.split(","))
    shared_cuts = list(all_cuts)        # the wrapper pops this copy as it cuts
    orig = run_file.build_adapter
    run_file.build_adapter = lambda cfg, name, g: ChaosWrapper(orig(cfg, name, g), shared_cuts)
    try:
        run_file.main(["--config", args.config, "--audio", args.audio, "--no-display"])
    finally:
        run_file.build_adapter = orig
    sdir = sorted(Path("sessions").iterdir())[-1]
    sentences = [json.loads(l) for l in (sdir / "sentences.jsonl").read_text().splitlines()]
    events = [json.loads(l) for l in (sdir / "events.jsonl").read_text().splitlines()]
    reconnects = sum(1 for e in events
                     if e.get("type") == "status" and "reconnecting" in e.get("message", ""))
    errs = check_invariants(sentences, [], [])
    assert reconnects >= len(all_cuts), f"expected >= {len(all_cuts)} reconnects, saw {reconnects}"
    dupes = [e for e in errs if "duplicate" in e]
    assert not dupes, dupes
    # Guard against the gone-mute failure mode: at least one finalized sentence
    # must end AFTER the last cut, proving the pipeline kept transcribing
    # (i.e. the reconnect stream offset was applied and finals weren't deduped away).
    assert sentences, "no sentences finalized at all"
    last_end = max(s["t_audio_end_ms"] for s in sentences)
    assert last_end > max(all_cuts), (
        f"pipeline went mute after a cut: last finalized sentence ends at "
        f"{last_end}ms but the last cut was at {max(all_cuts)}ms")
    print(f"chaos OK: {reconnects} reconnects, {len(sentences)} sentences, 0 dupes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
