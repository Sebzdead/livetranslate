import shutil, subprocess, sys
import pytest
from livetranslate.audio import RingBuffer, FileSource
from livetranslate.types import AudioChunk

def chunk(t_ms, seq, dur=100, fill=b"\x01\x02"):
    return AudioChunk(pcm16=fill * (16 * dur), sample_rate=16000,
                      t_start_ms=t_ms, duration_ms=dur, seq=seq)

def test_ring_replay_from_returns_audio_from_ms():
    rb = RingBuffer(seconds=2, sample_rate=16000)
    for i in range(10):                       # 1 s of audio in 100 ms chunks
        rb.append(chunk(i * 100, i))
    out = list(rb.replay_from(300))
    assert out[0].t_start_ms == 300
    assert sum(c.duration_ms for c in out) == 700

def test_ring_evicts_old_audio():
    rb = RingBuffer(seconds=1, sample_rate=16000)
    for i in range(30):                       # 3 s into a 1 s ring
        rb.append(chunk(i * 100, i))
    with pytest.raises(KeyError):             # too old: evicted
        list(rb.replay_from(0))
    out = list(rb.replay_from(2500))
    assert out[0].t_start_ms == 2500

@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_filesource_decodes_and_paces(tmp_path):
    wav = tmp_path / "t.wav"                  # 1 s of silence @16k mono
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i",
                    "anullsrc=r=16000:cl=mono", "-t", "1", str(wav)],
                   check=True, capture_output=True)
    src = FileSource(str(wav), chunk_ms=100, rtf=20.0)   # fast for test
    chunks = list(src.chunks())
    assert sum(c.duration_ms for c in chunks) == pytest.approx(1000, abs=100)
    assert chunks[0].t_start_ms == 0 and chunks[1].seq == 1
    assert all(c.sample_rate == 16000 for c in chunks)
