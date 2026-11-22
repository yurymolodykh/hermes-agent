# Красная база — t_0650ebf8

Тест написан заново (`tests/agent/test_kanban_budget_reminder_wip.py`), файлы кода взяты из
базовой ревизии `aed44879299`. Чтобы прогон не зависел от состояния рабочего дерева, база
экспортирована в отдельный каталог и тест подложен туда же:

```bash
git archive aed44879299 | tar -x -C /home/hermes/.hermes/profiles/03-software-engineer/cache/scratch/basewt
cp tests/agent/test_kanban_budget_reminder_wip.py \
   /home/hermes/.hermes/profiles/03-software-engineer/cache/scratch/basewt/tests/agent/
cd /home/hermes/.hermes/profiles/03-software-engineer/cache/scratch/basewt
HERMES_PYTHON=/home/hermes/.hermes/hermes-agent/.venv/bin/python \
  ./scripts/run_tests.sh tests/agent/test_kanban_budget_reminder_wip.py -q
```

Вывод:

```
[100.0% |     3/~3 | ✓1 | ✗2] ✗ tests/agent/test_kanban_budget_reminder_wip.py (1✓ 2✗, 2.5s)

  ║         assert _remind(_Agent(used=108, max_total=180), messages) is True
  ║ >       assert len(archives) == 1, f"expected one archive, got {archives}"
  ║ E       AssertionError: expected one archive, got []
  ║ E       assert 0 == 1
  ║ tests/agent/test_kanban_budget_reminder_wip.py:89: AssertionError
  ║ ___ test_repeat_reminder_on_an_unchanged_workspace_writes_no_second_archive ____
  ║ >       assert len(first) == 1
  ║ E       assert 0 == 1
  ║ tests/agent/test_kanban_budget_reminder_wip.py:125: AssertionError
  ║ FAILED tests/agent/test_kanban_budget_reminder_wip.py::test_soft_budget_archives_a_workspace_without_a_repository
  ║ FAILED tests/agent/test_kanban_budget_reminder_wip.py::test_repeat_reminder_on_an_unchanged_workspace_writes_no_second_archive
  ║ 2 failed, 1 passed in 0.70s

=== Summary: 1 files, 1 tests passed, 2 failed; FAILED: 1 of 1 file (100% complete) in 2.6s ===
```

Что это доказывает:

* `test_soft_budget_archives_a_workspace_without_a_repository` — на базе после мягкого порога
  (108/180) архива нет вообще: `preserve_wip_ref` упирается в `_repo_root(root) is None`
  и возвращает `None` (`:257`), обходного пути для scratch-каталога на мягком бюджете не было.
* `test_repeat_reminder_on_an_unchanged_workspace_writes_no_second_archive` — падает по той же
  причине (первого архива нет).
* `test_soft_budget_keeps_the_git_ref_and_writes_no_archive` на базе **проходит** — это инвариант
  «для git-карточки поведение не меняется», он зафиксирован тестом, а не только текстом задачи.

С правкой тот же файл: `=== Summary: 1 files, 3 tests passed, 0 failed ===`.

## Сравнение наборов падающих тестов (база vs правка)

7 файлов бюджета (`test_run_budget`, `test_iteration_budget_race`, `test_turn_iteration_prep`,
`test_kanban_budget_checkpoint`, `test_iteration_budget_warning`,
`test_kanban_budget_reminder_wip`, `test_kanban_budget_wip_checkpoint`), одинаковый прогон
в двух экспортированных деревьях (`basewt` = `aed44879299`, `patchwt` = HEAD карточки):

```
basewt  | failed: 58 | 7 files, 14 tests passed, 58 failed
patchwt | failed: 56 | 7 files, 16 tests passed, 56 failed
только на базе:  [test_kanban_budget_reminder_wip.py::test_repeat_reminder_on_an_unchanged_workspace_writes_no_second_archive,
                  test_kanban_budget_reminder_wip.py::test_soft_budget_archives_a_workspace_without_a_repository]
только с правкой: []
```

Остальные 56 падений — предсуществующие и совпадают с обеих сторон: страж
`tests/home_io_guard.py:160` считает путём боевого home каталог
`<worktree>/hermes-update-pull`, который появляется в git-worktree-выгрузке; с правкой
в дереве падает ровно столько же тестов, сколько на базе, минус два новых (они зелёные).
