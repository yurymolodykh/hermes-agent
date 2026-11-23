### 1.3 Точный список блокирующих обработчиков (воспроизводимый AST-аудит)

Все числа этого раздела печатает скрипт, ничего не считается руками:

```bash
python3 durable/t_eca8cb15/probes/route_audit.py \
    --json durable/t_eca8cb15/probes/route_audit.json \
    --md   durable/t_eca8cb15/probes/route_audit.md
python3 durable/t_eca8cb15/probes/route_audit.py --live   # сверка с FastAPI-объектом
python3 durable/t_eca8cb15/probes/probe_fs_cost.py --json durable/t_eca8cb15/probes/fs_cost.json
```

Ревизия снятия: `762abc960b3680fb8c6b855060dddcd9a7d4cbe9` (ветка `wt/t_eca8cb15`, база
`feature/multiuser-access @ 1456950ab7d`; `hermes_cli/` до базы не изменён). Полные таблицы —
`probes/route_audit.md`, сырые данные — `probes/route_audit.json` и `probes/fs_cost.json`.

#### Перепись (команда и вывод)

```
$ python3 durable/t_eca8cb15/probes/route_audit.py
Регистраций в hermes_cli/web_routers/*.py (38 модулей): 336 = HTTP 325 + api_route 1 + WS 10;
async 301 (уникальных 299), def 35. async-обработчиков с оффлоадом: 173.

$ python3 durable/t_eca8cb15/probes/route_audit.py --live
{"all_http": [402, [323, 79]], "all_ws": 11,
 "web_routers_http": [326, [291, 35]], "web_routers_ws": 10}
```

Обе переписи сходятся по модулям: сумма AST-регистраций = 336, сумма эндпоинтов живого
приложения из `hermes_cli.web_routers` = 336; разница `async` берётся из декораторов плагинов
(`hermes_dashboard_plugin_kanban`, 47 эндпоинтов) и из того, что `--live` считает уникальные
эндпоинты, а AST — регистрации. Почему прежние «316 / 285 / 31 / 37» были неверны: скрипт тогда
фильтровал декораторы по имени объекта `router` (есть ещё `hub_router`, `manage_router`,
`sessions_router`, `status_router`, `list_router`, `search_router`, `logs_router`,
`config_router`), не знал проектных обёрток оффлоада и не отличал метаданные от ввода-вывода.

`def`-обработчики (`35`) Starlette исполняет через `run_in_threadpool` — они вне списка.

#### Порог отбора: что считается достаточно дорогим

Порог явный и измеренный (`probes/probe_fs_cost.py`, медиана):

| Операция | p50 | Вывод |
|---|---|---|
| `stat` / `exists` / `is_file` (4 КиБ) | 0,0032 мс | **метаданные — не оффлоадим** |
| `read_text` 4 КиБ | 0,0165 мс | не оффлоадим |
| `read_text` 1 МиБ | 7,1 мс | оффлоадим, если размер не ограничен жёстко |
| `write_text` 8 МиБ + `os.replace` | 84,9 мс (max 299,5) | **оффлоадим обязательно** |
| `write_bytes` 25 МиБ | 292,8 мс (max 492,3) | оффлоадим обязательно |
| `rglob` по 220 записям | 0,78 мс (max 1,42) | на границе — решает не цена, а размер дерева |
| `unlink` | 0,22 мс (max 74,7 — хвост ФС) | оффлоадим, если вызывается в пачке |

Правило: **дороже 1 мс — блокирующее; метаданные (`exists`/`is_file`/`is_dir`/`stat`/`resolve`)
при цене 0,003 мс — нет.** Восемь обработчиков, у которых в теле только метаданные, вынесены в
отдельную таблицу ниже с решением по каждому: их не оффлоадим, но и не прячем.

#### Блокирующие обработчики (9)

| Файл:строка | Обработчик | Блокирующие вызовы (строка:вызов) | Что именно | Цена по замеру | Правка |
|---|---|---|---|---|---|
| `files.py:1083` | `fs_write_text` | 1139:`tmp.write_text`, 1140:`os.replace`, 1142:`tmp.unlink`, 1145:`tmp.unlink` | **самый крупный блокировщик цикла**: запись 8 МиБ (`_FS_TEXT_WRITE_MAX_BYTES`, `files.py:129`) на петле событий | 85 мс p50, до 300 мс | `await asyncio.to_thread` на весь блок «stat → проверки → write → replace → unlink», одной функцией, чтобы исключения `PermissionError`/`OSError` остались на месте |
| `actions.py:386` | `get_action_status` | 436:`proc.wait` | ждёт до 1 с (`timeout=1`) завершения процесса действия, если он только что вышел | до 1000 мс | `await asyncio.to_thread(proc.wait, 1)` (таймаут обязателен) |
| `audio.py:90` | `transcribe_audio_upload` | 123:`tmp.write` | запись входящего аудио до 25 МиБ (`audio.py:52`) во временный файл | ~293 мс p50, до 492 мс | писать во временный файл в `to_thread`; сам `transcribe_recording` уже уходит в пул (строка 132) |
| `local_models.py:1041` | `local_models_sideload` | 1050:`dest.parent.mkdir`, 1052:`os.link`, 1055:`os.symlink` | создание каталога и жёсткая/символьная ссылка на GGUF (гигабайты) | ссылка — операция ФС, не чтение данных; `mkdir` — метаданные плюс помещение узла | целиком в `to_thread` (копия уже в пуле, 1057) |
| `local_models.py:796` | `local_models_delete` | 803:`path.unlink` | удаление файла модели (гигабайты) | хвост `unlink` до 75 мс | `await asyncio.to_thread(path.unlink, missing_ok=True)` |
| `files.py:936` | `upload_managed_file_stream` | 946:`target.parent.mkdir` | создание каталога перед потоковой загрузкой | метаданные плюс помещение узла | `to_thread` (сам поток загрузки уже оффлоадит запись, 920–921) |
| `ops.py:601` | `run_backup` | 608:`archive.parent.mkdir` | создание каталога бэкапов | метаданные плюс помещение узла | `to_thread` |
| `ops.py:666` | `run_import_upload` | 674:`staging_dir.mkdir`, 685:`target.unlink` | подготовка каталога и снос невалидного архива | то же + `unlink` | оба в `to_thread` |
| `profiles.py:1124` | `open_profile_terminal_endpoint` | 1130/1133/1139:`subprocess.Popen` | запуск терминала (Windows `cmd /c start`, macOS, Linux) | спавн процесса — реальная блокировка | `to_thread`; в Linux-ветке уже есть `subprocess.Popen(..., start_new_session=True)` |

**Порядок правок сдвинулся: `fs_write_text` первым.** Раньше он вообще отсутствовал в списке;
теперь это первая строка §5.1, потому что 8 МиБ на петле — единственная точка, где цикл стоит
сотни миллисекунд на один запрос сохранения файла из редактора, а не микросекунды.

#### Только метаданные: остаются в цикле, но названы (8)

Проверяющий справедливо потребовал не терять дешёвые точки. По каждой решение:

| Файл:строка | Обработчик | Вызовы | Решение |
|---|---|---|---|
| `status.py:998` | `get_logs` | 1008:`log_path.exists` | **остаётся**: 0,003 мс; сама читка лога уже в пуле (`_load_logs`, 1037) |
| `files.py:1389` | `fs_git_root` | 1401:`target.stat` | **остаётся**: `stat` — 0,003 мс, а `_fs_find_git_root` (243) поднимается до 50 каталогов и уже после этой строки — его следует оффлоадить вместе; в §5.1 он записан отдельной строкой |
| `dashboard_ui.py:409` | `serve_plugin_asset` | 437:`base.resolve`, 439:`target.exists`, 439:`target.is_file` | **остаётся**: 3 `stat(2)` подряд, ~0,01 мс; тело отдаёт `FileResponse`, потоковое чтение делает Starlette |
| `files.py:428` | `get_media` | 442:`target.is_file`, 444:`target.stat` | **остаётся**: метаданные; сама читка под `to_thread` (450) |
| `ops.py:620` | `download_dashboard_backup` | 631:`target.is_file` | **остаётся**: метаданные перед `FileResponse` |
| `ops.py:647` | `run_import` | 652:`os.path.isfile` | **остаётся**: метаданные перед спавном действия |
| `profiles.py:1395` | `import_profile_endpoint` | 1419:`overlay_path.is_file` | **остаётся**: метаданные; сам импорт уже в `run_in_threadpool` (1408) |
| `audio.py:299` | `speak_text` | 328:`os.path.isfile` | **остаётся**: метаданные |

#### Уже оффлоадят (образец правки, 173 обработчика)

Скрипт считает обработчик оффлоадящим, если в нём есть `asyncio.to_thread`,
`anyio.to_thread.run_sync`, `loop.run_in_executor`, `starlette.concurrency.run_in_threadpool`,
а также проектные обёртки `scoped_to_thread` / `config_scoped_to_thread` (`_common.py`),
`_read_off_loop` (`profiles.py`), `_hub_lookup` (`skills.py`), `_run_config_scoped` (`audio.py`).
Именно по этой причине прежняя цифра «37» была занижена: она видела только `to_thread`, а,
например, `get_profile_soul` уходит в пул через `_read_off_loop(…)` и в список «блокирующих»
попадать не должен — и не попадает.

Отдельно учтены **вложенные помощники** (10 штук): `def _run(): …` исполняется не в момент
объявления. Все десять уже передаются в обёртку оффлоада по имени — например
`skills.py:288 scan_skill_hub::_run` (там `_shutil.rmtree`, `skills.py:328`) уходит в пул через
`_hub_lookup(_run, …)` (`skills.py:356`). Обратная сторона названа честно: блокирующий вызов во
вложенной функции, которую никто не оффлоадит, скрипт помечает в `nested_left` (сейчас пусто),
а не в основном списке.

#### Что остаётся в цикле событий намеренно

- маршруты, чья работа — `await` на уже-асинхронных примитивах (кэш статуса t_f4d564cb, пул
  чтения t_f8450188, коалесинг);
- WS-петли `audio.py:522 transcribe_stream_ws` (`asyncio.sleep`, не блокирует) и
  `webapp_relay_proxy.py:294 relay_ws` (`websockets.connect`, уже асинхронный) — WS обязан жить
  в цикле;
- 35 `def`-обработчиков — их в пул уводит сам Starlette;
- единственный писатель состояния — процесс, владеющий арендой профиля (§2).
