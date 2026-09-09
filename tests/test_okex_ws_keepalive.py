"""OKX application heartbeat and authenticated subscription readiness."""
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from pytradekit.utils.dynamic_types import OkexAuxiliary, WebsocketStatus
from pytradekit.ws.okex_ws import OkexWsManager


def make_manager(is_public=False):
    manager = OkexWsManager(
        Mock(), api_key='TEST_KEY', api_secret='TEST_SECRET',
        passphrase='TEST_PASSPHRASE', is_public=is_public,
    )
    manager.ws = SimpleNamespace(sock=SimpleNamespace(connected=True), send=Mock())
    manager.status = WebsocketStatus.ACTIVE.name
    manager._socket_thread = Mock()
    manager._socket_thread.is_alive.return_value = True
    manager._monitorThread = Mock()
    manager._monitorThread.is_alive.return_value = True
    return manager


def test_private_heartbeat_interval_is_below_okx_idle_disconnect_limit():
    assert OkexAuxiliary.ws_ping_sleep.value == 20


def test_private_health_requires_login_and_orders_subscription_ack():
    manager = make_manager()
    manager.send_json = Mock()
    assert not manager.get_connection_health()['connected']
    manager._on_message(manager.ws, json.dumps({'event': 'login', 'code': '0'}))
    assert manager.get_connection_health()['logged_on']
    assert not manager.get_connection_health()['connected']
    manager._on_message(manager.ws, json.dumps({
        'event': 'subscribe', 'arg': {'channel': 'orders', 'instType': 'SPOT'},
    }))
    health = manager.get_connection_health()
    assert health['connected'] and health['subscribed']
    assert health['last_message_ms'] is None  # No fill is required for readiness.


def test_unrelated_or_unsolicited_ack_cannot_make_private_stream_ready():
    manager = make_manager()
    orders_ack = json.dumps({'event': 'subscribe', 'arg': {'channel': 'orders', 'instType': 'SPOT'}})
    manager._on_message(manager.ws, orders_ack)
    assert not manager.get_connection_health()['connected']
    manager.send_json = Mock()
    manager._on_message(manager.ws, json.dumps({'event': 'login', 'code': '0'}))
    manager._on_message(manager.ws, json.dumps({'event': 'subscribe', 'arg': {'channel': 'tickers'}}))
    assert not manager.get_connection_health()['connected']


def test_private_recovery_logs_in_before_replaying_orders():
    manager = make_manager()
    manager._subs = [{'op': 'subscribe', 'args': [{'channel': 'orders', 'instType': 'SPOT'}]}]
    manager.send_json = Mock()
    manager._reconnect_streams()
    assert [call.args[0]['op'] for call in manager.send_json.call_args_list] == ['login']
    manager._on_message(manager.ws, json.dumps({'event': 'login', 'code': '0'}))
    assert [call.args[0]['op'] for call in manager.send_json.call_args_list] == ['login', 'subscribe']


def test_public_health_and_subscription_replay_do_not_require_login():
    manager = make_manager(is_public=True)
    manager._subs = [{'op': 'subscribe', 'args': [{'channel': 'tickers', 'instId': 'BTC-USDT'}]}]
    manager.send_json = Mock()
    manager._reconnect_streams()
    manager.send_json.assert_called_once_with(manager._subs[0])
    assert manager.get_connection_health()['connected']


def test_immediate_pong_is_not_overwritten_by_send_completion(monkeypatch):
    manager = make_manager()
    monkeypatch.setattr(manager._stop_event, 'wait', Mock(side_effect=[False, False, True]))
    manager.send = Mock(side_effect=lambda _: manager._on_message(manager.ws, 'pong'))
    manager.reconnect = Mock()
    manager._ping(20)
    assert manager.send.call_count == 2
    assert manager._pending_pong_socket is None
    manager.reconnect.assert_not_called()


def test_missing_pong_requests_recovery_for_the_ping_socket(monkeypatch):
    manager = make_manager()
    socket = manager.ws
    monkeypatch.setattr(manager._stop_event, 'wait', Mock(side_effect=[False, False, True]))
    manager.send = Mock()
    manager.reconnect = Mock()
    manager._ping(20)
    manager.send.assert_called_once_with('ping')
    manager.reconnect.assert_called_once_with(socket)


def test_socket_replacement_discards_old_pending_pong(monkeypatch):
    manager = make_manager()
    old = manager.ws
    def send_ping(_):
        if manager.ws is old:
            manager.ws = SimpleNamespace(sock=SimpleNamespace(connected=True), send=Mock())
            manager._on_open(manager.ws)
        else:
            manager._on_message(manager.ws, 'pong')
    monkeypatch.setattr(manager._stop_event, 'wait', Mock(side_effect=[False, False, True]))
    manager.send = Mock(side_effect=send_ping)
    manager.reconnect = Mock()
    manager._ping(20)
    assert manager.send.call_count == 2
    manager.reconnect.assert_not_called()


def test_old_ping_send_failure_requests_only_the_old_socket(monkeypatch):
    manager = make_manager()
    old = manager.ws
    def failed_old_send(_):
        manager.ws = SimpleNamespace(sock=SimpleNamespace(connected=True))
        manager._on_open(manager.ws)
        raise OSError('synthetic old transport failure')
    monkeypatch.setattr(manager._stop_event, 'wait', Mock(side_effect=[False, True]))
    manager.send = Mock(side_effect=failed_old_send)
    manager.reconnect = Mock()
    manager._ping(20)
    manager.reconnect.assert_called_once_with(old)
    assert manager._pending_pong_socket is None


def test_open_resets_session_and_old_callbacks_are_ignored():
    manager = make_manager()
    manager._logged_on = True
    manager._orders_subscribed = True
    manager._pending_pong_socket = manager.ws
    old = manager.ws
    old_pong = manager._wrap_callback(manager._on_message, 'message')
    manager.ws = SimpleNamespace(sock=SimpleNamespace(connected=True))
    manager._on_open(manager.ws)
    assert not manager.get_connection_health()['connected']
    assert manager._pending_pong_socket is None
    manager._pending_pong_socket = manager.ws
    old_pong(old, 'pong')
    assert manager._pending_pong_socket is manager.ws


def test_stop_wakes_keepalive_without_another_ping(monkeypatch):
    manager = make_manager()
    manager.close()
    manager.send = Mock()
    manager._ping(20)
    manager.send.assert_not_called()


@pytest.mark.parametrize('event', [
    {'event': 'login', 'code': '60009'},
    {'event': 'error', 'code': '60011'},
    {'event': 'unsubscribe', 'arg': {'channel': 'orders', 'instType': 'SPOT'}},
])
def test_rejected_or_removed_private_subscription_cannot_remain_healthy(event):
    manager = make_manager()
    manager._logged_on = True
    manager._orders_subscribed = True
    manager.reconnect = Mock()
    manager._on_message(manager.ws, json.dumps(event))
    assert not manager.get_connection_health()['connected']


@pytest.mark.parametrize('ack', [
    {'event': 'subscribe', 'arg': {'channel': 'orders', 'instType': 'SWAP'}},
    {'event': 'subscribe', 'arg': {'channel': 'orders'}},
    {'event': 'subscribe', 'code': '60011', 'arg': {'channel': 'orders', 'instType': 'SPOT'}},
])
def test_non_target_or_failed_orders_ack_does_not_mark_ready(ack):
    manager = make_manager()
    manager.send_json = Mock()
    manager._on_message(manager.ws, json.dumps({'event': 'login', 'code': '0'}))
    manager._on_message(manager.ws, json.dumps(ack))
    assert not manager.get_connection_health()['connected']
    assert not manager.get_connection_health()['subscribed']
