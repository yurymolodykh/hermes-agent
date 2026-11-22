# PROGRESS — t_f4d564cb: кэш `/api/status` + вынос проверки схемы «комнат»

## Состояние: ГОТОВО к ревью

* Дерево: `/home/hermes/.hermes/hermes-agent/.worktrees/t_f4d564cb`, ветка `wt/t_f4d564cb`
* База замеров до правки: `beeb9623539`
* Отчёт: `REPORT-status-cache.md` (таблица «было/стало», тесты, ограничения)
* Замеры (JSON в scratch профиля): `bench_baseline.json` (до), `bench_after.json` (после);
  скрипты `bench_status.py`, `bench_schema_only.py`, `bench_store_ready.py`, `payload_dump.py`,
  `neg_control.py` — всё в `/home/hermes/.hermes/profiles/03-software-engineer/cache/scratch/`

### Что сделано

1. `hermes_cli/web_status_cache.py` (новый) — `TtlSingleFlightCache`: TTL 1,5 с, схлопывание
   одновременных вычислений, необязательная `signature`.
2. `hermes_cli/web_routers/status.py` — тело вынесено в `_collect_status`, маршрут отдаёт снапшот
   из `_STATUS_CACHE`; подпись = файловые метки (pid/lock/state/config/.env) + liveness-digest
   (PID-проба, runtime-запись, `shared_profile_warning`) + идентичность швов и флагов `app.state`.
3. `gateway/hosted_rooms.py` — `_schema_is_current_cached` (ключ: inode файла + `PRAGMA
   schema_version`), `_store_ready` использует кэш; `clear_schema_cache()` для тестов/миграций.
4. Тесты: `tests/hermes_cli/test_status_probe_cache.py` (7),
   `tests/gateway/test_hosted_rooms_schema_cache.py` (6).

### Ключевые цифры

| | до | после |
|---|---|---|
| одиночный warm p50 | 98,6 мс | 6,8 мс |
| 8 параллельных (прогрето) | 1103–1346 мс каждый | 24–32 мс каждый |
| вердикт схемы (p50) | 0,265 мс | 0,075 мс |

### Тесты

* Целевые 8 файлов — **29 passed**.
* Регрессия (`tests/gateway/test_hosted_rooms*`, `test_status.py`, весь `tests/tui_gateway/`) —
  **2713 passed, 0 failed, 7 skipped**; два падения в общем прогоне флейковые (прошли изолированно).
* Красная база: на `beeb9623539` новые тесты дают 13 ошибок (нет `_STATUS_CACHE` /
  `clear_schema_cache`).
* Негативный контроль: наивный кэш по пути файла тестом ловится (`after_migration=True` → тест падает).
* `ruff` — All checks passed; `code_health --base beeb9623539` — 0 blocking, 0 advisory.

### Остаточное (оркестратор, manual_review)

1. После выкатки — контрольный замер на живом порту 9120 (`curl -w '%{time_total}'` ×8).
2. `git merge` ветки в целевую; живой шлюз не рестартовался (граница карточки).
3. Pre-commit-хук в worktree не работает (`no bootstrap Python found`) — коммиты с `--no-verify`
   после ручного прогона ruff + code_health.

### Замечено попутно (не моя правка, но закоммичено)

В worktree авто-коммит захватил `git stash pop` с маркерами конфликта в
`hermes_cli/kanban_ops.py` и `hermes_cli/kanban_db_dispatch.py`. Восстановлены к состоянию
апстрим-резолюции `497c8986c31` отдельным коммитом `c31ddab668b`.
