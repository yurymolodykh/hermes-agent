"""Rule 4 gap (t_637df501): [needs-decision] must reach the author's Telegram
EVEN while the OWUI outbox stage is ``pending`` (the production reality for an
open blocked / triage card — the outbox row awaits a server-owned readback that
often lags or never ACKs an open card).

Reproduction: before the fix, ``handle_event`` returned ``action=rewind`` when
``owui_delivered=False`` (stage_state != "acknowledged") — and that early
return happened BEFORE the Telegram lane and before the wake.  So an author
whose OWUI event sat in ``pending`` NEVER got the rule-4 phone ping, violating
the mandate «[needs-decision] всегда в обе лейны, немедленно» and «до карточка
в triage часами без уведомления недопустима».

The fix decouples the complementary lanes: a ``pending`` OWUI stage keeps the
OWUI cursor pinning semantics (the notifier's rewind/retry budget) but must
NOT starve the TG lane or the wake.  Same single classification authority
(``classify_event``) and same dual-lane table — only the early-return gate is
relaxed.
"""
import os
import json

import pytest

from hermes_cli import kanban_db as kb


OWNER_OWUI = "sess-owui-creator"
OWNER_TG = "tg-owner-100200300"
ALLOWED_TG = [OWNER_TG]


# --- light-weight helpers (mirror the sibling dual-lane test) -------------
from tests.gateway.test_kanban_mission_chat_silence_dual_lane import (
    _dual_adapters,
    _mk,
    _owui_sub,
    _run_one_notifier_tick,
    adapter_sent_texts,
)


def _pending_sink(monkeypatch, staged):
    """Stage returning ``("pending", id)`` — the outbox row is written but not
    yet ACK'd by the server (realistic for an open blocked/triage card)."""
    from gateway import kanban_origin_delivery as _kod

    monkeypatch.setenv("HERMES_OPENWEBUI_DB_PATH", "test-registry")

    def fake_stage(outbox, registry, sub, profile, board, event_id, message):
        staged.append((sub.get("chat_id"), message, event_id))
        return ("pending", event_id)

    monkeypatch.setattr(_kod, "stage_terminal", fake_stage)
    return staged


@pytest.fixture
def mission_env(tmp_path, monkeypatch, isolated_kanban_assignee_registry):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "nd.db"))
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "default")
    monkeypatch.setenv("HERMES_TELEGRAM_DELIVERY_POLICY", "on")
    monkeypatch.setenv(
        "HERMES_OPENWEBUI_TELEGRAM_USERS", json.dumps({OWNER_OWUI: OWNER_TG}),
    )
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", ",".join(ALLOWED_TG))
    monkeypatch.setenv("TELEGRAM_HOME_CHANNEL", OWNER_TG)
    kb.init_db()
    conn = kb.connect(board="default")
    try:
        yield conn
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_needs_decision_reaches_author_tg_while_owui_pending(
    mission_env, monkeypatch,
):
    """Rule 4: a human-action needs-decision on an api_server sub whose OWUI
    outbox stage is ``pending`` (not acked) MUST still reach the author's bound
    Telegram chat and wake — never silently parked behind the OWUI ack gate."""
    child = _mk(mission_env, "нужно решение", assignee="worker")
    _owui_sub(mission_env, child, "owui-child")

    # The human block (blocked + needs_input, provider_route → [needs-decision]).
    with kb.write_txn(mission_env):
        payload = {
            "reason": "Выберите маршрут: letovo или deepseek",
            "kind": "needs_input",
            "provider_route": "letovo/deepseek-v4-flash",
        }
        mission_env.execute(
            "UPDATE tasks SET status='blocked', block_kind='needs_input' "
            "WHERE id=? AND status IN ('running','ready')",
            (child,),
        )
        kb._append_event(mission_env, child, "blocked", payload)

    staged = []
    _pending_sink(monkeypatch, staged)
    owui, tg, runner = _dual_adapters()
    await _run_one_notifier_tick(monkeypatch, runner)

    # OWUI lane: the outbox row is written (pending), cursor may rewind — that
    # is the existing idempotent-ack contract and is unchanged here.
    assert len(staged) == 1, f"OWUI stage must be attempted: {staged}"

    # Rule 4: the TG lane must STILL fire even though OWUI is pending — the
    # phone ping is the «всегда доводить» guarantee, independent of OWUI ack.
    tg_texts = adapter_sent_texts(tg)
    assert len(tg_texts) == 1, (
        f"needs-decision must reach author TG even with OWUI pending: {tg_texts}"
    )
    assert "Требуется решение" in tg_texts[0]
    assert "нужно решение" in tg_texts[0]
    assert "letovo или deepseek" in tg_texts[0]
