# t_96d65020 — Д1 + Д2: уборка scratch не врёт «чисто»; `boards rm --delete` под защитой

База: `b55cbeb3c64` (верхушка `feature/multiuser-access` на момент выдачи карточки).
Ветка: `wt/t_96d65020`. Дерево: `/home/hermes/.hermes/hermes-agent/.worktrees/t_96d65020`.
Основание: `/home/hermes/.hermes/kanban/workspaces/t_896b6548/VERDICT-t_896b6548.md`, разделы 3 (Д1–Д2), 6.

---

## Д1 — нечитаемый подкаталог = «не знаю», а не «чисто»

### Что было

`_child_dirs()` в `except OSError` возвращала `[]`. `_iter_scratch_repos()` читала пустой
список как «подкаталогов нет», `truncated` оставался `False`, `_scratch_workspace_unsaved_reason()`
возвращала `None` — то есть «чисто». Дальше `shutil.rmtree(..., ignore_errors=True)`:
всё читаемое снесено, нечитаемое осталось, события нет.

### Что сделано

| Локатор | Изменение |
|---|---|
| `hermes_cli/kanban_db_workspace.py:369` | `_child_dirs(path) -> tuple[list[Path], bool]` — второй элемент «был узел, который не удалось прочитать». `OSError` и на `os.scandir`, и на `entry.is_dir(follow_symlinks=False)` даёт частичный список + `True`. Докстринг перечисляет реальные причины: `chmod 000`, чужие права, отвалившийся FUSE/NFS. |
| `hermes_cli/kanban_db_workspace.py:394` | `_iter_scratch_repos(root) -> tuple[list[Path], Optional[str]]` — второй элемент это **причина**, по которой дерево не проверено, а не флаг. Нечитаемый узел → `"workspace tree not fully readable"`; лимит глубины → `"workspace tree too deep to verify"`. `None` = обойдено полностью. |
| `hermes_cli/kanban_db_workspace.py:530` | `_scratch_workspace_unsaved_reason()`: если ни в одном найденном репозитории нет несохранённого, но `unverified` непустой — возвращает `(wp, unverified)`. «Не смог посмотреть» доходит до вызывающего как причина сохранить. |

Смысл разделения: причина нужна оператору в событии и в отказе, поэтому
`Optional[str]`, а не `bool`.

### Красная база (Д1)

`durable/t_96d65020/evidence/red-base.txt` — прогон новых тестов на коде до правки:

```
FAILED tests/hermes_cli/test_kanban_scratch_unsaved_work.py::test_unreadable_subdirectory_preserves_whole_workspace
E   AssertionError: one workspace, one event
E   assert 0 == 1
FAILED tests/hermes_cli/test_kanban_scratch_unsaved_work.py::test_unreadable_workspace_root_preserves_workspace
E   assert 0 == 1
```

События нет, каталог снесён — ровно дефект из вердикта.

### Тесты

`tests/hermes_cli/test_kanban_scratch_unsaved_work.py`:

* `test_unreadable_subdirectory_preserves_whole_workspace` — `chmod 000` на **промежуточном**
  каталоге `ws/mid`, под ним репозиторий с незапушенным коммитом, рядом читаемый `notes.md`.
  Проверяется: каталог сохранён, **ровно одно** событие `workspace_cleanup_preserved_unsaved_work`,
  `payload["path"] == str(ws)`, причина содержит «readable», читаемый сосед цел по содержимому
  (это и отличает исправление от «каталог оставили, файлы снесли»). Права возвращаются в `finally`.
* `test_unreadable_workspace_root_preserves_workspace` — то же для нечитаемого корня workspace.

### Живой прогон (Д1)

`durable/t_96d65020/smoke_unreadable_dir.py` → `evidence/smoke-unreadable-dir.txt`:
изолированный `HERMES_HOME`/`HERMES_KANBAN_HOME`, реальный `complete_task`, реальный `chmod 000`:

```
Preserving scratch workspace for task t_d063c261: unsaved work (workspace tree not fully readable) in …/workspaces/t_d063c261
CHECK workspace kept: True
CHECK readable neighbour intact: True
CHECK unpushed file intact: True
CHECK exactly one preserved event: True
CHECK reason says it could not read: True
reason: workspace tree not fully readable
```

---

## Д2 — `boards rm --delete` под защитой

### Что было

На не-default борде scratch лежит в `boards/<slug>/workspaces/<id>/`, и
`remove_board(..., archive=False)` вызывал `shutil.rmtree(d)` без единой проверки. Ревьюер
воспроизвёл: защита при завершении сработала, событие записано — и `--delete` всё равно удалил
каталог, напечатав только `Board 'probeboard' deleted.`

### Что сделано

| Локатор | Изменение |
|---|---|
| `hermes_cli/kanban_db_boards.py:170` | `_board_scratch_workspaces(d) -> tuple[list[Path], bool]` — обход `d/"workspaces"`; нет корня → `([], False)`; любой `OSError` (в т.ч. на `iterdir`) → частичный список + `True`. |
| `hermes_cli/kanban_db_boards.py:196` | `_preserved_board_workspaces(slug, d) -> tuple[list[Path], list[Path]]` — по каждой задаче борды вызывает существующий `_scratch_workspace_unsaved_reason`; отдельно возвращает **сохранённые** (там реально есть что терять) и **непроверяемые** пути. |
| `hermes_cli/kanban_db_boards.py:222` | `remove_board(slug, *, archive=True, force=False)`. Перед `shutil.rmtree(d)`: если `preserved or unverified` и не `force` — `ValueError` с перечнем путей. При `force` — удаляет и кладёт пути в `result["preserved"]`. Путь к БД берётся как `d/"kanban.db"` (ровно тот каталог, который удаляем), а не через повторный резолв борды. |
| `hermes_cli/kanban_boards.py:100` | `_cmd_boards_rm`: `force_delete = args.delete or args.boards_action == "delete"` (у алиаса `boards delete` флага `--delete` в парсере нет — #23139); `force=args.force`; `ValueError` → `_err("kanban boards rm: …")`; при `--force` печатается список выброшенных путей. |
| `hermes_cli/kanban_parser.py:117` | `boards rm --force`; help `--delete` дополнен: hard delete отказывает, пока в workspace задачи есть git-работа, существующая только здесь. |
| `plugins/kanban/dashboard/plugin_api.py:1519` | `DELETE /boards/{slug}?force=…`; без `force` — HTTP 400 при сохранённой работе; `remove_board(..., force=force)`. |
| `website/docs/user-guide/features/kanban.md` | Поведение `--delete`/`--force` и «tree too deep **or unreadable** to verify». |

Проверка стоит на общем бэкенд-пути `remove_board`, поэтому закрывает CLI и дашборд сразу.
Путь `archive` (переименование) не тронут: дерево переезжает целиком, работа едет с ним.

### Красная база (Д2)

`red-base.txt`, те же 6 падений:

```
FAILED …::test_hard_delete_refuses_and_keeps_unsaved_scratch_work
E   Failed: DID NOT RAISE ValueError
FAILED …::test_hard_delete_with_force_reports_what_it_discards
FAILED …::test_force_on_a_clean_board_reports_nothing
FAILED …::test_unreadable_workspace_root_also_vetoes_the_delete
FAILED …::test_cli_reports_the_refusal
FAILED …::test_cli_force_deletes_and_lists_paths
```

### Тесты

`tests/hermes_cli/test_kanban_board_delete_guard.py` (новый файл, 8 тестов):

* `test_hard_delete_refuses_and_keeps_unsaved_scratch_work` — отказ, борда и файл целы, коммит на месте,
  событие `workspace_cleanup_preserved_unsaved_work` записано ровно одно;
* `test_hard_delete_with_force_reports_what_it_discards` — `--force` удаляет и возвращает пути в `result["preserved"]`;
* `test_hard_delete_of_a_clean_board_still_works` / `test_force_on_a_clean_board_reports_nothing` —
  обычное удаление чистой борды не сломалось и лишних ключей не отдаёт;
* `test_archive_rename_path_is_unchanged` — `archive` перевозит дерево вместе с незапушенным файлом;
* `test_unreadable_workspace_root_also_vetoes_the_delete` — «не смог посмотреть» тоже вето;
* `test_cli_reports_the_refusal` / `test_cli_force_deletes_and_lists_paths` — через реальное
  argparse-дерево и диспетчер CLI: отказ печатает «unsaved work» и `--force`, форс печатает путь.

### Живой прогон (Д2)

`durable/t_96d65020/smoke_boards_rm.py` → `evidence/smoke-boards-rm.txt`, отдельный
`HERMES_KANBAN_HOME`, настоящая борда, настоящий CLI (`python -m hermes_cli.main kanban …`):

```
$ hermes kanban boards rm smokeboard --delete  -> exit 1
STDERR: kanban boards rm: board 'smokeboard' holds scratch workspaces with unsaved work that exists nowhere else (…/boards/smokeboard/workspaces/t_180aca1e); recover them first, or re-run with --force to delete the board anyway
CHECK refused(rc!=0): True
CHECK board intact: True
CHECK file intact: True
CHECK event recorded: 1 ['created', 'workspace_cleanup_preserved_unsaved_work']
$ hermes kanban boards rm smokeboard --delete --force  -> exit 0
Deleted scratch workspaces with unsaved work (--force):
  …/boards/smokeboard/workspaces/t_180aca1e
Board 'smokeboard' deleted.
CHECK forced(rc==0): True
CHECK forced names the path: True
CHECK board gone: True
```

---

## Прогоны

| Файл | Результат |
|---|---|
| `evidence/red-base.txt` | 8 FAILED на коде до правки (красная база по всем новым тестам) |
| `evidence/green-focus.txt` | `test_kanban_scratch_unsaved_work.py` + `test_kanban_board_delete_guard.py` + `test_kanban_gc_retention.py` — **34 passed** |
| `evidence/green-full.txt` | те же + `test_kanban_boards.py` (соседний набор борд): **58 passed, 2 failed** — ровно те же два предсуществующих падения, что и на базе (`evidence/base-boards.txt`: 24 passed, те же 2 failed), наши правки их не добавляют |
| `evidence/green-neighbours.txt` | `tests/plugins/test_kanban_dashboard_plugin.py`, `test_kanban_cli.py`, `test_kanban_board_path_priority.py`, `test_kanban_board_project.py` — **67 passed** |
| `evidence/base-boards.txt` | база по `test_kanban_boards.py`: 2 предсуществующих падения (`TestWorkerSpawnEnv`, `test_per_board_task_isolation_via_cli`) — те же на патче, не наши |
| `evidence/green-round2.txt` | раунд 2: `scratch_unsaved_work` + `board_delete_guard` + `gc_retention` — **37 passed** |
| `evidence/red-base-pin-round2.txt` | раунд 2, красная база: `test_refusal_does_not_clear_the_active_board` и `test_cli_refusal_keeps_the_active_board` — **2 failed** на коде до правки (`assert 'default' == 'pinboard'`) |
| `evidence/red-base-dashboard.txt` | раунд 2, красная база: дашбордный `DELETE ...?delete=true` на `b55cbeb3c64` возвращает 200 и сносит каталог — **1 failed** |
| `evidence/smoke-unreadable-dir-round2.txt` | живой `chmod 000` на промежуточном каталоге: workspace сохранён, сосед цел, ровно одно событие, reason = `workspace tree not fully readable` |
| `evidence/smoke-boards-rm-round2.txt` | живой CLI: `current before: smokeboard` → `rm --delete` exit 1 → `current after refusal: smokeboard` (пин выжил) → `--delete --force` exit 0 со списком путей |
| `ruff check` | All checks passed по всем изменённым файлам; дерево целиком 79 находок = 79 на `b55cbeb3c64` (новых строк нет) |
| `code_health --base b55cbeb3c64` | **0 blocking, 0 advisory** (16 файлов) |


Команда прогона:

```bash
cd /home/hermes/.hermes/hermes-agent/.worktrees/t_96d65020
PYTHONPATH="$PWD:/home/hermes/.hermes/hermes-agent/.venv/lib/python3.14/site-packages" \
  /home/hermes/t72venv/bin/python -m pytest \
  tests/hermes_cli/test_kanban_scratch_unsaved_work.py \
  tests/hermes_cli/test_kanban_board_delete_guard.py \
  tests/hermes_cli/test_kanban_gc_retention.py -q -p no:cacheprovider
```

---

## Раунд 2 — правки по ревью

| Замечание ревью | Что сделано | Локатор |
|---|---|---|
| Отказ `rm --delete` снимал активную борду: `clear_current_board()` стоял до проверки | Обход `workspaces/` + `ValueError` перенесены **выше** любых мутаций (`clear_current_board`, `_INITIALIZED_PATHS.discard`); при `--force` результат тот же, что и раньше | `hermes_cli/kanban_db_boards.py:240-256` |
| Тесты на пин отсутствовали | 3 новых теста: отказ не меняет `get_current_board()` (backend и реальный CLI-диспетчер), успешное удаление по-прежнему откатывает пин на `default` | `tests/hermes_cli/test_kanban_board_delete_guard.py:200,216,228` |
| Смоук-скрипты валили гейт: PLW1510, RUF100, HX006 (git без `timeout=`) | `check=False, encoding="utf-8", errors="replace", timeout=` во всех `subprocess.run`, `# noqa: E402` убраны | `durable/t_96d65020/smoke_boards_rm.py:27,37`, `smoke_unreadable_dir.py:39` |
| `REPORT.md` заявлял «All checks passed» без оговорки | Таблица прогонов дополнена полной атрибуцией ruff (79 = 79) и `code_health` (0 blocking, 0 advisory) | этот файл, раздел «Прогоны» |
| Справочник CLI не описывал защиту | Строка `boards rm` в справочнике дополнена отказом при несохранённой/непроверяемой работе и флагом `--force` | `website/docs/reference/cli-commands.md:834` |
| chmod-000 тесты без `skipif(root)` | `pytest.mark.skipif(os.geteuid() == 0)` на трёх chmod-тестах (конвенция `tests/AGENTS.md`) | `test_kanban_scratch_unsaved_work.py:251`, `test_kanban_board_delete_guard.py:173` |
| Дашбордный путь не имел своего теста на отказ | Новый тест: `DELETE /boards/widget?delete=true` → 400 со списком путей, `&force=true` → 200 + `result.preserved` | `tests/plugins/test_kanban_board_lifecycle_api.py:87` |

Прогон через `durable/t_96d65020/run_pytest.py` (композитный интерпретатор: `.venv` даёт ruamel/fastapi,
`t72venv` — pytest; bridge строится в скрипте, чтобы общий checkout не попадал в командную строку).

Дополнительно зелёные: `tests/plugins/test_kanban_board_lifecycle_api.py` + `test_kanban_dashboard_plugin.py`
+ три целевых файла — **80 passed**; `test_kanban_cli.py`, `test_kanban_board_path_priority.py`,
`test_kanban_board_project.py`, `test_kanban_db_init.py` — **34 passed**.

---

## Границы

* Не тронуто: `worktree`-путь уборки, retention чекпойнтов, `_preserve_scratch_workspace` внутри
  (переиспользован как есть), пороги диспетчера.
* Д4–Д7 из вердикта в карточку не входили и не делались.
* Merge в `feature/multiuser-access` — за оркестратором. Ветка `wt/t_96d65020` только запушена.

## Локаторы для приёмки

* `hermes_cli/kanban_db_workspace.py:369, 394, 530`
* `hermes_cli/kanban_db_boards.py:170, 196, 222`
* `hermes_cli/kanban_boards.py:100`
* `hermes_cli/kanban_parser.py:117`
* `plugins/kanban/dashboard/plugin_api.py:1519`
* `tests/hermes_cli/test_kanban_scratch_unsaved_work.py` (2 новых теста)
* `tests/hermes_cli/test_kanban_board_delete_guard.py` (8 тестов)
* `website/docs/user-guide/features/kanban.md:135, 186`
* `durable/t_96d65020/` — отчёт, смоук-скрипты, `evidence/`
