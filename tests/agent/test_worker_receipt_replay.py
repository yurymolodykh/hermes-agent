"""A stalled owner loop must not turn a written turn into a failed one.

``WorkerRPC.__call__`` used to give every call one fixed 20 s receipt budget and, on expiry, raise
with the outcome unknown — while the owner had in fact committed the receipt. Under 7 concurrent
workers the loop stalls logged as ``ws write slow (loop stalled >10s)`` made exactly that the
common case: 38 ``finalize_turn: _persist_session failed: worker_receipt_timeout`` lines in one
day, each one a finished card whose result was not recorded.

``worker.persist`` has a replay contract (an exact execution/sequence/digest retry returns the
stored result), so the call may ask once more instead of failing.
"""
import json

import pytest


class FakeLink:
    """Scripted owner connection: each ``receive`` pops one action.

    ``'stall'`` raises the receipt timeout; a dict is the reply; an exception instance is raised.
    """

    def __init__(self, actions):
        self.actions = list(actions)
        self.sent = []
        self.reader = type('R', (), {'is_alive': lambda self: False})()

    def send(self, request_id, message):
        self.sent.append(json.loads(message))

    def receive(self, timeout):
        if not self.actions:
            return {'jsonrpc': '2.0', 'id': self.sent[-1]['id'], 'result': {'ok': True}}
        action = self.actions.pop(0)
        if action == 'stall':
            raise TimeoutError('worker_receipt_timeout')
        if isinstance(action, BaseException):
            raise action
        return action

    def close(self):
        pass


@pytest.fixture
def make_rpc(tmp_path, monkeypatch):
    from agent.runtime_session_store import WorkerRPC

    def build(actions):
        rpc = WorkerRPC(home=tmp_path)
        link = FakeLink(actions)
        rpc._connection = lambda: link
        return rpc, link

    return build


def test_receipt_budget_defaults_to_twenty_seconds(monkeypatch):
    from agent.runtime_session_store import WorkerRPC
    monkeypatch.delenv('HERMES_WORKER_RECEIPT_TIMEOUT_S', raising=False)
    assert WorkerRPC.receipt_budget_s() == 20.0


def test_receipt_budget_is_configurable_and_clamped(monkeypatch):
    from agent.runtime_session_store import WorkerRPC
    monkeypatch.setenv('HERMES_WORKER_RECEIPT_TIMEOUT_S', '45')
    assert WorkerRPC.receipt_budget_s() == 45.0
    monkeypatch.setenv('HERMES_WORKER_RECEIPT_TIMEOUT_S', '0')
    assert WorkerRPC.receipt_budget_s() == 5.0
    monkeypatch.setenv('HERMES_WORKER_RECEIPT_TIMEOUT_S', 'nonsense')
    assert WorkerRPC.receipt_budget_s() == 20.0
    monkeypatch.setenv('HERMES_WORKER_RECEIPT_TIMEOUT_S', '100000')
    assert WorkerRPC.receipt_budget_s() == 300.0


def test_persist_replays_once_after_a_receipt_timeout(make_rpc):
    """The stall that used to fail the turn now ends in the owner's own receipt."""
    rpc, link = make_rpc(['stall', {'jsonrpc': '2.0', 'id': 1, 'result': {'value': None}}])
    assert rpc('worker.persist', execution_id='e', sequence=1) == {'value': None}
    assert len(link.sent) == 2, 'a replay must re-send the same call'
    assert link.sent[0] == link.sent[1], 'the replay must be byte-identical (same id, exact args)'


def test_persist_gives_up_after_one_replay(make_rpc):
    rpc, link = make_rpc(['stall', 'stall'])
    with pytest.raises(TimeoutError) as exc:
        rpc('worker.persist', execution_id='e', sequence=1)
    assert 'worker_receipt_timeout' in str(exc.value)
    assert len(link.sent) == 2, 'exactly one replay, never a loop'


@pytest.mark.parametrize('method', ['worker.adopt', 'prompt.receipt', 'session.list'])
def test_methods_without_a_replay_contract_still_fail_fast(make_rpc, method):
    rpc, link = make_rpc(['stall'])
    with pytest.raises(TimeoutError):
        rpc(method, session_id='s')
    assert len(link.sent) == 1, 'no replay without a contract'


def test_a_connect_timeout_is_not_the_replayable_one(make_rpc):
    """A handshake timeout never sent the request: it must surface, not replay."""
    class ConnectTimeout(RuntimeError):
        pass

    rpc, link = make_rpc([])

    def refuse():
        raise ConnectTimeout('connect timed out')

    rpc._connection = refuse
    with pytest.raises(ConnectTimeout):
        rpc('worker.persist', execution_id='e', sequence=1)
    assert len(link.sent) == 0


def test_rpc_errors_are_still_raised_not_replayed(make_rpc):
    rpc, link = make_rpc([{'jsonrpc': '2.0', 'id': 1, 'error': {'message': 'stale_generation'}}])
    with pytest.raises(Exception) as exc:
        rpc('worker.persist', execution_id='e', sequence=1)
    assert 'stale_generation' in str(exc.value)
    assert len(link.sent) == 1
