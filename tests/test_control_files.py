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
