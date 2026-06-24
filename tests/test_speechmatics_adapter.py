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


def test_recv_loop_routes_recognition_started_and_transcripts():
    a = make_adapter()
    events, statuses = [], []
    a.on_event = events.append
    a.on_status = statuses.append
    a.on_draft = None

    class ScriptWS:
        def __init__(self, msgs):
            self._msgs = list(msgs)
        def recv(self):
            if self._msgs:
                return json.dumps(self._msgs.pop(0))
            raise ConnectionError("done")

    a._ws = ScriptWS([FIXTURES["recognition_started"], FIXTURES["audio_added"],
                      FIXTURES["partial"], FIXTURES["final"],
                      FIXTURES["end_of_transcript"]])
    a._recv_loop()
    assert a._started.is_set()
    assert [e.kind for e in events] == ["partial", "final"]
    assert any("connected" in s.message for s in statuses)


def test_recv_loop_error_surfaces_as_error_status():
    a = make_adapter()
    statuses = []
    a.on_event = lambda e: None
    a.on_status = statuses.append
    a.on_draft = None

    class OneShot:
        def __init__(self, msg):
            self.msg, self.done = msg, False
        def recv(self):
            if self.done:
                raise ConnectionError("done")
            self.done = True
            return json.dumps(self.msg)

    a._ws = OneShot(FIXTURES["error"])
    a._recv_loop()
    assert any(s.level == "error" and "invalid_audio_type" in s.message for s in statuses)


def test_recv_loop_emits_draft_when_callback_present():
    a = make_adapter(target_languages=["es"])
    drafts = []
    a.on_event = lambda e: None
    a.on_status = lambda s: None
    a.on_draft = lambda lang, text: drafts.append((lang, text))

    class OneShot:
        def __init__(self, msg):
            self.msg, self.done = msg, False
        def recv(self):
            if self.done:
                raise ConnectionError("done")
            self.done = True
            return json.dumps(self.msg)

    a._ws = OneShot(FIXTURES["translation"])
    a._recv_loop()
    assert drafts == [("es", "El primer movimiento es lo que pone todo en marcha.")]


def test_recv_loop_maps_cmn_draft_to_zh():
    a = make_adapter(target_languages=["zh"])
    drafts = []
    a.on_event = lambda e: None
    a.on_status = lambda s: None
    a.on_draft = lambda lang, text: drafts.append((lang, text))

    class OneShot:
        def __init__(self, msg):
            self.msg, self.done = msg, False
        def recv(self):
            if self.done:
                raise ConnectionError("done")
            self.done = True
            return json.dumps(self.msg)

    a._ws = OneShot(FIXTURES["translation_cmn"])
    a._recv_loop()
    assert drafts and drafts[0][0] == "zh"


def test_end_of_stream_carries_last_seq_no():
    a = make_adapter()
    sent = []

    class CaptureWS:
        def send(self, data):
            sent.append(json.loads(data))
        def send_binary(self, data):
            pass

    a._ws = CaptureWS()
    a._seq = 7
    a._send_end_of_stream()
    assert sent == [{"message": "EndOfStream", "last_seq_no": 7}]


def test_recv_loop_warns_on_unknown_message():
    a = make_adapter()
    statuses = []
    a.on_event = lambda e: None
    a.on_status = statuses.append
    a.on_draft = None

    class OneShot:
        def __init__(self, msg):
            self.msg, self.done = msg, False
        def recv(self):
            if self.done:
                raise ConnectionError("done")
            self.done = True
            return json.dumps(self.msg)

    a._ws = OneShot({"message": "SomeFutureMessage", "detail": "x"})
    a._recv_loop()
    warns = [s for s in statuses if s.level == "warn"]
    assert any("SomeFutureMessage" in s.message for s in warns)


def test_recv_loop_does_not_warn_on_translation_when_drafts_disabled():
    # A translation message with on_draft=None must be silently consumed,
    # NOT treated as an unknown message.
    a = make_adapter()
    statuses = []
    a.on_event = lambda e: None
    a.on_status = statuses.append
    a.on_draft = None

    class OneShot:
        def __init__(self, msg):
            self.msg, self.done = msg, False
        def recv(self):
            if self.done:
                raise ConnectionError("done")
            self.done = True
            return json.dumps(self.msg)

    a._ws = OneShot(FIXTURES["translation"])
    a._recv_loop()
    assert not [s for s in statuses if s.level == "warn"]


def _make_chunk(seq=0):
    from livetranslate.types import AudioChunk
    return AudioChunk(pcm16=b"\x00\x00", sample_rate=16000,
                      t_start_ms=seq * 100, duration_ms=100, seq=seq)


def test_send_audio_never_blocks_and_sheds_oldest_when_full():
    # The send loop is gated on RecognitionStarted; until it arrives nothing
    # drains the queue. send_audio must never block the feed thread — on a full
    # queue it sheds the oldest chunk (ResilientASR replays from the ring on
    # reconnect). With the old blocking put() this loop would hang forever.
    a = make_adapter()
    maxsize = a._send_q.maxsize
    for i in range(maxsize + 50):       # far exceed capacity, no consumer running
        a.send_audio(_make_chunk(i))
    assert a._send_q.qsize() == maxsize  # bounded; never blocked


def test_flush_and_stop_does_not_hang_when_never_started_and_queue_full():
    # Reproduces the reconnect/shutdown deadlock: queue full + sender gated on
    # RecognitionStarted. flush_and_stop must enqueue the sentinel without
    # blocking and return promptly.
    import threading

    a = make_adapter()
    # Dummy already-finished threads so the joins in flush_and_stop return at once.
    done = threading.Thread(target=lambda: None)
    done.start()
    done.join()
    a._sender = done
    a._receiver = done
    a._ws = type("WS", (), {"close": lambda self: None})()
    for i in range(a._send_q.maxsize):  # fill to capacity
        a.send_audio(_make_chunk(i))
    a.flush_and_stop(timeout_s=1.0)     # must not hang
    assert a._stop.is_set()
