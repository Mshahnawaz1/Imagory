#!/usr/bin/env bash
# Run Imagory in dev with a dev library, never the real one.
# The dev library (.dev-library in the repo, git-ignored) is kept between runs, so earlier
# scans are still there. Add your own photo folders in the app. Delete .dev-library to start over.
# Usage: scripts/dev_run.sh
set -euo pipefail
cd "$(dirname "$0")/.."

# WSLg keeps its Wayland socket here; /run/user/<uid> often lacks it.
if [ -d /mnt/wslg/runtime-dir ]; then
  export XDG_RUNTIME_DIR=/mnt/wslg/runtime-dir
fi

export IMAGORY_LIBRARY="$PWD/.dev-library"

echo "Library: $IMAGORY_LIBRARY"
exec uv run python -m imagory
