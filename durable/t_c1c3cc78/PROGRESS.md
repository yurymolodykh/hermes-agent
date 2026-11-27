# PROGRESS — t_c1c3cc78 [ДИСК-5] read-only остатки уборщика

Модель/провайдер: `deepseek-v4-flash` / `letovo`. Время работы: 2026-11-27 08:15–08:55 МСК.

## Итог: дефект закрыт, оба каталога из «Факта» удалены живым `--apply`

## Где живёт код

Носитель уборки — НЕ файл в git-репозитории hermes-agent: крон `disk-reclaim-every-4h`
(`0 */4 * * *`, job `4c6be16c021f`, `script=disk_reclaim_cron_wrapper.py`) исполняет
`/home/hermes/.hermes/profiles/01-orchestrator/scripts/disk_reclaim_cron.py`. Этот каталог
не версионирован (`git rev-parse` → «не найден git репозиторий»), поэтому правка внесена
в живой файл, а в worktree закоммичены точные копии до/после + доказательства:

| файл | sha256 |
|---|---|
| `durable/t_c1c3cc78/pre-fix/disk_reclaim_cron.py` (до правки) | `92b358dd096501658acb4ab29b6ddadd51b70b8be51de8de64716d2aaebb8a49` |
| `durable/t_c1c3cc78/post-fix-disk_reclaim_cron.py` (после, = живой) | `84f9bdbe765bff51bf610ab07c7517f1f240cf6cc86ad82334ba571e5a13f5f1` |
| `durable/t_c1c3cc78/pre-fix/test_disk_reclaim_card_refs.py` (Z3-тест до) | `207b6da2fea200fc15a380b5b2a9b6f1b56b8344fe504afbe80b62a83c193472` |
| `durable/t_c1c3cc78/post-fix-test_disk_reclaim_readonly_removal.py` (новый тест) | `db60bc857de1c59147de67ffb79f41fcc959db646a21b4c038aaf4ec2ed55404` |

Откат: вернуть `pre-fix/disk_reclaim_cron.py` на место живого файла
(`cp pre-fix/disk_reclaim_cron.py /home/hermes/.hermes/profiles/01-orchestrator/scripts/`) —
поведение вернётся к `shutil.rmtree(path)` без обработчика.

## 1. Правка (disk_reclaim_cron.py)

Было (строка 1020): `shutil.rmtree(c["path"])` — первый же каталог режима 000/0300/0555
валил всё удаление, объект оставался навсегда, каждый прогон снова писал отказ.

Стало — две функции перед `dir_bytes()`:

- `_make_writable(target)` — `os.chmod(node, st_mode | 0o700)` для узла **и его родителя**
  (unlink/rmdir смотрят на родителя, а не на сам узел);
- `force_remove(path)` — `shutil.rmtree(path, onexc=onexc)`; в обработчике: один `chmod`,
  затем повтор операции (`os.unlink` для файла, вложенный `rmtree` для каталога/`os.open`-отказа).
  Множество `attempted` гарантирует **ровно один** повтор: второй отказ того же узла
  пробрасывается и попадает в отказ с причиной.

Точка вызова: `else: force_remove(Path(c["path"]))` вместо прежней ветки dir/unlink.
Формулировка отказа: `ошибка удаления: не удалось даже после снятия прав (<exc>)`
(было просто `ошибка удаления: <exc>`).

Защита Z3 не переделывалась: `check()`, `open_card_paths()`, `under()`, `--ignore-card-ref`
не тронуты; правка только в слое удаления.

## 2. Тест: красный до / зелёный после

Новый файл `/home/hermes/.hermes/profiles/01-orchestrator/scripts/tests/test_disk_reclaim_readonly_removal.py`
(5 сценариев): подкаталог 000, подкаталог 0300, read-only файл в каталоге 0555,
«живой держатель/открытая карточка по-прежнему защищает путь», «второй отказ пишется
в манифест с явной причиной и повторяется ровно один раз». Прогон идёт через настоящий
`main()` с `--apply --manifest`, дом и доска — временные (`mod.HH = home`), сеть не нужна,
боевые каталоги не читаются.

Одна команда, один файл-комплект:

```
red:   cd .../red-proof/scripts && python -m pytest -q tests/test_disk_reclaim_readonly_removal.py
       → 4 failed, 1 passed  (exit 1)   durable/t_c1c3cc78/evidence/red-prefix.out
green: cd /home/hermes/.hermes/profiles/01-orchestrator/scripts
       python -m pytest -q tests/test_disk_reclaim_readonly_removal.py tests/test_disk_reclaim_card_refs.py
       → 13 passed (exit 0)             durable/t_c1c3cc78/evidence/green-postfix.out
```

Интерпретатор: `/home/hermes/.hermes/hermes-agent/.venv/bin/python` (Python 3.14, `onexc=` доступен).
Красный прогон — на точной копии пре-фикс скрипта (`red-proof` собран из `pre-fix/`),
красное сообщение — ровно то, что в журнале: `ошибка удаления: [Errno 13] Permission denied: .../arc2`.
13 passed = 5 новых + 8 существующих Z3-тестов, то есть защита из Z3 не сломана (требование ≥8).

## 3. Живой прогон

`durable/t_c1c3cc78/live_run.py` (dry → apply → check), журнал живой:

| шаг | режим | объектов | отказов | освобождено | свободно ГиБ |
|---|---|---|---|---|---|
| dry-run `--json` | dry-run | 26 would | 16 | — | 52,5 |
| `--apply` | apply | 26 | 16 | 2 154 526 B (2,05 МиБ) | 52,5 |

Оба объекта из «Факта» в dry-run попали в **would-удаление**, в отказах их нет:

- `/home/hermes/.hermes/profiles/02-hermes-platform/cache/scratch/tmpi_f19jp8`
- `/home/hermes/.hermes/profiles/09-independent-reviewer/cache/scratch/t9be97024`

Readback после `--apply` (тот же скрипт, режим `check`):

```
ABSENT /home/hermes/.hermes/profiles/02-hermes-platform/cache/scratch/tmpi_f19jp8
ABSENT /home/hermes/.hermes/profiles/09-independent-reviewer/cache/scratch/t9be97024
```

Манифест: `evidence/live-apply-manifest.json` — `totals: {items: 26, bytes: 2154526}`,
`host_free_bytes: 56 339 267 584`. Структурный журнал `~/.hermes/logs/disk-prune.log`
получил `kind=run/class/summary/manifest` за 2026-11-27T08:48:55+0300.

Оставшиеся 16 отказов — не дефект, а работающие правила: «стоп-имя» (pytest-of-hermes),
«живой процесс держит» (agent.log/errors.log активных профилей), «ссылка из незакрытой
карточки» (t_9f57d0cb, t_f4f15fa9). Ни один не относится к read-only.

Замечание по прогону: живой скан защищает пути, упомянутые в теле ЭТОЙ карточки
(она цитирует оба каталога), поэтому прогон выполнен с операторским
`--ignore-card-ref t_c1c3cc78` — штатный инструмент из Z3, `workspace_path` он не снимает.

## 4. Ограничения соблюдены

- Z3 (`open_card_paths`/`check`/`under`/`--ignore-card-ref`) только используется, не переделан.
- Носитель уборки — крон `disk-reclaim-every-4h`; обёртка не менялась. Системный таймер
  `hermes-disk-reclaim.timer` проверен чтением: `is-enabled → disabled`, `is-active → inactive`,
  юнит-файлы на месте. Не включался и не трогался.
- Правки в worktree + живой файл носителя (см. «Где живёт код»); полный обход диска не делался.

## 5. Что осталось за пределами карточки

Профильные скрипты (`profiles/01-orchestrator/scripts/`) не версионируются, поэтому
фикс существует в единственном экземпляре на диске и его история — только этот worktree
(pre/post копии + хеши). Если каталог профиля будет восстановлен из бэкапа старше
2026-11-27, правка исчезнет: признак — снова «ошибка удаления: [Errno 13] Permission denied»
по read-only подкаталогам в `disk-reclaim.log`. Кандидат на отдельную карточку —
перенести `disk_reclaim_cron.py` под git.
