# fast-restart-e2e — isolated test profile for the fast-reboot e2e harness

This directory ships the **isolated test profile** used by the fast-reboot
(fast gateway restart) end-to-end harness (`../test_fast_restart_e2e.py` and
friends).  The profile is deliberately tiny and self-contained: it runs the
Hermes gateway with **only the `api_server` platform on port 18642**, pointed
at a throwaway mock LLM on port 18643, against a **fresh temporary
HERMES_HOME** that is re-created on every invocation.

## What is in here

| File | Purpose |
|------|---------|
| `test-profile/config.yaml` | api_server-only profile **template** (never used in place).  Contains no reference to the production port 8642 and no telegram/yadisk platform config. |
| `test-profile/.env.template` | Environment **template** with placeholders for throwaway keys.  No real credentials anywhere in the tree. |
| `bootstrap_test_profile.py` | Materializes the template into a fresh `%TEMP%\hermes-e2e\<stamp>\profiles\test-fast-restart`, generates random throwaway API keys, prints the path (or JSON summary). |
| `start_test_gateway.cmd` | Windows launcher: clears inherited env, materializes a fresh profile, validates the path, runs `gateway_preflight.py`, then starts the gateway against it. |
| `verify_isolated_profile.py` | Acceptance checker — asserts the isolation invariants (see below).  Scans both the checked-in template and a materialized profile (`--dir`). |
| `live_boot_proof.py` | Boots the **real gateway** against a freshly materialized profile and asserts: healthy on 18642, prod 8642 untouched, no telegram/yadisk creds leaked into the gateway env. |

## The isolation contract (why this is safe)

The checked-in `test-profile/` tree is **never used directly as HERMES_HOME**.
Every run materializes a fresh copy into a brand-new temporary directory, so
the production HERMES_HOME and the production gateway on port **8642** are
never touched.  The invariants enforced by `verify_isolated_profile.py`:

1. `test-profile/` template exists.
2. No `8642` reference in the template **or** in a materialized profile
   (comment-stripped — config only).
3. api_server-only: no telegram / yadisk platform enablement or credentials.
4. `API_SERVER_PORT=18642`, `API_SERVER_HOST=127.0.0.1` (loopback only).
5. No real credential values (only generated throwaway keys).
6. HERMES_HOME is under `%TEMP%` and is not the default profile home.

The `.cmd` launcher adds defense in depth on top:

* `setlocal` alone **does not** clear inherited environment — a stale
  `HERMES_HOME` pointing at a production profile would launch the *real*
  gateway.  The wrapper therefore explicitly `set ""` every gateway-trusted
  variable (`HERMES_HOME`, `HERMES_KANBAN_HOME`, `HERMES_PROFILE`, all
  `HERMES_KANBAN_*`, `HERMES_SESSION*`) **before** it does anything else.
* The materialized path is captured through a temp file (robust against the
  quoting fragility of `for /f` with nested quotes) and then **validated** to
  match `<temp>\hermes-e2e\...\profiles\test-fast-restart` before it is
  adopted.  If either the capture or the validation fails, the wrapper aborts
  with a FATAL message and exit code 1 — it never falls back to an inherited
  profile.
* Any inherited `TELEGRAM_*` / `YADISK_*` credential variables are stripped
  from the gateway environment, so real credentials can never reach the test
  gateway even if the calling shell exports them.

## Usage

```bat
:: 1.  Static check of the checked-in template
python scripts\fast-restart-e2e\verify_isolated_profile.py

:: 2.  Bootstrap a fresh profile and check the materialized copy
set PD=
for /f "delims=" %%i in ('venv\Scripts\python.exe scripts\fast-restart-e2e\bootstrap_test_profile.py --print-home') do set PD=%%i
venv\Scripts\python.exe scripts\fast-restart-e2e\verify_isolated_profile.py --dir "%PD%"

:: 3.  Live proof: boot the REAL gateway against a fresh isolated profile
venv\Scripts\python.exe scripts\fast-restart-e2e\live_boot_proof.py

:: 4.  One-shot interactive start of the test gateway (clears env, preflights,
::     runs gateway on 18642 with the isolated profile)
scripts\fast-restart-e2e\start_test_gateway.cmd
```

`live_boot_proof.py` shuts its gateway down after the checks; `start_test_gateway.cmd`
keeps it running until you press Ctrl+C — it multiplexes api_server (18642) plus
cron, exactly like the real gateway, but against the throwaway profile.

## The e2e smoke script (fresh worker, no setup)

`../test_fast_restart_e2e.py` is the full acceptance smoke test for the
fast-restart path.  It is self-contained: it boots a REAL gateway twice against
a freshly materialized isolated profile, drives a mock-LLM cron job through an
in-band fast restart, and asserts downtime <= 30 s, cron resume + terminal
status, the `FAST_RESTART` log marker, and old-generation kanban reclaim
<= 60 s.  It never touches production port 8642 and sends no real Telegram
messages.  Run it with zero arguments from any directory:

```bat
python scripts\test_fast_restart_e2e.py
```

* Requires the repo's `venv` (the interpreter is auto-resolved from
  `venv/pyvenv.cfg`; repo site-packages are injected via `PYTHONPATH`).
* Writes `fast_restart_e2e_report.json` (fields: `downtime_s`, `resumed`,
  `reclaimed`, `asserts`) into the current working directory by default
  (`--report PATH` to redirect).
* Cleans up its temporary HERMES_HOME/kanban home after the run by default;
  pass `--keep` to retain it for inspection.
* Exits 0 only when every assertion passes; the JSON report still records the
  per-assert results when the run fails.

## Verification status

All checks in `verify_isolated_profile.py` (template + materialized) and all
checks in `live_boot_proof.py` currently **PASS**.  Production gateway on 8642
remains untouched across every run in this suite (observed PID unchanged).

## Notes

* Port map: 18642 = api_server (test), 18643 = mock LLM, 8642 = production
  (never bound by this profile).
* The gateway auto-loads the profile's `.env` (`hermes_cli/env_loader.py`), so
  `API_SERVER_PORT` / `API_SERVER_KEY` take effect automatically; `api_server`
  auto-enables from env (`gateway/platforms/api_server.py`).
* `telegram_delivery_policy: off` is a deliberate safety switch that disables
  delivery; the api_server-only invariant check does not treat the word
  "telegram" in prose as enablement — only explicit config/credential markers.
* Generated keys are throwaway per run (`secrets.token_urlsafe`); nothing real
  is ever committed.  See `.env.template` comments (plain prose, no
  secret-shaped text).
