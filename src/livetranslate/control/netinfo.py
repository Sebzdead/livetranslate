"""LAN IP discovery and audience link building."""
import socket

LANG_NAMES = {"es": "Español", "fr": "Français", "de": "Deutsch",
              "pt": "Português", "ar": "العربية", "zh": "中文", "en": "English"}


def lan_ip() -> str:
    """Best-effort LAN IP of the outbound interface.

    UDP connect() picks a route without sending any packet; works offline as
    long as any interface is up. Falls back to loopback.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 80))  # TEST-NET-1: never actually routed
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def links(ip: str, port: int, targets: list) -> dict:
    base = f"http://{ip}:{port}"
    return {
        "operator": f"{base}/",
        "languages": [{"lang": code, "name": LANG_NAMES.get(code, code),
                       "url": f"{base}/v/{code}"} for code in targets],
    }
