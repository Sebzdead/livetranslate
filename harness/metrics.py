from collections import Counter


def check_invariants(sentences: list[dict], translations: list[dict],
                     langs: list[str]) -> list[str]:
    """Spec §4 invariants. Returns a list of human-readable violations (empty = pass)."""
    errs: list[str] = []
    sids = [s["sid"] for s in sentences]
    for prev, cur in zip(sids, sids[1:]):
        if cur != prev + 1:
            errs.append(f"sid gap/regression: {prev} -> {cur}")
    texts = Counter(s["text"] for s in sentences)
    for text, n in texts.items():
        if n > 1:
            errs.append(f"duplicate sentence text x{n}: {text[:60]!r}")
    per = Counter((t["sid"], t["lang"]) for t in translations)
    for s in sentences:
        for lang in langs:
            n = per.get((s["sid"], lang), 0)
            if n == 0:
                errs.append(f"missing terminal translation: sid={s['sid']} lang={lang}")
            elif n > 1:
                errs.append(f"multiple terminal translations: sid={s['sid']} lang={lang}")
    return errs
