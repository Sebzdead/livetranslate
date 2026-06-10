from harness.metrics import wer, jargon_recall, latency_percentiles


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
