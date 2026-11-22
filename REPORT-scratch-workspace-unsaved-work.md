# REPORT — t_78e323fe: scratch-workspace удалялся вместе с неотправленными коммитами

## Дефект и его корень

`hermes_cli/kanban_db_workspace._cleanup_workspace` делал `shutil.rmtree(wp, ignore_errors=True)`
для `workspace_kind=scratch` без всякой проверки содержимого. Защита существовала только для
`worktree` (`_cleanup_worktree_workspace`: чистое дерево + все коммиты достижимы из
remote-tracking ref, иначе `Preserving worktree ...`). Карточка, склонировавшая репозиторий в
свой scratch-workspace и сделавшая там коммиты, теряла их при закрытии — так погибли 5 коммитов
ветки `wt/t_3e4b45f4`.

Тот же безусловный `rmtree` стоял ещё в двух местах:
`_try_cleanup_parent_workspaces` (отложенная уборка родителя, когда последний ребёнок
завершился) и `kanban_ops._cmd_gc` (ночной `hermes kanban gc` для архивных карточек).
Правка только в `_cleanup_workspace` оставила бы два обходных пути — второй из них
(`gc`) реально воспроизведён красным тестом.

## Что изменено

| Файл | Изменение |
|---|---|
| `hermes_cli/kanban_db_workspace.py` | `_iter_scratch_repos`, `_child_dirs`, `_scratch_repo_unsaved_reason`, `_scratch_workspace_unsaved_reason`, `_preserve_scratch_workspace`; вызов в `_cleanup_workspace` и `_try_cleanup_parent_workspaces` |
| `hermes_cli/kanban_ops.py` | `_cmd_gc` уважает предикат, считает `preserved_ws`, печатает их число |
| `tests/hermes_cli/test_kanban_scratch_unsaved_work.py` | 14 тестов (новый файл) |
| `website/docs/user-guide/features/kanban.md` | описано исключение и событие `workspace_cleanup_preserved_unsaved_work` |
| `evidence/probe_scratch_guard.py`, `evidence/probe_scratch_guard.json` | живая проба через CLI + её вердикт |

### Семантика предиката

Репозитории ищутся в корне workspace и в подкаталогах (глубина ≤ 8, без
`node_modules`, `.venv`, `__pycache__`, `site-packages`, `.git` и т.п.). Найденный репозиторий
не разворачивается дальше: работа сабмодуля видна как изменённая запись в родителе.

Сохраняем каталог, если у любого найденного репозитория:

* `git status --porcelain` непуст — незакоммиченные изменения/untracked;
* нет ни одной remote-tracking ref — история вообще никуда не уезжала;
* `git log HEAD --branches --not --remotes` непуст — есть коммиты, недостижимые из remote;
* `git stash list` непуст;
* любая git-команда не выполнилась (таймаут, ошибка) — «не смог проверить» = сохранять;
* обход остановился на лимите глубины, а ниже ещё есть каталоги — тоже сохранять
  (`workspace tree too deep to verify`), потому что «не нашли» здесь не равно «нет».

Удаляем как раньше, если репозиториев нет вовсе (обычный случай — один `scandir`) или все
найденные чисты и полностью достижимы из remote.

`worktree` и `dir` не затронуты, retention событий/логов не затронут.

## Доказательства

### Тесты

```
tests/hermes_cli/test_kanban_scratch_unsaved_work.py        14 passed
+ test_kanban_gc_retention.py + test_kanban_swarm.py         33 passed
```

Покрыто: локальный непушенный коммит, незакоммиченные изменения, репозиторий без remote,
stash, вложенный (не в корне) репозиторий, чистый-запушенный → удаляется, коммит на
запушенной ветке → удаляется, workspace без репозитория → удаляется, вендорные каталоги не
сканируются, обрыв по лимиту глубины → сохраняется, архив, отложенная уборка родителя,
событие с путём, `gc` не удаляет сохранённое.

### Краснота (каждая ветка защиты отдельно)

| Снят guard | Результат |
|---|---|
| в `_cleanup_workspace` | 7 из 14 падают |
| в `_try_cleanup_parent_workspaces` | падает `test_deferred_parent_cleanup_preserves_unsaved_scratch_workspace` |
| в `_cmd_gc` | падает `test_gc_does_not_delete_a_preserved_workspace`; вывод `GC complete: 1 workspace(s) … removed` при уже сохранённом completion каталоге |

### Живая проба через реальный CLI

`evidence/probe_scratch_guard.py` — два прогона `hermes kanban` (create → claim → работа →
`complete --force`) на изолированных `HERMES_HOME` и `HERMES_KANBAN_HOME`:

* клон с локальным непушенным коммитом → `status=done`, каталог на месте, файл на месте,
  одно событие `workspace_cleanup_preserved_unsaved_work` с `reason=commits unreachable from
  any remote branch` и путём каталога;
* чистый полностью запушенный клон → `status=done`, каталога нет, событий нет.

Вердикт `pass` сохранён в `evidence/probe_scratch_guard.json`. На базовой версии файла проба
даёт `FAIL` (первый кейс: `workspace_exists: false`).

### Статика

* `python -m scripts.code_health --base beeb9623539` → 0 blocking, 0 advisory
  (один `# health: allow PS-P05` с обоснованием в пробе).
* `ruff check` по трём изменённым py-файлам → All checks passed. `py_compile` чисто.

### Что НЕ является регрессией

Эти падения воспроизведены на неизменённой базовой версии файла (подменой и обратно):
`test_kanban_worktree_teardown.py::test_tree_dirtied_between_check_and_removal_preserved`
(`import cli` → `home_io_guard` бьётся о реальный `~/.hermes/hermes-agent/.git/...` в
worktree-окружении), два spawn-теста в `test_kanban_db.py`, три теста в
`test_kanban_core_functionality.py`.

## Осадок после проб

Первые запуски пробы (до того как в ней появился `HERMES_KANBAN_HOME`) писали карточки в живой
борд. Две карточки (`t_786ba75e`, `t_b0b29c42`) и их каталоги удалены скриптом
`evidence/_cleanup_probe_residue.py`; проверка «probe cards remaining: 0». Все последующие
прогоны шли на изолированном `HERMES_KANBAN_HOME`.

## Остаточное

* Merge `wt/t_78e323fe` и рестарт шлюза — оркестратор, по условиям карточки.
* Уже потерянные 22.11 коммиты t_3e4b45f4 эта правка не восстанавливает — она предотвращает
  повторение.
