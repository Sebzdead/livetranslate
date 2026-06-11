import math
import sys
import types

import pytest


@pytest.fixture
def fake_sounddevice(monkeypatch):
    mod = types.SimpleNamespace()
    mod.query_devices = lambda: [
        {"name": "MacBook Pro Microphone", "max_input_channels": 1, "default_samplerate": 48000.0},
        {"name": "Scarlett 2i2 USB", "max_input_channels": 2, "default_samplerate": 48000.0},
        {"name": "External Headphones", "max_input_channels": 0, "default_samplerate": 48000.0},
    ]

    class FakeStream:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.started = False

        def start(self):
            self.started = True

        def stop(self):
            self.started = False

        def close(self):
            pass

    mod.RawInputStream = FakeStream
    monkeypatch.setitem(sys.modules, "sounddevice", mod)
    return mod


def test_list_input_devices_filters_outputs_and_flags_match(fake_sounddevice):
    from livetranslate.control.audio_probe import list_input_devices
    devices = list_input_devices("scarlett")
    names = [d["name"] for d in devices]
    assert "External Headphones" not in names          # output-only excluded
    assert [d["matches"] for d in devices] == [False, True]
    assert devices[1]["index"] == 1                    # original sounddevice index kept


def test_level_meter_computes_dbfs(fake_sounddevice):
    from livetranslate.control.audio_probe import LevelMeter
    meter = LevelMeter(device_index=1)
    meter.start()
    assert meter._stream.started
    # feed one block of a half-scale square wave: RMS = peak = 16384 -> ~ -6.02 dBFS
    pcm = (16384).to_bytes(2, "little", signed=True) * 1600
    meter._callback(pcm, 1600, None, None)
    reading = meter.read()
    assert math.isclose(reading["rms_dbfs"], -6.0, abs_tol=0.1)
    assert math.isclose(reading["peak_dbfs"], -6.0, abs_tol=0.1)
    meter.stop()
    assert meter._stream is None


def test_level_meter_silence_floor(fake_sounddevice):
    from livetranslate.control.audio_probe import LevelMeter
    meter = LevelMeter(device_index=0)
    meter._callback(b"\x00\x00" * 1600, 1600, None, None)
    assert meter.read()["rms_dbfs"] <= -90.0
