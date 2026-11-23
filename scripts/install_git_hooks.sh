#!/usr/bin/env bash
# Install the lefthook pre-commit hooks into this clone (every worktree of it).
#
# `.git/hooks/` is NOT versioned: on a fresh clone — or after the repository is
# re-created — the hooks that carry `just artifact-guard`, the card-artifact gate,
# simply do not exist, and the gate disappears silently.  This script is the
# reproducible install path (recipe: `just hooks-install`): it runs
# `lefthook install` and then PROVES the hook is on disk and executable, so a
# green run means the chain is live, not that a command exited 0.
#
# Usage: scripts/install_git_hooks.sh

set -euo pipefail

repo=$(git rev-parse --show-toplevel)
cd "$repo"

hooks_dir=$(git rev-parse --path-format=absolute --git-path hooks)

if ! command -v lefthook >/dev/null 2>&1; then
  echo "Отказ: lefthook не найден в PATH. Установите зависимости: pnpm install" >&2
  exit 1
fi

lefthook install

# lefthook only writes the hooks lefthook.yml declares, so verify exactly those:
# pre-commit carries the card-artifact gate.
target="$hooks_dir/pre-commit"
if [ ! -x "$target" ]; then
  echo "Отказ: хук $target не установлен или не исполняем после lefthook install." >&2
  exit 1
fi
if ! grep -q 'guard card artifacts' lefthook.yml; then
  echo "Отказ: lefthook.yml не содержит задания 'guard card artifacts' — гейт не подключён." >&2
  exit 1
fi

printf 'Хук установлен: %s (lefthook). Проверка служебных артефактов карточек активна.\n' "$target"
printf 'Обход — только явным флагом: %s=1 git commit ...\n' ALLOW_ARTIFACT_COMMIT
