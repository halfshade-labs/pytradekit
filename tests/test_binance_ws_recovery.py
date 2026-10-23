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
    with patch.object(manager, '_wait_reconnect_delay') as sleep, patch(
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
    with patch('pytradekit.ws.binance_ws.get_monotonic_timestamp_ns', return_value=61000000000), patch.object(manager, '_wait_reconnect_delay') as sleep, patch('pytradekit.gateway.websocket.ws_manager.WsManager._recovery'):
        manager._recovery()
        manager._recovery()
    assert [call.args[0] for call in sleep.call_args_list] == [1, 2]


def test_stop_during_backoff_does_not_reopen_connection():
    manager = make_manager()
    def stop(_delay):
        manager.status = WebsocketStatus.STOP.name
    with patch.object(manager, '_wait_reconnect_delay', side_effect=stop), patch(
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


def test_public_reconnect_jitter_is_bounded_and_private_backoff_is_unchanged():
    manager = make_manager()
    manager._public_bookticker = True
    with patch('pytradekit.ws.binance_ws.random.randint', return_value=250), patch.object(
        manager, '_wait_reconnect_delay'
    ) as wait, patch('pytradekit.gateway.websocket.ws_manager.WsManager._recovery'):
        manager._recovery()
        manager._reconnect_attempt = 6
        manager._recovery()
    assert [call.args[0] for call in wait.call_args_list] == [1.25, 30]


def test_public_control_pacing_reserves_headroom_and_stop_interrupts_wait():
    manager = make_manager()
    manager._public_bookticker = True
    with patch('pytradekit.ws.binance_ws.get_monotonic_timestamp_ns', return_value=1_000_000_000), patch.object(
        manager._stop_event, 'wait', return_value=False
    ) as wait:
        manager._pace_public_control()
        manager._pace_public_control()
    assert [call.args[0] for call in wait.call_args_list] == [0, 0.5]
    manager._stop_event.set()
    import pytest
    from pytradekit.utils.exceptions import ExchangeException
    with pytest.raises(ExchangeException):
        manager._pace_public_control()


def test_public_reconnect_cap_still_has_jitter():
    manager = make_manager()
    manager._public_bookticker = True
    manager._reconnect_attempt = 6
    with patch('pytradekit.ws.binance_ws.random.randint', return_value=0), patch.object(
        manager, '_wait_reconnect_delay'
    ) as wait, patch('pytradekit.gateway.websocket.ws_manager.WsManager._recovery'):
        manager._recovery()
    wait.assert_called_once_with(29.75)


def test_real_public_update_and_replay_sends_obey_pacing_and_stop(monkeypatch):
    import json
    from types import SimpleNamespace
    import pytest
    from pytradekit.utils.exceptions import ExchangeException
    manager = make_manager()
    manager._public_bookticker = True
    manager.status = WebsocketStatus.ACTIVE.name
    clock = SimpleNamespace(ns=0, stopped=False)
    class Stop:
        def is_set(self):
            return clock.stopped
        def wait(self, seconds):
            if not clock.stopped:
                clock.ns += round(seconds * 1_000_000_000)
            return clock.stopped
    manager._stop_event = Stop()
    broker = []
    manager.ws = SimpleNamespace(sock=SimpleNamespace(connected=True),
        send=lambda message, **kwargs: broker.append((clock.ns, json.loads(message))))
    monkeypatch.setattr('pytradekit.ws.binance_ws.get_monotonic_timestamp_ns', lambda: clock.ns)
    manager.start_subscribe(['btcusdt@bookTicker'])
    manager._set_bookticker_symbols(frozenset(('BTCUSDT',)))
    manager.update_bookticker_stream(('ETHUSDT',))
    manager._reconnect_streams()
    assert [stamp for stamp, _ in broker] == [0, 500_000_000, 1_000_000_000, 1_500_000_000]
    assert [message['method'] for _, message in broker] == ['SUBSCRIBE', 'SUBSCRIBE', 'UNSUBSCRIBE', 'SUBSCRIBE']
    clock.stopped = True
    with pytest.raises(ExchangeException):
        manager.update_bookticker_stream(('SOLUSDT',))
    assert len(broker) == 4
