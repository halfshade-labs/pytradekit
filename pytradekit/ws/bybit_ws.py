import time
import json
import hmac

from pytradekit.utils.dynamic_types import BybitAuxiliary, BybitWebSocket
from pytradekit.gateway.websocket.ws_manager import WsManager
from pytradekit.utils.time_handler import get_timestamp_s, get_millisecond_str, get_datetime, get_bybit_timestamp


class BybitWsManager(WsManager):
    _listen_key = {}

    def __init__(self, logger, queue=None, api_key=None, api_secret=None, strategy_id=None, portfolio_id=None,
                 account_id=None, url=BybitAuxiliary.url_ws.value, api_url=BybitAuxiliary.url.value,
                 start_end_time_dict=None):
        super().__init__(api_key, logger, start_end_time_dict)
        self._api_url = api_url
        self._url = url
        self._api_key = api_key
        self._api_secret = api_secret
        self._queue = queue
        self._strategy_id = strategy_id
        self._portfolio_id = portfolio_id
        self._account_id = account_id
        self._ws_connected = False
        self.logger = logger

    def get_signature(self, expires):
        signature = hmac.new(bytes(self._api_secret, "utf-8"), bytes(f"GET/realtime{expires}", "utf-8"),
                             digestmod="sha256").hexdigest()
        return signature

    def _get_api_url(self) -> str:
        return self._api_url

    def _get_url(self) -> str:
        return self._url

    def _pong(self, ts) -> None:
        self.send(json.dumps({"action": "pong", "data": {"ts": ts}}))

    def _send_order_trade(self):
        _rqs_orders = {"op": "subscribe", "args": ["order", "execution"]}
        self._subs = [_rqs_orders]
        self.send_json(_rqs_orders)

    def _ping(self, n_seconds, reconnection_time=None) -> None:
        now_times = get_timestamp_s()
        time.sleep(5)
        while True:
            expires = get_bybit_timestamp()
            signature = self.get_signature(expires)
            _rqs = {
                'op': 'auth',
                'args': [self._api_key, expires, signature]
            }
            self.send_json(_rqs)
            self._send_order_trade()
            time.sleep(30)
            # self.send(json.dumps({"req_id": "100001", "op": "ping"}))
            # self.send(json.dumps({"op": "subscribe", "args": ["order", "execution"]}))
            # self._send_order_trade()

    def subscribe(self):
        try:
            self.start_subscribe()
            self._ping(BybitAuxiliary.ws_ping_sleep.value,
                       reconnection_time=BybitAuxiliary.reconnection_time_sleep.value)
        except Exception as e:
            self.logger.exception(e)

    def start_subscribe(self):
        try:
            expires = get_bybit_timestamp()
            signature = self.get_signature(expires)
            _rqs = {
                'op': 'auth',
                'args': [self._api_key, expires, signature]
            }
            self.send_json(_rqs)

        except Exception as e:
            self.logger.exception(e)

    def _on_message(self, _ws, message):
        try:
            msg = json.loads(message)
            if 'topic' in msg and (msg['topic'] == 'order' or msg['topic'] == 'execution'):
                res = {'data': msg['data'], 'type': msg['topic']}
                res[BybitWebSocket.portfolio_id.value] = self._portfolio_id
                res[BybitWebSocket.strategy_id.value] = self._strategy_id
                res[BybitWebSocket.account_id.value] = self._account_id
                res[BybitWebSocket.run_time_ms.value] = get_millisecond_str(get_datetime())
                self._queue.put_nowait(res)
        except Exception as e:
            self.logger.exception(e)
            self.logger.debug(f"bybit message error {e}")
