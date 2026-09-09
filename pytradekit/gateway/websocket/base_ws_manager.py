import json
from threading import Event, Lock, Semaphore, Thread, current_thread

from websocket import WebSocketApp
from websocket._abnf import ABNF

from pytradekit.utils.dynamic_types import WebsocketStatus
from pytradekit.utils.exceptions import ExchangeException
from pytradekit.utils.time_handler import get_monotonic_timestamp_ns, get_timestamp_ms


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
        self._ready = Event()
        self._monitorThread = None
        self._socket_thread = None
        self._generation = 0
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
        self.reconnect(ws)

    def _on_error(self, ws, error, *args, **kwargs):
        self.logger.debug(f"websocket connection error_type={type(error).__name__}")
        self.reconnect(ws)

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

    def reconnect(self, ws=None) -> None:
        with self._state_lock:
            if not self._stop_event.is_set() and (ws is None or ws is self.ws):
                self._ready.clear()
                self._needRecovery.set()

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
            self._stop_event.set()
            self.status = WebsocketStatus.STOP.name
            self._ready.clear()
            self._needRecovery.set()
            ws = self.ws
            self.ws = None
        self._close_socket(ws)

    @staticmethod
    def _is_connected(ws):
        sock = getattr(ws, 'sock', None)
        return bool(sock is not None and sock.connected)

    def get_connection_health(self):
        """Return a non-blocking snapshot; a quiet account is not unhealthy."""
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
        }

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
            self._needRecovery.clear()
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
            on_close=self._wrap_callback(self._on_close),
            on_error=self._wrap_callback(self._on_error),
            on_ping=self._wrap_callback(self._on_ping),
            on_pong=self._wrap_callback(self._on_pong),
        )
        with self._state_lock:
            if self._stop_event.is_set():
                return
            self._generation += 1
            self.ws = ws
            self._socket_thread = Thread(
                target=self._run_websocket, args=(ws,), daemon=True,
                name=f'websocket-socket-{self._generation}',
            )
            self._socket_thread.start()
        if not self._wait_connected(ws):
            self._disconnect()
            return
        with self._state_lock:
            if not self._stop_event.is_set() and ws is self.ws:
                self.status = WebsocketStatus.ACTIVE.name
                self._ready.set()

    def _wait_connected(self, ws):
        deadline_ns = get_monotonic_timestamp_ns() + int(self._connect_timeout_s * 1e9)
        while not self._stop_event.is_set() and ws is self.ws:
            self._monitor_heartbeat_ms = get_timestamp_ms()
            if self._is_connected(ws):
                return True
            if (not self._socket_thread.is_alive() or self._needRecovery.is_set()
                    or get_monotonic_timestamp_ns() >= deadline_ns):
                return False
            self._stop_event.wait(self._monitor_interval_s)
        return False

    def _wrap_callback(self, callback, event_name=''):
        def wrapped(ws, *args, **kwargs):
            if self._stop_event.is_set() or ws is not self.ws:
                # A handshake can complete after close() saw sock=None. Close
                # that late transport too; ignoring its callback leaks a loop.
                if event_name == 'open':
                    self._close_socket(ws)
                return
            if event_name == 'open':
                self._last_open_ms = get_timestamp_ms()
            elif event_name == 'message':
                self._last_message_ms = get_timestamp_ms()
            try:
                callback(ws, *args, **kwargs)
            except Exception as error:
                self.logger.debug(
                    f"websocket callback error_type={type(error).__name__}"
                )
                self.reconnect(ws)
        return wrapped

    def _run_websocket(self, ws):
        try:
            ws.run_forever(ping_interval=self._ping_interval)
        except Exception as error:
            self.logger.debug(f"websocket run error_type={type(error).__name__}")
        finally:
            self._close_socket(ws)
            self.reconnect(ws)

    def _close_socket(self, ws):
        if ws is not None:
            try:
                ws.close(timeout=1)
            except Exception as error:
                self.logger.debug(f"websocket close error_type={type(error).__name__}")

    def _disconnect(self):
        with self._state_lock:
            ws = self.ws
            self.ws = None
            self._ready.clear()
        self._close_socket(ws)
        socket_thread = self._socket_thread
        if socket_thread and socket_thread is not current_thread():
            socket_thread.join(timeout=1)

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
