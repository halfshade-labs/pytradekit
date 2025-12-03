import time
import json
import hmac
import base64

from pytradekit.utils.dynamic_types import OkexAuxiliary, OkexWebSocket
from pytradekit.gateway.websocket.ws_manager import WsManager
from pytradekit.utils.time_handler import get_timestamp_s, get_millisecond_str, get_datetime


class OkexWsManager(WsManager):
    _listen_key = {}

    def __init__(self, logger, queue=None, api_key=None, api_secret=None, passphrase=None, strategy_id=None,
                 portfolio_id=None,
                 account_id=None, url=OkexAuxiliary.url_ws.value, api_url=OkexAuxiliary.url.value,
                 is_reconnecting_queue=None, start_end_time_dict=None):
        super().__init__(api_key, logger, is_reconnecting_queue, start_end_time_dict)
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
        now_times = get_timestamp_s()
        time.sleep(3)
        while True:
            # if reconnection_time:
            #     if int(get_timestamp_s() - now_times) >= reconnection_time:
            #         break
            self._send_order()
            time.sleep(30)
        # self.subscribe()

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
        msg = json.loads(message)
        try:
            if 'event' in msg and msg['event'] == 'login':
                if msg['code'] == '0':
                    self._send_order()
            elif "code" in msg and msg['code'] == '60011':
                self._login()
            if 'arg' in msg and msg['arg']['channel'] == 'orders' and "data" in msg:
                res = {'data': msg['data']}
                res[OkexWebSocket.portfolio_id.value] = self._portfolio_id
                res[OkexWebSocket.strategy_id.value] = self._strategy_id
                res[OkexWebSocket.account_id.value] = self._account_id
                res[OkexWebSocket.run_time_ms.value] = get_millisecond_str(get_datetime())
                self._queue.put_nowait(res)
            elif 'ping' in msg:
                self._pong(msg['ping'])
        except Exception as e:
            self.logger.exception(e)
            self.logger.debug(msg)
