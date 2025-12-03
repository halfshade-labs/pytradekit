import json
import hmac
import hashlib
import time

from pytradekit.utils.dynamic_types import BitfinexAuxiliary, BitfinexWebSocket
from pytradekit.gateway.websocket.ws_manager import WsManager
from pytradekit.utils.time_handler import get_timestamp_ms, get_timestamp_s, get_millisecond_str, get_datetime


class BitfinexWsManager(WsManager):
    _listen_key = {}

    def __init__(self, logger, queue=None, api_key=None, api_secret=None, strategy_id=None, portfolio_id=None,
                 account_id=None, url=BitfinexAuxiliary.url_ws.value, api_url=BitfinexAuxiliary.url.value,
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

    def get_signature(self, auth_payload):
        return hmac.new(self._api_secret.encode(), auth_payload.encode(), hashlib.sha384).hexdigest()

    def _get_api_url(self) -> str:
        return self._api_url

    def _get_url(self) -> str:
        return self._url

    def _pong(self) -> None:
        self.send(json.dumps({"pong": get_timestamp_ms()}))

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
            self.start_subscribe()
            self._ping(BitfinexAuxiliary.ws_ping_sleep.value,
                       reconnection_time=BitfinexAuxiliary.reconnection_time_sleep.value)
        except Exception as e:
            self.logger.exception(e)

    def start_subscribe(self):
        try:
            nonce = str(get_timestamp_ms())
            auth_payload = f'AUTH{nonce}'
            signature = self.get_signature(auth_payload)
            msg = {
                "event": "auth",
                "apiKey": self._api_key,
                "authSig": signature,
                "authPayload": auth_payload,
                "authNonce": nonce
            }
            # self._subs.append(msg)
            self._subs = [msg]
            self.send_json(msg)
        except Exception as e:
            self.logger.exception(e)

    def _on_message(self, _ws, message):
        msg = json.loads(message)
        try:
            if isinstance(msg, list):
                if len(msg) >= 3 and msg[0] == 0:
                    if msg[1] in [BitfinexWebSocket.order_type_new.value, BitfinexWebSocket.order_type_update.value,
                                  BitfinexWebSocket.order_type_cannel.value]:
                        res = {'data': msg}
                        res[BitfinexWebSocket.run_time_ms.value] = get_millisecond_str(get_datetime())
                        res[BitfinexWebSocket.portfolio_id.value] = self._portfolio_id
                        res[BitfinexWebSocket.strategy_id.value] = self._strategy_id
                        res[BitfinexWebSocket.account_id.value] = self._account_id
                        self._queue.put_nowait(res)
        except Exception as e:
            self.logger.exception(e)
            self.logger.debug(f"bifinex message error {msg}")
