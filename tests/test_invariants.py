from harness.metrics import check_invariants

def test_gapless_sids_pass():
    sentences = [{"sid": 0, "text": "A."}, {"sid": 1, "text": "B."}]
    translations = [{"sid": 0, "lang": "es", "status": "ok"},
                    {"sid": 1, "lang": "es", "status": "failed"}]
    assert check_invariants(sentences, translations, langs=["es"]) == []

def test_gap_in_sids_reported():
    errs = check_invariants([{"sid": 0, "text": "A."}, {"sid": 2, "text": "B."}], [], ["es"])
    assert any("gap" in e for e in errs)

def test_duplicate_text_reported():
    errs = check_invariants([{"sid": 0, "text": "Same."}, {"sid": 1, "text": "Same."}], [], [])
    assert any("duplicate" in e for e in errs)

def test_missing_or_double_translation_reported():
    sentences = [{"sid": 0, "text": "A."}]
    errs = check_invariants(sentences, [], ["es"])
    assert any("missing" in e for e in errs)
    errs = check_invariants(sentences,
                            [{"sid": 0, "lang": "es", "status": "ok"},
                             {"sid": 0, "lang": "es", "status": "ok"}], ["es"])
    assert any("multiple" in e for e in errs)
