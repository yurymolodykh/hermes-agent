# PROGRESS — t_d220fb25 (остаток выноса блокирующих вызовов, §5.1 №2/№3-хвост/№4/№6-удалённая/№7)

Состояние: **разведка и локаторы сверены, правок кода ещё нет.** Прогон остановлен бюджетом времени.

## Дерево и база

- worktree: `/home/hermes/.hermes/hermes-agent/.worktrees/t_d220fb25`
- ветка: `wt/t_d220fb25`, HEAD `f39aaeffe232` (база `feature/multiuser-access` @ `0f07beb968002daca2d3152db08cb97e3bee7452`)
- рабочее дерево чистое (`git status --short` пусто)

## Проверенное по дереву (факты, не план)

Механизм: `hermes_cli/web_routers/handler_offload.py` — `off_loop(fn, *args, **kwargs)` (237),
`HANDLER_OFFLOAD.run` (184), выключатель `_enabled` (192), `_bind` копирует contextvars (218).
**`off_loop` импортирован только в `files.py`** (`from hermes_cli.web_routers.handler_offload import off_loop`);
в `audio.py` и `actions.py` импорта нет вообще.

Текущее число `off_loop` в трёх файлах: `files.py` — 7, `audio.py` — 0, `actions.py` — 0.

### №2 `audio.py:90 transcribe_audio_upload` — НЕ вынесен
- `base64.b64decode(encoded, validate=True)` — строка **107**, в цикле (`async def`, 90).
- `tmp.write(audio_bytes)` внутри `tempfile.NamedTemporaryFile` — строка **123**, в цикле.
- Уже в потоке: `_run_config_scoped` (81, `asyncio.to_thread`) для транскрипции (132).
- Цена (записка §1.3/§5.1): b64decode 25 МиБ p50 109,8 мс; запись 25 МиБ p50 100,9 / p95 565,7 мс.
- Лимит: `_MAX_TRANSCRIPTION_UPLOAD_BYTES` (проверка на 113) — размер задаёт вызывающий → порог (3) применим.
- Нужно: единый helper вида `_decode_and_stage(encoded, suffix) -> temp_path`, читающий ошибки
  b64 (binascii/ValueError → 400) и пустой/превышающий лимит результат; обернуть в `await off_loop(...)`.
  Внимание: `http_failure` (118) сейчас охватывает участок с записью — при выносе исключение
  HTTPException должно проходить через seam неизменным (в `HandlerOffload.run` так и есть, тест
  `test_http_exception_crosses_the_boundary_intact`).

### №3-хвост `audio.py:299 speak_text` — наполовину
- `_read_and_unlink` (333–341) уже в `asyncio.to_thread` (344) — `open` 338 закрыт.
- `base64.b64encode(audio_bytes).decode("ascii")` — строка **348**, в цикле.
- Цена: 25 МиБ p50 70,9 мс; 1 МиБ p50 2,0 мс.
- Нужно: перенести кодирование в тот же поток, что и чтение/удаление (например, `_read_and_unlink`
  возвращает готовую строку base64, либо второй `off_loop(base64.b64encode, ...)`).
  Предпочтительно — внутрь `_read_and_unlink`, чтобы не появился второй проход по байтам.

### №4 `files.py:483 proxy_remote_media` — НЕ вынесен
- `encoded = base64.b64encode(data).decode("ascii")` — строка **520**, в цикле (конец `async def` 483).
- `data = response.content` — 516; лимит `_MEDIA_MAX_BYTES` проверяется на 517.
- Цена: ≤25 МиБ p50 70,9 мс.
- Нужно: `encoded = await off_loop(lambda d=data: base64.b64encode(d).decode("ascii"))` —
  или лучше добавить модульный helper `_b64_text(data: bytes) -> str` и вызвать `off_loop(_b64_text, data)`
  (лямбду в пул отдавать можно, но именованный helper проще для теста).

### №6-удалённая `files.py:1231 fs_read_data_url` — частично
- Удалённая ветка: `data, _size, target = await asyncio.to_thread(backend.read_bytes, ...)` (1239),
  затем `encoded = base64.b64encode(data).decode("ascii")` — строка **1244**, на петле.
- Локальная ветка закрыта: `_render` (1261–1263, `b64encode(_fs_read_bytes(target))`) в `to_thread` (1266).
- Четыре существующих `to_thread` (1236 `_remote_fs_backend`, 1239 `read_bytes`, 1249 `_is_host_file`,
  1266 `_render`) кодирование 1244 не покрывают.
- Цена: 16 МиБ p50 47,9 мс.
- Требование карточки: перенести кодирование в уже существующий `_render`, чтобы не появился второй
  проход по данным. Здесь есть тонкость: `_render` (1261) читает файл с диска (`_fs_read_bytes(target)`),
  а удалённая ветка файла на хосте НЕ имеет — у неё уже прочитанные `data` и `target` с SSH-машины.
  Поэтому буквально «в тот же `_render`» не получится без параметризации: правильный инкремент —
  вынести общий рендер `_render_data_url(data: bytes, target) -> JSONResponse` и вызывать его
  через `await off_loop(_render_data_url, data, target)` в удалённой ветке, а в локальной —
  `await off_loop(_render_local, target, validators)` (или оставить существующий `to_thread(_render)`).
  Это надо решить при исполнении и обосновать в отчёте: два прохода по 16 МиБ не появляются.

### №7 `actions.py:386 get_action_status` — НЕ вынесен
- `tail = _tail_lines(log_dir / log_file_name, requested_lines)` — строка **394**.
- `_durable_completed_update_action_id(_tail_lines(log_file_path("update"), 2000))` — **403**.
- `_latest_spawned_update_action_id(_tail_lines(log_dir / log_file_name, 2000))` — **404**.
- `_tail_lines` определён на 87, читает `path.open("rb")` на 103 (seek-хвост, `_ACTION_LOG_TAIL_MAX_BYTES`
  = 256 КиБ, начальный чанк 8 КиБ, максимум чанка 64 КиБ).
- Цена: лог 64 МиБ p50 2,2 / p95 3,4 / max 95,8 мс (n=20) → порог p50 ≥ 1 мс.
- `actions.py` — `import asyncio` есть (строка 7), `off_loop` не импортирован.
- Нужно: завернуть три чтения в `await off_loop(_tail_lines, path, n)`. Осторожно со 403/404: они
  вычисляются только при `name == "hermes-update"` (398). Логика сравнения id (406–413) остаётся на петле —
  она дешёвая. `log_file_path` берётся внутри условия — сохранить импорт внутри ветки.

## Что осталось сделать (следующий шаг)

1. Правки пяти точек (№2, №3-хвост, №4, №6-удалённая, №7) через `off_loop`; новые абстракции не вводить.
2. Тесты по образцу уже существующих:
   - `tests/hermes_cli/test_handler_offload.py` — контракт seam (switch on/off, contextvars, HTTPException).
   - `tests/hermes_cli/test_fs_write_text_offload.py` — точечный regression-тест одной точки.
   - `tests/hermes_cli/test_web_profiles_off_loop.py` — **шаблон доказательства конкурентности**
     (`_Blocker` на `threading.Event` + тайминг постороннего запроса через `TestClient` в контексте,
     `BLOCK_SECONDS=5.0`, `CONCURRENT_BUDGET=2.5`). Это правильный образец для теста «поведение при
     включённом выключателе» — не проверка номера строки, а доказательство, что петля свободна.
   - `tests/hermes_cli/test_status_offload_live.py` — счётчики `/api/status`.
   Новый файл: `tests/hermes_cli/test_web_routers_offload_residual.py` (не править существующие наборы).
3. Прогон затронутых наборов: `scripts/run_tests.sh` (не bare pytest):
   `tests/hermes_cli/test_web_server_fs_data_url_revalidation.py`,
   `test_web_server_ssh_media_stream.py`, `test_web_server.py` (proxy + media),
   `test_web_server_update_status.py`, `test_audio_playback_guard.py`,
   `test_web_server_files.py`, `test_web_server_fs.py`, `test_status_offload_live.py`,
   `test_handler_offload.py`.
4. Публикация штатным гейтом `/home/hermes/workspaces/01-orchestrator/ops/bin/ops-card-merge.py`
   (существует, проверено), readback верхушки `feature/multiuser-access`; `--no-verify` и ручной `git push` запрещены.
5. Отчёт по каждой точке: до/после число вызовов `off_loop` (сейчас 0/0/0 → audio.py 2, actions.py 3(или 1 helper),
   files.py 7 → 9) + ссылка на строку + обоснование каждой оставшейся на петле точки по порогу §1.3.

## Замечания по приёмке, которые нельзя забыть

- `git grep -nE "b64encode|_tail_lines|open\(" -- hermes_cli/web_routers/{audio.py,files.py,actions.py}`
  после правки должен показать минимум: `files.py:425` (`_read_base64_file` — кодирование УЖЕ в
  `to_thread` 449, остаётся), `files.py:1262` (внутри `_render`, в потоке), `actions.py:79` (`open` в
  `_spawn_hermes_action`, sync-путь, не в async), `audio.py:258` (`urlopen` в `_fetch`, уже `to_thread` 262),
  `audio.py:447/460` (`_audio_file_to_pcm` — вызывается только из `_sync_sentence_to_pcm`, sync-контекст
  speak-stream; проверить при исполнении, что это не async-петля), `files.py:236/760` (в `to_thread`/opener).
- Выключатель: при `HERMES_HANDLER_OFFLOAD=0` / `dashboard.handler_offload: false` поведение строго
  прежнее (inline) — это обеспечивает сам `HandlerOffload.run` (192), отдельного кода в роутерах не нужно,
  но тест «off» обязателен.
- Единственный писатель `state.db` не затрагивается: новых записей из потоков не появляется
  (`_tail_lines` только читает, b64encode/b64decode — чистые).

## Артефакты-ориентиры

- Записка: `durable/t_eca8cb15/DESIGN-MEMO.md`, §5.1 таблица фактического состояния (строки ~440–470),
  §1.3 порог отбора (строки ~160–185).
- Ревьюер записки (t_eca8cb15, круг 4, approved) подтвердил: «2 из 7 (№1, №5); №3 наполовину; №6 частично;
  №2/№4/№7 остаток» — совпадает с тем, что я сверил в дереве выше.
