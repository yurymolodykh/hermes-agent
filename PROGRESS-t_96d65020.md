# PROGRESS — t_96d65020: Д1 + Д2 (нечитаемый каталог = «не знаю»; `boards rm --delete` под защитой)

## Состояние: ГОТОВО, передано на ревью

* Дерево: `/home/hermes/.hermes/hermes-agent/.worktrees/t_96d65020`, ветка `wt/t_96d65020`
* База: `b55cbeb3c64`; верхушка: `79ef7c11c19`; запушена в `ours` (`git ls-remote ours refs/heads/wt/t_96d65020` совпадает)
* Дерево чистое (`git status --short` пуст)
* Отчёт: `durable/t_96d65020/REPORT.md`; доказательства: `durable/t_96d65020/evidence/`

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

## Следующий шаг

1. Ревьюеру: сверить дифф `b55cbeb3c64..wt/t_96d65020` по 7 файлам + 2 тестовым.
2. Merge в `feature/multiuser-access` — за оркестратором, не мной.
3. Д4–Д7 из вердикта в карточку не входили.

Примечание: `PROGRESS.md` в корне дерева принадлежит карточке `t_4cb8f83b`, которая делит этот
workspace; не перезаписывал.
