#!/bin/bash
# Base parity for one test file: run with the base hermes_acl_profiles.py and with the fix,
# comparing pass/fail counts. Usage: bash parity.sh <base-commit> <test-file> [more test files...]
set -u
cd "$(dirname "$0")" || exit 3
BASE="$1"; shift
TARGET=hermes_acl_profiles.py
BACKUP=.parity_backup.py
cp "$TARGET" "$BACKUP" || exit 4
trap 'cp "$BACKUP" "$TARGET"; rm -f "$BACKUP"' EXIT

summary() {
  bash run_pytest.sh "$@" -q -p no:cacheprovider 2>&1 \
    | grep -oE "[0-9]+ (passed|failed|error|errors|skipped)" | paste -sd' ' -
}

echo "### BASE $BASE"
git show "$BASE:$TARGET" > "$TARGET" || exit 5
summary "$@"
cp "$BACKUP" "$TARGET"
echo "### FIX"
summary "$@"
