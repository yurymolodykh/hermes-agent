# PROGRESS — t_4cb8f83b: точечный перенос двух апстрим-фиксов

## Состояние: ГОТОВО (обе правки внесены, тесты и проверки сняты). Остались финальный коммит и передача.

* Дерево: `/home/hermes/.hermes/hermes-agent/.worktrees/t_4cb8f83b`, ветка `wt/t_4cb8f83b`
* База для сравнения: `beeb9623539`
* Содержимое правки = `git diff beeb9623539`
* Отчёт: `REPORT-patches.md`; доказательства: `evidence-t4cb8f83b/`

## Сделано

### Патч 1 — класс `owner_unavailable` (PR #152921, перенос по смыслу, не `git apply`)

* `hermes_state_errors.py:99-103` — `PERSISTENCE_ERROR_CAUSES` += `"owner_unavailable"` (перед `"unknown"`).
* `hermes_state_errors.py` — `_PERSISTENCE_CAUSE_BY_PHRASE` += `(("owner_unavailable",), …)` и
  `(("worker_receipt_timeout",), "owner_unavailable")`, **после** `foreign key constraint failed` и
  **перед** `_PERMISSION_MARKERS`; комментарий PR сохранён (фраза, не тип; RPC теряет тип; импорт
  фасада воркера инвертировал бы зависимость листового модуля).
* Докстринг `classify_persistence_error` дополнен.
* `agent/turn_explainers.py:53-56` — `_PERSISTENCE_CAUSES` += `"owner_unavailable"` + комментарий.
* `locales/*.yaml` — 17 файлов, ключ `explainer.persistence.owner_unavailable` после `turn_lease`.
* `tests/agent/test_turn_completion_explainer.py` — 3 новых теста (+параметризация).

### Патч 2 — уборщик не убивает живых воркеров (issue #157057, диффа нет)

* `hermes_cli/kanban_db_dispatch.py::_reap_terminal_worker_row` — перед сигналом проба
  `_worker_alive(pid, fingerprint)`; живой → `_log.debug` + `return` (строка остаётся в выборке);
  мёртвый → снятие улик. Ветка сигнала удалена.
* Докстринги `reap_terminal_workers` и `DispatchResult.reaped_terminal_workers` приведены к факту.
* `tests/hermes_cli/test_kanban_terminal_worker_reaper.py` — 5 тестов переписаны под контракт.
* `website/docs/user-guide/features/kanban.md:138` — описание диспетчера обновлено.

Не тронуто: `TERMINAL_WORKER_REAP_GRACE_SECONDS`, `reclaim`-логика, `_terminate_reclaimed_worker`,
`enforce_max_runtime`, `kanban_auto_continue.py`, `kanban_profile_scope.py`, пороги ретраев.

## Проверки (все сняты)

* reaper-тесты: 5 passed; на базе — 2 failed (красная база доказана).
* explainer-тесты: 34 passed; на базе 4 новых падают (`-k owner`).
* reaper + explainer + outbox: 61 passed.
* Смежные наборы состояния: 109 passed, 7 skipped.
* `ruff check` — All checks passed. `code_health --base beeb9623539` — 0 blocking, 0 advisory.
* По-файлово `tests/hermes_cli/*kanban*` (99 файлов): база 17 FAIL / 82 PASS, патч 16 FAIL / 83 PASS;
  `diff` наборов = единственная строка `test_kanban_terminal_worker_reaper.py`.
* `tests/hermes_state` по-файлово: 134 падающих файла, все предсуществующие (страж
  `home_io_guard.py:160` ругается на путь worktree; выборка 12 файлов даёт одинаковые числа на
  базе и с правкой).

## Следующий шаг для будущей попытки

1. `git add -A && git commit` сообщением с номером карточки (t_4cb8f83b), затем
   `kanban_comment` — самопроверка, `kanban_complete` с artifacts:
   `REPORT-patches.md`, `evidence-t4cb8f83b/`.
2. Не вливать в общее дерево и не перезапускать шлюз — отдельная карточка выката.


---

# PROGRESS — t_54c6b6f2: `hermes user add` (одна команда — целый пользователь)

## Состояние: ГОТОВО. Тесты зелёные, живая проба снята, боевой ~/.hermes чист.

* Дерево: `/home/hermes/.hermes/kanban/workspaces/t_54c6b6f2`, ветка `wt/t_54c6b6f2`
* База: `feature/multiuser-access` = `65db1ee540b` (ветка ребейзнута на неё)
* Ревизия: `859fe4502ba` (верхушка; автокоммит поверх `412ef36c6f4`)

## Сделано

* `hermes_cli/user_admin.py` (новый, 676 строк): `add_user`, `remove_user`, `list_users`,
  `build_user_parser`, `cmd_user_add/list/remove`, `Refused` (код 2) / `Failed` (код 1).
  Порядок шагов и собственный undo на каждый: профиль → SOUL+опросник → привязка →
  users-file+пароль → маршрут. Любой сбой раскручивает undo в обратном порядке.
* Регистрация: `hermes_cli/main.py` — `build_user_parser(subparsers)` + `"user"` в
  `_BUILTIN_SUBCOMMANDS` (одна строка + локальный импорт, файл остаётся в своём капе).
* `tests/hermes_cli/test_user_admin.py` (новый, 14 тестов).
* Форматы взяты из кода, не выдуманы:
  * профиль — `profiles.create_profile(name, clone_from=..., clone_config=True, no_alias=True)`;
  * SOUL — `hermes_acl_soul.seed_personal_soul` через `personal_soul_seed` в `create_profile`,
    опросник `SOUL-QUESTIONNAIRE.md` — `seed_questionnaire(only_personal=True)`;
  * привязка — `hermes_acl_bindings.store().bind(slug="person/<login>", profiles=[...],
    subjects=["tg:<id>"])`; реестр `<root>/acl/profile-bindings.json`;
  * users-file — `dashboard_users.UserStore.upsert_record(UserRecord(...))`, `hash_password`;
  * маршрут — `gateway.profile_routes`: `{name, platform: telegram, profile, user_id,
    bot_profile: null, enabled}` (null = бот главного профиля).

## Проверено (команда + число)

* `scripts/run_tests.sh tests/hermes_cli/test_user_admin.py -q` → **14 passed**.
* `+ test_acl_bindings.py + test_dashboard_users.py` → **184 passed, 0 failed, 8 skipped**.
* `ruff check --config ruff.pre-commit.toml` на оба файла → All checks passed.
* `python -m scripts.code_health --base feature/multiuser-access` → **0 blocking, 0 advisory**.
* Живая проба `HERMES_HOME=$(mktemp -d)`:
  * `user add demo1 --telegram-id 555000111` → **exit 0**; SOUL 7252 Б, опросник 5530 Б,
    привязка `tg:555000111`, маршрут `bot_profile: null`, токенов Telegram в config.yaml **0**,
    `users.jsonl` и `credentials/demo1.password` — режим **600**;
  * повторный `user add demo1` → **exit 2** («profile 01-demo1 already exists»);
  * `user list` → строка `demo1 / 01-demo1 / bound yes / password yes / route yes`;
  * `user remove demo1 --yes` → удалены маршрут, привязка, запись users-file, файл пароля, профиль.

## Замечания для следующего прогона

1. Профили и ACL-реестр привязаны к `get_default_hermes_root()`, а НЕ к `HERMES_HOME`.
   Проба с `HERMES_HOME` вне `~/.hermes` (например `/var/tmp/ua-probe-XXXX`) изолирована
   полностью; `HERMES_HOME` внутри `~/.hermes` пишет профиль в боевой `profiles/`.
   Первая проба так и сделала — созданное удалено (профиль, `acl/`, запись маршрута), боевой
   `config.yaml` и `profile_routes: []` не изменились.
2. `tests/hermes_cli/test_profiles.py::...lazy_skill_count...` и
   `test_left_core_migration.py::test_old_mcp_serve...` падают и на базе (страж
   `home_io_guard` на путь worktree) — предсуществующие, не связаны с правкой.
3. Пароли владельцу не передавались (по указанию оркестратора — после закрытия карточки).
