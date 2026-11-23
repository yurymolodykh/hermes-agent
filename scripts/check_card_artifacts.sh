#!/usr/bin/env bash
# Refuse card service artifacts in the Git index (pre-commit gate).
#
# Kanban workers leave PROGRESS*.md journals, durable/ evidence trees, evidence-*/
# directories and REPORT-*.md behind in the working tree.  .gitignore and
# .git/info/exclude do not apply to paths that are already tracked, so a worker's
# `git add -A` (or `git add -f`) puts them back into the index and the artifacts land
# in the product repository.  This gate inspects the STAGED paths, not the work tree,
# and refuses the commit.  Deletions are never judged, so cleanup commits still pass.
#
# Deliberate, one-off exception: ALLOW_ARTIFACT_COMMIT=1 git commit ...
#
# Usage: scripts/check_card_artifacts.sh [PATH...]
#   PATH...  judge these repo-relative paths instead of the index (tests, probes).
#
# Dependency-free on purpose: the gate runs before every commit, so it must not need
# the checkout's Python environment (or a bootstrap interpreter) to resolve.

set -euo pipefail

BYPASS_ENV="ALLOW_ARTIFACT_COMMIT"
HINT="Артефакт хранится в рабочем каталоге карточки или в ~/.hermes/durable/kanban/card-artifacts/<task>/."

if [ "${!BYPASS_ENV:-}" != "" ]; then
  case "${!BYPASS_ENV}" in
    1 | true | TRUE | yes | YES | on | ON)
      printf '%s=%s — проверка служебных артефактов пропущена.\n' "$BYPASS_ENV" "${!BYPASS_ENV}" >&2
      exit 0
      ;;
  esac
fi

if [ "$#" -gt 0 ]; then
  staged=$(printf '%s\n' "$@")
else
  staged=$(git diff --cached --name-only --diff-filter=ACMR -z | tr '\0' '\n')
fi

offenders=()
while IFS= read -r path; do
  [ -n "$path" ] || continue
  base=${path##*/}
  first=${path%%/*}
  hit=0

  # Progress journals are card artifacts wherever they appear.
  case "$base" in
    PROGRESS.md | PROGRESS-*.md) hit=1 ;;
  esac

  # Root-anchored board trees: durable/ and evidence-*/
  if [ "$hit" -eq 0 ]; then
    case "$path" in
      durable/* | evidence-*) hit=1 ;;
    esac
  fi

  # Root-level card reports.
  if [ "$hit" -eq 0 ] && [ "$first" = "$path" ]; then
    case "$base" in
      REPORT-*.md) hit=1 ;;
    esac
  fi

  [ "$hit" -eq 0 ] || offenders+=("$path")
done <<< "$staged"

if [ "${#offenders[@]}" -eq 0 ]; then
  exit 0
fi

{
  printf 'Отказ: служебные артефакты карточки не должны попадать в коммит (найдено %d):\n' "${#offenders[@]}"
  printf '  - %s\n' "${offenders[@]}"
  printf '%s\n' "$HINT"
  printf 'Осознанное исключение — только явным флагом: %s=1 git commit ...\n' "$BYPASS_ENV"
} >&2
exit 1
