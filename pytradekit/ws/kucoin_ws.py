import time
import json
import hmac
import base64
import hashlib
import requests

from pytradekit.utils.dynamic_types import KucoinAuxiliary, HttpMmthod
from pytradekit.gateway.websocket.ws_manager import WsManager
from pytradekit.utils.time_handler import get_timestamp_s, get_timestamp_ms


class KucoinWsManager(WsManager):
    _listen_key = {}

    def __init__(self, logger, queue=None, api_key=None, api_secret=None, passphrase=None, strategy_id=None,
                 portfolio_id=None, account_id=None, url=KucoinAuxiliary.url_ws.value,
                 api_url=KucoinAuxiliary.url.value, start_end_time_dict=None):
        super().__init__(api_key, logger, start_end_time_dict)
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

    def get_signature(self, timestamp, method, api, params):
        msg = f'{timestamp}{method}{api}{params}'
        sign = base64.b64encode(
            hmac.new(self._api_secret.encode('utf-8'), msg.encode('utf-8'), hashlib.sha256).digest())
        return sign

    def get_ws_token(self, timestamp):
        api = KucoinAuxiliary.url_ws_token.value
        url = f"{self._get_api_url()}{api}"
        signature = self.get_signature(timestamp, HttpMmthod.POST.name, api, '')
        headers = {
            'Content-Type': 'application/json',
            "KC-API-KEY": self._api_key,
            "KC-API-TIMESTAMP": str(timestamp),
            "KC-API-PASSPHRASE": self._passphrase,
            "KC-API-SIGN": signature
        }
        response = requests.post(url, headers=headers)
        return response.json()['data']

    def _get_api_url(self) -> str:
        return self._api_url

    def _get_url(self) -> str:
        return self._url

    def _pong(self, ping_id) -> None:
        self.send(json.dumps({'id': ping_id, 'type': 'pong'}))

    def _ping(self, n_seconds, reconnection_time=None) -> None:
        now_times = get_timestamp_s()
        while True:
            if reconnection_time:
                if int(get_timestamp_s() - now_times) >= reconnection_time:
                    break
            time.sleep(n_seconds)
        self.subscribe()

    def subscribe(self):
        try:
            nonce = str(get_timestamp_ms())
            sign = self.get_ws_token(nonce)
            connect_id = f"test{nonce}"
            self._url = f"{sign['instanceServers'][0]['endpoint']}?token={sign['token']}&[connectId={connect_id}]"
            params = {
                "id": connect_id,
                "type": "subscribe",
                "topic": "/spotMarket/tradeOrders",
                "privateChannel": True,
                "response": True
            }
            self.start_subscribe(params)
            self._ping(KucoinAuxiliary.ws_ping_sleep.value,
                       reconnection_time=KucoinAuxiliary.reconnection_time_sleep.value)
        except:
            pass

    def start_subscribe(self, params):
        try:
            self._subs.append(params)
            self.send_json(params)
        except:
            pass

    def _on_message(self, _ws, message):
        msg = json.loads(message)
        try:
            if 'ping' in msg:
                self._pong(msg['id'])
        except Exception as e:
            self.logger.exception(e)
