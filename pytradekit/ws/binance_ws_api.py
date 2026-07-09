"""Binance WebSocket API user-data subscription client (session-based, Ed25519).

Replaces the deprecated listenKey model (`userDataStream.start/ping/stop`) with
`session.logon` + `userDataStream.subscribe` on wss://ws-api.binance.com
(see pytradekit#83). Events delivered on this channel carry the same payloads
as the legacy user-data stream (executionReport, outboundAccountPosition, ...).

Shadow mode: when running in parallel with the legacy stream, set shadow=True so
events are only counted and debug-logged — never pushed to the business queue —
to avoid double-processing during the observation window.
"""
import base64
import json
import threading
from urllib.parse import urlencode

import websocket
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from pytradekit.utils.time_handler import get_timestamp_ms

WS_API_URL = 'wss://ws-api.binance.com:443/ws-api/v3'
RECONNECT_DELAY_S = 5
PING_INTERVAL_S = 180

LOGON_ID = 'logon'
SUBSCRIBE_ID = 'subscribe'


class Ed25519RequestSigner:
    """Sign WS-API request params with an Ed25519 private key (PEM)."""

    def __init__(self, private_key_pem: bytes):
        key = serialization.load_pem_private_key(private_key_pem, password=None)
        if not isinstance(key, Ed25519PrivateKey):
            raise ValueError("private key is not Ed25519")
        self._key = key

    @classmethod
    def from_file(cls, path: str) -> "Ed25519RequestSigner":
        with open(path, 'rb') as f:
            return cls(f.read())

    def sign(self, params: dict) -> str:
        """Return base64 signature over the alphabetically-sorted query string."""
        payload = urlencode(sorted(params.items()))
        return base64.b64encode(self._key.sign(payload.encode('ascii'))).decode('ascii')


class BinanceWsApiUserData:
    """Session-based user-data subscription over the Binance WebSocket API.

    Lifecycle: connect -> session.logon (Ed25519) -> userDataStream.subscribe ->
    events flow on the same connection. On disconnect, reconnects and re-logons.
    """

    def __init__(self, logger, api_key: str, signer: Ed25519RequestSigner,
                 queue=None, on_event=None, shadow: bool = False, url: str = WS_API_URL):
        self.logger = logger
        self._api_key = api_key
        self._signer = signer
        self._queue = queue
        self._on_event = on_event
        self.shadow = shadow
        self._url = url
        self._ws = None
        self._stop = threading.Event()
        self._thread = None
        # shadow/diagnostic counters, keyed by event type ('e' field)
        self.event_counts: dict = {}
        self.last_event_ms: int = 0
        self.subscribed = False

    # ---- request builders (pure, unit-testable) ----

    def build_logon(self) -> dict:
        params = {
            'apiKey': self._api_key,
            'timestamp': get_timestamp_ms(),
        }
        params['signature'] = self._signer.sign(params)
        return {'id': LOGON_ID, 'method': 'session.logon', 'params': params}

    @staticmethod
    def build_subscribe() -> dict:
        return {'id': SUBSCRIBE_ID, 'method': 'userDataStream.subscribe'}

    # ---- message handling ----

    def handle_message(self, raw: str):
        try:
            msg = json.loads(raw)
        except (ValueError, TypeError):
            self.logger.debug(f"ws-api non-json message: {str(raw)[:120]}")
            return
        if not isinstance(msg, dict):
            return
        if 'id' in msg:
            self._handle_response(msg)
            return
        # user-data events arrive either wrapped as {"event": {...}} or bare
        event = msg.get('event') if isinstance(msg.get('event'), dict) else msg
        if isinstance(event, dict) and 'e' in event:
            self._handle_event(event)

    def _handle_response(self, msg: dict):
        req_id, status = msg.get('id'), msg.get('status')
        if req_id == LOGON_ID:
            if status == 200:
                self.logger.debug("ws-api session.logon OK, subscribing user data stream")
                self._send(self.build_subscribe())
            else:
                self.logger.warning(f"ws-api session.logon failed: {msg.get('error')}")
        elif req_id == SUBSCRIBE_ID:
            if status == 200:
                self.subscribed = True
                self.logger.debug("ws-api userDataStream.subscribe OK")
            else:
                self.logger.warning(f"ws-api subscribe failed: {msg.get('error')}")

    def _handle_event(self, event: dict):
        etype = event.get('e', '?')
        self.event_counts[etype] = self.event_counts.get(etype, 0) + 1
        self.last_event_ms = get_timestamp_ms()
        if self.shadow:
            self.logger.debug(f"ws-api shadow event: {etype} (count={self.event_counts[etype]})")
            return
        if self._on_event is not None:
            self._on_event(event)
        elif self._queue is not None:
            self._queue.put(event)

    # ---- connection lifecycle ----

    def _send(self, message: dict):
        if self._ws is not None:
            self._ws.send(json.dumps(message))

    def _on_open(self, _ws):
        self.subscribed = False
        self.logger.debug("ws-api connection open, sending session.logon")
        self._send(self.build_logon())

    def _on_close(self, _ws, *args):
        self.subscribed = False
        self.logger.debug(f"ws-api connection closed: {args}")

    def _on_error(self, _ws, error):
        self.logger.debug(f"ws-api error: {error}")

    def _run(self):
        while not self._stop.is_set():
            self._ws = websocket.WebSocketApp(
                self._url,
                on_open=self._on_open,
                on_message=lambda _ws, raw: self.handle_message(raw),
                on_close=self._on_close,
                on_error=self._on_error,
            )
            self._ws.run_forever(ping_interval=PING_INTERVAL_S)
            if not self._stop.is_set():
                self.logger.debug(f"ws-api disconnected, reconnecting in {RECONNECT_DELAY_S}s")
                self._stop.wait(RECONNECT_DELAY_S)

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True, name='bn_ws_api_user_data')
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._ws is not None:
            self._ws.close()
