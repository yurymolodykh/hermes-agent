# Красная база — t_0650ebf8

Команда (файлы `agent/kanban_budget_checkpoint.py` и `agent/turn_iteration_prep.py`
восстановлены из базовой ревизии `aed44879299`, тест — новый):

```
git show aed44879299:agent/kanban_budget_checkpoint.py > agent/kanban_budget_checkpoint.py
git show aed44879299:agent/turn_iteration_prep.py      > agent/turn_iteration_prep.py
HERMES_PYTHON=/home/hermes/.hermes/hermes-agent/.venv/bin/python \
  ./scripts/run_tests.sh tests/agent/test_kanban_budget_reminder_wip.py -q
```

Вывод:

```
tests/agent/test_kanban_budget_reminder_wip.py:89: AssertionError
___ test_repeat_reminder_on_an_unchanged_workspace_writes_no_second_archive ____
    _remind(_Agent(used=108, max_total=180), _tool_turn())
    first = _archives(home)
>   assert len(first) == 1
E   assert 0 == 1
tests/agent/test_kanban_budget_reminder_wip.py:125: AssertionError
=========================== short test summary info ============================
FAILED tests/agent/test_kanban_budget_reminder_wip.py::test_soft_budget_archives_a_workspace_without_a_repository
FAILED tests/agent/test_kanban_budget_reminder_wip.py::test_repeat_reminder_on_an_unchanged_workspace_writes_no_second_archive
2 failed, 1 passed in 1.04s
```

Что это доказывает:

* `test_soft_budget_archives_a_workspace_without_a_repository` — на базе после
  мягкого порога (108/180) архива нет вообще: `preserve_wip_ref` упирается в
  `_repo_root(root) is None` и возвращает `None` (`:257`), а обходного пути для
  scratch-каталога на мягком бюджете не существовало.
* `test_repeat_reminder_on_an_unchanged_workspace_writes_no_second_archive` —
  падает по той же причине (первого архива нет).
* `test_soft_budget_keeps_the_git_ref_and_writes_no_archive` на базе **проходит** —
  это и есть инвариант «для git-карточки поведение не меняется», он зафиксирован
  тестом, а не только текстом задачи.
