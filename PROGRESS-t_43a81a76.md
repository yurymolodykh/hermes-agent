# PROGRESS — t_43a81a76 (кэши обвязки, продолжение t_77d98767)

**Состояние: шаг 1 (.env) СДЕЛАН и проверен. Шаги 2 (подпись навыков) и 3 (дескрипторы) НЕ сделаны.**
Прогон остановлен по бюджету после первого доведённого инкремента. `PROGRESS.md` в дереве оставлен
как заметки предыдущей карточки (t_77d98767), этот файл — состояние t_43a81a76.

## Шаг 1 — `.env` без чтения файла на горячем пути: СДЕЛАНО

`agent/secret_scope.py::load_env_file`:
- ключ мемо теперь `(file_signature(st), raw, secrets)`; попадание по сигнатуре **не открывает файл**;
- **racу-правило**: пока `mtime_is_racy(max(mtime_ns, ctime_ns))`, сигнатуре не доверяем и читаем байты
  (точная сверка, как раньше) — это ровно инцидент CI 39146191939 (coarse-clock правка того же размера
  в одном тике); открытие пропускается только у «устоявшегося» файла (> 2 с);
- на промахе — прежний путь open+read, сверка байтов, при совпадении байтов разбор пропускается;
- удаление/нечитаемость → `invalidate_env_file_cache` + `{}`, фантомной записи не остаётся;
- комментарий-обоснование (`agent/secret_scope.py:317-341`) обновлён: объясняет, как сигнатура ловит
  каждый класс перезаписи (атомарная замена → inode, in-place → ctime, тот же тик → сверка байтов).

**Измерено (interleaved A/B на одном файле 26,7 КБ, `cache/scratch/ab_env2.py`, устоявшийся файл):**
| | median | p95 |
|---|---|---|
| база `load_env_file` (open+read+parse) | **15,14 µs** | 23,47 µs |
| патч (попадание без open) | **6,66 µs** | 6,82 µs |

2,3× на реальном `build_profile_secret_scope`. Оговорка измерена: на свежем (<2 с) файле выигрыша НЕТ
(ab_env.py: 25,35 µs против 14,99 µs) — это цена корректности, а не дефект.

**Тесты.** `tests/agent/test_secret_scope_env_cache.py`: в базе 3 теста (не 5, как в постановке) — все
зелёные; добавлены 3:
- `test_a_settled_hit_does_not_open_the_file` — попадание не открывает файл (счётчик `builtins.open`);
- `test_a_settled_edit_is_visible_on_the_very_next_call` — правка видна в следующем вызове (append и
  атомарная замена с восстановленным mtime);
- `test_deleting_the_file_drops_the_memo_entry` — удаление сбрасывает запись, пересоздание свежее.

Ловушка для потомков: тесты должны патчить **`ss.mtime_is_racy` / `ss.file_signature`** (модуль связывает
имена при импорте); патч `utils.mtime_is_racy` не действует и тест молча падает.

**Обязательный набор из постановки:** `HOME=/tmp/t77 ../../.venv/bin/python -m pytest
tests/agent/test_secret_scope_env_cache.py tests/tools/test_skills_tool_discovery_cache.py
tests/agent/test_skill_commands_freshness.py tests/hermes_state/test_session_db_read_conn_pool.py
tests/hermes_cli/test_read_handle_pool_wiring.py -q` → **48 passed**.
Дополнительно `tests/agent/test_secret_scope*.py` + `test_env_loader_secret_sources.py` → 65 passed.

## Что осталось

### 2. Кэш подписи навыков — СДЕЛАНО ЧАСТИЧНО: обход подешевел, TTL отклонён

Предложенный в постановке TTL 0,2–0,5 с реализован, **измерен и ОТКАЧЕН**: он валит
`tests/agent/test_skill_commands_freshness.py::test_skill_added_on_disk_after_the_first_scan_is_served_without_reload`
и `::test_a_skill_installed_under_an_existing_category_subdir_is_served` — оба ставят навык между двумя
вызовами и требуют увидеть его СРАЗУ. TTL здесь — регресс корректности, а не настройка; это записано в
docstring `_skills_scan_signature`, чтобы следующий воркер не переоткрывал.

Вместо TTL уменьшено число syscall в обходе: `_dir_tree_signature` теперь делает **один `os.scandir` на
каталог** — имена детей и mtime каждого ребёнка берутся из того же readdir (`DirEntry.stat` уже заполнен
ядром), явный `os.stat` остаётся только для корня. Прунинг (`EXCLUDED_SKILL_DIRS`, support-каталоги
навыка) и переход по симлинкам не изменены.

Проверено `cache/scratch/sig_ab.py` (база vs патч, 259 строк, есть исключённые каталоги и симлинк):
**подписи побайтово идентичны**, число строк совпадает; обход **4060 → 3847 µs (1,06×)**.
Честная оценка: обход упирается в readdir, а не в stat, поэтому порядок величины даёт только кэш,
а кэш запрещён контрактом свежести. Тесты: весь навыковый срез `tests/tools/test_skill_*.py` +
`test_skills_tool*.py` → **382 passed**.

### 3. Дескрипторы — выигрыша НЕТ, закрыто замером

`cache/scratch/split_fd.py` (2000 итераций): `_fd_headroom_ok` на прогретом мемо **1,47 µs**,
`_fd_soft_limit` 1,11 µs, `_open_fd_count` 9,3 µs. `get_session` = **72,45 µs** штатно,
**72,73 µs** с гардом, застабленным в `True`, **74,45 µs** с вечно пустым мемо. То есть вклад гарда
**ниже шума** (потолок ~2 %); остальное — `_session_row_dict` (SQLite row → dict) и сам запрос.
Поднимать TTL/выносить в таймер не даёт измеримого выигрыша и усложняет инвариант «fail closed только
по факту». **Рекомендация: пункт закрыть как исполненный-замером, а не кодом.**
Тесты `tests/hermes_state/test_session_db_read_conn_pool.py:575-620` не тронуты — зелёные.

## Итог по трём точкам

| точка | было | стало | чем доказано |
|---|---|---|---|
| `load_env_file` (устоявшийся .env 26,7 КБ) | 15,14 µs | **6,66 µs** (2,3×) | `ab_env2.py`, interleaved A/B |
| `_skills_scan_signature` (259 каталогов) | 4060 µs | **3847 µs** (1,06×) | `sig_ab.py`, подписи идентичны |
| `_fd_headroom_ok` / `get_session` | 1,47 / 72,45 µs | без изменений | `split_fd.py`, вклад ниже шума |

## Стенды и локаторы

- `probe_hotpaths.py`, `bench_obv.py` — в дереве (унаследованы из t_77d98767).
- База: `/home/hermes/.hermes/profiles/03-software-engineer/cache/scratch/base_tree`
- Пробы: `cache/scratch/micro.py`, `split_fd.py`, `ab_env.py`, `ab_env2.py`
- **Шум хоста:** `probe_hotpaths.py` по одному дереву даёт разные цифры в разные минуты
  (build_profile_secret_scope 76,5 → 90,5 → 106 → 165 µs; find_all_skills 3775 → 4718 → 5632 → 7501 µs).
  Сравнимы ТОЛЬКО interleaved A/B в одном прогоне (`ab_env2.py`), не «до/после» разными запусками.

## Следующий шаг

Шаг 2 по рецепту выше → обязательный pytest-набор → interleaved A/B по `find_all_skills` → шаг 3 закрыть
замером из `split_fd.py` → коммит → публикация по гейту `t_f8450188`
(`ops-card-merge.py --task <id> --dry-run` → `already_ancestor: true`) → `kanban_request_review` (09).
