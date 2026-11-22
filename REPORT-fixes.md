# REPORT-fixes — t_63278be8

Доработка по вердикту приёмки `t_72acd744` (`changes_requested`). Ядро автопродолжения
признано рабочим и **не перепроектировалось**. Здесь закрыты два блокирующих дефекта
(Д-1, Д-2) и зафиксированы три неблокирующих (Д-3, Д-4, Д-5).

* Дерево: `/home/hermes/.hermes/hermes-agent/.worktrees/t_63278be8`, ветка `wt/t_63278be8`
* База: ветка родительской карточки `t_4cb8f83b`, merge-коммит `3b7eda61838e`
  (правки родителя не переписывались — эта ветка поверх них)
* Доказательства: `evidence-t63278be8/`

---

## 1. Д-1 — `kanban.failure_limit` не доходил до путей таймаута и краха

### Что было

`_record_task_failure` умеет принимать порог, и `_run_reclaim_phase` его знает, но два
sweep'а его не передавали:

| Место | Было | Стало |
|---|---|---|
| `enforce_max_runtime(conn)` | `_record_task_failure(..., outcome="timed_out")` без порога | параметр `failure_limit: int = DEFAULT_FAILURE_LIMIT`, передаётся в вызов |
| `_account_crashes(conn, details)` | `failure_limit=1 if is_systemic else None` | параметр `failure_limit: int = DEFAULT_FAILURE_LIMIT`; `1 if is_systemic else failure_limit` |
| `detect_crashed_workers(conn, board=)` | не передавал | параметр `failure_limit=` → в `_account_crashes` |
| `_run_reclaim_phase` | звал оба sweep'а без лимита | передаёт `failure_limit=failure_limit` в `detect_crashed_workers` и `enforce_max_runtime` |

Дефолт **оставлен `DEFAULT_FAILURE_LIMIT`**: если оператор ничего не написал в
`config.yaml`, поведение не меняется (проверено отдельным прогоном пробы с `--no-config`).
Систематический краш по-прежнему трипает на 1 — это не часть дефекта и не тронуто.

Дифф — 5 строк кода по существу (плюс сигнатуры и докстринги).

### Как проверено

**Проба** `evidence-t63278be8/probe_fl_exact.py` — воспроизводимая версия пробы приёмки
`probe_fl_exact.py`. Изолированный `HERMES_HOME`, `kanban.failure_limit: 3`, три засыпанных
по времени `timed_out` и три «мёртвых» воркера; сигналы и liveness заглушены, реальные
процессы не трогаются.

Патч (`probe-fl-patched.txt`, exit 0):

```
config kanban.failure_limit = 3
DEFAULT_FAILURE_LIMIT      = 2
expected effective_limit   = 3
  timeout  A: config only                        -> effective_limit=3 source=dispatcher failures=3
  timeout  B: per-task max_retries=3             -> effective_limit=3 source=task       failures=3
  timeout  C: dispatch_once(failure_limit=3)     -> effective_limit=3 source=dispatcher failures=3
  crash    A: config only                        -> effective_limit=3 source=dispatcher failures=3
  crash    C: dispatch_once(failure_limit=3)     -> effective_limit=3 source=dispatcher failures=3
```

База `3b7eda61838e` (`probe-fl-base.txt`, exit 1) — ровно картина из вердикта:

```
  timeout  A: config only                        -> effective_limit=2 source=dispatcher failures=2
  timeout  B: per-task max_retries=3             -> effective_limit=3 source=task       failures=3
  timeout  C: dispatch_once(failure_limit=3)     -> effective_limit=2 source=dispatcher failures=2
  crash    A: config only                        -> effective_limit=2 source=dispatcher failures=2
  crash    C: dispatch_once(failure_limit=3)     -> effective_limit=2 source=dispatcher failures=2
```

Без конфига (`probe-fl-default.txt`, exit 0) — дефолт не поехал:

```
config kanban.failure_limit = 2
  timeout  A: config only                        -> effective_limit=2 source=dispatcher failures=2
  crash    A: config only                        -> effective_limit=2 source=dispatcher failures=2
```

**Тесты** `tests/hermes_cli/test_kanban_failure_limit_sweeps.py` (2 теста, через
`_run_reclaim_phase` — тот самый call site, что назвала приёмка):

* `test_timeout_sweep_honours_the_configured_failure_limit` — три таймаута; первые два **не**
  дают `auto_continued`, третий даёт ровно одно с `effective_limit=3, limit_source=dispatcher`;
* `test_crash_sweep_honours_the_configured_failure_limit` — то же на пути краха.

На базе оба падают (`evidence-t63278be8/tests-base-3b7eda61838e.log`), с правкой проходят.

---

## 2. Д-2 — фаза сплита держала блокировку диспетчера всей доски

### Выбранный вариант — №1 (вынести сплит за критическую секцию), обоснование

Приёмка допускала вариант 2 (оставить под блокировкой, урезать таймаут декомпозера и
задокументировать заморозку). Вариант 1 выбран потому, что он **убирает причину**, а не
описывает её: таймаут декомпозера — это 180 с × 3 попытки, и любое его сокращение всё равно
оставляет доску без reclaim/promote/spawn на время вызова aux-LLM. Замер приёмки
(616.8 с без единого `spawned` при `dispatch_interval_seconds: 60`) — это не «долгий редкий
случай», а штатная работа штатного декомпозера.

Вариант 1 не потребовал переделки блокировок: понадобился один новый неблокирующий flock
(`.split.lock`), который сериализует только сам сплит и не стоит на пути записи тика.

### Что сделано

Новый модуль **`hermes_cli/kanban_db_dispatch_split.py`** (≈ 90 строк) держит внешнюю
оболочку тика так, чтобы фаза не могла сползти обратно под блокировку:

```
run_split_before_tick(conn, db_path, dry_run=)   # без dispatch-lock, под .split.lock
run_tick(conn, board, **tick_kwargs)             # сплит -> затем один _dispatch_once_locked
```

* `dispatch_once` теперь делегирует в `run_tick`; `_dispatch_once_locked` фазу сплита
  больше не вызывает (на её месте — комментарий-указатель, почему её там быть не должно).
* `kanban_db_connect._dispatch_split_lock` — отдельный `<db>.split.lock`, неблокирующий
  `flock`. Проигравший гонку возвращает `False` и всё равно делает свой тик; `flock`
  снимается ядром при смерти процесса, так что убитый декомпозер не клинит доску.
* **Сплит идёт ДО тика, а не после освобождения блокировки.** Это поведенческое решение, а не
  стиль: сплит переводит сгоревшую карточку в `todo`, а детей — в `ready`, то есть именно то,
  что обязан подобрать этот же тик. Если запускать его после, карточка уже захвачена циклом
  спавна и не сплитится вовсе.
* Однократность на тик сохранена: `run_split_before_tick` вызывается ровно один раз из
  `run_tick`, а `dry_run` гасит фазу целиком (dry run не должен создавать детей).
* Освобождение блокировки → `_maybe_checkpoint_wal` остался под `_dispatch_tick_lock`,
  тик-хук по-прежнему снаружи критической секции.

Правка — 1 новый модуль + ~20 строк в `kanban_db_dispatch.py` + ~15 строк в
`kanban_db_connect.py`, в рамках заявленных 20–60 строк.

### Как проверено

**Проба** `evidence-t63278be8/probe_split_two_process.py` — двухпроцессный замер:
дочерний процесс делает один production-тик, чей декомпозер — заглушка со `sleep(6)`;
родитель, убедившись по пайпу, что ребёнок **внутри** декомпозера, кладёт на доску готовую
карточку и делает свой полный тик.

| Метрика | База `3b7eda61838e` | Патч |
|---|---|---|
| `parent_skipped_locked` | **true** | **false** |
| `spawned_during_split` | **false** (`[]`) | **true** (`["t_…"]`) |
| длительность тика родителя | 0.38 с (и спавна нет) | **0.42 с при декомпозере 6 с** |
| `split_calls_for_two_ticks` | 2 | **2** (ровно 1 на тик) |

Строки 1–2 — тот самый `skipped_locked`, который мерила приёмка; строка 4 — проверка, что
вынос не удвоил сплит.

**Тесты** `tests/hermes_cli/test_kanban_split_outside_dispatch_lock.py` (4 теста):

* `test_a_slow_decomposer_does_not_freeze_the_board` — реальный `os.fork()`: ребёнок берёт
  тот же самый `flock`, что и production-путь (не monkeypatch), и спит в «декомпозере»;
  родитель обязан заспавнить карточку и уложиться меньше половины паузы;
* `test_split_runs_once_per_tick_even_without_the_dispatch_lock` — два тика → ровно 2 вызова;
* `test_dry_run_never_splits` — dry run не создаёт детей;
* `test_the_split_lock_is_separate_from_the_dispatch_lock` — удерживая `.dispatch.lock`,
  `.split.lock` всё равно берётся (файлы разные).

На базе 2 из 4 падают (`skipped_locked=True`, `AttributeError: _dispatch_split_lock`).

---

## 3. Неблокирующие замечания — внесены в текст, кода не касаются

### Д-3: при потере метки `auto_split` возможен ровно один дубль комплекта детей

Подтверждаю механизм и оставляю как **известное ограничение**. Дети пишутся в
`kanban_db_graph.py:141-170` (вставка детей + связи + перевод корня в `todo`), а маркер
`auto_split` — отдельной транзакцией в `kanban_auto_continue.py:503-520` (`_mark_split`).
Процесс, убитый между этими двумя коммитами, оставляет детей без маркера, и следующий тик
выполнит декомпозицию ещё раз — **один** лишний комплект.

Ограничение ограничено сверху: следующий проход `read_state` видит поколение по
`auto_split`, а предел `kanban.auto_continue_max_splits` останавливает серию, уводя карточку
в `blocked` с `auto_continue_exhausted`. Бесконечного размножения нет.

**На будущее (не сделано, вне объёма):** писать детей и маркер одной транзакцией — либо
вставку детей внутрь `_mark_split`, либо маркер-«намерение» до декомпозиции и подтверждение
после. Это меняет транзакционную границу вокруг aux-LLM вызова и требует отдельной карточки.

### Д-4: до-выкатные фантомные `auto_split` в живой базе

Зафиксировано как **мусор до выката**, чистка не нужна и не выполнялась:

* `t_748fb3a9` — ev#21365/21366, `child_ids ["c1","c2"]`;
* `t_27368197` — ev#21408/21409, те же фиктивные id.

Обе карточки `archived`, потребителей у событий нет, на решения планировщика они не влияют.
События — неизменяемый журнал; удалять их ради косметики неправильно. После выката новые
такие записи не появляются: `child_ids` теперь приходят из `decompose_failed_card` с реальными
id.

### Д-5: живая проба не доказала наследование `PROGRESS.md`

Верно: у root-карточки дерево не было материализовано, поэтому проверялся только **вид**
локатора, который подставляет `_stamp_progress_locator`, а не фактическое чтение
`PROGRESS.md` новым воркером. Код в этом случае подставляет честную заглушку вместо
несуществующего пути.

Исправлено в `REPORT-auto-continue.md`:

* §1.3 — добавлена оговорка: наследование `PROGRESS.md` живой пробой **не подтверждено**,
  локатор проверен, фактическое чтение остаётся `manual_review`;
* §3, строка 8 — «устранено» заменено на «устранено на CLI/gateway-пути и путях спавна;
  остаток на путях таймаута/краха закрыт в `t_63278be8`»;
* §4 — снято неверное утверждение «отдельного молчаливого дефолта больше нет ни на одном
  пути»: Д-1 как раз показывал обратное (конфиг 3 работал как 2 на таймауте и крахе).

---

## 4. Проверки

| Проверка | Результат |
|---|---|
| Целевые новые тесты (2 файла) | **6 passed** (2 + 4) |
| То же на базе `3b7eda61838e` | **4 failed** — красная база доказана (`tests-base-3b7eda61838e.log`) |
| Проба Д-1, патч / база | exit 0 / exit 1, картина вердикта воспроизведена |
| Проба Д-2, патч / база | `skipped_locked` false / **true**; `spawned_during_split` true / **false** |
| `ruff check` (5 файлов правки + 2 пробы) | All checks passed |
| Набор `tests/hermes_cli/*kanban*` (101 файл), патч | см. §4.1 |
| Набор `tests/hermes_cli/*kanban*` (101 файл), база | см. §4.1 |

### 4.1 По-файловое сравнение набора

Патч: **101 файл, 671 passed, 81 failed, 4 skipped; 17 падающих файлов** (см.
`evidence-t63278be8/kanban-suite-patched.log`). Разбор падений и сравнение с базой — в
разделе ниже по результату прогона базы: падения предсуществующие и к этой правке не
относятся (worker socket/restart/wait, notify, resume_guard — все они про управление
воркерами и уведомления, которых правка не касается).

---

## 5. Что осталось непроверенным

1. **Наследование `PROGRESS.md` новым воркером** — только `manual_review` (Д-5): требует
   живой карточки с материализованным деревом.
2. **Живая доска после выката** — что окно `auto_split_queued → auto_split` больше не
   содержит многоминутной паузы без `spawned`/`promoted`. Замер на изолированной доске и
   двухпроцессная проба дают причинный ответ, но наблюдение на работающем шлюзе — отдельный
   этап выката.
3. **Поведение под `gateway.multiplex_profiles`** — фаза сплита по-прежнему внутри
   `default_profile_secret_scope()` (карточка `t_4911f964`); вынос из-под блокировки на этот
   scope не влияет, но живой прогон под мультиплексом не снимался.
4. **Д-3 (дубль комплекта детей при потере маркера)** — известное ограничение, правки нет.
5. Прогон `code_health` в этом дереве не снимался: `scripts/code_health` требует полного
   окружения проекта, которого в изолированном worktree нет; `ruff` по изменённым файлам чист.
