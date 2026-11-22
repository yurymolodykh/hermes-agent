"""Живой прогон: (1) git-worktree даёт ref и НЕ даёт tar; (2) цена tar на большом дереве.

Часть 1 — контракт «для git-карточки поведение не меняется».
Часть 2 — замер времени: мягкий порог срабатывает на 108-й итерации, и если tar
дорогой, это надо назвать числом, а не отключать молча.
"""
from __future__ import annotations

import os
import pathlib
import subprocess
import tempfile
import time

HOME = pathlib.Path(tempfile.mkdtemp(prefix="probe-t0650-git-"))
KHOME = HOME / "kanban-home"
KHOME.mkdir()
os.environ.update({
    "HERMES_HOME": str(HOME),
    "HERMES_KANBAN_HOME": str(KHOME),
    "HERMES_KANBAN_DB": str(KHOME / "kanban.db"),
    "HERMES_KANBAN_TASK": "t_probe_git",
    "HERMES_KANBAN_RUN_ID": "run-probe-git",
    "HERMES_KANBAN_WORKSPACE": str(HOME / "repo"),
    "HERMES_PROFILE": "03-software-engineer",
    "HERMES_SESSION_SOURCE": "kanban",
})
os.environ.pop("HERMES_DELEGATED_CHILD_CONTEXT", None)
(HOME / "config.yaml").write_text("agent:\n  budget_warning_ratio: 0.6\n", encoding="utf-8")

ws = HOME / "repo"
ws.mkdir()


def git(*args):
    return subprocess.run(["git", "-C", str(ws), *args], capture_output=True, text=True,
                          timeout=120)


git("init", "-q", "-b", "wt/t_probe_git")
git("config", "user.email", "probe@example.invalid")
git("config", "user.name", "probe")
(ws / "base.py").write_text("x = 1\n", encoding="utf-8")
git("add", "base.py")
git("commit", "-q", "-m", "base")
(ws / "mid.py").write_text("partial = True\n", encoding="utf-8")

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
for _ in range(108):
    agent.iteration_budget.consume()

from agent.turn_iteration_prep import _maybe_inject_iteration_budget_warning
from hermes_cli.kanban_db import task_attachments_dir

messages = [
    {"role": "user", "content": "работай"},
    {"role": "assistant", "content": "", "tool_calls": [{"id": "t1"}]},
    {"role": "tool", "tool_call_id": "t1", "content": "правлю mid.py"},
]
t0 = time.perf_counter()
injected = _maybe_inject_iteration_budget_warning(agent, messages)
elapsed = time.perf_counter() - t0

print("=== часть 1: git-worktree ===")
print(f"порог сработал: {injected}, время: {elapsed * 1000:.1f} мс")
print("refs/hermes-wip/:",
      git("for-each-ref", "refs/hermes-wip/", "--format=%(refname)").stdout.split())
print("содержимое mid.py в ref:",
      repr(git("show", "refs/hermes-wip/t_probe_git:mid.py").stdout))
print("HEAD не сдвинут:", git("rev-parse", "HEAD").stdout.strip()[:12])
print("mid.py остался в рабочем дереве:", (ws / "mid.py").exists())
att = task_attachments_dir("t_probe_git")
print("каталог архивов:", att, "существует:", att.exists())
print("tar-архивов:", sorted(att.glob('wip-*.tar.gz')) if att.exists() else [])
notice = messages[-1]["content"]
print("напоминание называет ref:", "refs/hermes-wip/t_probe_git" in notice)
print("напоминание НЕ упоминает архив:", "survives the budget stop" not in notice)

# ── часть 2: цена tar на большом scratch-дереве ────────────────────────────
print("\n=== часть 2: стоимость архива на большом дереве ===")
from agent.kanban_budget_checkpoint import _archive_tree, _changed_paths

for n_files, size in ((200, 4_000), (2000, 2_000), (5000, 1_000)):
    big_home = pathlib.Path(tempfile.mkdtemp(prefix="probe-t0650-big-"))
    os.environ["HERMES_KANBAN_HOME"] = str(big_home)
    os.environ["HERMES_KANBAN_TASK"] = f"t_big_{n_files}"
    tree = big_home / "tree"
    tree.mkdir()
    payload = ("x" * (size - 1) + "\n")
    for i in range(n_files):
        sub = tree / f"d{i % 20}"
        sub.mkdir(exist_ok=True)
        (sub / f"f{i}.txt").write_text(payload, encoding="utf-8")
    files = _changed_paths(tree)
    total = sum((tree / f).stat().st_size for f in files)
    t0 = time.perf_counter()
    archive = _archive_tree(tree, files, f"t_big_{n_files}", "probe")
    dt = time.perf_counter() - t0
    size_mb = pathlib.Path(archive).stat().st_size / 1e6 if archive else 0
    print(f"файлов={len(files)} исходно={total / 1e6:.1f} МБ "
          f"-> архив={size_mb:.2f} МБ за {dt * 1000:.0f} мс")

agent._session_db.close()
print(f"\nPROBE_HOME={HOME}")
