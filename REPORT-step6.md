# Шаг 6 МПД — результаты: файлы, скиллы, память, вложения карточек (Т6)

Карточка: `t_f3d28e37`. Эпик: `t_e963ba56`. Родитель: шаг 5 (`t_a08e47a4`).
Ветка: `wt/t_f3d28e37`, worktree `.worktrees/t_f3d28e37`.
База: `9b2e01a4f5` (merges шага 5 в ветку карточки).

## Цель и что было дырой

Шаги 3–5 дали каждому человеку личность, свои сессии и свой оркестратор. Поверхности «покажи
мне Вот этот путь / этот навык / эту память / это вложение» при этом отвечали за ВЕСЬ хост:

* `/api/files*` и `/api/fs/*` резолвили любой путь, который мог прочитать процесс
  (`GET /api/files?path=/home/someone-else` листал чужой дом; `..` отвергался только
  синтаксически, никогда после резолва).
* `/api/skills`, `/api/skills/content` и `/api/memory` при отсутствии `?profile=` падали в
  ПРОФИЛЬ ЗАПУСКА — на общем хосте в профиль владельца.
* Вложение карточки скачивалось, листалось и УДАЛЯЛОСЬ по своему целочисленному id без
  проверки владельца: подписанный сосед просто шёл по последовательности.

## Что сделано

### `hermes_acl_results.py` (новый, 400 стр.) — одна точка политики

| Функция | Правило |
|---|---|
| `scoped(request)` | `acl.multi_user` выключен или вызывающий — владелец хоста → `False` (прежнее поведение) |
| `file_roots(request)` | `None` = unscoped; иначе кортеж РЕАЛЬНЫХ путей: свои профили из реестра шага 2 + `kanban/workspaces/<id>` и `kanban/attachments/<id>` **своих** карточек. Пустой кортеж = fail closed |
| `path_allowed` / `require_path` | содержательность решается по `resolve()` — поэтому `..` и симлинк наружу отвергаются (403 `Path is outside the caller's allowed roots`) |
| `resolve_profile(request, name)` | без `?profile=` → свой профиль (а не профиль запуска); чужое имя → 404 шага 5 (нет оракула перебора) |
| `memory_dirs_for(request, all_dirs)` | scoped → только собственный бакет `u/<subject>`; чужой и легаси-базовый не считаются и не стираются |
| `task_visible` / `require_task` | карточка своя, если `created_by`/`assignee` несёт штамп субъекта; чужое → 404, идентичное отсутствующему id |
| `_multi_user()` | читает флаг БЕЗ создания `~/.hermes` (см. «Найденный дефект») |

Чтение доски для корней карточек: `_owned_task_rows()` — один пассивный `SELECT id, created_by,
assignee FROM tasks`; недоступная/битая доска = нет карточных корней (границу не расширяет).

### Ручки

| Ручка | Шов |
|---|---|
| `GET /api/files`, `/api/files/read`, `mkdir`, `delete`, `upload`, `download` | `_resolve_managed_path` → `require_path(request, resolved)` — один шов на все, наследующие границу |
| `GET /api/fs/read-text`, `/api/fs/list`, `/api/fs/read-data-url`, `/api/fs/download` | `_hosted_fs_read_guard(target, request)` |
| `POST /api/fs/write-text` | `require_path` ДО записи байтов (иначе соседу клался файл) |
| `GET /api/fs/git-root` | проверка и стартовой точки, и найденного корня (обход вверх выше названной папки) |
| `GET /api/files` default-cwd | `_caller_default` — браузер открывается в СВОЁМ корне, не в `Path.home()` процесса |
| `GET /api/skills`, `/content`, `POST/PUT /api/skills*`, `/api/skills/hub/*` | `results_profile` + `require_results_write` (shared видим и используем, перенастраивает только владелец) |
| `GET /api/memory`, `PUT /api/memory/provider`, `POST /api/memory/reset` | `results_profile` + `memory_dirs_for` (+ `require_results_write` на записи) |
| `GET /api/plugins/kanban/tasks/{id}/attachments`, `POST …/attachments`, `GET/DELETE /api/plugins/kanban/attachments/{id}` | `_require_owned_task` — владелец решается по КАРТОЧКЕ вложения, не по id файла |

С `acl.multi_user=false` (поставочный дефолт) все швы — no-op; положительные случаи это пинят.

### Найденный и исправленный дефект (собственный, в ходе шага)

Первая редакция `_multi_user()` шла через `multi_user_enabled()` → `load_config()`, а тот
**создаёт скелет `~/.hermes`** как побочный эффект. То есть простое листание каталога
материализовало домашний каталог оператора. Обнаружено падением
`test_web_server_files.py::test_directory_listing_skips_vanished_regular_file` (в листинге
появлялся лишний `.hermes`). Исправлено: флаг читается только если домашний каталог уже есть
(нет каталога — нет `config.yaml` — нет флага), и это запинено новым тестом
`test_reading_the_flag_never_creates_a_hermes_home`.

## Критерии приёмки — ручка → тест → проба → результат

| № | Критерий | Тест (`tests/hermes_cli/test_results_route_scope.py`) | Живая проба (`verify-step6-live.py`) | Результат |
|---|---|---|---|---|
| 1 | Тесты зелёные, число названо | **79 passed** (13.99 s), по классу на каждую группу ручек | — | ✔ |
| 2 | Чужой файл по прямому URL → отказ | `TestFsRoutes::test_a_neighbours_file_is_refused`, `TestManagedFilesRoutes` | `GET /api/fs/read-text чужой файл` → **403**, тело `Path is outside the caller's allowed roots`; `/api/files/read` → **403** | ✔ |
| 2 | Обход `..` → отказ | `TestFsRoutes::test_traversal_out_of_the_root_is_refused`, `TestPathContainment::test_traversal_out_of_the_root_is_refused`, `…a_symlink_pointing_out_is_refused` | `/api/fs/read-text обход ..` → **403** (резолв, не синтаксис) | ✔ |
| 3 | Вложение чужой карточки по id → отказ | `TestKanbanAttachmentRoutes` (7 тестов) | `GET /attachments/{чужой id}` → **404** `{"detail":"attachment not found"}`; список → **404** `{"detail":"Task not found"}`; `DELETE` → **404**, чужой блоб уцелел | ✔ |
| 4 | Свои файлы читаются как раньше | `TestFsRoutes::test_own_file_reads`, `…test_own_directory_lists`, `…test_writing_an_own_file_works`, `TestSkillsRoutes`, `TestMemoryRoutes` | свой каталог **200**, свой навык виден, свой бакет **13 == 13**, сброс стирает только свой | ✔ |
| 4 | До/после по ЧИСЛУ доступных файлов | — | `verify-step6-counts.py`: **owner 20/10 до и после; alice 10 своих, 0 чужих, 0 в корне** (на базе alice получала 20 и 10 чужих) | ✔ |
| 5 | Windows-ограничение шима | — | не проверено на Windows-хосте → `manual_review` (см. ниже) | ⚠ |

Живая проба: **ИТОГ 25/25** (`PROBE-step6-output.txt`), exit 0.

## База красная (та же проба на базе)

`verify-step6-baseline-red.py` (копия `verify-step6-live.py`, положенная в дерево базы) на
`9b2e01a4f5`: **9/25**, 16 LEAK, exit 1 —
в т.ч. чужой файл 200 (содержимое «bob only» отдано), запись чужого файла 200 (`planted.txt`
создан), `/api/skills?profile=01-bob` 200 со списком чужого навыка, память считает 0 == 13,
сброс стирает оба бакета, вложение чужой карточки 200 и DELETE 200 (блоб удалён).

## Смежные наборы — ветка против базы

| Наборы | Ветка | База |
|---|---|---|
| `test_results_route_scope.py` (новый) | **79 passed** | не собирается (`ModuleNotFoundError: hermes_acl_results`) |
| kanban attachments + web_server_files/fs + skills_profiles + unknown_profile_404 + webapp_uploads + memory_user_dir + skill_editor + plugin_api_import_isolation | **2 failed / 180 passed / 10 skipped** | **2 failed / 180 passed / 10 skipped** |

Оба падения идентичны и предсуществующие на базе:
`test_web_server_files.py::test_download_resolves_paths_in_the_originating_profile_session[/api/fs/download]`
и `[/api/fs/read-data-url]` (503 «Session store is not initialized» — тест не поднимает store).
Падений не добавлено.

## Гигиена кода

* `python -m scripts.code_health --base 9b2e01a4f5` → **0 blocking, 0 advisory** (10 файлов).
* `ruff check` по 8 изменённым/новым файлам → **All checks passed**.
* `scripts/boundary_lint.py --check` — падает ровно как база (те же 6 предсуществующих
  кроссов `plugins/dashboard_auth/basic`, они не мои). Единственная моя дельта — одна запись в
  `scripts/boundary_lint_baseline.json`: `plugins/kanban/dashboard/plugin_api.py
  hermes_acl_results.require_task: 1`. Это осознанный кросс: плагин kanban обязан спрашивать
  политику доступа, а `ctx`-метода для «чей это результат» в опубликованном plugin API нет;
  заводить его отдельным PR — вне объёма шага.

## Ограничения

* `manual_review`: **Windows-ограничение шима не проверено** — проба и тесты выполнены на Linux.
  Содержательность путей на Windows (`C:\`, UNC, `%5c`) опирается на существующий
  `web_server_files` (он это уже разбирает), но шаг 6 на Windows-хосте не прогонялся;
  за проверенное не выдаю.
* `manual_review`: два живых браузера против реально запущенного шлюза с `acl.multi_user: true`
  — проверено на уровне роутеров/политики и живых HTTP-кодов через `TestClient`, не по TCP.
* Включение `acl.multi_user: true` — решение владельца; поставка ничего не включает.
* Тесты и пробы гоняются на копии дерева вне `~/.hermes` (`/tmp/mpd6/branch3`): worktree внутри
  реального дома, `tests/home_io_guard.py` рубит инициализацию БД. Проба берёт `HERMES_HOME` в
  `/tmp`, иначе `get_default_hermes_root` вернёт реальный `~/.hermes`.
* `pre-commit` пропущен: `scripts/hermes-python` в worktree падает («no bootstrap Python
  found»); ruff и code health прогнаны напрямую.
* Соседние шаги не тронуты: идентичность и сессии (шаги 3–4) не менялись, данные не
  переносились, общий каталог `~/.hermes/hermes-agent` не изменялся.

## Воспроизведение

```bash
# копия дерева вне ~/.hermes
mkdir -p /tmp/mpd6/branch3 && cd <worktree> && \
  tar -c --exclude=.git --exclude=.venv --exclude=__pycache__ -f - . | tar -x -f - -C /tmp/mpd6/branch3
PY=/home/hermes/.hermes/installs/1a23eac39a17788f/test-environment/gen-5ac62ab36b984c04812d85ab1fc76459/venv/bin/python3

cd /tmp/mpd6/branch3 && HERMES_HOME=$(mktemp -d /tmp/mpd6t.XXXX) \
  $PY -m pytest -q tests/hermes_cli/test_results_route_scope.py     # 79 passed

cd /tmp/mpd6/branch3 && HERMES_DASHBOARD_SESSION_TOKEN=x $PY verify-step6-live.py      # 25/25
cd /tmp/mpd6/branch3 && $PY verify-step6-counts.py                                    # owner 20/10

# база красная: та же проба в дереве базы
cp /tmp/mpd6/branch3/verify-step6-live.py /tmp/mpd6/base/verify-step6-baseline-red.py
cd /tmp/mpd6/base && HERMES_DASHBOARD_SESSION_TOKEN=x $PY verify-step6-baseline-red.py  # 9/25, 16 LEAK

cd <worktree> && $PY -m scripts.code_health --base 9b2e01a4f5    # 0 blocking
```
