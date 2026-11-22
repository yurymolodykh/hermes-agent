# Отчёт: исчерпан счётчик повторов — карточка продолжается сама, при необходимости делится

**Карточка:** `t_35fe5619` · **Ветка:** `wt/t_35fe5619` · **База:** `3bbf87774c`
**Модель/провайдер:** `deepseek-v4-flash` / `letovo` (дефолт профиля, не подменялась)

## 1. Задача владельца и что изменилось в поведении

«Если чисто по счётчику повторов — карточка сама идёт в работу, но при необходимости
декомпозируется на отдельные карточки, чтобы пройти до конца.»

Было: `_record_task_failure` при `consecutive_failures >= effective_limit` ставил `blocked` и писал
`gave_up`. Дальше нужен был человек (`kanban_unblock`) или оркестратор.

Стало: исчерпание счётчика по **внутренней причине карточки** больше не паркует работу.

| Ситуация | Было | Стало |
|---|---|---|
| Первое исчерпание счётчика (поколение 0) | `blocked` + `gave_up` | `ready` + `auto_continued`, карточка перезапускается |
| Повторное исчерпание в том же поколении | `blocked` + `gave_up` | `auto_split_queued` → следующий тик делит карточку на детей |
| Поколения сплита исчерпаны (`auto_continue_max_splits`) | — | `blocked`, `block_kind='needs_input'`, причина `auto_continue_exhausted` |
| Декомпозер недоступен | — | `ready` + `auto_continued` (`reason=split_unavailable`), работа и дерево сохранены |
| `rate_limited`, `host_interrupted`, `owner_lost`, `infrastructure` | не провал | **без изменений** |
| Терминальные провайдерские (auth/billing/model_gone), системная волна | `blocked` | **без изменений** (`force_trip`/`sticky` сохраняется) |
| `kanban.auto_continue: false` | — | **строго прежнее** поведение: `blocked` + `gave_up` |

## 2. Где это живёт

**Новый модуль `hermes_cli/kanban_auto_continue.py`** (564 стр.) — вся политика, без схемы БД:

* `AutoContinueSettings`, `resolve_settings()` — читает `kanban.auto_continue` /
  `kanban.auto_continue_max_splits`. Нечитаемый конфиг → **документированный дефолт** (включено),
  а не «выключено»: правило владельца в том, что карточка не паркуется из-за счётчика, а сломанный
  конфиг — не доказательство, что автоматику отключили.
* `ineligible_reason(outcome, extra)` — что НЕ является «чистым» провалом счётчика:
  `terminal_provider`, `sticky` (системная волна одинаковых ошибок), `infrastructure`,
  а также любой outcome вне `{timed_out, crashed, reclaimed}` — в частности `spawn_failed`
  (хост не поднял воркер; ни продолжение, ни сплит тут не помогут).
* `plan_after_failure(...)` — **чистая** функция решения: `block` / `continue` / `split` /
  `block+auto_continue_exhausted`. Без БД и без LLM, поэтому таблица решений тестируется напрямую.
* `apply_trip(...)` — исполняет решение в той же write-txn, что и счётчик. Каждая ветка закрывает
  попытку своим исходом (`timed_out`/`crashed`), поэтому история прогонов честная: `gave_up`
  появляется только там, где карточка действительно запаркована.
* `run_split_phase(...)` — сам сплит, **вне** write-txn (это вызов aux-LLM), из тика диспетчера.
  При любой ошибке декомпозера — `_mark_split_unavailable` → карточка продолжается.

**Состояние — только в `task_events`**, без миграции БД:

* `auto_split` — сколько поколений сплита уже сделано (в payload `generation`, `child_ids`);
* `auto_continued` — продолжения в текущем поколении (в payload `reason`, `attempt`);
* `auto_split_queued` — «сплит заказан, следующий тик исполнит». Это замена флаговой колонке,
  которую падение воркера могло бы оставить висеть: событие переживает рестарт.

**`hermes_cli/kanban_db_dispatch.py`**: `_record_task_failure` делегирует решение в
`_kac.plan_for_trip` / `_kac.apply_trip`. В `_dispatch_once_locked` добавлена фаза
`_kac.run_split_phase(conn)` — после reclaim/crash, **до** расчёта spawn-бюджета (сплит двигает
родителя в `todo`, детей в `ready`, что меняет, кого тик может запустить); при `dry_run` фаза
не выполняется, чтобы проба не создавала реальных карточек. `DispatchResult` получил поля
`auto_split` / `auto_split_failed`.

**`hermes_cli/kanban_db_graph.py`**: `decompose_triage_task` принимает `source_statuses`
(по умолчанию `("triage",)`; для сплита — `("ready","todo","blocked")`) и `allow_redecompose`
(повторный сплит — новое поколение детей над той же карточкой, тогда как triage-карточка
декомпозируется ровно один раз). Проверка статуса осталась optimistic-concurrency стражем,
а не политикой: политику уже принял вызывающий, а карточка, уехавшая дальше, не перезаписывается.

**`hermes_cli/kanban_decompose.py`** → `decompose_failed_card(...)`: тот же контракт, что у
`decompose_task` — ожидаемые отказы (нет aux-клиента, ошибка API, битый JSON, нет детей) приходят
как `ok=False` с причиной, **не исключением**. `_stamp_progress_locator` добавляет каждому ребёнку
абсолютный локатор дерева родителя и `PROGRESS.md` — иначе новый воркер начинает с нуля и сжигает
тот же бюджет. `fanout=false` от декомпозера отвергается: карточку, сжёгшую бюджет, нельзя
«утрамбовать» прозой.

## 3. Критерии приёмки — состояние

| # | Критерий | Как проверено |
|---|---|---|
| 1 | Чистый счётчик → `ready`/сплит, не `blocked`; `gave_up` отсутствует | `test_counter_exhaustion_returns_card_to_ready_without_gave_up` (статус, `block_kind is None`, `gave_up not in kinds`, payload `auto_continued.reason=counter_exhausted`, `attempt=1`); строка в `task_events` — сам payload |
| 2 | Повторный провал → дочерние карточки, связи parent→child, родитель в `todo`, `auto_split` с id детей | `test_second_burn_splits_card_into_linked_children` (`kb.child_ids` = 2, `kb.parent_ids(cid) == [tid]`, статус `todo`, `payload["child_ids"]`, `generation == 1`) |
| 3 | `auto_continue_max_splits` соблюдается; на N+1 → `blocked` + `auto_continue_exhausted` | `test_split_budget_is_bounded_and_blocks_with_exhausted_reason` (+ `recompute_ready` не воскрешает: `block_kind='needs_input'` делает блок липким) и `test_exhausted_block_survives_dispatcher_recompute` |
| 4 | Регрессий нет: `rate_limited`, `host_interrupted`, `owner_lost`, `infrastructure`; `auto_continue: false` | `test_rate_limited_requeue_counts_no_failure`, `test_non_card_causes_keep_the_old_block` (3 параметра: infrastructure / terminal_provider / sticky), `test_infrastructure_spawn_failure_is_not_counted_twice`, `test_auto_continue_false_restores_blocked_and_gave_up`, плюс `tests/hermes_cli/test_kanban_owner_lost_budget.py` (оба теста, включая новый `..._blocks_when_auto_continue_is_disabled`) |
| 5 | Декомпозер недоступен → `ready`, работа не потеряна, причина в журнале | `test_unavailable_decomposer_continues_card_and_records_reason` (статус `ready`, `workspace_path` не изменился, `reason=split_unavailable`, `split_failures == 1`); плюс `test_repeated_decomposer_outage_is_bounded` — постоянный отказ не превращается в вечный цикл |
| 6 | Минимум 7 сценариев, все зелёные | 15 тестов в `test_kanban_auto_continue.py` + 5 в `test_kanban_cli_failure_limit_config.py` — **20 passed**; дополнительно поколение 0 (`test_generation_zero_continues_before_any_split`) и таблица решений (`test_plan_modes_are_pure_and_ordered`) |
| 7 | `code_health --base HEAD` 0 blocking; `ruff` чист | см. §5 |
| 8 | Расхождение `failure_limit: 3` vs `DEFAULT_FAILURE_LIMIT = 2` | **устранено**, см. §4 |

## 4. Пункт 8: расхождение `failure_limit` — найдено и устранено

**Причина.** Не «конфиг не доходит до диспетчера» вообще, а конкретный путь: CLI
`hermes kanban dispatch` (и устаревший `hermes kanban daemon`) передавали в `dispatch_once`
жёсткий `kbd.DEFAULT_FAILURE_LIMIT` (=2), **не читая** `kanban.failure_limit`. Gateway-путь
(`gateway/kanban_watchers_dispatcher.py::_resolve_dispatcher_settings`) конфиг читал корректно —
поэтому в журнале и видны `effective_limit=2, limit_source=dispatcher`: те провалы шли через CLI-путь.

**Исправление:**

* `hermes_cli/kanban_ops.py::_resolve_failure_limit(cli_value, config_value)` — приоритет
  `--failure-limit` > `kanban.failure_limit` > `DEFAULT_FAILURE_LIMIT`; значения < 1 проваливаются
  дальше (тот же sanity floor, что у гейтвея).
* `_kanban_failure_limit_setting()` — читает `kanban.failure_limit` через публичный `load_config()`.
* `hermes_cli/kanban_parser.py` — дефолт `--failure-limit` переведён с `kbd.DEFAULT_FAILURE_LIMIT`
  на `None`, иначе флаг с дефолтом маскировал бы конфиг точно так же.
* `kanban daemon` — тот же хелпер.

**Живая проверка** (реальный `config.yaml`, не заглушка):

```
HERMES_HOME=/home/hermes/.hermes  budget config:
  _kanban_failure_limit_setting()      = 3
  _resolve_failure_limit(None, 3)      = 3
  _resolve_failure_limit(1, 3)         = 1
  _resolve_failure_limit(None, None)   = 2   (= DEFAULT_FAILURE_LIMIT)
  _resolve_failure_limit(0, 0)         = 2   (значения <1 проваливаются)
```

Тесты: `tests/hermes_cli/test_kanban_cli_failure_limit_config.py` — 5 passed (конфиг доходит,
флаг побеждает, отсутствующий конфиг и нечитаемый конфиг дают дефолт, значения <1 проваливаются).

**Важное уточнение по среде:** у профиля `03-software-engineer` свой `config.yaml` с
`kanban.failure_limit: 2` — это другой конфиг, не корневой. Когда диспетчер работает под
корневым `HERMES_HOME=/home/hermes/.hermes`, действует 3. Расхождение в корневом конфиге
устранено кодом; отдельного «молчаливого дефолта» больше нет ни на одном пути.

## 5. Проверки

* `tests/hermes_cli/test_kanban_auto_continue.py` — **15 passed**
* `tests/hermes_cli/test_kanban_cli_failure_limit_config.py` — **5 passed**
* `tests/hermes_cli/test_kanban_db.py` — 30 passed, 1 skipped (обновлён
  `test_stale_claim_reclaim_without_spawn_counts_toward_breaker`: теперь `ready` + `auto_continued`)
* `tests/hermes_cli/test_kanban_owner_lost_budget.py` — обновлён под новую политику + новый тест
  `test_a_plain_crash_blocks_when_auto_continue_is_disabled`
* `python -m scripts.code_health --base HEAD` → **0 blocking, 0 advisory** (8 файлов)
* `ruff check` по всем изменённым файлам → **All checks passed** (один автофикс сортировки `__all__`)
* Ratchet `FILE_LINES` по `hermes_cli/config_defaults.py` (файл был 3139 при пределе 3138) устранён
  сжатием комментариев до 3138 — новый код в фасад не добавлялся.

**Известный шум окружения (не регрессия).** `tests/hermes_cli/test_kanban_db.py` завершается
ненулевым кодом при 30 зелёных тестах: `tests/home_io_guard.py` ругается на путь тест-venv
`~/.hermes/installs/.../site-packages/_pytest/main.py`. **Воспроизведено на базе** (изменения
отложены, прогон тот же) — страж окружения, а не поведение карточки.

## 6. Ограничения и что осталось человеку (`manual_review`)

* **Не проверено на живом воркере с настоящим бюджетом.** Все тесты — на изолированной БД и
  подменённом декомпозере (никакой сети). Живая карточка, реально сжёгшая `agent.max_turns`,
  должна дать `auto_continued` в `hermes kanban show` — это `manual_review` после перезапуска шлюза.
* **Windows-путь не проверялся** (только Linux).
* **Aux-LLM декомпозер вживую не вызывался** — контракт «ожидаемые отказы как `ok=False`»
  проверен стабами, реальный вызов `decompose_failed_card` с живой моделью не прогонялся.
* Схема БД не менялась — состояние живёт в `task_events`, миграций нет.

## 7. Инцидент по пути (фиксируется честно)

`refs/stash` в git общий на весь репозиторий (не на worktree). Я сделал `git stash push` для пробы
базы, а `git stash pop` притянул **чужой** WIP карточки `t_afdf38ac`
(`gateway/session_worker_construct.py`, `tests/gateway/test_named_route_applies_card_key.py`).
Чужие файлы из моего дерева убраны (`git reset` + `git checkout --` + `rm`); чужой патч целиком
сохранён в `stash@{1}` и не удалялся — ничего не потеряно.
**Урок для следующих попыток:** в worktree при параллельных карточках не пользоваться `git stash`;
для пробы базы брать `git worktree add` или `git show HEAD:<file>`.
