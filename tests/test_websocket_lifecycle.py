"""Exercise reconnect races without exchange access or real WebSocket I/O."""
from threading import Event, Thread
from time import monotonic
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from pytradekit.gateway.websocket.base_ws_manager import BaseWebsocketManager
from pytradekit.utils.dynamic_types import WebsocketStatus
from pytradekit.ws.binance_ws import BinanceWsManager
from pytradekit.ws.huobi_ws import HuobiWsManager
from pytradekit.ws.okex_ws import OkexWsManager


def wait_for(predicate, timeout=2):
    deadline = monotonic() + timeout
    while not predicate():
        assert monotonic() < deadline, "background lifecycle did not progress"
        Event().wait(0.002)


class FakeSocket:
    def __init__(self, url, **callbacks):
        self.url = url
        self.callbacks = callbacks
        self.sock = None
        self.stop = Event()
        self.open_gate = Event()
        self.open_gate.set()
        self.started = Event()
        self.finished = Event()
        self.raise_on_run = False
        self.sent = []
        self.close_count = 0

    def run_forever(self, **kwargs):
        self.started.set()
        try:
            if self.raise_on_run:
                raise OSError("synthetic disconnect")
            while not self.open_gate.wait(0.002):
                if self.stop.is_set():
                    return
            if self.stop.is_set():
                return
            self.sock = SimpleNamespace(connected=True)
            self.callbacks['on_open'](self)
            self.stop.wait(3)
            self.sock.connected = False
            self.callbacks['on_close'](self, 1006, 'synthetic close')
        finally:
            self.finished.set()

    def close(self, **kwargs):
        self.close_count += 1
        self.stop.set()
        if self.sock is not None:
            self.sock.connected = False

    def send(self, message, **kwargs):
        self.sent.append(message)


@pytest.fixture
def sockets(monkeypatch):
    created = []
    def factory(*args, **kwargs):
        socket = FakeSocket(*args, **kwargs)
        created.append(socket)
        return socket
    monkeypatch.setattr('pytradekit.gateway.websocket.base_ws_manager.WebSocketApp', factory)
    return created


@pytest.fixture
def manager(sockets):
    value = BinanceWsManager(Mock(), is_perp=True)
    value._reconnect_base_delay_ms = 1
    value._reconnect_max_delay_ms = 2
    value._connect_timeout_s = 0.06
    value._monitor_interval_s = 0.002
    yield value
    value.close()
    monitor = getattr(value, '_monitorThread', None)
    if monitor:
        monitor.join(timeout=2)
        assert not monitor.is_alive()


def test_health_snapshot_reports_thread_failure_and_idle_business_stream(manager):
    manager.connect()
    health = manager.get_connection_health()
    assert health['status'] == WebsocketStatus.ACTIVE.name
    assert health['connected'] and health['monitor_alive'] and health['socket_thread_alive']
    assert health['generation'] == 1
    assert len(str(health['monitor_heartbeat_ms'])) == 13
    assert len(str(health['last_open_ms'])) == 13
    assert health['last_message_ms'] is None
    # An account without fills still has a healthy, live socket.
    assert manager.get_connection_health()['connected']
    manager.close()
    wait_for(lambda: not manager.get_connection_health()['socket_thread_alive'])
    assert manager.get_connection_health()['status'] == WebsocketStatus.STOP.name
    assert not manager.get_connection_health()['connected']


def test_url_rotation_and_disconnect_share_one_monitor_and_one_current_socket(manager, sockets):
    manager.connect()
    monitor = manager._monitorThread
    original = sockets[0]
    entered_backoff = Event()
    release_backoff = Event()
    original_wait = manager._wait_reconnect_delay
    def block_backoff(delay):
        entered_backoff.set()
        release_backoff.wait(1)
        return original_wait(0)
    manager._wait_reconnect_delay = block_backoff
    manager.reconnect()
    assert entered_backoff.wait(1)
    manager._url = 'wss://example.invalid/new-private-stream'
    rotation = Thread(target=manager._reconnect_with_new_url)
    rotation.start()
    release_backoff.set()
    rotation.join(timeout=2)
    assert not rotation.is_alive()
    wait_for(lambda: manager.get_connection_health()['connected'])
    assert manager._monitorThread is monitor
    assert monitor.is_alive()
    assert manager.ws.url.endswith('/new-private-stream')
    assert original.finished.wait(1)
    assert sum(socket.sock is not None and socket.sock.connected for socket in sockets) == 1
    assert all(socket.close_count for socket in sockets if socket is not manager.ws)


def test_obsolete_callbacks_cannot_invalidate_current_connection_or_deliver_messages(manager, sockets):
    manager._on_message = Mock()
    manager._on_ping = Mock()
    manager._on_pong = Mock()
    manager.connect()
    old = sockets[0]
    manager._url = 'wss://example.invalid/renewed'
    manager._reconnect_with_new_url()
    generation = manager.get_connection_health()['generation']
    for name, args in [('on_message', ('{}',)), ('on_error', (OSError(),)),
                       ('on_close', (1006, 'old')), ('on_open', ()),
                       ('on_ping', ('ping',)), ('on_pong', ('pong',))]:
        old.callbacks[name](old, *args)
    assert not manager._needRecovery.is_set()
    assert manager.get_connection_health()['generation'] == generation
    manager._on_message.assert_not_called()
    manager._on_ping.assert_not_called()
    manager._on_pong.assert_not_called()


@pytest.mark.parametrize('failure', ['run_exception', 'handshake_timeout'])
def test_failed_connection_is_closed_and_retried_by_same_monitor(manager, monkeypatch, sockets, failure):
    factory = __import__('pytradekit.gateway.websocket.base_ws_manager', fromlist=['WebSocketApp']).WebSocketApp
    def fail_once(*args, **kwargs):
        socket = factory(*args, **kwargs)
        if len(sockets) == 1:
            if failure == 'run_exception':
                socket.raise_on_run = True
            else:
                socket.open_gate.clear()
        return socket
    monkeypatch.setattr('pytradekit.gateway.websocket.base_ws_manager.WebSocketApp', fail_once)
    manager.connect()
    assert len(sockets) == 2
    assert sockets[0].finished.wait(1)
    assert sockets[0].close_count >= 1
    assert manager.get_connection_health()['generation'] == 2
    assert manager.get_connection_health()['connected']


def test_health_snapshot_does_not_wait_for_handshake(manager, monkeypatch, sockets):
    factory = __import__('pytradekit.gateway.websocket.base_ws_manager', fromlist=['WebSocketApp']).WebSocketApp
    def block_open(*args, **kwargs):
        socket = factory(*args, **kwargs)
        socket.open_gate.clear()
        return socket
    monkeypatch.setattr('pytradekit.gateway.websocket.base_ws_manager.WebSocketApp', block_open)
    caller = Thread(target=manager.connect)
    caller.start()
    wait_for(lambda: sockets and sockets[0].started.is_set())
    started = monotonic()
    health = manager.get_connection_health()
    assert monotonic() - started < 0.03
    assert health['monitor_alive'] and not health['connected']
    manager.close()
    caller.join(timeout=1)
    assert not caller.is_alive()
    assert sockets[0].finished.wait(1)
    manager.connect()
    manager._reconnect_with_new_url()
    manager.reconnect()
    assert manager.status == WebsocketStatus.STOP.name
    assert len(sockets) == 1


def test_monitor_iteration_exception_is_contained_and_retries(manager, monkeypatch, sockets):
    original_connect = manager._connect
    calls = []
    def fail_once():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError('synthetic monitor iteration failure')
        return original_connect()
    monkeypatch.setattr(manager, '_connect', fail_once)
    manager.connect()
    assert len(calls) == 2
    assert len(sockets) == 1
    assert manager.get_connection_health()['monitor_alive']


@pytest.mark.parametrize('manager_class', [HuobiWsManager, OkexWsManager])
def test_shared_managers_replay_subscriptions_after_recovery(sockets, manager_class):
    value = manager_class(Mock())
    value._monitor_interval_s = 0.002
    value._reconnect_delay_s = 0.002
    value._subs = [{'channel': 'synthetic'}]
    try:
        value.connect()
        original_monitor = value._monitorThread
        value.reconnect()
        wait_for(lambda: len(sockets) == 2 and bool(sockets[1].sent))
        assert value._monitorThread is original_monitor
        assert value.get_connection_health()['connected']
    finally:
        value.close()
        value._monitorThread.join(timeout=1)


def test_late_handshake_after_replacement_closes_the_obsolete_socket(manager, sockets):
    manager.connect()
    obsolete = sockets[0]
    manager._url = 'wss://example.invalid/rotated'
    manager._reconnect_with_new_url()
    # websocket-client can finish a previously blocked handshake after close()
    # saw sock=None. Reproduce that late on_open with a newly attached socket.
    obsolete.sock = SimpleNamespace(connected=True)
    closes_before = obsolete.close_count
    obsolete.callbacks['on_open'](obsolete)
    assert obsolete.close_count == closes_before + 1
    assert not obsolete.sock.connected
    assert manager.get_connection_health()['connected']


def test_stopped_keepalive_does_not_renew_or_recreate_listen_key(manager):
    manager.close()
    manager.put_listen_key = Mock()
    manager.post_listen_key = Mock()
    manager._ping(0.01, reconnection_time=0.01)
    manager.subscribe()
    manager.put_listen_key.assert_not_called()
    manager.post_listen_key.assert_not_called()


def test_initial_send_does_not_hold_send_lock_while_waiting_for_connection(manager):
    entered = Event()
    release = Event()
    def connecting():
        entered.set()
        release.wait(1)
        manager.ws = SimpleNamespace(sock=SimpleNamespace(connected=True), send=Mock())
    manager.connect = connecting
    sender = Thread(target=manager.send, args=('synthetic subscription',))
    sender.start()
    assert entered.wait(1)
    acquired = manager._semaphore.acquire(blocking=False)
    try:
        assert acquired, "recovery must be able to replay while a sender waits"
    finally:
        if acquired:
            manager._semaphore.release()
        release.set()
        sender.join(timeout=1)
    assert not sender.is_alive()


def test_failed_handshake_then_short_connection_cannot_deadlock_subscription_replay(manager, monkeypatch, sockets):
    factory = __import__('pytradekit.gateway.websocket.base_ws_manager', fromlist=['WebSocketApp']).WebSocketApp
    def fail_first(*args, **kwargs):
        socket = factory(*args, **kwargs)
        socket.raise_on_run = len(sockets) == 1
        return socket
    monkeypatch.setattr('pytradekit.gateway.websocket.base_ws_manager.WebSocketApp', fail_first)
    replay_entered = Event()
    allow_caller = Event()
    original_replay = manager._reconnect_streams
    original_wait = manager._ready.wait
    def disconnect_before_replay():
        if manager._generation == 2:
            manager.ws.sock.connected = False
            manager.reconnect(manager.ws)
            replay_entered.set()
            allow_caller.set()
        original_replay()
    def wait_until_replay(timeout):
        result = original_wait(timeout)
        if result and manager._generation == 2:
            assert allow_caller.wait(1)
        return result
    monkeypatch.setattr(manager, '_reconnect_streams', disconnect_before_replay)
    monkeypatch.setattr(manager._ready, 'wait', wait_until_replay)
    manager._subs = [{'channel': 'synthetic'}]
    errors = []
    def send_subscription():
        try:
            manager.send_json(manager._subs[0])
        except Exception as error:
            errors.append(error)
    caller = Thread(target=send_subscription)
    caller.start()
    try:
        assert replay_entered.wait(1)
        caller.join(timeout=1)
        assert not caller.is_alive(), "initial sender and replay monitor deadlocked"
        assert not errors
        assert manager._generation == 3
        assert manager.get_connection_health()['connected']
        assert sockets[2].sent
    finally:
        manager.close()
        caller.join(timeout=1)
