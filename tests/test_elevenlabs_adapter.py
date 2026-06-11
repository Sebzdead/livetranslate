import json, pathlib
from livetranslate.asr.elevenlabs import ElevenLabsScribeAdapter

FIXTURES = json.loads((pathlib.Path(__file__).parent / "fixtures" /
                       "elevenlabs_messages.json").read_text())

def make_adapter():
    return ElevenLabsScribeAdapter(api_key="test", language="en", keyterms=["Tübingen"])

def test_partial_normalized_with_stream_offset():
    a = make_adapter()
    a._stream_offset_ms = 5000      # session connected when stream time was 5 s
    ev = a._normalize(FIXTURES["partial"])
    assert ev.kind == "partial" and ev.vendor == "elevenlabs"
    assert ev.t_audio_start_ms >= 5000          # vendor-relative -> stream timeline
    assert ev.vendor_raw == FIXTURES["partial"]

def test_final_normalized():
    a = make_adapter()
    a._stream_offset_ms = 0
    ev = a._normalize(FIXTURES["final"])
    assert ev.kind == "final" and ev.text.strip()
    assert ev.t_audio_end_ms > ev.t_audio_start_ms   # seconds->ms conversion sane

def test_non_transcript_messages_return_none():
    a = make_adapter()
    assert a._normalize(FIXTURES["config_ack"]) is None


def test_keyterm_longer_than_20_chars_excluded():
    long_term = "a" * 21   # 21 chars — exceeds ElevenLabs realtime 20-char limit
    a = ElevenLabsScribeAdapter(api_key="test", language="en",
                                keyterms=[long_term, "short"])
    assert long_term not in a.keyterms
    assert "short" in a.keyterms
