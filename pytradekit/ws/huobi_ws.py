import time
import json
import hmac
import base64
import hashlib
import urllib.parse
import gzip

from pytradekit.utils.dynamic_types import HuobiAuxiliary, HuobiWebSocket
from pytradekit.gateway.websocket.ws_manager import WsManager
from pytradekit.utils.time_handler import get_htx_timestamp, get_timestamp_s, get_millisecond_str, get_datetime


class HuobiWsManager(WsManager):
    _listen_key = {}

    def __init__(self, logger, queue=None, api_key=None, api_secret=None, strategy_id=None, portfolio_id=None,
                 account_id=None, url=HuobiAuxiliary.url_ws.value, api_url=HuobiAuxiliary.url.value,
                 is_reconnecting_queue=None, start_end_time_dict=None):
        super().__init__(api_key, logger, is_reconnecting_queue, start_end_time_dict)
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

    def get_signature(self, params):
        sorted_params = sorted(params.items(), key=lambda d: d[0], reverse=False)
        paramurl = urllib.parse.urlencode(sorted_params)
        payload = ["GET", 'api.huobi.pro', "/ws/v2", paramurl]
        signatureraw = "\n".join(payload)
        digest = hmac.new(self._api_secret.encode(encoding='UTF8'), signatureraw.encode(encoding='UTF8'),
                          digestmod=hashlib.sha256).digest()
        signature = base64.b64encode(digest)
        signature = signature.decode()
        return signature

    def get_sign_params(self):
        return {
            'accessKey': self._api_key,
            'signatureMethod': 'HmacSHA256',
            'signatureVersion': '2.1',
            'timestamp': get_htx_timestamp()
        }

    def _get_api_url(self) -> str:
        return self._api_url

    def _get_url(self) -> str:
        return self._url

    def _pong(self, ts) -> None:
        self.send(json.dumps({"action": "pong", "data": {"ts": ts}}))

    def _send_order(self):
        _rqs_orders = {'action': 'sub', 'ch': 'orders#*'}
        if not self._subs:
            self._subs.append(_rqs_orders)
        self.send_json(_rqs_orders)

    def _ping(self, n_seconds, reconnection_time=None) -> None:
        now_times = get_timestamp_s()
        while True:
            if reconnection_time:
                if int(get_timestamp_s() - now_times) >= reconnection_time:
                    break
            time.sleep(n_seconds)
        self.subscribe()

    def start_bookticker_stream(self, symbol_list):
        for index, symbol in enumerate(symbol_list):
            topic = f"market.{symbol.lower()}.bbo"
            params = {'sub': topic}
            if params not in self._subs:
                self._subs.append(params)
        req = {'ch': 'sub', 'params': self._subs}
        self.start_subscribe(req)
        self._ping(HuobiAuxiliary.ws_ping_sleep.value,
                   reconnection_time=HuobiAuxiliary.reconnection_time_sleep.value)

    def subscribe(self):
        try:
            self._login()
            self._ping(HuobiAuxiliary.ws_ping_sleep.value,
                       reconnection_time=HuobiAuxiliary.reconnection_time_sleep.value)
        except Exception as e:
            self.logger.exception(e)

    def _login(self):
        payload = self.get_sign_params()
        signature = self.get_signature(payload)
        params = {**payload, **{'authType': 'api', 'signature': signature}}
        _rqs = {'action': 'req', 'ch': 'auth', 'params': params}
        self.start_subscribe(_rqs)

    def start_subscribe(self, params):
        try:
            self.send_json(params)
        except Exception as e:
            self.logger.exception(e)

    def _on_message(self, _ws, message):
        try:
            if isinstance(message, bytes):
                message = gzip.decompress(message).decode('utf-8')
            msg = json.loads(message)
            if 'ping' in msg:
                self.send(json.dumps({'pong': msg['ping']}))
                return
            if 'action' in msg and msg['action'] == 'ping':
                self._pong(msg['data']['ts'])
                return
            if 'ch' in msg and 'bbo' in msg['ch']:
                self._queue.put_nowait(msg)
                return
        except Exception as e:
            self.logger.exception(e)
            self.logger.debug(f"huobi message error {message}")
