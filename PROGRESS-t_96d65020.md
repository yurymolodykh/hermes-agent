# PROGRESS — t_96d65020: Д1 + Д2 (нечитаемый каталог = «не знаю»; `boards rm --delete` под защитой)

## Состояние: раунд 2 — правки по ревью внесены и проверены. Остались коммит, push, handoff

* Дерево: `/home/hermes/.hermes/hermes-agent/.worktrees/t_96d65020`, ветка `wt/t_96d65020`
* База: `b55cbeb3c64`; верхушка предыдущего раунда: `aee1af815a1`
* Отчёт: `durable/t_96d65020/REPORT.md` (раздел «Раунд 2»); доказательства: `durable/t_96d65020/evidence/`
* Прогон тестов: `/home/hermes/t72venv/bin/python durable/t_96d65020/run_pytest.py <pytest args>`

## Сделано

### Д1 — `hermes_cli/kanban_db_workspace.py`

* `_child_dirs(path) -> tuple[list[Path], bool]` (~369): `OSError` на `os.scandir`
  или `entry.is_dir()` больше не превращается в `[]`, а отдаёт частичный список + `True`.
* `_iter_scratch_repos(root) -> tuple[list[Path], Optional[str]]` (~394): второй элемент —
  **причина**, по которой дерево не проверено: `"workspace tree not fully readable"` (нечитаемый
  узел) либо `"workspace tree too deep to verify"` (лимит глубины). `None` = обойдено полностью.
* `_scratch_workspace_unsaved_reason()` (~530): если несохранённого не нашли, но `unverified`
  непустой — возвращает `(wp, unverified)`, то есть «сохранить, потому что не смог проверить».

### Д2 — `hermes_cli/kanban_db_boards.py` + CLI + дашборд

* `_board_scratch_workspaces(d) -> tuple[list[Path], bool]` (~170) — обход `d/"workspaces"`,
  `OSError` → частичный список + `True`.
* `_preserved_board_workspaces(slug, d) -> tuple[list[Path], list[Path]]` (~196) — по каждой задаче
  борды зовёт существующий `_scratch_workspace_unsaved_reason`; возвращает отдельно реальные
  сохранённые и непроверяемые пути.
* `remove_board(slug, *, archive=True, force=False)` (~222) — перед `shutil.rmtree(d)`: непустой
  любой из списков и не `force` → `ValueError` с перечнем путей; при `force` → `result["preserved"]`.
  Путь к БД = `d/"kanban.db"` (ровно удаляемый каталог), а не повторный резолв борды.
* `hermes_cli/kanban_boards.py:100` — `force_delete = args.delete or args.boards_action == "delete"`
  (у алиаса `boards delete` флага `--delete` нет — #23139), `force=args.force`, `ValueError` →
  `_err("kanban boards rm: …")`, при форсе печать выброшенных путей.
* `hermes_cli/kanban_parser.py:117` — `boards rm --force`, help `--delete` дополнен.
* `plugins/kanban/dashboard/plugin_api.py:1519` — `DELETE /boards/{slug}?force=`, без форса HTTP 400.
* `website/docs/user-guide/features/kanban.md:135,186` — поведение `--delete`/`--force` и
  «too deep **or unreadable** to verify».

## Проверки (сняты, все в `evidence/`)

* `red-base.txt` — 8 FAILED на коде до правки (2 новых теста Д1 + 6 Д2), в т.ч. `DID NOT RAISE ValueError`.
* `green-focus.txt` — 34 passed (scratch + delete-guard + gc-retention).
* `green-full.txt` — +`test_kanban_boards.py`: 58 passed, 2 failed (предсуществующие).
* `base-boards.txt` — база того же файла: те же 2 failed ⇒ не наши.
* `green-neighbours.txt` — дашборд-плагин, cli, board-path-priority, board-project: 67 passed.
* `smoke-boards-rm.txt` — живой CLI: отказ exit 1 с путём и `--force`, форс exit 0 с печатью пути.
* `smoke-unreadable-dir.txt` — живой `complete_task` с `chmod 000` на промежуточном каталоге.
* `ruff check` — All checks passed.

## Раунд 2 — правки по ревью

1. `hermes_cli/kanban_db_boards.py:240-256` — обход `workspaces/` и `ValueError` перенесены **выше**
   `clear_current_board()` / `_INITIALIZED_PATHS.discard()`: отказ больше не снимает пин активной борды.
2. `tests/hermes_cli/test_kanban_board_delete_guard.py:200,216,228` — 3 новых теста на пин
   (backend-отказ, CLI-отказ, успешное удаление по-прежнему откатывает на `default`);
   `skipif(geteuid()==0)` на chmod-тесте (`:173`).
3. `tests/hermes_cli/test_kanban_scratch_unsaved_work.py:251` — `skipif(geteuid()==0)` на двух chmod-тестах.
4. `tests/plugins/test_kanban_board_lifecycle_api.py:87` — новый тест дашбордного DELETE:
   `?delete=true` → 400 со списком путей, `&force=true` → 200 + `result.preserved`.
5. `durable/t_96d65020/smoke_*.py` — `check=False, encoding, errors, timeout=` во всех `subprocess.run`,
   `# noqa: E402` убраны; добавлен `run_pytest.py` (композитный интерпретатор).
6. `website/docs/reference/cli-commands.md:834` — строка `boards rm` дополнена защитой и `--force`.
7. `durable/t_96d65020/REPORT.md` — раздел «Раунд 2» + полная атрибуция ruff (79 = 79 на базе) и code_health.

## Проверки раунда 2

* целевые 3 файла: **37 passed** (`evidence/green-round2.txt`)
* + `test_kanban_board_lifecycle_api.py` и `test_kanban_dashboard_plugin.py`: **80 passed**
* соседние (`test_kanban_cli`, `board_path_priority`, `board_project`, `db_init`): **34 passed**
* красная база: 2 теста пина падают на `aee1af815a1` (`evidence/red-base-pin-round2.txt`),
  дашбордный тест падает на `b55cbeb3c64` (`evidence/red-base-dashboard.txt`)
* `ruff check` — All checks passed; по дереву 79 находок = 79 на базе; `code_health --base b55cbeb3c64`
  — **0 blocking, 0 advisory**
* живые смоуки: `evidence/smoke-unreadable-dir-round2.txt`, `evidence/smoke-boards-rm-round2.txt`
  (второй показывает `current before: smokeboard` → `current after refusal: smokeboard`)

## Следующий шаг

1. `git add -A && git commit` (t_96d65020), `git push ours wt/t_96d65020`.
2. `kanban_comment` — самопроверка раунда 2; `kanban_complete` с artifacts:
   `durable/t_96d65020/REPORT.md`, `durable/t_96d65020/evidence/`.
3. Merge в `feature/multiuser-access` — за оркестратором, не мной.
4. Д4–Д7 из вердикта в карточку не входили.

Примечание: `PROGRESS.md` в корне дерева принадлежит карточке `t_4cb8f83b`, которая делит этот
workspace; не перезаписывал.
