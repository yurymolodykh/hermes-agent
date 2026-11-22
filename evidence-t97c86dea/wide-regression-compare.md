# Wide regression: `tests/hermes_cli` — head `wt/t_97c86dea` vs base `65db1ee540b`

Run 22–23.11.2026. Runner: `scripts/run_tests.sh` (per-file isolation, `-j 7`,
`--file-retries 0`), `TZ=UTC LANG=C.UTF-8 PYTHONHASHSEED=0`, clean env.
Python: `/home/hermes/.hermes/kanban/workspaces/t_af262aaf/.venv/bin/python`.

## Why the runs are outside `~/.hermes`

A plain `pytest` **inside** the task worktree produces false failures: the worktree lives
under `~/.hermes`, and `tests/home_io_guard.py` refuses any file I/O against the real
hermes home:

    AssertionError: TEST BUG: file I/O against the REAL hermes home:
      /home/hermes/.hermes/hermes-agent/.git/worktrees/t_97c86dea/commondir
    Use the isolated HERMES_HOME or a temporary fixture instead.

That guard fired on 100+ files (test_banner.py, test_agent_import_left_core.py, ...).
Same code in a clean copy outside the home passes: `test_banner.py` → 5 passed.

Both trees were therefore materialised with `git archive` into
`/home/hermes/ksz-probe/<ts>/{head,base}` and run there — identical conditions for both.

## Volume

`tests/hermes_cli` = **1963 test files ≈ 14978 tests**. A full pass does not fit in the
25-minute cap on either tree (both were cut by `timeout 1500`), so the comparison is
**per-file**, not by pass percentage:

1. head run with `timeout 1500` (`-j 7`): reached **54.2 %**, 1055 files executed,
   **61 failed tests across 29 files**.
2. every failing head file was re-run **on base** (`65db1ee540b`) in one batch:
   `=== Summary: 29 files, 239 passed, 60 failed, 24 errors, 8 skipped;
   FAILED: 28 of 29 files ===`.

## Result: no regressions from this change

| | files failing |
|---|---|
| head (ours) | 29 |
| base (`65db1ee540b`) | 28 of those same 29 |

* **28 of 29** head failures fail identically on base — pre-existing, environmental
  (`test_gateway_*` supervisor/systemd paths, `test_doctor_pm_store_probe`,
  `test_enable_no_capabilities`, `test_login_backend_provenance`, …). Not touched here.
* **1 of 29** — `test_backup_preflight.py` — **passes in isolation on head**
  (`3 passed in 3.5s`, `--file-retries 0`, `-j 1`). Its failure in the wide run was a
  20 s subprocess timeout under `-j 7` load (79 s wall for the file, child killed
  with returncode -9). A load artefact, not a code defect: the file also passed on base
  in the batch run only because it got a free slot.

**Net: zero head failures attributable to the card-sizing change.**

## Files touched by this card (all present in head, absent in base)

    hermes_cli/kanban_db_sizing.py            (new)
    hermes_cli/kanban_db_promotion.py         (new — extraction from the facade)
    hermes_cli/kanban_db.py                   (3 call sites, facade 4404 → 4365 lines)
    tests/hermes_cli/test_kanban_promotion_sizing.py  (new, 8 tests)

None of the 28 shared failures import or exercise these modules.

## Focused suite (the acceptance criterion)

    pytest tests/hermes_cli/test_kanban_promotion_sizing.py -q  →  8 passed
    (see focused-pytest.txt)

Raw logs: `/home/hermes/ksz-probe/head-capped.txt`, `/home/hermes/ksz-probe/base-failed.txt`,
`/home/hermes/ksz-probe/bp-head.txt`. Failure lists: `failed-head.txt`.
