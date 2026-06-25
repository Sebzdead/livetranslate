"""Throwaway live wire-format probe for Speechmatics RT v2 (vendor-notes Task 1 Step 3).

Connects with the real SPEECHMATICS_API_KEY using the adapter's OWN StartRecognition
payload, feeds a slice of real audio, and dumps every raw server message. Validates:
  - RecognitionStarted arrives; message names match the fixture
  - metadata.transcript + timestamp UNITS (seconds float) on AddTranscript
  - AddTranslation.results[].content shape + that Chinese comes back as `cmn`
  - whether `ar` (Arabic) is accepted as a target (run with --targets ar to isolate)

Usage:
  set -a; source .env; set +a
  python -m harness.probe_speechmatics --audio recordings/<file>.m4a --targets es,zh --seconds 40
  python -m harness.probe_speechmatics --audio recordings/<file>.m4a --targets ar --seconds 25
"""
import argparse
import json
import os
import sys
import threading
import time

import websocket

from livetranslate.asr.speechmatics import SpeechmaticsRTAdapter, WS_URL, to_sm, to_app
from livetranslate.audio import FileSource


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--audio", required=True)
    ap.add_argument("--targets", default="es,zh", help="app-lang codes, comma-separated")
    ap.add_argument("--seconds", type=float, default=40.0, help="seconds of audio to feed")
    ap.add_argument("--language", default="en")
    ap.add_argument("--vocab", default="", help="comma-separated additional_vocab terms")
    args = ap.parse_args(argv)

    key = os.environ.get("SPEECHMATICS_API_KEY")
    if not key:
        print("SPEECHMATICS_API_KEY not set (did you `source .env`?)", file=sys.stderr)
        return 2

    targets = [t for t in args.targets.split(",") if t]
    vocab = [v for v in args.vocab.split(",") if v]

    # Build the EXACT StartRecognition the production adapter would send, so the
    # probe validates the real outgoing schema rather than a hand-rolled one.
    adapter = SpeechmaticsRTAdapter(
        api_key=key, language=args.language, additional_vocab=vocab,
        target_languages=targets)
    start_msg = adapter._start_recognition()
    print("=== StartRecognition (sent) ===")
    print(json.dumps(start_msg, ensure_ascii=False, indent=2))
    print(f"=== target_languages app->sm: {[(t, to_sm(t)) for t in targets]} ===\n")

    ws = websocket.create_connection(
        WS_URL, header=[f"Authorization: Bearer {key}"], timeout=15)
    ws.send(json.dumps(start_msg))

    seen_types: dict[str, dict] = {}      # message -> first raw sample
    translation_langs: set[str] = set()
    errors: list[dict] = []
    warnings: list[dict] = []
    started = threading.Event()
    done = threading.Event()

    def recv_loop():
        while not done.is_set():
            try:
                raw = ws.recv()
            except Exception as e:  # noqa: BLE001
                if not done.is_set():
                    print(f"[recv ended] {e}")
                return
            if not raw:
                continue
            try:
                msg = json.loads(raw)
            except (ValueError, TypeError):
                print(f"[non-JSON frame] {raw!r:.120}")
                continue
            m = msg.get("message", "?")
            if m not in seen_types:
                seen_types[m] = msg
                print(f"--- first {m} ---")
                print(json.dumps(msg, ensure_ascii=False)[:600])
            if m == "RecognitionStarted":
                started.set()
            elif m == "Error":
                errors.append(msg)
                print(f"!!! Error: {msg.get('type')} :: {msg.get('reason')}")
                done.set()
                return
            elif m == "Warning":
                warnings.append(msg)
                print(f"~~~ Warning: {msg.get('type')} :: {msg.get('reason')}")
            elif m in ("AddTranslation", "AddPartialTranslation"):
                translation_langs.add(msg.get("language", "?"))
            elif m == "EndOfTranscript":
                done.set()
                return

    rx = threading.Thread(target=recv_loop, daemon=True)
    rx.start()

    if not started.wait(timeout=15):
        print("!!! RecognitionStarted not received within 15s", file=sys.stderr)
        done.set()
        ws.close()
        return 1

    seq = 0
    t_feed0 = time.monotonic()
    for chunk in FileSource(args.audio, chunk_ms=100, rtf=1.0).chunks():
        if done.is_set() or (chunk.t_start_ms / 1000.0) >= args.seconds:
            break
        try:
            ws.send_binary(chunk.pcm16)
            seq += 1
        except Exception as e:  # noqa: BLE001
            print(f"[send ended] {e}")
            break
    print(f"\n[fed {seq} chunks (~{seq/10:.0f}s audio) in {time.monotonic()-t_feed0:.1f}s wall]")

    try:
        ws.send(json.dumps({"message": "EndOfStream", "last_seq_no": seq}))
    except Exception:  # noqa: BLE001
        pass

    done.wait(timeout=20)
    done.set()
    try:
        ws.close()
    except Exception:  # noqa: BLE001
        pass
    rx.join(timeout=2)

    print("\n========== SUMMARY ==========")
    print(f"message types seen: {sorted(seen_types)}")
    fin = seen_types.get("AddTranscript")
    if fin:
        meta = fin.get("metadata", {})
        print(f"AddTranscript.metadata keys: {sorted(meta)}  "
              f"start_time={meta.get('start_time')!r} end_time={meta.get('end_time')!r} "
              f"transcript={meta.get('transcript','')[:60]!r}")
    tr = seen_types.get("AddTranslation")
    if tr:
        print(f"AddTranslation keys: {sorted(tr)}  language={tr.get('language')!r} "
              f"results[0]={ (tr.get('results') or [{}])[0] }")
    print(f"translation languages returned (sm codes): {sorted(translation_langs)} "
          f"-> app: {sorted(to_app(l) for l in translation_langs)}")
    print(f"requested targets (sm): {sorted(to_sm(t) for t in targets)}")
    missing = {to_sm(t) for t in targets} - translation_langs
    if missing:
        print(f"!!! targets requested but NO translation received: {sorted(missing)}")
    print(f"errors: {[ (e.get('type'), e.get('reason')) for e in errors ]}")
    print(f"warnings: {[ (w.get('type'), w.get('reason')) for w in warnings ]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
