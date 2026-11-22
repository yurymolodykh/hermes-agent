# PROGRESS — t_35fe5619 (исчерпан счётчик повторов: автопродолжение и авто-сплит)

## Состояние: РАБОТА ЗАВЕРШЕНА И ЗАКОММИЧЕНА. Ждёт выката на шлюз (карточка t_e5c9a281).

* Ветка: `wt/t_35fe5619`, worktree `.worktrees/t_35fe5619`
* База: `3bbf87774c`
* Верхушка ветки: цепочка `d8e2a4a634` → `d95cfff41b` → `48c1beb911` → этот коммит PROGRESS
  (tip: `a5c28550f9`)
* Модель/провайдер: `deepseek-v4-flash` / `letovo` (дефолт профиля, не подменялась)

### Что сделано

1. **`hermes_cli/kanban_auto_continue.py`** (564 стр.) — вся политика: `AutoContinueSettings`,
   `resolve_settings`, `ineligible_reason`, `plan_after_failure`/`plan_for_trip` (чистые функции),
   `apply_trip`, `read_state` (состояние только из `task_events`), `run_split_phase`, `pending_split_ids`.
2. **`hermes_cli/kanban_db_dispatch.py`** — `_record_task_failure` делегирует решение в
   `plan_for_trip`/`apply_trip`; в `_dispatch_once_locked` фаза `run_split_phase` до расчёта
   spawn-бюджета (при `dry_run` не выполняется); `DispatchResult.auto_split/auto_split_failed`.
3. **`hermes_cli/kanban_db_graph.py`** — `decompose_triage_task(source_statuses=..., allow_redecompose=...)`.
4. **`hermes_cli/kanban_decompose.py`** — `decompose_failed_card`, `_load_failed_task`,
   `_failed_card_prompt_fields`, `_stamp_progress_locator` (локатор дерева + `PROGRESS.md`).
5. **`hermes_cli/config_defaults.py`** — `kanban.auto_continue: True`, `auto_continue_max_splits: 2`.
6. **П.8 (failure_limit 3 vs DEFAULT 2)** — устранён: `_resolve_failure_limit` /
   `_kanban_failure_limit_setting` в `kanban_ops.py`, дефолт `--failure-limit` → `None` в `kanban_parser.py`.
7. **Замечание оркестратора о пуле ключей** — `agent/kanban_key_pool_errors.py` (`KeyPoolSlotBusy`,
   `KeyPoolWait`), `hermes_cli/turn_exit.py` → код 75 (requeue без провала); простой `KeyPoolError`
   остаётся `crashed`.

### Проверки (все выполнены)

* `tests/hermes_cli/test_kanban_auto_continue.py` — **15 passed**
* `tests/hermes_cli/test_kanban_cli_failure_limit_config.py` — **5 passed**
* `tests/gateway/test_kanban_result_exit.py` — **25 passed**
* `tests/hermes_cli/test_kanban_restart_interrupt_budget.py` — **7 passed** (обновлён + парный тест на `auto_continue: false`)
* `tests/hermes_cli/test_kanban_decompose.py` — **10 passed** (4 новых на `decompose_failed_card`)
* Широкий регресс 122 файла канбана: `1015 passed, 9 failed, 3 CRASHED` — **все 9 и 3 воспроизведены
  на базе `3bbf87774c` без правок** (окружение прогона; подробности в `REPORT-auto-continue.md` §5)
* `code_health --base HEAD` → 0 blocking, 0 advisory; `ruff` по изменённым файлам — чисто
  (71 предсуществующая находка в `agent/kanban_key_pool.py` идентична базе)
* Три живые пробы на реальной БД → `REPORT-auto-continue.md` §6

### Остаток человеку

`manual_review`: увидеть `auto_continued`/`auto_split` на **живом** прогоне после выката ветки
на шлюз. Это делает карточка выката `t_e5c9a281` (15-sre-qa), не эта.

### Инцидент по пути (не терять)

`refs/stash` общий на весь репозиторий: `git stash pop` в worktree притянул чужой WIP карточки
`t_afdf38ac`. Чужой патч сохранён в `stash@{1}`, ничего не потеряно.
**Урок: не пользоваться `git stash` в worktree при параллельных карточках;** для пробы базы —
`git archive <rev> | tar -x -C <tmp>` (так и делалось в этой попытке) или `git show HEAD:<file>`.
