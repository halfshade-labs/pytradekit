import requests
from pytradekit.utils.dynamic_types import WazirxAuxiliary


class WazirxClient:
    def __init__(self, logger, key=None, secret=None, account_id=None, is_swap=False):
        self.api_key = key
        self.secret_key = secret
        self.account_id = account_id
        self.session = requests.session()
        self.logger = logger
        self._recvWindow = 5000
        if is_swap:
            self._url = WazirxAuxiliary.swap_url.value
        else:
            self._url = WazirxAuxiliary.url.value

    def _send_request(self, api, params=None):

        try:
            url = f'{self._url}{api}'
            response = requests.get(url=url, params=params)
            return response.json()
        except Exception as e:
            return e

    def get_ticker_24hr(self):
        url = WazirxAuxiliary.url_ticker.value
        datas = self._send_request(url)
        return datas

    def get_exchange_information(self):
        url = WazirxAuxiliary.url_exchange.value
        datas = self._send_request(url)
        return datas

    def get_orderbook(self, symbol, limit=100):
        params = {'symbol': symbol, 'limit': limit}
        url = WazirxAuxiliary.url_orderbook.value
        datas = self._send_request(url, params=params)
        return datas

    def get_klines(self, symbol):
        params = {'symbol': symbol,
                  'interval': '1d',
                  'limit': 1}
        url = WazirxAuxiliary.url_kline.value
        datas = self._send_request(url, params=params)
        return datas
