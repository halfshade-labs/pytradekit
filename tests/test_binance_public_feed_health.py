"""Public-feed recovery must require current-generation, usable quotations."""

import json
from queue import Queue
from threading import Event, Thread
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from pytradekit.utils.dynamic_types import WebsocketStatus
from pytradekit.ws.binance_ws import BinanceWsManager


NOW = 1_800_000_000_000


def book(update_id=1, **changes):
    return dict({
        'e': 'bookTicker', 's': 'BTCUSDT', 'u': update_id,
        'b': '100', 'a': '101', 'B': '2', 'A': '2', 'E': NOW, 'T': NOW,
    }, **changes)


@pytest.fixture
def manager(monkeypatch):
    monkeypatch.setattr('pytradekit.ws.binance_ws.get_timestamp_ms', lambda: NOW)
    value = BinanceWsManager(Mock(), queue=Queue(), is_perp=True)
    # Keep the fixture compatible with the pre-fix manager so the two stale
    # callback tests can replay against the immutable baseline revision.
    value._bookticker_symbols = frozenset({'BTCUSDT', 'ETHUSDT'})
    value._public_bookticker = True
    value._generation = 1
    value.ws = SimpleNamespace(sock=SimpleNamespace(connected=True), send=Mock())
    value.status = WebsocketStatus.ACTIVE.name
    value._on_open(value.ws)
    return value


def test_retired_socket_cannot_deliver_buffered_quotes_during_backoff(manager):
    old_socket = manager.ws
    manager.reconnect(old_socket)
    # The lifecycle worker clears the request before starting Binance backoff.
    manager._needRecovery.clear()
    manager.status = WebsocketStatus.RECOVERY.name
    manager._wrap_callback(manager._on_message, 'message')(old_socket, json.dumps(book()))
    assert manager._queue.empty()


def test_inflight_callback_cannot_publish_after_reconnect_request(manager, monkeypatch):
    original = json.loads

    def retire_during_parse(*args):
        result = original(*args)
        manager.reconnect(manager.ws)
        return result

    monkeypatch.setattr('pytradekit.ws.binance_ws.json.loads', retire_during_parse)
    manager._on_message(manager.ws, json.dumps(book()))
    assert manager._queue.empty()


def test_open_and_subscription_ack_are_not_valid_quotes(manager):
    manager.start_subscribe(['btcusdt@bookTicker', 'ethusdt@bookTicker'])
    manager._on_message(manager.ws, json.dumps({'id': 0, 'result': None}))
    health = manager.get_public_feed_health()
    assert health['connected']
    assert health['subscription_ack_ms'] == NOW
    assert health['first_valid_quote_ms'] is None
    assert health['first_valid_quote_delay_ms'] is None
    assert health['fresh_symbol_count'] == 0
    assert health['unavailable_symbols_sample'] == ['BTCUSDT', 'ETHUSDT']


def test_spot_without_exchange_clock_reports_local_receive_basis(manager):
    manager._is_perp = False
    message = book()
    del message['E']
    del message['T']
    manager._on_message(manager.ws, json.dumps(message))
    health = manager.get_public_feed_health()
    assert health['first_valid_quote_timestamp_basis'] == 'local_receive'
    assert health['local_receive_symbol_count'] == 1


@pytest.mark.parametrize('is_perp', [False, True])
def test_distinct_same_price_public_updates_preserve_quantity_transitions(manager, monkeypatch, is_perp):
    """A new ordered BBO remains a transition when only its size changed."""
    manager._is_perp = is_perp
    received = NOW
    monkeypatch.setattr('pytradekit.ws.binance_ws.get_timestamp_ms', lambda: received)
    for update in range(1, 5):
        received = NOW + update
        message = book(update, B=str(update), A=str(update + 1), E=received, T=received)
        manager._on_message(manager.ws, json.dumps(message))
    frames = [manager._queue.get_nowait() for _ in range(manager._queue.qsize())]
    assert [frame['u'] for frame in frames] == [1, 2, 3, 4]
    assert [frame['B'] for frame in frames] == ['1', '2', '3', '4']


def test_same_price_fresh_source_update_is_not_hidden_by_sampling(manager, monkeypatch):
    """Near-expiry old source data must not hide a newer raw observation."""
    received = NOW
    monkeypatch.setattr('pytradekit.ws.binance_ws.get_timestamp_ms', lambda: received)
    manager._on_message(manager.ws, json.dumps(book(1, E=NOW - 999, T=NOW - 999)))
    received += 1
    manager._on_message(manager.ws, json.dumps(book(2, E=received, T=received)))
    received += 1
    health = manager.get_public_feed_health()
    assert health['fresh_symbol_count'] == 1
    assert manager._queue.qsize() == 2


@pytest.mark.parametrize('changes', [
    {'b': 'NaN'}, {'a': '99'}, {'B': '0'}, {'E': NOW - 1001}, {'E': NOW + 1},
])
def test_invalid_or_stale_quote_does_not_claim_recovery(manager, changes):
    manager._on_message(manager.ws, json.dumps(book(**changes)))
    assert manager.get_public_feed_health()['first_valid_quote_ms'] is None


def test_reconnect_clears_quote_coverage_and_rejects_already_queued_generation(manager):
    manager._on_message(manager.ws, json.dumps(book()))
    queued = manager._queue.get_nowait()
    health = manager.get_public_feed_health()
    assert health['first_valid_quote_ms'] == NOW
    assert health['fresh_symbol_count'] == 1
    assert manager.is_current_bookticker_message(queued)
    manager._on_error(manager.ws, RuntimeError('private URL must not be logged'))
    manager._on_close(manager.ws, 1006, 'private close reason')
    assert not manager.is_current_bookticker_message(queued)
    assert manager.get_public_feed_health()['disconnect_count'] == 1
    manager._generation += 1
    manager.ws = SimpleNamespace(sock=SimpleNamespace(connected=True), send=Mock())
    manager._needRecovery.clear()
    manager._on_open(manager.ws)
    health = manager.get_public_feed_health()
    assert health['first_valid_quote_ms'] is None
    assert health['fresh_symbol_count'] == 0
    assert not manager.is_current_bookticker_message(queued)
    # Same BBO and update ID on the new connection must not inherit dedup state.
    manager._on_message(manager.ws, json.dumps(book()))
    assert manager.is_current_bookticker_message(manager._queue.get_nowait())


def test_private_message_delivery_is_not_changed_by_public_quote_retirement(manager):
    manager._bookticker_symbols = frozenset()
    manager._public_bookticker = False
    manager.reconnect(manager.ws)
    payload = {'e': 'ORDER_TRADE_UPDATE', 'o': {'s': 'BTCUSDT'}}
    manager._on_message(manager.ws, json.dumps(payload))
    assert manager._queue.get_nowait() == payload


def test_wrong_subscription_ack_cannot_certify_replay(manager):
    manager.start_subscribe(['btcusdt@bookTicker'])
    manager._on_message(manager.ws, json.dumps({'id': 999, 'result': None}))
    assert manager.get_public_feed_health()['subscription_ack_ms'] is None


def test_on_open_does_not_erase_a_subscription_sent_after_transport_connect(manager):
    manager._generation += 1
    manager.ws = SimpleNamespace(sock=SimpleNamespace(connected=True), send=Mock())
    manager.send_json({'method': 'SUBSCRIBE', 'params': ['btcusdt@bookTicker']})
    manager._on_open(manager.ws)
    manager._on_message(manager.ws, json.dumps({'id': 0, 'result': None}))
    assert manager.get_public_feed_health()['subscription_ack_ms'] == NOW


def test_health_contains_no_payload_error_or_account_identifiers(manager):
    secret = 'TEST_PRIVATE_DETAIL_NEVER_LOG'
    manager._on_error(manager.ws, RuntimeError(secret))
    manager._on_close(manager.ws, 1006, secret)
    text = str(manager.get_public_feed_health()) + str(manager.logger.mock_calls)
    assert secret not in text
    assert 'RuntimeError' in text


def test_recovery_latency_uses_monotonic_time_and_stays_unknown_without_quotes(manager, monkeypatch):
    clock = [1_000_000_000]
    monkeypatch.setattr('pytradekit.ws.bookticker_health.get_monotonic_timestamp_ns', lambda: clock[0])
    manager.reconnect(manager.ws)
    clock[0] += 2_000_000
    manager._generation += 1
    manager.ws = SimpleNamespace(sock=SimpleNamespace(connected=True), send=Mock())
    manager._on_open(manager.ws)
    health = manager.get_public_feed_health()
    assert health['socket_rebuild_ms'] == 2
    assert health['recovery_to_quote_ms'] is None
    clock[0] += 3_000_000
    manager._on_message(manager.ws, json.dumps(book()))
    health = manager.get_public_feed_health()
    assert health['first_valid_quote_delay_ms'] == 3
    assert health['recovery_to_quote_ms'] == 5


def test_empty_subscription_does_not_reenable_delivery_of_untracked_quotes(manager):
    manager.update_bookticker_stream([])
    manager._on_message(manager.ws, json.dumps(book()))
    assert manager._queue.empty()
    assert manager.get_public_feed_health()['expected_symbol_count'] == 0


def test_removed_targets_are_pruned_and_coverage_samples_are_bounded(manager):
    manager._on_message(manager.ws, json.dumps(book()))
    manager._set_bookticker_symbols(frozenset(f'SYNTHETIC{index}USDT' for index in range(100)))
    health = manager.get_public_feed_health()
    assert health['observed_symbol_count'] == 0
    assert health['unavailable_symbol_count'] == 100
    assert len(health['unavailable_symbols_sample']) == 50
    assert health['unavailable_symbols_truncated']
    assert not manager._bookticker_health.quotes


def test_missing_subscription_responses_have_bounded_state_and_never_claim_ack(manager):
    for _ in range(70):
        manager.send_json({'method': 'SUBSCRIBE', 'params': ['btcusdt@bookTicker']})
    manager._on_message(manager.ws, json.dumps({'id': 69, 'result': None}))
    health = manager.get_public_feed_health()
    assert health['subscription_tracking_lost']
    assert health['subscription_ack_ms'] is None
    assert len(manager._bookticker_health._subscription_ids) <= 64


@pytest.mark.parametrize('event_name, callback_name, args', [
    ('open', '_on_open', ()),
    ('', '_on_error', (RuntimeError('obsolete error'),)),
    ('', '_on_close', (1006, 'obsolete close')),
])
def test_callback_retired_after_wrapper_guard_cannot_mutate_new_generation(
    manager, event_name, callback_name, args,
):
    entered, release = Event(), Event()
    original_callback = getattr(manager, callback_name)

    def paused_callback(ws, *values):
        entered.set()
        assert release.wait(1)
        original_callback(ws, *values)

    wrapped = manager._wrap_callback(paused_callback, event_name)
    callback_thread = Thread(target=wrapped, args=(manager.ws, *args))
    callback_thread.start()
    try:
        assert entered.wait(1)
        manager._generation += 1
        manager.ws = SimpleNamespace(sock=SimpleNamespace(connected=True), send=Mock())
        manager._on_open(manager.ws)
        manager._on_message(manager.ws, json.dumps(book()))
        opened_ms = manager._connection_opened_ms
        current_health = manager.get_public_feed_health()
        release.set()
        callback_thread.join(timeout=1)
        assert not callback_thread.is_alive()
        assert manager.get_public_feed_health() == current_health
        assert manager._connection_opened_ms == opened_ms
        assert not manager._needRecovery.is_set()
    finally:
        release.set()
        callback_thread.join(timeout=1)


def test_failed_handshake_does_not_count_as_established_socket_disconnect(manager):
    manager._generation += 1
    manager.ws = SimpleNamespace(sock=None, send=Mock())
    manager.reconnect(manager.ws)
    health = manager.get_public_feed_health()
    assert health['retired_generation_count'] == 1
    assert health['disconnect_count'] == 0
    assert health['last_open_ms'] is None
    assert health['first_valid_quote_ms'] is None


def test_late_open_of_retired_current_socket_preserves_health_and_connection_age(manager, monkeypatch):
    manager.reconnect(manager.ws)
    retired_health = manager.get_public_feed_health()
    opened_ms = manager._connection_opened_ms
    monkeypatch.setattr(
        'pytradekit.ws.binance_ws.get_monotonic_timestamp_ns',
        lambda: (opened_ms + 60_000) * 1_000_000,
    )
    late_open = manager._wrap_callback(manager._on_open, 'open')
    late_open(manager.ws)
    assert manager.get_public_feed_health() == retired_health
    assert manager._connection_opened_ms == opened_ms
    assert manager._connection_age_ms() == 60_000


def test_subscription_update_during_replacement_replays_the_final_target_set(manager):
    """An added symbol sent on the old socket must be replayed on its replacement."""
    manager._set_bookticker_symbols(frozenset({'BTCUSDT'}))
    manager._subs = [{'method': 'SUBSCRIBE', 'params': ['btcusdt@bookTicker']}]
    old_socket = manager.ws
    added_on_old, release_update, replay_started, replay_done = (Event() for _ in range(4))
    replacement = SimpleNamespace(sock=SimpleNamespace(connected=True), send=Mock())
    original_send = old_socket.send
    errors = []

    def pause_after_old_send(message, **kwargs):
        original_send(message, **kwargs)
        if json.loads(message)['method'] == 'SUBSCRIBE':
            added_on_old.set()
            assert release_update.wait(2)

    old_socket.send = pause_after_old_send

    def update_targets():
        try:
            manager.update_bookticker_stream(['ETHUSDT'])
        except Exception as error:
            errors.append(error)

    def replay_replacement():
        try:
            with manager._state_lock:
                manager._generation += 1
                manager.ws = replacement
            manager._on_open(replacement)
            replay_started.set()
            manager._reconnect_streams()
        except Exception as error:
            errors.append(error)
        finally:
            replay_done.set()

    updater, replay = Thread(target=update_targets), Thread(target=replay_replacement)
    updater.start()
    try:
        assert added_on_old.wait(1)
        replay.start()
        assert replay_started.wait(1)
        # On the buggy implementation replay can finish with the obsolete set.
        # The repaired implementation keeps replay behind the target update.
        replay_done.wait(0.1)
    finally:
        release_update.set()
        updater.join(timeout=2)
        if replay.ident is not None:
            replay.join(timeout=2)
    assert not updater.is_alive() and not replay.is_alive()
    assert not errors
    subscriptions = set()
    for call in replacement.send.call_args_list:
        message = json.loads(call.args[0])
        if message['method'] == 'SUBSCRIBE':
            subscriptions.update(message['params'])
        else:
            subscriptions.difference_update(message['params'])
    assert subscriptions == {'ethusdt@bookTicker'}
    manager._on_message(replacement, json.dumps(book(s='ETHUSDT')))
    health = manager.get_public_feed_health()
    assert health['expected_symbol_count'] == health['fresh_symbol_count'] == 1
    assert health['first_valid_quote_ms'] == NOW


def test_concurrent_target_updates_leave_only_the_final_subscriptions(manager):
    manager._set_bookticker_symbols(frozenset({'BTCUSDT'}))
    manager._subs = [{'method': 'SUBSCRIBE', 'params': ['btcusdt@bookTicker']}]
    subscriptions = {'btcusdt@bookTicker'}
    first_added, release_first, second_started, second_done = (Event() for _ in range(4))
    errors = []

    def broker_send(raw, **kwargs):
        message = json.loads(raw)
        if message['method'] == 'SUBSCRIBE':
            subscriptions.update(message['params'])
        else:
            subscriptions.difference_update(message['params'])
        if message['params'] == ['ethusdt@bookTicker'] and message['method'] == 'SUBSCRIBE':
            first_added.set()
            assert release_first.wait(2)

    manager.ws.send = broker_send

    def update(target, completed):
        try:
            if target == 'SOLUSDT':
                second_started.set()
            manager.update_bookticker_stream([target])
        except Exception as error:
            errors.append(error)
        finally:
            completed.set()

    first = Thread(target=update, args=('ETHUSDT', Event()))
    second = Thread(target=update, args=('SOLUSDT', second_done))
    first.start()
    try:
        assert first_added.wait(1)
        second.start()
        assert second_started.wait(1)
        second_done.wait(0.1)
    finally:
        release_first.set()
        first.join(timeout=2)
        if second.ident is not None:
            second.join(timeout=2)
    assert not first.is_alive() and not second.is_alive()
    assert not errors
    assert subscriptions == set(manager._subs[0]['params'])
    assert subscriptions == {'solusdt@bookTicker'}
