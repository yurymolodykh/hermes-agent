# Rollback: fast-restart feature (RGZ wave A)

Rolls back the fast-restart feature to the pre-feature behavior. Two parts of the
feature were shipped as commits on branch `release/t_b0291572-owui-origin-binding`:

| id | commit | title |
|----|--------|-------|
| A1 | `069650ae863aee1b764a5f05bcc53bb6bb3dea7b` | `feat(gateway): fast restart path` |
| A2 | `0f7d8f52e7c583b08abcd1be23f45d31a74b0e33` | `feat(cron+kanban): resume tokens + generation-scoped claims` |

A2 is a direct child of A1; the pre-feature tip (A1's parent) is
`41e15c24f5fe7036c0fe2e3ce20722ea9cee99ba`. Reverting child-first (A2 → A1)
lands each inverse on the exact tree it expects, so the reverts apply cleanly.

An executable companion `scripts/rollback_fast_restart.py` ships alongside this
file (`--check` = read-only state+plan, `--dry-run`, `--apply`,
`--apply --restart-gateway`). Run it FIRST to confirm the current repo state.

---

## 0. Preconditions on the live tree

`git revert` refuses to run (or conflicts) when the files it rewrites carry
uncommitted edits. On the mission branch the working tree additionally holds
**other cards' uncommitted work** — never discard it. Stash only the files the
revert touches:

```
cd C:/Users/hermes/AppData/Local/hermes/hermes-agent

git stash push -m rollback-wip -- \
  cron/jobs.py cron/scheduler.py cron/scheduler_provider.py \
  gateway/restart.py gateway/run.py gateway/slash_commands.py \
  hermes_cli/gateway.py hermes_cli/kanban_db.py hermes_cli/subcommands/gateway.py \
  locales/en.yaml locales/ru.yaml \
  tests/cron/test_resume_tokens.py tests/cron/test_shutdown_interrupt.py \
  tests/gateway/test_fast_restart.py tests/gateway/test_restart_drain.py \
  tests/gateway/test_restart_service_detection.py \
  tests/hermes_cli/test_generation_reclaim.py tests/tools/test_process_registry.py
```

(If the tree is clean for those files, skip this step.)

## 1. Revert the commits (child first)

```
git revert --no-edit 0f7d8f52e7c583b08abcd1be23f45d31a74b0e33   # A2
git revert --no-edit 069650ae863aee1b764a5f05bcc53bb6bb3dea7b   # A1
git stash pop                                                    # if you stashed in step 0
```

After this, restore old-behavior /restart semantics. A fast restart logged
`FAST_RESTART {ts, reason, active_agents, active_cron, active_api_runs,
downtime_target}` at stop and `FAST_RESTART_STATS {total_downtime,
resumed_cron, reclaimed_cards, ...}` at the next boot. Post-revert, a normal
`/restart` waits for active turns (bounded by `restart_after_turn_timeout`)
and **no** `FAST_RESTART` marker appears in the log.

## 2. Restore pre-feature config values

```
hermes -p 01-orchestrator config set agent.restart_after_turn_timeout 1800
hermes -p 01-orchestrator config set agent.cron_drain_timeout 30
```

## 3. Restart the gateway by the NORMAL path

The normal restart path is the plain in-band restart — **not** `--fast`:

```
hermes -p 01-orchestrator gateway restart
```

If the gateway is fully stopped, start it normally:

```
hermes -p 01-orchestrator gateway start
```

## 4. Verify old behavior

```
# --fast flag must be gone from the CLI
hermes -p 01-orchestrator gateway restart --help

# no FAST_RESTART linkage left in the restart module
grep -n FAST_RESTART gateway/restart.py        # expect: no output

# config values restored
hermes -p 01-orchestrator config get agent.restart_after_turn_timeout   # -> 1800
hermes -p 01-orchestrator config get agent.cron_drain_timeout            # -> 30

# the new boot's gateway log carries no FAST_RESTART json marker
grep FAST_RESTART %LOCALAPPDATA%/hermes/profiles/01-orchestrator/logs/gateway.log
```

Behavioral check: issue `/restart` in any chat wired to the profile — the gateway
now waits for active agent turns to finish before draining (old
`restart_after_turn_timeout` path) instead of force-draining and pre-marking
sessions `resume_pending`.

## Notes

- No production gateway is restarted by any of the files in this task; step 3 is
  a deliberate operator action. Run it at a chosen moment, not from automation.
- `git revert` creates new revert commits; the two original commits remain in
  history (reversible forward). `git revert --no-edit <A2> <A1>` in one command
  also works but ordering then matters — the two-step child-first sequence is
  preferred and is what the companion script runs.
