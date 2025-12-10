import json
import time

from threading import Thread, Event, Semaphore
from websocket import WebSocketApp
from websocket._abnf import ABNF

from pytradekit.utils.tools import synchronized
from pytradekit.utils.dynamic_types import WebsocketStatus
from pytradekit.utils.exceptions import ExchangeException


class BaseWebsocketManager:

    def __init__(self, logger, start_end_time_dict):
        self._ping_interval = 0
        self._subs = []
        self.ws = None
        self.status = WebsocketStatus.INIT.name
        self.logger = logger
        self.start_end_time_dict = start_end_time_dict

    def _get_url(self):
        raise NotImplementedError()

    def _on_message(self, ws, message, *args, **kwargs):
        raise NotImplementedError()

    def _on_open(self):
        pass

    def _on_close(self):
        self.reconnect()

    def _on_error(self, ws, error):
        self.logger.debug(f"ws :{ws} connection error: {error}")
        self.reconnect()

    def _on_ping(self, ws, *args, **kwargs):
        pass

    def _on_pong(self, ws, message, *args, **kwargs):
        pass

    @synchronized
    def send(self, message, opcode=ABNF.OPCODE_TEXT):
        if self.status == WebsocketStatus.INIT.name:
            self.connect()
        self.ws.send(message, opcode=opcode)

    def send_json(self, message):
        self._semaphore = Semaphore()
        self.send(json.dumps(message))

    def reconnect(self) -> None:
        if self.status == WebsocketStatus.RECOVERY.name:
            return
        self._needRecovery.set()

    def connect(self):
        if self.status == WebsocketStatus.ACTIVE.name:
            return

        while not self.ws:
            self._connect()
            if self.ws:
                return

    def close(self):
        self.status = WebsocketStatus.STOP.name
        try:
            self.ws.close()
            self.ws = None
        except:
            pass

    def _monitor(self):
        ctr = 0
        while self.status != WebsocketStatus.STOP.name:
            ctr += 1
            if ctr >= 5:
                ctr = 0
                if self.status in [WebsocketStatus.RECOVERY.name, WebsocketStatus.INIT.name]:
                    continue
                if not self.ws.sock or not self.ws.sock.connected or self._needRecovery.is_set():
                    self.status = WebsocketStatus.RECOVERY.name
                    self._needRecovery.clear()
                    self._recovery()

            time.sleep(0.01)

    def _connect(self):
        assert not self.ws, "ws should be closed before attempting to connect"

        if self.start_end_time_dict:
            self.start_end_time_dict['connect_status'] = True

        self._needRecovery = Event()
        self._needRecovery.clear()

        self._monitorThread = Thread(target=self._monitor)
        self._monitorThread.daemon = True
        self._monitorThread.start()
        self.ws = WebSocketApp(
            self._get_url(),
            on_message=self._wrap_callback(self._on_message),
            on_open=self._wrap_callback(self._on_open),
            on_close=self._wrap_callback(self._on_close),
            on_error=self._wrap_callback(self._on_error),
            on_ping=self._on_ping,
            on_pong=self._on_pong,
        )

        wst = Thread(target=self._run_websocket, args=(self.ws,))
        wst.daemon = True
        wst.start()

        ctr = 0
        while self.ws and (not self.ws.sock or not self.ws.sock.connected):
            if ctr > 5 / 0.01:
                self.ws = None
                return
            ctr += 1
            time.sleep(0.01)
        self.status = WebsocketStatus.ACTIVE.name

    def _wrap_callback(self, f):
        def wrapped_f(ws, *args, **kwargs):
            if ws is self.ws:
                try:
                    f(ws, *args, **kwargs)
                except Exception as e:
                    raise ExchangeException(f'Error running websocket callback: {f.__name__}') from e

        return wrapped_f

    def _run_websocket(self, ws):
        try:
            ws.run_forever(ping_interval=self._ping_interval)  # , ping_payload='Hello Server!')
        except Exception as e:
            raise ExchangeException('Unexpected error while running websocket') from e
        finally:
            self.reconnect()

    def _recovery(self):
        try:
            if self.ws:
                self.ws.close()
        except:
            self.logger.debug("websocket recovery error")
        self.ws = None
        self.connect()
        self._reconnect_streams()

    def _reconnect_streams(self):
        self.logger.debug("reconnect_streams: ", self._subs)
        for sub in self._subs:
            self.send_json(sub)
