"""Binance WS-API session user-data stream (listenKey-free).

Binance is retiring the listenKey model entirely (#83): the WS-API
``userDataStream.start/ping/stop`` methods are deprecated in favour of a
session-based subscription — ``session.logon`` with an Ed25519 key, then
``userDataStream.subscribe`` on the same connection; account events
(executionReport / outboundAccountPosition / balanceUpdate / ...) are
delivered as frames on that connection.

Shadow mode runs this channel in parallel with the legacy listenKey
stream: events are counted and logged but NOT pushed to the business
queue, so downstream trade recording is not duplicated during the
observation window. After event counts match the legacy channel, flip
``shadow=False`` (or construct with the business queue) and retire the
listenKey stream.
"""
import base64
import json
import time
from collections import defaultdict
from threading import Thread

from cryptography.hazmat.primitives import serialization
from nacl.signing import SigningKey
from websocket import WebSocketApp

from pytradekit.utils.time_handler import get_timestamp_ms

WS_API_URL = 'wss://ws-api.binance.com:443/ws-api/v3'
# run_forever ping keeps intermediaries from dropping the idle connection;
# Binance also pings server-side every ~20s (websocket-client auto-pongs).
PING_INTERVAL_S = 180
PING_TIMEOUT_S = 10
RECONNECT_BASE_DELAY_S = 1
RECONNECT_MAX_DELAY_S = 60


class BinanceWsApiUserData:
    """Session-based BN user-data subscriber with reconnect + re-logon.

    Args:
        logger: standard logger.
        api_key: BN API key registered with an Ed25519 public key.
        private_key_pem: the Ed25519 private key in PEM form (same input
            the REST ``BinanceClient`` takes as ``secret``).
        passphrase: PEM encryption password, if any.
        queue: business queue receiving raw event dicts (same payload shape
            the legacy listenKey stream delivered), ignored in shadow mode.
        shadow: count/log events only; do not push to ``queue``.
        account_id: label used in logs only.
    """

    def __init__(self, logger, api_key, private_key_pem, passphrase=None,
                 queue=None, shadow=True, account_id=None, url=WS_API_URL):
        self.logger = logger
        self._api_key = api_key
        self._signing_key = self._load_signing_key(private_key_pem, passphrase)
        self._queue = queue
        self.shadow = shadow
        self.account_id = account_id
        self._url = url
        self._running = False
        self._thread = None
        self._ws = None
        self._logged_on = False
        self._subscribed = False
        self._reconnect_delay = RECONNECT_BASE_DELAY_S
        self._pending = {}  # request id -> method, to route acks
        # shadow-comparison counters
        self.event_counts = defaultdict(int)
        self.last_event_ms = None
        self.connected_since_ms = None

    # ── signing ─────────────────────────────────────────────

    @staticmethod
    def _load_signing_key(private_key_pem, passphrase=None):
        private_key = serialization.load_pem_private_key(
            private_key_pem.encode(),
            password=passphrase.encode() if passphrase else None,
        )
        raw = private_key.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        )
        return SigningKey(raw)

    def _sign(self, params):
        """Ed25519-sign the alphabetically ordered query string, base64 out."""
        payload = '&'.join(f'{k}={params[k]}' for k in sorted(params))
        signed = self._signing_key.sign(payload.encode('utf-8')).signature
        return base64.b64encode(signed).decode('ascii')

    # ── requests ────────────────────────────────────────────

    def _send(self, ws, method, params=None):
        # BN requires id to match ^[a-zA-Z0-9-_]{1,36}$ — method names contain
        # dots, so build the id from a sanitized prefix + timestamp.
        prefix = method.split('.')[-1]
        request_id = f'{prefix}-{get_timestamp_ms()}'
        msg = {'id': request_id, 'method': method}
        if params:
            msg['params'] = params
        self._pending[request_id] = method
        ws.send(json.dumps(msg))
        return request_id

    def _logon(self, ws):
        params = {'apiKey': self._api_key, 'timestamp': get_timestamp_ms()}
        params['signature'] = self._sign(params)
        self._send(ws, 'session.logon', params)

    # ── callbacks ───────────────────────────────────────────

    def _on_open(self, ws):
        self.connected_since_ms = get_timestamp_ms()
        self.logger.debug(f"bn ws-api connected ({self._label()}), logging on")
        self._logon(ws)

    def _on_message(self, ws, message):
        try:
            msg = json.loads(message)
        except (TypeError, ValueError) as e:
            self.logger.debug(f"bn ws-api undecodable frame: {e}")
            return
        if 'id' in msg:
            self._handle_ack(ws, msg)
            return
        event = self._extract_event(msg)
        if event is not None:
            self._dispatch(event)

    def _handle_ack(self, ws, msg):
        method = self._pending.pop(msg['id'], 'unknown')
        if msg.get('status') == 200:
            if method == 'session.logon':
                self._logged_on = True
                self._reconnect_delay = RECONNECT_BASE_DELAY_S
                self.logger.debug(f"bn ws-api session.logon ok ({self._label()})")
                self._send(ws, 'userDataStream.subscribe')
            elif method == 'userDataStream.subscribe':
                self._subscribed = True
                mode = 'shadow' if self.shadow else 'live'
                self.logger.info(f"bn ws-api user-data subscribed ({self._label()}, mode={mode})")
        else:
            # logon/subscribe rejection is not recoverable on this socket;
            # close and let the run loop reconnect with backoff.
            self.logger.debug(f"bn ws-api {method} failed: {msg.get('error')}")
            ws.close()

    @staticmethod
    def _extract_event(msg):
        """Subscription frames arrive as {"event": {...}}; tolerate a raw
        event dict too so a format drift doesn't silently drop events."""
        event = msg.get('event')
        if isinstance(event, dict):
            return event
        if 'e' in msg:
            return msg
        return None

    def _dispatch(self, event):
        event_type = event.get('e', 'unknown')
        self.event_counts[event_type] += 1
        self.last_event_ms = get_timestamp_ms()
        if self.shadow:
            self.logger.debug(
                f"[shadow] bn ws-api event {event_type} "
                f"({event.get('s', '')}, total={self.event_counts[event_type]})"
            )
            return
        if self._queue is not None:
            self._queue.put(event)

    def _on_error(self, _ws, error):
        self.logger.debug(f"bn ws-api error ({self._label()}): {error}")

    def _on_close(self, _ws, status_code, close_msg):
        self._logged_on = False
        self._subscribed = False
        self.logger.debug(
            f"bn ws-api closed ({self._label()}): code={status_code} msg={close_msg}"
        )

    # ── lifecycle ───────────────────────────────────────────

    def _run(self):
        while self._running:
            self._ws = WebSocketApp(
                self._url,
                on_open=self._on_open,
                on_message=self._on_message,
                on_error=self._on_error,
                on_close=self._on_close,
            )
            try:
                self._ws.run_forever(ping_interval=PING_INTERVAL_S, ping_timeout=PING_TIMEOUT_S)
            except Exception as e:
                self.logger.debug(f"bn ws-api run_forever error ({self._label()}): {e}")
            if not self._running:
                break
            time.sleep(self._reconnect_delay)
            self._reconnect_delay = min(self._reconnect_delay * 2, RECONNECT_MAX_DELAY_S)
            self.logger.debug(f"bn ws-api reconnecting ({self._label()})")

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = Thread(target=self._run, daemon=True, name='bn-ws-api-user-data')
        self._thread.start()

    def stop(self):
        self._running = False
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception as e:
                self.logger.debug(f"bn ws-api close error: {e}")

    # ── observability ───────────────────────────────────────

    def is_healthy(self):
        return self._logged_on and self._subscribed

    def stats(self):
        """Snapshot for shadow-vs-legacy event count comparison."""
        return {
            'account_id': self.account_id,
            'shadow': self.shadow,
            'logged_on': self._logged_on,
            'subscribed': self._subscribed,
            'connected_since_ms': self.connected_since_ms,
            'last_event_ms': self.last_event_ms,
            'event_counts': dict(self.event_counts),
        }

    def _label(self):
        return self.account_id or 'BN'
