Option Explicit
' Hermes Agent Gateway launcher - production profile 01-orchestrator (F2).
' F2 (fast-gateway-restart, t_86cfebc9): bWaitOnReturn=True on the Run(...)
' of safe_gateway_start.cmd is what makes this parent observe the gateway's
' exit code, so the supervisor can honour the EX_TEMPFAIL (75) service-manager
' restart contract.  The gateway's exit code is then propagated verbatim via
' WScript.Quit sh.ExitCode (the .cmd already does `exit /b <childcode>`).
'
' NOTE on the exit-75 contract: exit code 75 = "service manager, restart me"
' (EX_TEMPFAIL, see gateway/restart.py GATEWAY_SERVICE_RESTART_EXIT_CODE).
' A caller that invokes this launcher synchronously and observes ITS exit code
' sees 75 and must restart (bounded); 0 = clean exit (do not restart); any
' other code = permanent failure (do not restart).  The bounded policy is
' versioned and unit-tested in scripts/gateway_vbs_supervisor.py
' (decide_restart / supervisor_run_plan).
Dim sh
Set sh = CreateObject("WScript.Shell")
sh.Run "cmd.exe /c """ & sh.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\hermes\profiles\01-orchestrator\gateway-service\safe_gateway_start.cmd""", 0, True
WScript.Quit sh.ExitCode
