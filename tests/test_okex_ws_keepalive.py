"""OKX application heartbeat and authenticated subscription readiness."""
import json
from threading import Event, Thread
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from websocket._abnf import ABNF

from pytradekit.gateway.websocket.base_ws_manager import _TransportEvidence
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
    manager.ws.send.side_effect = lambda *args, **kwargs: manager._on_message(manager.ws, 'pong')
    manager.reconnect = Mock()
    manager._ping(20)
    assert manager.ws.send.call_count == 2
    assert manager._pending_pong_socket is None
    manager.reconnect.assert_not_called()


def test_missing_pong_requests_recovery_for_the_ping_socket(monkeypatch):
    manager = make_manager()
    socket = manager.ws
    monkeypatch.setattr(manager._stop_event, 'wait', Mock(side_effect=[False, False, True]))
    manager.reconnect = Mock()
    manager._ping(20)
    socket.send.assert_called_once_with('ping', opcode=ABNF.OPCODE_TEXT)
    manager.reconnect.assert_called_once_with(socket)


def test_missing_pong_can_request_recovery_while_another_send_is_blocked():
    manager = make_manager()
    manager._pending_pong_socket = manager.ws
    requested = Event()
    manager.reconnect = Mock(side_effect=lambda ws: requested.set())
    heartbeat = Thread(target=manager._send_heartbeat)
    try:
        with manager._semaphore:
            heartbeat.start()
            assert requested.wait(1), 'heartbeat timeout waited on send I/O'
    finally:
        heartbeat.join(timeout=1)
    assert not heartbeat.is_alive()
    manager.ws.send.assert_not_called()
    manager.reconnect.assert_called_once_with(manager.ws)


def test_socket_replacement_discards_old_pending_pong(monkeypatch):
    manager = make_manager()
    old = manager.ws
    replacement = SimpleNamespace(sock=SimpleNamespace(connected=True), send=Mock())
    def send_ping(*args, **kwargs):
        manager.ws = replacement
        manager._on_open(replacement)
    old.send.side_effect = send_ping
    replacement.send.side_effect = lambda *args, **kwargs: manager._on_message(replacement, 'pong')
    monkeypatch.setattr(manager._stop_event, 'wait', Mock(side_effect=[False, False, True]))
    manager.reconnect = Mock()
    manager._ping(20)
    assert old.send.call_count == replacement.send.call_count == 1
    manager.reconnect.assert_not_called()


def test_heartbeat_waiting_for_send_cannot_move_to_replacement_socket():
    manager = make_manager(is_public=True)
    old = manager.ws
    replacement = SimpleNamespace(sock=SimpleNamespace(connected=True), send=Mock())
    send_semaphore = manager._semaphore

    class ReplaceBeforeSend:
        def __enter__(self):
            send_semaphore.__enter__()
            with manager._state_lock:
                manager.ws = replacement
                manager._generation += 1
                manager._transport_evidence = _TransportEvidence(
                    manager._generation, manager._transport_log_context(),
                )
                manager._transport_diagnostics = manager._transport_evidence.state
            manager._on_open(replacement)

        def __exit__(self, *args):
            return send_semaphore.__exit__(*args)

    manager._semaphore = ReplaceBeforeSend()
    manager._send_heartbeat()  # Swap while acquiring the real send semaphore.
    old.send.assert_not_called()
    replacement.send.assert_not_called()
    assert manager._pending_pong_socket is None
    diagnostic = manager.get_connection_health()['transport']
    assert diagnostic['application_ping_attempt_ms'] is None
    assert diagnostic['application_ping_sent_ms'] is None

    manager._semaphore = send_semaphore
    manager._send_heartbeat()
    replacement.send.assert_called_once()
    assert replacement.send.call_args.args == ('ping',)
    assert manager._pending_pong_socket is replacement
    diagnostic = manager.get_connection_health()['transport']
    assert diagnostic['application_ping_attempt_ms'] is not None
    assert diagnostic['application_ping_sent_ms'] is not None


def test_started_old_open_cannot_clear_replacement_heartbeat():
    manager = make_manager(is_public=True)
    old = manager.ws
    entered, release = Event(), Event()
    on_open = manager._on_open

    def paused_open(ws):
        entered.set()
        assert release.wait(2)
        on_open(ws)

    callback = Thread(target=manager._wrap_callback(paused_open, 'open'), args=(old,))
    callback.start()
    try:
        assert entered.wait(1)
        replacement = SimpleNamespace(sock=SimpleNamespace(connected=True), send=Mock())
        with manager._state_lock:
            manager.ws = replacement
        manager._on_open(replacement)
        manager._send_heartbeat()
        assert manager._pending_pong_socket is replacement
    finally:
        release.set()
        callback.join(timeout=1)
    assert not callback.is_alive()
    assert manager._pending_pong_socket is replacement
    manager.reconnect = Mock()
    manager._send_heartbeat()
    replacement.send.assert_called_once_with('ping', opcode=ABNF.OPCODE_TEXT)
    manager.reconnect.assert_called_once_with(replacement)


def test_old_ping_send_failure_requests_only_the_old_socket(monkeypatch):
    manager = make_manager()
    old = manager.ws
    def failed_old_send(*args, **kwargs):
        manager.ws = SimpleNamespace(sock=SimpleNamespace(connected=True))
        manager._on_open(manager.ws)
        raise OSError('synthetic old transport failure')
    monkeypatch.setattr(manager._stop_event, 'wait', Mock(side_effect=[False, True]))
    old.send.side_effect = failed_old_send
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
    socket = manager.ws
    manager.close()
    manager._ping(20)
    socket.send.assert_not_called()


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


def test_failed_application_ping_is_attempt_evidence_without_successful_send():
    manager = make_manager()
    manager.ws.send.side_effect = OSError(104, 'test-private-ping-error')
    manager.reconnect = Mock()
    manager._send_heartbeat()
    diagnostic = manager.get_connection_health()['transport']
    assert diagnostic['application_ping_attempt_ms'] is not None
    assert diagnostic['application_ping_sent_ms'] is None
    assert diagnostic['error_errno'] == 104
    assert 'test-private-ping-error' not in str(manager.logger.mock_calls)
    manager.reconnect.assert_called_once_with(manager.ws)


def test_fast_application_pong_does_not_turn_send_attempt_into_premature_success():
    manager = make_manager()
    during_send = []
    def immediate_pong(*args, **kwargs):
        during_send.append(manager.get_connection_health()['transport'])
        manager._on_message(manager.ws, 'pong')
    manager.ws.send.side_effect = immediate_pong
    manager._send_heartbeat()
    assert during_send[0]['application_ping_attempt_ms'] is not None
    assert during_send[0]['application_ping_sent_ms'] is None
    diagnostic = manager.get_connection_health()['transport']
    assert diagnostic['application_ping_sent_ms'] is not None
    assert diagnostic['application_pong_received_ms'] is not None
    assert manager._pending_pong_socket is None
