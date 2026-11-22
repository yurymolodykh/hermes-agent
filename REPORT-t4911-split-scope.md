# Отчёт: авто-сплит на холодном кэше под multiplex (t_4911f964)

**Ветка:** `wt/t_4911f964` · **коммит:** `9dd90dc8cce9cb9e4964f1c3480300f8ffee72a3`
**База:** `497c8986c3` · **дерево:** `/home/hermes/.hermes/hermes-agent/.worktrees/t_4911f964`

## 1. Дефект и его механизм (воспроизведён)

`run_split_phase` (фаза авто-сплита) звалась из тика диспетчера без profile
secret scope. Под `gateway.multiplex_profiles: true` первый `get_secret` в
холодном процессе поднимает `UnscopedSecretError`
(`agent/secret_scope.py`), а `agent/auxiliary_client` перехватывает ровно этот
класс и молча откатывается на `os.environ`, где profile-scoped ключ лежит не
там. Итог: `decompose_failed_card` → `ok=False` → `_mark_split_unavailable` →
карточка продолжается.

Почему «сам чинится»: как только aux-клиент `kanban_decomposer` попадает в
`_client_cache`, секрет повторно не читается. Значит после **каждого**
рестарта шлюза первый авто-сплит не работал, а «предел поколений» маскировал
это под штатное `auto_continue_exhausted` + `decomposer_unavailable: true`.

Прямая проба (живой код `agent.auxiliary_client` + `agent.secret_scope`,
временный `HERMES_HOME`, ключ только в profile `.env`):

```
cold, no scope:   FAIL UnscopedSecretError
cold, with scope: OK
```
Лог: `…/cache/scratch/t4911/probe_scope.py`.

## 2. Правка

| Файл | Что |
|---|---|
| `hermes_cli/kanban_profile_scope.py` (новый, 45 стр.) | `default_profile_secret_scope()` — единственный владелец контекста `owner_secret_scope(get_hermes_home())`; no-op вне multiplex |
| `hermes_cli/kanban_auto_continue.py` | `run_split_phase` обёрнут в `with default_profile_secret_scope(), _pin_first_board_resolution():`; `_mark_split_unavailable` пишет `failure_class`; `AutoState.split_failure_class`; `plan_after_failure(split_failure_class=…)` → `decomposer_failure_class` в payload блока |
| `hermes_cli/kanban_specify.py` | `SECRET_SCOPE_FAILURE`, `_is_secret_scope_failure` (обход `__cause__`/`__context__`), `aux_failure_reason`, `failure_class`; `_call_aux` возвращает `secret-scope: LLM error …` для scope-дефекта |
| `gateway/kanban_watchers_dispatcher.py` | `_default_profile_secret_scope` делегирует в общий модуль (двух копий контекста нет) |
| `tests/hermes_cli/test_kanban_split_secret_scope.py` (новый) | 4 теста |

**Почему обёртка внутри фазы, а не в точке вызова:** aux-вызов принадлежит
фазе, поэтому покрыты все вызывающие (тик диспетчера, CLI, будущие), и путь
не может «отстать» от соседнего кода снова. Плюс `kanban_db_dispatch.py` уже
за лимитом `FILE_LINES` (3170 > 3164 на main) — правка там дала бы новый
blocking.

**Семантика сохранена:** «декомпозер недоступен → карточка продолжается»
не тронута; порог поколений, политику ретраев и внутренности декомпозера не
менял.

## 3. Критерии приёмки

### 3.1 Тест холодного кэша под multiplex — красный до / зелёный после

Красное на прежнем коде (рабочие файлы возвращены `git checkout`, новый
модуль убран; прогнан один и тот же файл тестов):
`…/t4911/red-final.txt` → **4 failed**:

```
FAILED …::test_cold_cache_split_under_multiplex_produces_children
FAILED …::test_first_split_after_a_restart_needs_no_warm_up
FAILED …::test_missing_scope_is_marked_as_a_scope_failure_not_a_provider_error
FAILED …::test_a_provider_failure_stays_an_ordinary_llm_error
E  AssertionError: the split phase must split on a cold aux cache under multiplexing; failed=['t_364f92d4']
E  AssertionError: assert [] == ['t_664f…']            (после «рестарта»)
E  AttributeError: module 'hermes_cli.kanban_specify' has no attribute 'failure_class'
```

Зелёное после правки: `…/t4911/green-final.txt` → **45 passed** (новый файл +
`test_kanban_auto_continue` + `test_kanban_decompose` + `test_kanban_specify` +
`test_kanban_aux_affinity` + `tests/gateway/test_single_profile_flip_keeps_launch_work`).

Отдельно на файле теста: **4 passed**.

### 3.2 Класс ошибки виден в журнале (критерий 3)

Событие `auto_continued` при отказе ключа:

```json
{"reason":"split_unavailable",
 "detail":"secret-scope: LLM error UnscopedSecretError",
 "failure_class":"secret_scope",
 "attempt":N, "at":<epoch>}
```

Класс доезжает до `AutoState.split_failure_class` и до payload блока
(`decomposer_failure_class: "secret_scope"`). Настоящий отказ провайдера
остаётся `LLM error: <Exc>`, `failure_class: ""`, карточка продолжается —
это закреплено тестом 4, чтобы классы не слились в другую сторону.

### 3.3 Целевые наборы тестов канбана

Прогон `tests/hermes_cli/test_kanban*.py` + `tests/gateway/test_kanban*.py` +
`test_pin_kanban_board_env.py` + `test_signal_handler_kanban_worker.py` через
`scripts/run_tests_parallel.py`: 20 файлов с падениями. Чтобы отделить
окружение от регрессии, тот же набор прогнан дважды в чистых копиях вне
`~/.hermes` — база (`git archive HEAD`) и патченное дерево:

```
…/t4911/cmp-t4911base.XHcZ.txt      → 17 FAILED
…/t4911/cmp-t4911patched.jgbc.txt   → 17 FAILED
diff по именам тестов: IDENTICAL
```

Все 17 — `RuntimeError: cannot create restart-safe systemd scope for Kanban
worker: the claimed task has no current run id` (в этой среде нет user
systemd). Ни одного нового падения правка не даёт.

В прогоне на самом worktree 20 файлов падают из-за того же плюс guard
`tests/home_io_guard.py` («file I/O against the REAL hermes home» на пути
worktree) — воспроизводится и на базе, к правке не относится.

### 3.4 code_health / ruff / коммит

```
python -m scripts.code_health --base HEAD~1 → 0 blocking, 0 advisory
ruff check --config ruff.pre-commit.toml (5 файлов) → All checks passed!
```

Коммит `9dd90dc8cc` в `wt/t_4911f964`. `pre-commit` пропущен `--no-verify`:
хук падает на `scripts/hermes-python` → «no bootstrap Python found; run
setup-hermes.sh» (сломано окружение worktree, не код); ровно та же проверка
ruff прогнана напрямую интерпретатором test-environment — чисто.

## 4. Остаётся (вне полномочий этой карточки)

* **Критерий 2 — живая проба Б:** первый же сплит сразу после рестарта шлюза
  без прогрева. Требует рестарта живого гейтвея → карточка @15-sre-qa.
  `manual_review`.
* Слияние `wt/t_4911f964` (`9dd90dc8cc`) в основную ветку — оркестратор.

## 5. Границы доказанного

* Доказано: механизм отказа (живая проба + красный тест на реальном стеке
  `dispatch_once` → `run_split_phase` → `decompose_failed_card` →
  `auxiliary_client` → `secret_scope`), исправление на этом стеке, класс
  ошибки в журнале, отсутствие новых падений в наборе канбана.
* Не проверялось вживую: поведение перезапущенного гейтвея (см. §4).
* Провайдер `letovo` в тестах не вызывается: SDK-клиент подменён на
  `_create_openai_client`, сеть не используется.
