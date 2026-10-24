# REFACTOR-CHANGELOG.md — единый движок доставки канбан-событий (R1)

**Миссия:** «РЕФАКТОРИНГ ДОСТАВКИ СООБЩЕНИЙ» (владелец, 2026-10-20)
**Карточки:** R0 = t_8ac0df0d (анализ + дизайн, REFACTOR-DESIGN.md); R1 = t_084099bf (реализация, задачи 0–2: t_a53f9abe, t_7485fb76, t_a008a64b; приёмка = t_9ab15e9b). R2 = перезагрузка гейтвея (02-hermes-platform).
**Ревизия кода:** рабочий каталог `C:\Users\hermes\AppData\Local\hermes\hermes-agent`, git HEAD `f3cf748cebe9f000dde75c95b215ef35a32fb7a1` (2026-10-20). R1-набор НЕ закоммичен — это промежуточная точка перед ревью точной ревизии и коммитом (15-sre-qa).

---

## Файл → что изменено → как откатить

| Файл | Статус | Что изменено | Откат |
|---|---|---|---|
| `gateway/delivery_engine.py` | **новый** (untracked, 1226 строк) | Единственный модуль политики доставки. `classify_event()` — единственная точка классификации, ровно 4 типа `[live-status]`/`[final]`/`[needs-decision]`/`[aborted]`; всё прочее → None (тишина) в одном месте. `DeliveryEngine.handle_event()` — единая точка обработки события подписки: classify → render → dual-lane dispatch → wake; курсоры (advance/rewind) — через инжектируемые callables. `ensure_live_status()` — edit-in-place по сохранённому message_id, бэкофф 30с→×2→кап 900с (`_LIVE_INTERVAL_S=30`, `_LIVE_INTERVAL_CAP_S=900`), рендер без LLM (`render_live_status`), `close_live_status` на [final]/[aborted]. `ENGINE_CLAIM_KINDS` — расширенный claim-набор (добавлены spawned/claimed для live-status). `handle_branch_final()` — [final] для закрытой ветки (branch-summary watcher), F1-контракт: итог несёт summary+артефакты. `handle_cron_alert()` — cron alert-once как [aborted]/[needs-decision] на единой ленте. `dedup_seen/dedup_mark/dedup_markers` — persisted (task_id, kind, cause_hash). `log_suppression()` — обёртка единственной точки журнала | `git rm --cached gateway/delivery_engine.py` (не в HEAD); физически удалить файл |
| `gateway/kanban_watchers.py` | изменён (-2785 строк) | Коллектор переключён на движок: нотификатор вызывает `engine.handle_event()` единым вызовом вместо веток kind-рендера + policy-гейта + dual-lane (дизайн §г шаг 3.1). branch-summary watcher → `engine.handle_branch_final()`. Удалены legacy: `_tg_notify_policy_decision`, `_tg_owui_dual_lane`, F2 block-mirror (`_owui_tg_block_mirror_*`), F6-гейт отдельной веткой, bare-itog (`_owui_tg_is_bare_itog`), `should_suppress_api_server_event`, `should_wake_agent_for_event`, `_owui_suppress_intermediate_event`, `branch_summary_replace` + обе ветки замени, `_owui_branch_result_text` (рендер [final] — в движке). `_task_do_not_deliver` сохранён | `git checkout -- gateway/kanban_watchers.py` (к HEAD f3cf748) |
| `hermes_cli/delivery_policy.py` | изменён (-326 строк) | Остались только: `sanitize_delivery_text` (+`_strip_think_blocks`/`_condense_interim`/`_INTERIM_STAMPS`), `verify_recipient`/`verify_subscription_recipient` (ИНВ1), `owui_telegram_users`+`tg_bindings.json` (F3, НЕ тронут), `PolicyDecision`, `log_suppression` (единственная точка журнала). Удалены: `classify()` с контент-эвристиками §1–§7, `detect_system_alert`, `is_routine_report`, `telegram_delivery_policy` флаг + `policy_enabled()`, ROUTINE/HUMAN kinds text-based, `_LOW_STAMPS`/`_ALERT_PREFIXES`/`_FAIL_TEXT_STAMPS` | `git checkout -- hermes_cli/delivery_policy.py` |
| `hermes_cli/kanban_db.py` | изменён (+158 строк) | Добавлены таблицы `delivery_live_status` (PK task_id: platform/chat_id/thread_id/message_id/interval_s/next_update_at/closed/updated_at) и `delivery_dedup` (PK task_id,kind,cause_hash) в каноническую схему; API `get_live_status`/`upsert_live_status`/`set_live_status_message_id`/`close_live_status`/`ensure_dedup_table` | `git checkout -- hermes_cli/kanban_db.py` (таблицы аддитивны — код-откат их не требует) |
| `cron/scheduler.py` | изменён (+386) | Cron alert-once (F4, решение владельца 2026-10-18) вписан в движок: алерты маппятся структурно в [aborted]/[needs-decision] и доставляются через `engine.handle_cron_alert()` (единая лента, НЕ отдельной веткой). Persisted-биты alert-once и классы `_FAILURE_ALERT_CLASSES` сохранены; cause_hash=execution_id per-run. Контент-детект (detect_system_alert/classify) удалён. Обычный cron-вывод — по-прежнему через `delivery.py` (не канбан-политика) | `git checkout -- cron/scheduler.py` |
| `gateway/platforms/api_server.py` | **НЕ R1** | Рабочий diff (+14) — это авторские правки СМЕЖНОЙ миссии RGZ (reasoning= max при RGZ-триггере, `agent/rgz_reasoning.py`). Сопряжения OWUI-лейны с движком тут НЕТ (движок вызывает stage/outbox из `kanban_watchers.py`); R1 не менял этот файл. | **НЕ включать в откат R1** — откат затрёт чужую миссию |
| `gateway/run.py` | **НЕ R1** | Рабочий diff (+29) — те же авторские RGZ-правки (спайка reasoning= max в TurnRunner/GatewayRunner). Грепом движка в run.py — 0 совпадений (спайка движка целиком в kanban_watchers.py, по дизайну §(г) «run.py не трогаем»). R1 не менял этот файл. | **НЕ включать в откат R1** — откат затрёт чужую миссию |

**Полный откат (только файлы R1):**
```
git checkout -- gateway/kanban_watchers.py hermes_cli/delivery_policy.py hermes_cli/kanban_db.py cron/scheduler.py
rm -f gateway/delivery_engine.py
```
> ⚠️ НЕ включать в откат `gateway/run.py` и `gateway/platforms/api_server.py` — их рабочие diffs принадлежат смежной миссии RGZ (правки reasoning= max), R1 их не менял; `git checkout --` по ним затрёт чужую незакоммиченную работу.
**Stash-точки до правок:** `git stash@{0}` и `git stash@{1}` — «pre-validated-resume-live-integration*» (сохранены до начала R1). Восстановление: `git stash apply stash@{1}` (при конфликтах — по файлам из таблицы).
**Проверка после отката:** `git diff --stat` по файлам R1 → пусто; `git status --short` не показывает gateway/delivery_engine.py; `python -m py_compile <модули>` проходит.

---

## R2 (t_702c62f2, 2026-10-21): перезагрузка гейтвея + smoke нового движка

**Что сделано в R2:**
1. **Контролируемый fast-restart гейтвея 01-orchestrator** через маркер `.gateway-fast-restart-request.json` (target_pid, ISO-UTC written_at, TTL 60с) — контракт `gateway/status.py`. Старый PID 47964 (запущен 2026-10-20 19:23, до правок R1 в 00:01) → новый PID 60448 → (после R2-фикса) PID 52364.
2. **Найден и исправлен интеграционный дефект R1** в `gateway/kanban_watchers.py`: коллектор подписок вызывал `claim_unseen_events_for_sub(kinds=TERMINAL_KINDS)`, где `TERMINAL_KINDS` НЕ содержит `spawned`/`claimed`. В результате live-status события (`[live-status]` по классификатору движка) никогда не доставлялись `DeliveryEngine`, и `delivery_live_status` оставался пустым. **Фикс:** используется `ENGINE_CLAIM_KINDS` (расширенный набор движка = TERMINAL + spawned + claimed), импорт поднят в начало `_deliver_notifications`. Бэкап: `gateway/kanban_watchers.py.bak-t_702c62f2-20261021-041823`.
3. **Smoke-проверка live-status** (карточка-песочница t_2c9754ec, do_not_deliver=0, OWUI-подписчик):
   - ✅ live-status row создаётся (`delivery_live_status`, PK task_id, edit-in-place)
   - ✅ интервалы удваиваются 30→60→120→240 (бэкофф движка `_next_interval`)
   - ✅ закреплено на ≥2 тиках + дополнительный `status→running` тик (240)
   - ✅ [final] для одиночной карточки обрабатывается branch-summary watcher → `state=unbound` для синтетического OWUI-канала (ожидаемо, канал не в реестре); реальные каналы stage'ятся в delivery-outbox (acked)
   - ✅ completed промежуточного узла (с живыми детьми) → `silenced (intermediate close)`, без спама — rule 3 работает
   - ✅ journal `delivery-policy-suppressed.log` функционален (single point `log_suppression`)
4. **Песочница убрана**: archived + do_not_deliver=1, live-status row закрыт, синтетическая подписка удалена.

**Состояние после R2:** гейтвей 01-orchestrator PID 52364, порт 8642, 3 платформы connected, чистый старт (без ошибок импорта новых модулей; прежний `name 'ev_id' is not defined` в OWUI-stage больше не воспроизводится).

**Откат R2:** `git checkout -- gateway/kanban_watchers.py` (покрывает и R1, и R2-фикс); бэкап `gateway/kanban_watchers.py.bak-t_702c62f2-20261021-041823` удалить не нужно (откат вернёт к HEAD f3cf748).

**Тестовые артефакты:** чекпоинт `C:\Users\hermes\AppData\Local\hermes\durable-results\t_702c62f2\status.json`.

> Примечание: в рабочем дереве есть и посторонние незакоммиченные изменения (contributors/, bootstrap-installer/, .envrc и др.) — это локальное состояние, НЕ часть R1. При откате НЕ трогать их.

---

## Legacy-демонтаж (из списка R0 §в) — grep-контроль

Канонический grep Шага 5:
`grep -rn "branch_summary_replace|should_suppress_api_server_event|should_wake_agent_for_event|_tg_owui_dual_lane|_owui_tg_block_mirror|policy_enabled|detect_system_alert|is_routine_report|bare_itog" gateway/ hermes_cli/delivery_policy.py cron/`

**Результат: 0 совпадений в коде.** Остались только 4 docstring-ссылки, документирующие удаление (не код):
- `gateway/kanban_watchers.py:982` — комментарий «branch_summary_replace semantics are the SINGLE behavior»
- `cron/scheduler.py:397` — docstring «detect_system_alert removed»
- `cron/scheduler.py:3112`, `:3116` — docstring «telegram_delivery_policy flag/policy_enabled() is GONE»

Расширенный список R0(в) (дополнительно): `_branch_summary_terminal` и `_INTERIM_STAMPS` сохранены (предикат терминальности и санитайзер — по дизайну); `telegram_delivery_policy` — 1 docstring-ссылка (scheduler.py:3111).

---

## Критерии приёмки R1 — итог

| # | Критерий | Статус | Доказательство |
|---|---|---|---|
| 1 | Все пункты дизайна б–г реализованы | ✅ | delivery_engine.py (1226 стр.): 4 типа событий; classify_event единственная точка; live-status edit-in-place бэкофф 30→900; final/needs-decision/aborted dual-lane; triage по kind И card.status; wake только [final]+[needs-decision]; idempotency (delivery_live_status PK, delivery_dedup persisted); cron alert-once в движке как [aborted]/[needs-decision]; openwebui_telegram_users/verify_recipient/sanitize не тронуты; журнал подавлений — одна точка log_suppression |
| 2 | grep по списку legacy R0(в) — отсутствие | ✅ | 0 совпадений в коде; 4 docstring-ссылки (список выше с файлами) |
| 3 | py_compile/import новых и изменённых модулей | ✅ | `python -m py_compile` по 7 модулям (delivery_engine, kanban_watchers, delivery_policy, kanban_db, scheduler, api_server, run) → PY_COMPILE_OK; `import` всех 7 → IMPORT_OK; `classify_event`/`render_live_status`/`log_suppression`/`DeliveryEngine` присутствуют |
| 4 | REFACTOR-CHANGELOG.md существует, все разделы | ✅ | этот файл: файл → что изменено → как откатить (таблица выше), git diff/stash-точки, legacy-контроль, статус приёмки |
| 5 | completion metadata: changed_files[], «готово к рестарту» = true | ✅ | в kanban_complete задачи t_9ab15e9b |

**Замечания:**
- Гейтвей НЕ перезагружен (R2 — отдельная карточка 02-hermes-platform). Код лежит на диске и активируется рестартом.
- `openwebui_telegram_users` + механизм TG↔OWUI (F3) — не тронуты.
- Класс «интерактивная подсказка» (таймаут быстрого режима, `agent/fast_budget_text.py`) — вне канбан-доставки (OWUI pill + footer через conversation_loop/turn_finalizer); движком не подавляется (дополнение владельца 2026-10-20 соблюдено).
- Тест-простыни не писались; проверка — py_compile/import + grep + эмпирическая проверка идемпотентности рестарта (задача t_a008a64b).
- R1-набор не закоммичен; коммит + ревью точной ревизии — следующий шаг (15-sre-qa).
- **Уточнение changed-files (проверка R1-родителя 2026-10-21):** `gateway/run.py` и `gateway/platforms/api_server.py` в рабочем дереве имеют diffs СМЕЖНОЙ миссии RGZ (reasoning= max, `agent/rgz_reasoning.py`, untracked) — НЕ R1. Спайка движка целиком в `gateway/kanban_watchers.py` (дизайн §(г) «run.py не трогаем»). Откат R1 их не включает (таблица выше).

---

## Продолжение миссии: [needs-decision] → TG автора (t_637df501, 2026-10-24)

**Задача:** правило 4 владельца — «Ситуации, где пользователю нужно что-то решить,
всегда доводятся до пользователя — и в канал постановки, и в Telegram; карточка
в triage часами без уведомления недопустима». Остаток миссии после R0/R1/R2:
достроить TG-ноку для [needs-decision] автору задачи.

### Найденная первопричина (по живым данным, 2026-10-24)

Движок (R1) уже имел dual-lane для [needs-decision] и «Требует решения»-рендер
(`render_needs_decision`). Но в `DeliveryEngine.handle_event` OWUI-лейна была
**жёстко связана с гейтом ACK**: при `stage_owui` → `"pending"` (нормальное
состояние для открытой blocked/triage карточки — outbox-строка ждёт серверный
readback, который для открытой карточки может приходить с задержкой или не
приходить вовсе) движок делал `return action="rewind"` **ДО** выполнения
TG-лейны и wake. Живой след в production-логах:

```
2026-10-24 03:32:38 INFO gateway.run: kanban notifier: t_4bff16dc/blocked rewound by delivery engine (owui stage pending)
2026-10-23 23:38:00 INFO gateway.run: kanban notifier: t_b7928d99/block_loop_detected rewound by delivery engine (owui stage pending)
2026-10-23 22:04:10 INFO gateway.run: kanban notifier: t_b7928d99/blocked rewound by delivery engine (owui stage pending)
```

Пока OWUI-плечо не заACKилось, TG-нока автору НЕ уходила — карточка могла висеть
в triage часами без уведомления (прямое нарушение правила 4). Существующие F2-тесты
этот путь не покрывали: их `_staged_sink` всегда возвращал `"acknowledged"`.

### Изменение (gateway/delivery_engine.py)

**Принцип:** гейт ACK OWUI-плеча владеет ТОЛЬКО курсором OWUI (rewind-to-retry
до readback), но никогда не голодает комплементарные лейны (TG + wake). Логика
`handle_event`:

1. OWUI-стейдж выполняется; `pending`/`error` больше **не** делают ранний return —
   фиксируется `owui_rewind` (причина для re-claim).
2. TG-лейна и wake выполняются безусловно (как и для acked-пути).
3. В конце: если `owui_rewind` установлен — возвращается `action="rewind"`
   (событие переустанавливается, outbox-строка дожидается ACK на следующем drain;
   bounded by MAX_API_STAGE_RETRIES=8 в watcher — permanently stuck дропается).
   Иначе — `action="advance"`.
4. `failed`/`unbound` (постоянные: нет сервер-авторизованного origin / битый контент;
   `enqueue` бросает детерминированный ValueError) трактуются как `advance`
   (fail-closed сохранён: никакой доставки чужим; повтор бесполезен — не спин).
   Раньше они тоже возвращали rewind и крутились до дропа через 8 попыток.

**Идемпотентность retry-цикла:** на следующем переустановленном тике TG проверяет
`dedup_seen` (уже помечен после первого успешного send → повтор не шлётся), wake
для api_server — no-op («outbox persist IS the wake», возвращает True). Повторный
`stage_owui` idempotenten (`INSERT OR IGNORE` возвращает ту же pending-строку).

**Точка классификации НЕ изменилась:** `classify_event()` остаётся единственной
точкой решения «доставлять или молчать»; изменился только порядок внутри
`handle_event` (движок доставляет [needs-decision] всегда, немедленно, в обе лейны).

### Тест (tests/gateway/test_kanban_needs_decision_tg_while_pending_t637df501.py)

- `test_needs_decision_reaches_author_tg_while_owui_pending` — регрессионный тест:
  api_server-подписка, bound-автор, OWUI-стейдж возвращает `"pending"` → TG-лейна
  ДОЛЖНА сработать (отправить «Требуется решение» с заголовком+причиной). До фикса
  тест падал (`tg_texts == []`) — точное воспроизведение production-гэпа.

### Откат (t_637df501)

| Файл | Откат |
|---|---|
| `gateway/delivery_engine.py` | удалить `owui_rewind`-блоки и вернуть ранние `return` на `pending`/`error` в OWUI-лейне `handle_event` |
| `tests/gateway/test_kanban_needs_decision_tg_while_pending_t637df501.py` | `git rm` (новый файл) |

### Статус приёмки (критерии карточки t_637df501)

| № | Критерий | Статус |
|---|---|---|
| 1 | [needs-decision] доставляется автору в Telegram (живой след) | ✅ движок доставляет TG-ноку немедленно; регресс-тест зелёный; F2-зеркало для bound-авторов подтверждено |
| 2 | И в канал постановки, и в Telegram (правило 4) — оба плеча | ✅ OWUI-плечо (outbox) + TG-плечо идут независимо; pending OWUI не блокирует TG |
| 3 | Существующая доставка владельцу и live-status/final НЕ регрессировали (smoke) | ✅ независимый QA-замер (т_388325e4, 2026-10-24 06:15 MSK): regress-тест 1 + resolve_author 10 + mission-silence + completion + origin + wake_only_ordering + delivery + ledger + silence_filter + telegram_final_delivery + restart_rededup = **132 passed**; тот же пред-существующий фейл-набор, что и на родителе f79bc08d7a (wake-scope x4, wake_mechanics terminal, 2 assignee-ValueError, 2 collection-broken) — не регрессия карточек 1-2 |
| 4 | Код без дублей с цепочкой OWUI (не пересекайся) | ✅ правка только в `gateway/delivery_engine.py` + новый тест; `kanban_watchers.py` (горячая точка OWUI-цепочки) НЕ тронут |
| 5 | REFACTOR-CHANGELOG.md дополнен; классификация осталась единственной | ✅ этот раздел; `classify_event` не изменён |

### Инструментальная приёмка (карточка t_388325e4 QA, 2026-10-24 06:15 MSK)

- **Живой след (правило 4) — end-to-end на боевой подписке.** Реальный боевой
  сабскрипшн `kanban_notify_subs` (t_4bff16dc, platform=api_server, owner
  9ae3eca0-...) + реальная серверная привязка `openwebui_telegram_users`
  (01-orchestrator/config.yaml: 9ae3eca0→125805788) пропущены через
  `DeliveryEngine.handle_event` на закоммиченном HEAD (68ebcb91c0) с OWUI-стаджем
  `pending` (реалистичное состояние открытой blocked/triage-карточки):
  - `classify_event` → `needs-decision`;
  - OWUI-лейна (канал постановки): outbox-строка staged (`pending`);
  - TG-лейна: сообщение **«⛔ Требуется решение: [blocked] …» отправлено в
    telegram:125805788** (боевой канал автора);
  - wake: сработал (1);
  - cursor: `rewind` (OWUI cursor repin, bounded MAX_API_STAGE_RETRIES=8).
  Итог: PASS. Вторая боевая привязка (d0390fe5→267988228) также резолвится.
- **Единственность классификации.** Правки карточек 1-2 (`f79bc08d7a..68ebcb91c0`)
  не трогают `classify_event` (diff=0); в репозитории 1 call-site классификации
  событий доставки — `gateway/delivery_engine.py:1050`. Дубли с OWUI-цепочкой
  отсутствуют: diff карточек = `delivery_engine.py` + 2 теста; `kanban_watchers.py`,
  fast-lane, api_server НЕ изменены. Резолвер автора — единая точка
  `resolve_author_telegram`, все вызовы (вкл. legacy `_tg_bound_chat_for`) через него.
- **Smoke-регрессии:** 132 passed широкого affected-набора (список выше).
  Пред-существующие фейлы подтверждены идентичными на родителе f79bc08d7a
  (независимый прогон в worktree C:/tmp/rereview-f79bc08d7a) — не регрессия карточек.
- **Замечание внедрения (manual_review для SRE):** боевой gateway 01-orchestrator
  (PID 9676/9872) запущен 2026-10-23 13:20 MSK — до коммитов карточек 1-2
  (05:48/06:04 MSK). Активный процесс работает со старым кодом; для активации
  фикса требуется рестарт gateway (релизный шаг 15-sre-qa). Живых
  needs-decision-событий после 06:04 в prod-БД нет (последнее t_4bff16dc 03:32 —
  до фикса), поэтому end-to-end-доказательство выполнено на боевой подписке
  программно, а не по реальному prod-сообщению.
