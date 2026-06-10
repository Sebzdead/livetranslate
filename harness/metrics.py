import json
import re
import unicodedata
from collections import Counter
from pathlib import Path

import jiwer


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


def _norm_text(s: str) -> str:
    s = unicodedata.normalize("NFKC", s).lower()
    s = re.sub(r"[^\w\s]", " ", s, flags=re.UNICODE)
    return " ".join(s.split())


def wer(ref: str, hyp: str) -> float:
    return jiwer.wer(_norm_text(ref), _norm_text(hyp))


def _norm_term(s: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", s).lower().replace("-", " ").split())


def jargon_recall(terms: list, ref: str, hyp: str) -> dict:
    nref, nhyp = _norm_term(ref), _norm_term(hyp)
    per_term, found_total, occ_total = {}, 0, 0
    for term in terms:
        nt = _norm_term(term)
        occurrences = nref.count(nt)
        if occurrences == 0:
            continue
        # binary per-term recall: if the term appears anywhere in hyp,
        # credit all occurrences; otherwise credit none
        found = occurrences if nhyp.count(nt) > 0 else 0
        per_term[term] = (found, occurrences)
        found_total += found
        occ_total += occurrences
    return {"overall": (found_total / occ_total) if occ_total else None,
            "per_term": per_term}


def latency_percentiles(values: list) -> dict:
    if not values:
        return {"p50": None, "p95": None}
    v = sorted(values)

    def pct(p):
        i = min(len(v) - 1, round(p / 100 * (len(v) - 1)))
        return v[i]

    return {"p50": pct(50), "p95": pct(95)}


def session_latencies(session_dir) -> dict:
    """Latency stages per spec §6 from the session JSONL."""
    session_dir = Path(session_dir)
    sentences = [json.loads(l) for l in
                 (session_dir / "sentences.jsonl").read_text().splitlines() if l.strip()]
    translations = [json.loads(l) for l in
                    (session_dir / "translations.jsonl").read_text().splitlines() if l.strip()]
    t_by = {(t["sid"], t["lang"]): t for t in translations}
    langs = {k[1] for k in t_by}
    sent_to_trans = [t_by[(s["sid"], lang)]["t_done_wall"] - s["t_finalized_wall"]
                     for s in sentences for lang in langs if (s["sid"], lang) in t_by]
    return {"sentence_to_translation": latency_percentiles(sent_to_trans)}


def write_report(session_dir, ref_path, glossary, langs) -> None:
    session_dir = Path(session_dir)
    sentences = [json.loads(l) for l in
                 (session_dir / "sentences.jsonl").read_text().splitlines() if l.strip()]
    translations = [json.loads(l) for l in
                    (session_dir / "translations.jsonl").read_text().splitlines() if l.strip()]
    report = {"invariants": check_invariants(sentences, translations, langs),
              "latency": session_latencies(session_dir)}
    hyp = " ".join(s["text"] for s in sentences)
    if ref_path:
        ref = Path(ref_path).read_text(encoding="utf-8")
        report["wer"] = wer(ref, hyp)
        report["jargon_recall"] = jargon_recall([t.src for t in glossary.terms], ref, hyp)
    gloss_ok, gloss_n = 0, 0
    t_by = {(t["sid"], t["lang"]): t for t in translations}
    for s in sentences:
        for term in glossary.terms:
            if _norm_term(term.src) in _norm_term(s["text"]):
                for lang in langs:
                    t = t_by.get((s["sid"], lang))
                    if not t or t["status"] != "ok":
                        continue
                    required = term.targets.get(lang) or term.src
                    gloss_n += 1
                    if _norm_term(required) in _norm_term(t["text"]):
                        gloss_ok += 1
    report["glossary_rendering_rate"] = (gloss_ok / gloss_n) if gloss_n else None
    (session_dir / "report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    lines = ["# Session report", "", f"WER: {report.get('wer', 'n/a')}",
             f"Jargon recall: {(report.get('jargon_recall') or {}).get('overall', 'n/a')}",
             f"Glossary rendering: {report['glossary_rendering_rate']}", ""]
    for lang in langs:
        lines += [f"## {lang}", "", "| sid | source | translation |", "|---|---|---|"]
        for s in sentences:
            t = t_by.get((s["sid"], lang))
            src_cell = s["text"].replace("|", "\\|")
            cell = (t["text"] if t else "—").replace("|", "\\|")
            lines.append(f"| {s['sid']} | {src_cell} | {cell} |")
        lines.append("")
    (session_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")
