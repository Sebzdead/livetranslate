#!/usr/bin/env python3
"""Merge new glossary rows into an existing livetranslate glossary.tsv.

Existing term_src rows always win (manual corrections are never overwritten).
Validates the column layout, collapses duplicates, sorts by (priority, term_src),
and warns when priority-1 terms exceed the ElevenLabs realtime keyterm budget.
"""
import argparse
import csv
import sys
import unicodedata
from pathlib import Path

COLUMNS = ["term_src", "es", "fr", "de", "pt", "ar", "zh", "priority", "notes"]
EL_KEYTERM_BUDGET = 50      # ElevenLabs realtime cap (docs/vendor-notes.md)
EL_KEYTERM_MAX_LEN = 20     # per-term char cap; longer terms aren't sent as keyterms


def norm_key(term: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", term).lower().split())


def load(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f, delimiter="\t")
        if reader.fieldnames != COLUMNS:
            sys.exit(f"{path}: bad header {reader.fieldnames!r}; expected {COLUMNS}")
        rows = []
        for i, row in enumerate(reader, start=2):
            term = (row.get("term_src") or "").strip()
            if not term:
                continue
            try:
                int(row.get("priority") or 99)
            except ValueError:
                sys.exit(f"{path}:{i}: priority must be an integer, got {row['priority']!r}")
            rows.append({c: (row.get(c) or "").strip() for c in COLUMNS})
        return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--existing", required=True, type=Path)
    ap.add_argument("--new", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    args = ap.parse_args()

    existing = load(args.existing)
    new = load(args.new)
    if not new:
        sys.exit(f"{args.new}: no rows to merge")

    seen = {norm_key(r["term_src"]) for r in existing}
    added, skipped = [], []
    for r in new:
        key = norm_key(r["term_src"])
        if key in seen:
            skipped.append(r["term_src"])
        else:
            seen.add(key)
            added.append(r)

    merged = sorted(existing + added,
                    key=lambda r: (int(r["priority"] or 99), norm_key(r["term_src"])))

    with open(args.out, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS, delimiter="\t",
                           lineterminator="\n", quoting=csv.QUOTE_NONE, quotechar=None)
        w.writeheader()
        w.writerows(merged)

    p1 = [r for r in merged if (r["priority"] or "99") == "1"]
    sendable = [r for r in p1 if len(r["term_src"]) <= EL_KEYTERM_MAX_LEN]
    print(f"merged: {len(added)} added, {len(skipped)} skipped (already present), "
          f"{len(merged)} total -> {args.out}")
    if skipped:
        print("  skipped:", ", ".join(skipped))
    print(f"priority-1 terms: {len(p1)} ({len(sendable)} sendable as ElevenLabs keyterms; "
          f"budget {EL_KEYTERM_BUDGET})")
    if len(sendable) > EL_KEYTERM_BUDGET:
        print(f"  WARNING: {len(sendable) - EL_KEYTERM_BUDGET} priority-1 keyterms over "
              f"budget; they will be truncated at startup — demote less critical terms to priority 2")
    too_long = [r["term_src"] for r in p1 if len(r["term_src"]) > EL_KEYTERM_MAX_LEN]
    if too_long:
        print(f"  note: {len(too_long)} priority-1 terms exceed {EL_KEYTERM_MAX_LEN} chars and "
              f"won't be sent as keyterms (still used by the MT glossary): {', '.join(too_long[:5])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
