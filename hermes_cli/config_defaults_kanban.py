"""Pure-data factory for the ``kanban`` config block's owner delivery policy."""

#: Deliverable file types the owner accepts in a chat. Source, log and diff artifacts stay
#: on disk: they are re-readable evidence for the board, not a message.
OWNER_DELIVERY_ARTIFACT_EXTENSIONS = [
    ".pdf", ".docx", ".doc", ".xlsx", ".pptx", ".html", ".htm",
    ".png", ".jpg", ".jpeg", ".svg", ".webp", ".zip", ".csv", ".txt",
]


def owner_delivery_policy_default() -> dict:
    """``kanban.owner_delivery_policy``: decisions and final deliverables only.

    On the platforms listed here the notifier delivers just the categories an owner acts
    on — a request for their decision and the finished deliverables of a card (article +
    whitelisted files). Crashes, time-outs, comments, review handoffs and every other
    machine event stay on the board, and the creator's session is woken for a decision
    only. Other platforms (desktop ``local``, api_server, cron) are untouched.
    ``enabled: false`` restores the previous behaviour verbatim.
    """
    return {
        "enabled": True,
        "platforms": ["telegram"],
        "artifact_extensions": list(OWNER_DELIVERY_ARTIFACT_EXTENSIONS),
    }
