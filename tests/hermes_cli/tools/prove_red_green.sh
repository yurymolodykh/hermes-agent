#!/bin/bash
# Prove the acceptance test is RED on the base revision and GREEN with the fix.
#
# Lives in tests/hermes_cli/tools/, beside the test it proves: every path below is relative to the
# REPO ROOT, resolved from this script's own location, so it runs from anywhere.
#
#   bash tests/hermes_cli/tools/prove_red_green.sh [base-commit]
#
# It restores hermes_acl_profiles.py from <base-commit> in place, runs the test, and puts the
# working copy back on exit (including Ctrl-C), so an interrupted run cannot leave the fix lost.
set -u
REPO_ROOT="$(cd "$(dirname "$0")/../../.." && pwd)" || exit 3
cd "$REPO_ROOT" || exit 3

BASE="${1:-e28bb9a4cb6}"
TEST=tests/hermes_cli/test_webapp_host_terminal_live_registry.py
TARGET=hermes_acl_profiles.py
BACKUP="$(mktemp -d)/hermes_acl_profiles.py" || exit 4
cp "$TARGET" "$BACKUP" || exit 4
trap 'cp "$BACKUP" "$TARGET"' EXIT

# pytest is not installed in this worktree; the shared runtime venv is named through $HOME.
PY="${HERMES_PYTHON:-$HOME/.hermes/hermes-agent/.venv/bin/python3}"
[ -x "$PY" ] || PY="$(command -v python3)"

echo "### BASE ($BASE): restoring $TARGET from the base commit"
git show "$BASE:$TARGET" > "$TARGET" || exit 5
if grep -q "def registry_slug" "$TARGET"; then
  echo "!! base already has registry_slug — wrong base commit" >&2
  exit 6
fi
"$PY" -m pytest "$TEST" -q -p no:cacheprovider 2>&1 | tail -4

echo
echo "### FIX: restoring the working copy"
cp "$BACKUP" "$TARGET"
"$PY" -m pytest "$TEST" -q -p no:cacheprovider 2>&1 | tail -3
