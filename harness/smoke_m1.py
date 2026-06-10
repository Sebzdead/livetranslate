import os, sys
from livetranslate.audio import FileSource
from livetranslate.asr.elevenlabs import ElevenLabsScribeAdapter

def main(path: str):
    a = ElevenLabsScribeAdapter(api_key=os.environ["ELEVENLABS_API_KEY"],
                                language="en", keyterms=[])
    a.start(on_event=lambda e: print(f"[{e.kind}] {e.t_audio_start_ms}-{e.t_audio_end_ms}ms: {e.text}"),
            on_status=lambda s: print(f"-- {s.level}: {s.message}"))
    for chunk in FileSource(path, chunk_ms=100, rtf=1.0).chunks():
        a.send_audio(chunk)
    a.flush_and_stop()

if __name__ == "__main__":
    main(sys.argv[1])
