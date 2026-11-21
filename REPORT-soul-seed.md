# SOUL-клон для `01-<login>` + опросник персонализации

Карточка: `t_819d3353`. Ветка: `wt/t_819d3353`, worktree
`/home/hermes/.hermes/kanban/workspaces/t_819d3353`, база `0a1960695e`.

## Что сделано

### 1. Клон SOUL подключён к созданию профиля

`hermes_acl_soul.py` (новый, ~560 строк, только stdlib) — единственная точка преобразования:

* `personal_soul_for(name)` → текст SOUL для имени `01-<login>`, `None` для любого другого имени
  (правило имени: `PERSONAL_ROOT_PREFIX = "01-"` + `normalize_login`, отсекает `01-`, `default`,
  `02-…`, `16-personal`);
* `build_user_soul(login)` — собирает документ: снимает служебные разделы
  (`Маршрутизация`, `Матрица проверки`, `Режимы работы`, `Выбор режима выполнения`, `Модель и
  handoff`, `Канон принципов`, `Источники и RAG`, `Self-check P6`, `Checkpoint P7`), обезличивает
  остаток (`02-…`→`специалист`, `01-orchestrator`→`ассистент`, абсолютные пути→`~/`), подставляет
  роль «персональный ассистент <login>» и генерирует разделы, в которые пишет опросник;
* источник текста — SOUL оркестратора, при его отсутствии `hermes_cli/data/user_soul_base.md`
  (bundled). Машина без `01-orchestrator` получает полноценный документ;
* `apply_questionnaire(text, answers)` — правит только названные разделы, остальное байт-в-байт;
* `seed_questionnaire(dir)` / `questionnaire_text()` — опросник;
* `main()` — CLI-обёртка (`python -m hermes_acl_soul --login alice …`); ops-скрипт
  `/home/hermes/workspaces/01-orchestrator/ops/soul-clone-for-user.py` переписан в тонкую обёртку
  над этим модулем (правка больше не живёт только в ops).

Швы в платформе:

* `hermes_cli/profiles_initialization.py` — `personal_soul_seed(name)` (best-effort: сбой →
  `None` → прежний `DEFAULT_SOUL_MD`, создание профиля не падает из-за сеяния) и
  `materialize_symlinked_files` (перенесён из фасада `profiles.py`, который за лимитом строк и
  может только убывать);
* `hermes_cli/profiles.py::create_profile` — при `soul is None` и имени `01-<login>` берёт
  персональный SOUL; передаёт `personal_root=True` в `finish_profile_layout`;
* `finish_profile_layout` — пишет SOUL и кладёт `SOUL-QUESTIONNAIRE.md` в каталог профиля;
* `hermes_cli/profile_cmd.py` (CLI) — печатает путь к опроснику, если он появился.

Явно переданный `soul` всегда побеждает. Клонирование (`--clone-config` / `--clone-all`) в
`01-<login>` тоже даёт персональный SOUL, а не протокол служебного профиля — `finish_profile_layout`
пишет SOUL после копирования дерева.

### 2. Опросник выдаётся

`hermes_cli/data/soul-questionnaire.md` — 6 блоков (кто ты / как отвечать / границы / как вести
работу / термины и источники / своё), рядом с каждым полем — ключ API. Правило: пустой ответ =
оставить по умолчанию. Копируется в каталог профиля как `SOUL-QUESTIONNAIRE.md`.

`create_profile_endpoint` (`POST /api/profiles`) возвращает `questionnaire_path` — путь к файлу
для персонального профиля, `""` для остальных (`_questionnaire_path` не сообщает путь к
несуществующему файлу). Поле аддитивное: `soul_set` и остальные ключи не менялись, десктопный
клиент (`create-profile-dialog.tsx`) продолжает работать как раньше.

### 3. Применение ответов

`POST /api/profiles/{name}/soul/apply-questionnaire`, тело — `ProfileSoulQuestionnaire`
(`hermes_cli/web_models.py`, все поля необязательные). Права: `acl_profiles.require_writable` —
чужая запись 404 (имени для вызывающего не существует), общий профиль 403, владелец проходит.
Ответы 1–4 правят разделы «Роль», «Ответ пользователю», «Границы», «Как вести работу»; факты 5–6
пишутся в память профиля через тот же стор, что и инструмент `memory`
(`load_on_disk_store` под `config_scoped_to_thread(name, …)` — свой бакет, не профиль запуска).
Ответ: `changed`, `changed_sections`, `soul_sha256`, `memory_entries`, `memory_error`.
Запись SOUL атомарная (`utils.atomic_write_text`). Пустой SOUL → 400. Сбой памяти не 500-ит:
SOUL уже сохранён, ошибка сообщается полем.

### 4. Права не расширены

`require_create` вызывается как раньше, до всего остального; новых путей создания нет.

### Совместимость и упаковка

`pyproject.toml` — `hermes_cli` package-data получает `data/*.md` (иначе sealed wheel не привезёт
ни опросник, ни bundled-базу и персональный профиль молча откатится на `DEFAULT_SOUL_MD`).
Модуль верхнего уровня попадает в `py-modules` автоматически (`setup.py::_root_py_modules`).

## Приёмка — предъявления

Живой прогон: `evidence/live_probe.py` против `TestClient` на изолированном `HERMES_HOME`
(`~/.hermes/profiles/03-software-engineer/cache/scratch/soulprobe-*`), вывод —
`evidence/live_probe.final.json` (перегенерирован в этом прогоне):

| п. | требование | факт |
|---|---|---|
| (а) | `01-testuser` через API, роль, 40–60 строк, нет служебного жаргона | `create_status=200`; `soul_lines=60`, `soul_chars=4025`; `Ты — персональный ассистент testuser. …`; `forbidden_hits` = 0 по всем шести маркерам (`01-orchestrator`, `Kanban`, `карточк`, `РГЗ`, `upgreat`, `needs_source`) |
| (б) | опросник в каталоге, путь в ответе | `questionnaire_exists=true`; `questionnaire_path=/…/profiles/01-testuser/SOUL-QUESTIONNAIRE.md` в теле ответа create |
| (в) | заполнить → правка только там | `apply_status=200`, `changed_sections=["Роль","Границы","Ответ пользователю"]`, diff (`apply_diff`) — три вставки/замены в этих разделах, «Как вести работу» и инварианты не тронуты; sha до `0621417f…` → после `4d307ea7…` |
| (г) | пустой → байт-в-байт | `empty_identical=true`, sha `4d307ea7…` до и после, `changed=false` |
| (д) | чужой namespace → 403 | `foreign_create_status=403`, `{"detail":"profile_not_allowed"}`; свой корень — `own_create_status=200` |
| (е) | тесты | `tests/hermes_cli/test_profile_soul_seed.py` — **49 passed** (требовалось ≥10); регрессия `tests/hermes_cli/test_profile_route_owner_scope.py` — **73 passed**, не уменьшилось |

Дополнительно проверен **CLI-путь** (не входит в букву приёмки, но это второй шов из п.1):
`hermes profile create 01-cliuser` → 60 строк, роль «Ты — персональный ассистент cliuser»,
`SOUL-QUESTIONNAIRE.md` рядом; `hermes profile create plaingroup` → прежний `DEFAULT_SOUL_MD`
(1 строка), опросника нет. Профили-зонды после проверки удалены из живого `~/.hermes`.

Ограничение из тела карточки проверено: раздел «Общие профили-специалисты» остаётся и читается
осмысленно — «есть общие профили-специалисты: поиск внешних источников, документы, визуалы,
расчёты, проверка результата. Это общий ресурс, а не чужие вещи: к ним можно обращаться с задачами
по их теме и пользоваться их результатами; менять их настройки нельзя.» Номера профилей не
утекают (`02-…`→`специалист`), заглушки нет.

## Регрессия шире буквы карточки

Прогон в копии дерева вне `~/.hermes` (`/tmp/soulbranch2`; `tests/home_io_guard.py` рубит I/O по
реальному дому):

* `test_profiles.py` + `test_profile_clone_channels.py` + `test_web_profile_soul_writes.py` —
  **129 passed, 3 skipped** (внутри `~/.hermes` те же файлы падают на home_io_guard — не дефект
  правки);
* `test_native_profile_route_names.py` + `test_profile_soul_seed.py` +
  `test_profile_route_owner_scope.py` — **135 passed**;
* `tests/gateway/test_native_profile_create.py` — **флейки на старте шлюза**, не связан с правкой:
  на базе `0a1960695e` тот же тест падает так же (1 failed из 3 прогонов на базе и на ветке,
  в остальных проходит). Предсуществующая гонка готовности гейтвея в тесте, не регрессия.

## Гигиена кода

* `python -m scripts.code_health --base 0a1960695e` → **0 blocking, 0 advisory** (8 файлов);
* `ruff check` по 7 файлам — All checks passed;
* переиспользован существующий путь записи SOUL (`atomic_write_text`), существующий ACL
  (`require_writable` / `require_create`), существующий стор памяти — новых механизмов нет.

## Остаточное

* `manual_review`: показ опросника в десктопном клиенте — вне рамок карточки (API отдаёт путь,
  интерфейс — отдельная тема).
* `manual_review`: миграция уже существующих профилей — вне рамок.
* Слияние в `feature/multiuser-access` и публикацию делает оркестратор (страж деревьев).

Верхушка ветки: см. `kanban_complete` (`metadata.commit`).
