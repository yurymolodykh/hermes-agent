# PROGRESS — t_b7850007: дневное окно 08–18 МСК ≤4 карточки + равномерная нагрузка по 7 ключам пула letovo

## Состояние: ГОТОВО (все критерии приёмки закрыты), работа в коммите с номером карточки.

* Дерево: `/home/hermes/.hermes/hermes-agent/.worktrees/t_b7850007`, ветка `wt/t_b7850007`
* База сравнения: `8cbd0cde6eb`
* Интерпретатор проверок: `/home/hermes/.hermes/hermes-agent/.venv/bin/python`

## Что доделано в этой попытке (остаток из карточки)

1. `agent/kanban_key_pool.py:614-660` — восстановлена `_pick_key(free, occupied, last_key_id, usage=None)`
   с новой логикой: (1) минимум активных слотов среди свободных (`min(len(occupied[kid]))`),
   (2) среди них минимум `_usage.busy_seconds` с допуском `_USAGE_TOLERANCE_SECONDS = 60.0`
   (`agent/kanban_key_pool.py:136`), (3) внутри допуска — прежний round-robin по курсору
   `last_key_id` (первый строго больший, с переходом через край). Вызов на
   `agent/kanban_key_pool.py:1234` соответствует сигнатуре; курсор обновляется вызывающим на
   выданный ключ.
2. `_account_usage` (`agent/kanban_key_pool.py:604-612`) делегирует в
   `_usage.account_slot(state, key_id, slot, now=now, ttl_seconds=_ttl_seconds())`;
   вызовы: `:802`, `:1452`, `:1519`.
3. `agent/kanban_key_pool.py:1892` — `_usage_bucket(state, now)` → `_usage.usage_bucket(state, now)`.
4. Фасадный шов: `agent/kanban_key_pool.py:66` — `_usage_today = _usage.usage_today`
   (тест `test_day_boundary_is_moscow_not_utc` читает день через фасад).
5. Импорты сиблингов: `agent/kanban_key_pool.py:60-61` — `_state`, `_usage`.

## Критерий 1 — тесты

`/home/hermes/.hermes/hermes-agent/.venv/bin/python -m pytest` по набору:
`tests/hermes_cli/test_kanban_day_window.py`, `tests/agent/test_kanban_key_pool_fairness.py`,
`tests/agent/test_kanban_key_pool.py`, `tests/hermes_cli/test_kanban_keypool_release.py`,
`tests/hermes_cli/test_kanban_keypool_integration.py`, `tests/hermes_cli/test_kanban_keypool_red_proof.py`,
`tests/hermes_cli/test_kanban_memory_guard.py`, `tests/hermes_cli/test_kanban_cli_dispatch_passthrough.py`,
`tests/hermes_cli/test_kanban_per_profile_cap.py`

→ **213 passed, 0 failed** (37.59s).

## Критерий 2 — боевая проверка окна и равномерности

`/home/hermes/.hermes/hermes-agent/.venv/bin/python evidence-t_b7850007/criterion2_probe.py`
(вывод — `evidence-t_b7850007/criterion2_output.txt`):

```
base_cap=7  day_cap=4  window=08:00-18:00  tz=Europe/Moscow
  OK  07:59 Europe/Moscow -> 7 (expected 7)
  OK  08:00 Europe/Moscow -> 4 (expected 4)
  OK  17:59 Europe/Moscow -> 4 (expected 4)
  OK  18:00 Europe/Moscow -> 7 (expected 7)
40 hand-outs over 4 of 7 keys (5 min each; quarantined: [6, 7, 8]):
  key #2: cards=10; #3: 10; #4: 10; #5: 10
  cards spread = 0 (limit 1)  total cards = 40 / 40
RESULT: window_failures=0 fairness_failures=0
```

Живой `_load_settings()` в профиле возвращает невыставленные значения (`None`) — дневной слой
включается только когда владелец задаст `kanban.day_*`; гейт проверен на явно заданных
значениях (base_cap=7, day_cap=4).

## Критерий 3 — коммит и дифф

Коммит с номером карточки — `git log -1`. `git diff --stat 8cbd0cde6eb` — 11 файлов,
1494 вставки / 147 удалений.

## Остаточные ограничения

* `ruff` в `.venv` воркtree отсутствует (`No module named ruff`); линт-проверка не снималась,
  критерии приёмки карточки её не требуют. Python-синтаксис подтверждён импортом модулей в пробах
  и прогоном тестов.
* Живой `~/.hermes/config.yaml` и `~/.hermes/kanban-keypool.state.json` не менялись; боевые
  значения `kanban.day_*` не выставлялись (решение владельца, отдельный шаг выката).
* Вливание в общую ветку и рестарт шлюза — за оркестратором, не этой карточкой.
