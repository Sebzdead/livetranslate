"""Read/validate/write the operator-editable files: .env, config.toml, glossary.tsv.

All writes are atomic (tmp file in the same directory + os.replace) so a crash
mid-write never corrupts an event-day file.
"""
import os
import tempfile
from pathlib import Path

SECRET_KEYS = ("ELEVENLABS_API_KEY", "ASSEMBLYAI_API_KEY", "TRANSLATE_API_KEY")


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
    """Parse KEY=VALUE lines; skip comments/blanks; strip optional quotes.

    Handles optional 'export ' prefix and inline comments (space-hash).
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
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        # If value is not quoted, strip inline comments
        if value and not (value.startswith('"') or value.startswith("'")):
            if " #" in value:
                value = value.split(" #")[0].strip()
        # Strip quotes
        value = value.strip('"').strip("'")
        out[key] = value
    return out


def write_env_keys(path, updates: dict) -> None:
    """Update KEY=VALUE lines in place, preserving unrelated lines and comments.

    Empty values are skipped so a blank form field never wipes a stored key.
    Handles optional 'export ' prefix in existing lines.
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
