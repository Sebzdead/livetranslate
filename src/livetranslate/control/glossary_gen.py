"""Generate glossary.tsv rows from presenter notes via the configured translate LLM.

The operator uploads a notes document (plain text or PDF); we ask the
[translate] provider (DeepSeek by default) for new rows, merge them UNDER the
existing glossary (the operator's hand-edited rows always win), and hand the
merged TSV back for review — nothing is written to disk here.
"""
import csv
import io
import logging

import requests

from ..glossary import LANG_COLS, _norm

log = logging.getLogger(__name__)

HEADER = ["term_src"] + LANG_COLS + ["priority", "notes"]
MAX_NOTES_CHARS = 60_000   # keep the prompt well inside the model context

SYSTEM_TEMPLATE = """You extract a translation glossary from presenter notes for a live conference-interpretation pipeline. The ASR vendor boosts recognition of each source term and the translator is REQUIRED to use your target renderings, so a wrong row is worse than no row: precision over recall.

Output STRICT TSV and nothing else — no markdown, no code fences, no commentary.
First line must be exactly this tab-separated header:
term_src	es	fr	de	pt	ar	zh	priority	notes
Then one row per term, tab-separated, in the same column order.

Rules:
- Collect terms the speaker will actually say aloud: people, places, organizations, acronyms, and field-specific technical phrases. Skip everyday words, bibliography-only items, citation formatting, and page numbers.
- Fill ONLY these language columns: {targets}. Leave every other language column empty.
- An empty target cell means "keep the source term untranslated". Proper nouns usually stay untranslated — leave their cells empty — except places with established exonyms or organizations with official published names in that language.
- Technical terms: use the canonical rendering established in that language's literature of the field, never a fresh literal translation. When two renderings genuinely compete or you are unsure, leave the cell empty and write "verify" in notes.
- priority: 1 = must-recognize (the talk's core jargon, names central to the argument, unusual phonetics or spelling); 2 = nice-to-have (well-known places, common org names).
- Aim for 40-80 rows for a full talk; a short abstract may only justify 10-20.
- Do not repeat any term from the EXISTING TERMS list."""


# ---------- input extraction ----------

def extract_text(data: bytes, filename: str = "") -> str:
    """Plain-text decode, or PDF text extraction when the bytes are a PDF."""
    if data[:5] == b"%PDF-":
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        text = "\n".join((page.extract_text() or "") for page in reader.pages)
        if not text.strip():
            raise ValueError(f"no extractable text in PDF {filename or '(upload)'} "
                             "(scanned image? export it as text first)")
        return text
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("latin-1")


# ---------- LLM call ----------

def build_messages(notes: str, targets: list, existing_terms: list) -> list:
    system = SYSTEM_TEMPLATE.format(targets=", ".join(targets))
    user_lines = ["TARGET LANGUAGES: " + ", ".join(targets)]
    if existing_terms:
        user_lines.append("EXISTING TERMS (do not repeat): " + "; ".join(existing_terms))
    user_lines.append("NOTES:\n" + notes[:MAX_NOTES_CHARS])
    return [{"role": "system", "content": system},
            {"role": "user", "content": "\n\n".join(user_lines)}]


def _default_post(url, headers, body, timeout_s):
    r = requests.post(url, headers=headers, json=body, timeout=timeout_s)
    r.raise_for_status()
    return {"json": r.json()}


def call_llm(cfg, api_key: str, messages: list, post=None, timeout_s: float = 120.0) -> str:
    """OpenAI-compatible chat call (DeepSeek). `post` is injectable for tests."""
    if str(cfg.get("provider", "openai_chat")) != "openai_chat":
        raise ValueError("glossary generation requires an openai_chat-compatible "
                         "[translate] provider")
    url = str(cfg["base_url"]).rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}"}
    body = {"model": str(cfg["model"]), "messages": messages, "temperature": 0}
    resp = (post or _default_post)(url, headers, body, timeout_s)
    if "text" in resp:               # injected test transport returns text directly
        return resp["text"]
    return resp["json"]["choices"][0]["message"]["content"].strip()


# ---------- reply parsing / merging ----------

def parse_reply(text: str, targets: list) -> list:
    """Extract glossary rows from the model reply, tolerating fences and prose.

    Returns a list of HEADER-keyed dicts; languages outside `targets` are
    blanked (the prompt forbids them, but the model is not trusted).
    """
    rows = []
    header_map = None
    for line in text.splitlines():
        line = line.strip().strip("`")
        if "\t" not in line:
            continue
        cells = [c.strip() for c in line.split("\t")]
        if "term_src" in cells:
            header_map = {name: i for i, name in enumerate(cells)}
            continue
        if header_map:
            get = lambda col: cells[header_map[col]] if header_map.get(col) is not None \
                  and header_map[col] < len(cells) else ""
        else:                        # no header seen: assume canonical column order
            cells += [""] * (len(HEADER) - len(cells))
            get = lambda col: cells[HEADER.index(col)]
        term = get("term_src")
        if not term or term == "term_src":
            continue
        row = {"term_src": term}
        for lang in LANG_COLS:
            row[lang] = get(lang) if lang in targets else ""
        row["priority"] = get("priority") if get("priority") in ("1", "2") else "2"
        row["notes"] = get("notes")
        rows.append(row)
    return rows


def merge(existing_text: str, new_rows: list) -> tuple:
    """Append new rows under the existing glossary; existing term_src wins.

    Returns (merged_tsv_text, added, skipped).
    """
    existing_rows = list(csv.DictReader(existing_text.splitlines(), delimiter="\t")) \
        if existing_text.strip() else []
    seen = {_norm(r.get("term_src", "")) for r in existing_rows}
    added, skipped = [], 0
    for row in new_rows:
        key = _norm(row["term_src"])
        if key in seen:
            skipped += 1
            continue
        seen.add(key)
        added.append(row)
    added.sort(key=lambda r: (r["priority"], _norm(r["term_src"])))

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=HEADER, delimiter="\t",
                            extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    for row in existing_rows + added:
        writer.writerow({col: (row.get(col) or "").strip() for col in HEADER})
    return buf.getvalue(), len(added), skipped


def generate(notes: str, targets: list, existing_text: str, cfg, api_key: str,
             post=None) -> tuple:
    """Full pipeline: prompt → LLM → parse → merge. Returns (tsv, added, skipped)."""
    existing_terms = [r.get("term_src", "") for r in
                      csv.DictReader(existing_text.splitlines(), delimiter="\t")] \
        if existing_text.strip() else []
    messages = build_messages(notes, targets, existing_terms)
    reply = call_llm(cfg, api_key, messages, post=post)
    rows = parse_reply(reply, targets)
    if not rows:
        raise ValueError("the model returned no usable glossary rows")
    return merge(existing_text, rows)
