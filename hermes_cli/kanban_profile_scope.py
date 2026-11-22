"""Profile secret scope for machine-flow kanban work (dispatcher ticks, split phase).

The dispatcher tick runs in a gateway worker thread with no per-turn scope, so
under ``gateway.multiplex_profiles`` every credential read fails closed
(``agent.secret_scope.get_secret`` -> ``UnscopedSecretError``). Anything the tick
does that reaches an auxiliary LLM — auto-decompose, the auto-split of a burned
card — must therefore bind the launch profile's own scope first, or the first
call in a cold process dies on a config bug that only looks like a provider
failure until an aux client happens to be cached.

One owner for that context: the gateway watchers and the dispatcher both enter
it, so the two paths cannot drift apart again (that drift is what made the split
phase unusable for the first minutes after every gateway restart).
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Iterator


@contextlib.contextmanager
def default_profile_secret_scope() -> Iterator[None]:
    """Bind the launch profile's secret scope while multiplexing is on.

    The credential source is ``owner_secret_scope(<this home>)``: the launch
    profile keeps the env-only keys it was started with, a served profile reads
    its own files. No-op for single-profile gateways, where unscoped reads are
    the launch profile's own process env.
    """
    from agent.secret_scope import is_multiplex_active, reset_secret_scope, set_secret_scope
    from hermes_constants import get_hermes_home
    from tui_gateway.launch_profile_policy import owner_secret_scope

    if not is_multiplex_active():
        yield
        return
    home = Path(get_hermes_home())
    token = set_secret_scope(owner_secret_scope(home), profile_home=str(home))
    try:
        yield
    finally:
        reset_secret_scope(token)


__all__ = ["default_profile_secret_scope"]
