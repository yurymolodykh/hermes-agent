# PROGRESS — t_5bb8406e: замер сжатия WebSocket и решение по объёму

## Состояние: измерение готово, решение принято, правка production не требуется. Идёт финальный прогон профилей + отчёт.

* Дерево: `/home/hermes/.hermes/hermes-agent/.worktrees/t_5bb8406e`, ветка `wt/t_5bb8406e`
* Гейт публикации: `ops-card-merge.py --task t_5bb8406e` (не выполнен)

## Установлено (проверено по коду и живой системе)

1. Production УЖЕ гейтит deflate: `hermes_cli/web_server_lifecycle.py:588`
   `per_message_deflate = auth_required or host not in _LOOPBACK_HOST_VALUES`;
   `hermes_cli/web_server.py:1091` — то же для dashboard. Введено коммитом
   `1ae92cf59f2` (py-spy: 48 % цикла на WS-отправке, половина — deflate).
2. Живой шлюз слушает `127.0.0.1:9120`, но `dashboard.public_url=https://molodykh.su`
   + `basic_auth` → `listener_auth_required(...)` = True → **deflate включён**.
   Именно поэтому py-spy видит `PerMessageDeflate.encode` в живом профиле.
3. nginx (мастер pid 1243) терминирует TLS на 127.0.0.1:80/443 и проксирует на
   loopback → все удалённые пиры приходят с 127.0.0.1. **Гейт по пиру невозможен**:
   отличить локального клиента от проксированного нельзя (uvicorn решает на
   `connection_made`, до TLS/Host/заголовков).

## Замеры

* py-spy, 15 с, 8 клиентов, `delta-coalesced`: **доля срезов в deflate 43,9 %**
  (152 из 346 срезов); deflate off — 0 % и втрое меньше занятых срезов (75).
* Матрица (matrix-v2.json), контроль (те же настройки через override-фабрику)
  совпадает с базой в пределах 0,8 % — путь подмены фабрики достоверен.
* Ужатие опровергнуто: level 1/4 и memLevel 1 дают +2…+174 % трафика без выигрыша
  CPU; `no_context_takeover` +75…+712 % трафика и +20…+50 % CPU.
* Отключение: трафик ×2,1…×56 (медиана ~×7), CPU на кадр −22…−60 %.

## Следующий шаг

1. Дождаться `bench-ws-deflate/profile-shares.jsonl` (12 прогонов py-spy).
2. `bench-ws-deflate/REPORT.md` — цифры до/после, решение, обоснование.
3. Коммит → `ops-card-merge.py` → `kanban_request_review` (09-independent-reviewer).
