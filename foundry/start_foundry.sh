#!/usr/bin/env bash
# start_foundry.sh - run the Foundry service in this terminal (macOS/Linux).
#
#   ./foundry/start_foundry.sh                 serve the repository this script lives in
#   ./foundry/start_foundry.sh ~/code/myrepo   serve another repository
#   ./foundry/start_foundry.sh --repo ~/code/myrepo
#
# Runs: .venv/bin/python -m foundry.serve --repo <path>
# The service listens on http://127.0.0.1:4310 (FOUNDRY_SERVE_PORT). Stop it with Ctrl+C.
# FOUNDRY_DRY_RUN=1 prints the command instead of running it.

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PY="$REPO_ROOT/.venv/bin/python"
TARGET="$REPO_ROOT"

say() { printf '[foundry] %s\n' "$*"; }
usage() {
  printf 'Usage: foundry/start_foundry.sh [--repo] [path]\n  path defaults to the repository root: %s\n' "$REPO_ROOT"
}

case "${1:-}" in
  -h|--help) usage; exit 0 ;;
  --repo)
    if [ -z "${2:-}" ]; then usage; exit 2; fi
    TARGET="$2" ;;
  "") ;;
  *) TARGET="$1" ;;
esac

if [ ! -x "$PY" ]; then
  say "No Python virtual environment found at:"
  say "  $PY"
  say "Create it from the repository root first - see \"Install\" in foundry/README.md:"
  say "  python3 -m venv .venv"
  say "  .venv/bin/python -m pip install -r foundry/requirements.txt"
  exit 1
fi
if [ ! -d "$TARGET" ]; then
  say "Repository folder not found: $TARGET"
  exit 1
fi
TARGET="$(cd "$TARGET" && pwd)"

cd "$REPO_ROOT" || exit 1
say "Repo:   $TARGET"
say "Python: $PY"
say "Starting the Foundry service on http://127.0.0.1:${FOUNDRY_SERVE_PORT:-4310} ..."
say "Stop it with Ctrl+C."

if [ "${FOUNDRY_DRY_RUN:-0}" = "1" ]; then
  printf '[dry-run] %s -m foundry.serve --repo %s\n' "$PY" "$TARGET"
  exit 0
fi
# Exit status 75 means "restart me" (a remote update pulled new code).
while :; do
  "$PY" -m foundry.serve --repo "$TARGET"
  rc=$?
  if [ "$rc" -ne 75 ]; then
    exit "$rc"
  fi
  say "Restarting with the updated code..."
done
