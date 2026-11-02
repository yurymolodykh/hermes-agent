"""First-turn latch (t_b36e6939): the marker must survive a missing session row.

The defect this file pins
-------------------------
``SessionDB.patch_session_model_config`` is a documented NO-OP when the session
row does not exist yet (``_MODEL_CONFIG_ROW_MISSING``). ``api_server`` /
``gateway/run.py`` write the fast-lane marker from ``_create_agent``, which runs
BEFORE the agent creates its own row (``AIAgent._ensure_db_session`` fires on
the first turn flush). So on the FIRST turn of a brand-new session the marker
was dropped silently: that turn ran SLOW, the next marker-less turn was judged
``source=no-marker`` and routed to ``11-fast-answers``.

Live evidence (parent card t_e031718d, report §3)::

    20:22:25 fast_lane=SLOW  session=owui-sticky-proof-... source=tg-prefix
    20:22:39 fast_lane=ROUTE session=owui-sticky-proof-... target=11-fast-answers

Contract this file locks in
---------------------------
1. ``patch_session_fast_lane_marker`` on a session with NO row creates the row
   and the marker reads back ``True`` (reader, async reader and the read-only
   ``session_slow_mode_from_db_file`` probe must all agree).
2. Turn 1 «подумай» -> turn 2 marker-less: turn 2 is ``session-sticky`` and
   stays on the origin profile (``route_profile`` is ``None``).
3. A clean session is untouched (still fast, still 11-fast-answers).
4. No new write per turn: once the row exists the ensure path is NOT re-run.
5. A handle that cannot create rows (legacy fake, third-party shim) keeps the
   old best-effort contract: no raise, no warning, no crash.
6. A write that still does not land after ensure IS reported at WARNING (the
   whole point of the card: today the failure is invisible).
7. MUTANT / discrimination: the same scenario against a handle WITHOUT
   ``ensure_session`` loses the marker — i.e. test 1 passes because of the
   ensure path, not tautologically.
8. Anti-regression: the agent's own later ``create_session`` (which carries
   ``max_iterations`` / ``reasoning_config`` / ``max_tokens``) must still land
   its keys on the row the marker created.
"""

from __future__ import annotations

import json
import logging

import pytest

from agent.fast_lane import (
    decide_slow_mode,
    patch_session_fast_lane_marker,
    resolve_fast_path,
    route_profile,
    session_slow_mode,
    session_slow_mode_async,
    session_slow_mode_from_db_file,
)

# What ``AIAgent`` stamps into ``model_config`` when it creates the row
# (``agent/agent_init.py::_session_init_model_config``), i.e. the write that
# used to be the ONLY writer on the first turn of an OWUI session.
INIT_MODEL_CONFIG = {
    "max_iterations": 200,
    "reasoning_config": {"enabled": True, "effort": "high"},
    "max_tokens": None,
}


def _slow():
    return resolve_fast_path(
        decide_slow_mode("подумай: разбери архитектуру"), "подумай: разбери"
    )


def _fast():
    return resolve_fast_path(decide_slow_mode("сколько будет 2+2?"), "2+2?")


def _real_db(tmp_path):
    from hermes_state import SessionDB

    return SessionDB(tmp_path / "state.db")


class _CountingDB:
    """Transparent proxy over a real ``SessionDB`` that counts the two calls."""

    def __init__(self, inner):
        self._inner = inner
        self.ensure_calls: list = []
        self.patch_calls: list = []

    def ensure_session(self, *args, **kwargs):
        self.ensure_calls.append((args, kwargs))
        return self._inner.ensure_session(*args, **kwargs)

    def patch_session_model_config(self, session_id, patch):
        self.patch_calls.append((session_id, dict(patch)))
        return self._inner.patch_session_model_config(session_id, patch)

    def __getattr__(self, name):
        return getattr(self._inner, name)


class _LegacyShim:
    """SessionDB-shaped handle WITHOUT ``ensure_session`` (old fakes, shims)."""

    def __init__(self, con):
        self._con = con

    def patch_session_model_config(self, sid, patch):
        row = self._con.execute(
            "SELECT model_config FROM sessions WHERE id=?", (sid,)
        ).fetchone()
        if row is None:
            return  # documented no-op — exactly the defect's mechanism
        cur = json.loads(row[0]) if row[0] else {}
        for k, v in patch.items():
            if v is None:
                cur.pop(k, None)
            else:
                cur[k] = v
        self._con.execute(
            "UPDATE sessions SET model_config=? WHERE id=?", (json.dumps(cur), sid)
        )
        self._con.commit()

    def get_session_model_config_value(self, sid, key, default=None):
        row = self._con.execute(
            "SELECT model_config FROM sessions WHERE id=?", (sid,)
        ).fetchone()
        if row is None:
            return default
        return (json.loads(row[0]) if row[0] else {}).get(key, default)


class _SilentPatchDB:
    """Real ``SessionDB`` whose marker write is a no-op (write-loss simulator)."""

    def __init__(self, inner):
        self._inner = inner

    def ensure_session(self, *args, **kwargs):
        return self._inner.ensure_session(*args, **kwargs)

    def patch_session_model_config(self, session_id, patch):
        return None  # write silently vanishes

    def __getattr__(self, name):
        return getattr(self._inner, name)


# ---------------------------------------------------------------------------
# 1. The core defect: marker write on a session with no row yet
# ---------------------------------------------------------------------------

class TestFirstTurnMarkerLands:
    def test_marker_lands_when_the_row_does_not_exist_yet(self, tmp_path):
        db = _real_db(tmp_path)
        try:
            # Precondition: this is the FIRST turn of a brand-new session.
            assert session_slow_mode(db, "owui-first") is None
            assert db.get_session("owui-first") is None

            patch_session_fast_lane_marker(
                db, "owui-first", _slow(), preserve_sticky=True, source="api_server"
            )

            # The latch every later turn depends on.
            assert session_slow_mode(db, "owui-first") is True
            row = db.get_session("owui-first")
            assert row is not None, "the marker write did not create the row"
            assert row["source"] == "api_server"
            marker = json.loads(row["model_config"])
            assert marker["slow_mode"] is True
            assert marker["slow_mode_source"] == "tg-prefix"
        finally:
            db.close()

    def test_readonly_file_probe_sees_the_latched_marker(self, tmp_path):
        """The api_server pre-``_create_agent`` probe must agree next turn."""
        db = _real_db(tmp_path)
        try:
            patch_session_fast_lane_marker(
                db, "owui-probe", _slow(), preserve_sticky=True, source="api_server"
            )
            assert session_slow_mode_from_db_file(db.db_path, "owui-probe") is True
        finally:
            db.close()

    @pytest.mark.asyncio
    async def test_async_reader_agrees_with_the_latched_marker(self, tmp_path):
        db = _real_db(tmp_path)
        try:
            patch_session_fast_lane_marker(
                db, "owui-async", _slow(), preserve_sticky=True, source="api_server"
            )
            assert await session_slow_mode_async(db, "owui-async") is True
        finally:
            db.close()

    def test_fast_marker_on_a_missing_row_creates_the_row_too(self, tmp_path):
        """A clean first turn still owns its row (no half-written state)."""
        db = _real_db(tmp_path)
        try:
            patch_session_fast_lane_marker(
                db, "owui-clean", _fast(), preserve_sticky=True, source="api_server"
            )
            assert session_slow_mode(db, "owui-clean") is False
        finally:
            db.close()


# ---------------------------------------------------------------------------
# 2-3. The owner-visible scenario: two turns, then a clean sibling
# ---------------------------------------------------------------------------

class TestFirstTurnLatchScenario:
    def test_turn_two_in_a_brand_new_session_stays_sticky(self, tmp_path):
        db = _real_db(tmp_path)
        try:
            # T1: «подумай» on a session with no row (the defect's trigger).
            t1_sticky = session_slow_mode(db, "owui-s")
            t1 = resolve_fast_path(
                decide_slow_mode("подумай: разбери архитектуру", session_slow=t1_sticky),
                "подумай: разбери архитектуру",
            )
            patch_session_fast_lane_marker(
                db, "owui-s", t1, preserve_sticky=True, source="api_server"
            )
            assert t1.slow_mode is True
            assert t1.source == "tg-prefix"

            # T2: no marker at all — must inherit the latch.
            t2_sticky = session_slow_mode(db, "owui-s")
            t2 = resolve_fast_path(
                decide_slow_mode("а теперь покороче, что по срокам?", session_slow=t2_sticky),
                "а теперь покороче, что по срокам?",
            )
            assert t2_sticky is True, "the first-turn latch never persisted"
            assert t2.slow_mode is True
            assert t2.source == "session-sticky"
            assert route_profile(t2) is None, "a sticky turn escaped to the fast profile"
        finally:
            db.close()

    def test_clean_sibling_session_is_unaffected(self, tmp_path):
        db = _real_db(tmp_path)
        try:
            patch_session_fast_lane_marker(
                db, "owui-slow", _slow(), preserve_sticky=True, source="api_server"
            )
            d = resolve_fast_path(
                decide_slow_mode(
                    "сколько будет 2+2?",
                    session_slow=session_slow_mode(db, "owui-other"),
                ),
                "сколько будет 2+2?",
            )
            assert d.slow_mode is False
            assert d.source == "no-marker"
            assert route_profile(d) == "11-fast-answers"
        finally:
            db.close()


# ---------------------------------------------------------------------------
# 4-5. Cost and degradation
# ---------------------------------------------------------------------------

class TestEnsureIsScopedAndSafe:
    def test_existing_row_is_not_re_created(self, tmp_path):
        """No extra write per turn: the ensure path runs only when it must."""
        inner = _real_db(tmp_path)
        db = _CountingDB(inner)
        try:
            patch_session_fast_lane_marker(
                db, "owui-busy", _slow(), preserve_sticky=True, source="api_server"
            )
            assert len(db.ensure_calls) == 1, db.ensure_calls
            # Turn 2 of the same session: row exists, ensure must be skipped.
            patch_session_fast_lane_marker(
                db, "owui-busy", _fast(), preserve_sticky=True, source="api_server"
            )
            patch_session_fast_lane_marker(
                db, "owui-busy", _slow(), preserve_sticky=True, source="api_server"
            )
            assert len(db.ensure_calls) == 1, (
                f"ensure_session re-ran on a session that already had a row: "
                f"{db.ensure_calls}"
            )
        finally:
            inner.close()

    def test_handle_without_ensure_keeps_the_best_effort_contract(self, caplog):
        """Old fakes / third-party shims: no raise, no warning, no change."""
        import sqlite3

        con = sqlite3.connect(":memory:")
        try:
            con.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, model_config TEXT)")
            shim = _LegacyShim(con)
            with caplog.at_level(logging.WARNING, logger="agent.fast_lane"):
                patch_session_fast_lane_marker(
                    shim, "gone", _slow(), preserve_sticky=True, source="api_server"
                )
            assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []
        finally:
            con.close()

    def test_broken_db_never_raises(self):
        class Boom:
            def get_session(self, *a, **k):
                raise RuntimeError("locked")

            def ensure_session(self, *a, **k):
                raise RuntimeError("locked")

            def patch_session_model_config(self, *a, **k):
                raise RuntimeError("locked")

        patch_session_fast_lane_marker(Boom(), "s", _slow(), preserve_sticky=True)
        patch_session_fast_lane_marker(None, "s", _slow(), preserve_sticky=True)
        patch_session_fast_lane_marker(object(), "s", _slow(), preserve_sticky=True)


# ---------------------------------------------------------------------------
# 6. A write that still does not land must be VISIBLE
# ---------------------------------------------------------------------------

class TestLostWriteIsReported:
    def test_warning_when_the_marker_still_does_not_land(self, tmp_path, caplog):
        inner = _real_db(tmp_path)
        db = _SilentPatchDB(inner)
        try:
            with caplog.at_level(logging.WARNING, logger="agent.fast_lane"):
                patch_session_fast_lane_marker(
                    db, "owui-lost", _slow(), preserve_sticky=True, source="api_server"
                )
            warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
            assert warnings, "a lost slow-mode marker must not be silent"
            assert any("owui-lost" in r.getMessage() for r in warnings), [
                r.getMessage() for r in warnings
            ]
        finally:
            inner.close()

    def test_no_warning_on_the_healthy_path(self, tmp_path, caplog):
        db = _real_db(tmp_path)
        try:
            with caplog.at_level(logging.WARNING, logger="agent.fast_lane"):
                patch_session_fast_lane_marker(
                    db, "owui-ok", _slow(), preserve_sticky=True, source="api_server"
                )
                patch_session_fast_lane_marker(
                    db, "owui-ok", _fast(), preserve_sticky=True, source="api_server"
                )
            assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []
        finally:
            db.close()


# ---------------------------------------------------------------------------
# 7. Mutant / discrimination: the ensure path is what makes test 1 pass
# ---------------------------------------------------------------------------

class TestEnsureIsTheDiscriminator:
    def test_without_ensure_the_first_turn_marker_is_lost(self):
        """Remove the ensure path and the defect reproduces — so test 1 is real.

        This is the mutation arm of the card's criterion 4: the same scenario,
        same decision, same writer, against a handle that cannot create rows.
        If this test ever starts passing as ``True``, the ensure path stopped
        being the thing that fixes the latch.
        """
        import sqlite3

        con = sqlite3.connect(":memory:")
        try:
            con.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, model_config TEXT)")
            shim = _LegacyShim(con)
            patch_session_fast_lane_marker(
                shim, "owui-mutant", _slow(), preserve_sticky=True, source="api_server"
            )
            assert session_slow_mode(shim, "owui-mutant") is None
        finally:
            con.close()


# ---------------------------------------------------------------------------
# 8. Anti-regression: the agent's own row creation still lands its config
# ---------------------------------------------------------------------------

class TestAgentRowCreationStillEnriches:
    def test_agent_create_after_the_marker_keeps_its_init_model_config(self, tmp_path):
        """The marker-created row must not swallow the agent's init config.

        ``_insert_session_row`` upserts with ``COALESCE(sessions.model_config,
        excluded.model_config)`` — once the marker created the row, a later
        ``create_session`` carrying ``max_iterations`` / ``reasoning_config`` /
        ``max_tokens`` would be discarded. ``_reset_from`` lineage markers and
        a resume's stored reasoning are read straight off this JSON, so losing
        it silently breaks resume.
        """
        db = _real_db(tmp_path)
        try:
            patch_session_fast_lane_marker(
                db, "owui-enrich", _slow(), preserve_sticky=True, source="api_server"
            )
            db.create_session(
                "owui-enrich",
                source="api_server",
                model="deepseek-v4-flash",
                model_config=dict(INIT_MODEL_CONFIG),
            )
            row = db.get_session("owui-enrich")
            config = json.loads(row["model_config"])
            assert row["model"] == "deepseek-v4-flash"
            # the enrichment landed...
            assert config["max_iterations"] == 200
            assert config["reasoning_config"] == {"enabled": True, "effort": "high"}
            # ...and the latch survived it.
            assert config["slow_mode"] is True
            assert config["slow_mode_source"] == "tg-prefix"
        finally:
            db.close()

    def test_existing_values_keep_precedence_over_a_later_bare_create(self, tmp_path):
        """Merge discipline: an earlier writer's keys are never overwritten."""
        db = _real_db(tmp_path)
        try:
            db.create_session(
                "owui-prec", source="api_server", model_config=dict(INIT_MODEL_CONFIG)
            )
            db.create_session(
                "owui-prec",
                source="unknown",
                model_config={"max_iterations": 11, "_delegate_from": "parent"},
            )
            config = json.loads(db.get_session("owui-prec")["model_config"])
            assert config["max_iterations"] == 200, config
            assert config["_delegate_from"] == "parent", config
            assert config["reasoning_config"] == {"enabled": True, "effort": "high"}
        finally:
            db.close()


# ---------------------------------------------------------------------------
# 9. The early row must keep the session's identity columns honest
# ---------------------------------------------------------------------------

class TestEarlyRowIdentity:
    """The marker-created row is NOT a stub that later writers cannot fix.

    ``_insert_session_row``'s upsert enriches ``model`` / ``session_key`` /
    ``chat_id`` / ``origin_json`` / ``display_name`` but does NOT touch
    ``user_id`` — so an early insert must either carry the real owner or be
    ``NULL`` there. It carries ``NULL`` (the fast-lane seam has no owner id),
    which is the same shape a gateway-created row starts with, and
    ``record_gateway_session_peer`` / the next ``create_session`` fill the rest.
    These two tests pin the parts that could silently rot: the ``source``
    column must be the REAL platform (not the ``"unknown"`` default), and the
    later enriching write must still win the columns it owns.
    """

    def test_source_column_is_the_real_platform_not_unknown(self, tmp_path):
        db = _real_db(tmp_path)
        try:
            patch_session_fast_lane_marker(
                db, "owui-src", _slow(), preserve_sticky=True, source="api_server"
            )
            assert db.get_session("owui-src")["source"] == "api_server"
            # And a later bare create cannot downgrade it.
            db.create_session("owui-src", source="unknown")
            assert db.get_session("owui-src")["source"] == "api_server"
        finally:
            db.close()

    def test_identity_columns_are_filled_by_the_later_enriching_write(self, tmp_path):
        db = _real_db(tmp_path)
        try:
            patch_session_fast_lane_marker(
                db, "owui-ident", _slow(), preserve_sticky=True, source="api_server"
            )
            early = db.get_session("owui-ident")
            assert early["user_id"] is None  # marker seam has no owner id
            assert early["model"] is None    # ...and no model

            db.create_session(
                "owui-ident",
                source="api_server",
                model="deepseek-v4-flash",
                user_id="owner-1",
                session_key="lane-key",
                chat_id="chat-1",
            )
            row = db.get_session("owui-ident")
            assert row["model"] == "deepseek-v4-flash"
            assert row["session_key"] == "lane-key"
            assert row["chat_id"] == "chat-1"
            # The latch is still there after enrichment.
            assert json.loads(row["model_config"])["slow_mode"] is True
        finally:
            db.close()
