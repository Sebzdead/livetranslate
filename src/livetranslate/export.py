"""Human-readable transcript export (runs at session end).

The session's machine-readable JSONL (`sentences.jsonl` + `translations.jsonl`)
is the source of truth; this module renders it into one plain-text transcript
per language under `transcripts/session-N/`, leaving the operational session
directory untouched. Pure, side-effect-localized: it only reads the session dir
and writes the export dir, so it can be unit-tested in isolation.
"""
import logging
from pathlib import Path

from .store import _load_jsonl_tolerant
from .types import Sentence, Translation

log = logging.getLogger(__name__)

# Language code -> human-readable filename stem. Unknown codes fall back to the
# bare code (e.g. an exotic target becomes "<code>.txt").
_LANG_NAMES = {
    "en": "english", "de": "german", "es": "spanish", "fr": "french",
    "pt": "portuguese", "ar": "arabic", "zh": "chinese",
}

# Written when a sentence has no successful translation for a language, so every
# language file stays line-aligned by sentence and nothing is silently dropped.
NO_TRANSLATION = "[no translation]"


def _lang_filename(code: str) -> str:
    return f"{_LANG_NAMES.get(code, code)}.txt"


def _next_session_dir(base_dir: Path) -> Path:
    """`session-N` where N = max(existing N) + 1 (1 if none). Using max+1 rather
    than a count keeps numbering monotonic across manual deletions/gaps."""
    nums = []
    if base_dir.exists():
        for p in base_dir.iterdir():
            if p.is_dir() and p.name.startswith("session-"):
                suffix = p.name[len("session-"):]
                if suffix.isdigit():
                    nums.append(int(suffix))
    return base_dir / f"session-{(max(nums) + 1) if nums else 1}"


def _write_lines(path: Path, lines: list[str]) -> None:
    path.write_text("".join(line + "\n" for line in lines), encoding="utf-8")


def export_session_transcripts(session_dir, source_lang: str, targets: list[str],
                               *, base_dir) -> Path | None:
    """Render the finalized session transcript to `base_dir/session-N/`.

    Writes `<source-language>.txt` (the original transcript) plus one file per
    target language, each one sentence per line in `sid` order. Returns the
    export directory, or None if the session produced no sentences.
    """
    session_dir = Path(session_dir)
    sentences: list[Sentence] = _load_jsonl_tolerant(
        session_dir / "sentences.jsonl", Sentence)
    if not sentences:
        log.info("export: no sentences in %s; skipping transcript export", session_dir)
        return None
    sentences.sort(key=lambda s: s.sid)

    translations: list[Translation] = _load_jsonl_tolerant(
        session_dir / "translations.jsonl", Translation)
    ok_text = {(t.sid, t.lang): t.text for t in translations if t.status == "ok"}

    out_dir = _next_session_dir(Path(base_dir))
    out_dir.mkdir(parents=True, exist_ok=True)

    _write_lines(out_dir / _lang_filename(source_lang), [s.text for s in sentences])
    for lang in targets:
        _write_lines(out_dir / _lang_filename(lang),
                     [ok_text.get((s.sid, lang), NO_TRANSLATION) for s in sentences])

    log.info("export: wrote %d sentences in %d language(s) to %s",
             len(sentences), 1 + len(targets), out_dir)
    return out_dir
