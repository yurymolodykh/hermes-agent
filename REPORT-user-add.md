# Отчёт: `hermes user add` — один пользователь одной командой (t_54c6b6f2)

## Что сделано

Одна команда заводит пользователя целиком или не заводит ничего:

```
hermes user add <login> [--telegram-id ID] [--display-name ...] [--role user|admin]
hermes user list
hermes user remove <login> [--yes]
```

Порядок шагов и собственный откат на каждый (при сбое разматывается в обратном порядке,
команда возвращает ненулевой код, частичного пользователя не остаётся):

1. профиль `01-<login>` — `create_profile(..., clone_from="01-orchestrator", clone_config=True)`
   (без `--clone-channels`: токены мессенджеров не копируются);
2. SOUL-клон + `SOUL-QUESTIONNAIRE.md` — через `personal_soul_seed` / `seed_questionnaire`;
3. ACL-привязка `person/<login>` с субъектами (`tg:<id>`), реестр `acl/profile-bindings.json`;
4. users-file `users.jsonl` (`UserRecord` + `hash_password`) и `credentials/<login>.password`;
5. маршрут `gateway.profile_routes`: `platform: telegram`, `profile: 01-<login>`, `user_id`,
   `bot_profile: null` (один бот владельца на всех), `enabled: true`.

Дубль профиля или занятый `user_id` → отказ **кодом 2 до первой записи**.
Пароль: в stdout только первые 7 символов, полный — в файл 0600, путь печатается.
`user remove` снимает маршрут, привязку, запись users-file, файл пароля и профиль.

Файлы: `hermes_cli/user_admin.py` (676 строк), `tests/hermes_cli/test_user_admin.py` (14 тестов),
регистрация в `hermes_cli/main.py` (локальный импорт + `"user"` в `_BUILTIN_SUBCOMMANDS`).

## Проверки

| Проверка | Результат |
|---|---|
| `scripts/run_tests.sh tests/hermes_cli/test_user_admin.py -q` | 14 passed, 0 failed |
| `+ test_acl_bindings.py + test_dashboard_users.py` | 184 passed, 0 failed, 8 skipped |
| `ruff check --config ruff.pre-commit.toml` (оба файла + main.py) | All checks passed |
| `python -m scripts.code_health --base feature/multiuser-access` | 0 blocking, 0 advisory |
| Живая проба: `user add demo1 --telegram-id 555000111` | exit 0, SOUL 7252 Б, опросник 5530 Б |
| Живая проба: повторный `user add demo1` | exit 2 («profile 01-demo1 already exists») |
| Живая проба: токены Telegram в config.yaml | 0 совпадений |
| Живая проба: права файлов | `users.jsonl` 600, `credentials/demo1.password` 600 |
| Живая проба: `user remove demo1 --yes` | удалены маршрут, привязка, запись, пароль, профиль |

Тесты покрывают: полное создание; пароль только в файле 0600; полного пароля нет в stdout;
работу без `--telegram-id`; отказ на дубле профиля и на занятом `tg` id без изменений на диске;
откат при подсунутом сбое после профиля и при сбое привязки; ненулевой код после отката;
`list` не печатает хэши; `remove` удаляет все пять артефактов; отказ при удалении несуществующего.

## Ограничение (важно для приёмки)

`profiles/` и ACL-реестр якорятся к `get_default_hermes_root()`, а не к `HERMES_HOME`.
Проба с `HERMES_HOME` **вне** `~/.hermes` (например `/var/tmp/...`) полностью изолирована.
Проба с `HERMES_HOME` **внутри** `~/.hermes` пишет профиль в боевой `profiles/` — первая
попытка так и сделала; созданное удалено (`user remove demo1 --yes`, каталог `acl/`), боевой
`config.yaml` не изменился (`profile_routes: []`), `01-demo1` и `~/.hermes/acl` отсутствуют.

## Вне рамок (соблюдено)

`acl.enabled` не включался, боевых пользователей не создавалось, `config.yaml` 02–15 не
менялись, общее дерево не тронуто, в upstream не пушилось, второй бот не запускался.

## Ревизия

Ветка `wt/t_54c6b6f2` от `feature/multiuser-access` (`65db1ee540b`), верхушка `42dfe97bc68`.
