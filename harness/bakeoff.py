import argparse
import csv
import json
import sys
from pathlib import Path

from harness import run_file

DECISION_NOTE = """Decision rule (spec §8): prefer ElevenLabs unless AssemblyAI wins
jargon recall by >= 3 points or WER by >= 1.5 points absolute, or recordings show
heavy DE/EN code-switching that ElevenLabs visibly fumbles."""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--audio", required=True)
    ap.add_argument("--ref", default=None,
                    help="verbatim reference transcript for WER/jargon recall; "
                         "omit on a partial slice where it would not align")
    ap.add_argument("--langs", default="es,fr,de,pt")
    ap.add_argument("--adapters", default="speechmatics,elevenlabs",
                    help="comma-separated adapters to compare")
    ap.add_argument("--rtf", type=float, default=None,
                    help="real-time factor for feeding; >1 feeds faster than "
                         "real time (fine for WER/jargon; distorts latency)")
    args = ap.parse_args(argv)
    rows = []
    for adapter in [a for a in args.adapters.split(",") if a]:
        run_argv = ["--config", args.config, "--audio", args.audio,
                    "--adapter", adapter, "--langs", args.langs, "--no-display"]
        if args.ref:
            run_argv += ["--ref", args.ref]
        if args.rtf is not None:
            run_argv += ["--rtf", str(args.rtf)]
        run_file.main(run_argv)
        sdir = sorted(Path("sessions").iterdir())[-1]
        rep = json.loads((sdir / "report.json").read_text())
        events = [json.loads(l) for l in (sdir / "events.jsonl").read_text().splitlines()]
        reconnects = sum(1 for e in events if e.get("type") == "status"
                         and "reconnecting" in e.get("message", ""))
        rows.append({"adapter": adapter, "wer": rep.get("wer"),
                     "jargon_recall": (rep.get("jargon_recall") or {}).get("overall"),
                     "lat_p50": rep["latency"]["sentence_to_translation"]["p50"],
                     "lat_p95": rep["latency"]["sentence_to_translation"]["p95"],
                     "reconnects": reconnects, "session": str(sdir)})
    with open("bakeoff.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=rows[0].keys())
        w.writeheader()
        w.writerows(rows)
    print("| adapter | WER | jargon recall | lat p50 | lat p95 | reconnects |")
    print("|---|---|---|---|---|---|")
    for r in rows:
        print(f"| {r['adapter']} | {r['wer']} | {r['jargon_recall']} "
              f"| {r['lat_p50']} | {r['lat_p95']} | {r['reconnects']} |")
    print("\n" + DECISION_NOTE)
    return 0


if __name__ == "__main__":
    sys.exit(main())
