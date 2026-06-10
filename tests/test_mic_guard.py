import sys
import types
import pytest


def _fake_sd(devices):
    m = types.ModuleType("sounddevice")
    m.query_devices = lambda: devices
    return m


def test_refuses_to_start_if_device_not_found(monkeypatch):
    monkeypatch.setitem(sys.modules, "sounddevice", _fake_sd(
        [{"name": "MacBook Pro Microphone", "max_input_channels": 1}]))
    from livetranslate.audio import MicSource
    with pytest.raises(SystemExit, match="Scarlett"):
        MicSource("Scarlett", chunk_ms=100).resolve_device()


def test_resolves_matching_input_device(monkeypatch):
    monkeypatch.setitem(sys.modules, "sounddevice", _fake_sd(
        [{"name": "MacBook Pro Microphone", "max_input_channels": 1},
         {"name": "Scarlett 2i2 USB", "max_input_channels": 2},
         {"name": "Scarlett Display Out", "max_input_channels": 0}]))   # not an input
    from livetranslate.audio import MicSource
    idx = MicSource("Scarlett", chunk_ms=100).resolve_device()
    assert idx == 1   # first INPUT device whose name contains the substring


def test_empty_substring_refused(monkeypatch):
    monkeypatch.setitem(sys.modules, "sounddevice", _fake_sd(
        [{"name": "Anything", "max_input_channels": 1}]))
    from livetranslate.audio import MicSource
    with pytest.raises(SystemExit, match="device_substring"):
        MicSource("", chunk_ms=100).resolve_device()
