# Шаг 3 МПД: идентичность входа и отзыв доступа — отчёт

Карточка: `t_00544442`. Ветка `feature/mpd-step3-identity` (worktree `.worktrees/t_00544442`),
база — `wt/t_00544442` + merge `--no-ff` шага 2 (`feature/mpd-step2-bindings`, `f2596a3524`).

## Что сделано

Одна поставка, четыре части:

1. **`hermes_acl_identity.py` (новый, 412 стр.)** — единственная точка перевода «логин → субъект»
   и «поколение → жива ли сессия»:
   * `resolve_subject` / `resolve_identity_subject`: запись с `role: owner` даёт
     `host_owner_subject()` (владелец сохраняет свои 700+ строк истории), любая другая —
     `person/<login>`; логин, которого нет в файле, оставляет прежний (legacy) ответ.
   * `credential_valid`: отказ при удалённой записи, `disabled`, отсутствующем или несовпадающем
     `gen`, и при нечитаемом файле пользователей (fail-closed).
   * `credential_marker`, `session_generation`, `session_ttl_seconds`/`refresh_ttl_seconds` (7 суток
     в многопользовательском режиме), `revoke_reason` (аудит без материала учётных данных).
   * Флаг `acl.multi_user` (`config.yaml`, по умолчанию **false**): при выключенном флаге каждая
     функция возвращает прежний ответ, ни одна сессия не несёт поколения.
2. **Провайдер пароля** (`plugins/dashboard_auth/basic/__init__.py`): несколько учёток с отзывом и
   отключением; `verify_session` отказывает отозванному; `refresh_session` бросает
   `RefreshExpiredError` (refresh-токен больше не переживает поколение); новое
   `credential_status()` → шлюз отличает «отозвано» от «истекло»; TTL резолвится через
   `hermes_acl_identity` (явная настройка побеждает).
3. **Склейка с субъектом и тикетами**: `gateway/owner_identity.principal_subject` (П4) получил
   узкое исключение для парольного входа; `hermes_cli/dashboard_auth/ws_tickets` кладёт `gen`/`src`
   в тикет и отказывает отозванному при `consume_ticket`; `middleware` отдаёт отдельный 401
   `session_revoked` с причиной; `hermes_acl_scope.owner_stamps` добавляет `person/<login>` и
   прогоняет реестр привязок по каждому производному штампу.
4. **CLI**: `hermes auth users revoke <login>` (поднимает `gen`, пароль не меняется) и
   `hermes auth users remove <login> [--yes]` (удаляет запись — её сессии перестают проверяться);
   `UserStore.remove_record` (остальные строки файла побайтно неизменны).

## Критерии приёмки

### 1. Тесты зелёные

```
$ <test-venv>/bin/python -m pytest tests/hermes_cli/test_acl_identity.py \
    tests/hermes_cli/test_multiuser_identity_wiring.py \
    tests/gateway/test_acl_identity_subject.py tests/plugins/dashboard_auth \
    tests/hermes_cli/test_dashboard_users.py tests/hermes_cli/test_auth_users_cli.py \
    tests/hermes_cli/test_acl_bindings.py tests/gateway/test_acl_bindings_scope.py -q
518 passed, 8 skipped in 144.00s
```

Новых тестов — **139** (`pytest --collect-only`: 139 collected): `test_acl_identity.py` 81,
`test_multiuser_identity_wiring.py` 41, `test_acl_identity_subject.py` 17; остальные 379 в прогоне —
существующие модули (dashboard-auth, users file, CLI, привязки шага 2), они зелёные.

Интерпретатор: `~/.hermes/installs/019d0d114d22b7e6/test-environment/gen-33b3d260ac3140808236b257c2f566fd/venv/bin/python`
(Python 3.14.9, pytest 9.1.1) — в worktree своего `.venv` нет.

### 2. Живая проба: две учётки разными паролями дают разные субъекты

Проба на **копиях** живых данных (`probe_live.py`, файл пользователей скопирован, `acl.multi_user`
форсируется в процессе):

```
=== criterion 2 — two accounts, two passwords, two subjects ===
users file (copy): .../live2/users/users.jsonl  records=25
probe.alice  -> user_id='probe.alice'  subject='person/probe.alice'
probe.bob    -> user_id='probe.bob'  subject='person/probe.bob'
subjects differ: True
neither is the host owner ('uid:1001'): True
alice's account refuses bob's password: True
```

С выключенным (поставочным) флагом те же два входа схлопываются в одного владельца — это и есть
исходный дефект, и он воспроизведён:

```
off: probe.alice -> 'uid:1001'   probe.bob -> 'uid:1001'   collapsed to one: True
```

Пароли в отчёт и в вывод не попадают (в файле только хэши, в печати — логины и субъекты).

### 3. Отзыв: старый тикет/cookie отвергается (до/после)

```
=== criterion 3 — revocation reaches the device ===
before: verify_session -> accepted
before: ws ticket minted, gen=1
revoke: user01 gen 1 -> 2 (password untouched)
after:  verify_session -> REFUSED
after:  gate status='revoked' detail='refused:generation_superseded:1!=2'
after:  ws ticket refused: credential revoked
after:  refresh refused: RefreshExpiredError
after:  same password signs back in at gen=2

=== criterion 3b — deleting the record kills the session ===
bob record removed: verify_session -> REFUSED
```

Шлюз отдаёт именно «отозвано», а не «истекло»: `credential_status` → `('revoked',
'refused:generation_superseded:1!=2')`, а `middleware._revoked_response` превращает это в 401
`{"error": "session_revoked", "reason": "credential_revoked", "revoked": ...}` (и чистит cookie).

### 4. Владелец сохраняет доступ к своим сессиям

```
=== criterion 4 — the owner keeps their own sessions ===
owner record: login='yury' role='owner' gen=1
owner subject: 'uid:1001' == host owner: True
owner scope stamps (1): ['uid:1001']
state.db files scanned: 26
sessions total=735  visible to the owner=453  invisible=282

=== criterion 4b — with the step-2 bindings registry ===
distinct live stamps: ['125805788', '449439313', '543447681', 'api', 'cron-owner', 'uid:1001']
registry subjects for the owner (3): ['uid:1001', 'cron-owner', '125805788']
owner scope stamps (4): ['uid:1001', 'person/yury', 'cron-owner', '125805788']
sessions total=735  visible to the owner=723  invisible=12
```

Пояснение к числам: карточка называет «246 сессий» (снимок 19.11). На текущих базах — 735 строк;
реестр шага 2 + `person/yury` отдают владельцу **723 из 735**, невидимыми остаются 12
(`api` 4, `<none>` 6, два чужих telegram по 1) — ровно те же 12, что и в отчёте шага 2. Ни одна
чужая строка владельцу не добавлена; `probe.alice` (обычная учётка) пересечения с его скоупом не
имеет (`[]`).

### 5. Smoke-гейт до/после

```
base  (f2596a3524, без правок): 2 failed, 56 passed, 30 errors
ветка (с правками):             2 failed, 195 passed, 30 errors
```

Падения **предсуществующие** и идентичные: `tests/gateway/test_webapp_owner_identity.py` (2) и
`tests/gateway/test_session_owner_scope.py` (30 ошибок) — те же, что зафиксированы в отчёте шага 2
на базе `e3d1ec0a52`; причина — `tests/home_io_guard.py:160` (bootstrap агента трогает реальный
корень репозитория). Число упавших из-за правок — **0**.

Тот же набор, что и на базе, падает в `tests/hermes_cli/test_refresh_singleflight.py` и
`test_webapp_*` (8 падений до правок и 8 после — проверено `git stash`), т.е. регрессий нет.

`python scripts/check`: 15 проверок, FAILED — footguns/no-tmp/ruff/health **на базе и на ветке
одинаково**; блокирующих health-находок 68 до и 68 после, ruff — 73 до и 73 после; ни одна не
называет новые файлы (`hermes_acl_identity.py`, три новых тест-модуля). Для четырёх «слепых
`except`» в новом коде стоит явная строка `# health: allow ... -- <почему>`.

## Границы соблюдены

* `acl.multi_user` поставляется **выключенным**; в живом `~/.hermes/config.yaml` ключа нет.
  Включение режима для всех — отдельное решение владельца.
* Живая установка не тронута: `~/.hermes/dashboard-users.jsonl` без изменений (mtime тот же),
  `~/.hermes/acl/` отсутствует, `~/.hermes/config.yaml` без правок; все пробы шли на копиях.
* Права на сессии (шаг 4) не трогались: менялось только «кто такой вошедший» и «жив ли его
  мандат».
* Пароли и хэши не печатались; в выводе — логины, субъекты и номера поколений.

## Остаётся на ручную проверку

* Живой gateway с `acl.multi_user: true` и реальным прокси: браузерная реакция на 401
  `session_revoked` (редирект на `/login` с очисткой cookie) проверена на уровне
  `middleware`/провайдера, но не в живом браузере.
* Включение режима для всех пользователей — решение владельца, не этого шага.
