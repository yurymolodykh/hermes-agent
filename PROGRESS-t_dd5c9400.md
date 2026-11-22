# PROGRESS — t_dd5c9400: разредить фоновый опрос веб-панели (116 → ≤25 запросов/мин)

Дерево: `/home/hermes/.hermes/hermes-agent/.worktrees/t_dd5c9400`, ветка `wt/t_dd5c9400`,
база — `feature/multiuser-access` = `beeb9623539`.

## Состояние: КРУГ 2 ЗАВЕРШЁН И ОПУБЛИКОВАН

* Верхушка ветки: `7c9fc07399307e3b8b323ebeb31a917af07c5e85`
* Влито в `feature/multiuser-access` = `b55cbeb3c64ad75328b29e3d812d1d34daefbf34`,
  отправлено в `ours`, readback совпал; расписка
  `/home/hermes/workspaces/01-orchestrator/ops/state/card-publish/t_dd5c9400.json`
  (`verified/merged/published: true`), `--dry-run` → `already_ancestor: true`.
* Отчёт: `REPORT-t_dd5c9400.md` (обновлён кругом 2).

## Круг 2 — что было дефектом и что исправлено

Ревью (оркестратор) нашло реальную регрессию: `SESSIONS_LIST_TICK_GAP_MS` 10 000 → 30 000
стал не только ограничителем частоты, но и задержкой ответа на событие «ход закончился».
Тик внутри окна вооружал trailing-таймер на остаток окна → до 30 с ожидания.

Исправление в `apps/desktop/src/app/contrib/hooks/use-background-sync.ts`:
подписка на `$busy`; при `busy → false`, если trailing-таймер вооружён, он снимается и
проход выполняется немедленно. Простаивающая вкладка (`$busy` не двигается) не получает
дополнительных чтений. Интервал остался 30 000. Тест не менялся.

Проверено: `use-background-sync.test.ts` 67 passed; шесть изменённых модулей 152 passed /
0 failed; `typecheck` exit 0; `build:webapp` ✓ built in 17.45s, `dist-webapp/index.html`
перезаписан (19:28), `.hermes-stale` нет.

## Остаточное (вне этой карточки)

* Живой замер опроса на новой сборке — карточка выката t_8af52d4a
  (`ops/measure/poll-rate.py`); при недостижении ≤25/мин откатывается эта правка.
* Независимое ревью — `09-independent-reviewer`.
