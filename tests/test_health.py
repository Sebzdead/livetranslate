from livetranslate.health import StallDetector


def test_stall_detected_after_threshold():
    sd = StallDetector(stall_s=10)
    sd.audio_sent(ms=12000)        # 12 s of audio sent...
    assert sd.stalled() is True    # ...zero events received
    sd.event_received()
    assert sd.stalled() is False
