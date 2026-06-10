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
