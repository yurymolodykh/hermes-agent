"""Session-sticky slow mode (t_e0350c47).

Once a session has been slowed by a marker („подумай“ / „гигантская задача“ /
OWUI deepthink), EVERY later message in the SAME session runs on the slow lane
(01-orchestrator) even when it carries no marker of its own.

Contract under test:

1. :func:`agent.fast_lane.session_slow_mode` — the reader. ``True``/``False``
   for a persisted marker, ``None`` for no row / no key / no DB / no getter /
   a raised read / an un-unwrappable async door. NEVER raises.
2. :func:`agent.fast_lane.session_slow_mode_async` — the loop-safe variant used
   by api_server request handlers (offloads through ``AsyncSessionDB``).
3. :func:`agent.fast_lane.apply_session_sticky` — the precedence rule:
   explicit fast escape > sticky > per-request markers > fast default.
4. ``decide_slow_mode(..., session_slow=...)`` — wiring the reader value in
   without making the decision function an I/O function.
5. ``patch_session_fast_lane_marker(..., preserve_sticky=True)`` — the marker is
   MONOTONIC: a later marker-less turn never downgrades a slow session.
6. ``AsyncSessionDB`` writes actually land (regression: the messenger path
   silently dropped every marker because the sync helper got a coroutine door).
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import sqlite3

import pytest

from agent.fast_lane import (
    FastLaneDecision,
    apply_session_sticky,
    decide_slow_mode,
    evaluate_request,
    explicit_fast_escape,
    patch_session_fast_lane_marker,
    resolve_fast_path,
    route_profile,
    session_slow_mode,
    session_slow_mode_async,
    session_slow_mode_from_db_file,
)


# ---------------------------------------------------------------------------
# Fake DBs
# ---------------------------------------------------------------------------

def _make_session_db(*, with_marker=None, existing=True):
    """sqlite-backed fake exposing the two model_config methods."""
    con = sqlite3.connect(":memory:", check_same_thread=False)
    con.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, model_config TEXT)")
    if existing:
        cfg = {"_branched_from": "parent-1"}
        if with_marker is not None:
            cfg["slow_mode"] = with_marker
        con.execute(
            "INSERT INTO sessions (id, model_config) VALUES ('sess-1', ?)",
            (json.dumps(cfg),),
        )
        con.commit()

    class FakeDB:
        def patch_session_model_config(self, sid, patch):
            row = con.execute(
                "SELECT model_config FROM sessions WHERE id=?", (sid,)
            ).fetchone()
            if row is None:
                return
            cur = json.loads(row[0]) if row[0] else {}
            for k, v in patch.items():
                if v is None:
                    cur.pop(k, None)
                else:
                    cur[k] = v
            con.execute(
                "UPDATE sessions SET model_config=? WHERE id=?",
                (json.dumps(cur), sid),
            )
            con.commit()

        def get_session_model_config_value(self, sid, key, default=None):
            row = con.execute(
                "SELECT model_config FROM sessions WHERE id=?", (sid,)
            ).fetchone()
            if row is None:
                return default
            cur = json.loads(row[0]) if row[0] else {}
            return cur.get(key, default)

    return con, FakeDB()


class _AsyncDoor:
    """Minimal stand-in for hermes_state.AsyncSessionDB (coroutine forwarder)."""

    def __init__(self, inner):
        self._db = inner

    def __getattr__(self, name):
        attr = getattr(self._db, name)
        if not callable(attr):
            return attr

        async def _offloaded(*args, **kwargs):
            return await asyncio.to_thread(attr, *args, **kwargs)

        return _offloaded


# ---------------------------------------------------------------------------
# 1. Reader: session_slow_mode
# ---------------------------------------------------------------------------

class TestSessionSlowModeReader:
    def test_true_marker_reads_back_true(self):
        con, db = _make_session_db(with_marker=True)
        try:
            assert session_slow_mode(db, "sess-1") is True
        finally:
            con.close()

    def test_false_marker_reads_back_false(self):
        con, db = _make_session_db(with_marker=False)
        try:
            assert session_slow_mode(db, "sess-1") is False
        finally:
            con.close()

    def test_missing_key_is_none(self):
        con, db = _make_session_db(with_marker=None)
        try:
            assert session_slow_mode(db, "sess-1") is None
        finally:
            con.close()

    def test_missing_row_is_none(self):
        con, db = _make_session_db(existing=False)
        try:
            assert session_slow_mode(db, "nope") is None
        finally:
            con.close()

    def test_no_db_or_no_sid_is_none(self):
        assert session_slow_mode(None, "sess-1") is None
        con, db = _make_session_db(with_marker=True)
        try:
            assert session_slow_mode(db, None) is None
            assert session_slow_mode(db, "") is None
        finally:
            con.close()

    def test_object_without_getter_is_none(self):
        assert session_slow_mode(object(), "sess-1") is None

    def test_raising_getter_is_none_and_does_not_raise(self):
        class Boom:
            def get_session_model_config_value(self, *a, **k):
                raise RuntimeError("db is gone")

        assert session_slow_mode(Boom(), "sess-1") is None

    def test_truthy_non_bool_is_coerced(self):
        class StrDB:
            def get_session_model_config_value(self, sid, key, default=None):
                return "true"

        assert session_slow_mode(StrDB(), "sess-1") is True

        class IntDB:
            def get_session_model_config_value(self, sid, key, default=None):
                return 0

        assert session_slow_mode(IntDB(), "sess-1") is False

    def test_async_door_is_unwrapped(self):
        """gateway/run.py hands AsyncSessionDB; the sync reader must see through it."""
        con, db = _make_session_db(with_marker=True)
        try:
            assert session_slow_mode(_AsyncDoor(db), "sess-1") is True
        finally:
            con.close()

    def test_ununwrapable_async_getter_is_none_without_warning_noise(self):
        """A getter that is a coroutine function and has no _db -> None, no raise."""

        class AwaitableDB:
            def get_session_model_config_value(self, sid, key, default=None):
                async def _inner():
                    return True

                return _inner()

        assert session_slow_mode(AwaitableDB(), "sess-1") is None

    def test_autovivifying_stub_does_not_hang_the_unwrap(self):
        """Regression: an unbounded ``_db`` walk looped forever on a MagicMock.

        ``getattr(mock, "_db")`` returns a NEW child mock each access, so the
        identity-cycle guard never trips. Any lazy-proxy DB (or a test double)
        handed to the gateway would have hung the request path; the walk must
        be depth-bounded.
        """
        import time
        from unittest.mock import MagicMock

        started = time.monotonic()
        result = session_slow_mode(MagicMock(), "sess-1")
        elapsed = time.monotonic() - started
        assert result is None
        assert elapsed < 2.0, f"unwrap took {elapsed:.1f}s — depth bound missing"

    def test_unrecognized_marker_type_is_no_opinion(self):
        """A non-scalar marker (proxy/mock/dict) must not read as False.

        ``False`` means "this session explicitly wants the fast lane" and would
        silently cancel stickiness; anything we cannot interpret is "no
        opinion" (None).
        """

        class WeirdDB:
            def get_session_model_config_value(self, sid, key, default=None):
                return {"nested": True}

        assert session_slow_mode(WeirdDB(), "sess-1") is None

    @pytest.mark.parametrize(
        "stored,expected",
        [
            ("true", True),
            ("True", True),
            ("TRUE", True),
            ("1", True),
            (" false ", False),
            ("False", False),
            ("0", False),
            (1, True),
            (0, False),
        ],
    )
    def test_string_and_int_forms_are_normalized(self, stored, expected):
        """Card item 3: 'true'/'1'/1 -> True, 'false'/'0'/0 -> False."""

        class _DB:
            def __init__(self, value):
                self.value = value

            def get_session_model_config_value(self, sid, key, default=None):
                return self.value

        assert session_slow_mode(_DB(stored), "sess-1") is expected

    @pytest.mark.parametrize("stored", ["garbage", "", "maybe", 2, -1, 2.5, 0.0])
    def test_unrecognized_scalars_are_no_opinion(self, stored):
        """Card item 3: anything unrecognized -> None (never a silent False).

        A lenient coercion would turn ``2.5`` into True and ``"garbage"`` into
        False; the second is the dangerous one — False cancels stickiness.
        """

        class _DB:
            def __init__(self, value):
                self.value = value

            def get_session_model_config_value(self, sid, key, default=None):
                return self.value

        assert session_slow_mode(_DB(stored), "sess-1") is None

    def test_benign_no_opinion_paths_do_not_log_warnings(self, caplog):
        """Card item 2: best-effort no-ops stay quiet; no warning per turn.

        An unmarked session is the NORMAL case, so a warning per request would
        flood the log. Only a raised read is worth reporting.
        """
        import logging

        class _NoGetter:
            pass

        class _Raising:
            def get_session_model_config_value(self, *a, **k):
                raise RuntimeError("boom")

        with caplog.at_level(logging.DEBUG, logger="agent.fast_lane"):
            assert session_slow_mode(None, "sess-1") is None
            assert session_slow_mode(_NoGetter(), "sess-1") is None
            assert session_slow_mode(_NoGetter(), "") is None
            benign = [r for r in caplog.records if r.levelno >= logging.WARNING]
            assert benign == []

        caplog.clear()
        with caplog.at_level(logging.DEBUG, logger="agent.fast_lane"):
            assert session_slow_mode(_Raising(), "sess-1") is None
            assert any(r.levelno >= logging.WARNING for r in caplog.records)


# ---------------------------------------------------------------------------
# 2. Async reader (api_server loop path)
# ---------------------------------------------------------------------------

class TestSessionSlowModeAsyncReader:
    @pytest.mark.asyncio
    async def test_async_door_is_awaited(self):
        con, db = _make_session_db(with_marker=True)
        try:
            assert await session_slow_mode_async(_AsyncDoor(db), "sess-1") is True
        finally:
            con.close()

    @pytest.mark.asyncio
    async def test_sync_db_still_works(self):
        con, db = _make_session_db(with_marker=False)
        try:
            assert await session_slow_mode_async(db, "sess-1") is False
        finally:
            con.close()

    @pytest.mark.asyncio
    async def test_none_and_missing(self):
        assert await session_slow_mode_async(None, "sess-1") is None
        con, db = _make_session_db(existing=False)
        try:
            assert await session_slow_mode_async(db, "gone") is None
        finally:
            con.close()

    @pytest.mark.asyncio
    async def test_error_degrades_to_none(self):
        class Boom:
            def get_session_model_config_value(self, *a, **k):
                raise RuntimeError("locked")

        assert await session_slow_mode_async(Boom(), "sess-1") is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "stored,expected",
        [("true", True), ("false", False), (1, True), (0, False)],
    )
    async def test_async_reader_normalizes_like_sync(self, stored, expected):
        """Both readers must agree, or sync/async call sites route differently."""
        con, db = _make_session_db(with_marker=stored)
        try:
            assert await session_slow_mode_async(db, "sess-1") is expected
            assert session_slow_mode(db, "sess-1") is expected
        finally:
            con.close()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("stored", ["garbage", 2, 2.5])
    async def test_async_reader_unrecognized_is_none(self, stored):
        con, db = _make_session_db(with_marker=stored)
        try:
            assert await session_slow_mode_async(db, "sess-1") is None
            assert session_slow_mode(db, "sess-1") is None
        finally:
            con.close()


# ---------------------------------------------------------------------------
# 3. Explicit fast escape
# ---------------------------------------------------------------------------

class TestExplicitFastEscape:
    def test_absent_is_not_an_escape(self):
        assert explicit_fast_escape(None) is False
        assert explicit_fast_escape({}) is False
        assert explicit_fast_escape({"reasoning": {"enabled": True}}) is False

    def test_false_flags_are_escapes(self):
        assert explicit_fast_escape({"slow_mode": False}) is True
        assert explicit_fast_escape({"slow": False}) is True
        assert explicit_fast_escape({"slow_mode": "false"}) is True

    def test_true_flags_are_not_escapes(self):
        assert explicit_fast_escape({"slow_mode": True}) is False
        assert explicit_fast_escape({"slow": True}) is False

    def test_explicit_none_is_not_an_escape(self):
        assert explicit_fast_escape({"slow_mode": None}) is False


# ---------------------------------------------------------------------------
# 4. apply_session_sticky precedence
# ---------------------------------------------------------------------------

def _fast() -> FastLaneDecision:
    return resolve_fast_path(decide_slow_mode("сколько будет 2+2?"), "2+2?")


def _slow() -> FastLaneDecision:
    return resolve_fast_path(
        decide_slow_mode("подумай: разбери архитектуру"), "подумай: разбери"
    )


class TestApplySessionSticky:
    def test_sticky_true_promotes_markerless_fast(self):
        d = apply_session_sticky(_fast(), True)
        assert d.slow_mode is True
        assert d.fast_lane is False
        assert d.mode == "slow"
        assert d.source == "session-sticky"
        # Slow requests are never review-exempt (D5).
        assert d.review_exempt is False

    def test_sticky_false_and_none_leave_decision_unchanged(self):
        for value in (False, None):
            d = apply_session_sticky(_fast(), value)
            assert d.slow_mode is False
            assert d.source == "no-marker"

    def test_sticky_with_own_marker_keeps_marker_source(self):
        base = _slow()
        d = apply_session_sticky(base, True)
        assert d.slow_mode is True
        # Card item 3: the log line must show BOTH reasons, so the source is
        # composite and the concrete marker stays readable separately.
        assert d.source == f"session-sticky+{base.source}"
        assert d.debug["sticky_with_source"] == base.source

    def test_composite_source_names_the_marker_for_every_marker_kind(self):
        for text, options, marker in (
            ("подумай: разбери", None, "tg-prefix"),
            ("гигантская задача: миграция", None, "phrase"),
            (
                "привет",
                {"reasoning": {"enabled": True, "effort": "high"}},
                "api-deepthink",
            ),
        ):
            base = decide_slow_mode(text, model_options=options)
            assert base.source == marker, (text, base.source)
            d = apply_session_sticky(base, True, model_options=options)
            assert d.source == f"session-sticky+{marker}"
            assert d.slow_mode is True

    def test_sticky_applied_twice_is_idempotent(self):
        d1 = apply_session_sticky(_slow(), True)
        d2 = apply_session_sticky(d1, True)
        assert d2.source == d1.source  # never session-sticky+session-sticky
        assert d2.debug["reasons"].count("session-sticky (already slow)") <= 2

    def test_explicit_fast_escape_beats_sticky(self):
        base = _fast()
        d = apply_session_sticky(base, True, model_options={"slow_mode": False})
        assert d.slow_mode is False
        assert d.source == "no-marker"
        assert d.debug["sticky_overridden_by"] == "explicit-fast-escape"

    def test_explicit_slow_flag_still_slow(self):
        base = decide_slow_mode("привет", model_options={"slow_mode": True})
        d = apply_session_sticky(base, True, model_options={"slow_mode": True})
        assert d.slow_mode is True
        # Composite source: the sticky session AND the explicit marker both
        # apply, and the log line shows both (card item 3).
        assert d.source == "session-sticky+api-deepthink"
        assert d.debug["sticky_with_source"] == "api-deepthink"

    def test_sticky_does_not_mutate_input(self):
        base = _fast()
        before = (base.slow_mode, base.source, dict(base.debug))
        apply_session_sticky(base, True)
        assert (base.slow_mode, base.source, dict(base.debug)) == before

    def test_sticky_does_not_mutate_input_on_the_cofire_branch(self):
        """The already-slow branch used to append into the INPUT's reasons.

        ``dict(decision.debug)`` shares the nested ``reasons`` list, so the
        co-fire path silently mutated the caller's decision — a caller that
        logged the pre-sticky decision showed a sticky reason it never had.
        Regression guard: compare a DEEP snapshot, not a shallow dict.
        """
        base = _slow()
        before = copy.deepcopy(base.debug)
        d = apply_session_sticky(base, True)
        assert base.debug == before
        assert base.source == "tg-prefix"
        # ...and the derived decision did get the extra reason.
        assert "session-sticky (already slow)" in d.debug["reasons"]
        assert d.debug["reasons"] is not base.debug["reasons"]

    def test_sticky_does_not_mutate_input_on_the_escape_branch(self):
        base = _fast()
        before = copy.deepcopy(base.debug)
        d = apply_session_sticky(base, True, model_options={"slow_mode": False})
        assert base.debug == before
        assert "sticky_overridden_by" not in base.debug
        assert d.debug["sticky_overridden_by"] == "explicit-fast-escape"


# ---------------------------------------------------------------------------
# 5. decide_slow_mode / evaluate_request wiring
# ---------------------------------------------------------------------------

class TestDecideSlowModeSessionSlow:
    def test_markerless_plus_sticky_is_slow(self):
        d = decide_slow_mode("сколько будет 2+2?", session_slow=True)
        assert d.slow_mode is True
        assert d.source == "session-sticky"

    def test_markerless_plus_no_sticky_stays_fast(self):
        for value in (None, False):
            d = decide_slow_mode("сколько будет 2+2?", session_slow=value)
            assert d.slow_mode is False
            assert d.source == "no-marker"

    def test_phrase_marker_wins_over_sticky_source(self):
        d = decide_slow_mode("гигантская задача: миграция", session_slow=True)
        assert d.slow_mode is True
        # The marker still wins the SCORE, but the source reports both so the
        # fast_lane=SLOW|ROUTE line explains why the session stayed slow.
        assert d.source == "session-sticky+phrase"
        assert d.debug["sticky_with_source"] == "phrase"

    def test_deepthink_marker_wins_over_sticky_source(self):
        d = decide_slow_mode(
            "привет",
            model_options={"reasoning": {"enabled": True, "effort": "high"}},
            session_slow=True,
        )
        assert d.slow_mode is True
        assert d.source == "session-sticky+api-deepthink"
        assert d.debug["sticky_with_source"] == "api-deepthink"

    def test_evaluate_request_forwards_session_slow(self):
        d = evaluate_request("обычный вопрос", session_slow=True)
        assert d.slow_mode is True
        assert d.source == "session-sticky"
        d2 = evaluate_request("обычный вопрос")
        assert d2.slow_mode is False

    def test_route_profile_follows_sticky(self):
        d = decide_slow_mode("обычный вопрос", session_slow=True)
        d = resolve_fast_path(d, "обычный вопрос")
        assert route_profile(d) is None  # None = stay on origin (01)
        d3 = decide_slow_mode("обычный вопрос")
        assert route_profile(resolve_fast_path(d3, "обычный вопрос")) == (
            "11-fast-answers"
        )

    @pytest.mark.parametrize(
        "raw",
        [True, 1, "true", "1", "yes", "on", "да", "slow"],
    )
    def test_truthy_marker_forms_force_slow(self, raw):
        """AC-2: a TRUTHY ``session_slow`` forces SLOW, not just ``True``.

        A call site that hands over the raw persisted marker (``'true'`` from
        JSON, ``1`` from an int column) must still stick. Gating the rule on a
        bare ``is True`` silently un-stuck those sessions.
        """
        d = decide_slow_mode("сколько будет 2+2?", session_slow=raw)  # type: ignore[arg-type]
        assert d.slow_mode is True
        assert d.source == "session-sticky"

    @pytest.mark.parametrize(
        "raw",
        [False, 0, "false", "0", "no", "off", "нет", "fast", None, "", 2, -1, 2.5, "garbage"],
    )
    def test_non_sticky_values_preserve_todays_behaviour(self, raw):
        """AC-2: ``None``/False/unrecognized keep the pre-sticky behaviour."""
        d = decide_slow_mode("сколько будет 2+2?", session_slow=raw)  # type: ignore[arg-type]
        assert d.slow_mode is False
        assert d.source == "no-marker"
        assert route_profile(resolve_fast_path(d, "сколько будет 2+2?")) == (
            "11-fast-answers"
        )

    def test_raw_marker_value_from_storage_sticks(self):
        """The value a real session row yields (JSON ``true``) sticks end-to-end."""
        con, db = _make_session_db(with_marker=True)
        try:
            # Read the RAW cell the way a careless call site would.
            raw = db.get_session_model_config_value("sess-1", "slow_mode", None)
            for value in (raw, session_slow_mode(db, "sess-1")):
                d = decide_slow_mode("спасибо", session_slow=value)
                assert d.slow_mode is True
                assert d.source == "session-sticky"
        finally:
            con.close()

    def test_truly_missing_db_and_session_id_is_none_then_not_slow(self):
        """AC-4: db/session_id None yields ``None`` and no exception end-to-end."""
        for db, sid in ((None, "sess-1"), (object(), None), (None, None)):
            sticky = session_slow_mode(db, sid)
            assert sticky is None
            d = decide_slow_mode("сколько будет 2+2?", session_slow=sticky)
            assert d.slow_mode is False
            assert d.source == "no-marker"

    def test_never_raises_on_hostile_input(self):
        for bad in (None, object(), 42):
            d = decide_slow_mode("текст", session_slow=bad)  # type: ignore[arg-type]
            assert d.slow_mode is True or d.slow_mode is False


# ---------------------------------------------------------------------------
# 6. Monotonic write (preserve_sticky)
# ---------------------------------------------------------------------------

class TestPreserveStickyWrite:
    def test_slow_marker_persists_and_reads_back(self):
        con, db = _make_session_db()
        try:
            patch_session_fast_lane_marker(
                db, "sess-1", _slow(), preserve_sticky=True
            )
            assert session_slow_mode(db, "sess-1") is True
            # merge discipline: lineage key survives
            assert db.get_session_model_config_value("sess-1", "_branched_from") == (
                "parent-1"
            )
        finally:
            con.close()

    def test_later_fast_turn_does_not_downgrade_sticky_session(self):
        con, db = _make_session_db()
        try:
            patch_session_fast_lane_marker(
                db, "sess-1", _slow(), preserve_sticky=True
            )
            assert session_slow_mode(db, "sess-1") is True
            # A marker-less turn arrives: sticky must survive.
            patch_session_fast_lane_marker(
                db, "sess-1", _fast(), preserve_sticky=True
            )
            assert session_slow_mode(db, "sess-1") is True
        finally:
            con.close()

    def test_preserve_sticky_false_keeps_legacy_overwrite(self):
        """Default behaviour stays 'write exactly what was decided'."""
        con, db = _make_session_db()
        try:
            patch_session_fast_lane_marker(db, "sess-1", _slow())
            assert session_slow_mode(db, "sess-1") is True
            patch_session_fast_lane_marker(db, "sess-1", _fast())
            assert session_slow_mode(db, "sess-1") is False
        finally:
            con.close()

    def test_fast_marker_on_clean_session_still_written(self):
        con, db = _make_session_db()
        try:
            patch_session_fast_lane_marker(
                db, "sess-1", _fast(), preserve_sticky=True
            )
            assert session_slow_mode(db, "sess-1") is False
        finally:
            con.close()


# ---------------------------------------------------------------------------
# 7. AsyncSessionDB regression (messenger path used to silently drop writes)
# ---------------------------------------------------------------------------

class TestAsyncDoorMarkerWrite:
    def test_write_through_async_door_lands(self):
        con, db = _make_session_db()
        try:
            patch_session_fast_lane_marker(
                _AsyncDoor(db), "sess-1", _slow(), preserve_sticky=True
            )
            # Read back through the SYNC core: the write must be on disk.
            assert db.get_session_model_config_value("sess-1", "slow_mode") is True
            assert (
                db.get_session_model_config_value("sess-1", "slow_mode_source")
                == "tg-prefix"
            )
        finally:
            con.close()

    def test_degraded_marker_through_async_door_lands(self):
        from agent.fast_lane import patch_session_fast_lane_degraded

        con, db = _make_session_db()
        try:
            patch_session_fast_lane_degraded(_AsyncDoor(db), "sess-1")
            assert (
                db.get_session_model_config_value("sess-1", "fast_lane_degraded")
                is True
            )
        finally:
            con.close()

    def test_no_unawaited_coroutine_warning(self):
        """The old bug produced 'coroutine was never awaited' RuntimeWarnings."""
        import warnings

        con, db = _make_session_db()
        try:
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                patch_session_fast_lane_marker(
                    _AsyncDoor(db), "sess-1", _slow(), preserve_sticky=True
                )
            messages = [str(w.message) for w in caught]
            assert not any("never awaited" in m for m in messages), messages
        finally:
            con.close()

    def test_write_degrades_silently_on_broken_db(self):
        patch_session_fast_lane_marker(None, "s", _slow())          # no db
        patch_session_fast_lane_marker(object(), "s", _slow())      # no method
        patch_session_fast_lane_marker(object(), None, _slow())     # no sid

        class Boom:
            def patch_session_model_config(self, *a, **k):
                raise RuntimeError("locked")

        patch_session_fast_lane_marker(Boom(), "s", _slow(), preserve_sticky=True)


# ---------------------------------------------------------------------------
# 8. End-to-end sticky scenario on one session (the owner-visible behaviour)
# ---------------------------------------------------------------------------

class TestStickySessionLifecycle:
    def test_five_turn_scenario(self):
        """Turn 1 «подумай» -> slow; turns 2-4 marker-less -> still slow."""
        con, db = _make_session_db()
        try:
            prompts = [
                "подумай: разбери архитектуру",
                "а теперь сколько будет 2+2?",
                "спасибо",
                "и ещё один короткий вопрос",
            ]
            decisions = []
            for prompt in prompts:
                sticky = session_slow_mode(db, "sess-1")
                d = resolve_fast_path(
                    decide_slow_mode(prompt, session_slow=sticky), prompt
                )
                patch_session_fast_lane_marker(
                    db, "sess-1", d, preserve_sticky=True
                )
                decisions.append(d)

            assert decisions[0].slow_mode is True
            assert decisions[0].source == "tg-prefix"
            for d in decisions[1:]:
                assert d.slow_mode is True
                assert d.source == "session-sticky"
                assert route_profile(d) is None  # stays on 01
            # The marker is still True at the end of the session.
            assert session_slow_mode(db, "sess-1") is True
        finally:
            con.close()

    def test_clean_sibling_session_is_unaffected(self):
        """Sticky must not leak between sessions (scenario 4)."""
        con, db = _make_session_db()
        try:
            d = resolve_fast_path(
                decide_slow_mode("подумай: разбери"), "подумай: разбери"
            )
            patch_session_fast_lane_marker(
                db, "sess-1", d, preserve_sticky=True
            )
            # Second session row, never slowed.
            con.execute(
                "INSERT INTO sessions (id, model_config) VALUES ('sess-2', '{}')"
            )
            con.commit()

            assert session_slow_mode(db, "sess-1") is True
            assert session_slow_mode(db, "sess-2") is None

            clean = resolve_fast_path(
                decide_slow_mode(
                    "сколько будет 2+2?",
                    session_slow=session_slow_mode(db, "sess-2"),
                ),
                "сколько будет 2+2?",
            )
            assert clean.slow_mode is False
            assert route_profile(clean) == "11-fast-answers"
        finally:
            con.close()

    def test_giant_task_phrase_makes_session_sticky(self):
        con, db = _make_session_db()
        try:
            d = resolve_fast_path(
                decide_slow_mode("гигантская задача: спланируй миграцию"),
                "гигантская задача: спланируй миграцию",
            )
            assert d.source == "phrase"
            patch_session_fast_lane_marker(db, "sess-1", d, preserve_sticky=True)
            follow = resolve_fast_path(
                decide_slow_mode(
                    "продолжай", session_slow=session_slow_mode(db, "sess-1")
                ),
                "продолжай",
            )
            assert follow.slow_mode is True
            assert follow.source == "session-sticky"
        finally:
            con.close()


# ---------------------------------------------------------------------------
# 7. Read-only file probe (api_server cold-start path, t_ca558566)
# ---------------------------------------------------------------------------
#
# ``session_slow_mode_from_db_file`` exists so the api_server request handler
# can read the marker before ``_create_agent`` WITHOUT constructing a writable
# SessionDB (whose __init__ runs schema init and took ~3 s cold on a fresh
# HERMES_HOME). Same no-opinion contract as the DB-backed reader.

class TestSessionSlowModeFromDbFile:
    def _db_file(self, *, marker="unset", existing=True, create_file=True):
        import tempfile
        from pathlib import Path

        path = Path(tempfile.mkdtemp(prefix="sticky-file-")) / "state.db"
        if not create_file:
            return path
        con = sqlite3.connect(str(path))
        con.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, model_config TEXT)")
        if existing:
            cfg = {"_branched_from": "p1"}
            if marker != "unset":
                cfg["slow_mode"] = marker
            con.execute(
                "INSERT INTO sessions (id, model_config) VALUES ('sess-1', ?)",
                (json.dumps(cfg),),
            )
        con.commit()
        con.close()
        return path

    def test_true_marker_reads_back_true(self):
        path = self._db_file(marker=True)
        assert session_slow_mode_from_db_file(path, "sess-1") is True

    def test_false_marker_reads_back_false(self):
        path = self._db_file(marker=False)
        assert session_slow_mode_from_db_file(path, "sess-1") is False

    def test_missing_key_and_missing_row_are_none(self):
        assert session_slow_mode_from_db_file(self._db_file(), "sess-1") is None
        assert session_slow_mode_from_db_file(self._db_file(marker=True), "gone") is None

    def test_missing_file_is_none(self):
        assert session_slow_mode_from_db_file(self._db_file(create_file=False), "sess-1") is None

    def test_no_args_are_none(self):
        assert session_slow_mode_from_db_file(None, "sess-1") is None
        assert session_slow_mode_from_db_file(self._db_file(marker=True), None) is None

    def test_unrecognized_value_is_no_opinion(self):
        assert session_slow_mode_from_db_file(self._db_file(marker="garbage"), "sess-1") is None

    def test_garbage_file_degrades_and_logs(self, caplog):
        import tempfile
        from pathlib import Path

        path = Path(tempfile.mkdtemp(prefix="sticky-bad-")) / "state.db"
        path.write_bytes(b"not a database at all")
        with caplog.at_level(logging.WARNING):
            assert session_slow_mode_from_db_file(path, "sess-1") is None
        assert any(r.levelno >= logging.WARNING for r in caplog.records)

    def test_file_without_sessions_table_is_benign_and_silent(self, caplog):
        """An existing state.db with no ``sessions`` table = no opinion, no log.

        This is the state of a fresh HERMES_HOME touched by any sqlite opener
        before ``SessionDB`` schema init, and the api_server probe runs on
        EVERY ``/v1/runs`` — one WARNING+traceback per request would flood the
        hot path for a condition that resolves itself on the first marker turn
        (t_ca558566). The docstring already lists "a missing table" as a
        no-opinion path, so the reader must behave like the missing-file case.
        """
        import tempfile
        from pathlib import Path

        path = Path(tempfile.mkdtemp(prefix="sticky-empty-")) / "state.db"
        sqlite3.connect(str(path)).close()  # file exists, zero tables
        assert path.exists()
        with caplog.at_level(logging.WARNING):
            assert session_slow_mode_from_db_file(path, "sess-1") is None
        assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []
        # Still read-only: probing must not create the missing table.
        con = sqlite3.connect(str(path))
        try:
            tables = [
                r[0]
                for r in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            ]
        finally:
            con.close()
        assert tables == []

    def test_agrees_with_the_db_backed_reader(self):
        """Both readers must agree or the two seams route the same session differently."""
        path = self._db_file(marker=True)
        con, db = _make_session_db(with_marker=True)
        try:
            assert session_slow_mode_from_db_file(path, "sess-1") is session_slow_mode(
                db, "sess-1"
            ) is True
        finally:
            con.close()

    def test_probe_does_not_write(self):
        """Read-only means read-only: the file's bytes must not change."""
        path = self._db_file(marker=True)
        before = path.read_bytes()
        session_slow_mode_from_db_file(path, "sess-1")
        assert path.read_bytes() == before
