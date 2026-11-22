# PROGRESS — t_63278be8: Д-1 `failure_limit` в sweep'ах, Д-2 сплит вне dispatch-lock

## Состояние: код и тесты готовы, доказательства сняты. Остались `REPORT-fixes.md`, самопроверка, коммит и передача.

* Дерево: `/home/hermes/.hermes/hermes-agent/.worktrees/t_63278be8`, ветка `wt/t_63278be8`
* База: ветка `t_4cb8f83b` (merge `3b7eda61838e`)
* Свой venv: `.venv` (site-packages скопированы из общего дерева: `pip install` без сети не идёт,
  а пробы/тесты требуют `pytest` + `ruamel.yaml`)
* ВНИМАНИЕ: наследуется `HERMES_DELEGATED_CHILD_CONTEXT` — он делает всё под `~/.hermes` read-only.
  Пробы обязаны его снимать, иначе `sqlite3.OperationalError: unable to open database file`.

## Сделано

### Д-1 — `kanban.failure_limit` доходит до путей таймаута и краха

* `enforce_max_runtime(..., failure_limit=DEFAULT_FAILURE_LIMIT)` → `_record_task_failure(..., failure_limit=...)`
* `_account_crashes(..., failure_limit=DEFAULT_FAILURE_LIMIT)` → оба вызова `_record_task_failure`
* `detect_crashed_workers(..., failure_limit=...)` → `_account_crashes`
* `_run_reclaim_phase` передаёт лимит в оба sweep'а
* Дефолт оставлен `DEFAULT_FAILURE_LIMIT`: без конфига поведение не менялось (проба `--no-config`)

### Д-2 — сплит вынесен из-под блокировки диспетчера (вариант 1)

* Новый модуль `hermes_cli/kanban_db_dispatch_split.py`: `run_split_before_tick` (сплит без
  dispatch-lock, под своим неблокирующим `.split.lock`) + `run_tick` (сплит → затем один
  `_dispatch_once_locked` под `_dispatch_tick_lock`)
* `_dispatch_once_locked` больше не вызывает `run_split_phase`; комментарий-указатель оставлен
* `kanban_db_connect._dispatch_split_lock` — отдельный `.split.lock` (flock, неблокирующий)
* `dispatch_once` делегирует в `_kbdsp.run_tick`; докстринг описывает порядок и причину
* Сплит идёт ДО тика намеренно: сплит переводит сгоревшую карточку в `todo`, детей в `ready` —
  именно это тик должен заспавнить

## Проверки (сняты)

* `tests/hermes_cli/test_kanban_failure_limit_sweeps.py` + `test_kanban_split_outside_dispatch_lock.py`:
  **6 passed**; на базе `3b7eda61838e` — **4 failed** (красная база доказана,
  `evidence-t63278be8/tests-base-3b7eda61838e.log`)
* Проба Д-1 (`evidence-t63278be8/probe_fl_exact.py`): патч — все 5 вариантов `effective_limit=3`;
  база — A=2, B=3, C=2, crash A=2, crash C=2 (ровно как в вердикте)
* Проба Д-2 (`evidence-t63278be8/probe_split_two_process.py`): патч — `parent_skipped_locked=false`,
  `spawned_during_split=true`, тик 0.42 с при декомпозере 6 с, 2 вызова сплита на 2 тика;
  база — `parent_skipped_locked=true`, `spawned_during_split=false`

## Следующий шаг

1. `REPORT-fixes.md` (Д-1/Д-2/Д-3/Д-4/Д-5 + выбор варианта Д-2 + непроверенное)
2. Прогон по-файлово `tests/hermes_cli/*kanban*` база vs патч, `ruff`, `code_health`
3. `kanban_comment` — самопроверка по чек-листу 09; `kanban_complete` с artifacts
