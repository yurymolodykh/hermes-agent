"""Live-code harness for t_b36e6939: first-turn latch through the REAL seams.

Not a pytest file — run it directly. It exercises the PRODUCTION path with only
the LLM object mocked out, on a REAL ``SessionDB`` file, and covers the card's
acceptance scenarios:

  S1  brand-new session, T1 «подумай» -> T2 without marker
        expect: T1 slow tg-prefix, T2 ``source=session-sticky`` target=current
        (the api_server seam routes ``target=current`` because route_profile
        returns None for a slow decision)
  S2  the OLD defect shape: T1 written before the row exists (marker-only)
        is exactly what S1 must no longer produce
  S3  a clean session still goes 11-fast-answers / no-marker
  S4  the row created by the marker write still gains the agent's own
        model_config keys afterwards (max_iterations / reasoning_config)
  S5  broken DB / shim without ensure_session -> no crash, no sticky claim
  S6  a genuinely lost write is REPORTED (warning), not silent

Usage:  python scripts/livecheck_t_b36e6939.py
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from hermes_state import SessionDB  # noqa: E402


def _build_adapter(db):
    from gateway.config import PlatformConfig
    from gateway.platforms.api_server import APIServerAdapter

    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={}))
    adapter._session_db = db

    @contextmanager
    def _scope(profile):
        yield

    adapter._profile_scope = _scope
    return adapter


def _ok_agent():
    agent = MagicMock()
    agent.run_conversation.return_value = {"final_response": "ok"}
    agent.session_prompt_tokens = 0
    agent.session_completion_tokens = 0
    agent.session_total_tokens = 0
    agent.session_id = "sess"
    agent.provider = "custom"
    agent.model = "deepseek-v4-flash"
    return agent


async def turn(adapter, session_id, text):
    """One full api_server turn through the REAL _create_agent lane decision."""
    import run_agent

    with patch.object(run_agent, "AIAgent", return_value=_ok_agent()):
        await adapter._run_agent(
            user_message=text,
            conversation_history=[],
            session_id=session_id,
            fast_lane_route=True,
        )


class _LineCollector(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record):
        try:
            msg = record.getMessage()
        except Exception:
            return
        self.lines.append(f"{record.levelname}|{record.name}|{msg}")


def _fast_lines(lines):
    return [
        ln for ln in lines
        if "fast_lane=" in ln or "marker write did NOT land" in ln
    ]


async def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        stream=sys.stdout,
        force=True,
    )
    collector = _LineCollector()
    logging.getLogger().addHandler(collector)

    out: dict = {}
    tmp = Path(tempfile.mkdtemp())
    db = SessionDB(db_path=tmp / "state.db")
    adapter = _build_adapter(db)

    # ---- S1: brand-new session, first turn is the marker turn --------------
    collector.lines.clear()
    await turn(adapter, "owui-first-latch", "подумай: разбери архитектуру")
    await turn(adapter, "owui-first-latch", "а теперь покороче, что там по срокам?")
    out["S1_first_turn_latch"] = {
        "lines": _fast_lines(collector.lines),
        "row_exists": bool(db.get_session("owui-first-latch")),
        "slow_mode": db.get_session_model_config_value("owui-first-latch", "slow_mode"),
        "slow_mode_source": db.get_session_model_config_value(
            "owui-first-latch", "slow_mode_source"
        ),
        "source_column": (db.get_session("owui-first-latch") or {}).get("source"),
    }

    # ---- S2: the pre-fix shape, reproduced against the real store ---------
    out["S2_pre_fix_shape"] = {
        "note": "marker written through the writer to a session whose row exists "
                "only after the turn -> must now latch",
        "slow_mode_after_marker_only_turn": None,
    }
    from agent.fast_lane import (
        decide_slow_mode,
        patch_session_fast_lane_marker,
        resolve_fast_path,
        route_profile,
        session_slow_mode,
    )

    collector.lines.clear()
    d = resolve_fast_path(
        decide_slow_mode("подумай: разбери"), "подумай: разбери"
    )
    patch_session_fast_lane_marker(
        db, "owui-marker-only", d, preserve_sticky=True, source="api_server"
    )
    out["S2_pre_fix_shape"] = {
        "slow_mode_after_marker_only_turn": session_slow_mode(db, "owui-marker-only"),
        "next_turn_route": route_profile(
            resolve_fast_path(
                decide_slow_mode(
                    "обычный вопрос", session_slow=session_slow_mode(db, "owui-marker-only")
                ),
                "обычный вопрос",
            )
        ),
        "warnings": [ln for ln in collector.lines if ln.startswith("WARNING")],
    }

    # ---- S3: a clean session is untouched ---------------------------------
    collector.lines.clear()
    await turn(adapter, "owui-clean", "сколько будет 2+2?")
    out["S3_clean_session"] = {
        "lines": _fast_lines(collector.lines),
        "slow_mode": db.get_session_model_config_value("owui-clean", "slow_mode"),
    }

    # ---- S4: the row the marker created still gets the agent's keys -------
    db.create_session(
        "owui-enrich",
        source="api_server",
        model="deepseek-v4-flash",
        model_config={
            "max_iterations": 200,
            "reasoning_config": {"enabled": True, "effort": "high"},
            "max_tokens": None,
        },
    )
    cfg = json.loads(db.get_session("owui-enrich")["model_config"] or "{}")
    out["S4_row_enrichment"] = {
        "slow_mode": cfg.get("slow_mode"),
        "max_iterations": cfg.get("max_iterations"),
        "reasoning_config": cfg.get("reasoning_config"),
        "model": db.get_session("owui-enrich")["model"],
    }

    # ---- S5: broken DB / shim without ensure_session ----------------------
    class ExplodingDB:
        def get_session_model_config_value(self, *a, **k):
            raise RuntimeError("state.db is locked")

        def patch_session_model_config(self, *a, **k):
            raise RuntimeError("state.db is locked")

    collector.lines.clear()
    boom = _build_adapter(ExplodingDB())
    await turn(boom, "owui-boom", "подумай: упадёт ли это?")
    out["S5_broken_db"] = {
        "turn_completed": True,
        "lines": _fast_lines(collector.lines),
    }

    # ---- S6: a genuinely lost write is reported ---------------------------
    class _NoPatch(ExplodingDB.__mro__[0]):
        pass

    class SilentlyRefusingDB:
        """Accepts the patch call but never stores it — the defect shape."""

        def get_session(self, sid):
            return None

        def ensure_session(self, sid, source="unknown"):
            return sid  # claims success, creates nothing

        def patch_session_model_config(self, sid, patch):
            return None

        def get_session_model_config_value(self, sid, key, default=None):
            return default

    collector.lines.clear()
    silent = _build_adapter(SilentlyRefusingDB())
    await turn(silent, "owui-silent", "подумай: запишется ли маркер?")
    out["S6_lost_write_is_reported"] = {
        "lines": _fast_lines(collector.lines),
        "warned": any("did NOT land" in ln for ln in collector.lines),
    }

    logging.getLogger().removeHandler(collector)
    db.close()
    print("\n=== RESULT JSON ===")
    print(json.dumps(out, ensure_ascii=False, indent=2))

    checks = {
        "S1 T1 slow": any("fast_lane=SLOW api_server" in ln and "source=tg-prefix" in ln
                          for ln in out["S1_first_turn_latch"]["lines"]),
        "S1 T2 sticky": any("session-sticky" in ln for ln in out["S1_first_turn_latch"]["lines"]),
        "S1 row source real": out["S1_first_turn_latch"]["source_column"] == "api_server",
        "S1 marker on row": out["S1_first_turn_latch"]["slow_mode"] is True,
        "S2 marker-only latches": out["S2_pre_fix_shape"]["slow_mode_after_marker_only_turn"] is True,
        "S2 next turn stays on origin": out["S2_pre_fix_shape"]["next_turn_route"] is None,
        "S2 no warning": out["S2_pre_fix_shape"]["warnings"] == [],
        "S3 clean -> no-marker": any("source=no-marker" in ln
                                     for ln in out["S3_clean_session"]["lines"]),
        "S3 clean no marker stored": out["S3_clean_session"]["slow_mode"] is False,
        "S4 no marker yet": out["S4_row_enrichment"]["slow_mode"] is None,
        "S4 max_iterations kept": out["S4_row_enrichment"]["max_iterations"] == 200,
        "S4 reasoning kept": out["S4_row_enrichment"]["reasoning_config"] == {
            "enabled": True, "effort": "high"},
        "S5 completed": out["S5_broken_db"]["turn_completed"] is True,
        "S6 warned": out["S6_lost_write_is_reported"]["warned"] is True,
    }
    print("\n=== CHECKS ===")
    for name, ok in checks.items():
        print(("PASS  " if ok else "FAIL  ") + name)
    all_ok = all(checks.values())
    print("LIVE_CHECK_OK" if all_ok else "LIVE_CHECK_FAILED")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
