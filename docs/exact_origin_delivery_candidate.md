# Exact-origin delivery candidate (cron / Kanban / Open WebUI)

Scope: isolated core candidate, not a release or a migration of live chats. The
canonical repository and the user's live WebUI are untouched. No file content
leaves the machine.

## Opt-in contract

* A profile-local gateway and cron process must see the same `HERMES_HOME` and
  `HERMES_OPENWEBUI_DB_PATH` (local server-owned SQLite `chat` registry). Writing
  is separately opt-in on the gateway with
  `HERMES_OPENWEBUI_DELIVERY_WRITE_ENABLED=1`; the DB path alone only enables
  read-only origin validation. Without
  this explicit path, API delivery fails closed; no home-chat fallback or
  HTTP self-post counts as a receipt. The gateway notifier tick drains the
  profile-local `delivery-outbox.sqlite` even if the Kanban board is empty.
* A pre-binding cron record without an owner requires an operator-reviewed
  exact `job-id:owui-uuid` entry in the comma-separated
  `HERMES_OPENWEBUI_LEGACY_CRON_ALLOWLIST`. The allowlist proves an operator
  reviewed the particular archived origin, not that the chat id is a secret.
  A Kanban subscription must already carry the matching server-owned
  `user_id` and the correct `notifier_profile`; absent/mismatched ownership
  retains its cursor and never creates a delivery.
* The privileged local writer uses `BEGIN IMMEDIATE` on the WebUI registry,
  verifies exact owner and active history branch, appends a deterministic
  assistant message, commits, reads back the exact persisted message and only
  then ACKs the outbox. Concurrent retries are idempotent. No public write
  endpoint and no credentials are introduced. Pending and exhausted records
  remain in the outbox; retries back off exponentially and stop after a
  bounded number of attempts. `gateway.origin_outbox.counts(path)` reports
  `pending`, `acknowledged`, `failed`; `last_delivery_error` remains distinct
  from cron execution success. Do not interpret a pending enqueue as delivery.
* Telegram push remains on its existing adapter path. The API server is NOT
  permitted to wake itself with a fake event. A claimed Kanban cursor is
  rewound until the exact persisted receipt exists; root, child and review
  events use their event ID plus board as deduplication key. PWA has no native
  platform/renderer contract in this repository and is NOT covered by this
  candidate; a separate authenticated PWA ingress/receipt implementation is
  needed before claiming cross-client parity.

## Surface matrix

| Surface | Candidate | Evidence / remaining gate |
|---|---|---|
| Open WebUI cron | Exact owner-bound outbox + opt-in SQLite writer + persisted active-branch readback | Fixture integration, duplicate/restart; live client reload, concurrent writer and UI visibility need security/release review. |
| Open WebUI Kanban | Terminal event outbox + cursor retained until persisted ACK | Notifier integration and root/child/review staging fixtures; active browser visibility unverified. |
| Telegram | Existing push adapter and topic metadata retained, not changed by this candidate | Focused notifier regression; no new external send/readback in this branch. |
| PWA | Unsupported: no accepted native Bridge owner/receipt contract in this codebase | Never infer success from the Open WebUI/API-server fixture; separate owner-authenticated S2C integration required. |

## Validation and release gates

Fixtures exercise cron producer -> worker -> persistent chat readback,
Kanban notifier cursor -> stage -> ACK -> cursor advancement, owner/branch
conflicts, absent registry, retries/exhaustion, and existing Telegram notifier
regressions. SQLite fixture readback proves persistence, not that a currently
open browser re-renders without reload. Before release, independently inspect
an Open WebUI instance's actual `chat` schema and active UI behavior (including
multiple branches), exercise both reload and live view with a non-sensitive
canary, verify exact owner mapping and old-job allowlist, and review concurrent
WebUI app writes. No live backfill or PWA/Telegram external send has been done
in this candidate. A shared `kanban_watchers.py` hotspot needs reconciliation
with concurrent notifier changes before cherry-picking.
