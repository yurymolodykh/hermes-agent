"""Checkpoint store pruning rides the gateway housekeeping tick, not the constructor.

Its ``git gc`` repacks the whole store (tens of seconds on a GB store); run at construction it
delayed the control socket, adapters and the code_sha stamp, so the first restart of the day (the
``hermes update`` one) looked hung and failed fleet verification.

It must also run once PER SERVED PROFILE: the store, its ``auto_prune`` flag, ``retention_days``
and the ``.last_prune`` marker are the profile's own. Unscoped, only the launch profile's store
was ever maintained — 09 sat at 154 MB against its own 150 MB cap with no ``.last_prune`` at all.
"""

import gateway.run as gateway_run


class _OneTickStopEvent:
    def __init__(self):
        self.waited = False

    def is_set(self):
        return self.waited

    def wait(self, timeout=None):
        self.waited = True
        return True


def test_gateway_housekeeping_runs_the_checkpoint_prune(monkeypatch):
    import tools.checkpoint_maintenance as cm

    calls = []
    monkeypatch.setattr(cm, "auto_prune_from_config", lambda: calls.append(True) or {"skipped": False})

    gateway_run._start_gateway_housekeeping(_OneTickStopEvent(), interval=0)

    assert calls == [True]


def test_checkpoint_prune_chore_is_profile_scoped(monkeypatch):
    """Under the multiplexer the prune must run for every served profile, not only the launch one.

    The failure this pins: an unscoped chore kept the launch home's store at 1 MB while every
    other profile's store grew unmaintained (measured: profiles/09 at 154 MB > its 150 MB cap,
    ``.last_prune`` absent from all 12 non-launch profiles).
    """
    from types import SimpleNamespace
    import gateway.run_profile_reconcile as reconcile
    import tools.checkpoint_maintenance as cm

    homes = [("aa-launch", "/tmp/home-aa"), ("bb-second", "/tmp/home-bb")]
    runner = SimpleNamespace(config=SimpleNamespace(multiplex_profiles=True))
    monkeypatch.setattr(reconcile, "_for_each_served_profile",
                        lambda run, body: [body(label) for label, _ in homes])
    seen = []
    monkeypatch.setattr(cm, "auto_prune_from_config",
                        lambda: seen.append(True) or {"skipped": False})

    gateway_run._start_gateway_housekeeping(_OneTickStopEvent(), interval=0,
                                           runner=runner)

    # One prune per served profile, not one per tick.
    assert seen == [True, True], seen
