from livetranslate.glossary import Glossary

TSV = ("term_src\tes\tfr\tde\tpt\tar\tzh\tpriority\tnotes\n"
       "rate of profit\ttasa de ganancia\ttaux de profit\tProfitrate\ttaxa de lucro\t\t\t1\t\n"
       "Tübingen\tTübingen\tTübingen\tTübingen\tTübingen\t\t\t2\tkeep as-is\n"
       "long extra term here\t\t\t\t\t\t\t1\t\n")

def make(tmp_path):
    p = tmp_path / "g.tsv"; p.write_text(TSV, encoding="utf-8")
    return Glossary.load(p)

def test_keyterms_sorted_by_priority_then_length(tmp_path):
    g = make(tmp_path)
    # priority 1 first; within priority 1, longer first
    assert g.keyterms(cap=100) == ["long extra term here", "rate of profit", "Tübingen"]

def test_keyterms_truncated_with_warning(tmp_path, caplog):
    import logging
    g = make(tmp_path)
    with caplog.at_level(logging.WARNING):
        assert len(g.keyterms(cap=2)) == 2
    assert any("truncat" in r.message for r in caplog.records)

def test_glossary_block_keeps_empty_cells_as_source(tmp_path):
    g = make(tmp_path)
    block = g.block_for("es")
    assert "rate of profit → tasa de ganancia" in block
    # empty target cell => keep source term untranslated => identical rendering
    assert "long extra term here → long extra term here" in block

def test_normalized_term_set_for_metrics(tmp_path):
    g = make(tmp_path)
    assert "rate of profit" in g.normalized_terms()
    assert "tübingen" in g.normalized_terms()

def test_hash_stable(tmp_path):
    assert make(tmp_path).sha256 == make(tmp_path).sha256
