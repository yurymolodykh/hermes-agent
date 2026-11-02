"""LIVE-code harness: sticky slow mode through the real api_server seam.

Not a pytest file — run it directly. It exercises the PRODUCTION code path
(``APIServerAdapter._run_agent`` -> ``_create_agent`` lane decision ->
``patch_session_fast_lane_marker``) against a REAL ``SessionDB``, with only
the LLM agent construction mocked out (no provider spend). Log lines emitted
here come from the same ``logger.info`` calls the live gateway uses, so they
are the actual evidence for the acceptance scenarios.

Usage:  python scripts/livecheck_t_e0350c47.py
"""
from __future__ import annotations

import asyncio
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
    # Real SessionDB, exposed exactly the way the adapter does in production.
    adapter._session_db = db

    @contextmanager
    def _scope(profile):
        # No profile switching: the temp DB stands in for the origin profile.
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


async def turn(adapter, session_id, text, deepthink=False):
    """One full api_server turn through the REAL _create_agent lane decision.

    Only ``run_agent.AIAgent`` (the LLM object) is replaced — ``_create_agent``,
    the sticky read, the fast-lane decision, the marker write and the profile
    routing all run as in production.
    """
    model_options = {"reasoning": {"enabled": True, "effort": "high"}} if deepthink else None
    import run_agent

    with patch.object(run_agent, "AIAgent", return_value=_ok_agent()):
        await adapter._run_agent(
            user_message=text,
            conversation_history=[],
            session_id=session_id,
            model_options=model_options,
            fast_lane_route=True,
        )


async def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        stream=sys.stdout,
        force=True,
    )
    tmp = Path(tempfile.mkdtemp())
    db = SessionDB(db_path=tmp / "state.db")

    for sid in ("owui-live-a", "owui-live-b"):
        db.create_session(session_id=sid, source="api_server", user_id="owner")

    adapter = _build_adapter(db)

    print("\n=== Scen 1: session A, «подумай» turn (expect SLOW tg-prefix) ===")
    await turn(adapter, "owui-live-a", "подумай: разбери архитектуру")
    print("marker in db:", db.get_session_model_config_value("owui-live-a", "slow_mode"))

    print("\n=== Scen 1b: session A, marker-less turn (expect SLOW session-sticky, NOT 11) ===")
    await turn(adapter, "owui-live-a", "а теперь покороче, что там по срокам?")
    print("marker in db:", db.get_session_model_config_value("owui-live-a", "slow_mode"))

    print("\n=== Scen 2: clean session B, marker-less turn (expect FAST -> 11-fast-answers) ===")
    await turn(adapter, "owui-live-b", "сколько будет 2+2?")

    print("\n=== Scen 3: session B, «гигантская задача» (expect SLOW phrase) ===")
    await turn(adapter, "owui-live-b", "гигантская задача: спланируй миграцию базы")
    print("\n=== Scen 3b: session B marker-less (expect SLOW session-sticky) ===")
    await turn(adapter, "owui-live-b", "ладно, и что первым делом?")

    print("\n=== Scen 4: isolation — new clean session C (expect FAST -> 11) ===")
    db.create_session(session_id="owui-live-c", source="api_server", user_id="owner")
    await turn(adapter, "owui-live-c", "какая сегодня погода?")

    print("\n=== Scen 5: DB unavailable (expect no crash, marker-less -> FAST) ===")
    broken = _build_adapter(None)
    await turn(broken, "owui-live-d", "просто вопрос без маркера")

    print("\n=== Scen 5b: DB raises on read/write (expect no crash) ===")

    class ExplodingDB:
        def get_session_model_config_value(self, *a, **k):
            raise RuntimeError("state.db is locked")

        def patch_session_model_config(self, *a, **k):
            raise RuntimeError("state.db is locked")

    boom = _build_adapter(ExplodingDB())
    await turn(boom, "owui-live-e", "подумай: упадёт ли это?")

    print("\n=== final session markers ===")
    for sid in ("owui-live-a", "owui-live-b", "owui-live-c"):
        print(
            sid,
            "slow_mode=", db.get_session_model_config_value(sid, "slow_mode"),
            "source=", db.get_session_model_config_value(sid, "slow_mode_source"),
        )
    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
