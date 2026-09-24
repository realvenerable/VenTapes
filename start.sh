#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

if [[ ! -f .venv/bin/activate ]]; then
  echo "Missing .venv. Create it and install requirements.txt first." >&2
  exit 1
fi

source .venv/bin/activate

if command -v glib-compile-resources >/dev/null 2>&1; then
  glib-compile-resources \
    --sourcedir=. \
    src/ventapes.gresource.xml \
    --target=src/ventapes.gresource
else
  echo "Warning: glib-compile-resources is unavailable; some bundled action icons may be missing." >&2
fi

exec python3 src/main.py "$@"
