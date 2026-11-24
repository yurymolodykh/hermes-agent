# t_9f42e621 — PROGRESS (прогон 2)

Состояние: **Готово к передаче.** Правка внесена, проверена, закоммичена.
Коммит: **`63a679321f60`** в ветке `wt/t_9f42e621` (worktree
`/home/hermes/.hermes/kanban/workspaces/t_9f42e621`, база `f39aaeffe232`).

## 1. Что изменено (3 файла, +238/−7)

### `tools/checkpoint_pruning.py`
- `STORE_LOCK_TIMEOUT_SECONDS = 5.0` — ожидание вместо мгновенного отказа.
- `store_lock(base, *, timeout=STORE_LOCK_TIMEOUT_SECONDS)` ждёт через
  `pm.filesystem.lock_path` (тот же примитив, что и раньше умел `timeout`; шаг опроса 0,05 с).
  `timeout=0` = одна попытка, `None` = бесконечно.
- `lock_holder_hint(lock)` — кто держит, через `hermes_cli._early_recovery._held_open`
  (единственная инстанция в дереве; на Linux `/proc`, на macOS/BSD `lsof`).
  Возвращает `held by pid N (cmd)` (одна строка, обрезка 120 симв.), либо честное
  `holder unknown (...)`, либо `no live process holds a descriptor on the lock file`.
  Исключение пробника логируется (`logger.debug(..., exc_info=True)`), а не глотается.
- Отказ теперь: `checkpoint store is busy: <base> — held by pid N (cmd); gave up after 5.0s of waiting (<lock>)`.

### `tools/checkpoint_manager.py`
- `record_agent_write` берёт лок с `timeout=0` (одна попытка): вызывается на **каждый
  записанный файл**, очередь там недопустима. Busy → пропуск одной записи ledger (debug), ход не встаёт.

### `tests/tools/test_checkpoint_store_contention.py` (новый, 178 строк)
6 проверок на реальных процессах и реальной ФС (без моков):
1. `test_waiter_joins_a_held_store_lock` — второй дожидается и входит;
2. `test_two_concurrent_snapshots_of_one_profile_both_land` — два воркера одного профиля,
   оба `ok=True`, оба ref в сторе;
3. `test_timeout_still_refuses_and_names_the_holder` — отказ остаётся, в тексте есть `pid`;
4. `test_zero_timeout_refuses_immediately_without_waiting` — `timeout=0` не ждёт;
5. `test_store_lock_can_be_taken_again_after_release` — нет самоблокировки;
6. `test_holder_hint_is_honest_when_nothing_holds_the_lock` — выдуманного pid нет.

## 2. Замеры «до/после» (инструментом)

| Сценарий | База `f39aaeffe232` | Правка `63a679321f60` |
|---|---|---|
| `repro_profile_race.py race 6` | **ok=6 refused=6** | **ok=12 refused=0** |
| `repro_manager.py race 8` | 24 отказа | 0 отказов |
| latency отказа | **0,027 с** (первый `_lock(wait=False)`) | — |
| успешный чекпойнт держит лок | 0,6–0,75 с | 0,6–0,75 с |
| новый тест-файл | **4 failed**, 1 passed, 1 skipped (`evidence/red-on-base.log`) | **6 passed** |
| регресс (8 модулей чекпойнтов) | — | **111 passed** (`evidence/after-regression.log`) |
| code health vs базы | — | **0 blocking, 0 advisory** |

Реальный двойной прогон после правки: 2 воркера одного профиля → оба `ok=true`, в сторе два
ref'а с записями `race pA` / `race pB` (`git log` по обоим показал новую запись).

Масштаб (живая доска, `kanban.db`): **167** записей `PruneError`, 2026-11-22 22:58 … 2026-11-24 06:33,
равномерно по 31 часу и ~60 карточкам — это не один долгий простой, а постоянная конкуренция.

## 3. Отдельно проверенное (важно для ревью)

- **Брошенный 0-байтовый лок хранилище НЕ блокирует**: `flock` снимает ядро при смерти
  процесса. Проверено: 0-байтовый `.checkpoints.lock` без держателя → лок взят за 1 мс.
  Версия «замок 9,5 часов блокировал хранилище» из комментария оркестратора не подтверждается
  механизмом; реальная причина — конкуренция за лок, что и починено.
- **`ok=False, failure=None` — не потеря чекпойнта**: это «нечего снимать» (дерево не изменилось).
- **`auto_prune` в этой эксплуатации ВКЛЮЧЁН** (`config.yaml:376-382`: `enabled: true`,
  `max_snapshots: 10`, `max_total_size_mb: 150`, `auto_prune: true`, `retention_days: 3`), т.е.
  обслуживание идёт по таймеру (`gateway/run.py:4439`, `hermes_cli/cli_auto_maintenance.py:81`)
  под тем же `.checkpoints.lock`. Сторы профилей 03 — 136 МБ, 09 — 137 МБ, 15 — 110 МБ,
  02 — 109 МБ при потолке 150 МБ: запас ~10 %, поэтому `prune_checkpoints` будет запускаться
  регулярно. Измеренный держатель лока — дольше 420 с (окно замера истекло) на сторе 134 МБ.
  **Это осознанный размен:** ожидание 5 с закрывает конкуренцию снятий между карточками одного
  профиля (что и даёт все 167 записей: они идут равномерно по 31 часу, а не одним блоком), но
  против долгого `git gc` отказ остаётся — и теперь он называет держателя. Если отказы после
  выката продолжатся и держателем окажется `prune`, нужен второй шаг — разделить лок
  «снятие»/«обслуживание» (порядок `maintenance → ref`), это отдельная карточка.
  `record_agent_write` оставлен с `timeout=0` именно поэтому: он вызывается на каждый
  записанный файл и ждать 420 с не может.

## 4. Что НЕ сделано (вне объёма карточки по решению оркестратора)

- Разделение лока «снятие» / «обслуживание» — не понадобилось: минимальная правка (ожидание)
  закрывает дефект. Если понадобится — отдельная карточка.
- `unstage excluded failed — working directory not found` (комментарий оркестратора 23.11 11:54,
  карточка `t_12e58882`) — второй дефект, отдельная карточка.
- Выкат в живое дерево `/home/hermes/.hermes/hermes-agent` — **запрещён стражем деревьев**
  этой карточке (живая ветка `feature/multiuser-access`, `c29dcac552b4`; моя база
  `f39aaeffe232` в неё входит, но merge делает оркестратор). Правка лежит в `wt/t_9f42e621`.

## 5. Стенды (не выбрасывать)

- `evidence/repro_profile_race.py` — один профиль, N воркеров-процессов: `race <N>` / `take`.
- `evidence/repro_store_lock.py` — чистый лок: `hold` / `wait`, печатает держателя из `/proc`.
- `evidence/repro_manager.py` — реальный `CheckpointManager`: `take` / `race <N>` / `maintenance`.
- Логи: `before-profile-race.log`, `after-profile-race.log`, `red-on-base.log`,
  `after-regression.log`, `before-mgr.log`, `before-maint.log`.
- Интерпретатор: `/home/hermes/t72venv/bin/python`. Ruff: `/home/hermes/.local/bin/ruff`
  (venv-обёртка `scripts/hermes-python` в этом worktree не поднимается: «no bootstrap Python»).

## 6. Приёмка карточки: где стоим

- [x] Воспроизведение до правки: текст отказа, pid держателя, длительность (`before.log`,
      `before-mgr.log`, `before-profile-race.log`: 0,027 с; ok=6 refused=6).
- [x] После правки тот же сценарий даёт снятый чекпойнт: оба воркера `ok=true`, `git log`
      стора показывает новые записи `race pA` / `race pB`; отказов 0.
- [x] Тест на конкуренцию: падает на базе (4 из 6), проходит на правке (6 из 6).
- [x] Регресс `pytest` по чекпойнтам зелёный (111 passed); отказ (если случится) называет держателя.
- [ ] Через сутки после выката — **только после слияния оркестратором**, вне этого прогона.
- [ ] Выкат в живое дерево — **оркестратору** (страж деревьев).
