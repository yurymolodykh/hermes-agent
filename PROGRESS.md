# PROGRESS — t_6c16a6cb (вынос блокирующих обработчиков из цикла событий)

**Состояние: исполнение сделано, дефекты выноса найдены и закрыты, тесты прогнаны, REPORT.md
написан. Осталось: повтор прогона `probe_sqlite_threads.py` и `scripts/iso-certify.py`
(не поместились в бюджет), публикация — гейт оркестратора.**

Отчёт с таблицами до/после, списком тестов и процедурой отката:
`durable/t_a61becbc/REPORT.md`.

## Шов

`hermes_cli/web_routers/handler_offload.py` — `HANDLER_OFFLOAD.install(app, workers=None)` /
`.shutdown()` / `.reset()` / `.resolve()`, `off_loop()`, `offload_status()`.
Приватный `ThreadPoolExecutor` (2×CPU, floor 8, cap 32), `contextvars.copy_context()` в воркер.
Выключатель: `dashboard.handler_offload: false` **или** `HERMES_HANDLER_OFFLOAD=0` → inline
на цикле (прежнее поведение, без правки файлов). Режим логируется однократно при старте и
отдаётся в `/api/status` → `handler_offload: {mode, enabled, workers, submitted, inline,
in_flight, queue_depth, max_queue_depth, avg_wait_ms}`.

## Переведено off-loop (29 из 37 аудита; 8 остальных — осознанно)

files.py (`fs_write_text`, `upload_managed_file_stream`, `fs_git_root`, `get_media`),
ops.py (backup / download-backup / import), local_models.py (delete / sideload),
profiles.py (open-terminal, import, is_file), status.py (`get_logs`), dashboard_ui.py
(`serve_plugin_asset`), chat_ws.py (`_pty_channel_marker_state`).

Не переведён `messaging.py:start_whatsapp_onboarding` — перевод ломал 21 тест
`test_whatsapp_dashboard_repair` (парный поток создаётся в контексте запроса); путь разовый (QR),
причина записана комментарием в файле. Остальные 5 — не блокировки (`sleep` в ожидающих петлях,
`await asyncio.sleep(0)`, `wait` на сокете, и ложное срабатывание AST-аудита `str.replace`).

## Дефекты, которые породил сам вынос (исправлены, доказаны красными)

1. **Гонка стейджа в `files.py:_fs_write_text_blocking`.** Имя `.<name>.hermes-tmp-<pid>`
   постоянно для процесса; в пуле два сейва одного файла берут ОДИН путь → второй падает ENOENT
   и в `except` удаляет файл победителя. Замер: 8 одновременных 8-МиБ сейвов — **6 из 8 по 500**;
   доказательство `.probe-tmp/proof_concurrent_save.py` (`ok=2 failed=6`). Фикс: `token_hex(6)`.
   Тест `tests/hermes_cli/test_fs_write_text_offload.py`.
2. **Счётчики пула отдавались из TTL-кэша `/api/status`** (TTL 1,5 с) — при всплеске 64
   запросов отдавалось `max_queue_depth: 0`, хотя очередь была. Фикс: поле подмешивается после
   кэша. Тест `tests/hermes_cli/test_status_offload_live.py`; красное доказательство
   `.probe-tmp/proof_status_cache.py` (`2 failed`).

## Тесты

Раннер: `HERMES_PYTHON=/home/hermes/.hermes/hermes-agent/.venv/bin/python HOME=/tmp/t6c
scripts/run_tests.sh …` (иначе `run-in-hermes-env` ищет bootstrap-Python и падает).

- 19 файлов: **351 passed, 1 failed, 11 skipped**. Провал — `test_status_probe_off_loop`
  («async def functions are not natively supported»), **тот же провал на базе** `dde0f0befbc`.
- Пред-существующие провалы подтверждены прогоном в `.probe-tmp/base-tree` (detached
  `dde0f0befbc`): `test_serve_handler_ledger` 1 failed, `test_webapp_uploads` 1 failed,
  `test_webapp_upload_security` 2 errors — ровно те же на базе.
- Новые этой карточкой: `test_handler_offload.py` (11), `test_fs_write_text_offload.py` (1),
  `test_status_offload_live.py` (2).

## Замеры (файлы в `durable/t_a61becbc/`)

- `probe_handler_lag.py` — прямой замер предмета: max разрыв цикла inline **110,9 / 795,7 мс**
  против threaded **78,8 / 38,6 мс**; в inline тикер просыпается ОДИН раз за всплеск.
- `probe_pool.py` — 64 запроса на пул из 16: 64×200, `max_queue_depth=24`, очередь работает,
  таймаутов нет.
- `probe_blocking_write.py` — A/B 8 МиБ×8: threaded 8×200, inline 8×200; остаток лага ~140 мс —
  разбор JSON-тела на цикле, не вынесенное тело.
- `ab_offload.py` + `ab-curve.json` / `ab-loop.json` — A/B с переключением выключателя, 2 повтора.
  **Выигрыша в латентности на sessions/search нет** (разброс повторов больше разницы режимов):
  эти маршруты уже читают БД через `to_thread`. Лаг цикла был мал и до правки (0 превышений
  50 мс в обоих режимах).
- `curve-after.json` / `after.json` сняты, но с `curve-before.json` **несравнимы**: разное время,
  разная посторонняя нагрузка (load average 3,2–4,6 на 8 ядрах). Сравнивать надо по `ab-*.json`.

## Осталось (не выполнено, честно)

1. `probe_sqlite_threads.py` **не поднялся** — сервер не отдал `READY` в отведённое время,
   проба вышла «server failed». Сквозного подтверждения «нет `ProgrammingError: created in a
   thread`» на этом стенде нет; правило в коде соблюдено (`_with_db` открывает и закрывает
   соединение внутри воркера, объект `db` границу потока не пересекает).
2. Сценарий (в) `scripts/iso-certify.py --duration-s 120` не прогонялся — бюджет исчерпан.
3. Публикация — гейт оркестратора; выкатка/рестарт — за оркестратором.

## Процедура аварийного отката (одна команда, без правки файлов)

```yaml
dashboard:
  handler_offload: false      # ~/.hermes/config.yaml
```
```bash
hermes serve --restart        # либо: systemctl --user restart hermes-gateway
# эквивалент без правки конфига: Environment=HERMES_HANDLER_OFFLOAD=0 в юните
```
В журнале: `handler offload mode=inline (single-process)`; `/api/status` → `mode = "inline"`.
