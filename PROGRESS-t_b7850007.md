# PROGRESS — t_b7850007: дневное окно 08–18 МСК ≤4 карточки + равномерная нагрузка по 7 ключам пула letovo

## Состояние: ГОТОВО — реализация и проверки сняты, коммит с номером карточки есть.

* Дерево: `/home/hermes/.hermes/hermes-agent/.worktrees/t_b7850007`, ветка `wt/t_b7850007`
* База сравнения: `8cbd0cde6eb`
* Коммит: `7c6b15eae854017b9c99a56e85f997db4878fc02`
* Интерпретатор проверок: `/home/hermes/.hermes/hermes-agent/.venv/bin/python`

## Что доделано в этой попытке (остаток из карточки)

1. `agent/kanban_key_pool.py:614-660` — восстановлена `_pick_key(free, occupied, last_key_id, usage=None)`:
   (1) минимум активных слотов среди свободных (`min(len(occupied[kid]))`);
   (2) среди них минимум `_usage.busy_seconds` с допуском `_USAGE_TOLERANCE_SECONDS = 60.0`
   (`agent/kanban_key_pool.py:136`);
   (3) внутри допуска — прежний round-robin по курсору `last_key_id` (первый строго больший, с
   переходом через край). Вызов на `agent/kanban_key_pool.py:1234` соответствует сигнатуре;
   курсор ставит вызывающий на выданный ключ.
2. `_account_usage` (`agent/kanban_key_pool.py:604-612`) делегирует в
   `_usage.account_slot(state, key_id, slot, now=now, ttl_seconds=_ttl_seconds())`;
   вызовы: `:802`, `:1452`, `:1519`.
3. `agent/kanban_key_pool.py:1892` — `_usage_bucket(state, now)` → `_usage.usage_bucket(state, now)`.
4. Фасадный шов `agent/kanban_key_pool.py:66` — `_usage_today = _usage.usage_today`
   (нужен `test_day_boundary_is_moscow_not_utc`); импорты сиблингов `:60-61`.

## Критерий 1 — тесты (боевой интерпретатор)

```
213 passed in 37.59s
```
Наборы: `tests/hermes_cli/test_kanban_day_window.py`, `tests/agent/test_kanban_key_pool_fairness.py`,
`tests/agent/test_kanban_key_pool.py`, `tests/hermes_cli/test_kanban_keypool_release.py`,
`tests/hermes_cli/test_kanban_keypool_integration.py`, `tests/hermes_cli/test_kanban_keypool_red_proof.py`,
`tests/hermes_cli/test_kanban_memory_guard.py`, `tests/hermes_cli/test_kanban_cli_dispatch_passthrough.py`,
`tests/hermes_cli/test_kanban_per_profile_cap.py`.
Отдельно два новых набора: **45 passed** (3.07s).

## Критерий 2 — окно и равномерность

`evidence-t_b7850007/criterion2_probe.py` (вывод — `evidence-t_b7850007/criterion2_output.txt`):

```
base_cap=7  day_cap=4  window=08:00-18:00  tz=Europe/Moscow
  OK  07:59 -> 7 | OK 08:00 -> 4 | OK 17:59 -> 4 | OK 18:00 -> 7
40 hand-outs over 4 of 7 keys (quarantined [6,7,8]): #2=10 #3=10 #4=10 #5=10
  cards spread = 0 (limit 1)  total 40/40
RESULT: window_failures=0 fairness_failures=0
```

## Критерий 3 — дифф от базы

`git diff --stat 8cbd0cde6eb` — 12 файлов, 1564 вставки / 201 удаление.

## Остаточные ограничения

- `ruff` в этом `.venv` отсутствует (`No module named ruff`) — линт не снимался; критерии приёмки его
  не требуют. Синтаксис подтверждён импортом модулей в пробе и прогоном 213 тестов.
- Живые `~/.hermes/config.yaml` и `~/.hermes/kanban-keypool.state.json` **не** менялись; боевые
  `kanban.day_*` не выставлялись — это шаг карточки внедрения `t_31378ea5` после PASS ревью `t_5ef4ef20`.
- Вливание в общее дерево и рестарт шлюза — не этой карточкой (`ВЫХОД`: только worktree).
- Корневой `PROGRESS.md` — общий разделяемый файл миссии; карточка держит его равным версии целевой
  ветки, а свой журнал ведёт здесь, чтобы не давать конфликт слияния на чужом файле.
