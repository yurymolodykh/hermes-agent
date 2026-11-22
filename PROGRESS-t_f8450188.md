# PROGRESS — t_f8450188: пер-запросная обвязка (пул state.db + WAL раз на соединение)

## Объём (сужен): только п.1 (пул соединений state.db) и п.2 (WAL-проверка раз на соединение)

## Состояние: правки A и B внесены, тесты зелёные. Осталось: пул read-only (главный выигрыш), коммит, публикация.

Дерево: `/home/hermes/.hermes/hermes-agent/.worktrees/t_f8450188`, ветка `wt/t_f8450188`.

## Сделано (в рабочем дереве, не закоммичено)

### Правка A — кэш сканирования манифестов плагинов (`gateway/config.py`)

`_bundled_platform_scan_cached` + `_bundled_platform_scan_signature`: мемоизация листинга
`plugins/platforms/` на файловой подписи (mtime каталога + (name, mtime_ns, size) каждого манифеста).
Инвалидация автоматическая. Наружу отдаются копии. Замер: 26,1 → 24,2 мс на `/api/status` (~7%).

### Правка B — `journal_mode` один раз на соединение (`hermes_state_wal.py`)

`_probe_journal_mode` (прежняя логика) + `_on_disk_journal_mode` мемоизирует в `conn._journal_mode_memo`.
Новое соединение мемо не имеет → зонд обязателен конструктивно. `invalidate_journal_mode_memo`
вызывается вокруг каждого `PRAGMA journal_mode=...` (`_set_journal_mode_no_wait`, `_enable_wal`,
EIO-retry). Замер: `_open_writer_conn` 2 зонда → 1.

## Замеры (реальный стенд: TestClient над hermes_cli.web_server.app, temp HERMES_HOME, 120×4)

| метрика | до | после |
|---|---|---|
| `/api/status` последовательно | 26,1 мс | 24,2 мс |
| 8 конкурентных `/api/status` | wall 374 мс, max 371 мс, 0 ошибок | wall 394 мс, max 393 мс, 0 ошибок |
| `_count_status_active_sessions` (open+read+close) | 13,75 мс/вызов | — |
| тот же путь на переиспользуемом read-only хэндле | — | **5,49 мс (−60%)** |
| read-only open: доля `apply_database_pragmas` | 1,80 из 2,23 мс (47%) | — |
| `open+close` без PRAGMA / с `cache_size=-8000` | 0,19 / 1,23 мс | — |

Вывод: резерв — переиспользование read-only хэндла (−60%), не кэш PRAGMA
(`cache_size=-8000` аллоцирует 8 МБ на каждое соединение).

## Следующий шаг

1. Пул read-only attach'ей: `_open_session_db_at_path(read_only=True)` открывает новый SessionDB на
   каждый запрос; `hermes_state_readpool.py` действует только для writable-хэндлов. Нужен
   ограниченный LRU по пути с инвалидацией по `_stat_db_file_identity` и TTL. Вызывающие:
   `web_routers/status.py:86`, `sessions.py:209,1069`, `analytics.py:87,310`, `cron.py:521`,
   `chat_workspaces.py:50`, `profiles.py:321` — возвращают хэндл вместо `close()`.
   ВНИМАНИЕ: тесты вида `_live_count(path) == 0` после `close()` потребуют обхода в test-isolation.
2. Тесты: WAL на свежем соединении; пул не отдаёт соединение с чужими настройками.
3. Коммит, `ops-card-merge.py --task t_f8450188`, `kanban_request_review` (09-independent-reviewer).

## Проверки (сняты)

`HOME=/tmp/fh ../../.venv/bin/python -m pytest tests/hermes_state/test_session_db_read_conn_pool.py
tests/hermes_state/test_session_db_read_path_split.py -q` → **32 passed** (17,27 с).
`HOME` обязателен вне реального `~/.hermes` (autouse-страж `tests/home_io_guard.py`).
`run_tests.sh` в worktree не поднимает bootstrap Python — использовать `../../.venv/bin/python -m pytest`.

## Инструментальные заметки

`terminal` систематически отдаёт «1 lines output» на длинных выводах — писать в файл и читать окнами.
Рабочий интерпретатор — `../../.venv/bin/python` (системный python3 3.14.9 без pip).
