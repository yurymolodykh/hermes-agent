"""Unit tests for slice A3(1): slow_mode marker parsing (design §3).

Locks in the §3 marker contract:

1. OWUI deepthink (api_server ``model_options``): slow iff ``reasoning.
   enabled=true`` AND ``effort`` in {high, xhigh, max, ultra}. Enabled-only
   (or medium/lower) is NOT slow (design §3 orchestrator decision: включённое
   reasoning само по себе не замедляет).
2. TG «подумай» prefix: leading ``подумай``/``думай`` (case-insensitive,
   word boundary; covers «подумай:», «подумай,», «думай …»/«думай: …»).
3. Always-slow phrases: «гигантск…» followed by whitespace and one of
   (план|задача|миссия|проект) — NIGHT-PLAN list.
4. Absence of all markers => fast by default (slow_mode=False).

Also asserts the flag is available for the next stages: the decision object
carries ``slow_mode``/``source``/``reason``, and
``patch_session_fast_lane_marker`` persists it into session ``model_config``
(readable via ``get_session_model_config_value``) while preserving lineage
keys and degrading silently on any failure.
"""

from __future__ import annotations

import json
import sqlite3

from agent.fast_lane import (
    FastLaneDecision,
    classify_complexity,
    decide_slow_mode,
    evaluate_request,
    fast_lane_marker_for_session,
    model_options_mark_slow,
    patch_session_fast_lane_marker,
    resolve_fast_path,
    session_is_fast_lane,
    strip_telegram_slow_prefix,
    telegram_prefix_marks_slow,
    contains_always_slow_phrase,
)


# ---------------------------------------------------------------------------
# Marker 1: OWUI deepthink (model_options)
# ---------------------------------------------------------------------------

def test_owui_deepthink_high_effort_is_slow():
    for effort in ("high", "xhigh", "max", "ultra"):
        d = decide_slow_mode(
            model_options={"reasoning": {"enabled": True, "effort": effort}}
        )
        assert d.slow_mode is True, f"effort={effort} should be slow"
        assert d.source == "api-deepthink"


def test_owui_flat_reasoning_effort_is_slow():
    d = decide_slow_mode(model_options={"reasoning_effort": "high"})
    assert d.slow_mode is True


def test_owui_enabled_only_is_not_slow():
    # §3: enabled=true without high effort must NOT be slow.
    d = decide_slow_mode(model_options={"reasoning": {"enabled": True}})
    assert d.slow_mode is False
    assert d.source == "no-marker"


def test_owui_medium_is_not_slow():
    d = decide_slow_mode(
        model_options={"reasoning": {"enabled": True, "effort": "medium"}}
    )
    assert d.slow_mode is False


def test_owui_disabled_is_not_slow():
    d = decide_slow_mode(
        model_options={"reasoning": {"enabled": False, "effort": "high"}}
    )
    assert d.slow_mode is False


def test_owui_empty_or_missing_options_is_not_slow():
    assert decide_slow_mode(model_options={}).slow_mode is False
    assert decide_slow_mode(model_options=None).slow_mode is False


def test_model_options_mark_slow_primitive():
    assert model_options_mark_slow({"reasoning": {"enabled": True, "effort": "high"}})
    assert not model_options_mark_slow({"reasoning": {"enabled": True}})
    assert not model_options_mark_slow({})


# ---------------------------------------------------------------------------
# Marker 2: TG «подумай» prefix
# ---------------------------------------------------------------------------

TG_CASES = [
    ("подумай", True),
    ("подумай:", True),
    ("подумай,", True),
    ("подумай   поработай", True),
    ("думай", True),
    ("думай:", True),
    ("ДУМАЙ над этим", True),
    ("  подумай о задаче", True),
    ("подумайка над этим", False),     # word boundary
    ("подумай-ка над этим", False),     # word boundary (hyphenated particle)
    ("надо подумай над этим", False),  # not a prefix
    ("просто сообщение", False),
]


def test_tg_prefix_marks_slow():
    for text, want in TG_CASES:
        d = decide_slow_mode(text=text)
        assert d.slow_mode is want, f"{text!r}: got {d.slow_mode}, want {want}"
        assert telegram_prefix_marks_slow(text) is want


def test_tg_prefix_source():
    d = decide_slow_mode(text="подумай: проанализируй")
    assert d.slow_mode is True
    assert d.source == "tg-prefix"


# ---------------------------------------------------------------------------
# Marker 3: always-slow phrases
# ---------------------------------------------------------------------------

def test_always_slow_phrases():
    for text in (
        "гигантский план",
        "ГИГАНТСКАЯ ЗАДАЧА",
        "нужен гигантский план",
        "гигантская миссия",
        "гигантский проект",
    ):
        d = decide_slow_mode(text=text)
        assert d.slow_mode is True, f"{text!r} should be slow"
        assert d.source == "phrase"


def test_non_matching_giant_is_not_slow():
    assert contains_always_slow_phrase("гигант") is False
    assert contains_always_slow_phrase("гигантский шар") is False
    assert decide_slow_mode(text="гигантский шар").slow_mode is False


def test_multimodal_list_content_still_fires_markers():
    """api_server multimodal attachments must not swallow «подумай»/RGZ.

    A message that carries an attachment arrives as a *list* of parts
    (``{type:text, text:...}`` + ``{type:image_url/file,...}``), not a plain
    string. The fast-lane markers must still fire on the text part, otherwise
    «подумай» (prefix) and the RGZ «гигантская задача» phrase (both slow) are
    silently lost on any message with an attached file/image.
    """
    # «подумай» prefix inside a multimodal message with an image part.
    multimodal_podumai = [
        {"type": "text", "text": "подумай: что тут на картинке?"},
        {"type": "image_url", "image_url": {"url": "https://x/i.png"}},
    ]
    assert telegram_prefix_marks_slow(multimodal_podumai) is True
    d = decide_slow_mode(text=multimodal_podumai, model_options={})
    assert d.slow_mode is True, "«подумай» + attachment must be slow, not degrade to fast"
    assert d.source == "tg-prefix"

    # RGZ «гигантская задача» phrase inside a multimodal message with a file.
    multimodal_rgz = [
        {"type": "text", "text": "гигантская задача: спланируй миграцию"},
        {"type": "file", "text": "[документ приложен]", "file": {"id": "f1"}},
    ]
    assert contains_always_slow_phrase(multimodal_rgz) is True
    d2 = decide_slow_mode(text=multimodal_rgz, model_options={})
    assert d2.slow_mode is True, "«гигантская задача» + attachment must be slow"
    assert d2.source == "phrase"

    # Prefix stripping must also survive a multimodal message.
    stripped = strip_telegram_slow_prefix(multimodal_podumai)
    assert stripped == "что тут на картинке?"


# ---------------------------------------------------------------------------
# No markers => fast by default
# ---------------------------------------------------------------------------

def test_no_markers_is_fast():
    d = decide_slow_mode(text="Привет, как дела?", model_options={})
    assert d.slow_mode is False
    assert d.source == "no-marker"
    assert d.mode == "fast"
    assert d.fast_lane is True


def test_none_input_is_fast_and_never_raises():
    d = decide_slow_mode(None, None)
    assert d.slow_mode is False
    # Degradation contract: never raise, always a decision.
    assert isinstance(d, FastLaneDecision)


# ---------------------------------------------------------------------------
# Flag available for the next stages
# ---------------------------------------------------------------------------

def test_decision_exposes_flag_fields():
    d = evaluate_request(
        text="подумай об этом",
        model_options={"reasoning": {"enabled": True, "effort": "high"}},
        config={"slow_phrases": []},
    )
    assert isinstance(d.slow_mode, bool)
    assert d.source in {"tg-prefix", "api-deepthink"}
    assert isinstance(d.reason, str) and d.reason


def test_evaluate_request_alias_matches_decide():
    a = evaluate_request(text="это гигантская задача")
    b = decide_slow_mode(text="это гигантская задача")
    assert a == b


def _make_session_db():
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, model_config TEXT)")
    con.execute(
        "INSERT INTO sessions (id, model_config) VALUES ('sess-1', '{\"a\":1}')"
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


def test_marker_persists_to_session_and_preserves_lineage():
    con, db = _make_session_db()
    try:
        d = decide_slow_mode(text="подумай: сделай анализ")
        patch_session_fast_lane_marker(db, "sess-1", d)
        assert db.get_session_model_config_value("sess-1", "slow_mode") is True
        assert db.get_session_model_config_value("sess-1", "slow_mode_source") == "tg-prefix"
        assert db.get_session_model_config_value("sess-1", "fast_lane") is False
        # merge discipline: lineage key survives
        assert db.get_session_model_config_value("sess-1", "a") == 1

        d2 = decide_slow_mode(text="привет", model_options={})
        patch_session_fast_lane_marker(db, "sess-1", d2)
        assert db.get_session_model_config_value("sess-1", "slow_mode") is False
        assert db.get_session_model_config_value("sess-1", "a") == 1
    finally:
        con.close()


def test_marker_patch_degrades_silently():
    d = decide_slow_mode(text="подумай")
    patch_session_fast_lane_marker(None, "sess-1", d)   # no db
    patch_session_fast_lane_marker(object(), "sess-1", d)  # no method
    patch_session_fast_lane_marker(_make_session_db()[1], None, d)  # no sid


def test_fast_lane_marker_for_session_shape():
    # Slow request (resolved): full-lane fields, NOT review-exempt (D5).
    slow = resolve_fast_path(
        decide_slow_mode(text="гигантская задача: X"), "гигантская задача: X"
    )
    m = fast_lane_marker_for_session(slow)
    assert m == {
        "slow_mode": True,
        "slow_mode_source": slow.source,
        "fast_lane": False,
        "fast_lane_source": slow.source,
        "review_exempt": False,
        "reasoning_effort": "high",
        "fast_lane_complexity": slow.complexity,
    }
    # Fast request (after resolve_fast_path): review-exempt True, fast_lane True.
    f = resolve_fast_path(
        decide_slow_mode(text="сколько будет 2+2?"), "сколько будет 2+2?"
    )
    mf = fast_lane_marker_for_session(f)
    assert mf["fast_lane"] is True
    assert mf["review_exempt"] is True
    assert mf["slow_mode"] is False
    assert mf["reasoning_effort"] == "low"


# ---------------------------------------------------------------------------
# Prefix strip (pure helper; consumed by the fast-path slice)
# ---------------------------------------------------------------------------

def test_strip_telegram_slow_prefix():
    assert strip_telegram_slow_prefix("подумай: разбери это") == "разбери это"
    assert strip_telegram_slow_prefix("подумай, разбери это") == "разбери это"
    assert strip_telegram_slow_prefix("думай над этим") == "над этим"
    assert strip_telegram_slow_prefix("обычное сообщение") == "обычное сообщение"
    assert strip_telegram_slow_prefix("") == ""
    assert strip_telegram_slow_prefix(None) == ""


# ---------------------------------------------------------------------------
# Writer/reader drift guard (t_aca69ccc)
#
# ``patch_session_fast_lane_marker`` (writer) and ``session_slow_mode``
# (reader) MUST agree on the key and on the value round-trip: if the writer
# ever stores what the reader cannot interpret as True, stickiness silently
# dies and every follow-up turn drops back to the fast lane. The exhaustive
# reader matrix lives in test_fast_lane_session_sticky.py; this is the single
# end-to-end guard kept next to the writer's own tests.
# ---------------------------------------------------------------------------

def test_writer_and_reader_agree_on_marker_key_and_roundtrip():
    from agent.fast_lane import session_slow_mode

    con, db = _make_session_db()
    try:
        for decision, expected in (
            (decide_slow_mode(text="подумай: разбери"), True),
            (decide_slow_mode(text="сколько будет 2+2?"), False),
        ):
            patch_session_fast_lane_marker(db, "sess-1", decision)
            # Same key the reader reads — no drift between write and read.
            assert db.get_session_model_config_value("sess-1", "slow_mode") is expected
            assert session_slow_mode(db, "sess-1") is expected
    finally:
        con.close()


def test_reader_degrades_to_none_on_absent_db_session_or_key():
    from agent.fast_lane import session_slow_mode

    assert session_slow_mode(None, "sess-1") is None
    assert session_slow_mode(object(), "sess-1") is None
    con, db = _make_session_db()
    try:
        assert session_slow_mode(db, "sess-1") is None  # row without a marker
        assert session_slow_mode(db, "") is None
    finally:
        con.close()
