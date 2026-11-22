# PROGRESS — t_dd5c9400: разредить фоновый опрос веб-панели (116 → ≤25 запросов/мин)

Дерево: `/home/hermes/.hermes/hermes-agent/.worktrees/t_dd5c9400`, ветка `wt/t_dd5c9400`,
база — `feature/multiuser-access`.

## Замер (nginx, 22.11.2026)

Окно 13:14–13:19 (6 мин):

```
GET /api/desktop-plugins/root             11.8/m  gaps=5s
GET /api/desktop-plugins/list             11.8/m  gaps=5s
GET /api/ui-preferences                    6.0/m  gaps=10s
GET /api/profiles                          5.0/m
GET /api/profiles/sessions/sidebar         4.7/m  gaps=10s
GET /api/cron/jobs                         4.5/m  gaps=10s
GET /api/config                            4.2/m  пачки gap=0
GET /api/profiles/sessions                 4.2/m  gaps=10s
GET /api/gateway-connections               3.7/m  gaps=30s
GET /api/config/defaults                   3.0/m  пачки gap=0
GET /api/audio/voice-live/status           1.7/m
GET /api/status                            1.5/m  gaps=60s
GET /api/model/info                        1.5/m
итого                                     66.8/m
```

Полное окно 10 мин (карточка): 116.4/мин — разница из-за окон с активной вкладкой.

## Найденные драйверы и что сделано

1. `src/contrib/runtime-loader.ts` — `DISK_POLL_MS 5_000 → 60_000`
   (устаревший Electron без `watchDirectory`; 2 запроса/тик: root+list).
2. `src/lib/browser-ui-preferences.ts` — `POLL_MS 10_000 → 60_000`.
3. `src/app/hooks/use-config-record.ts` — `staleTime 0 → HERMES_CONFIG_STALE_MS 30_000`
   (убирает пачки 5–7 `/api/config` в одну секунду при перемонтировании).
4. `src/app/contrib/hooks/use-background-sync.ts` —
   `SESSIONS_LIST_TICK_GAP_MS 10_000 → 30_000` (тянет `refreshSessions` → `refreshCronJobs`).
5. `src/app/settings/webapp-gateway-settings.tsx` — `SAVED_CONNECTIONS_POLL_MS 5_000 → 30_000`.
6. `src/plugins/hermes-bots/relay.ts` — `RELAY_DRAIN_INTERVAL_MS 30_000 → 60_000`
   (тик = routes + registry + served-set).
7. `src/store/profile.ts` — сигнатурный гейт публикации `$profiles`.
   Устраняет цикл: новый массив → `useUnservedProfiles` (`[profiles]`) → roster
   (`/api/profiles` + `/api/gateway-connections` + `/api/status`).

## Остаётся

- `pnpm run build:webapp` (фон, session `proc_a7d60e9cfeed`).
- Типы/линт затронутых файлов.
- Тесты затронутых модулей.
- Коммит + `git show --stat`.
- `kanban_request_review` → `09-independent-reviewer`.
