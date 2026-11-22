# t_30b89c61 — `kanban dispatcher stuck`: механизм, правка, доказательства

Дата: 22.11.2026, 02:35–04:10 МСК. Ветка карточки `wt/t_30b89c61`; код правки — `eda4feff3f`, сверху только коммиты этого отчёта.
Рабочий каталог доказательств: `/home/hermes/.hermes/kanban/workspaces/t_30b89c61/evidence/`.

---

## 1. Механизм (файл:строка + запрос к базе)

Тревога `kanban dispatcher stuck: … 0 workers spawned. Check profile health (venv, PATH, credentials)`
не описывает залипание выбора карточки. Карточки **выбираются нормально**; тик останавливает
лимит хоста, а правило тревоги этого не различает.

### Цепочка в коде (прод-коммит `6308ff67cc`, файл `hermes_cli/kanban_db_dispatch.py`)

| шаг | локатор | что происходит |
|---|---|---|
| 1 | `kanban_db_dispatch.py:2447-2451` | `if total_running >= max_in_progress: result.budget_hold = "max_in_progress"; return False, None` — тик останавливается лимитом |
| 2 | `kanban_db_dispatch.py:2598-2602` | `may_spawn, budget = _tick_spawn_budget(...)`; `if not may_spawn: return result` — **lane-циклы не выполняются**, все бакеты `skipped_*` пусты |
| 3 | `gateway/kanban_watchers.py:311` | `bad_ticks = bad_ticks + 1 if ready_pending and not any_spawned else 0` — правило смотрит только на «ready непуст и спавнов 0» |
| 4 | `gateway/kanban_watchers.py:313` | через `_HEALTH_WINDOW = 6` тиков (`:37`) и не чаще раза в 300 с → WARNING с подсказкой про venv/PATH/credentials |

Причина не в `claim_lock`, не в `max_in_progress_per_profile` и не в
`_hold_if_workspace_unready` — все три проверены и чисты (см. ниже).

### Запрос к базе (`evidence/mechanism-db-query.txt`, 03:00)

```
SELECT COUNT(*) FROM tasks WHERE status='running'                          -> 7
SELECT id,assignee,claim_lock FROM tasks WHERE status='ready' AND claim_lock IS NULL
   t_16e161fa 02-hermes-platform None
   t_84ccbf64 02-hermes-platform None
   ... (13 строк, claim_lock у всех NULL)
SELECT assignee, COUNT(*) FROM tasks WHERE status='running' GROUP BY assignee
   02-hermes-platform 3 | 03-software-engineer 2 | 05-rag-knowledge-engineer 1 | 07-document-producer 1
```

Вывод: 7 воркеров живы (лимит `kanban.max_in_progress: 7` выбран ровно), 13 карточек в `ready`
без единой блокировки — то есть **диспетчеру есть кого забирать, но некуда**: слотов нет.
Это насыщение, а не залипание.

### A/B на одном снимке живой доски (`evidence/false-alarm-before-after.txt`)

Один и тот же снимок `/home/hermes/.hermes/kanban.db`, оба дерева, `dispatch_once(dry_run=True)`,
лимиты 7/3:

```
BEFORE base-prod 6308ff67cc  capacity_bound_present=False
  running 7 / ready 11 / spawned 0   OLD_rule_warns=true (3 из 3 тиков)
AFTER  repo      18ee183313  capacity_bound_present=True
  running 7 / ready 11 / spawned 0   budget_hold=max_in_progress, saturated=true
  OLD_rule_warns=true (3 из 3)  NEW_rule_warns=false (0 из 3)
```

Разница только в коде — база, лимиты и нагрузка идентичны.

### Второй дефект той же природы (найден attempt 11)

`_run_reclaim_phase` выполняется **до** гейта бюджета, поэтому тик с `budget_hold` может нести
`crashed=[...]` (мёртвый pid воркера переведён в `ready`, слот освобождается только следующим
тиком). `capacity_bound()` проверял `crashed` раньше `budget_hold` → `False` → тревога.
Замер: `evidence/shadow-ticks-merged.jsonl` (22 тика, 00:29–00:51) — 21 `saturated=true`,
1 (`n=11`, 00:40:19) `stuck_would_warn=true` при `budget_hold=max_in_progress`.
Синтетика: `probe_deadpid.py` (живой снимок, одному running-воркеру подставлен мёртвый pid
4194000): до правки `capacity_bound=False`, после `True` — `evidence/deadpid-before-after.txt`.

---

## 2. Правка (минимальная, ветка карточки)

| коммит | содержание |
|---|---|
| `6f8c6bb758` | `capacity_bound(results)` в `hermes_cli/kanban_db_dispatch.py:195-232`; поле `DispatchResult.budget_hold`; `describe_suppression` называет `budget_hold:*` и `per_profile_capped` (`hermes_cli/kanban_db_suppression.py`); обе поверхности тревоги — gateway watcher (`gateway/kanban_watchers.py:309-333`) и CLI daemon (`hermes_cli/kanban_ops.py:227-247`) — при насыщении **не инкрементируют `bad_ticks`**, а пишут `INFO dispatcher saturated: … not a stall` не чаще раза в 5 мин |
| `81bc8010b3` | `budget_hold` проверяется **до** reclaim-бакетов: тик, остановленный лимитом, не может быть объявлен тревогой из-за `crashed` |
| `18ee183313`, `eda4feff3f` | merge `ours/feature/multiuser-access` (6308ff67cc → f60b97e0d5) в ветку карточки: правка наложена на актуальный прод-код |
| `1f3dd09424` | этот отчёт в дереве (ветка карточки) |

Что **не** тронуто (по требованию карточки): `_hold_if_workspace_unready`, `kanban.max_in_progress: 7`,
`max_in_progress_per_profile: 3`, `agent.max_turns`. Тревога по-прежнему срабатывает на
`env_unready`, `respawn_guarded`, `rate_limited`, `skipped_locked`, `memory_pressure` — это
оператор-действенные причины.

---

## 3. Приёмка

### (а) Механизм с локатором — выполнено
`hermes_cli/kanban_db_dispatch.py:2447-2451` + `:2598-2602` + `gateway/kanban_watchers.py:311`
(прод), запрос к базе с выводом — `evidence/mechanism-db-query.txt`.

### (б) Коммит и публикация — частично, нужен оркестратор
Код правки — верхушка **`eda4feff3f`** (merge `ours/feature/multiuser-access`).
Сверху в ветке лежат только коммиты этого отчёта (docs), код они не меняют.
Локальная и удалённая ветка совпадают (`refs/heads/wt/t_30b89c61` в `ours`) —
проверено `git rev-parse HEAD` против `git ls-remote ours refs/heads/wt/t_30b89c61`;
обе печатают один и тот же sha верхушки ветки.
Слияние в `ours/feature/multiuser-access` **не делалось**: страж деревьев
(`bin/card-tree-guard.py`) блокирует это карточке, по постановке слияние делает оркестратор.
Удалённая верхушка `feature/multiuser-access` на момент отчёта — `f60b97e0d5` (без правки).

### (в) 60+ минут наблюдения, `dispatcher stuck` = 0 — выполнено
`evidence/shadow-ticks-acceptance.jsonl`: **65 тиков, 02:49:56 → 03:54:36 (64 мин 40 с)**,
исправленное дерево, тот же тик, что и у шлюза (`dispatch_once(dry_run=True)`, лимиты 7/3),
свежий снимок живой базы каждую минуту:

```
stuck_would_warn (правило правки)         = 0 из 65
prod_rule (что делал бы прод-код 6308)    = 65 из 65
real_stall (слот свободен, спавна нет)    = 0
ошибок классификатора                     = 0
```

Контроль в том же окне по живому журналу: `journalctl --user -u hermes-gateway
--since "02:49:56" --until "03:54:36" | grep -c "dispatcher stuck"` → **7** (прод-код правки не
содержит, `hermes-agent` = `6308ff67cc`, `capacity_bound` в нём отсутствует).

### (г) Штатный переход `ready → running` за окно — выполнено
`evidence/ready-to-running-window.txt`, `task_events(kind='spawned')` без предшествующего
`reclaimed/crashed/worker_lost` у той же карточки:
```
03:00:20 t_86be05fb
03:06:28 t_819d3353
03:35:05 t_819d3353
03:42:28 t_cdf10daa
03:57:08 t_946c5142
```
Итого 5 штатных захватов; ручных `reclaim` в окне нет.

### (д) Регрессия — выполнено, «не меньше прошедших»
`tests/hermes_cli/` целиком параллельными шардами на этом хосте не идёт: прогон убивается
на середине (лог обрывается на точках, rc=1 без summary; в журнале OOM нет) — воспроизведено
дважды. Поэтому регрессия сделана **пер-файлово** по всем файлам, ссылающимся на четыре
изменённых модуля (`evidence/scoped-files.txt`, 66 файлов; те же файлы в обоих деревьях),
по одному процессу pytest на файл — `run_scoped_perfile.sh`, результаты
`evidence/perfile-before-6308/` и `evidence/perfile-after-18ee/`, сводка
`evidence/perfile-compare.txt`:

```
BEFORE 6308ff67cc (прод) : passed=484 failed=51  absent_files=2
AFTER  eda4feff3f (правка): passed=501 failed=52 absent_files=0
DELTA: passed +17
```

Отдельно: целевые тесты правки — `test_kanban_dispatcher_capacity_telemetry.py` +
`tests/gateway/test_kanban_dispatcher_saturation_telemetry.py` → **18 passed**;
смежные `test_kanban_dispatch_tick_hook`, `test_kanban_per_profile_cap`,
`test_kanban_host_cap` → в пакете 36 passed.

Про «+1 failed»: набор падающих узлов до/после (`evidence/failing-set-diff.txt`) —
единственное отличие `tests/gateway/test_session_kanban.py::test_real_dispatcher_lifecycle[timeout]`,
флейк нагрузки: изолированно в **обоих** деревьях проходит 3/3 (`17.5 s / 17.8 s / 17.2 s` прод,
`16.9 / 16.8 / 15.8` правка), а `[complete]` в том же файле стабильно падает 3/3 в **обоих**
деревьях (предсуществующий дефект теста, не связан с диспетчером). Ни одного нового
падающего узла правка не добавила.

---

## 4. Что осталось человеку/оркестратору

1. **Слияние** `wt/t_30b89c61` (код правки `eda4feff3f`, верхушка ветки — последний коммит с этим отчётом) в `ours/feature/multiuser-access` — страж деревьев
   запрещает это карточке. Конфликтов не ожидается: 5 коммитов, из них правка — 2 файла кода
   + 2 файла тестов, остальное — merge актуального `ours`.
2. **Рестарт шлюза** после слияния (карточке запрещён). Без рестарта прод продолжает слать
   ложную тревогу: в окне наблюдения 7 срабатываний.
3. Проверка после рестарта: `journalctl --user -u hermes-gateway --since <старт> |
   grep -c "dispatcher stuck"` = 0 при непустом `ready`, плюс в журнале появляется
   `kanban dispatcher saturated: … not a stall`.

## 5. Риски

- Правка превращает в INFO **только** насыщение по лимитам (`budget_hold`, `keypool_capped`,
  `per_profile_capped`). Ложное «всё хорошо» исключено: любая оператор-действенная причина
  (`env_unready`, respawn guard, `rate_limited`, `skipped_locked`, `memory_pressure`) сохраняет WARNING.
- Наблюдение 65 минут шло на копии базы, а не на живом шлюзе с правкой: прод-шлюз править
  нельзя без рестарта. Поэтому 7 ложных тревог в том же окне — это и есть прод-поведение,
  а 0 — поведение кода правки на той же доске.
