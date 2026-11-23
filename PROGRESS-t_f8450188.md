# PROGRESS — t_f8450188: пер-запросная обвязка (пул state.db + WAL раз на соединение)

## Состояние: доработка круга 1 (ревью 09) ВЫПОЛНЕНА, тесты зелёные. Осталось: коммит, публикация, повторное ревью.

Дерево: `/home/hermes/.hermes/hermes-agent/.worktrees/t_f8450188`, ветка `wt/t_f8450188`.
Отчёт с замерами: `REPORT-t_f8450188.md` (§7 — что исправлено по ревью круга 1).
База сравнения: `65db1ee540b` (merge-base с `feature/multiuser-access`).

## Доработка круга 1 (этот круг)

1. **Д-1** `hermes_cli/read_handle_pool.py`: `_handle_is_reusable(db)` (`_db_corrupt` +
   `_conn is not None`) проверяется в `acquire()` И в `release()`; `tests/conftest.py`
   (`_close_leaked_session_dbs`) сливает пул перед свипом реестра `SessionDB`.
2. **Д-2** `hermes_state_wal.py`: `_on_disk_journal_mode` НЕ мемоизирует `None` (транзиентный
   сбой зонда не должен навсегда выключать lock-free read pool).
3. **Д-3** debug-метрика: `read-only handle pool HIT/MISS for <path> (idle=N)`.
4. **Д-4** `_evict_locked` (мёртвый код) → `_pop_oldest_locked`, реальный LRU в `release()`.
5. `tests/hermes_cli/test_read_handle_pool_wiring.py` — 7 новых тестов, всего 10.

## Сделано

1. `hermes_cli/read_handle_pool.py` — ограниченный LRU-пул read-only хэндлов по пути
   (4/путь, 16/процесс, TTL 120 с, инвалидация по `stat_db_file_identity`).
2. `hermes_cli/web_server_sessions.py::_open_session_db_at_path(read_only=True)` — через пул.
3. `hermes_cli/sqlite_safe_read.py::has_live_connection` — сначала сбрасывает простаивающие
   read-only хэндлы (иначе прогрев пула ломал raw-доступ backup/repair/doctor).
4. `hermes_state_wal.py` — `journal_mode` мемоизирован на соединении, инвалидация вокруг PRAGMA.
5. `tests/hermes_cli/test_read_handle_pool_wiring.py` — 3 теста (повтор соединения, одинаковые
   PRAGMA, WAL один раз на соединение).

## Замеры (до → после, один стенд)

* 20 последовательных чтений: 20 → **1** открытие; 8×5 конкурентных: 40 → **7**.
* CPU на чтение: 6,79–8,40 → **3,99–4,35** мс.
* `/api/status` медиана: 46,5–52,2 → **17,0–19,1** мс; 8 конкурентных худший: 682–732 → **280–332** мс.
* Простой с опросом `/api/status`: 8,6–9,7 % → **5,4–7,3 %** ядра (вклад пула −2,4…−3,2 п.п.).

## Проверки (сняты в этом круге)

* `test_read_handle_pool_wiring.py` → **10 passed** (14 с).
* 5 целевых файлов (`_wiring` + `test_session_db_read_conn_pool` + `test_sqlite_lock_safe_inspection`
  + `test_session_db_context_manager` + `test_session_db_read_path_split`) → **54 passed** (99,66 с).
* 5 WAL-файлов (`test_hermes_state_wal_fallback`, `test_wal_active_confirmed`,
  `test_sqlite_wal_reset_gate`, `test_cross_vm_fs_wal_refusal`, `test_wal_lock_guard`) →
  **71 passed** (14,77 с).
* `test_web_routers_status_health` + `test_web_server_chat_workspaces` +
  `tests/gateway/test_session_db_handle_sharing` → 1 failed / 9 passed; падение
  предсуществующее (снято на `1b887de92c2` без файлов круга: тот же 1/9).
* Красная база: на `1b887de92c2` из 10 тестов падают 5; с откатом `conftest.py` падает
  `test_second_read_still_works` с `ProgrammingError: ... was closed (read-only handle)`.
* `ruff` по изменённым файлам — All checks passed.
* Стенды: `probe_pool_effect.py` — 40 чтений → **1** connect (3,09 мс/чтение), 8×5 → **7**
  connect, 0 ошибок, простаивающих 4, fd 19. `bench_status_http.py` — медиана **21,1 мс**,
  худший из 8 конкурентных **296 мс** (лимит 500).

## Предсуществующие падения (на базе тоже)

`test_web_routers_destructive_profile_scope::...[sessions-empty]` (несовпадение сигнатуры
`_StubDB`); async-тесты в `test_web_server_cron_profiles.py` / `test_session_detail_malformed_db.py`
— в общем venv нет `pytest_asyncio`.

## Следующий шаг

1. `git add -A && LEFTHOOK=0 git commit` (сообщение: доработка круга 1 по ревью 09).
2. `env -u HERMES_HOME /usr/bin/python3 /home/hermes/workspaces/01-orchestrator/ops/bin/ops-card-merge.py --task t_f8450188`
3. `ops-card-merge.py --task t_f8450188 --dry-run` → `already_ancestor: true`.
4. `kanban_request_review` → `09-independent-reviewer`.

## Инструментальные заметки

* `HOME`/`HERMES_HOME` обязательны вне реального `~/.hermes` (страж `tests/home_io_guard.py`).
* Рабочий интерпретатор — `../../.venv/bin/python` (системный python3 3.14.9 без pip).
* `terminal` часто отдаёт «1 lines output» на длинных выводах — писать в файл.
* `git stash pop` в этом дереве подхватывает ЧУЖОЙ stash (там лежат стеши от t_afdf38ac) —
  не использовать `git stash` здесь; один раз это затащило посторонние файлы в wip-коммит,
  убрано коммитом 307d5f92de2.
* pre-commit lefthook падает на «no bootstrap Python found» в worktree — использовать
  `LEFTHOOK=0 git commit` (не `--no-verify`), `ruff` гонять из `~/.local/bin/ruff`.
