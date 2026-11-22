# PROGRESS — t_35fe5619 (исчерпан счётчик повторов: автопродолжение и авто-сплит)

## Состояние: код завершён, линт/health чистые, целевые тесты зелёные. Идут широкий регресс и коммит.

* Ветка: `wt/t_35fe5619`, worktree `.worktrees/t_35fe5619`
* База: `3bbf87774c` (HEAD ветки на старте попытки)
* Модель/провайдер: `deepseek-v4-flash` / `letovo` (дефолт профиля, не подменялась)

> Прежнее содержимое этого файла относилось к карточке `t_af262aaf` (бюджет итераций) — оно
> закоммичено там (`wt/t_af262aaf`, верхушка `3ab1936e05`, отчёт `REPORT-budget-stop.md`) и здесь
> не нужно.

### Что сделано (унаследовано от предыдущей попытки + доработано сейчас)

1. **Новый модуль `hermes_cli/kanban_auto_continue.py`** (564 стр.): `AutoContinueSettings`,
   `resolve_settings` (читает `kanban.auto_continue` / `auto_continue_max_splits`),
   `ineligible_reason` (классификация причины: своя причина карточки vs внешняя),
   `plan_for_trip` (чистая функция решения: continue / split / block-exhausted),
   `apply_trip` (запись статуса + события `auto_continued` / `auto_split`),
   `run_split_phase` (сплит вне write-txn, вызов декомпозера), `pending_split_ids`.
2. **`hermes_cli/kanban_db_dispatch.py`**: `_record_task_failure` больше не ставит `blocked`
   напрямую — решение уходит в `_kac.plan_for_trip` / `_kac.apply_trip`. В
   `_dispatch_once_locked` добавлена фаза `run_split_phase` (после reclaim/crash, ДО расчёта
   spawn-бюджета; при `dry_run` не выполняется). `DispatchResult` получил поля
   `auto_split` / `auto_split_failed`.
3. **`hermes_cli/kanban_db_graph.py`**: `decompose_triage_task` принимает `source_statuses`
   (по умолчанию `("triage",)`, для сплита — `("ready","todo","blocked")`) и `allow_redecompose`
   (повторный сплит — новое поколение детей); проверка статуса остаётся optimistic-concurrency
   стражем, не политикой.
4. **`hermes_cli/kanban_decompose.py`**: путь сплита упавшей карточки (детерминированные дети при
   недоступном aux-LLM, входные локаторы `PROGRESS.md` + отчёт родителя).
5. **`hermes_cli/config_defaults.py`**: новые ключи `kanban.auto_continue: True`,
   `kanban.auto_continue_max_splits: 2`.
6. **П.8 критерия — устранено (сегодня).** Причина расхождения `config.yaml: failure_limit: 3`
   против факта `effective_limit=2`: CLI-путь `hermes kanban dispatch` (и `kanban daemon`) передавал
   в `dispatch_once` жёсткий `kbd.DEFAULT_FAILURE_LIMIT` (2), игнорируя `kanban.failure_limit`.
   Добавлены `_resolve_failure_limit` (приоритет `--failure-limit` > `kanban.failure_limit` >
   `DEFAULT_FAILURE_LIMIT`, значения <1 проваливаются) и `_kanban_failure_limit_setting`
   в `hermes_cli/kanban_ops.py`; дефолты `--failure-limit` в `hermes_cli/kanban_parser.py`
   переведены с `kbd.DEFAULT_FAILURE_LIMIT` на `None`, чтобы флаг стал явным opt-in.
   Проверено вживую: при `HERMES_HOME=/home/hermes/.hermes` → `_kanban_failure_limit_setting()=3`,
   `_resolve_failure_limit(None, 3)=3`, `_cmd_dispatch` передаёт 3. Gateway-путь читал конфиг верно
   и раньше.
7. **Ratchet `FILE_LINES`** по `config_defaults.py` устранён сжатием комментариев (3139→3138).

### Тесты

* `tests/hermes_cli/test_kanban_auto_continue.py` — **15 passed** (сценарии 1–6 + чистота `plan_*`).
* `tests/hermes_cli/test_kanban_cli_failure_limit_config.py` — **5 passed** (новый, п.8).
* `tests/hermes_cli/test_kanban_db.py` — 30 passed, 1 skipped (обновлён
  `test_stale_claim_reclaim_without_spawn_counts_toward_breaker`: `ready` + `auto_continued`).
* `tests/hermes_cli/test_kanban_owner_lost_budget.py` — обновлён + новый тест на `auto_continue: false`.

### Проверки

* `python -m scripts.code_health --base HEAD` → **0 blocking, 0 advisory** (8 файлов).
* `ruff check` по всем изменённым файлам → **All checks passed** (1 автофикс сортировки `__all__`).
* Известный шум окружения: `tests/hermes_cli/test_kanban_db.py` завершается ненулевым кодом из-за
  `tests/home_io_guard.py`, ругающегося на путь тест-venv `~/.hermes/installs/.../site-packages`.
  **Воспроизведено на базе (git stash → тот же отказ) — не регрессия этой карточки**; все 30 тестов
  проходят.

### Инцидент по пути (зафиксировано)

`refs/stash` общий на весь репозиторий, поэтому `git stash pop` в этом worktree притянул чужой WIP
карточки `t_afdf38ac` (`gateway/session_worker_construct.py`,
`tests/gateway/test_named_route_applies_card_key.py`). Чужие файлы из моего дерева убраны
(`git reset` + `git checkout --` + `rm`), чужой патч целиком сохранён в `stash@{1}` — ничего не
потеряно. Стэши не удалял: они общие и могут понадобиться той карточке.
**Урок: не пользоваться `git stash` в worktree при параллельных карточках; для проб базы брать
`git worktree add` или `git show HEAD:<file>`.**

### Осталось

1. Широкий регресс по канбану + точечный коммит.
2. Отчёт `REPORT-auto-continue.md` + живая проба.
3. Отчёт о самопроверке по чек-листу ревьюера 09 — комментарием к карточке.
4. `manual_review`: увидеть `auto_continued` / `auto_split` в `hermes kanban show` на реальном
   прогоне после перезапуска шлюза.
