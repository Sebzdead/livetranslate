import pytest
from livetranslate.config import load_config

MINIMAL = b"""
[session]
source_language = "en"
[translate]
targets = ["es", "fr"]
"""

def test_defaults_applied(tmp_path):
    p = tmp_path / "c.toml"; p.write_bytes(MINIMAL)
    cfg = load_config(p)
    assert cfg["audio"]["chunk_ms"] == 100
    assert cfg["audio"]["ring_seconds"] == 120
    assert cfg["segmenter"]["max_words"] == 45
    assert cfg["segmenter"]["max_pending_s"] == 12
    assert cfg["asr"]["overlap_ms"] == 2000
    assert cfg["health"]["stall_s"] == 10
    assert cfg["harness"]["rtf"] == 1.0
    assert cfg["display"]["port"] == 8080
    assert cfg["translate"]["targets"] == ["es", "fr"]

def test_chunk_ms_range_validated(tmp_path):
    p = tmp_path / "c.toml"
    p.write_bytes(MINIMAL + b"\n[audio]\nchunk_ms = 500\n")
    with pytest.raises(ValueError, match="chunk_ms"):
        load_config(p)

def test_unknown_target_lang_rejected(tmp_path):
    p = tmp_path / "c.toml"
    p.write_bytes(b'[session]\nsource_language="en"\n[translate]\ntargets=["xx"]\n')
    with pytest.raises(ValueError, match="target"):
        load_config(p)

def test_no_secrets_in_config(tmp_path):
    p = tmp_path / "c.toml"
    p.write_bytes(MINIMAL + b'\n[asr.elevenlabs]\napi_key = "sk-123"\n')
    with pytest.raises(ValueError, match="secret"):
        load_config(p)

def test_config_accepts_speechmatics_adapter(tmp_path):
    from livetranslate.config import load_config
    p = tmp_path / "config.toml"
    p.write_text(
        '[session]\nsource_language = "en"\n'
        '[asr]\nadapter = "speechmatics"\nfailover = "elevenlabs"\n'
        '[translate]\ntargets = ["es"]\nprovider = "openai_chat"\n'
    )
    cfg = load_config(p)
    assert cfg["asr"]["adapter"] == "speechmatics"
    assert cfg["asr"]["speechmatics"]["additional_vocab_max"] == 50
    assert cfg["asr"]["speechmatics"]["max_delay"] == 1.0


def test_speechmatics_draft_rejects_more_than_five_targets(tmp_path):
    from livetranslate.config import load_config
    p = tmp_path / "config.toml"
    p.write_text(
        '[session]\nsource_language = "en"\n'
        '[asr]\nadapter = "speechmatics"\n'
        '[translate]\ntargets = ["es", "fr", "de", "pt", "ar", "zh"]\n'
        'provider = "openai_chat"\n'
        '[display]\ndraft_translation = true\n'
    )
    import pytest
    with pytest.raises(ValueError, match="at most 5 target"):
        load_config(p)


def test_speechmatics_draft_rejects_unsupported_target_lang(tmp_path):
    # Live-validated 2026-06-25: Speechmatics RT rejects `ar` as a translation
    # target from `en` with a protocol_error that kills the whole session.
    # Guard it so a misconfig fails at load, not mid-event.
    from livetranslate.config import load_config
    p = tmp_path / "config.toml"
    p.write_text(
        '[session]\nsource_language = "en"\n'
        '[asr]\nadapter = "speechmatics"\n'
        '[translate]\ntargets = ["es", "ar"]\n'
        'provider = "openai_chat"\n'
        '[display]\ndraft_translation = true\n'
    )
    import pytest
    with pytest.raises(ValueError, match="ar"):
        load_config(p)


def test_speechmatics_draft_allows_unsupported_target_when_draft_off(tmp_path):
    # `ar` is fine for the LLM translator path; the guard only applies to the
    # Speechmatics draft-translation path.
    from livetranslate.config import load_config
    p = tmp_path / "config.toml"
    p.write_text(
        '[session]\nsource_language = "en"\n'
        '[asr]\nadapter = "speechmatics"\n'
        '[translate]\ntargets = ["es", "ar"]\n'
        'provider = "openai_chat"\n'
        '[display]\ndraft_translation = false\n'
    )
    cfg = load_config(p)
    assert cfg["translate"]["targets"] == ["es", "ar"]


def test_six_targets_allowed_when_draft_translation_off(tmp_path):
    # The 5-cap only applies to Speechmatics' draft path; the LLM translator has
    # no such limit, so 6 targets are fine when draft_translation is off.
    from livetranslate.config import load_config
    p = tmp_path / "config.toml"
    p.write_text(
        '[session]\nsource_language = "en"\n'
        '[asr]\nadapter = "speechmatics"\n'
        '[translate]\ntargets = ["es", "fr", "de", "pt", "ar", "zh"]\n'
        'provider = "openai_chat"\n'
        '[display]\ndraft_translation = false\n'
    )
    cfg = load_config(p)
    assert len(cfg["translate"]["targets"]) == 6


def test_six_targets_allowed_for_non_speechmatics_adapter(tmp_path):
    from livetranslate.config import load_config
    p = tmp_path / "config.toml"
    p.write_text(
        '[session]\nsource_language = "en"\n'
        '[asr]\nadapter = "elevenlabs"\n'
        '[translate]\ntargets = ["es", "fr", "de", "pt", "ar", "zh"]\n'
        'provider = "openai_chat"\n'
        '[display]\ndraft_translation = true\n'
    )
    cfg = load_config(p)
    assert len(cfg["translate"]["targets"]) == 6
