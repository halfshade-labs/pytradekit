import requests
from pytradekit.utils.dynamic_types import BullishAuxiliary


class BullishClient:
    def __init__(self, logger, key=None, secret=None, account_id=None, is_swap=False):
        self.api_key = key
        self.secret_key = secret
        self.account_id = account_id
        self.session = requests.session()
        self.logger = logger
        self._recvWindow = 5000
        if is_swap:
            self._url = BullishAuxiliary.swap_url.value
        else:
            self._url = BullishAuxiliary.url.value

    def _send_request(self, api, params=None):

        try:
            url = f'{self._url}{api}'
            response = requests.get(url=url, params=params)
            return response.json()
        except Exception as e:
            return e

    def get_ticker_24hr(self, symbol):
        url = BullishAuxiliary.url_exchange.value + f"/{symbol}" + BullishAuxiliary.url_ticker.value
        datas = self._send_request(url)
        return datas

    def get_exchange_information(self):
        url = BullishAuxiliary.url_exchange.value
        datas = self._send_request(url)
        return datas

    def get_orderbook(self, symbol, limit=100):
        params = {'depth': limit}
        url = BullishAuxiliary.url_exchange.value + f"/{symbol}" + BullishAuxiliary.url_orderbook.value
        datas = self._send_request(url, params=params)
        return datas
