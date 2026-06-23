import json
import pathlib

from livetranslate.asr.speechmatics import SpeechmaticsRTAdapter, to_app, to_sm

FIXTURES = json.loads((pathlib.Path(__file__).parent / "fixtures" /
                       "speechmatics_messages.json").read_text())


def make_adapter(**kw):
    kw.setdefault("api_key", "test")
    kw.setdefault("language", "en")
    kw.setdefault("additional_vocab", ["Tübingen"])
    return SpeechmaticsRTAdapter(**kw)


def test_lang_code_mapping_zh_is_cmn():
    assert to_sm("zh") == "cmn" and to_sm("es") == "es"
    assert to_app("cmn") == "zh" and to_app("es") == "es"


def test_partial_normalized_with_stream_offset():
    a = make_adapter()
    a._stream_offset_ms = 5000
    ev = a._normalize(FIXTURES["partial"])
    assert ev.kind == "partial" and ev.vendor == "speechmatics"
    assert ev.text == "the first move is what sets"
    assert ev.t_audio_start_ms == 5000 + 1240   # 1.24 s -> ms, plus offset
    assert ev.vendor_raw == FIXTURES["partial"]


def test_final_normalized_seconds_to_ms():
    a = make_adapter()
    a._stream_offset_ms = 0
    ev = a._normalize(FIXTURES["final"])
    assert ev.kind == "final"
    assert ev.text == "The first move is what sets everything in motion."
    assert ev.t_audio_start_ms == 1240 and ev.t_audio_end_ms == 3550


def test_control_messages_are_not_transcripts():
    a = make_adapter()
    for key in ("recognition_started", "audio_added", "end_of_transcript",
                "warning", "error", "translation"):
        assert a._normalize(FIXTURES[key]) is None


def test_start_recognition_includes_audio_format_and_vocab():
    a = make_adapter(language="de", additional_vocab=["Profitrate", "Komintern"])
    msg = a._start_recognition()
    assert msg["message"] == "StartRecognition"
    assert msg["audio_format"] == {"type": "raw", "encoding": "pcm_s16le", "sample_rate": 16000}
    assert msg["transcription_config"]["language"] == "de"
    assert msg["transcription_config"]["enable_partials"] is True
    assert msg["transcription_config"]["additional_vocab"] == [
        {"content": "Profitrate"}, {"content": "Komintern"}]
    assert "translation_config" not in msg   # phase 1: no targets


def test_start_recognition_omits_empty_vocab():
    a = make_adapter(additional_vocab=[])
    assert "additional_vocab" not in a._start_recognition()["transcription_config"]


def test_additional_vocab_truncated_to_cap():
    a = make_adapter(additional_vocab=[f"t{i}" for i in range(60)],
                     additional_vocab_max=50)
    assert len(a.additional_vocab) == 50
