"""Budgeted reads at the actual offline Binance manager lock boundary."""

from contextlib import contextmanager
from dataclasses import FrozenInstanceError
from functools import partial
from threading import Event, Lock, Thread
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from pytradekit.gateway.websocket import public_feed_access
from pytradekit.gateway.websocket.public_feed_access import PublicFeedReadBudget
from pytradekit.utils.dynamic_types import BinanceWebSocket
from pytradekit.utils.exceptions import (
    JwjException, PublicFeedReadCancelled, PublicFeedReadTimeout,
)
from pytradekit.utils.time_handler import get_monotonic_timestamp_ns
from tests.test_binance_public_feed_health import manager as manager


@pytest.fixture(params=['state', 'message'])
def read(manager, request):
    manager._state_lock = Lock()
    if request.param == 'state':
        return manager.get_public_feed_state
    message = {
        BinanceWebSocket.connection_id.value: manager._bookticker_health.stream_id,
        BinanceWebSocket.connection_generation.value: manager._generation,
    }
    return partial(manager.is_current_bookticker_message, message)


def _budget(duration_ns=1_000_000_000, cancelled=None):
    return PublicFeedReadBudget(get_monotonic_timestamp_ns() + duration_ns, cancelled)


def _start_reader(read, **kwargs):
    outcome = SimpleNamespace(started=Event(), done=Event(), result=None, error=None)

    def run():
        outcome.started.set()
        try:
            outcome.result = read(**kwargs)
        except BaseException as error:
            outcome.error = error
        finally:
            outcome.done.set()

    outcome.thread = Thread(target=run)
    outcome.thread.start()
    return outcome


@contextmanager
def _contended_reader(manager, read, **kwargs):
    manager._state_lock.acquire()
    outcome = None
    try:
        outcome = _start_reader(read, **kwargs)
        assert outcome.started.wait(1)
        yield outcome
    finally:
        manager._state_lock.release()
        if outcome is not None:
            outcome.thread.join(timeout=1)
            assert not outcome.thread.is_alive()


class _ObservedLock:
    """Record acquisition calls while keeping synchronization on a real Lock."""

    def __init__(self):
        self.lock = Lock()
        self.calls = []
        self.waiting = Event()

    def acquire(self, blocking=True, timeout=-1):
        self.calls.append((blocking, timeout))
        if timeout != -1:
            self.waiting.set()
        return self.lock.acquire(blocking, timeout)

    def release(self):
        self.lock.release()


@pytest.mark.parametrize('deadline', [True, False, 1.0, float('nan'), float('inf'), '1', None])
def test_budget_rejects_non_integer_deadlines(deadline):
    with pytest.raises(TypeError, match='deadline_ns'):
        PublicFeedReadBudget(deadline)


@pytest.mark.parametrize('deadline', [-1, 1 << 63])
def test_budget_rejects_out_of_range_deadlines(deadline):
    with pytest.raises(ValueError, match='deadline_ns'):
        PublicFeedReadBudget(deadline)


@pytest.mark.parametrize('deadline', [0, (1 << 63) - 1])
def test_budget_is_frozen_and_accepts_range_boundaries(deadline):
    budget = PublicFeedReadBudget(deadline, Event().is_set)
    assert budget.deadline_ns == deadline
    with pytest.raises(FrozenInstanceError):
        budget.deadline_ns = 1


@pytest.mark.parametrize('cancelled', [False, True, 0, 'cancel', Event()])
def test_budget_rejects_non_callable_predicates(cancelled):
    with pytest.raises(TypeError, match='cancelled'):
        _budget(cancelled=cancelled)


@pytest.mark.parametrize('invalid', [False, 1, {}, object()])
def test_accessors_reject_malformed_budget_without_locking(manager, read, invalid):
    with manager._state_lock:
        with pytest.raises(TypeError, match='read_budget'):
            read(read_budget=invalid)


def test_uncontended_budget_preserves_result_and_uses_immediate_acquire(manager, read):
    expected = read()
    observed = _ObservedLock()
    manager._state_lock = observed
    assert read(read_budget=_budget()) == expected
    assert observed.calls == [(False, -1)]
    assert not observed.lock.locked()


def test_budget_preserves_original_state_fields(manager):
    assert manager.get_public_feed_state(read_budget=_budget()) == {
        'stream_id': manager._bookticker_health.stream_id,
        'generation': manager._generation,
        'connected': True,
    }


def test_retirement_remains_visible_to_budgeted_accessors(manager, read):
    manager.reconnect(manager.ws)
    result = read(read_budget=_budget())
    assert result == read()
    assert result is False or result['connected'] is False


def test_no_budget_keeps_blocking_lock_semantics(manager, read):
    with _contended_reader(manager, read) as outcome:
        assert not outcome.done.wait(0.03)
    assert outcome.error is None
    assert outcome.result == read()


def test_no_budget_bypasses_budget_helper(read, monkeypatch):
    expected = read()
    helper = Mock(side_effect=AssertionError('unbudgeted read used budget helper'))
    monkeypatch.setattr('pytradekit.ws.binance_ws._public_feed_read_lock', helper)
    assert read() == expected
    assert read(read_budget=None) == expected
    helper.assert_not_called()


def test_unlock_before_deadline_allows_original_result(manager, read):
    with _contended_reader(manager, read, read_budget=_budget()) as outcome:
        assert not outcome.done.wait(0.03)
    assert outcome.error is None
    assert outcome.result == read()


def test_contended_read_times_out_without_renewing_deadline(manager, read):
    observed = _ObservedLock()
    manager._state_lock = observed
    budget = _budget(70_000_000)
    with _contended_reader(manager, read, read_budget=budget) as outcome:
        assert outcome.done.wait(0.5)
        assert isinstance(outcome.error, PublicFeedReadTimeout)
        assert outcome.result is None
        assert observed.lock.locked()
    timed_waits = [timeout for _, timeout in observed.calls if timeout != -1]
    assert timed_waits
    assert all(0 < timeout <= 0.01 for timeout in timed_waits)
    assert budget.deadline_ns < get_monotonic_timestamp_ns()
    assert str(budget.deadline_ns) in outcome.error.note
    assert isinstance(outcome.error, JwjException)


def test_cancellation_during_contention_returns_while_lock_stays_held(manager, read):
    observed, cancelled = _ObservedLock(), Event()
    manager._state_lock = observed
    with _contended_reader(manager, read, read_budget=_budget(cancelled=cancelled.is_set)) as outcome:
        assert observed.waiting.wait(1)
        cancelled.set()
        assert outcome.done.wait(0.5)
        assert isinstance(outcome.error, PublicFeedReadCancelled)
        assert isinstance(outcome.error, JwjException)
        assert 'deadline_ns=' in outcome.error.note
        assert outcome.result is None
        assert observed.lock.locked()


def test_timed_wait_is_exactly_remaining_budget_below_slice(manager, read, monkeypatch):
    observed, clock, deadline = _ObservedLock(), [100], 4_000_100
    original_acquire = observed.acquire

    def acquire_then_expire(*args, **kwargs):
        acquired = original_acquire(*args, **kwargs)
        if kwargs.get('timeout', -1) != -1:
            assert acquired is False
            clock[0] = deadline
        return acquired

    observed.acquire = acquire_then_expire
    manager._state_lock = observed
    monkeypatch.setattr(public_feed_access, 'get_monotonic_timestamp_ns', lambda: clock[0])
    with _contended_reader(manager, read, read_budget=PublicFeedReadBudget(deadline)) as outcome:
        assert outcome.done.wait(0.5)
        assert isinstance(outcome.error, PublicFeedReadTimeout)
        assert observed.lock.locked()
    assert [timeout for _, timeout in observed.calls if timeout != -1] == [0.004]


@pytest.mark.parametrize('failure_kind', ['raised', 'wrong_type'])
def test_predicate_failure_after_unsuccessful_timed_wait(manager, read, failure_kind):
    observed, timed_out = _ObservedLock(), Event()
    predicate_error = RuntimeError('contended predicate failure')
    original_acquire = observed.acquire

    def observe_unsuccessful_wait(*args, **kwargs):
        acquired = original_acquire(*args, **kwargs)
        if kwargs.get('timeout', -1) != -1 and not acquired:
            timed_out.set()
        return acquired

    def predicate():
        if not timed_out.is_set():
            return False
        if failure_kind == 'raised':
            raise predicate_error
        return 1

    observed.acquire = observe_unsuccessful_wait
    manager._state_lock = observed
    with _contended_reader(manager, read, read_budget=_budget(cancelled=predicate)) as outcome:
        assert outcome.done.wait(0.5)
        assert timed_out.is_set()
        if failure_kind == 'raised':
            assert outcome.error is predicate_error
        else:
            assert isinstance(outcome.error, TypeError)
            assert 'predicate must return bool' in str(outcome.error)
        assert outcome.result is None
        assert observed.lock.locked()


@pytest.mark.parametrize('invalidated', ['expired', 'cancelled'])
def test_invalid_budget_is_rejected_before_any_lock_attempt(manager, read, invalidated):
    observed, cancelled = _ObservedLock(), Event()
    manager._state_lock = observed
    cancelled.set()
    budget = PublicFeedReadBudget(0) if invalidated == 'expired' else _budget(cancelled=cancelled.is_set)
    expected = PublicFeedReadTimeout if invalidated == 'expired' else PublicFeedReadCancelled
    with pytest.raises(expected):
        read(read_budget=budget)
    assert observed.calls == []


def test_budget_is_not_renewed_between_reads(manager, read, monkeypatch):
    clock = [100]
    monkeypatch.setattr(public_feed_access, 'get_monotonic_timestamp_ns', lambda: clock[0])
    budget = PublicFeedReadBudget(200)
    expected = read()
    assert read(read_budget=budget) == expected
    clock[0] = 200
    with pytest.raises(PublicFeedReadTimeout):
        read(read_budget=budget)
    assert not manager._state_lock.locked()


@pytest.mark.parametrize('contended', [False, True])
def test_lock_acquisition_at_deadline_rejects_before_snapshot(manager, read, monkeypatch, contended):
    observed, clock = _ObservedLock(), [100]
    original_acquire = observed.acquire

    def acquire_at_deadline(*args, **kwargs):
        if contended and kwargs.get('blocking') is False:
            return False
        acquired = original_acquire(*args, **kwargs)
        clock[0] = 200
        return acquired

    observed.acquire = acquire_at_deadline
    manager._state_lock = observed
    monkeypatch.setattr(public_feed_access, 'get_monotonic_timestamp_ns', lambda: clock[0])

    def forbidden_snapshot(*args):
        pytest.fail('snapshot must not run at the acquisition deadline')

    monkeypatch.setattr(manager, '_accept_public_socket', forbidden_snapshot)
    with pytest.raises(PublicFeedReadTimeout):
        read(read_budget=PublicFeedReadBudget(200))
    assert not observed.lock.locked()


def test_cancellation_while_snapshot_is_in_flight_discards_result(manager, read, monkeypatch):
    entered, release, cancelled = Event(), Event(), Event()
    original_connected = manager._is_connected

    def pause_snapshot(ws):
        entered.set()
        assert release.wait(1)
        return original_connected(ws)

    monkeypatch.setattr(manager, '_is_connected', pause_snapshot)
    outcome = _start_reader(read, read_budget=_budget(cancelled=cancelled.is_set))
    try:
        assert entered.wait(1)
        cancelled.set()
        assert not outcome.done.is_set()
    finally:
        release.set()
        outcome.thread.join(timeout=1)
    assert not outcome.thread.is_alive()
    assert isinstance(outcome.error, PublicFeedReadCancelled)
    assert outcome.result is None
    assert not manager._state_lock.locked()


@pytest.mark.parametrize('invalidated', ['expired', 'cancelled'])
def test_invalidation_during_snapshot_discards_result(manager, read, monkeypatch, invalidated):
    clock, cancelled = [100], Event()
    original_connected = manager._is_connected

    def invalidate_during_snapshot(ws):
        assert manager._state_lock.locked()
        clock[0] = 200 if invalidated == 'expired' else 100
        cancelled.set()
        return original_connected(ws)

    monkeypatch.setattr(manager, '_is_connected', invalidate_during_snapshot)
    monkeypatch.setattr(public_feed_access, 'get_monotonic_timestamp_ns', lambda: clock[0])
    expected = PublicFeedReadTimeout if invalidated == 'expired' else PublicFeedReadCancelled
    with pytest.raises(expected):
        read(read_budget=PublicFeedReadBudget(200, cancelled.is_set))
    assert not manager._state_lock.locked()


def test_snapshot_exception_stays_primary_after_invalidation(manager, read, monkeypatch):
    clock, cancelled, predicate_calls = [100], Event(), []
    snapshot_error = RuntimeError('snapshot failure')

    def predicate():
        predicate_calls.append(True)
        return cancelled.is_set()

    def fail_snapshot(ws):
        clock[0] = 200
        cancelled.set()
        raise snapshot_error

    monkeypatch.setattr(manager, '_is_connected', fail_snapshot)
    monkeypatch.setattr(public_feed_access, 'get_monotonic_timestamp_ns', lambda: clock[0])
    with pytest.raises(RuntimeError) as captured:
        read(read_budget=PublicFeedReadBudget(200, predicate))
    assert captured.value is snapshot_error
    assert predicate_calls == [True]
    assert not manager._state_lock.locked()


def test_predicate_runs_before_and_after_snapshot_only_outside_lock(manager, read):
    calls = []

    def predicate():
        acquired = manager._state_lock.acquire(blocking=False)
        if acquired:
            manager._state_lock.release()
        assert acquired, 'predicate ran while the reader held the state lock'
        calls.append(True)
        return False

    assert read(read_budget=_budget(cancelled=predicate)) == read()
    assert calls == [True, True]


@pytest.mark.parametrize('failure_call', [1, 2])
@pytest.mark.parametrize('failure_kind', ['raised', 'wrong_type'])
def test_predicate_failure_propagates_without_lock_leak(manager, read, failure_call, failure_kind):
    calls, predicate_error = [], RuntimeError('predicate failure')

    def predicate():
        calls.append(True)
        if len(calls) != failure_call:
            return False
        if failure_kind == 'raised':
            raise predicate_error
        return 1

    expected = RuntimeError if failure_kind == 'raised' else TypeError
    with pytest.raises(expected) as captured:
        read(read_budget=_budget(cancelled=predicate))
    if failure_kind == 'raised':
        assert captured.value is predicate_error
    assert not manager._state_lock.locked()


@pytest.mark.parametrize('failure_call', [1, 2])
def test_predicate_consuming_deadline_rejects_result(manager, read, monkeypatch, failure_call):
    clock, calls = [100], []

    def predicate():
        calls.append(True)
        if len(calls) == failure_call:
            clock[0] = 200
        return False

    monkeypatch.setattr(public_feed_access, 'get_monotonic_timestamp_ns', lambda: clock[0])
    with pytest.raises(PublicFeedReadTimeout):
        read(read_budget=PublicFeedReadBudget(200, predicate))
    assert len(calls) == failure_call
    assert not manager._state_lock.locked()
