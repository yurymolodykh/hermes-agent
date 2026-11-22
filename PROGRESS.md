# PROGRESS — t_84ccbf64 (404 при скачивании файла из чата: у платформы api_server нет файловых маршрутов)

## Состояние: код готов и проверен, ждёт слияния оркестратором

Ветка `wt/t_84ccbf64`, worktree `/home/hermes/.hermes/hermes-agent/.worktrees/t_84ccbf64`,
база `bc8ecfaee2` (`feature/multiuser-access`).

## Сделано

1. `gateway/platforms/api_server_fs.py` (новый) — read-only `GET/HEAD /api/fs/download` и
   `GET /api/fs/read-data-url` на платформе api_server; правила импортируются из панели
   (`_fs_download_path`, `_fs_owned_session`, `_fs_regular_file`, `_hosted_fs_read_guard`,
   `_refuse_live_database`, `_remote_fs_backend`, пределы 100 МиБ / data-url).
   Дополнительно — гейт `base._path_under_denied_prefix` (denylist исходящей доставки): из-за него
   `~/.ssh/*`, `~/.aws/*` дают 403, а не 200 (панельный гейт — denylist только по имени файла).
2. `gateway/platforms/api_server.py` — `routes.extend(_api_fs.http_routes(self))`; SSE-примитивы
   вынесены в `api_server_sse.py` с ре-экспортом из фасада (ratchet FILE_LINES: фасад был 4139
   строк при пределе 2000 и обязан уменьшаться).
3. `tests/gateway/test_api_server_fs_download.py` (новый) — 19 тестов на реальном aiohttp-сокете.
4. `verify-fs-download-live.py` — живая проба на реальном адаптере против реального `HERMES_HOME`
   теми же запросами, что делает клиент (`?path=…&session_id=…`, Bearer API_SERVER_KEY).

## Проверки

* 19 passed — `tests/gateway/test_api_server_fs_download.py`
* 48 passed — затронутые соседи + `tests/hermes_cli/web_routers` (0 failed)
* живая проба: **24/24** в ветке, **4/24** на базе `bc8ecfaee2`
* `scripts.code_health --base bc8ecfaee2`: 0 blocking, 0 advisory; ruff (pre-commit) — чисто
* падения вне карточки, воспроизведены на чистой копии базы в `cache/scratch/base-t_84cc`:
  `test_web_server_files.py` 2 (`503 Session store is not initialized`),
  `test_api_server.py` 16 (`TEST BUG: file I/O against the REAL hermes home`),
  `test_platform_base.py::TestGetHumanDelay` 6

## Осталось (не участок карточки)

1. **Слияние** `wt/t_84ccbf64` → `feature/multiuser-access` — оркестратор (страж деревьев не
   пускает карточку в общее дерево `~/.hermes/hermes-agent`).
2. **Критерий 6**: после слияния и тихого рестарта шлюза прогнать `verify-fs-download-live.py`
   на боевом `127.0.0.1:8642` (до слияния там 404: слушатель работает из общего дерева — это и
   есть исходный дефект).
3. **`manual_review`**: владелец нажимает «Скачать» в чате и получает файл.

Полный отчёт: `REPORT-fs-download.md`. Выводы проб: `PROBE-fs-download-worktree.txt` и
`/home/hermes/.hermes/profiles/02-hermes-platform/cache/scratch/base-t_84cc/PROBE-fs-download-baseline.txt`.

---

## Устаревший файл другого worktree

Ниже — содержимое, оставшееся от карточки **t_af262aaf** (тот же worktree переиспользован).
К этой карточке отношения не имеет, сохранено только как история.

# PROGRESS — t_af262aaf (лимит итераций теряет готовую работу: сохранять до остановки)

## Состояние: РАБОТА ЗАВЕРШЕНА И ЗАКОММИЧЕНА (код, 211 тестов, проба 32/32, база красная 16 LEAK)

* Верхушка ветки: `3ab1936e05` (3 коммита над `0a1960695e`: код 9f7f8b581c, проба 08c4d6fbd4,
  фикс линта пробы 3ab1936e05)
* Ветка: `wt/t_af262aaf`, worktree `.worktrees/t_af262aaf`
* Отчёт: `REPORT-budget-stop.md`
* Проба: `verify-budget-stop-live.py` + `PROBE-budget-stop-output.txt` (32/32)
  Красная база: `verify-budget-stop-baseline-red.py` (16/32, 16 LEAK на `0a1960695e`)

### Что сделано

1. **Сохранение до предела.** `agent/turn_iteration_prep.py` (85 %, было 0,9) пакует дерево
   под `refs/hermes-wip/<task>` — без коммита в ветку. `agent/turn_finalizer.py::
   _checkpoint_work_before_stop` на пределе коммитит содержательные файлы в ветку с темой
   `WIP бюджет: N/N` ДО `_record_kanban_budget_exhausted`. Новый модуль
   `agent/kanban_budget_checkpoint.py` (checkpoint_wip / preserve_wip_ref / card_workspace /
   describe_stop), tar.gz-фолбэк в `task_attachments_dir` без git.
2. **Честный статус.** `describe_stop` пишет «остановлено по бюджету N/N · работа в коммит X
   (ветка B) · сохранено файлов: K (…) · осталось: …» в `last_failure_error` и в событие
   `timed_out` (`budget_used/budget_max/stop_note`).
3. **Размер карточки.** `agent/kanban_card_sizing.py`: `40 + 30 × пунктов приёмки`.
4. Вынос `describe_suppression` в `hermes_cli/kanban_db_suppression.py`.

### Осталось (t_af262aaf)

1. Слияние в `feature/multiuser-access` — оркестратор. Верхушка: `3ab1936e05`.
2. `manual_review` после перезапуска шлюза.
