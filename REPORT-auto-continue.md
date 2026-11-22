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
тот же бюджет. **Оговорка:** живая проба наследование `PROGRESS.md` не подтвердила — у root-карточки
дерево не было материализовано, поэтому проверялся только вид локатора. Код в этом случае
подставляет честную заглушку вместо несуществующего пути; фактическое чтение `PROGRESS.md` новым
воркером остаётся непроверенным (`manual_review`). `fanout=false` от декомпозера отвергается: карточку, сжёгшую бюджет, нельзя
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
| 8 | Расхождение `failure_limit: 3` vs `DEFAULT_FAILURE_LIMIT = 2` | **устранено** на CLI/gateway и путях спавна (§4); остаток на путях таймаута/краха закрыт в `t_63278be8` (см. `REPORT-fixes.md` §1) |

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
устранено на CLI/gateway-пути и на путях спавна. **Оговорка (доработка `t_63278be8`, дефект Д-1):**
два sweep-пути — `enforce_max_runtime` (таймаут) и `_account_crashes` (крах) — по-прежнему бронировали
`DEFAULT_FAILURE_LIMIT`, поэтому утверждение «ни на одном пути» было неверным и снято. Эти два пути
закрыты отдельной правкой, см. `REPORT-fixes.md` §1; на ревизии до неё конфиг 3 работал как 2 именно там.

## 4a. Замечание оркестратора 22.11: исчерпание пула карточных ключей — тоже не провал

Оркестратор нашёл в журнале `t_e4cf9f2a`: воркер не смог стартовать
(`agent.kanban_key_pool.KeyPoolError: no free card key slot: every card key is full at 1 card(s)
per key (#2 1/1, #3 1/1, …)`), диспетчер записал это как `crashed` c `exit_code 1` и сжёг бюджет
повторов. Замечание принято — это ожидание ресурса, а не падение работы; карточка к тому же не
начала работу.

Реализовано в этой же ветке:

* `agent/kanban_key_pool_errors.py` — типы вынесены в сиблинг (фасад `kanban_key_pool.py` уже за
  ratchet'ом по строкам); `KeyPoolError` остаётся базой, добавлены `KeyPoolSlotBusy`
  («все карточные слоты заняты») и `KeyPoolWait` (таймаут файловой блокировки пула).
  `agent.kanban_key_pool.KeyPoolError` по-прежнему единственный путь импорта для вызывающих.
* `hermes_cli/turn_exit.py::credential_failure_flags` — `KeyPoolSlotBusy` в цепочке причин даёт
  `credentials_rate_limited: True` → воркер выходит кодом 75, и диспетчер возвращает карточку в
  `ready` **без инкремента** `consecutive_failures` и без `gave_up` (тот же путь, что у quota wall).
* Простой `KeyPoolError` (пул не включён, ключ отвергнут провайдером на ходу) остаётся `crashed`:
  повтор его не лечит, и выдавать его за контеншн нельзя.

Тест: `tests/gateway/test_kanban_result_exit.py::test_key_pool_slot_exhaustion_does_not_charge_the_card`
(4 параметра: busy → 75, обёрнутая busy → 75, «пул не включён» → 1, «ключ отвергнут» → 1) —
**25 passed** в файле.

## 5. Проверки

**Целевые наборы (все зелёные):**

* `tests/hermes_cli/test_kanban_auto_continue.py` — **15 passed**
* `tests/hermes_cli/test_kanban_cli_failure_limit_config.py` — **5 passed**
* `tests/gateway/test_kanban_result_exit.py` — **25 passed** (включая 4 новых параметра про пул ключей)
* `tests/hermes_cli/test_kanban_restart_interrupt_budget.py` — **7 passed** (обновлён тест «обычный
  краш ещё считается»: провал по-прежнему инкрементится, но трип даёт `ready` + `auto_continued`;
  добавлен парный тест `..._blocks_when_auto_continue_is_off` — `blocked` + `gave_up`)

**Широкий регресс по канбану** (`tests/hermes_cli/test_kanban*.py`, `tests/gateway/test_kanban*.py`,
`tests/agent/test_kanban*.py` — 122 файла, раннер `scripts/run_tests.sh`, изолированный
`HERMES_HOME`):

```
=== Summary: 122 files, 1015 tests passed, 9 failed, 3 files CRASHED, 4 skipped (237 с, 8 workers) ===
```

*Все 9 отказавших тестов и все 3 «CRASHED» воспроизводятся на базе `3bbf87774c` без моих правок*
(прогнал тот же набор против `git archive 3bbf87774c` в отдельном каталоге — тот же 9 failed):

* `test_kanban_test_board_isolation.py`, `test_kanban_creator_origin.py[cli]`,
  `test_kanban_worker_socket_recovery.py` (6), `test_kanban_worker_restart_wait.py` — 9 failed на базе
  и на ветке **одинаково** (в этих файлах я не менял ни строки; причины — окружение прогона:
  живая доска, `notify+wake` вместо `wake`, GatewayClientError).
* 3 файла «interpreter killed by SIGTERM» (`test_kanban_owner_lost_budget.py`,
  `test_kanban_anonymous_card_fuse.py`, `test_kanban_dispatch_claim_allowlist.py`) — на ветке при
  одиночном прогоне: **29 passed**, то есть это гонка раннера под 8 воркерами, а не результат правки.
* 16 файлов «все тесты прошли, но pytest вышел ненулевым» — `tests/home_io_guard.py` ругается на путь
  тест-venv (`~/.hermes/installs/.../site-packages/_pytest/main.py`) при разборе traceback. Тот же шум
  воспроизводится на базе; лечится изолированным `HERMES_HOME` + прямым `python -m pytest`, и в этом
  режиме те же файлы дают «N passed» с кодом 0.

**Линт / health:**

* `python -m scripts.code_health --base HEAD` → **0 blocking, 0 advisory**
  (на финальном коммите — «1 files vs d8e2a4a634: 0 blocking, 0 advisory»).
* `ruff check` (пин из `pyproject.toml`) по изменённым файлам: **All checks passed**, кроме
  предсуществующих 71 находки в `agent/kanban_key_pool.py` (`UP006`/`RUF022`/`UP037`/`UP024`,
  питон-3.14-апгрейды старого кода). Доказательство, что это не мой вклад: тот же файл из базы
  `3bbf87774c` даёт **тот же набор 71** (`ruff` по `git show HEAD:agent/kanban_key_pool.py`), а
  `code_health` (который считает дельту) даёт 0 blocking по файлу.
* Ratchet `FILE_LINES` по `agent/kanban_key_pool.py` (фасад был 2068 при пределе 2067 из-за
  добавленного ре-экспорта) — снят в тот же день: вынес типы в `agent/kanban_key_pool_errors.py`,
  фасад импортирует. По `hermes_cli/config_defaults.py` ratchet снят сжатием комментариев.

## 6. Живые пробы на настоящей БД (не тесты, не стабы)

Три прогона в отдельных `HERMES_HOME` на реальном SQLite-борде, с реальным
`detect_crashed_workers` / `dispatch_once` / `run_split_phase` и реальным выводом
`hermes kanban show`.

**Проба 1 — самопродолжение.** Два «мёртвых» прогона одной карточки.

```
attempt 1: detect_crashed_workers -> ['t_ab0a7c8f'] status=ready failures=1
attempt 2: detect_crashed_workers -> ['t_ab0a7c8f'] status=ready failures=2
  auto_continued {"reason":"counter_exhausted","failures":2,"effective_limit":2,
                  "limit_source":"dispatcher","generation":0,"max_splits":2,"attempt":1}
kanban show:  status: ready   Events: … auto_continued {…}      (gave_up отсутствует)
```

**Проба 2 — три подряд краха и недоступный декомпозер** (`AuxiliaryClientUnavailable` в
пустом доме — то есть путь п.5 критерия в живом виде):

```
crash 2: failures=2 auto_events=['auto_continued']
crash 3: failures=3 auto_events=['auto_continued', 'auto_split_queued']
dispatch_once: auto_split= [] auto_split_failed= ['t_903fe108']
  auto_continued {"reason":"split_unavailable","detail":"LLM error: AuxiliaryClientUnavailable",
                  "attempt":2}
parent: {'status': 'ready'}          <- работа не запаркована и не потеряна
```

**Проба 3 — механика сплита** (aux-LLM в одноразовом доме недоступен, поэтому список детей
задан детерминированно; всё остальное — настоящий код: `run_split_phase` → `decompose_triage_task`
→ `task_links` → `todo`-парковка родителя):

```
crash3: {'status': 'ready', 'consecutive_failures': 3}
run_split_phase: split= ['t_cdb9608c'] failed= []
parent: {'id': 't_cdb9608c', 'status': 'todo'}
parent events tail: ['auto_split_queued', 'decomposed', 'auto_split']
  auto_split {'child_ids': ['t_c01510b5', 't_90eeed49'], 'generation': 1}
kanban show:  parents:   t_90eeed49, t_c01510b5
              Comments:  Decomposed into t_c01510b5, t_90eeed49. Root will wake when all
                         children complete.        (обоих детей видно, связи parent→child есть)
```

## 7. Ограничения и что осталось человеку (`manual_review`)

* **Не проверено на живом воркере с настоящим бюджетом.** Живая карточка, реально сжёгшая
  `agent.max_turns`, должна дать `auto_continued` в `hermes kanban show` — но это требует
  выката ветки на шлюз, то есть карточки выката, а не этой. Со стороны кода поведение
  подтверждено пробами 1–3 выше и 15 автотестами.
* **Aux-LLM декомпозер вживую не вызывался**: в одноразовом доме его нет (`AuxiliaryClientUnavailable`),
  и это ровно тот путь, который п.5 требует (продолжить). Сама ветка «LLM вернул детей» проверена
  стабом в `test_second_burn_splits_card_into_linked_children` и в пробе 3 через настоящий
  graph-write.
* **Windows-путь не проверялся** (только Linux).
* Схема БД не менялась — состояние живёт в `task_events`, миграций нет.
* Уточнение по окружению: `kanban_db_path()` в этой версии резолвит доску от `cwd`, а не от `$HOME`
  (коммит `c0e8d0acd2`), поэтому живые пробы пинуют `HERMES_KANBAN_DB` — иначе декомпозер читает
  не ту доску, что видит проба.

## 8. Инцидент по пути (фиксируется честно)

`refs/stash` в git общий на весь репозиторий (не на worktree). Я сделал `git stash push` для пробы
базы, а `git stash pop` притянул **чужой** WIP карточки `t_afdf38ac`
(`gateway/session_worker_construct.py`, `tests/gateway/test_named_route_applies_card_key.py`).
Чужие файлы из моего дерева убраны (`git reset` + `git checkout --` + `rm`); чужой патч целиком
сохранён в `stash@{1}` и не удалялся — ничего не потеряно.
**Урок для следующих попыток:** в worktree при параллельных карточках не пользоваться `git stash`;
для пробы базы брать `git worktree add` или `git show HEAD:<file>`.
