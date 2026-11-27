"""OKX application heartbeat and authenticated subscription readiness."""
import json
import base64
import hashlib
import socket
import ssl
import subprocess
from socket import socketpair, timeout
from threading import Event, Thread
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from websocket import WebSocket, WebSocketApp
from websocket._abnf import ABNF

from pytradekit.gateway.websocket.base_ws_manager import _TransportEvidence
from pytradekit.gateway.websocket import base_ws_manager
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
    manager._reconnect_streams()
    assert [json.loads(call.args[0])['op'] for call in manager.ws.send.call_args_list] == ['login']
    manager._on_message(manager.ws, json.dumps({'event': 'login', 'code': '0'}))
    sent = [json.loads(call.args[0]) for call in manager.ws.send.call_args_list]
    assert [message['op'] for message in sent] == ['login', 'subscribe']
    assert [message['id'] for message in sent] == [0, 1]


def test_public_health_and_subscription_replay_do_not_require_login():
    manager = make_manager(is_public=True)
    manager._subs = [{'op': 'subscribe', 'args': [{'channel': 'tickers', 'instId': 'BTC-USDT'}]}]
    manager.send_json = Mock()
    manager._reconnect_streams()
    manager.send_json.assert_called_once_with(manager._subs[0])
    assert manager.get_connection_health()['connected']


def test_public_default_uses_okx_port_443():
    assert make_manager(is_public=True)._get_url() == "wss://ws.okx.com/ws/v5/public"


@pytest.mark.parametrize('is_public', [True, False])
def test_close_interrupts_pending_upgrade_only_for_public_stream(is_public):
    manager = OkexWsManager(Mock(), is_public=is_public)
    app = WebSocketApp('ws://127.0.0.1')
    app.sock = WebSocket()
    owned, peer = socketpair()
    with owned, peer:
        app.sock.sock = owned
        manager.ws = app
        peer.settimeout(0.1)
        manager.close()
        if is_public:
            assert peer.recv(1) == b''
        else:
            with pytest.raises(timeout):
                peer.recv(1)


def _read_opening_headers(peer):
    data = bytearray()
    while not data.endswith(b'\r\n\r\n'):
        part = peer.recv(1024)
        assert part and len(data) + len(part) <= 4096
        data.extend(part)
    return bytes(data)


@pytest.mark.parametrize('phase', ['proxy_connect', 'tls_to_upgrade', 'opened'])
def test_public_opening_read_deadline_and_restore(phase, tmp_path, monkeypatch):
    for name in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY'):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.lower(), raising=False)
    manager = OkexWsManager(Mock(), is_public=True)
    manager._connect_timeout_s = 1.0
    completed = Event()
    steady_timeouts = []

    def on_open(ws):
        manager._on_open(ws)
        steady_timeouts.append((ws.sock.gettimeout(), ws.sock.sock.gettimeout()))
        completed.set()

    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        listener.listen(1)
        listener.settimeout(3)
        port = listener.getsockname()[1]
        if phase == 'proxy_connect':
            monkeypatch.setenv('https_proxy', f'http://127.0.0.1:{port}')
            url = 'wss://public.example.invalid/quotes'
        else:
            cert, key = tmp_path / 'cert.pem', tmp_path / 'key.pem'
            subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
                            '-subj', '/CN=localhost', '-addext', 'subjectAltName=IP:127.0.0.1',
                            '-keyout', str(key), '-out', str(cert)], timeout=10, check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            server.load_cert_chain(cert, key)
            monkeypatch.setenv('WEBSOCKET_CLIENT_CA_BUNDLE', str(cert))
            url = f'wss://127.0.0.1:{port}/quotes'
        app = base_ws_manager.WebSocketApp(url, on_open=on_open)
        manager.ws = app
        worker = Thread(target=manager._run_socket, args=(app,))
        worker.start()
        try:
            peer, _ = listener.accept()
            with peer:
                peer.settimeout(3)
                if phase == 'proxy_connect':
                    assert _read_opening_headers(peer).startswith(b'CONNECT public.example.invalid:443 ')
                    assert app.sock is not None and app.sock.sock is None
                    manager.close()
                    worker.join(2)
                    assert not worker.is_alive()
                else:
                    assert peer.recv(1, socket.MSG_PEEK) == b'\x16'
                    if phase == 'tls_to_upgrade':
                        assert app.sock is not None and app.sock.sock is None
                        manager.close()
                    with server.wrap_socket(peer, server_side=True) as tls:
                        request = _read_opening_headers(tls)
                        assert request.startswith(b'GET /quotes HTTP/1.1')
                        if phase == 'opened':
                            ws_key, = [line.split(b':', 1)[1].strip() for line in request.splitlines()
                                       if line.lower().startswith(b'sec-websocket-key:')]
                            accept = base64.b64encode(hashlib.sha1(
                                ws_key + b'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').digest())
                            tls.sendall(b'HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n'
                                        b'Connection: Upgrade\r\nSec-WebSocket-Accept: ' + accept + b'\r\n\r\n')
                            assert completed.wait(2)
                            assert steady_timeouts == [(None, None)]
                            manager.close()
                        worker.join(2)
                        assert not worker.is_alive()
        finally:
            manager.close()
            worker.join(3)


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
