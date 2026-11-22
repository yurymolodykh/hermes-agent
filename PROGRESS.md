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
