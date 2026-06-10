from livetranslate.types import AudioChunk, TranscriptEvent, Sentence, Translation, StatusEvent

def test_sentence_has_paragraph_break_default_false():
    s = Sentence(sid=1, text="Hello.", t_audio_start_ms=0, t_audio_end_ms=900,
                 t_finalized_wall=1.0)
    assert s.paragraph_break is False

def test_frozen_dataclasses_are_immutable():
    c = AudioChunk(pcm16=b"\x00\x00", sample_rate=16000, t_start_ms=0, duration_ms=100, seq=0)
    import dataclasses, pytest
    with pytest.raises(dataclasses.FrozenInstanceError):
        c.seq = 1  # type: ignore[misc]

def test_translation_fields():
    t = Translation(sid=3, lang="es", text="hola", status="ok",
                    t_done_wall=2.0, model="m", attempt=1)
    assert t.status in ("ok", "failed")
