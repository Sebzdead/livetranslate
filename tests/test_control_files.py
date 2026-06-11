import pytest

from livetranslate.control import files


def test_read_env_parses_and_strips_quotes(tmp_path):
    p = tmp_path / ".env"
    p.write_text('# comment\n\nELEVENLABS_API_KEY="abc123"\nTRANSLATE_API_KEY=xyz789\n')
    env = files.read_env(p)
    assert env == {"ELEVENLABS_API_KEY": "abc123", "TRANSLATE_API_KEY": "xyz789"}


def test_read_env_missing_file_returns_empty(tmp_path):
    assert files.read_env(tmp_path / "nope.env") == {}


def test_write_env_keys_updates_in_place_preserving_comments(tmp_path):
    p = tmp_path / ".env"
    p.write_text("# keep me\nELEVENLABS_API_KEY=old\nOTHER=untouched\n")
    files.write_env_keys(p, {"ELEVENLABS_API_KEY": "new", "TRANSLATE_API_KEY": "added"})
    text = p.read_text()
    assert "# keep me" in text
    assert "ELEVENLABS_API_KEY=new" in text
    assert "OTHER=untouched" in text
    assert "TRANSLATE_API_KEY=added" in text
    assert "old" not in text


def test_write_env_keys_skips_empty_values(tmp_path):
    p = tmp_path / ".env"
    p.write_text("ELEVENLABS_API_KEY=keepme\n")
    files.write_env_keys(p, {"ELEVENLABS_API_KEY": ""})
    assert "keepme" in p.read_text()


def test_write_env_keys_creates_file(tmp_path):
    p = tmp_path / ".env"
    files.write_env_keys(p, {"TRANSLATE_API_KEY": "abc"})
    assert files.read_env(p) == {"TRANSLATE_API_KEY": "abc"}


def test_mask_shows_only_last_four():
    assert files.mask("sk-1234567890abcd") == "…abcd"
    assert files.mask("abc") == "…"
    assert files.mask("") == ""


def test_read_env_handles_export_prefix_and_inline_comments(tmp_path):
    p = tmp_path / ".env"
    p.write_text('export ELEVENLABS_API_KEY="abc123"\nTRANSLATE_API_KEY=xyz789  # prod key\n')
    env = files.read_env(p)
    assert env == {"ELEVENLABS_API_KEY": "abc123", "TRANSLATE_API_KEY": "xyz789"}


def test_write_env_keys_updates_export_line_without_duplicating(tmp_path):
    p = tmp_path / ".env"
    p.write_text("export ELEVENLABS_API_KEY=old\n")
    files.write_env_keys(p, {"ELEVENLABS_API_KEY": "new"})
    text = p.read_text()
    assert text.count("ELEVENLABS_API_KEY") == 1
    assert files.read_env(p) == {"ELEVENLABS_API_KEY": "new"}


def test_read_env_quoted_value_with_trailing_comment(tmp_path):
    p = tmp_path / ".env"
    p.write_text('A="val" # comment\nB=\'v # x\'\nC=  # commented out\nD=plain#nothash\n')
    env = files.read_env(p)
    assert env["A"] == "val"
    assert env["B"] == "v # x"
    assert env["C"] == ""
    assert env["D"] == "plain#nothash"


GOOD_TOML = """\
[session]
source_language = "en"   # pinned
output_dir = "sessions"

[audio]
device_substring = "Scarlett"
chunk_ms = 100

[asr]
adapter = "elevenlabs"

[translate]
targets = ["es", "fr"]

[glossary]
path = "glossary.tsv"

[display]
host = "0.0.0.0"
port = 8765
"""


def test_validate_config_accepts_good_toml():
    assert files.validate_config_text(GOOD_TOML) == []


def test_validate_config_rejects_syntax_error():
    problems = files.validate_config_text("[session\nbroken")
    assert len(problems) == 1 and "TOML" in problems[0]


def test_validate_config_rejects_missing_section():
    problems = files.validate_config_text("[session]\nsource_language='en'\n")
    assert any("missing [audio]" in p for p in problems)


def test_validate_config_rejects_bad_adapter_and_port():
    bad = GOOD_TOML.replace('adapter = "elevenlabs"', 'adapter = "whisper"')
    bad = bad.replace("port = 8765", "port = 99999")
    problems = files.validate_config_text(bad)
    assert any("adapter" in p for p in problems)
    assert any("port" in p for p in problems)


def test_write_config_text_rejects_invalid(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(GOOD_TOML)
    with pytest.raises(ValueError):
        files.write_config_text(p, "[broken")
    assert p.read_text() == GOOD_TOML  # untouched


def test_update_config_fields_preserves_comments(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(GOOD_TOML)
    files.update_config_fields(p, {"audio.device_substring": "USB Audio",
                                   "translate.targets": ["es", "fr", "de"]})
    text = p.read_text()
    assert "# pinned" in text                      # comment survived
    assert 'device_substring = "USB Audio"' in text
    assert '"de"' in text
