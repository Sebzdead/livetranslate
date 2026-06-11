import json
import tempfile
from pathlib import Path

import pytest

from harness.metrics import wer, jargon_recall, latency_percentiles, session_latencies


def test_wer_normalizes():
    assert wer("Hello, World!", "hello world") == 0.0
    assert wer("a b c d", "a b x d") == 0.25


def test_jargon_recall_hyphen_space_insensitive():
    terms = ["rate of profit", "Tübingen"]
    ref = "the rate of profit falls in Tübingen and the rate-of-profit rises"
    hyp = "the rate-of-profit falls in tubingen"   # missing diacritic = miss
    r = jargon_recall(terms, ref, hyp)
    assert r["per_term"]["rate of profit"] == (2, 2)
    assert r["per_term"]["Tübingen"] == (0, 1)
    assert r["overall"] == 2 / 3


def test_latency_percentiles():
    p = latency_percentiles([1.0, 2.0, 3.0, 4.0, 10.0])
    assert p["p50"] == 3.0 and p["p95"] >= 4.0


def test_chaos_imports():
    import harness.chaos


# ---- B2 locking test: end-to-end latency computed from feed.json ----

def _write_session(d: Path, sentences, translations, feed=None):
    (d / "sentences.jsonl").write_text(
        "\n".join(json.dumps(s) for s in sentences), encoding="utf-8")
    (d / "translations.jsonl").write_text(
        "\n".join(json.dumps(t) for t in translations), encoding="utf-8")
    if feed is not None:
        (d / "feed.json").write_text(json.dumps(feed), encoding="utf-8")


def test_audio_end_to_sentence_computed():
    """
    Synthetic session:
      feed_t0 = 100.0, rtf = 1.0
      sentence sid=0: t_audio_end_ms=1000 → audio_end_wall = 100.0 + 1.0 = 101.0
                       t_finalized_wall = 101.5
      → audio_end_to_sentence = 0.5
    """
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        sentences = [{"sid": 0, "text": "Hello.", "t_audio_start_ms": 0,
                      "t_audio_end_ms": 1000, "t_finalized_wall": 101.5}]
        translations = [{"sid": 0, "lang": "es", "text": "Hola.", "status": "ok",
                         "t_done_wall": 102.0, "model": "m", "attempt": 1}]
        feed = {"feed_t0_monotonic": 100.0, "rtf": 1.0}
        _write_session(d, sentences, translations, feed)

        result = session_latencies(d)

    assert "audio_end_to_sentence" in result
    assert result["audio_end_to_sentence"]["p50"] == pytest.approx(0.5)


def test_audio_end_to_translation_computed():
    """
    Same synthetic session:
      translation t_done_wall = 102.0, audio_end_wall = 101.0
      → audio_end_to_translation = 1.0
    """
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        sentences = [{"sid": 0, "text": "Hello.", "t_audio_start_ms": 0,
                      "t_audio_end_ms": 1000, "t_finalized_wall": 101.5}]
        translations = [{"sid": 0, "lang": "es", "text": "Hola.", "status": "ok",
                         "t_done_wall": 102.0, "model": "m", "attempt": 1}]
        feed = {"feed_t0_monotonic": 100.0, "rtf": 1.0}
        _write_session(d, sentences, translations, feed)

        result = session_latencies(d)

    assert "audio_end_to_translation" in result
    assert result["audio_end_to_translation"]["p50"] == pytest.approx(1.0)


def test_e2e_latencies_absent_without_feed_json():
    """Live sessions have no feed.json — the keys must be absent, no crash."""
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        sentences = [{"sid": 0, "text": "Hello.", "t_audio_start_ms": 0,
                      "t_audio_end_ms": 1000, "t_finalized_wall": 101.5}]
        translations = [{"sid": 0, "lang": "es", "text": "Hola.", "status": "ok",
                         "t_done_wall": 102.0, "model": "m", "attempt": 1}]
        _write_session(d, sentences, translations, feed=None)

        result = session_latencies(d)

    assert "audio_end_to_sentence" not in result
    assert "audio_end_to_translation" not in result
