#!/bin/zsh
# Double-clickable macOS launcher for the LiveTranslate operator control panel.
set -e
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  echo "First run: creating Python environment (one-time, ~1 minute)..."
  python3 -m venv .venv
  .venv/bin/pip install --quiet -e '.[dev]'
fi
exec .venv/bin/python -m livetranslate.control
