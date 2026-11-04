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
| **Канон правила ревью** | **`hermes_cli/kanban_db.py::branch_review_gate(conn, root_id, outcome)`** | **можно ли отдать итог + причина + аудит. ЗДЕСЬ И ТОЛЬКО ЗДЕСЬ** |
| Предикат (стабильное имя) | `hermes_cli/kanban_db.py::summary_has_passed_review(conn, root_id, outcome)` | тонкая обёртка над гейтом; его зовут существующие вызывающие |
| Что и каким текстом отдать | `hermes_cli/kanban_db.py::branch_summary_decision(conn, root_id)` | `deliver`, `kind` (success/refusal/failure/blocked), `msg`, `refused_ids`, `review_passed`, `gate` |
| Классификация вердикта | `hermes_cli/kanban_db.py::classify_review_verdict` + `REVIEW_VERDICT_*_TOKENS` | positive / negative / unknown / missing по `task_runs.metadata.verdict` |
| Вспомогательное | `hermes_cli/kanban_db.py::has_reviewer_descendant(conn, root_id)` | есть ли в ветке карточка ревьюера (`REVIEWER_PROFILES`) |
| Вердикт отказа | `hermes_cli/kanban_db.py::acceptance_refusal_ids(conn, root_id, failed)` + `branch_summary_message(..., refused_ids=)` | какие провалы — отказ приёмки (формулировка «Доработка отклонена») |
| Путь 1: ветка-итог | `gateway/kanban_watchers.py::_plan_branch_summary` | читает `branch_summary_decision`; `deliver=False` → `None` (итог не планируется), `kind` пишется в marker |
| Путь 2: событие самой карточки | `gateway/kanban_watchers.py::GatewayKanbanWatchersMixin` (`_collect`) + `gateway/delivery_engine.py::handle_event` (`review_passed`) | закрытие корня с открытыми детьми / без положительного ревью не является итогом; передаётся `decision["review_passed"]`, НЕ `deliver` |
| Путь 3: ручной итог | `gateway/final_report.py::_compose` | тот же `branch_summary_decision`; `deliver=False` → «❌ Итог не отдан» |
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

## 4. Машиночитаемый вердикт (решено и внедрено в t_5f76c8d0)

**Решение t_f6790ef5 «отдельное поле сейчас не вводится» отменено: оно
основывалось на фактически неверной посылке.**

Что проверено на живой БД (`C:/Users/hermes/AppData/Local/hermes/kanban.db`):

- `task_runs.conclusion` действительно нет, и `tasks` колонки вердикта не имеет —
  это было верно;
- но **`task_runs.metadata` уже несёт ключ `verdict`**: 208 закрытых карточек
  ревьюеров с распознанным вердиктом, 563 различных значения;
- и `done` у ревьюера НЕ означает «принято»: профиль 09/15 закрывает
  `kanban_complete` и при положительном, и при отрицательном исходе. Вердикт
  живёт в `metadata`, статус одинаковый.

Поэтому отдельного события `review_verdict` не потребовалось — достаточно читать
уже существующее поле. Миграции схемы нет.

### 4.1 Где правило живёт

| Что | Где |
|---|---|
| Словарь классов вердикта | `hermes_cli/kanban_db.py::REVIEW_VERDICT_*_TOKENS`, `classify_review_verdict` |
| Гейт «можно ли отдать итог» + причина + аудит | `kanban_db.py::branch_review_gate` (единственная точка) |
| Предикат для существующих вызывающих | `kanban_db.py::summary_has_passed_review` (тонкая обёртка над гейтом) |
| Что и каким текстом отдать | `kanban_db.py::branch_summary_decision` |

Оба пути доставки читают ОДИН payload: ветка-итог
(`kanban_watchers.py::_plan_branch_summary`) и ручной итог
(`gateway/final_report.py::_compose`).

### 4.2 Классификация вердикта — не подстрочный поиск

Значения в `metadata.verdict` бывают и метками, и повествованием:

- метки: `approved`, `PASS`, `ok`, `approved_with_limitations`,
  `revision_required`, `CHANGES`, `REWORK`, `REQUEST_CHANGES`,
  `ДОРАБОТАТЬ`, `changes_requested` (12+ написаний одного класса);
- повествование: `PASS 7/7 (0 FAIL, 0 UNCERTAIN)`,
  `fails before task-1 ✓ (RED at HEAD …); passes after ✓ …`.

Подстрочный поиск здесь неверен: слово `FAIL` внутри `0 FAIL`/`FAILOVER`
читается как отказ и **глушит законный итог**, что прямо запрещено правилом
владельца №4. Классификация поэтому «метко-образная» и намеренно асимметричная:

1. явный маркер `вердикт:` / `verdict:` авторитетен в любом месте значения;
2. иначе читается только ЛИД значения (до 4 первых токенов, служебные слова
   пропускаются), сравнение — по каноническим ТОКЕНАМ класса, не по подстрокам:
   `changes` матчит `changes`/`changes_requested`, но не `exchange`;
   `pass` матчит `pass`/`passed`, но не `compass`;
3. побеждает ПЕРВЫЙ класс-токен лида (метка ставит класс в начало, дальше идёт
   повествование — читать дальше значит дать повествованию перебить метку);
4. значение длиннее 110 символов без маркера — это ПОВЕСТВОВАНИЕ, а не метка →
   `unknown`, никогда не `negative`: недосмотреть отказ = сегодняшнее поведение,
   а выдумать отказ = заглушить отчёт владельца;
5. отрицание перед класс-токеном переворачивает класс
   (`not approved` → отказ, `no critical FAIL` → принято).

Проверка на живой доске: 45 канонических написаний классифицируются верно,
576 различных значений не дают ни одного ложного отказа.

### 4.3 Несколько карточек ревьюеров (68 ветвей на живой доске)

Решает САМЫЙ ПОЗДНИЙ вердикт по `ended_at`: поздний положительный после
доработки открывает итог, поздний отрицательный снова его закрывает. Все
карточки перечислены в `reviewers` с таймстампами — решение аудируемо.

### 4.4 Отсутствующий вердикт

`unknown`/`missing` → итог проходит, класс пишется в payload и в лог. Обоснование
измерено: 62 из 131 последних закрытых карточек ревьюеров не несут
машиночитаемого вердикта вообще. Fail-closed там заглушил бы ~47% законных
итогов — запрещено правилом владельца №4. Дефект, который исправлялся, —
ОБРАТНЫЙ случай (свидетельство отказа игнорировалось), поэтому для удержания
итога требуется ИЗВЕСТНЫЙ отказ.

## 5. Состояние внедрения

- Правки в рабочем дереве `hermes-agent`, **НЕ закоммичены**, шлюз
  **НЕ перезапущен** (рестарт оборвал бы живые сессии; отдельный шаг с
  согласия владельца).
- Файлы: `hermes_cli/kanban_db.py`, `gateway/kanban_watchers.py`,
  `gateway/final_report.py`, `design/REVIEW-DELIVERY-RULE.md`, тесты
  `tests/gateway/test_f10_terminal_only_after_acceptance.py` (16),
  `tests/gateway/test_t298_negative_verdict_bypass.py` (13).
- Целевые наборы: **140 passed** (`test_t298_negative_verdict_bypass` 9 на тот
  момент, `test_f10…` 16, `test_kanban_branch_summary_watcher`,
  `test_final_report_delivery_t_1fd3f22b`, `tests/hermes_cli/test_kanban_branch_summary`,
  `tests/cron/test_branch_summary_t2`, `tests/cron/test_branch_summary_batch_close_t9552`).
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

### 5.2 t_5f76c8d0: вердикт ревью вместо статуса карточки

Дефект (независимая проверка `t_298a2513`, профиль 09): предикат отвечал на
вопрос «карточка ревьюера закрыта?», а не «приёмка приняла?». Ревьюер закрывает
`kanban_complete` при ЛЮБОМ исходе, поэтому `done` + `verdict=revision_required`
давал «✅ Задача выполнена». На живой доске — 32 пары (корень, ревьюер) с
отрицательным вердиктом и доставленным «выполнено».

| Критерий карточки | Результат |
|---|---|
| 1. Гейт читает машиночитаемый вердикт | `branch_review_gate` читает `task_runs.metadata.verdict` последнего прогона карточки ревьюера; `done` больше не доказательство |
| 2. Отрицательный вердикт не даёт «✅» | `passed=False` → ветка-итог отдаёт `❌ Доработка отклонена — приёмка не приняла результат`; «✅» не появляется ни на одном из трёх путей |
| 3. Отказ не глушится (правило владельца №4) | `deliver=True`, `kind="refusal"`, в тексте названа карточка ревьюера: `test_negative_verdict_delivers_a_refusal_not_silence` |
| 4. Нет вердикта ≠ отказ | `verdict_unknown`/`missing` → итог проходит, класс в payload + лог: `test_missing_verdict_on_done_reviewer_still_delivers` |
| 5. Несколько ревьюеров | решает самый поздний по `ended_at`; перепроверка после доработки открывает итог, поздний отказ снова закрывает: `test_later_positive_verdict_supersedes_an_earlier_rejection`, `test_later_rejection_relocks_the_itog` |
| 6. Один словарь классов | `REVIEW_VERDICT_{POSITIVE,NEGATIVE}_TOKENS` + `classify_review_verdict`; 12+ написаний на живой доске, 45 канонических кейсов, 0 ложных отказов на 576 значениях |
| 7. Аудит решения | `branch_review_gate` → `{passed, reason, verdict_class, decided_by, reviewers[]}`; marker ветки несёт `kind` + `review{passed,reason,verdict_class}` |

Дополнительно закрыта вторая дыра того же класса, найденная при разборе:
per-event путь (`_collect` → `handle_event(review_passed=…)`) получал
`decision["deliver"]` вместо `decision["review_passed"]`. Отказ доставляем
(`deliver=True`), но ревью не пройдено — такая подстановка вернула бы «✅» на
этом пути. Пиннится `test_deliver_and_review_passed_are_different_questions`.

## 6. Остаточные пробелы (явно)

- `manual_review` — рестарт шлюза и живых сессий: требует согласия владельца,
  автотестом не подтверждается.
- `manual_review` — Telegram-путь: для нативной TG-подписки закрытие корня
  намеренно остаётся видимым (один канал, нет отдельной ветки-итога);
  правило ревью применяется к ветке-итогу, а не к одиночному пингу закрытия.
  Это осознанное отступление, а не пробел-ошибка, но проверено только в
  тестах предикатов.
- `needs_source` — ревьюер как **родитель** (ревью-гейт через `parents=`):
  `branch_review_gate` смотрит только ПОТОМКОВ корня ветки,
  а ревьюер-родитель оказывается выше корня и учитывается через
  `_branch_summary_terminal` (закрытые предки), отдельной семантики вердикта
  для этой формы нет. Встречающейся в бою формы не найдено.
- `needs_source` — вердикт живёт в свободном тексте `metadata.verdict`.
  Классификатор метко-образный; значение-повествование даёт `unknown`
  (итог проходит). Полностью устранить это может только эмиттер, пишущий
  канонический класс; такая карточка не создавалась — дефект закрыт на стороне
  чтения, а 208 живых карточек уже несут распознаваемый вердикт.
- `needs_source` — предсуществующие падения в `tests/gateway`
  (`test_kanban_notifier` и др., 12 failed): воспроизводятся на HEAD без
  наших правок (`git archive HEAD` в отдельное дерево, те же 12 имён),
  к этой задаче отношения не имеют. Причина — отсутствие профиля `worker`
  в реестре assignee этой машины.
- `needs_source` — `tests/gateway/test_api_server.py` не собирается
  (`_classify_openwebui_service_request` отсутствует), посторонняя поломка.
