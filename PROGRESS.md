# PROGRESS — t_af262aaf (лимит итераций теряет готовую работу: сохранять до остановки)

## Состояние: РАБОТА ЗАВЕРШЕНА (код, 14+101+96 тестов, живая проба 32/32, база красная 16 LEAK)

* Ветка: `wt/t_af262aaf`, worktree `.worktrees/t_af262aaf`
* База: `0a1960695e`
* Отчёт: `REPORT-budget-stop.md`
* Проба: `verify-budget-stop-live.py` + `PROBE-budget-stop-output.txt` (32/32)
  Красная база: `verify-budget-stop-baseline-red.py` (16/32, 16 LEAK на `0a1960695e`)

### Что сделано

1. **Сохранение до предела.** `agent/turn_iteration_prep.py` (85 %, было 0,9) пакует дерево
   под `refs/hermes-wip/<task>` — без коммита в ветку. `agent/turn_finalizer.py::
   _checkpoint_work_before_stop` на пределе коммитит содержательные файлы в ветку с темой
   `WIP бюджет: N/N` ДО `_record_kanban_budget_exhausted`. Новый модуль
   `agent/kanban_budget_checkpoint.py` (checkpoint_wip / preserve_wip_ref / card_workspace /
   describe_stop), tar.gz-фолбэк в `task_attachments_dir` без git.
2. **Честный статус.** `describe_stop` пишет «остановлено по бюджету N/N · работа в коммит X
   (ветка B) · сохранено файлов: K (…) · осталось: …» в `last_failure_error` и в событие
   `timed_out` (`budget_used/budget_max/stop_note`). Исправлено: `event_payload_extra` теперь
   попадает и на нетрипнутую ветку `_record_task_failure`.
3. **Размер карточки.** `agent/kanban_card_sizing.py`: `40 + 30 × пунктов приёмки`; бюджет из
   конфига ПРОФИЛЯ-ИСПОЛНИТЕЛЯ; предупреждение в ответе `kanban_create` (`sizing_warning`).
   Правило и замер (383 прогона, ловит 19 % остановов при 5 % ложных) записаны в навык
   `kanban-card-sizing` (копии default + 01-orchestrator), раздел Р12 + раздел про чекпоинт.
4. Вынос `describe_suppression` в `hermes_cli/kanban_db_suppression.py` — файл-фасад диспетчера
   вырос на 6 строк и обязан уменьшаться (ratchet FILE_LINES).

### Проверки (все выполнены)

* 14 passed — новые наборы (`test_kanban_budget_wip_checkpoint`, `test_kanban_card_sizing`)
* 101 passed — + финализатор/восстановление/handoff/env-provisioning
* 96 passed, 1 skipped — канбан dispatch/db/closed-card/budget-warning
* 1001 passed, 11 failed, 4 skipped — широкий канбан-прогон (118 файлов); те же 11 падают на базе
* `code_health --base 0a1960695e`: 0 blocking, 0 advisory (10 файлов)
* `ruff check` 10 файлов: All checks passed
* Живая проба 32/32 OK; на базе 16/32 (16 LEAK)

### Ограничения

* Живой воркер с настоящим `agent.max_turns` не прогонялся: бюджет в пробе урезан программно.
* Windows-путь не проверен (только Linux).
* `manual_review`: увидеть коммит в `hermes kanban show` на реальном останове после перезапуска.
* `agent.max_turns` не менял — часть 3 сдаёт правило, не числа.

### Осталось

1. `git add`/`commit` в ветке карточки (шаг следующего действия).
2. Слияние в `feature/multiuser-access` — оркестратор (страж деревьев).
