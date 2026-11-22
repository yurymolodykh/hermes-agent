# t_dd5c9400 — разредить фоновый опрос веб-панели (116 → ≤25 запросов/мин)

Ветка `wt/t_dd5c9400` (база `feature/multiuser-access` = `beeb9623539`).
Верхушка перед публикацией: `96c43b112219c5723212a1b8c1c6d7d010f2eb54`.

## Что сделано (по драйверам)

| эндпоинт | кто вызывал / почему опрос | что сделали | ожидаемый /мин |
|---|---|---|---|
| `/api/desktop-plugins/root` + `/list` | `src/contrib/runtime-loader.ts:584` — в браузере нет Electron-наблюдателя `watchDirectory`, включался вечный резервный опрос диска; каждый тик = 2 запроса | `DISK_POLL_MS` 5 000 → 60 000; тик уже пропускается при `visibilityState !== 'visible'` и сам снимается, как только наблюдение за каталогом удалось | 1+1 (итого 2) |
| `/api/ui-preferences` | `src/lib/browser-ui-preferences.ts:8` — меж-устройственная синхронизация предпочтений | `POLL_MS` 10 000 → 60 000 (опрос и так только при `visible`, плюс мгновенный `focus`/`visibilitychange`) | 1 |
| `/api/config` + `/api/config/defaults` | `src/app/session/hooks/use-hermes-config.ts` — пачки 5–7 в одну секунду: `session.info`-edge, монтирование черновика и смена scope приходят одновременно; плюс `staleTime: 0` в `use-config-record.ts` | (а) `readHermesConfigOnce` — одна общая пара запросов на (connection, profile) в полёте; `force` (смена scope, явный reseed) не присоединяется; (б) `staleTime` 0 → 30 000 (`HERMES_CONFIG_STALE_MS`) | `/api/config` ≤1, `defaults` ≤0,5 |
| `/api/profiles` + `/api/profiles/active` | `src/store/profile.ts` — `/api/profiles` отвечает НОВЫМ массивом на каждое чтение, новый identity перезапускал эффекты всех потребителей; в вебе это `useUnservedProfiles`, который на каждый новый массив ходит ещё в `/api/profiles` + `/api/gateway-connections` + `/api/status` (наблюдалось ×4). `refreshActiveProfile` вообще не имел single-flight — boot, эффект `activeGatewayProfile` и фокус-обновление рельсы успевали выпустить по запросу (наблюдались пачки по 4 в одну секунду) | (а) сигнатурный гейт публикации `publishProfiles`: неизменившийся список сохраняет прежний identity; (б) `refreshActiveProfile` переведён на single-flight, как `refreshProfiles`, с разбором слота в `invalidateProfileListFetches`; конкурирующие вызовы делят одно чтение | `profiles` ≤2, `active` ≤2 |
| `/api/profiles/sessions`, `/api/profiles/sessions/sidebar`, `/api/cron/jobs` | `src/app/contrib/hooks/use-background-sync.ts:739` — один тик `SESSIONS_LIST_TICK_GAP_MS` тянет `refreshSessions`, который заодно обновляет крон | 10 000 → 30 000; отложенный внутри окна тик теперь выполняется сразу при `busy → false` (окно ограничивает частоту, а не задерживает ответ на событие) | по ≤2 |
| `/api/gateway-connections` | `src/app/settings/webapp-gateway-settings.tsx:20` — список сохранённых подключений, пока открыта страница настроек | `SAVED_CONNECTIONS_POLL_MS` 5 000 → 30 000 | ≤1 |
| реестр/маршруты ботов | `src/plugins/hermes-bots/relay.ts:42` — тик дренажа очередей (routes + registry + served-set) | `RELAY_DRAIN_INTERVAL_MS` 30 000 → 60 000 | ≤1 |
| `/api/model/info` | `use-model-controls.ts` — только по событиям: смена scope (`useGatewayScopeRefresh`), выбор модели, `applySavedMainModel`. Таймера нет | оставлено как есть: опрос по событию, не по таймеру | ≤1 |
| `/api/git/status` | `src/store/coding-status.ts:350` — `refreshRepoStatus` по структурным краям (смена cwd, завершение хода, фокус окна, мутация worktree); комментарий в модуле прямо фиксирует «never per-token» | оставлено как есть: драйвер событийный, сам себя ограничивает | ≤1 |
| `/api/status` | liveness гейтвея (`gateway.ping` идёт по WS; REST `/api/status` — не таймер, а пробы на открытии/переподключении) | оставлено как есть | ≤2 |
| `/api/audio/voice-live/status` | `use-hermes-config.ts:220` — статус дёргается при каждом обновлении конфига | **не тронуто осознанно.** Пробовал добавить 30-секундный кэш freshness (`refreshVoiceLiveStatus(force)` + `settledAt/settledKey`), но это сломало два существующих контрактных теста `voice-live.test.ts` («re-reads the status after the write», «does not let the previous profile publish after a switch»): запись режима обязана перечитывать статус немедленно, а маршрут профиля — не склеиваться. Кэш поверх этого делал поведение хрупким. Вместо этого частоту задаёт конфиг-опрос, который уже разряжен пунктом выше | ≤1 (следствие) |

Голосовой статус: отдельного «опроса» у него нет — это вызов в рамках обновления
конфига, поэтому после разряжения конфига он падает вместе с ним. Отдельный
таймер для voice-live в коде отсутствует (`fetchVoiceLiveStatus` вызывается только
из `refreshVoiceLiveStatus` и `setVoiceChatMode`).

## Проверки (все — реальные прогоны в этом дереве)

1. **Сборка** — `pnpm run build:webapp`: `✓ built in 16.60s`,
   `built …/apps/desktop/dist-webapp/index.html`; `index.html` от 19:05,
   маркера `.hermes-stale` нет, `hermes-build.json` перезаписан
   (`outputHash: 35de455e…`).
2. **Типы** — `NODE_OPTIONS=--max-old-space-size=6144 pnpm run typecheck`: exit 0
   (без `NODE_OPTIONS` падает по памяти — свойство машины, не кода).
3. **Тесты изменённых модулей** —
   `vitest run src/store/profile-active-refresh.test.ts src/app/session/hooks/use-hermes-config.test.ts src/plugins/hermes-bots/relay.test.ts src/store/profile.test.ts src/store/voice-live.test.ts src/app/contrib/hooks/use-background-sync.test.ts`
   → **152 passed, 0 failed** (круг 2: `use-background-sync` зелёный после исправления
   дефекта «отложенный тик после простоя», см. раздел ниже).
   В круге 1 было 151 passed / 1 failed — падение `only defers an external tick
   while busy, then refreshes once after idle` ошибочно списано на базу; это была
   регрессия правки, исправлена в круге 2.
4. **Красная база нового теста** — `profile-active-refresh.test.ts` на
   `beeb9623539:profile.ts`: `expected 4 to be 1` (4 запроса при 4 конкурентных
   вызовах). С правкой: 1. Тест проверяет именно наблюдаемую пачку.

## Круг 2: исправление дефекта ревью (отложенный тик после простоя)

**Дефект, найденный ревью.** `SESSIONS_LIST_TICK_GAP_MS` (10 000 → 30 000) — это
и ограничитель частоты фонового опроса, и задержка ответа на событие «ход
закончился». Тик, пришедший внутри окна, вооружал trailing-таймер на остаток окна
(`use-background-sync.ts:1495-1503`), поэтому после занятости список сессий
обновлялся не сразу, а через остаток 30-секундного окна (до 30 с).
Контракт теста `only defers an external tick while busy, then refreshes once after
idle`: «пока занято — не дёргаем; как только освободилось — дёргаем один раз сразу».

**Исправление** (`use-background-sync.ts`): подписка на `$busy`; при переходе
`busy → false`, если trailing-таймер уже вооружён, он снимается и проход
выполняется немедленно. Таймер вооружён только тогда, когда тик уже был отложен
внутри окна, поэтому простаивающая вкладка (где `$busy` не меняется) не получает
ни одного дополнительного чтения — ожидаемая частота не растёт. Интервал остаётся
30 000 (контракт свежести «событие или ≤30 с»). Тест не менялся.

**Проверки круга 2** (все — реальные прогоны в этом дереве, 22.11.2026 19:26–19:28):

- `vitest run use-background-sync.test.ts` — **67 passed** (файл целиком).
- Набор изменённых модулей (6 файлов) — **152 passed, 0 failed**.
- `NODE_OPTIONS=--max-old-space-size=6144 pnpm run typecheck` — exit 0.
- `pnpm run build:webapp` — `✓ built in 17.45s`, `dist-webapp/index.html`
  перезаписан (19:28), маркера `.hermes-stale` нет.
- Красная база дефекта: с `SESSIONS_LIST_TICK_GAP_MS = 10_000` и без `$busy`-флеша
  тест падает (`expected "vi.fn()" to be called 1 times, but got 0 times`), с
  флешем — зелёный.


## Ограничение по замеру (честно)

Живой замер через nginx **сейчас измеряет старый бандл**: живой
`/home/hermes/.hermes/hermes-agent/apps/desktop/dist-webapp/index.html` от
15.11.2026, код правки в него не выкачен (выкатка — за оркестратором). Поэтому
живые 55–72/мин, снятые в ходе работы, относятся к старому коду и подтверждают
только базу.

Попытка замерить новый бандл локально доведена до изолированного инстанса
(`hermes dashboard --isolated --skip-build` с `HERMES_WEB_DIST` на собранный
`dist-webapp`, порт 9347, отдельный `HERMES_HOME`) — он отдаёт именно новый
бандл (200 на `/`, токен сессии инжектится, `/api/config` с токеном → 200).
Но веб-панель грузит состояние через WS-гейтвей и без живого гейтвея в этом home
остаётся на экране «Backend offline» — фоновые таймеры не стартуют, и такой
замер не показателен. Гнать полноценный второй гейтвей на хосте нельзя: один
гейтвей на хост, существующий уже обслуживает все профили.

Итог: фактические /мин на живой панели после выкатки меряет оркестратор
штатным `ops/measure/poll-rate.py`. Ожидаемые значения по каждому эндпоинту
даны в таблице выше и следуют из констант и гейтов в коде (каждый проверен
чтением места вызова).

## Изменённые файлы

Код:
- `src/contrib/runtime-loader.ts` — период опроса диска плагинов 60 с
- `src/lib/browser-ui-preferences.ts` — период 60 с
- `src/app/hooks/use-config-record.ts` — `staleTime` 30 с вместо 0
- `src/app/session/hooks/use-hermes-config.ts` — общая пара config+defaults в полёте
- `src/app/contrib/hooks/use-background-sync.ts` — тик списка сессий/крона 30 с
- `src/app/settings/webapp-gateway-settings.tsx` — подключения 30 с
- `src/plugins/hermes-bots/relay.ts` — дренаж 60 с
- `src/store/profile.ts` — сигнатурный гейт `$profiles` + single-flight `refreshActiveProfile`

Тесты:
- `src/store/profile-active-refresh.test.ts` (новый, 3 теста)
- `src/store/profile.test.ts` (+2 теста на identity `$profiles`)
- `src/app/session/hooks/use-hermes-config.test.ts` (+тесты дедупликации)
- `src/plugins/hermes-bots/relay.test.ts`, `src/app/contrib/hooks/use-background-sync.test.ts` (правка ожиданий под новые периоды)

Вне кода: `PROGRESS-t_dd5c9400.md` (этот отчёт).
