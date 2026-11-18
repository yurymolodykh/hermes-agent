"""Independent verification of memory-by-subject (task t_296c8418, criteria 1/2/7).

Deliberately does NOT import the project's test module: this is a fresh demonstration
against the shipped code, so a test that "passes for the wrong reason" cannot carry it.

Run:  PYTHONPATH=<repo> python3 verify_memory_subject.py
Exit code 0 = every check printed PASS.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

REPO = Path(os.environ.get("ACL_REPO", Path(__file__).resolve().parents[1])).resolve()
sys.path.insert(0, str(REPO))

FAILURES: list[str] = []
EVIDENCE: dict[str, object] = {}


def check(name: str, ok: bool, detail: object = "") -> None:
    print(("PASS " if ok else "FAIL ") + name + (f" {detail}" if detail != "" else ""))
    if not ok:
        FAILURES.append(name)


def fresh_home() -> Path:
    home = Path(tempfile.mkdtemp(prefix="mem-verify-"))
    (home / "memories").mkdir(parents=True, exist_ok=True)
    return home


def main() -> int:
    from gateway import session_context
    from tools import memory_tool

    home = fresh_home()
    os.environ["HERMES_HOME"] = str(home)
    memory_tool._acl_enabled = lambda: True  # the switch that gates the whole model

    def bind(actor: str, chat_type: str = "dm", user_id: str | None = None) -> None:
        session_context.set_session_vars(
            platform="telegram", source="telegram", chat_id="42", chat_type=chat_type,
            user_id=actor if user_id is None else user_id, actor_id=actor,
            session_key="k", session_id="s1",
        )

    def store():
        s = memory_tool.MemoryStore(memory_char_limit=2200, user_char_limit=1375,
                                    memory_enabled=True)
        s.load_from_disk()
        return s

    A, B = "tg:alice", "tg:bob"

    # ---- criterion 1a: two subjects, two directories -------------------------------
    bind(A)
    dir_a = Path(memory_tool.get_memory_dir())
    ok_a = store().add("memory", "A-note: alice keeps calibration at 3.2 deg")
    check("A's note stored", bool(ok_a.get("success")), ok_a)

    bind(B)
    dir_b = Path(memory_tool.get_memory_dir())
    ok_b = store().add("memory", "B-note: bob keeps telemetry in cold storage")
    check("B's note stored", bool(ok_b.get("success")), ok_b)

    check("two subjects resolved to different directories", dir_a != dir_b,
          {"A": str(dir_a), "B": str(dir_b)})
    check("A's directory is under memories/u/", dir_a.parent.name == "u", str(dir_a))
    check("B's directory is under memories/u/", dir_b.parent.name == "u", str(dir_b))

    text_a = (dir_a / "MEMORY.md").read_text(encoding="utf-8")
    text_b = (dir_b / "MEMORY.md").read_text(encoding="utf-8")
    check("A's file contains A's note", "A-note" in text_a)
    check("B's file does NOT contain A's note", "A-note" not in text_b)
    check("B's file contains B's note", "B-note" in text_b)
    check("A's file does NOT contain B's note", "B-note" not in text_a)

    # ---- criterion 1b: the SYSTEM PROMPT, built by the REAL builder -----------------
    from agent.system_prompt import build_system_prompt
    from types import SimpleNamespace

    def prompt_for(actor: str) -> str:
        """Same two steps agent init performs: per-subject store, then the real builder."""
        bind(actor)
        agent = SimpleNamespace(
            load_soul_identity=False, skip_context_files=False, valid_tool_names=[],
            _task_completion_guidance=False, _tool_use_enforcement=False,
            _environment_probe=False, _kanban_worker_guidance="",
            _memory_store=store(), _memory_manager=None, _memory_enabled=True,
            _user_profile_enabled=True, model="", provider="", platform="cli",
            pass_session_id=False, session_id="", enabled_toolsets=None,
            disabled_toolsets=None, memory_agent_context=None,
            _emit_status=lambda *_a, **_k: None,
        )
        return build_system_prompt(agent)

    prompt_a = prompt_for(A)
    prompt_b = prompt_for(B)
    check("prompt builder produced a real prompt for A", len(prompt_a) > 1000, len(prompt_a))

    check("A's prompt contains A's note (grounding)", "A-note" in prompt_a)
    check("A's prompt does NOT contain B's note", "B-note" not in prompt_a)
    check("B's prompt contains B's note (grounding)", "B-note" in prompt_b)
    check("B's prompt does NOT contain A's note", "A-note" not in prompt_b)
    EVIDENCE["prompt_a_len"] = len(prompt_a)
    EVIDENCE["prompt_b_len"] = len(prompt_b)
    # Keep the raw prompts as the artifact the criterion asks for.
    (REPO / "probe" / "prompt-A.txt").write_text(prompt_a, encoding="utf-8")
    (REPO / "probe" / "prompt-B.txt").write_text(prompt_b, encoding="utf-8")

    # ---- criterion 2: bare thread, no ContextVar inheritance ----------------------
    bind(A)
    session_context._session_context_engaged = True
    seen: dict[str, object] = {}

    def bg() -> None:
        seen["subject"] = memory_tool.memory_subject()
        seen["dir"] = str(memory_tool.get_memory_dir())
        st = store()
        st.add("memory", "review-note: written by the background review fork")
        seen["entries"] = list(getattr(st, "memory_entries", []) or [])

    t = threading.Thread(target=bg, name="bg-review", daemon=True)
    t.start()
    t.join(15)
    bucket = Path(str(seen["dir"]))
    check("background thread is not alive", not t.is_alive())
    subj = seen["subject"]
    check("background thread has NO subject", subj[0] == "", subj)
    check("background thread wrote into _nouser", bucket.name == "_nouser", str(bucket))
    check("background thread did NOT write into A's bucket", bucket != dir_a, str(bucket))
    check("background thread did NOT write into the shared base",
          bucket != home / "memories", str(bucket))
    check("A's bucket unchanged by the background write",
          "review-note" not in (dir_a / "MEMORY.md").read_text(encoding="utf-8"))
    EVIDENCE["nouser_dir"] = str(bucket)
    EVIDENCE["nouser_entries"] = seen["entries"]

    # ---- criterion 7: acl.enabled=false is byte-identical legacy behaviour --------
    # Process-level fact ("no session ever bound"), so it is proven in a SUBPROCESS.
    script = (
        "import os, sys\n"
        "sys.path.insert(0, os.environ['ACL_REPO'])\n"
        "from tools import memory_tool\n"
        "memory_tool._acl_enabled = lambda: False\n"
        "print(memory_tool.get_memory_dir())\n"
    )
    for label, flag in (("acl OFF", False),):
        env = dict(os.environ, ACL_REPO=str(REPO), HERMES_HOME=str(home))
        out = subprocess.run([sys.executable, "-c", script], capture_output=True,
                             text=True, env=env, check=False)
        resolved = (out.stdout or "").strip()
        check(f"{label}: get_memory_dir() is the legacy shared base",
              resolved == str(home / "memories"), {"resolved": resolved,
                                                   "stderr": (out.stderr or "")[-300:]})
        check(f"{label}: no subject bucket is consulted",
              "/u/" not in resolved, resolved)

    # ---- summary ------------------------------------------------------------------
    (REPO / "probe" / "verify_memory_subject.result.json").write_text(
        json.dumps({"evidence": EVIDENCE, "failures": FAILURES}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    print("\nRESULT:", "ALL PASS" if not FAILURES else f"{len(FAILURES)} FAILED: {FAILURES}")
    shutil.rmtree(home, ignore_errors=True)
    return 0 if not FAILURES else 1


if __name__ == "__main__":
    raise SystemExit(main())
