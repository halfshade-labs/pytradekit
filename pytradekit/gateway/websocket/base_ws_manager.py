import json
from threading import Event, Lock, Semaphore, Thread, current_thread
from uuid import uuid4

from websocket import WebSocketApp
from websocket._abnf import ABNF

from pytradekit.utils.dynamic_types import WebsocketStatus
from pytradekit.utils.exceptions import ExchangeException
from pytradekit.utils.time_handler import get_monotonic_timestamp_ns, get_timestamp_ms


SLOW_CALLBACK_MS = 100
LOCAL_CLOSE_REASONS = frozenset({
    'stop', 'connection_recovery', 'socket_close', 'socket_error', 'url_rotation',
    'handshake_timeout', 'callback_error', 'run_exit', 'heartbeat_timeout',
})


def _numeric_errno(error):
    """Inspect a bounded exception chain, without serializing any error text."""
    for _ in range(8):
        if error is None:
            break
        number = getattr(error, 'errno', None)
        if type(number) is int:
            return number
        error = error.__cause__ or error.__context__ if isinstance(error, BaseException) else None
    return None


def _new_transport_diagnostics(generation):
    state = dict.fromkeys((
        'error_type', 'error_errno', 'error_phase', 'close_code', 'local_close_reason',
        'protocol_ping_received_ms', 'protocol_pong_received_ms',
        'application_ping_attempt_ms', 'application_ping_sent_ms', 'application_pong_received_ms',
        'run_exit_ms',
        'connect_wait_result', 'connect_wait_ms',
        'callback_event', 'callback_started_ms', 'callback_finished_ms', 'callback_duration_ms',
    ))
    state.update(generation=generation, phase='connecting')
    return state


class _TransportEvidence:
    """Retained only by the current socket and already-started lifecycle work."""

    def __init__(self, generation, context=None):
        self.state = _new_transport_diagnostics(generation)
        self.context = context
        self.callback_sequence = 0
        self.terminal_logged = False


class BaseWebsocketManager:
    """Keep socket replacement in one persistent lifecycle worker.

    Callbacks and callers only request recovery. They never retire or replace a
    socket while the monitor is connecting it. The state lock protects short
    publications only; neither callbacks nor health readers wait on network I/O.
    """

    def __init__(self, logger, start_end_time_dict):
        self._ping_interval = 0
        self._subs = []
        self.ws = None
        self.status = WebsocketStatus.INIT.name
        self.logger = logger
        self.start_end_time_dict = start_end_time_dict
        self._semaphore = Semaphore()
        self._state_lock = Lock()
        self._stop_event = Event()
        self._needRecovery = Event()
        self._pending_recovery_reason = None
        self._ready = Event()
        self._monitorThread = None
        self._socket_thread = None
        self._generation = 0
        self._transport_stream_id = uuid4().hex
        self._transport_evidence = _TransportEvidence(0)
        self._transport_diagnostics = self._transport_evidence.state
        self._monitor_heartbeat_ms = None
        self._last_message_ms = None
        self._last_open_ms = None
        self._connect_timeout_s = 5
        self._monitor_interval_s = 0.05
        self._reconnect_delay_s = 1

    def _get_url(self):
        raise NotImplementedError()

    def _on_message(self, ws, message, *args, **kwargs):
        raise NotImplementedError()

    def _on_open(self, ws, *args, **kwargs):
        pass

    def _on_close(self, ws, *args, **kwargs):
        self.reconnect(ws, reason='socket_close')

    def _on_error(self, ws, error, *args, **kwargs):
        self.logger.debug(f"websocket connection error_type={type(error).__name__}")
        self.reconnect(ws, reason='socket_error')

    def _on_ping(self, ws, *args, **kwargs):
        pass

    def _on_pong(self, ws, message, *args, **kwargs):
        pass

    def send(self, message, opcode=ABNF.OPCODE_TEXT):
        # Waiting for connect while holding the send lock can deadlock recovery
        # when the monitor replays subscriptions after a failed first handshake.
        if (self.status == WebsocketStatus.INIT.name
                and current_thread() is not self._monitorThread):
            self.connect()
        with self._semaphore:
            ws = self.ws
            if self._stop_event.is_set() or not self._is_connected(ws):
                raise ExchangeException("websocket is not connected")
            ws.send(message, opcode=opcode)

    def send_json(self, message):
        self.send(json.dumps(message))

    def reconnect(self, ws=None, *, reason='connection_recovery') -> None:
        with self._state_lock:
            if not self._stop_event.is_set() and (ws is None or ws is self.ws):
                self._request_recovery_locked(reason)

    def _request_recovery_locked(self, reason):
        reason = reason if reason in LOCAL_CLOSE_REASONS else 'connection_recovery'
        if (reason in {'stop', 'url_rotation'}
                or self._pending_recovery_reason in {None, 'connection_recovery'}):
            self._pending_recovery_reason = reason
        # A request made while detached belongs to the next attempt, not the
        # retained evidence for a socket whose cleanup may still be running.
        if self.ws is not None:
            self._set_local_close_reason(reason)
        self._ready.clear()
        self._needRecovery.set()
        return reason

    def connect(self):
        with self._state_lock:
            if self._stop_event.is_set():
                return
            if self._monitorThread is None or not self._monitorThread.is_alive():
                self._monitorThread = Thread(
                    target=self._monitor, daemon=True, name='websocket-monitor',
                )
                self._monitorThread.start()
        # Preserve the synchronous first-connect contract used by send/login.
        # Recovery retries remain owned by the monitor even if a caller exits.
        while not self._stop_event.is_set():
            if self._ready.wait(self._monitor_interval_s):
                if not self._needRecovery.is_set() and self._is_connected(self.ws):
                    return

    def close(self):
        with self._state_lock:
            self._set_local_close_reason('stop')
            self._transport_diagnostics['phase'] = 'closed'
            self._stop_event.set()
            self.status = WebsocketStatus.STOP.name
            self._ready.clear()
            self._needRecovery.set()
            ws = self.ws
            evidence = self._transport_evidence
            self.ws = None
            self._pending_recovery_reason = None
        self._close_socket(ws, evidence)
        self._emit_transport_terminal(evidence)

    @staticmethod
    def _is_connected(ws):
        sock = getattr(ws, 'sock', None)
        return bool(sock is not None and sock.connected)

    def get_connection_health(self):
        """Copy generation and associated transport state in one short lock."""
        with self._state_lock:
            ws = self.ws
            monitor = self._monitorThread
            socket_thread = self._socket_thread
            status = self.status
            return {
                'status': status,
                'connected': bool(
                    status == WebsocketStatus.ACTIVE.name
                    and not self._stop_event.is_set()
                    and not self._needRecovery.is_set()
                    and self._is_connected(ws)
                ),
                'monitor_alive': bool(monitor and monitor.is_alive()),
                'socket_thread_alive': bool(socket_thread and socket_thread.is_alive()),
                'generation': self._generation,
                'monitor_heartbeat_ms': self._monitor_heartbeat_ms,
                'last_message_ms': self._last_message_ms,
                'last_open_ms': self._last_open_ms,
                'transport': self._copy_transport_locked(self._transport_evidence),
            }

    def _transport_log_context(self):
        return {'stream_id': self._transport_stream_id, 'exchange': 'unspecified',
                'stream_kind': 'unspecified', 'market': 'unspecified'}

    def _copy_transport_locked(self, evidence):
        if evidence.context is None:
            evidence.context = self._transport_log_context()
        return dict(evidence.context, **evidence.state)

    def get_transport_diagnostics(self):
        """Copy only allowlisted scalar evidence for the current generation."""
        with self._state_lock:
            return self._copy_transport_locked(self._transport_evidence)

    def _set_local_close_reason(self, reason, evidence=None):
        evidence = evidence or self._transport_evidence
        reason = reason if reason in LOCAL_CLOSE_REASONS else 'connection_recovery'
        if (reason in {'stop', 'url_rotation'}
                or evidence.state['local_close_reason'] in {None, 'connection_recovery'}):
            evidence.state['local_close_reason'] = reason

    def _capture_transport(self, ws):
        with self._state_lock:
            return self._transport_evidence if ws is self.ws else None

    def _record_transport_close(self, ws, reason, evidence=None):
        with self._state_lock:
            if evidence is not None:
                self._set_local_close_reason(reason, evidence)
            elif not self._stop_event.is_set() and (ws is None or ws is self.ws):
                self._set_local_close_reason(reason)

    def _record_heartbeat(self, ws, event):
        if event not in {'application_ping_attempt_ms', 'application_ping_sent_ms',
                         'application_pong_received_ms'}:
            return
        with self._state_lock:
            if ws is self.ws and not self._stop_event.is_set():
                self._transport_diagnostics[event] = get_timestamp_ms()
                if event == 'application_ping_attempt_ms':
                    self._transport_diagnostics['application_ping_sent_ms'] = None

    def _log_transport_event(self, event, ws, error=None, evidence=None):
        with self._state_lock:
            if evidence is None:
                if ws is not self.ws:
                    return
                evidence = self._transport_evidence
            if error is not None:
                self._record_transport_error(error, evidence.state['phase'], evidence)
            snapshot = self._copy_transport_locked(evidence)
        self._write_transport_event(event, snapshot)

    def _write_transport_event(self, event, snapshot):
        self.logger.debug(f"websocket_transport event={event} {json.dumps(snapshot, sort_keys=True)}")

    def _record_transport_error(self, error, phase, evidence=None):
        evidence = evidence or self._transport_evidence
        evidence.state.update(
            error_type=type(error).__name__, error_errno=_numeric_errno(error), error_phase=phase,
        )

    def _emit_transport_terminal(self, evidence):
        if evidence is None:
            return
        with self._state_lock:
            if evidence.terminal_logged or evidence.state['generation'] == 0:
                return
            evidence.terminal_logged = True
            evidence.state['phase'] = 'closed'
            snapshot = self._copy_transport_locked(evidence)
        self._write_transport_event('terminal', snapshot)

    def _monitor(self):
        try:
            while not self._stop_event.is_set():
                self._monitor_heartbeat_ms = get_timestamp_ms()
                try:
                    self._monitor_connection()
                except Exception as error:
                    # A callback/handshake failure must not kill the only
                    # recovery worker. Log types only: URLs can hold secrets.
                    self.logger.debug(
                        f"websocket monitor error_type={type(error).__name__}"
                    )
                    self.reconnect()
                    self._wait_reconnect_delay(self._reconnect_delay_s)
                self._stop_event.wait(self._monitor_interval_s)
        finally:
            self._disconnect()

    def _monitor_connection(self):
        if self._generation == 0 and self.ws is None:
            with self._state_lock:
                self._needRecovery.clear()
                self._pending_recovery_reason = None
            self._connect()
            return
        socket_thread = self._socket_thread
        if (self._needRecovery.is_set() or not self._is_connected(self.ws)
                or socket_thread is None or not socket_thread.is_alive()):
            with self._state_lock:
                if self._stop_event.is_set():
                    return
                self.status = WebsocketStatus.RECOVERY.name
                self._ready.clear()
                self._needRecovery.clear()
            self._recovery()

    def _connect(self):
        if self._stop_event.is_set():
            return
        if self.start_end_time_dict:
            self.start_end_time_dict['connect_status'] = True
        ws = WebSocketApp(
            self._get_url(),
            on_message=self._wrap_callback(self._on_message, 'message'),
            on_open=self._wrap_callback(self._on_open, 'open'),
            on_close=self._wrap_callback(self._on_close, 'close'),
            on_error=self._wrap_callback(self._on_error, 'error'),
            on_ping=self._wrap_callback(self._on_ping, 'ping'),
            on_pong=self._wrap_callback(self._on_pong, 'pong'),
        )
        with self._state_lock:
            if self._stop_event.is_set():
                return
            self._generation += 1
            evidence = _TransportEvidence(self._generation, self._transport_log_context())
            self._transport_evidence = evidence
            self._transport_diagnostics = evidence.state
            self.ws = ws
            self._socket_thread = Thread(
                target=self._run_websocket, args=(ws, evidence), daemon=True,
                name=f'websocket-socket-{self._generation}',
            )
            self._socket_thread.start()
        if not self._wait_connected(ws):
            reason = ('handshake_timeout'
                      if evidence.state['connect_wait_result'] == 'deadline_elapsed'
                      else 'connection_recovery')
            self._record_transport_close(ws, reason, evidence)
            self._log_transport_event('connect_failed', ws, evidence=evidence)
            self._disconnect()
            return
        with self._state_lock:
            if not self._stop_event.is_set() and ws is self.ws:
                self.status = WebsocketStatus.ACTIVE.name
                self._ready.set()

    def _wait_connected(self, ws):
        started_ns = get_monotonic_timestamp_ns()
        deadline_ns = started_ns + int(self._connect_timeout_s * 1e9)
        with self._state_lock:
            evidence = self._transport_evidence
        while True:
            result = self._get_connection_wait_result(ws, deadline_ns)
            if result is not None:
                elapsed_ms = max(0, (get_monotonic_timestamp_ns() - started_ns) // 1_000_000)
                with self._state_lock:
                    evidence.state.update(connect_wait_result=result, connect_wait_ms=elapsed_ms)
                    if result == 'recovery_requested':
                        self._set_local_close_reason(
                            self._pending_recovery_reason or 'connection_recovery', evidence,
                        )
                return result == 'connected'
            self._monitor_heartbeat_ms = get_timestamp_ms()
            self._stop_event.wait(self._monitor_interval_s)

    def _get_connection_wait_result(self, ws, deadline_ns):
        if self._stop_event.is_set():
            return 'stopped'
        if ws is not self.ws:
            return 'socket_replaced'
        if not self._socket_thread.is_alive():
            return 'socket_thread_exit'
        if self._needRecovery.is_set():
            return 'recovery_requested'
        if self._is_connected(ws):
            return 'connected'
        if get_monotonic_timestamp_ns() >= deadline_ns:
            return 'deadline_elapsed'
        return None

    def _begin_callback(self, ws, event_name, args):
        with self._state_lock:
            if self._stop_event.is_set() or ws is not self.ws:
                return None
            evidence = self._transport_evidence
            now_ms = get_timestamp_ms()
            if event_name == 'open':
                self._last_open_ms = now_ms
                evidence.state['phase'] = 'open'
            elif event_name == 'message':
                self._last_message_ms = now_ms
            elif event_name in {'ping', 'pong'}:
                evidence.state[f'protocol_{event_name}_received_ms'] = now_ms
            elif event_name == 'error' and args:
                self._record_transport_error(args[0], evidence.state['phase'], evidence)
                self._set_local_close_reason('socket_error', evidence)
            elif event_name == 'close':
                evidence.state['close_code'] = args[0] if args and type(args[0]) is int else None
                evidence.state['phase'] = 'closed'
                self._set_local_close_reason('socket_close', evidence)
            evidence.callback_sequence += 1
            evidence.state.update(callback_event=event_name, callback_started_ms=now_ms,
                                  callback_finished_ms=None, callback_duration_ms=None)
            return (evidence, event_name, evidence.callback_sequence,
                    now_ms, get_monotonic_timestamp_ns())

    def _finish_callback(self, ws, ticket, error):
        evidence, event_name, sequence, started_ms, started_ns = ticket
        elapsed_ms = max(0, (get_monotonic_timestamp_ns() - started_ns) // 1_000_000)
        completed = dict(callback_event=event_name, callback_started_ms=started_ms,
                         callback_finished_ms=get_timestamp_ms(), callback_duration_ms=elapsed_ms)
        with self._state_lock:
            retired = ws is not self.ws or evidence is not self._transport_evidence
            if sequence == evidence.callback_sequence:
                evidence.state.update(completed)
            if error is not None:
                self._record_transport_error(error, 'callback', evidence)
                self._set_local_close_reason('callback_error', evidence)
            snapshot = self._copy_transport_locked(evidence)
            snapshot.update(completed)
        if error is not None:
            self._write_transport_event('callback_error', snapshot)
        elif retired and event_name not in {'open', 'close', 'error'}:
            self._write_transport_event('callback_retired', snapshot)
        elif elapsed_ms >= SLOW_CALLBACK_MS:
            self._write_transport_event('callback_slow', snapshot)
        elif event_name in {'open', 'close', 'error'}:
            self._write_transport_event(event_name, snapshot)

    def _wrap_callback(self, callback, event_name=''):
        def wrapped(ws, *args, **kwargs):
            ticket = self._begin_callback(ws, event_name, args)
            if ticket is None:
                # A handshake can complete after close() saw sock=None.
                if event_name == 'open':
                    self._close_socket(ws)
                return
            failure = None
            try:
                callback(ws, *args, **kwargs)
            except Exception as error:
                failure = error
            finally:
                self._finish_callback(ws, ticket, failure)
            if failure is not None:
                self.reconnect(ws)
        return wrapped

    def _run_websocket(self, ws, evidence=None):
        evidence = evidence or self._capture_transport(ws)
        try:
            ws.run_forever(ping_interval=self._ping_interval)
        except Exception as error:
            self._log_transport_event('run_error', ws, error, evidence)
        finally:
            if evidence is not None:
                with self._state_lock:
                    self._set_local_close_reason('run_exit', evidence)
                    evidence.state.update(phase='closed', run_exit_ms=get_timestamp_ms())
                self._log_transport_event('run_exit', ws, evidence=evidence)
            self._close_socket(ws, evidence)
            self.reconnect(ws)
            self._emit_transport_terminal(evidence)

    def _close_socket(self, ws, evidence=None):
        evidence = evidence or self._capture_transport(ws)
        if ws is not None:
            try:
                ws.close(timeout=1)
            except Exception as error:
                self._log_transport_event('close_error', ws, error, evidence)

    def _disconnect(self):
        with self._state_lock:
            ws = self.ws
            evidence = self._transport_evidence
            self._set_local_close_reason('connection_recovery', evidence)
            evidence.state['phase'] = 'closed'
            self.ws = None
            # All requests for this socket are satisfied by detaching it.
            # Later callbacks fail the identity gate; new rotation requests
            # must remain set until the next replacement consumes them.
            self._needRecovery.clear()
            self._pending_recovery_reason = None
            self._ready.clear()
        self._close_socket(ws, evidence)
        socket_thread = self._socket_thread
        if socket_thread and socket_thread is not current_thread():
            socket_thread.join(timeout=1)
        self._emit_transport_terminal(evidence)

    def _wait_reconnect_delay(self, delay):
        return self._stop_event.wait(delay)

    def _recovery(self):
        self._disconnect()
        if self._stop_event.is_set():
            return
        self._wait_reconnect_delay(self._reconnect_delay_s)
        self._connect()
        if self.status == WebsocketStatus.ACTIVE.name and self._is_connected(self.ws):
            self._reconnect_streams()

    def _reconnect_streams(self):
        for sub in tuple(self._subs):
            self.send_json(sub)
