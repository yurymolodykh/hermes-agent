# PROGRESS — t_00544442 (МПД шаг 3)

## Состояние: РАБОТА ЗАВЕРШЕНА (код, тесты, живая проба, отчёт, коммит)

* Ветка: `feature/mpd-step3-identity`, worktree `.worktrees/t_00544442`
* Коммит: `931a460a17` (база — merge шага 2 `feature/mpd-step2-bindings` = `f2596a3524`)
* Отчёт: `REPORT-step3.md`; вывод живой пробы: `PROBE-step3-output.txt`; проба: `verify-step3-live.py`

## Что сделано

`hermes_acl_identity.py` (новый, 412 стр.) — логин → субъект, поколение → живость мандата, TTL;
флаг `acl.multi_user: false` (поставка выключена). Провайдер `basic` — много учёток, отзыв,
`credential_status`, refresh не переживает поколение. `owner_identity.principal_subject` (П4) —
узкое исключение для парольного входа. `ws_tickets` — `gen`/`src` в тикете и отказ при consume.
`middleware` — отдельный 401 `session_revoked`. `hermes_acl_scope.owner_stamps` — `person/<login>`
+ реестр привязок по каждому производному штампу. CLI `auth users revoke|remove`.

## Проверено

* 518 passed / 8 skipped в затронутых модулях; новых тестов 139.
* Живая проба на копиях 26 `state.db`: два логина → `person/probe.alice` / `person/probe.bob`
  (с выключенным флагом оба → `uid:1001`); отзыв → отказ access-токена, ws-тикета и refresh,
  повторный вход тем же паролем; удаление записи убивает сессию; владелец видит 723 из 735.
* Smoke-гейт: 2 failed / 30 errors — те же, что на базе (предсуществующие, `home_io_guard.py:160`).
* `scripts/check`: 68 блокирующих health-находок и 73 ruff на базе и на ветке — новых нет.

## Что осталось (вне объёма шага)

* Включение `acl.multi_user: true` для всех — отдельное решение владельца.
* `manual_review`: 401 `session_revoked` в живом браузере (на уровне шлюза проверено).
* Шаг 4 (права на сессии) не трогался.
