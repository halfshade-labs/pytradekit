"""Exercise reconnect races without exchange access or real WebSocket I/O."""
from threading import Event, Thread, current_thread
from time import monotonic
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from pytradekit.gateway.websocket import base_ws_manager
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
        self.opened = Event()
        self.finished = Event()
        self.raise_on_run = False
        self.sent = []
        self.close_count = 0

    def run_forever(self, **kwargs):
        self.started.set()
        try:
            if self.raise_on_run:
                if isinstance(self.raise_on_run, BaseException):
                    raise self.raise_on_run
                raise OSError("synthetic disconnect")
            while not self.open_gate.wait(0.002):
                if self.stop.is_set():
                    return
            if self.stop.is_set():
                return
            self.sock = SimpleNamespace(connected=True)
            self.callbacks['on_open'](self)
            self.opened.set()
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


@pytest.mark.parametrize('target_symbols', [['ETHUSDT'], []])
def test_initial_public_target_update_after_failed_handshake_does_not_deadlock(
    manager, monkeypatch, sockets, target_symbols,
):
    factory = __import__(
        'pytradekit.gateway.websocket.base_ws_manager', fromlist=['WebSocketApp'],
    ).WebSocketApp

    def fail_first(*args, **kwargs):
        socket = factory(*args, **kwargs)
        socket.raise_on_run = len(sockets) == 1
        return socket

    monkeypatch.setattr('pytradekit.gateway.websocket.base_ws_manager.WebSocketApp', fail_first)
    manager._set_bookticker_symbols(frozenset({'BTCUSDT'}))
    manager._subs = [{'method': 'SUBSCRIBE', 'params': ['btcusdt@bookTicker']}]
    errors = []

    def update_targets():
        try:
            manager.update_bookticker_stream(target_symbols)
        except Exception as error:
            errors.append(error)

    caller = Thread(target=update_targets, daemon=True)
    caller.start()
    try:
        caller.join(timeout=1)
        assert not caller.is_alive(), 'initial update blocked recovery subscription replay'
        assert not errors
        assert manager.get_connection_health()['connected']
        assert manager._bookticker_symbols == frozenset(target_symbols)
        assert sockets[-1].sent
    finally:
        manager.close()
        caller.join(timeout=1)


def test_stop_releases_initial_public_target_update_without_opening_another_socket(
    manager, monkeypatch, sockets,
):
    factory = __import__(
        'pytradekit.gateway.websocket.base_ws_manager', fromlist=['WebSocketApp'],
    ).WebSocketApp

    def hold_handshake(*args, **kwargs):
        socket = factory(*args, **kwargs)
        socket.open_gate.clear()
        return socket

    monkeypatch.setattr('pytradekit.gateway.websocket.base_ws_manager.WebSocketApp', hold_handshake)
    manager._set_bookticker_symbols(frozenset())
    errors = []

    def update_targets():
        try:
            manager.update_bookticker_stream(['ETHUSDT'])
        except Exception as error:
            errors.append(error)

    caller = Thread(target=update_targets, daemon=True)
    caller.start()
    try:
        wait_for(lambda: sockets and sockets[0].started.is_set())
        manager.close()
        caller.join(timeout=1)
        assert not caller.is_alive()
        assert len(errors) == 1
        assert manager.status == WebsocketStatus.STOP.name
        assert len(sockets) == 1
        assert manager._bookticker_symbols == frozenset()
    finally:
        manager.close()
        caller.join(timeout=1)


@pytest.mark.parametrize('stream', ['bn_public_perp', 'bn_private_spot', 'okx_private'])
def test_wrapped_transport_error_has_safe_identity_errno_and_observed_timing(sockets, stream):
    logger = Mock()
    if stream == 'okx_private':
        value = OkexWsManager(logger, api_key='test-sensitive-key', is_public=False)
        expected = ('OKX', 'private', 'spot')
    else:
        value = BinanceWsManager(logger, api_key='test-sensitive-key', is_perp='perp' in stream)
        if stream == 'bn_public_perp':
            value._set_bookticker_symbols(frozenset({'BTCUSDT'}))
        expected = ('BN', 'public_bookticker' if 'public' in stream else 'private_user_data',
                    'perp' if 'perp' in stream else 'spot')
    value._url = 'wss://invalid/SENSITIVE_URL'
    value._monitor_interval_s = 0.002
    try:
        value.connect()
        wait_for(lambda: value.get_connection_health()['transport']['phase'] == 'open')
        value.reconnect = Mock()
        socket = sockets[-1]
        socket.callbacks['on_ping'](socket, 'SENSITIVE_PING')
        socket.callbacks['on_pong'](socket, 'SENSITIVE_PONG')
        error = RuntimeError('SENSITIVE_ERROR')
        error.__cause__ = OSError(104, 'SENSITIVE_CAUSE')
        socket.callbacks['on_error'](socket, error)
        diagnostic = value.get_connection_health()['transport']
        assert (diagnostic['exchange'], diagnostic['stream_kind'], diagnostic['market']) == expected
        assert diagnostic['stream_id'] and diagnostic['generation'] == 1
        assert diagnostic['error_errno'] == 104 and diagnostic['error_phase'] == 'open'
        assert diagnostic['protocol_ping_received_ms'] is not None
        assert diagnostic['protocol_pong_received_ms'] is not None
        assert diagnostic['callback_event'] == 'error'
        assert diagnostic['callback_duration_ms'] >= 0
        assert diagnostic['local_close_reason'] == 'socket_error'  # Callback requested recovery.
        logs = str(logger.mock_calls)
        assert all(secret not in logs for secret in [
            'test-sensitive-key', 'SENSITIVE_URL', 'SENSITIVE_PING', 'SENSITIVE_PONG',
            'SENSITIVE_ERROR', 'SENSITIVE_CAUSE',
        ])
        assert '"error_errno": 104' in logs
        value.close()
        assert value.get_connection_health()['transport']['local_close_reason'] == 'stop'
    finally:
        value.close()
        value._monitorThread.join(timeout=1)


def test_retired_callback_cannot_overwrite_transport_diagnostics(manager, sockets):
    manager.connect()
    old = sockets[0]
    manager._reconnect_with_new_url()
    before = manager.get_connection_health()['transport']
    old.callbacks['on_error'](old, OSError(104, 'SENSITIVE_RETIRED'))
    old.callbacks['on_pong'](old, 'SENSITIVE_RETIRED')
    assert manager.get_connection_health()['transport'] == before
    assert before['error_errno'] is None and before['protocol_pong_received_ms'] is None


def patch_callback_clock(monkeypatch, values):
    """Script only this thread; lifecycle work keeps the real monotonic clock."""
    callback_thread = current_thread()
    real_clock = base_ws_manager.get_monotonic_timestamp_ns
    ticks = iter(values)

    def clock_for_thread():
        if current_thread() is callback_thread:
            return next(ticks)
        return real_clock()

    monkeypatch.setattr(base_ws_manager, 'get_monotonic_timestamp_ns', clock_for_thread)


def test_callback_failure_and_slow_callback_include_timing_without_payload(manager, sockets, monkeypatch):
    def failed_message(*args):
        raise ValueError('SENSITIVE_CALLBACK')
    manager._on_message = failed_message
    manager.connect()
    socket = sockets[0]
    assert socket.opened.wait(1), 'wrapped on_open did not complete'
    manager.reconnect = Mock()
    with monkeypatch.context() as clock_patch:
        patch_callback_clock(clock_patch, [1_000_000_000, 1_150_000_000])
        socket.callbacks['on_message'](socket, 'SENSITIVE_PAYLOAD')
        diagnostic = manager.get_connection_health()['transport']
        assert diagnostic['callback_event'] == 'message'
        assert diagnostic['callback_duration_ms'] == 150
        assert diagnostic['error_phase'] == 'callback'
        assert diagnostic['error_type'] == 'ValueError'
        assert diagnostic['error_errno'] is None
        assert 'SENSITIVE_' not in str(manager.logger.mock_calls)
        assert 'callback_error' in str(manager.logger.mock_calls)
        manager.reconnect.assert_called_once_with(socket)
    manager.close()


def test_successful_slow_callback_is_logged_without_per_message_logging(manager, sockets, monkeypatch):
    manager._on_message = Mock()
    manager.connect()
    socket = sockets[0]
    assert socket.opened.wait(1), 'wrapped on_open did not complete'
    manager.reconnect = Mock()
    with monkeypatch.context() as clock_patch:
        patch_callback_clock(clock_patch, [1_000_000_000, 1_150_000_000, 2_000_000_000, 2_001_000_000])
        manager.logger.reset_mock()
        socket.callbacks['on_message'](socket, 'SENSITIVE_SLOW_PAYLOAD')
        assert manager.get_connection_health()['transport']['callback_duration_ms'] == 150
        assert 'callback_slow' in str(manager.logger.mock_calls)
        assert 'SENSITIVE_' not in str(manager.logger.mock_calls)
        manager.logger.reset_mock()
        socket.callbacks['on_message'](socket, 'SENSITIVE_FAST_PAYLOAD')
        assert manager.get_connection_health()['transport']['callback_duration_ms'] == 1
        manager.logger.debug.assert_not_called()
        manager.reconnect.assert_not_called()


def test_run_error_before_open_retains_connecting_phase_in_safe_log(manager, monkeypatch, sockets):
    factory = __import__('pytradekit.gateway.websocket.base_ws_manager', fromlist=['WebSocketApp']).WebSocketApp
    def fail_once(*args, **kwargs):
        socket = factory(*args, **kwargs)
        if len(sockets) == 1:
            socket.raise_on_run = OSError(111, 'SENSITIVE_RUN_ERROR')
        return socket
    monkeypatch.setattr('pytradekit.gateway.websocket.base_ws_manager.WebSocketApp', fail_once)
    manager.connect()
    logs = str(manager.logger.mock_calls)
    assert 'run_error' in logs and '"error_phase": "connecting"' in logs
    assert '"error_errno": 111' in logs
    assert 'SENSITIVE_RUN_ERROR' not in logs
    assert manager.get_connection_health()['transport']['error_errno'] is None


def test_okx_wrapped_application_pong_records_only_current_socket(sockets):
    value = OkexWsManager(Mock(), is_public=False)
    value._monitor_interval_s = 0.002
    try:
        value.connect()
        socket = sockets[0]
        value._send_heartbeat()
        socket.callbacks['on_message'](socket, 'pong')
        diagnostic = value.get_connection_health()['transport']
        assert diagnostic['application_ping_sent_ms'] is not None
        assert diagnostic['application_pong_received_ms'] is not None
        assert diagnostic['protocol_pong_received_ms'] is None
        assert value._pending_pong_socket is None
    finally:
        value.close()
        value._monitorThread.join(timeout=1)


def transport_events(logger, event):
    import json
    prefix = f'websocket_transport event={event} '
    return [json.loads(call.args[0][len(prefix):]) for call in logger.debug.call_args_list
            if call.args and call.args[0].startswith(prefix)]


def test_inflight_callback_health_clears_prior_completion(manager, sockets):
    entered, release = Event(), Event()
    def blocked_message(*args):
        entered.set()
        assert release.wait(2)
    manager._on_message = blocked_message
    manager.connect()
    wait_for(lambda: manager.get_connection_health()['transport']['callback_duration_ms'] is not None)
    caller = Thread(target=sockets[0].callbacks['on_message'], args=(sockets[0], 'test-message'))
    caller.start()
    try:
        assert entered.wait(1)
        diagnostic = manager.get_connection_health()['transport']
        assert diagnostic['callback_event'] == 'message'
        assert diagnostic['callback_started_ms'] is not None
        assert diagnostic['callback_finished_ms'] is None
        assert diagnostic['callback_duration_ms'] is None
    finally:
        release.set()
        caller.join(timeout=1)
    assert not caller.is_alive()


@pytest.mark.parametrize('failed', [False, True])
def test_started_callback_completion_survives_real_replacement_without_changing_current(
    manager, sockets, failed,
):
    entered, release = Event(), Event()
    def blocked_message(*args):
        entered.set()
        assert release.wait(2)
        if failed:
            raise OSError(104, 'test-private-callback-error')
    manager._on_message = blocked_message
    manager.connect()
    old = sockets[0]
    caller = Thread(target=old.callbacks['on_message'], args=(old, 'test-private-payload'))
    caller.start()
    try:
        assert entered.wait(1)
        manager._reconnect_with_new_url()
        wait_for(lambda: manager.get_connection_health()['transport']['callback_duration_ms'] is not None)
        before = manager.get_connection_health()['transport']
        assert before['generation'] == 2
        release.set()
        caller.join(timeout=1)
        assert not caller.is_alive()
        assert manager.get_connection_health()['transport'] == before
        old.callbacks['on_message'](old, 'test-private-retired-payload')
        assert manager.get_connection_health()['transport'] == before
        event = 'callback_error' if failed else 'callback_retired'
        rows = transport_events(manager.logger, event)
        assert len(rows) == 1 and rows[0]['generation'] == 1
        assert rows[0]['callback_event'] == 'message'
        assert rows[0]['callback_duration_ms'] is not None
        assert rows[0]['callback_finished_ms'] is not None
        if failed:
            assert rows[0]['error_errno'] == 104 and rows[0]['error_phase'] == 'callback'
        assert 'test-private-' not in str(manager.logger.mock_calls)
    finally:
        release.set()
        caller.join(timeout=1)


def test_detached_close_error_and_terminal_summary_keep_retiring_identity(manager, sockets):
    manager.connect()
    old = sockets[0]
    close = old.close
    def failed_close(**kwargs):
        close(**kwargs)
        raise OSError(5, 'test-private-local-close')
    old.close = failed_close
    manager._reconnect_with_new_url()
    rows = transport_events(manager.logger, 'close_error')
    assert rows and all(row['generation'] == 1 and row['error_errno'] == 5 for row in rows)
    assert all(row['phase'] == 'closed' for row in rows)
    terminal = transport_events(manager.logger, 'terminal')
    assert len([row for row in terminal if row['generation'] == 1]) == 1
    assert terminal[0]['local_close_reason'] == 'url_rotation'
    assert manager.get_connection_health()['transport']['error_errno'] is None
    assert 'test-private-local-close' not in str(manager.logger.mock_calls)


def test_stop_emits_one_terminal_summary_for_the_current_generation(manager):
    manager.connect()
    manager.close()
    manager._monitorThread.join(timeout=1)
    manager.close()
    terminal = transport_events(manager.logger, 'terminal')
    assert len(terminal) == 1
    assert terminal[0]['generation'] == 1 and terminal[0]['phase'] == 'closed'
    assert terminal[0]['local_close_reason'] == 'stop'
    assert len(transport_events(manager.logger, 'run_exit')) == 1


def test_run_exit_summary_survives_next_generation(manager, sockets):
    manager.connect()
    old = sockets[0]
    # Model run_forever returning without a close callback; lifecycle stays real.
    old.callbacks['on_close'] = lambda *args: None
    old.stop.set()
    wait_for(lambda: manager.get_connection_health()['generation'] == 2)
    rows = transport_events(manager.logger, 'terminal')
    assert len([row for row in rows if row['generation'] == 1]) == 1
    assert rows[0]['local_close_reason'] == 'run_exit' and rows[0]['phase'] == 'closed'
    exits = transport_events(manager.logger, 'run_exit')
    assert exits[0]['generation'] == 1 and exits[0]['run_exit_ms'] is not None


def test_current_registered_close_keeps_code_phase_and_reason_after_replacement(manager, sockets):
    manager.connect()
    socket = sockets[0]
    socket.callbacks['on_close'](socket, 1006, 'test-private-remote-close-reason')
    wait_for(lambda: manager.get_connection_health()['generation'] == 2)
    rows = transport_events(manager.logger, 'close')
    assert rows and rows[0]['generation'] == 1
    assert rows[0]['close_code'] == 1006 and rows[0]['phase'] == 'closed'
    assert rows[0]['local_close_reason'] == 'socket_close'
    assert 'test-private-remote-close-reason' not in str(manager.logger.mock_calls)


def test_health_generation_and_transport_are_copied_before_replacement(manager, sockets, monkeypatch):
    manager.connect()
    entered, release = Event(), Event()
    snapshot = manager._copy_transport_locked
    def blocked_snapshot(evidence):
        entered.set()
        assert release.wait(2)
        return snapshot(evidence)
    monkeypatch.setattr(manager, '_copy_transport_locked', blocked_snapshot)
    result = []
    reader = Thread(target=lambda: result.append(manager.get_connection_health()))
    reader.start()
    assert entered.wait(1)
    rotation = Thread(target=manager._reconnect_with_new_url)
    rotation.start()
    try:
        assert len(sockets) == 1
    finally:
        release.set()
        reader.join(timeout=1)
        rotation.join(timeout=1)
    assert not reader.is_alive() and not rotation.is_alive()
    assert result[0]['generation'] == result[0]['transport']['generation'] == 1
    assert manager.get_connection_health()['generation'] == 2
