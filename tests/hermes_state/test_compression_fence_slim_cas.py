"""The slimmed ``held_for_cas`` must not blind the in-transaction staleness fence (#124102).

``compression.archive`` used to ship the whole handoff projection as ``held_for_cas``, so the frame
carried the transcript twice and overflowed a worker's 4 MiB journal (``outbox_full``). It now ships
only the identity columns. That projection is only safe if the owner's ``held_row_mismatches`` still
catches an in-place rewrite landed before the commit — these two tests prove the fence is alive on
the slim payload, and that a clean transcript still commits.
"""

import agent.conversation_loop  # imported at collection time: the insert path lazily imports it
# for actionable user turns, and that import touches the real home outside the test guard.


def _held(store, session_id="owned"):
    """Held dicts as the compressor holds them: durable row ids plus the post-commit persist marker."""
    rows = store.get_messages_as_conversation(session_id, include_row_ids=True)
    for row in rows:
        row["_db_persisted"] = True  # sync_flushed_message_markers' post-commit stamp
    return rows


def test_rewrite_after_the_precheck_still_aborts_the_commit(worker):
    from hermes_state_errors import HeldRowRewrittenError

    db, store = worker
    store.append_messages_batch("owned", [
        {"role": "user", "content": "remember this answer"},
        {"role": "assistant", "content": "noted: 42"},
        {"role": "user", "content": "and this too"},
    ])
    held = _held(store)
    assert held and all(isinstance(m.get("_row_id"), int) for m in held)
    assert store.try_acquire_compression_lock("owned", "holder")
    # The race #124102 is about: another writer rewrites a held row in place after the
    # caller's precheck, and only the in-transaction compare can see it.
    db.set_user_message_content("owned", held[0]["_row_id"], "TAMPERED")
    try:
        store.archive_and_compact("owned", held, lock_holder="holder", held_for_cas=held)
    except HeldRowRewrittenError:
        pass
    else:
        raise AssertionError("the commit published a stale compaction over a rewritten row")


def test_a_clean_transcript_still_commits_through_the_slim_payload(worker):
    _db, store = worker
    store.append_messages_batch("owned", [
        {"role": "user", "content": "remember this answer"},
        {"role": "assistant", "content": "noted: 42"},
    ])
    held = _held(store)
    assert store.try_acquire_compression_lock("owned", "holder")
    archived = store.archive_and_compact("owned", held, lock_holder="holder", held_for_cas=held)
    assert archived == len(held)
    assert store.failure is None
