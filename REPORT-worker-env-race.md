# Гонка провижининга окружения: мост install-key на пути спавна воркера

Карточка: `t_44bd1c6a` · ветка `wt/t_44bd1c6a` · база `b8c2c25eaf35`

## 1. Причина (подтверждена первоисточником, а не по описанию)

Воркер запускается как `python -m …` с `cwd = workspace`. При `-m` интерпретатор ставит `""`
(текущий каталог) первым в `sys.path`, поэтому `import hermes_bootstrap` берёт модуль **из дерева
карточки**, а `hermes_bootstrap._root` указывает на это дерево. PM ключует состояние зависимостей
по пути дерева: `install_key(path) = sha256(realpath(path))[:16]`, состояние —
`<home>/installs/<key>`.

Свежесозданный git-worktree имеет свой ключ, и под ним ничего не закоммичено. Симлинк
`installs/<key(дерева)> → installs/<key(главного дерева)>` ставил **таймер**
(`ops/bin/card-worktree-provision.py`, крон `kanban-worktree-provision`, раз в минуту), поэтому
первые спавны нового дерева проигрывали гонку и падали на bootstrap с

```
NoCommittedEnvironment: no dependency environment is committed for this install
```

Живая проба воспроизводит это на дереве-близнеце без моста — см. §4 (строка «red baseline»).

Замер по `~/.hermes/kanban.db`: 19 прогонов на 10 карточках, длительность 60–72 с (min 60,
медиана 63, max 72) — падение всегда на bootstrap, никогда в работе, то есть бюджет неудач
карточки сгорал, хотя работа не начиналась.

## 2. Что изменено

### `hermes_cli/kanban_db_worker_env.py` (новый)

Единственная новая точка политики. Две обязанности, обе — сторона **спавна**, не загрузки:

| функция | назначение |
|---|---|
| `ensure_tree_install_state(workspace, *, source_root=None) -> (bool, reason)` | гарантирует, что дерево воркера найдёт закоммиченное окружение: либо своё, либо через мост install-key |
| `worker_terminal_timeout_env(max_runtime, current)` | перенос тела из диспетчера (чтобы фасад не рос) |

Ключевые решения:

* **Мост ставится до старта воркера.** Вызов стоит в `_dispatch_lane_task` сразу после
  материализации workspace (`resolve_workspace` / `_resolve_worktree_workspace`) и **до**
  `spawn_fn`. Таймер остаётся страховкой для деревьев, в которые этот диспетчер не спавнит.
* **Один хоп, всегда.** Источник моста берётся не обходом ФС вверх, а по git:
  `git rev-parse --git-common-dir` для linked worktree даёт `<главный checkout>/.git`. Поэтому
  диспетчер, запущенный из дерева карточки, ставит ссылку на **реальный** install-каталог, а не на
  другую ссылку (цепочка ломается от одного переименования). `source.resolve()` — вторая гарантия.
* **Идемпотентность.** Существующая верная ссылка не пересоздаётся (тест сверяет inode); висячая
  ссылка чинится; ссылка на другой install или реальный каталог на её месте — это **решение**, а не
  гонка, и она возвращает причину, а не перезаписывается.
* **Готовность = ответ PM.** Проверка идёт через `pm.environments.committed_venv`, то есть ровно
  то, что примет `activate_dependencies` при загрузке. Своей реплики критериев нет.

### `hermes_cli/kanban_db_capacity.py` (новый)

`count_running_tasks` и `count_running_tasks_other_boards` вынесены из диспетчера: правка
затрагивает измеряемый файл, а ratchet `scripts/code_health` разрешает фасаду только ужиматься.
Оба зонда по-прежнему «fail open» (0): сломанная доска не должна останавливать диспетчеризацию
здоровых.

### `hermes_cli/kanban_db_dispatch.py` (только швы, −рост относительно базы)

* импорты `_kbenv`, `_kbcap`;
* `DispatchResult.skipped_env_unready: list[tuple[task_id, reason]]`;
* `_hold_if_workspace_unready(...)` — вызов моста и отказ от спавна;
* `_record_nonspawnable_assignee(...)` — вынос существующего блока из `_dispatch_lane_task`
  (функция упиралась в CC 20 при добавлении ветки);
* шимы `_worker_terminal_timeout_env`, `count_running_tasks`, `count_running_tasks_other_boards`
  (публичные имена импортируют `gateway.session_kanban`, `hermes_cli.kanban_db_context`,
  `tests/hermes_cli/test_kanban_host_cap.py`);
* строка `env_unready=N` в `describe_suppression` — удержание не читается как «диспетчер встал».

### Семантика отказа

Если мост поставить нельзя (нет committed-окружения у install, на который мы работаем; нет прав на
каталог installs; ссылка ведёт в другой install), то:

* воркер **не стартует**;
* карточка **остаётся `ready`**, `consecutive_failures` **не растёт**
  (`_record_task_failure(..., infrastructure=True)`);
* причина пишется на карточку событием `spawn_failed` и в лог диспетчера, и попадает в
  `DispatchResult.skipped_env_unready` с машинным маркером
  `dependency_environment_unready`.

Ключевой пул, `pm repair` и число ключей не затронуты.

## 3. Приёмка: чем проверено

### Новый набор `tests/hermes_cli/test_kanban_worker_env_provisioning.py` (10 тестов)

1. `test_spawn_bridges_the_install_link_before_the_worker_starts` — внутри `spawn_fn` проверяется,
   что ссылка **уже** существует, не висит и ведёт на install-каталог главного дерева, а
   `committed_venv(workspace)` возвращает ту же venv, что выберет загрузка.
2. `test_worker_boot_after_the_bridge_does_not_raise_no_committed_environment` — реальный
   `hermes_bootstrap` в дочернем процессе на **store-python** (`sys._base_executable`), `cwd` =
   дерево карточки, наш код на `PYTHONPATH`. Сначала доказывается красная база (без моста —
   `no dependency environment is committed`), затем после моста — код возврата 0, а `ROOT`
   остаётся деревом карточки (изоляция worktree не сломана).
3. `test_existing_correct_link_is_left_alone` — повторный вызов не пересоздаёт ссылку (inode).
4. `test_dangling_link_is_replaced` — висячая ссылка чинится.
5. `test_tree_without_a_hermes_checkout_needs_no_bridge` — scratch-каталог ничего не получает.
6. `test_source_root_is_the_main_checkout_of_a_linked_worktree` — источник моста определяется по
   git, а не по расположению ФС.
7. `test_no_spawn_when_the_running_install_has_no_dependencies` — спавна нет, карточка `ready`,
   `consecutive_failures == 0`, событие `spawn_failed` есть.
8. `test_no_spawn_when_the_link_cannot_be_created` — `PermissionError` на `symlink_to` тоже даёт
   причину, а не цикл падений.
9. `test_env_unready_is_named_in_the_suppression_summary` — `env_unready=1` в
   `describe_suppression`.
10. `test_unready_card_is_retried_once_the_environment_appears` — удержание это ожидание, а не
    блокировка: на следующем тике карточка спавнится.

### Результаты

| проверка | результат |
|---|---|
| новый набор (копия дерева `/tmp/t44branch`) | **10 passed** |
| новый набор + 8 смежных канбан-наборов | **54 passed** |
| красная база `b8c2c25eaf35` + новый модуль | **5 failed** из 10 (спавн-путь: ссылки нет, спавн происходит, `env_unready` пуст) |
| красная база без нового модуля | ImportError на сборе — набор не может существовать без фикса |
| `python -m scripts.code_health --base HEAD` | **0 blocking, 0 advisory** |
| `ruff check` (4 файла) | All checks passed |
| `scripts/boundary_lint.py --check` | те же предсуществующие кроссы, что на базе; дельта 0 |
| живая проба на боевом `HERMES_HOME` | **14/14 OK**, включая красный бейзлайн |

Прогоны делались в копии дерева вне `~/.hermes`: `tests/home_io_guard.py` намеренно рубит
инициализацию БД внутри реального дома, и это ограничение тестов, а не продукта.

## 4. Живая проба (`live_probe_worker_env.py`, 14/14 OK)

Проба идёт тем же путём, что диспетчер: создаёт linked worktree от этого репозитория, зовёт
`ensure_tree_install_state`, затем запускает **store-python** с `cwd` = новое дерево.

```
store python: /home/hermes/.hermes/tools/python-3.14.9+…/bin/python3.14
OK  fresh worktree created
OK  HERMES_HOME is the real platform home — /home/hermes/.hermes
OK  fresh tree starts with no install state — /home/hermes/.hermes/installs/a8f061eb4f577f65
OK  fresh tree resolves no committed environment
OK  ensure_tree_install_state reports ready — 3 ms
OK  install state is a symlink now — …/installs/019d0d114d22b7e6
OK  link points at ONE real install (no chain)
OK  fresh tree resolves the committed environment — …/environments/58568b…/venv
OK  worker boot exits 0 in the fresh tree — ROOT /tmp/t44probe/wt-fresh
OK  no 'no dependency environment is committed'
OK  the tree, not the caller, owns the boot root — ROOT /tmp/t44probe/wt-fresh
OK  red baseline reproduces the failure — hermes: no dependency environment is committed …; run `hermes pm repair`
OK  second ensure is a no-op
RESULT all checks passed
```

Третья строка — та самая гонка: у свежего дерева (`a8f061eb4f577f65`) состояния нет, через 3 мс
после вызова моста оно есть. Предпоследняя строка — доказательство, что проба не «зелёная по
построению»: дерево-близнец без моста падает ровно с той строкой из отчёта.

## 5. Что осталось и чего эта поставка НЕ доказывает

* **Рестарт шлюза — шаг оркестратора.** Из карточки шлюз не перезапускался; работающий диспетчер
  исполняет старый код, поэтому живой спавн карточки через `--workspace worktree` на этом коде не
  наблюдался. Проба проверяет ровно тот путь кода, который диспетчер вызовет после рестарта.
* **`manual_review`: метрика «0 прогонов с `no dependency environment` за 24 ч после внедрения».**
  Это наблюдение вперёд, агенту недоступное; проверяется по `task_runs` после планового рестарта.
* Таймер-провижининг оставлен как страховка (карточка прямо это требует) — дефектом не считается.
* Windows-ветка моста (`target_is_directory`, права) не проверялась: прогон только на Linux.

## 6. Откат

Изменения аддитивны и локализованы:

```bash
cd /home/hermes/.hermes/hermes-agent/.worktrees/t_44bd1c6a
git revert <commit>          # или: git checkout b8c2c25eaf35 -- hermes_cli/kanban_db_dispatch.py
rm hermes_cli/kanban_db_worker_env.py hermes_cli/kanban_db_capacity.py \
   tests/hermes_cli/test_kanban_worker_env_provisioning.py
```

Мост, уже поставленные ссылки `installs/<key>` не ломает: они идемпотентны и остаются валидными.
