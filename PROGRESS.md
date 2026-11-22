# PROGRESS — t_819d3353: SOUL-клон для 01-<login> + опросник персонализации

## Состояние: КРУГ 2 ГОТОВ — дефект ревью (address_style) исправлен, ветка закоммичена

* Дерево: `/home/hermes/.hermes/kanban/workspaces/t_819d3353`, ветка `wt/t_819d3353`
* База карточки: `0a1960695e`; рабочий коммит круга 1: `1c4dce2c24` (слит оркестратором в
  `feature/multiuser-access` как `8c02518532`); merge-коммит в ветке: `7ddd6106a6`
* **Верхушка ветки круга 2: `06faae7b4c`**
* Отчёт: `REPORT-soul-seed.md` (обновлён: uvicorn вместо TestClient + раздел про дефект)
* Доказательства: `evidence/` (`live_probe.py`, `live_probe.final.json`, `SOUL-01-testuser.md`,
  `SOUL-QUESTIONNAIRE.md`, `MEMORY-01-testuser.md` — перегенерированы в круге 2)

### Круг 2: что было дефектом и что исправлено

`address_style` печатался в `hermes_cli/data/soul-questionnaire.md:31`, принимался
`ProfileSoulQuestionnaire`, но `hermes_acl_soul.py` ключа не знал (`grep -c` → 0): `_section_reply`
собирал bullets только из `answer_length`/`answer_format`/`uncertainty`/`display_name`, ответ
молча терялся. Тесты были зелёными, потому что проверяли реализованное поведение.

1. `_REPLY_LINES` + `_reply_bullet`: `address_style` рендерится в «Ответ пользователю», каждый
   напечатанный выбор даёт собственную формулировку, свободный текст пишется дословно; подпись
   `- Как обращаться в ответе:` внесена в `_OWNED_LINE_PREFIXES` (идемпотентность).
2. `_section_work` пересобирает 4 слота из таблицы каждый раз (повтор переписывает, а не копит) и
   пишет свободный ответ как `- <подпись>: <ответ>` — `planning = «по обстоятельствам»` и
   `reporting = «итог + что проверено»` больше не no-op.
3. `questionnaire_answer_keys()` / `questionnaire_printed_choices()` — разбор самого файла
   опросника; `SAME_AS_DEFAULT = {("failure_mode", "сказать сразу")}` — единственный осознанный no-op.
4. `soul-questionnaire.md:16` — `address_form` печатается как `ты / вы`.
5. `TestInterfaceMatchesImplementation` — 9 `def test_` (параметризация → +40): каждый напечатанный
   выбор каждого поля обязан менять SOUL; каждое поле `ProfileSoulQuestionnaire` обязано быть
   напечатано; каждое напечатанное поле известно реализации; факты 5–6 идут в память, SOUL не
   трогают. Тест читает файл, а не захардкоженный список.
6. `evidence/live_probe.py` теперь отправляет `address_style: "без обращения"`.

### Проверки круга 2

* `tests/hermes_cli/test_profile_soul_seed.py` — **89 passed** (было 49).
* `tests/hermes_cli/test_profile_route_owner_scope.py` — **73 passed**; вместе — 162 passed.
* Смежная регрессия вне `~/.hermes` (`/tmp/soulbranch3.O5zjGY`, 6 файлов) — **304 passed, 3 skipped**.
* Живая проба: (а) 60 строк / 4025 симв., роль, 0 маркеров; (в) `changed_sections=["Роль","Границы",
  "Ответ пользователю"]`, sha `0621417f…`→`f2a8e5d0…`, в diff есть
  «- Как обращаться в ответе: без обращения — сразу к делу, без имени и приветствий.»;
  (г) пустой → sha идентичен; (д) 403 / 200.
* `python -m scripts.code_health --base HEAD` → 0 blocking, 0 advisory; ruff — All checks passed.
* Красная база `7ddd6106a6`: `address_style` × 3 = `changed=False`, `planning`/`reporting`
  напечатанные варианты = `changed=False`, `questionnaire_printed_choices` отсутствует (ImportError).
* **Осадок живой CLI-проверки убран:** `hermes profile delete -y 01-cliuser|plaingroup` — каталоги
  отсутствуют, в `gateway_state.json:served_profiles` их больше нет; рестарт шлюза не потребовался.

### Остаточное

* Оркестратор: слияние `wt/t_819d3353` (`06faae7b4c`) в `feature/multiuser-access` и публикация в `ours`.

### Круг 1 (для истории)

#### Что сделано

1. `hermes_acl_soul.py` (новый, stdlib) — `personal_soul_for`, `build_user_soul`,
   `apply_questionnaire`, `seed_questionnaire`, `questionnaire_text`, CLI `main`.
2. `hermes_cli/data/user_soul_base.md` + `hermes_cli/data/soul-questionnaire.md` — bundled
   источник и опросник (объявлены в `pyproject.toml` → `data/*.md`).
3. `hermes_cli/profiles_initialization.py` — `personal_soul_seed()` (best effort) и
   `materialize_symlinked_files` (перенос из фасада `profiles.py`).
4. `hermes_cli/profiles.py::create_profile` — персональный SOUL при `soul is None`;
   `finish_profile_layout(..., personal_root=True)` кладёт опросник.
5. `hermes_cli/web_routers/profiles.py` — `questionnaire_path` в ответе create;
   `POST /api/profiles/{name}/soul/apply-questionnaire` (гейт `require_writable`).
6. `hermes_cli/web_models.py` — `ProfileSoulQuestionnaire`.
7. `hermes_cli/profile_cmd.py` — CLI печатает путь к опроснику.
8. `tests/hermes_cli/test_profile_soul_seed.py` — 49 тестов.
9. `/home/hermes/workspaces/01-orchestrator/ops/soul-clone-for-user.py` — тонкая обёртка
   над платформенным модулем (правка не живёт только в ops).

#### Проверки

* `test_profile_soul_seed.py` — **49 passed** (требовалось ≥10).
* `test_profile_route_owner_scope.py` — **73 passed** (не уменьшилось).
* Живая проба (`evidence/live_probe.py`, изолированный HERMES_HOME): (а) 60 строк / 4025 симв.,
  роль «Ты — персональный ассистент testuser», 0 попаданий по 6 маркерам; (б) `questionnaire_path`
  в ответе create; (в) apply → 3 раздела, sha `0621417f`→`4d307ea7`, diff только в них;
  (г) пустой → sha идентичен; (д) чужой namespace → 403; свой корень → 200.
* CLI-путь: `01-cliuser` → 60 строк + опросник; `plaingroup` → прежний `DEFAULT_SOUL_MD` (1 строка),
  без опросника. Профили-зонды удалены из живого `~/.hermes`.
* Смежная регрессия вне `~/.hermes` (`/tmp/soulbranch2`): 129 passed + 3 skipped
  (profiles / clone_channels / soul_writes) и 135 passed (route_names / soul_seed / owner_scope).
* `tests/gateway/test_native_profile_create.py` — флейк готовности шлюза, воспроизводится и на базе
  `0a1960695e` (не регрессия).
* `scripts.code_health --base 0a1960695e` → 0 blocking, 0 advisory. `ruff check` — All checks passed.
* `pre-commit` пропущен при коммите: `scripts/hermes-python` в worktree падает на
  «no bootstrap Python found; run setup-hermes.sh». Та же проверка ruff прогнана напрямую
  интерпретатором test-environment — All checks passed.

### Остаточное (manual_review, вне рамок карточки)

* Показ опросника в десктопном клиенте — API отдаёт путь, UI отдельной темой.
* Миграция существующих профилей.
* Слияние в `feature/multiuser-access` и публикацию делает оркестратор (страж деревьев).

---

# PROGRESS — t_44bd1c6a (гонка провижининга: воркер стартует раньше симлинка окружения)

## Состояние: КОД И ПРОВЕРКИ ГОТОВЫ; ОСТАЛОСЬ — КОММИТ

* Дерево: `/home/hermes/.hermes/hermes-agent/.worktrees/t_44bd1c6a`, ветка `wt/t_44bd1c6a`
* База: `b8c2c25eaf35`
* Отчёт: `REPORT-worker-env-race.md`
* Проба: `/home/hermes/.hermes/profiles/02-hermes-platform/cache/scratch/live_probe_worker_env.py`
  (14/14 OK на боевом `HERMES_HOME`, включая красный бейзлайн)

### Что сделано

* `hermes_cli/kanban_db_worker_env.py` (новый) — `ensure_tree_install_state(workspace)`: мост
  install-key на пути спавна; `worker_terminal_timeout_env` (перенос тела из диспетчера);
  `UNREADY_REASON = "dependency_environment_unready"`; `_dependency_source_root` берёт главный
  checkout через `git rev-parse --git-common-dir` (не обходом ФС), чтобы мост был В ОДИН хоп.
* `hermes_cli/kanban_db_capacity.py` (новый) — `count_running_tasks` /
  `count_running_tasks_other_boards` вынесены из диспетчера (ratchet «facade may only shrink»).
* `hermes_cli/kanban_db_dispatch.py` — только швы: импорты `_kbenv`/`_kbcap`, поле
  `DispatchResult.skipped_env_unready`, `_hold_if_workspace_unready` (вызов в
  `_dispatch_lane_task` ПОСЛЕ материализации workspace и ДО спавна),
  `_record_nonspawnable_assignee` (вынос из той же функции ради CC ≤ 20), шимы
  `_worker_terminal_timeout_env` / `count_running_tasks*`, строка `env_unready` в
  `describe_suppression`.
* `tests/hermes_cli/test_kanban_worker_env_provisioning.py` (новый, 10 тестов).

### Проверки (все выполнены)

* `10 passed` — новый набор (в копии дерева `/tmp/t44branch`).
* `54 passed` — новый набор + 8 смежных канбан-наборов (там же; `tests/home_io_guard.py` рубит
  I/O по реальному дому, поэтому прогон вне `~/.hermes`).
* Красная база: на `b8c2c25eaf35` с подложенным новым модулем — **5 failed** из 10 (симлинка нет,
  спавн всё равно происходит, `env_unready` пуст); без модуля — ImportError на сборе.
* `python -m scripts.code_health --base HEAD` → **0 blocking, 0 advisory**.
* `ruff check` по 4 файлам — All checks passed.
* `scripts/boundary_lint.py --check` — те же предсуществующие кроссы, что и на базе (дельта 0).
* Живая проба на боевом `~/.hermes`: 14/14 OK; свежее дерево получает ссылку за 3 мс, воркерский
  бут на store-python выходит 0 и видит `ROOT /tmp/t44probe/wt-fresh` (изоляция worktree цела), а
  дерево-близнец без моста воспроизводит `no dependency environment is committed`.
1. `git add`/`commit` в ветке карточки.
2. Рестарт шлюза — шаг оркестратора (из карточки не делаем).
3. `manual_review`: «0 прогонов с `no dependency environment` за 24 ч после внедрения» —
   наблюдение после рестарта, вперёд агенту недоступно.

---

# PROGRESS — t_f3d28e37 (МПД шаг 6: результаты — файлы, скиллы, память, вложения)

## Состояние: РАБОТА ЗАВЕРШЕНА (код, 79 тестов, живая проба 25/25, база красная 16 LEAK, отчёт)

* Ветка: `wt/t_f3d28e37`, worktree `.worktrees/t_f3d28e37`
* База: `9b2e01a4f5` (merge шага 5 в ветку карточки)
* Отчёт: `REPORT-step6.md`
* Пробы: `verify-step6-live.py` + `PROBE-step6-output.txt` (25/25), `verify-step6-counts.py`
  (owner 20/10 до и после; alice 10 своих / 0 чужих / 0 в корне)

## Что сделано (шаг 6)

* `hermes_acl_results.py` (новый, 400 стр.) — одна точка политики Т6: `scoped`, `file_roots`
  (свои профили + `kanban/{workspaces,attachments}/<id>` СВОИХ карточек; пустой кортеж =
  fail closed), `path_allowed`/`require_path` (содержательность по `resolve()`, поэтому `..` и
  симлинк наружу = 403), `resolve_profile` (без `?profile=` → свой профиль, не профиль запуска;
  чужое имя → 404 шага 5), `memory_dirs_for` (только свой бакет `u/<subject>`),
  `task_visible`/`require_task` (владение по карточке, не по id файла; чужое → 404 как у
  отсутствующего id).
* Швы: `web_server_files._resolve_managed_path` + `_hosted_fs_read_guard` + `_caller_default`;
  `web_routers/files.py` (`write-text` до записи байтов, `git-root` на точку и корень);
  `web_routers/skills.py` и `ops.py` (`results_profile` + `require_results_write` +
  `memory_dirs_for`); `plugins/kanban/dashboard/plugin_api.py` (`_require_owned_task` на всех
  четырёх ручках вложений).
* `hermes_cli/web_routers/_common.py` — `results_profile` / `require_results_write`.
* `tests/hermes_cli/test_results_route_scope.py` (новый, **79 тестов**, 12 классов).

## Найденный и исправленный собственный дефект

Первая редакция `_multi_user()` читала флаг через `load_config()`, а тот СОЗДАЁТ скелет
`~/.hermes` — т.е. листание каталога материализовало домашний каталог оператора. Ловилось
падением `test_web_server_files.py::test_directory_listing_skips_vanished_regular_file`
(лишний `.hermes` в листинге). Исправлено: флаг читается, только если домашний каталог уже
есть; запинено тестом `test_reading_the_flag_never_creates_a_hermes_home`.

## Проверенное

* 79 passed (`tests/hermes_cli/test_results_route_scope.py`, 14 s).
* Смежные 9 наборов: ветка **2 failed / 180 passed / 10 skipped**; база — ровно те же 2
  (предсуществующие `test_download_resolves_paths_in_the_originating_profile_session`
  [/api/fs/download] и [/api/fs/read-data-url], 503 «Session store is not initialized»).
* Живая проба `verify-step6-live.py`: **ИТОГ 25/25**, exit 0 (`PROBE-step6-output.txt`).
* База красная: та же проба, скопированная в дерево базы (`verify-step6-baseline-red.py` —
  копия `verify-step6-live.py`), на `9b2e01a4f5` — **9/25, 16 LEAK**.
* `python -m scripts.code_health --base 9b2e01a4f5` → **0 blocking, 0 advisory** (10 файлов).
* `ruff check` по 8 файлам — All checks passed.
* `scripts/boundary_lint.py --check` — падает так же, как база (те же 6 предсуществующих
  кроссов `plugins/dashboard_auth/basic`); единственная дельта — запись baseline
  `plugins/kanban/dashboard/plugin_api.py hermes_acl_results.require_task: 1` (осознанно).

## Ограничения

* `manual_review`: Windows-ограничение шима **не проверено** (прогон только на Linux) — так и
  написано в отчёте, за проверенное не выдаётся.
* `manual_review`: два живых браузера против запущенного шлюза с `acl.multi_user: true`
  (проверено на уровне роутеров/политики и живых HTTP-кодов через TestClient, не по TCP).
* Включение `acl.multi_user: true` — решение владельца, поставка ничего не включает.
* Тесты/пробы гонять на копии дерева вне `~/.hermes` (`/tmp/mpd6/branch3`):
  `tests/home_io_guard.py` рубит инициализацию БД внутри реального дома. Проба берёт
  `HERMES_HOME` в `/tmp`, иначе `get_default_hermes_root` вернёт реальный `~/.hermes`.
* `pre-commit` пропущен: `scripts/hermes-python` в worktree падает («no bootstrap Python found»).
* Шаги 3–5 не менялись, данные не переносились, общий каталог платформы не тронут.

## Команды для повтора

```bash
PY=/home/hermes/.hermes/installs/1a23eac39a17788f/test-environment/gen-5ac62ab36b984c04812d85ab1fc76459/venv/bin/python3
mkdir -p /tmp/mpd6/branch3 && cd <worktree> && \
  tar -c --exclude=.git --exclude=.venv --exclude=__pycache__ -f - . | tar -x -f - -C /tmp/mpd6/branch3
cd /tmp/mpd6/branch3 && HERMES_HOME=$(mktemp -d /tmp/mpd6t.XXXX) \
  $PY -m pytest -q tests/hermes_cli/test_results_route_scope.py      # 79 passed
cd /tmp/mpd6/branch3 && HERMES_DASHBOARD_SESSION_TOKEN=x $PY verify-step6-live.py       # 25/25
cd /tmp/mpd6/branch3 && $PY verify-step6-counts.py                                     # owner 20/10
cd /tmp/mpd6/base && HERMES_DASHBOARD_SESSION_TOKEN=x $PY verify-step6-baseline-red.py # 9/25
cd <worktree> && $PY -m scripts.code_health --base 9b2e01a4f5                          # 0 blocking
```

---

# История: шаг 5 (t_a08e47a4) — профили

## Состояние: РАБОТА ЗАВЕРШЕНА (код, 73 теста, живая проба 24/24, база красная, отчёт, коммит)

* Ветка: `wt/t_a08e47a4`, worktree `.worktrees/t_a08e47a4`
* База: `8db3cd6c1c` (верхушка шага 4)
* Коммит шага 5: **`afaf9d8145`**
* Отчёт: `REPORT-step5.md`
* Пробы: `verify-step5-live.py` + `PROBE-step5-output.txt` (24/24 OK),
  `verify-step5-baseline-red.py` (на базе — 4 утечки)

## Что сделано (шаг 5)

* `hermes_acl_profiles.py` (новый, 389 стр.) — одна точка политики: `visible_profiles`,
  `allows`, `writable_profiles`, `shared_candidates`, `scope_for`/`scope_for_names`,
  `require_visible`/`require_writable`/`require_owner`/`require_create`.
  Owner — всё; человек — свои записи реестра шага 2 + shared; shared видим и используем,
  но не перенастраиваем; `acl.multi_user=false` → unscoped (прежнее поведение);
  битый/отсутствующий реестр → fail closed.
* `hermes_cli/web_routers/profiles.py` — фильтрация `GET /api/profiles`, отказ `POST`
  (`profile_not_allowed` 403), проверки на всех `/api/profiles/{name}/…` (чужое = 404,
  shared на запись = 403 `profile_read_only`), `active` не называет чужой sticky и
  POST owner-only, delete/export/import owner-only, веер `?profile=` только по видимым;
  кэши `_sidebar_payload` / `_projects_tree_payload` получили видимый набор и owner-скоуп
  в КЛЮЧ (иначе второй человек в том же процессе получил бы чужой payload).
* Строки внутри видимого профиля сохраняют свой owner-скоуп: `user_id=owner_scope` в
  `list_sessions_rich` / `session_count` / `usage_totals` и в дереве проектов
  (`tui_gateway/methods_projects.py` + `hermes_state_usage.py` —
  `_usage_owner_clause` → `hermes_state_sessions._owner_scope_clause`, один WHERE-арм).
* `tests/hermes_cli/test_profile_route_owner_scope.py` (новый, 563 стр., **73 теста**).

## Проверенное (шаг 5)

* 73 passed (`tests/hermes_cli/test_profile_route_owner_scope.py`, 6.8 s).
* 136 passed (session scope + profile scope + unification + gateway native profile
  create/registry grant).
* Смежные 12 наборов: ветка 8 failed / 153 passed / 3 skipped; база — ровно те же 8
  (предсуществующие `test_dashboard_param_clamps`) — падений не добавлено.
* Живая проба `verify-step5-live.py`: `ИТОГ: OK — 24/24`; критерии 2,3,4,5 подтверждены
  кодами и телами (alice → `['01-alice','02-hermes-platform','03-software-engineer']`;
  POST чужого имени → 403; bob → своя тройка; owner → весь roster).
* База красная: `verify-step5-baseline-red.py` на `8db3cd6c1c` — LEAK 1..4 все True;
  новый тестовый файл на базе не собирается (`ModuleNotFoundError: hermes_acl_profiles`).
* `python -m scripts.code_health --base 8db3cd6c1c` → 0 blocking, 0 advisory.
* `scripts/check --only health` — ни одной находки в изменённых файлах.
* `ruff check` по 7 файлам — All checks passed.

## Ограничения (шаг 5)

* `manual_review`: два живых браузера против запущенного шлюза с `acl.multi_user: true`
  (проверено на уровне роутеров, политики и живых HTTP-кодов, не по TCP).
* Включение `acl.multi_user: true` — решение владельца, поставка ничего не включает.
* Тесты гонять на копии дерева вне `~/.hermes` (`/tmp/mpd5/branch`): worktree внутри
  реального дома, `tests/home_io_guard.py` рубит инициализацию БД. Проба должна брать
  `HERMES_HOME` в `/tmp`, не в `~/.hermes/...` — иначе `get_default_hermes_root` вернёт
  реальный `~/.hermes`, и проба увидит настоящий roster.
* `pre-commit` пропущен: `scripts/hermes-python` в worktree падает на «no bootstrap
  Python found»; ruff прогнан напрямую.
* Шаг 6 (файлы) не трогался. Профили между пользователями не переносились.

## Команды для повтора

```bash
# тесты (копия дерева вне ~/.hermes)
tar -c --exclude=.git --exclude=.venv --exclude=__pycache__ -f - . | tar -x -f - -C /tmp/mpd5/branch
cd /tmp/mpd5/branch && HERMES_HOME=$(mktemp -d /tmp/mpd5home.XXXX) \
  /home/hermes/.hermes/hermes-agent/.venv/bin/python -m pytest -q \
  tests/hermes_cli/test_profile_route_owner_scope.py
# живая проба
cd /tmp/mpd5/branch && HERMES_DASHBOARD_SESSION_TOKEN=x \
  /home/hermes/.hermes/hermes-agent/.venv/bin/python verify-step5-live.py
# code health
cd <worktree> && /home/hermes/.hermes/hermes-agent/.venv/bin/python -m scripts.code_health --base 8db3cd6c1c
```

---

# История: шаг 4 (t_4997de4c)

## Состояние: РАБОТА ЗАВЕРШЕНА (код, тесты, две живые пробы, отчёт, коммит)

* Ветка: `feature/mpd-step4-sessions`, worktree `.worktrees/t_4997de4c`
* База: шаг 3 — `787f022c9b` (`feature/mpd-step3-identity`)
* Коммит шага 4 (REST): `7b9634c390`; сокетный скоуп — коммит поверх него (см. `git log`)
* Отчёт: `REPORT-step4.md`
* Пробы: `verify-step4-live.py` + `PROBE-step4-output.txt`,
  `verify-step4-socket.py` + `PROBE-step4-socket-output.txt`

## Что сделано (шаг 4)

Дашборд аутентифицирует профиль, а не человека: cookie выдавала `session:operator` каждому
входу, а 18 REST-путей `web_routers/sessions.py` владельца не проверяли вовсе.

* `gateway/owner_identity.py` — `is_host_owner` / `identity_is_host_owner` /
  `is_host_operator`; при `acl.multi_user=true` грант даёт обход владельца только владельцу,
  при выключенном флаге поведение прежнее (контракт шага 3).
* `session_listing` / `session_history_mount` / `session_authority` / `session_prune` /
  `session_group_controls` — перевод на `is_host_operator`.
* `hermes_cli/web_routers/_session_acl.py` (новый, 89 стр.) — один шов: `session_scope`,
  `require_row` (чужой ряд = 404, не 403), `visible_row`, `refuse_writes`, `scoped_ids`.
* `hermes_acl_scope.request_owner_scope` — личность из проверенного гранта или логина
  дашборда; заголовок только при `identity_trust=trusted-proxy`; несогласные свидетельства → отказ.
* `chat_ws.socket_operator` — при МПД оператор сокета только владелец; чужой профиль по имени
  не выбирается. Сокет не отключается.
* `gateway/session_subject_scope.py` (новый, 82 стр.) — владелец ряда считается тем же кодом,
  что REST (`owner_stamps` + `row_outside_scope`); `SessionAuthority.authorize` проверял владельца
  только для `Platform.LOCAL`, поэтому мессенджерный маршрут открывался второму человеку по id.
  Отказ — `not_found`, тот же кадр, что для несуществующего id (не оракул существования).
* `session_listing._stored_closed` → `is_host_operator`.
* `tests/gateway/test_session_subject_scope.py` (новый) — 19 тестов: модуль скоупа (стемпы,
  fail-closed, живой источник vs холодный ряд), грант, листинг, прямая ручка, attach, веер
  событий, канонический локальный ряд, режим с выключенным флагом.
* `hermes_state_owner_scope.py` (новый) — общие scope-SQL правила; `user_id=` в
  `message_count`, `session_count_by_source`, `count/delete_empty_sessions`; пустой scope = `1 = 0`.
* `hermes_state_empty_hygiene.py` (новый) — `SessionEmptyHygieneMixin` вынесен из
  `hermes_state_sessions.py` (файл упирался в лимит 2000 строк).
* `tests/hermes_cli/test_session_route_owner_scope.py` (новый) — 33 теста: по тесту на путь,
  сокет и режим с выключенным флагом.

## Проверено

* `tests/hermes_cli/test_session_route_owner_scope.py` → 33 passed (16.95 s).
* `tests/gateway/test_session_subject_scope.py` → **19 passed**; на базе `7b9634c390`
  (без сокетного скоупа) → 13 failed / 6 passed: `session.info`/`resume`/`events.since`
  открывали чужой ряд, листинг A содержал чужое, модуля не существовало.
* Он же + `test_session_route_owner_scope` + `test_session_owner_scope` +
  `test_session_operator_scope` → **91 passed** (42.15 s).
* Смежные сокетные наборы (authority connection/principal/listing/reconnect/approval/cold route,
  listener lazy dashboard, ws-upgrade failure, detach end_reason, commands session scope) →
  **61 passed** на ветке и **61 passed** на базе, набор падений совпадает (пусто).
* Широкий смежный прогон (session api, list live hidden, list allowed sources, stored picker,
  stored reads + consistency, info, hygiene, route identity, authenticated identity) →
  **17 failed / 74 passed / 1 error и ровно те же 17+1 на базе** — падения предсуществующие.
* Смежные наборы (timeline, message-page owner, foreign sessions, dashboard ws auth,
  webapp ticket, mutation standalone, empty hygiene, scaffold notices) → 120 + 18 passed.
* Живая проба `verify-step4-live.py` на изолированном `HERMES_HOME` с `acl.multi_user: true`:
  A(alice) → сессия B(bob) — 404 на детали, сообщениях, таймлайне, странице сообщений,
  экспорте, снапшоте, наследнике; список/поиск/статистика/пустые — 200 без утечки; мутации
  чужого id 404 (удаление ничего не удалило, переименование не изменило); prune и
  owner-backfill → 403; свои пути 200; владелец видит `['owner-s1']`. ИТОГ: OK.
* Живая проба сокета `verify-step4-socket.py` на изолированном `HERMES_HOME` с
  `acl.multi_user: true`: alice=False/owner=True на гранте; по прямому id чужая сессия `not_found`
  на info/resume/events.since/mutate (своя — OK, ответ на чужой id совпадает с ответом на
  несуществующий); `session.list`/`active_list` у A — только своя строка, у владельца все 4;
  prune → permission_denied; событие B не дошло до сокета A, своё дошло, владелец получил все 4;
  владелец открыл все 4 строки. ИТОГ: OK. На базе `7b9634c390` проба падает: чужая открывалась.
* `python -m scripts.code_health --base 7b9634c390` → **0 blocking, 0 advisory** (одна `BLE001`
  разрешена комментарием с причиной).
* `scripts/check --only health` — новых находок нет (набор «файл+правило» — подмножество базы:
  вынос пустых сессий снял `FILE_LINES` с `hermes_state_sessions.py`).
* `scripts/check` целиком — те же 5 падающих проверок, что и на базе (состояние репозитория).

## Ограничения

* `manual_review`: живой браузер второго пользователя на сокете против запущенного шлюза с
  `acl.multi_user: true` — проверено на уровне `socket_operator`, `SessionAuthority.authorize`
  и `AuthorityConnection.dispatch` (настоящий веер событий), но не по TCP в браузере.
* Включение `acl.multi_user: true` — решение владельца, эта поставка ничего не включает.
* Тесты гоняются на копии дерева вне `~/.hermes` (`/tmp/mpd4/repo5`): worktree лежит внутри
  реального дома, `tests/home_io_guard.py` рубит инициализацию БД и на базе (те же 17+1).
* `pre-commit` пропущен при коммите: `scripts/hermes-python` в worktree падает на
  «no bootstrap Python found»; ruff по изменённым файлам прогнан напрямую — All checks passed.
* Шаги 5 (профили) и 6 (файлы) не трогались.

## История: шаг 3 (t_00544442)

Сделан в ветке `feature/mpd-step3-identity`, коммит `931a460a17`; отчёт —
`.worktrees/t_00544442/REPORT-step3.md` (в этой ветке — как база `787f022c9b`).

# PROGRESS — t_35fe5619 (исчерпан счётчик повторов: автопродолжение и авто-сплит)

## Состояние: РАБОТА ЗАВЕРШЕНА И ЗАКОММИЧЕНА. Ждёт выката на шлюз (карточка t_e5c9a281).

* Ветка: `wt/t_35fe5619`, worktree `.worktrees/t_35fe5619`
* База: `3bbf87774c`
* Верхушка ветки: цепочка `d8e2a4a634` → `d95cfff41b` → `48c1beb911` → коммит с этим PROGRESS.
  Точный tip бери командой `git -C .worktrees/t_35fe5619 rev-parse HEAD`; он же записан в handoff
  карточки (`kanban_complete.metadata.commit`). Хеш не печатается здесь, чтобы файл не устаревал
  от собственного amend'а.
* Модель/провайдер: `deepseek-v4-flash` / `letovo` (дефолт профиля, не подменялась)

### Что сделано

1. **`hermes_cli/kanban_auto_continue.py`** (564 стр.) — вся политика: `AutoContinueSettings`,
   `resolve_settings`, `ineligible_reason`, `plan_after_failure`/`plan_for_trip` (чистые функции),
   `apply_trip`, `read_state` (состояние только из `task_events`), `run_split_phase`, `pending_split_ids`.
2. **`hermes_cli/kanban_db_dispatch.py`** — `_record_task_failure` делегирует решение в
   `plan_for_trip`/`apply_trip`; в `_dispatch_once_locked` фаза `run_split_phase` до расчёта
   spawn-бюджета (при `dry_run` не выполняется); `DispatchResult.auto_split/auto_split_failed`.
3. **`hermes_cli/kanban_db_graph.py`** — `decompose_triage_task(source_statuses=..., allow_redecompose=...)`.
4. **`hermes_cli/kanban_decompose.py`** — `decompose_failed_card`, `_load_failed_task`,
   `_failed_card_prompt_fields`, `_stamp_progress_locator` (локатор дерева + `PROGRESS.md`).
5. **`hermes_cli/config_defaults.py`** — `kanban.auto_continue: True`, `auto_continue_max_splits: 2`.
6. **П.8 (failure_limit 3 vs DEFAULT 2)** — устранён: `_resolve_failure_limit` /
   `_kanban_failure_limit_setting` в `kanban_ops.py`, дефолт `--failure-limit` → `None` в `kanban_parser.py`.
7. **Замечание оркестратора о пуле ключей** — `agent/kanban_key_pool_errors.py` (`KeyPoolSlotBusy`,
   `KeyPoolWait`), `hermes_cli/turn_exit.py` → код 75 (requeue без провала); простой `KeyPoolError`
   остаётся `crashed`.

### Проверки (все выполнены)

* `tests/hermes_cli/test_kanban_auto_continue.py` — **15 passed**
* `tests/hermes_cli/test_kanban_cli_failure_limit_config.py` — **5 passed**
* `tests/gateway/test_kanban_result_exit.py` — **25 passed**
* `tests/hermes_cli/test_kanban_restart_interrupt_budget.py` — **7 passed** (обновлён + парный тест на `auto_continue: false`)
* `tests/hermes_cli/test_kanban_decompose.py` — **10 passed** (4 новых на `decompose_failed_card`)
* Широкий регресс 122 файла канбана: `1015 passed, 9 failed, 3 CRASHED` — **все 9 и 3 воспроизведены
  на базе `3bbf87774c` без правок** (окружение прогона; подробности в `REPORT-auto-continue.md` §5)
* `code_health --base HEAD` → 0 blocking, 0 advisory; `ruff` по изменённым файлам — чисто
  (71 предсуществующая находка в `agent/kanban_key_pool.py` идентична базе)
* Три живые пробы на реальной БД → `REPORT-auto-continue.md` §6

### Остаток человеку

`manual_review`: увидеть `auto_continued`/`auto_split` на **живом** прогоне после выката ветки
на шлюз. Это делает карточка выката `t_e5c9a281` (15-sre-qa), не эта.

### Инцидент по пути (не терять)

`refs/stash` общий на весь репозиторий: `git stash pop` в worktree притянул чужой WIP карточки
`t_afdf38ac`. Чужой патч сохранён в `stash@{1}`, ничего не потеряно.
**Урок: не пользоваться `git stash` в worktree при параллельных карточках;** для пробы базы —
`git archive <rev> | tar -x -C <tmp>` (так и делалось в этой попытке) или `git show HEAD:<file>`.

---

# PROGRESS — t_4911f964 (авто-сплит на холодном кэше под multiplex)

## Состояние: ГОТОВО — закоммичено `9dd90dc8cc` (правка+тесты), `16c2b7e34a` (отчёт+PROGRESS)

Набор канбана сверен с базой: 17 падений идентичны на базе и на правке (systemd scope
в среде отсутствует) — новых нет. code_health 0 blocking, ruff чист. Остаётся живая
проба Б — карточка `t_093fe731` (@15-sre-qa) + слияние оркестратором.

Ветка `wt/t_4911f964`, дерево `/home/hermes/.hermes/hermes-agent/.worktrees/t_4911f964`,
база `497c8986c3`.

### Дефект (воспроизведён, не по описанию)

Фаза сплита звалась из тика диспетчера без profile secret scope. Под
`gateway.multiplex_profiles: true` первый `get_secret` в холодном процессе даёт
`UnscopedSecretError`; `agent/auxiliary_client` ловит ровно этот класс и молча
откатывается на `os.environ`, где profile-scoped ключа нет. Итог:
`decompose_failed_card` → `ok=False` → `_mark_split_unavailable` → карточка
продолжается. Когда aux-клиент уже в `_client_cache`, секрет не перечитывается и
сплит проходит — отсюда «после рестарта мёртв, через минуты сам чинится».

Проба: (`…/cache/scratch/t4911/probe_scope.py`) cold/no-scope → FAIL
UnscopedSecretError, cold/with-scope → OK.

### Что сделано

1. `hermes_cli/kanban_profile_scope.py` (новый, ~45 стр.) — `default_profile_secret_scope()`,
   единственный владелец контекста (no-op вне multiplex).
2. `hermes_cli/kanban_auto_continue.py::run_split_phase` — обёрнут в
   `with default_profile_secret_scope(), _pin_first_board_resolution():`
   (шимы с late-import). Владелец aux-вызова — сама фаза ⇒ покрыты все вызывающие.
3. `gateway/kanban_watchers_dispatcher.py::_default_profile_secret_scope` — делегирован
   в тот же модуль (двух копий контекста нет).
4. `hermes_cli/kanban_specify.py` — `SECRET_SCOPE_FAILURE`, `_is_secret_scope_failure`,
   `aux_failure_reason`, `failure_class`; scope-дефект → `secret-scope: LLM error …`,
   прочие → прежний `LLM error: <Exc>`.
5. `kanban_auto_continue` — `_mark_split_unavailable` пишет `failure_class`;
   `AutoState.split_failure_class`; `plan_after_failure(..., split_failure_class=…)` →
   `decomposer_failure_class` в payload блока.
6. Семантика «декомпозер недоступен → карточка продолжается» сохранена.

### Тесты `tests/hermes_cli/test_kanban_split_secret_scope.py` (4)

1. холодный кэш + multiplex → тик даёт детей;
2. рестарт (сброс кэша) → первый же сплит даёт детей без прогрева;
3. `secret-scope:` в `detail` + `failure_class="secret_scope"` в событии/AutoState/payload;
4. настоящий отказ провайдера остаётся `LLM error: …`, класс пуст, карточка продолжается.

* Красное на прежнем коде (файлы откачены, модуль убран): **4 failed** —
  `…/t4911/red-final.txt` (первый тест: `failed=['t_…']` вместо детей).
* Зелёное после: **45 passed** — `…/t4911/green-final.txt`.
* `code_health --base HEAD` → 0 blocking, 0 advisory; ruff по 5 файлам — passed.

### Следующий шаг

1. `…/t4911/kanban-set.txt` — набор `tests/**/test_kanban*.py`; приложить хвост.
2. Коммит в `wt/t_4911f964`.
3. `REPORT-t4911-split-scope.md` + комментарий-самопроверка в карточку.
4. Живая проба Б (первый сплит сразу после рестарта) — карточка @15-sre-qa.

### Среда

* `scripts/run_tests.sh` в worktree падает («no bootstrap Python found»): гонять
  `HERMES_PYTHON=$(ls -t /home/hermes/.hermes/installs/*/test-environment/gen-*/venv/bin/python | head -1)`.
* Полный `tests/hermes_cli/` шумит падениями `test_agent_import_left_core.py`
  (guard на worktree-путь) — окружение, не регрессия; набор ограничен kanban-файлами.
* `scratch/` держать ВНЕ дерева карточки: `code_health` сканирует и его.
