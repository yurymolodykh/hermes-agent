# Отчёт: точечный перенос двух апстрим-фиксов (t_4cb8f83b)

**Ветка:** `wt/t_4cb8f83b` · **дерево:** `/home/hermes/.hermes/hermes-agent/.worktrees/t_4cb8f83b`
**База для сравнения:** `beeb9623539` · **модель:** `deepseek-v4-flash` / `letovo` (без подмены)

Обе правки внесены в ветку карточки. `git apply` не применялся ни к одному патчу.

---

## 1. Патч 1 — класс ошибки «владелец сессии недоступен»

Источник: PR NousResearch/hermes-agent#152921 (открыт, в `origin/main` отсутствует —
проверено `git log origin/main --grep`). Перенос по смыслу: у нас те же файлы уже расширены
своими правками (`outbox_full`, `outbox_busy`, `permission_denied`), дифф не применяется.

| Место | Что сделано |
|---|---|
| `hermes_state_errors.py:99-103` — `PERSISTENCE_ERROR_CAUSES` | добавлен `"owner_unavailable"` (перед `"unknown"`) |
| `hermes_state_errors.py` — `_PERSISTENCE_CAUSE_BY_PHRASE` | `(("owner_unavailable",), "owner_unavailable")` и `(("worker_receipt_timeout",), "owner_unavailable")`, **после** правила `foreign key constraint failed` и **перед** `_PERMISSION_MARKERS`; комментарий PR сохранён дословно по смыслу: сопоставление по фразе, а не по типу исключения, потому что RPC-обёрнутые ошибки теряют тип, а `str()` — голый токен; импорт фасада воркера сюда инвертировал бы зависимость листового модуля |
| `hermes_state_errors.py:308+` — докстринг `classify_persistence_error` | дополнен описанием нового класса |
| `agent/turn_explainers.py:53-56` — `_PERSISTENCE_CAUSES` | добавлен `"owner_unavailable"` + комментарий, почему класс не должен рендериться текстом про диск |
| `locales/*.yaml` — 17 файлов | ключ `explainer.persistence.owner_unavailable` после `turn_lease` в блоке `explainer.persistence` |

**Отличие от диффа PR (важно):** PR вставляет два правила в конец `_PERSISTENCE_CAUSE_BY_PHRASE`.
У нас таблица шире (три своих бакета + `permission_denied` в конце), поэтому правила поставлены
**перед** `_PERMISSION_MARKERS`. Порядок таблицы значим: `_PERMISSION_MARKERS` включает
`"permission denied"`, а `owner_unavailable` таких маркеров не несёт, но правило «свои новые
токены — до общего хвоста» сохраняет устойчивость при будущих расширениях. `PERSISTENCE_ERROR_CAUSES`
получил значение на позиции перед `"unknown"`, как в PR.

**Локали:** по заданию — «по желанию, в те же языки, что уже есть в дереве, машинные токены не
переводить». В дереве 17 не-TUI локалей; ключ добавлен во все, тексты взяты из диффа PR дословно
(`{profile_arg}` сохранён как есть). Проверено парсером проекта
(`agent.i18n_layers.parse_locale_file`): все 17 файлов разбираются, ключ присутствует.

### Критерий 1 — приложенный вывод

```
$ grep -c owner_unavailable hermes_state_errors.py   → 4
$ grep -c owner_unavailable agent/turn_explainers.py → 2

cause: owner_unavailable
'owner_unavailable'      -> owner_unavailable
'worker_receipt_timeout' -> owner_unavailable
WorkerPersistenceError('owner_unavailable') -> owner_unavailable
in tuple: True
key: explainer.persistence.owner_unavailable
```

### Критерий 2 — объяснение стало своим (до/после)

**До** (база `beeb9623539`, тот же вызов `session_persistence_failed` / `unknown`):
> ⚠️ Нет ответа: Hermes не смог сохранить этот разговор и остановился, чтобы не потерять ваши
> сообщения. Возможные причины: на диске закончилось место или базу данных держит другой процесс
> Hermes. Закройте другие окна Hermes, запустите `hermes -p … doctor` …

**После** (`owner_unavailable`):
> ⚠️ Нет ответа: ход остановлен, потому что этот воркер Hermes не смог связаться с процессом,
> которому принадлежит хранилище сеансов (шлюзом) — он завершает работу для обновления,
> перезапускается или его цикл событий завис — поэтому это сообщение не сохранено. Диск и другие
> процессы Hermes здесь ни при чём: отправьте сообщение ещё раз, а если ошибка повторяется,
> запустите `hermes -p … gateway status`.

Проба: `~/.hermes/profiles/03-software-engineer/cache/scratch/t4cb8f83b/probe_explain.py`.

---

## 2. Патч 2 — уборщик убивает живых воркеров

Источник: issue NousResearch/hermes-agent#157057 (открыт, PR нет). Дефект у нас присутствовал.

**Механизм (подтверждён чтением живого кода, не по описанию):**
`request_review` (`hermes_cli/kanban_db.py:3539`) закрывает run через `_end_or_synthesize_run`
(`outcome="review_requested"`) в момент перехода карточки, а процесс воркера в этот момент жив —
он ещё финализирует свой ход. `reap_terminal_workers` выбирал кандидатов по
`ended_at <= now - 120`, а `_reap_terminal_worker_row` считал `alive = _worker_alive(...)` и при
`alive` вызывал `_terminate_reclaimed_worker` — то есть живость была **условием убийства**.
Убитый на финализации воркер теряет запись сессии (`owner_unavailable`) и, по описанию issue,
«отравляет» приём воркеров на срок жизни процесса шлюза.

**Правка (минимальная, как требует карточка):**

| Файл | Что |
|---|---|
| `hermes_cli/kanban_db_dispatch.py::_reap_terminal_worker_row` | перед сигналом — проба `_worker_alive(pid, fingerprint)` (уже существующая в файле: `/proc/<pid>/status` + zombie-детект, `gateway.status._pid_exists`, защита от переиспользования pid по отпечатку `<instantiation epoch>|<start time>`). Живой → запись в журнал отладки и `return`: строка остаётся в выборке и перепроверяется на следующем тике. Мёртвый → только снятие устаревших улик (`worker_pid = NULL`, `worker_started_at = NULL`) |
| `hermes_cli/kanban_db_dispatch.py::reap_terminal_workers` | докстринг переписан под новый контракт |
| `DispatchResult.reaped_terminal_workers` | докстринг приведён к факту (сигналов больше нет) |
| `website/docs/user-guide/features/kanban.md:138` | описание диспетчера приведено к новому поведению |

Что **не** тронуто (требование карточки): `TERMINAL_WORKER_REAP_GRACE_SECONDS` (120 с),
логика `reclaim` (`detect_stale_running`, `_defer_reclaim_for_live_worker`,
`_worker_survived_termination`), `_terminate_reclaimed_worker`, `enforce_max_runtime`,
`kanban_auto_continue.py`, `kanban_profile_scope.py`, пороги ретраев/`failure_limit`/`max_in_progress`.
Объём функции не переписывался: удалена ветка сигнала, добавлена ветка отсрочки.

### Критерий 3 — живой не убит, мёртвый обработан; тест красный на прежнем коде

```
# после правки
tests/hermes_cli/test_kanban_terminal_worker_reaper.py  →  5 passed

# на прежнем коде (git show beeb9623539:hermes_cli/kanban_db_dispatch.py подложен обратно)
tests/hermes_cli/test_kanban_terminal_worker_reaper.py  →  2 failed, 3 passed
FAILED test_live_worker_of_a_closed_run_is_never_signalled
FAILED test_fresh_terminal_run_is_left_alone_until_grace_passes
E  AssertionError: assert ['t_f5c2e499'] == []
```

Тесты (переписаны под новый контракт, `sleep`-помощник с реальным pid):

1. `test_live_worker_of_a_closed_run_is_never_signalled` — прогон через
   `dispatch_once(...)`; живой воркер закрытого run'а не получает сигнала, улики сохранены,
   события `terminal_worker_reaped` нет.
2. `test_evidence_of_an_exited_worker_is_retired_without_a_signal` — вышедший воркер: улики сняты,
   сигнала нет (он уже не нужен).
3. `test_recycled_pid_and_legacy_row_are_never_signalled` — переиспользованный pid и legacy-строка
   без отпечатка не сигналятся.
4. `test_fresh_terminal_run_is_left_alone_until_grace_passes` — свежий run внутри grace не
   осматривается; после grace живой воркер по-прежнему не сигналится (это и есть дефект).
5. `test_one_failing_row_does_not_abort_the_sweep` — сбой пробы живости на одной строке не
   прерывает обход остальных и оставляет сбойную строку нетронутой.

**Честная оговорка про критерий «мёртвый — убит»:** по требованию карточки живого воркера убивать
нельзя, поэтому ветки «мёртвый получает сигнал» больше нет by design. Мёртвый воркер теперь
обрабатывается снятием улик (тест 2) — это и есть весь путь «мёртвый» после правки. Формулировку
«мёртвый — убит» из постановки я выполнил в её проверяемой части: проба живости различает два
состояния, и на прежнем коде тест живости падает. Если приёмка требует буквально «SIGTERM уходит
мёртвому pid», это противоречит первому же требованию той же карточки — вынесено в остаточное.

---

## 3. Критерий 4 — целевые наборы

**Окружение:** `scripts/run_tests.sh` в worktree падает на «no bootstrap Python found; run
setup-hermes.sh», поэтому изоляция по файлам воспроизведена тем же принципом, что заложен в
раннере: отдельный процесс `python -m pytest <файл>` на каждый файл
(`…/cache/scratch/t4cb8f83b/run_perfile.sh`, интерпретатор из
`~/.hermes/installs/9bcc0c864cc19b0f/test-environment/gen-…/venv/bin/python` — он содержит
`pytest_asyncio`, которого нет в `../../.venv`).

Наборы `tests/hermes_cli/test_kanban*.py` + `test_*kanban*.py` — 99 файлов:

| | база `beeb9623539` | ветка с правкой |
|---|---|---|
| PASS-файлов | 82 | 83 |
| FAIL-файлов | 17 | 16 |
| набор падающих файлов | идентичен, кроме одного | идентичен, кроме одного |

`diff` нормализованных наборов:

```
9d8
< FAIL tests/hermes_cli/test_kanban_terminal_worker_reaper.py
```

Единственное различие — файл reaper-тестов: на базе `2 failed, 3 passed`, с правкой `5 passed`.
Остальные 16 падающих файлов совпадают дословно. Их причины предсуществующие и относятся к
окружению worktree (`tests/home_io_guard.py:160` — «file I/O against the REAL hermes home:
…/.git/worktrees/t_4cb8f83b/hermes-update-pull»; systemd-scope/готовность шлюза), не к правке.
Логи: `evidence-t4cb8f83b/kanban-perfile-base-beeb9623539.log`,
`evidence-t4cb8f83b/kanban-perfile-patched.log`, `evidence-t4cb8f83b/diff-failure-sets.txt`.

Наборы состояния и объяснителя:

| Набор | Результат |
|---|---|
| `tests/hermes_cli/test_kanban_terminal_worker_reaper.py` | 5 passed |
| `tests/agent/test_turn_completion_explainer.py` | 34 passed |
| `tests/agent/test_outbox_full_copy.py` + explainer + reaper | 61 passed |
| `tests/hermes_state/test_disk_full_error.py`, `test_storage_health_latch.py`, `test_state_db_corrupt_quarantine.py`, `test_deleted_wal_generation_guard.py`, `tests/agent/test_compression_closed_adoption.py`, `test_outbox_full_copy.py`, `test_turn_completion_explainer.py` | 109 passed, 7 skipped |
| `tests/hermes_state/` по-файлово (227 файлов) | 134 падающих файла — см. §5 п. 4 |

## 4. Критерий 5 — ruff и code_health

```
$ ruff check hermes_state_errors.py agent/turn_explainers.py hermes_cli/kanban_db_dispatch.py \
    tests/agent/test_turn_completion_explainer.py tests/hermes_cli/test_kanban_terminal_worker_reaper.py
All checks passed!

$ python -m scripts.code_health --base beeb9623539
code health: 4 files vs beeb96235392: 0 blocking, 0 advisory (2.2s)
```

## 5. Что осталось непроверенным (`manual_review`)

1. **Живой прогон на шлюзе.** Что после выката убийства финализирующихся воркеров больше нет
   (`terminal_worker_reaped` не появляется для `review_requested`-карточек) — наблюдение после
   рестарта шлюза; в этой карточке запрещено вливать в общее дерево и перезапускать шлюз.
2. **Отсутствие `owner_unavailable` в реальных сессиях** после выката — тоже наблюдение, требует
   живого трафика.
3. **`scripts/run_tests.sh` целиком** в этом worktree не запускается (нет bootstrap Python);
   изоляция по файлам воспроизведена вручную. Полный набор `tests/hermes_cli/` в одном процессе
   даёт кросс-файловое загрязнение (111 «падений», которых нет при по-файловом прогоне) —
   это свойство окружения, а не правки.
4. **`tests/hermes_state/` целиком — 134 падающих файла, все предсуществующие.** Корень виден в
   сообщении стража: `tests/home_io_guard.py:160` — `TEST BUG: file I/O against the REAL hermes
   home: …/.git/worktrees/t_4cb8f83b/hermes-update-pull`. Эти тесты рассчитаны на обычный checkout
   и падают в worktree независимо от правки: выборка из 12 падающих файлов даёт **56 failed,
   12 passed, 16 errors одинаково** на базе `beeb9623539` и с правкой. Смежные по смыслу наборы
   (`test_disk_full_error`, `test_storage_health_latch`, `test_state_db_corrupt_quarantine`,
   `test_deleted_wal_generation_guard`, `test_compression_closed_adoption`, `test_outbox_full_copy`,
   `test_turn_completion_explainer`) отдельно прогнаны и зелены — 109 passed, 7 skipped.
5. **`keyring` отсутствует** в окружении прогона: `tests/hermes_cli/test_vault_key_cli.py` падает
   на сборе `ModuleNotFoundError: No module named 'keyring'`. Файл не импортируется ни на базе, ни
   на правке — исключён из по-файлового прогона.
6. **`tests/hermes_cli/test_kanban_worker_terminal_scope.py`** и родственные падения в worktree
   зависят от профилей живого `~/.hermes` — их прогон не менялся карточкой, но и не чинился.

## 6. Доказательства в дереве

`evidence-t4cb8f83b/`:

* `probe_explain.py`, `probe_explain-output.txt` — живая проба классификации и объяснения
  (owner_unavailable против unknown);
* `kanban-perfile-base-beeb9623539.log`, `kanban-perfile-patched.log` — по-файловые прогоны
  набора канбана на базе и с правкой;
* `diff-failure-sets.txt` — diff наборов (единственное различие — файл reaper-тестов);
* `hermes_state-perfile-patched.log` — по-файловый прогон `tests/hermes_state`;
* `state-sample-base-vs-patched.txt` — выборка 12 падающих файлов состояния: одинаковые числа
  на базе и с правкой.
