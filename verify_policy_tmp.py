"""Ad-hoc verification: the nine service kinds never reach the owner on telegram."""
from gateway.owner_delivery_policy import OwnerDeliveryConfig, classify_kanban_event

cfg = OwnerDeliveryConfig()
assert cfg.covers("telegram") and not cfg.covers("local")
service = ["crashed", "timed_out", "gave_up", "status", "review_requested",
           "changes_requested", "block_loop_detected", "commented", "blocked"]
payloads = {"status": {"status": "running"}, "blocked": {"kind": "dependency"},
            "block_loop_detected": {"kind": "transient"}}
for kind in service:
    cat, reason = classify_kanban_event(
        kind=kind, payload=payloads.get(kind, {"summary": "x"}),
        has_open_ancestor=False, artifacts=["/a.pdf"])
    print(f"{kind:<20}{str(cat):<10}{reason}")
    assert cat is None, kind
print("decision:", classify_kanban_event(kind="blocked", payload={"kind": "needs_input", "reason": "r"}))
print("final   :", classify_kanban_event(
    kind="completed", payload={"artifacts": ["/r.pdf", "/a.md"]}, has_open_ancestor=False))
print("intermed:", classify_kanban_event(
    kind="completed", payload={"artifacts": ["/r.pdf"]}, has_open_ancestor=True))
