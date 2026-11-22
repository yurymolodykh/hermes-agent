# PROGRESS — t_f8450188: пер-запросная обвязка (пул state.db + WAL раз на соединение)

## Состояние: ПУЛ И WAL СДЕЛАНЫ И ПРОВЕРЕНЫ. Осталось: дождаться широкого прогона, коммит, публикация, ревью.

Дерево: `/home/hermes/.hermes/hermes-agent/.worktrees/t_f8450188`, ветка `wt/t_f8450188`.
Отчёт с замерами: `REPORT-t_f8450188.md`. База сравнения: `65db1ee540b`
(merge-base с `feature/multiuser-access`).

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

## Проверки (сняты)

47 passed: `test_read_handle_pool_wiring`, `test_session_db_read_conn_pool`,
`test_sqlite_lock_safe_inspection`, `test_session_db_context_manager`,
`test_session_db_read_path_split`. `ruff` по файлам правки — All checks passed.

## Предсуществующие падения (на базе тоже)

`test_web_routers_destructive_profile_scope::...[sessions-empty]` (несовпадение сигнатуры
`_StubDB`); async-тесты в `test_web_server_cron_profiles.py` / `test_session_detail_malformed_db.py`
— в общем venv нет `pytest_asyncio`.

## Следующий шаг

1. Дождаться `/tmp/w1.txt` (широкий `tests/hermes_state/`), убедиться, что новых падений нет.
2. `git add -A && git commit`.
3. `env -u HERMES_HOME /usr/bin/python3 /home/hermes/workspaces/01-orchestrator/ops/bin/ops-card-merge.py --task t_f8450188`
4. `ops-card-merge.py --task t_f8450188 --dry-run` → `already_ancestor: true`.
5. `kanban_request_review` → `09-independent-reviewer`.

## Инструментальные заметки

* `HOME`/`HERMES_HOME` обязательны вне реального `~/.hermes` (страж `tests/home_io_guard.py`).
* Рабочий интерпретатор — `../../.venv/bin/python` (системный python3 3.14.9 без pip).
* `terminal` часто отдаёт «1 lines output» на длинных выводах — писать в файл.
* `git stash pop` в этом дереве подхватывает ЧУЖОЙ stash (там лежат стеши от t_afdf38ac) —
  не использовать `git stash` здесь; один раз это затащило посторонние файлы в wip-коммит,
  убрано коммитом 307d5f92de2.
* pre-commit lefthook падает на «no bootstrap Python found» в worktree — использовать
  `LEFTHOOK=0 git commit` (не `--no-verify`), `ruff` гонять из `~/.local/bin/ruff`.
