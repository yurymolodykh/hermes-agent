"""Card 1 (t_1884c864): author/owner → Telegram resolution helper.

Spec (AUDIT-PLAN §5, карточка 1):

  ``resolve_author_telegram(task, sub, owui_to_tg) -> Optional[str]``

    1. ``owner = sub.user_id`` (or ``delivery_metadata[owui_user_id_attr]``
       fallback) — the OWNER of the posting channel (verified-origin), NOT
       ``tasks.created_by``/``assignee`` (profile slugs are not comparable to
       TG ids — audit §3.3).
    2. return ``owui_to_tg.get(owner)`` — the single mapping surface, fed
       from delivery_policy.owui_telegram_users() (delivery_policy.py:379),
       injected into the engine by the watcher (kanban_watchers.py:1202-1206).
    3. else None (fail-closed, OWUI-only).

  Additional contract (inherited from the original ``_tg_bound_chat_for`` the
  helper now replaces):
    * F6: ``HERMES_TELEGRAM_DELIVERY_POLICY=off`` → None (OWUI stays OWUI-only).
    * A native-TG subscription resolves to its OWN chat (channel of record),
      never through the owner binding.

  The helper is the SINGLE owner→TG mapping surface: both the legacy
  ``_tg_bound_chat_for`` and the [needs-decision] lane must go through it
  (критерий 4 — без дублей с OWUI-цепочкой).
"""

import pytest

from gateway.delivery_engine import DeliveryEngine, resolve_author_telegram


OWNER = "sess-owui-creator"
TG = "tg-owner-100200300"
MARKER_ONLY = "pwa-marker-owner"


@pytest.fixture
def owui_to_tg():
    return {OWNER: TG, MARKER_ONLY: "tg-marker-777"}


def _mk_sub(**overrides):
    sub = {
        "task_id": "t_x",
        "platform": "api_server",
        "chat_id": "owui-abc",
        "user_id": None,
        "delivery_metadata": None,
    }
    sub.update(overrides)
    return sub


# ---------------------------------------------------------------------------
# Spec cases: resolve_author_telegram
# ---------------------------------------------------------------------------


def test_owner_to_tg(owui_to_tg):
    """Bound owner (sub.user_id) resolves to their TG chat."""
    sub = _mk_sub(user_id=OWNER)
    assert resolve_author_telegram(task=None, sub=sub, owui_to_tg=owui_to_tg) == TG


def test_marker_fallback_when_no_user_id(owui_to_tg):
    """F3: api_server sub WITHOUT user_id but WITH delivery_metadata
    [owui_user_id_attr] (server-side verified-origin marker) → bound TG."""
    from hermes_cli.kanban_db import _OWUI_ATTR_META_KEY
    sub = _mk_sub(user_id="", delivery_metadata={_OWUI_ATTR_META_KEY: MARKER_ONLY})
    assert (
        resolve_author_telegram(task=None, sub=sub, owui_to_tg=owui_to_tg)
        == "tg-marker-777"
    )


def test_policy_off_returns_none(owui_to_tg, monkeypatch):
    """F6: policy off → OWUI stays OWUI-only (fail-closed), even for a bound
    owner — the mirror lane is never opened."""
    monkeypatch.setenv("HERMES_TELEGRAM_DELIVERY_POLICY", "off")
    sub = _mk_sub(user_id=OWNER)
    assert resolve_author_telegram(task=None, sub=sub, owui_to_tg=owui_to_tg) is None


def test_no_owner_returns_none(owui_to_tg):
    """No owner (no user_id, no marker) → None (OWUI-only)."""
    sub = _mk_sub(user_id="", delivery_metadata={})
    assert resolve_author_telegram(task=None, sub=sub, owui_to_tg=owui_to_tg) is None


def test_no_binding_returns_none():
    """Owner present but no binding in owui_to_tg → None (OWUI-only)."""
    sub = _mk_sub(user_id=OWNER)
    assert resolve_author_telegram(task=None, sub=sub, owui_to_tg={}) is None


def test_non_dict_sub_returns_none():
    """Defensive: a non-dict sub → None."""
    assert resolve_author_telegram(task=None, sub=None, owui_to_tg={OWNER: TG}) is None


# ---------------------------------------------------------------------------
# The helper is the SINGLE surface: _tg_bound_chat_for delegates to it
# ---------------------------------------------------------------------------


def test_tg_bound_chat_for_api_server_delegates(owui_to_tg):
    """api_server sub with bound owner → the SAME chat as the standalone
    helper (single implementation, критерий 4)."""
    engine = DeliveryEngine(owui_to_tg=owui_to_tg)
    sub = _mk_sub(user_id=OWNER)
    assert engine._tg_bound_chat_for(sub, "api_server") == TG
    assert (
        engine._tg_bound_chat_for(sub, "api_server")
        == resolve_author_telegram(task=None, sub=sub, owui_to_tg=owui_to_tg)
    )


def test_tg_bound_chat_for_native_tg_is_own_chat():
    """native-TG sub: the channel of record IS the TG lane — its own chat,
    NOT the owner binding (rule «для native-TG подписок TG-лейна == каналу
    постановки»)."""
    engine = DeliveryEngine(owui_to_tg={OWNER: TG})
    sub = _mk_sub(user_id="any", chat_id="tg-native-555")
    sub["platform"] = "telegram"
    assert engine._tg_bound_chat_for(sub, "telegram") == "tg-native-555"


def test_tg_bound_chat_for_non_api_platform_is_none():
    """Platform other than api_server/telegram → no TG lane."""
    engine = DeliveryEngine(owui_to_tg={OWNER: TG})
    sub = _mk_sub(user_id=OWNER, platform="slack")
    assert engine._tg_bound_chat_for(sub, "slack") is None
