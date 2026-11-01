#!/usr/bin/env bash
# Run Imagory on WSL (WSLg) against a scratch library, never the real one.
# Usage: scripts/dev_run.sh [photo_folder_to_generate_or_reuse]
set -euo pipefail
cd "$(dirname "$0")/.."

# WSLg keeps its Wayland socket here; /run/user/<uid> often lacks it.
if [ -d /mnt/wslg/runtime-dir ]; then
  export XDG_RUNTIME_DIR=/mnt/wslg/runtime-dir
fi
export IMAGORY_LIBRARY="${IMAGORY_LIBRARY:-$HOME/.imagory-dev-lib}"

PHOTOS="${1:-$HOME/.imagory-dev-photos}"
if [ ! -d "$PHOTOS" ]; then
  uv run python scripts/make_fake_library.py "$PHOTOS" --count 300
fi
echo "Library: $IMAGORY_LIBRARY   Photos to add as a source: $PHOTOS"
exec uv run python -m imagory
