#!/usr/bin/env bash
# Воспроизведение конкуренции за store_lock. $1 = каталог стора, $2 = удержание сек.
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY=/home/hermes/t72venv/bin/python
BASE="$1"
HOLD="${2:-6}"
mkdir -p "$BASE"
"$PY" "$HERE/repro_store_lock.py" "$BASE" "$HOLD" hold > "$HERE/holder.log" 2>&1 &
HPID=$!
sleep 1.5
echo "--- lock-holder fds during contention (снято извне):"
ls -l "$(dirname "$BASE")/.$(basename "$BASE").lock"
"$PY" "$HERE/repro_store_lock.py" "$BASE" "$HOLD" wait 2>&1 | tee "$HERE/waiter.log"
echo "--- holder.log:"
cat "$HERE/holder.log"
wait $HPID
