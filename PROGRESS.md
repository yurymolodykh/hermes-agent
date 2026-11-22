# PROGRESS — t_97c86dea: card-sizing advisory on promotion (CLI hole)

## Состояние: ГОТОВО — все критерии приёмки закрыты

* Дерево: `/home/hermes/.hermes/hermes-agent/.worktrees/t_97c86dea`, ветка `wt/t_97c86dea`
* База: `65db1ee540b`
* Python с pytest: `/home/hermes/.hermes/kanban/workspaces/t_af262aaf/.venv/bin/python`
* Артефакты: `evidence-t97c86dea/`
* (Прежний `PROGRESS.md` от карточки t_4cb8f83b сохранён как `PROGRESS.stale-t4cb8f83b.md`.)

## Что сделано (закоммичено)

1. **Новый sibling-модуль** `hermes_cli/kanban_db_sizing.py`:
   `comment_body` / `promotion_note` / `has_sizing_comment` / `note_promotion` / `note_if_ready`.
   Правило advisory: ни блокировки, ни смены статуса; начало тела —
   `**card-sizing**` = ключ идемпотентности (одна карточка → максимум один комментарий).
   Бюджет берётся у assignee через `agent.kanban_card_sizing.assignee_iteration_budget`;
   unknown/unlimited → молчание. Ошибки внутри оценки ловятся и логируются, промоушен не рушится.
2. **Три точки вызова** в `hermes_cli/kanban_db.py`:
   * `recompute_ready` — dispatcher tick: промоушеннутые в `ready` id собираются в `sized`
     и обрабатываются ПОСЛЕ транзакции (author `dispatcher`);
   * `promote_task` — операторский промоушен (author = actor);
   * `create_task` — карточка, сразу рождённая в `ready` (ровно CLI-путь
     `hermes kanban create`: `initial_status="running"` → `ready` без открытых родителей);
     через шим `note_if_ready`.
3. **Вынос из фасада** (фасад 4404 → 4365 строк): `_has_sticky_block`, `_latest_event`,
   `_resume_status_from_events` перенесены в новый `hermes_cli/kanban_db_promotion.py`
   и реэкспортированы в конце `kanban_db.py` (цикл как у прочих sibling-модулей: поздний импорт фасада).
4. **Тесты** `tests/hermes_cli/test_kanban_promotion_sizing.py` — 8 тестов, все зелёные:
   over-budget → 1 комментарий и статус `ready`; tick-путь; влезающая карточка → 0 комментариев;
   повторный промоушен → второго комментария нет; операторский `promote_task`;
   CLI-создание сразу в `ready`; unknown budget → тишина.
   Красная база доказана: с вырезанными вызовами 4 из 8 падают.

## Проверки (артефакты в `evidence-t97c86dea/`)

* `focused-pytest.txt` — `pytest tests/hermes_cli/test_kanban_promotion_sizing.py -q` → 8 passed.
* `code-health.txt` — `python -m scripts.code_health --base 65db1ee540b` → **0 blocking, 0 advisory**.
* `PROBE-cli-create.txt` — живая проба на текущем HEAD через реальный CLI-вход
  `hermes_cli.main:main`: карточка с 10 пунктами приёмки на `03-software-engineer`
  (бюджет 180, оценка 340, превышение 160) → один `**card-sizing**` комментарий, статус
  остаётся `ready`; карточка с 1 пунктом → комментариев нет.
* `live_probe.py` — воспроизводимый скрипт пробы (изолированный `HERMES_KANBAN_HOME`,
  реальную доску не трогает).
* `ruff --select BLE001,S110,E722,F,RUF006` по новым файлам → All checks passed.

## Важно про окружение (не путать с регрессией)

Прямой `pytest` в этом рабочем дереве даёт **ложные** падения: дерево лежит внутри
`~/.hermes`, а `tests/home_io_guard.py` запрещает любой I/O по реальному hermes home
(`TEST BUG: file I/O against the REAL hermes home`). Поэтому широкий регресс гоняется
в чистой копии **вне** `~/.hermes`:

    git archive <rev> | tar -x -C /home/hermes/ksz-probe/<dir>/{head|base}
    cd <copy> && TMPDIR=/home/hermes/ksz-probe/tmp \
      HERMES_PYTHON=/home/hermes/.hermes/kanban/workspaces/t_af262aaf/.venv/bin/python \
      bash scripts/run_tests.sh tests/hermes_cli/ -q -j 7 --file-retries 0

`tests/hermes_cli` — ~1963 файла / ~15k тестов, полный проход не укладывается в 25 минут
ни на одном дереве; поэтому запуск идёт с `timeout 1500`, а каждый упавший файл
**доотдельно** перегоняется на базе `65db1ee540b` — сверка пофайловая, а не по проценту.

## Итог широкого регресса

* head (`timeout 1500`, `-j 7`): 1055 файлов выполнено (54.2 % от 1963), 29 файлов с падениями.
* база `65db1ee540b`, те же 29 файлов: **28 падают идентично** (среда: gateway-супервизоры,
  `doctor_pm_store_probe`, `enable_no_capabilities`, `login_backend_provenance`, …).
* `test_backup_preflight.py` — падал в head только на общем прогоне (20-секундный таймаут
  подпроцесса под нагрузкой `-j 7`), **изолированно на head проходит**: 3 passed in 3.5s.
* **Ноль регрессий, объяснимых изменением карточки.**
* Детали и сырые логи — `evidence-t97c86dea/wide-regression-compare.md`.

## Осталось

Ничего: критерии приёмки закрыты, изменения закоммичены, карточка сдаётся.
