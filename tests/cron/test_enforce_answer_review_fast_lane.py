"""Hermetic tests for the review-forcer fast-lane skip logic (design §7 / D5, t_f8274c12).

Acceptance point (4) of t_3caaf861:

  enforce_answer_review.py must SKIP creating review cards for FAST requests
  that carry no slow marker, and must STILL create review cards for:
    * slow requests (no fast marker), and
    * fast requests that DO carry a slow marker («подумай» / «гигантская задача»).
  The old behaviour is preserved for every other case, and the watermark
  advances past every scanned id even when the card is skipped (§10.5 — fast
  sessions never get stuck).

The script under test lives OUTSIDE this repo — it is a living file owned by
profile 01-orchestrator (not tracked by the engine git repo, per
fast-lane-design.md §11). So this suite executes a *copy* of the real script
with its path constants redirected into a temp dir, forces ``--dry-run``
(never shells out to the hermes CLI), and exercises both the pure helper
functions and the full ``new_answers``/``main`` pipeline against freshly made
SQLite schemas. No real state.db / kanban.db / enforcer_state.json is ever
touched.
"""
from __future__ import annotations

import io
import json
import os
import shutil
import sqlite3
import sys
import time

import pytest

# The living script under test. Path is overridable via env so a CI host that
# does not carry it can skip instead of failing.
ENFORCER = os.environ.get(
    "HERMES_ENFORCER_SCRIPT",
    r"C:/Users/hermes/AppData/Local/hermes/scripts/enforce_answer_review.py",
)
AUDIT = (r"C:/Users/hermes/AppData/Local/hermes/profiles/01-orchestrator/"
         r"scripts/answer_review_audit.py")

pytestmark = pytest.mark.skipif(
    not os.path.isfile(ENFORCER),
    reason=f"enforce_answer_review.py not present at {ENFORCER}",
)


# ---------------------------------------------------------------------------
# Hermetic loading: execute a COPY of the enforcer script with its path
# constants redirected into a temp dir and HERMES neutered to a non-existent
# path (``--dry-run`` never reaches create_card's subprocess branch anyway).
# ---------------------------------------------------------------------------


def build_dbs(dirp):
    """Create hermetic state.db + kanban.db with the real schemas' columns."""
    sdb = os.path.join(dirp, "state.db")
    con = sqlite3.connect(sdb)
    con.execute(
        """CREATE TABLE sessions (
            id TEXT PRIMARY KEY, source TEXT NOT NULL, model_config TEXT,
            profile_name TEXT, started_at REAL NOT NULL DEFAULT 0)"""
    )
    con.execute(
        """CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
            role TEXT NOT NULL, content TEXT, tool_calls TEXT, tool_name TEXT,
            timestamp REAL NOT NULL, finish_reason TEXT)"""
    )
    con.commit()
    con.close()

    kdb = os.path.join(dirp, "kanban.db")
    con2 = sqlite3.connect(kdb)
    con2.execute(
        """CREATE TABLE tasks (
            id TEXT PRIMARY KEY, assignee TEXT, title TEXT, body TEXT,
            created_at REAL)"""
    )
    con2.commit()
    con2.close()
    return sdb, kdb


def load_enforcer(dirp, sdb, kdb):
    """Exec the live script with redirected constants; return its namespace."""
    code = open(ENFORCER, encoding="utf-8").read()
    code = code.replace(
        'STATE_DB = os.path.join(PROFILE_HOME, "state.db")',
        'STATE_DB = r"%s"' % sdb.replace("\\", "/"),
    )
    code = code.replace(
        'KANBAN_DB = r"C:/Users/hermes/AppData/Local/hermes/kanban.db"',
        'KANBAN_DB = r"%s"' % kdb.replace("\\", "/"),
    )
    code = code.replace(
        'STATE_FILE = os.path.join(AUD_DIR, "enforcer_state.json")',
        'STATE_FILE = r"%s"' % os.path.join(dirp, "enforcer_state.json").replace("\\", "/"),
    )
    code = code.replace(
        'HERMES = r"C:/Users/hermes/AppData/Local/hermes/hermes-agent/venv/Scripts/hermes"',
        'HERMES = r"C:/__no_such_hermes__"',
    )
    code = code.replace(
        'os.path.join(PROFILE_HOME, "scripts", "answer_review_audit.py")',
        r'"%s"' % AUDIT.replace("\\", "/"),
    )
    ns = {"__name__": "enforce_under_test"}
    exec(compile(code, ENFORCER, "exec"), ns)
    return ns


def run_dry(ns, out_buf):
    """Invoke main() in --dry-run mode, capturing its stdout into out_buf."""
    sys.argv = ["enforce_answer_review.py", "--dry-run"]
    real_stdout = sys.stdout
    sys.stdout = out_buf
    try:
        return ns["main"]()
    finally:
        sys.stdout = real_stdout


# ---------------------------------------------------------------------------
# DB population helpers (mirror the t_f8274c12 hermetic acceptance matrix)
# ---------------------------------------------------------------------------


def _add_session(con, sid, source, profile=None, mc=None):
    con.execute(
        "INSERT INTO sessions (id, source, model_config, profile_name, started_at)"
        " VALUES (?,?,?,?,?)",
        (sid, source, mc, profile, time.time() - 5000),
    )


def _add_msg(con, sid, role, content, ts_delta, finish="stop"):
    con.execute(
        "INSERT INTO messages (session_id, role, content, tool_calls, timestamp, finish_reason)"
        " VALUES (?,?,?,?,?,?)",
        (sid, role, content, None, time.time() - ts_delta, finish),
    )


FAST_MC = {"fast_lane": True, "review_exempt": True, "slow_mode": False}


def _populate(con):
    """One shared DB, independent sessions — the full A–F acceptance matrix."""
    # A) fast, no slow marker -> SKIP (no card)
    _add_session(con, "s" + "f" * 30, "telegram", mc=json.dumps(FAST_MC))
    _add_msg(con, "s" + "f" * 30, "user", "сколько будет 2+2", 4000)
    _add_msg(con, "s" + "f" * 30, "assistant", "ответ сформулирован", 3500)

    # B) slow, no fast flag -> card
    _add_session(con, "s" + "g" * 30, "telegram")
    _add_msg(con, "s" + "g" * 30, "user", "расскажи про черные дыры", 4000)
    _add_msg(con, "s" + "g" * 30, "assistant", "ответ сформулирован", 3500)

    # C) fast + «подумай» -> card
    s = "s" + "h" * 30
    _add_session(con, s, "telegram", mc=json.dumps(FAST_MC))
    _add_msg(con, s, "user", "подумай над архитектурой сервиса", 4000)
    _add_msg(con, s, "assistant", "ответ сформулирован", 3500)

    # D) fast + «гигантская задача» -> card
    s = "s" + "i" * 30
    _add_session(con, s, "api_server", mc=json.dumps(FAST_MC))
    _add_msg(con, s, "user", "это гигантская задача, нужен план", 4000)
    _add_msg(con, s, "assistant", "ответ сформулирован", 3500)

    # E) flag OFF (fast_lane=false) -> old behaviour, card
    s = "s" + "j" * 30
    _add_session(con, s, "telegram", mc=json.dumps(
        {"fast_lane": False, "review_exempt": False, "slow_mode": False}))
    _add_msg(con, s, "user", "привет как дела", 4000)
    _add_msg(con, s, "assistant", "ответ сформулирован", 3500)

    # F) fast, but NO user request text in DB (edge case) -> no marker => SKIP
    s = "s" + "k" * 30
    _add_session(con, s, "telegram", mc=json.dumps(
        {"fast_lane": True, "review_exempt": True}))
    _add_msg(con, s, "assistant", "ответ", 3500)

    # G) marker stored as STRING "false" -> string trap must NOT read as fast -> card
    s = "s" + "l" * 30
    _add_session(con, s, "telegram", mc=json.dumps(
        {"fast_lane": "false", "review_exempt": "false"}))
    _add_msg(con, s, "user", "обычный вопрос", 4000)
    _add_msg(con, s, "assistant", "ответ сформулирован", 3500)


# ---------------------------------------------------------------------------
# Acceptance point (1) — fast_lane.enabled defaults to FALSE in the shipped
# config (design §6.2). Pins the literal default so a future flip to True by
# default is caught here, independently of the runtime gate reader in
# agent/fast_lane.py (tested in tests/agent/test_fast_lane_fast_path.py).
# ---------------------------------------------------------------------------


def test_fast_lane_config_default_is_false():
    from hermes_cli.config_defaults import DEFAULT_CONFIG

    assert "fast_lane" in DEFAULT_CONFIG
    assert DEFAULT_CONFIG["fast_lane"].get("enabled") is False


# ---------------------------------------------------------------------------
# Pure-helper unit tests (no DB needed)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def enforcer_ns(tmp_path_factory):
    d = tmp_path_factory.mktemp("enforcer_unit")
    sdb, kdb = build_dbs(d)
    return load_enforcer(d, sdb, kdb)


def test_session_is_fast_lane(enforcer_ns):
    f = enforcer_ns["session_is_fast_lane"]
    assert f((None, None)) is False
    assert f(("11-fast-answers", None)) is True
    assert f(("01-orchestrator", '{"fast_lane": true}')) is True
    assert f(("01-orchestrator", '{"review_exempt": true}')) is True
    assert f(("01-orchestrator", '{"fast_lane": false}')) is False
    assert f(("01-orchestrator", '{"fast_lane": "false"}')) is False  # string trap
    assert f(("01-orchestrator", '{"review_exempt": 0}')) is False
    assert f(("01-orchestrator", '{"slow_mode": true}')) is False  # slow alone != fast
    assert f(("01-orchestrator", "not-json")) is False
    assert f(("01-orchestrator", "")) is False
    assert f(("01-orchestrator", None)) is False


def test_marker_bool(enforcer_ns):
    b = enforcer_ns["_marker_bool"]
    assert b("true") is True
    assert b("false") is False
    assert b("1") is True
    assert b("  TRUE ") is True
    assert b("да") is True
    assert b("0") is False
    assert b(True) is True
    assert b(False) is False
    assert b(0) is False
    assert b(2) is True
    assert b(None) is False
    assert b([]) is False


def test_has_slow_marker(enforcer_ns):
    h = enforcer_ns["has_slow_marker"]
    assert h("подумай над эскизом") is True
    assert h("Подумай: спроектируй") is True
    assert h("  думай давай") is True
    assert h("подумайка ещё раз") is False  # «подумайка» — НЕ slow (design §3)
    assert h("это гигантская задача") is True
    assert h("гигантская задача по плану") is True
    assert h("гигантский план запуска") is True
    assert h("гигантская миссия") is True
    assert h("гигантская работа") is False
    assert h("") is False
    assert h(None) is False
    assert h("сколько будет 2+2") is False


# ---------------------------------------------------------------------------
# End-to-end: full new_answers()/main() pipeline on a shared hermetic DB.
# ---------------------------------------------------------------------------


def test_enforcer_skips_fast_nomark_keeps_slow_and_marked(tmp_path):
    d = tmp_path / "db"
    d.mkdir()
    sdb, kdb = build_dbs(d)

    con = sqlite3.connect(sdb)
    _populate(con)
    con.commit()
    con.close()

    ns = load_enforcer(d, sdb, kdb)
    buf = io.StringIO()
    run_dry(ns, buf)
    out = buf.getvalue()

    skip_sids = set()
    for line in out.splitlines():
        if "[enforcer][skip-fast-lane]" in line:
            for tok in line.split():
                if tok.startswith("session="):
                    skip_sids.add(tok.split("=", 1)[1])

    created_sids = set()
    for line in out.splitlines():
        if "[dry-run] would create:" in line:
            t = line.split("would create:")[1]
            for sid in ("s" + c * 30 for c in "ghijl"):
                if sid[:26] in t:
                    created_sids.add(sid)

    expected_skip = {"s" + "f" * 30, "s" + "k" * 30}
    expected_card = {
        "s" + "g" * 30,  # slow plain
        "s" + "h" * 30,  # fast + «подумай»
        "s" + "i" * 30,  # fast + «гигантская задача»
        "s" + "j" * 30,  # flag OFF
        "s" + "l" * 30,  # string-"false" trap
    }

    assert skip_sids == expected_skip, f"skip set wrong: got={skip_sids} want={expected_skip}"
    missing = expected_card - created_sids
    assert not missing, f"expected cards not created: {missing}"
    extra = created_sids - expected_card
    assert not extra, f"unexpected cards created: {extra}"

    # Watermark must advance past ALL scanned ids (incl. skipped fast) — §10.5.
    st = json.load(open(d / "enforcer_state.json", encoding="utf-8"))
    ro = sqlite3.connect(f"file:{sdb}?mode=ro", uri=True)
    maxid = ro.execute("SELECT COALESCE(MAX(id),0) FROM messages").fetchone()[0]
    ro.close()
    assert st.get("watermark", 0) >= maxid, (
        f"watermark {st.get('watermark')} < max id {maxid} | out={out}"
    )
