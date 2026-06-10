import json
import pathlib

from livetranslate.asr.assemblyai import AssemblyAIStreamingAdapter

FIXTURES = json.loads((pathlib.Path(__file__).parent / "fixtures" /
                       "assemblyai_messages.json").read_text())


def make_adapter():
    return AssemblyAIStreamingAdapter(api_key="test", language="en",
                                      keyterms=["Tübingen"], prompt="econ talk")


def test_partial_normalized_with_stream_offset():
    a = make_adapter()
    a._stream_offset_ms = 5000
    ev = a._normalize(FIXTURES["partial"])
    assert ev.kind == "partial" and ev.vendor == "assemblyai"
    assert ev.t_audio_start_ms >= 5000
    assert ev.vendor_raw == FIXTURES["partial"]


def test_final_normalized_ms_passthrough():
    a = make_adapter()
    a._stream_offset_ms = 0
    ev = a._normalize(FIXTURES["final"])
    assert ev.kind == "final" and ev.text.strip()
    assert ev.t_audio_end_ms > ev.t_audio_start_ms
    # ms passthrough sanity: fixture words are 1240..3550 ms
    assert 1000 <= ev.t_audio_start_ms <= 2000
    assert 3000 <= ev.t_audio_end_ms <= 4000


def test_non_transcript_messages_return_none():
    a = make_adapter()
    assert a._normalize(FIXTURES["config_ack"]) is None


def test_keyterms_truncated_to_cap():
    a = AssemblyAIStreamingAdapter(api_key="t", language="en",
                                   keyterms=[f"term{i}" for i in range(150)], prompt="")
    assert len(a.keyterms) == 100


def test_bakeoff_imports():
    import harness.bakeoff  # noqa: F401
