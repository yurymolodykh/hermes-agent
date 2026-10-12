# Gateway generation, dead-generation reclaim, and the PID-reuse scenario (F3)

This document describes how the gateway supervisor distinguishes one gateway
"generation" from another, how the kanban dispatcher reclaims cards claimed by
a dead generation, and — the F3 follow-up — how the reclaim behaves when the OS
reuses a dead worker's PID for a brand-new process during a fast shim restart.

It is part of the fast-gateway-restart mission (wave A: commits
`069650ae863` / `0f7d8f52e7c` / `d56f11302f6`; follow-ups F1 `update_job`
resume-token preservation, F2 VBS exit-75, F3 PID-reuse here).

## 1. What a "generation" is

Every claim the kanban dispatcher writes is stamped with the generation of the
process that wrote it (see `current_gateway_generation()` in
`hermes_cli/kanban_db.py`):

    pid:<pid>:start:<create_time:.3f>:boot:<boot_id>

* `pid` — the **dispatcher** process's PID (the gateway process that owns the
  kanban dispatch loop).
* `start` — that process's create time (epoch seconds, ms-quantized), read via
  psutil.
* `boot` — a per-machine boot identifier (`/proc/sys/kernel/random/boot_id` on
  Linux, `MachineGuid` on Windows, `IOPlatformUUID` on macOS; hostname as a
  last resort).

The process also writes its own stamp to a shared marker file,
`<root>/kanban/.gateway_generation`, at startup and on every spawn tick.
`read_gateway_generation()` reads the marker; `current_gateway_generation()`
returns the in-process cached stamp.

### Why the generation disambiguates a restart

When a gateway dies (crash, kill -9, host reboot) its worker children die with
it, but the cards they claimed stay `running` until the `release_stale_claims`
TTL (~4 h) expires. The next gateway instance has a *different* generation
stamp, so a dispatch tick sees those cards as foreign (`stamp != current`) and
reclaims them to `ready` immediately — rather than making the user wait out the
TTL.

The three segments are deliberately redundant so that a restart is never
misread as "same generation":

* A reused **PID** is caught by `start` (the new process has a different create
  time).
* Same PID *and* same start time cannot happen for two distinct processes on
  one host at once — and across a host reboot the **boot** id changes, which
  also covers same-PID-same-start resurrection on a fresh boot.
* The **boot** id alone would be insufficient when the machine does not reboot
  between restarts (the common fast-restart case), which is exactly why `pid`
  and `start` are in the stamp.

## 2. Dead-generation reclaim

`reclaim_dead_generation_running()` (in `hermes_cli/kanban_db.py`) is invoked on
each dispatch tick. It selects `running` cards whose `gateway_generation` is
non-NULL and differs from the current generation. For each:

* If the recorded `worker_pid` is **not alive** → the worker died with its
  gateway → requeue the card to `ready` immediately.
* If the recorded `worker_pid` **is alive** → the worker may be doing real work
  on the *old* gateway's behalf (e.g. it outlived a detach). Reclaiming it and
  spawning a duplicate would be wrong. This is the safety invariant: **a live
  worker is never touched unless we can *prove* the PID now belongs to a
  different process.**

The `worker_pid` liveness check itself is robust — see
`_pid_alive()`: it uses `OpenProcess`/`WaitForSingleObject` on Windows (never
`os.kill(pid, 0)`, which sends `CTRL_C_EVENT` bpo-14484), handles zombie
reaping on POSIX, and downgrades to "dead" only on a confident answer.

## 3. The PID-reuse scenario (F3)

### 3.1 The problem

After a gateway is killed and the shim respawns the gateway quickly, the OS can
reuse the dead worker's PID for the new worker (or for any unrelated process).
`_pid_alive(pid)` alone would then report "alive" for a PID that is no longer
the original worker. Without an extra signal, the reclaim would leave the stale
card `running` **forever** — its generation is dead but its PID "lives", so
neither the reclaim nor the TTL/extend path notices in time.

### 3.2 How the generation is determined and what we compare

The generator stamp's `pid:`/`start:` segments describe the **dispatcher**
process — NOT the worker. A worker identity is a separate fact that the
dispatcher records at spawn time. This split is critical:

* `gateway_generation` = who *claimed* the card (the dispatcher).
* `worker_pid` + `worker_start` = which *process* is doing the work.

The naive approach — compare the live create time of `worker_pid` against the
**generation stamp's** `start:` — is wrong: those describe two different
processes, so the comparison would always "mismatch" and the reclaim would
reclaim cards whose workers are genuinely alive (the duplicate-spawn bug).
The F3 fix records the **worker's own** create time.

### 3.3 The fix

* **At spawn** (`_set_worker_pid`): alongside `worker_pid`, persist
  `worker_start` — the spawned worker process's create time (epoch seconds,
  ms-quantized), best-effort via psutil. A `(pid, create_time)` pair is a
  positive, unforgeable-by-reuse process identity; PID alone is forgeable.
  When the create time is unreadable at spawn, `worker_start` stays NULL.
* **At reclaim** (`reclaim_dead_generation_running`): when `_pid_alive(pid)`
  is true, additionally call `pid_looks_reused(pid, worker_start)`:
  * `worker_start` NULL (legacy rows / unreadable at spawn) → **not reused**
    (conservative). We never reclaim on a guess; keeping a stale card running
    for one more TTL is always safer than spawning a duplicate beside live
    work.
  * live create time unreadable → **not reused** (same conservative reason).
  * live create time differs from `worker_start` beyond a 10 ms tolerance →
    **reused**: the dead worker's PID was taken over by a new process → the old
    generation is definitively gone → requeue.
  * within tolerance → same process, worker genuinely alive → leave untouched.

The guard is deliberately conservative in every unprovable direction: the only
path to reclaiming a live-PID card is a *proven* create-time mismatch.

### 3.4 What a correct reclaim looks like under PID-reuse

```
old worker (pid 4242, start T0)  →  killed with its gateway
OS reuses 4242 for a new process (start T1 ≫ T0)
next dispatch tick:
  gateway_generation != current            → foreign
  _pid_alive(4242)                         → True (reused)
  pid_looks_reused(4242, worker_start=T0)  → True (live T1 != T0)
  → requeue to ready; event reason = dead_gateway_generation,
    no duplicate is spawned because the old worker is provably gone.
```

## 4. Test coverage

`tests/hermes_cli/test_generation_reclaim.py`:

* `test_marker_file_written_and_readable` — the marker file plumbing.
* `test_claim_stamps_current_generation` — claims carry the dispatcher stamp.
* `test_dead_generation_card_reclaimed_ready_on_first_tick` — dead generation +
  dead PID → immediate requeue.
* `test_live_pid_foreign_generation_card_untouched` — **safety invariant**: live
  worker + foreign generation → never reclaimed, no duplicate.
* `test_current_generation_card_untouched` / `test_null_generation_card_untouched`
  — non-foreign and legacy claims stay on their normal paths.
* F3 PID-reuse:
  * `test_pid_reuse_foreign_generation_reclaimed` — live PID but worker_start
    mismatch → requeued.
  * `test_pid_reuse_same_worker_start_untouched` — live PID and matching
    worker_start → untouched.
  * `test_pid_reuse_unknown_worker_start_untouched` — live PID, no recorded
    worker_start → conservative, untouched.
  * `test_pid_reuse_guard_helper` — the `pid_looks_reused` decision table.

The tests run against an in-memory/fixture kanban DB; they need **no live
gateway** and no production state. Run:

```
venv\Scripts\python.exe -m pytest tests/hermes_cli/test_generation_reclaim.py -v
```

## 5. Notes / operational semantics

* `worker_start` is ms-quantized at write; psutil returns float-epoch, so the
  comparison uses a 10 ms tolerance. A reused PID differs by seconds-to-hours,
  so the tolerance is generous without ever mistaking two processes.
* The generation marker, the stamp, and `worker_start` live in the shared kanban
  home — not per-board — because one gateway dispatches every board.
* Fast restart ("/restart fast", `HERMES_GATEWAY_FAST_RESTART`) does not change
  any of this: it is the *reclaim on next boot* that consumes the generation
  signal, and PID-reuse handling is identical whether the restart was fast or
  slow.
