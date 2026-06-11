"""Read/validate/write the operator-editable files: .env, config.toml, glossary.tsv.

All writes are atomic (tmp file in the same directory + os.replace) so a crash
mid-write never corrupts an event-day file.
"""
import os
import re
import tempfile
from pathlib import Path

SECRET_KEYS = ("ELEVENLABS_API_KEY", "ASSEMBLYAI_API_KEY", "TRANSLATE_API_KEY")

# Regex patterns for parsing .env values
_QUOTED = re.compile(r'^(["\'])(.*)\1\s*(?:#.*)?$')
_UNQUOTED = re.compile(r'^(.*?)(\s+#.*)?$')


def _parse_value(raw: str) -> str:
    """Parse a value from .env, handling quotes and comments.

    - Quoted values ("..." or '...') preserve everything inside the quotes
    - Unquoted values are stripped and inline comments (space-hash) are removed
    - Pure comment lines (starting with #) return empty string
    """
    raw = raw.strip()
    if raw.startswith("#"):
        return ""
    m = _QUOTED.match(raw)
    if m:
        return m.group(2)
    return _UNQUOTED.match(raw).group(1).strip()


def atomic_write(path, text: str) -> None:
    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_env(path) -> dict:
    """Parse KEY=VALUE lines; skip comments/blanks; handle quoted values.

    Handles optional 'export ' prefix, quoted values (with inline comments),
    and inline comments (space-hash) for unquoted values.
    """
    p = Path(path)
    if not p.exists():
        return {}
    out = {}
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        # Remove optional 'export ' prefix
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, _, raw_value = line.partition("=")
        key = key.strip()
        value = _parse_value(raw_value)
        out[key] = value
    return out


def write_env_keys(path, updates: dict) -> None:
    """Update KEY=VALUE lines in place, preserving unrelated lines and comments.

    Empty values are skipped so a blank form field never wipes a stored key.
    Handles optional 'export ' prefix in existing lines (intentionally not
    round-tripped: updated lines are rewritten as plain KEY=value).
    """
    p = Path(path)
    lines = p.read_text(encoding="utf-8").splitlines() if p.exists() else []
    remaining = {k: v for k, v in updates.items() if v}
    out = []
    for line in lines:
        stripped = line.strip()
        key = None
        if stripped and not stripped.startswith("#") and "=" in stripped:
            # Remove optional 'export ' prefix when extracting key
            if stripped.startswith("export "):
                stripped = stripped[7:].lstrip()
            key = stripped.partition("=")[0].strip()
        if key in remaining:
            out.append(f"{key}={remaining.pop(key)}")
        else:
            out.append(line)
    for key, value in remaining.items():
        out.append(f"{key}={value}")
    atomic_write(p, "\n".join(out) + "\n")


def mask(value: str) -> str:
    """Never return enough of a secret to be useful: last 4 chars at most."""
    if not value:
        return ""
    return "…" + value[-4:] if len(value) > 4 else "…"
