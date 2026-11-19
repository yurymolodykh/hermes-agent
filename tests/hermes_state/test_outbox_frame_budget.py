"""A long run's ``compression.archive`` frame must fit the worker journal's fixed cap.

The frame used to carry the whole transcript twice (``messages`` plus a full copy in
``held_for_cas``): measured on a real 284-row run the journal needed 4 617 174 B against the
4 MiB cap and the worker refused with ``outbox_full``. ``held_for_cas`` only feeds the owner's
staleness compare, so it now ships the identity columns instead of the projection.

Read the printed sizes with ``pytest -s``: they are the contract, not just the assertion.
"""
import json

import pytest

import agent.conversation_loop  # imported at collection time: the insert path lazily imports it
# for actionable user turns, and that import touches the real home outside the test guard.
from agent.runtime_session_store import RuntimeSessionStore
from hermes_state import SessionDB
from hermes_state_runtime import begin_runtime_epoch, mutate_worker_execution, register_worker_execution

CAP = 4 * 1024 * 1024
# A transcript shaped like the runs that failed: tool couples, long reasoning, big tool results.
ROW_COUNT = 280


def _transcript():
    rows = []
    for i in range(ROW_COUNT):
        if i % 4 == 0:
            rows.append({"role": "user", "content": f"step {i}: " + "plan the next verification " * 12})
        elif i % 4 == 1:
            # Reasoning dominates the way it did on the real run (791 KB of a 2.3 MB projection).
            rows.append({"role": "assistant", "content": f"working on {i} " * 8,
                         "reasoning_content": "reasoning about the state " * 950,
                         "tool_calls": [{"id": f"call_{i}", "type": "function",
                                         "function": {"name": "terminal",
                                                      "arguments": json.dumps({"command": "ls -la" * 20})}}]})
        else:
            rows.append({"role": "tool", "tool_call_id": f"call_{i - 1}",
                         "content": "output line with detail " * 120})
    return rows


@pytest.fixture()
def store(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    db.create_session("root", "cli", system_prompt="PREFIX")
    epoch = begin_runtime_epoch(db, instance_id="owner")
    scope = dict(epoch=epoch, execution_id="worker", session_id="root", generation=0)
    register_worker_execution(db, **scope, kind="compute", adoption_secret="secret")
    s = RuntimeSessionStore(lambda method, **params: mutate_worker_execution(db, **params),
                            scope, tmp_path / "outbox")
    yield db, s
    s.failure = None
    s.journal["pending"] = []
    s.close()
    db.close()


def _archive_recording_journal(s, session_id, messages):
    """Run ``archive_and_compact`` and return (bytes, journal) of its largest write attempt.

    The journal file itself is emptied again the moment the receipt lands, so the frame size has to
    be captured at ``_save``, which is exactly the byte budget ``outbox_full`` enforces.
    """
    written = []
    real_save = s._save

    def recording(journal):
        encoded = json.dumps(journal, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
        written.append((len(encoded.encode()), json.loads(encoded)))
        return real_save(journal)

    s._save = recording
    try:
        assert s.try_acquire_compression_lock(session_id, "holder")
        s.archive_and_compact(session_id, messages, lock_holder="holder", held_for_cas=messages)
    finally:
        del s._save
    return max(written, key=lambda pair: pair[0])


def test_archive_frame_for_a_long_run_fits_the_journal_cap(store):
    _db, s = store
    messages = _transcript()
    s.append_messages_batch("root", messages)
    frame_bytes, journal = _archive_recording_journal(s, "root", messages)
    payload = journal["pending"][0]["payload"]
    cas_bytes = len(json.dumps(payload["held_for_cas"], ensure_ascii=False).encode())
    msg_bytes = len(json.dumps(payload["messages"], ensure_ascii=False).encode())
    print(f"\njournal={frame_bytes:,} B cap={CAP:,} messages={msg_bytes:,} held_for_cas={cas_bytes:,}")
    assert not ({"reasoning", "reasoning_content", "timestamp", "system_prompt"} & set(payload["held_for_cas"][0])), \
        "held_for_cas must not carry the bulk columns — that duplication is what overflowed the journal"
    assert cas_bytes < msg_bytes // 2, "held_for_cas must stay a slim identity payload, not a transcript copy"
    assert msg_bytes * 2 + 400 > CAP, "the fixture must be big enough that the old double copy overflowed"
    assert frame_bytes < CAP, f"frame {frame_bytes:,} B exceeds the {CAP:,} B journal cap"


def test_cas_payload_still_carries_everything_the_staleness_fence_reads(store):
    """The budget fix must not blind the fence: identity, marker, digest and the loader-lens
    payload columns all have to survive the projection."""
    _db, s = store
    messages = [{"role": "user", "content": "keep me"}]
    s.append_messages_batch("root", messages)
    _size, journal = _archive_recording_journal(s, "root", messages)
    held = journal["pending"][0]["payload"]["held_for_cas"][0]
    assert {"role", "content", "_row_id"} <= set(held)
    assert held.get("message_uid")
    assert set(held) <= {"role", "content", "tool_call_id", "tool_calls", "_row_id", "message_uid",
                         "_db_row_snapshot", "_tool_call_uids", "_tool_call_uid", "_db_persisted"}
