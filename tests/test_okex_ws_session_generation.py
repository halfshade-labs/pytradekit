"""Exercise already-started private callbacks across socket replacement."""

import json
from itertools import product
from threading import Event, Thread, current_thread
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from pytradekit.gateway.websocket.base_ws_manager import BaseWebsocketManager, _TransportEvidence
from pytradekit.utils.dynamic_types import WebsocketStatus
from pytradekit.ws.okex_ws import OkexWsManager


LOGIN = {'event': 'login', 'code': '0'}
ORDERS = {'event': 'subscribe', 'arg': {'channel': 'orders', 'instType': 'SPOT'}}


@pytest.fixture
def manager():
    value = OkexWsManager(
        Mock(), api_key='TEST_KEY', api_secret='TEST_SECRET',
        passphrase='TEST_PASSPHRASE', is_public=False,
    )
    value.status = WebsocketStatus.ACTIVE.name
    publish_socket(value)
    return value


def install_socket(manager):
    socket = SimpleNamespace(sock=SimpleNamespace(connected=True), send=Mock(), close=Mock())
    with manager._state_lock:
        manager.ws = socket
        manager._generation += 1
        manager._transport_evidence = _TransportEvidence(
            manager._generation, manager._transport_log_context(),
        )
        manager._transport_diagnostics = manager._transport_evidence.state
    return socket


def publish_socket(manager):
    socket = install_socket(manager)
    manager._on_open(socket)
    return socket


def prepare_session(manager, state):
    if state[0]:
        manager._on_message(manager.ws, json.dumps(LOGIN))
    if state[1]:
        manager._on_message(manager.ws, json.dumps(ORDERS))
    manager.ws.send.reset_mock()


@pytest.mark.parametrize('case', product([
    LOGIN, ORDERS, {'event': 'login', 'code': '60009'},
    {'event': 'unsubscribe', 'arg': ORDERS['arg']},
    {'event': 'error', 'code': '60011'},
], [(False, False), (True, False), (True, True)]))
def test_started_session_event_cannot_change_replacement(manager, monkeypatch, case):
    event, state = case
    old = manager.ws
    entered, release = Event(), Event()
    loads = json.loads

    def paused_loads(message):
        if current_thread() is callback:
            entered.set()
            assert release.wait(2)
        return loads(message)

    monkeypatch.setattr('pytradekit.ws.okex_ws.json.loads', paused_loads)
    callback = Thread(
        target=manager._wrap_callback(manager._on_message, 'message'),
        args=(old, json.dumps(event)),
    )
    callback.start()
    try:
        assert entered.wait(1)
        replacement = publish_socket(manager)
        prepare_session(manager, state)
    finally:
        release.set()
        callback.join(timeout=1)
    assert not callback.is_alive()
    assert (manager._logged_on, manager._orders_subscribed) == state
    replacement.send.assert_not_called()
    assert not manager._needRecovery.is_set()
    assert not manager.get_connection_health()['transport']['error_type']


def test_started_old_close_cannot_reset_replacement_readiness(manager):
    old = manager.ws
    entered, release = Event(), Event()

    def paused_close(ws, *args):
        entered.set()
        assert release.wait(2)
        manager._on_close(ws, *args)

    callback = Thread(target=manager._wrap_callback(paused_close, 'close'), args=(old, 1006, 'synthetic'))
    callback.start()
    try:
        assert entered.wait(1)
        replacement = publish_socket(manager)
        prepare_session(manager, (True, True))
    finally:
        release.set()
        callback.join(timeout=1)
    assert not callback.is_alive()
    assert manager.get_connection_health()['connected']
    replacement.send.assert_not_called()
    assert not manager._needRecovery.is_set()


@pytest.mark.parametrize('retire', ['replace', 'stop'])
def test_subscription_waiting_for_send_lock_cannot_follow_replacement(manager, retire):
    old = manager.ws
    waiting = Event()
    semaphore = manager._semaphore

    class ObserveSendLock:
        def __enter__(self):
            waiting.set()
            return semaphore.__enter__()

        def __exit__(self, *args):
            return semaphore.__exit__(*args)

    manager._semaphore = ObserveSendLock()
    request_id, subscriptions = manager._cur_id, list(manager._subs)
    callback = Thread(
        target=manager._wrap_callback(manager._on_message, 'message'),
        args=(old, json.dumps(LOGIN)),
    )
    try:
        with semaphore:
            callback.start()
            assert waiting.wait(1)
            # Reading health while send waits must not wait on network I/O.
            assert manager.get_connection_health()['generation'] == 1
            if retire == 'replace':
                replacement = publish_socket(manager)
            else:
                manager.close()
                replacement = old
    finally:
        callback.join(timeout=1)
    assert not callback.is_alive()
    old.send.assert_not_called()
    replacement.send.assert_not_called()
    health = manager.get_connection_health()
    assert not health['logged_on'] and not health['subscribed']
    assert manager._cur_id == request_id
    assert manager._subs == subscriptions


def test_subscription_send_stays_on_original_socket_after_gate(manager):
    old = manager.ws
    replacements = []
    old.send.side_effect = lambda *args, **kwargs: replacements.append(publish_socket(manager))
    manager._wrap_callback(manager._on_message, 'message')(old, json.dumps(LOGIN))
    assert old.send.call_count == 1
    replacements[0].send.assert_not_called()
    assert not manager._logged_on and not manager._orders_subscribed


def test_synchronous_orders_ack_and_request_ids_are_preserved(manager):
    socket = manager.ws
    socket.send.side_effect = lambda *args, **kwargs: manager._on_message(socket, json.dumps(ORDERS))
    manager._wrap_callback(manager._on_message, 'message')(socket, json.dumps(LOGIN))
    health = manager.get_connection_health()
    assert health['connected'] and health['logged_on'] and health['subscribed']
    message = json.loads(socket.send.call_args.args[0])
    assert message == {'op': 'subscribe', 'args': [{'channel': 'orders', 'instType': 'SPOT'}], 'id': 0}
    assert manager._cur_id == 1
    assert 'id' not in manager._subs[0]
    manager._send_order()  # Keep the existing zero-argument entrypoint usable.
    assert json.loads(socket.send.call_args.args[0])['id'] == 1
    assert manager.get_connection_health()['subscribed']


def test_new_socket_before_open_does_not_inherit_private_readiness(manager):
    prepare_session(manager, (True, True))
    install_socket(manager)
    health = manager.get_connection_health()
    assert not health['connected']
    assert not health['logged_on'] and not health['subscribed']


def test_orders_ack_before_new_open_cannot_inherit_old_login(manager):
    prepare_session(manager, (True, True))
    socket = install_socket(manager)
    manager._wrap_callback(manager._on_message, 'message')(socket, json.dumps(ORDERS))
    assert not manager.get_connection_health()['connected']
    socket.send.assert_not_called()


def test_health_cannot_combine_old_generation_with_new_readiness(manager, monkeypatch):
    entered, release = Event(), Event()
    snapshots = []
    original = BaseWebsocketManager.get_connection_health

    def paused_snapshot(value):
        health = original(value)
        if current_thread() is reader:
            entered.set()
            assert release.wait(2)
        return health

    monkeypatch.setattr(BaseWebsocketManager, 'get_connection_health', paused_snapshot)
    reader = Thread(target=lambda: snapshots.append(manager.get_connection_health()))
    reader.start()
    try:
        assert entered.wait(1)
        publish_socket(manager)
        prepare_session(manager, (True, True))
    finally:
        release.set()
        reader.join(timeout=1)
    assert not reader.is_alive()
    assert snapshots[0]['generation'] == 1
    assert not snapshots[0]['connected']
    assert not snapshots[0]['logged_on'] and not snapshots[0]['subscribed']
    assert manager.get_connection_health()['connected']


@pytest.mark.parametrize('failure', [False, True])
def test_blocked_subscription_io_does_not_hold_state_lock_or_recover_new_socket(manager, failure):
    old = manager.ws
    entered, release, health_read = Event(), Event(), Event()

    def blocked_send(*args, **kwargs):
        entered.set()
        assert release.wait(2)
        if failure:
            raise OSError(104, 'test-private-session-send-failure')

    old.send.side_effect = blocked_send
    callback = Thread(
        target=manager._wrap_callback(manager._on_message, 'message'),
        args=(old, json.dumps(LOGIN)),
    )
    reader = Thread(target=lambda: (manager.get_connection_health(), health_read.set()))
    callback.start()
    try:
        assert entered.wait(1)
        reader.start()
        assert health_read.wait(1), 'health waited on subscription network I/O'
        replacement = publish_socket(manager)
    finally:
        release.set()
        callback.join(timeout=1)
        if reader.ident is not None:
            reader.join(timeout=1)
    assert not callback.is_alive() and not reader.is_alive()
    replacement.send.assert_not_called()
    assert not manager._needRecovery.is_set()
    assert not manager.get_transport_diagnostics()['error_type']
    assert 'test-private-' not in str(manager.logger.mock_calls)
    prepare_session(manager, (True, True))
    assert manager.get_connection_health()['connected']


def test_started_login_after_stop_cannot_restore_session(manager, monkeypatch):
    old = manager.ws
    loads = json.loads

    def stop_before_decode(message):
        manager.close()
        return loads(message)

    monkeypatch.setattr('pytradekit.ws.okex_ws.json.loads', stop_before_decode)
    manager._wrap_callback(manager._on_message, 'message')(old, json.dumps(LOGIN))
    assert not manager._logged_on and not manager._orders_subscribed
    old.send.assert_not_called()
    assert not manager.get_connection_health()['connected']


def test_initial_login_keeps_synchronous_connect_outside_send_lock(manager, monkeypatch):
    manager.status = WebsocketStatus.INIT.name
    manager.ws = None

    def connect():
        assert manager._semaphore.acquire(blocking=False), 'connect held the send lock'
        manager._semaphore.release()
        publish_socket(manager)
        manager.status = WebsocketStatus.ACTIVE.name

    connector = Mock(side_effect=connect)
    monkeypatch.setattr(manager, 'connect', connector)
    manager._login()
    connector.assert_called_once()
    assert json.loads(manager.ws.send.call_args.args[0])['op'] == 'login'
    manager._on_message(manager.ws, json.dumps(LOGIN))
    manager._on_message(manager.ws, json.dumps(ORDERS))
    sent = [json.loads(call.args[0]) for call in manager.ws.send.call_args_list]
    assert [message['id'] for message in sent] == [0, 1]
    assert manager.get_connection_health()['connected']


def test_health_cannot_combine_pre_recovery_transport_with_late_session_ack(manager, monkeypatch):
    entered, release = Event(), Event()
    snapshots = []
    original = BaseWebsocketManager.get_connection_health

    def paused_snapshot(value):
        health = original(value)
        if current_thread() is reader:
            entered.set()
            assert release.wait(2)
        return health

    monkeypatch.setattr(BaseWebsocketManager, 'get_connection_health', paused_snapshot)
    reader = Thread(target=lambda: snapshots.append(manager.get_connection_health()))
    reader.start()
    try:
        assert entered.wait(1)
        manager.reconnect(manager.ws)
        prepare_session(manager, (True, True))
    finally:
        release.set()
        reader.join(timeout=1)
    assert not reader.is_alive()
    assert not snapshots[0]['connected']
    assert not manager.get_connection_health()['connected']


def test_started_transport_error_cannot_recover_replacement_session(manager):
    old = manager.ws
    entered, release = Event(), Event()

    def paused_error(ws, error):
        entered.set()
        assert release.wait(2)
        manager._on_error(ws, error)

    callback = Thread(
        target=manager._wrap_callback(paused_error, 'error'),
        args=(old, OSError(104, 'test-private-transport-error')),
    )
    callback.start()
    try:
        assert entered.wait(1)
        replacement = publish_socket(manager)
        prepare_session(manager, (True, True))
    finally:
        release.set()
        callback.join(timeout=1)
    assert not callback.is_alive()
    assert manager.get_connection_health()['connected']
    assert not manager._needRecovery.is_set()
    assert manager.get_transport_diagnostics()['error_type'] is None
    replacement.send.assert_not_called()
    assert 'test-private-' not in str(manager.logger.mock_calls)


@pytest.mark.parametrize('event', [{'event': 'login', 'code': '60009'}, {'event': 'error'}])
def test_replacement_between_handler_gate_and_reset_preserves_new_session(manager, event):
    old = manager.ws
    reset = manager._reset_private_session
    replacements = []

    def replace_before_reset(ws):
        replacements.append(publish_socket(manager))
        prepare_session(manager, (True, True))
        reset(ws)

    manager._reset_private_session = replace_before_reset
    manager._wrap_callback(manager._on_message, 'message')(old, json.dumps(event))
    assert len(replacements) == 1
    replacements[0].send.assert_not_called()
    assert manager.get_connection_health()['connected']
    assert not manager._needRecovery.is_set()


@pytest.mark.parametrize('failure', ['missing_socket', 'disconnected', 'send_error'])
def test_current_subscription_failure_requests_current_recovery(manager, failure):
    socket = manager.ws
    if failure == 'missing_socket':
        socket.sock = None
    elif failure == 'disconnected':
        socket.sock.connected = False
    else:
        socket.send.side_effect = OSError(104, 'test-private-current-send-error')
    manager._wrap_callback(manager._on_message, 'message')(socket, json.dumps(LOGIN))
    assert manager._needRecovery.is_set()
    assert not manager.get_connection_health()['connected']
    assert socket.send.call_count == int(failure == 'send_error')
    assert 'test-private-' not in str(manager.logger.mock_calls)
