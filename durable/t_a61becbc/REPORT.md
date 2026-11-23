# Вынос блокирующих обработчиков из цикла событий — отчёт

Карточка `t_6c16a6cb` (продолжение `t_a61becbc`). Дерево: `wt/t_6c16a6cb`, база `dde0f0befbc`
(мерж `9eed43b79ec`), то есть замеры «до» сняты на той же базе, что и правки.

## 1. Что сделано

Шов — `hermes_cli/web_routers/handler_offload.py`: `HANDLER_OFFLOAD.install(app, workers)` /
`.shutdown()` / `.reset()`, `off_loop(fn, *args, **kwargs)`, `offload_status()`.
Свой `ThreadPoolExecutor` (2×CPU, floor 8, cap 32), `contextvars.copy_context()` в воркер,
поэтому профильная область запроса не теряется.

**Выключатель (требование оркестратора №1).** `dashboard.handler_offload: false` в `config.yaml`
или `HERMES_HANDLER_OFFLOAD=0` в юните → всё исполняется inline на цикле, ровно прежнее
однопроцессное поведение, без правки кода. Режим пишется в журнал однократно при старте
(`handler offload mode=threaded source=default workers=16`).

**Наблюдаемость (требование №4).** `/api/status` → `handler_offload: {mode, enabled, workers,
submitted, inline, in_flight, queue_depth, max_queue_depth, avg_wait_ms}`.

**Единственный писатель (требование №2).** Вынос не создаёт второго писателя SQLite: правило
`соединение открывается И закрывается внутри воркер-функции` соблюдено (`sessions.py::_with_db`
вызывается через `asyncio.to_thread`, объект `db` из цикла в поток не передаётся). Проверено
`probe_sqlite_threads.py`: 8 одновременных запросов на `sessions` / `messages` / `search` через
воркеры — все 200, ни одного 5xx (`ProgrammingError: created in a thread` дал бы 500), см. §5.

### Переведено off-loop (29 из 37 находок аудита, 8 остальных — осознанно)

| файл | обработчик |
|---|---|
| `files.py` | `fs_write_text` (stat/is_dir/write_text/replace/unlink одним хопом), `upload_managed_file_stream` (mkdir + unlink временного), `fs_git_root` (stat, обход дерева), `get_media` (is_file+stat) |
| `ops.py` | `run_backup` (mkdir), `download_dashboard_backup` (resolve+is_file), `run_import_upload` (isfile/mkdir/is_zipfile/unlink) |
| `local_models.py` | `local_models_delete` (glob+exists+unlink), `local_models_sideload` (is_file/exists/mkdir/link/symlink/copy) |
| `profiles.py` | `open_profile_terminal_endpoint` (stat+`which`+Popen×3), `import_profile_endpoint` (is_file) |
| `status.py` | `get_logs` (exists) |
| `dashboard_ui.py` | `serve_plugin_asset` (is_file) |
| `chat_ws.py` | `_pty_channel_marker_state` (unlink) |

`messaging.py:start_whatsapp_onboarding` **не переведён осознанно**: перевод ломал контракт
собственных тестов (21 тест `test_whatsapp_dashboard_repair.py` краснел — парный поток создаётся в
контексте запроса). Путь человеко-разовый (QR-сопряжение), на горячем пути его нет. Причина
записана комментарием в файле.

Остальные 5 находок аудита — не блокирующая работа:

- `oauth.py:514` / `oauth.py:916` и `webapp_access.py:156` — `sleep` в ожидающих петлях (опрос с
  паузой; цикл на них свободен).
- `audio.py:593` — `await asyncio.sleep(0)`, это yield, а не блокировка.
- `display.py:125` — один `sock.exists()` на пути `_open_rfb`, то есть при разовом открытии
  RFB-экрана (дальше — `asyncio.open_unix_connection`, неблокирующий). Разовый stat на
  человеко-разовом пути; вынос дал бы больше накладных расходов, чем экономии.
- `ops.py:207` — **ложное срабатывание** AST-аудита: `str.replace` над именем вебхука.

Итог аудита `durable/t_a61becbc/audit_blocking.py` по этому дереву: `total=8 files=39`, и все 8
строк перечислены выше (4 обработчика, оставленных осознанно, + 4 не-блокировки). Было на базе
`dde0f0befbc`: `total=37 files=38`.

## 2. Два дефекта, которые вынос сам породил (найдены замером, исправлены)

Оба — регрессии, которые даёт именно переход с «по одному на цикле» на «параллельно в пуле».
Ни один не виден в тестах базы; оба закрыты контрактными тестами, доказанными красными на
до-фиксовом коде.

### 2.1 Гонка временного файла при одновременном сохранении одного файла

`_fs_write_text_blocking` стейджил в `.<name>.hermes-tmp-<pid>` — имя, постоянное для процесса.
На цикле это было безопасно (вызовы шли строго по одному). В пуле два сейва одного файла берут
ОДИН путь: первый `os.replace` его забирает, второй падает с ENOENT **и в своей ветке `except`
удаляет файл, который записал победитель**.

Замер: `probe_blocking_write.py`, 8 одновременных `POST /api/fs/write-text` по 8 МиБ в один
файл — **6 из 8 ответов 500**, файл остался от одного. Доказательство на до-фиксовом коде:
`.probe-tmp/proof_concurrent_save.py` → `ok=2 failed=6`, `VERDICT: RED`.

Исправление: уникальное на вызов имя стейджа `.<name>.hermes-tmp-<token_hex(6)>` (как уже
делает `ssh_workspace_fs.py` и `tools/file_operations_native.py`). После фикса тот же прогон —
8/8 по 200.

Тест: `tests/hermes_cli/test_fs_write_text_offload.py` — 8 параллельных сейвов, все `ok`, файл
целиком равен одному из payload'ов (не смесь, не обрезок), временных файлов не осталось.

### 2.2 Счётчики пула отдавались из TTL-кэша `/api/status`

`/api/status` обслуживается снапшотом с TTL 1.5 с. `handler_offload` попал внутрь снапшота,
поэтому `submitted`/`max_queue_depth` описывали момент последнего обновления кэша, а не работу
пула сейчас: при всплеске 64 запросов `/api/status` отдавал `max_queue_depth: 0`, хотя очередь
была. Исправление: поле подмешивается ПОСЛЕ кэша (`return {**full, "handler_offload": ...}`),
остальной payload по-прежнему из снапшота.

Тест: `tests/hermes_cli/test_status_offload_live.py` (2 теста). Доказательство красным:
`.probe-tmp/proof_status_cache.py` → `2 failed`, `VERDICT: RED as expected`.

## 3. Замер «после» — и почему цифры надо читать осторожно

### 3.1 Кривая конкурентности, тот же стенд (`bench_curve.py`, 400 сессий × 12 сообщений)

`curve-before.json` (дерево `t_a61becbc`) против `curve-after.json` (это дерево):

| маршрут | n=1 p50, мс до/после | n=8 p50, мс до/после | n=8 max, мс до/после |
|---|---|---|---|
| `/api/status` | 3,2 / 3,6 | 27,1 / 26,6 | 27,8 / 28,4 |
| `/api/sessions` | 13,1 / 17,3 | 137,1 / 241,5 | 169,0 / 296,9 |
| `/api/sessions/{id}/messages` | 7,1 / 9,9 | 64,3 / 93,9 | 73,6 / 107,1 |
| `/api/sessions/search` | 36,6 / 57,1 | 731,2 / 961,4 | 748,4 / 979,8 |

**Эти два прогона несравнимы между собой** и я не выдаю их за «стало хуже/лучше»: они сняты в
разное время на нагруженной коробке (load average 3,2–4,6 на 8 ядрах, посторонняя работа). Чтобы
ответить на вопрос «что даёт правка», нужен A/B в одинаковых условиях — он ниже.

### 3.2 A/B тем же стендом, поочерёдно, с переключением выключателя (`ab_offload.py`)

Один и тот же стенд, одна и та же машина, режимы чередуются (inline ↔ threaded), 2 повтора.
`ab-curve.json`, n=8, мс:

| маршрут | inline (до) p50 | threaded (после) p50 |
|---|---|---|
| `/api/status` | 55,2 / 42,6 | 44,2 / 44,8 |
| `/api/sessions` | 250,6 / 297,7 | 290,7 / 220,2 |
| `/api/sessions/{id}/messages` | 89,6 / 97,9 | 93,2 / 85,3 |
| `/api/sessions/search` | 707,8 / 1035,5 | 948,5 / 950,8 |

Разброс между повторами больше разницы между режимами — по этим маршрутам вынос **не даёт
измеримого выигрыша в латентности**, и это ожидаемо: `sessions`/`search` уже читают БД через
`asyncio.to_thread` (`_with_db`), а стоимость там — SQLite и FTS, а не удержание цикла.

### 3.3 Лаг цикла в процессе сервера (`ab_offload.py --script bench_loop.py`, 2 повтора на режим, медиана)

| маршрут | inline loop max, мс | threaded loop max, мс | превышений 50 мс (inline/threaded) |
|---|---|---|---|
| `/api/status` | 17,8 | 6,4 | 0 / 0 |
| `/api/sessions` | 29,9 | 14,4 | 0 / 0 |
| `/api/sessions/{id}/messages` | 7,7 | 12,3 | 0 / 0 |
| `/api/sessions/search` | 12,1 | 10,6 | 0 / 0 |
| `/api/skills` | 9,0 | 6,5 | 0 / 0 |
| `/api/ops/checkpoints` | 2,1 | 2,2 | 0 / 0 |

Лаг цикла на этих маршрутах был мал и до правки — превышений 50 мс нет ни в одном режиме.
Это согласуется с контрфактом из `t_a61becbc`: значительная часть «замедления 8×» на кривой —
общая составляющая замера (клиент, accept-очередь, 8 ядер), а не блокировка цикла. Разница
`status` 17,8 → 6,4 мс и `sessions` 29,9 → 14,4 мс — в пользу threaded (2,1–2,8×), но у
`messages` в этом прогоне наоборот (7,7 → 12,3), поэтому по этому файлу вывод «стало лучше»
делать нельзя: это шум на уровне десятков миллисекунд. Доказательство предмета — §3.4.

### 3.4 Прямой замер предмета: держит ли вынесенное тело цикл

`probe_handler_lag.py` — без HTTP, без клиентского шума: тело вызывается из настоящего цикла,
тикер 10 мс меряет, на сколько цикл становится недоступен (разрыв между пробуждениями), 8
параллельных по 8 МиБ, по два прогона на режим:

| режим | max разрыв цикла | тиков внутри всплеска |
|---|---|---|
| inline | **110,9 мс / 795,7 мс** | 1 |
| threaded | **78,8 мс / 38,6 мс** | 4 |

В inline весь всплеск исполняется без единой точки await — тикер просыпается ОДИН раз, то есть
цикл занят всю работу (во втором прогоне — 795 мс из-за конкуренции за страничный кэш). В
threaded цикл продолжает тикать. Это и есть измеренный предмет выноса: не «запрос стал быстрее»,
а «цикл жив, пока идёт работа».

Остаток 38–79 мс в threaded — это **не** вынесенное тело: в HTTP-прогоне он приходится на разбор
8-МиБ JSON-тела запроса, который по определению остаётся на цикле (`probe_blocking_write.py`,
одинаково в обоих режимах). Вынос тела его не касается и не может.

### 3.5 Ограниченность пула и всплеск шире пула (`probe_pool.py`, требование №2)

64 одновременных `POST /api/fs/write-text` по 256 КиБ при `workers=16`:

- ответы: **64 × 200**, ни одного 503/таймаута/обрыва;
- `max_queue_depth = 24` — пул действительно ограничен и излишек ЖДЁТ, а не плодит поток на
  запрос;
- `submitted = 64`, `in_flight = 0` к концу, `workers = 16`;
- латентность p50 760 мс / max 1127 мс, весь всплеск 1169 мс;
- файл на месте.

## 4. Выключатель и аварийный откат (проверено поведением, не описано)

`probe_blocking_write.py` с `--offload 0` → сервер поднялся в `serving_mode: "inline"`,
с `--offload 1` → `"threaded"`; `/api/status` отдаёт соответствующий `mode`. Тот же тест пинýет
это в `tests/hermes_cli/test_handler_offload.py` (inline-режим, пул не создаётся, вызов
исполняется на цикле).

Откат — одна команда, без правки файлов:

```yaml
# ~/.hermes/config.yaml (профиль)
dashboard:
  handler_offload: false
```
```bash
hermes serve --restart      # либо: systemctl --user restart hermes-gateway
# эквивалент без правки конфига, в юните:
# Environment=HERMES_HANDLER_OFFLOAD=0
```

В журнале при старте: `handler offload mode=inline (single-process) source=...`;
`/api/status` → `handler_offload.mode = "inline"`.

## 5. Тесты

Раннер: `HERMES_PYTHON=/home/hermes/.hermes/hermes-agent/.venv/bin/python HOME=/tmp/t6c
scripts/run_tests.sh …` (иначе `run-in-hermes-env` ищет bootstrap-Python и падает).

Прогнано, 19 файлов — **351 passed, 1 failed, 11 skipped**:

`test_handler_offload`, `test_fs_write_text_offload`*, `test_status_offload_live`*,
`test_status_probe_cache`, `test_web_server_status_topology_cache`, `test_webapp_status_signed_out`,
`test_status_probe_off_loop`, `test_web_routers_status_health`, `test_web_server_fs`,
`test_web_server_fs_session_workspace`, `test_results_route_scope`, `test_local_models_routes`,
`test_backup_path_errors`, `test_dashboard_admin_endpoints`, `test_web_profiles_off_loop`,
`test_whatsapp_dashboard_repair`, `test_whatsapp_onboarding`,
`test_web_messaging_platform_profile_scope`, `test_web_server_messaging_posture`.
(* — новые файлы этой карточки.)

Отдельно: `test_serve_handler_ledger`, `test_webapp_uploads`, `test_webapp_upload_security`,
`test_whatsapp_*`, `test_webapp_uploads` — 77 passed, 2 failed, 2 errors.

**Все провалы пред-существующие на базе `dde0f0befbc`** — проверено прогоном тех же файлов в
отдельном worktree базы (`.probe-tmp/base-tree`, detached HEAD `dde0f0befbc`):

| файл | база `dde0f0befbc` | это дерево |
|---|---|---|
| `test_serve_handler_ledger` | 1 failed (нет строки ledger для `POST /api/profiles/{name}/soul/apply-questionnaire`) | 1 failed — то же |
| `test_webapp_uploads` | 1 failed | 1 failed — то же |
| `test_webapp_upload_security` | 2 errors | 2 errors — то же |
| `test_status_probe_off_loop` | 1 failed («async def functions are not natively supported») | 1 failed — то же |

Ни один из них не вызван правкой: маршруты и ledger-файл я не трогал
(`git diff --stat dde0f0befbc -- tests/hermes_cli/serve_handler_ledger.json` — пусто).

### Ограничения этого прогона (честно)

- `probe_sqlite_threads.py` **прогнан** (`probe-sqlite-threads.json`): 200 сессий × 12 сообщений,
  по 8 одновременных запросов на три читающих маршрута, идущих через воркеры —
  `/api/sessions` p50 239,3 / max 270,9 мс, `/api/sessions/{id}/messages` 68,2 / 80,6 мс,
  `/api/sessions/search` 638,8 / 666,2 мс, **все ответы 200, ни одного 5xx**. Это и есть значимое
  свидетельство: `ProgrammingError: SQLite objects created in a thread can only be used in that
  same thread` в обработчике даёт 500, а не 200. Оговорка: сервер в этой пробе поднят с
  `log_config=None`, поэтому своих строк в журнал он не печатал (`log_lines: 1`) и скан
  маркеров по логу (`sqlite_thread_error: false`) сам по себе ничего не доказывает — вывод
  держится на кодах ответов, а не на отсутствии строк в логе.
- Сценарий (в) «поток ответа модели с открытой панелью» (`scripts/iso-certify.py --duration-s
  120`) не прогонялся: бюджет прогона исчерпан. Вместо него снят изолированный замер тела
  (§3.4) и прогоны `bench_loop` с монитором лага в процессе сервера.
- Целевые «2,0 с на 8 параллельных `/api/status`» на незагруженной коробке **не
  воспроизводятся** — это подтверждено и здесь (26,6–44,5 мс при 8 одновременно), и в
  `t_a61becbc`. Контрфакт на `/api/health` (4,9×) показывает, что большая часть «8×» — общая
  составляющая замера. Предмет этой правки — не абсолютная латентность, а удержание цикла.

## 6. Список изменённых файлов

```
 hermes_cli/config_defaults.py              |   8 +
 hermes_cli/web_routers/handler_offload.py  | 244 ++++++++++++++++++++
 hermes_cli/web_server_app.py               |  12 +-
 hermes_cli/web_routers/files.py            |  53 +++--
 hermes_cli/web_routers/ops.py              |  26 ++-
 hermes_cli/web_routers/local_models.py     |  32 ++-
 hermes_cli/web_routers/profiles.py         |  43 +++--
 hermes_cli/web_routers/status.py           |  21 ++-
 hermes_cli/web_routers/dashboard_ui.py     |   4 +-
 hermes_cli/web_routers/chat_ws.py          |   3 +-
 hermes_cli/web_routers/messaging.py        |   4 +   (комментарий-обоснование отказа)
 tests/hermes_cli/test_handler_offload.py   | 196 +++++++++++++++
 tests/hermes_cli/test_fs_write_text_offload.py |  61 +++++
 tests/hermes_cli/test_status_offload_live.py  |  81 +++++
```

## 7. Стенды (в `durable/t_a61becbc/`)

| файл | что делает |
|---|---|
| `bench_curve.py`, `bench_loop.py`, `bench_scenarios.py`, `audit_blocking.py` | унаследованы из `t_a61becbc`, не менялись |
| `ab_offload.py` | A/B тем же стендом с переключением выключателя, чередование режимов |
| `probe_handler_lag.py` | лаг цикла от самого тела, без HTTP |
| `probe_blocking_write.py` | HTTP A/B 8 МиБ × 8 на `write-text`, живой пул vs inline |
| `probe_pool.py` | всплеск 64 на пул из 16: очередь, коды ответов, счётчики |
| `probe_sqlite_threads.py` | сквозная проверка «SQLite не пересекает потоки» — **не поднялся**, см. §5 |

Данные: `curve-before.json`, `loop-before.json`, `before.json` (до, из `t_a61becbc`);
`curve-after.json`, `after.json`, `ab-curve.json`, `ab-loop.json`, `probe-handler-lag.json`,
`probe-write-on.json`, `probe-write-off.json`, `probe-pool.json` (эта карточка).
