import hashlib
import hmac
from urllib.parse import urlencode
import requests
from pytradekit.utils.dynamic_types import LbankAuxiliary, HttpMmthod
from pytradekit.utils.time_handler import get_timestamp_ms, get_timestamp_s


class LbankClient:
    def __init__(self, logger, key=None, secret=None, account_id=None, is_swap=False):
        self.api_key = key
        self.secret_key = secret
        self.account_id = account_id
        self.session = requests.session()
        self.logger = logger
        if is_swap:
            self._url = LbankAuxiliary.swap_url.value
        else:
            self._url = LbankAuxiliary.url.value

    def sha512(self, data):
        H = hmac.new(key=bytes(self.secret_key, encoding='utf-8'), digestmod=hashlib.sha512)
        H.update(data.encode('utf-8'))
        return H.hexdigest()

    def _send_request(self, api, method=HttpMmthod.GET.name, params=None, use_sign=False):
        try:
            url = f'{self._url}{api}'
            response = requests.get(url=url, params=params)
            return response.json()
        except Exception as e:
            return e

    def get_ticker_24hr(self, symbol="all"):
        url = LbankAuxiliary.url_ticker.value
        params = {}
        params['symbol'] = symbol
        datas = self._send_request(url, params=params)
        return datas
    
    def get_ticker_price(self):
        url = LbankAuxiliary.url_ticker_price.value
        datas = self._send_request(url)
        return datas

    def get_exchange_information(self):
        url = LbankAuxiliary.url_exchange.value
        datas = self._send_request(url)
        return datas

    def get_orderbook(self, symbol, limit=100):
        params = {'symbol': symbol, 'size': limit}
        url = LbankAuxiliary.url_orderbook.value
        datas = self._send_request(url, method=HttpMmthod.POST.name, params=params)
        return datas
