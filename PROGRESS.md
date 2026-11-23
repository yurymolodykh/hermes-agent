# PROGRESS — t_e414a3cc: провенанс сохранённых scratch-каталогов (Д3)

## Состояние: РАУНД 2 — оба дефекта ревью исправлены, доказательства сняты, коммит впереди.

### Раунд 2 (после request_changes от 06:48)

* **Д3.1** `kanban_preserved_workspaces.directory_is_gone()` — «нет каталога»
  только при достоверном отрицании (`lstat`→ENOENT + листингуемый родитель);
  `EACCES`/`EIO`/`ENOTDIR` на предке = «не знаю» → запись И событие живут.
  Используется в `prune_gone()` и `adopt()`. Заголовок `gc` →
  «Registered preserved scratch workspaces».
* **Д3.2** `adopt(conn, task_id=None)` переносит legacy preserved-события в реестр;
  вызывается из `kanban_db._register_preserved_workspaces()` перед
  `delete_archived_task`/`delete_task` и из `_reap_preserved_workspaces()` в `gc`.
* 3 новых теста (всего 9): нечитаемый родитель, hard-delete legacy-карточки,
  adopt на первом `gc`.
* Доказательства: `red-before-round2.txt` (3 failed на b2f959e8ffe),
  `green-round2.txt` (89 passed), `live-smoke-round2.txt` (S1–S4),
  `live-board-adoption.txt` (копия живой доски: adopted=2),
  `wide-suite-round2.txt` (759 passed, 29 failed — те же 13 файлов, что на базе).

### Раунд 1

* Дерево: `/home/hermes/.hermes/hermes-agent/.worktrees/t_e414a3cc`
* Ветка: `wt/t_e414a3cc`; база `c22f1fdcad3`; code-health база `3e1895729437`
* Checkpoints недоступны (store busy) → откат только через git-коммиты.

## Выбранный вариант (обоснование)

(а) + durable-реестр + учёт в `gc` — гибрид: ретенция исключает вид события
`workspace_cleanup_preserved_unsaved_work`, а рядом с БД доски лежит
`kanban-preserved-workspaces.json`, который переживает и ретенцию, и hard-delete.
Исключение из ретенции ограничено: `gc` удаляет запись реестра И событие, как
только каталога на диске нет (тест `test_gc_forgets_a_preserved_dir_the_operator_removed`).
Вариант (в) отклонён: отказ `delete_archived_task` ломает штатный жизненный цикл
карточки ради побочного артефакта.

## Изменения

1. `hermes_cli/kanban_preserved_workspaces.py` (новый) — реестр: `record()`
   (идемпотентно по (task, path): событие не дублируется — Д7, запись upsert),
   `entries()`, `prune_gone()` (удаляет записи без каталога + их события),
   `registry_path(conn)` (по файлу БД **этого** соединения, не по ambient-пину),
   flock/msvcrt-лок с таймаутом 5с, атомарная запись через `os.replace`.
2. `hermes_cli/kanban_db_workspace.py::_preserve_scratch_workspace` — вместо
   прямой `_append_event` зовёт `kpw.record(...)`.
3. `hermes_cli/kanban_db_retention.py` (новый) — `gc_events`/`gc_worker_logs`/
   `_retention_seconds` вынесены из фасада; в DELETE добавлено
   `kind != PRESERVED_KIND`. Вынос нужен и по code-health: фасад был 4372 строк.
4. `hermes_cli/kanban_db.py` — блок ретенции удалён, имена ре-экспортируются
   tail-import'ом (как остальные sibling'и).
5. `hermes_cli/kanban_ops.py::_cmd_gc` — после сводки печатает пути сохранённых
   каталогов (путь + task id + причина) через `_reap_preserved_workspaces()`.
6. `website/docs/user-guide/features/kanban.md` — документирован реестр и команда поиска.
7. `tests/hermes_cli/test_kanban_preserved_provenance.py` (новый) — 6 тестов.

## Доказательства

* Красная база (до реализации): 6 failed — `evidence-t_e414a3cc/red-before.txt`
* Зелёный после: 6 passed — `evidence-t_e414a3cc/green-after.txt`
* ruff + code_health — `evidence-t_e414a3cc/checks.txt`

## Проверки

* `tests/hermes_cli/test_kanban_preserved_provenance.py` → 6 passed
* `python -m scripts.code_health --base 3e1895729437` → 0 blocking, 0 advisory
* `ruff check --config ruff.pre-commit.toml` → All checks passed
* Широкий `tests/hermes_cli/` → см. `evidence-t_e414a3cc/wide-suite.txt`

---

# PROGRESS — t_9bddc422: убрать упоминание диска из `owner_unavailable`

## Состояние: ГОТОВО. Коммит `9c058a57bab` в ветке `wt/t_9bddc422`, дерево чистое.

* Дерево: `/home/hermes/.hermes/hermes-agent/.worktrees/t_9bddc422`
* База: `660c14d4093`
* Diff = `git show 9c058a57bab` (17 локалей + 1 тест, 35+/20-)

## Что сделано

1. Во всех 17 поставляемых локалях (`locales/*.yaml`, кроме `*.tui.yaml`) в ключе
   `explainer.persistence.owner_unavailable` отрицающая оговорка «Диск и другие процессы
   Hermes здесь ни при чём» заменена утвердительной «Это не проблема хранилища»
   (en: `This is not a storage problem`). Из текста ушли слова disk/drive/диск/قرص/
   Festplatte/disque/disco/diosca/lemez/磁碟/磁盘/ディスク/디스크 и «другой процесс Hermes».
   Переводы сделаны по смыслу для каждого языка, дословно по образцу вердикта для en/ru.
2. `tests/agent/test_turn_completion_explainer.py` — в
   `test_explanation_persistence_owner_unavailable_never_blames_disk` добавлены строгие
   проверки `assert "disk" not in lower`, `"out of room" not in lower`,
   `"another hermes process" not in lower`; плюс отдельная проверка ru-локали
   (`диск` / `закончилось место` / `другой процесс`) через `parse_locale_file`, потому что
   форматтер рендерит только активную локаль.

## Доказательства (`evidence-t9bddc422/`, gitignored `evidence-*/`)

* `red-before-locale-fix.txt` — тест с новыми проверками ДО правки локалей:
  **2 failed, 32 passed** (`AssertionError: assert 'disk' not in '⚠️ no reply…'` оба параметра).
* `green-after-locale-fix.txt` — ПОСЛЕ правки: **34 tests passed, 0 failed**.
* `grep-and-copy.txt` — критерий 1: 0/17 локалей с запрещёнными словами в `owner_unavailable`;
  буквальный `grep -rn "out of room\|another hermes process\|Диск и\|на диске закончилось" locales/`
  находит только ключ `explainer.persistence.default` (общий текст «неизвестной причины»),
  который вне области карточки и не менялся.
* `adjacent-regression.txt` / `adjacent-baseline-locales.txt` — смежные наборы:
  7 падений и с правкой, и на базовых локалях (идентичный набор) — предсуществующий артефакт
  `home_io_guard` на путях `.git/worktrees` в worktree-полосе, не связан с правкой.

## Окружение (важно для повтора)

`scripts/run_tests.sh` без указания интерпретатора падает («no bootstrap Python found») и
подхватывает venv, чей `sys.prefix` — симлинк `installs/bc197d…` → `installs/019d0d…`;
`home_io_guard` сравнивает лексический путь и ругается на site-packages. Рабочий вызов:

```
HERMES_PYTHON=/home/hermes/.hermes/installs/019d0d114d22b7e6/test-environment/gen-9bbf42a937e84eb3a86cc3daad8a5c69/venv/bin/python \
  scripts/run_tests.sh tests/agent/test_turn_completion_explainer.py
```

## Проверки

* `test_turn_completion_explainer.py` → **34 passed**.
* `ruff check --config ruff.pre-commit.toml` на тесте → All checks passed.
* `python -m scripts.code_health --base 660c14d4093` → 0 blocking, 0 advisory.

## Передача

Ревизия `9c058a57bab`, ветка `wt/t_9bddc422`, НЕ влита в общую ветку, служба не перезапускалась —
это отдельный шаг оркестратора. Коммит сделан ops-autocommit'ом (`wip(auto): t_9bddc422`),
содержимое проверено: 34 изменённые строки локалей — все `owner_unavailable`, соседние причины
не тронуты.

---

# PROGRESS — t_0650ebf8: мягкий бюджет сохраняет работу и в не-git каталоге

## Состояние: реализовано, проверено, закоммичено (`60626efab961`). Передано на ревью 15-sre-qa.

* Дерево: `/home/hermes/.hermes/hermes-agent/.worktrees/t_0650ebf8`, ветка `wt/t_0650ebf8`
* Базовая ревизия: `aed44879299`
* Правка = `git diff aed44879299` (два файла кода, один тестовый файл, `evidence-t0650ebf8/`)

## Что сделано

1. `agent/kanban_budget_checkpoint.py`
   * `WipPreservation` — результат мягкого сохранения: `mode` в `git|archive|none`, `locator`, `detail`.
   * `preserve_wip_tree()` — на пороге напоминания: git-дерево → прежний ref (`preserve_wip_ref`),
     каталог без репозитория → тот же tar-фолбэк (`_archive_tree`), что на жёстком пределе;
     формат имени `wip-<stamp>.tar.gz` и каталог `task_attachments_dir(task_id)` не менялись.
   * `_tree_fingerprint` + `_existing_archive` + `.wip-fingerprint` рядом с архивами — повторный
     вызов на неизменённом дереве возвращает существующий архив, второй файл не пишется.
     Сравнение по пути/size/mtime набора файлов (полный хеш не считается).
   * Существующие архивы сортируются по `st_mtime_ns`, не по имени: суффикс `-2` одного тика
     сортируется раньше «чистой» метки.
   * Усечённый по `_ARCHIVE_MAX_BYTES` архив не записывает fingerprint — иначе следующий
     неизменённый вызов переиспользовал бы неполный архив.
2. `agent/turn_iteration_prep.py` — `_maybe_inject_iteration_budget_warning` зовёт
   `preserve_wip_tree()` вместо `preserve_wip_ref()`; в текст напоминания попадает либо ref,
   либо путь архива. Проверка «последняя строка — tool-результат, не персистентная» перенесена
   ДО сохранения: без неё ref/tar создавался бы на ходе, где напоминание не вставится.
3. `tests/agent/test_kanban_budget_reminder_wip.py` — 3 теста: scratch → архив с содержимым +
   путь в тексте; git-worktree → ref и НЕТ архива; повтор на неизменённом дереве → второй архив
   не появляется, напоминание называет существующий.

## Проверки

* `tests/agent/test_kanban_budget_reminder_wip.py` → 3 passed.
* Красная база: файлы кода из `aed44879299` (изолированный export дерева + новый тест) →
  2 failed, 1 passed (`evidence-t0650ebf8/baseline-red.md`).
* Наборы падающих тестов «база vs правка» на 7 файлах бюджета: база 58 / правка 56;
  «только с правкой» — пусто, «только на базе» — ровно два новых теста в их базовых версиях.
  Остальные падения предсуществующие (страж `home_io_guard.py` ругается на путь worktree
  `<worktree>/hermes-update-pull`), наборы совпадают.
* `ruff check --config ruff.pre-commit.toml` — All checks passed.
* `python -m scripts.code_health --base aed44879299` → 0 blocking, 0 advisory.
* Живой прогон на учебной scratch-карточке (`evidence-t0650ebf8/live-scratch.txt`):
  108/180 → архив 942 Б за 57 мс, верхние записи `PROGRESS.md, report.md, nested/notes.txt`,
  повтор на неизменённом дереве — архивов до=1 после=1, после правки — 2.
* Git-карточка (`live-git-and-cost.txt`): ref `refs/hermes-wip/t_probe_git`, HEAD не сдвинут,
  tar нет. Стоимость на больших деревьях — там же (5 тыс. файлов ≈ 1 с; 1 ГБ несжимаемых ≈ 12 с).

## Передача

Ревизия `60626efab961` в ветке `wt/t_0650ebf8` (не влита в общую ветку).
Независимое ревью точной ревизии, тесты и выкат — карточка профиля `15-sre-qa`.

---

# (архив) PROGRESS — t_4cb8f83b: точечный перенос двух апстрим-фиксов

## Состояние: ГОТОВО (обе правки внесены, тесты и проверки сняты). Остались финальный коммит и передача.

* Дерево: `/home/hermes/.hermes/hermes-agent/.worktrees/t_4cb8f83b`, ветка `wt/t_4cb8f83b`
* База для сравнения: `beeb9623539`
* Содержимое правки = `git diff beeb9623539`
* Отчёт: `REPORT-patches.md`; доказательства: `evidence-t4cb8f83b/`
