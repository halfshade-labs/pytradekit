"""Bound Binance reconnect bursts and preserve shutdown semantics."""
from unittest.mock import Mock, patch

from pytradekit.utils.dynamic_types import WebsocketStatus
from pytradekit.ws.binance_ws import BinanceWsManager


def make_manager():
    manager = BinanceWsManager(Mock())
    manager.status = WebsocketStatus.RECOVERY.name
    return manager


def test_short_connections_back_off_and_cap_without_changing_subscriptions():
    manager = make_manager()
    manager._subs = [{'method': 'SUBSCRIBE', 'params': ['btcusdt@bookTicker']}]
    with patch('pytradekit.ws.binance_ws.sleep_min_time') as sleep, patch(
        'pytradekit.gateway.websocket.ws_manager.WsManager._recovery'
    ) as recover:
        for _ in range(9):
            manager._recovery()
    assert [call.args[0] for call in sleep.call_args_list] == [
        1, 2, 4, 8, 16, 30, 30, 30, 30,
    ]
    assert recover.call_count == 9
    assert manager._subs[0]['params'] == ['btcusdt@bookTicker']


def test_stable_connection_resets_backoff_only_once():
    manager = make_manager()
    manager._reconnect_attempt = 6
    manager._connection_opened_ms = 1000
    with patch('pytradekit.ws.binance_ws.get_monotonic_timestamp_ns', return_value=61000000000), patch(
        'pytradekit.ws.binance_ws.sleep_min_time'
    ) as sleep, patch('pytradekit.gateway.websocket.ws_manager.WsManager._recovery'):
        manager._recovery()
        manager._recovery()
    assert [call.args[0] for call in sleep.call_args_list] == [1, 2]


def test_stop_during_backoff_does_not_reopen_connection():
    manager = make_manager()
    def stop(_delay):
        manager.status = WebsocketStatus.STOP.name
    with patch('pytradekit.ws.binance_ws.sleep_min_time', side_effect=stop), patch(
        'pytradekit.gateway.websocket.ws_manager.WsManager._recovery'
    ) as recover:
        manager._recovery()
        manager._recovery()
    recover.assert_not_called()


def test_open_records_lifetime_without_resetting_short_connection_failures():
    manager = make_manager()
    manager._reconnect_attempt = 4
    with patch('pytradekit.ws.binance_ws.get_monotonic_timestamp_ns', return_value=5000000000):
        manager._on_open(None)
    assert manager._connection_opened_ms == 5000
    assert manager._reconnect_attempt == 4
