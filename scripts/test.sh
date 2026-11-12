#!/usr/bin/env bash
# Run the test suite, or one category of it.
#   unit         pure logic: walk rules, diff, dates, Takeout parsing, cleanup planning (no database, no Qt)
#   integration  database, scans and cleanup runs against generated fixture folders (no Qt)
#   ui           Qt widgets, dialogs and window wiring (pytest-qt)
#   all          everything (default)
# Extra arguments go to pytest, e.g. scripts/test.sh unit -k walk -x
# Usage: scripts/test.sh [unit|integration|ui|all] [pytest args...]
set -euo pipefail
cd "$(dirname "$0")/.."

category="${1:-all}"
[ $# -gt 0 ] && shift

case "$category" in
  unit|integration|ui) exec uv run pytest -q "tests/$category" "$@" ;;
  all) exec uv run pytest -q tests "$@" ;;
  -h|--help) sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
  *) echo "Unknown category: $category (use unit, integration, ui or all)" >&2; exit 2 ;;
esac
