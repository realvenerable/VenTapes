#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

for required_command in python3 glib-compile-resources clang; do
  if ! command -v "$required_command" >/dev/null 2>&1; then
    echo "Missing required build command: $required_command" >&2
    exit 1
  fi
done

python3 -m venv .venv --system-site-packages
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m pip install nuitka

VENV_SITE_PACKAGES=$(python -c 'import sysconfig; print(sysconfig.get_path("purelib"))')
export PYTHONPATH="$VENV_SITE_PACKAGES:${PYTHONPATH:-}"

glib-compile-resources \
  --sourcedir=. \
  src/ventapes.gresource.xml \
  --target=src/ventapes.gresource

cd src
python -m nuitka \
  --clang \
  --file-reference-choice=runtime \
  --include-package=ui \
  --include-package=api \
  --include-package=player \
  --include-module=logger \
  --include-module=version \
  --output-filename=ventapes \
  --assume-yes-for-downloads \
  main.py
