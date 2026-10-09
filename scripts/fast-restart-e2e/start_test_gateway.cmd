@echo off
REM ============================================================================
REM start_test_gateway.cmd — start the gateway with the ISOLATED fast-restart
REM e2e test profile (api_server only, port 18642).
REM
REM Modeled on 01-orchestrator/gateway-service/safe_gateway_start.cmd and the
REM local scripts/gateway_preflight.py reference.  Every start materializes a
REM FRESH temporary HERMES_HOME — production HERMES_HOME is never touched and
REM the production gateway port 8642 is never bound by this profile.
REM
REM SAFETY (why this wrapper looks the way it does):
REM   * HERMES_HOME / HERMES_KANBAN_HOME / HERMES_PROFILE and all kanban task
REM     vars are EXPLICITLY cleared first.  `setlocal` alone is NOT enough —
REM     it preserves inherited environment, and a stale HERMES_HOME pointing
REM     at a production profile would launch the REAL gateway.  We must fail
REM     loudly rather than fall back to anything inherited.
REM   * The materialized path is captured via a temp FILE (not `for /f` with
REM     nested quotes, which is brittle) and then VALIDATED to match
REM     <temp>\hermes-e2e\...\profiles\test-fast-restart before it is adopted.
REM   * Any TELEGRAM_* / YADISK_* credential vars inherited from the shell are
REM     stripped from the gateway environment, so real credentials can never
REM     reach the test gateway (mirrors live_boot_proof.py).
REM
REM Exit codes: preflight failure -> 20 (gateway_preflight.py contract);
REM             1 if the profile could not be materialized or validated.
REM ============================================================================
setlocal

REM --- clear ALL inherited env that a real gateway would trust -----------------
set "HERMES_HOME="
set "HERMES_KANBAN_HOME="
set "HERMES_PROFILE="
set "HERMES_KANBAN_TASK="
set "HERMES_KANBAN_RUN_ID="
set "HERMES_KANBAN_WORKSPACE="
set "HERMES_KANBAN_CLAIM_LOCK="
set "HERMES_SESSION="
set "HERMES_SESSION_ID="

REM --- strip any real messaging credentials inherited from the shell -----------
for %%V in (TELEGRAM_BOT_TOKEN TELEGRAM_API_ID TELEGRAM_API_HASH TELEGRAM_ALLOWED_USERS TELEGRAM_CHAT_ID YADISK_TOKEN YADISK_CLIENT_ID YADISK_CLIENT_SECRET YADISK_OAUTH) do set "%%V="

set "BASE=%LOCALAPPDATA%\hermes"
set "REPO=%BASE%\hermes-agent"
set "PYTHON=%REPO%\venv\Scripts\python.exe"
set "PPT=%REPO%\scripts\fast-restart-e2e"

REM --- materialize a fresh isolated profile, capture path via temp file -------
set "HOMEFILE=%TEMP%\hermes-e2e_home_%RANDOM%.txt"
"%PYTHON%" "%PPT%\bootstrap_test_profile.py" --print-home > "%HOMEFILE%" 2>NUL
if errorlevel 1 goto :fatalfile
set /p _HERMES_HOME=<"%HOMEFILE%"
del "%HOMEFILE%" >NUL 2>NUL
set "HOMEFILE="

if not defined _HERMES_HOME goto :fatalfile

REM --- validate the materialized path before trusting it -----------------------
echo %_HERMES_HOME% | findstr /i /c:"hermes-e2e" >NUL || goto :fatalpath
echo %_HERMES_HOME% | findstr /i /c:"\profiles\test-fast-restart" >NUL || goto :fatalpath

set "HERMES_HOME=%_HERMES_HOME%"
set "HERMES_KANBAN_HOME=%_HERMES_HOME%\..\.."
echo [test-gateway] HERMES_HOME=%HERMES_HOME%
echo [test-gateway] KANBAN_HOME=%HERMES_KANBAN_HOME%

set "HERMES_GATEWAY_DETACHED=1"
set "PYTHONIOENCODING=utf-8"
set "PYTHONPATH=%REPO%;%PYTHONPATH%"

REM --- preflight against the repo and this isolated profile home -------------
"%PYTHON%" "%BASE%\scripts\gateway_preflight.py" --repo "%REPO%" --profile-home "%HERMES_HOME%"
if errorlevel 1 exit /b %errorlevel%

cd /d "%HERMES_HOME%"
"%PYTHON%" -m hermes_cli.main gateway run --replace
exit /b %errorlevel%

:fatalfile
echo [test-gateway] FATAL: could not materialize the isolated test profile.
echo [test-gateway] Refusing to start a gateway without a fresh temp HERMES_HOME.
exit /b 1

:fatalpath
echo [test-gateway] FATAL: materialized path is not an isolated hermes-e2e profile:
echo [test-gateway]   captured: %_HERMES_HOME%
echo [test-gateway] Refusing to start a gateway on any other profile.
exit /b 1
