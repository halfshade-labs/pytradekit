import hashlib
import hmac
import json
import requests
from pytradekit.utils import time_handler
from pytradekit.utils.dynamic_types import HttpMmthod, BitfinexAuxiliary


class BitfinexClient:
    def __init__(self, logger, key=None, secret=None, account_id=None):
        self.api_key = key
        self.secret_key = secret
        self.account_id = account_id
        self.session = requests.session()
        self.logger = logger

    def request(self, method, url, headers=None, params=None):
        try:
            headers = headers or {"accept": "application/json"}
            params = json.dumps(params) if params else {}
            if method == 'GET':
                response = self.session.get(url, headers=headers, params=params)
            else:
                response = self.session.post(url, headers=headers, data=params)

            if response.status_code != 200:
                self.logger.debug(f'Request failed:{response.json()}')
                return None
            result = response.json()
            return result
        except Exception as e:
            self.logger.exception(e)
            return e

    @staticmethod
    def get_timestamp():
        return time_handler.get_timestamp_ms()

    def _hashing(self, query_string):
        return hmac.new(self.secret_key.encode('utf-8'), query_string.encode('utf-8'), hashlib.sha384).hexdigest()

    def _make_public_url(self, url_path, params):
        url = BitfinexAuxiliary.url.value + url_path + params
        self.logger.debug(f"{url}")
        return url

    def _make_private_url(self, url_path, params=None):
        base_url = BitfinexAuxiliary.url_private.value
        nonce = self.get_timestamp() * time_handler.TimeConvert.S_TO_MS
        params = params or {}
        body = json.dumps(params) if params else ''
        signature = self._hashing(f'/api/{url_path}{nonce}{body}')
        url = f"{base_url}{url_path}"
        headers = {
            'bfx-nonce': str(nonce),
            'bfx-apikey': self.api_key,
            'bfx-signature': signature,
            'Content-Type': 'application/json'
        }
        self.logger.debug(f"Private URL: {url}")
        return url, headers

    def get_orderbook(self, symbol, limit=None):
        if symbol.startswith('APENFT'):
            symbol = symbol.replace('APENFT', 'APENFT:')
        if symbol.startswith('HTXDAO'):
            symbol = symbol.replace('HTXDAO', 'HTXDAO:')
        url_list = f'/t{symbol}/P0?len={limit}'
        url = self._make_public_url(url_path=BitfinexAuxiliary.url_orderbook.value,
                                    params=url_list)
        ob = self.request(HttpMmthod.GET.name, url)
        return ob

    def get_klines(self, symbol, interval):
        if symbol.startswith('APENFT'):
            symbol = symbol.replace('APENFT', 'APENFT:')
        if symbol.startswith('HTXDAO'):
            symbol = symbol.replace('HTXDAO', 'HTXDAO:')
        url_list = f'/trade%3A{interval}%3At{symbol}/hist'
        url = self._make_public_url(url_path=BitfinexAuxiliary.url_kline.value,
                                    params=url_list)
        kline = self.request(HttpMmthod.GET.name, url)
        return kline

    def get_balances(self):
        url, headers = self._make_private_url(url_path=BitfinexAuxiliary.url_balance.value, params={})
        balances = self.request(HttpMmthod.POST.name, url, headers=headers)
        return balances

    def get_deposit_history(self):
        url, headers = self._make_private_url(url_path=BitfinexAuxiliary.url_deposit.value, params={})
        balances = self.request(HttpMmthod.POST.name, url, headers=headers)
        return balances

    def get_transfer_history(self):
        url, headers = self._make_private_url(url_path=BitfinexAuxiliary.url_transfer.value, params={})
        balances = self.request(HttpMmthod.POST.name, url, headers=headers)
        return balances

    def get_ticker_24hr(self, symbol='ALL'):
        url = self._make_public_url(url_path=BitfinexAuxiliary.url_ticker.value, params=f'?symbols={symbol}')
        res = self.request(HttpMmthod.GET.name, url)
        return res

    def get_orders(self, start_time, end_time):
        params = {
            'start': start_time,
            'end': end_time,
            'limit': BitfinexAuxiliary.limit.value
        }
        url, headers = self._make_private_url(url_path=BitfinexAuxiliary.url_orders.value, params=params)
        res = self.request(HttpMmthod.POST.name, url, headers=headers, params=params)
        return res

    def get_trades(self, start_time, end_time):
        params = {
            'start': start_time,
            'end': end_time,
            'sort': 1,
            'limit': BitfinexAuxiliary.limit.value
        }
        url, headers = self._make_private_url(url_path=BitfinexAuxiliary.url_trades.value, params=params)
        res = self.request(HttpMmthod.POST.name, url, headers=headers, params=params)
        return res
