import dataclasses
import json

from livetranslate.export import export_session_transcripts
from livetranslate.types import Sentence, Translation


def _sentence(sid, text):
    return Sentence(sid=sid, text=text, t_audio_start_ms=sid * 1000,
                    t_audio_end_ms=sid * 1000 + 500, t_finalized_wall=0.0)


def _translation(sid, lang, text, status="ok"):
    return Translation(sid=sid, lang=lang, text=text, status=status,
                       t_done_wall=0.0, model="m", attempt=1)


def _write_session(session_dir, sentences, translations):
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / "sentences.jsonl").write_text(
        "\n".join(json.dumps(dataclasses.asdict(s)) for s in sentences) + "\n",
        encoding="utf-8")
    (session_dir / "translations.jsonl").write_text(
        "".join(json.dumps(dataclasses.asdict(t)) + "\n" for t in translations),
        encoding="utf-8")


def test_writes_per_language_files_in_sid_order(tmp_path):
    sdir = tmp_path / "sessions" / "20260628-1530"
    # Deliberately out of sid order on disk; export must sort by sid.
    _write_session(sdir,
                   [_sentence(1, "Second."), _sentence(0, "First.")],
                   [_translation(0, "es", "Primero."), _translation(1, "es", "Segundo."),
                    _translation(0, "fr", "Premier."), _translation(1, "fr", "Deuxième.")])
    base = tmp_path / "transcripts"
    out = export_session_transcripts(sdir, "en", ["es", "fr"], base_dir=base)

    assert out == base / "session-1"
    assert (out / "english.txt").read_text(encoding="utf-8") == "First.\nSecond.\n"
    assert (out / "spanish.txt").read_text(encoding="utf-8") == "Primero.\nSegundo.\n"
    assert (out / "french.txt").read_text(encoding="utf-8") == "Premier.\nDeuxième.\n"


def test_missing_or_failed_translation_uses_placeholder(tmp_path):
    sdir = tmp_path / "sessions" / "s"
    _write_session(sdir,
                   [_sentence(0, "A."), _sentence(1, "B.")],
                   [_translation(0, "es", "Ah."),                       # sid 1 missing
                    _translation(1, "es", "nope", status="failed")])    # sid 1 failed
    out = export_session_transcripts(sdir, "en", ["es"], base_dir=tmp_path / "transcripts")
    assert (out / "spanish.txt").read_text(encoding="utf-8") == "Ah.\n[no translation]\n"
    # English is always complete.
    assert (out / "english.txt").read_text(encoding="utf-8") == "A.\nB.\n"


def test_source_language_naming_de(tmp_path):
    sdir = tmp_path / "sessions" / "s"
    _write_session(sdir, [_sentence(0, "Hallo.")], [])
    out = export_session_transcripts(sdir, "de", ["es"], base_dir=tmp_path / "transcripts")
    assert (out / "german.txt").read_text(encoding="utf-8") == "Hallo.\n"
    # Unknown-code target falls back to the bare code.
    assert (out / "spanish.txt").exists()


def test_unknown_target_code_falls_back_to_code(tmp_path):
    sdir = tmp_path / "sessions" / "s"
    _write_session(sdir, [_sentence(0, "Hi.")], [_translation(0, "xx", "??")])
    out = export_session_transcripts(sdir, "en", ["xx"], base_dir=tmp_path / "transcripts")
    assert (out / "xx.txt").read_text(encoding="utf-8") == "??\n"


def test_numbering_increments(tmp_path):
    base = tmp_path / "transcripts"
    sdir = tmp_path / "sessions" / "s"
    _write_session(sdir, [_sentence(0, "Hi.")], [])
    first = export_session_transcripts(sdir, "en", [], base_dir=base)
    second = export_session_transcripts(sdir, "en", [], base_dir=base)
    assert first == base / "session-1"
    assert second == base / "session-2"


def test_numbering_uses_max_plus_one_with_gaps(tmp_path):
    base = tmp_path / "transcripts"
    (base / "session-5").mkdir(parents=True)        # a gap / manual leftover
    sdir = tmp_path / "sessions" / "s"
    _write_session(sdir, [_sentence(0, "Hi.")], [])
    out = export_session_transcripts(sdir, "en", [], base_dir=base)
    assert out == base / "session-6"


def test_no_sentences_returns_none_and_writes_nothing(tmp_path):
    sdir = tmp_path / "sessions" / "s"
    _write_session(sdir, [], [])
    base = tmp_path / "transcripts"
    out = export_session_transcripts(sdir, "en", ["es"], base_dir=base)
    assert out is None
    assert not base.exists()
