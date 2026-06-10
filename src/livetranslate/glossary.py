import csv
import hashlib
import logging
import unicodedata
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)
LANG_COLS = ["es", "fr", "de", "pt", "ar", "zh"]

@dataclass(frozen=True)
class Term:
    src: str
    targets: dict          # lang -> rendering ("" = keep source untranslated)
    priority: int
    notes: str

class Glossary:
    def __init__(self, terms: list[Term], sha256: str, domain_blurb: str = ""):
        self.terms, self.sha256, self.domain_blurb = terms, sha256, domain_blurb

    @classmethod
    def load(cls, path, blurb_path=None) -> "Glossary":
        raw = Path(path).read_bytes()
        terms = []
        rows = csv.DictReader(raw.decode("utf-8").splitlines(), delimiter="\t")
        for row in rows:
            terms.append(Term(src=row["term_src"].strip(),
                              targets={l: (row.get(l) or "").strip() for l in LANG_COLS},
                              priority=int(row.get("priority") or 99),
                              notes=(row.get("notes") or "").strip()))
        blurb = ""
        if blurb_path and Path(blurb_path).exists():
            blurb = Path(blurb_path).read_text(encoding="utf-8").strip()
        return cls(terms, hashlib.sha256(raw).hexdigest(), blurb)

    def keyterms(self, cap: int) -> list[str]:
        ordered = sorted(self.terms, key=lambda t: (t.priority, -len(t.src)))
        out = [t.src for t in ordered]
        if len(out) > cap:
            log.warning("glossary: keyterm list truncated %d -> %d", len(out), cap)
            out = out[:cap]
        return out

    def block_for(self, lang: str) -> str:
        lines = []
        for t in self.terms:
            target = t.targets.get(lang, "") or t.src   # empty cell = keep source
            lines.append(f"{t.src} → {target}")
        return "\n".join(lines)

    def normalized_terms(self) -> set[str]:
        out = set()
        for t in self.terms:
            out.add(_norm(t.src))
            for v in t.targets.values():
                if v:
                    out.add(_norm(v))
        return out

def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s).lower()
    return " ".join(s.replace("-", " ").split())
