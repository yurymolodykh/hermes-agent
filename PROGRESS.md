# PROGRESS — t_5bb8406e: замер сжатия WebSocket permessage-deflate

## Состояние: ИЗМЕРЕНО, РЕШЕНИЕ ПРИНЯТО, отчёт написан. Осталось: проверить стенд после правок линта → коммит → публикация.

### Сделано (всё проверено инструментом)

1. **Доля сжатия в занятости цикла (py-spy, `-r 100`, 15 с, 8 клиентов)** —
   `bench-ws-deflate/profile-shares.jsonl`, 12 прогонов:
   * `delta-coalesced`: дефолт 323 занятых среза, 127 в deflate = **39,3 %**; выкл → 100 срезов, 0 deflate.
   * `panel`: дефолт 974 среза, 380 deflate = **39,0 %**; выкл → 124, 0.
   * Ранее тем же методом 43,9 % — диапазон 39–44 %.
2. **Трафик (TCP-релей `drive.py`, байты на сокете)**:
   delta ×3,1 / card ×56,9 / models ×26,1 / panel ×7,5 при отключении; экономия при включённом 67–98 %.
3. **CPU на кадр (`cpu_precise.py`, выборка ~1 с, чередование вариантов)**:
   `models` repeat20: дефолт 0,8875 мс, w13 −16,4 %, w15 −7,3 %, выкл −33,1 %, control −3,5 % (=шум);
   `delta` repeat40: дефолт 0,0748, w13 −7,2 %, выкл −55,7 %, control −1,0 %.
4. **Тюнинг опровергнут** (`matrix-v2.json`, медианы 4 сценариев): level1 +35,4 % трафика,
   level4 +11,1 %, memLevel1 +2,4 % и +21,6 % CPU, noCTX +137,4 %, окно 9 +89,2 % (card +1623 %),
   окно 10 +24,2 %, окно 11 +10,6 % — экономии CPU нет. Окно 13/15 улучшает обе оси,
   но требует подмены `ServerPerMessageDeflateFactory` в uvicorn (ключ не предусмотрен).
5. **Код уже гейтован** до этой карточки: `web_server_lifecycle.py:588` и `web_server.py:1091`,
   коммит `1ae92cf59f2` (прочитано сообщение коммита — совпадает).
6. **Топология подтверждена**: nginx `sites-available/molodykh-pipe` проксирует на
   `127.0.0.1:9120` и **передаёт** `X-Real-IP`/`X-Forwarded-For`; uvicorn решает про сжатие
   в `__init__` протокола (:97) до `connection_made` (:128-134) ⇒ гейт «по клиенту» невозможен
   без подмены класса протокола (`web_server_lifecycle.py:598` — `proxy_headers=auth_required`).

### Решение
**Оставить сжатие как есть.** Отключение: ×3,1…×56,9 трафика за 33–56 % CPU на кадр. Ужатие:
ни один доступный ключ не экономит CPU. Правок продакшн-кода нет — и это результат замера.

### Осталось
1. Проверить, что стенд запускается после правок линта (`server_app.py` теперь dispatch-таблица
   `_EMIT`, `_write_json` вынесен в `asyncio.to_thread`, добавлены timeout'ы).
2. `git add -A && git commit`.
3. Публикация: `env -u HERMES_HOME /usr/bin/python3 /home/hermes/workspaces/01-orchestrator/ops/bin/ops-card-merge.py --task t_5bb8406e`.
4. `kanban_request_review` с ревьюером `09-independent-reviewer`.

### Локаторы
* Отчёт: `bench-ws-deflate/REPORT.md`
* Данные: `matrix-v2.json`, `matrix-window.json`, `cpu-precise.json`, `cpu-precise-delta.json`,
  `noise-floor.json`, `profile-shares.jsonl`
* Код стенда: `server_app.py`, `drive.py`, `matrix_v2.py`, `cpu_precise.py`, `noise_floor.py`,
  `profile_share.py`, `run_profiles.sh`
* База: merge-base с `main` = `3b4215785882`

### Предупреждение оператора
Чекпоинты не снимаются (`checkpoint store is busy`). Вся работа лежит в git-файлах worktree
и запушена в ref `refs/hermes-wip/t_5bb8406e`; коммит — следующий шаг.
