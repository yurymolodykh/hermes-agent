# PROGRESS — t_97c86dea: card-sizing advisory on promotion (CLI hole)

## Состояние: реализация + тесты готовы, идёт широкий регресс и живая проба

* Дерево: `/home/hermes/.hermes/hermes-agent/.worktrees/t_97c86dea`, ветка `wt/t_97c86dea`
* База: `65db1ee540b`
* Python с pytest: `/home/hermes/.hermes/kanban/workspaces/t_af262aaf/.venv/bin/python`
* (Прежний `PROGRESS.md` от карточки t_4cb8f83b сохранён как `PROGRESS.stale-t4cb8f83b.md`.)

## Что сделано

1. **Новый sibling-модуль** `hermes_cli/kanban_db_sizing.py` (117 строк):
   `comment_body` / `promotion_note` / `has_sizing_comment` / `note_promotion`.
   Правило advisory: ни блокировки, ни смены статуса; начало тела —
   `**card-sizing**` = ключ идемпотентности (одна карточка → максимум один комментарий).
   Бюджет берётся у assignee через `agent.kanban_card_sizing.assignee_iteration_budget`;
   unknown/unlimited → молчание. Ошибки внутри оценки ловятся и логируются, промоушен не рушится.
2. **Три точки вызова** в `hermes_cli/kanban_db.py`:
   * `recompute_ready` — dispatcher tick: промоушеннутые в `ready` id собираются в `sized`
     и обрабатываются ПОСЛЕ транзакции (author `dispatcher`);
   * `promote_task` — операторский промоушен (author = actor);
   * `create_task` — карточка, сразу рождённая в `ready` (ровно CLI-путь
     `hermes kanban create`: `initial_status="running"` → `ready` без открытых родителей).
3. **Вынос из фасада** (фасад 4404 → 4365 строк): `_has_sticky_block`, `_latest_event`,
   `_resume_status_from_events` перенесены в новый `hermes_cli/kanban_db_promotion.py`
   и реэкспортированы в конце `kanban_db.py` (цикл как у прочих sibling-модулей: поздний импорт фасада).
4. **Тесты** `tests/hermes_cli/test_kanban_promotion_sizing.py` — 8 тестов, все зелёные:
   over-budget → 1 комментарий и статус `ready`; tick-путь; влезающая карточка → 0 комментариев;
   повторный промоушен → второго комментария нет; операторский `promote_task`;
   CLI-создание сразу в `ready`; unknown budget → тишина.
   Красная база доказана: с вырезанными вызовами 4 из 8 падают.

## Проверки

* `pytest tests/hermes_cli/test_kanban_promotion_sizing.py -q` → 8 passed.
* `python -m scripts.code_health --base 65db1ee540b` → 0 blocking, 0 advisory.
* `ruff --select BLE001,S110,E722,F,RUF006` по новым файлам → All checks passed.
* `pytest tests/hermes_cli -q` — запущен в фоне (`proc_2af14bfb9665`).

## Осталось

1. Дождаться широкого регресса `tests/hermes_cli`; сверить падения с базой.
2. Живая проба: CLI-карточка с 10 пунктами приёмки на `03-software-engineer`, показать
   `hermes kanban show <id>` с комментарием, сложить вывод в `evidence-t97c86dea/`.
3. Финал: `git commit`, `kanban_comment`, `kanban_complete` с artifacts.
