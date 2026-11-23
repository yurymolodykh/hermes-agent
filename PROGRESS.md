# PROGRESS — t_754f37fe: мягкое сохранение не называет усечённый архив и не копит копии

## Состояние: реализовано, проверено, закоммичено. Готово к передаче на ревью 15-sre-qa.

* Дерево: `/home/hermes/.hermes/hermes-agent/.worktrees/t_754f37fe`, ветка `wt/t_754f37fe`
* База: `f376b64a8b4` (вершина `feature/multiuser-access`)
* Правка = `git diff f376b64a8b4..HEAD`
* Доказательства: `evidence-t_754f37fe/` (probes + `probe-*.out.txt`)

## Д-1 — напоминание больше не называет усечённый архив

`_existing_archive` больше не берёт «последний `wip-*.tar.gz` по mtime». Кандидат — только
файл, рядом с которым есть маркер полноты `<архив>.tar.gz.done`, и **сам маркер содержит
отпечаток дерева**; сравнение идёт по содержимому маркера. Тарбол без маркера — не
доказательство полноты (у прошлой версии маркера не было вовсе), поэтому он не возвращается
никогда, при любом порядке появления. mtime-сортировка сохранена для выбора среди
*нескольких* полных маркеров (суффикс `-2` одного тика сортируется раньше чистой метки).

## Д-2 — повторы не плодят файлы, текст не врёт

* «Влезает / не влезает» решается **до** открытия тарбола — по сумме размеров файлов.
* Дерево больше `_ARCHIVE_MAX_BYTES` → тарбол не создаётся вообще. Пишется один
  перезаписываемый `wip-partial.json` (fingerprint, cap, tree_bytes, file_count, первые
  10 путей, время). Повтор на неизменённом усечённом дереве возвращает тот же файл и не
  пишет ничего: 4 вызова → 1 файл вместо 4.
* `WipPreservation.mode` получил значение `partial`; `detail` — «дерево больше потолка
  архива — копия частичная: ни один файл не скопирован, записан только признак потери в …».
* Напоминание в `turn_iteration_prep.py` для `partial` больше не обещает «that archive
  survives the budget stop», а прямо говорит: копии нет, делайте коммит/handoff сейчас.
  Ветка «не сохранено, есть detail без locator» тоже сообщается, а не молчит.
* Когда дерево снова влезает, старый `wip-partial.json` удаляется — запись о потере не
  переживает правду.
* `checkpoint_wip` (жёсткий предел) идёт тем же путём и тоже честно сообщает потерю.

## Тесты (TDD: 4 новых, все падали на базе)

`tests/agent/test_kanban_budget_reminder_wip.py` — 7 тестов, все зелёные:
1. `test_a_truncated_archive_is_never_the_one_the_notice_names` (Д-1)
2. `test_over_cap_repeats_write_one_partial_copy_and_say_so` (Д-2)
3. `test_a_complete_copy_clears_the_loss_record_and_a_shrunk_tree_is_copied`
4. `test_a_legacy_tarball_without_a_done_marker_is_not_reused`

Красная база проверена независимо: `git archive f376b64a8b4` в
`~/.hermes/profiles/03-software-engineer/cache/scratch/redbase-VABq` + новый тестовый файл
→ **4 failed, 3 passed**.

## Пробы (все зелёные, выводы в `evidence-t_754f37fe/probe-*.out.txt`)

| проба | результат |
|---|---|
| `probe-truncated-named2.py` | `DEFECT_notice_names_incomplete_archive: false`; шаг 2 → `partial`, шаг 3 → исходный полный архив `[a.txt, b.txt]` |
| `probe-overcap-repeat.py` | 4 вызова → `archive_count: 0`, `total_bytes_written: 0`, каждый ход `mode=partial` с честным `detail` |
| `probe-independent.py` | `INDEPENDENT_OK: true` (усечённый не называется, повтор переиспользует полный, отказ ничего не пишет, git-карточка = ref без tar) |
| `probe-live-deployed.py` | прогон на живом дереве: архив 330 Б, 3 записи, путь в напоминании, повтор = 1 архив; git → ref, tar нет |
| `probe-minor.py` | HEAD/branch/index/status git-карточки не меняются, ref несёт оба файла |
| `probe-truncated-notice.py` | в напоминании нет `.tar.gz`, есть «Only a partial record was written … no copy of it exists» |
| `probe-truncated-named.py` | `DEFECT_named_archive_is_truncated: false` |

Прим.: исходного `probe-independent.py` в каталоге ревью не сохранилось (только
`independent.txt`), поэтому проба восстановлена по записанным в нём полям и перезапущена;
`probe-live-deployed.py` получил путь к дереву параметром (живое общее дерево воркеру
менять запрещено card-tree-guard'ом).

## Проверки

* `scripts/run_tests.sh tests/agent/test_kanban_budget_reminder_wip.py` → **7 passed**.
* `ruff check` (в т.ч. `--config ruff.pre-commit.toml`) на трёх файлах → All checks passed.
* Соседние наборы (`test_kanban_budget_checkpoint.py`, `test_kanban_budget_wip_checkpoint.py`,
  `test_kanban_handoff_ends_turn.py`, `test_iteration_budget_warning.py`,
  `test_turn_iteration_prep.py`, `test_run_budget.py`): наборы падений «база vs правка»
  **совпадают** (47 и 9 позиций), новых падений нет. Падения предсуществующие: страж
  `tests/home_io_guard.py` ругается на путь `<worktree>/.git/worktrees/.../hermes-update-pull`,
  а в тестовой среде профиля не хватало `psutil`/`requests` (поставлены локально, чтобы
  прогоны вообще стартовали).

## Ограничения и замечания для ревьюера

* Слияние в `feature/multiuser-access` — оркестратор, не воркер.
* `wip-*.tar.gz` без маркера `.done` (архивы прошлой версии) остаются на диске и не
  удаляются: они просто больше не предлагаются модели. Ретенция старых файлов вне объёма.
* Менеджер чекпоинтов сделал автокоммиты `bbd4c3f65ec`, `6667222942d`, `38cc5a57941` —
  вся правка в них. Побочный инцидент: `git stash pop` поднял чужие стэши и оставил
  конфликтные маркеры в `hermes_cli/kanban_ops.py` / `hermes_cli/kanban_db_dispatch.py`
  (файлы вне правки); восстановлены `git checkout HEAD --`, дерево чистое, стэши не тронуты.
* Модель запроса: `letovo/deepseek-v4-flash` (не подменялась).
* ⚠️ Менеджер чекпоинтов профиля не работает: `PruneError: checkpoint store is busy:
  /home/hermes/.hermes/profiles/03-software-engineer/checkpoints` — откат этого прогона
  невозможен. Страховка: коммит `f683da1597e` в `wt/t_754f37fe` + файлы
  `evidence-t_754f37fe/` и `REPORT-t_754f37fe.md` на диске (оба каталога/файлы в
  `.gitignore`, поэтому отчёт приложен к карточке вложениями).


---

# (архив) PROGRESS — t_0650ebf8: мягкий бюджет сохраняет работу и в не-git каталоге

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
