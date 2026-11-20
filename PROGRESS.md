# t_e81e8826 — МПД шаг 2: реестр привязок «персона → профили»

## Состояние: РЕАЛИЗОВАНО И ПРОВЕРЕНО, готово к коммиту/ревью

- [x] Среда: worktree, ветка `feature/mpd-step2-bindings` (от `feature/multiuser-access` e3d1ec0a52)
- [x] Test venv: `~/.hermes/installs/019d0d114d22b7e6/test-environment/gen-33b3d260ac3140808236b257c2f566fd/venv/bin/python` (pytest 9.1.1)
- [x] Замер на копиях: 717 сессий в 26 БД; штампы `uid:1001`=437, `cron-owner`=267, `<none>`=6, `api`=4, telegram 125805788/449439313/543447681 = 1/1/1
- [x] Код: `hermes_acl_bindings.py` (762) — формат, fail-closed load, бэкап, миграция, `same_person`, `person_stamps`, `prime`
- [x] Интеграция: `owner_identity.same_owner` (+`_same_person_binding`), `hermes_acl_scope.owner_stamps`/`caller_owner_scope`/`request_owner_scope`, `prime` в `gateway/run_bootstrap.py`
- [x] CLI: `hermes auth bindings list|show|bind|unbind|share|owner` (`hermes_cli/subcommands/auth_bindings.py`)
- [x] Тесты: 144 passed (104 + 15 + 25), три файла
- [x] Отчёт: `REPORT-step2.md` — числа 717 → 705, путь бэкапа, до/после на копиях

## Важное для продолжающего

- Флаг привязки профиля — **`--holds`**, не `--profile`: `main._apply_profile_override`
  вычитывает `--profile/-p` из argv до argparse и перевешивает процесс на другой профиль.
- Реестр root-anchored (`<root>/acl/profile-bindings.json`), не по профилю. Для сквозных
  прогонов задавать `acl.bindings_file` в изолированном `config.yaml`, иначе CLI пишет в реальный
  корень оператора.
- Предсуществующие падения (не регрессия, воспроизведены на базе): 30 errors в
  `test_session_owner_scope.py`, 2 failed в `test_webapp_owner_identity.py`, по 2/2/4 в
  `test_authenticated_session_identity` / `test_legacy_client_label_adoption` /
  `test_legacy_session_migration`; причина — `tests/home_io_guard.py:160`.

## Дальше

Коммит в `feature/mpd-step2-bindings` с номером карточки, затем передача ревизии
на независимую проверку (15-sre-qa).
