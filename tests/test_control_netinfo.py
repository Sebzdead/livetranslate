import re

from livetranslate.control import netinfo


def test_lan_ip_returns_ipv4_string():
    ip = netinfo.lan_ip()
    assert re.fullmatch(r"\d+\.\d+\.\d+\.\d+", ip)


def test_links_builds_operator_and_language_urls():
    out = netinfo.links("192.168.1.50", 8765, ["es", "fr", "xx"])
    assert out["operator"] == "http://192.168.1.50:8765/"
    langs = {entry["lang"]: entry for entry in out["languages"]}
    assert langs["es"]["url"] == "http://192.168.1.50:8765/v/es"
    assert langs["es"]["name"] == "Español"
    assert langs["xx"]["name"] == "xx"  # unknown code falls back to the code itself
