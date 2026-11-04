# Правило доставки итогового отчёта: только после положительного ревью

Статус: канон (решение владельца, 03.11.2026). Карточка t_ff1867f5.
Реализация дочерней карточки t_f6790ef5.

## 1. Правило (одна фраза)

**Итоговый отчёт миссии уходит в канал только когда вся её ветка закрыта
и приёмка/ревью этой ветки завершилось положительно; закрытие отдельного
этапа при ещё идущей работе в канал не уходит, а отказ приёмки уходит
вердиктом «не выполнено», а не отчётом о выполнении.**

## 2. Единственная точка правды

| Слой | Файл / функция | Что решает |
|---|---|---|
| Канон статусов | `hermes_cli/kanban_db.py::classify_branch_node_status`, `BRANCH_TERMINAL_SUCCESS`, `SA_BRANCH_NODE_OK/OPEN/FAIL` | терминальность ОДНОГО узла |
| Канон ветки | `hermes_cli/kanban_db.py::branch_state` | `None` = ветка открыта; иначе `{outcome: success\|failure, failed, fingerprint}` |
| **Канон правила ревью** | **`hermes_cli/kanban_db.py::summary_has_passed_review(conn, root_id, outcome)`** | **можно ли отдать итог: True/False. ЗДЕСЬ И ТОЛЬКО ЗДЕСЬ** |
| Вспомогательное | `hermes_cli/kanban_db.py::has_reviewer_descendant(conn, root_id)` | есть ли в ветке карточка ревьюера (`REVIEWER_PROFILES`) |
| Вердикт отказа | `hermes_cli/kanban_db.py::acceptance_refusal_ids(conn, root_id, failed)` + `branch_summary_message(..., refused_ids=)` | какие провалы — отказ приёмки (формулировка «Доработка отклонена») |
| Путь 1: ветка-итог | `gateway/kanban_watchers.py::_plan_branch_summary` (~2065) | вызывает `summary_has_passed_review`; False → `None` (итог не планируется) |
| Путь 2: событие самой карточки | `gateway/kanban_watchers.py::GatewayKanbanWatchersMixin` (`_collect`, `has_open_descendants` ~1100) + `gateway/delivery_engine.py::handle_event` (`branch_terminal` ~1583–1633, параметр `review_passed`) | закрытие корня с открытыми детьми / без положительного ревью не является итогом |
| Терминальность ветки (TG-зеркало) | `gateway/kanban_watchers.py::_branch_summary_terminal` (~315) | нет открытых предков и потомков + нет живого контроллера миссии |

Второй список статусов не заводится: правило читает только
`BRANCH_TERMINAL_SUCCESS` и `classify_branch_node_status` из `kanban_db.py`.

## 3. Три конфигурации ревью (все разобраны)

| Конфигурация | Где живёт | Что делает правило |
|---|---|---|
| **(а) Ревью = статус карточки** (`request_review` → `status='review'`) | та же карточка | `classify_branch_node_status` даёт `SA_BRANCH_NODE_OPEN` → `branch_state` = `None` → итог не планируется. Уровень выше правила ревью; покрыто тестом `test_review_status_on_card_keeps_branch_open`. |
| **(б) Ревью = отдельная дочерняя карточка** `09-independent-reviewer` / `15-sre-qa` (реальный пример: `t_f783b127` — child `t_15ab8811`) | `task_links`-потомок | `has_reviewer_descendant` находит её по `assignee` ∈ `REVIEWER_PROFILES` (`agent/reasoning_effort_l1.py:61`). Пока карточка не закрыта — ветка не терминальна; закрыта `done`/`archived` — итог законен. Тесты: `test_open_reviewer_child_blocks_success_summary`, `test_reviewer_child_running_means_no_summary`, `test_reviewer_child_closed_success_delivers_once`, `test_only_sre_qa_reviewer_counts_as_review`. |
| **(в) Ревью нет вовсе** (в т.ч. изолированный корень без детей) | — | `has_reviewer_descendant` = False → итог доставляется. Тесты: `test_no_review_at_all_is_a_legitimate_summary`, `test_childless_root_stays_terminal`. |

Отказ приёмки (`blocked` с `block_kind` ∉ {dependency, transient}, либо
`gave_up`) — это НЕ «нет ревью»: `branch_state` даёт `outcome='failure'`,
`summary_has_passed_review` возвращает True (вердикт обязан быть виден),
а текст — `branch_summary_message` failure-ветка: `❌ Задача не выполнена
(нужен фикс): <id:title>`. Тест: `test_refusal_is_a_verdict_not_a_completion`,
`test_blocked_acceptance_is_a_terminal_verdict`.

Ветка `success`, но в ней есть ревьюер не в `BRANCH_TERMINAL_SUCCESS` —
противоречивое состояние (по статусам невозможно, `branch_state` вернул бы
`None`): правило fail-closed → итог не отдаётся.

## 4. Решение по машиночитаемому вердикту

**Решено: отдельное поле/событие `review_verdict` СЕЙЧАС не вводится.**
Обоснование по фактам схемы: колонки вердикта в `tasks` нет; `completed`
ревьюера не несёт `conclusion` (280 записей — все NULL, проверено);
вердикт живёт текстом в `task_comments.body`; отказ приёмки уже даёт
машиночитаемый сигнал через `changes_requested` (`kanban_db.request_changes`,
~8329) и через `blocked`/`gave_up` статус.

Схема, зафиксированная на случай, если текста окажется недостаточно
(вводить НЕ раньше, чем появится живой случай, где статусов не хватило):

- событие `review_verdict` в `task_events`, payload:
  `{"verdict": "passed" | "changes_requested", "reviewer": "<profile>",
    "target_task_id": "<id>", "reason": "<≤200 chars, redacted>"}`;
- эмиттеры: `kanban_complete` (passed) и `kanban_request_changes`
  (changes_requested) — оба уже существуют;
- точка чтения одна: `summary_has_passed_review` (расширяется, не
  дублируется); `classify_branch_node_status` не меняется;
- обратная совместимость: отсутствие события у ревьюера = «passed по
  факту закрытия» (текущее поведение), чтобы старые ветки не заглохли.

## 5. Состояние внедрения

- Правки в рабочем дереве `hermes-agent`, **НЕ закоммичены**, шлюз
  **НЕ перезапущен** (рестарт оборвал бы живые сессии; отдельный шаг с
  согласия владельца).
- Файлы: `gateway/kanban_watchers.py`, `gateway/delivery_engine.py`,
  `hermes_cli/kanban_db.py`, `gateway/final_report.py`, тесты
  `tests/gateway/test_f10_terminal_only_after_acceptance.py` (16 тестов).
- Целевые наборы: 148 passed (`test_f10…` 16, `test_kanban_branch_summary_watcher`,
  `test_final_report_delivery_t_1fd3f22b`, `tests/hermes_cli/test_kanban_branch_summary`,
  `test_kanban_mission_chat_silence_dual_lane`, `tests/cron/test_branch_summary_t2`,
  `tests/cron/test_branch_summary_batch_close_t9552`).
- **Активация требует рестарта шлюза** — без него правило в бою не действует.
  Кроме того, `t_b69696d5`: `taskkill /T` при рестарте убивает kanban-воркеров
  в полёте, поэтому рестарт нужен в окне без живых воркеров.

### 5.1 t_f6790ef5: что доделано поверх канонизации

| Пункт карточки | Результат |
|---|---|
| (1) путь (б) — дочерний ревьюер 09 | Предикаты покрывали, но **гейт в движке был мёртвым**: `handle_event(review_passed=...)` объявлен, а `_collect` его не передавал → всегда `True`. Теперь `_collect` (`gateway/kanban_watchers.py`, api_server-ветка) считает `kb.summary_has_passed_review` и кладёт в `deliveries[].review_passed`; `handle_event` его получает. Fail-closed при исключении. |
| (2) отказ приёмки | Введён `kb.acceptance_refusal_ids(conn, root_id, failed)` (по `_reviewer_assignees`, не по новому списку статусов) + параметр `branch_summary_message(..., refused_ids=…)`. Отказ уходит как `❌ Доработка отклонена — приёмка не приняла результат: …` вместо `❌ Задача не выполнена (нужен фикс)`. Доставляется (не глушится) ровно один раз. То же в `gateway/final_report.py::_compose`. |
| (3) не глушить законные итоги | Тесты `test_no_review_at_all_is_a_legitimate_summary`, `test_childless_root_stays_terminal`, `test_reviewless_mission_still_delivers` (доставка на уровне watcher-тика). |
| (4) один словарь статусов | Новых статусов нет: используются `BRANCH_TERMINAL_SUCCESS`, `classify_branch_node_status`, `REVIEWER_PROFILES`. `acceptance_refusal_ids` читает только `tasks.assignee`. |

Доставка проверена на уровне тика watcher'а (`_kanban_branch_summary_watcher` +
запатченный `stage_terminal`), а не только предикатами: «не уходит» / «уходит
ровно один раз» / формулировка вердикта.

## 6. Остаточные пробелы (явно)

- `manual_review` — рестарт шлюза и живых сессий: требует согласия владельца,
  автотестом не подтверждается.
- `manual_review` — Telegram-путь: для нативной TG-подписки закрытие корня
  намеренно остаётся видимым (один канал, нет отдельной ветки-итога);
  правило ревью применяется к ветке-итогу, а не к одиночному пингу закрытия.
  Это осознанное отступление, а не пробел-ошибка, но проверено только в
  тестах предикатов.
- `needs_source` — ревьюер как **родитель** (ревью-гейт через `parents=`):
  `summary_has_passed_review` смотрит только ПОТОМКОВ корня ветки,
  а ревьюер-родитель оказывается выше корня и учитывается через
  `_branch_summary_terminal` (закрытые предки), отдельной семантики вердикта
  для этой формы нет. Встречающейся в бою формы не найдено.
- `needs_source` — предсуществующие падения в `tests/gateway`
  (`test_kanban_notifier` и др., 44 failed): воспроизводятся на HEAD без
  наших правок, к этой задаче отношения не имеют.
- `needs_source` — `tests/gateway/test_api_server.py` не собирается
  (`_classify_openwebui_service_request` отсутствует), посторонняя поломка.
