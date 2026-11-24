# Вердикт ревью t_96fef625 — раунд 1 (линза «артефакт» + «исполнение»)

**Вердикт: REQUEST CHANGES.** Код трёх правок — по существу верный и доказан RED/GREEN.
Блокирует приёмку один конкретный дефект: **новый тест-файл `test_ws_ticket_profile_gate.py`
негерметичен** — падает 4/4 при прогоне из самого worktree карточки (т.е. ровно там, где его
гоняет исполнитель и следующий ревьюер), тогда как `test_webapp_ticket_profile_gate.py` (6) и
`test_acl_multi_user_inheritance.py` (5) в том же worktree зелёные.

Ревизия: `42ea2501fdce73889e2d16a72f29767d7c01afa3`, база `f2f8a59cc2c6d3fb8d0ea49dff97e262db26fc26`.
Диф: 6 файлов, +510/−1. Дерево чистое.

## 1. Что проверено инструментом (не по сводке)

| Проба | Команда/место | Результат |
|---|---|---|
| Новые тесты, дерево вне дома | `/tmp/t96rev_*` (копия) | `17 passed` (6+4+5 + прежний `test_webapp_ticket_profile.py` 2) |
| RED на базе | `/tmp/t96base_*` = `git archive f2f8a59c` + 3 новых файла тестов | **3 падения ровно целевые**: `test_a_foreign_profile_is_refused_with_the_unknown_name_404`, `test_a_foreign_profile_ticket_closes_the_socket`, `test_a_profile_without_an_acl_section_inherits_the_root` |
| Регресс `test_acl_identity.py` | копия | `81 passed` |
| Новые тесты **в worktree карточки** | `pytest tests/hermes_cli/test_ws_ticket_profile_gate.py` | **4 failed** — `TEST BUG: file I/O against the REAL hermes home: …/.git/worktrees/t_96fef625/hermes-update-pull` |
| То же без `.git` в дереве | `/tmp/t96nogit_*` | **4 passed** |
| `tests/gateway/test_session_subject_scope.py` | копия / без `.git` / worktree | `19 passed` / `19 passed` / `2 failed, 3 passed, 14 errors` |

## 2. Код: три правки — verified

1. `hermes_cli/web_routers/webapp.py:270-279` — `_require_profile_for_principal(request, identity, profile)`
   вызван **внутри `if checks_profile:` и до `_cron_profile_home(profile)`** (стр. 275 → 276).
   Гейт применяется к identity **самого тикета** (`_require_profile_for_principal`, стр. 178-202:
   строит `state.session` из `identity`, пробрасывает `native_http_principal`), а не к анонимному
   `request.state` — это снимает lockout владельца на loopback. Политика не изобретена:
   `hermes_acl_profiles.require_visible` → `request_allows` → `allows`/`multi_user_enabled`/`is_owner`.
   Loopback-владелец проверен тестом (ниже).
2. `hermes_cli/web_routers/chat_ws.py` — `_ticket_profile_refused(identity, ticket_home)` стоит
   **перед** `identity = {**identity, 'profile_id': served.profile_id, …}`; отказ → `await ws.close(code=4403)`.
   Порядок строк подтверждён чтением `gateway_ws`. Тем самым `session_authority.authorize` больше
   не сравнивает подменённое с самим собой. `allows()` — та же функция, что у REST-роутов.
3. `hermes_acl_identity.py:64-109` — `_flag_from_user_layer` читает **user-layer**
   (`load_user_config_effective`), а не merged config: различает «выключено» и «не задано»
   (в `DEFAULT_CONFIG` секция `acl` есть — merged-чтение дало бы ложный False). Порядок:
   свой файл → `get_default_hermes_root()/config.yaml` → `load_config()`. Явный `false` в профиле
   выигрывает (тест есть). RED на базе по этому тесту воспроизведён.

## 3. Блокирующий дефект: негерметичность нового теста

**Где:** `tests/hermes_cli/test_ws_ticket_profile_gate.py`, все 4 теста.
**Как воспроизводится** (в самом worktree карточки):
```
cd /home/hermes/.hermes/hermes-agent/.worktrees/t_96fef625
python3 -m pytest tests/hermes_cli/test_ws_ticket_profile_gate.py -q
# 4 failed — tests/home_io_guard.py:160
#   TEST BUG: file I/O against the REAL hermes home:
#   /home/hermes/.hermes/hermes-agent/.git/worktrees/t_96fef625/hermes-update-pull
```
**Механизм (прослежен по трейсу, не догадка):** внутри тела теста импортируется
`hermes_cli.web_server` → `agent.conversation_loop` → `agent.process_bootstrap` → `hermes_bootstrap`,
а тот на **импорте** исполняет `_settle_interrupted_update()` →
`_early_recovery.restore_interrupted_pull(_root)` → `interrupted_pull_marker(root).is_file()`.
`_root` = каталог чекаута, у worktree `_git_dir` резолвит `.git`-файл в
`…/hermes-agent/.git/worktrees/t_96fef625`, и `stat` этого маркера попадает под autouse
`HomeIOGuard` (`tests/conftest.py:1770`), потому что импорт происходит **после** установки стража.
**Почему это дефект теста, а не окружения:** тот же прогон в копии дерева вне реального дома — `4 passed`;
в копии без `.git` внутри реального дома — тоже `4 passed`; при выносе импорта в module-level
тест-файла (до установки autouse-стража) — **`4 passed` уже в самом worktree**. То есть тест
чинится на своей стороне, и приёмка по «окружение виновато» была бы ложной.

**Минимальное исправление:** поднять импорт продакшн-цепочки на уровень модуля
(`import hermes_cli.web_server  # noqa: F401` + `import tui_gateway.ws  # noqa: F401` рядом с
`import hermes_acl_identity`), чтобы `hermes_bootstrap` отработал на collection, до стража.
Проверено: после этого `4 passed in 20.59s` в worktree. Помечать набор `allow_real_home_io`
нельзя — это обесценивает доказательство рубежа (совпадает с требованием оркестратора).

**Побочно (не в скоупе этой карточки, записать и не править):** тот же дефект класса у
прежнего `tests/hermes_cli/test_webapp_ticket_profile.py` (в worktree `2 failed` при
`2 passed` в копии) и у `tests/gateway/test_session_subject_scope.py` (в worktree
`2 failed, 14 errors`, в копии `19 passed`). Их импорт тоже идёт из тела тестов; правка —
отдельная карточка.

## 4. Поправка к диагнозу оркестратора

Утверждение «14 ошибок и 2 падения в `test_session_subject_scope.py` — дефект фикстуры,
`manifest.json` не изолирован» **не подтверждается** на этой ревизии: в копии дерева **и на базе
`f2f8a59c`, и на верхушке карточки** файл даёт `19 passed`. В worktree под живым домом — да,
падает, и там в 16 из 16 случаев путь
`/home/hermes/.hermes/profiles/15-sre-qa/cache/scratch/manifest.json`, т.е. **тот же** корень
причины — импорт-цепочка под стражем, а не отдельный дефект фикстуры `manifest.json`.
На базе в worktree я этот файл не гонял (бюджет), поэтому точное сравнение «было/стало»
по нему не заявляю: дефект присутствует и до правки по признаку «импорт из тела теста».

## 5. Чего я НЕ проверил

- Живой замер после выката и перезапуск шлюза — запрещены карточкой, за оркестратором.
- Прогон на базе **внутри worktree** для `test_session_subject_scope.py` (бюджет).
- `tests/hermes_cli/test_webapp_socket_tickets.py` висит (`exit 137`, 13 собравшихся, `.....E.E.E....E.`)
  **одинаково на базе и с правкой** — к правке не относится; отдельный дефект, эскалировать отдельно.
