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

1. **`hermes_cli/kanban_preserved_workspaces.py`** (новый, 261 строка) —
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
7. **`tests/hermes_cli/test_kanban_preserved_provenance.py`** (новый, 231 строка, 6 тестов).

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
* `wide-suite-baseline.txt` + `wide-suite-delta.txt` — **решающее сравнение**:
  тот же набор, прогнанный на чистом `git archive 3e1895729437` (без моей
  правки, но с новым тестовым файлом) даёт
  `103 files, 750 passed, 29 failed, 4 skipped; FAILED: 13 of 103 files`
  — те же 29 падений в тех же 13 файлах. С правкой:
  `104 files, 756 passed, 29 failed, 4 skipped; FAILED: 13 of 104 files`.
  Дельта ровно `+1 файл`, `+6 тестов пройдено`, `0 новых падений`.
  Падения предсуществующие: страж `tests/home_io_guard.py` ругается на путь
  `<worktree>/hermes-update-pull` (80 совпадений `TEST BUG: file I/O against
  the REAL hermes home`, все 12 падений `test_kanban_resume_guard.py` — из них),
  плюс 4 файла «all passed but pytest exited non-zero». Ни одного падения,
  связанного с правкой, нет.
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

---

# Раунд 2 — исправление двух дефектов ревью round 1

Ревьюер (комментарий 643, вложение `review-round1-t_e414a3cc.md`) принял
четыре заявленных сценария, но нашёл два материальных дефекта в ядре
требования «провенанс переживает и retention, и hard-delete». Оба исправлены.

## Д3.1 — временный отказ stat больше не читается как «каталога нет»

`prune_gone()` считал каталог исчезшим по `not Path(path).is_dir()`, а `is_dir()`
даёт `False` и при недоступном родителе (отмонтированный том, `EACCES`, сбой
NFS). Тогда удалялись запись реестра **и** освобождённое от ретенции событие —
при живом каталоге на диске.

Новый предикат `kanban_preserved_workspaces.directory_is_gone()` (источник
истины один, используется и в `prune_gone`, и в `adopt`):

* `os.lstat` → `FileNotFoundError` = кандидат на «нет»;
* любой другой `OSError` (`EACCES`/`EIO`/`ENOTDIR` на предке) = «не знаю»,
  запись и событие живут;
* `lstat` успешен (каталог, файл, висячий симлинк) = «на месте»;
* дополнительно листингуется родитель: путь, которого нет в нечитаемом
  родителе, — тоже «не знаю».

Это то же правило, что уже принято в Д1+Д2: нечитаемое = «не знаю», а не «чисто».
Заголовок вывода `gc` сменён на «Registered preserved scratch workspaces» —
запись с нечитаемым родителем печатается, и утверждать «на диске» было бы догадкой.

## Д3.2 — каталоги, сохранённые до обновления, переживают hard-delete

`record()` вызывался только из `_preserve_scratch_workspace`, поэтому у карточек,
сохранённых до реестра, единственный указатель — событие, а `delete_archived_task`
его стирал.

Новый `kanban_preserved_workspaces.adopt(conn, task_id=None)` переносит
preserved-события в реестр (переиспользует `record()`, идемпотентно по
`(task, path)`) и вызывается в двух точках:

* `_register_preserved_workspaces()` в `delete_archived_task` **и** `delete_task` —
  до удаления строк задачи, чтобы hard-delete физически не мог стереть указатель;
  ошибка bookkeeping'а не блокирует удаление (каталог на диске, событие ещё есть);
* `kanban_ops._reap_preserved_workspaces()` — первым шагом `gc`, чтобы
  существующая доска мигрировала на первом прогоне, не дожидаясь удаления.

## Проверено на живой доске (read-only копия)

`live-board-adoption.txt` — копия `/home/hermes/.hermes/kanban.db` (backup API,
10.6 МБ), прогон только шага adopt: `adopted=2`, реестр создан с обеими
записями — ровно те две карточки (`t_af262aaf` archived, `t_2c42e5ad` done),
которые ревьюер нашёл вручную. Сам `gc` на копии не запускался: он делает
`rmtree` по путям в `/home/hermes/.hermes/kanban/workspaces/`, то есть удалял
бы реальные каталоги живой установки.

## Доказательства раунда 2

* `red-before-round2.txt` — **3 failed, 6 passed** на чистом `git archive
  b2f959e8ffe` + новый тест-файл (каждый новый тест красный по своему дефекту).
* `green-round2.txt` — 4 файла (`preserved_provenance`, `gc_retention`,
  `scratch_unsaved_work`, `db`): **89 passed, 0 failed, 1 skipped**.
* `live-smoke-round2.txt` — живой прогон на изолированном `HERMES_HOME`:
  * S1 legacy-событие → hard-delete → `registry entries: 1`, событий 0,
    каталог на диске, `gc` печатает путь;
  * S2 legacy-событие на done-карточке → `gc` записал его в реестр;
  * S3 нечитаемый родитель → `entries=3 events=1` до и после, файл цел;
    родитель снова читаем → запись жива, не зачищена;
  * S4 оператор удалил каталог → `entries=2 events=0` (запись и событие забыты).
* `wide-suite-round2.txt` — 104 файла, 759 passed, 29 failed, 4 skipped;
  13 файлов с падениями — **тот же самый набор**, что на базе (`new in round2: []`,
  сравнение списком файлов, а не на глаз). Причина — `tests/home_io_guard.py`
  на путях `<worktree>/.git/worktrees/...` и отсутствие user-D-Bus; сохраняется
  как `manual_review`.
* ruff — `All checks passed!`; `code_health --base 3e1895729437` — 0 blocking / 0 advisory.

## Новые тесты (раунд 2, 3 шт.)

* `test_unreadable_parent_keeps_registry_entry_and_event` — `chmod 0o000` на
  родителе: прогон `gc` не теряет ни запись, ни событие; после возврата прав
  запись снова «живая».
* `test_hard_delete_adopts_a_pre_registry_preserved_event` — legacy-событие,
  `delete_archived_task`, затем `gc` называет путь.
* `test_gc_adopts_a_pre_registry_preserved_event` — существующая доска
  мигрирует на первом же `gc`.

## Финальные проверки раунда 2 (после коммита `e10451c1965`)

* `tests/hermes_cli/test_kanban_preserved_provenance.py` — **9 passed** (1 файл).
  `preserved_provenance` + `gc_retention` + `scratch_unsaved_work` — **35 passed, 0 failed**.
* `ruff check --config ruff.pre-commit.toml` по 6 изменённым файлам — `All checks passed!`
* `python -m scripts.code_health --base 3e1895729437` — 9 files, **0 blocking, 0 advisory**.
* `git status` чистый, HEAD = `origin/wt/t_e414a3cc` = `e10451c1965`.

## Изменение интерфейса `gc` (осознанное)

Заголовок блока путей: «Preserved scratch workspaces still on disk (N)» →
«Registered preserved scratch workspaces (N)». Причина: при нечитаемом родителе
запись печатается (оператор должен знать о каталоге), но утверждать, что он
«на диске», было бы догадкой — ровно тот класс ошибки, который закрывает Д3.1.

## Финальный широкий прогон (после коммита `888d167994a`)

`wide-suite-round2-final.txt` — `scripts/run_tests.sh` по всем 104
`tests/hermes_cli/test_kanban_*.py`:

```
104 files, 759 passed, 29 failed, 4 skipped (13 of 104 files) in 176.5s
```

Сравнение с базой (`wide-suite-baseline.txt`, 103 файла / 24 failed) сделано
программно по блоку `=== Failure output ===`, а не на глаз:

* base failing files: 13, head failing files: 13;
* **NEW in head: `[]`** — ни одного нового падающего файла;
* fixed: `[]` — ни один предсуществующий не «починился» случайно;
* прирост: +759−750 = +9 тестов проходит (9 тестов провенанса вместо 6 в раунде 1).

Набор файлов совпадает дословно: `core_functionality`, `creator_origin`,
`gateway_restart_handoff`, `managed_worker_loss`, `notify`, `resume_guard`,
`worker_restart_wait`, `worker_session_source`, `worker_socket_recovery`,
`worker_spawn_toolsets`, `worker_terminal_cwd`, `worker_terminal_scope`,
`worktree_teardown`. Причина падений — окружение воркtree: `tests/home_io_guard.py`
отвергает пути `<worktree>/.git/worktrees/...`, плюс нет user-D-Bus для
systemd-scope. Правка этого предохранителя вне области Д3; пункт приёмки
«широкий набор зелёный» закрыт как `manual_review` с доказанным нулевым
приращением падений.
