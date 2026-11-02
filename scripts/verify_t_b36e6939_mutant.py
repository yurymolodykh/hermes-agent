"""Mutation check for t_b36e6939: the first-turn latch depends on the ensure path.

Card criterion 4 ("Мутант: убрать ensure → тест 1 обязан покраснеть"). A
regression test that cannot fail is a tautology; this script proves the new
behaviour is produced by the ``ensure_session`` step specifically.

It runs the exact card scenario against a REAL ``SessionDB`` three times:

  A. production code                      -> latch persists          (must PASS)
  B. ``_ensure_marker_row`` mutated to a  -> latch LOST, defect shape (must REPRODUCE)
     no-op (the "remove ensure" mutant)
  C. a handle without ``ensure_session``  -> latch LOST, no crash    (must DEGRADE)

Exit 0 only when all three arms behave as stated.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

REPO = Path(r"C:\Users\hermes\AppData\Local\hermes\hermes-agent")
sys.path.insert(0, str(REPO))

import agent.fast_lane as fl  # noqa: E402
from hermes_state import SessionDB  # noqa: E402


def _decision(text: str, session_slow=None):
    return fl.resolve_fast_path(
        fl.decide_slow_mode(text, session_slow=session_slow), text
    )


def _two_turns(db) -> dict:
    """T1 «подумай» on a brand-new session, then T2 with no marker."""
    t1 = _decision("подумай: разбери архитектуру", session_slow=fl.session_slow_mode(db, "s1"))
    fl.patch_session_fast_lane_marker(
        db, "s1", t1, preserve_sticky=True, source="api_server"
    )
    sticky_t2 = fl.session_slow_mode(db, "s1")
    t2 = _decision("а теперь покороче?", session_slow=sticky_t2)
    return {
        "t1_slow": t1.slow_mode,
        "t1_source": t1.source,
        "t2_sticky": sticky_t2,
        "t2_slow": t2.slow_mode,
        "t2_source": t2.source,
        "t2_route": fl.route_profile(t2),
    }


class _NoEnsureDB:
    """Real SessionDB minus the row-creation primitive (third-party shim shape)."""

    def __init__(self, inner):
        self._inner = inner

    def patch_session_model_config(self, sid, patch):
        return self._inner.patch_session_model_config(sid, patch)

    def get_session_model_config_value(self, sid, key, default=None):
        return self._inner.get_session_model_config_value(sid, key, default)

    def get_session(self, sid):
        return self._inner.get_session(sid)


def main() -> int:
    out = {}
    ok = True

    with tempfile.TemporaryDirectory() as tmp:
        db = SessionDB(Path(tmp) / "a.db")
        out["A_production"] = _two_turns(db)
        db.close()

    with tempfile.TemporaryDirectory() as tmp:
        db = SessionDB(Path(tmp) / "b.db")
        original = fl._ensure_marker_row
        fl._ensure_marker_row = lambda target, sid, source: False  # MUTANT
        try:
            out["B_mutant_no_ensure"] = _two_turns(db)
        finally:
            fl._ensure_marker_row = original
        db.close()

    with tempfile.TemporaryDirectory() as tmp:
        db = SessionDB(Path(tmp) / "c.db")
        out["C_shim_without_ensure"] = _two_turns(_NoEnsureDB(db))
        db.close()

    print(json.dumps(out, ensure_ascii=False, indent=2))

    a = out["A_production"]
    ok &= a["t1_source"] == "tg-prefix" and a["t1_slow"] is True
    ok &= a["t2_sticky"] is True
    ok &= a["t2_source"] == "session-sticky"
    ok &= a["t2_route"] is None

    b = out["B_mutant_no_ensure"]
    ok &= b["t1_slow"] is True and b["t1_source"] == "tg-prefix"
    # The mutant still latched => the new test would be a tautology.
    ok &= b["t2_sticky"] is None
    ok &= b["t2_source"] == "no-marker"
    ok &= b["t2_route"] == "11-fast-answers"

    c = out["C_shim_without_ensure"]
    ok &= c["t1_slow"] is True
    ok &= c["t2_sticky"] is None
    ok &= c["t2_source"] == "no-marker"

    print("MUTANT_CONFIRMS_THE_FIX" if ok else "MUTANT_CHECK_FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
