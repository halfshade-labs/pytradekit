"""Funding bill pagination and repeated windows obey the independent 1/s quota."""
import threading
import time
from unittest.mock import Mock

import pytest

from pytradekit.restful import okex_restful as mod
from pytradekit.utils.exceptions import ExchangeException


@pytest.fixture
def clock(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(mod, 'time', time, raising=False)
    monkeypatch.setattr(mod, 'get_monotonic_timestamp_ns', lambda: int(now[0] * 1_000_000_000), raising=False)
    monkeypatch.setattr(mod.time, 'sleep', lambda seconds: now.__setitem__(0, now[0] + seconds))
    monkeypatch.setattr(mod, '_funding_bills_gates', {}, raising=False)
    return now


def client(owner='test'):
    value = object.__new__(mod.OkexClient)
    value.account_id = owner
    value._send_request = Mock(return_value={'code': '0', 'data': []})
    return value


def test_next_page_and_new_client_for_same_account_wait_after_completion(clock):
    first, second = client(), client()
    starts = []
    for value in (first, second):
        value._send_request.side_effect = lambda *a, **kw: starts.append(clock[0]) or {'code': '0', 'data': []}
    first.get_funding_bills_history(limit=100, deadline_monotonic=110)
    second.get_funding_bills_history(after='bill-1', deadline_monotonic=110)
    assert starts == [100, 101]
    assert second._send_request.call_args.kwargs['params'] == {
        'pagingType': '2', 'limit': 100, 'after': 'bill-1'}


def test_failed_request_consumes_interval_without_automatic_replay(clock):
    value = client()
    value._send_request.side_effect = [ExchangeException('http err:429'), {'code': '0', 'data': []}]
    with pytest.raises(ExchangeException):
        value.get_funding_bills_history()
    assert value._send_request.call_count == 1
    value.get_funding_bills_history()
    assert clock[0] == 101


@pytest.mark.parametrize('deadline', [99, 100, 100.5, 101])
def test_expired_or_insufficient_deadline_never_sends_another_get(clock, deadline):
    value = client()
    value.get_funding_bills_history()
    with pytest.raises(TimeoutError):
        value.get_funding_bills_history(deadline_monotonic=deadline)
    assert value._send_request.call_count == 1
    assert clock[0] == 100


def test_wait_overshoot_rechecks_deadline_before_dispatch(clock, monkeypatch):
    value = client()
    value.get_funding_bills_history()
    monkeypatch.setattr(mod.time, 'sleep', lambda seconds: clock.__setitem__(0, 103))
    with pytest.raises(TimeoutError):
        value.get_funding_bills_history(deadline_monotonic=102)
    assert value._send_request.call_count == 1


def test_threads_cannot_dispatch_same_account_reads_concurrently(clock):
    first, second = client(), client()
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    starts = []

    def blocked(*args, **kwargs):
        starts.append(clock[0])
        entered.set()
        assert release.wait(2)
        return {'code': '0', 'data': []}

    first._send_request.side_effect = blocked
    second._send_request.side_effect = lambda *a, **kw: starts.append(clock[0]) or {'code': '0', 'data': []}
    one = threading.Thread(target=first.get_funding_bills_history)
    two = threading.Thread(target=lambda: (second.get_funding_bills_history(), finished.set()))
    one.start()
    assert entered.wait(2)
    two.start()
    dispatched_while_first_in_flight = finished.wait(0.02)
    release.set()
    one.join(2)
    two.join(2)
    assert not one.is_alive() and not two.is_alive()
    assert not dispatched_while_first_in_flight
    assert starts == [100, 101]
