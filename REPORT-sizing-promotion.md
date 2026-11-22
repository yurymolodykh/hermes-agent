# REPORT — t_97c86dea: advisory о размере карточки на промоушене в `ready` (закрыть дыру CLI-пути)

## Проблема

Правило «одна карточка = одна проверяемая поставка» реально работало только в ответе
инструмента `kanban_create` (`tools/kanban_tools.py::_card_sizing_warning`). Карточка,
созданная через `hermes kanban create`, предупреждения не получала вообще, а диспетчер на
переходе `todo → ready` размер не смотрел. Крупная карточка уходила в работу молча.

Причина дыры на CLI-пути: `hermes_cli/kanban.py` передаёт
`initial_status=getattr(args, "initial_status", "running")`, а `initial_task_state`
(`hermes_cli/kanban_db_graph.py:45-51`) при отсутствии открытых родителей возвращает `ready`.
То есть CLI-карточка **рождается сразу в `ready`** и не проходит ни одного промоушена —
предупреждение, повешенное только на промоушен, эту популяцию пропустило бы.

## Что изменено

### 1. Новый sibling-модуль `hermes_cli/kanban_db_sizing.py` (117 строк)

Не стал раздувать `kanban_db.py` (4404 строки на базе). API:

* `comment_body(estimate, assignee)` — текст предупреждения или `None`. Признак «не влезает»
  делегирован `agent.kanban_card_sizing.sizing_warning`, поэтому DB-путь и tool-путь не могут
  разойтись в трактовке «over budget».
* `promotion_note(conn, task_id)` — читает `assignee`/`body` самой карточки, берёт бюджет
  **исполнителя** через `assignee_iteration_budget` (не создателя), `None` при
  отсутствующем/нечитаемом/unlimited бюджете.
* `has_sizing_comment(conn, task_id)` — идемпотентность: маркер `**card-sizing**` в начале
  тела; на карточке максимум один такой комментарий.
* `note_promotion(conn, task_id, author=...)` — запись через обычный
  `kanban_db.add_comment`. Ошибка внутри оценки/записи ловится и логируется
  (`logger.exception`), промоушен не рушится.

Формула оценки не тронута (`40 + 30 × пунктов приёмки`), статус не меняется, блокировки нет.

### 2. Три точки вызова в `hermes_cli/kanban_db.py`

| Точка | Кто | Когда |
|---|---|---|
| `recompute_ready` | диспетчерский тик | промоушеннутые в `ready` id собираются в `sized`, обработка **после** `write_txn` |
| `promote_task` | оператор | после транзакции, author = actor |
| `create_task` | создатель | только если карточка родилась в `ready` — ровно CLI-путь |

Все три — после своей транзакции, чтобы предупреждение не могло стать причиной отката
промоушена.

### 3. Разгрузка фасада: `hermes_cli/kanban_db_promotion.py`

`_has_sticky_block`, `_latest_event`, `_resume_status_from_events` вынесены из
`kanban_db.py` как есть (чистые читатели событий) и реэкспортированы в конце фасада —
так `kanban_db.py` остаётся в пределах порога 2000 строк и не растёт от новой логики.
Фасад: 4404 → 4365 строк.

## Тесты

`tests/hermes_cli/test_kanban_promotion_sizing.py` — 8 тестов, все зелёные:

1. over-budget карточка → ровно один комментарий, статус `ready` (промоушен состоялся);
2. диспетчерский тик (`recompute_ready`) пишет комментарий;
3. влезающая карточка → комментариев нет;
4. повторный промоушен → второго комментария нет (идемпотентность);
5. операторский `promote_task` → тот же advisory, тоже идемпотентно;
6. **CLI-создание сразу в `ready`** → комментарий есть (закрытие дыры CLI-пути);
7. CLI-создание влезающей карточки → комментариев нет;
8. неизвестный бюджет → тишина (не выдумываем cap).

Бюджет в тестах — фикстура (`assignee_iteration_budget` подменён), чтобы набор не зависел
от конфигов профилей на хосте. Красная база доказана: при вырезанных вызовах
`note_promotion` 4 из 8 тестов падают.

## Проверки

* `pytest tests/hermes_cli/test_kanban_promotion_sizing.py -q` → **8 passed**
* `python -m scripts.code_health --base 65db1ee540b` → **0 blocking, 0 advisory** (3 файла)
* `ruff check --select BLE001,S110,E722,F,RUF006` по новым файлам → All checks passed
* Широкий регресс `tests/hermes_cli`: head — 29 упавших файлов из 1055 выполненных
  (обрезка `timeout 1500` на 54.2 %); те же 29 перегнаны на базе `65db1ee540b` —
  28 падают идентично (среда, не наш код), `test_backup_preflight.py` проходит на head
  изолированно. Итог: **ноль регрессий от этого изменения**. Подробно —
  `evidence-t97c86dea/wide-regression-compare.md`.

## Живая проба (CLI, временная kanban-база)

Воспроизводимый скрипт `evidence-t97c86dea/live_probe.py` (изолированный
`HERMES_KANBAN_HOME`, живая доска не тронута), прогон на текущем HEAD через реальный
CLI-вход `hermes_cli.main:main`. Полный вывод — `evidence-t97c86dea/PROBE-cli-create.txt`.

```
Created t_0a04f39d  (ready, assignee=03-software-engineer)
Created t_af845441  (ready, assignee=03-software-engineer)

=== 1. hermes kanban show t_0a04f39d
  status:    ready
  assignee:  03-software-engineer
  [2026-11-23 00:29] user: **card-sizing** карточка не влезает в бюджет профиля-исполнителя.
  - исполнитель: `03-software-engineer`
  - пунктов приёмки: 10
  - ожидаемо итераций: 340
  - бюджет профиля (`agent.max_turns`): 180
  - превышение: 160
  Рекомендация: разделить карточку или вынести часть пунктов приёмки в отдельную карточку
  с `parents` = эта. Промоушен не заблокирован …

=== 2. hermes kanban show t_af845441
  status:    ready          (1 пункт приёмки → комментариев нет)
```

## Что осталось за границей карточки

* Правило остаётся предупреждающим: авто-декомпозиция для `ready` не делалась (она есть
  только для `triage`), статус не менялся.
* Порог и формула оценки не пересматривались — используется существующая
  `agent.kanban_card_sizing`.
