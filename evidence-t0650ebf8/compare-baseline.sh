#!/usr/bin/env bash
# Сравнить наборы падающих тестов на базовой ревизии и с правкой (один и тот же прогон).
set -uo pipefail
cd "$(dirname "$0")/.."
PY="$(cd ../../.venv && pwd)/bin/python"
export HERMES_PYTHON="$PY"
FILES="tests/agent/test_run_budget.py tests/agent/test_iteration_budget_race.py tests/agent/test_turn_iteration_prep.py tests/agent/test_kanban_budget_checkpoint.py tests/agent/test_iteration_budget_warning.py"

mkdir -p .cmp
git show aed44879299:agent/kanban_budget_checkpoint.py > .cmp/kbc.py
git show aed44879299:agent/turn_iteration_prep.py > .cmp/tip.py

run() {
  ./scripts/run_tests.sh $FILES -q 2>&1 | grep -oE "FAILED [^ ]+" | sed 's/^FAILED //' | sort > "$1"
}

cp .cmp/kbc.py agent/kanban_budget_checkpoint.py
cp .cmp/tip.py agent/turn_iteration_prep.py
run .cmp/base-failed.txt
echo "base failures: $(wc -l < .cmp/base-failed.txt)"

git show HEAD:agent/kanban_budget_checkpoint.py > agent/kanban_budget_checkpoint.py
git show HEAD:agent/turn_iteration_prep.py > agent/turn_iteration_prep.py
run .cmp/patch-failed.txt
echo "patch failures: $(wc -l < .cmp/patch-failed.txt)"

echo "--- diff наборов (пусто = база не хуже) ---"
diff .cmp/base-failed.txt .cmp/patch-failed.txt && echo "IDENTICAL"
rm -rf .cmp
