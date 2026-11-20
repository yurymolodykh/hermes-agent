# МПД шаг 5 — профили: свои и общие (свой оркестратор)

Карточка `t_a08e47a4`, ветка `wt/t_a08e47a4`, worktree
`/home/hermes/.hermes/hermes-agent/.worktrees/t_a08e47a4`.
База: `8db3cd6c1c` (верхушка шага 4).

## Что было не так

Дашборд авторизовывал **профиль**, а не человека. Поэтому в режиме `acl.multi_user=true`
второй человек на хосте:

* получал в `GET /api/profiles` весь roster — все оркестраторы машины;
* читал и **перезаписывал** `SOUL.md` любого профиля (`GET/PUT /api/profiles/{name}/soul`);
* заводил профиль с любым именем (`POST /api/profiles`), в том числе в чужом namespace;
* через `?profile=<имя>` в веере `/api/profiles/sessions`, `/sidebar`, `/projects/tree`,
  `/sessions/pull-requests` открывал **чужой `state.db`**;
* читал чужую персональную настройку `active` (sticky-профиль хоста);
* мог удалить, экспортировать (вместе с `.env` — токенами ботов и ключами) и импортировать
  любой профиль.

Проверено на базе: `verify-step5-baseline-red.py` → 4 утечки (`LEAK 1..4` = True):
`GET /api/profiles` как alice содержит `01-bob`; `GET /api/profiles/01-bob/soul` → 200;
`PUT` того же → 200 и файл перезаписан; `POST /api/profiles {"name":"07-in-bobs-namespace"}`
→ 200.

## Что сделано

### 1. `hermes_acl_profiles.py` (новый, 389 стр.) — одна точка принятия решений

По образцу `hermes_acl_scope` (скоуп строк сессий) и `hermes_acl_bindings` (реестр шага 2).
Никакой параллельной логики определения владельца: `is_owner` → `gateway.owner_identity.is_host_owner`,
`multi_user_enabled` → `hermes_acl_identity.multi_user_enabled`.

| Функция | Смысл |
|---|---|
| `visible_profiles(subject, names)` | `None` — скоупинг выключен; иначе кортеж видимых имён |
| `allows(subject, profile)` | можно ли **использовать** профиль |
| `writable_profiles(subject)` | `None` — можно всё; иначе только личные профили субъекта |
| `shared_candidates(names)` | удобство для оператора: имена диапазона 02…15 |
| `request_*` | те же ответы для HTTP-запроса (личность из проверенного гранта) |
| `require_visible/require_writable/require_owner/require_create` | отказы с нужным кодом |

Правила:

* **owner** (host owner) — весь roster: видит, использует, пишет, создаёт, удаляет;
* **обычный человек** — видит свои профили из своей записи реестра + **shared**-профили
  (записи `shared/…`); использует ровно то, что видит; **пишет** только свои личные;
* **shared-профиль не writable для не-owner** — это общий runtime, которым пользуются
  несколько людей, а не объект перенастройки одним из них;
* `acl.multi_user=false` (поставка по умолчанию) — политика **unscoped**, прежнее поведение
  целиком;
* реестр нечитаем/битый → видимых профилей нет (fail closed), владелец свои права сохраняет.

### 2. Роутер `hermes_cli/web_routers/profiles.py`

| Маршрут | Правило |
|---|---|
| `GET /api/profiles` | payload фильтруется по видимым; при выключенном флаге/owner — тот же кэшированный ответ, что раньше |
| `POST /api/profiles` | `profile_not_allowed` (403) вне набора; по умолчанию только админ |
| `GET /api/profiles/active` | чужой sticky-профиль не называется — подменяется своим |
| `POST /api/profiles/active` | owner-only (403): значение **host-wide**, следующий CLI владельца пошёл бы в чужой профиль |
| `GET /api/profiles/{n}/soul`, `/desktop-overlay`, `/setup-command` | `require_visible` → чужое = 404 |
| `PUT /api/profiles/{n}/soul`, `/description`, `/model`, `POST /describe-auto` | `require_writable`: чужое = 404, shared = 403 `profile_read_only` |
| `PATCH /api/profiles/{n}` (rename) | writable для цели + `require_create` для **нового** имени |
| `DELETE`, `POST /export`, `POST /import` | owner-only (403) |
| `/api/profiles/sessions`, `/sidebar`, `/projects/tree`, `/sessions/pull-requests` | веер идёт по видимым профилям; явный чужой `?profile=` → 404 |

Кэши `_sidebar_payload` / `_projects_tree_payload` получили видимый набор и owner-скоуп
**в ключ**: иначе второй человек в том же процессе получил бы чужой закэшированный payload.

### 3. Строки внутри видимого профиля — свой скоуп

«Можно пользоваться профилем 03» ≠ «можно читать чужую переписку в нём». Веер и дерево
проектов получили `user_id=owner_scope`:

* `hermes_cli/web_routers/profiles.py` — `list_sessions_rich(..., user_id=owner_scope)`,
  `session_count`, `db.usage_totals(user_id=...)`;
* `hermes_state_usage.py` — `usage_totals` принимает `user_id` и строит **тот же** WHERE-арм,
  что листинг (`_usage_owner_clause` → `hermes_state_sessions._owner_scope_clause`), чтобы
  сумма не расходилась со строками, которые она суммирует;
* `tui_gateway/methods_projects.py` — `_project_tree_inputs` / `_build_project_tree` принимают
  `user_id`; `None` (все прежние вызывающие) сохраняет прежний SQL.

### 4. Тесты — `tests/hermes_cli/test_profile_route_owner_scope.py` (новый, 563 стр., **73 теста**)

* юнит-слой политики: видимость, права записи, отсутствующий/битый реестр, владелец без
  записи в реестре, имена (`login_of`, `personal_root`, `root_login`, `shared_candidates`);
* HTTP-слой: листинг (свои+общие у A и B, полный у владельца), чтение/запись SOUL,
  создание, `active`, owner-only операции, веер `?profile=`, режим с выключенным флагом.

Форма отказа зафиксирована как контракт: **чужой профиль = 404**, байт-в-байт по форме
совпадающий с несуществующим именем (403 был бы оракулом существования); **разрушающая
операция хоста = 403**.

## Проверено инструментом

### Тесты

```
tests/hermes_cli/test_profile_route_owner_scope.py            → 73 passed  (9.8 s)
```

Смежные наборы, дерево копируется вне `~/.hermes` (`/tmp/mpd5/branch`) — worktree лежит
внутри реального дома, и `tests/home_io_guard.py` рубит инициализацию БД:

```
test_profiles_sidebar_scope, test_profiles, test_web_profiles_model_scope,
test_web_profile_create_serves_now, test_web_profile_soul_writes,
test_web_profile_delete_settlement, test_profile_delete_service_name,
test_profile_listing_cache, test_profiles_session_archived_pages,
test_native_profile_route_names, test_dashboard_param_clamps, test_web_read_coalescing
  ветка:  8 failed / 153 passed / 3 skipped
  база (8db3cd6c1c, тот же список): 8 failed / 153 passed / 3 skipped
  → набор падений совпадает: те же 8 в test_dashboard_param_clamps (предсуществующие)

test_session_route_owner_scope + test_profile_route_owner_scope +
test_web_server_profile_unification + gateway/test_native_profile_create +
gateway/test_native_profile_registry_grant                        → 136 passed (48 s)
```

### Живая проба `verify-step5-live.py` → `PROBE-step5-output.txt`

Изолированный `HERMES_HOME` в `/tmp` с `acl.enabled: true`, `acl.multi_user: true` и настоящим
реестром привязок; прогон через реальные роутеры дашборда. **ИТОГ: OK — 24/24**, в том числе
все критерии приёмки:

| Критерий | Вывод пробы |
|---|---|
| 2. A не видит `01-B` | `GET /api/profiles` как alice → `['01-alice', '02-hermes-platform', '03-software-engineer']` |
| 3. чужое имя → отказ | `POST {"name":"01-bob"}` → **403** `{"detail":"profile_not_allowed"}` |
| 4. общие 02–15 видны обоим | bob → `['01-bob', '02-hermes-platform', '03-software-engineer']` |
| 5. владелец видит всё | owner → `['default','01-alice','01-bob','02-hermes-platform','03-software-engineer']` |

Дополнительно (швы этого шага):

```
GET  /api/profiles/01-bob/soul      as alice → 404 {"detail":"Profile '01-bob' does not exist."}
GET  /api/profiles/99-nobody/soul   as alice → 404 (та же форма)
PUT  /api/profiles/01-bob/soul      as alice → 404, файл не изменён
PUT  /api/profiles/02-hermes-platform/soul as alice → 403 {"detail":"profile_read_only"}
PUT  /api/profiles/01-alice/soul    as alice → 200
GET  /api/profiles/sessions?profile=01-bob        as alice → 404
GET  /api/profiles/sessions?profile=01-alice      as alice → 200
GET  /api/profiles/sessions/sidebar?recents_profile=01-bob as alice → 404
POST /api/profiles/01-alice/export  as alice → 403 {"detail":"owner_only"}
DELETE /api/profiles/01-alice       as alice → 403, каталог на месте
acl off: GET /api/profiles → весь roster; GET /api/profiles/01-bob/soul → 200
```

### База красная

`verify-step5-baseline-red.py` на `8db3cd6c1c`: `LEAK 1..4` все True (листинг, чтение,
запись чужого SOUL, создание профиля). Новый тестовый файл на базе не собирается
(`ModuleNotFoundError: hermes_acl_profiles`) — вся политика отсутствует.

### Code health

```
python -m scripts.code_health --base 8db3cd6c1c
  → 0 blocking, 0 advisory по 5 изменённым файлам
     (одна BLE001 в _own_profile_names разрешена комментарием с причиной)
python scripts/check --only health
  → 70 blocking / 4 advisory — те же, что и по всему репозиторию; ни одной находки
     в hermes_acl_profiles.py, hermes_cli/web_routers/profiles.py,
     tui_gateway/methods_projects.py, hermes_state_usage.py
```

## Принятые допущения (записаны, не решены молча)

1. **Кто заводит профили** (§6.2 анализа, владелец не ответил): по умолчанию **только админ**.
   Флаг `acl.profile_self_service: true` разрешает человеку создать **ровно свой**
   `01-<login>` и ничего больше; имя, уже закреплённое за другим человеком, не отдаётся.
2. **Shared-профиль не writable для не-owner** (видим, используем, не перенастраиваем).
3. **Env-флаг** — существующий `acl.multi_user` (шаг 3), нового ключа не вводилось.

## Ограничения

* `manual_review`: живой браузер второго пользователя против реально запущенного шлюза с
  `acl.multi_user: true`. Поведение проверено на уровне роутеров, политики и живых HTTP-кодов
  (настоящие `TestClient`-запросы через настоящие хендлеры), но не по TCP из двух браузеров.
* Включение `acl.multi_user: true` — решение владельца; эта поставка ничего не включает.
* Перенос существующих профилей между пользователями не делался (границы шага 5).
* Шаг 6 (файлы и вложения) не трогался.
* `pre-commit` в worktree недоступен (`scripts/hermes-python`: no bootstrap Python);
  `ruff` по изменённым файлам прогнан напрямую — чисто.
