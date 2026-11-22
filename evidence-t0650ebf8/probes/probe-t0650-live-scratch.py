"""Живой прогон мягкого бюджета на учебной карточке в scratch (t_0650ebf8).

Настоящий AIAgent, настоящее окружение диспетчера (полный конверт
HERMES_KANBAN_*), настоящий scratch-каталог без git. Порог 108/180
(agent.budget_warning_ratio: 0.6), прогон НЕ останавливается.
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import tarfile
import tempfile
import time

HOME = pathlib.Path(tempfile.mkdtemp(prefix="probe-t0650-"))
SCRATCH = HOME / "workspace"          # рабочее дерево карточки (scratch)
SCRATCH.mkdir()
KHOME = HOME / "kanban-home"
KHOME.mkdir()

os.environ["HERMES_HOME"] = str(HOME)
os.environ["HERMES_KANBAN_HOME"] = str(KHOME)
os.environ["HERMES_KANBAN_DB"] = str(KHOME / "kanban.db")
os.environ["HERMES_KANBAN_TASK"] = "t_probe_scratch"
os.environ["HERMES_KANBAN_RUN_ID"] = "run-probe-0650"
os.environ["HERMES_KANBAN_WORKSPACE"] = str(SCRATCH)
os.environ["HERMES_PROFILE"] = "03-software-engineer"
os.environ["HERMES_SESSION_SOURCE"] = "kanban"
# В этом процессе стоит маркер делегированного ребёнка (наследие шлюза); у настоящего
# воркера его нет, а без снятия ветка kanban в напоминании не срабатывает.
os.environ.pop("HERMES_DELEGATED_CHILD_CONTEXT", None)

(HOME / "config.yaml").write_text("agent:\n  budget_warning_ratio: 0.6\n", encoding="utf-8")

# Работа «исполнителя» в scratch: PROGRESS.md и содержательный артефакт.
(SCRATCH / "PROGRESS.md").write_text(
    "# PROGRESS — учебная карточка\n\n- [x] собрать отчёт\n- [ ] прогнать проверку\n",
    encoding="utf-8",
)
report = "# Отчёт\n\n" + "строка проверенного результата\n" * 2000
(SCRATCH / "report.md").write_text(report, encoding="utf-8")
(SCRATCH / "nested" ).mkdir()
(SCRATCH / "nested" / "notes.txt").write_text("заметки\n" * 20, encoding="utf-8")

from model_tools import _clear_tool_defs_cache
from tools.registry import invalidate_check_fn_cache

invalidate_check_fn_cache()
_clear_tool_defs_cache()

from hermes_state import SessionDB
from run_agent import AIAgent

agent = AIAgent(
    session_db=SessionDB(db_path=HOME / "proof.db"),
    model="test-model", provider="openai-compat", api_key="test",
    base_url="http://127.0.0.1:1/v1", max_iterations=180,
    quiet_mode=True, skip_context_files=True, skip_memory=True,
)
print(f"max_iterations={agent.iteration_budget.max_total} "
      f"ratio={agent.budget_warning_ratio}")

from agent.kanban_budget_checkpoint import _repo_root
print(f"git в рабочем каталоге: {_repo_root(SCRATCH)}")

from hermes_cli.kanban_db import task_attachments_dir
att_dir = task_attachments_dir("t_probe_scratch")


def tree_files(root: pathlib.Path) -> list[str]:
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())


def archives() -> list[pathlib.Path]:
    return sorted(att_dir.glob("wip-*.tar.gz"))


def reminder(used: int) -> bool:
    from agent.turn_iteration_prep import _maybe_inject_iteration_budget_warning

    messages = [
        {"role": "user", "content": "работай"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "t1"}]},
        {"role": "tool", "tool_call_id": "t1", "content": "записал report.md"},
    ]
    injected = _maybe_inject_iteration_budget_warning(agent, messages)
    notice = messages[-1]["content"]
    return injected, notice


# ── итерации 1..107: порог не пройден ──────────────────────────────────────
for _ in range(107):
    agent.iteration_budget.consume()
print(f"итераций израсходовано: {agent.iteration_budget.used}, "
      f"осталось: {agent.iteration_budget.remaining}, архивов: {len(archives())}")

# ── 108-я итерация: мягкий порог ───────────────────────────────────────────
agent.iteration_budget.consume()
t0 = time.perf_counter()
injected, notice = reminder(agent.iteration_budget.used)
elapsed = time.perf_counter() - t0
print(f"\n[108-я итерация] порог сработал: {injected}; "
      f"время сохранения: {elapsed * 1000:.1f} мс")

made = archives()
print(f"архивов в каталоге: {len(made)}")
for path in made:
    print(f"  {path}  размер={path.stat().st_size} байт")
with tarfile.open(made[0]) as tar:
    names = tar.getnames()
print(f"верхние записи архива ({len(names)}): {names[:10]}")
src = {f: (SCRATCH / f).stat().st_size for f in tree_files(SCRATCH)}
print(f"файлов в рабочем каталоге: {len(src)} суммарно {sum(src.values())} байт")
print("совпадение набора записей с рабочим каталогом:",
      sorted(names) == sorted(src))

# ── повтор на неизменённом каталоге ────────────────────────────────────────
before = len(archives())
agent._iteration_budget_warning_injected = False       # латч сбрасывается на новом ходу
injected2, notice2 = reminder(agent.iteration_budget.used + 5)
after = len(archives())
print(f"\n[повтор, дерево не менялось] порог: {injected2}; "
      f"архивов до={before} после={after}")
print("текст напоминания называет существующий архив:",
      str(made[0]) in notice2)

# ── правка дерева: новый архив ─────────────────────────────────────────────
(SCRATCH / "report.md").write_text(report + "дописано\n", encoding="utf-8")
agent._iteration_budget_warning_injected = False
reminder(agent.iteration_budget.used + 10)
print(f"[после правки дерева] архивов: {len(archives())}")

print("\n--- фрагмент напоминания в последнем результате инструмента ---")
print(notice[notice.index("[SYSTEM NOTICE"):][:1200])

agent._session_db.close()
print(f"\nPROBE_HOME={HOME}")
