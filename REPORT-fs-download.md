# t_84ccbf64 — 404 при скачивании файла из чата (api_server без файловых маршрутов)

## Итог

Причина подтверждена замером и устранена: у платформы **api_server** не было ни одного файлового
маршрута, а десктоп-чат запрашивает у своего же шлюза `GET /api/fs/download` (fallback
`/api/fs/read-data-url`) — маршруты регистрировала только панель. Теперь платформа отдаёт ровно
два read-only маршрута теми же правилами, что панель.

Ветка: `wt/t_84ccbf64`, worktree `/home/hermes/.hermes/hermes-agent/.worktrees/t_84ccbf64`.
База: `bc8ecfaee2` (верхушка `feature/multiuser-access` на момент работы).

## Что изменено

| Файл | Что |
|---|---|
| `gateway/platforms/api_server_fs.py` (новый) | `http_routes(adapter)` — `GET/HEAD /api/fs/download`, `GET /api/fs/read-data-url`; импортирует правила панели, ничего не переписывает |
| `gateway/platforms/api_server.py` | `routes.extend(_api_fs.http_routes(self))` в `_http_route_table()`; SSE-примитивы вынесены в `api_server_sse.py` (фасад был на пределе строк и обязан уменьшаться), с ре-экспортом имён |
| `gateway/platforms/api_server_sse.py` (новый) | `ThreadSafeAsyncQueue`, `_sse_frame` — перенос без изменения поведения |
| `tests/gateway/test_api_server_fs_download.py` (новый) | 19 тестов на реальном aiohttp-сокете: байты, размер, sha256, `Content-Disposition`, HEAD, data URL, 401/404/403/413, `session_id`, `profile`, отсутствие write-маршрутов |

Переиспользованы (импортом из `hermes_cli.web_routers.files` / `web_server`):

`_fs_download_path`, `_fs_owned_session`, `_fs_regular_file`, `_hosted_fs_read_guard`,
`_refuse_live_database`, `_is_host_file`, `_remote_fs_backend`, `_fs_mime_type`, `_fs_etag`,
`_fs_read_bytes`, `_fs_profile_home`, `_remote_download_target`; пределы
`_MANAGED_FILE_MAX_BYTES` (100 МиБ) и `_FS_DATA_URL_MAX_BYTES`.

Одно осознанное отличие от панели: к списку панели добавлен вызываемый гейт
`gateway.platforms.base._path_under_denied_prefix` (тот же denylist, через который проходит любая
исходящая доставка медиа). Панельный `_is_sensitive_path` — denylist по ИМЕНИ файла, и
`~/.ssh/id_ed25519`, `~/.ssh/config`, `~/.aws/*` через него проходят. Для сетевого слушателя,
на котором идут ходы чата, это неприемлемо; замерено на живом адаптере: до правки 200, после — 403.

Аутентификация — собственная `API_SERVER_KEY` платформы (`self._check_auth`), включая зеркало
`/p/<profile>/api/fs/...`. Write-маршрутов (`upload`, `mkdir`, `delete`) не добавлено —
проверяется списком маршрутов в тесте и в живой пробе.

## Живая проба

`verify-fs-download-live.py` поднимает РЕАЛЬНЫЙ `APIServerAdapter` из этой ветки на свободном
порту против РЕАЛЬНОГО `HERMES_HOME` (`/home/hermes/.hermes`, живой `state.db`, отчёт владельца) и
ходит к нему ровно так, как это делает клиент
(`gateway-file-download.ts::gatewayFileRequestPaths` — `?path=…&session_id=…`, Bearer-токен).

```
$ python verify-fs-download-live.py 8649      # в этой ветке
24/24 checks passed

$ python verify-fs-download-live.py 8648      # та же проба на базе bc8ecfaee2
4/24 checks passed
```

Успешное (ветка): 200, 2 988 017 б, sha256 `5e754cc223dc9c4d…`, `attachment; filename*=UTF-8''…`;
`/api/fs/read-data-url` — тот же sha256 после base64; HEAD с `Content-Length`;
401 без токена; 404 с `detail` на несуществующем пути и неизвестном `session_id`; 403 на `.env`,
`~/.ssh/id_ed25519`, `~/.ssh/config`, на путь вне закреплённого корня; 413 на файл больше 100 МиБ;
свой `session_id` (взят из живого `state.db`) → 200 те же байты; неизвестный `profile` → 404;
под `/api/fs` ровно четыре маршрута, все GET/HEAD.

Полные выводы: `PROBE-fs-download-worktree.txt`, база —
`PROBE-fs-download-baseline.txt` в `02-hermes-platform/cache/scratch/base-t_84cc/`.

## Тесты и линт

* `scripts/run_tests.sh tests/gateway/test_api_server_fs_download.py` — 19 passed.
* Затронутые соседи: `test_api_server_profile_prefix_misdelivery`, `test_api_server_sse_keepalive`,
  `test_api_server_media_data_urls`, `test_api_server_multimodal`, `test_api_server_toolset`,
  `tests/hermes_cli/web_routers` — 48 passed, 0 failed.
* `python -m scripts.code_health --base bc8ecfaee2` — 0 blocking, 0 advisory.
* `ruff` (pre-commit конфиг) — чисто.

Падения, которые есть и на базе (не мои): `tests/hermes_cli/test_web_server_files.py` — 2 теста
«download resolves paths in the originating profile session» падают одинаково на `bc8ecfaee2`
(`503 Session store is not initialized`); `tests/gateway/test_api_server.py` — 16 падений
(`TEST BUG: file I/O against the REAL hermes home: …/hermes-update-pull`);
`tests/gateway/test_platform_base.py::TestGetHumanDelay` — 6 падений. Все три набора сверены на
чистой копии базы в `cache/scratch/base-t_84cc` — те же цифры.

## Что осталось (не мой участок)

1. **Слияние** `wt/t_84ccbf64` → `feature/multiuser-access` — делает оркестратор (страж деревьев
   не пускает карточку в общее дерево `~/.hermes/hermes-agent`).
2. **Критерий 6 (живой 8642)** — после слияния и тихого рестарта шлюза повторить
   `verify-fs-download-live.py` **без указания порта**, то есть против боевого слушателя:
   проба принимает порт аргументом, но проверяет тот же адаптер и тот же `HERMES_HOME`.
   До слияния на `127.0.0.1:8642` по-прежнему 404 (`GET /api/fs/download`), что и есть
   исходный дефект: слушатель работает из общего дерева.
3. **`manual_review`** — владелец нажимает «Скачать» в чате и получает файл (после показа, не
   блокер разработки).

## Rollback

`git revert` коммита ветки; маршруты существуют только в `api_server_fs.http_routes()`, их
удаление из `_http_route_table()` возвращает прежнее поведение. Данные не затрагиваются —
маршруты read-only и ничего не пишут.
