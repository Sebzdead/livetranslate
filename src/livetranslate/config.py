import copy
import tomllib
from pathlib import Path

ALLOWED_LANGS = {"es", "fr", "de", "pt", "ar", "zh"}

DEFAULTS: dict = {
    "session": {"source_language": "en", "output_dir": "sessions"},
    "audio": {"device_substring": "", "chunk_ms": 100, "ring_seconds": 120},
    "asr": {
        "adapter": "elevenlabs", "failover": "", "give_up_after_s": 0,
        "overlap_ms": 2000,
        "max_session_s": 0,  # 0 = off; set 5400 (90 min) for ElevenLabs; AAI hard limit 3 h
        "elevenlabs": {"keyterms_max": 50},  # realtime cap per docs/vendor-notes.md
        "assemblyai": {"use_domain_prompt": True},
    },
    "segmenter": {"max_words": 45, "max_pending_s": 12},
    "translate": {
        "targets": ["es", "fr", "de", "pt"], "provider": "", "base_url": "",
        "model": "", "api_key_env": "TRANSLATE_API_KEY", "timeout_s": 10,
        "batch_threshold": 3, "batch_max": 6,
    },
    "glossary": {"path": "glossary.tsv", "domain_blurb": "domain_blurb.txt"},
    "display": {"host": "0.0.0.0", "port": 8080, "font_scale": 1.6,
                "draft_translation": False},
    "health": {"stall_s": 10},
    "harness": {"rtf": 1.0},
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


_SECRET_SUFFIXES = ("_key", "_secret", "_token", "_password", "_passwd")


def _looks_like_secret_field(name: str) -> bool:
    """Return True if the field name itself suggests it holds a raw secret."""
    lower = name.lower()
    # Exact match or ends with one of the secret suffixes (whole-word boundary)
    if lower in ("key", "secret", "token", "password", "passwd"):
        return True
    return any(lower.endswith(s) for s in _SECRET_SUFFIXES)


def _scan_for_secrets(d: dict, path: str = "") -> None:
    for k, v in d.items():
        if isinstance(v, dict):
            _scan_for_secrets(v, f"{path}{k}.")
        elif _looks_like_secret_field(k):
            raise ValueError(
                f"config field {path}{k} looks like a secret; "
                "secrets must come from environment variables only")


def load_config(path: str | Path) -> dict:
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    _scan_for_secrets(raw)
    cfg = _deep_merge(DEFAULTS, raw)
    if not 50 <= cfg["audio"]["chunk_ms"] <= 200:
        raise ValueError("audio.chunk_ms must be in [50, 200]")
    if cfg["audio"]["ring_seconds"] < 120:
        raise ValueError("audio.ring_seconds must be >= 120")
    bad = set(cfg["translate"]["targets"]) - ALLOWED_LANGS
    if bad:
        raise ValueError(f"unknown translate target(s): {sorted(bad)}")
    if cfg["session"]["source_language"] not in ("en", "de"):
        raise ValueError("session.source_language must be 'en' or 'de'")
    return cfg
