#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PYTHON=$(command -v python3)
PY_VERSION=$($PYTHON -c 'import sys; print(sys.version_info >= (3, 11))')
if [ "$PY_VERSION" != "True" ]; then
  echo "ai-usage requires Python 3.11 or newer" >&2
  exit 1
fi

mkdir -p "$HOME/.local/bin" "$HOME/.config/ai-usage/snapshots"
cp "$ROOT/ai_usage.py" "$HOME/.local/bin/ai-usage"
chmod 755 "$HOME/.local/bin/ai-usage"
if [ ! -f "$HOME/.config/ai-usage/config.toml" ]; then
  cp "$ROOT/config.example.toml" "$HOME/.config/ai-usage/config.toml"
fi
echo "Installed ai-usage in $HOME/.local/bin"
echo "Run: $HOME/.local/bin/ai-usage"

