# PROGRESS — t_77d98767 (кэши пер-запросной обвязки)

**Состояние: ФАЗА 1 (замеры и разбор) закрыта. Правок продуктового кода НЕТ.** Прогон остановлен
по бюджету времени до первой правки. Дальше — только шаги 1–4 ниже, они расписаны до строчки.

## Что уже сделано и где лежит

Дерево карточки: `/home/hermes/.hermes/hermes-agent/.worktrees/t_77d98767` (ветка `wt/t_77d98767`).
База сравнения (merge-base, `1456950ab7d`), распакована из `git archive` — **git-каталога нет**,
поэтому `BASE_TREE`-стенды заводятся этим путём:

```
/home/hermes/.hermes/profiles/03-software-engineer/cache/scratch/base_tree
```

### Стенды (в корне дерева, коммитятся)

1. `bench_obv.py <tree> [out.json] [window]` — приёмочный стенд: py-spy 15 с + 8 одновременных
   `/api/status` на реальном uvicorn-приложении, считает долю кадров-точек, CPU процесса и
   латентность. **Важно:** attach к чужому процессу запрещён (`/proc/sys/kernel/yama/ptrace_scope
   = 1`, py-spy → `Permission Denied`), поэтому py-spy **запускает** сервер сам
   (`py-spy record -d N -f raw -o prof -- python server.py`), а бенч только гоняет нагрузку.
   `-d` должен быть целым числом (строка `15` роняет py-spy: `ParseIntError`).
2. `probe_hotpaths.py <tree> [iters]` — прямая стоимость трёх точек по путям, которые реально
   исполняет запрос (`build_profile_secret_scope`, `find_all_skills`, `SessionDB.get_session`),
   с числом вызовов нижнего уровня.

### Замеры «до» (база `1456950ab7d`)

`bench_obv.py`: 1663 сэмпла, окно 12,1 с, 3295 запросов, CPU процесса 11,17 с → **92 % одного
ядра** под нагрузкой 8×`, латентность `/api/status` median 27,3 мс, p95 33,8 мс, max 447 мс.
**Три целевые рамки на этом стенде НИЖЕ разрешения сэмплирования** (0 зафиксированных сэмплов на
`load_env_file`, `_dir_tree_signature`, `_open_fd_count`): задача, которая ждёт от py-spy падения
доли этих трёх точек, на тестовом стенде цифру не даст — доля видна только на живом шлюзе
(там она была 5,4 / 2,7 / 3,0 % при 148 потоках и 790 МБ). Это ограничение приёмки, его надо
писать в отчёт, а не обходить.

`probe_hotpaths.py` (300 итераций) — вот где стоимость видна и её надо снимать:

| точка | база, µs/оп | нижний уровень на 300 итераций |
|---|---|---|
| `build_profile_secret_scope` | **76,5** (p95 89,5) | `_parse_env_text` — 0 вызовов (мемо уже есть), файл всё равно читается на каждый вызов |
| `find_all_skills` | **3775** (p95 4000) | `_dir_tree_signature` — **65 016 вызовов** (217 на один листинг: полный обход ~170 каталогов каждый раз) |
| `SessionDB.get_session` | 66,3 | `_open_fd_count` — 1 листинг (мемо 0,25 с уже есть) |

## Что выяснилось про «уже сделано» (это меняет объём)

Кэши из постановки **частично существуют в базе**, и их надо не «написать», а доделать:

1. `.env` — мемо уже есть (`_ENV_FILE_CACHE`, LRU 64, ключ = точные байты файла, каждый вызов
   **читает файл** и только пропускает разбор; `invalidate_env_file_cache()` — ручка сброса).
   Остаток работы: убрать чтение файла с горячего пути по `file_signature()` (mtime, size, inode,
   ctime — `utils.py:42`) с обязательной сверкой байтов, когда сигнатура изменилась, и с
   сохранением комментария-обоснования из `agent/secret_scope.py:317-336` (там записан инцидент
   CI 39146191939: coarse-clock перезапись того же размера внутри одного тика). Готовый тест
   контракта — `tests/agent/test_secret_scope_env_cache.py` (5 тестов, включая оба сценария
   свежести) — его нельзя ломать.
2. Навыки — кэш каталога есть (`_SKILLS_CACHE`, TTL 30 с), но **подпись считается полным обходом
   дерева на каждый вызов** (217 `_dir_tree_signature` = 3,7 мс чистой стоимости листинга).
   Остаток работы: посчитать подпись по mtime только тех каталогов, что реально меняются, с
   коротким TTL (0,2–0,5 с) — свежесть установки навыка обязана остаться «сразу».
3. Дескрипторы — мемо уже есть (`_FD_USAGE_CACHE_SECONDS = 0.25`, проверяется в
   `_fd_headroom_ok`), поведение при исчерпании (`_read_open_denied_fd_headroom`, отказ → откат на
   писательское соединение) покрыто тестами `test_fd_headroom_guard_*`. Остаток работы: вынести
   замер в фоновый таймер (или увеличить TTL с сохранением семантики «fail closed только по
   факту»), не меняя ни один из этих тестов.

## Следующий шаг (по порядку)

1. `agent/secret_scope.py::load_env_file` — сигнатура `file_signature(os.stat())` → попадание без
   `open()`; промах → прежний путь «прочитать байты → сверить → разобрать». Тест: существующий
   файл `test_secret_scope_env_cache.py` зелёный + новый тест «попадание не открывает файл»
   (monkeypatch на `builtins.open`/счётчик) и «правка видна в следующем вызове».
2. `tools/skills_tool.py::_skills_scan_signature` — кэш подписи по (корни, disabled) с TTL
   `_SKILLS_SIGNATURE_TTL_SECONDS = 0.25` и сбросом из `clear_skills_cache()`. Тесты:
   `tests/tools/test_skills_tool_discovery_cache.py`, `tests/agent/test_skill_commands_freshness.py`
   (там же сценарии «навык установлен/удалён — видно сразу»; TTL ≤0,25 с их не ломает).
3. `hermes_state_readpool.py` — таймер (threading.Timer/фоновый поток, остановка по weakref) или
   TTL 1–2 с; тесты `tests/hermes_state/test_session_db_read_conn_pool.py` (строки 575–620
   monkeypatch-ят `_fd_usage_cache` и `_open_fd_count` — контракт сохранить дословно).
4. Прогон: `HOME=/tmp/t77 ../../.venv/bin/python -m pytest tests/agent/test_secret_scope_env_cache.py
   tests/tools/test_skills_tool_discovery_cache.py tests/agent/test_skill_commands_freshness.py
   tests/hermes_state/test_session_db_read_conn_pool.py tests/hermes_cli/test_read_handle_pool_wiring.py -q`
   (`HOME` обязателен — иначе `tests/home_io_guard.py`), затем `probe_hotpaths.py` по своему дереву
   и `bench_obv.py` по базе и по своему дереву той же методикой.
5. Коммит в `wt/t_77d98767` → публикация `ops-card-merge.py --task t_77d98767` (гейт публикации
   унаследован из `t_f8450188`; порядок и запреты — в теле той карточки) → `kanban_request_review`
   (09).

## Локаторы

- Бенч-база: `/home/hermes/.hermes/profiles/03-software-engineer/cache/scratch/base_tree`
- Замер «до» (JSON): `/home/hermes/.hermes/profiles/03-software-engineer/cache/scratch/bench_before.json`
- Живой профиль для сверки: `/home/hermes/.hermes/profiles/*/.env` (~27 КБ), дерево навыков ~170
  каталогов — `probe_hotpaths.py` воспроизводит этот масштаб.

## Ограничения, которые нельзя терять из вида

- Кэш не имеет права отдать устаревшие секреты или устаревший список навыков (границы карточки).
- `state.db` общий для всех профилей: транзакционную семантику не трогать.
- Checkpoint-снапшоты в этом прогоне не снимались (store busy) — откат только через git.
