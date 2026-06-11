"""Input-device listing and a short-lived level meter for soundcheck.

The meter opens its own RawInputStream on the chosen device. It MUST be
stopped before the pipeline launches so the device is not opened twice;
server.py enforces this on /api/server/start.
"""
import math
import threading

import numpy as np

SILENCE_DBFS = -120.0


def list_input_devices(substring: str = "") -> list:
    import sounddevice  # lazy: PortAudio loads only when actually probing
    out = []
    for idx, dev in enumerate(sounddevice.query_devices()):
        if dev.get("max_input_channels", 0) <= 0:
            continue
        out.append({
            "index": idx,
            "name": dev.get("name", ""),
            "default_samplerate": dev.get("default_samplerate"),
            "matches": bool(substring) and substring.lower() in dev.get("name", "").lower(),
        })
    return out


class LevelMeter:
    """RMS/peak dBFS from one input device. start() / read() / stop()."""

    def __init__(self, device_index: int):
        self.device_index = device_index
        self._rms_dbfs = SILENCE_DBFS
        self._peak_dbfs = SILENCE_DBFS
        self._lock = threading.Lock()
        self._stream = None

    def _callback(self, indata, frames, time_info, status_flags):
        pcm = np.frombuffer(bytes(indata), dtype=np.int16).astype(np.float64)
        if pcm.size == 0:
            return
        rms = math.sqrt(float(np.mean(pcm * pcm)))
        peak = float(np.max(np.abs(pcm)))
        with self._lock:
            self._rms_dbfs = 20 * math.log10(max(rms, 1.0) / 32768.0)
            self._peak_dbfs = 20 * math.log10(max(peak, 1.0) / 32768.0)

    def start(self) -> None:
        import sounddevice
        self._stream = sounddevice.RawInputStream(
            samplerate=16000, channels=1, dtype="int16",
            device=self.device_index, blocksize=1600, callback=self._callback)
        self._stream.start()

    def read(self) -> dict:
        with self._lock:
            return {"rms_dbfs": round(self._rms_dbfs, 1),
                    "peak_dbfs": round(self._peak_dbfs, 1)}

    def stop(self) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None
