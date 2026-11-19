"""Memory is keyed by SUBJECT, not switched off (ACCESS-MODEL D13).

Memory enters the system prompt during agent init — before any tool call — so no permission
check can gate it (D2). The archive's ``memory-per-user.patch`` lesson is adopted with one
correction: the subject is ``HERMES_SESSION_ACTOR_ID`` ("who asked this turn"), never
``HERMES_SESSION_USER_ID`` ("the session ROUTE", deliberately empty in a shared chat and cleared
when a turn is merged from two senders).

What these tests pin, each with a real artifact rather than an assertion about intent:

* two subjects write to two directories and neither store can read the other's note;
* the SYSTEM PROMPT built for A does not contain B's text (built from the real store, not
  reasoned about);
* a raw ``threading.Thread`` started from a bound session (the background self-improvement
  review: no ContextVar inheritance) writes into ``u/_nouser``, never the shared base and never
  a real subject's bucket;
* a process that never bound a session (CLI owner, cron, single-user) keeps the legacy
  ``memories/`` directory — verified in a SUBPROCESS, because "no session ever bound" is a
  process-level fact that an in-process pytest run cannot honestly reproduce;
* a merged two-sender turn (actor cleared) has no subject;
* a hostile subject string cannot escape the bucket.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools import memory_tool
from tools.memory_tool import MemoryStore, get_memory_dir, memory_subject

A = "tg:alice"
B = "tg:bob"


@pytest.fixture(autouse=True)
def _acl_on(monkeypatch):
    """Per-subject memory is gated on ``acl.enabled``; this module tests it in the ON state.

    ``TestBackwardCompatibility`` overrides the patch to exercise the OFF state.
    """
    from tools import memory_tool

    monkeypatch.setattr(memory_tool, "_acl_enabled", lambda: True)


@pytest.fixture()
def session(monkeypatch):
    """Bind the gateway session vars of a real messaging turn, with ``acl.enabled=true``.

    Per-subject memory is part of the access model and turns on with the same switch
    (``acl.enabled``); with it off the directory is the legacy shared base by design.
    """
    from gateway import session_context
    from tools import memory_tool

    monkeypatch.setattr(session_context, "_session_context_engaged", False, raising=False)
    monkeypatch.setattr(memory_tool, "_acl_enabled", lambda: True)

    def bind(actor: str = "", user_id: str = "", platform: str = "telegram", chat_type: str = "dm"):
        session_context.set_session_vars(
            platform=platform, source="telegram", chat_id="42", chat_type=chat_type,
            user_id=user_id or actor, actor_id=actor, session_key="k", session_id="s1",
        )

    yield bind
    monkeypatch.setattr(session_context, "_session_context_engaged", False, raising=False)


def _store() -> MemoryStore:
    """A store the way agent init builds it: resolved per call, loaded from disk."""
    store = MemoryStore(memory_char_limit=2200, user_char_limit=1375, memory_enabled=True)
    store.load_from_disk()
    return store


class TestSubjectDirectories:
    def test_two_subjects_write_to_separate_buckets(self, session, tmp_path, monkeypatch):
        session(A)
        assert get_memory_dir() == memory_tool.get_hermes_home() / "memories" / "u" / "tg_alice"
        alice = _store()
        assert alice.add("memory", "A-note: Alice runs the telescope pipeline")["success"]

        session(B)
        bob = _store()
        assert bob.add("memory", "B-note: Bob owns the telemetry archive")["success"]

        alice_dir = memory_tool.get_hermes_home() / "memories" / "u" / "tg_alice"
        bob_dir = memory_tool.get_hermes_home() / "memories" / "u" / "tg_bob"
        assert (alice_dir / "MEMORY.md").exists() and (bob_dir / "MEMORY.md").exists()
        assert "A-note" in (alice_dir / "MEMORY.md").read_text(encoding="utf-8")
        assert "B-note" not in (alice_dir / "MEMORY.md").read_text(encoding="utf-8")
        assert "B-note" in (bob_dir / "MEMORY.md").read_text(encoding="utf-8")
        assert "A-note" not in (bob_dir / "MEMORY.md").read_text(encoding="utf-8")

    def test_a_store_cannot_read_b_entries(self, session):
        session(A)
        assert _store().add("memory", "A-note: only Alice")["success"]
        session(B)
        assert _store().add("memory", "B-note: only Bob")["success"]

        session(A)
        entries = _store().memory_entries
        assert any("A-note" in e for e in entries)
        assert not any("B-note" in e for e in entries)

    def test_route_user_id_is_never_the_subject(self, session):
        """A shared chat: USER_ID carries the ROUTE, ACTOR carries the person — key on the actor."""
        session(actor=A, user_id="route-777", chat_type="group")
        assert memory_subject()[0] == A
        assert get_memory_dir().name == "tg_alice"


class TestSystemPromptIsolation:
    """The claim under test is about the PROMPT, so the prompt is what gets built."""

    def _agent(self, **overrides):
        base = dict(
            load_soul_identity=False, skip_context_files=False, valid_tool_names=[],
            _task_completion_guidance=False, _tool_use_enforcement=False, _environment_probe=False,
            _kanban_worker_guidance="", _memory_store=None, _memory_manager=None,
            model="", provider="", platform="cli", pass_session_id=False, session_id="",
            enabled_toolsets=None, disabled_toolsets=None, memory_agent_context=None,
            _emit_status=lambda *_a, **_k: None,
        )
        base.update(overrides)
        return SimpleNamespace(**base)

    def _prompt_for(self, subject: str, session) -> str:
        """Build the prompt the way agent init does: per-subject store, then the real builder.

        ``agent_init._init_memory`` constructs ``MemoryStore(...)`` for the resolved dir and calls
        ``load_from_disk()``; ``system_prompt`` then renders ``format_for_system_prompt``. This
        test performs exactly those two steps (the init module cannot be imported under the
        test-suite's home-I/O guard in a git worktree — a pre-existing environment limit, not a
        property of this change), so what is asserted is the PROMPT BYTES, not an intent.
        """
        from agent.system_prompt import build_system_prompt

        session(subject)
        agent = self._agent(_memory_store=_store(), _memory_enabled=True, _user_profile_enabled=True)
        assert agent._memory_store.memory_entries is not None
        return build_system_prompt(agent)

    def test_prompt_of_a_does_not_contain_b_note(self, session):
        session(A)
        assert _store().add("memory", "A-note: Alice calibration offset is 3.2 deg")["success"]
        session(B)
        assert _store().add("memory", "B-note: Bob keeps the raw telemetry in cold storage")["success"]

        a_prompt = self._prompt_for(A, session)
        b_prompt = self._prompt_for(B, session)
        assert "A-note" in a_prompt, "grounding: A's own note must reach A's prompt"
        assert "B-note" not in a_prompt, "B's memory leaked into A's system prompt"
        assert "B-note" in b_prompt
        assert "A-note" not in b_prompt

    def test_prompt_uses_the_subject_profile_file_holder(self, session):
        """USER.md follows the same bucket as MEMORY.md (one directory per subject)."""
        session(A)
        store = _store()
        assert store.add("user", "A-profile: prefers metric units")["success"]
        assert store.add("memory", "A-env: bench host is t480s")["success"]
        base = memory_tool.get_hermes_home() / "memories" / "u" / "tg_alice"
        assert (base / "USER.md").exists() and (base / "MEMORY.md").exists()
        assert not (memory_tool.get_hermes_home() / "memories" / "USER.md").exists()


class TestBackgroundThreadWithoutContext:
    """The self-improvement review is a bare ``threading.Thread``: no ContextVars arrive."""

    def test_background_thread_lands_in_nouser(self, session, monkeypatch):
        from gateway import session_context

        session(A)
        monkeypatch.setattr(session_context, "_session_context_engaged", True, raising=False)
        seen: dict[str, object] = {}

        def work():
            seen["subject"] = memory_subject()
            seen["dir"] = get_memory_dir()
            store = _store()
            store.add("memory", "review-note: written by the review fork")
            seen["entries"] = list(store.memory_entries)

        t = threading.Thread(target=work, name="bg-review", daemon=True)
        t.start()
        t.join(10)
        assert not t.is_alive()

        assert seen["subject"][0] == "", "a bare thread must not inherit the turn's subject"
        bucket = Path(str(seen["dir"]))
        assert bucket.name == memory_tool.NOUSER_BUCKET
        assert bucket != memory_tool.get_hermes_home() / "memories", "never the shared base"
        assert "tg_alice" not in str(bucket), "never a real subject's bucket"

    def test_nouser_is_not_readable_as_a_subjects_memory(self, session, monkeypatch):
        """A quarantined write must not surface in any real subject's store."""
        from gateway import session_context

        session(A)
        monkeypatch.setattr(session_context, "_session_context_engaged", True, raising=False)
        threading.Thread(
            target=lambda: _store().add("memory", "review-note: quarantined"),
            daemon=True).start()
        # wait for the file, then read as A
        import time
        bucket = memory_tool.get_hermes_home() / "memories" / "u" / memory_tool.NOUSER_BUCKET / "MEMORY.md"
        for _ in range(100):
            if bucket.exists():
                break
            time.sleep(0.02)
        assert bucket.exists()
        session(A)
        assert not any("quarantined" in e for e in _store().memory_entries)


class TestUnresolvableContextFailsClosed:
    def test_merged_turn_has_no_subject(self, session):
        """``absorb_turn_input`` clears the actor: a two-sender turn owns nothing."""
        session(actor="")
        subject, quarantine = memory_subject()
        assert subject == ""
        assert quarantine is True
        assert get_memory_dir().name == memory_tool.NOUSER_BUCKET

    def test_broken_session_module_quarantines(self, monkeypatch):
        """A resolver that cannot answer must quarantine, never fall back to the shared base."""
        import builtins

        real_import = builtins.__import__

        def fake_import(name, *a, **kw):
            if name == "gateway.session_context":
                raise RuntimeError("session module unavailable")
            return real_import(name, *a, **kw)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        subject, quarantine = memory_subject()
        assert (subject, quarantine) == ("", True)
        assert get_memory_dir().name == memory_tool.NOUSER_BUCKET

    @pytest.mark.parametrize("hostile", ["..", "../..", ".", "", "   ", "/etc", "a/../../b", "." * 8])
    def test_hostile_subject_cannot_escape_the_bucket(self, monkeypatch, hostile):
        from gateway import session_context

        monkeypatch.setattr(session_context, "_session_context_engaged", True, raising=False)
        monkeypatch.setattr(memory_tool, "memory_subject", lambda: (hostile, True))
        bucket = get_memory_dir()
        base = memory_tool.get_hermes_home() / "memories"
        assert base in bucket.parents, f"{bucket} escaped {base}"
        assert bucket.parent.name == "u"

    def test_sanitized_subject_stays_inside(self, monkeypatch):
        from gateway import session_context

        monkeypatch.setattr(session_context, "_session_context_engaged", True, raising=False)
        monkeypatch.setattr(memory_tool, "memory_subject", lambda: ("auth:v1:[p,i,u]", True))
        assert get_memory_dir() == memory_tool.get_hermes_home() / "memories" / "u" / "auth_v1__p_i_u_"


class TestLegacyCompatibility:
    """``acl.enabled=false`` (the default) must keep the pre-change behaviour exactly."""

    _CODE = (
        "import sys; sys.path.insert(0, %r)\n"
        "from tools.memory_tool import get_memory_dir, memory_subject\n"
        "print(memory_subject())\n"
        "print(get_memory_dir())\n"
    )

    def _fresh(self, tmp_path, *, acl: bool):
        home = tmp_path / "home"
        home.mkdir(parents=True, exist_ok=True)
        if acl:
            (home / "config.yaml").write_text("acl:\n  enabled: true\n", encoding="utf-8")
        env = {**os.environ, "HERMES_HOME": str(home), "HOME": str(tmp_path / "user")}
        for var in ("HERMES_SESSION_ACTOR_ID", "HERMES_SESSION_USER_ID", "HERMES_PLATFORM"):
            env.pop(var, None)
        code = self._CODE % str(Path(__file__).resolve().parents[2])
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=180, check=False)
        assert r.returncode == 0, r.stderr
        subject_line, dir_line = r.stdout.strip().splitlines()[:2]
        return home, subject_line, dir_line

    def test_default_config_keeps_the_shared_base_dir(self, tmp_path):
        """A fresh interpreter with the default config: the pre-change directory, unchanged."""
        home, subject_line, dir_line = self._fresh(tmp_path, acl=False)
        assert subject_line == "('', False)"
        assert dir_line == str(home / "memories"), dir_line

    def test_acl_on_without_any_bound_session_also_keeps_the_base_dir(self, tmp_path):
        """CLI owner path: the switch on, but this process never bound a session (cron/CLI/TUI)."""
        home, _, dir_line = self._fresh(tmp_path, acl=True)
        assert dir_line == str(home / "memories"), dir_line

    def _write_note(self, tmp_path, acl: bool):
        home = tmp_path / "home"
        home.mkdir(parents=True, exist_ok=True)
        if acl:
            (home / "config.yaml").write_text("acl:\n  enabled: true\n", encoding="utf-8")
        env = {**os.environ, "HERMES_HOME": str(home), "HOME": str(tmp_path / "user")}
        for var in ("HERMES_SESSION_ACTOR_ID", "HERMES_SESSION_USER_ID", "HERMES_PLATFORM"):
            env.pop(var, None)
        code = (
            f"import sys; sys.path.insert(0, {str(Path(__file__).resolve().parents[2])!r})\n"
            "from tools.memory_tool import MemoryStore\n"
            "s = MemoryStore(memory_char_limit=500, user_char_limit=300, memory_enabled=True)\n"
            "s.load_from_disk(); print(s.add('memory', 'cli-note: legacy path'))\n"
        )
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=180, check=False)
        assert r.returncode == 0, r.stderr
        return home

    @pytest.mark.parametrize("acl", [False, True])
    def test_cli_writes_land_in_the_base_dir_and_no_bucket_is_created(self, tmp_path, acl):
        home = self._write_note(tmp_path, acl)
        assert (home / "memories" / "MEMORY.md").read_text(encoding="utf-8").count("cli-note") == 1
        assert not (home / "memories" / "u").exists(), "a sessionless CLI must not create buckets"

    def test_acl_off_keeps_a_messaging_turn_on_the_shared_base(self, tmp_path):
        """The A/B that matters for criterion 7: SAME bound turn, switch off -> one shared dir.

        Also flipped ON in the same shape, so the two runs differ in exactly one thing.
        """
        code = (
            f"import sys; sys.path.insert(0, {str(Path(__file__).resolve().parents[2])!r})\n"
            "from gateway.session_context import set_session_vars\n"
            "set_session_vars(platform='telegram', source='telegram', chat_id='42', chat_type='dm',\n"
            "                 user_id='tg:alice', actor_id='tg:alice', session_id='s1')\n"
            "from tools.memory_tool import get_memory_dir, memory_subject\n"
            "print(memory_subject())\n"
            "print(get_memory_dir())\n"
        )

        def run(acl: bool):
            home = tmp_path / ("home-on" if acl else "home-off")
            home.mkdir(parents=True)
            if acl:
                (home / "config.yaml").write_text("acl:\n  enabled: true\n", encoding="utf-8")
            env = {**os.environ, "HERMES_HOME": str(home), "HOME": str(tmp_path / "user")}
            r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                               env=env, timeout=180, check=False)
            assert r.returncode == 0, r.stderr
            subject_line, dir_line = r.stdout.strip().splitlines()[:2]
            return home, subject_line, dir_line

        off_home, off_subject, off_dir = run(acl=False)
        on_home, on_subject, on_dir = run(acl=True)
        assert off_subject == on_subject == "('tg:alice', True)"
        assert off_dir == str(off_home / "memories"), off_dir
        assert on_dir == str(on_home / "memories" / "u" / "tg_alice"), on_dir


class TestMaintenanceCoversEveryBucket:
    def test_memory_dirs_all_lists_base_and_subjects(self, session):
        session(A)
        assert _store().add("memory", "A-note")["success"]
        session(B)
        assert _store().add("memory", "B-note")["success"]
        dirs = memory_tool.memory_dirs_all()
        names = {d.name for d in dirs}
        assert names == {"memories", "tg_alice", "tg_bob"}
