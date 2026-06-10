from livetranslate.asr.base import ASRAdapter
from tests.fakes import FakeAdapter
from livetranslate.types import AudioChunk

def test_fake_adapter_conforms_and_emits():
    events, statuses = [], []
    a = FakeAdapter(scripted=[("partial", "hel", 0, 300), ("final", "hello.", 0, 600)])
    a.start(on_event=events.append, on_status=statuses.append)
    a.send_audio(AudioChunk(b"\x00\x00" * 1600, 16000, 0, 100, 0))
    a.flush_and_stop()
    assert [e.kind for e in events] == ["partial", "final"]
    assert events[1].text == "hello."
    assert isinstance(a, ASRAdapter)
