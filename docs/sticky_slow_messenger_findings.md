# Sticky slow-mode on the messenger path — feasibility findings

Card: `t_c48aec43` (inspection only — **no production code changed** by this task)
Repo: `C:\Users\hermes\AppData\Local\hermes\hermes-agent`
Branch: `release/t_b0291572-owui-origin-binding`
Base revision (HEAD): `3a9ab31016f4b6a0687e6e506c4b01aa49ce3752`
Tree state at inspection: UNCOMMITTED work present (609 pre-existing dirty paths, incl.
`gateway/run.py`, `agent/fast_lane.py`; this task added **only** the untracked
`docs/sticky_slow_messenger_findings.md`). Both "HEAD" and "working tree (WT)" line numbers
are given below, because the sticky wiring already exists in the WT but **not** at HEAD.

Artefacts inspected (sha256 of the exact bytes read):

| file | size | sha256 |
|---|---|---|
| `gateway/run.py` | 1 570 064 | `d84815322bb939d639c71fd38fe04170ae7bdc4e01705842f6232fdeb41c5d95` |
| `agent/fast_lane.py` | 49 536 | `d658a030ee321a9f4b6cbcb8a58e78c93eea62281cf0b73d93e3537752be2dfa` |

---

## Verdict

**YES** — the messenger slow-mode site *can* read the sticky session marker.
`session_id` is already a parameter at the decision site and a DB handle is reachable
through the runner's own `_session_db` property. No call-graph restructuring is needed.
`session_slow_mode(db, session_id)` (`agent/fast_lane.py:571`) can be called from the
gateway path; on the event-loop thread it must be awaited off-loop
(`session_slow_mode_async`, `agent/fast_lane.py:628`, or `await asyncio.to_thread(...)`),
because the reader does a synchronous SQLite query.

A `# TODO(sticky-slow)` / "limitation" answer is **not** warranted.

---

## 1. Enclosing function of the decision site

The card's quoted site (`decide_slow_mode` + `return route_profile(decision)`) is inside

```
GatewayRunner._resolve_fast_lane_execute_profile(self, message, source, session_id=None, *, ...)
```

| reference | HEAD (`3a9ab31`) | working tree |
|---|---|---|
| `def _resolve_fast_lane_execute_profile` | 28980 | **29094** |
| `decision = decide_slow_mode(message, config=fl_cfg)` | **29042** (single line) | **29171** (`session_slow=...` added) |
| `return route_profile(decision)` | **29044** | **29198** |
| enclosing class `GatewayRunner` | 7234-… | 7234-… (same) |

Signature drift WT vs HEAD — the WT profile of the same method:

```python
def _resolve_fast_lane_execute_profile(
    self, message, source, session_id: Optional[str] = None, *,
    session_slow: Any = _STICKY_UNSET, decision_out: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
```
(`gateway/run.py:29094-29102`; `_STICKY_UNSET` sentinel defined at `7231`)

At HEAD the method is `(self, message, source)` only — no session identity at all.

## 2. Caller and availability of `session_id`

Sole production caller: **`GatewayRunner._run_agent`** (`async def`, WT `28892` / HEAD `28861`),
call site WT `28966` / HEAD `28899`:

```python
fast_exec_profile = self._resolve_fast_lane_execute_profile(
    message=message, source=source, session_id=session_id,        # WT only
    session_slow=fast_lane_session_slow, decision_out=_fl_decision_out,
)
```

- `session_id: str` is a **required positional parameter of `_run_agent`**
  (WT `28898`, HEAD `28867`) — not derived, not optional.
- It is supplied by `_handle_message_with_agent` at WT `21102`
  (`session_id=_run_start_session_id`) and by the in-band queued-follow-up
  recursion at WT `30880`.
- A second, non-production call site is the test module
  `tests/gateway/test_fast_lane_session_sticky_wiring.py` (10 call sites).

So `session_id` is available **without any restructuring** — it is already in scope one
frame up, and (in the WT) already threaded through.

## 3. DB handle availability

Two reachable handles, both on `GatewayRunner`:

1. **`self._session_db` property** — `gateway/run.py:7804`, returns the
   `AsyncSessionDB` for the *active profile scope* (per-path cache,
   `_open_session_db_for_active_scope` at `7768`), or a pinned override installed by
   tests (`setter` at `7818`). This is exactly what the site already uses elsewhere in
   the same file (`5809-5813`, `5870`, `6188`, `5986`).
2. **Sync core** — `AsyncSessionDB._db` (`hermes_state.py:13770`,
   `__getattr__` offloads every call through `asyncio.to_thread`). `agent.fast_lane._resolve_sync_db`
   (`531`) unwraps the door deliberately for sync call sites.

**`_ensure_session_db_async()` does NOT exist in `gateway/run.py`** (verified:
`rg -c` returns 0 hits). It is an `api_server`-only helper
(`gateway/platforms/api_server.py:3218`). The run.py equivalents are the `_session_db`
property above plus the module-level `asyncio.to_thread` idiom already used throughout
this file (e.g. `10564`, `11578`, `20569`). So the card's "check whether
`_ensure_session_db_async()` or an equivalent async DB accessor is reachable" resolves to:
*not that helper, but a working equivalent exists.*

## 4. Exact insertion point

Primary (used by the WT implementation):

> `GatewayRunner._run_agent`, immediately after the eligibility gate
> `_fl_route_cfg = self._fast_lane_route_config(source)` (`gateway/run.py:28947`)
> and **before** the selector call at `28966`.

```python
# WT 28946-28963 (already present)
fast_lane_session_slow = None
_fl_route_cfg = self._fast_lane_route_config(source)
if _fl_route_cfg is not None and session_id:
    try:
        from agent.fast_lane import session_slow_mode as _fl_sticky_read
        fast_lane_session_slow = await asyncio.to_thread(
            _fl_sticky_read, getattr(self, "_session_db", None), session_id
        )
    except Exception:
        fast_lane_session_slow = None      # best-effort -> "no session opinion"
```

Rationale for this exact spot (all verified in the file):
`_run_agent` is `async` and runs on the event loop, `_resolve_fast_lane_execute_profile`
is **sync** and is called inline from it — a bare `session_slow_mode(...)` inside the
selector would run a blocking SQLite read on the loop (measured cost of the real query:
2.5 ms on the live profile-01 store, see §6).
Gate choice matters: `_fast_lane_route_config` (`29049`) is the single eligibility
predicate (`fast_lane.enabled` + `route_to_profile` + `route_platforms`), so with the
feature off there is no DB read at all.

Alternate (minimal-diff, weaker) insertion point is the selector's own fallback branch,
WT `29157-29170`, where `session_slow_mode(getattr(self, "_session_db", None), session_id)`
is called inline when the caller passed no value. Functionally works, but it is a blocking
read on the loop and it is only reached when the caller did not pass a value.

## 5. Marker write on this path — confirmed

- Import: `patch_session_fast_lane_marker as _patch_fast_lane_marker` — `gateway/run.py:5796`
  (identical line number at HEAD and in the WT: **5796 confirmed**).
- Call: WT `5869` (`_patch_fast_lane_marker(..., preserve_sticky=True)`), HEAD `5846`
  (no `preserve_sticky`).
- Enclosing function of the write: **`TurnRunner.run_sync`** (`gateway/run.py:5563`).
- `run_sync` is executed **off the event loop** via
  `self._run_in_executor_with_context(_run_sync_with_timeout_lifecycle)` at
  `30298-30299` (helper `_run_in_executor_with_context` at `25464`) — so the *write*
  path is thread-safe by construction and correctly uses the sync reader.
- Gate: `if _fast_lane_active:` (`5868`), where `_fast_lane_active = _fast_lane_enabled(ctx.user_config)`
  (`5799`). Live config `profiles/01-orchestrator/config.yaml:1513-1517` has
  `fast_lane.enabled: true`, `route_to_profile: true`, `route_platforms: [yadisk_bridge]`
  → the gate is OPEN for this deployment.

### Defect that made the write a silent no-op at HEAD (documented here because it decides
whether stickiness can ever be observed on this path)

At HEAD, `patch_session_fast_lane_marker` (`agent/fast_lane.py`, HEAD version) does
`patch = getattr(db, "patch_session_model_config")` and calls it **synchronously**. On the
gateway path `db` is an `AsyncSessionDB`, whose `__getattr__` returns an *async* forwarder —
so HEAD's write returns an un-awaited coroutine and the marker never lands. Proven on a
throw-away `SessionDB`/`AsyncSessionDB` pair:

```
AsyncSessionDB.patch_session_model_config is coroutine fn: True
HEAD-style sync call returns: coroutine | isawaitable: True
row after never-awaited write  : None        <-- marker lost
row after WT patch (unwrapped) : True        <-- marker persisted
```

Live corroboration on the real store (`profiles/01-orchestrator/state.db`, read-only):

| source | sessions | with `slow_mode` marker |
|---|---|---|
| api_server | 397 | 135 |
| telegram | 154 | **0** |
| yadisk_bridge | 171 | **0** |

(`fast_lane`/`slow_mode` marker counts; same picture for the `fast_lane` key.)
The WT fixes this via `_resolve_sync_db` unwrap (`agent/fast_lane.py:531`; reads at `603`,
sticky-latch/write at `1056` and `1095`).

## 6. Can `session_slow_mode(db, session_id)` be called from here?

**Yes.** Evidence, all executed against the real store (`state.db`, 7.1 GB, read-only):

- Sync reader over the async door + `asyncio.to_thread` + `session_slow_mode_async` all
  return the persisted value:
  ```
  sync read via AsyncSessionDB door : True
  read via asyncio.to_thread        : True
  read via session_slow_mode_async  : True
  degrades: no-db -> None | no-sid -> None | missing row -> None
  ```
- Real-profile lookups (`session_slow_mode_from_db_file`, `agent/fast_lane.py:659`):
  ```
  marked session   -> True   2.55 ms   (owui-eef9a9f1-189f-41b9-b020-a33deb90d146)
  unmarked random  -> None   2.59 ms   (20261028_172140_9f2fbbda)
  missing row      -> None   2.35 ms   (no-such-session)
  ```
- End-to-end decision with the value threaded in
  (real `SessionDB` + `AsyncSessionDB`, WT `decide_slow_mode`/`route_profile`):
  ```
  marker-less follow-up in slow session   slow=True  source=session-sticky        route_profile -> None
  fresh session, marker-less              slow=False source=no-marker             route_profile -> 11-fast-answers
  fresh session, «подумай» prefix         slow=True  source=tg-prefix             route_profile -> None
  sticky + explicit fast escape           slow=True  source=session-sticky+tg-prefix route_profile -> None
  ```
  `route_profile -> None` == stay on the current (full-lane / `01-orchestrator`) profile.

Contract notes: reader and writer use the same key (`model_config.slow_mode`,
`session_slow_mode` docstring `agent/fast_lane.py:571-620`); every failure path returns
`None` = "no session opinion", which leaves per-request marker behaviour exactly as today
(`decide_slow_mode` is pure — no DB/IO, value passed in, signature at `882` with
keyword-only `session_slow`).

Existing gateway coverage (WT, run by this task):
`python -m pytest tests/gateway/test_fast_lane_session_sticky_wiring.py -q`
→ **26 passed** in 36.17 s; `tests/gateway/test_fast_lane_profile_routing_runpy.py -q`
→ **10 passed** in 7.43 s. That module asserts the `fast_lane=ROUTE ... source=session-sticky`
log line for the messenger path at `457-487`.

## 7. Caveats worth carrying into the implementation card

1. **Everything sticky is uncommitted.** `git show HEAD:agent/fast_lane.py` contains no
   `session_slow` symbol at all, and HEAD's `_resolve_fast_lane_execute_profile` has no
   `session_id`. Any downstream card must work on the working tree, not on HEAD.
2. **The running gateway is older than the working tree.** The `01-orchestrator` gateway
   process started `2026-10-28 18:53:06`; `gateway/run.py` was modified `2026-10-30 01:47:32`.
   So live traffic today still executes the pre-fix code (consistent with 0 telegram /
   0 yadisk_bridge markers in the table above). A restart is required before any live
   observation, and that restart is **not** in this card's scope.
3. **Read on the loop.** Use `await asyncio.to_thread(session_slow_mode, ...)` (or
   `session_slow_mode_async`) from `_run_agent`; do not call the sync reader inline in the
   selector on the production path.
4. **Fallback-less platforms.** `route_platforms: [yadisk_bridge]` in the live config means
   `_fast_lane_route_config` returns `None` for every other messenger → sticky is not even
   consulted there. Expected by design, but it means "messenger path" in the acceptance
   criteria effectively means yadisk_bridge on this config.

## Recommendation

**YES — wire the sticky read.** `session_id` + a DB handle are both already available at
the site; `session_slow_mode` is callable and degrades safely. The smallest correct shape is
the one already present in the working tree: read once in `_run_agent` off-loop (gated by
`_fast_lane_route_config`), pass the value into `decide_slow_mode(...,
session_slow=...)` via the selector's `session_slow` parameter. No call-graph restructuring,
no `# TODO(sticky-slow)`.
