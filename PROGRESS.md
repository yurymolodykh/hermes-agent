# PROGRESS — t_6c16a6cb (вынос блокирующих обработчиков из цикла событий)

**Состояние: исполнение сделано, тесты профильных наборов зелёные (кроме пред-существующих
провалов базы). Остаётся: замер «после» стендом и REPORT.md.**

## Что сделано (проверено)

Шов: `hermes_cli/web_routers/handler_offload.py` — `HANDLER_OFFLOAD.install(app, workers=None)` /
`.shutdown()` / `.reset()`, `off_loop(fn, *args, **kwargs)`, `offload_status()`.
Приватный `ThreadPoolExecutor` (2×CPU, floor 8, cap 32), contextvars копируются в воркер
(`_bind` → `contextvars.copy_context()`), поэтому профиль запроса не теряется.
Выключатель: `dashboard.handler_offload: false` **или** env `HERMES_HANDLER_OFFLOAD=0` → всё
исполняется inline на цикле (прежнее поведение). Режим логируется однократно при старте
(`handler offload mode=threaded source=default workers=16`) и отдаётся в `/api/status` →
`handler_offload: {mode, enabled, workers, submitted, inline, in_flight, queue_depth,
max_queue_depth, avg_wait_ms}`.

### Переведено off-loop (31 из 37 находок аудита)

| файл | обработчик |
|---|---|
| `files.py` | `fs_write_text` (stat/is_dir/write_text/replace/unlink одним хопом), `upload_managed_file_stream` (mkdir + unlink временного), `fs_git_root` (stat, обход дерева), `get_media` (is_file+stat) |
| `ops.py` | `run_backup` (mkdir), `download_dashboard_backup` (resolve+is_file), `run_import_upload` (isfile/mkdir/is_zipfile/unlink) |
| `local_models.py` | `local_models_delete` (glob+exists+unlink), `local_models_sideload` (is_file/exists/mkdir/link/symlink/copy) |
| `profiles.py` | `open_profile_terminal_endpoint` (stat+`which`+Popen×3), `import_profile_endpoint` (is_file) |
| `status.py` | `get_logs` (exists) + новый `_handler_offload_view()` в ответе `/api/status` |
| `dashboard_ui.py` | `serve_plugin_asset` (is_file) |
| `chat_ws.py` | `_pty_channel_marker_state` (unlink) |

### Осознанно НЕ переведено (решение по каждой)

- `oauth.py:514/916` — `sleep` в ожидающих петлях, не блокирующая работа.
- `audio.py:593` — `await asyncio.sleep(0)`, это yield, а не блокировка.
- `webapp_relay_proxy.py` / `display.py:125` — `wait` на сокетах / WS.
- `webapp_access.py:156` — `await asyncio.sleep()` в опросе блокировки.
- `ops.py:207` — **ложное срабатывание** AST-аудита: `str.replace` над именем вебхука.
- `messaging.py:start_whatsapp_onboarding` — **откатано осознанно**: перевод ломал контракт
  собственных тестов (парный поток создаётся в контексте запроса, 21 тест
  `test_whatsapp_dashboard_repair.py` краснел). Путь человеко-разовый (QR-сопряжение), на горячем
  пути его нет. Причина записана комментарием в файле.

Итог аудита `durable/t_a61becbc/audit_blocking.py`: было `total=37 files=38`, стало
`total=6 files=39` — и все 6 перечислены выше как осознанные.

## Тесты

Раннер: `HERMES_PYTHON=/home/hermes/.hermes/hermes-agent/.venv/bin/python HOME=/tmp/t6c
scripts/run_tests.sh …` (иначе `run-in-hermes-env` ищет bootstrap-Python и падает).

- `tests/hermes_cli/test_handler_offload.py` — **новый, 11 тестов, зелёные.** Пинует: работа уходит
  с потока цикла; args/kwargs; выключатель env → inline; выключатель конфига → inline;
  contextvars доходят и НЕ утекают обратно; HTTPException едет через границу с тем же статусом;
  переполнение пула ставит в очередь, а не роняет; `/api/status` отдаёт режим и число воркеров;
  shutdown идемпотентен; в inline-режиме пул не нужен.
- 100 тестов зелёные: `test_web_profiles_off_loop`, `test_web_messaging_platform_profile_scope`,
  `test_web_server_messaging_posture`, `test_local_models_routes`, `test_web_routers_status_health`,
  `test_backup_path_errors`, `test_dashboard_admin_endpoints`.
- `test_whatsapp_dashboard_repair` + `test_whatsapp_onboarding` — 27 зелёных после отката.
- **Пред-существующие провалы базы (проверено на распакованном `4489b127681` в
  `…/scratch/bv2`):** 2 теста `test_web_server_files.py::test_download_resolves_paths_in_the_originating_profile_session`
  (503 «Session store is not initialized»), 1 провал + 2 ошибки в `test_webapp_uploads`/
  `test_webapp_upload_security`, 1 провал ledger-теста на маршрут
  `POST /api/profiles/{name}/soul/apply-questionnaire` (маршрут есть в базе, строки в
  `serve_handler_ledger.json` нет). Ни один из них моей правкой не вызван.

## Осталось

1. Замер «после»: `bench_curve.py --out curve-after.json`, `bench_loop.py --out loop-after.json`,
   `bench_scenarios.py --label after` в `durable/t_a61becbc/`.
2. `durable/t_a61becbc/REPORT.md`: таблица до/после по трём сценариям, список тестов,
   `git show --stat`, процедура отката.
3. Публикация — гейт оркестратора.

## Процедура аварийного отката (одна команда)

```bash
# в config.yaml профиля: dashboard.handler_offload: false   (или в юните: HERMES_HANDLER_OFFLOAD=0)
hermes serve --restart   # либо systemctl --user restart hermes-gateway
```
Правки файлов не нужны: `off_loop` начинает исполнять вызовы inline на цикле, то есть ровно
прежнее однопроцессное поведение. В журнале при старте будет
`handler offload mode=inline (single-process) source=…`, `/api/status` → `handler_offload.mode = "inline"`.
