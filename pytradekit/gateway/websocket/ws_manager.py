import time

import requests

from pytradekit.gateway.websocket.base_ws_manager import BaseWebsocketManager
from pytradekit.utils.dynamic_types import BinanceAuxiliary, BinanceWebSocket


class WsManager(BaseWebsocketManager):

    def __init__(self, api_key, logger, start_end_time_dict, api_url=None, url=None):
        super().__init__(logger, start_end_time_dict)
        self._api_key = api_key
        self._ping_interval = 60
        self._cur_id = 0
        self._api_url = api_url
        self._url = url
        self.logger = logger

    def _get_api_url(self) -> str:
        return self._api_url

    def _get_url(self) -> str:
        return self._url

    def _post_listen_key(self):
        self._listen_key = requests.post(url=self._get_api_url() + BinanceAuxiliary.user_data_stream.value,
                                         headers={'X-MBX-APIKEY': self._api_key}).json()['listenKey']

    def _put_listen_key(self):
        requests.put(url=self._get_api_url() + BinanceAuxiliary.user_data_stream.value,
                     headers={'X-MBX-APIKEY': self._api_key}).json()

    def _delete_listen_key(self):
        requests.delete(url=self._get_api_url() + BinanceAuxiliary.user_data_stream.value,
                        headers={'X-MBX-APIKEY': self._api_key}).json()

    def send_json(self, message):
        msg = message.copy()
        msg['id'] = self._cur_id
        self._cur_id += 1
        super().send_json(msg)

    def _reconnect_streams(self):
        for sub in self._subs:
            self.send_json(sub)

    def _ping(self, n_seconds) -> None:
        while True:
            self._put_listen_key()
            time.sleep(n_seconds)

    def _start_stream(self, params):
        sub = {'method': BinanceWebSocket.subscribe.value, 'params': params}
        if sub not in self._subs:
            self._subs.append(sub)
        self.send_json(sub)
