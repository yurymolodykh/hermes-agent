# t_e8d5daf8 — P2: негерметичный тест жизненного цикла доски

Ветка `wt/t_e8d5daf8`, база — верхушка `feature/multiuser-access` (`3f1bc134b05`, содержит `c22f1fdcad3`).
Изменён только файл теста; продуктовый код (`kanban_db_workspace.py`, `kanban_db_boards.py`) не тронут.

## 1. Дефект и правка

Было (`tests/plugins/test_kanban_board_lifecycle_api.py:97-100`): три `subprocess.run(["git", ...])`
без `cwd`, из-за чего `git config user.email/user.name` писали в `.git/config` **того** репозитория,
откуда запущен pytest. Две последующие команды (`add`, `commit`) уже имели `cwd=repo`.

Стало (там же, `:89-104`):

- `_git(repo, *args)` — единственная точка запуска git, `cwd=repo` у каждой команды.
- `_seed_scratch_clone(repo)` — вся подготовка (init, два config, файл, add, commit) идёт через `_git`,
  внешнее рабочее состояние не читается вовсе.

Циклы с `("init", "-b", "main", str(repo))` и раздельные `cwd`-ветки удалены: один хелпер вместо
смеси «путь в аргументе» и «cwd».

## 2. Страж против побочного эффекта

`test_seeding_the_scratch_clone_never_writes_into_the_enclosing_repository` (`:107-135`) —
не зависит от внешнего состояния, строит всё в `tmp_path`:

1. создаёт внешний репозиторий с честной identity (`honest-dev@example.org` / `Honest Dev`);
2. `monkeypatch.chdir(outer)` — ровно тот cwd, который наследовал уязвимый вариант;
3. вызывает `_seed_scratch_clone(...)` и требует, чтобы `git config --local --get-regexp ^user\.`
   внешнего репозитория не изменился;
4. повторяет вызов из каталога без репозитория (сценарий прогона из архива) — падение `exit 128`
   больше невозможно, а identity нового клона именно `t@example.com` / `t`.

Страж доказан красным на прежней форме кода: `ablation-prefix-shape.log` — восстановлен точный
допрежный вид (init с путём, два config без cwd), результат `2 failed, 4 passed`, диагноз
`User.Email t@example.com` вместо `honest-dev@example.org`. В рабочей версии — `6 passed`.

## 3. Прогоны (артефакты в этом каталоге)

| Прогон | Лог | Результат |
|---|---|---|
| из git-дерева (`wt/t_e8d5daf8`, cwd = корень worktree) | `run-inside-git-tree.txt` | `6 passed` |
| из копии без `.git` (`git archive HEAD` → scratch, `git rev-parse --show-toplevel` → `fatal: не найден git репозиторий`) | `run-outside-git-tree.txt` | `6 passed` |
| аварийный контроль: прежняя форма кода | `ablation-prefix-shape.log` | `2 failed, 4 passed` |
| аварийный контроль: страж без обвязки | `ablation-guard-only.log` | `1 failed` (страж ловит) |

Окружение прогонов: `HERMES_HOME` = отдельный пустой каталог, `TZ=UTC LANG=C.UTF-8 PYTHONHASHSEED=0`,
kanban-переменные сняты, интерпретатор — `.venv` чекаута (pytest 9.2.0). Для копии без `.git`
`PYTHONPATH` указывал на копию; проверено, что `hermes_cli` импортировался из неё.

## 4. Identity объемлющего репозитория

`git --git-dir=<platform>/.git config --local --get-regexp '^user\.'` до и после всех прогонов:
`exit=1` (ничего не задано) — `enclosing-identity-before.txt`, `enclosing-identity-after.txt`.
Секции `[user]` в `.git/config` нет. Ничего не восстанавливал и не задавал.

## 5. Просмотр остальных тестов на тот же шаблон

AST-проход по всем `tests/**/*.py` (и `plugins/**/*.py`) на `subprocess.*(["git", ...])` без `cwd`
и без `-C`/`--git-dir`:

- **Пишущие `git config` без `cwd`/`--file`/`--global`: 2** — оба в
  `tests/scripts/test_stable_release.py:471-472`. Ложное срабатывание: функция `:465-467` начинается
  с `monkeypatch.chdir(repo)`, т.е. cwd явно перенесён в `tmp_path`. Герметично, не правил.
- **`git init` без пути: ~70 вызовов** — все с `cwd=` в аргументах вызова либо внутри функции,
  где `monkeypatch.chdir` уже выполнен (единственный такой случай — `test_stable_release.py:469-470`,
  `chdir` на строке 467). Ни один не пишет в объемлющий репозиторий.
- **Запись в `HOME`**: `tests/pm/**`, часть `tests/gateway/**` делают `monkeypatch.setenv("HOME", tmp_path)` —
  это изоляция от настоящего `~/.hermes`, а не утечка; `tests/ci/**` и `tests/scripts/test_source_build_env.py`
  выставляют `GIT_CONFIG_GLOBAL=os.devnull`/`tmp_path` + `GIT_CONFIG_NOSYSTEM=1` — тоже защита.
  Ничего не менял.
- Итог: реальный нарушитель был один — целевой файл. Правка не расширена.

## 6. Линт

`ruff check --config ruff.pre-commit.toml tests/plugins/test_kanban_board_lifecycle_api.py` →
`All checks passed!` (`ruff.txt`, rc=0; предупреждения о preview-селекторах относятся к конфигу, не к файлу).

## 7. Остаточные риски

- Страж проверяет только `--local` объемлющего репозитория (`tmp_path/outer`), а не глобальный
  `.gitconfig`: тест не пишет туда по конструкции (`cwd=repo` у всех команд), а чтение/запись
  настоящего `$HOME` в тестах запрещена политикой репозитория.
- Identity общего дерева остаётся «локально не задано»; 16 старых коммитов с `t@example.com`
  не переписывались — вне объёма карточки.
