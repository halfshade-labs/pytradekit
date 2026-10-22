"""Keep exchange source clocks separate from local transport observations."""

import json
from queue import Queue
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from pytradekit.utils.dynamic_types import WebsocketStatus
from pytradekit.ws.binance_ws import BinanceWsManager
from pytradekit.ws.okex_ws import OkexWsManager


NOW = 1_800_000_000_000


def book(**timestamps):
    return dict({
        'e': 'bookTicker', 's': 'BTCUSDT', 'u': 1,
        'b': '100', 'a': '101', 'B': '2', 'A': '2',
    }, **timestamps)


@pytest.fixture
def clock(monkeypatch):
    value = [NOW]
    for module in ('pytradekit.gateway.websocket.base_ws_manager',
                   'pytradekit.ws.binance_ws'):
        monkeypatch.setattr(f'{module}.get_timestamp_ms', lambda: value[0])
    return value


@pytest.fixture
def binance_manager(clock):
    manager = BinanceWsManager(Mock(), queue=Queue(), is_perp=True)
    manager._set_bookticker_symbols(frozenset({'BTCUSDT'}))
    manager._generation = 1
    manager.ws = SimpleNamespace(sock=SimpleNamespace(connected=True))
    manager.status = WebsocketStatus.ACTIVE.name
    manager._on_open(manager.ws)
    return manager


@pytest.mark.parametrize('age_ms', [-1, 0, 1, 999, 1000, 1001])
def test_binance_first_quote_and_freshness_have_inclusive_ms_boundary(
    binance_manager, age_ms,
):
    manager = binance_manager
    # E is the exchange event clock; T is a distinct transaction clock.
    payload = book(E=NOW - age_ms, T=NOW)
    manager._wrap_callback(manager._on_message, 'message')(
        manager.ws, json.dumps(payload),
    )
    health = manager.get_public_feed_health()
    expected = 0 <= age_ms <= 1000
    assert health['first_valid_quote_ms'] == (NOW if expected else None)
    assert health['fresh_symbol_count'] == int(expected)
    assert manager.get_connection_health()['last_message_ms'] == NOW
    queued = manager._queue.get_nowait()
    assert queued['E'] == payload['E']
    assert queued['T'] == NOW
    assert queued['run_time_ms'] == NOW


@pytest.mark.parametrize('source', [
    0, -1, True, False, str(NOW), float(NOW),
    NOW // 1000, NOW * 1000, NOW * 1_000_000,
])
@pytest.mark.parametrize('field', ['E', 'T'])
def test_binance_invalid_or_wrong_unit_source_cannot_certify_freshness(
    binance_manager, source, field,
):
    manager = binance_manager
    manager._wrap_callback(manager._on_message, 'message')(
        manager.ws, json.dumps(book(**{field: source})),
    )
    health = manager.get_public_feed_health()
    assert health['first_valid_quote_ms'] is None
    assert health['fresh_symbol_count'] == 0
    assert manager.get_connection_health()['last_message_ms'] == NOW
    # Health classification must not rewrite the downstream payload contract.
    assert manager._queue.get_nowait()[field] == source


@pytest.mark.parametrize('is_perp', [False, True])
def test_only_spot_missing_both_source_clocks_uses_receive_time(
    binance_manager, is_perp,
):
    manager = binance_manager
    manager._is_perp = is_perp
    manager._on_message(manager.ws, json.dumps(book()))
    health = manager.get_public_feed_health()
    assert health['fresh_symbol_count'] == int(not is_perp)
    assert health['local_receive_symbol_count'] == int(not is_perp)
    assert health['first_valid_quote_timestamp_basis'] == (
        None if is_perp else 'local_receive'
    )


def test_binance_transaction_clock_is_used_when_event_clock_is_absent(
    binance_manager,
):
    manager = binance_manager
    manager._on_message(manager.ws, json.dumps(book(T=NOW - 1000)))
    health = manager.get_public_feed_health()
    assert health['first_valid_quote_ms'] == NOW
    assert health['first_valid_quote_timestamp_basis'] == 'exchange'
    assert health['fresh_symbol_count'] == 1


def test_binance_null_event_clock_uses_transaction_clock(binance_manager):
    manager = binance_manager
    manager._on_message(manager.ws, json.dumps(book(E=None, T=NOW)))
    health = manager.get_public_feed_health()
    assert health['first_valid_quote_ms'] == NOW
    assert health['first_valid_quote_timestamp_basis'] == 'exchange'


@pytest.mark.parametrize('is_perp', [False, True])
def test_binance_null_source_clocks_have_the_same_contract_as_missing_clocks(
    binance_manager, is_perp,
):
    manager = binance_manager
    manager._is_perp = is_perp
    manager._on_message(manager.ws, json.dumps(book(E=None, T=None)))
    health = manager.get_public_feed_health()
    assert health['first_valid_quote_ms'] == (None if is_perp else NOW)
    assert health['fresh_symbol_count'] == int(not is_perp)
    assert health['first_valid_quote_timestamp_basis'] == (None if is_perp else 'local_receive')


def test_binance_future_source_is_reclassified_when_query_clock_catches_up(
    binance_manager, clock,
):
    manager = binance_manager
    manager._on_message(manager.ws, json.dumps(book(E=NOW + 1)))
    health = manager.get_public_feed_health()
    assert health['fresh_symbol_count'] == 0
    assert health['first_valid_quote_ms'] is None
    clock[0] += 1
    health = manager.get_public_feed_health()
    assert health['fresh_symbol_count'] == 1
    # The historical first-valid observation is only set by a received quote.
    assert health['first_valid_quote_ms'] is None
    manager._on_message(manager.ws, json.dumps(book(E=clock[0], u=2, b='100.1')))
    assert manager.get_public_feed_health()['first_valid_quote_ms'] == clock[0]


@pytest.mark.parametrize('source', [0, True, str(NOW)])
def test_binance_present_invalid_event_clock_does_not_fall_back_to_transaction(
    binance_manager, source,
):
    manager = binance_manager
    manager._on_message(manager.ws, json.dumps(book(E=source, T=NOW)))
    health = manager.get_public_feed_health()
    assert health['first_valid_quote_ms'] is None
    assert health['fresh_symbol_count'] == 0


@pytest.mark.parametrize('case', [(999, 1), (1000, 0)])
def test_binance_snapshot_ages_exchange_source_as_well_as_local_receipt(
    binance_manager, clock, case,
):
    elapsed_ms, expected_fresh = case
    manager = binance_manager
    manager._on_message(manager.ws, json.dumps(book(E=NOW - 1)))
    clock[0] += elapsed_ms
    assert manager.get_public_feed_health()['fresh_symbol_count'] == expected_fresh


def test_binance_clock_rollback_does_not_count_a_future_local_receipt_as_fresh(
    binance_manager, clock,
):
    manager = binance_manager
    manager._on_message(manager.ws, json.dumps(book(E=NOW - 1)))
    clock[0] -= 1
    assert manager.get_public_feed_health()['fresh_symbol_count'] == 0


@pytest.mark.parametrize('source_ms', [
    NOW, NOW - 1001, NOW + 1, NOW // 1000, NOW * 1000,
])
def test_okx_raw_source_string_does_not_replace_transport_receive_clock(
    clock, source_ms,
):
    manager = OkexWsManager(Mock(), queue=Queue())
    manager.ws = SimpleNamespace(sock=SimpleNamespace(connected=True))
    manager.status = WebsocketStatus.ACTIVE.name
    item = {
        'instId': 'BTC-USDT', 'bidPx': '100', 'askPx': '101',
        'bidSz': '2', 'askSz': '2', 'ts': str(source_ms),
    }
    manager._wrap_callback(manager._on_message, 'message')(
        manager.ws,
        json.dumps({'arg': {'channel': 'tickers'}, 'data': [item]}),
    )
    assert manager._queue.get_nowait() == item
    health = manager.get_connection_health()
    assert health['last_message_ms'] == NOW
    assert health['transport']['callback_started_ms'] == NOW
    assert health['transport']['callback_finished_ms'] == NOW
    # OKX reports transport connectivity; source freshness belongs downstream.
    assert health['connected']
    assert 'fresh_symbol_count' not in health
