# Отчёт — t_e81e8826 «[МПД] Шаг 2: реестр привязок персона → профили»

Ветка: `feature/mpd-step2-bindings` (от `feature/multiuser-access` @ `e3d1ec0a52`).
Worktree: `/home/hermes/.hermes/hermes-agent/.worktrees/t_e81e8826`.
Тестовый интерпретатор: `~/.hermes/installs/019d0d114d22b7e6/test-environment/gen-33b3d260ac3140808236b257c2f566fd/venv/bin/python`
(Python 3.14.9, pytest 9.1.1).

## Что сделано

Реестр — отдельный модуль `hermes_acl_bindings.py` (762 строки), а не расширение
`hermes_acl_scope.py`: он отвечает ровно на два вопроса («кто этот субъект» и «чьи это профили»)
и является единственной точкой разрешения «один человек / двое» для шагов 3–6.

| Файл | Роль |
| --- | --- |
| `hermes_acl_bindings.py` (новый, 762) | `Bindings`/`PersonRecord`/`BindingStore`, `same_person`, `person_stamps`, `prime`, `load_cached`, бэкапы, миграция |
| `hermes_acl_scope.py` | `owner_stamps` расширяет штампы персоны; `caller_owner_scope`/`request_owner_scope` при битом реестре отвечают `[]` (отказ) |
| `gateway/owner_identity.py` | `same_owner` → `_same_person_binding`; старые правила сохранены, реестр только добавляет явное «это один человек» |
| `gateway/run_bootstrap.py` | `prime()` при старте gateway, через `_best_effort` — сбой файла не роняет процесс |
| `hermes_cli/subcommands/auth_bindings.py` (новый, 247) | `hermes auth bindings list\|show\|bind\|unbind\|share\|owner` |
| `hermes_cli/subcommands/auth.py`, `hermes_cli/auth_commands.py` | регистрация парсера и отдельный dispatch (`bindings_action`) |
| `hermes_cli/config_defaults.py` | дефолт `acl.bindings_file: ""` (пусто = `<root>/acl/profile-bindings.json`) |

## Решения, которые стоит знать ревьюеру

1. **Fail-closed означает пустой скоуп, а не «уже»-набор.** Битый реестр → `request_owner_scope`
   возвращает `[]`, store рендерит `1 = 0`. Откат к «только `uid:1001`» запрещён: это скрыло бы
   от владельца 268 его записей, не выдав никому чужих — и выглядело бы как штатная работа.
2. **Реестр аддитивен.** Нет файла → все прежние сравнения дают ровно прежний ответ, поэтому шаг 2
   безопасно ложится до включения идентичности (шаг 3).
3. **`acl.enabled` не трогается.** Шаг 2 ничего не включает; при выключенном ACL битый файл вообще
   не влияет на одно-пользовательскую установку.
4. **`--holds`, не `--profile`.** Флаг `--profile/-p` вычитывается
   `main._apply_profile_override` из argv ДО argparse и перевешивает процесс на другой профиль.
   Проверено живьём: `auth bindings bind person/alice --profile 01-alice` → `Error: Profile
   '01-alice' does not exist`. Поэтому флаг назван `--holds`; тест
   `test_profile_flag_is_not_registered_on_the_bindings_verbs` не даёт ловушке вернуться.
5. **Идемпотентность `owner` строгая:** повтор с теми же аргументами не перезаписывает файл и не
   плодит бэкапы, поэтому миграцию можно перезапускать как ремонт.

## Критерии приёмки

### 1. Реестр создаётся, читается, отдаёт привязки — 144 теста зелёные

```
tests/hermes_cli/test_acl_bindings.py          104 passed
tests/gateway/test_acl_bindings_scope.py        15 passed
tests/hermes_cli/test_auth_bindings_cli.py      25 passed
                                        итого  144 passed
```
`scripts/run_tests.sh` на первых двух файлах: `119 tests passed, 0 failed`.

### 2. Битый реестр → отказ (отдельные тесты)

`TestBrokenRegistryDenies` (6 тестов) и `TestBrokenRegistry` в CLI-файле (4 теста):
невалидный JSON, нечитаемый файл (I/O), «файл сломался ПОСЛЕ здоровой загрузки» (кэш не спасает),
битый при выключенном ACL (ничего не меняет), CLI не перезаписывает битый файл и печатает путь
бэкапа для восстановления.

### 3. Владелец видит ровно свои сессии — на копиях баз

Скрипт: `~/.hermes/profiles/03-software-engineer/cache/scratch/t_e81e8826/verify_owner_sessions.py`
(26 копий `state.db` в отдельный tmp-каталог, живые базы только читаются).

```
total sessions across every store: 717
  uid:1001     437      cron-owner  267      <none>  6
  api  4    125805788  1    449439313  1    543447681  1

acl.enabled=false -> scope None (unscoped): OK
resolved owner scope: ['person/yury', 'uid:1001', 'cron-owner', '125805788']

step 3 с БЕЗ реестра (субъект 'person/yury'):   0 строк   (потеряно 717)
только legacy-штамп 'uid:1001':                437 строк   (потеряно 280)
с реестром:                                    705 строк   (потеряно  12)

corrupt registry -> scope []
rows served under the denial: 0
```

705 = 437 + 267 + 1 (все штампы владельца), ни одной чужой строки. Исключённые 12 — это `api` (4),
`<none>` (6) и два чужих telegram-id по одной сессии; они и не должны попадать владельцу.
Скрипт падает с assert, если скоуп отдаёт хоть одну строку сверх штампов владельца.

> Расхождение с формулировкой карточки («было 246»): замер на текущих базах даёт 717 сессий,
> из них штампы владельца — 705. Цифра 246 в карточке — снимок на 19.11; за сутки парк вырос.
> Привожу измеренное, а не унаследованное.

### 4. Резервная копия при каждом изменении

`<parent>/backups/profile-bindings-latest.json` + штампованные копии, режим `0600`, ротация
(`BACKUP_KEEP`, по умолчанию хранит ограниченное число). Покрыто `TestBackups` (6 тестов):
бэкап пишется на каждое изменение, latest содержит ПРЕДЫДУЩУЮ версию, восстановление возвращает
прежнюю модель, ротация ограничена, бэкап возможен даже с нечитаемого файла.
Путь в живом прогоне: `…/acl/backups/profile-bindings-latest.json`.

## Проверки, которые НЕ проходят, и это не регрессия

Воспроизведено на базовой ревизии `e3d1ec0a52` в отдельном worktree:

| Набор | Мой прогон | База |
| --- | --- | --- |
| `tests/gateway/test_session_owner_scope.py` | 7 passed, 30 errors | 7 passed, 30 errors |
| `tests/gateway/test_webapp_owner_identity.py` | 2 failed, 12 passed | 2 failed, 12 passed |
| `test_authenticated_session_identity` / `test_legacy_client_label_adoption` / `test_legacy_session_migration` | 2 / 2 / 4 failed | 2 / 2 / 4 failed |

Причина — `tests/home_io_guard.py:160`: bootstrap агента трогает реальный корень репозитория
(`.git/worktrees/…/hermes-update-pull`) при `append_message`. К реестру отношения не имеет.
`scripts/check` на базе тоже падает (`43 blocking`), у меня — те же `43 blocking`, ни одна строка
не называет мои файлы. Новых нарушений нет; `ruff` по всем моим файлам — «All checks passed».

## Границы, которые соблюдены

- Поведение входа не менялось (шаг 3).
- Мультипользовательский режим не включался; `acl.enabled` в дефолтах остался `False`.
- Общий каталог `~/.hermes/hermes-agent` не тронут: работа в своём worktree и своей ветке.
- Секретов в реестре нет: `hermes_acl_bindings` не читает `dashboard-users.jsonl`; CLI-тест
  `test_list_never_prints_hash_material` проверяет отсутствие `password`/`scrypt$`/`$2b$`/`$argon2`.

## Осталось / на ревью

- `manual_review`: поведение на живом gateway с `acl.enabled=true` и реальным прокси — это шаг 3,
  вне объёма.
- Унаследованное замечание шага 1 (сообщение bind показывает только skip-reason nous) не затронуто.
