@echo off
setlocal
set "BASE=%LOCALAPPDATA%\hermes"
set "HERMES_HOME=%BASE%\profiles\01-orchestrator"
set "HERMES_GATEWAY_DETACHED=1"
set "PYTHONIOENCODING=utf-8"
set "PYTHONPATH=%BASE%\hermes-agent;%PYTHONPATH%"
rem --- 2026-10-04: sanitize stale kanban worker env (prevents KANBAN_BINDING
rem --- false rejections when the launcher chain inherits a worker session env) ---
set "HERMES_KANBAN_TASK="
set "HERMES_KANBAN_RUN_ID="
set "HERMES_KANBAN_WORKSPACE="
set "HERMES_KANBAN_CLAIM_LOCK="

rem --- F2 (fast-gateway-restart, t_86cfebc9): duplicate-start guard ----------
rem Refuse to start a SECOND gateway when a live one is already on port 8642
rem (bound + healthy).  The versioned guard returns exit 0 when safe to start,
rem and a nonzero (3) when starting would spawn a twin that fights the live
rem gateway - so a lightning restart can never pile up duplicates.
"%BASE%\hermes-agent\venv\Scripts\python.exe" "%BASE%\hermes-agent\scripts\gateway_vbs_supervisor.py" --guard-port 8642
if errorlevel 1 exit /b %errorlevel%

"%BASE%\hermes-agent\venv\Scripts\python.exe" "%BASE%\scripts\gateway_preflight.py" --repo "%BASE%\hermes-agent" --profile-home "%HERMES_HOME%"
if errorlevel 1 exit /b %errorlevel%
cd /d "%HERMES_HOME%"
"%BASE%\hermes-agent\venv\Scripts\python.exe" -m hermes_cli.main --profile 01-orchestrator gateway run
exit /b %errorlevel%
