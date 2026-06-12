import base64
import json
import sys
import urllib.error
import urllib.request

import pytest

from livetranslate.control import glossary_gen
from livetranslate.control.server import ControlServer, ControlState

# ---------- extract_text ----------

def test_extract_text_utf8():
    assert glossary_gen.extract_text("Comintern Tübingen".encode()) == "Comintern Tübingen"


def test_extract_text_latin1_fallback():
    assert glossary_gen.extract_text(b"caf\xe9") == "café"


def _pdf_with_text(text=None):
    from io import BytesIO
    from pypdf import PdfWriter
    from pypdf.generic import (DecodedStreamObject, DictionaryObject, NameObject)
    writer = PdfWriter()
    page = writer.add_blank_page(612, 792)
    if text is not None:
        stream = DecodedStreamObject()
        stream.set_data(f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode())
        page[NameObject("/Contents")] = writer._add_object(stream)
        page[NameObject("/Resources")] = DictionaryObject({
            NameObject("/Font"): DictionaryObject({
                NameObject("/F1"): DictionaryObject({
                    NameObject("/Type"): NameObject("/Font"),
                    NameObject("/Subtype"): NameObject("/Type1"),
                    NameObject("/BaseFont"): NameObject("/Helvetica")})})})
    buf = BytesIO()
    writer.write(buf)
    return buf.getvalue()


def test_extract_text_pdf():
    pdf = _pdf_with_text("Rosa Luxemburg")
    assert pdf[:5] == b"%PDF-"
    assert "Rosa Luxemburg" in glossary_gen.extract_text(pdf, "notes.pdf")


def test_extract_text_pdf_without_text_raises():
    with pytest.raises(ValueError, match="no extractable text"):
        glossary_gen.extract_text(_pdf_with_text(None), "scan.pdf")


# ---------- parse_reply ----------

REPLY = """Here is the glossary:
```
term_src\tes\tfr\tde\tpt\tar\tzh\tpriority\tnotes
Comintern\t\t\tKomintern\t\t\t\t1\t
rate of profit\ttasa de ganancia\ttaux de profit\t\t\t\t\t1\tverify
Munich\tMúnich\t\tMünchen\t\tميونخ\t\t9\t
```"""


def test_parse_reply_strips_fences_prose_and_offtarget_langs():
    rows = glossary_gen.parse_reply(REPLY, targets=["es", "fr", "de"])
    assert [r["term_src"] for r in rows] == ["Comintern", "rate of profit", "Munich"]
    assert rows[0]["de"] == "Komintern"
    assert rows[1]["es"] == "tasa de ganancia"
    assert rows[2]["ar"] == ""            # not a target: blanked
    assert rows[2]["priority"] == "2"     # bad priority coerced
    assert rows[1]["notes"] == "verify"


def test_parse_reply_headerless_positional():
    rows = glossary_gen.parse_reply("Comintern\t\t\tKomintern", targets=["de"])
    assert rows == [{"term_src": "Comintern", "es": "", "fr": "", "de": "Komintern",
                     "pt": "", "ar": "", "zh": "", "priority": "2", "notes": ""}]


def test_parse_reply_no_rows():
    assert glossary_gen.parse_reply("I could not find any terms.", ["es"]) == []


# ---------- merge ----------

EXISTING = ("term_src\tes\tfr\tde\tpt\tar\tzh\tpriority\tnotes\n"
            "Comintern\t\t\tKomintern\t\t\t\t1\thand-checked\n")


def test_merge_existing_rows_win_and_new_sorted():
    new = glossary_gen.parse_reply(
        "term_src\tes\tfr\tde\tpt\tar\tzh\tpriority\tnotes\n"
        "comintern\tComintern2\t\t\t\t\t\t1\t\n"        # dupe (case-insensitive)
        "Zimmerwald\t\t\t\t\t\t\t2\t\n"
        "Bolshevik\tbolchevique\t\t\t\t\t\t1\t\n", targets=["es"])
    merged, added, skipped = glossary_gen.merge(EXISTING, new)
    assert (added, skipped) == (2, 1)
    lines = merged.splitlines()
    assert lines[0].split("\t")[0] == "term_src"
    assert lines[1].startswith("Comintern\t")           # operator row intact, first
    assert "hand-checked" in lines[1]
    assert lines[2].startswith("Bolshevik\t")           # new rows: priority then term
    assert lines[3].startswith("Zimmerwald\t")


def test_merge_into_empty():
    merged, added, skipped = glossary_gen.merge(
        "", glossary_gen.parse_reply("Borodin\t\t\t\t\t\t\t1\t", ["es"]))
    assert (added, skipped) == (1, 0)
    assert merged.splitlines()[0] == "\t".join(glossary_gen.HEADER)


# ---------- call_llm ----------

def test_call_llm_rejects_non_openai_provider():
    with pytest.raises(ValueError, match="openai_chat"):
        glossary_gen.call_llm({"provider": "anthropic", "base_url": "x", "model": "m"},
                              "key", [])


def test_call_llm_request_shape():
    seen = {}

    def post(url, headers, body, timeout_s):
        seen.update(url=url, auth=headers["Authorization"], model=body["model"])
        return {"text": "term_src\tes\nX\t"}

    out = glossary_gen.call_llm(
        {"provider": "openai_chat", "base_url": "https://api.deepseek.com/",
         "model": "deepseek-v4-flash"}, "sk-123", [], post=post)
    assert seen == {"url": "https://api.deepseek.com/chat/completions",
                    "auth": "Bearer sk-123", "model": "deepseek-v4-flash"}
    assert out.startswith("term_src")


# ---------- endpoint ----------

CONFIG = """\
[session]
source_language = "en"
output_dir = "sessions"

[audio]
device_substring = "Scarlett"
chunk_ms = 100

[asr]
adapter = "elevenlabs"

[translate]
targets = ["es", "fr"]
provider = "openai_chat"
base_url = "https://api.deepseek.com"
model = "deepseek-v4-flash"
api_key_env = "TRANSLATE_API_KEY"

[glossary]
path = "glossary.tsv"

[display]
host = "0.0.0.0"
port = 8765
"""

TSV = ("term_src\tes\tfr\tde\tpt\tar\tzh\tpriority\tnotes\n"
       "Comintern\tComintern\tComintern\tKomintern\t\t\t\t1\t\n")


@pytest.fixture
def srv(tmp_path):
    (tmp_path / "config.toml").write_text(CONFIG)
    (tmp_path / "glossary.tsv").write_text(TSV)
    (tmp_path / ".env").write_text("TRANSLATE_API_KEY=tr123456\n")
    state = ControlState(tmp_path)
    server = ControlServer(state, host="127.0.0.1", port=0)
    server.start()
    yield f"http://127.0.0.1:{server.port}", state
    server.stop()


def post(base, path, payload):
    req = urllib.request.Request(base + path, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def b64(text):
    return base64.b64encode(text.encode()).decode()


def test_generate_endpoint_merges_and_reports(srv):
    base, state = srv
    state.llm_post = lambda url, headers, body, timeout_s: {"text": (
        "term_src\tes\tfr\tde\tpt\tar\tzh\tpriority\tnotes\n"
        "Zimmerwald\t\t\t\t\t\t\t1\t\n"
        "Comintern\tdupe\t\t\t\t\t\t1\t\n")}
    code, body = post(base, "/api/glossary/generate",
                      {"filename": "notes.txt", "content_b64": b64("talk notes")})
    assert code == 200
    assert (body["added"], body["skipped"]) == (1, 1)
    assert body["terms"] == 2
    assert "Zimmerwald" in body["text"]
    assert "dupe" not in body["text"]                 # operator's Comintern row won
    # nothing written to disk: review-then-save
    assert "Zimmerwald" not in (state.root / "glossary.tsv").read_text()


def test_generate_endpoint_requires_key(srv, tmp_path):
    base, state = srv
    (state.root / ".env").write_text("")
    code, body = post(base, "/api/glossary/generate",
                      {"filename": "n.txt", "content_b64": b64("x")})
    assert code == 400
    assert "API key" in body["error"]


def test_generate_endpoint_requires_content(srv):
    base, _ = srv
    code, body = post(base, "/api/glossary/generate", {"filename": "n.txt"})
    assert code == 400


def test_generate_endpoint_model_garbage_is_400(srv):
    base, state = srv
    state.llm_post = lambda *a, **kw: {"text": "no terms here, sorry"}
    code, body = post(base, "/api/glossary/generate",
                      {"filename": "n.txt", "content_b64": b64("x")})
    assert code == 400
    assert "no usable glossary rows" in body["error"]
