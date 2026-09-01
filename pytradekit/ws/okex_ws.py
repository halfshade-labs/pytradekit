import time
import json
import hmac
import base64
from decimal import Decimal
from typing import Iterable, List

from pytradekit.utils.dynamic_types import OkexAuxiliary, OkexWebSocket, WebsocketStatus
from pytradekit.gateway.websocket.ws_manager import WsManager
from pytradekit.utils.time_handler import get_timestamp_s, get_millisecond_str, get_datetime
from pytradekit.ws.subscription_update import (
    SubscriptionUpdate,
    calculate_subscription_update,
    normalize_subscription_targets,
)


class OkexWsManager(WsManager):
    _listen_key = {}

    def __init__(self, logger, queue=None, api_key=None, api_secret=None, passphrase=None, strategy_id=None,
                 portfolio_id=None,
                 account_id=None, url=OkexAuxiliary.url_ws_public.value, api_url=OkexAuxiliary.url.value,
                 is_reconnecting_queue=None, start_end_time_dict=None, is_public=True):
        super().__init__(api_key, logger, is_reconnecting_queue, start_end_time_dict)
        if not is_public:
            url = OkexAuxiliary.url_ws_private.value
        self._api_url = api_url
        self._url = url
        self._api_key = api_key
        self._api_secret = api_secret
        self._passphrase = passphrase
        self._queue = queue
        self._strategy_id = strategy_id
        self._portfolio_id = portfolio_id
        self._account_id = account_id
        self._ws_connected = False
        self.logger = logger
        self._bookticker_symbols = frozenset()

    def get_signature(self, params):
        mac = hmac.new(bytes(self._api_secret, encoding='utf8'), bytes(params, encoding='utf-8'), digestmod='sha256')
        return base64.b64encode(mac.digest())

    def _get_api_url(self) -> str:
        return self._api_url

    def _get_url(self) -> str:
        return self._url

    def _pong(self, ts) -> None:
        self.send(json.dumps({"pong": ts}))

    def _send_order(self):
        _rqs_orders = {
            "op": "subscribe",
            "args": [{
                "channel": "orders",
                "instType": "SPOT"
            }]
        }
        if not self._subs:
            self._subs.append(_rqs_orders)
        self.send_json(_rqs_orders)

    def _ping(self, n_seconds, reconnection_time=None) -> None:
        while True:
            time.sleep(n_seconds)
            if self.status in (WebsocketStatus.RECOVERY.name, WebsocketStatus.INIT.name):
                continue
            try:
                self.send("ping")
            except Exception as e:
                self.logger.debug(f"okex heartbeat error: {e}")
                if self.status == WebsocketStatus.ACTIVE.name:
                    self.reconnect()
                continue

    def _login(self):
        nonce = str(get_timestamp_s())
        params = nonce + 'GET' + '/users/self/verify' + ''
        sign = self.get_signature(params)
        login_params = {
            'op': 'login',
            "args": [{
                "apiKey": self._api_key,
                "passphrase": self._passphrase,
                "timestamp": nonce,
                "sign": sign.decode("utf-8")
            }]
        }
        self.start_subscribe(login_params)

    def start_bookticker_stream(self, symbol_list: Iterable[str]) -> None:
        target_symbols = normalize_subscription_targets(symbol_list)
        args = self._build_bookticker_args(target_symbols)
        params = {"op": "subscribe", "args": args}
        self._bookticker_symbols = target_symbols
        if params not in self._subs:
            self._subs.append(params)
        self.start_subscribe(params)
        self._ping(20)

    @staticmethod
    def _build_bookticker_args(symbols: Iterable[str]) -> List[dict]:
        return [
            {"channel": "tickers", "instId": symbol}
            for symbol in sorted(symbols)
        ]

    def update_bookticker_stream(
        self,
        symbols: Iterable[str],
    ) -> SubscriptionUpdate:
        """Update a live public book-ticker stream in place."""
        target_symbols = normalize_subscription_targets(symbols)
        current_symbols = getattr(self, "_bookticker_symbols", frozenset())
        update = calculate_subscription_update(current_symbols, target_symbols)
        if not update.added and not update.removed:
            return update
        if update.added:
            self.send_json({
                "op": "subscribe",
                "args": self._build_bookticker_args(update.added),
            })
        if update.removed:
            self.send_json({
                "op": "unsubscribe",
                "args": self._build_bookticker_args(update.removed),
            })
        self._bookticker_symbols = target_symbols
        self._subs = [{
            "op": "subscribe",
            "args": self._build_bookticker_args(target_symbols),
        }]
        return update

    def subscribe(self):
        try:
            self._login()
            self._ping(OkexAuxiliary.ws_ping_sleep.value,
                       reconnection_time=OkexAuxiliary.reconnection_time_sleep.value)
        except Exception as e:
            self.logger.exception(e)

    def start_subscribe(self, login_params):
        try:
            self.send_json(login_params)
        except Exception as e:
            self.logger.exception(e)

    def _on_message(self, _ws, message):
        try:
            if message == 'pong':
                return
            msg = json.loads(message)
            if 'event' in msg and msg['event'] == 'login':
                if msg['code'] == '0':
                    self._send_order()
            elif "code" in msg and msg['code'] == '60011':
                self._login()

            #添加 Ticker 数据处理逻辑
            if 'arg' in msg and msg['arg']['channel'] == 'tickers' and "data" in msg:
                if self._queue:
                    # OKX 数据通常是列表，取出来放入队列
                    for item in msg['data']:
                        self._queue.put_nowait(item)

            #添加 trade
            if 'arg' in msg and msg['arg']['channel'] == 'orders' and "data" in msg:
                if self._queue:
                    # OKX 数据通常是列表，取出来放入队列
                    for item in msg['data']:
                        # `fee` and `accFillSz` are cumulative per order, while
                        # `fillSz` identifies a real execution update. Forward
                        # partial fills as well as the terminal filled snapshot;
                        # otherwise a partially-filled-then-cancelled order is
                        # invisible to residual-close accounting.
                        try:
                            has_fill = Decimal(str(item.get('fillSz') or 0)) > 0
                        except Exception:
                            has_fill = False
                        if "filled" == item.get('state') or has_fill:
                            self._queue.put_nowait(item)

        except Exception as e:
            self.logger.exception(e)
            self.logger.debug(message)
