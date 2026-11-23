# Д3 — провенанс сохранённых scratch-каталогов переживает retention и hard-delete

Карточка `t_e414a3cc`, ветка `wt/t_e414a3cc`, база `c22f1fdcad3`
(верхушка `feature/multiuser-access` `b55cbeb3c64` включена).
Основание: `VERDICT-t896b6548.md`, раздел «Д3» и пункт 4 раздела 6.

## Что было сломано

Единственным указателем на сохранённый scratch-каталог было событие
`workspace_cleanup_preserved_unsaved_work` в `task_events`. Это событие
удалялось штатной ретенцией (`hermes_cli/kanban_db.py:4195`, по умолчанию
30 дней) и при hard-delete архивной карточки. Каталог оставался на диске
навсегда и через месяц найти его было нечем: рекомендация «уберите вручную»
неисполнима ⇒ защита от потери работы превращалась в медленную утечку диска.

## Выбранный вариант и почему

Выбран **(а) + durable-реестр + учёт в `gc`** — предложенное расширение
предпочтительного варианта, укладывается в бюджет (см. «Размер»).

* **(а) исключение вида события из ретенции** — в `gc_events` добавлено
  `kind != ?` рядом с существующим исключением `decomposed`. Дешёвый слой,
  который сам по себе даёт выживание при retention.
* **durable-реестр** `kanban-preserved-workspaces.json` рядом с БД доски —
  обязательный слой: событие исчезает при hard-delete карточки, а файл рядом
  с `kanban.db` переживает и ретенцию, и удаление задачи. Только (а) условия
  приёмки не закрывает.
* **учёт в `hermes kanban gc`** — печатает абсолютные пути сохранённых
  каталогов (путь, task id, причина), а не только счётчик. Это и есть
  «оператор находит каталог командой, а не `du`-обходом».

**(в) отклонён**: отказ `delete_archived_task` ломает штатный жизненный цикл
карточки ради побочного артефакта и не решает retention.

Исключение из ретенции при этом **ограничено**: `gc` удаляет запись реестра
И событие, как только каталога на диске нет (оператор вернул работу и убрал
каталог — ровно то, что просит документация). Без этого (а) в чистом виде
копил бы события вечно.

## Изменения (локаторы)

1. **`hermes_cli/kanban_preserved_workspaces.py`** (новый, 232 строки) —
   реестр сохранённых каталогов:
   * `record(conn, task_id, path, repo, reason)` — идемпотентно по
     `(task, path)`: событие добавляется, только если у задачи ещё нет события
     с этим путём (**Д7**), запись реестра — upsert, не append;
   * `entries(conn)`, `prune_gone(conn)` — второй удаляет записи без каталога
     и их устаревшие события;
   * `registry_path(conn)` привязан к файлу БД **этого** соединения
     (`_main_db_file`), а не к ambient-пину `HERMES_KANBAN_BOARD`: реестр
     должен лежать рядом с той БД, чьи строки описывает;
   * лок `flock`/`msvcrt` на `<registry>.lock` с таймаутом 5 с (зависший
     держатель не должен вешать завершение воркера); запись атомарная
     (`os.replace`); битый/отсутствующий файл читается как пустой.
2. **`hermes_cli/kanban_db_workspace.py`**, `_preserve_scratch_workspace` —
   вместо прямой `_kb._append_event(...)` зовёт `kpw.record(...)`. Логика
   обхода каталогов (Д1+Д2) не тронута.
3. **`hermes_cli/kanban_db_retention.py`** (новый) — `gc_events`,
   `gc_worker_logs`, `_retention_seconds` вынесены из фасада `kanban_db.py`
   (вынос обязателен и по code-health: фасад на базе 4372 строки, любая
   правка в нём — блокирующий `FILE_LINES`). В `DELETE` добавлено
   `kind != kpw.PRESERVED_KIND`.
4. **`hermes_cli/kanban_db.py`** — блок ретенции удалён, три имени
   ре-экспортируются tail-import'ом (как остальные sibling'и пакета).
5. **`hermes_cli/kanban_ops.py`**, `_cmd_gc` + `_reap_preserved_workspaces` —
   после сводки печатаются пути; `prune_gone` вызывается до `entries`, поэтому
   исчезнувший каталог не рекламируется.
6. **`website/docs/user-guide/features/kanban.md`** — раздел про `scratch`
   описывает реестр и команду поиска вместо неисполнимого «уберите вручную».
7. **`tests/hermes_cli/test_kanban_preserved_provenance.py`** (новый, 6 тестов).

Retention чекпойнтов и уборка `worktree` не тронуты. Обход каталогов
(`_repo_has_unsaved_work`, `_scratch_workspace`) не менялся.

## Доказательства (красное → зелёное)

`evidence-t_e414a3cc/` (gitignored, `/evidence-*/`):

* `red-before.txt` — **6 failed** на чистом дереве базы
  (`git archive 3e1895729437` + только новый тестовый файл):
  ``` 
  FAILED ...::test_event_retention_keeps_preserved_workspace_provenance
  FAILED ...::test_hard_delete_keeps_preserved_workspace_findable
  FAILED ...::test_gc_prints_preserved_workspace_paths
  FAILED ...::test_gc_forgets_a_preserved_dir_the_operator_removed
  FAILED ...::test_gc_event_is_idempotent_on_repeated_runs
  FAILED ...::test_registry_survives_a_board_scoped_round_trip
  ==== 6 failed in 9.90s ====
  ```
  Каждый тест падает на своём утверждении приёмки: ретенция съедает событие;
  после hard-delete `gc` не называет путь; реестра нет; повторные прогоны
  добавляют события.
* `green-after.txt` — `6 tests passed, 0 failed`.
* `checks.txt` — `ruff check --config ruff.pre-commit.toml` → `All checks
  passed!`; `python -m scripts.code_health --base 3e1895729437` →
  `0 blocking, 0 advisory`.
* `wide-suite.txt` — широкий набор `tests/hermes_cli/test_kanban_*.py`
  (104 файла): **756 passed, 29 failed, 4 skipped**. Все 13 не-зелёных файлов
  падают **идентично на чистой базе** (проверено прогоном того же набора в
  `git archive`-дереве) — это предсуществующий артефакт окружения worktree:
  страж `tests/home_io_guard.py` ругается на путь
  `<worktree>/hermes-update-pull` (80 совпадений `TEST BUG: file I/O against
  the REAL hermes home` в полном прогоне, в том числе во всех 12 падениях
  `test_kanban_resume_guard.py`), плюс 4 файла с «all passed but pytest exited
  non-zero». Ни одного падения, связанного с правкой, нет: наборы «только на
  базе» и «только с правкой» пусты.
* `live-smoke.txt` — живой прогон через настоящий `hermes kanban gc` на
  изолированном `HERMES_HOME`:
  1. карточка завершена → архивирована → **hard-deleted**; `gc` печатает
     каталог с task id и причиной;
  2. ретенция: `events_before=5 removed=4 preserved_after=1` — соседнее
     событие `completed` съедено, провенанс на месте, каталог на диске;
  3. **Д7**: три дополнительных прогона `gc` → событий по-прежнему 1,
     записей реестра 2 (не 4/5/6);
  4. оператор удалил каталог → `gc` печатает только оставшийся, реестр
     содержит 1 запись, `SELECT` по сохранённым событиям → `[]`.

## Д7 (дополнительное, уложилось)

Стоимость — часть `record()` и `prune_gone()`, ~20 строк внутри нового
модуля: событие не дублируется по `(task, path)`, запись реестра — upsert.
Ограничением не стало.

## Ограничения и остаточные риски

* Реестр — файл рядом с `kanban.db`, не таблица: `rename`/`cp` БД без файла
  реестра (или наоборот) оставит провенанс только в событиях (которые
  ретенция теперь хранит). Чтение битого файла = пустой список, запись
  восстановится следующим сохранением.
* Лок реестра ограничен 5 с: при таймауте запись идёт без него (возможна
  потеря строки реестра при гонке двух воркеров, но не каталога — событие
  остаётся).
* Правка **не** меняет поведение `gc` для уже существующих установок: при
  первом прогоне реестр пуст, пути появятся только у новых сохранений. Каталоги,
  сохранённые до обновления, ретенция уже могла осиротить — это не
  восстанавливается и в отчёте названо.
* `merge` в целевую ветку — за оркестратором (карточка этого не делает).

## Как воспроизвести

```
PY=/home/hermes/.hermes/installs/019d0d114d22b7e6/test-environment/gen-8f22a0933fa3482c9b164508f3f0f9e2/venv/bin/python
HERMES_PYTHON=$PY scripts/run_tests.sh tests/hermes_cli/test_kanban_preserved_provenance.py
$PY -m scripts.code_health --base 3e1895729437
```
