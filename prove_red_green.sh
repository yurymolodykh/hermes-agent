#!/bin/bash
# Prove the new acceptance test is RED on the base revision and GREEN with the fix.
# Restores the production file from <base-commit> in a temp copy, runs the test, restores the fix.
set -u
cd "$(dirname "$0")" || exit 3
BASE="${1:-e28bb9a4cb6}"
TARGET=hermes_acl_profiles.py
BACKUP=.prove_base_backup.py
cp "$TARGET" "$BACKUP" || exit 4
trap 'cp "$BACKUP" "$TARGET"; rm -f "$BACKUP"' EXIT

echo "### BASE ($BASE): restoring $TARGET from the base commit"
git show "$BASE:$TARGET" > "$TARGET" || exit 5
if grep -q "def registry_slug" "$TARGET"; then
  echo "!! base already has registry_slug — wrong base commit" >&2
  exit 6
fi
bash run_pytest.sh tests/hermes_cli/test_webapp_host_terminal_live_registry.py \
  -q -p no:cacheprovider 2>&1 | tail -4

echo
echo "### FIX: restoring the working copy"
cp "$BACKUP" "$TARGET"
bash run_pytest.sh tests/hermes_cli/test_webapp_host_terminal_live_registry.py \
  -q -p no:cacheprovider 2>&1 | tail -3
