# F2 gateway VBS-supervisor exit-code contract — deployment artifacts

Follow-up F2 of the fast-gateway-restart mission (t_60a7f508 / this card
t_86cfebc9).  The production launch chain on this host is:

    wscript Hermes_Gateway_01-orchestrator.vbs
        -> cmd.exe /c safe_gateway_start.cmd
        -> python -m hermes_cli.main --profile 01-orchestrator gateway run

## The defect (from the 48 h observation window)

1. **VBS never observed the child exit code.**  `sh.Run(..., 0, False)` uses
   `bWaitOnReturn=False`, so when the gateway exits 75 (EX_TEMPFAIL → service
   manager restart contract) the VBS parent cannot see it.  The window logged
   4 external kill-restarts with no exit-75 semantics.

2. **`safe_gateway_start.cmd` unconditionally launches `gateway run`** even
   when port 8642 is already bound and healthy — a fast second launch can
   spawn a twin gateway that fights the live one.

## The fix contract

The decision core is versioned and unit-tested at
`scripts/gateway_vbs_supervisor.py` (+ `tests/scripts/test_gateway_vbs_supervisor.py`):

- `decide_restart(exit_code, restarts_so_far, max_restarts)` — bounded exit-75
  policy; a restart fires ONLY on 75 with budget headroom; 0 / permanent /
  budget-exhausted → stop (restart loop is protected).
- `supervisor_run_plan(exit_sequence, max_restarts)` — end-to-end policy walk.
- `gateway_port_guard(port, port_bound, health_ok)` — refuse to start a
  duplicate when the port is bound AND healthy; warn otherwise.

The thin deployment wrappers below translate the policy into the actual
Windows launcher chain.  Their **versioned copies** live in this directory
under `launchers/`; they are deployed into
`<profile>/gateway-service/` by a later, explicitly-delegated deployment task
(this card does NOT touch the live gateway — see "Deployment / rollback").

### `launchers/Hermes_Gateway_01-orchestrator.vbs` (deploy as
`<profile>/gateway-service/Hermes_Gateway_01-orchestrator.vbs`)

The only change vs. the shipping VBS is `bWaitOnReturn=True` on the
`Run(...)` of `safe_gateway_start.cmd` — that is what makes the parent observe
the child's exit code; the `.cmd` already propagates it with `exit /b
%errorlevel%`, and the VBS re-propagates with `WScript.Quit sh.ExitCode`.

### `launchers/safe_gateway_start.cmd` — duplicate-start guard

A port-guard preflight runs before the final `gateway run`.  It probes the
loopback health endpoint (`/health`, 200) plus `local_port_in_use(8642)`, so a
launch when a healthy gateway is already on 8642 refuses instead of spawning a
twin.  The guard's exit code semantics (versioned CLI in
`gateway_vbs_supervisor.py --guard-port`):

| Condition                                    | exit code | .cmd action                      |
|----------------------------------------------|-----------|----------------------------------|
| port free                                    | 0         | continue to `gateway run`        |
| port bound + health 200 (live twin risk)     | 3         | `if errorlevel 1 exit /b 3` halts |
| port bound + health dead (starting/stale)    | 0 (warn)  | continue (bounded cooldown is the caller's job) |
| probe error (defensive)                      | 0         | never hard-block the launcher    |

Note: the batch test is `if errorlevel 1 exit /b %errorlevel%` — NOT
`if not errorlevel 0`, which is always false in cmd and would never halt.

### The rest of the restart decision is the supervisor's job

`bWaitOnReturn=True` + `--guard-port` are only half the contract.  Exit code 75
("restart me") still has to be turned into a *bounded* respawn.  On this host
the genuine supervisor is Task Scheduler `GatewaySupervisor` →
`scripts/gateway_supervisor.py` (pythonw, rate-limited 2/h), which launches the
chain detached via `gateway_restart.py`.  The bounded exit-75 policy is
encoded and unit-tested here (`decide_restart` / `supervisor_run_plan`) so that
any supervisor loop that observes the child's exit code (including a future
resident VBS/cmd loop) can apply it without unbounded restart churn.

## Smoke: "port busy" does not spawn duplicates

Two layers:

1. **Unit** (`tests/scripts/test_gateway_vbs_supervisor.py`,
   `test_port_guard_*`): the guard refuses a healthy bound port and stays
   silent on a free port — i.e. the decision that prevents a twin.
2. **Live CLI** (`gateway_vbs_supervisor.py --guard-port 8642`): against the
   REAL gateway currently on 8642 it prints the conflict and exits 3.  A
   command-launcher wired with `if errorlevel 1 exit /b %errorlevel%` therefore
   halts before `gateway run`, so no duplicate process is created.  Run:

   ```
   python scripts\gateway_vbs_supervisor.py --guard-port 8642
   echo %errorlevel%        :: 3 when a healthy gateway is on 8642 (as now)
   python scripts\gateway_vbs_supervisor.py --guard-port 18642
   echo %errorlevel%        :: 0 on a free port → safe to start
   ```

## Deployment / rollback

- Deploying to the live profile is **out of scope for this card**: the task
  forbids touching the live gateway (PID 24792 / port 8642 stays up until a
  deliberately-delegated deployment replaces it).  The `launchers/` files here
  are the versioned references that deployment installs.
- Rollback: keep the pre-F2 `Hermes_Gateway_01-orchestrator.vbs.pre-safe-*`,
  `safe_gateway_start.cmd.bak-*` snapshots already present in the profile's
  `gateway-service/` directory, and restore them on regression.
- `gateway_vbs_supervisor.py --guard-port` mode is a thin CLI shim used by the
  .cmd; the policy is identical to the Python-imported functions (unit-tested).
