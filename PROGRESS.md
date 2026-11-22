# PROGRESS — t_4cb8f83b: точечный перенос двух апстрим-фиксов

## Состояние: ГОТОВО (обе правки внесены, тесты и проверки сняты). Остались финальный коммит и передача.

* Дерево: `/home/hermes/.hermes/hermes-agent/.worktrees/t_4cb8f83b`, ветка `wt/t_4cb8f83b`
* База для сравнения: `beeb9623539`
* Содержимое правки = `git diff beeb9623539`
* Отчёт: `REPORT-patches.md`; доказательства: `evidence-t4cb8f83b/`

## Сделано

### Патч 1 — класс `owner_unavailable` (PR #152921, перенос по смыслу, не `git apply`)

* `hermes_state_errors.py:99-103` — `PERSISTENCE_ERROR_CAUSES` += `"owner_unavailable"` (перед `"unknown"`).
* `hermes_state_errors.py` — `_PERSISTENCE_CAUSE_BY_PHRASE` += `(("owner_unavailable",), …)` и
  `(("worker_receipt_timeout",), "owner_unavailable")`, **после** `foreign key constraint failed` и
  **перед** `_PERMISSION_MARKERS`; комментарий PR сохранён (фраза, не тип; RPC теряет тип; импорт
  фасада воркера инвертировал бы зависимость листового модуля).
* Докстринг `classify_persistence_error` дополнен.
* `agent/turn_explainers.py:53-56` — `_PERSISTENCE_CAUSES` += `"owner_unavailable"` + комментарий.
* `locales/*.yaml` — 17 файлов, ключ `explainer.persistence.owner_unavailable` после `turn_lease`.
* `tests/agent/test_turn_completion_explainer.py` — 3 новых теста (+параметризация).

### Патч 2 — уборщик не убивает живых воркеров (issue #157057, диффа нет)

* `hermes_cli/kanban_db_dispatch.py::_reap_terminal_worker_row` — перед сигналом проба
  `_worker_alive(pid, fingerprint)`; живой → `_log.debug` + `return` (строка остаётся в выборке);
  мёртвый → снятие улик. Ветка сигнала удалена.
* Докстринги `reap_terminal_workers` и `DispatchResult.reaped_terminal_workers` приведены к факту.
* `tests/hermes_cli/test_kanban_terminal_worker_reaper.py` — 5 тестов переписаны под контракт.
* `website/docs/user-guide/features/kanban.md:138` — описание диспетчера обновлено.

Не тронуто: `TERMINAL_WORKER_REAP_GRACE_SECONDS`, `reclaim`-логика, `_terminate_reclaimed_worker`,
`enforce_max_runtime`, `kanban_auto_continue.py`, `kanban_profile_scope.py`, пороги ретраев.

## Проверки (все сняты)

* reaper-тесты: 5 passed; на базе — 2 failed (красная база доказана).
* explainer-тесты: 34 passed; на базе 4 новых падают (`-k owner`).
* reaper + explainer + outbox: 61 passed.
* Смежные наборы состояния: 109 passed, 7 skipped.
* `ruff check` — All checks passed. `code_health --base beeb9623539` — 0 blocking, 0 advisory.
* По-файлово `tests/hermes_cli/*kanban*` (99 файлов): база 17 FAIL / 82 PASS, патч 16 FAIL / 83 PASS;
  `diff` наборов = единственная строка `test_kanban_terminal_worker_reaper.py`.
* `tests/hermes_state` по-файлово: 134 падающих файла, все предсуществующие (страж
  `home_io_guard.py:160` ругается на путь worktree; выборка 12 файлов даёт одинаковые числа на
  базе и с правкой).

## Следующий шаг для будущей попытки

1. `git add -A && git commit` сообщением с номером карточки (t_4cb8f83b), затем
   `kanban_comment` — самопроверка, `kanban_complete` с artifacts:
   `REPORT-patches.md`, `evidence-t4cb8f83b/`.
2. Не вливать в общее дерево и не перезапускать шлюз — отдельная карточка выката.
