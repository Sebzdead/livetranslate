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


def test_audio_frame_is_tagged_protocol_message():
    # Verified live 2026-06-10: untagged frames are rejected with input_error.
    a = make_adapter()
    frame = a._audio_frame(b"\x00\x02", commit=False)
    assert frame["message_type"] == "input_audio_chunk"
    assert set(frame) == {"message_type", "audio_base_64", "commit", "sample_rate"}
    assert frame["commit"] is False and frame["sample_rate"] == 16000


def test_unrecognized_vendor_message_surfaces_as_status():
    a = make_adapter()
    statuses = []
    a.on_status = statuses.append
    a.on_event = lambda e: None

    class OneShotWS:
        def __init__(self):
            self.sent = False
        def recv(self):
            if self.sent:
                raise ConnectionError("done")
            self.sent = True
            return '{"message_type": "input_error", "error": "Message must be a valid protocol message"}'

    a._ws = OneShotWS()
    a._recv_loop()
    assert any("input_error" in s.message for s in statuses)
    assert all(s.level != "error" for s in statuses if "input_error" in s.message)


def test_bare_committed_transcript_ignored():
    # Verified live 2026-06-10: commits arrive both bare and with timestamps;
    # only the timestamped variant may become a final event.
    a = make_adapter()
    bare = {"message_type": "committed_transcript", "text": "Hello."}
    assert a._normalize(bare) is None
