# Отчёт — шаг 4 МПД (Т5): права на сессии и сокет

**Карточка:** `t_4997de4c` — «[МПД] Шаг 4: сессии — закрыть ~10 REST-путей и сокет дашборда»
**Ветка:** `feature/mpd-step4-sessions`, worktree `.worktrees/t_4997de4c`
**База:** шаг 3 — `787f022c9b` (ветка `feature/mpd-step3-identity`)
**Первоисточник:** `/home/hermes/workspaces/01-orchestrator/multiuser/MISSION-GAP-ANALYSIS.md`, §2.2 П6

## Что было не так

Дашборд аутентифицирует **профиль**, а не человека. Cookie-сессия дашборда выдавала
`session:operator` каждому вошедшему браузеру, и три проверки читали этот грант как «можно
действовать от имени любого сохранённого владельца»:

* `gateway/session_listing.py` — обход проверки владельца при листинге;
* `gateway/session_history_mount.py` — монтирование истории;
* `gateway/session_authority.py` — авторизация локальной сессии.

Плюс `hermes_cli/web_routers/sessions.py` (18 путей) не проверял владельца вообще: любой
ключ профиля/логин листал, открывал, экспортировал и удалял все диалоги профиля, а сокет
`/api/ws` с `operator=True` отдавал их события.

## Что сделано

### 1. `session:operator` — грант владельца хоста, а не универсальный ключ

`gateway/owner_identity.py`:

* `is_host_owner(subject)`, `identity_is_host_owner(identity)` — одна формулировка «это владелец
  этого хоста» вместо сравнения строк в каждом месте;
* `is_host_operator(actor)` — новая точка правды: при `acl.multi_user: true` грант
  `session:operator` даёт обход владельца только тому, чей субъект и есть владелец хоста;
  при выключенном флаге поведение прежнее (один человек на хосте).

Три места обхода переведены на него: `session_listing.py`, `session_history_mount.py`,
`session_authority.py`. Дополнительно `session_prune.py` (общехранилищное удаление) и
`session_group_controls.py` (ростер).

### 2. Один шов для 18 REST-путей сессий — `hermes_cli/web_routers/_session_acl.py`

* `session_scope(request)` — стемпы владельца из **проверенной** личности запроса
  (`hermes_acl_scope.request_owner_scope`): `None` при выключенном `acl.enabled` (поведение не
  меняется ни на строку), `[]` — fail-closed «личность не установлена»;
* `require_row(row, scope)` — чужая строка отвечает **404**, тем же телом, что и несуществующий
  id (403 превратил бы поверхность в оракул существования id);
* `visible_row`, `row_outside`, `scoped_ids` — фильтр для массовых выборок;
* `refuse_writes(request)` — общехранилищные операции (prune, owner-backfill) только владельцу
  (`403 owner_only`): их выборка — предикат по строкам всех владельцев.

`hermes_acl_scope.request_owner_scope` переработан: личность берётся из проверенного гранта
(`request.state.native_http_principal`) или проверенного логина дашборда
(`request.state.session`), заголовок — только при `identity_trust=trusted-proxy`. Два
несогласованных свидетельства → отказ (fail closed). Добавлены `request_identity_subject` и
`request_is_host_owner`.

### 3. Сокет дашборда — грант по личности, а не по факту входа

`hermes_cli/web_routers/chat_ws.py`: `socket_operator(identity)` — при МПД оператором сокета
является только владелец хоста; второй вошедший получает `profile_scope='profile'` (не может
выбрать чужой профиль по имени) и обычные права своего субъекта. Сокет не отключается.

### 4. SQL-слой: счётчики тоже в области владельца

* `hermes_state_owner_scope.py` (новый) — общие правила scope-SQL: `None` = без ограничения,
  пустой scope = `1 = 0` (не «всё»);
* `SessionDB.message_count(..., user_id=)`, `session_count_by_source(..., user_id=)`;
* `SessionEmptyHygieneMixin` (`hermes_state_empty_hygiene.py`, новый) — вынесен из
  `hermes_state_sessions.py` (файл упирался в лимит 2000 строк): `count_empty_sessions` /
  `delete_empty_sessions` с `user_id=`.

## Таблица путей: путь → тест → закрыт

Перепись декораторов (`grep -nE "^@(list|search|manage)_router\." hermes_cli/web_routers/sessions.py`)
даёт **18** путей. §2.2 П6 называет 11 — они закрыты в первую очередь, остальные 7 добавочно.

| № | Путь | П6 | Закрытие | Тест |
|---|---|---|---|---|
| 1 | `GET /api/sessions` | да | `user_id=scope` в листинге и `session_count` | `test_listing_shows_only_the_callers_sessions`, `test_listing_for_the_owner_is_their_own_rows`, `test_listing_without_a_verified_identity_lists_nothing`, `test_acl_off_is_unchanged` |
| 2 | `GET /api/sessions/search` | да | scope на всех трёх полосах (id, FTS, титул) + оба конца lineage | `test_search_does_not_return_a_foreign_session_by_content`, `..._by_id`, `test_search_for_the_owner_finds_both` |
| 3 | `POST /api/sessions/bulk-delete` | да | `scoped_ids` — чужие id отбрасываются молча | `test_bulk_delete_ignores_foreign_ids` |
| 4 | `POST /api/sessions/import` | — | чужой id → `400 import_conflict`, ничего не пишется | `test_import_onto_a_foreign_id_is_refused` |
| 5 | `GET /api/sessions/empty/count` | да | `count_empty_sessions(user_id=)` | `test_empty_count_is_scoped` |
| 6 | `DELETE /api/sessions/empty` | — | `delete_empty_sessions(user_id=)` — свои пустые | `test_empty_sweep_is_scoped_to_the_callers_own_rows` |
| 7 | `GET /api/sessions/stats` | да | все счётчики, включая `messages` и `by_source` | `test_statistics_count_only_the_callers_rows` |
| 8 | `GET /api/sessions/{id}` | да | `require_row` → 404 | `test_every_read_route_refuses_a_foreign_id_as_a_miss` |
| 9 | `GET /api/sessions/{id}/mutation-snapshot` | да | `require_row` (счётчики не раскрываются даже нулями) | там же |
| 10 | `GET /api/sessions/{id}/latest-descendant` | да | scope на старте и на каждом шаге обхода | там же |
| 11 | `GET /api/sessions/{id}/messages` | да | `require_row` до загрузки сообщений | там же |
| 12 | `GET /api/sessions/{id}/timeline` | да | `_timeline_session_id(..., scope)` — id и tip | там же |
| 13 | `GET /api/sessions/{id}/messages/around` | да | там же | там же + `test_messages_around_reads_the_owners_own_row` |
| 14 | `DELETE /api/sessions/{id}` | — | 404 до мутации; отсутствующий id сохраняет идемпотентность | `test_delete_of_a_foreign_session_is_404_and_deletes_nothing` |
| 15 | `POST /api/sessions/owner-backfill` | — | `refuse_writes` → `403 owner_only` | `test_store_wide_writes_refuse_a_non_owner` |
| 16 | `PATCH /api/sessions/{id}` | — | 404 на существующую чужую строку | `test_rename_of_a_foreign_session_is_404` |
| 17 | `GET /api/sessions/{id}/export` | да | scope до старта стрима — ни байта чужого транскрипта | `test_every_read_route_refuses_a_foreign_id_as_a_miss` |
| 18 | `POST /api/sessions/prune` | — | `refuse_writes` → `403 owner_only` | `test_store_wide_writes_refuse_a_non_owner`, `..._still_work_for_the_owner` |

Сокет и грант:

| Что | Тест |
|---|---|
| `socket_operator`: только владелец хоста | `test_socket_operator_is_only_the_host_owner`, `test_socket_operator_is_unchanged_with_multi_user_off` |
| `session:operator` = владелец | `test_operator_capability_belongs_to_the_host_owner`, `test_operator_capability_is_unchanged_with_multi_user_off` |
| Резолв личности владельца | `test_host_owner_identity_resolution`, `test_bindings_registry_names_the_owner_as_owner_branch` |
| Профильная привязка сокета | `test_socket_scope_is_profile_bound_for_a_non_owner` |

## Результаты прогонов

### Новые тесты

`tests/hermes_cli/test_session_route_owner_scope.py` — **33 теста**, все зелёные.

```
$ pytest tests/hermes_cli/test_session_route_owner_scope.py -q
33 passed in 16.95s
```

### Затронутые модули

```
$ pytest tests/hermes_cli/test_session_route_owner_scope.py \
         tests/gateway/test_session_owner_scope.py \
         tests/gateway/test_session_operator_scope.py -q
72 passed in 41.47s
```

Ранее, вместе со смежными: `tests/hermes_cli/test_session_timeline.py`,
`test_session_message_page_owner.py`, `test_foreign_sessions.py`,
`test_foreign_sessions_browser.py`, `test_web_multi_profile_scope.py`,
`test_dashboard_auth_ws_auth.py`, `test_web_server_session_owner_forward.py`,
`test_webapp_gateway_ticket.py`, `test_session_mutation_standalone.py`,
`test_empty_session_hygiene.py`, `test_sessions_rest_scaffold_notices.py` —
**120 passed + 18 passed**, без падений.

### Живая проба

`verify-step4-live.py` (изолированный `HERMES_HOME`, реальное FastAPI-приложение,
`acl.multi_user: true`):

```
-- A(alice) → сессия B(bob): чтение --
  A→B  200  leak=no   /api/sessions?limit=50
  A→B  200  leak=no   /api/sessions/search?q=ONLY
  A→B  200  leak=no   /api/sessions/stats
  A→B  404  leak=no   /api/sessions/bob-s1
  A→B  404  leak=no   /api/sessions/bob-s1/mutation-snapshot
  A→B  404  leak=no   /api/sessions/bob-s1/latest-descendant
  A→B  404  leak=no   /api/sessions/bob-s1/messages
  A→B  404  leak=no   /api/sessions/bob-s1/timeline
  A→B  404  leak=no   /api/sessions/bob-s1/messages/around?row_id=3
  A→B  404  leak=no   /api/sessions/bob-s1/export
  A→B  200  leak=no   /api/sessions/empty/count

-- A(alice): свои пути (200 ожидаемы) --
  own_ok=True  200 200 200 200 200

-- A(alice) → мутации чужого id --
  A→B  404  DELETE /api/sessions/bob-s1
  A→B  404  PATCH /api/sessions/bob-s1
  A→B  200  POST /api/sessions/bulk-delete        (чужой id отброшен, deleted=0 для него)

-- A(alice) → общехранилищные операции --
  A    403  POST /api/sessions/prune
  A    403  POST /api/sessions/owner-backfill

-- владелец --
  список владельца: ['owner-s1']
  prune dry-run: 200

ИТОГ: OK — утечек нет, свои пути работают, чужое не тронуто
```

Полный вывод: `PROBE-step4-output.txt`.

## Код-здоровье

`python scripts/check --only health`: **новых находок нет**. Набор «файл+правило» на ветке —
подмножество базы (на одну меньше: вынесенный `hermes_state_empty_hygiene.py` снял
`FILE_LINES` с `hermes_state_sessions.py`). Три оставшихся `BLE001` в `hermes_acl_scope.py`
(`owner_stamps`, `caller_subject`, `row_outside_scope`) — предсуществующие, на базе те же.

`python scripts/check` целиком: FAILED в тех же пяти проверках, что и на базе
(`footguns, no-tmp, install-root, ruff, health`) — это состояние репозитория, не регрессия.

## Ограничения и что осталось

* `manual_review`: подключение живого браузера к сокету второго пользователя против
  запущенного шлюза с `acl.multi_user: true` — проверено на уровне `socket_operator` и
  `session_scope`, но не в браузере.
* Включение `acl.multi_user: true` для всех — по-прежнему решение владельца (шаг 3 оставил
  флаг выключенным); эта поставка ничего не включает сама.
* `session:operator` при выключенном МПД сохраняет прежний смысл намеренно: контракт-тест
  `test_operators_share_local_sessions_without_reassigning_receipts` фиксирует, что в
  однопользовательском режиме нативные поверхности делят локальные сессии.
* Прогон тестов выполнен на копии дерева вне `~/.hermes` (`/tmp/mpd4/repo`): worktree лежит
  внутри реального дома, и `tests/home_io_guard.py` рубит инициализацию БД **на базе тоже**
  (те же 30 ошибок без единой правки). На копии вне дома — 72/72 зелёных.
* Шаги 5 (профили) и 6 (файлы) не трогались, как требует карточка.

## Файлы

Изменены:

* `gateway/owner_identity.py` — `is_host_owner`, `identity_is_host_owner`, `is_host_operator`
* `gateway/session_listing.py`, `gateway/session_history_mount.py`, `gateway/session_authority.py`,
  `gateway/session_prune.py`, `gateway/session_group_controls.py` — обход через `is_host_operator`
* `hermes_acl_scope.py` — личность запроса из проверенных источников
* `hermes_cli/web_routers/sessions.py` — 18 путей
* `hermes_cli/web_routers/chat_ws.py` — `socket_operator`, `profile_scope`
* `hermes_cli/web_server_sessions.py` — комментарий к гранту
* `hermes_state_sessions.py`, `hermes_state_messages.py` — scope-параметры счётчиков
* `hermes_state.py` — миксин в MRO

Новые:

* `hermes_cli/web_routers/_session_acl.py` — шов прав на 18 путей
* `hermes_state_owner_scope.py` — общие scope-SQL правила
* `hermes_state_empty_hygiene.py` — `SessionEmptyHygieneMixin` (вынесен)
* `tests/hermes_cli/test_session_route_owner_scope.py` — 33 теста
* `verify-step4-live.py`, `PROBE-step4-output.txt`, `REPORT-step4.md`

Живая установка `~/.hermes` не тронута: все пробы — на копиях в scratch-каталоге профиля.
